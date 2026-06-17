"""SQLAlchemy models, engine and session helpers (mirrors sibling project)."""
from sqlalchemy import (
    create_engine, Column, String, Float, Integer, Boolean, Index, text,
)
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import StaticPool

from backend.config import DB_PATH


class Base(DeclarativeBase):
    pass


class PlayerGameStat(Base):
    """One row per player per game (LCK only)."""
    __tablename__ = "player_game_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    gameid = Column(String, nullable=False)
    league = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    split = Column(String)               # Oracle "split" value e.g. Spring/Summer
    playoffs = Column(Boolean, default=False)
    date = Column(String)
    playername = Column(String, nullable=False)
    teamname = Column(String)
    side = Column(String)                       # "Blue" / "Red" (side of the Rift)
    position = Column(String, nullable=False)   # top/jng/mid/bot/sup
    champion = Column(String)
    champion_ddragon = Column(String)           # normalized id for image URLs
    # Item-timing scaffold: time (seconds) the 1st/2nd/3rd item was completed.
    # Oracle's Elixir has no item data; populated by `backend.enrich_lolesports`
    # from the lolesports livestats feed (NULL when a game can't be resolved).
    item1_completed_s = Column(Integer)
    item2_completed_s = Column(Integer)
    item3_completed_s = Column(Integer)
    # Final champion level. Not in Oracle's Elixir; populated by the same
    # lolesports enrichment step (NULL until/unless the game is resolved).
    level = Column(Integer)
    kills = Column(Integer, default=0)
    deaths = Column(Integer, default=0)
    assists = Column(Integer, default=0)
    teamkills = Column(Integer)                 # total kills by the player's team
    gamelength_s = Column(Integer)              # game length in seconds
    totalgold = Column(Integer)
    total_cs = Column(Integer)                  # creep score (minions + monsters)
    # Team objective totals, denormalized from the Oracle "team" row onto each
    # player row (blank on player rows in the source data).
    towers = Column(Integer)
    dragons = Column(Integer)
    barons = Column(Integer)
    cspm = Column(Float)                        # CS per minute (direct column)
    dpm = Column(Float)                         # damage per minute (direct column)
    damageshare = Column(Float)                 # 0..1
    earnedgoldshare = Column(Float)             # 0..1 (gold%)
    golddiffat15 = Column(Float)                # nullable
    golddiffat20 = Column(Float)                # nullable (null for <20min games)
    golddiffat25 = Column(Float)                # nullable (null for <25min games)
    xpdiffat15 = Column(Float)                  # nullable
    xpdiffat20 = Column(Float)                  # nullable (null for <20min games)
    xpdiffat25 = Column(Float)                  # nullable (null for <25min games)
    csdiffat15 = Column(Float)                  # nullable
    result = Column(String)                     # "Win" / "Loss" (nullable)
    datacompleteness = Column(String)


Index("ix_pgs_player", PlayerGameStat.playername)
Index("ix_pgs_role_time", PlayerGameStat.league, PlayerGameStat.position,
      PlayerGameStat.year, PlayerGameStat.split)
Index("ix_pgs_champ_time", PlayerGameStat.league, PlayerGameStat.position,
      PlayerGameStat.champion, PlayerGameStat.year, PlayerGameStat.split)


def get_engine(db_path: str = DB_PATH):
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# Columns added after the table first shipped. `create_all` won't ALTER an existing
# table, so we add any missing ones in place — avoids forcing a DB delete + reload.
_ADDED_COLUMNS = {
    "item1_completed_s": "INTEGER",
    "item2_completed_s": "INTEGER",
    "item3_completed_s": "INTEGER",
    "level": "INTEGER",
}


def _add_missing_columns(session: Session) -> None:
    existing = {row[1] for row in session.execute(
        text("PRAGMA table_info(player_game_stats)"))}
    for col, sqltype in _ADDED_COLUMNS.items():
        if col not in existing:
            session.execute(
                text(f"ALTER TABLE player_game_stats ADD COLUMN {col} {sqltype}"))


def init_db(engine=None) -> None:
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.execute(text("PRAGMA journal_mode=WAL"))
        _add_missing_columns(session)
        session.commit()


_engine = None


def get_session() -> Session:
    global _engine
    if _engine is None:
        _engine = get_engine()
        init_db(_engine)
    return Session(_engine)
