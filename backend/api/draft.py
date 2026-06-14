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
from itertools import combinations

from sqlalchemy.orm import Session

from backend.config import SEASONS
from backend.database import PlayerGameStat
from backend.champions import image_url

# ── Tunable constants ─────────────────────────────────────────────────────────
K_SHRINK = 6.0          # pulls sparse pairs toward 0 (essential for small samples)
MIN_TEAM_GAMES = 5      # below this a team gets the average rating (0)
P_CLAMP = (0.05, 0.95)  # clamp win rate before the logit, to avoid infinities
TOP_N = 8               # default ranking length per category

_ROLES = ["top", "jng", "mid", "bot", "sup"]

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

    syn_sum: dict = defaultdict(float)   # frozenset({a, b}) -> Σ margin
    syn_n: dict = defaultdict(int)
    cnt_sum: dict = defaultdict(float)   # (a, b) -> Σ A-side margin (a vs b)
    cnt_n: dict = defaultdict(int)
    champ_games: dict = defaultdict(lambda: [0, 0, 0.0])  # champ -> [games, wins, Σmargin]
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
        margin_b = -margin_a

        champs_a = sorted({r.champion for r in rows_a})
        champs_b = sorted({r.champion for r in rows_b})
        won_a = res_a == "Win"

        # Per-champion win rate + adjusted (margin) totals.
        for champs, margin, won in ((champs_a, margin_a, won_a),
                                    (champs_b, margin_b, not won_a)):
            for c in champs:
                rec = champ_games[c]
                rec[0] += 1
                rec[1] += 1 if won else 0
                rec[2] += margin

        # Synergy: unordered same-side pairs.
        for champs, margin in ((champs_a, margin_a), (champs_b, margin_b)):
            for c1, c2 in combinations(champs, 2):
                key = frozenset((c1, c2))
                syn_sum[key] += margin
                syn_n[key] += 1

        # Counter: directional cross-side pairs (a's perspective is margin_a).
        for a in champs_a:
            for b in champs_b:
                cnt_sum[(a, b)] += margin_a
                cnt_n[(a, b)] += 1
                cnt_sum[(b, a)] += margin_b
                cnt_n[(b, a)] += 1

    return {"synergy_sum": syn_sum, "synergy_n": syn_n,
            "counter_sum": cnt_sum, "counter_n": cnt_n,
            "champ_games": champ_games, "meta": meta}


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


def champion_graph(session: Session, champ: str, season=None, split=None,
                   top_n: int = TOP_N) -> dict:
    """Ranked synergies + counters for one champion in a timeframe.

    ``synergies`` are the best teammates (highest win-margin together). ``counters``
    combine the most favourable matchups (positive weight = champ's side tends to
    win) and the most unfavourable ones (negative), sorted high→low.
    """
    edges = build_edges(session, season, split)
    meta = edges["meta"]

    syn = []
    for key, total in edges["synergy_sum"].items():
        if champ not in key:
            continue
        other = next(iter(key - {champ}))
        if other not in meta:
            continue
        syn.append({**meta[other], "weight": _weight(total, edges["synergy_n"][key]),
                    "games": edges["synergy_n"][key]})
    syn.sort(key=lambda e: e["weight"], reverse=True)

    cnt = []
    for (a, b), total in edges["counter_sum"].items():
        if a != champ or b not in meta:
            continue
        cnt.append({**meta[b], "weight": _weight(total, edges["counter_n"][(a, b)]),
                    "games": edges["counter_n"][(a, b)]})
    cnt.sort(key=lambda e: e["weight"], reverse=True)

    # Top best + top worst, de-duplicated, high→low (for both synergies and counters).
    if len(syn) > 2 * top_n:
        syn = syn[:top_n] + syn[-top_n:]
    if len(cnt) > 2 * top_n:
        cnt = cnt[:top_n] + cnt[-top_n:]

    games, wins, sum_margin = edges["champ_games"].get(champ, [0, 0, 0.0])
    win_rate = round(wins / games * 100, 1) if games else None
    adjusted = round(min(max(0.5 + sum_margin / games, 0.0), 1.0) * 100, 1) if games else None

    self_meta = meta.get(champ, {"champion": champ, "champion_ddragon": "",
                                 "image_url": image_url(champ)})
    return {**self_meta, "season": season, "split": split,
            "games": games, "win_rate": win_rate, "adjusted_win_rate": adjusted,
            "synergies": syn, "counters": cnt}
