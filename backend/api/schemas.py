"""Pydantic response models."""
from pydantic import BaseModel


class PlayerInfo(BaseModel):
    name: str
    role: str
    role_label: str
    team: str


class Metrics(BaseModel):
    games: int
    kills: float | None = None
    deaths: float | None = None
    assists: float | None = None
    kda: float | None = None
    cspm: float | None = None
    gpm: float | None = None
    dpm: float | None = None
    gold_pct: float | None = None
    dmg_pct: float | None = None
    csd15: float | None = None
    gd15: float | None = None


class ChampionMetrics(Metrics):
    champion: str
    champion_ddragon: str
    image_url: str
    tier: str | None = None        # "top" / "bottom" / None


class Streak(BaseModel):
    type: str                      # "win" / "loss"
    length: int


class MatchSummary(BaseModel):
    gameid: str
    date: str | None = None
    tournament: str | None = None
    side: str | None = None            # "Blue" / "Red"
    result: str | None = None          # "Win" / "Loss"
    champion: str | None = None
    champion_ddragon: str = ""
    image_url: str = ""
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    team: str | None = None
    opponent_team: str | None = None
    opponent_champion: str | None = None
    opponent_image_url: str = ""
    # Item-timing scaffold (seconds). Always None until a data source provides it.
    item1_completed_s: int | None = None
    item2_completed_s: int | None = None
    item3_completed_s: int | None = None


class MatchPlayer(BaseModel):
    position: str | None = None
    playername: str | None = None
    champion: str | None = None
    champion_ddragon: str = ""
    image_url: str = ""
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    cs: int | None = None
    gold: int | None = None
    level: int | None = None           # not in Oracle's Elixir → "N/A"


class MatchTeam(BaseModel):
    side: str | None = None            # "Blue" / "Red"
    teamname: str | None = None
    result: str | None = None          # "Win" / "Loss"
    kills: int | None = None           # total team kills
    towers: int | None = None
    dragons: int | None = None
    barons: int | None = None
    players: list[MatchPlayer] = []


class MatchDetail(BaseModel):
    gameid: str
    date: str | None = None
    tournament: str | None = None
    gamelength_s: int | None = None
    teams: list[MatchTeam] = []


class SeasonOption(BaseModel):
    year: int
    season: int
    label: str


class SplitOption(BaseModel):
    value: str
    label: str


class FiltersResponse(BaseModel):
    seasons: list[SeasonOption]
    splits_by_season: dict[int, list[SplitOption]]


class PlayerCard(BaseModel):
    name: str
    role: str
    role_label: str
    team: str
    games: int
    win_pct: float | None = None
    streak: Streak | None = None
    rating: str                        # "strong" / "average" / "struggling"


class TeamInfo(BaseModel):
    team: str
    player_count: int


class RoleInfo(BaseModel):
    role: str
    role_label: str
    player_count: int


class TeamGroupResponse(BaseModel):
    team: str
    players: list[PlayerCard] = []


class RoleGroupResponse(BaseModel):
    role: str
    role_label: str
    players: list[PlayerCard] = []


class ChampionInfo(BaseModel):
    champion: str
    champion_ddragon: str = ""
    image_url: str = ""


class GraphEdge(BaseModel):
    champion: str
    champion_ddragon: str = ""
    image_url: str = ""
    weight: float                      # skill-adjusted win-margin %, signed
    games: int                         # number of games this pair shared


class ChampionRole(BaseModel):
    role: str
    role_label: str
    games: int
    win_rate: float | None = None
    adjusted_win_rate: float | None = None


class ChampionGraphResponse(BaseModel):
    champion: str
    champion_ddragon: str = ""
    image_url: str = ""
    season: int | None = None
    split: str | None = None
    role: str | None = None            # selected role (None = all roles merged)
    roles: list[ChampionRole] = []     # per-role win-rate summary
    games: int = 0
    win_rate: float | None = None          # raw wins / games %
    adjusted_win_rate: float | None = None  # skill-adjusted (50% + avg margin)
    synergies: list[GraphEdge] = []    # best (+) → worst (−) teammates
    counters: list[GraphEdge] = []     # favourable (+) → unfavourable (−)


class StatsResponse(BaseModel):
    player: str
    role: str
    role_label: str
    team: str
    season: int | None = None
    split: str | None = None
    overall: Metrics
    lck_role_baseline: Metrics
    champions: list[ChampionMetrics]
    selected_champion: ChampionMetrics | None = None
    lck_champion_baseline: Metrics | None = None
    streak: Streak | None = None
    matches: list[MatchSummary] = []
