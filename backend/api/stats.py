"""Aggregation helpers: compute the 8 pro-play metrics over a filtered query."""
from statistics import mean

from sqlalchemy.orm import Session, Query

from backend.config import SEASONS, ROLE_LABELS, PLAYERS
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


def tournament_label(row: PlayerGameStat) -> str:
    """Synthesize a tournament name from the stored timeframe fields.

    Oracle's Elixir has no event name; build one from league/year/split/playoffs,
    e.g. "LCK 2026 Spring" or "LCK 2026 Spring Playoffs"."""
    parts = [row.league or "LCK"]
    if row.year:
        parts.append(str(row.year))
    if row.split:
        parts.append(row.split)
    label = " ".join(parts)
    return f"{label} Playoffs" if row.playoffs else label


# Player roles in scoreboard order (excludes Oracle's "team" aggregate row).
_ROLE_ORDER = ["top", "jng", "mid", "bot", "sup"]


def player_matches(session: Session, name: str, season=None, split=None,
                   champion: str | None = None, limit: int = 5) -> list[dict]:
    """The player's `limit` most-recent games in the timeframe, newest first.

    Each match includes the score line, both teams, the side of the Rift and the
    opposing laner (same gameid, same position, opposite side). Item-timing fields
    are passed through from the lolesports enrichment step (None when unresolved).
    """
    q = session.query(PlayerGameStat).filter(PlayerGameStat.playername == name)
    q = _apply_timeframe(q, season, split)
    if champion:
        q = q.filter(PlayerGameStat.champion == champion)
    rows = (q.order_by(PlayerGameStat.date.desc(), PlayerGameStat.gameid.desc())
             .limit(limit).all())

    out = []
    for r in rows:
        opp = (session.query(PlayerGameStat)
               .filter(PlayerGameStat.gameid == r.gameid,
                       PlayerGameStat.position == r.position,
                       PlayerGameStat.side != r.side)
               .first()) if r.side else None
        out.append({
            "gameid": r.gameid,
            "date": r.date,
            "tournament": tournament_label(r),
            "side": r.side,
            "result": r.result,
            "champion": r.champion,
            "champion_ddragon": r.champion_ddragon or "",
            "image_url": image_url(r.champion) if r.champion else "",
            "kills": r.kills,
            "deaths": r.deaths,
            "assists": r.assists,
            "team": r.teamname,
            "opponent_team": opp.teamname if opp else None,
            "opponent_champion": opp.champion if opp else None,
            "opponent_image_url": image_url(opp.champion) if opp and opp.champion else "",
            "item1_completed_s": r.item1_completed_s,
            "item2_completed_s": r.item2_completed_s,
            "item3_completed_s": r.item3_completed_s,
        })
    return out


def match_detail(session: Session, gameid: str) -> dict | None:
    """Full scoreboard for one game: both teams, objectives and all 10 champions.

    Returns ``None`` when the gameid isn't in the DB. Objective totals (team kills,
    towers/dragons/barons) are denormalized onto every player row, so they're read
    from any row of a side. Champion level comes from the lolesports enrichment step
    (``None`` — rendered as "N/A" — for games that couldn't be resolved)."""
    rows = (session.query(PlayerGameStat)
            .filter(PlayerGameStat.gameid == gameid)
            .filter(PlayerGameStat.position.in_(_ROLE_ORDER))
            .all())
    if not rows:
        return None

    by_side: dict[str, list[PlayerGameStat]] = {}
    for r in rows:
        by_side.setdefault(r.side or "", []).append(r)

    def build_team(side_rows: list[PlayerGameStat]) -> dict:
        anchor = side_rows[0]
        players = sorted(
            side_rows,
            key=lambda r: _ROLE_ORDER.index(r.position) if r.position in _ROLE_ORDER else 99,
        )
        return {
            "side": anchor.side,
            "teamname": anchor.teamname,
            "result": anchor.result,
            "kills": anchor.teamkills,
            "towers": anchor.towers,
            "dragons": anchor.dragons,
            "barons": anchor.barons,
            "players": [{
                "position": p.position,
                "playername": p.playername,
                "champion": p.champion,
                "champion_ddragon": p.champion_ddragon or "",
                "image_url": image_url(p.champion) if p.champion else "",
                "kills": p.kills,
                "deaths": p.deaths,
                "assists": p.assists,
                "cs": p.total_cs,
                "gold": p.totalgold,
                "level": p.level,    # from lolesports enrichment (None if unresolved)
            } for p in players],
        }

    anchor = rows[0]
    teams = [build_team(by_side[s]) for s in ("Blue", "Red") if by_side.get(s)]
    return {
        "gameid": gameid,
        "date": anchor.date,
        "tournament": tournament_label(anchor),
        "gamelength_s": anchor.gamelength_s,
        "teams": teams,
    }


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


# ── Team / role group cards (VIN-14) ─────────────────────────────────────────

# champion_tier "top"/"bottom" → a player-level performance rating.
_RATING_FROM_TIER = {"top": "strong", "bottom": "struggling"}


def current_team(session: Session, name: str, default: str | None = None) -> str | None:
    """The player's most recent team (by game date), or `default` if no games."""
    row = (session.query(PlayerGameStat.teamname)
           .filter(PlayerGameStat.playername == name)
           .order_by(PlayerGameStat.date.desc())
           .first())
    return row[0] if row and row[0] else default


def current_teams(session: Session) -> dict[str, str | None]:
    """Most-recent team for every allowlist player (config team is the fallback)."""
    return {p["name"]: current_team(session, p["name"], p["team"]) for p in PLAYERS}


def distinct_teams(session: Session) -> list[str | None]:
    """De-duplicated current teams across the allowlist, in PLAYERS order."""
    cmap = current_teams(session)
    seen: list[str | None] = []
    for p in PLAYERS:
        if cmap[p["name"]] not in seen:
            seen.append(cmap[p["name"]])
    return seen


def player_card(session: Session, player: dict) -> dict:
    """A compact performance snapshot for a team/role list entry.

    Uses the player's full LCK history (no timeframe filter): win rate, current
    win/loss streak, and a Strong/Average/Struggling rating derived from the same
    composite-vs-role-baseline logic used for champion tiers.
    """
    name, role = player["name"], player["role"]
    rows = player_rows(session, name)
    games = len(rows)
    wins = sum(1 for r in rows if r.result == "Win")
    win_pct = round(wins / games * 100, 1) if games else None

    baseline = lck_role_baseline(session, role)
    tier = champion_tier(metrics_from_rows(rows), baseline)

    return {
        "name": name,
        "role": role,
        "role_label": role_label(role),
        "team": current_team(session, name, player["team"]),
        "games": games,
        "win_pct": win_pct,
        "streak": current_streak(rows),
        "rating": _RATING_FROM_TIER.get(tier, "average"),
    }


def team_group(session: Session, team: str) -> dict:
    """All allowlist players whose most-recent team is `team`, with their cards."""
    cmap = current_teams(session)
    players = [player_card(session, p) for p in PLAYERS if cmap[p["name"]] == team]
    return {"team": team, "players": players}


def role_group(session: Session, role: str) -> dict:
    """All allowlist players in `role`, with their performance cards."""
    players = [player_card(session, p) for p in PLAYERS if p["role"] == role]
    return {"role": role, "role_label": role_label(role), "players": players}
