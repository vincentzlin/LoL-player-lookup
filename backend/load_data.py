"""Load Oracle's Elixir yearly CSVs into SQLite (LCK rows only).

Usage:
    python -m backend.load_data            # auto-discovers CSVs in data/raw/
    python -m backend.load_data path1.csv path2.csv

Download the CSVs from https://oracleselixir.com/tools/downloads
(files named e.g. ``2025_LoL_esports_match_data_from_OraclesElixir.csv``)
and drop them in ``data/raw/``.
"""
import logging
import sys
from pathlib import Path

import pandas as pd

from backend.config import RAW_DIR, LEAGUE, SEASONS, player_names
from backend.champions import to_ddragon_id, refresh_version
from backend.database import get_engine, init_db, PlayerGameStat
from sqlalchemy.orm import Session

logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")
log = logging.getLogger(__name__)

# Oracle columns we read (a subset of the ~120 available).
USECOLS = [
    "gameid", "datacompleteness", "league", "year", "split", "playoffs",
    "date", "playername", "teamname", "position", "champion",
    "kills", "deaths", "assists", "gamelength", "totalgold",
    "cspm", "dpm", "damageshare", "earnedgoldshare",
    "golddiffat15", "csdiffat15", "result",
]


def _to_int(v, default=None):
    try:
        if pd.isna(v):
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_float(v, default=None):
    try:
        if pd.isna(v):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_bool(v):
    return str(v).strip() in ("1", "1.0", "True", "true")


def _to_result(v):
    """Oracle's `result` is 1 (win) / 0 (loss); map to 'Win'/'Loss' or None."""
    n = _to_int(v)
    if n is None:
        return None
    return "Win" if n else "Loss"


def discover_csvs() -> list[Path]:
    return sorted(RAW_DIR.glob("*OraclesElixir*.csv")) or sorted(RAW_DIR.glob("*.csv"))


def load(paths: list[Path]) -> None:
    if not paths:
        log.error("No CSV files found in %s. Download them from "
                  "https://oracleselixir.com/tools/downloads", RAW_DIR)
        sys.exit(1)

    refresh_version()
    engine = get_engine()
    init_db(engine)

    wanted_years = set(SEASONS.keys())
    pros = player_names()
    total = 0
    pro_hits: dict[str, int] = {p: 0 for p in pros}

    with Session(engine) as session:
        # Fresh load each run.
        session.query(PlayerGameStat).delete()
        session.commit()

        for path in paths:
            log.info("Reading %s ...", path.name)
            df = pd.read_csv(path, usecols=lambda c: c in USECOLS, low_memory=False)

            # LCK player rows only, within the target seasons.
            df = df[df["league"] == LEAGUE]
            df = df[df["position"].isin(["top", "jng", "mid", "bot", "sup"])]
            df = df[df["year"].apply(lambda y: _to_int(y) in wanted_years)]

            rows = []
            for r in df.itertuples(index=False):
                d = r._asdict()
                champ = d.get("champion")
                pname = str(d.get("playername") or "").strip()
                if pname in pro_hits:
                    pro_hits[pname] += 1
                rows.append(PlayerGameStat(
                    gameid=str(d.get("gameid") or ""),
                    league=d.get("league"),
                    year=_to_int(d.get("year")),
                    split=(str(d["split"]).strip() if not pd.isna(d.get("split")) else None),
                    playoffs=_to_bool(d.get("playoffs")),
                    date=(str(d.get("date")) if not pd.isna(d.get("date")) else None),
                    playername=pname,
                    teamname=(str(d.get("teamname")) if not pd.isna(d.get("teamname")) else None),
                    position=d.get("position"),
                    champion=(str(champ) if not pd.isna(champ) else None),
                    champion_ddragon=to_ddragon_id(str(champ) if not pd.isna(champ) else None),
                    kills=_to_int(d.get("kills"), 0),
                    deaths=_to_int(d.get("deaths"), 0),
                    assists=_to_int(d.get("assists"), 0),
                    gamelength_s=_to_int(d.get("gamelength")),
                    totalgold=_to_int(d.get("totalgold")),
                    cspm=_to_float(d.get("cspm")),
                    dpm=_to_float(d.get("dpm")),
                    damageshare=_to_float(d.get("damageshare")),
                    earnedgoldshare=_to_float(d.get("earnedgoldshare")),
                    golddiffat15=_to_float(d.get("golddiffat15")),
                    csdiffat15=_to_float(d.get("csdiffat15")),
                    result=_to_result(d.get("result")),
                    datacompleteness=(str(d.get("datacompleteness")) if not pd.isna(d.get("datacompleteness")) else None),
                ))

            session.bulk_save_objects(rows)
            session.commit()
            total += len(rows)
            log.info("  + %d LCK rows from %s", len(rows), path.name)

    log.info("Done. Loaded %d LCK player-game rows total.", total)
    for name, n in pro_hits.items():
        flag = "OK" if n else "!! NOT FOUND"
        log.info("  pro %-8s : %5d rows  [%s]", name, n, flag)
    missing = [n for n, c in pro_hits.items() if c == 0]
    if missing:
        log.warning("These players had 0 LCK rows in the target seasons: %s "
                    "(check spelling vs Oracle's `playername`).", missing)


if __name__ == "__main__":
    args = sys.argv[1:]
    paths = [Path(a) for a in args] if args else discover_csvs()
    load(paths)
