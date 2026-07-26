"""SQLite reference SQL profile for the strict OpenStatSpec wide-table contract."""

import json
import re
from datetime import UTC, datetime
from uuid import uuid4
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import delete, BigInteger, Column, Float, Integer, MetaData, String, Table, Text, create_engine, insert, select, update
from sqlalchemy.dialects import mysql, postgresql, sqlite
from .profiles import preflight, validate_connection_url

_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_]+")


def binary64_type() -> Float:
    """Return the required IEEE-754 binary64 SQL type for every profile.

    ``Float()`` is not adequate as a portable declaration: SQLAlchemy compiles
    it to ``FLOAT`` for MySQL, which is single precision there. The strict
    profile therefore declares the physical type explicitly for every target.
    """
    return (
        Float(precision=53)
        .with_variant(mysql.DOUBLE(asdecimal=False), "mysql")
        .with_variant(mysql.DOUBLE(asdecimal=False), "mariadb")
        .with_variant(postgresql.DOUBLE_PRECISION(), "postgresql")
        .with_variant(sqlite.REAL(), "sqlite")
    )


def catalog(metadata: MetaData) -> tuple[Table, Table, Table, Table]:
    datasets = Table(
        "dataset_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("data_table", String(255), nullable=False, unique=True),
        Column("source_format", String(16), nullable=False),
        Column("source_name", Text, nullable=False),
        Column("source_table_name", Text),
        Column("source_sha256", String(64), nullable=False),
        Column("source_created_at", String(40)),
        Column("source_modified_at", String(40)),
        Column("imported_at", String(40), nullable=False),
        Column("source_encoding", String(128)),
        Column("case_count", BigInteger, nullable=False),
        Column("file_label", Text, nullable=False, default=""),
        Column("documents", Text, nullable=False, default="[]"),
        Column("multiple_response_sets", Text, nullable=False, default="{}"),
    )
    variables = Table(
        "variable_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("ordinal", Integer, primary_key=True),
        Column("source_name", String(255), nullable=False),
        Column("physical_name", String(255), nullable=False),
        Column("storage_kind", String(16), nullable=False),
        Column("readstat_storage_type", String(32)),
        Column("string_width", Integer),
        Column("label", Text, nullable=False, default=""),
        Column("format", String(64)),
        Column("measure", String(32)),
        Column("alignment", String(32)),
        Column("display_width", Integer),
        Column("value_labels", Text, nullable=False, default="{}"),
        Column("missing_ranges", Text, nullable=False, default="[]"),
    )
    fidelity_events = Table(
        "fidelity_event_catalog", metadata,
        Column("operation_id", String(36), primary_key=True),
        Column("ordinal", Integer, primary_key=True),
        Column("dataset_id", String(255)),
        Column("direction", String(16), nullable=False),
        Column("severity", String(16), nullable=False),
        Column("detail", Text, nullable=False),
        Column("details", Text, nullable=False, default="{}"),
        Column("code", String(128), nullable=False),
    )
    operations = Table(
        "operation_catalog", metadata,
        Column("operation_id", String(36), primary_key=True),
        Column("direction", String(16), nullable=False),
        Column("status", String(16), nullable=False),
        Column("dataset_id", String(255)),
        Column("source", Text),
        Column("destination", Text),
        Column("created_at", String(40), nullable=False),
        Column("completed_at", String(40)),
        Column("details", Text, nullable=False, default="{}"),
    )
    return datasets, variables, fidelity_events, operations


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _event_rows(
    *, operation_id: str, dataset_id: str | None, direction: str,
    fidelity_events: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize the public compact diagnostic shape into durable catalog rows."""
    rows: list[dict[str, Any]] = []
    for ordinal, event in enumerate(fidelity_events, start=1):
        detail = str(event["detail"])
        details = event.get("details", {})
        rows.append({
            "operation_id": operation_id, "ordinal": ordinal, "dataset_id": dataset_id,
            "direction": str(event.get("direction", direction)),
            "severity": str(event.get("severity", "warning")),
            "code": str(event["code"]), "detail": detail,
            "details": json.dumps(details, default=str, sort_keys=True),
        })

    return rows



def _record_failed_preflight(
    *, engine: Any, metadata: MetaData, datasets: Table, variable_catalog: Table,
    multiple_response_catalog: Table, fidelity_event_catalog: Table,
    operation_catalog: Table, operation_id: str, source_name: str,
    variable_count: int, profile_name: str, error: Exception,
) -> None:
    """Persist a failed preflight without creating any source dataset state."""
    with engine.begin() as connection:
        metadata.create_all(connection, tables=[
            datasets, variable_catalog, multiple_response_catalog,
            fidelity_event_catalog, operation_catalog,
        ])
        connection.execute(insert(operation_catalog).values(
            operation_id=operation_id, direction="import", status="failed", dataset_id=None,
            source=source_name, created_at=_now(), completed_at=_now(),
            details=json.dumps({"reason": "preflight", "variable_count": variable_count}, sort_keys=True),
        ))
        connection.execute(insert(fidelity_event_catalog), _event_rows(
            operation_id=operation_id, dataset_id=None, direction="import", fidelity_events=({
                "code": "target-capability-exceeded", "detail": str(error), "severity": "error",
                "details": {"variable_count": variable_count, "profile": profile_name},
            },),
        ))

def multiple_response_set_catalog(metadata: MetaData) -> Table:
    return Table(
        "multiple_response_set_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("set_name", String(255), primary_key=True),
        Column("member_ordinal", Integer, primary_key=True),
        Column("kind", String(16)), Column("label", Text),
        Column("counted_value", Text), Column("variable_name", String(255)),
        Column("definition", Text, nullable=False),
    )

def multiple_response_set_rows(dataset_id: str, definitions: str) -> list[dict[str, Any]]:
    sets = json.loads(definitions)
    rows = []
    for set_name, definition in sets.items():
        members = definition.get("variable_list", definition.get("variables", []))
        if isinstance(members, str):
            members = members.split()
        for ordinal, variable_name in enumerate(members or [None], start=1):
            rows.append({
                "dataset_id": dataset_id, "set_name": set_name, "member_ordinal": ordinal,
                "kind": definition.get("set_type", definition.get("type")),
                "label": definition.get("label"),
                "counted_value": str(definition.get("counted_value", definition.get("countedvalue", ""))),
                "variable_name": variable_name, "definition": json.dumps(definition, default=str, sort_keys=True),
            })
    return rows

def physical_name(source_name: str, used: set[str]) -> str:
    stem = _IDENTIFIER.sub("_", source_name).strip("_").lower() or "variable"
    stem = stem[:54]
    candidate, suffix = stem, 2
    while candidate.lower() in used or candidate.startswith("__"):
        candidate = f"{stem[:50]}_{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def data_table_name(dataset_id: str) -> str:
    stem = _IDENTIFIER.sub("_", dataset_id).strip("_").lower() or "dataset"
    return f"data_{stem[:48]}"


def create_wide_dataset(
    *, database_url: str, dataset_id: str, source_name: str, source_format: str,
    rows: Iterable[Mapping[str, Any]], variables: list[dict[str, Any]], file_label: str = "",
    source_encoding: str | None = None, documents: str = "[]",
    source_table_name: str | None = None,
    source_sha256: str = "",
    source_created_at: str | None = None, source_modified_at: str | None = None,
    imported_at: str = "",
    multiple_response_sets: str = "{}",
    fidelity_events: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    profile = validate_connection_url(database_url)
    engine = create_engine(database_url)
    metadata = MetaData()
    datasets, variable_catalog, fidelity_event_catalog, operation_catalog = catalog(metadata)
    multiple_response_catalog = multiple_response_set_catalog(metadata)
    operation_id = str(uuid4())
    try:
        preflight(profile, len(variables))
    except Exception as error:
        _record_failed_preflight(
            engine=engine, metadata=metadata, datasets=datasets,
            variable_catalog=variable_catalog, multiple_response_catalog=multiple_response_catalog,
            fidelity_event_catalog=fidelity_event_catalog, operation_catalog=operation_catalog,
            operation_id=operation_id, source_name=source_name, variable_count=len(variables),
            profile_name=profile.name, error=error,
        )
        raise
    data_table = Table(
        data_table_name(dataset_id), metadata,
        Column("__case_ordinal", BigInteger, primary_key=True, nullable=False),
        *(Column(item["physical_name"], binary64_type() if item["storage_kind"] == "numeric" else Text(),
                 nullable=item["storage_kind"] == "numeric") for item in variables),
    )
    with engine.begin() as connection:
        metadata.create_all(connection, tables=[datasets, variable_catalog, multiple_response_catalog, fidelity_event_catalog, operation_catalog])
        connection.execute(insert(operation_catalog).values(
            operation_id=operation_id, direction="import", status="running", dataset_id=dataset_id,
            source=source_name, created_at=_now(), details=json.dumps({"variable_count": len(variables)}, sort_keys=True),
        ))
        if connection.execute(select(datasets.c.dataset_id).where(datasets.c.dataset_id == dataset_id)).first():
            raise ValueError(f"Dataset {dataset_id!r} already exists; imports never overwrite a dataset.")
        if connection.execute(select(datasets.c.dataset_id).where(datasets.c.data_table == data_table.name)).first():
            raise ValueError(f"Dataset ID {dataset_id!r} collides with an existing physical data-table name; import was not started.")
        data_table.create(connection)
        materialized = [{"__case_ordinal": ordinal, **row} for ordinal, row in enumerate(rows, start=1)]
        connection.execute(insert(datasets).values(
            dataset_id=dataset_id, data_table=data_table.name, source_format=source_format,
            source_name=source_name, source_encoding=source_encoding, case_count=len(materialized),
            source_table_name=source_table_name,
            source_sha256=source_sha256,
            source_created_at=source_created_at, source_modified_at=source_modified_at,
            imported_at=imported_at,
            file_label=file_label, documents=documents,
            multiple_response_sets=multiple_response_sets,
        ))
        connection.execute(insert(variable_catalog), [dict(dataset_id=dataset_id, **item) for item in variables])
        mrset_rows = multiple_response_set_rows(dataset_id, multiple_response_sets)
        if mrset_rows:
            connection.execute(insert(multiple_response_catalog), mrset_rows)
        event_rows = _event_rows(
            operation_id=operation_id, dataset_id=dataset_id, direction="import", fidelity_events=fidelity_events,
        )
        if event_rows:
            connection.execute(insert(fidelity_event_catalog), event_rows)
        if materialized:
            try:
                connection.execute(insert(data_table), materialized)
            except Exception:
                data_table.drop(connection, checkfirst=True)
                connection.execute(delete(multiple_response_catalog).where(multiple_response_catalog.c.dataset_id == dataset_id))
                connection.execute(delete(fidelity_event_catalog).where(fidelity_event_catalog.c.dataset_id == dataset_id))
                connection.execute(delete(variable_catalog).where(variable_catalog.c.dataset_id == dataset_id))
                connection.execute(delete(datasets).where(datasets.c.dataset_id == dataset_id))
                connection.commit()
                raise
        connection.execute(update(operation_catalog).where(operation_catalog.c.operation_id == operation_id).values(
            status="succeeded", completed_at=_now(),
        ))
    return {"dataset_id": dataset_id, "data_table": data_table.name, "case_count": len(materialized), "operation_id": operation_id}


def read_wide_dataset(*, database_url: str, dataset_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    engine = create_engine(database_url)
    metadata = MetaData()
    datasets, variable_catalog, _, _ = catalog(metadata)
    with engine.connect() as connection:
        dataset = connection.execute(select(datasets).where(datasets.c.dataset_id == dataset_id)).mappings().one()
        data_table = Table(dataset["data_table"], MetaData(), autoload_with=connection)
        variables = connection.execute(
            select(variable_catalog).where(variable_catalog.c.dataset_id == dataset_id).order_by(variable_catalog.c.ordinal)
        ).mappings().all()
        rows = connection.execute(select(data_table).order_by(data_table.c.__case_ordinal)).mappings().all()
    return dict(dataset), [dict(item) for item in variables], [dict(item) for item in rows]


def read_fidelity_events(*, database_url: str, dataset_id: str) -> tuple[dict[str, str], ...]:
    """Read import-time fidelity diagnostics for a catalogued dataset."""
    engine = create_engine(database_url)
    metadata = MetaData()
    _, _, fidelity_event_catalog, _ = catalog(metadata)
    with engine.connect() as connection:
        fidelity_event_catalog.create(connection, checkfirst=True)
        events = connection.execute(
            select(fidelity_event_catalog)
            .where(fidelity_event_catalog.c.dataset_id == dataset_id)
            .order_by(fidelity_event_catalog.c.code)
        ).mappings().all()
    return tuple({"code": item["code"], "detail": item["detail"]} for item in events)



def record_export_operation(
    *, database_url: str, dataset_id: str, destination: str,
    allowed_fidelity_events: Iterable[Mapping[str, Any]],
) -> str:
    """Persist a completed export and the fidelity loss explicitly accepted by its caller."""
    engine = create_engine(database_url)
    metadata = MetaData()
    datasets, variables, fidelity_events, operations = catalog(metadata)
    multiple_response = multiple_response_set_catalog(metadata)
    operation_id = str(uuid4())
    events = tuple(allowed_fidelity_events)
    with engine.begin() as connection:
        metadata.create_all(connection, tables=[datasets, variables, multiple_response, fidelity_events, operations])
        connection.execute(insert(operations).values(
            operation_id=operation_id, direction="export", status="succeeded", dataset_id=dataset_id,
            destination=destination, created_at=_now(), completed_at=_now(),
            details=json.dumps({"allow_loss": [event["code"] for event in events]}, sort_keys=True),
        ))
        rows = _event_rows(
            operation_id=operation_id, dataset_id=dataset_id, direction="export",
            fidelity_events=({**event, "severity": event.get("severity", "warning"),
                              "details": {**event.get("details", {}), "accepted_by_user": True}}
                             for event in events),
        )
        if rows:
            connection.execute(insert(fidelity_events), rows)
    return operation_id

def validate_wide_dataset(*, database_url: str, dataset_id: str) -> dict[str, Any]:
    dataset, variables, rows = read_wide_dataset(database_url=database_url, dataset_id=dataset_id)
    profile = validate_connection_url(database_url)
    preflight(profile, len(variables))
    if not variables:
        raise ValueError("A conforming dataset needs at least one source variable.")
    expected_columns = {"__case_ordinal", *(item["physical_name"] for item in variables)}
    reflected_table = Table(dataset["data_table"], MetaData(), autoload_with=create_engine(database_url))
    reflected_columns = {column.name: column for column in reflected_table.columns}
    actual_columns = set(reflected_columns)
    if actual_columns != expected_columns:
        raise ValueError("Data-table columns do not match the registered source variables.")
    if dataset["case_count"] != len(rows):
        raise ValueError("Registered case count does not match the data table.")
    if len({item["physical_name"] for item in variables}) != len(variables):
        raise ValueError("Registered physical variable names are not unique.")
    case_ordinal = reflected_columns["__case_ordinal"]
    if not isinstance(case_ordinal.type, BigInteger) or case_ordinal.nullable:
        raise ValueError("Reserved case ordinal must be a non-null BIGINT column.")
    for item in variables:
        column = reflected_columns[item["physical_name"]]
        if item["storage_kind"] == "numeric":
            if not isinstance(column.type, Float) or not column.nullable:
                raise ValueError(f"Numeric variable {item['source_name']!r} must be a nullable binary64 column.")
        elif not isinstance(column.type, Text) or column.nullable:
            raise ValueError(f"String variable {item['source_name']!r} must be a non-null text column.")
    if [row["__case_ordinal"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("Case ordinals are not contiguous source order.")
    return {"dataset_id": dataset["dataset_id"], "valid": True, "case_count": len(rows), "variable_count": len(variables)}

