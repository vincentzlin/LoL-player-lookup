"""SQLAlchemy models, engine and session helpers (mirrors sibling project)."""
from sqlalchemy import (
    create_engine, inspect, Column, String, Float, Integer, Boolean, Index, text,
)
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import StaticPool

from backend.config import DB_PATH, DATABASE_URL


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
    # % of the game (5:00 → end) this champion held a "meaningful lead" — ≥5% more total
    # gold than their lane opponent. Oracle only has 15/20/25 snapshots, so this comes
    # from `enrich_lolesports --gold` (NULL until/unless the game is resolved).
    meaningful_lead_pct = Column(Float)
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


def get_engine(db_path: str = DB_PATH, url: str | None = None):
    """Engine for ``url`` (or ``DATABASE_URL``), falling back to local SQLite at ``db_path``.

    An explicit ``db_path`` argument always wins over ``DATABASE_URL`` so callers that
    pass a path (tests, the RDS copy script's source) get SQLite.
    """
    url = url or (DATABASE_URL if db_path == DB_PATH else None)
    if url and not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    return create_engine(
        url or f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _is_sqlite(bind) -> bool:
    return bind.dialect.name == "sqlite"


# Columns added after the table first shipped. `create_all` won't ALTER an existing
# table, so we add any missing ones in place — avoids forcing a DB delete + reload.
_ADDED_COLUMNS = {
    "item1_completed_s": "INTEGER",
    "item2_completed_s": "INTEGER",
    "item3_completed_s": "INTEGER",
    "level": "INTEGER",
    "meaningful_lead_pct": "REAL",
}


def _add_missing_columns(session: Session) -> None:
    existing = {c["name"] for c in inspect(session.connection()).get_columns(
        PlayerGameStat.__tablename__)}
    for col, sqltype in _ADDED_COLUMNS.items():
        if col not in existing:
            sqltype = "DOUBLE PRECISION" if (sqltype == "REAL" and
                                             not _is_sqlite(session.bind)) else sqltype
            session.execute(
                text(f"ALTER TABLE player_game_stats ADD COLUMN {col} {sqltype}"))


def init_db(engine=None) -> None:
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        if _is_sqlite(engine):
            session.execute(text("PRAGMA journal_mode=WAL"))
        _add_missing_columns(session)
        session.commit()


def data_version(session: Session) -> int:
    """SQLite's per-connection change counter.

    Bumps whenever *another* connection commits, so a long-lived reader can tell that the
    DB changed underneath it. Cheap (no I/O). The offline backfills
    (`backend.enrich_lolesports`) write to the live DB, so the API's in-memory caches use
    this to know when to rebuild rather than serve pre-backfill data forever.

    Non-SQLite databases (RDS) have no equivalent, so this returns a constant: caches are
    built once per process. Restart the app after reloading data there.
    """
    if not _is_sqlite(session.bind):
        return 0
    return session.execute(text("PRAGMA data_version")).scalar()


_engine = None


def get_session() -> Session:
    global _engine
    if _engine is None:
        _engine = get_engine()
        init_db(_engine)
    return Session(_engine)
