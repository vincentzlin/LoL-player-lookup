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
        # Teddy (bot) — a transfer: older game on Kiwoom DRX, newer on HANJIN BRION.
        # His displayed team should follow the most recent game (HANJIN BRION).
        _stat("t1", 2025, "Summer", "Teddy", "Kiwoom DRX", "bot", "Jinx", 3, 2, 4),
        _stat("t2", 2026, "Spring", "Teddy", "HANJIN BRION", "bot", "Jinx", 5, 1, 6,
              result="Loss"),
    ] + _tier_streak_rows() + _draft_rows()


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


def _draft_stat(gameid, side, team, pos, champ, result, split="Spring",
                gamelength_s=1800, dragons=None, golddiffat15=None, golddiffat25=None):
    """A full-game roster row for the draft-graph tests (carries side + result)."""
    return PlayerGameStat(
        gameid=gameid, league="LCK", year=2024, split=split, playoffs=False,
        date="2024-02-01", playername=f"{team}_{pos}", teamname=team, side=side,
        position=pos, champion=champ, champion_ddragon=to_ddragon_id(champ),
        kills=3, deaths=3, assists=3, gamelength_s=gamelength_s, totalgold=12000,
        dragons=dragons, golddiffat15=golddiffat15, golddiffat25=golddiffat25,
        cspm=8.0, dpm=480.0, damageshare=0.2, earnedgoldshare=0.2,
        result=result, datacompleteness="complete",
    )


# Three isolated 2024 'Spring' games for the champion graph model. Each team plays
# only 3 times (< MIN_TEAM_GAMES), so every team rating is the average (0) and each
# side's skill-adjusted margin is a clean ±0.5 — making edge signs deterministic.
# Alpha (Darius/.../Ashe/Lulu) beats Bravo (Teemo/...) all 3 games, so every Alpha
# pair clears the 3-game minimum: Ashe+Lulu get a positive synergy and Darius a
# favourable counter vs Teemo.
_ALPHA = [("top", "Darius"), ("jng", "Sejuani"), ("mid", "Orianna"),
          ("bot", "Ashe"), ("sup", "Lulu")]
_BRAVO = [("top", "Teemo"), ("jng", "Vi"), ("mid", "Syndra"),
          ("bot", "Jinx"), ("sup", "Thresh")]


def _draft_rows():
    rows = []
    for gid in ("d1", "d2", "d3"):
        for pos, champ in _ALPHA:
            rows.append(_draft_stat(gid, "Blue", "Alpha", pos, champ, "Win"))
        for pos, champ in _BRAVO:
            rows.append(_draft_stat(gid, "Red", "Bravo", pos, champ, "Loss"))
    return rows + _strong_team_rows() + _multi_role_rows() + _cait_rows()


def _multi_role_rows():
    """A flex champion (Graves) for the role-split tests, in 2024 'Fall'.

    Graves plays JUNGLE on team GJ (3 games, all wins) with teammate Karma in all 3
    and Nami in only 2; and TOP on team GT (3 games, all losses) with teammate Lux in
    all 3. Opponents are one-off teams (rating 0). So jng WR is 100 and top WR is 0,
    the Karma synergy clears the 3-game minimum while Nami (2 games) does not, and Lux
    only appears for the top role.
    """
    rows = []
    # Jungle: GJ wins all 3. Karma in g1-3; Nami only g1-2.
    jng_team = [("jng", "Graves"), ("sup", "Karma"), ("bot", "Nami")]
    for i in ("j1", "j2", "j3"):
        roster = jng_team if i != "j3" else jng_team[:2]   # drop Nami in j3
        for pos, champ in roster:
            rows.append(_draft_stat(i, "Blue", "GJ", pos, champ, "Win", split="Fall"))
        rows.append(_draft_stat(i, "Red", f"GJO_{i}", "jng", "Galio", "Loss", split="Fall"))
    # Top: GT loses all 3. Lux in all 3.
    for i in ("t1", "t2", "t3"):
        for pos, champ in (("top", "Graves"), ("mid", "Lux")):
            rows.append(_draft_stat(i, "Blue", "GT", pos, champ, "Loss", split="Fall"))
        rows.append(_draft_stat(i, "Red", f"GTO_{i}", "top", "Galio", "Win", split="Fall"))
    return rows


def _strong_team_rows():
    """A strong team (Titan) for the adjusted-win-rate test, in 2024 'Summer'.

    Titan plays 5 games (>= MIN_TEAM_GAMES) on champion Riven, winning 4 → an 80%
    raw win rate and a positive rating (expected score 0.8). Each opponent plays only
    once (rating 0). So Riven merely *meets* expectations: its skill-adjusted win rate
    recenters to 50%, well below the 80% raw rate.
    """
    rows = []
    results = ["Win", "Win", "Win", "Win", "Loss"]
    for i, res in enumerate(results, start=1):
        opp_res = "Loss" if res == "Win" else "Win"
        rows.append(_draft_stat(f"s{i}", "Blue", "Titan", "mid", "Riven", res, split="Summer"))
        rows.append(_draft_stat(f"s{i}", "Red", f"Opp{i}", "mid", "Poppy", opp_res, split="Summer"))
    return rows


def _cait_rows():
    """Caitlyn (bot) for the duration/dragon/GD@15 + pairing tests, in 2024 'Winter'.

    4 games with hand-picked length / dragons / gold-diff@15; teammate Lux in g1-3,
    opponent Jinx in g1-3. All teams play < 5 games so ratings are 0 (adjusted == raw
    within any subset). Overall GD@15 = mean(100,300,-200,0) = 50; the Lux/Jinx subset
    (g1-3) recomputes to mean(100,300,-200) = 66.7, proving the pairing recompute.

    GD@25 gives swings (GD@25 − GD@15) of +300, −300, −400, +50 → avg_swing −87.5,
    throwing_factor +87.5; only c2 is a throw (ahead at 15, lead shrank) so
    throw_count 1, avg_throw_size 300.
    """
    # (gameid, result, length_s, dragons, gd15, gd25, teammate sup, opp bot champ)
    games = [
        ("c1", "Win", 1440, 1, 100.0, 400.0, "Lux", "Jinx"),
        ("c2", "Win", 1560, 2, 300.0, 0.0, "Lux", "Jinx"),
        ("c3", "Loss", 1860, 4, -200.0, -600.0, "Lux", "Jinx"),
        ("c4", "Loss", 2160, 0, 0.0, 50.0, "Sona", "Kaisa"),
    ]
    rows = []
    for gid, res, length, drag, gd, gd25, sup, opp_champ in games:
        opp_res = "Loss" if res == "Win" else "Win"
        rows += [
            _draft_stat(gid, "Blue", "CT", "bot", "Caitlyn", res, split="Winter",
                        gamelength_s=length, dragons=drag, golddiffat15=gd, golddiffat25=gd25),
            _draft_stat(gid, "Blue", "CT", "sup", sup, res, split="Winter",
                        gamelength_s=length, dragons=drag),
            _draft_stat(gid, "Red", f"CTO_{gid}", "bot", opp_champ, opp_res,
                        split="Winter", gamelength_s=length),
        ]
    return rows


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
