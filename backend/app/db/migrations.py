"""Transactional JSON-record migrations; submitted graphs and media stay intact."""

from copy import deepcopy

from sqlalchemy import Column, Integer, MetaData, String, Table, select

from app.db.store import now
from app.workflows.ownership import canonicalize

SCHEMA_VERSION = 1


def migrate(store) -> None:
    metadata = MetaData()
    migrations = Table(
        "schema_migrations",
        metadata,
        Column("version", Integer, primary_key=True),
        Column("applied_at", String, nullable=False),
    )
    metadata.create_all(store.engine)
    with store.lock, store.engine.begin() as connection:
        applied = set(connection.execute(select(migrations.c.version)).scalars())
        if SCHEMA_VERSION in applied:
            return
        rows = connection.execute(select(store.records)).mappings().all()
        for row in rows:
            record = deepcopy(row["data"])
            if row["kind"] == "workflow":
                canonicalize(record)
                record.setdefault("parameter_rules", {})
            elif row["kind"] == "job" and record.get("profile_snapshot"):
                canonicalize(record["profile_snapshot"])
                record.setdefault("advanced_mode", bool(record.get("parameter_values")))
                record.setdefault("parameter_sources", {})
            elif row["kind"] == "episode":
                record.setdefault(
                    "advanced_mode",
                    bool(
                        record.get("image_parameters")
                        or record.get("video_parameters")
                        or record.get("width")
                    ),
                )
                record.setdefault("workflow_overrides", {})
                record.setdefault("reference_workflow_id", None)
                record.setdefault("allowed_asset_ids", [])
                record["legacy_shot_limit"] = len(record.get("shots", []))
            else:
                continue
            if record != row["data"]:
                version = row["version"] + 1
                record.update(version=version, updated_at=now())
                connection.execute(
                    store.records.update()
                    .where(store.records.c.id == row["id"])
                    .values(data=record, version=version)
                )
        connection.execute(migrations.insert().values(version=SCHEMA_VERSION, applied_at=now()))
