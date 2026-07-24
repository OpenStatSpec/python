"""SQLite reference SQL profile for the strict OpenStatSpec wide-table contract."""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import BigInteger, Column, Float, Integer, MetaData, String, Table, Text, create_engine, insert, select
from .profiles import preflight, profile_for_url

_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_]+")


def catalog(metadata: MetaData) -> tuple[Table, Table]:
    datasets = Table(
        "dataset_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("data_table", String(255), nullable=False, unique=True),
        Column("source_format", String(16), nullable=False),
        Column("source_name", Text, nullable=False),
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
        Column("string_width", Integer),
        Column("label", Text, nullable=False, default=""),
        Column("format", String(64)),
        Column("measure", String(32)),
        Column("alignment", String(32)),
        Column("display_width", Integer),
        Column("value_labels", Text, nullable=False, default="{}"),
        Column("missing_ranges", Text, nullable=False, default="[]"),
    )
    return datasets, variables


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
    source_sha256: str = "",
    source_created_at: str | None = None, source_modified_at: str | None = None,
    imported_at: str = "",
    multiple_response_sets: str = "{}",
) -> dict[str, Any]:
    profile = profile_for_url(database_url)
    preflight(profile, len(variables))
    engine = create_engine(database_url)
    metadata = MetaData()
    datasets, variable_catalog = catalog(metadata)
    data_table = Table(
        data_table_name(dataset_id), metadata,
        Column("__case_ordinal", BigInteger, primary_key=True, nullable=False),
        *(Column(item["physical_name"], Float() if item["storage_kind"] == "numeric" else Text(),
                 nullable=item["storage_kind"] == "numeric") for item in variables),
    )
    with engine.begin() as connection:
        metadata.create_all(connection, tables=[datasets, variable_catalog])
        if connection.execute(select(datasets.c.dataset_id).where(datasets.c.dataset_id == dataset_id)).first():
            raise ValueError(f"Dataset {dataset_id!r} already exists; imports never overwrite a dataset.")
        data_table.create(connection)
        materialized = [{"__case_ordinal": ordinal, **row} for ordinal, row in enumerate(rows, start=1)]
        connection.execute(insert(datasets).values(
            dataset_id=dataset_id, data_table=data_table.name, source_format=source_format,
            source_name=source_name, source_encoding=source_encoding, case_count=len(materialized),
            source_sha256=source_sha256,
            source_created_at=source_created_at, source_modified_at=source_modified_at,
            imported_at=imported_at,
            file_label=file_label, documents=documents,
            multiple_response_sets=multiple_response_sets,
        ))
        connection.execute(insert(variable_catalog), [dict(dataset_id=dataset_id, **item) for item in variables])
        if materialized:
            connection.execute(insert(data_table), materialized)
    return {"dataset_id": dataset_id, "data_table": data_table.name, "case_count": len(materialized)}


def read_wide_dataset(*, database_url: str, dataset_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    engine = create_engine(database_url)
    metadata = MetaData()
    datasets, variable_catalog = catalog(metadata)
    with engine.connect() as connection:
        dataset = connection.execute(select(datasets).where(datasets.c.dataset_id == dataset_id)).mappings().one()
        data_table = Table(dataset["data_table"], MetaData(), autoload_with=connection)
        variables = connection.execute(
            select(variable_catalog).where(variable_catalog.c.dataset_id == dataset_id).order_by(variable_catalog.c.ordinal)
        ).mappings().all()
        rows = connection.execute(select(data_table).order_by(data_table.c.__case_ordinal)).mappings().all()
    return dict(dataset), [dict(item) for item in variables], [dict(item) for item in rows]


def validate_wide_dataset(*, database_url: str, dataset_id: str) -> dict[str, Any]:
    dataset, variables, rows = read_wide_dataset(database_url=database_url, dataset_id=dataset_id)
    profile = profile_for_url(database_url)
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

