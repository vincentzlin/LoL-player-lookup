"""Copy a loaded SQLite DB into an external database (e.g. Amazon RDS PostgreSQL).

``load_data`` + ``enrich_lolesports`` build ``data/lol_performance.db`` locally; this
pushes that finished table to the database named by ``DATABASE_URL`` (or ``--target``),
so the enrichment backfills never have to re-run against RDS.

    DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db \
        python -m backend.copy_to_db --source data/lol_performance.db

Destructive on the target, like ``load_data``: existing rows are deleted first.
"""
import argparse
import logging

from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from backend.config import DATABASE_URL, DB_PATH
from backend.database import PlayerGameStat, get_engine, init_db

log = logging.getLogger(__name__)

_CHUNK = 1000


def copy(source_path: str, target_url: str) -> int:
    src = get_engine(db_path=source_path)
    dst = get_engine(url=target_url)
    init_db(src)   # adds any columns an older source DB is missing
    init_db(dst)
    table = PlayerGameStat.__table__
    copied = 0
    with Session(src) as s_in, Session(dst) as s_out:
        s_out.execute(table.delete())
        result = s_in.execute(select(table)).mappings()
        while chunk := result.fetchmany(_CHUNK):
            s_out.execute(insert(table), [dict(r) for r in chunk])
            copied += len(chunk)
        if dst.dialect.name == "postgresql":
            # Explicit ids were inserted, so move the serial sequence past them.
            s_out.execute(text(
                "SELECT setval(pg_get_serial_sequence('player_game_stats', 'id'), "
                "COALESCE((SELECT MAX(id) FROM player_game_stats), 1))"))
        s_out.commit()
    return copied


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default=DB_PATH, help="SQLite file to read")
    ap.add_argument("--target", default=DATABASE_URL,
                    help="SQLAlchemy URL to write (default: $DATABASE_URL)")
    args = ap.parse_args()
    if not args.target:
        ap.error("no target: set DATABASE_URL or pass --target")
    n = copy(args.source, args.target)
    log.info("Copied %d rows into %s", n, args.target.split("@")[-1])


if __name__ == "__main__":
    _main()
