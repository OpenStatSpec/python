"""SQLite reference SQL profile for the strict OpenStatSpec wide-table contract."""

import json
import math
import re
import sys
from datetime import UTC, datetime
from uuid import uuid4
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import delete, BigInteger, Boolean, Column, Float, Integer, MetaData, String, Table, Text, create_engine, insert, inspect, select, text, update
from sqlalchemy.dialects import mysql, postgresql, sqlite
from ..core import UnsupportedOperationError
from .profiles import preflight, validate_connection_url

_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_]+")


class CatalogPreflightError(UnsupportedOperationError):
    """A semantic SPSS catalog rule failed before an import or writer runs."""

    def __init__(self, code: str, detail: str, *, details: Mapping[str, Any]) -> None:
        super().__init__(f"OpenStatSpec catalog preflight failed [{code}]: {detail}")
        self.code = code
        self.details = {"reason": code, **details}


def _catalog_error(code: str, detail: str, **details: Any) -> CatalogPreflightError:
    return CatalogPreflightError(code, detail, details=details)


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
        Column("file_attributes", Text, nullable=False, default="{}"),
        Column("case_weight_variable", String(255)),
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
        Column("role", String(32)),
        Column("alignment", String(32)),
        Column("display_width", Integer),
        Column("attributes", Text, nullable=False, default="{}"),
        Column("compat_name", String(255)),
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

def _migrate_catalog_columns(
    connection: Any, datasets: Table, variables: Table, multiple_response: Table,
) -> None:
    """Add pyspssio metadata columns to catalogs created by earlier adapters.

    This additive migration is intentionally small and portable: no existing
    source data or dictionary row is rewritten, while a later import can store
    the newly observable metadata alongside it.
    """
    inspector = inspect(connection)
    additions = {
        datasets.name: {
            "file_attributes": "TEXT NOT NULL DEFAULT '{}'",
            "case_weight_variable": "VARCHAR(255)",
        },
        variables.name: {
            "role": "VARCHAR(32)",
            "attributes": "TEXT NOT NULL DEFAULT '{}'",
            "compat_name": "VARCHAR(255)",
        },
        multiple_response.name: {
            "is_dichotomy": "BOOLEAN",
            "use_category_labels": "BOOLEAN",
            "use_first_var_label": "BOOLEAN",
            "counted_value_type": "VARCHAR(16)",
            "counted_numeric": "DOUBLE",
            "counted_text": "TEXT",
        },
    }
    preparer = connection.dialect.identifier_preparer
    for table_name, columns in additions.items():
        if not inspector.has_table(table_name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for name, declaration in columns.items():
            if name not in existing:
                connection.execute(text(
                    f"ALTER TABLE {preparer.quote(table_name)} ADD COLUMN "
                    f"{preparer.quote(name)} {declaration}"
                ))



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
            details=json.dumps({"reason": "preflight", "variable_count": variable_count,
                "capability": getattr(error, "details", {})}, sort_keys=True),
        ))
        connection.execute(insert(fidelity_event_catalog), _event_rows(
            operation_id=operation_id, dataset_id=None, direction="import", fidelity_events=({
                "code": getattr(error, "code", "target-capability-exceeded"), "detail": str(error), "severity": "error",
                "details": {"variable_count": variable_count, "profile": profile_name,
                            **getattr(error, "details", {})},
            },),
        ))

def multiple_response_set_catalog(metadata: MetaData) -> Table:
    return Table(
        "multiple_response_set_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("set_name", String(255), primary_key=True),
        Column("member_ordinal", Integer, primary_key=True),
        Column("kind", String(16)), Column("label", Text),
        Column("is_dichotomy", Boolean),
        Column("use_category_labels", Boolean),
        Column("use_first_var_label", Boolean),
        Column("counted_value", Text),
        Column("counted_value_type", String(16)),
        Column("counted_numeric", binary64_type()),
        Column("counted_text", Text),
        Column("variable_name", String(255)),
        Column("definition", Text, nullable=False),
    )


def source_extension_catalog(metadata: MetaData) -> Table:
    """Namespaced raw source semantics retained even when export is fail-closed."""
    return Table(
        "source_extension_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("extension_key", String(255), primary_key=True),
        Column("payload", Text, nullable=False),
    )


def source_extension_rows(dataset_id: str, extensions: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"dataset_id": dataset_id, "extension_key": str(key),
         "payload": json.dumps(payload, default=str, ensure_ascii=False, sort_keys=True)}
        for key, payload in sorted(extensions.items())
    ]


def document_catalog(metadata: MetaData) -> Table:
    """Ordered file documents, normalized independently from the legacy JSON column."""
    return Table(
        "document_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("ordinal", Integer, primary_key=True),
        Column("text", Text, nullable=False),
    )


def value_label_catalog(metadata: MetaData) -> Table:
    """Typed, ordered value labels; JSON on variable_catalog remains a read fallback."""
    return Table(
        "value_label_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("variable_ordinal", Integer, primary_key=True),
        Column("ordinal", Integer, primary_key=True),
        Column("value_type", String(16), nullable=False),
        Column("numeric_value", binary64_type()),
        Column("text_value", Text),
        Column("label", Text, nullable=False),
    )


def missing_rule_catalog(metadata: MetaData) -> Table:
    """Typed inclusive SPSS user-missing intervals, including discrete values as lo == hi."""
    return Table(
        "missing_rule_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("variable_ordinal", Integer, primary_key=True),
        Column("ordinal", Integer, primary_key=True),
        Column("kind", String(16), nullable=False),
        Column("lower_type", String(16), nullable=False),
        Column("lower_numeric", binary64_type()),
        Column("lower_text", Text),
        Column("upper_type", String(16), nullable=False),
        Column("upper_numeric", binary64_type()),
        Column("upper_text", Text),
        Column("lower_inclusive", Boolean, nullable=False, default=True),
        Column("upper_inclusive", Boolean, nullable=False, default=True),
    )


def attribute_catalog(metadata: MetaData) -> Table:
    """Ordered SPSS custom-attribute values for files and variables.

    SPSS custom attributes are text-valued, but one attribute name can carry
    an ordered array of values. ``scope`` is ``file`` for a file attribute
    (with ``variable_ordinal == 0``) and ``variable`` for an attribute of one
    source variable. This table is authoritative whenever it contains rows;
    the JSON columns on older catalogs remain a migration fallback.
    """
    return Table(
        "attribute_catalog", metadata,
        Column("dataset_id", String(255), primary_key=True),
        Column("scope", String(16), primary_key=True),
        Column("variable_ordinal", Integer, primary_key=True),
        Column("attribute_ordinal", Integer, primary_key=True),
        Column("value_ordinal", Integer, primary_key=True),
        Column("attribute_name", String(255), nullable=False),
        Column("attribute_value", Text, nullable=False),
    )


def _attribute_values(value: Any) -> list[str]:
    """Normalize one attribute scalar or ordered array without coercing it."""
    if isinstance(value, (list, tuple)):
        return ["" if item is None else str(item) for item in value]
    return ["" if value is None else str(value)]


def attribute_rows(
    dataset_id: str, variables: Iterable[Mapping[str, Any]], *,
    file_attributes: Mapping[str, Any] | None = None,
    variable_attributes: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Encode ordered file and variable attributes into canonical rows."""
    rows: list[dict[str, Any]] = []

    def append(scope: str, variable_ordinal: int, attributes: Mapping[str, Any] | None) -> None:
        if not attributes:
            return
        for attribute_ordinal, (name, value) in enumerate(attributes.items(), start=1):
            for value_ordinal, text_value in enumerate(_attribute_values(value), start=1):
                rows.append({
                    "dataset_id": dataset_id, "scope": scope,
                    "variable_ordinal": variable_ordinal,
                    "attribute_ordinal": attribute_ordinal,
                    "value_ordinal": value_ordinal,
                    "attribute_name": str(name), "attribute_value": text_value,
                })

    append("file", 0, file_attributes)
    attributes_by_variable = variable_attributes or {}
    for variable in variables:
        append("variable", int(variable["ordinal"]),
               attributes_by_variable.get(str(variable["source_name"])))
    return rows


def attributes_from_rows(
    rows: Iterable[Mapping[str, Any]], *, variables: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Rebuild ordered attributes; rows, not legacy JSON, are authoritative."""
    by_ordinal = {int(item["ordinal"]): str(item["source_name"]) for item in variables}
    grouped: dict[tuple[str, int], dict[str, list[str]]] = {}
    for row in rows:
        target = grouped.setdefault((str(row["scope"]), int(row["variable_ordinal"])), {})
        target.setdefault(str(row["attribute_name"]), []).append(str(row["attribute_value"]))

    def collapse(values: Mapping[str, list[str]]) -> dict[str, Any]:
        return {name: value[0] if len(value) == 1 else value for name, value in values.items()}

    file_attributes = collapse(grouped.get(("file", 0), {}))
    variable_attributes = {
        by_ordinal[ordinal]: collapse(attributes)
        for (scope, ordinal), attributes in grouped.items()
        if scope == "variable" and ordinal in by_ordinal
    }
    return file_attributes, variable_attributes


def _typed_endpoint(value: Any) -> tuple[str, float | None, str | None]:
    """Encode typed values without losing SPSS LOWEST/HIGHEST sentinels.

    ReadStat exposes a LOWEST range bound as NaN and HIGHEST as positive
    infinity. Storing either in a SQL binary64 column is not portable, so the
    catalog records an explicit endpoint type and reconstructs the ReadStat
    writer input on export.
    """
    if isinstance(value, float):
        if math.isnan(value):
            return "lowest", None, None
        if math.isinf(value):
            return ("highest" if value > 0 else "lowest"), None, None
    if isinstance(value, bool):
        return "text", None, str(value)
    if isinstance(value, (int, float)):
        return "numeric", float(value), None
    return "text", None, "" if value is None else str(value)


def document_rows(dataset_id: str, documents: str) -> list[dict[str, Any]]:
    return [
        {"dataset_id": dataset_id, "ordinal": ordinal, "text": str(text)}
        for ordinal, text in enumerate(json.loads(documents or "[]"), start=1)
    ]


def value_label_rows(dataset_id: str, variables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for variable in variables:
        labels = json.loads(variable.get("value_labels") or "{}")
        for ordinal, (raw_value, label) in enumerate(labels.items(), start=1):
            value: Any = float(raw_value) if variable["storage_kind"] == "numeric" else raw_value
            value_type, numeric_value, text_value = _typed_endpoint(value)
            rows.append({
                "dataset_id": dataset_id, "variable_ordinal": variable["ordinal"], "ordinal": ordinal,
                "value_type": value_type, "numeric_value": numeric_value, "text_value": text_value,
                "label": str(label),
            })
    return rows


def missing_rule_rows(dataset_id: str, variables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for variable in variables:
        rules = json.loads(variable.get("missing_ranges") or "[]")
        for ordinal, raw_rule in enumerate(rules, start=1):
            if isinstance(raw_rule, dict):
                lower = raw_rule.get("lo")
                upper = raw_rule.get("hi")
            else:
                lower = upper = raw_rule
            lower_type, lower_numeric, lower_text = _typed_endpoint(lower)
            upper_type, upper_numeric, upper_text = _typed_endpoint(upper)
            kind = "discrete" if lower_type == upper_type and (lower_numeric == upper_numeric and lower_text == upper_text) else "range"
            rows.append({
                "dataset_id": dataset_id, "variable_ordinal": variable["ordinal"], "ordinal": ordinal,
                "kind": kind,
                "lower_type": lower_type, "lower_numeric": lower_numeric, "lower_text": lower_text,
                "upper_type": upper_type, "upper_numeric": upper_numeric, "upper_text": upper_text,
                "lower_inclusive": True, "upper_inclusive": True,
            })
    return rows


def normalized_metadata_tables(metadata: MetaData) -> tuple[Table, Table, Table, Table]:
    return (
        document_catalog(metadata), value_label_catalog(metadata), missing_rule_catalog(metadata),
        attribute_catalog(metadata),
    )

def _mr_counted_value(definition: Mapping[str, Any]) -> tuple[str | None, float | None, str | None]:
    value = definition.get("counted_value", definition.get("countedvalue"))
    if value is None:
        return None, None, None
    if isinstance(value, bool):
        return "text", None, str(value)
    if isinstance(value, (int, float)):
        return "numeric", float(value), None
    return "text", None, str(value)


def _multiple_response_definitions(definitions: str | Mapping[str, Any] | None) -> Mapping[str, Any]:
    if definitions is None or definitions == "":
        return {}
    if isinstance(definitions, str):
        try:
            definitions = json.loads(definitions)
        except json.JSONDecodeError as error:
            raise _catalog_error(
                "multiple-response-definition-invalid",
                "Multiple-response set catalog is not valid JSON.",
                error=str(error),
            ) from error
    if not isinstance(definitions, Mapping):
        raise _catalog_error(
            "multiple-response-definition-invalid",
            "Multiple-response set catalog must be an object keyed by set name.",
            actual_type=type(definitions).__name__,
        )
    return definitions


def _mr_is_dichotomy(definition: Mapping[str, Any]) -> bool:
    value = definition.get("is_dichotomy")
    if value is not None:
        return bool(value)
    return definition.get("set_type", definition.get("type")) in {"MD", "D", "E"} or (
        definition.get("counted_value", definition.get("countedvalue")) is not None
    )


def validate_spss_catalog(
    variables: Iterable[Mapping[str, Any]], *,
    case_weight_variable: str | None = None,
    multiple_response_sets: str | Mapping[str, Any] | None = None,
) -> None:
    """Validate SPSS dictionary cross-references before persisting or writing."""
    variables_by_name: dict[str, Mapping[str, Any]] = {}
    for variable in variables:
        source_name = variable.get("source_name")
        if isinstance(source_name, str):
            variables_by_name[source_name] = variable

    if case_weight_variable is not None:
        if not isinstance(case_weight_variable, str) or not case_weight_variable:
            raise _catalog_error(
                "case-weight-variable-invalid",
                "The case-weight reference must be a non-empty source variable name.",
                case_weight_variable=case_weight_variable,
            )
        weight = variables_by_name.get(case_weight_variable)
        if weight is None:
            raise _catalog_error(
                "case-weight-variable-not-found",
                "The case-weight reference does not name a source variable.",
                case_weight_variable=case_weight_variable,
            )
        if weight.get("storage_kind") != "numeric":
            raise _catalog_error(
                "case-weight-variable-not-numeric",
                "The case-weight variable must use SPSS numeric storage.",
                case_weight_variable=case_weight_variable,
                storage_kind=weight.get("storage_kind"),
            )
        if str(weight.get("measure") or "").lower() != "scale":
            raise _catalog_error(
                "case-weight-variable-not-scale",
                "The case-weight variable must have SPSS scale measurement level.",
                case_weight_variable=case_weight_variable,
                measurement_level=weight.get("measure"),
            )

    for set_name, definition in _multiple_response_definitions(multiple_response_sets).items():
        if not isinstance(set_name, str) or not set_name:
            raise _catalog_error(
                "multiple-response-set-name-invalid",
                "A multiple-response set needs a non-empty string name.",
                set_name=set_name,
            )
        if not isinstance(definition, Mapping):
            raise _catalog_error(
                "multiple-response-definition-invalid",
                "A multiple-response set definition must be an object.",
                set_name=set_name,
                actual_type=type(definition).__name__,
            )
        members = definition.get("variable_list", definition.get("variables", []))
        if isinstance(members, str):
            members = members.split()
        if not isinstance(members, (list, tuple)) or not members:
            raise _catalog_error(
                "multiple-response-members-invalid",
                "A multiple-response set needs at least one ordered member variable.",
                set_name=set_name,
            )
        if any(not isinstance(member, str) or not member for member in members):
            raise _catalog_error(
                "multiple-response-member-invalid",
                "Every multiple-response member must be a non-empty source variable name.",
                set_name=set_name,
            )
        if len(set(members)) != len(members):
            raise _catalog_error(
                "multiple-response-member-duplicate",
                "A multiple-response set cannot list one source variable twice.",
                set_name=set_name,
            )
        member_variables: list[Mapping[str, Any]] = []
        for member in members:
            variable = variables_by_name.get(member)
            if variable is None:
                raise _catalog_error(
                    "multiple-response-member-not-found",
                    "A multiple-response member does not name a source variable.",
                    set_name=set_name,
                    member=member,
                )
            member_variables.append(variable)
        storage_kinds = {str(variable.get("storage_kind")) for variable in member_variables}
        if len(storage_kinds) != 1 or storage_kinds == {"None"}:
            raise _catalog_error(
                "multiple-response-member-type-mismatch",
                "All multiple-response members must have one shared SPSS storage kind.",
                set_name=set_name,
                members=list(members),
                storage_kinds=sorted(storage_kinds),
            )
        is_dichotomy = _mr_is_dichotomy(definition)
        counted_value = definition.get("counted_value", definition.get("countedvalue"))
        if not is_dichotomy:
            if counted_value is not None:
                raise _catalog_error(
                    "multiple-response-counted-value-not-permitted",
                    "An MC multiple-response set cannot define a dichotomy counted value.",
                    set_name=set_name,
                    counted_value=counted_value,
                )
            continue
        if counted_value is None:
            raise _catalog_error(
                "multiple-response-counted-value-missing",
                "An MD multiple-response set must define its counted value.",
                set_name=set_name,
            )
        storage_kind = storage_kinds.pop()
        if storage_kind == "numeric":
            if isinstance(counted_value, bool) or not isinstance(counted_value, (int, float)) or not math.isfinite(float(counted_value)):
                raise _catalog_error(
                    "multiple-response-counted-value-type-mismatch",
                    "An MD set of numeric variables requires a finite numeric counted value.",
                    set_name=set_name,
                    counted_value=counted_value,
                    storage_kind=storage_kind,
                )
        elif storage_kind == "string":
            if not isinstance(counted_value, str):
                raise _catalog_error(
                    "multiple-response-counted-value-type-mismatch",
                    "An MD set of string variables requires a string counted value.",
                    set_name=set_name,
                    counted_value=counted_value,
                    storage_kind=storage_kind,
                )
            encoded_length = len(counted_value.encode("utf-8"))
            for member, variable in zip(members, member_variables):
                width = variable.get("string_width")
                if isinstance(width, int) and encoded_length > width:
                    raise _catalog_error(
                        "multiple-response-counted-value-too-wide",
                        "The MD counted value exceeds a member's declared SPSS string width.",
                        set_name=set_name,
                        member=member,
                        counted_value=counted_value,
                        counted_value_bytes=encoded_length,
                        string_width=width,
                    )
        else:
            raise _catalog_error(
                "multiple-response-member-storage-invalid",
                "A multiple-response member has unsupported SPSS storage kind.",
                set_name=set_name,
                storage_kind=storage_kind,
            )

def multiple_response_set_rows(dataset_id: str, definitions: str) -> list[dict[str, Any]]:
    sets = json.loads(definitions or "{}")
    rows: list[dict[str, Any]] = []
    for set_name, definition in sets.items():
        if not isinstance(definition, Mapping):
            continue
        members = definition.get("variable_list", definition.get("variables", []))
        if isinstance(members, str):
            members = members.split()
        is_dichotomy = definition.get("is_dichotomy")
        if is_dichotomy is None:
            is_dichotomy = definition.get("set_type", definition.get("type")) in {"MD", "D", "E"} or (
                definition.get("counted_value", definition.get("countedvalue")) is not None
            )
        kind = "MD" if is_dichotomy else "MC"
        value_type, numeric_value, text_value = _mr_counted_value(definition)
        for ordinal, variable_name in enumerate(members or [None], start=1):
            rows.append({
                "dataset_id": dataset_id, "set_name": set_name, "member_ordinal": ordinal,
                "kind": kind, "label": definition.get("label"),
                "is_dichotomy": bool(is_dichotomy),
                "use_category_labels": bool(definition.get("use_category_labels", False)),
                "use_first_var_label": bool(definition.get("use_first_var_label", False)),
                "counted_value": "" if value_type is None else str(
                    numeric_value if value_type == "numeric" else text_value
                ),
                "counted_value_type": value_type,
                "counted_numeric": numeric_value, "counted_text": text_value,
                "variable_name": variable_name,
                "definition": json.dumps(definition, default=str, ensure_ascii=False, sort_keys=True),
            })
    return rows


def multiple_response_sets_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reconstruct pyspssio MR metadata from normalized catalog rows.

    The new typed fields are authoritative. ``definition`` only supplies
    backwards compatibility for catalogs written before those fields existed.
    """
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["set_name"])
        if name not in result:
            try:
                legacy = json.loads(row.get("definition") or "{}")
            except (TypeError, json.JSONDecodeError):
                legacy = {}
            definition = dict(legacy) if isinstance(legacy, Mapping) else {}
            definition["variable_list"] = []
            result[name] = definition
        definition = result[name]
        if row.get("label") is not None:
            definition["label"] = row["label"]
        if row.get("is_dichotomy") is not None:
            definition["is_dichotomy"] = bool(row["is_dichotomy"])
        elif row.get("kind") is not None:
            definition["is_dichotomy"] = str(row["kind"]).upper() == "MD"
        if row.get("use_category_labels") is not None:
            definition["use_category_labels"] = bool(row["use_category_labels"])
        if row.get("use_first_var_label") is not None:
            definition["use_first_var_label"] = bool(row["use_first_var_label"])
        value_type = row.get("counted_value_type")
        if value_type == "numeric":
            definition["counted_value"] = row.get("counted_numeric")
        elif value_type == "text":
            definition["counted_value"] = row.get("counted_text")
        elif row.get("counted_value") not in (None, ""):
            definition["counted_value"] = row["counted_value"]
        if row.get("variable_name") is not None:
            definition["variable_list"].append(row["variable_name"])
    return result

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
    file_attributes: str = "{}", case_weight_variable: str | None = None,
    file_attribute_values: Mapping[str, Any] | None = None,
    variable_attribute_values: Mapping[str, Mapping[str, Any]] | None = None,
    source_table_name: str | None = None,
    source_sha256: str = "",
    source_created_at: str | None = None, source_modified_at: str | None = None,
    imported_at: str = "",
    multiple_response_sets: str = "{}",
    source_extensions: Mapping[str, Any] | None = None,
    fidelity_events: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    profile = validate_connection_url(database_url)
    engine = create_engine(database_url)
    metadata = MetaData()
    datasets, variable_catalog, fidelity_event_catalog, operation_catalog = catalog(metadata)
    multiple_response_catalog = multiple_response_set_catalog(metadata)
    source_extensions_catalog = source_extension_catalog(metadata)
    documents_catalog, value_labels_catalog, missing_rules_catalog, attributes_catalog = normalized_metadata_tables(metadata)
    operation_id = str(uuid4())
    try:
        preflight(profile, variables)
        validate_spss_catalog(
            variables,
            case_weight_variable=case_weight_variable,
            multiple_response_sets=multiple_response_sets,
        )
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
        metadata.create_all(connection, tables=[datasets, variable_catalog, multiple_response_catalog, source_extensions_catalog, documents_catalog, value_labels_catalog, missing_rules_catalog, attributes_catalog, fidelity_event_catalog, operation_catalog])
        _migrate_catalog_columns(connection, datasets, variable_catalog, multiple_response_catalog)
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
            file_attributes=file_attributes, case_weight_variable=case_weight_variable,
            multiple_response_sets=multiple_response_sets,
        ))
        connection.execute(insert(variable_catalog), [dict(dataset_id=dataset_id, **item) for item in variables])
        docs_rows = document_rows(dataset_id, documents)
        if docs_rows:
            connection.execute(insert(documents_catalog), docs_rows)
        labels_rows = value_label_rows(dataset_id, variables)
        if labels_rows:
            connection.execute(insert(value_labels_catalog), labels_rows)
        missing_rows = missing_rule_rows(dataset_id, variables)
        if missing_rows:
            connection.execute(insert(missing_rules_catalog), missing_rows)
        attributes_rows = attribute_rows(
            dataset_id, variables,
            file_attributes=file_attribute_values,
            variable_attributes=variable_attribute_values,
        )
        if attributes_rows:
            connection.execute(insert(attributes_catalog), attributes_rows)
        mrset_rows = multiple_response_set_rows(dataset_id, multiple_response_sets)
        if mrset_rows:
            connection.execute(insert(multiple_response_catalog), mrset_rows)
        extension_rows = source_extension_rows(dataset_id, source_extensions or {})
        if extension_rows:
            connection.execute(insert(source_extensions_catalog), extension_rows)
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
                connection.execute(delete(source_extensions_catalog).where(source_extensions_catalog.c.dataset_id == dataset_id))
                connection.execute(delete(documents_catalog).where(documents_catalog.c.dataset_id == dataset_id))
                connection.execute(delete(value_labels_catalog).where(value_labels_catalog.c.dataset_id == dataset_id))
                connection.execute(delete(missing_rules_catalog).where(missing_rules_catalog.c.dataset_id == dataset_id))
                connection.execute(delete(attributes_catalog).where(attributes_catalog.c.dataset_id == dataset_id))
                connection.execute(delete(fidelity_event_catalog).where(fidelity_event_catalog.c.dataset_id == dataset_id))
                connection.execute(delete(variable_catalog).where(variable_catalog.c.dataset_id == dataset_id))
                connection.execute(delete(datasets).where(datasets.c.dataset_id == dataset_id))
                connection.commit()
                raise
        connection.execute(update(operation_catalog).where(operation_catalog.c.operation_id == operation_id).values(
            status="succeeded", completed_at=_now(),
        ))
    return {"dataset_id": dataset_id, "data_table": data_table.name, "case_count": len(materialized), "operation_id": operation_id}


def _endpoint_from_row(row: Mapping[str, Any], *, prefix: str) -> Any:
    endpoint_type = row[f"{prefix}_type"]
    if endpoint_type == "lowest":
        return -sys.float_info.max
    if endpoint_type == "highest":
        return sys.float_info.max
    return row[f"{prefix}_numeric"] if endpoint_type == "numeric" else row[f"{prefix}_text"]


def read_wide_dataset(*, database_url: str, dataset_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Read a strict dataset, preferring normalized metadata with JSON compatibility fallback."""
    engine = create_engine(database_url)
    metadata = MetaData()
    datasets, variable_catalog, _, _ = catalog(metadata)
    multiple_response_catalog = multiple_response_set_catalog(metadata)
    source_extensions_catalog = source_extension_catalog(metadata)
    documents_catalog, value_labels_catalog, missing_rules_catalog, attributes_catalog = normalized_metadata_tables(metadata)
    with engine.begin() as connection:
        metadata.create_all(connection, tables=[datasets, variable_catalog, multiple_response_catalog, source_extensions_catalog, documents_catalog, value_labels_catalog, missing_rules_catalog, attributes_catalog])
        _migrate_catalog_columns(connection, datasets, variable_catalog, multiple_response_catalog)
        dataset = dict(connection.execute(select(datasets).where(datasets.c.dataset_id == dataset_id)).mappings().one())
        data_table = Table(dataset["data_table"], MetaData(), autoload_with=connection)
        variables = [dict(item) for item in connection.execute(
            select(variable_catalog).where(variable_catalog.c.dataset_id == dataset_id).order_by(variable_catalog.c.ordinal)
        ).mappings().all()]
        rows = [dict(item) for item in connection.execute(
            select(data_table).order_by(data_table.c.__case_ordinal)
        ).mappings().all()]
        document_rows_result = connection.execute(
            select(documents_catalog).where(documents_catalog.c.dataset_id == dataset_id)
            .order_by(documents_catalog.c.ordinal)
        ).mappings().all()
        label_rows_result = connection.execute(
            select(value_labels_catalog).where(value_labels_catalog.c.dataset_id == dataset_id)
            .order_by(value_labels_catalog.c.variable_ordinal, value_labels_catalog.c.ordinal)
        ).mappings().all()
        missing_rows_result = connection.execute(
            select(missing_rules_catalog).where(missing_rules_catalog.c.dataset_id == dataset_id)
            .order_by(missing_rules_catalog.c.variable_ordinal, missing_rules_catalog.c.ordinal)
        ).mappings().all()
        attribute_rows_result = connection.execute(
            select(attributes_catalog).where(attributes_catalog.c.dataset_id == dataset_id)
            .order_by(
                attributes_catalog.c.scope, attributes_catalog.c.variable_ordinal,
                attributes_catalog.c.attribute_ordinal, attributes_catalog.c.value_ordinal,
            )
        ).mappings().all()
        mrset_rows_result = connection.execute(
            select(multiple_response_catalog).where(multiple_response_catalog.c.dataset_id == dataset_id)
            .order_by(multiple_response_catalog.c.set_name, multiple_response_catalog.c.member_ordinal)
        ).mappings().all()
        extension_rows_result = connection.execute(
            select(source_extensions_catalog).where(source_extensions_catalog.c.dataset_id == dataset_id)
            .order_by(source_extensions_catalog.c.extension_key)
        ).mappings().all()

    if document_rows_result:
        dataset["documents"] = json.dumps([item["text"] for item in document_rows_result], ensure_ascii=False)
    if attribute_rows_result:
        file_attributes, variable_attributes = attributes_from_rows(
            attribute_rows_result, variables=variables,
        )
        dataset["file_attributes"] = json.dumps(file_attributes, ensure_ascii=False)
        for variable in variables:
            variable["attributes"] = json.dumps(
                variable_attributes.get(variable["source_name"], {}), ensure_ascii=False,
            )
    if mrset_rows_result:
        dataset["multiple_response_sets"] = json.dumps(
            multiple_response_sets_from_rows(mrset_rows_result), ensure_ascii=False, default=str,
        )
    if extension_rows_result:
        dataset["source_extensions"] = {
            item["extension_key"]: json.loads(item["payload"])
            for item in extension_rows_result
        }
    variables_by_ordinal = {item["ordinal"]: item for item in variables}
    labels_by_variable: dict[int, dict[Any, str]] = {}
    for item in label_rows_result:
        labels_by_variable.setdefault(item["variable_ordinal"], {})[
            item["numeric_value"] if item["value_type"] == "numeric" else item["text_value"]
        ] = item["label"]
    for ordinal, labels in labels_by_variable.items():
        variables_by_ordinal[ordinal]["value_labels"] = json.dumps(labels, ensure_ascii=False)
    rules_by_variable: dict[int, list[dict[str, Any]]] = {}
    for item in missing_rows_result:
        lower = _endpoint_from_row(item, prefix="lower")
        upper = _endpoint_from_row(item, prefix="upper")
        rules_by_variable.setdefault(item["variable_ordinal"], []).append({"lo": lower, "hi": upper})
    for ordinal, rules in rules_by_variable.items():
        variables_by_ordinal[ordinal]["missing_ranges"] = json.dumps(rules, ensure_ascii=False)
    return dataset, variables, rows


def read_fidelity_events(*, database_url: str, dataset_id: str) -> tuple[dict[str, Any], ...]:
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
    return tuple({
        "code": item["code"], "detail": item["detail"],
        "details": json.loads(item["details"] or "{}"),
    } for item in events)



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
    preflight(profile, variables)
    validate_spss_catalog(
        variables,
        case_weight_variable=dataset.get("case_weight_variable"),
        multiple_response_sets=dataset.get("multiple_response_sets"),
    )
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

