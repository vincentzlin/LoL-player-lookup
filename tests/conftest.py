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


def _stat(gameid, year, split, name, team, pos, champ, k, d, a, complete=True,
          result="Win", cspm=8.5, dpm=500.0, totalgold=12000,
          damageshare=0.28, earnedgoldshare=0.24):
    """One player-game row with fixed, easily-asserted stat values.

    The rate stats (cspm/dpm/totalgold/shares) default to the original fixed
    values; tier tests override them to push a champion above/below baseline.
    """
    return PlayerGameStat(
        gameid=gameid, league="LCK", year=year, split=split, playoffs=False,
        date=f"{year}-03-01", playername=name, teamname=team, position=pos,
        champion=champ, champion_ddragon=to_ddragon_id(champ),
        kills=k, deaths=d, assists=a,
        gamelength_s=1800,          # 30 min -> gpm = totalgold/30
        totalgold=totalgold,        # -> gpm = totalgold/30
        cspm=cspm, dpm=dpm,
        damageshare=damageshare,    # -> dmg_pct = damageshare*100
        earnedgoldshare=earnedgoldshare,  # -> gold_pct = earnedgoldshare*100
        golddiffat15=(300.0 if complete else None),
        csdiffat15=(5.0 if complete else None),
        result=result,
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
    ] + _tier_streak_rows()


# Strong / weak rate-stat kwargs for tier tests (relative to the avg opponents).
_STRONG = dict(cspm=11.0, dpm=650.0, totalgold=15000, damageshare=0.34, earnedgoldshare=0.30)
_WEAK = dict(cspm=5.5, dpm=300.0, totalgold=9000, damageshare=0.16, earnedgoldshare=0.15)
_AVG = dict(cspm=8.0, dpm=480.0, totalgold=12000, damageshare=0.25, earnedgoldshare=0.22)


def _tier_streak_rows():
    """Isolated 2026 'Spring' data exercising champion tiers + a win streak.

    Subject is Kiin (top) so it doesn't touch the Zeus/Ruler filter assertions.
    Opponents are non-searchable names that only anchor the LCK role baseline.
    Results are ordered (by gameid) so Kiin's most recent 3 games are all wins.
    """
    return [
        # Kennen x3 — strong on every metric -> "top"
        _stat("k1", 2026, "Spring", "Kiin", "Gen.G", "top", "Kennen", 6, 1, 6, result="Loss", **_STRONG),
        _stat("k2", 2026, "Spring", "Kiin", "Gen.G", "top", "Kennen", 6, 1, 6, result="Loss", **_STRONG),
        _stat("k3", 2026, "Spring", "Kiin", "Gen.G", "top", "Kennen", 6, 1, 6, result="Win", **_STRONG),
        # Gragas x3 — weak on every metric -> "bottom"
        _stat("gr1", 2026, "Spring", "Kiin", "Gen.G", "top", "Gragas", 1, 5, 2, result="Win", **_WEAK),
        _stat("gr2", 2026, "Spring", "Kiin", "Gen.G", "top", "Gragas", 1, 5, 2, result="Loss", **_WEAK),
        _stat("gr3", 2026, "Spring", "Kiin", "Gen.G", "top", "Gragas", 1, 5, 2, result="Loss", **_WEAK),
        # Sion x2 — strong but only 2 games -> tier None (below min games)
        _stat("s1", 2026, "Spring", "Kiin", "Gen.G", "top", "Sion", 6, 1, 6, result="Win", **_STRONG),
        _stat("s2", 2026, "Spring", "Kiin", "Gen.G", "top", "Sion", 6, 1, 6, result="Win", **_STRONG),
        # Average opponent top laners — baseline anchor only
        _stat("o1", 2026, "Spring", "Doran", "KT", "top", "Rumble", 3, 3, 3, **_AVG),
        _stat("o2", 2026, "Spring", "Doran", "KT", "top", "Rumble", 3, 3, 3, **_AVG),
        _stat("o3", 2026, "Spring", "Kingen", "DK", "top", "Aatrox", 3, 3, 3, **_AVG),
        _stat("o4", 2026, "Spring", "Kingen", "DK", "top", "Aatrox", 3, 3, 3, **_AVG),
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
