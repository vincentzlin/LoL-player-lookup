"""Aggregation helpers: compute the 8 pro-play metrics over a filtered query."""
from statistics import mean

from sqlalchemy.orm import Session, Query

from backend.config import SEASONS, ROLE_LABELS
from backend.database import PlayerGameStat
from backend.champions import image_url


def _avg(values):
    vals = [v for v in values if v is not None]
    return round(mean(vals), 3) if vals else None


def metrics_from_rows(rows: list[PlayerGameStat]) -> dict:
    """Compute the 8 requested metrics (+ games) from player-game rows."""
    if not rows:
        return {"games": 0, **{k: None for k in METRIC_KEYS}}

    def gpm(r):
        if r.totalgold is None or not r.gamelength_s:
            return None
        return r.totalgold / (r.gamelength_s / 60.0)

    return {
        "games": len(rows),
        "kills": _avg([r.kills for r in rows]),
        "deaths": _avg([r.deaths for r in rows]),
        "assists": _avg([r.assists for r in rows]),
        "cspm": _avg([r.cspm for r in rows]),
        "gpm": _avg([gpm(r) for r in rows]),
        "dpm": _avg([r.dpm for r in rows]),
        "gold_pct": _round_pct(_avg([r.earnedgoldshare for r in rows])),
        "dmg_pct": _round_pct(_avg([r.damageshare for r in rows])),
        "csd15": _avg([r.csdiffat15 for r in rows]),
        "gd15": _avg([r.golddiffat15 for r in rows]),
    }


METRIC_KEYS = ["kills", "deaths", "assists", "cspm", "gpm", "dpm",
               "gold_pct", "dmg_pct", "csd15", "gd15"]


def _round_pct(frac):
    return round(frac * 100, 2) if frac is not None else None


def _apply_timeframe(q: Query, season: int | None, split: str | None) -> Query:
    """season is the in-game number (e.g. 15); map back to a calendar year."""
    if season is not None:
        year = next((y for y, s in SEASONS.items() if s == season), None)
        if year is not None:
            q = q.filter(PlayerGameStat.year == year)
    if split:
        q = q.filter(PlayerGameStat.split == split)
    return q


# ── Player queries ───────────────────────────────────────────────────────────

def player_rows(session: Session, name: str, season=None, split=None,
                champion: str | None = None) -> list[PlayerGameStat]:
    q = session.query(PlayerGameStat).filter(PlayerGameStat.playername == name)
    q = _apply_timeframe(q, season, split)
    if champion:
        q = q.filter(PlayerGameStat.champion == champion)
    return q.all()


def player_champions(rows: list[PlayerGameStat]) -> list[dict]:
    """Group a player's rows by champion, with per-champion metrics + image."""
    by_champ: dict[str, list[PlayerGameStat]] = {}
    ddragon: dict[str, str] = {}
    for r in rows:
        if not r.champion:
            continue
        by_champ.setdefault(r.champion, []).append(r)
        ddragon[r.champion] = r.champion_ddragon
    out = []
    for champ, crows in by_champ.items():
        m = metrics_from_rows(crows)
        out.append({
            "champion": champ,
            "champion_ddragon": ddragon.get(champ) or "",
            "image_url": image_url(champ),
            **m,
        })
    out.sort(key=lambda c: c["games"], reverse=True)
    return out


# ── LCK baseline queries ─────────────────────────────────────────────────────

def lck_role_baseline(session: Session, role: str, season=None, split=None,
                      champion: str | None = None) -> dict:
    """Average of each metric across ALL LCK players in `role` (same timeframe)."""
    q = session.query(PlayerGameStat).filter(PlayerGameStat.position == role)
    q = _apply_timeframe(q, season, split)
    if champion:
        q = q.filter(PlayerGameStat.champion == champion)
    return metrics_from_rows(q.all())


def available_filters(session: Session, name: str) -> dict:
    """Distinct seasons + splits the player actually has LCK games in."""
    rows = session.query(PlayerGameStat.year, PlayerGameStat.split).filter(
        PlayerGameStat.playername == name).distinct().all()
    seasons: dict[int, set] = {}
    for year, split in rows:
        if year not in SEASONS:
            continue
        seasons.setdefault(year, set())
        if split:
            seasons[year].add(split)

    season_list = [
        {"year": y, "season": SEASONS[y], "label": f"Season {SEASONS[y]}"}
        for y in sorted(seasons.keys(), reverse=True)
    ]
    splits_by_season = {
        SEASONS[y]: _split_buttons(sorted(splits))
        for y, splits in seasons.items()
    }
    return {"seasons": season_list, "splits_by_season": splits_by_season}


# Stable ordering for split buttons.
_SPLIT_ORDER = ["Winter", "Spring", "Summer", "Fall", "Split 1", "Split 2", "Split 3"]


def _split_buttons(splits: list[str]) -> list[dict]:
    def keyfn(s):
        return _SPLIT_ORDER.index(s) if s in _SPLIT_ORDER else 99
    ordered = sorted(splits, key=keyfn)
    out = []
    for i, s in enumerate(ordered, start=1):
        out.append({"value": s, "label": f"Split {i} · {s}"})
    return out


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)
