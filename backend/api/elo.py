"""Team Elo rating system (VIN-21).

A classic Elo rating over historical LCK results. Team Elo is usually the single
best predictor of who wins a *set* (a best-of series), so ratings are updated
**once per series**, not once per game.

Pipeline:

1. **Matches** — collapse the per-player ``player_game_stats`` rows to one record
   per ``gameid`` (one winner, one loser).
2. **Series** — group same-day games between the same two teams into a series;
   the series winner is whoever took the majority of its games. (LCK plays one
   series per team per day, so date + team-pair reconstructs the set reliably. A
   future Oracle ``game``-ordinal column could replace this heuristic.)
3. **Elo** — walk series in date order. At each new split, every rating is pulled
   partway back toward ``BASE`` (roster/meta churn), then each series nudges the
   two teams' ratings by ``K * (actual − expected)``.

Ratings are built lazily and cached in-memory per engine — LCK data is small, so
a single pass is cheap and the cache avoids recomputing per request.
"""
from collections import defaultdict
from typing import NamedTuple

from sqlalchemy.orm import Session

from backend.database import PlayerGameStat

# ── Tunable constants ─────────────────────────────────────────────────────────
BASE = 1500.0       # starting / mean rating
K = 32.0            # update step per series
REGRESS = 0.25      # at each new split, pull ratings 25% back toward BASE
RECENT_N = 5        # recent-form window (number of most-recent series)


def expected_score(rating_a: float, rating_b: float) -> float:
    """Elo expected score for team A vs team B (0..1). Same formula as draft.py."""
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


class Series(NamedTuple):
    date: str
    year: int
    split: str | None
    winner: str
    loser: str


# id(engine) -> {"teams": {team: {...}}, "ranked": [team, ...]}
_cache: dict = {}


# ── Build: rows → matches → series ────────────────────────────────────────────

def _matches(session: Session) -> list[dict]:
    """One record per gameid: {date, day, year, split, winner, loser}.

    ``date`` is the full game timestamp (used for chronological order); ``day`` is
    its calendar-day prefix (used to group a series). Skips games that don't
    resolve to exactly one Win + one Loss (missing team/result or incomplete rows)."""
    rows = (session.query(
                PlayerGameStat.gameid, PlayerGameStat.date, PlayerGameStat.year,
                PlayerGameStat.split, PlayerGameStat.teamname, PlayerGameStat.result)
            .all())
    games: dict[str, dict] = defaultdict(
        lambda: {"date": "", "year": None, "split": None, "win": set(), "loss": set()})
    for gameid, date, year, split, team, result in rows:
        if not gameid or not team or result not in ("Win", "Loss"):
            continue
        g = games[gameid]
        g["date"], g["year"], g["split"] = (date or ""), year, split
        g["win" if result == "Win" else "loss"].add(team)

    out: list[dict] = []
    for g in games.values():
        if len(g["win"]) == 1 and len(g["loss"]) == 1:
            out.append({
                "date": g["date"], "day": g["date"][:10],
                "year": g["year"], "split": g["split"],
                "winner": next(iter(g["win"])), "loser": next(iter(g["loss"])),
            })
    return out


def _series(matches: list[dict]) -> list[Series]:
    """Group a single day's games between the same two teams into one series; the
    series winner took the majority of its games. Returned in chronological order.

    A LoL series (Bo3/Bo5) is played within one day, so (day, team-pair) recovers
    the set. (A future Oracle game-ordinal column could replace this heuristic.)"""
    # (day, frozenset{teamA, teamB}) -> series wins per team + earliest meta
    wins: dict[tuple, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    meta: dict[tuple, dict] = {}
    for m in matches:
        key = (m["day"], frozenset({m["winner"], m["loser"]}))
        wins[key][m["winner"]] += 1
        prev = meta.get(key)
        if prev is None or m["date"] < prev["date"]:
            meta[key] = m

    series: list[Series] = []
    for key, w in wins.items():
        _, pair = key
        if len(pair) != 2:
            continue
        a, b = sorted(pair)
        winner = a if w[a] >= w[b] else b
        loser = b if winner == a else a
        info = meta[key]
        series.append(Series(info["date"], info["year"], info["split"], winner, loser))

    series.sort(key=lambda s: s.date)
    return series


def _build(session: Session) -> dict:
    """Run Elo over all series and return current ratings + history per team."""
    series = _series(_matches(session))

    ratings: dict[str, float] = defaultdict(lambda: BASE)
    teams: dict[str, dict] = defaultdict(
        lambda: {"peak": BASE, "wins": 0, "losses": 0, "form": []})
    prev_bucket: tuple | None = None

    for s in series:
        bucket = (s.year, s.split)
        if prev_bucket is not None and bucket != prev_bucket:
            for t in ratings:                       # partial regression at split boundary
                ratings[t] = BASE + (1.0 - REGRESS) * (ratings[t] - BASE)
        prev_bucket = bucket

        ra, rb = ratings[s.winner], ratings[s.loser]
        ea = expected_score(ra, rb)
        ratings[s.winner] = ra + K * (1.0 - ea)
        ratings[s.loser] = rb + K * (0.0 - (1.0 - ea))

        tw, tl = teams[s.winner], teams[s.loser]
        tw["wins"] += 1
        tl["losses"] += 1
        tw["peak"] = max(tw["peak"], ratings[s.winner])
        tl["peak"] = max(tl["peak"], ratings[s.loser])
        tw["form"].append("W")
        tl["form"].append("L")

    for t, info in teams.items():
        info["rating"] = ratings[t]

    ranked = sorted(teams, key=lambda t: teams[t]["rating"], reverse=True)
    return {"teams": teams, "ranked": ranked}


def _data(session: Session) -> dict:
    key = id(session.get_bind())
    if key not in _cache:
        _cache[key] = _build(session)
    return _cache[key]


def clear_cache() -> None:
    _cache.clear()


# ── Public API ────────────────────────────────────────────────────────────────

def all_team_elos(session: Session) -> list[dict]:
    """Every team sorted by current rating (desc), each with rank/peak/record."""
    data = _data(session)
    teams, ranked = data["teams"], data["ranked"]
    n = len(ranked)
    out = []
    for i, t in enumerate(ranked, start=1):
        info = teams[t]
        out.append({
            "team": t,
            "rating": round(info["rating"]),
            "rank": i,
            "team_count": n,
            "peak": round(info["peak"]),
            "series_played": info["wins"] + info["losses"],
            "wins": info["wins"],
            "losses": info["losses"],
        })
    return out


def team_elo(session: Session, team: str) -> dict | None:
    """One team's Elo summary plus recent form, or None if it has no series."""
    data = _data(session)
    info = data["teams"].get(team)
    if info is None:
        return None
    rank = data["ranked"].index(team) + 1
    return {
        "rating": round(info["rating"]),
        "rank": rank,
        "team_count": len(data["ranked"]),
        "peak": round(info["peak"]),
        "series_played": info["wins"] + info["losses"],
        "wins": info["wins"],
        "losses": info["losses"],
        "recent_form": list(reversed(info["form"][-RECENT_N:])),  # most-recent first
    }
