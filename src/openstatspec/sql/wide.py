"""SQLite reference SQL profile for the strict OpenStatSpec wide-table contract."""

import hashlib
import json
import math
import re
import sys
from datetime import UTC, datetime
from contextlib import contextmanager
from uuid import uuid4
from decimal import Decimal
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import (
    BigInteger, Column, Float, MetaData, Table, Text, create_engine, insert,
    inspect, or_, select, update,
)
from sqlalchemy.dialects import mysql, postgresql, sqlite
from ..core import UnsupportedOperationError
from .capabilities import active_connection, dolt_operational_write_enabled, effective_profile
from .profiles import preflight, statement_payload_bytes, validate_connection_url
from .normative import (
    CATALOG_CONTRACT_ID,
    CATALOG_SCHEMA_VERSION,
    catalog as normative_catalog,
    create as create_normative_catalog,
    delete_dataset_representation as delete_normative_dataset,
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


def _catalog_error(code: str, detail: str, **details: Any) -> CatalogPreflightError:
    return CatalogPreflightError(code, detail, details=details)


def string_type(profile: Any) -> Text:
    """Use Dolt's tested LONGTEXT storage without changing MySQL/MariaDB DDL."""
    return mysql.LONGTEXT() if profile.name == "dolt" else Text()


def _wide_column_type(profile: Any, storage_kind: str) -> Any:
    """Return the strict physical type for one wide-table source column."""
    return binary64_type() if storage_kind == "numeric" else string_type(profile)


def _valid_wide_string_type(profile: Any, column_type: Any) -> bool:
    """Validate the profile-specific reflected string storage type."""
    if profile.name == "dolt":
        return isinstance(column_type, mysql.LONGTEXT)
    return isinstance(column_type, Text)


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





def _record_failed_preflight(
    *, engine: Any, normative: Any, operation_id: str, source_name: str,
    source_format: str, variable_count: int, profile_name: str,
    error: Exception,
) -> None:
    """Persist a failed preflight in the normative audit catalog."""
    with engine.begin() as connection:
        _verify_normative_catalog(connection, normative)
        failed_at = datetime.now(UTC).replace(tzinfo=None)
        record_normative_operation(
            connection, normative, operation_id=operation_id,
            operation_kind="import", status="failed", source_format=source_format,
            started_at=failed_at, completed_at=failed_at,
        )
        record_normative_fidelity_events(
            connection, normative, operation_id=operation_id, dataset_id=None,
            direction="import", events=({
                "code": str(getattr(
                    error, "code", "target_capability_exceeded")).replace("-", "_"),
                "detail": str(error), "severity": "error",
                "source_item": source_name,
                "details": {
                    "variable_count": variable_count,
                    "profile": profile_name,
                    **getattr(error, "details", {}),
                },
            },),
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



def physical_name(source_name: str, used: set[str]) -> str:
    stem = _IDENTIFIER.sub("_", source_name).strip("_").lower() or "variable"
    stem = stem[:54]
    candidate, suffix = stem, 2
    while candidate.lower() in used or candidate.startswith("__"):
        candidate = f"{stem[:50]}_{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _dolt_evidence_block(
    rows: Iterable[Mapping[str, Any]], *, expected_keys: tuple[str, ...],
) -> dict[str, Any]:
    normalized = [
        {key: dict(row)[key] for key in expected_keys}
        for row in rows
    ]
    normalized.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
    return {"rows": normalized, "sha256": _canonical_sha256(normalized)}


def _capture_dolt_state(
    connection: Any, *, profile_name: str, audit_relations: set[str],
) -> dict[str, Any] | None:
    """Capture the Dolt working-set identity without mutating it."""
    del audit_relations
    if profile_name != "dolt":
        return None
    identity = connection.exec_driver_sql(
        "SELECT DATABASE() AS database_name, ACTIVE_BRANCH() AS active_branch, "
        "DOLT_HASHOF('HEAD') AS head_hash"
    ).mappings().one()
    state = {
        "database": str(identity["database_name"]).strip(),
        "active_branch": str(identity["active_branch"]).strip(),
        "head": str(identity["head_hash"]).strip(),
        "status": _dolt_evidence_block(
            connection.exec_driver_sql(
                "SELECT table_name, staged, status FROM dolt_status "
                "ORDER BY table_name, staged, status"
            ).mappings().all(),
            expected_keys=("table_name", "staged", "status"),
        ),
        "diff_summaries": {},
    }
    for label, left, right in (
        ("head_to_working", "HEAD", "WORKING"),
        ("head_to_staged", "HEAD", "STAGED"),
        ("staged_to_working", "STAGED", "WORKING"),
    ):
        state["diff_summaries"][label] = _dolt_evidence_block(
            connection.exec_driver_sql(
                "SELECT from_table_name, to_table_name, diff_type, "
                "data_change, schema_change "
                f"FROM DOLT_DIFF_SUMMARY('{left}', '{right}') "
                "ORDER BY from_table_name, to_table_name, diff_type"
            ).mappings().all(),
            expected_keys=(
                "from_table_name", "to_table_name", "diff_type",
                "data_change", "schema_change",
            ),
        )
    state["snapshot_sha256"] = _canonical_sha256(state)
    return state


def _require_dolt_working_set_binding(
    snapshot: Mapping[str, Any] | None,
    active: Mapping[str, Any],
    *,
    phase: str,
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
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    phase: str,
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
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "applicable": before is not None or after is not None,
        "before": before,
        "after": after,
    }


@contextmanager
def _bound_catalog_transaction(
    *, engine: Any, profile_name: str, active: Mapping[str, Any],
    audit_relations: set[str], phase: str,
):
    """Bind a catalog mutation to one Dolt database, branch, and HEAD."""
    with engine.connect() as connection:
        before = _capture_dolt_state(
            connection, profile_name=profile_name,
            audit_relations=audit_relations,
        )
        _require_dolt_working_set_binding(before, active, phase=f"{phase} preflight")
        if profile_name != "sqlite":
            connection.rollback()
        try:
            with connection.begin():
                yield connection
        except Exception:
            after = _capture_dolt_state(
                connection, profile_name=profile_name,
                audit_relations=audit_relations,
            )
            _dolt_failure_boundary_evidence(before, after)
            raise
        after = _capture_dolt_state(
            connection, profile_name=profile_name,
            audit_relations=audit_relations,
        )
        _require_dolt_success_identity(before, after, phase=phase)


def dolt_state_snapshot(
    *, database_url: str, dolt_conformance_source: Any | None = None,
) -> dict[str, Any]:
    """Return read-only branch, HEAD, status, and diff evidence for Dolt."""
    validate_connection_url(database_url)
    active = active_connection(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    if active["profile"] != "dolt":
        raise UnsupportedOperationError(
            "dolt_state_snapshot requires a positively identified Dolt connection."
        )
    engine = create_engine(database_url)
    with engine.connect() as connection:
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
            summaries[label] = _dolt_evidence_block(
                connection.exec_driver_sql(
                    "SELECT from_table_name, to_table_name, diff_type, "
                    "data_change, schema_change "
                    f"FROM DOLT_DIFF_SUMMARY('{left}', '{right}') "
                    "ORDER BY from_table_name, to_table_name, diff_type"
                ).mappings().all(),
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
            expected_keys=("table_name", "staged", "status"),
        )
    state = {
        "database": str(identity["database_name"]).strip(),
        "active_branch": str(identity["active_branch"]).strip(),
        "head": str(identity["head_hash"]).strip(),
        "status": status,
        "diff_summaries": summaries,
    }
    binding = active.get("working_set_binding")
    if (
        not isinstance(binding, Mapping)
        or state["database"] != binding.get("database")
        or state["active_branch"] != binding.get("active_branch")
    ):
        raise UnsupportedOperationError(
            "Dolt database/branch working-set binding mismatch during read-only state capture."
        )
    state["snapshot_sha256"] = _canonical_sha256(state)
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
    *, database_url: str, dolt_conformance_source: Any | None = None,
) -> dict[str, Any]:
    """Initialize or verify the singular normative OpenStatSpec catalog."""
    validate_connection_url(database_url)
    profile, _active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    normative = normative_catalog(MetaData())
    with engine.begin() as connection:
        inspector = inspect(connection)
        views = set(inspector.get_view_names())
        if views and not inspector.has_table(normative.catalog_identity.name):
            raise UnsupportedOperationError(
                "The selected database catalog is foreign; initialization is not permitted."
            )
        try:
            create_normative_catalog(connection, normative)
        except RuntimeError as error:
            raise UnsupportedOperationError(
                "The selected database catalog is foreign or incompatible; "
                "initialization is not permitted."
            ) from error
        require_verified_catalog(connection)
    return {"profile": profile.name, "catalog": "verified"}


def _bounded_batches(
    rows: Iterable[Mapping[str, Any]], variables: Iterable[Mapping[str, Any]],
    maximum_statement_bytes: int | None,
) -> Iterable[list[Mapping[str, Any]]]:
    """Partition rows without exceeding the profile's statement payload budget."""
    batch: list[Mapping[str, Any]] = []
    size = 0
    for row in rows:
        row_size = statement_payload_bytes(row, variables)
        if (
            batch and maximum_statement_bytes is not None
            and size + row_size > maximum_statement_bytes
        ):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += row_size
    if batch:
        yield batch


def _canonicalize_database_numeric_rows(
    rows: Iterable[Mapping[str, Any]],
    variables: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize finite DB-driver Decimal wrappers to binary64 values."""
    numeric_names = {
        str(variable["physical_name"])
        for variable in variables
        if variable.get("storage_kind") == "numeric"
    }
    normalized = []
    for source in rows:
        row = dict(source)
        for name in numeric_names:
            if isinstance(row.get(name), Decimal) and row[name].is_finite():
                row[name] = float(row[name])
        normalized.append(row)
    return normalized


def data_table_name(dataset_id: str) -> str:
    stem = _IDENTIFIER.sub("_", dataset_id).strip("_").lower() or "dataset"
    return f"data_{stem[:48]}"


def create_wide_dataset(
    *, database_url: str, dataset_id: str, source_name: str, source_format: str,
    rows: Iterable[Mapping[str, Any]], variables: list[dict[str, Any]],
    file_label: str = "", documents: str = "[]", file_attributes: str = "{}",
    file_attribute_values: Mapping[str, Any] | None = None,
    variable_attribute_values: Mapping[str, Mapping[str, Any]] | None = None,
    case_weight_variable: str | None = None, multiple_response_sets: str = "{}",
    source_encoding: str | None = None,
    source_table_name: str | None = None, source_sha256: str = "",
    source_created_at: str | None = None, source_modified_at: str | None = None,
    imported_at: str | None = None,
    source_extensions: Mapping[str, Any] | None = None,
    fidelity_events: Iterable[Mapping[str, Any]] = (),
    operation_details: Mapping[str, Any] | None = None,
    dolt_conformance_source: Any | None = None,
) -> dict[str, Any]:
    del source_table_name, source_created_at, source_modified_at, operation_details
    validate_connection_url(database_url)
    profile, _active_connection = (
        effective_profile(database_url)
        if dolt_conformance_source is None
        else effective_profile(
            database_url, dolt_conformance_source=dolt_conformance_source,
        )
    )
    engine = create_engine(database_url)
    normative = normative_catalog(MetaData())
    with engine.begin() as catalog_connection:
        catalog_existed = inspect(catalog_connection).has_table(
            normative.catalog_identity.name
        )
        create_normative_catalog(catalog_connection, normative)
        if catalog_existed:
            require_verified_catalog(catalog_connection)
        else:
            _verify_normative_catalog(catalog_connection, normative)
    operation_id = str(uuid4())
    normative_dataset_id = str(uuid4())
    fidelity_events = tuple(fidelity_events)
    source_rows = _canonicalize_database_numeric_rows(rows, variables)
    try:
        preflight(profile, variables, rows=source_rows)
        validate_spss_catalog(
            variables,
            case_weight_variable=case_weight_variable,
            multiple_response_sets=multiple_response_sets,
        )
    except Exception as error:
        _record_failed_preflight(
            engine=engine, normative=normative, operation_id=operation_id,
            source_name=source_name, source_format=source_format,
            variable_count=len(variables), profile_name=profile.name, error=error,
        )
        raise

    data_table = Table(
        data_table_name(dataset_id), MetaData(),
        Column("__case_ordinal", BigInteger, primary_key=True, nullable=False),
        *(
            Column(
                item["physical_name"],
                binary64_type() if item["storage_kind"] == "numeric"
                else string_type(profile),
                nullable=item["storage_kind"] == "numeric",
            )
            for item in variables
        ),
    )
    materialized = [
        {"__case_ordinal": ordinal, **row}
        for ordinal, row in enumerate(source_rows, start=1)
    ]
    docs_rows = document_rows(normative_dataset_id, documents)
    labels_rows = value_label_rows(normative_dataset_id, variables)
    missing_rows = missing_rule_rows(normative_dataset_id, variables)
    attributes_rows = attribute_rows(
        normative_dataset_id, variables,
        file_attributes=file_attribute_values,
        variable_attributes=variable_attribute_values,
    )
    mrset_rows = multiple_response_set_rows(
        normative_dataset_id, multiple_response_sets,
    )
    namespace_owned = False
    data_table_created = False
    try:
        with engine.begin() as setup:
            _verify_normative_catalog(setup, normative)
        namespace_owned = True
        with engine.begin() as connection:
            if connection.execute(
                select(normative.dataset.c.dataset_id).where(or_(
                    normative.dataset.c.dataset_name == dataset_id,
                    normative.dataset.c.dataset_id == dataset_id,
                    normative.dataset.c.dataset_name == normative_dataset_id,
                ))
            ).first():
                raise ValueError(
                    f"Dataset {dataset_id!r} already exists; imports never overwrite a dataset."
                )
            if connection.execute(
                select(normative.dataset.c.dataset_id).where(
                    normative.dataset.c.physical_table_name == data_table.name,
                    normative.dataset.c.physical_table_schema.is_(None),
                )
            ).first():
                raise ValueError(
                    f"Dataset ID {dataset_id!r} collides with an existing physical "
                    "data-table name; import was not started."
                )
            if inspect(connection).has_table(data_table.name):
                raise ValueError(
                    f"Physical data-table name {data_table.name!r} is already occupied."
                )
            record_normative_operation(
                connection, normative, operation_id=operation_id,
                operation_kind="import", status="started",
                source_format=source_format,
            )
            data_table.create(connection)
            data_table_created = True
            store_normative_dataset(
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
                dataset_id=normative_dataset_id,
            )
            record_normative_fidelity_events(
                connection, normative, operation_id=operation_id,
                dataset_id=normative_dataset_id, direction="import",
                events=fidelity_events,
            )
            if materialized:
                connection.execute(insert(data_table), materialized)
            finish_normative_operation(
                connection, normative, operation_id=operation_id,
                status="succeeded",
            )
    except Exception as error:
        if namespace_owned:
            try:
                with engine.begin() as cleanup:
                    create_normative_catalog(cleanup, normative)
                    if cleanup.execute(
                        select(normative.dataset.c.dataset_id).where(
                            normative.dataset.c.dataset_id == normative_dataset_id
                        )
                    ).first():
                        delete_normative_dataset(
                            cleanup, normative, normative_dataset_id,
                        )
                    if (
                        data_table_created
                        and inspect(cleanup).has_table(data_table.name)
                    ):
                        data_table.drop(cleanup, checkfirst=True)
                    operation_exists = cleanup.execute(
                        select(normative.operation.c.operation_id).where(
                            normative.operation.c.operation_id == operation_id
                        )
                    ).first()
                    if operation_exists:
                        finish_normative_operation(
                            cleanup, normative, operation_id=operation_id,
                            status="failed",
                        )
                    else:
                        failed_at = datetime.now(UTC).replace(tzinfo=None)
                        record_normative_operation(
                            cleanup, normative, operation_id=operation_id,
                            operation_kind="import", status="failed",
                            source_format=source_format,
                            started_at=failed_at, completed_at=failed_at,
                        )
                    record_normative_fidelity_events(
                        cleanup, normative, operation_id=operation_id,
                        dataset_id=None, direction="import", events=({
                            "code": "import_failed",
                            "detail": str(error), "severity": "error",
                            "source_item": source_name,
                            "details": {"reason": "post_preflight"},
                        },),
                    )
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"OpenStatSpec compensating cleanup failed: {cleanup_error}"
                ) from cleanup_error
        raise
    return {
        "dataset_id": normative_dataset_id,
        "dataset_name": dataset_id,
        "data_table": data_table.name,
        "case_count": len(materialized),
        "operation_id": operation_id,
    }




def read_wide_dataset(
    *, database_url: str, dataset_id: str, profile: Any | None = None,
    dolt_conformance_source: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Read an export descriptor from the normative catalog without mutation."""
    if profile is None:
        profile, _active = effective_profile(
            database_url, dolt_conformance_source=dolt_conformance_source,
        )
    engine = create_engine(database_url)
    normative = normative_catalog(MetaData())
    with engine.connect() as connection:
        _verify_normative_catalog(connection, normative)
        dataset_row = _resolve_normative_dataset(connection, normative, dataset_id)
        core_id = str(dataset_row["dataset_id"])
        data_table = Table(
            str(dataset_row["physical_table_name"]), MetaData(),
            schema=dataset_row["physical_table_schema"],
            autoload_with=connection,
        )
        source_variables = connection.execute(
            select(normative.variable)
            .where(normative.variable.c.dataset_id == core_id)
            .order_by(normative.variable.c.source_ordinal)
        ).mappings().all()
        variables = [_export_variable(row) for row in source_variables]
        variables_by_id = {
            str(row["variable_id"]): variable
            for row, variable in zip(source_variables, variables, strict=True)
        }
        variable_ids = tuple(variables_by_id)
        rows = [dict(row) for row in connection.execute(
            select(data_table).order_by(data_table.c.__case_ordinal)
        ).mappings()]
        documents = connection.execute(
            select(normative.document)
            .where(normative.document.c.dataset_id == core_id)
            .order_by(normative.document.c.source_ordinal)
        ).mappings().all()
        dataset_attributes = connection.execute(
            select(normative.dataset_attribute)
            .where(normative.dataset_attribute.c.dataset_id == core_id)
            .order_by(
                normative.dataset_attribute.c.attribute_name,
                normative.dataset_attribute.c.array_ordinal,
            )
        ).mappings().all()
        variable_attributes = connection.execute(
            select(normative.variable_attribute)
            .where(normative.variable_attribute.c.variable_id.in_(variable_ids))
            .order_by(
                normative.variable_attribute.c.variable_id,
                normative.variable_attribute.c.attribute_name,
                normative.variable_attribute.c.array_ordinal,
            )
        ).mappings().all()
        labels = connection.execute(
            select(
                normative.variable_value_label_set.c.variable_id,
                normative.value_label,
            )
            .join(
                normative.value_label,
                normative.value_label.c.value_label_set_id
                == normative.variable_value_label_set.c.value_label_set_id,
            )
            .where(
                normative.variable_value_label_set.c.variable_id.in_(variable_ids)
            )
            .order_by(
                normative.variable_value_label_set.c.variable_id,
                normative.value_label.c.ordinal,
            )
        ).mappings().all()
        missing_rules = connection.execute(
            select(normative.missing_rule)
            .where(normative.missing_rule.c.variable_id.in_(variable_ids))
            .order_by(
                normative.missing_rule.c.variable_id,
                normative.missing_rule.c.ordinal,
            )
        ).mappings().all()
        variable_sets = connection.execute(
            select(normative.variable_set)
            .where(normative.variable_set.c.dataset_id == core_id)
            .order_by(normative.variable_set.c.source_ordinal)
        ).mappings().all()
        variable_set_ids = tuple(
            str(row["variable_set_id"]) for row in variable_sets
        )
        variable_set_members = connection.execute(
            select(normative.variable_set_member)
            .where(normative.variable_set_member.c.variable_set_id.in_(variable_set_ids))
            .order_by(
                normative.variable_set_member.c.variable_set_id,
                normative.variable_set_member.c.source_ordinal,
            )
        ).mappings().all()
        response_sets = connection.execute(
            select(normative.multiple_response_set)
            .where(normative.multiple_response_set.c.dataset_id == core_id)
            .order_by(normative.multiple_response_set.c.source_ordinal)
        ).mappings().all()
        response_set_ids = tuple(
            str(row["multiple_response_set_id"]) for row in response_sets
        )
        response_members = connection.execute(
            select(normative.multiple_response_member)
            .where(
                normative.multiple_response_member.c.multiple_response_set_id.in_(
                    response_set_ids
                )
            )
            .order_by(
                normative.multiple_response_member.c.multiple_response_set_id,
                normative.multiple_response_member.c.source_ordinal,
            )
        ).mappings().all()
        weight_id = connection.execute(
            select(normative.dataset_weight_variable.c.variable_id).where(
                normative.dataset_weight_variable.c.dataset_id == core_id
            )
        ).scalar_one_or_none()

    dataset = {
        "dataset_id": core_id,
        "data_table": str(dataset_row["physical_table_name"]),
        "physical_table_schema": dataset_row["physical_table_schema"],
        "source_format": dataset_row["source_format"],
        "source_encoding": dataset_row["source_encoding"],
        "case_count": int(dataset_row["source_case_count"]),
        "file_label": dataset_row["dataset_label"] or "",
        "documents": json.dumps(
            [row["document_text"] for row in documents], ensure_ascii=False,
        ),
        "file_attributes": json.dumps(
            _collapse_attributes(dataset_attributes), ensure_ascii=False,
        ),
        "case_weight_variable": (
            variables_by_id[str(weight_id)]["source_name"]
            if weight_id is not None else None
        ),
    }
    for row in labels:
        variable = variables_by_id[str(row["variable_id"])]
        values = json.loads(variable["value_labels"])
        code = (
            row["numeric_code"]
            if row["code_kind"] == "numeric" else row["string_code"]
        )
        values[str(code)] = row["label"]
        variable["value_labels"] = json.dumps(values, ensure_ascii=False)
    for row in missing_rules:
        variable = variables_by_id[str(row["variable_id"])]
        rules = json.loads(variable["missing_ranges"])
        rules.append(_export_missing_rule(row))
        variable["missing_ranges"] = json.dumps(rules, ensure_ascii=False)
    grouped_attributes: dict[str, list[Mapping[str, Any]]] = {}
    for row in variable_attributes:
        grouped_attributes.setdefault(str(row["variable_id"]), []).append(row)
    for variable_id, attribute_rows in grouped_attributes.items():
        variables_by_id[variable_id]["attributes"] = json.dumps(
            _collapse_attributes(attribute_rows), ensure_ascii=False,
        )
    names_by_id = {
        variable_id: variable["source_name"]
        for variable_id, variable in variables_by_id.items()
    }
    members_by_variable_set: dict[str, list[str]] = {}
    for row in variable_set_members:
        members_by_variable_set.setdefault(str(row["variable_set_id"]), []).append(
            names_by_id[str(row["variable_id"])]
        )
    dataset["source_extensions"] = {
        "spss.variable_sets": {
            str(row["set_name"]): members_by_variable_set.get(
                str(row["variable_set_id"]), []
            )
            for row in variable_sets
        }
    } if variable_sets else {}
    members_by_response_set: dict[str, list[str]] = {}
    for row in response_members:
        set_id = str(row["multiple_response_set_id"])
        variable_id = str(row["variable_id"])
        if variable_id not in names_by_id:
            raise _catalog_error(
                "multiple-response-member-not-found",
                "A multiple-response set references an unknown variable.",
                multiple_response_set_id=set_id,
                variable_id=variable_id,
            )
        members_by_response_set.setdefault(set_id, []).append(
            names_by_id[variable_id]
        )
    dataset["multiple_response_sets"] = json.dumps({
        str(row["set_name"]): _export_response_set(
            row,
            members_by_response_set.get(
                str(row["multiple_response_set_id"]), []
            ),
        )
        for row in response_sets
    }, ensure_ascii=False)
    return dataset, variables, rows


def _verify_normative_catalog(connection: Any, tables: Any) -> None:
    if not inspect(connection).has_table(tables.catalog_identity.name):
        raise UnsupportedOperationError("The OpenStatSpec catalog is absent.")
    identities = connection.execute(select(tables.catalog_identity)).mappings().all()
    if len(identities) != 1 or (
        identities[0]["catalog_identity_key"] != 1
        or identities[0]["contract_id"] != CATALOG_CONTRACT_ID
        or identities[0]["schema_version"] != CATALOG_SCHEMA_VERSION
    ):

        raise RuntimeError("The core OpenStatSpec catalog identity is incompatible.")

def require_verified_catalog(
    connection: Any,
    *,
    allowed_migrations: Mapping[str, set[str]] | None = None,
) -> None:
    """Verify that the namespace contains only the normative catalog and owned data."""
    normative = normative_catalog(MetaData())
    _verify_normative_catalog(connection, normative)
    expected = {table.name for table in normative.all()}
    expected.update(
        str(name) for name in connection.execute(
            select(normative.dataset.c.physical_table_name)
        ).scalars()
    )
    expected.update((allowed_migrations or {}).keys())
    expected.add("transformation_apply")
    inspector = inspect(connection)
    unknown = set(inspector.get_table_names()) - expected
    views = set(inspector.get_view_names())
    if unknown or views:
        relations = ", ".join(sorted(unknown | views))
        raise UnsupportedOperationError(
            "The selected database catalog contains foreign or obsolete "
            f"relations: {relations}. Remove them manually before continuing."
        )


def _resolve_normative_dataset(
    connection: Any, tables: Any, identifier: str,
) -> Mapping[str, Any]:
    row = connection.execute(
        select(tables.dataset).where(tables.dataset.c.dataset_id == identifier)
    ).mappings().one_or_none()
    if row is not None:
        return row
    return connection.execute(
        select(tables.dataset).where(tables.dataset.c.dataset_name == identifier)
    ).mappings().one()


def _format_json(family: Any, width: Any, decimals: Any) -> str | None:
    if family is None:
        return None
    try:
        numeric_family = int(family)
    except (TypeError, ValueError):
        suffix = f".{int(decimals or 0)}" if decimals else ""
        return f"{family}{int(width)}{suffix}"
    return json.dumps([numeric_family, width, decimals])


def _export_variable(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ordinal": int(row["source_ordinal"]),
        "source_name": row["source_name"],
        "physical_name": row["physical_name"],
        "storage_kind": row["storage_kind"],
        "string_width": row["declared_string_width"],
        "label": row["variable_label"] or "",
        "print_format": _format_json(
            row["print_format_family"], row["print_format_width"],
            row["print_format_decimals"],
        ),
        "write_format": _format_json(
            row["write_format_family"], row["write_format_width"],
            row["write_format_decimals"],
        ),
        "measure": row["measurement_level"],
        "role": row["variable_role"],
        "alignment": row["display_alignment"],
        "display_width": row["display_width"],
        "attributes": "{}",
        "compat_name": None,
        "value_labels": "{}",
        "missing_ranges": "[]",
    }


def _collapse_attributes(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["attribute_name"]), []).append(
            str(row["attribute_value"])
        )
    return {
        name: values[0] if len(values) == 1 else values
        for name, values in grouped.items()
    }


def _export_missing_rule(row: Mapping[str, Any]) -> Any:
    if row["rule_kind"] == "discrete":
        return (
            row["numeric_value"]
            if row["code_kind"] == "numeric" else row["string_value"]
        )
    lower = (
        -sys.float_info.max
        if row["lower_special"] == "LOWEST" else row["numeric_lower"]
    )
    upper = (
        sys.float_info.max
        if row["upper_special"] == "HIGHEST" else row["numeric_upper"]
    )
    return {"lo": lower, "hi": upper}


def _export_response_set(
    row: Mapping[str, Any], members: list[str],
) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "variable_list": members,
        "is_dichotomy": str(row["set_kind"]).upper() == "MD",
        "use_category_labels": row["category_label_behavior"] == "counted_values",
        "use_first_var_label": row["label_source"] == "variable_label",
    }
    if row["set_label"] is not None:
        definition["label"] = row["set_label"]
    if row["counted_value_kind"] == "numeric":
        definition["counted_value"] = row["counted_numeric_value"]
    elif row["counted_value_kind"] == "string":
        definition["counted_value"] = row["counted_string_value"]
    return definition


def read_fidelity_events(
    *, database_url: str, dataset_id: str,
    dolt_conformance_source: Any | None = None,
) -> tuple[dict[str, Any], ...]:
    """Read fidelity diagnostics from the normative catalog."""
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    normative = normative_catalog(MetaData())
    with engine.connect() as connection:
        _verify_normative_catalog(connection, normative)
        dataset = _resolve_normative_dataset(connection, normative, dataset_id)
        events = connection.execute(
            select(normative.fidelity_event)
            .where(normative.fidelity_event.c.dataset_id == dataset["dataset_id"])
            .order_by(normative.fidelity_event.c.event_code)
        ).mappings().all()
    result = []
    for item in events:
        details = json.loads(item["detail_json"] or "{}")
        result.append({
            "code": item["event_code"],
            "detail": details.pop("message", ""),
            "details": details,
        })
    return tuple(result)


def record_export_operation(
    *, database_url: str, dataset_id: str, destination: str,
    allowed_fidelity_events: Iterable[Mapping[str, Any]],
    operation_details: Mapping[str, Any] | None = None,
    terminal: bool = True,
    dolt_conformance_source: Any | None = None,
) -> str:
    """Persist a completed export only in the normative audit catalog."""
    del destination, operation_details
    engine = create_engine(database_url)
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    normative = normative_catalog(MetaData())
    operation_id = str(uuid4())
    events = tuple(allowed_fidelity_events)
    with engine.begin() as connection:
        _verify_normative_catalog(connection, normative)
        dataset = _resolve_normative_dataset(connection, normative, dataset_id)
        completed_at = datetime.now(UTC).replace(tzinfo=None)
        status = "succeeded" if terminal else "started"
        record_normative_operation(
            connection, normative, operation_id=operation_id,
            operation_kind="export", status=status, source_format=None,
            started_at=completed_at, completed_at=completed_at if terminal else None,
        )
        record_normative_fidelity_events(
            connection, normative, operation_id=operation_id,
            dataset_id=str(dataset["dataset_id"]), direction="export",
            events=({
                **event,
                "severity": event.get("severity", "warning"),
                "details": {
                    **event.get("details", {}),
                    "accepted_by_user": True,
                },
            } for event in events),
        )
    return operation_id


def _export_operation_row(
    connection: Any, normative: Any, operation_id: str,
) -> Mapping[str, Any]:
    row = connection.execute(
        select(normative.operation).where(
            normative.operation.c.operation_id == operation_id
        )
    ).mappings().one_or_none()
    if row is None or row["operation_kind"] != "export":
        raise UnsupportedOperationError("The export operation does not exist.")
    return row


def finish_export_operation(
    *, database_url: str, operation_id: str,
    dolt_conformance_source: Any | None = None,
) -> None:
    """Mark a started normative export operation as succeeded."""
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    normative = normative_catalog(MetaData())
    with create_engine(database_url).begin() as connection:
        _verify_normative_catalog(connection, normative)
        row = _export_operation_row(connection, normative, operation_id)
        if row["status"] != "started":
            raise UnsupportedOperationError(
                "Only a started export operation can be finalized."
            )
        finish_normative_operation(
            connection, normative, operation_id=operation_id, status="succeeded",
        )


def read_export_operation_state(
    *, database_url: str, operation_id: str,
    dolt_conformance_source: Any | None = None,
) -> dict[str, Any]:
    """Read the singular normative export-operation state."""
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    normative = normative_catalog(MetaData())
    with create_engine(database_url).connect() as connection:
        _verify_normative_catalog(connection, normative)
        row = _export_operation_row(connection, normative, operation_id)
    classification = {
        "started": "running",
        "succeeded": "succeeded",
        "failed": "failed",
    }.get(str(row["status"]), "ambiguous")
    return {
        "operation_id": operation_id,
        "normative": {
            "operation_kind": row["operation_kind"],
            "status": row["status"],
        },
        "classification": classification,
    }


def fail_export_operation(
    *, database_url: str, operation_id: str,
    failure_details: Mapping[str, Any],
    dolt_conformance_source: Any | None = None,
) -> None:
    """Close a started normative export after filesystem compensation."""
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    normative = normative_catalog(MetaData())
    event = {
        "code": "export_failed",
        "detail": "Export publication or finalization failed after audit start.",
        "severity": "error",
        "details": dict(failure_details),
    }
    with create_engine(database_url).begin() as connection:
        _verify_normative_catalog(connection, normative)
        row = _export_operation_row(connection, normative, operation_id)
        if row["status"] != "started":
            raise UnsupportedOperationError(
                "Only a started export operation can be failed."
            )
        finish_normative_operation(
            connection, normative, operation_id=operation_id, status="failed",
        )
        record_normative_fidelity_events(
            connection, normative, operation_id=operation_id,
            dataset_id=None, direction="export", events=(event,),
        )


def record_export_backup_retained(
    *, database_url: str, operation_id: str, destination: str, backup: str,
    cleanup_error: Exception,
    dolt_conformance_source: Any | None = None,
) -> None:
    """Append a warning to a successfully finalized normative export."""
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    normative = normative_catalog(MetaData())
    with create_engine(database_url).begin() as connection:
        _verify_normative_catalog(connection, normative)
        row = _export_operation_row(connection, normative, operation_id)
        if row["status"] != "succeeded":
            raise UnsupportedOperationError(
                "A retained backup warning requires a succeeded export."
            )
        record_normative_fidelity_events(
            connection, normative, operation_id=operation_id,
            dataset_id=None, direction="export", events=({
                "code": "backup_retained",
                "detail": "A successful export retained its durable prior-file backup.",
                "severity": "warning",
                "source_item": destination,
                "details": {
                    "durable_backup": backup,
                    "cleanup_error_type": type(cleanup_error).__name__,
                },
            },),
        )


def record_export_cleanup_failure(
    *, database_url: str, destination: str, original_error: Exception,
    cleanup_error: Exception,
    residual_object_inventory: Mapping[str, Any],
    deterministic_recovery_evidence: Mapping[str, Any],
    operation_id: str | None = None,
    dolt_conformance_source: Any | None = None,
) -> str:
    """Persist terminal cleanup failure in the normative audit catalog."""
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    normative = normative_catalog(MetaData())
    operation_id = operation_id or str(uuid4())
    event = {
        "code": "cleanup_failed",
        "detail": "Export destination recovery failed; out-of-band review is required.",
        "severity": "error",
        "source_item": destination,
        "details": {
            "original_error_type": type(original_error).__name__,
            "cleanup_error_type": type(cleanup_error).__name__,
            "residual_object_inventory": dict(residual_object_inventory),
            "deterministic_recovery_evidence": dict(
                deterministic_recovery_evidence
            ),
        },
    }
    with create_engine(database_url).begin() as connection:
        _verify_normative_catalog(connection, normative)
        row = connection.execute(
            select(normative.operation).where(
                normative.operation.c.operation_id == operation_id
            )
        ).mappings().one_or_none()
        if row is None:
            failed_at = datetime.now(UTC).replace(tzinfo=None)
            record_normative_operation(
                connection, normative, operation_id=operation_id,
                operation_kind="export", status="failed", source_format=None,
                started_at=failed_at, completed_at=failed_at,
            )
        else:
            if row["operation_kind"] != "export" or row["status"] != "started":
                raise UnsupportedOperationError(
                    "Existing export operation cannot transition to cleanup failure."
                )
            finish_normative_operation(
                connection, normative, operation_id=operation_id, status="failed",
            )
        record_normative_fidelity_events(
            connection, normative, operation_id=operation_id,
            dataset_id=None, direction="export", events=(event,),
        )
    return operation_id


def validate_wide_dataset(
    *, database_url: str, dataset_id: str,
    dolt_conformance_source: Any | None = None,
) -> dict[str, Any]:
    profile, _active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    dataset, variables, rows = read_wide_dataset(
        database_url=database_url, dataset_id=dataset_id, profile=profile,
        dolt_conformance_source=dolt_conformance_source,
    )
    preflight(profile, variables, rows=rows)
    validate_spss_catalog(
        variables,
        case_weight_variable=dataset.get("case_weight_variable"),
        multiple_response_sets=dataset.get("multiple_response_sets"),
    )
    if not variables:
        raise ValueError("A conforming dataset needs at least one source variable.")
    expected_columns = {"__case_ordinal", *(item["physical_name"] for item in variables)}
    engine = create_engine(database_url)
    reflected_table = Table(
        dataset["data_table"], MetaData(),
        schema=dataset["physical_table_schema"],
        autoload_with=engine,
    )
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
        elif profile.name == "dolt":
            if not isinstance(column.type, mysql.LONGTEXT) or column.nullable:
                raise ValueError(
                    f"String variable {item['source_name']!r} must be a non-null LONGTEXT column."
                )
        elif not isinstance(column.type, Text) or column.nullable:
            raise ValueError(f"String variable {item['source_name']!r} must be a non-null text column.")
    if [row["__case_ordinal"] for row in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("Case ordinals are not contiguous source order.")
    return {"dataset_id": dataset["dataset_id"], "valid": True, "case_count": len(rows), "variable_count": len(variables)}

