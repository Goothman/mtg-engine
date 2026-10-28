from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import ROOT, Settings
from .models import Base


def make_engine(settings: Settings) -> Engine:
    url = settings.database_url
    kwargs = {}
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////") and ":memory:" not in url:
        path = ROOT / url.removeprefix("sqlite:///")
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    if ":memory:" in url:
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


class Database:
    def __init__(self, settings: Settings):
        self.engine = make_engine(settings)
        self.sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Minimal forward migration: add columns introduced after a table was created."""
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                have = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in have:
                        continue
                    ddl = col.type.compile(dialect=self.engine.dialect)
                    default = col.default.arg if col.default is not None and col.default.is_scalar else None
                    sql = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}"
                    if isinstance(default, bool):
                        sql += f" NOT NULL DEFAULT {int(default) if self.engine.dialect.name == 'sqlite' else str(default).upper()}"
                    elif isinstance(default, (int, float)):
                        sql += f" DEFAULT {default}"
                    conn.execute(text(sql))

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


__all__ = ["Database", "Path"]
