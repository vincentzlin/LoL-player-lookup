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

    # Everything is keyed by the FOCAL champion's role so the champion view can
    # filter by the role that champion was played in.
    role_games: dict = defaultdict(lambda: [0, 0, 0.0])           # (champ, role) -> [games, wins, Σmargin]
    syn: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))  # (champ, role) -> teammate -> [Σmargin, n]
    cnt: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))  # (champ, role) -> opponent -> [Σmargin, n]
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

        # (champion, role) pairs per side; champions are distinct within a side.
        side_a = {r.champion: r.position for r in rows_a if r.champion and r.position}
        side_b = {r.champion: r.position for r in rows_b if r.champion and r.position}

        for mine, theirs, margin, won in ((side_a, side_b, margin_a, won_a),
                                          (side_b, side_a, -margin_a, not won_a)):
            champs = set(mine)
            for champ, role in mine.items():
                rec = role_games[(champ, role)]
                rec[0] += 1
                rec[1] += 1 if won else 0
                rec[2] += margin
                for teammate in champs - {champ}:
                    e = syn[(champ, role)][teammate]
                    e[0] += margin
                    e[1] += 1
                for opponent in theirs:
                    e = cnt[(champ, role)][opponent]
                    e[0] += margin
                    e[1] += 1

    return {"role_games": role_games, "syn": syn, "cnt": cnt, "meta": meta}


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


def _ranked_edges(merged: dict, meta: dict, top_n: int) -> list[dict]:
    """Build the best+worst ranked edge list from a {other: [Σmargin, n]} dict.

    Drops pairs below MIN_EDGE_GAMES, then keeps the top_n best and top_n worst.
    """
    out = []
    for other, (total, n) in merged.items():
        if n < MIN_EDGE_GAMES or other not in meta:
            continue
        out.append({**meta[other], "weight": _weight(total, n), "games": n})
    out.sort(key=lambda e: e["weight"], reverse=True)
    if len(out) > 2 * top_n:
        out = out[:top_n] + out[-top_n:]
    return out


def champion_graph(session: Session, champ: str, season=None, split=None,
                   role: str | None = None, top_n: int = TOP_N) -> dict:
    """Win rates + ranked synergies/counters for one champion, optionally by role.

    ``roles`` summarises each role the champion was played in. When ``role`` is given
    only that role's games are used; otherwise all roles are merged. ``synergies`` and
    ``counters`` only include pairs with at least ``MIN_EDGE_GAMES`` shared games.
    """
    edges = build_edges(session, season, split)
    meta = edges["meta"]
    role_games, syn, cnt = edges["role_games"], edges["syn"], edges["cnt"]

    # Roles the champion has games in, ordered top→jng→mid→bot→sup.
    champ_roles = [r for r in _ROLES if (champ, r) in role_games]
    roles_summary = []
    for r in champ_roles:
        g, w, m = role_games[(champ, r)]
        wr, adj = _win_rates(g, w, m)
        roles_summary.append({"role": r, "role_label": ROLE_LABELS.get(r, r),
                              "games": g, "win_rate": wr, "adjusted_win_rate": adj})

    sel_role = role if role in champ_roles else None
    use_roles = [sel_role] if sel_role else champ_roles

    # Aggregate the selected role(s).
    games = wins = 0
    sum_margin = 0.0
    merged_syn: dict = defaultdict(lambda: [0.0, 0])
    merged_cnt: dict = defaultdict(lambda: [0.0, 0])
    for r in use_roles:
        g, w, m = role_games.get((champ, r), [0, 0, 0.0])
        games += g
        wins += w
        sum_margin += m
        for other, (total, n) in syn.get((champ, r), {}).items():
            merged_syn[other][0] += total
            merged_syn[other][1] += n
        for other, (total, n) in cnt.get((champ, r), {}).items():
            merged_cnt[other][0] += total
            merged_cnt[other][1] += n

    win_rate, adjusted = _win_rates(games, wins, sum_margin)
    self_meta = meta.get(champ, {"champion": champ, "champion_ddragon": "",
                                 "image_url": image_url(champ)})
    return {**self_meta, "season": season, "split": split,
            "role": sel_role, "roles": roles_summary,
            "games": games, "win_rate": win_rate, "adjusted_win_rate": adjusted,
            "synergies": _ranked_edges(merged_syn, meta, top_n),
            "counters": _ranked_edges(merged_cnt, meta, top_n)}
