"""API routes."""
from fastapi import APIRouter, HTTPException, Query

from backend.config import PLAYERS, find_player, distinct_roles
from backend.database import get_session
from backend.api import stats, draft, elo
from backend.api.schemas import (
    PlayerInfo, FiltersResponse, StatsResponse, Metrics, ChampionMetrics, Streak,
    MatchSummary, MatchDetail, TeamInfo, RoleInfo, TeamGroupResponse, RoleGroupResponse,
    ChampionInfo, ChampionGraphResponse, ChampionPairingResponse,
)

router = APIRouter(prefix="/api")


@router.get("/players", response_model=list[PlayerInfo])
def list_players():
    with get_session() as session:
        return [
            PlayerInfo(name=p["name"], role=p["role"],
                       role_label=stats.role_label(p["role"]),
                       team=stats.current_team(session, p["name"], p["team"]))
            for p in PLAYERS
        ]


@router.get("/teams", response_model=list[TeamInfo])
def list_teams():
    with get_session() as session:
        cmap = stats.current_teams(session)
        return [
            TeamInfo(team=t, player_count=sum(1 for v in cmap.values() if v == t))
            for t in stats.distinct_teams(session)
        ]


@router.get("/roles", response_model=list[RoleInfo])
def list_roles():
    return [
        RoleInfo(role=r, role_label=stats.role_label(r),
                 player_count=sum(1 for p in PLAYERS if p["role"] == r))
        for r in distinct_roles()
    ]


@router.get("/team/{team}", response_model=TeamGroupResponse)
def team_group(team: str):
    with get_session() as session:
        if team not in stats.distinct_teams(session):
            raise HTTPException(status_code=404, detail=f"'{team}' is not a known team.")
        data = stats.team_group(session, team)
        data["elo"] = elo.team_elo(session, team)
        return data


@router.get("/role/{role}", response_model=RoleGroupResponse)
def role_group(role: str):
    if role not in distinct_roles():
        raise HTTPException(status_code=404, detail=f"'{role}' is not a known role.")
    with get_session() as session:
        return stats.role_group(session, role)


@router.get("/champions", response_model=list[ChampionInfo])
def list_champions():
    with get_session() as session:
        return [ChampionInfo(**c) for c in draft.list_champions(session)]


@router.get("/champion/{name}/graph", response_model=ChampionGraphResponse)
def champion_graph(
    name: str,
    season: int | None = Query(None),
    split: str | None = Query(None),
    role: str | None = Query(None),
):
    with get_session() as session:
        champ = draft.canonical_champion(session, name)
        if champ is None:
            raise HTTPException(status_code=404,
                                detail=f"'{name}' is not a known LCK champion.")
        return ChampionGraphResponse(
            **draft.champion_graph(session, champ, season, split, role))


@router.get("/champion/{name}/pairing", response_model=ChampionPairingResponse)
def champion_pairing(
    name: str,
    other: str = Query(...),
    kind: str = Query(...),
    season: int | None = Query(None),
    split: str | None = Query(None),
    role: str | None = Query(None),
):
    if kind not in ("synergy", "counter"):
        raise HTTPException(status_code=400, detail="kind must be 'synergy' or 'counter'.")
    with get_session() as session:
        champ = draft.canonical_champion(session, name)
        other_champ = draft.canonical_champion(session, other)
        if champ is None or other_champ is None:
            missing = name if champ is None else other
            raise HTTPException(status_code=404,
                                detail=f"'{missing}' is not a known LCK champion.")
        return ChampionPairingResponse(**draft.champion_pairing(
            session, champ, other_champ, kind, season, split, role))


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
            team=stats.current_team(session, p["name"], p["team"]),
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
