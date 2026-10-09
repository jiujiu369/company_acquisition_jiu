from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(data_dir: Path):
    database_path = data_dir / "database" / "app.db"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def migrate_collection_schema(engine):
    """Only add columns needed to read older records; never rewrite historical rows."""
    additions = {"tasks": {"available_at": "DATETIME"}}
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {r[1] for r in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name,declaration in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
