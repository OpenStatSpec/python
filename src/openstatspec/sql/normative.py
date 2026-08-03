"""Normative OpenStatSpec SPSS 1.0 relational catalogue.

Historical compatibility tables remain private to the adapter. Every new
import and export also writes this singular UUID-keyed public contract.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping
from uuid import uuid4

from sqlalchemy import (
    BigInteger, CheckConstraint, Column, DateTime, Float, ForeignKey, Integer,
    MetaData, String, Table, Text, UniqueConstraint, delete, insert, inspect, select, update,
)
from sqlalchemy.dialects import mysql, postgresql, sqlite

SPEC_VERSION = "1.0"
CATALOG_CONTRACT_ID = "openstatspec-strict-wide-table-v1"
CATALOG_SCHEMA_VERSION = 1
FORMAT = re.compile(r"^([A-Za-z]+)([0-9]+)(?:[.]([0-9]+))?$")


def binary64_type() -> Float:
    return (
        Float(precision=53)
        .with_variant(mysql.DOUBLE(asdecimal=False), "mysql")
        .with_variant(mysql.DOUBLE(asdecimal=False), "mariadb")
        .with_variant(postgresql.DOUBLE_PRECISION(), "postgresql")
        .with_variant(sqlite.REAL(), "sqlite")
    )


def lossless_text_type() -> Text:
    """Use unbounded catalog text on the supported MySQL-wire profiles."""
    return (
        Text()
        .with_variant(mysql.LONGTEXT(), "mysql")
        .with_variant(mysql.LONGTEXT(), "mariadb")
    )


@dataclass(frozen=True)
class NormativeTables:
    catalog_identity: Table
    dataset: Table
    operation: Table
    variable: Table
    dataset_weight_variable: Table
    value_label_set: Table
    value_label: Table
    variable_value_label_set: Table
    missing_rule: Table
    dataset_attribute: Table
    variable_attribute: Table
    document: Table
    variable_set: Table
    variable_set_member: Table
    multiple_response_set: Table
    multiple_response_member: Table
    fidelity_event: Table

    def all(self) -> tuple[Table, ...]:
        return tuple(getattr(self, field.name) for field in fields(self))


def catalog(metadata: MetaData) -> NormativeTables:
    catalog_identity = Table(
        "catalog_identity", metadata,
        Column("catalog_identity_key", Integer, primary_key=True, autoincrement=False),
        Column("contract_id", String(128), nullable=False, unique=True),
        Column("schema_version", Integer, nullable=False),
        Column("created_at", DateTime, nullable=False),
        CheckConstraint("catalog_identity_key = 1"),
        CheckConstraint(f"contract_id = '{CATALOG_CONTRACT_ID}'"),
    )
    dataset = Table(
        "dataset", metadata,
        Column("dataset_id", String(36), primary_key=True),
        Column("spec_version", String(32), nullable=False),
        Column("source_format", String(16), nullable=False),
        Column("physical_table_schema", String(255)),
        Column("physical_table_name", String(255), nullable=False),
        Column("dataset_name", String(255)),
        Column("dataset_label", lossless_text_type()),
        Column("source_encoding", String(128)),
        Column("source_hash", String(128)),
        Column("source_case_count", BigInteger, nullable=False),
        Column("imported_at", DateTime, nullable=False),
    )
    operation = Table(
        "operation", metadata,
        Column("operation_id", String(36), primary_key=True),
        Column("operation_kind", String(16), nullable=False),
        Column("status", String(16), nullable=False),
        Column("source_format", String(16)),
        Column("started_at", DateTime, nullable=False),
        Column("completed_at", DateTime),
    )
    variable = Table(
        "variable", metadata,
        Column("variable_id", String(36), primary_key=True),
        Column("dataset_id", String(36), ForeignKey("dataset.dataset_id"), nullable=False),
        Column("source_ordinal", Integer, nullable=False),
        Column("source_name", String(255), nullable=False),
        Column("physical_name", String(255), nullable=False),
        Column("storage_kind", String(16), nullable=False),
        Column("declared_string_width", Integer),
        Column("variable_label", lossless_text_type()),
        Column("print_format_family", String(64)),
        Column("print_format_width", Integer),
        Column("print_format_decimals", Integer),
        Column("write_format_family", String(64)),
        Column("write_format_width", Integer),
        Column("write_format_decimals", Integer),
        Column("measurement_level", String(32)),
        Column("variable_role", String(32)),
        Column("display_width", Integer),
        Column("display_alignment", String(32)),
        UniqueConstraint("dataset_id", "source_ordinal"),
        UniqueConstraint("dataset_id", "source_name"),
        UniqueConstraint("dataset_id", "physical_name"),
    )
    dataset_weight_variable = Table(
        "dataset_weight_variable", metadata,
        Column("dataset_id", String(36), ForeignKey("dataset.dataset_id"), primary_key=True),
        Column("variable_id", String(36), ForeignKey("variable.variable_id"), nullable=False, unique=True),
    )
    value_label_set = Table(
        "value_label_set", metadata,
        Column("value_label_set_id", String(36), primary_key=True),
        Column("dataset_id", String(36), ForeignKey("dataset.dataset_id"), nullable=False),
        Column("name", String(255)),
    )
    value_label = Table(
        "value_label", metadata,
        Column("value_label_id", String(36), primary_key=True),
        Column("value_label_set_id", String(36), ForeignKey("value_label_set.value_label_set_id"), nullable=False),
        Column("ordinal", Integer, nullable=False),
        Column("code_kind", String(16), nullable=False),
        Column("numeric_code", binary64_type()),
        Column("string_code", lossless_text_type()),
        Column("label", lossless_text_type(), nullable=False),
        UniqueConstraint("value_label_set_id", "ordinal"),
    )
    variable_value_label_set = Table(
        "variable_value_label_set", metadata,
        Column("variable_id", String(36), ForeignKey("variable.variable_id"), primary_key=True),
        Column("value_label_set_id", String(36), ForeignKey("value_label_set.value_label_set_id"), nullable=False),
    )
    missing_rule = Table(
        "missing_rule", metadata,
        Column("missing_rule_id", String(36), primary_key=True),
        Column("variable_id", String(36), ForeignKey("variable.variable_id"), nullable=False),
        Column("ordinal", Integer, nullable=False),
        Column("rule_kind", String(32), nullable=False),
        Column("code_kind", String(16)),
        Column("numeric_value", binary64_type()),
        Column("string_value", lossless_text_type()),
        Column("numeric_lower", binary64_type()),
        Column("numeric_upper", binary64_type()),
        Column("lower_special", String(16)),
        Column("upper_special", String(16)),
        UniqueConstraint("variable_id", "ordinal"),
    )
    dataset_attribute = Table(
        "dataset_attribute", metadata,
        Column("dataset_attribute_id", String(36), primary_key=True),
        Column("dataset_id", String(36), ForeignKey("dataset.dataset_id"), nullable=False),
        Column("attribute_name", String(255), nullable=False),
        Column("array_ordinal", Integer, nullable=False, default=1),
        Column("attribute_value", lossless_text_type(), nullable=False),
        UniqueConstraint("dataset_id", "attribute_name", "array_ordinal"),
    )
    variable_attribute = Table(
        "variable_attribute", metadata,
        Column("variable_attribute_id", String(36), primary_key=True),
        Column("variable_id", String(36), ForeignKey("variable.variable_id"), nullable=False),
        Column("attribute_name", String(255), nullable=False),
        Column("array_ordinal", Integer, nullable=False, default=1),
        Column("attribute_value", lossless_text_type(), nullable=False),
        UniqueConstraint("variable_id", "attribute_name", "array_ordinal"),
    )
    document = Table(
        "document", metadata,
        Column("document_id", String(36), primary_key=True),
        Column("dataset_id", String(36), ForeignKey("dataset.dataset_id"), nullable=False),
        Column("source_ordinal", Integer, nullable=False),
        Column("document_text", lossless_text_type(), nullable=False),
        UniqueConstraint("dataset_id", "source_ordinal"),
    )
    variable_set = Table(
        "variable_set", metadata,
        Column("variable_set_id", String(36), primary_key=True),
        Column("dataset_id", String(36), ForeignKey("dataset.dataset_id"), nullable=False),
        Column("source_ordinal", Integer, nullable=False),
        Column("set_name", String(255), nullable=False),
        UniqueConstraint("dataset_id", "source_ordinal"),
        UniqueConstraint("dataset_id", "set_name"),
    )
    variable_set_member = Table(
        "variable_set_member", metadata,
        Column("variable_set_id", String(36), ForeignKey("variable_set.variable_set_id"), primary_key=True),
        Column("variable_id", String(36), ForeignKey("variable.variable_id"), nullable=False),
        Column("source_ordinal", Integer, primary_key=True),
        UniqueConstraint("variable_set_id", "variable_id"),
    )
    multiple_response_set = Table(
        "multiple_response_set", metadata,
        Column("multiple_response_set_id", String(36), primary_key=True),
        Column("dataset_id", String(36), ForeignKey("dataset.dataset_id"), nullable=False),
        Column("source_ordinal", Integer, nullable=False),
        Column("set_name", String(255), nullable=False),
        Column("set_label", lossless_text_type()),
        Column("set_kind", String(4), nullable=False),
        Column("counted_value_kind", String(16)),
        Column("counted_numeric_value", binary64_type()),
        Column("counted_string_value", lossless_text_type()),
        Column("category_label_behavior", lossless_text_type()),
        Column("label_source", lossless_text_type()),
        UniqueConstraint("dataset_id", "source_ordinal"),
        UniqueConstraint("dataset_id", "set_name"),
    )
    multiple_response_member = Table(
        "multiple_response_member", metadata,
        Column("multiple_response_set_id", String(36), ForeignKey("multiple_response_set.multiple_response_set_id"), primary_key=True),
        Column("variable_id", String(36), ForeignKey("variable.variable_id"), nullable=False),
        Column("source_ordinal", Integer, primary_key=True),
        UniqueConstraint("multiple_response_set_id", "variable_id"),
    )
    fidelity_event = Table(
        "fidelity_event", metadata,
        Column("fidelity_event_id", String(36), primary_key=True),
        Column("operation_id", String(36), ForeignKey("operation.operation_id"), nullable=False),
        Column("dataset_id", String(36), ForeignKey("dataset.dataset_id")),
        Column("direction", String(16), nullable=False),
        Column("severity", String(16), nullable=False),
        Column("event_code", String(128), nullable=False),
        Column("source_item", lossless_text_type()),
        Column("detail_json", lossless_text_type(), nullable=False),
        Column("created_at", DateTime, nullable=False),
    )
    return NormativeTables(
        catalog_identity, dataset, operation, variable, dataset_weight_variable,
        value_label_set, value_label,
        variable_value_label_set, missing_rule, dataset_attribute,
        variable_attribute, document, variable_set, variable_set_member,
        multiple_response_set, multiple_response_member, fidelity_event,
    )


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def timestamp(value: str | datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    if value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
    return now()


def create(connection: Any, tables: NormativeTables) -> None:
    expected = {table.name for table in tables.all()}
    existing = set(inspect(connection).get_table_names(schema=tables.dataset.schema))
    if tables.catalog_identity.name not in existing:
        if existing:
            raise RuntimeError(
                "The selected catalog namespace is occupied by an unowned OpenStatSpec relation "
                "and has no catalog identity: " + ", ".join(sorted(existing))
            )
        tables.dataset.metadata.create_all(connection, tables=list(tables.all()))
        connection.execute(insert(tables.catalog_identity).values(
            catalog_identity_key=1,
            contract_id=CATALOG_CONTRACT_ID,
            schema_version=CATALOG_SCHEMA_VERSION,
            created_at=now(),
        ))
        return
    identities = connection.execute(select(tables.catalog_identity)).mappings().all()
    if len(identities) != 1:
        raise RuntimeError("The selected catalog must contain exactly one identity marker.")
    identity = identities[0]
    if (
        identity["catalog_identity_key"] != 1
        or identity["contract_id"] != CATALOG_CONTRACT_ID
        or identity["schema_version"] != CATALOG_SCHEMA_VERSION
    ):
        raise RuntimeError("The selected catalog namespace belongs to an incompatible contract.")
    tables.dataset.metadata.create_all(connection, tables=list(tables.all()))


def record_operation(
    connection: Any, tables: NormativeTables, *, operation_id: str,
    operation_kind: str, status: str, source_format: str | None,
    started_at: datetime | None = None, completed_at: datetime | None = None,
) -> None:
    connection.execute(insert(tables.operation).values(
        operation_id=operation_id, operation_kind=operation_kind, status=status,
        source_format=source_format, started_at=started_at or now(),
        completed_at=completed_at,
    ))


def finish_operation(connection: Any, tables: NormativeTables, *, operation_id: str, status: str) -> None:
    connection.execute(
        update(tables.operation).where(tables.operation.c.operation_id == operation_id)
        .values(status=status, completed_at=now())
    )


def record_fidelity_events(
    connection: Any, tables: NormativeTables, *, operation_id: str,
    dataset_id: str | None, direction: str, events: Iterable[Mapping[str, Any]],
) -> None:
    rows = []
    for event in events:
        detail_json = {"message": str(event.get("detail", ""))}
        details = event.get("details")
        if isinstance(details, Mapping):
            detail_json.update(details)
        rows.append({
            "fidelity_event_id": str(uuid4()), "operation_id": operation_id,
            "dataset_id": dataset_id,
            "direction": str(event.get("direction", direction)),
            "severity": str(event.get("severity", "warning")),
            "event_code": str(event["code"]), "source_item": event.get("source_item"),
            "detail_json": json.dumps(detail_json, default=str, ensure_ascii=False, sort_keys=True),
            "created_at": now(),
        })
    if rows:
        connection.execute(insert(tables.fidelity_event), rows)


def format_parts(variable: Mapping[str, Any], key: str) -> tuple[str | None, int | None, int | None]:
    encoded = variable.get(key)
    if encoded:
        try:
            decoded = json.loads(str(encoded))
        except (TypeError, json.JSONDecodeError):
            decoded = encoded
        if isinstance(decoded, (list, tuple)) and len(decoded) >= 3:
            return str(decoded[0]), int(decoded[1]), int(decoded[2])
    legacy = variable.get("format")
    if legacy:
        match = FORMAT.match(str(legacy))
        if match:
            return match.group(1).upper(), int(match.group(2)), int(match.group(3) or 0)
    return None, None, None


def member_names(value: Any) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def store_imported_dataset(
    connection: Any, tables: NormativeTables, *, dataset_name: str,
    source_format: str, physical_table_name: str, dataset_label: str,
    source_encoding: str | None, source_hash: str | None, source_case_count: int,
    imported_at: str | datetime | None, variables: list[Mapping[str, Any]],
    documents: Iterable[Mapping[str, Any]], value_labels: Iterable[Mapping[str, Any]],
    missing_rules: Iterable[Mapping[str, Any]], attributes: Iterable[Mapping[str, Any]],
    multiple_response_sets: Iterable[Mapping[str, Any]], source_extensions: Mapping[str, Any],
    case_weight_variable: str | None = None,
    dataset_id: str | None = None,
) -> str:
    dataset_id = dataset_id or str(uuid4())
    connection.execute(insert(tables.dataset).values(
        dataset_id=dataset_id, spec_version=SPEC_VERSION, source_format=source_format,
        physical_table_schema=None, physical_table_name=physical_table_name,
        dataset_name=dataset_name, dataset_label=dataset_label or None,
        source_encoding=source_encoding, source_hash=source_hash or None,
        source_case_count=source_case_count, imported_at=timestamp(imported_at),
    ))
    variable_ids: dict[int, str] = {}
    ids_by_name: dict[str, str] = {}
    for variable in variables:
        ordinal = int(variable["ordinal"])
        variable_id = str(uuid4())
        variable_ids[ordinal] = variable_id
        ids_by_name[str(variable["source_name"])] = variable_id
        pf, pw, pd = format_parts(variable, "print_format")
        wf, ww, wd = format_parts(variable, "write_format")
        if wf is None:
            wf, ww, wd = pf, pw, pd
        connection.execute(insert(tables.variable).values(
            variable_id=variable_id, dataset_id=dataset_id, source_ordinal=ordinal,
            source_name=str(variable["source_name"]), physical_name=str(variable["physical_name"]),
            storage_kind=str(variable["storage_kind"]),
            declared_string_width=variable.get("string_width"),
            variable_label=variable.get("label") or None,
            print_format_family=pf, print_format_width=pw, print_format_decimals=pd,
            write_format_family=wf, write_format_width=ww, write_format_decimals=wd,
            measurement_level=variable.get("measure"), variable_role=variable.get("role"),
            display_width=variable.get("display_width"),
            display_alignment=variable.get("alignment"),
        ))
    if case_weight_variable is not None:
        connection.execute(insert(tables.dataset_weight_variable).values(
            dataset_id=dataset_id,
            variable_id=ids_by_name[case_weight_variable],
        ))
    labels_by_variable: dict[int, list[Mapping[str, Any]]] = {}
    for row in value_labels:
        labels_by_variable.setdefault(int(row["variable_ordinal"]), []).append(row)
    for ordinal, rows in labels_by_variable.items():
        set_id = str(uuid4())
        connection.execute(insert(tables.value_label_set).values(
            value_label_set_id=set_id, dataset_id=dataset_id, name=None,
        ))
        connection.execute(insert(tables.variable_value_label_set).values(
            variable_id=variable_ids[ordinal], value_label_set_id=set_id,
        ))
        for row in rows:
            connection.execute(insert(tables.value_label).values(
                value_label_id=str(uuid4()), value_label_set_id=set_id,
                ordinal=int(row["ordinal"]),
                code_kind=(
                    "string" if str(row["value_type"]) == "text"
                    else str(row["value_type"])
                ),
                numeric_code=row.get("numeric_value"), string_code=row.get("text_value"),
                label=str(row["label"]),
            ))
    for row in missing_rules:
        variable_id = variable_ids[int(row["variable_ordinal"])]
        discrete = str(row["kind"]) == "discrete"
        code_kind = str(row["lower_type"]) if discrete else None
        if code_kind == "text":
            code_kind = "string"
        connection.execute(insert(tables.missing_rule).values(
            missing_rule_id=str(uuid4()), variable_id=variable_id,
            ordinal=int(row["ordinal"]), rule_kind="discrete" if discrete else "numeric_range",
            code_kind=code_kind if code_kind in {"numeric", "string"} else None,
            numeric_value=row.get("lower_numeric") if discrete and code_kind == "numeric" else None,
            string_value=row.get("lower_text") if discrete and code_kind == "string" else None,
            numeric_lower=None if discrete else row.get("lower_numeric"),
            numeric_upper=None if discrete else row.get("upper_numeric"),
            lower_special=None if discrete or row.get("lower_type") == "numeric" else str(row.get("lower_type")).upper(),
            upper_special=None if discrete or row.get("upper_type") == "numeric" else str(row.get("upper_type")).upper(),
        ))
    for row in attributes:
        if row["scope"] == "file":
            connection.execute(insert(tables.dataset_attribute).values(
                dataset_attribute_id=str(uuid4()), dataset_id=dataset_id,
                attribute_name=str(row["attribute_name"]),
                array_ordinal=int(row["value_ordinal"]),
                attribute_value=str(row["attribute_value"]),
            ))
        else:
            connection.execute(insert(tables.variable_attribute).values(
                variable_attribute_id=str(uuid4()),
                variable_id=variable_ids[int(row["variable_ordinal"])],
                attribute_name=str(row["attribute_name"]),
                array_ordinal=int(row["value_ordinal"]),
                attribute_value=str(row["attribute_value"]),
            ))
    for row in documents:
        connection.execute(insert(tables.document).values(
            document_id=str(uuid4()), dataset_id=dataset_id,
            source_ordinal=int(row["ordinal"]), document_text=str(row["text"]),
        ))
    variable_sets = source_extensions.get("spss.variable_sets")
    if isinstance(variable_sets, Mapping):
        for set_ordinal, (set_name, raw_members) in enumerate(variable_sets.items(), start=1):
            set_id = str(uuid4())
            connection.execute(insert(tables.variable_set).values(
                variable_set_id=set_id, dataset_id=dataset_id,
                source_ordinal=set_ordinal, set_name=str(set_name),
            ))
            for ordinal, member in enumerate(member_names(raw_members), start=1):
                if member not in ids_by_name:
                    raise ValueError(f"Variable set {set_name!r} references unknown variable {member!r}.")
                connection.execute(insert(tables.variable_set_member).values(
                    variable_set_id=set_id, variable_id=ids_by_name[member],
                    source_ordinal=ordinal,
                ))
    mr_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for row in multiple_response_sets:
        mr_by_name.setdefault(str(row["set_name"]), []).append(row)
    for set_ordinal, (set_name, rows) in enumerate(mr_by_name.items(), start=1):
        first = rows[0]
        set_id = str(uuid4())
        behavior = "counted_values" if first.get("use_category_labels") else "variable_labels"
        label_source = "variable_label" if first.get("use_first_var_label") else "set_label"
        counted_kind = first.get("counted_value_type")
        if counted_kind == "text":
            counted_kind = "string"
        connection.execute(insert(tables.multiple_response_set).values(
            multiple_response_set_id=set_id, dataset_id=dataset_id,
            source_ordinal=set_ordinal, set_name=set_name,
            set_label=first.get("label"), set_kind=str(first["kind"]),
            counted_value_kind=counted_kind,
            counted_numeric_value=first.get("counted_numeric"),
            counted_string_value=first.get("counted_text"),
            category_label_behavior=behavior, label_source=label_source,
        ))
        for row in rows:
            member = row.get("variable_name")
            if member is None:
                continue
            if str(member) not in ids_by_name:
                raise ValueError(f"Multiple-response set {set_name!r} references unknown variable {member!r}.")
            connection.execute(insert(tables.multiple_response_member).values(
                multiple_response_set_id=set_id, variable_id=ids_by_name[str(member)],
                source_ordinal=int(row["member_ordinal"]),
            ))
    return dataset_id


def delete_dataset_representation(
    connection: Any, tables: NormativeTables, dataset_id: str,
) -> None:
    """Delete one partially written dataset while retaining operation audit rows."""
    variable_ids = select(tables.variable.c.variable_id).where(
        tables.variable.c.dataset_id == dataset_id
    )
    label_set_ids = select(tables.value_label_set.c.value_label_set_id).where(
        tables.value_label_set.c.dataset_id == dataset_id
    )
    variable_set_ids = select(tables.variable_set.c.variable_set_id).where(
        tables.variable_set.c.dataset_id == dataset_id
    )
    response_set_ids = select(
        tables.multiple_response_set.c.multiple_response_set_id
    ).where(tables.multiple_response_set.c.dataset_id == dataset_id)

    connection.execute(delete(tables.fidelity_event).where(
        tables.fidelity_event.c.dataset_id == dataset_id
    ))
    connection.execute(delete(tables.dataset_weight_variable).where(
        tables.dataset_weight_variable.c.dataset_id == dataset_id
    ))
    connection.execute(delete(tables.variable_value_label_set).where(
        tables.variable_value_label_set.c.variable_id.in_(variable_ids)
    ))
    connection.execute(delete(tables.missing_rule).where(
        tables.missing_rule.c.variable_id.in_(variable_ids)
    ))
    connection.execute(delete(tables.variable_attribute).where(
        tables.variable_attribute.c.variable_id.in_(variable_ids)
    ))
    connection.execute(delete(tables.variable_set_member).where(
        tables.variable_set_member.c.variable_set_id.in_(variable_set_ids)
    ))
    connection.execute(delete(tables.multiple_response_member).where(
        tables.multiple_response_member.c.multiple_response_set_id.in_(response_set_ids)
    ))
    connection.execute(delete(tables.value_label).where(
        tables.value_label.c.value_label_set_id.in_(label_set_ids)
    ))
    connection.execute(delete(tables.value_label_set).where(
        tables.value_label_set.c.dataset_id == dataset_id
    ))
    connection.execute(delete(tables.dataset_attribute).where(
        tables.dataset_attribute.c.dataset_id == dataset_id
    ))
    connection.execute(delete(tables.document).where(
        tables.document.c.dataset_id == dataset_id
    ))
    connection.execute(delete(tables.variable_set).where(
        tables.variable_set.c.dataset_id == dataset_id
    ))
    connection.execute(delete(tables.multiple_response_set).where(
        tables.multiple_response_set.c.dataset_id == dataset_id
    ))
    connection.execute(delete(tables.variable).where(
        tables.variable.c.dataset_id == dataset_id
    ))
    connection.execute(delete(tables.dataset).where(
        tables.dataset.c.dataset_id == dataset_id
    ))


def dataset_id_for_name(connection: Any, tables: NormativeTables, dataset_name: str) -> str:
    return str(connection.execute(
        select(tables.dataset.c.dataset_id).where(tables.dataset.c.dataset_name == dataset_name)
    ).scalar_one())
