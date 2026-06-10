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


class StatsResponse(BaseModel):
    player: str
    role: str
    role_label: str
    season: int | None = None
    split: str | None = None
    overall: Metrics
    lck_role_baseline: Metrics
    champions: list[ChampionMetrics]
    selected_champion: ChampionMetrics | None = None
    lck_champion_baseline: Metrics | None = None
