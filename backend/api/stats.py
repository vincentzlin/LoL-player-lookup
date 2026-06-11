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

    # KDA is an aggregate ratio of sums (standard esports convention), not an
    # average of per-game ratios — this handles deathless games naturally.
    total_d = sum(r.deaths or 0 for r in rows)
    total_ka = sum((r.kills or 0) + (r.assists or 0) for r in rows)
    kda = round(total_ka / total_d, 3) if total_d else round(float(total_ka), 3)

    return {
        "games": len(rows),
        "kills": _avg([r.kills for r in rows]),
        "deaths": _avg([r.deaths for r in rows]),
        "assists": _avg([r.assists for r in rows]),
        "kda": kda,
        "cspm": _avg([r.cspm for r in rows]),
        "gpm": _avg([gpm(r) for r in rows]),
        "dpm": _avg([r.dpm for r in rows]),
        "gold_pct": _round_pct(_avg([r.earnedgoldshare for r in rows])),
        "dmg_pct": _round_pct(_avg([r.damageshare for r in rows])),
        "csd15": _avg([r.csdiffat15 for r in rows]),
        "gd15": _avg([r.golddiffat15 for r in rows]),
    }


METRIC_KEYS = ["kills", "deaths", "assists", "kda", "cspm", "gpm", "dpm",
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


def player_champions(rows: list[PlayerGameStat],
                     role_baseline: dict | None = None) -> list[dict]:
    """Group a player's rows by champion, with per-champion metrics + image.

    When ``role_baseline`` is given, each champion also gets a ``tier`` of
    ``"top"``/``"bottom"``/``None`` from :func:`champion_tier`.
    """
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
            "tier": champion_tier(m, role_baseline) if role_baseline else None,
            **m,
        })
    out.sort(key=lambda c: c["games"], reverse=True)
    return out


# ── Performance tier & streaks ───────────────────────────────────────────────

# Metrics with a stable, non-zero LCK role baseline and where higher is better.
# The diff@15 metrics are intentionally excluded: their role average is ~0, so a
# percentage delta against them is meaningless/unstable.
COMPOSITE_KEYS = ["kda", "cspm", "gpm", "dpm", "gold_pct", "dmg_pct"]

MIN_TIER_GAMES = 3
TIER_THRESHOLD_PCT = 15.0


def champion_tier(champ_metrics: dict, role_baseline: dict) -> str | None:
    """Classify a champion as a top/bottom performer vs the LCK role average.

    Returns ``"top"`` if the player's composite %-delta across COMPOSITE_KEYS is
    >= +15%, ``"bottom"`` if <= -15%, else ``None``. Requires >= 3 games.
    """
    if champ_metrics.get("games", 0) < MIN_TIER_GAMES:
        return None
    pcts = []
    for k in COMPOSITE_KEYS:
        p, b = champ_metrics.get(k), role_baseline.get(k)
        if p is None or b is None or b == 0:
            continue
        pcts.append((p - b) / abs(b) * 100)  # all keys are higher-is-better
    if not pcts:
        return None
    composite = mean(pcts)
    if composite >= TIER_THRESHOLD_PCT:
        return "top"
    if composite <= -TIER_THRESHOLD_PCT:
        return "bottom"
    return None


STREAK_MIN = 3


def current_streak(rows: list[PlayerGameStat]) -> dict | None:
    """Detect a 3+ game win/loss streak at the most-recent end of ``rows``.

    Rows are ordered by ``(date, gameid)``; rows without a result are skipped.
    Returns ``{"type": "win"|"loss", "length": n}`` or ``None``.
    """
    played = sorted(
        (r for r in rows if r.result in ("Win", "Loss")),
        key=lambda r: (r.date or "", r.gameid or ""),
    )
    if not played:
        return None
    last = played[-1].result
    length = 0
    for r in reversed(played):
        if r.result != last:
            break
        length += 1
    if length < STREAK_MIN:
        return None
    return {"type": "win" if last == "Win" else "loss", "length": length}


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
