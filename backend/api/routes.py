"""API routes."""
from fastapi import APIRouter, HTTPException, Query

from backend.config import PLAYERS, find_player
from backend.database import get_session
from backend.api import stats
from backend.api.schemas import (
    PlayerInfo, FiltersResponse, StatsResponse, Metrics, ChampionMetrics, Streak,
    MatchSummary, MatchDetail,
)

router = APIRouter(prefix="/api")


@router.get("/players", response_model=list[PlayerInfo])
def list_players():
    return [
        PlayerInfo(name=p["name"], role=p["role"],
                   role_label=stats.role_label(p["role"]), team=p["team"])
        for p in PLAYERS
    ]


def _require_player(name: str) -> dict:
    p = find_player(name)
    if not p:
        raise HTTPException(status_code=404,
                            detail=f"'{name}' is not one of the searchable players.")
    return p


@router.get("/player/{name}/filters", response_model=FiltersResponse)
def player_filters(name: str):
    p = _require_player(name)
    with get_session() as session:
        return stats.available_filters(session, p["name"])


@router.get("/match/{gameid}", response_model=MatchDetail)
def match_detail(gameid: str):
    with get_session() as session:
        detail = stats.match_detail(session, gameid)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No game found for '{gameid}'.")
    return MatchDetail(**detail)


@router.get("/player/{name}/stats", response_model=StatsResponse)
def player_stats(
    name: str,
    season: int | None = Query(None),
    split: str | None = Query(None),
    champion: str | None = Query(None),
):
    p = _require_player(name)
    role = p["role"]
    with get_session() as session:
        all_rows = stats.player_rows(session, p["name"], season, split)
        overall = stats.metrics_from_rows(all_rows)
        matches = stats.player_matches(session, p["name"], season, split, champion)
        role_base = stats.lck_role_baseline(session, role, season, split)
        champions = stats.player_champions(all_rows, role_base)
        streak = stats.current_streak(all_rows)

        selected = None
        champ_base = None
        if champion:
            crows = [r for r in all_rows if r.champion == champion]
            cm = stats.metrics_from_rows(crows)
            selected = ChampionMetrics(
                champion=champion,
                champion_ddragon=(crows[0].champion_ddragon if crows else ""),
                image_url=stats.image_url(champion),
                **cm,
            )
            champ_base = Metrics(**stats.lck_role_baseline(
                session, role, season, split, champion=champion))

        return StatsResponse(
            player=p["name"],
            role=role,
            role_label=stats.role_label(role),
            season=season,
            split=split,
            overall=Metrics(**overall),
            lck_role_baseline=Metrics(**role_base),
            champions=[ChampionMetrics(**c) for c in champions],
            selected_champion=selected,
            lck_champion_baseline=champ_base,
            streak=Streak(**streak) if streak else None,
            matches=[MatchSummary(**m) for m in matches],
        )
