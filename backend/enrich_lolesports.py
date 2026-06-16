"""Backfill item-completion timing + champion level from the lolesports feed.

Offline, best-effort enrichment. Run **after** ``backend.load_data``::

    python -m backend.enrich_lolesports                 # full backfill (~1-2h, resumable)
    python -m backend.enrich_lolesports --max-games 20  # quick partial slice
    python -m backend.enrich_lolesports --probe 115548128963037588   # one game, no DB

Because Oracle's ``gameid`` (``ESPORTSTMNT01_…``) has no public crosswalk to the
lolesports ``int64`` game id, games are matched on a **champion-lineup fingerprint +
date**: pro drafts pick each champion at most once per game, so the set of 10
``champion_ddragon`` ids on a date is effectively a unique key, and the feed reports
champions with the same Data-Dragon ids the loader already stores.

Cost model (measured): a ``/details`` call returns only ~10s of game time, so dense
paging is ~150 calls/game. Instead we **coarse-sample** ``/details`` every
``step_s`` seconds (~22 calls/game, item timing accurate to ~±step_s), fetch games
**concurrently**, and persist **incrementally** so the run is resumable and the site
updates as it goes. Two caches under ``data/`` survive a ``load_data`` rebuild:

* ``lolesports_index.json``      — discovered games + lineups (the ``window`` crawl)
* ``lolesports_enrichment.json`` — extracted per-game values, keyed by esports game id
"""
import argparse
import datetime as dt
import json
import logging
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.orm import Session

from backend.config import DATA_DIR, SEASONS
from backend.database import get_engine, init_db, PlayerGameStat
from backend import lolesports, items

logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")
log = logging.getLogger(__name__)

INDEX_CACHE = DATA_DIR / "lolesports_index.json"
ENRICH_CACHE = DATA_DIR / "lolesports_enrichment.json"

DEFAULT_WORKERS = 6
DEFAULT_STEP_S = 90
MAX_GAME_MINUTES = 45
_SAVE_EVERY = 25


# ── Pure helpers (unit-tested, no network/DB) ────────────────────────────────

def _date_of(value: str | None) -> dt.date | None:
    """Oracle date string or rfc timestamp → a ``date`` (first 10 chars)."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def oracle_fingerprints(rows: list[PlayerGameStat]) -> dict[str, dict]:
    """Group DB rows by gameid → ``{date, champs:Counter, by_champ:{champ:row}}``.

    ``champs`` is the multiset of all ``champion_ddragon`` ids in the game (the join
    key); ``by_champ`` maps each champion id back to its row for write-back.
    """
    games: dict[str, dict] = {}
    for r in rows:
        if not r.gameid or not r.champion_ddragon:
            continue
        g = games.setdefault(r.gameid, {"date": _date_of(r.date),
                                        "champs": Counter(), "by_champ": {}})
        # Lowercase the join key: Data Dragon's id is "Fiddlesticks" but the feed
        # sends "FiddleSticks". by_champ is keyed lowercase too (see _apply).
        key = r.champion_ddragon.lower()
        g["champs"][key] += 1
        g["by_champ"][key] = r
    return games


def index_by_date(index: list[dict]) -> dict[dt.date, list[dict]]:
    """Bucket lolesports index records by their game-start date."""
    out: dict[dt.date, list[dict]] = defaultdict(list)
    for rec in index:
        d = _date_of(rec.get("start"))
        if d is not None:
            out[d].append(rec)
    return out


def _record_champs(rec: dict) -> Counter:
    sides = rec.get("sides", {})
    return Counter([c.lower() for side in sides.values() for c in side if c])


def resolve_game(fp: dict, by_date: dict[dt.date, list[dict]]) -> str | None:
    """Find the esports game id whose lineup matches an Oracle fingerprint.

    Match = identical 10-champion multiset on a date within ±1 day (timezone slack).
    Returns the esports game id, or ``None`` if there isn't exactly one match.
    """
    d = fp["date"]
    if d is None:
        return None
    candidates: list[dict] = []
    for delta in (0, -1, 1):
        candidates.extend(by_date.get(d + dt.timedelta(days=delta), []))
    hits = [rec["esports_game_id"] for rec in candidates
            if _record_champs(rec) == fp["champs"]]
    # Unique match only — ambiguity (rare mirror of the same 10 champs same day) is
    # left unresolved rather than risk writing the wrong game's data.
    return hits[0] if len(set(hits)) == 1 else None


def all_have_three_items(frame: dict, completed_ids: set[int],
                         seen: dict[int, set[int]]) -> bool:
    """Update ``seen`` with a frame's completed items; True once every pid has ≥3.

    ``seen`` maps participantId → set of completed item ids observed so far. Used to
    early-stop coarse sampling when every player has finished 3 legendary items.
    """
    for p in frame.get("participants", []):
        pid = p.get("participantId")
        for iid in p.get("items", []) or []:
            if iid in completed_ids:
                seen.setdefault(pid, set()).add(iid)
    return bool(seen) and all(len(v) >= 3 for v in seen.values()) and len(seen) >= 10


def extract_game(meta: dict, frames: list[dict], completed_ids: set[int]) -> dict:
    """Per-champion item timings + final level for one game.

    Returns ``{champion_key: {item1_completed_s, item2_completed_s,
    item3_completed_s, level}}``. Item timings are seconds from game start; level is
    the max seen across frames.
    """
    start = meta["start"] if isinstance(meta["start"], dt.datetime) \
        else lolesports._parse_ts(meta["start"])
    pid_to_champ = {p["participant_id"]: p["champion"]
                    for side in meta["players"].values() for p in side}

    seen: dict[int, set[int]] = defaultdict(set)
    timings: dict[int, list[int]] = defaultdict(list)
    levels: dict[int, int] = {}

    for f in sorted(frames, key=lambda f: f["ts"]):
        rel = int((lolesports._parse_ts(f["ts"]) - start).total_seconds())
        for p in f.get("participants", []):
            pid = p.get("participantId")
            lvl = p.get("level")
            if lvl is not None:
                levels[pid] = max(levels.get(pid, 0), lvl)
            if len(timings[pid]) >= 3:
                continue
            for iid in p.get("items", []) or []:
                if iid in completed_ids and iid not in seen[pid]:
                    seen[pid].add(iid)
                    timings[pid].append(rel)
                    if len(timings[pid]) >= 3:
                        break

    out: dict[str, dict] = {}
    for pid, champ in pid_to_champ.items():
        t = timings.get(pid, [])
        out[champ] = {
            "item1_completed_s": t[0] if len(t) > 0 else None,
            "item2_completed_s": t[1] if len(t) > 1 else None,
            "item3_completed_s": t[2] if len(t) > 2 else None,
            "level": levels.get(pid),
        }
    return out


# ── Cache I/O ────────────────────────────────────────────────────────────────

def _load_json(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _save_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)               # atomic: a Ctrl-C never leaves a half-written cache


# ── Network: per-game sampling + extraction ──────────────────────────────────

def sample_game_frames(esports_game_id: str, start: dt.datetime, completed_ids: set[int],
                       step_s: int = DEFAULT_STEP_S, max_minutes: int = MAX_GAME_MINUTES,
                       ) -> list[dict]:
    """Coarsely sample ``/details`` from game start → end (one snapshot per ``step_s``).

    Stops early once every participant holds ≥3 completed items, or when a snapshot
    comes back empty (game over), or at ``max_minutes``.
    """
    frames: list[dict] = []
    seen: dict[int, set[int]] = {}
    cursor = start
    hard_stop = start + dt.timedelta(minutes=max_minutes)
    while cursor < hard_stop:
        frame = lolesports.details_at(esports_game_id, cursor)
        if frame is None:
            break
        frames.append(frame)
        if all_have_three_items(frame, completed_ids, seen):
            break
        cursor += dt.timedelta(seconds=step_s)
    return frames


def enrich_game(esports_game_id: str, meta: dict, step_s: int = DEFAULT_STEP_S) -> dict | None:
    """Fetch + extract one game's item timings + levels (no cache, no DB)."""
    start = lolesports._parse_ts(meta["start"]) if isinstance(meta["start"], str) \
        else meta["start"]
    completed = items.load_completed_ids(meta.get("patch"))
    frames = sample_game_frames(esports_game_id, start, completed, step_s=step_s)
    if not frames:
        return None
    return extract_game(meta, frames, completed)


# ── Network: index crawl (resumable + concurrent) ────────────────────────────

def _match_games_meta(match_id: str) -> list[dict]:
    """All completed games of a match → window metadata records (worker task)."""
    recs: list[dict] = []
    for g in lolesports.event_games(match_id):
        if g.get("state") != "completed":
            continue
        meta = lolesports.window_metadata(g["id"])
        if not meta:
            continue
        meta = dict(meta)
        meta["start"] = meta["start"].isoformat()      # JSON-friendly
        recs.append(meta)
    return recs


def build_index(workers: int = DEFAULT_WORKERS, limit_matches: int | None = None,
                refresh: bool = False) -> list[dict]:
    """Crawl the LCK schedule → per-game ``window`` metadata.

    Resumable + concurrent + incrementally saved: completed ``match_id``s are recorded
    so a re-run skips them; the index is flushed to disk every ``_SAVE_EVERY`` matches.
    """
    cached = None if refresh else _load_json(INDEX_CACHE)
    games: list[dict] = (cached or {}).get("games", []) if cached else []
    done: set[str] = set((cached or {}).get("done_matches", [])) if cached else set()
    if cached:
        log.info("resuming index: %d games, %d matches already done", len(games), len(done))

    wanted = set(SEASONS)
    todo: list[str] = []
    # The schedule is paginated (ascending within a page, pages descending), so an
    # out-of-range event can be emitted *before* all in-range ones are seen. We must
    # `continue` past out-of-range years (not `break`) and let the generator exhaust,
    # or we'd drop the oldest in-range matches (e.g. Jan/Feb 2024). The crawl is only
    # ~8 cheap schedule pages; expensive `window` calls stay gated to `wanted` years.
    for start_time, match_id in lolesports.iter_completed_matches():
        year = int(start_time[:4]) if start_time else None
        if year is None or year not in wanted or match_id in done:
            continue
        todo.append(match_id)
        if limit_matches and len(todo) >= limit_matches:
            break
    log.info("index: %d new matches to crawl (%d workers)", len(todo), workers)

    processed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_match_games_meta, mid): mid for mid in todo}
        for fut in as_completed(futures):
            mid = futures[fut]
            try:
                games.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 - one bad match shouldn't abort
                log.warning("event %s failed: %s", mid, exc)
            done.add(mid)
            processed += 1
            if processed % _SAVE_EVERY == 0:
                _save_json(INDEX_CACHE, {"games": games, "done_matches": sorted(done)})
                log.info("  indexed %d/%d matches (%d games)…", processed, len(todo), len(games))

    _save_json(INDEX_CACHE, {"games": games, "done_matches": sorted(done)})
    log.info("index ready: %d games", len(games))
    return games


# ── Orchestration ────────────────────────────────────────────────────────────

def _apply(row_map: dict, extracted: dict) -> int:
    """Write extracted values onto the game's rows (matched by champion). Returns #rows.

    ``row_map`` (``fp['by_champ']``) is keyed by lowercased champion id; ``extracted``
    is keyed by the feed's champion casing — so match case-insensitively.
    """
    n = 0
    for champ, vals in extracted.items():
        r = row_map.get(champ.lower())
        if r is None:
            continue
        r.item1_completed_s = vals["item1_completed_s"]
        r.item2_completed_s = vals["item2_completed_s"]
        r.item3_completed_s = vals["item3_completed_s"]
        r.level = vals["level"]
        n += 1
    return n


def run(refresh_index: bool = False, limit_index: int | None = None,
        max_games: int | None = None, workers: int = DEFAULT_WORKERS,
        step_s: int = DEFAULT_STEP_S) -> None:
    engine = get_engine()
    init_db(engine)

    index = build_index(workers=workers, limit_matches=limit_index, refresh=refresh_index)
    by_date = index_by_date(index)
    index_by_id = {rec["esports_game_id"]: rec for rec in index}
    cache: dict = _load_json(ENRICH_CACHE) or {}

    with Session(engine) as session:
        fps = oracle_fingerprints(session.query(PlayerGameStat).all())
        log.info("DB has %d distinct games to resolve", len(fps))

        # Resolve first (cheap, in-memory), so we know the real workload.
        resolved: list[tuple[str, dict, str]] = []   # (gameid, fp, esports_id)
        unresolved = 0
        for gameid, fp in fps.items():
            esid = resolve_game(fp, by_date)
            if esid:
                resolved.append((gameid, fp, esid))
            else:
                unresolved += 1
        log.info("resolved %d/%d games (%.1f%%); enriching…",
                 len(resolved), len(fps), 100 * len(resolved) / max(len(fps), 1))

        # Apply anything already in the cache up front (free), then fetch the rest.
        to_fetch = []
        updated = cached_count = 0
        for gameid, fp, esid in resolved:
            if esid in cache:
                updated += _apply(fp["by_champ"], cache[esid])
                cached_count += 1
            elif esid in index_by_id:
                to_fetch.append((gameid, fp, esid))
        session.commit()
        if max_games:
            to_fetch = to_fetch[:max_games]
        log.info("%d games already cached, %d to fetch", cached_count, len(to_fetch))

        done = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(enrich_game, esid, index_by_id[esid], step_s): (gameid, fp, esid)
                    for gameid, fp, esid in to_fetch}
            for fut in as_completed(futs):
                gameid, fp, esid = futs[fut]
                try:
                    extracted = fut.result()
                except Exception as exc:  # noqa: BLE001 - never let one game abort the run
                    log.warning("enrich %s failed: %s", esid, exc)
                    extracted = None
                if extracted:
                    cache[esid] = extracted
                    updated += _apply(fp["by_champ"], extracted)
                done += 1
                if done % _SAVE_EVERY == 0:
                    session.commit()
                    _save_json(ENRICH_CACHE, cache)
                    rate = done / (time.time() - t0)
                    eta = (len(to_fetch) - done) / rate / 60 if rate else 0
                    log.info("  enriched %d/%d games (%.1f/s, ETA %.0f min)",
                             done, len(to_fetch), rate, eta)
        session.commit()

    _save_json(ENRICH_CACHE, cache)
    log.info("done. updated %d player rows (%d games unresolved → left N/A)",
             updated, unresolved)


def probe(esports_game_id: str, step_s: int = DEFAULT_STEP_S) -> None:
    """Fetch + extract one game and print results (no DB write)."""
    meta = lolesports.window_metadata(esports_game_id)
    if not meta:
        log.error("no window data for %s", esports_game_id)
        return
    extracted = enrich_game(esports_game_id, meta, step_s=step_s)
    if not extracted:
        log.error("no details frames for %s", esports_game_id)
        return
    print(f"game {esports_game_id} | patch {meta.get('patch')} | step {step_s}s")
    for champ, v in extracted.items():
        def mmss(s):
            return "N/A" if s is None else f"{s // 60}:{s % 60:02d}"
        print(f"  {champ:14s} lvl {v['level']}  items "
              f"{mmss(v['item1_completed_s'])} / {mmss(v['item2_completed_s'])} / "
              f"{mmss(v['item3_completed_s'])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh-index", action="store_true",
                    help="rebuild the lolesports game index from scratch")
    ap.add_argument("--limit-index", type=int, default=None,
                    help="cap matches scanned when building the index (testing)")
    ap.add_argument("--max-games", type=int, default=None,
                    help="stop after enriching this many games (testing / partial run)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"concurrent fetch workers (default {DEFAULT_WORKERS})")
    ap.add_argument("--step-seconds", type=int, default=DEFAULT_STEP_S,
                    help=f"item sampling interval (default {DEFAULT_STEP_S}s)")
    ap.add_argument("--probe", metavar="ESPORTS_GAME_ID",
                    help="fetch + print one game's timings/levels, no DB write")
    args = ap.parse_args()

    if args.probe:
        probe(args.probe, step_s=args.step_seconds)
    else:
        run(refresh_index=args.refresh_index, limit_index=args.limit_index,
            max_games=args.max_games, workers=args.workers, step_s=args.step_seconds)
