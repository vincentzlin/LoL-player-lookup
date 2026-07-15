"""Backfill item timing, champion level + gold curves from the lolesports feed.

Offline, best-effort enrichment. Run **after** ``backend.load_data``::

    python -m backend.enrich_lolesports                 # full backfill (~1-2h, resumable)
    python -m backend.enrich_lolesports --max-games 20  # quick partial slice
    python -m backend.enrich_lolesports --probe 115548128963037588   # one game, no DB

Two independent passes over the same game index, each with its own cache:

* **items** (default) — ``/details`` every 90s → ``item*_completed_s`` + ``level``.
* **gold** (``--gold``) — ``/window`` every 60s → ``meaningful_lead_pct``: the share of
  the game (5:00 → end) a champion held ≥5% more total gold than their lane opponent.

They are kept apart on purpose. The passes hit different endpoints (``/details`` reports
``totalGoldEarned``, not the ``totalGold`` the ratio needs), stop on different conditions,
and the item cache is already ~99% complete — so a unified pass would re-fetch tens of
thousands of calls for nothing, and a shared cache would let one pass clobber the other.

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
* ``lolesports_gold.json``       — per-champion 60s gold curves (versioned envelope)
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
# Gold curves live in their own cache, deliberately: the two passes hit different
# endpoints and neither should be able to clobber the other's data (see `run_gold`).
GOLD_CACHE = DATA_DIR / "lolesports_gold.json"
GOLD_CACHE_VERSION = 1

DEFAULT_WORKERS = 6
DEFAULT_STEP_S = 90
MAX_GAME_MINUTES = 45
_SAVE_EVERY = 25

GOLD_STEP_S = 60          # gold sampling interval; with the t=0 origin, index i == minute i
MAX_GOLD_MINUTES = 60     # safety net only — the real stop is gameState == "finished".
                          # 45 would truncate the 2.4% of games that run longer.
MEANINGFUL_LEAD_RATIO = 1.05    # "meaningful lead" = ≥5% more total gold than the lane opponent
MEANINGFUL_LEAD_FLOOR_MIN = 5   # ignore the first 5 min: 5% of tiny early gold is noise
                                # (at 2:00 a 5% lead is ~30 gold — one cannon minion)


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


def lane_opponents(meta: dict) -> dict[str, str]:
    """champion → its lane opponent's champion (same position, opposite side).

    Reads the cached index's ``players[].position``, which is already in Oracle codes
    (top/jng/mid/bot/sup) — so lane pairing needs no extra network calls. A position
    without exactly one champion on each side (feed role oddities) is omitted rather
    than guessed at.
    """
    by_side_pos: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for side, players in meta["players"].items():
        for p in players:
            by_side_pos[side][p["position"]].append(p["champion"])
    out: dict[str, str] = {}
    for side, other in (("Blue", "Red"), ("Red", "Blue")):
        for pos, mine in by_side_pos.get(side, {}).items():
            theirs = by_side_pos.get(other, {}).get(pos, [])
            if len(mine) == 1 and len(theirs) == 1:
                out[mine[0]] = theirs[0]
    return out


def extract_gold_series(meta: dict, snaps: list[dict]) -> dict[str, list[int]]:
    """Per-champion gold series from ``sample_game_gold`` output → ``{champion: [gold…]}``.

    Values are ordered by time on the uniform sampling grid, so index ``i`` is the sample
    at ``i * step_s`` seconds. A champion missing from some snapshots would end up with a
    short series whose indices no longer map to minutes, so ragged series are dropped
    rather than silently misread.
    """
    pid_to_champ = {p["participant_id"]: p["champion"]
                    for side in meta["players"].values() for p in side}
    out: dict[str, list[int]] = {}
    for snap in sorted(snaps, key=lambda s: s["ts"]):
        for pid, champ in pid_to_champ.items():
            g = snap["gold"].get(pid)
            if g is not None:
                out.setdefault(champ, []).append(g)
    n = max((len(v) for v in out.values()), default=0)
    return {c: v for c, v in out.items() if len(v) == n}


def meaningful_lead_pct(mine: list[int], theirs: list[int], *, step_s: int = GOLD_STEP_S,
                        ratio: float = MEANINGFUL_LEAD_RATIO,
                        floor_min: int = MEANINGFUL_LEAD_FLOOR_MIN) -> float | None:
    """% of samples in [``floor_min``, game end] where ``mine >= ratio * theirs``.

    Sampling is uniform at ``step_s``, so the fraction of samples ahead *is* the fraction
    of time ahead — no integration needed. The denominator is the measured window, not the
    whole game: a champion ahead from ``floor_min`` to the end reads 100%.

    Returns None when the window is empty (game ended before ``floor_min``, or a series is
    missing/short). Callers must store that as NULL, not 0 — "no data" and "never ahead"
    are different facts and must not collapse.
    """
    if not mine or not theirs:
        return None
    first = (floor_min * 60 + step_s - 1) // step_s   # first index at/after the floor
    n = min(len(mine), len(theirs))                   # tolerate a ragged tail sample
    window = [(mine[i], theirs[i]) for i in range(first, n)]
    if not window:
        return None
    # Opponent gold is never 0 after 5 min (everyone starts at 500), but guard anyway:
    # against 0 gold any positive gold is an infinite lead; 0-vs-0 is not a lead.
    ahead = sum(1 for m, t in window if (m >= ratio * t if t > 0 else m > 0))
    return round(100.0 * ahead / len(window), 1)


def game_lead_pcts(meta: dict, series: dict[str, list[int]], **kw) -> dict[str, float]:
    """champion → meaningful lead % for one game.

    Champions without a resolvable lane opponent, or without a usable window, are omitted.
    """
    opps = lane_opponents(meta)
    out: dict[str, float] = {}
    for champ, mine in series.items():
        opp = opps.get(champ)
        if opp is None or opp not in series:
            continue
        pct = meaningful_lead_pct(mine, series[opp], **kw)
        if pct is not None:
            out[champ] = pct
    return out


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


def _load_gold_cache() -> dict:
    """The gold cache envelope, or a fresh empty one if absent/stale.

    Unlike the bare item cache, this one is versioned: a stored series is only
    interpretable given ``step_s`` (index i ↔ minute i), so the file must be
    self-describing. A version or step change makes the index→minute mapping wrong, so we
    discard and refetch rather than silently misread.
    """
    raw = _load_json(GOLD_CACHE)
    if not raw or raw.get("version") != GOLD_CACHE_VERSION or raw.get("step_s") != GOLD_STEP_S:
        if raw:
            log.warning("gold cache version/step mismatch — starting fresh")
        return {"version": GOLD_CACHE_VERSION, "step_s": GOLD_STEP_S, "games": {}}
    return raw


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


def sample_game_gold(esports_game_id: str, start: dt.datetime, step_s: int = GOLD_STEP_S,
                     max_minutes: int = MAX_GOLD_MINUTES) -> list[dict]:
    """Sample ``/window`` every ``step_s`` from game start → game end.

    Unlike ``sample_game_frames`` (items) this must **not** early-stop — the point is a
    full-duration gold curve. It also can't share that function: different endpoint,
    step, stop rule and return shape.

    Stops on ``gameState == "finished"``. The feed serves a clamped final frame forever
    past the end of the game, so the falsy-frame check ``sample_game_frames`` uses would
    run to ``max_minutes`` on every game and pad the series with frozen end-of-game gold.
    ``max_minutes`` is a safety net only.
    """
    out: list[dict] = []
    cursor = start
    hard_stop = start + dt.timedelta(minutes=max_minutes)
    while cursor < hard_stop:
        snap = lolesports.window_frames_at(esports_game_id, cursor)
        if snap is None or snap.get("state") == "finished":
            # The finished frame is dropped, not appended: its timestamp is the true game
            # end, which is off the sampling grid (e.g. 26:00 → 27:12). A non-uniform
            # sample would break the "fraction of samples == fraction of time" property
            # the stat rests on. Losing ≤step_s at the tail is unbiased — both lane
            # opponents lose the same sample.
            break
        out.append(snap)
        cursor += dt.timedelta(seconds=step_s)
    return out


def gold_game(esports_game_id: str, meta: dict, step_s: int = GOLD_STEP_S) -> dict | None:
    """Fetch + reduce one game's gold curve → ``{champion: [gold @ 0, 1, 2… min]}``."""
    start = lolesports._parse_ts(meta["start"]) if isinstance(meta["start"], str) \
        else meta["start"]
    snaps = sample_game_gold(esports_game_id, start, step_s=step_s)
    return extract_gold_series(meta, snaps) if snaps else None


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


def apply_cached(session: Session) -> int:
    """Write cached enrichment onto the current DB rows. Returns #rows updated.

    Network-free: re-uses the two ``data/`` caches written by a prior ``run()`` to
    restore ``item*_completed_s`` + ``level`` after a ``load_data`` rebuild (which
    wipes the table). Resolves each game's lineup fingerprint to an esports game id,
    then applies the cached values. Safe no-op if either cache is missing.
    """
    index = _load_json(INDEX_CACHE)
    cache = _load_json(ENRICH_CACHE)
    if not index or not cache:
        return 0
    by_date = index_by_date(index.get("games", []))
    updated = 0
    for fp in oracle_fingerprints(session.query(PlayerGameStat).all()).values():
        esid = resolve_game(fp, by_date)
        if esid and esid in cache:
            updated += _apply(fp["by_champ"], cache[esid])
    session.commit()
    return updated


def _apply_gold(row_map: dict, meta: dict, series: dict) -> int:
    """Write meaningful_lead_pct onto the game's rows (matched by champion). Returns #rows.

    Separate from ``_apply`` on purpose: the gold cache has no item data, so this pass
    must never touch the item/level columns — and ``_apply`` must never touch gold.
    """
    n = 0
    for champ, pct in game_lead_pcts(meta, series).items():
        r = row_map.get(champ.lower())      # row_map is keyed lowercase; feed casing varies
        if r is None:
            continue
        r.meaningful_lead_pct = pct
        n += 1
    return n


def apply_cached_gold(session: Session) -> int:
    """Write the cached gold curves' lead % onto the current DB rows. Returns #rows.

    The gold-pass mirror of ``apply_cached``: network-free, re-applies after a
    ``load_data`` rebuild wipes the column. Safe no-op if either cache is missing.
    """
    index = _load_json(INDEX_CACHE)
    raw = _load_json(GOLD_CACHE)
    if not index or not raw or raw.get("version") != GOLD_CACHE_VERSION:
        return 0
    cache = raw.get("games", {})
    games = index.get("games", [])
    by_date = index_by_date(games)
    index_by_id = {rec["esports_game_id"]: rec for rec in games}
    updated = 0
    for fp in oracle_fingerprints(session.query(PlayerGameStat).all()).values():
        esid = resolve_game(fp, by_date)
        if esid and esid in cache and esid in index_by_id:
            updated += _apply_gold(fp["by_champ"], index_by_id[esid], cache[esid])
    session.commit()
    return updated


def run_gold(max_games: int | None = None, workers: int = DEFAULT_WORKERS,
             step_s: int = GOLD_STEP_S) -> None:
    """Backfill per-champion gold curves → meaningful_lead_pct (~45k calls, ~2-4h).

    A separate pass from ``run()``: the item cache is already ~99% complete, and a unified
    pass would re-fetch tens of thousands of ``/details`` calls to gain nothing. Reuses the
    index ``run()`` built. Resumable (skips games already in the gold cache).
    """
    engine = get_engine()
    init_db(engine)

    index = _load_json(INDEX_CACHE)
    if not index:
        log.error("no %s — run the item backfill first to build the index", INDEX_CACHE.name)
        return
    games = index.get("games", [])
    by_date = index_by_date(games)
    index_by_id = {rec["esports_game_id"]: rec for rec in games}
    envelope = _load_gold_cache()
    cache = envelope["games"]

    with Session(engine) as session:
        fps = oracle_fingerprints(session.query(PlayerGameStat).all())
        resolved = [(fp, esid) for fp in fps.values()
                    if (esid := resolve_game(fp, by_date))]
        log.info("resolved %d/%d games (%.1f%%)", len(resolved), len(fps),
                 100 * len(resolved) / max(len(fps), 1))

        updated = apply_cached_gold(session)      # free: anything already fetched
        to_fetch = [(fp, esid) for fp, esid in resolved
                    if esid not in cache and esid in index_by_id]
        cached_count = len(resolved) - len(to_fetch)    # before --max-games truncates
        if max_games:
            to_fetch = to_fetch[:max_games]
        log.info("gold: %d games already cached, %d to fetch (~%d calls)",
                 cached_count, len(to_fetch), len(to_fetch) * 33)

        done, t0 = 0, time.time()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(gold_game, esid, index_by_id[esid], step_s): (fp, esid)
                    for fp, esid in to_fetch}
            for fut in as_completed(futs):
                fp, esid = futs[fut]
                try:
                    series = fut.result()
                except Exception as exc:  # noqa: BLE001 - one game never aborts the run
                    log.warning("gold %s failed: %s", esid, exc)
                    series = None
                if series:
                    cache[esid] = series
                    updated += _apply_gold(fp["by_champ"], index_by_id[esid], series)
                done += 1
                if done % _SAVE_EVERY == 0:
                    session.commit()
                    _save_json(GOLD_CACHE, envelope)
                    rate = done / (time.time() - t0)
                    eta = (len(to_fetch) - done) / rate / 60 if rate else 0
                    log.info("  gold %d/%d games (%.2f/s, ETA %.0f min)",
                             done, len(to_fetch), rate, eta)
        session.commit()

    _save_json(GOLD_CACHE, envelope)
    log.info("gold done. updated %d player rows", updated)


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
        updated = apply_cached(session)
        to_fetch = [(gameid, fp, esid) for gameid, fp, esid in resolved
                    if esid not in cache and esid in index_by_id]
        cached_count = len(resolved) - len(to_fetch)
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


def probe_gold(esports_game_id: str, step_s: int = GOLD_STEP_S) -> None:
    """Fetch + print one game's gold curve + lead percentages (no DB write)."""
    meta = lolesports.window_metadata(esports_game_id)
    if not meta:
        log.error("no window data for %s", esports_game_id)
        return
    series = gold_game(esports_game_id, meta, step_s=step_s)
    if not series:
        log.error("no window frames for %s", esports_game_id)
        return
    pcts = game_lead_pcts(meta, series, step_s=step_s)
    n = len(next(iter(series.values())))
    print(f"game {esports_game_id} | {n} samples (~{n * step_s // 60} min) | step {step_s}s")
    for champ, opp in sorted(lane_opponents(meta).items()):
        g = series.get(champ, [])
        pct = pcts.get(champ)
        def at(i):
            return g[i] if len(g) > i else "—"
        print(f"  {champ:14s} vs {opp:14s} lead "
              f"{'  N/A' if pct is None else f'{pct:5.1f}%'}  "
              f"gold @5/15/end {at(5)}/{at(15)}/{g[-1] if g else '—'}")


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
    ap.add_argument("--gold", action="store_true",
                    help="run the gold-curve backfill (meaningful_lead_pct) instead of "
                         "the item/level one (~2-4h); combines with --probe/--max-games")
    ap.add_argument("--gold-step-seconds", type=int, default=GOLD_STEP_S,
                    help=f"gold sampling interval (default {GOLD_STEP_S}s)")
    args = ap.parse_args()

    if args.probe and args.gold:
        probe_gold(args.probe, step_s=args.gold_step_seconds)
    elif args.probe:
        probe(args.probe, step_s=args.step_seconds)
    elif args.gold:
        run_gold(max_games=args.max_games, workers=args.workers,
                 step_s=args.gold_step_seconds)
    else:
        run(refresh_index=args.refresh_index, limit_index=args.limit_index,
            max_games=args.max_games, workers=args.workers, step_s=args.step_seconds)
