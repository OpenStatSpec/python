"""SQLite reference SQL profile for the strict OpenStatSpec wide-table contract."""

import hashlib
import json
import math
import re
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import delete, BigInteger, Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, Text, create_engine, insert, inspect, select, text, update
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import make_url
from ..core import UnsupportedOperationError, safe_error_identity as _safe_error_identity
from .capabilities import (
    active_connection, dolt_operational_write_enabled, effective_profile,
)
from .dolt_conformance import DoltConformanceSource
from .profiles import (
    MYSQL_WIRE_PROFILES, preflight, preflight_identifier,
    statement_payload_bytes, validate_connection_url,
)
from .normative import (
    binary64_type,
    CATALOG_CONTRACT_ID,
    CATALOG_SCHEMA_VERSION,
    catalog as normative_catalog,
    create as create_normative_catalog,
    dataset_id_for_name as normative_dataset_id_for_name,
    finish_operation as finish_normative_operation,
    record_fidelity_events as record_normative_fidelity_events,
    record_operation as record_normative_operation,
    store_imported_dataset as store_normative_dataset,
)

_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_]+")


class CatalogPreflightError(UnsupportedOperationError):
    """A semantic SPSS catalog rule failed before an import or writer runs."""

    def __init__(self, code: str, detail: str, *, details: Mapping[str, Any]) -> None:
        super().__init__(f"OpenStatSpec catalog preflight failed [{code}]: {detail}")
        self.code = code
        self.details = {"reason": code, **details}


class ImportRecoveryError(UnsupportedOperationError):
    """Import recovery could not establish the promised terminal state."""

    def __init__(self, code: str, detail: str, *, details: Mapping[str, Any]) -> None:
        super().__init__(f"OpenStatSpec import recovery failed [{code}]: {detail}")
        self.code = code
        self.details = {"reason": code, **details}


def _catalog_error(code: str, detail: str, **details: Any) -> CatalogPreflightError:
    return CatalogPreflightError(code, detail, details=details)


def string_type(profile: Any) -> Text:
    """Use Dolt's tested LONGTEXT storage without changing MySQL/MariaDB DDL."""
    return mysql.LONGTEXT() if profile.name == "dolt" else Text()


def _wide_column_type(profile: Any, storage_kind: str) -> Any:
    """Select the physical value type from the effective SQL profile."""
    return binary64_type() if storage_kind == "numeric" else string_type(profile)


def _valid_wide_string_type(profile: Any, column_type: Any) -> bool:
    """Require Dolt's declared LONGTEXT boundary during reflected validation."""
    return (
        isinstance(column_type, mysql.LONGTEXT)
        if profile.name == "dolt"
        else isinstance(column_type, Text)
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _verification_fault_identity(
    code: str, *, phase: str, evidence: Any,
) -> dict[str, Any]:
    return {
        "type": "InvariantVerificationError",
        "code": code,
        "phase": phase,
        "message_sha256": _canonical_sha256(evidence),
    }


def _normalized_dolt_rows(
    rows: Iterable[Mapping[str, Any]], *, expected_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    normalized = []
    for row in rows:
        raw = dict(row)
        if set(raw) != set(expected_keys):
            raise UnsupportedOperationError(
                "Dolt state probe returned an unexpected column shape."
            )
        if expected_keys == ("table_name", "staged", "status"):
            if (
                not isinstance(raw["table_name"], str)
                or not raw["table_name"].strip()
                or raw["staged"] not in {False, True, 0, 1}
                or not isinstance(raw["status"], str)
                or not raw["status"].strip()
            ):
                raise UnsupportedOperationError(
                    "Dolt status probe returned an invalid row value shape."
                )
            raw["staged"] = bool(raw["staged"])
        else:
            relation_names = (raw["from_table_name"], raw["to_table_name"])
            if (
                not any(isinstance(name, str) and name.strip() for name in relation_names)
                or any(name is not None and not isinstance(name, str) for name in relation_names)
                or not isinstance(raw["diff_type"], str)
                or not raw["diff_type"].strip()
                or raw["data_change"] not in {False, True, 0, 1}
                or raw["schema_change"] not in {False, True, 0, 1}
            ):
                raise UnsupportedOperationError(
                    "Dolt diff-summary probe returned an invalid row value shape."
                )
            raw["data_change"] = bool(raw["data_change"])
            raw["schema_change"] = bool(raw["schema_change"])
        normalized.append({key: raw[key] for key in expected_keys})
    return sorted(normalized, key=lambda row: json.dumps(row, sort_keys=True, default=str))


def _dolt_evidence_block(
    rows: Iterable[Mapping[str, Any]], *, audit_relations: set[str],
    expected_keys: tuple[str, ...],
) -> dict[str, Any]:
    normalized = _normalized_dolt_rows(rows, expected_keys=expected_keys)

    def is_audit_row(row: Mapping[str, Any]) -> bool:
        if expected_keys == ("table_name", "staged", "status"):
            return row["table_name"] in audit_relations and (
                str(row["status"]).strip().casefold() == "modified"
            )
        from_name = row.get("from_table_name")
        to_name = row.get("to_table_name")
        return (
            isinstance(from_name, str)
            and from_name == to_name
            and from_name in audit_relations
            and str(row.get("diff_type") or "").strip().casefold() == "modified"
            and bool(row.get("data_change"))
            and not bool(row.get("schema_change"))
        )

    audit = [row for row in normalized if is_audit_row(row)]
    non_audit = [row for row in normalized if not is_audit_row(row)]
    return {
        "rows": normalized,
        "sha256": _canonical_sha256(normalized),
        "audit_catalog_rows": audit,
        "audit_catalog_sha256": _canonical_sha256(audit),
        "non_audit_rows": non_audit,
        "non_audit_sha256": _canonical_sha256(non_audit),
    }


def _capture_dolt_state(
    connection: Any, *, profile_name: str, audit_relations: set[str],
) -> dict[str, Any] | None:
    """Capture read-only Dolt version-control state without changing branches or HEAD."""
    if profile_name != "dolt":
        return None
    identity = connection.exec_driver_sql(
        "SELECT DATABASE() AS database_name, ACTIVE_BRANCH() AS active_branch, "
        "DOLT_HASHOF('HEAD') AS head_hash"
    ).mappings().one()
    summaries = {}
    for label, left, right in (
        ("head_to_working", "HEAD", "WORKING"),
        ("head_to_staged", "HEAD", "STAGED"),
        ("staged_to_working", "STAGED", "WORKING"),
    ):
        rows = connection.exec_driver_sql(
            "SELECT from_table_name, to_table_name, diff_type, "
            "data_change, schema_change "
            f"FROM DOLT_DIFF_SUMMARY('{left}', '{right}') "
            "ORDER BY from_table_name, to_table_name, diff_type"
        ).mappings().all()
        summaries[label] = _dolt_evidence_block(
            rows, audit_relations=audit_relations,
            expected_keys=(
                "from_table_name", "to_table_name", "diff_type",
                "data_change", "schema_change",
            ),
        )
    status = _dolt_evidence_block(
        connection.exec_driver_sql(
            "SELECT table_name, staged, status FROM dolt_status "
            "ORDER BY table_name, staged, status"
        ).mappings().all(),
        audit_relations=audit_relations,
        expected_keys=("table_name", "staged", "status"),
    )
    for key in ("database_name", "active_branch", "head_hash"):
        if not isinstance(identity[key], str) or not identity[key].strip():
            raise UnsupportedOperationError(
                f"Dolt state probe returned no non-empty {key}."
            )
    result = {
        "database": identity["database_name"].strip(),
        "active_branch": identity["active_branch"].strip(),
        "head": identity["head_hash"].strip(),
        "status": status,
        "diff_summaries": summaries,
    }
    result["snapshot_sha256"] = _canonical_sha256(result)
    return result


def _require_dolt_working_set_binding(
    snapshot: dict[str, Any] | None, active: Mapping[str, Any], *, phase: str,
) -> None:
    if snapshot is None:
        return
    binding = active.get("working_set_binding")
    if (
        not isinstance(binding, Mapping)
        or snapshot["database"] != binding.get("database")
        or snapshot["active_branch"] != binding.get("active_branch")
    ):
        raise UnsupportedOperationError(
            f"Dolt database/branch working-set binding mismatch during {phase}."
        )


def _require_dolt_success_identity(
    before: dict[str, Any] | None, after: dict[str, Any] | None, *, phase: str,
) -> None:
    if before is None and after is None:
        return
    if before is None or after is None or any(
        before[key] != after[key] for key in ("database", "active_branch", "head")
    ):
        raise UnsupportedOperationError(
            f"Dolt database/branch/HEAD changed during {phase}."
        )


def _dolt_failure_boundary_evidence(
    before: dict[str, Any] | None, after: dict[str, Any] | None,
) -> dict[str, Any]:
    if before is None and after is None:
        return {"applicable": False}
    if before is None or after is None:
        return {"applicable": True, "verified": False, "reason": "snapshot_missing"}
    invariant_failures = []
    for key in ("database", "active_branch", "head"):
        if before[key] != after[key]:
            invariant_failures.append(f"{key}_changed")
    if before["status"]["non_audit_sha256"] != after["status"]["non_audit_sha256"]:
        invariant_failures.append("non_audit_status_changed")
    for label in sorted(before["diff_summaries"]):
        if (
            before["diff_summaries"][label]["non_audit_sha256"]
            != after["diff_summaries"][label]["non_audit_sha256"]
        ):
            invariant_failures.append(f"non_audit_{label}_changed")
    return {
        "applicable": True,
        "verified": not invariant_failures,
        "invariant_failures": invariant_failures,
        "before": before,
        "after": after,
        "permitted_delta": "failed-operation audit catalog relations only",
        "prohibited_vc_actions": [
            "DOLT_ADD", "DOLT_COMMIT", "checkout", "reset", "branch_change",
        ],
    }


@contextmanager
def _bound_catalog_transaction(
    *, engine: Any, profile_name: str, active: Mapping[str, Any],
    audit_relations: set[str], phase: str,
) -> Iterable[Any]:
    """Bind a write transaction to one Dolt database/branch/HEAD identity."""
    with engine.connect() as connection:
        before = _capture_dolt_state(
            connection, profile_name=profile_name,
            audit_relations=audit_relations,
        )
        _require_dolt_working_set_binding(before, active, phase=f"{phase} preflight")
        connection.rollback()
        with connection.begin():
            yield connection
            after = _capture_dolt_state(
                connection, profile_name=profile_name,
                audit_relations=audit_relations,
            )
            _require_dolt_working_set_binding(after, active, phase=f"{phase} completion")
            _require_dolt_success_identity(before, after, phase=phase)
            boundary = _dolt_failure_boundary_evidence(before, after)
            if boundary.get("applicable") and not boundary.get("verified"):
                raise UnsupportedOperationError(
                    f"Dolt non-audit working-set state changed during {phase}; "
                    "the transaction was rolled back."
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
        # format remains the legacy print-format mirror; SPSS has a distinct write format.
        Column("print_format", String(64)),
        Column("write_format", String(64)),
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
    text_declaration = str(
        Text().compile(dialect=connection.dialect)
    )
    additions = {
        datasets.name: {
            "file_attributes": f"{text_declaration} NOT NULL DEFAULT '{{}}'",
            "case_weight_variable": "VARCHAR(255)",
        },
        variables.name: {
            "role": "VARCHAR(32)",
            "attributes": f"{text_declaration} NOT NULL DEFAULT '{{}}'",
            "compat_name": "VARCHAR(255)",
            "print_format": "VARCHAR(64)",
            "write_format": "VARCHAR(64)",
        },
        multiple_response.name: {
            "is_dichotomy": "BOOLEAN",
            "use_category_labels": "BOOLEAN",
            "use_first_var_label": "BOOLEAN",
            "counted_value_type": "VARCHAR(16)",
            "counted_numeric": "DOUBLE",
            "counted_text": text_declaration,
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
    operation_catalog: Table, normative: Any, operation_id: str, source_name: str,
    source_format: str, variable_count: int, profile_name: str, error: Exception,
    legacy: tuple[Table, ...],
) -> None:
    """Persist a failed preflight without creating any source dataset state."""
    with engine.begin() as connection:
        _require_verified_catalog(connection, normative, legacy)
        failed_at = datetime.now(UTC).replace(tzinfo=None)
        record_normative_operation(
            connection, normative, operation_id=operation_id,
            operation_kind="import", status="failed", source_format=source_format,
            started_at=failed_at, completed_at=failed_at,
        )
        connection.execute(insert(operation_catalog).values(
            operation_id=operation_id, direction="import", status="failed", dataset_id=None,
            source=source_name, created_at=_now(), completed_at=_now(),
            details=json.dumps({"reason": "preflight", "variable_count": variable_count,
                "capability": getattr(error, "details", {})}, sort_keys=True),
        ))
        failed_events = ({
            "code": getattr(error, "code", "target_capability_exceeded"),
            "detail": str(error), "severity": "error", "source_item": source_name,
            "details": {"variable_count": variable_count, "profile": profile_name,
                        **getattr(error, "details", {})},
        },)
        connection.execute(insert(fidelity_event_catalog), _event_rows(
            operation_id=operation_id, dataset_id=None, direction="import",
            fidelity_events=failed_events,
        ))
        record_normative_fidelity_events(
            connection, normative, operation_id=operation_id, dataset_id=None,
            direction="import", events=failed_events,
        )

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


def _catalog_layout(metadata: MetaData) -> tuple[tuple[Table, ...], Any]:
    datasets, variables, fidelity_events, operations = catalog(metadata)
    multiple_response = multiple_response_set_catalog(metadata)
    source_extensions = source_extension_catalog(metadata)
    documents, value_labels, missing_rules, attributes = normalized_metadata_tables(metadata)
    return (
        datasets, variables, multiple_response, source_extensions, documents,
        value_labels, missing_rules, attributes, fidelity_events, operations,
    ), normative_catalog(metadata)

def _normalized_sql_type(inspector: Any, value: Any) -> str:
    compiled = " ".join(
        str(value.compile(dialect=inspector.bind.dialect)).strip().upper().split()
    )
    if inspector.bind.dialect.name in {"mysql", "mariadb"}:
        if compiled in {"BOOL", "BOOLEAN", "TINYINT(1)"}:
            return "BOOLEAN/TINYINT(1)"
        compiled = re.sub(
            r"\b(TINYINT|SMALLINT|MEDIUMINT|INTEGER|INT|BIGINT)\(\d+\)",
            r"\1",
            compiled,
        )
    return compiled


def _normalized_default(value: Any) -> str | None:
    if value is None:
        return None
    result = " ".join(str(value).strip().split())
    while result.startswith("(") and result.endswith(")"):
        result = result[1:-1].strip()
    return result


def _expected_unique_constraints(table: Table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if getattr(constraint, "__visit_name__", "") == "unique_constraint"
    }


def _actual_unique_constraints(
    inspector: Any, table_name: str,
) -> set[tuple[str, ...]]:
    constraints = {
        tuple(str(name) for name in item.get("column_names") or ())
        for item in inspector.get_unique_constraints(table_name)
    }
    constraints.update(
        tuple(str(name) for name in item.get("column_names") or ())
        for item in inspector.get_indexes(table_name)
        if item.get("unique")
    )
    constraints.discard(())
    return constraints


def _expected_foreign_keys(table: Table) -> set[tuple[Any, ...]]:
    return {
        (
            tuple(column.name for column in constraint.columns),
            next(iter(constraint.elements)).column.table.name,
            tuple(element.column.name for element in constraint.elements),
        )
        for constraint in table.foreign_key_constraints
    }


def _actual_foreign_keys(
    inspector: Any, table_name: str,
) -> set[tuple[Any, ...]]:
    return {
        (
            tuple(str(name) for name in item.get("constrained_columns") or ()),
            str(item.get("referred_table") or ""),
            tuple(str(name) for name in item.get("referred_columns") or ()),
        )
        for item in inspector.get_foreign_keys(table_name)
    }


def _normalized_check_sql(value: Any) -> str:
    result = " ".join(str(value).strip().casefold().split())
    result = result.replace("`", "").replace('"', "")
    result = re.sub(r"\[([^]]+)\]", r"\1", result)
    result = re.sub(r"(?<![a-z0-9])_[a-z0-9]+(?=')", "", result)
    result = re.sub(
        r"::\s*(?:character varying|varchar|text|integer|bigint|smallint|boolean)\b",
        "",
        result,
    )
    result = re.sub(r"\(([a-z_][a-z0-9_]*)\)", r"\1", result)
    while result.startswith("(") and result.endswith(")"):
        result = result[1:-1].strip()
    return result


def _expected_check_constraints(table: Table) -> set[str]:
    return {
        _normalized_check_sql(constraint.sqltext)
        for constraint in table.constraints
        if getattr(constraint, "__visit_name__", "") == "table_or_column_check_constraint"
    }


def _actual_check_constraints(inspector: Any, table_name: str) -> set[str]:
    return {
        _normalized_check_sql(item.get("sqltext") or "")
        for item in inspector.get_check_constraints(table_name)
    }


_MIGRATED_SERVER_DEFAULTS = {
    ("dataset_catalog", "file_attributes"): {None, "'{}'", '"{}"', "{}"},
    ("variable_catalog", "attributes"): {None, "'{}'", '"{}"', "{}"},
}


def _catalog_table_shape_valid(
    inspector: Any, table: Table, *, allow_missing: bool,
) -> bool:
    actual = {
        str(column["name"]): column
        for column in inspector.get_columns(table.name)
    }
    expected = {column.name: column for column in table.columns}
    if set(actual) - set(expected):
        return False
    if not allow_missing and set(actual) != set(expected):
        return False
    for name in set(actual) & set(expected):
        expected_column = expected[name]
        actual_column = actual[name]
        if (
            _normalized_sql_type(inspector, expected_column.type)
            != _normalized_sql_type(inspector, actual_column["type"])
        ):
            return False
        if bool(actual_column.get("nullable")) != bool(expected_column.nullable):
            return False
        actual_default = _normalized_default(actual_column.get("default"))
        if (table.name, name) in _MIGRATED_SERVER_DEFAULTS:
            if actual_default not in _MIGRATED_SERVER_DEFAULTS[(table.name, name)]:
                return False
        else:
            expected_default = _normalized_default(
                expected_column.server_default.arg
                if expected_column.server_default is not None else None
            )
            if actual_default != expected_default:
                return False
        if actual_column.get("identity") is not None or actual_column.get("computed") is not None:
            return False
        if expected_column.autoincrement is True and actual_column.get("autoincrement") is not True:
            return False
        if expected_column.autoincrement is False and actual_column.get("autoincrement") is True:
            return False
    expected_pk = tuple(column.name for column in table.primary_key.columns)
    actual_pk = tuple(
        str(name) for name in (
            inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
        )
    )
    if actual_pk != expected_pk:
        return False
    if _actual_unique_constraints(inspector, table.name) != _expected_unique_constraints(table):
        return False
    if _actual_foreign_keys(inspector, table.name) != _expected_foreign_keys(table):
        return False
    if _actual_check_constraints(inspector, table.name) != _expected_check_constraints(table):
        return False
    return True


def _identity_shape_valid(
    inspector: Any, table: Table, *, key_name: str,
) -> bool:
    try:
        return (
            [column.name for column in table.primary_key.columns] == [key_name]
            and _catalog_table_shape_valid(inspector, table, allow_missing=False)
        )
    except Exception:
        return False


def _catalog_existing_shapes_valid(
    inspector: Any, tables: Iterable[Table],
) -> bool:
    """Require exact existing shape while allowing only absent migration columns."""
    try:
        return all(
            _catalog_table_shape_valid(inspector, table, allow_missing=True)
            for table in tables
        )
    except Exception:
        return False


_MIGRATABLE_CATALOG_COLUMNS = {
    "dataset_catalog": {"file_attributes", "case_weight_variable"},
    "variable_catalog": {
        "role", "attributes", "compat_name", "print_format", "write_format",
    },
    "multiple_response_set_catalog": {
        "is_dichotomy", "use_category_labels", "use_first_var_label",
        "counted_value_type", "counted_numeric", "counted_text",
    },
}


def _catalog_missing_columns(
    inspector: Any, tables: Iterable[Table],
) -> dict[str, set[str]]:
    missing = {}
    for table in tables:
        absent = {column.name for column in table.columns} - {
            str(column["name"])
            for column in inspector.get_columns(table.name)
        }
        if absent:
            missing[table.name] = absent
    return missing


def _catalog_missing_columns_are_migratable(
    missing: Mapping[str, set[str]],
    *, allowed: Mapping[str, set[str]] | None = None,
) -> bool:
    allowed_columns = _MIGRATABLE_CATALOG_COLUMNS if allowed is None else allowed
    return bool(missing) and all(
        columns <= allowed_columns.get(table_name, set())
        for table_name, columns in missing.items()
    )


def _registered_physical_relations(
    connection: Any, *, existing_tables: set[str], normative: Any,
    legacy: Iterable[Table],
) -> tuple[set[str], tuple[Table, ...], set[str], set[str], str]:
    """Return owned relations and the optional workflow identity state."""
    legacy = tuple(legacy)
    declared_tables = legacy + normative.all()
    static_tables = {table.name for table in declared_tables}
    physical_tables: set[str] = set()
    physical_views: set[str] = set()
    inspector = inspect(connection)
    if (
        legacy[0].name in existing_tables
        and "data_table" in {
            str(column["name"]) for column in inspector.get_columns(legacy[0].name)
        }
    ):
        physical_tables.update(str(name) for name in connection.execute(
            select(legacy[0].c.data_table)
        ).scalars() if name)
    if (
        normative.dataset.name in existing_tables
        and "physical_table_name" in {
            str(column["name"]) for column in inspector.get_columns(normative.dataset.name)
        }
    ):
        physical_tables.update(str(name) for name in connection.execute(
            select(normative.dataset.c.physical_table_name)
        ).scalars() if name)

    # The optional workflow is another OpenStatSpec-owned relation profile in
    # the same dedicated namespace. Import locally to avoid its documented
    # dependency on this module during module initialization.
    from .workflow import (  # pylint: disable=import-outside-toplevel
        PROFILE_ID, PROFILE_SCHEMA_VERSION, workflow_catalog,
    )
    workflow = workflow_catalog(MetaData())
    workflow_tables = {table.name for table in workflow.all()}
    workflow_identity = workflow.transformation_profile_identity
    profile_present = workflow_identity.name in existing_tables
    if profile_present:
        declared_tables += workflow.all()
        static_tables.update(workflow_tables)
        if not workflow_tables <= existing_tables:
            return static_tables, declared_tables, physical_tables, physical_views, "foreign"
        if not _identity_shape_valid(
            inspector, workflow_identity, key_name="profile_identity_key",
        ):
            return static_tables, declared_tables, physical_tables, physical_views, "foreign"
        identities = connection.execute(select(workflow_identity)).mappings().all()
        if len(identities) != 1:
            return static_tables, declared_tables, physical_tables, physical_views, "ambiguous"
        if (
            identities[0]["profile_identity_key"] != 1
            or identities[0]["contract_id"] != PROFILE_ID
            or identities[0]["schema_version"] != PROFILE_SCHEMA_VERSION
            or identities[0]["core_contract_id"] != CATALOG_CONTRACT_ID
        ):
            return static_tables, declared_tables, physical_tables, physical_views, "foreign"
        for row in connection.execute(select(
            workflow.derived_dataset.c.physical_relation_name,
            workflow.derived_dataset.c.output_mode,
        )).mappings():
            name = str(row["physical_relation_name"])
            if row["output_mode"] == "view":
                physical_views.add(name)
            else:
                physical_tables.add(name)
    elif workflow_tables & existing_tables:
        return static_tables, declared_tables, physical_tables, physical_views, "foreign"

    # The compact in-place apply audit is optional but catalog-owned. Import
    # locally because inplace_transform depends on this module at load time.
    from .inplace_transform import (  # pylint: disable=import-outside-toplevel
        apply_audit_catalog,
    )
    apply_audit = apply_audit_catalog(MetaData())
    if apply_audit.name in existing_tables:
        declared_tables += (apply_audit,)
        static_tables.add(apply_audit.name)

    return static_tables, declared_tables, physical_tables, physical_views, "valid"


def _catalog_dataset_bijection_state(
    connection: Any, *, normative: Any, legacy: Iterable[Table],
) -> str:
    """Require one exact legacy-to-normative dataset/table mapping per row."""
    datasets = tuple(legacy)[0]
    legacy_rows = [
        (row["dataset_id"], row["data_table"])
        for row in connection.execute(select(
            datasets.c.dataset_id, datasets.c.data_table,
        )).mappings()
    ]
    normative_rows = [
        (row["dataset_name"], row["physical_table_name"])
        for row in connection.execute(select(
            normative.dataset.c.dataset_name,
            normative.dataset.c.physical_table_name,
        )).mappings()
    ]
    for rows in (legacy_rows, normative_rows):
        if any(
            not isinstance(dataset_name, str) or not dataset_name.strip()
            or not isinstance(table_name, str) or not table_name.strip()
            for dataset_name, table_name in rows
        ):
            return "unverified"
        dataset_names = [dataset_name for dataset_name, _table_name in rows]
        table_names = [table_name for _dataset_name, table_name in rows]
        if (
            len(set(dataset_names)) != len(dataset_names)
            or len(set(table_names)) != len(table_names)
            or len(set(rows)) != len(rows)
        ):
            return "ambiguous"
    return "valid" if set(legacy_rows) == set(normative_rows) else "unverified"


def _catalog_variable_bijection_state(
    connection: Any, *, normative: Any, legacy: Iterable[Table],
) -> str:
    """Require exact legacy-to-normative variable identity mappings."""
    variables = tuple(legacy)[1]
    legacy_rows = [
        (
            row["dataset_id"], row["ordinal"], row["source_name"],
            row["physical_name"],
        )
        for row in connection.execute(select(
            variables.c.dataset_id, variables.c.ordinal,
            variables.c.source_name, variables.c.physical_name,
        )).mappings()
    ]
    normative_rows = [
        (
            row["dataset_name"], row["source_ordinal"], row["source_name"],
            row["physical_name"],
        )
        for row in connection.execute(
            select(
                normative.dataset.c.dataset_name,
                normative.variable.c.source_ordinal,
                normative.variable.c.source_name,
                normative.variable.c.physical_name,
            ).join(
                normative.variable,
                normative.variable.c.dataset_id == normative.dataset.c.dataset_id,
            )
        ).mappings()
    ]
    for rows in (legacy_rows, normative_rows):
        if any(
            not isinstance(dataset_name, str) or not dataset_name.strip()
            or not isinstance(ordinal, int) or ordinal < 1
            or not isinstance(source_name, str) or not source_name.strip()
            or not isinstance(physical_name, str) or not physical_name.strip()
            for dataset_name, ordinal, source_name, physical_name in rows
        ):
            return "unverified"
        if len(set(rows)) != len(rows):
            return "ambiguous"
    return "valid" if set(legacy_rows) == set(normative_rows) else "unverified"


def _catalog_state(
    connection: Any, normative: Any, legacy: Iterable[Table],
    *, allowed_migrations: Mapping[str, set[str]] | None = None,
) -> str:
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    existing_views = set(inspector.get_view_names())
    if existing_tables & existing_views:
        return "ambiguous"
    existing_relations = existing_tables | existing_views
    if normative.catalog_identity.name not in existing_tables:
        return "absent" if not existing_relations else "foreign"
    try:
        identities = connection.execute(
            select(normative.catalog_identity)
        ).mappings().all()
    except Exception:
        return "foreign"
    if len(identities) != 1:
        return "ambiguous"
    if not _identity_shape_valid(
        inspector, normative.catalog_identity, key_name="catalog_identity_key",
    ):
        return "foreign"
    identity = identities[0]
    if (
        identity["catalog_identity_key"] != 1
        or identity["contract_id"] != CATALOG_CONTRACT_ID
        or identity["schema_version"] != CATALOG_SCHEMA_VERSION
    ):
        return "foreign"
    static_tables, declared_tables, physical_tables, physical_views, profile_valid = (
        _registered_physical_relations(
            connection, existing_tables=existing_tables,
            normative=normative, legacy=legacy,
        )
    )
    if profile_valid != "valid":
        return profile_valid
    if (
        physical_tables & physical_views
        or static_tables & physical_tables
        or static_tables & physical_views
    ):
        return "ambiguous"
    owned_relations = static_tables | physical_tables | physical_views
    if existing_relations - owned_relations:
        return "foreign"
    if physical_tables - existing_tables or physical_views - existing_views:
        return "unverified"
    if not static_tables <= existing_tables:
        return "unverified"
    if not _catalog_existing_shapes_valid(inspector, declared_tables):
        return "foreign"
    missing_columns = _catalog_missing_columns(inspector, declared_tables)
    if missing_columns:
        return (
            "migration_required"
            if _catalog_missing_columns_are_migratable(
                missing_columns, allowed=allowed_migrations,
            )
            else "unverified"
        )
    mapping_state = _catalog_dataset_bijection_state(
        connection, normative=normative, legacy=legacy,
    )
    if mapping_state != "valid":
        return mapping_state
    variable_mapping_state = _catalog_variable_bijection_state(
        connection, normative=normative, legacy=legacy,
    )
    if variable_mapping_state != "valid":
        return variable_mapping_state
    return "verified"


def _require_verified_catalog(
    connection: Any, normative: Any, legacy: Iterable[Table],
    *, allowed_migrations: Mapping[str, set[str]] | None = None,
) -> None:
    state = _catalog_state(
        connection, normative, legacy, allowed_migrations=allowed_migrations,
    )
    accepted_states = (
        {"verified", "migration_required"}
        if allowed_migrations is not None
        else {"verified"}
    )
    if state not in accepted_states:
        raise UnsupportedOperationError(
            f"The selected OpenStatSpec catalog is {state}; run explicit catalog initialization first."
        )


def require_verified_catalog(
    connection: Any,
    *, allowed_migrations: Mapping[str, set[str]] | None = None,
) -> None:
    """Require catalog ownership, shape, bijection, and only explicit migrations."""
    metadata = MetaData()
    legacy, normative = _catalog_layout(metadata)
    _require_verified_catalog(
        connection, normative, legacy, allowed_migrations=allowed_migrations,
    )


def _catalog_snapshot(
    connection: Any,
) -> tuple[set[str], dict[str, set[str]]]:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    return tables, {
        table_name: {
            str(column["name"]) for column in inspector.get_columns(table_name)
        }
        for table_name in tables
    }


def _compensate_catalog_initialization(
    connection: Any, *, metadata: MetaData, before_tables: set[str],
    before_columns: Mapping[str, set[str]], normative: Any,
    legacy: Iterable[Table],
) -> None:
    """Restore only when no concurrent initializer completed the catalog."""
    if _catalog_state(connection, normative, legacy) == "verified":
        return
    current_tables = set(inspect(connection).get_table_names())
    for table in reversed(metadata.sorted_tables):
        if table.name in current_tables and table.name not in before_tables:
            table.drop(connection, checkfirst=True)
    inspector = inspect(connection)
    preparer = connection.dialect.identifier_preparer
    for table_name, original_columns in before_columns.items():
        if not inspector.has_table(table_name):
            raise RuntimeError(
                f"Pre-existing catalog table {table_name!r} disappeared during initialization."
            )
        current_columns = {
            str(column["name"]) for column in inspect(connection).get_columns(table_name)
        }
        for column_name in sorted(current_columns - original_columns):
            connection.execute(text(
                f"ALTER TABLE {preparer.quote(table_name)} "
                f"DROP COLUMN {preparer.quote(column_name)}"
            ))


def _catalog_residual_inventory(
    engine: Any, *, before_tables: set[str],
    before_columns: Mapping[str, set[str]],
) -> dict[str, Any]:
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            current_tables = set(inspector.get_table_names())
            added_columns = {
                table_name: sorted(
                    {
                        str(column["name"])
                        for column in inspector.get_columns(table_name)
                    } - original_columns
                )
                for table_name, original_columns in before_columns.items()
                if table_name in current_tables
            }
            return {
                "new_tables": sorted(current_tables - before_tables),
                "missing_preexisting_tables": sorted(before_tables - current_tables),
                "added_columns": {
                    name: columns for name, columns in added_columns.items() if columns
                },
                "views": sorted(inspector.get_view_names()),
            }
    except Exception as inventory_error:
        return {"inspection_error_type": type(inventory_error).__name__}


def dolt_state_snapshot(
    *,
    database_url: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    """Return a read-only, digest-bound snapshot of one active Dolt database."""
    validate_connection_url(database_url)
    active = active_connection(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    if active["profile"] != "dolt":
        raise UnsupportedOperationError(
            "dolt_state_snapshot requires a positively identified Dolt connection."
        )
    metadata = MetaData()
    legacy, normative = _catalog_layout(metadata)
    audit_relations = {
        legacy[8].name, legacy[9].name,
        normative.fidelity_event.name, normative.operation.name,
    }
    engine = create_engine(database_url)
    with engine.connect() as connection:
        state = _capture_dolt_state(
            connection, profile_name="dolt", audit_relations=audit_relations,
        )
    assert state is not None
    _require_dolt_working_set_binding(
        state, active, phase="read-only state capture",
    )
    binding = active["working_set_binding"]
    return {
        "profile": "dolt",
        "server_version": active["server_version"],
        "read_only": True,
        "operational_write_enabled": dolt_operational_write_enabled(
            active, declaration_matched=bool(active["claimed_supported"]),
        ),
        "working_set_binding": binding,
        "state": state,
    }


def initialize_wide_catalog(
    *,
    database_url: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    """Install or explicitly migrate a dedicated catalog after server preflight."""
    validate_connection_url(database_url)
    parsed_url = make_url(database_url)
    if (
        parsed_url.get_backend_name() == "sqlite"
        and parsed_url.database in {None, "", ":memory:"}
    ):
        raise UnsupportedOperationError(
            "Catalog initialization requires a persistent SQLite database URL."
        )
    profile, active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    metadata = MetaData()
    legacy, normative = _catalog_layout(metadata)
    datasets, variables, multiple_response = legacy[:3]
    with engine.connect() as connection:
        state = _catalog_state(connection, normative, legacy)
        if state not in {"absent", "verified", "migration_required"}:
            raise UnsupportedOperationError(
                f"The selected database catalog is {state}; initialization is not permitted."
            )
        before_tables, before_columns = _catalog_snapshot(connection)
        pre_dolt_state = _capture_dolt_state(
            connection, profile_name=profile.name, audit_relations=set(),
        )
        _require_dolt_working_set_binding(
            pre_dolt_state, active, phase="catalog initialization preflight",
        )
        connection.rollback()
        try:
            with connection.begin():
                create_normative_catalog(connection, normative)
                metadata.create_all(connection, tables=list(legacy))
                _migrate_catalog_columns(
                    connection, datasets, variables, multiple_response,
                )
                _require_verified_catalog(connection, normative, legacy)
            post_dolt_state = _capture_dolt_state(
                connection, profile_name=profile.name, audit_relations=set(),
            )
            _require_dolt_working_set_binding(
                post_dolt_state, active, phase="catalog initialization completion",
            )
            _require_dolt_success_identity(
                pre_dolt_state, post_dolt_state, phase="catalog initialization",
            )
        except Exception as install_error:
            try:
                with connection.begin():
                    _compensate_catalog_initialization(
                        connection, metadata=metadata, before_tables=before_tables,
                        before_columns=before_columns, normative=normative,
                        legacy=legacy,
                    )
            except Exception as cleanup_error:
                inventory = _catalog_residual_inventory(
                    engine, before_tables=before_tables, before_columns=before_columns,
                )
                try:
                    after_dolt_state = _capture_dolt_state(
                        connection, profile_name=profile.name, audit_relations=set(),
                    )
                    dolt_boundary = _dolt_failure_boundary_evidence(
                        pre_dolt_state, after_dolt_state,
                    )
                except Exception as snapshot_error:
                    dolt_boundary = {
                        "applicable": profile.name == "dolt",
                        "verified": False,
                        "snapshot_fault": _safe_error_identity(
                            snapshot_error, phase="post_catalog_cleanup_dolt_state_capture",
                        ),
                    }
                raise ImportRecoveryError(
                    "cleanup_failed",
                    "Catalog initialization failed and its DDL compensation also failed.",
                    details={
                        "subcode": "catalog_install_cleanup_failed",
                        "original_cause": _safe_error_identity(
                            install_error, phase="catalog_initialization",
                        ),
                        "cleanup_fault": _safe_error_identity(
                            cleanup_error, phase="catalog_compensation",
                        ),
                        "residual_object_inventory": inventory,
                        "deterministic_recovery_evidence": {
                            "procedure_id": "openstatspec.catalog-init-compensation.v1",
                            "action_id": _canonical_sha256({
                                "namespace": active["catalog_binding"]["namespace"],
                                "before_tables": sorted(before_tables),
                            }),
                            "targets": {
                                "namespace": active["catalog_binding"]["namespace"],
                                "catalog_relations": sorted(
                                    table.name for table in metadata.tables.values()
                                ),
                            },
                            "residual_inventory_sha256": _canonical_sha256(inventory),
                            "cleanup_attempted": True,
                            "cleanup_succeeded": False,
                            "preexisting_unverified_catalog_mutation_forbidden": True,
                            "dolt_failure_boundary": dolt_boundary,
                        },
                        "success_forbidden": True,
                    },
                ) from cleanup_error
            try:
                after_dolt_state = _capture_dolt_state(
                    connection, profile_name=profile.name, audit_relations=set(),
                )
                dolt_boundary = _dolt_failure_boundary_evidence(
                    pre_dolt_state, after_dolt_state,
                )
            except Exception as snapshot_error:
                inventory = _catalog_residual_inventory(
                    engine, before_tables=before_tables, before_columns=before_columns,
                )
                recovery = {
                    "procedure_id": "openstatspec.dolt-failure-boundary.v1",
                    "action_id": _canonical_sha256({
                        "namespace": active["catalog_binding"]["namespace"],
                        "before_tables": sorted(before_tables),
                    }),
                    "targets": {
                        "namespace": active["catalog_binding"]["namespace"],
                        "catalog_relations": sorted(metadata.tables),
                    },
                    "residual_inventory_sha256": _canonical_sha256(inventory),
                    "dolt_failure_boundary": {
                        "applicable": profile.name == "dolt",
                        "verified": False,
                    },
                }
                raise ImportRecoveryError(
                    "cleanup_failed",
                    "Catalog compensation completed but Dolt state could not be verified.",
                    details={
                        "subcode": "dolt_state_capture_failed",
                        "original_cause": _safe_error_identity(
                            install_error, phase="catalog_initialization",
                        ),
                        "cleanup_fault": _safe_error_identity(
                            snapshot_error, phase="post_catalog_cleanup_dolt_state_capture",
                        ),
                        "residual_object_inventory": inventory,
                        "deterministic_recovery_evidence": recovery,
                        "success_forbidden": True,
                    },
                ) from snapshot_error
            if dolt_boundary.get("applicable") and not dolt_boundary.get("verified"):
                inventory = _catalog_residual_inventory(
                    engine, before_tables=before_tables, before_columns=before_columns,
                )
                recovery = {
                    "procedure_id": "openstatspec.dolt-failure-boundary.v1",
                    "action_id": _canonical_sha256({
                        "namespace": active["catalog_binding"]["namespace"],
                        "before_tables": sorted(before_tables),
                    }),
                    "targets": {
                        "namespace": active["catalog_binding"]["namespace"],
                        "catalog_relations": sorted(metadata.tables),
                    },
                    "residual_inventory_sha256": _canonical_sha256(inventory),
                    "dolt_failure_boundary": dolt_boundary,
                }
                raise ImportRecoveryError(
                    "cleanup_failed",
                    "Catalog compensation did not preserve Dolt failure-boundary invariants.",
                    details={
                        "subcode": "dolt_state_invariant_failed",
                        "original_cause": _safe_error_identity(
                            install_error, phase="catalog_initialization",
                        ),
                        "cleanup_fault": _verification_fault_identity(
                            "dolt_state_invariant_failed",
                            phase="post_catalog_cleanup_dolt_state_verification",
                            evidence=dolt_boundary,
                        ),
                        "residual_object_inventory": inventory,
                        "deterministic_recovery_evidence": recovery,
                        "success_forbidden": True,
                    },
                ) from install_error
            raise
    return {
        "profile": profile.name,
        "server_version": active["server_version"],
        "catalog": "verified",
    }


def _bounded_batches(
    rows: list[dict[str, Any]], variables: list[dict[str, Any]],
    maximum: int | None,
) -> Iterable[list[dict[str, Any]]]:
    if maximum is None:
        if rows:
            yield rows
        return
    batch: list[dict[str, Any]] = []
    used = 0
    for row in rows:
        size = statement_payload_bytes(row, variables)
        if size > maximum:
            raise RuntimeError("A preflighted row exceeds the active statement payload limit.")
        if batch and used + size > maximum:
            yield batch
            batch, used = [], 0
        batch.append(row)
        used += size
    if batch:
        yield batch


def _delete_normative_import_state(
    connection: Any, normative: Any, *, dataset_name: str,
    physical_table_name: str, normative_dataset_id: str | None,
    normative_dataset_creation_attempted: bool, operation_id: str,
) -> None:
    dataset_ids = []
    if normative_dataset_creation_attempted:
        dataset_ids = list(connection.execute(
            select(normative.dataset.c.dataset_id).where(
                normative.dataset.c.dataset_name == dataset_name,
                normative.dataset.c.physical_table_name == physical_table_name,
            )
        ).scalars())
    if normative_dataset_id is not None and normative_dataset_id not in dataset_ids:
        dataset_ids.append(normative_dataset_id)
    connection.execute(delete(normative.fidelity_event).where(
        normative.fidelity_event.c.operation_id == operation_id
    ))
    for normative_dataset_id in dataset_ids:
        variable_ids = list(connection.execute(
            select(normative.variable.c.variable_id)
            .where(normative.variable.c.dataset_id == normative_dataset_id)
        ).scalars())
        label_set_ids = list(connection.execute(
            select(normative.value_label_set.c.value_label_set_id)
            .where(normative.value_label_set.c.dataset_id == normative_dataset_id)
        ).scalars())
        variable_set_ids = list(connection.execute(
            select(normative.variable_set.c.variable_set_id)
            .where(normative.variable_set.c.dataset_id == normative_dataset_id)
        ).scalars())
        response_set_ids = list(connection.execute(
            select(normative.multiple_response_set.c.multiple_response_set_id)
            .where(normative.multiple_response_set.c.dataset_id == normative_dataset_id)
        ).scalars())
        if variable_set_ids:
            connection.execute(delete(normative.variable_set_member).where(
                normative.variable_set_member.c.variable_set_id.in_(variable_set_ids)
            ))
        if response_set_ids:
            connection.execute(delete(normative.multiple_response_member).where(
                normative.multiple_response_member.c.multiple_response_set_id.in_(response_set_ids)
            ))
        if variable_ids:
            for table in (
                normative.variable_value_label_set, normative.missing_rule,
                normative.variable_attribute,
            ):
                connection.execute(delete(table).where(table.c.variable_id.in_(variable_ids)))
        if label_set_ids:
            connection.execute(delete(normative.value_label).where(
                normative.value_label.c.value_label_set_id.in_(label_set_ids)
            ))
        for table in (
            normative.dataset_weight_variable, normative.dataset_attribute,
            normative.document, normative.variable_set, normative.multiple_response_set,
            normative.value_label_set, normative.fidelity_event, normative.variable,
        ):
            connection.execute(delete(table).where(table.c.dataset_id == normative_dataset_id))
        connection.execute(delete(normative.dataset).where(
            normative.dataset.c.dataset_id == normative_dataset_id
        ))
    connection.execute(delete(normative.operation).where(
        normative.operation.c.operation_id == operation_id
    ))


def _create_operation_owned_data_table(
    connection: Any, data_table: Table, state: dict[str, Any],
) -> None:
    """Mark ownership only after this operation successfully creates the table."""
    data_table.create(connection)
    state["data_table_created"] = True


def _cleanup_import_state(
    connection: Any, *, dataset_id: str, operation_id: str, data_table: Table,
    state: Mapping[str, Any], normative: Any, legacy: tuple[Table, ...],
) -> None:
    (
        datasets, variables, multiple_response, source_extensions, documents,
        value_labels, missing_rules, attributes, fidelity_events, operations,
    ) = legacy
    _delete_normative_import_state(
        connection, normative, dataset_name=dataset_id,
        physical_table_name=data_table.name,
        normative_dataset_id=state["normative_dataset_id"],
        normative_dataset_creation_attempted=state["normative_dataset_creation_attempted"],
        operation_id=operation_id,
    )
    if state["legacy_dataset_created"]:
        for table in (
            multiple_response, source_extensions, documents, value_labels,
            missing_rules, attributes,
        ):
            connection.execute(delete(table).where(table.c.dataset_id == dataset_id))
        connection.execute(delete(variables).where(variables.c.dataset_id == dataset_id))
        connection.execute(delete(datasets).where(datasets.c.dataset_id == dataset_id))
    connection.execute(delete(fidelity_events).where(
        fidelity_events.c.operation_id == operation_id
    ))
    connection.execute(delete(operations).where(operations.c.operation_id == operation_id))
    if state["data_table_created"]:
        data_table.drop(connection, checkfirst=True)


def _record_failed_import_audit(
    *, engine: Any, operation_id: str, source_name: str, source_format: str,
    variable_count: int, profile_name: str, import_error: Exception,
    normative: Any, legacy: tuple[Table, ...],
) -> None:
    """Persist only a failed operation and NULL-dataset event after cleanup."""
    fidelity_events, operations = legacy[8:]
    failed_event = {
        "code": "import_failed",
        "detail": "Import failed after mutation began; operation-owned state was removed.",
        "severity": "error",
        "source_item": source_name,
        "details": {
            "phase": "mutation",
            "profile": profile_name,
            "variable_count": variable_count,
            "error_type": type(import_error).__name__,
        },
    }
    with engine.begin() as connection:
        _require_verified_catalog(connection, normative, legacy)
        failed_at = datetime.now(UTC).replace(tzinfo=None)
        record_normative_operation(
            connection, normative, operation_id=operation_id,
            operation_kind="import", status="failed", source_format=source_format,
            started_at=failed_at, completed_at=failed_at,
        )
        connection.execute(insert(operations).values(
            operation_id=operation_id, direction="import", status="failed",
            dataset_id=None, source=source_name, created_at=_now(),
            completed_at=_now(), details=json.dumps({
                "reason": "runtime_failure",
                "variable_count": variable_count,
                "error_type": type(import_error).__name__,
            }, sort_keys=True),
        ))
        connection.execute(insert(fidelity_events), _event_rows(
            operation_id=operation_id, dataset_id=None, direction="import",
            fidelity_events=(failed_event,),
        ))
        record_normative_fidelity_events(
            connection, normative, operation_id=operation_id, dataset_id=None,
            direction="import", events=(failed_event,),
        )


def _record_import_cleanup_failure_audit(
    *, engine: Any, operation_id: str, source_name: str, source_format: str,
    profile_name: str, import_error: Exception, cleanup_error: Exception,
    residual_object_inventory: Mapping[str, Any],
    deterministic_recovery_evidence: Mapping[str, Any],
    normative: Any, legacy: tuple[Table, ...],
) -> None:
    """Best-effort immutable audit for verified-catalog cleanup failure."""
    fidelity_events, operations = legacy[8:]
    original = _safe_error_identity(import_error, phase="import_mutation")
    cleanup = _safe_error_identity(cleanup_error, phase="compensating_cleanup")
    event_details = {
        "original_cause": original,
        "cleanup_fault": cleanup,
        "residual_object_inventory": dict(residual_object_inventory),
        "deterministic_recovery_evidence": dict(
            deterministic_recovery_evidence
        ),
    }
    event = {
        "code": "cleanup_failed",
        "detail": "Import cleanup failed; terminal recovery requires out-of-band review.",
        "severity": "error",
        "source_item": source_name,
        "details": event_details,
    }
    with engine.begin() as connection:
        _require_verified_catalog(connection, normative, legacy)
        existing = connection.execute(select(operations).where(
            operations.c.operation_id == operation_id
        )).mappings().one_or_none()
        normative_existing = connection.execute(select(normative.operation).where(
            normative.operation.c.operation_id == operation_id
        )).mappings().one_or_none()
        if (existing is None) != (normative_existing is None):
            raise UnsupportedOperationError(
                "Import operation catalogs disagree about cleanup-failure state."
            )
        if existing is not None:
            if (
                existing["direction"] != "import"
                or existing["status"] != "running"
                or normative_existing["status"] != "started"
            ):
                raise UnsupportedOperationError(
                    "Existing import operation is not in an auditable running state."
                )
            details = json.loads(existing["details"] or "{}")
            details["cleanup_failure"] = event_details
            connection.execute(update(operations).where(
                operations.c.operation_id == operation_id
            ).values(
                status="failed", completed_at=_now(),
                details=json.dumps(details, sort_keys=True),
            ))
            finish_normative_operation(
                connection, normative, operation_id=operation_id, status="failed",
            )
            ordinals = connection.execute(select(fidelity_events.c.ordinal).where(
                fidelity_events.c.operation_id == operation_id
            )).scalars().all()
            event_row = _event_rows(
                operation_id=operation_id, dataset_id=None, direction="import",
                fidelity_events=(event,),
            )[0]
            event_row["ordinal"] = max(ordinals, default=0) + 1
            connection.execute(insert(fidelity_events).values(**event_row))
            record_normative_fidelity_events(
                connection, normative, operation_id=operation_id, dataset_id=None,
                direction="import", events=(event,),
            )
        else:
            failed_at = datetime.now(UTC).replace(tzinfo=None)
            record_normative_operation(
                connection, normative, operation_id=operation_id,
                operation_kind="import", status="failed", source_format=source_format,
                started_at=failed_at, completed_at=failed_at,
            )
            connection.execute(insert(operations).values(
                operation_id=operation_id, direction="import", status="failed",
                dataset_id=None, source=source_name, created_at=_now(),
                completed_at=_now(), details=json.dumps({
                    "reason": "cleanup_failed",
                    "profile": profile_name, **event_details,
                }, sort_keys=True),
            ))
            connection.execute(insert(fidelity_events), _event_rows(
                operation_id=operation_id, dataset_id=None, direction="import",
                fidelity_events=(event,),
            ))
            record_normative_fidelity_events(
                connection, normative, operation_id=operation_id, dataset_id=None,
                direction="import", events=(event,),
            )


def _import_residual_inventory(
    engine: Any, *, dataset_id: str, operation_id: str, data_table: Table,
    state: Mapping[str, Any], normative: Any, legacy: tuple[Table, ...],
) -> dict[str, Any]:
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())

            def count_rows(table: Table, condition: Any) -> int | None:
                if table.name not in tables:
                    return None
                return len(connection.execute(select(table).where(condition)).all())

            return {
                "data_table": {
                    "name": data_table.name,
                    "present": data_table.name in tables,
                },
                "legacy_dataset_rows": count_rows(
                    legacy[0], legacy[0].c.dataset_id == dataset_id,
                ),
                "legacy_operation_rows": count_rows(
                    legacy[9], legacy[9].c.operation_id == operation_id,
                ),
                "legacy_fidelity_event_rows": count_rows(
                    legacy[8], legacy[8].c.operation_id == operation_id,
                ),
                "normative_dataset_rows": count_rows(
                    normative.dataset,
                    (
                        normative.dataset.c.dataset_name == dataset_id
                    ) & (
                        normative.dataset.c.physical_table_name == data_table.name
                    ),
                ),
                "normative_operation_rows": count_rows(
                    normative.operation,
                    normative.operation.c.operation_id == operation_id,
                ),
                "normative_fidelity_event_rows": count_rows(
                    normative.fidelity_event,
                    normative.fidelity_event.c.operation_id == operation_id,
                ),
                "mutation_markers": dict(state),
            }
    except Exception as inventory_error:
        return {"inspection_error_type": type(inventory_error).__name__}


def _requires_compensating_import_cleanup(profile_name: str) -> bool:
    """Transactional profiles rely on rollback, never stale cleanup markers."""
    return profile_name in MYSQL_WIRE_PROFILES


@contextmanager
def _import_cleanup_guard(
    *, engine: Any, dataset_id: str, operation_id: str, data_table: Table,
    source_name: str, source_format: str, variable_count: int,
    profile_name: str, normative: Any, legacy: tuple[Table, ...],
    snapshot_connection: Any, pre_dolt_state: dict[str, Any] | None,
) -> Iterable[dict[str, Any]]:
    state: dict[str, Any] = {
        "data_table_created": False,
        "legacy_dataset_created": False,
        "normative_dataset_creation_attempted": False,
        "normative_dataset_id": None,
    }
    audit_relations = {
        legacy[8].name, legacy[9].name,
        normative.fidelity_event.name, normative.operation.name,
    }

    def capture_boundary() -> dict[str, Any]:
        after = _capture_dolt_state(
            snapshot_connection, profile_name=profile_name,
            audit_relations=audit_relations,
        )
        return _dolt_failure_boundary_evidence(pre_dolt_state, after)

    try:
        yield state
    except Exception as import_error:
        try:
            if _requires_compensating_import_cleanup(profile_name):
                with engine.begin() as cleanup_connection:
                    _cleanup_import_state(
                        cleanup_connection, dataset_id=dataset_id,
                        operation_id=operation_id, data_table=data_table,
                        state=state, normative=normative, legacy=legacy,
                    )
        except Exception as cleanup_error:
            inventory = _import_residual_inventory(
                engine, dataset_id=dataset_id, operation_id=operation_id,
                data_table=data_table, state=state, normative=normative,
                legacy=legacy,
            )
            try:
                pre_audit_dolt_boundary = capture_boundary()
            except Exception as snapshot_error:
                pre_audit_dolt_boundary = {
                    "applicable": profile_name == "dolt",
                    "verified": False,
                    "snapshot_fault": _safe_error_identity(
                        snapshot_error, phase="post_cleanup_dolt_state_capture",
                    ),
                }
            audit_recovery = {
                "procedure_id": "openstatspec.import-compensation.v1",
                "action_id": operation_id,
                "targets": {
                    "dataset_id": dataset_id,
                    "physical_table": data_table.name,
                },
                "residual_inventory_sha256": _canonical_sha256(inventory),
                "cleanup_attempted": True,
                "cleanup_succeeded": False,
                "operation_owned_state_targeted": True,
                "dolt_failure_boundary": pre_audit_dolt_boundary,
            }
            cleanup_audit_fault = None
            audit_permitted = not (
                pre_audit_dolt_boundary.get("applicable")
                and not pre_audit_dolt_boundary.get("verified")
            )
            if not audit_permitted:
                cleanup_audit_fault = _verification_fault_identity(
                    "dolt_state_unverified_before_cleanup_failed_audit",
                    phase="pre_cleanup_failed_audit_boundary",
                    evidence=pre_audit_dolt_boundary,
                )
                dolt_boundary = pre_audit_dolt_boundary
            else:
                try:
                    _record_import_cleanup_failure_audit(
                    engine=engine, operation_id=operation_id,
                    source_name=source_name, source_format=source_format,
                    profile_name=profile_name, import_error=import_error,
                    cleanup_error=cleanup_error,
                    residual_object_inventory=inventory,
                    deterministic_recovery_evidence=audit_recovery,
                    normative=normative, legacy=legacy,
                    )
                except Exception as audit_error:
                    cleanup_audit_fault = _safe_error_identity(
                        audit_error, phase="cleanup_failed_audit",
                    )
                try:
                    dolt_boundary = capture_boundary()
                except Exception as snapshot_error:
                    dolt_boundary = {
                        "applicable": profile_name == "dolt",
                        "verified": False,
                        "snapshot_fault": _safe_error_identity(
                            snapshot_error,
                            phase="post_cleanup_audit_dolt_state_capture",
                        ),
                    }
            raise ImportRecoveryError(
                "cleanup_failed",
                "Import failed and complete compensating cleanup also failed.",
                details={
                    "subcode": "import_cleanup_failed",
                    "original_cause": _safe_error_identity(
                        import_error, phase="import_mutation",
                    ),
                    "cleanup_fault": _safe_error_identity(
                        cleanup_error, phase="compensating_cleanup",
                    ),
                    "residual_object_inventory": inventory,
                    "deterministic_recovery_evidence": {
                        "procedure_id": "openstatspec.import-compensation.v1",
                        "action_id": operation_id,
                        "targets": {
                            "dataset_id": dataset_id,
                            "physical_table": data_table.name,
                        },
                        "residual_inventory_sha256": _canonical_sha256(inventory),
                        "cleanup_attempted": True,
                        "cleanup_succeeded": False,
                        "operation_owned_state_targeted": True,
                        "cleanup_failed_audit_persisted": cleanup_audit_fault is None,
                        "terminal_reporting": (
                            "catalog_and_exception" if cleanup_audit_fault is None
                            else "out_of_band_exception"
                        ),
                        "dolt_failure_boundary": dolt_boundary,
                    },
                    "audit_fault": cleanup_audit_fault,
                    "success_forbidden": True,
                },
            ) from cleanup_error
        inventory = _import_residual_inventory(
            engine, dataset_id=dataset_id, operation_id=operation_id,
            data_table=data_table, state=state, normative=normative,
            legacy=legacy,
        )
        try:
            pre_failed_audit_boundary = capture_boundary()
        except Exception as snapshot_error:
            raise ImportRecoveryError(
                "cleanup_failed",
                "Import cleanup completed but its pre-audit Dolt boundary could not be captured.",
                details={
                    "subcode": "pre_failed_audit_dolt_state_capture_failed",
                    "original_cause": _safe_error_identity(
                        import_error, phase="import_mutation",
                    ),
                    "cleanup_fault": _safe_error_identity(
                        snapshot_error, phase="pre_failed_audit_dolt_state_capture",
                    ),
                    "residual_object_inventory": inventory,
                    "deterministic_recovery_evidence": {
                        "procedure_id": "openstatspec.dolt-failure-boundary.v1",
                        "action_id": operation_id,
                        "targets": {
                            "dataset_id": dataset_id,
                            "physical_table": data_table.name,
                        },
                        "residual_inventory_sha256": _canonical_sha256(inventory),
                        "failed_operation_audit_attempted": False,
                        "terminal_reporting": "out_of_band_exception",
                        "dolt_failure_boundary": {
                            "applicable": profile_name == "dolt",
                            "verified": False,
                        },
                    },
                    "audit_fault": None,
                    "success_forbidden": True,
                },
            ) from snapshot_error
        if (
            pre_failed_audit_boundary.get("applicable")
            and not pre_failed_audit_boundary.get("verified")
        ):
            raise ImportRecoveryError(
                "cleanup_failed",
                "Import cleanup completed but its pre-audit Dolt boundary is unverified.",
                details={
                    "subcode": "pre_failed_audit_dolt_state_invariant_failed",
                    "original_cause": _safe_error_identity(
                        import_error, phase="import_mutation",
                    ),
                    "cleanup_fault": _verification_fault_identity(
                        "dolt_state_invariant_failed",
                        phase="pre_failed_audit_dolt_state_verification",
                        evidence=pre_failed_audit_boundary,
                    ),
                    "residual_object_inventory": inventory,
                    "deterministic_recovery_evidence": {
                        "procedure_id": "openstatspec.dolt-failure-boundary.v1",
                        "action_id": operation_id,
                        "targets": {
                            "dataset_id": dataset_id,
                            "physical_table": data_table.name,
                        },
                        "residual_inventory_sha256": _canonical_sha256(inventory),
                        "failed_operation_audit_attempted": False,
                        "terminal_reporting": "out_of_band_exception",
                        "dolt_failure_boundary": pre_failed_audit_boundary,
                    },
                    "audit_fault": None,
                    "success_forbidden": True,
                },
            ) from import_error
        try:
            _record_failed_import_audit(
                engine=engine, operation_id=operation_id,
                source_name=source_name, source_format=source_format,
                variable_count=variable_count, profile_name=profile_name,
                import_error=import_error, normative=normative, legacy=legacy,
            )
        except Exception as audit_error:
            raise ImportRecoveryError(
                "failure_audit_failed",
                "Import cleanup succeeded but its failed-operation audit could not be persisted.",
                details={
                    "original_cause": _safe_error_identity(
                        import_error, phase="import_mutation",
                    ),
                    "cleanup_fault": _safe_error_identity(
                        audit_error, phase="failed_operation_audit",
                    ),
                    "residual_object_inventory": inventory,
                    "deterministic_recovery_evidence": {
                        "procedure_id": "openstatspec.failed-import-audit.v1",
                        "action_id": operation_id,
                        "targets": {
                            "dataset_id": dataset_id,
                            "physical_table": data_table.name,
                        },
                        "residual_inventory_sha256": _canonical_sha256(inventory),
                    },
                    "success_forbidden": True,
                },
            ) from audit_error
        try:
            dolt_boundary = capture_boundary()
        except Exception as snapshot_error:
            inventory = _import_residual_inventory(
                engine, dataset_id=dataset_id, operation_id=operation_id,
                data_table=data_table, state=state, normative=normative,
                legacy=legacy,
            )
            raise ImportRecoveryError(
                "cleanup_failed",
                "Import cleanup completed but Dolt failure-boundary state could not be verified.",
                details={
                    "subcode": "dolt_state_capture_failed",
                    "original_cause": _safe_error_identity(
                        import_error, phase="import_mutation",
                    ),
                    "cleanup_fault": _safe_error_identity(
                        snapshot_error, phase="post_audit_dolt_state_capture",
                    ),
                    "residual_object_inventory": inventory,
                    "deterministic_recovery_evidence": {
                        "procedure_id": "openstatspec.dolt-failure-boundary.v1",
                        "action_id": operation_id,
                        "targets": {
                            "dataset_id": dataset_id,
                            "physical_table": data_table.name,
                        },
                        "residual_inventory_sha256": _canonical_sha256(inventory),
                        "dolt_failure_boundary": {
                            "applicable": profile_name == "dolt",
                            "verified": False,
                        },
                    },
                    "success_forbidden": True,
                },
            ) from snapshot_error
        if dolt_boundary.get("applicable") and not dolt_boundary.get("verified"):
            inventory = _import_residual_inventory(
                engine, dataset_id=dataset_id, operation_id=operation_id,
                data_table=data_table, state=state, normative=normative,
                legacy=legacy,
            )
            raise ImportRecoveryError(
                "cleanup_failed",
                "Import cleanup did not preserve the Dolt failure-boundary invariants.",
                details={
                    "subcode": "dolt_state_invariant_failed",
                    "original_cause": _safe_error_identity(
                        import_error, phase="import_mutation",
                    ),
                    "cleanup_fault": _verification_fault_identity(
                        "dolt_state_invariant_failed",
                        phase="post_audit_dolt_state_verification",
                        evidence=dolt_boundary,
                    ),
                    "residual_object_inventory": inventory,
                    "deterministic_recovery_evidence": {
                        "procedure_id": "openstatspec.dolt-failure-boundary.v1",
                        "action_id": operation_id,
                        "targets": {
                            "dataset_id": dataset_id,
                            "physical_table": data_table.name,
                        },
                        "residual_inventory_sha256": _canonical_sha256(inventory),
                        "dolt_failure_boundary": dolt_boundary,
                    },
                    "success_forbidden": True,
                },
            ) from import_error
        raise


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
    operation_details: Mapping[str, Any] | None = None,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    validate_connection_url(database_url)
    profile, active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    metadata = MetaData()
    datasets, variable_catalog, fidelity_event_catalog, operation_catalog = catalog(metadata)
    normative = normative_catalog(metadata)
    multiple_response_catalog = multiple_response_set_catalog(metadata)
    source_extensions_catalog = source_extension_catalog(metadata)
    documents_catalog, value_labels_catalog, missing_rules_catalog, attributes_catalog = normalized_metadata_tables(metadata)
    legacy = (
        datasets, variable_catalog, multiple_response_catalog, source_extensions_catalog,
        documents_catalog, value_labels_catalog, missing_rules_catalog,
        attributes_catalog, fidelity_event_catalog, operation_catalog,
    )
    operation_id = str(uuid4())
    fidelity_events = tuple(fidelity_events)
    source_rows = list(rows)
    data_table = Table(
        data_table_name(dataset_id), metadata,
        Column("__case_ordinal", BigInteger, primary_key=True, nullable=False),
    )
    audit_relations = {
        fidelity_event_catalog.name, operation_catalog.name,
        normative.fidelity_event.name, normative.operation.name,
    }
    preflight_state = {
        "data_table_created": False,
        "legacy_dataset_created": False,
        "normative_dataset_creation_attempted": False,
        "normative_dataset_id": None,
    }
    with engine.connect() as preflight_connection:
        _require_verified_catalog(preflight_connection, normative, legacy)
        preflight_dolt_state = _capture_dolt_state(
            preflight_connection, profile_name=profile.name,
            audit_relations=audit_relations,
        )
        _require_dolt_working_set_binding(
            preflight_dolt_state, active, phase="import preflight",
        )
        preflight_connection.rollback()
        try:
            preflight_identifier(
                profile, data_table.name, role="physical data-table identifier",
            )
            preflight(profile, variables, rows=source_rows)
            validate_spss_catalog(
                variables,
                case_weight_variable=case_weight_variable,
                multiple_response_sets=multiple_response_sets,
            )
            for item in variables:
                data_table.append_column(Column(
                    item["physical_name"],
                    _wide_column_type(profile, item["storage_kind"]),
                    nullable=item["storage_kind"] == "numeric",
                ))
        except Exception as error:
            try:
                _record_failed_preflight(
                    engine=engine, metadata=metadata, datasets=datasets,
                    variable_catalog=variable_catalog,
                    multiple_response_catalog=multiple_response_catalog,
                    fidelity_event_catalog=fidelity_event_catalog,
                    operation_catalog=operation_catalog, operation_id=operation_id,
                    source_name=source_name, source_format=source_format,
                    variable_count=len(variables), profile_name=profile.name,
                    error=error, normative=normative, legacy=legacy,
                )
            except Exception as audit_error:
                inventory = _import_residual_inventory(
                    engine, dataset_id=dataset_id, operation_id=operation_id,
                    data_table=data_table, state=preflight_state,
                    normative=normative, legacy=legacy,
                )
                raise ImportRecoveryError(
                    "failure_audit_failed",
                    "Preflight failed and its failed-operation audit could not be persisted.",
                    details={
                        "original_cause": _safe_error_identity(
                            error, phase="import_preflight",
                        ),
                        "cleanup_fault": _safe_error_identity(
                            audit_error, phase="failed_preflight_audit",
                        ),
                        "residual_object_inventory": inventory,
                        "deterministic_recovery_evidence": {
                            "procedure_id": "openstatspec.failed-preflight-audit.v1",
                            "action_id": operation_id,
                            "targets": {
                                "dataset_id": dataset_id,
                                "physical_table": data_table.name,
                            },
                            "residual_inventory_sha256": _canonical_sha256(inventory),
                        },
                        "success_forbidden": True,
                    },
                ) from audit_error
            try:
                post_preflight_dolt_state = _capture_dolt_state(
                    preflight_connection, profile_name=profile.name,
                    audit_relations=audit_relations,
                )
                dolt_boundary = _dolt_failure_boundary_evidence(
                    preflight_dolt_state, post_preflight_dolt_state,
                )
            except Exception as snapshot_error:
                inventory = _import_residual_inventory(
                    engine, dataset_id=dataset_id, operation_id=operation_id,
                    data_table=data_table, state=preflight_state,
                    normative=normative, legacy=legacy,
                )
                raise ImportRecoveryError(
                    "cleanup_failed",
                    "Preflight audit completed but Dolt state could not be verified.",
                    details={
                        "subcode": "dolt_state_capture_failed",
                        "original_cause": _safe_error_identity(
                            error, phase="import_preflight",
                        ),
                        "cleanup_fault": _safe_error_identity(
                            snapshot_error, phase="post_preflight_audit_dolt_state_capture",
                        ),
                        "residual_object_inventory": inventory,
                        "deterministic_recovery_evidence": {
                            "procedure_id": "openstatspec.dolt-failure-boundary.v1",
                            "action_id": operation_id,
                            "targets": {
                                "dataset_id": dataset_id,
                                "physical_table": data_table.name,
                            },
                            "residual_inventory_sha256": _canonical_sha256(inventory),
                            "dolt_failure_boundary": {
                                "applicable": profile.name == "dolt",
                                "verified": False,
                            },
                        },
                        "success_forbidden": True,
                    },
                ) from snapshot_error
            if dolt_boundary.get("applicable") and not dolt_boundary.get("verified"):
                inventory = _import_residual_inventory(
                    engine, dataset_id=dataset_id, operation_id=operation_id,
                    data_table=data_table, state=preflight_state,
                    normative=normative, legacy=legacy,
                )
                raise ImportRecoveryError(
                    "cleanup_failed",
                    "Preflight audit changed non-audit Dolt state.",
                    details={
                        "subcode": "dolt_state_invariant_failed",
                        "original_cause": _safe_error_identity(
                            error, phase="import_preflight",
                        ),
                        "cleanup_fault": _verification_fault_identity(
                            "dolt_state_invariant_failed",
                            phase="post_preflight_audit_dolt_state_verification",
                            evidence=dolt_boundary,
                        ),
                        "residual_object_inventory": inventory,
                        "deterministic_recovery_evidence": {
                            "procedure_id": "openstatspec.dolt-failure-boundary.v1",
                            "action_id": operation_id,
                            "targets": {
                                "dataset_id": dataset_id,
                                "physical_table": data_table.name,
                            },
                            "residual_inventory_sha256": _canonical_sha256(inventory),
                            "dolt_failure_boundary": dolt_boundary,
                        },
                        "success_forbidden": True,
                    },
                ) from error
            raise
    with engine.connect() as mutation_connection:
        pre_dolt_state = _capture_dolt_state(
            mutation_connection, profile_name=profile.name,
            audit_relations=audit_relations,
        )
        _require_dolt_working_set_binding(
            pre_dolt_state, active, phase="import mutation preflight",
        )
        mutation_connection.rollback()
        with _import_cleanup_guard(
            engine=engine, dataset_id=dataset_id, operation_id=operation_id,
            data_table=data_table, source_name=source_name,
            source_format=source_format, variable_count=len(variables),
            profile_name=profile.name, normative=normative, legacy=legacy,
            snapshot_connection=mutation_connection,
            pre_dolt_state=pre_dolt_state,
        ) as mutation:
            with mutation_connection.begin():
                connection = mutation_connection
                _require_verified_catalog(connection, normative, legacy)
                record_normative_operation(
                    connection, normative, operation_id=operation_id,
                    operation_kind="import", status="started", source_format=source_format,
                )
                connection.execute(insert(operation_catalog).values(
                    operation_id=operation_id, direction="import", status="running", dataset_id=dataset_id,
                    source=source_name, created_at=_now(), details=json.dumps({"variable_count": len(variables), **dict(operation_details or {})}, sort_keys=True),
                ))
                if connection.execute(select(datasets.c.dataset_id).where(datasets.c.dataset_id == dataset_id)).first():
                    raise ValueError(f"Dataset {dataset_id!r} already exists; imports never overwrite a dataset.")
                if connection.execute(select(datasets.c.dataset_id).where(datasets.c.data_table == data_table.name)).first():
                    raise ValueError(f"Dataset ID {dataset_id!r} collides with an existing physical data-table name; import was not started.")
                _create_operation_owned_data_table(
                    connection, data_table, mutation,
                )
                materialized = [
                    {"__case_ordinal": ordinal, **row}
                    for ordinal, row in enumerate(source_rows, start=1)
                ]
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
                mutation["legacy_dataset_created"] = True
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
                mutation["normative_dataset_creation_attempted"] = True
                normative_dataset_id = store_normative_dataset(
                    connection, normative, dataset_name=dataset_id,
                    source_format=source_format, physical_table_name=data_table.name,
                    dataset_label=file_label, source_encoding=source_encoding,
                    source_hash=source_sha256, source_case_count=len(materialized),
                    imported_at=imported_at or None, variables=variables,
                    documents=docs_rows, value_labels=labels_rows,
                    missing_rules=missing_rows, attributes=attributes_rows,
                    multiple_response_sets=mrset_rows,
                    source_extensions=source_extensions or {},
                    case_weight_variable=case_weight_variable,
                )
                mutation["normative_dataset_id"] = normative_dataset_id
                record_normative_fidelity_events(
                    connection, normative, operation_id=operation_id,
                    dataset_id=normative_dataset_id, direction="import",
                    events=fidelity_events,
                )
                if materialized:
                    for batch in _bounded_batches(
                        materialized, variables, profile.max_statement_bytes,
                    ):
                        connection.execute(insert(data_table), batch)
                connection.execute(update(operation_catalog).where(operation_catalog.c.operation_id == operation_id).values(
                    status="succeeded", completed_at=_now(),
                ))
                finish_normative_operation(
                    connection, normative, operation_id=operation_id, status="succeeded",
                )
                post_dolt_state = _capture_dolt_state(
                    mutation_connection, profile_name=profile.name,
                    audit_relations=audit_relations,
                )
                _require_dolt_working_set_binding(
                    post_dolt_state, active, phase="import completion",
                )
                _require_dolt_success_identity(
                    pre_dolt_state, post_dolt_state, phase="import",
                )
    return {"dataset_id": dataset_id, "data_table": data_table.name, "case_count": len(materialized), "operation_id": operation_id}


def _endpoint_from_row(row: Mapping[str, Any], *, prefix: str) -> Any:
    endpoint_type = row[f"{prefix}_type"]
    if endpoint_type == "lowest":
        return -sys.float_info.max
    if endpoint_type == "highest":
        return sys.float_info.max
    return row[f"{prefix}_numeric"] if endpoint_type == "numeric" else row[f"{prefix}_text"]


def _canonicalize_database_numeric_rows(
    rows: Iterable[Mapping[str, Any]],
    variables: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert exact numeric driver wrappers at the SQL read boundary.

    MySQL-family drivers may return DOUBLE columns as Decimal instances. The
    public adapter contract remains binary64-only, so database-native decimal
    wrappers are converted back to their declared physical representation
    before strict preflight validation. Other unexpected values are preserved
    so preflight can reject them with its machine-readable diagnostic.
    """
    numeric_names = {
        str(variable["physical_name"])
        for variable in variables
        if variable.get("storage_kind") == "numeric"
    }
    normalized: list[dict[str, Any]] = []
    for source_row in rows:
        row = dict(source_row)
        for physical_name in numeric_names:
            value = row.get(physical_name)
            if isinstance(value, Decimal):
                row[physical_name] = float(value)
        normalized.append(row)
    return normalized


def read_wide_dataset(
    *,
    database_url: str,
    dataset_id: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Read a strict dataset, preferring normalized metadata with JSON compatibility fallback."""
    profile, _active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    metadata = MetaData()
    datasets, variable_catalog, fidelity_catalog, operation_catalog = catalog(metadata)
    normative = normative_catalog(metadata)
    multiple_response_catalog = multiple_response_set_catalog(metadata)
    source_extensions_catalog = source_extension_catalog(metadata)
    documents_catalog, value_labels_catalog, missing_rules_catalog, attributes_catalog = normalized_metadata_tables(metadata)
    legacy = (
        datasets, variable_catalog, multiple_response_catalog, source_extensions_catalog,
        documents_catalog, value_labels_catalog, missing_rules_catalog,
        attributes_catalog, fidelity_catalog, operation_catalog,
    )
    with engine.connect() as connection:
        _require_verified_catalog(connection, normative, legacy)
        dataset = dict(connection.execute(select(datasets).where(datasets.c.dataset_id == dataset_id)).mappings().one())
        data_table = Table(dataset["data_table"], MetaData(), autoload_with=connection)
        variables = [dict(item) for item in connection.execute(
            select(variable_catalog).where(variable_catalog.c.dataset_id == dataset_id).order_by(variable_catalog.c.ordinal)
        ).mappings().all()]
        rows = _canonicalize_database_numeric_rows(
            connection.execute(
                select(data_table).order_by(data_table.c.__case_ordinal)
            ).mappings().all(),
            variables,
        )
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
    preflight(profile, variables, rows=rows)
    return dataset, variables, rows


def read_fidelity_events(
    *,
    database_url: str,
    dataset_id: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> tuple[dict[str, Any], ...]:
    """Read import-time fidelity diagnostics for a catalogued dataset."""
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    metadata = MetaData()
    legacy, normative = _catalog_layout(metadata)
    fidelity_event_catalog = legacy[8]
    with engine.connect() as connection:
        _require_verified_catalog(connection, normative, legacy)
        events = connection.execute(
            select(fidelity_event_catalog)
            .where(
                fidelity_event_catalog.c.dataset_id == dataset_id,
                fidelity_event_catalog.c.direction == "import",
            )
            .order_by(fidelity_event_catalog.c.code)
        ).mappings().all()
    return tuple({
        "code": item["code"], "detail": item["detail"],
        "details": json.loads(item["details"] or "{}"),
    } for item in events)



def record_export_cleanup_failure(
    *, database_url: str, destination: str, original_error: Exception,
    cleanup_error: Exception,
    residual_object_inventory: Mapping[str, Any],
    deterministic_recovery_evidence: Mapping[str, Any],
    operation_id: str | None = None,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> str:
    """Best-effort immutable export cleanup-failure audit."""
    profile, active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    metadata = MetaData()
    legacy, normative = _catalog_layout(metadata)
    fidelity_events, operations = legacy[8:]
    requested_operation_id = operation_id
    operation_id = operation_id or str(uuid4())
    original = _safe_error_identity(original_error, phase="export")
    cleanup = _safe_error_identity(cleanup_error, phase="export_destination_restore")
    event_details = {
        "original_cause": original,
        "cleanup_fault": cleanup,
        "residual_object_inventory": dict(residual_object_inventory),
        "deterministic_recovery_evidence": dict(
            deterministic_recovery_evidence
        ),
    }
    event = {
        "code": "cleanup_failed",
        "detail": "Export destination recovery failed; out-of-band review is required.",
        "severity": "error",
        "source_item": destination,
        "details": event_details,
    }
    audit_relations = {
        legacy[8].name, legacy[9].name,
        normative.fidelity_event.name, normative.operation.name,
    }
    with _bound_catalog_transaction(
        engine=engine, profile_name=profile.name, active=active,
        audit_relations=audit_relations, phase="record export cleanup failure",
    ) as connection:
        _require_verified_catalog(connection, normative, legacy)
        if requested_operation_id is not None:
            existing = connection.execute(select(operations).where(
                operations.c.operation_id == operation_id
            )).mappings().one_or_none()
            normative_existing = connection.execute(select(normative.operation).where(
                normative.operation.c.operation_id == operation_id
            )).mappings().one_or_none()
            if (
                existing is None or normative_existing is None
                or existing["direction"] != "export"
                or existing["status"] != "running"
                or normative_existing["status"] != "started"
            ):
                raise UnsupportedOperationError(
                    "Existing export operation is not in an auditable terminal-transition state."
                )
            details = json.loads(existing["details"] or "{}")
            details["cleanup_failure"] = event_details
            connection.execute(update(operations).where(
                operations.c.operation_id == operation_id
            ).values(
                status="failed", completed_at=_now(),
                details=json.dumps(details, sort_keys=True),
            ))
            finish_normative_operation(
                connection, normative, operation_id=operation_id, status="failed",
            )
            ordinals = connection.execute(select(fidelity_events.c.ordinal).where(
                fidelity_events.c.operation_id == operation_id
            )).scalars().all()
            event_row = _event_rows(
                operation_id=operation_id, dataset_id=None, direction="export",
                fidelity_events=(event,),
            )[0]
            event_row["ordinal"] = max(ordinals, default=0) + 1
            connection.execute(insert(fidelity_events).values(**event_row))
            record_normative_fidelity_events(
                connection, normative, operation_id=operation_id, dataset_id=None,
                direction="export", events=(event,),
            )
        else:
            failed_at = datetime.now(UTC).replace(tzinfo=None)
            record_normative_operation(
                connection, normative, operation_id=operation_id,
                operation_kind="export", status="failed", source_format=None,
                started_at=failed_at, completed_at=failed_at,
            )
            connection.execute(insert(operations).values(
                operation_id=operation_id, direction="export", status="failed",
                dataset_id=None, destination=destination, created_at=_now(),
                completed_at=_now(), details=json.dumps({
                    "reason": "cleanup_failed", **event_details,
                }, sort_keys=True),
            ))
            connection.execute(insert(fidelity_events), _event_rows(
                operation_id=operation_id, dataset_id=None, direction="export",
                fidelity_events=(event,),
            ))
            record_normative_fidelity_events(
                connection, normative, operation_id=operation_id, dataset_id=None,
                direction="export", events=(event,),
            )
    return operation_id


def record_export_operation(
    *, database_url: str, dataset_id: str, destination: str,
    allowed_fidelity_events: Iterable[Mapping[str, Any]],
    operation_details: Mapping[str, Any] | None = None,
    terminal: bool = True,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> str:
    """Persist a completed export and the fidelity loss explicitly accepted by its caller."""
    profile, active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    metadata = MetaData()
    legacy, normative = _catalog_layout(metadata)
    datasets, variables, multiple_response = legacy[:3]
    fidelity_events, operations = legacy[8:]
    operation_id = str(uuid4())
    events = tuple(allowed_fidelity_events)
    audit_relations = {
        legacy[8].name, legacy[9].name,
        normative.fidelity_event.name, normative.operation.name,
    }
    with _bound_catalog_transaction(
        engine=engine, profile_name=profile.name, active=active,
        audit_relations=audit_relations, phase="record export operation",
    ) as connection:
        _require_verified_catalog(connection, normative, legacy)
        normative_dataset_id = normative_dataset_id_for_name(connection, normative, dataset_id)
        completed_at = datetime.now(UTC).replace(tzinfo=None)
        normative_status = "succeeded" if terminal else "started"
        legacy_status = "succeeded" if terminal else "running"
        record_normative_operation(
            connection, normative, operation_id=operation_id,
            operation_kind="export", status=normative_status, source_format=None,
            started_at=completed_at, completed_at=completed_at if terminal else None,
        )
        connection.execute(insert(operations).values(
            operation_id=operation_id, direction="export", status=legacy_status, dataset_id=dataset_id,
            destination=destination, created_at=_now(),
            completed_at=_now() if terminal else None,
            details=json.dumps({"allow_loss": [event["code"] for event in events], **dict(operation_details or {})}, sort_keys=True),
        ))
        rows = _event_rows(
            operation_id=operation_id, dataset_id=dataset_id, direction="export",
            fidelity_events=({**event, "severity": event.get("severity", "warning"),
                              "details": {**event.get("details", {}), "accepted_by_user": True}}
                             for event in events),
        )
        if rows:
            connection.execute(insert(fidelity_events), rows)
        record_normative_fidelity_events(
            connection, normative, operation_id=operation_id,
            dataset_id=normative_dataset_id, direction="export",
            events=({**event, "severity": event.get("severity", "warning"),
                     "details": {**event.get("details", {}), "accepted_by_user": True}}
                    for event in events),
        )
    return operation_id


def finish_export_operation(
    *,
    database_url: str,
    operation_id: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> None:
    """Mark a published export successful only after filesystem finalization."""
    profile, active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    metadata = MetaData()
    legacy, normative = _catalog_layout(metadata)
    operations = legacy[9]
    audit_relations = {
        legacy[8].name, legacy[9].name,
        normative.fidelity_event.name, normative.operation.name,
    }
    with _bound_catalog_transaction(
        engine=engine, profile_name=profile.name, active=active,
        audit_relations=audit_relations, phase="finish export operation",
    ) as connection:
        _require_verified_catalog(connection, normative, legacy)
        row = connection.execute(select(operations).where(
            operations.c.operation_id == operation_id
        )).mappings().one()
        if row["direction"] != "export" or row["status"] != "running":
            raise UnsupportedOperationError(
                "Only a running export operation can be finalized."
            )
        connection.execute(update(operations).where(
            operations.c.operation_id == operation_id
        ).values(status="succeeded", completed_at=_now()))
        finish_normative_operation(
            connection, normative, operation_id=operation_id, status="succeeded",
        )


def read_export_operation_state(
    *,
    database_url: str,
    operation_id: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    """Read both export-operation catalogs without changing either one."""
    validate_connection_url(database_url)
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    metadata = MetaData()
    legacy, normative = _catalog_layout(metadata)
    operations = legacy[9]
    with engine.connect() as connection:
        _require_verified_catalog(connection, normative, legacy)
        legacy_row = connection.execute(select(
            operations.c.direction,
            operations.c.status,
        ).where(
            operations.c.operation_id == operation_id
        )).mappings().one_or_none()
        normative_row = connection.execute(select(
            normative.operation.c.operation_kind,
            normative.operation.c.status,
        ).where(
            normative.operation.c.operation_id == operation_id
        )).mappings().one_or_none()

    legacy_state = (
        None if legacy_row is None else {
            "direction": legacy_row["direction"],
            "status": legacy_row["status"],
        }
    )
    normative_state = (
        None if normative_row is None else {
            "operation_kind": normative_row["operation_kind"],
            "status": normative_row["status"],
        }
    )
    if (
        legacy_state == {"direction": "export", "status": "succeeded"}
        and normative_state == {
            "operation_kind": "export", "status": "succeeded",
        }
    ):
        classification = "succeeded"
    elif (
        legacy_state == {"direction": "export", "status": "running"}
        and normative_state == {
            "operation_kind": "export", "status": "started",
        }
    ):
        classification = "running"
    else:
        classification = "ambiguous"
    return {
        "operation_id": operation_id,
        "legacy": legacy_state,
        "normative": normative_state,
        "classification": classification,
    }


def fail_export_operation(
    *,
    database_url: str,
    operation_id: str,
    failure_details: Mapping[str, Any],
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> None:
    """Close one running export after filesystem compensation succeeded."""
    profile, active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    metadata = MetaData()
    legacy, normative = _catalog_layout(metadata)
    fidelity_events, operations = legacy[8:]
    audit_relations = {
        fidelity_events.name, operations.name,
        normative.fidelity_event.name, normative.operation.name,
    }
    with _bound_catalog_transaction(
        engine=engine, profile_name=profile.name, active=active,
        audit_relations=audit_relations, phase="fail export operation",
    ) as connection:
        _require_verified_catalog(connection, normative, legacy)
        row = connection.execute(select(operations).where(
            operations.c.operation_id == operation_id
        )).mappings().one()
        normative_row = connection.execute(select(normative.operation).where(
            normative.operation.c.operation_id == operation_id
        )).mappings().one()
        if (
            row["direction"] != "export" or row["status"] != "running"
            or normative_row["operation_kind"] != "export"
            or normative_row["status"] != "started"
        ):
            raise UnsupportedOperationError(
                "Only matching running export operations can be failed."
            )
        details = json.loads(row["details"] or "{}")
        details["failure"] = dict(failure_details)
        connection.execute(update(operations).where(
            operations.c.operation_id == operation_id
        ).values(
            status="failed", completed_at=_now(),
            details=json.dumps(details, sort_keys=True),
        ))
        finish_normative_operation(
            connection, normative, operation_id=operation_id, status="failed",
        )
        event = {
            "code": "export_failed",
            "detail": "Export publication or finalization failed after audit start.",
            "severity": "error",
            "source_item": row["destination"],
            "details": dict(failure_details),
        }
        ordinals = connection.execute(select(fidelity_events.c.ordinal).where(
            fidelity_events.c.operation_id == operation_id
        )).scalars().all()
        event_row = _event_rows(
            operation_id=operation_id, dataset_id=row["dataset_id"],
            direction="export", fidelity_events=(event,),
        )[0]
        event_row["ordinal"] = max(ordinals, default=0) + 1
        connection.execute(insert(fidelity_events).values(**event_row))
        normative_dataset_id = normative_dataset_id_for_name(
            connection, normative, row["dataset_id"],
        )
        record_normative_fidelity_events(
            connection, normative, operation_id=operation_id,
            dataset_id=normative_dataset_id, direction="export", events=(event,),
        )


def record_export_backup_retained(
    *, database_url: str, operation_id: str, destination: str, backup: str,
    cleanup_error: Exception,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> None:
    """Append a warning without rewriting a successfully finalized export."""
    profile, active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    metadata = MetaData()
    legacy, normative = _catalog_layout(metadata)
    fidelity_events, operations = legacy[8:]
    audit_relations = {
        fidelity_events.name, operations.name,
        normative.fidelity_event.name, normative.operation.name,
    }
    with _bound_catalog_transaction(
        engine=engine, profile_name=profile.name, active=active,
        audit_relations=audit_relations, phase="record retained export backup",
    ) as connection:
        _require_verified_catalog(connection, normative, legacy)
        row = connection.execute(select(operations).where(
            operations.c.operation_id == operation_id
        )).mappings().one()
        normative_row = connection.execute(select(normative.operation).where(
            normative.operation.c.operation_id == operation_id
        )).mappings().one()
        if (
            row["direction"] != "export" or row["status"] != "succeeded"
            or normative_row["operation_kind"] != "export"
            or normative_row["status"] != "succeeded"
        ):
            raise UnsupportedOperationError(
                "A retained backup warning requires a matching succeeded export."
            )
        details = json.loads(row["details"] or "{}")
        details["backup_retained"] = {
            "destination": destination, "durable_backup": backup,
            "cleanup_error_type": type(cleanup_error).__name__,
        }
        connection.execute(update(operations).where(
            operations.c.operation_id == operation_id
        ).values(details=json.dumps(details, sort_keys=True)))
        event = {
            "code": "backup_retained",
            "detail": "A successful export retained its durable prior-file backup.",
            "severity": "warning",
            "source_item": destination,
            "details": {
                "durable_backup": backup,
                "cleanup_error_type": type(cleanup_error).__name__,
            },
        }
        ordinals = connection.execute(select(fidelity_events.c.ordinal).where(
            fidelity_events.c.operation_id == operation_id
        )).scalars().all()
        event_row = _event_rows(
            operation_id=operation_id, dataset_id=row["dataset_id"],
            direction="export", fidelity_events=(event,),
        )[0]
        event_row["ordinal"] = max(ordinals, default=0) + 1
        connection.execute(insert(fidelity_events).values(**event_row))
        normative_dataset_id = normative_dataset_id_for_name(
            connection, normative, row["dataset_id"],
        )
        record_normative_fidelity_events(
            connection, normative, operation_id=operation_id,
            dataset_id=normative_dataset_id, direction="export", events=(event,),
        )


def validate_wide_dataset(
    *,
    database_url: str,
    dataset_id: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    profile, _active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    dataset, variables, rows = read_wide_dataset(
        database_url=database_url,
        dataset_id=dataset_id,
        dolt_conformance_source=dolt_conformance_source,
    )
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
        elif not _valid_wide_string_type(profile, column.type) or column.nullable:
            raise ValueError(f"String variable {item['source_name']!r} must be a non-null text column.")
    if [row["__case_ordinal"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("Case ordinals are not contiguous source order.")
    return {"dataset_id": dataset["dataset_id"], "valid": True, "case_count": len(rows), "variable_count": len(variables)}

