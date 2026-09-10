from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, create_engine, event, select

from app.core.errors import AppError


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid() -> str:
    return str(uuid4())


class Store:
    """Versioned JSON aggregates; every update is an atomic SQLite transaction.

    Episode owns its ordered shots/bible/continuity. Large media, workflow profiles,
    render jobs and settings are independent records. Never hold a transaction over I/O.
    """

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.lock = RLock()
        self.engine = create_engine(
            f"sqlite:///{root / 'autodirector.sqlite3'}", connect_args={"check_same_thread": False}
        )

        @event.listens_for(self.engine, "connect")
        def configure(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA journal_mode=WAL")
            dbapi_connection.execute("PRAGMA busy_timeout=5000")

        metadata = MetaData()
        self.records = Table(
            "records",
            metadata,
            Column("id", String, primary_key=True),
            Column("kind", String, nullable=False, index=True),
            Column("parent_id", String, index=True),
            Column("version", Integer, nullable=False),
            Column("data", JSON, nullable=False),
        )
        metadata.create_all(self.engine)

    def create(
        self, kind: str, data: dict, *, id: str | None = None, parent: str | None = None
    ) -> dict:
        record = deepcopy(data)
        record.update(id=id or uid(), created_at=now(), updated_at=now(), version=1)
        with self.lock, self.engine.begin() as connection:
            connection.execute(
                self.records.insert().values(
                    id=record["id"], kind=kind, parent_id=parent, version=1, data=record
                )
            )
        return deepcopy(record)

    def get(self, kind: str, id: str) -> dict:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(self.records.c.data).where(
                    self.records.c.id == id, self.records.c.kind == kind
                )
            ).first()
        if row is None:
            raise AppError("NOT_FOUND", f"{kind} 不存在", {"id": id}, status=404)
        return deepcopy(row[0])

    def list(self, kind: str, parent: str | None = None) -> list[dict]:
        query = select(self.records.c.data).where(self.records.c.kind == kind)
        if parent is not None:
            query = query.where(self.records.c.parent_id == parent)
        with self.engine.connect() as connection:
            return sorted(
                [deepcopy(row[0]) for row in connection.execute(query)],
                key=lambda row: row["created_at"],
                reverse=True,
            )

    def update(self, kind: str, id: str, change: dict | Callable[[dict], None]) -> dict:
        with self.lock, self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(self.records).where(self.records.c.id == id, self.records.c.kind == kind)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("NOT_FOUND", f"{kind} 不存在", status=404)
            record = deepcopy(row["data"])
            if callable(change):
                change(record)
            else:
                record.update(deepcopy(change))
            record.update(id=id, updated_at=now(), version=row["version"] + 1)
            result = connection.execute(
                self.records.update()
                .where(self.records.c.id == id, self.records.c.version == row["version"])
                .values(data=record, version=record["version"])
            )
            if result.rowcount != 1:
                raise AppError("CONFLICT", "数据已变更，请刷新重试", status=409)
        return deepcopy(record)

    def delete(self, kind: str, id: str) -> None:
        with self.lock, self.engine.begin() as connection:
            connection.execute(
                self.records.delete().where(self.records.c.id == id, self.records.c.kind == kind)
            )

    def close(self) -> None:
        self.engine.dispose()
