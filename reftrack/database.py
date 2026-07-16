"""Database engine, session factory, initialization, and backups."""

import logging
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger("reftrack.database")

DB_PATH = os.environ.get("REFTRACK_DB", "reftrack.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _enable_sqlite_fks(dbapi_connection, connection_record):
    # SQLite ships with foreign key enforcement OFF per-connection.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _migrate_add_columns() -> None:
    """Additive auto-migration: ALTER TABLE ADD COLUMN for any model column
    missing from an existing table. Never drops or rewrites data."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                ddl += col.type.compile(engine.dialect)
                default = col.default.arg if col.default is not None else None
                if default is not None and not callable(default):
                    if isinstance(default, bool):
                        ddl += f" DEFAULT {int(default)}"
                    elif isinstance(default, (int, float)):
                        ddl += f" DEFAULT {default}"
                    else:
                        ddl += f" DEFAULT '{default}'"
                conn.execute(text(ddl))


def init_db() -> None:
    from reftrack import models  # noqa: F401  (register mappings)

    Base.metadata.create_all(engine)
    _migrate_add_columns()


def backup_db(keep: int = 30) -> str | None:
    """Copy the DB to backups/<name>-YYYYMMDD.db (once per day), pruning to the
    last `keep` copies. Returns the backup path, or None if nothing was copied.

    Call this BEFORE init_db(): the point is to capture the pre-migration state,
    and a migration that mutates the only copy of 3-year-retention records is
    exactly what a backup is for.

    Distinguishes its two "no copy" outcomes in the log so a misconfigured
    REFTRACK_DB can't silently mean "backups never happen".
    """
    import shutil
    from datetime import date
    from pathlib import Path

    src = Path(DB_PATH)
    if not src.exists():
        logger.info(
            "No database at %s yet (first run?) - nothing to back up.", src
        )
        return None
    backup_dir = src.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    dest = backup_dir / f"{src.stem}-{date.today():%Y%m%d}.db"
    if dest.exists():
        logger.debug("Backup for today already exists at %s.", dest)
        return None
    try:
        shutil.copy2(src, dest)
    except OSError:
        logger.exception(
            "BACKUP FAILED for %s - your records are not protected. Copy the "
            "database file manually.", src
        )
        return None
    for old in sorted(backup_dir.glob(f"{src.stem}-*.db"))[:-keep]:
        old.unlink()
    logger.info("Database backed up to %s", dest)
    return str(dest)


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
