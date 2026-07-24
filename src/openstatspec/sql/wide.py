"""SQLite reference SQL profile for the strict OpenStatSpec wide-table contract."""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import BigInteger, Column, Float, Integer, MetaData, String, Table, Text, create_engine, insert, select

_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_]+")


def catalog(metadata: MetaData) -> tuple[Table, Table]:
    datasets = Table(
        "dataset_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("data_table", String(255), nullable=False, unique=True),
        Column("source_format", String(16), nullable=False),
        Column("source_name", Text, nullable=False),
        Column("case_count", BigInteger, nullable=False),
        Column("file_label", Text, nullable=False, default=""),
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
) -> dict[str, Any]:
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
            source_name=source_name, case_count=len(materialized), file_label=file_label,
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
    if not variables:
        raise ValueError("A conforming dataset needs at least one source variable.")
    if [row["__case_ordinal"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("Case ordinals are not contiguous source order.")
    return {"dataset_id": dataset["dataset_id"], "valid": True, "case_count": len(rows), "variable_count": len(variables)}

