"""Champion draft graph model (VIN-20).

A graph over champions learned from LCK pro games:

* nodes  = champions
* synergy edges = how much a champion *pair on the same team* wins together
* counter edges = how much one champion's side beats another champion's side

Edge weights are **skill-adjusted**: instead of crediting a win as a flat +1, each
game contributes an *expected-win-rate margin* ``actual − expected``. The expected
score comes from a lightweight team rating derived from each team's win rate, so a
win over a much weaker team counts for less than an upset (the VIN-20 requirement).

Edges are built lazily and cached in-memory per ``(engine, season, split)`` — LCK
data is small, so a single pass is cheap and the cache avoids recomputing per request.
"""
import math
from collections import defaultdict
from typing import NamedTuple

from sqlalchemy.orm import Session

from backend.config import SEASONS, ROLE_LABELS
from backend.database import PlayerGameStat
from backend.champions import image_url

# ── Tunable constants ─────────────────────────────────────────────────────────
K_SHRINK = 6.0          # pulls sparse pairs toward 0 (essential for small samples)
MIN_TEAM_GAMES = 5      # below this a team gets the average rating (0)
MIN_EDGE_GAMES = 3      # a synergy/counter edge needs this many shared games to show
P_CLAMP = (0.05, 0.95)  # clamp win rate before the logit, to avoid infinities
TOP_N = 8               # default ranking length per category
DURATION_MINUTES = [25, 30, 35]   # cumulative ">N min" win-rate splits

_ROLES = ["top", "jng", "mid", "bot", "sup"]


class GameRec(NamedTuple):
    """One game from a focal champion's perspective (in a given role)."""
    margin: float                  # skill-adjusted actual − expected (focal side)
    won: bool
    dur_s: int | None              # game length in seconds
    dragons: int | None            # focal team's dragon total
    gd15: float | None             # focal champion's gold diff @15 (nullable)
    gd25: float | None             # focal champion's gold diff @25 (null <25min)
    teammates: frozenset           # other champions on the focal side
    opponents: frozenset           # champions on the opposing side

# (id(engine), season, split) -> built edge dict
_edge_cache: dict = {}


# ── Team strength (skill-gap adjustment) ──────────────────────────────────────

def expected_score(rating_a: float, rating_b: float) -> float:
    """Elo expected score for team A vs team B (0..1)."""
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def _rating_from_winrate(wins: int, games: int) -> float:
    """Win rate -> Elo-style rating; average (0) when under MIN_TEAM_GAMES."""
    if games < MIN_TEAM_GAMES:
        return 0.0
    p = min(max(wins / games, P_CLAMP[0]), P_CLAMP[1])
    return 400.0 * math.log10(p / (1.0 - p))


def _team_ratings(by_game: dict) -> dict[str, float]:
    wins: dict[str, int] = defaultdict(int)
    games: dict[str, int] = defaultdict(int)
    for sides in by_game.values():
        for rows in sides.values():
            team, res = rows[0].teamname, rows[0].result
            if team is None or res is None:
                continue
            games[team] += 1
            if res == "Win":
                wins[team] += 1
    return {t: _rating_from_winrate(wins[t], n) for t, n in games.items()}


# ── Data access / grouping ────────────────────────────────────────────────────

def _timeframe_year(season: int | None) -> int | None:
    if season is None:
        return None
    return next((y for y, s in SEASONS.items() if s == season), None)


def _rows(session: Session, season, split) -> list[PlayerGameStat]:
    q = session.query(PlayerGameStat).filter(PlayerGameStat.position.in_(_ROLES))
    year = _timeframe_year(season)
    if season is not None and year is not None:
        q = q.filter(PlayerGameStat.year == year)
    if split:
        q = q.filter(PlayerGameStat.split == split)
    return q.all()


def _group_games(rows: list[PlayerGameStat]) -> dict:
    """gameid -> {side -> [rows]}, keeping only rows with a champion + side."""
    by_game: dict = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if not r.gameid or not r.side or not r.champion:
            continue
        by_game[r.gameid][r.side].append(r)
    return by_game


# ── Edge learning ─────────────────────────────────────────────────────────────

def _build(session: Session, season, split) -> dict:
    rows = _rows(session, season, split)
    by_game = _group_games(rows)
    ratings = _team_ratings(by_game)

    # One GameRec per (focal champion, role) appearance, so the champion view can
    # filter by role and recompute any stat over an arbitrary subset of games.
    records: dict = defaultdict(list)   # (champ, role) -> [GameRec, ...]
    meta: dict[str, dict] = {}

    for r in rows:
        if r.champion and r.champion not in meta:
            meta[r.champion] = {
                "champion": r.champion,
                "champion_ddragon": r.champion_ddragon or "",
                "image_url": image_url(r.champion),
            }

    for sides in by_game.values():
        if len(sides) != 2:
            continue
        (rows_a, rows_b) = list(sides.values())
        ta, tb = rows_a[0].teamname, rows_b[0].teamname
        res_a = rows_a[0].result
        if res_a is None:
            continue
        margin_a = (1.0 if res_a == "Win" else 0.0) - expected_score(
            ratings.get(ta, 0.0), ratings.get(tb, 0.0))
        won_a = res_a == "Win"

        for mine, theirs, margin, won in ((rows_a, rows_b, margin_a, won_a),
                                          (rows_b, rows_a, -margin_a, not won_a)):
            champs = frozenset(r.champion for r in mine if r.champion)
            opponents = frozenset(r.champion for r in theirs if r.champion)
            for r in mine:
                if not r.champion or not r.position:
                    continue
                records[(r.champion, r.position)].append(GameRec(
                    margin=margin, won=won, dur_s=r.gamelength_s, dragons=r.dragons,
                    gd15=r.golddiffat15, gd25=r.golddiffat25,
                    teammates=champs - {r.champion}, opponents=opponents))

    return {"records": records, "meta": meta}


def build_edges(session: Session, season=None, split=None) -> dict:
    """Built (and cached) edge maps for a timeframe."""
    key = (id(session.get_bind()), season, split)
    if key not in _edge_cache:
        _edge_cache[key] = _build(session, season, split)
    return _edge_cache[key]


def clear_cache() -> None:
    _edge_cache.clear()


def _weight(total: float, n: int) -> float:
    """Shrunk edge weight as a win-margin percentage."""
    return round(total / (n + K_SHRINK) * 100, 1)


# ── Public queries ────────────────────────────────────────────────────────────

def list_champions(session: Session) -> list[dict]:
    """Every distinct LCK champion, with image URL, alphabetically."""
    rows = (session.query(PlayerGameStat.champion, PlayerGameStat.champion_ddragon)
            .filter(PlayerGameStat.champion.isnot(None))
            .filter(PlayerGameStat.position.in_(_ROLES))
            .distinct().all())
    seen: dict[str, dict] = {}
    for champ, ddragon in rows:
        if champ and champ not in seen:
            seen[champ] = {"champion": champ,
                           "champion_ddragon": ddragon or "",
                           "image_url": image_url(champ)}
    return [seen[c] for c in sorted(seen)]


def canonical_champion(session: Session, name: str) -> str | None:
    """Resolve a champion name case-insensitively to its stored spelling."""
    low = (name or "").strip().lower()
    for c in list_champions(session):
        if c["champion"].lower() == low:
            return c["champion"]
    return None


def _win_rates(games: int, wins: int, sum_margin: float) -> tuple:
    """(raw win %, skill-adjusted win %) or (None, None) when no games."""
    if not games:
        return None, None
    raw = round(wins / games * 100, 1)
    adjusted = round(min(max(0.5 + sum_margin / games, 0.0), 1.0) * 100, 1)
    return raw, adjusted


def _dragon_bucket(d: int | None) -> str | None:
    if d is None:
        return None
    return "4+" if d >= 4 else str(d)


_DRAGON_ORDER = ["0", "1", "2", "3", "4+"]


def _split(recs: list[GameRec]) -> dict:
    """Win-rate block for a list of GameRecs (games, win rate, adjusted, …)."""
    games = len(recs)
    wins = sum(1 for r in recs if r.won)
    margin = sum(r.margin for r in recs)
    win_rate, adjusted = _win_rates(games, wins, margin)
    return {"games": games, "win_rate": win_rate, "adjusted_win_rate": adjusted}


def _throwing(recs: list[GameRec]) -> dict:
    """Lead-swing metrics: how the gold diff moves from 15 → 25 minutes.

    swing = GD@25 − GD@15. ``throwing_factor`` = −mean(swing) (high = loses leads).
    A 'throw' is a game the champion led at 15 (GD@15 > 0) yet the lead shrank by 25.
    Only games that reached 25 min (both diffs present) are counted.
    """
    swings = [(r.gd15, r.gd25) for r in recs if r.gd15 is not None and r.gd25 is not None]
    if not swings:
        return {"swing_games": 0, "avg_swing": None, "throw_gold_pg": None,
                "throwing_factor": None, "throw_count": 0, "throw_rate": None,
                "avg_throw_size": None}
    avg_swing = sum(g25 - g15 for g15, g25 in swings) / len(swings)
    throws = [g15 - g25 for g15, g25 in swings if g15 > 0 and g25 < g15]
    return {
        "swing_games": len(swings),
        "avg_swing": round(avg_swing, 1),
        # Gold of leads surrendered per game played (denominator = ALL games, so
        # closing out before 25 min lowers it). The 0–100 index is set by the caller.
        "throw_gold_pg": round(sum(throws) / len(recs), 1),
        "throwing_factor": None,
        "throw_count": len(throws),
        "throw_rate": round(len(throws) / len(swings) * 100, 1),
        "avg_throw_size": round(sum(throws) / len(throws), 1) if throws else None,
    }


MIN_INDEX_GAMES = 3   # min games for a champion to enter the throwing-index peer set


def _raw_throw_gpg(recs: list[GameRec]) -> float | None:
    """Gold of leads surrendered per game played (all games as denominator)."""
    if not recs:
        return None
    thrown = sum(r.gd15 - r.gd25 for r in recs
                 if r.gd15 is not None and r.gd25 is not None and r.gd15 > 0 and r.gd25 < r.gd15)
    return thrown / len(recs)


def _peer_raw_values(records: dict, roles: set) -> list[float]:
    """Each LCK champion's gold-thrown-per-game, over the in-scope roles."""
    by_champ: dict = defaultdict(list)
    for (champ, role), recs in records.items():
        if role in roles:
            by_champ[champ].extend(recs)
    return [_raw_throw_gpg(recs) for recs in by_champ.values()
            if len(recs) >= MIN_INDEX_GAMES]


def _throw_index(raw: float | None, peers: list[float]) -> float | None:
    """Mid-rank percentile of `raw` among `peers` (0–100, higher = throwier)."""
    if raw is None or len(peers) < 3:
        return None
    below = sum(1 for v in peers if v < raw)
    ties = sum(1 for v in peers if v == raw)
    return round(100 * (below + 0.5 * ties) / len(peers), 1)


def _aggregate(recs: list[GameRec]) -> dict:
    """Full champion stat block (overall + duration/dragon splits + GD@15)."""
    gd = [r.gd15 for r in recs if r.gd15 is not None]
    gd15 = round(sum(gd) / len(gd), 1) if gd else None

    duration = []
    for mins in DURATION_MINUTES:
        sub = [r for r in recs if r.dur_s is not None and r.dur_s > mins * 60]
        duration.append({"min_minutes": mins, **_split(sub)})

    by_bucket: dict = defaultdict(list)
    for r in recs:
        b = _dragon_bucket(r.dragons)
        if b is not None:
            by_bucket[b].append(r)
    dragons = [{"bucket": b, **_split(by_bucket[b])}
               for b in _DRAGON_ORDER if b in by_bucket]

    return {**_split(recs), "gd15": gd15, **_throwing(recs),
            "duration_splits": duration, "dragon_splits": dragons}


def _ranked_edges(recs: list[GameRec], attr: str, meta: dict, top_n: int) -> list[dict]:
    """Best+worst ranked edges over `recs`, grouping by each teammate/opponent.

    ``attr`` is "teammates" or "opponents". Drops pairs below MIN_EDGE_GAMES, then
    keeps the top_n best and top_n worst by shrunk win-margin weight.
    """
    agg: dict = defaultdict(lambda: [0.0, 0])
    for r in recs:
        for other in getattr(r, attr):
            agg[other][0] += r.margin
            agg[other][1] += 1
    out = []
    for other, (total, n) in agg.items():
        if n < MIN_EDGE_GAMES or other not in meta:
            continue
        out.append({**meta[other], "weight": _weight(total, n), "games": n})
    out.sort(key=lambda e: e["weight"], reverse=True)
    if len(out) > 2 * top_n:
        out = out[:top_n] + out[-top_n:]
    return out


def _select_records(edges: dict, champ: str, role: str | None) -> tuple:
    """(selected GameRecs, resolved role or None, roles-summary list)."""
    records = edges["records"]
    champ_roles = [r for r in _ROLES if (champ, r) in records]
    roles_summary = [{"role": r, "role_label": ROLE_LABELS.get(r, r),
                      **_split(records[(champ, r)])} for r in champ_roles]
    sel_role = role if role in champ_roles else None
    use_roles = [sel_role] if sel_role else champ_roles
    recs = [rec for r in use_roles for rec in records.get((champ, r), [])]
    return recs, sel_role, roles_summary


def champion_graph(session: Session, champ: str, season=None, split=None,
                   role: str | None = None, top_n: int = TOP_N) -> dict:
    """Stats + ranked synergies/counters for one champion, optionally by role.

    ``roles`` summarises each role the champion was played in. When ``role`` is given
    only that role's games are used; otherwise all roles are merged. ``synergies`` and
    ``counters`` only include pairs with at least ``MIN_EDGE_GAMES`` shared games.
    """
    edges = build_edges(session, season, split)
    meta = edges["meta"]
    recs, sel_role, roles_summary = _select_records(edges, champ, role)

    stats = _aggregate(recs)
    peers = _peer_raw_values(edges["records"], {sel_role} if sel_role else set(_ROLES))
    stats["throwing_factor"] = _throw_index(stats["throw_gold_pg"], peers)

    self_meta = meta.get(champ, {"champion": champ, "champion_ddragon": "",
                                 "image_url": image_url(champ)})
    return {**self_meta, "season": season, "split": split,
            "role": sel_role, "roles": roles_summary,
            "stats": stats,
            "synergies": _ranked_edges(recs, "teammates", meta, top_n),
            "counters": _ranked_edges(recs, "opponents", meta, top_n)}


def champion_pairing(session: Session, champ: str, other: str, kind: str,
                     season=None, split=None, role: str | None = None) -> dict:
    """Recompute the champion's full stat block over games where `other` co-occurs.

    ``kind`` is "synergy" (other is a teammate) or "counter" (other is an opponent).
    Returns both the with-pairing ``stats`` and the champion's ``overall`` block.
    """
    edges = build_edges(session, season, split)
    meta = edges["meta"]
    recs, sel_role, _ = _select_records(edges, champ, role)

    attr = "teammates" if kind == "synergy" else "opponents"
    subset = [r for r in recs if other in getattr(r, attr)]

    stats, overall = _aggregate(subset), _aggregate(recs)
    peers = _peer_raw_values(edges["records"], {sel_role} if sel_role else set(_ROLES))
    stats["throwing_factor"] = _throw_index(stats["throw_gold_pg"], peers)
    overall["throwing_factor"] = _throw_index(overall["throw_gold_pg"], peers)

    self_meta = meta.get(champ, {"champion": champ, "champion_ddragon": "",
                                 "image_url": image_url(champ)})
    other_meta = meta.get(other, {"champion": other, "champion_ddragon": "",
                                  "image_url": image_url(other)})
    return {**self_meta, "other": other_meta, "kind": kind,
            "season": season, "split": split, "role": sel_role,
            "stats": stats, "overall": overall}
