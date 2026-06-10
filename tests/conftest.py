"""Test fixtures: a temp SQLite DB seeded with deterministic synthetic data.

The seeded engine is injected into ``backend.database._engine`` so the API
routes (via ``get_session``) read from it. ``TestClient(app)`` is created
WITHOUT a context manager so the startup event (network + real DB) does not run.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend import database
from backend.database import get_engine, init_db, PlayerGameStat
from backend.champions import to_ddragon_id
from backend.main import app


def _stat(gameid, year, split, name, team, pos, champ, k, d, a, complete=True):
    """One player-game row with fixed, easily-asserted stat values."""
    return PlayerGameStat(
        gameid=gameid, league="LCK", year=year, split=split, playoffs=False,
        date=f"{year}-03-01", playername=name, teamname=team, position=pos,
        champion=champ, champion_ddragon=to_ddragon_id(champ),
        kills=k, deaths=d, assists=a,
        gamelength_s=1800,          # 30 min -> gpm = totalgold/30
        totalgold=12000,            # -> gpm = 400
        cspm=8.5, dpm=500.0,
        damageshare=0.28,           # -> dmg_pct 28.0
        earnedgoldshare=0.24,       # -> gold_pct 24.0
        golddiffat15=(300.0 if complete else None),
        csdiffat15=(5.0 if complete else None),
        datacompleteness="complete" if complete else "partial",
    )


def _seed_rows():
    return [
        # Zeus (top) — 2025 Spring: Jax x2, Gnar x1, Renata Glasc x1 (partial)
        _stat("g1", 2025, "Spring", "Zeus", "T1", "top", "Jax", 4, 2, 6),
        _stat("g2", 2025, "Spring", "Zeus", "T1", "top", "Jax", 3, 1, 8),
        _stat("g3", 2025, "Spring", "Zeus", "T1", "top", "Gnar", 2, 3, 5),
        _stat("g4", 2025, "Spring", "Zeus", "T1", "top", "Renata Glasc", 0, 0, 10,
              complete=False),
        # Zeus — 2024 Summer (gives him a second season in filters)
        _stat("g5", 2024, "Summer", "Zeus", "T1", "top", "Jax", 5, 1, 4),
        # Other LCK top laners — 2025 Spring (for the role baseline)
        _stat("g6", 2025, "Spring", "Kiin", "Gen.G", "top", "Jax", 2, 4, 3),
        _stat("g7", 2025, "Spring", "Doran", "KT", "top", "Gnar", 1, 3, 4),
        # Ruler (bot) — only 2025 LCK (his 2024 LPL data is excluded at load time)
        _stat("g8", 2025, "Spring", "Ruler", "Gen.G", "bot", "Aphelios", 7, 2, 5),
    ]


@pytest.fixture(scope="session")
def seeded_engine(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    engine = get_engine(str(db_path))
    init_db(engine)
    with Session(engine) as s:
        s.add_all(_seed_rows())
        s.commit()
    return engine


@pytest.fixture(autouse=True)
def use_seeded_engine(seeded_engine, monkeypatch):
    monkeypatch.setattr(database, "_engine", seeded_engine)
    yield


@pytest.fixture()
def client():
    return TestClient(app)
