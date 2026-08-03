"""Strict SAV/ZSAV adapter backed exclusively by pyspssio.

The adapter keeps SPSS data in one physical wide table and preserves every
metadata feature exposed by pyspssio plus standard type-6 document records. A source feature that neither path can
observe or write is a durable fidelity event and blocks export unless the
caller explicitly accepts that exact loss.
"""

import hashlib
import os
import json
import math
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from tempfile import mkstemp, TemporaryDirectory
from typing import Any, Callable

try:  # POSIX locks let independent export processes share one publication gate.
    import fcntl
except ImportError:  # pragma: no cover - the supported CI/runtime is POSIX.
    fcntl = None

import pandas as pd
import pyspssio

from .raw_dictionary import (
    RawDictionaryError,
    read_document_lines,
    write_compatible_names,
    write_document_lines,
    write_extended_mrset_labels,
)
from .dictionary import (
    attribute_pairs,
    attribute_values,
    file_attribute_pairs,
    format_string,
    format_tuple,
    format_tuples,
    set_file_attribute_pairs,
    set_format_tuples,
    set_variable_attribute_pairs,
    variable_attribute_pairs,
)
from ..core import (
    UnsupportedOperationError,
    safe_error_identity as _export_error_identity,
)
from ..sql.capabilities import effective_profile
from ..sql.dolt_conformance import DoltConformanceSource
from ..sql.wide import (
    create_wide_dataset,
    fail_export_operation,
    finish_export_operation,
    physical_name,
    read_export_operation_state,
    read_fidelity_events,
    read_wide_dataset,
    record_export_backup_retained,
    record_export_cleanup_failure,
    record_export_operation,
    validate_spss_catalog,
)


class ExportRecoveryError(UnsupportedOperationError):
    """Export publication could not restore the destination's prior state."""

    def __init__(self, code: str, detail: str, *, details: dict[str, Any]) -> None:
        super().__init__(f"OpenStatSpec export recovery failed [{code}]: {detail}")
        self.code = code
        self.details = {"reason": code, **details}


_UTF8_ENCODINGS = {"UTF-8", "UTF8"}
def engine_identity() -> dict[str, str]:
    """Return the exact pinned SPSS engine identity for audit records."""
    return {
        "package": "openstatspec-pyspssio",
        "module": "pyspssio",
        "repository": "TonisOrmisson/pyspssio",
        "pinned_commit": "e069adf33c70bcd9e8e6ee495106479463a84fa2",
        "installed_version": str(pyspssio.__version__),
    }


def _dictionary(source_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read public pyspssio metadata plus the exposed variable-set property."""
    metadata = dict(pyspssio.read_metadata(str(source_path)))
    try:
        with pyspssio.Reader(str(source_path), mode="r") as reader:
            metadata["_var_sets"] = dict(reader.var_sets or {})
            print_formats, write_formats = format_tuples(reader)
            metadata["_print_format_tuples"] = print_formats
            metadata["_write_format_tuples"] = write_formats
            metadata["file_attributes"] = attribute_values(file_attribute_pairs(reader))
            metadata["var_attributes"] = {
                name: attribute_values(variable_attribute_pairs(reader, name))
                for name in reader.var_names
                if variable_attribute_pairs(reader, name)
            }
        try:
            metadata["_documents"] = read_document_lines(
                source_path, encoding=str(metadata.get("encoding") or "UTF-8"),
            )
        except RawDictionaryError as error:
            metadata["_documents"] = None
            metadata["_documents_error"] = _export_error_identity(
                error,
                phase="read_sav_documents",
            )
    except Exception as error:  # The source stays importable, but not silently faithful.
        metadata["_var_sets"] = None
        metadata["_var_sets_error"] = _export_error_identity(
            error,
            phase="read_sav_variable_sets",
        )
    return metadata, _engine_loss_report(metadata)


def _variables(metadata: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    used = {"__case_ordinal"}
    types = dict(metadata.get("var_types") or {})
    labels = dict(metadata.get("var_labels") or {})
    formats = dict(metadata.get("var_formats") or {})
    print_formats = dict(metadata.get("_print_format_tuples") or {})
    write_formats = dict(metadata.get("_write_format_tuples") or {})
    measures = dict(metadata.get("var_measure_levels") or {})
    roles = dict(metadata.get("var_roles") or {})
    alignments = dict(metadata.get("var_alignments") or {})
    displays = dict(metadata.get("var_column_widths") or {})
    value_labels = dict(metadata.get("var_value_labels") or {})
    missing = dict(metadata.get("var_missing_values") or {})
    attributes = dict(metadata.get("var_attributes") or {})
    compat_names = dict(metadata.get("var_compat_names") or {})
    variables: list[dict[str, Any]] = []
    for ordinal, source_name in enumerate(names, start=1):
        width = int(types.get(source_name) or 0)
        kind = "string" if width else "numeric"
        variables.append({
            "ordinal": ordinal,
            "source_name": source_name,
            "physical_name": physical_name(source_name, used),
            "storage_kind": kind,
            "readstat_storage_type": f"pyspssio:{kind}",
            "string_width": width if kind == "string" else None,
            "label": str(labels.get(source_name) or ""),
            "format": format_string(print_formats[source_name]) if source_name in print_formats else formats.get(source_name),
            "print_format": _json(list(print_formats[source_name])) if source_name in print_formats else None,
            "write_format": _json(list(write_formats[source_name])) if source_name in write_formats else None,
            "measure": measures.get(source_name),
            "role": roles.get(source_name),
            "alignment": alignments.get(source_name),
            "display_width": displays.get(source_name),
            "attributes": _json_ordered(attributes.get(source_name, {})),
            "compat_name": compat_names.get(source_name),
            "value_labels": _json(value_labels.get(source_name, {})),
            "missing_ranges": _json(_missing_ranges(missing.get(source_name))),
        })
    return variables


def _missing_ranges(rule: Any) -> list[Any]:
    """Map pyspssio's {values, lo, hi} form to the catalog's ordered rules."""
    if not rule:
        return []
    if not isinstance(rule, dict):
        return list(rule) if isinstance(rule, (list, tuple)) else [rule]
    result: list[Any] = []
    if "lo" in rule or "hi" in rule:
        result.append({"lo": rule.get("lo"), "hi": rule.get("hi")})
    result.extend(rule.get("values") or [])
    return result


def _pyspssio_missing_rules(encoded: str) -> dict[str, Any] | None:
    rules = json.loads(encoded or "[]")
    if not rules:
        return None
    discrete: list[Any] = []
    range_rule: dict[str, Any] | None = None
    for rule in rules:
        if isinstance(rule, dict):
            lower, upper = rule.get("lo"), rule.get("hi")
            if lower == upper:
                discrete.append(lower)
            else:
                if range_rule is not None:
                    raise UnsupportedOperationError(
                        "SPSS permits at most one user-missing range per variable; catalog contains more."
                    )
                range_rule = {"lo": lower, "hi": upper}
        else:
            discrete.append(rule)
    if range_rule is not None:
        if len(discrete) > 1:
            raise UnsupportedOperationError(
                "SPSS permits one discrete user-missing value alongside a range; catalog contains more."
            )
        if discrete:
            range_rule["values"] = discrete
        return range_rule
    if len(discrete) > 3:
        raise UnsupportedOperationError(
            "SPSS permits at most three discrete user-missing values per variable."
        )
    return {"values": discrete}


def _rows(frame: pd.DataFrame, variables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for input_row in frame.to_dict(orient="records"):
        output: dict[str, Any] = {}
        for item in variables:
            value = input_row[item["source_name"]]
            if item["storage_kind"] == "numeric" and pd.isna(value):
                value = None
            elif item["storage_kind"] == "string" and (value is None or pd.isna(value)):
                value = ""
            output[item["physical_name"]] = value
        records.append(output)
    return records


def inspect_sav(source: str | Path) -> dict[str, Any]:
    source_path = Path(source)
    _require_source(source_path)
    metadata, loss_report = _dictionary(source_path)
    names = list(metadata.get("var_names") or [])
    variables = _variables(metadata, names)
    loss_report = _merge_loss_reports(tuple(loss_report.values()))
    return {
        "source_format": source_path.suffix[1:].upper(),
        "engine": engine_identity(),
        "source_name": source_path.name,
        "source_sha256": _sha256(source_path),
        "source_encoding": metadata.get("encoding"),
        "file_label": str(metadata.get("file_label") or ""),
        "documents": list(metadata.get("_documents") or []),
        "file_attributes": dict(metadata.get("file_attributes") or {}),
        "case_weight_variable": metadata.get("case_weight_var") or None,
        "multiple_response_sets": dict(metadata.get("mrsets") or {}),
        "loss_report": loss_report,
        "variable_count": len(names),
        "variables": variables,
    }


def import_sav_dataset(
    *, source: str | Path, database_url: str, dataset_id: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    source_path = Path(source)
    _require_source(source_path)
    frame, metadata = pyspssio.read_sav(
        str(source_path), convert_datetimes=False, include_user_missing=True,
    )
    metadata = dict(metadata)
    dictionary, loss_report = _dictionary(source_path)
    # read_sav and read_metadata expose the same header fields; retain the
    # frame read's values where a future pyspssio version exposes more there.
    metadata = {**metadata, **dictionary}
    variables = _variables(metadata, list(frame.columns))
    loss_report = _merge_loss_reports(tuple(loss_report.values()))
    result = create_wide_dataset(
        database_url=database_url,
        dataset_id=dataset_id,
        dolt_conformance_source=dolt_conformance_source,
        source_name=source_path.name,
        source_format=source_path.suffix[1:].upper(),
        rows=_rows(frame, variables),
        variables=variables,
        file_label=str(metadata.get("file_label") or ""),
        source_encoding=metadata.get("encoding"),
        source_sha256=_sha256(source_path),
        imported_at=datetime.now(UTC).isoformat(),
        documents=_json_ordered(metadata.get("_documents") or []),
        file_attributes=_json_ordered(metadata.get("file_attributes") or {}),
        file_attribute_values=dict(metadata.get("file_attributes") or {}),
        variable_attribute_values=dict(metadata.get("var_attributes") or {}),
        case_weight_variable=metadata.get("case_weight_var") or None,
        multiple_response_sets=_json(metadata.get("mrsets") or {}),
        source_extensions=(
            {"spss.variable_sets": metadata["_var_sets"]}
            if metadata.get("_var_sets") else {}
        ),
        fidelity_events=loss_report,
        operation_details={"engine": engine_identity()},
    )
    return {**result, "loss_report": loss_report}


def _path_reference(path: Path, *, role: str) -> dict[str, str]:
    """Return an opaque path identity without disclosing any path component."""
    absolute_path = os.path.abspath(os.fspath(path))
    return {
        "role": role,
        "path_sha256": hashlib.sha256(
            absolute_path.encode("utf-8")
        ).hexdigest(),
    }


def _path_reference_text(path: Path, *, role: str) -> str:
    """Serialize a redacted path identity for legacy text audit columns."""
    return json.dumps(
        _path_reference(path, role=role),
        sort_keys=True, separators=(",", ":"),
    )


def _path_entry_exists(path: Path) -> bool:
    """Report directory entries without following a possibly dangling symlink."""
    return path.exists() or path.is_symlink()


_DESTINATION_IDENTITY_UNCHECKED = object()


def _destination_identity(path: Path) -> tuple[int, int] | None:
    """Identify the current directory entry without following symlinks."""
    if not _path_entry_exists(path):
        return None
    status = path.lstat()
    return status.st_dev, status.st_ino


def _reserve_export_backup(destination: Path) -> Path:
    descriptor, name = mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.",
        suffix=".previous",
    )
    os.close(descriptor)
    return Path(name)


def _publish_staged_destination(
    *, staged: Path, destination: Path, backup: Path,
    state: dict[str, Any],
) -> None:
    """Publish without overwriting an entry created during export preparation."""
    state.update({
        "had_previous": False,
        "backup_installed": False,
        "published_identity": None,
    })
    if _path_entry_exists(destination):
        os.replace(destination, backup)
        state.update({"had_previous": True, "backup_installed": True})
    else:
        backup.unlink(missing_ok=True)

    # The hard-link claim is atomic and fails if another process publishes the
    # destination after the check above. Staging is on the same filesystem.
    os.link(staged, destination)
    published_identity = _destination_identity(destination)
    if published_identity is None:
        raise FileNotFoundError("The published export destination disappeared.")
    state["published_identity"] = published_identity
    staged.unlink()




@contextmanager
def _export_destination_lock(destination: Path):
    """Serialize export publication and recovery for one destination directory.

    The lock covers the existence observation, backup move, publication, and
    compensating restore. Without it, two exporters could each observe the
    old destination, and the later exporter could move or delete the newer
    export while attempting to restore its own failure.
    """
    if fcntl is None:  # pragma: no cover - see import guard above.
        raise UnsupportedOperationError(
            "SAV export publication requires POSIX advisory file locking."
        )
    descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _serialize_export_publication(
    export: Callable[..., dict[str, Any]],
) -> Callable[..., dict[str, Any]]:
    """Guard a complete export, including any post-publication recovery."""
    @wraps(export)
    def guarded(*args: Any, **kwargs: Any) -> dict[str, Any]:
        destination = kwargs.get("destination")
        if destination is None:
            raise TypeError("export requires a destination keyword argument")
        with _export_destination_lock(Path(destination)):
            return export(*args, **kwargs)
    return guarded


def _restore_export_destination(
    *, destination: Path, backup: Path, had_previous: bool,
    expected_identity: tuple[int, int] | None | object = (
        _DESTINATION_IDENTITY_UNCHECKED
    ),
) -> None:
    if (
        expected_identity is not _DESTINATION_IDENTITY_UNCHECKED
        and _destination_identity(destination) != expected_identity
    ):
        raise FileExistsError(
            "The export destination is no longer owned by this operation."
        )
    if had_previous:
        os.replace(backup, destination)
    else:
        destination.unlink(missing_ok=True)


def _raise_export_cleanup_failed(
    *, original_error: Exception, cleanup_error: Exception,
    phase: str, destination: Path, backup: Path, staged: Path,
    had_previous: bool, database_url: str, operation_id: str | None = None,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> None:
    inventory = {
        "destination": _path_reference(destination, role="destination"),
        "destination_exists": destination.exists(),
        "backup": _path_reference(backup, role="durable_backup"),
        "backup_exists": _path_entry_exists(backup),
        "staged_export": _path_reference(staged, role="staged_export"),
        "staged_export_exists": staged.exists(),
    }
    inventory_sha256 = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    action_id = hashlib.sha256(
        json.dumps(
            {
                "destination_path_sha256": inventory["destination"]["path_sha256"],
                "phase": phase,
            },
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    recovery = {
        "procedure_id": "openstatspec.export-destination-restore.v1",
        "action_id": action_id,
        "targets": {
            "destination": inventory["destination"],
            "durable_backup": inventory["backup"],
            "staged_export": inventory["staged_export"],
        },
        "residual_inventory_sha256": inventory_sha256,
        "cleanup_attempted": True,
        "cleanup_succeeded": False,
        "previous_destination_existed": had_previous,
        "durable_backup_survives_staging_cleanup": _path_entry_exists(backup),
    }
    cleanup_audit_operation_id = None
    cleanup_audit_fault = None
    try:
        cleanup_audit_operation_id = record_export_cleanup_failure(
            database_url=database_url,
            destination=_path_reference_text(
                destination, role="destination",
            ),
            original_error=original_error, cleanup_error=cleanup_error,
            residual_object_inventory=inventory,
            deterministic_recovery_evidence=recovery,
            operation_id=operation_id,
            dolt_conformance_source=dolt_conformance_source,
        )
    except Exception as audit_error:
        cleanup_audit_fault = _export_error_identity(
            audit_error, phase="cleanup_failed_audit",
        )
    exception_recovery = {
        **recovery,
        "cleanup_failed_audit_persisted": cleanup_audit_fault is None,
        "cleanup_failed_audit_operation_id": cleanup_audit_operation_id,
        "terminal_reporting": (
            "catalog_and_exception" if cleanup_audit_fault is None
            else "out_of_band_exception"
        ),
    }
    raise ExportRecoveryError(
        "cleanup_failed",
        "Export failed and the destination's prior state could not be restored.",
        details={
            "subcode": "export_destination_restore_failed",
            "original_cause": _export_error_identity(
                original_error, phase=f"export_{phase}",
            ),
            "cleanup_fault": _export_error_identity(
                cleanup_error, phase="export_destination_restore",
            ),
            "residual_object_inventory": inventory,
            "deterministic_recovery_evidence": exception_recovery,
            "audit_fault": cleanup_audit_fault,
            "success_forbidden": True,
        },
    ) from cleanup_error


def _mark_export_failed_after_restore(
    *, database_url: str, operation_id: str, error: Exception, phase: str,
    destination: Path, backup: Path, had_previous: bool,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> None:
    """Close a running audit after the old destination has been restored."""
    failure = {
        "phase": phase,
        "cause": _export_error_identity(error, phase=f"export_{phase}"),
        "destination": _path_reference(destination, role="destination"),
        "durable_backup": _path_reference(backup, role="durable_backup"),
        "previous_destination_existed": had_previous,
        "destination_restored": True,
    }
    try:
        fail_export_operation(
            database_url=database_url, operation_id=operation_id,
            failure_details=failure,
            dolt_conformance_source=dolt_conformance_source,
        )
    except Exception as audit_error:
        raise ExportRecoveryError(
            "failure_audit_failed",
            "The destination was restored, but the running export audit could not be closed.",
            details={
                "subcode": "export_failure_audit_failed",
                "original_cause": failure["cause"],
                "audit_fault": _export_error_identity(
                    audit_error, phase="export_failure_audit",
                ),
                "residual_object_inventory": {
                    "destination": _path_reference(
                        destination, role="destination",
                    ),
                    "destination_exists": destination.exists(),
                    "backup": _path_reference(
                        backup, role="durable_backup",
                    ),
                    "backup_exists": _path_entry_exists(backup),
                    "operation_id": operation_id,
                },
                "deterministic_recovery_evidence": {
                    "procedure_id": "openstatspec.export-audit-reconciliation.v1",
                    "action_id": operation_id,
                    "phase": phase,
                    "destination_restored": True,
                    "operation_terminal_state_verified": False,
                    "terminal_reporting": "out_of_band_exception",
                },
                "success_forbidden": True,
            },
        ) from audit_error


@contextmanager
def _export_staging_directory(
    *, destination: Path, database_url: str,
    publication_state: dict[str, Any],
    dolt_conformance_source: DoltConformanceSource | None = None,
):
    """Compensate a published file if staging-directory cleanup fails."""
    try:
        with TemporaryDirectory(
            dir=destination.parent,
            prefix=f".{destination.name}.staging.",
        ) as export_directory:
            yield export_directory
    except Exception as staging_error:
        if not publication_state.get("published"):
            raise
        backup = publication_state["backup"]
        staged = publication_state["staged"]
        had_previous = publication_state["had_previous"]
        operation_id = publication_state["operation_id"]
        try:
            _restore_export_destination(
                destination=destination,
                backup=backup,
                had_previous=had_previous,
                expected_identity=publication_state["published_identity"],
            )
        except Exception as restore_error:
            _raise_export_cleanup_failed(
                original_error=staging_error,
                cleanup_error=restore_error,
                phase="staging_cleanup",
                destination=destination,
                backup=backup,
                staged=staged,
                had_previous=had_previous,
                database_url=database_url,
                operation_id=operation_id,
                dolt_conformance_source=dolt_conformance_source,
            )
        _mark_export_failed_after_restore(
            database_url=database_url,
            operation_id=operation_id,
            error=staging_error,
            phase="staging_cleanup",
            destination=destination,
            backup=backup,
            had_previous=had_previous,
            dolt_conformance_source=dolt_conformance_source,
        )
        raise


@_serialize_export_publication
def export_sav_dataset(
    *, database_url: str, dataset_id: str, destination: str | Path,
    allow_loss: tuple[str, ...] = (), legacy_locale: str | None = None,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    destination_path = Path(destination)
    if destination_path.suffix.lower() not in {".sav", ".zsav"}:
        raise UnsupportedOperationError("Export destinations must use the .sav or .zsav extension.")
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    dataset, variables, rows = read_wide_dataset(
        database_url=database_url, dataset_id=dataset_id,
        dolt_conformance_source=dolt_conformance_source,
    )
    validate_spss_catalog(
        variables,
        case_weight_variable=dataset.get("case_weight_variable"),
        multiple_response_sets=dataset.get("multiple_response_sets"),
    )
    persisted_events = read_fidelity_events(
        database_url=database_url, dataset_id=dataset_id,
        dolt_conformance_source=dolt_conformance_source,
    )
    if legacy_locale is not None:
        persisted_events = tuple(
            event for event in persisted_events
            if event["code"] != "source-encoding-not-preserved"
        )
    loss_report = _merge_loss_reports(
        _export_loss_report(dataset, variables, legacy_locale=legacy_locale),
        persisted_events,
    )
    rejected = [event["code"] for event in loss_report if event["code"] not in allow_loss]
    if rejected:
        raise UnsupportedOperationError("Export requires explicit allow_loss for: " + ", ".join(rejected))

    frame = pd.DataFrame(
        [
            {
                variable["source_name"]: (
                    math.nan if row[variable["physical_name"]] is None else float(row[variable["physical_name"]])
                ) if variable["storage_kind"] == "numeric" else row[variable["physical_name"]]
                for variable in variables
            }
            for row in rows
        ],
        columns=[variable["source_name"] for variable in variables],
    )
    accepted_events = tuple(
        event for event in loss_report if event["code"] in allow_loss
    )
    operation_id = None
    publication_state: dict[str, Any] = {"published": False}
    with _export_staging_directory(
        destination=destination_path, database_url=database_url,
        publication_state=publication_state,
        dolt_conformance_source=dolt_conformance_source,
    ) as export_directory:
        staged_destination = Path(export_directory) / destination_path.name
        _write_with_dictionary_bridge(
            staged_destination, frame, dataset, variables,
            legacy_locale=legacy_locale,
        )
        had_previous = _path_entry_exists(destination_path)
        backup = _reserve_export_backup(destination_path)
        if not had_previous:
            backup.unlink()
        try:
            operation_id = record_export_operation(
                database_url=database_url,
                dataset_id=dataset_id,
                destination=_path_reference_text(
                    destination_path, role="destination",
                ),
                allowed_fidelity_events=accepted_events,
                operation_details={
                    "engine": engine_identity(), "legacy_locale": legacy_locale,
                    "recovery": {
                        "procedure_id": "openstatspec.export-destination-restore.v1",
                        "phase": "prepared",
                        "destination": _path_reference(
                            destination_path, role="destination",
                        ),
                        "durable_backup": _path_reference(
                            backup, role="durable_backup",
                        ),
                        "previous_destination_existed": had_previous,
                        "publication_finalized": False,
                    },
                },
                terminal=False,
                dolt_conformance_source=dolt_conformance_source,
            )
        except Exception as audit_error:
            try:
                backup.unlink(missing_ok=True)
            except Exception as cleanup_error:
                _raise_export_cleanup_failed(
                    original_error=audit_error, cleanup_error=cleanup_error,
                    phase="audit_start_placeholder_cleanup",
                    destination=destination_path, backup=backup,
                    staged=staged_destination, had_previous=had_previous,
                    database_url=database_url,
                    dolt_conformance_source=dolt_conformance_source,
                )
            raise
        backup_installed = False
        try:
            _publish_staged_destination(
                staged=staged_destination,
                destination=destination_path,
                backup=backup,
                state=publication_state,
            )
            had_previous = publication_state["had_previous"]
            backup_installed = publication_state["backup_installed"]
            publication_state.update({
                "published": True,
                "backup": backup,
                "staged": staged_destination,
                "operation_id": operation_id,
            })
        except Exception as publish_error:
            had_previous = publication_state.get("had_previous", had_previous)
            backup_installed = publication_state.get(
                "backup_installed", backup_installed,
            )
            published_identity = publication_state.get("published_identity")
            if backup_installed or not had_previous:
                try:
                    _restore_export_destination(
                        destination=destination_path, backup=backup,
                        had_previous=had_previous,
                        expected_identity=published_identity,
                    )
                except Exception as cleanup_error:
                    _raise_export_cleanup_failed(
                        original_error=publish_error, cleanup_error=cleanup_error,
                        phase="publish", destination=destination_path, backup=backup,
                        staged=staged_destination, had_previous=had_previous,
                        database_url=database_url, operation_id=operation_id,
                        dolt_conformance_source=dolt_conformance_source,
                    )
            else:
                try:
                    backup.unlink(missing_ok=True)
                except Exception as cleanup_error:
                    _raise_export_cleanup_failed(
                        original_error=publish_error,
                        cleanup_error=cleanup_error,
                        phase="publish_placeholder_cleanup",
                        destination=destination_path, backup=backup,
                        staged=staged_destination,
                        had_previous=had_previous,
                        database_url=database_url, operation_id=operation_id,
                        dolt_conformance_source=dolt_conformance_source,
                    )
            _mark_export_failed_after_restore(
                database_url=database_url, operation_id=operation_id,
                error=publish_error, phase="publish", destination=destination_path,
                backup=backup, had_previous=had_previous,
                dolt_conformance_source=dolt_conformance_source,
            )
            raise
    assert operation_id is not None
    finalization_state = None
    try:
        finish_export_operation(
            database_url=database_url, operation_id=operation_id,
            dolt_conformance_source=dolt_conformance_source,
        )
    except Exception as finalization_error:
        state_read_fault = None
        try:
            finalization_state = read_export_operation_state(
                database_url=database_url, operation_id=operation_id,
                dolt_conformance_source=dolt_conformance_source,
            )
        except Exception as state_error:
            state_read_fault = _export_error_identity(
                state_error, phase="export_finalization_state_read",
            )
        if (
            finalization_state is not None
            and finalization_state["classification"] == "succeeded"
        ):
            pass
        elif (
            finalization_state is not None
            and finalization_state["classification"] == "running"
        ):
            try:
                _restore_export_destination(
                    destination=destination_path, backup=backup,
                    had_previous=had_previous,
                    expected_identity=publication_state["published_identity"],
                )
            except Exception as cleanup_error:
                _raise_export_cleanup_failed(
                    original_error=finalization_error,
                    cleanup_error=cleanup_error,
                    phase="audit_finalization",
                    destination=destination_path,
                    backup=backup,
                    staged=staged_destination,
                    had_previous=had_previous,
                    database_url=database_url,
                    operation_id=operation_id,
                    dolt_conformance_source=dolt_conformance_source,
                )
            _mark_export_failed_after_restore(
                database_url=database_url, operation_id=operation_id,
                error=finalization_error, phase="audit_finalization",
                destination=destination_path, backup=backup,
                had_previous=had_previous,
                dolt_conformance_source=dolt_conformance_source,
            )
            raise
        else:
            raise ExportRecoveryError(
                "audit_finalization_ambiguous",
                "The published export and durable backup were preserved because "
                "the operation catalogs do not prove whether finalization committed.",
                details={
                    "subcode": "export_finalization_commit_ambiguous",
                    "operation_id": operation_id,
                    "finalization_cause": _export_error_identity(
                        finalization_error, phase="export_audit_finalization",
                    ),
                    "state_read_fault": state_read_fault,
                    "observed_operation_state": finalization_state,
                    "residual_object_inventory": {
                        "destination": _path_reference(
                            destination_path, role="published_destination",
                        ),
                        "destination_exists": destination_path.exists(),
                        "backup": _path_reference(
                            backup, role="durable_backup",
                        ),
                        "backup_exists": _path_entry_exists(backup),
                    },
                    "deterministic_recovery_evidence": {
                        "procedure_id": "openstatspec.export-audit-reconciliation.v1",
                        "operation_terminal_state_verified": False,
                        "automatic_filesystem_recovery_performed": False,
                        "published_file_preserved": destination_path.exists(),
                        "durable_backup_preserved": _path_entry_exists(backup),
                        "manual_reconciliation_required": True,
                        "terminal_reporting": "out_of_band_exception",
                    },
                    "success_forbidden": True,
                },
            ) from finalization_error
    if _path_entry_exists(backup):
        try:
            backup.unlink()
        except Exception as cleanup_error:
            audit_fault = None
            try:
                record_export_backup_retained(
                    database_url=database_url, operation_id=operation_id,
                    destination=_path_reference_text(
                        destination_path, role="destination",
                    ),
                    backup=_path_reference_text(
                        backup, role="durable_backup",
                    ),
                    cleanup_error=cleanup_error,
                    dolt_conformance_source=dolt_conformance_source,
                )
            except Exception as warning_error:
                audit_fault = _export_error_identity(
                    warning_error, phase="backup_retained_warning_audit",
                )
            raise ExportRecoveryError(
                "backup_retained",
                "The export succeeded, but its durable prior-file backup could not be removed.",
                details={
                    "subcode": "post_success_backup_retained",
                    "operation_id": operation_id,
                    "operation_status": "succeeded",
                    "destination": _path_reference(
                        destination_path, role="destination",
                    ),
                    "durable_backup": _path_reference(
                        backup, role="durable_backup",
                    ),
                    "cleanup_fault": _export_error_identity(
                        cleanup_error, phase="post_success_backup_disposal",
                    ),
                    "warning_audit_persisted": audit_fault is None,
                    "audit_fault": audit_fault,
                    "success_forbidden": False,
                },
            )

    return {
        "dataset_id": dataset_id,
        "destination": str(destination_path),
        "operation_id": operation_id,
        "loss_report": loss_report,
    }


def _write_with_dictionary_bridge(
    destination: Path, frame: pd.DataFrame, dataset: dict[str, Any], variables: list[dict[str, Any]],
    *, legacy_locale: str | None = None,
) -> None:
    """Write a dictionary through pyspssio and preserve its document record.

    IBM I/O can copy document records but cannot expose their lines.  The
    strict helper reads and writes a temporary UTF-8 SAV source; the selected
    pyspssio engine then copies it into the real SAV or ZSAV writer, which
    keeps any ZSAV internal dictionary offsets valid.
    """
    compatible_names = {
        str(variable["source_name"]): str(variable["compat_name"])
        for variable in variables
        if variable.get("compat_name")
        and str(variable["compat_name"]).casefold()
        != str(variable["source_name"]).casefold()
    }
    variable_sets = (dataset.get("source_extensions") or {}).get(
        "spss.variable_sets"
    )
    metadata = _writer_metadata(dataset, variables)
    documents = _json_load(dataset.get("documents"), [])
    source_encoding = str(dataset.get("source_encoding") or "UTF-8")
    legacy_output = _is_non_utf8_encoding(source_encoding) and legacy_locale is not None
    output_encoding: str | None = None
    with ExitStack() as stack:
        document_source = None
        if documents:
            temporary_directory = Path(stack.enter_context(TemporaryDirectory()))
            temporary_source = temporary_directory / "documents.sav"
            pyspssio.write_sav(
                str(temporary_source), pd.DataFrame({"document_source": [0.0]}),
                unicode=not legacy_output, locale=legacy_locale,
            )
            temporary_encoding = str(pyspssio.read_metadata(str(temporary_source)).get("encoding") or "UTF-8")
            _require_matching_legacy_encoding(source_encoding, temporary_encoding, legacy_output)
            write_document_lines(temporary_source, documents, encoding=temporary_encoding)
            document_source = stack.enter_context(
                pyspssio.Reader(
                    str(temporary_source), mode="r",
                    unicode=not legacy_output, locale=legacy_locale,
                )
            )
        writer = stack.enter_context(
            pyspssio.Writer(
                str(destination), mode="w", unicode=not legacy_output, locale=legacy_locale,
            )
        )
        writer.compression = 2 if destination.suffix.lower() == ".zsav" else 1
        writer.file_label = str(dataset.get("file_label") or "")
        for variable in variables:
            writer._add_var(  # pylint: disable=protected-access
                variable["source_name"],
                int(variable["string_width"] or 0) if variable["storage_kind"] == "string" else 0,
            )
        for variable in variables:
            set_format_tuples(
                writer, name=variable["source_name"],
                print_format=_catalog_format(variable, "print_format"),
                write_format=_catalog_format(variable, "write_format"),
            )
        for name in (
            "var_labels", "var_measure_levels", "var_roles", "var_alignments",
            "var_column_widths", "var_value_labels", "var_missing_values", "mrsets",
        ):
            value = metadata.get(name)
            if value:
                setattr(writer, name, value)
        if dataset.get("case_weight_variable"):
            writer.case_weight_var = dataset["case_weight_variable"]
        set_file_attribute_pairs(
            writer, attribute_pairs(_json_load(dataset.get("file_attributes"), {})),
        )
        for variable in variables:
            values = _json_load(variable.get("attributes"), {})
            if values:
                set_variable_attribute_pairs(
                    writer, variable["source_name"], attribute_pairs(values),
                )
        if variable_sets:
            writer.var_sets = variable_sets
        if document_source is not None:
            writer.copy_documents_from(document_source)
        writer.commit_header()
        output_encoding = str(writer.file_encoding or "")
        if not output_encoding:
            raise RawDictionaryError("Writer did not expose its output file encoding.")
        _require_matching_legacy_encoding(source_encoding, output_encoding, legacy_output)
        writer.write_data(frame)
    if output_encoding is None:
        raise RawDictionaryError("Writer output encoding was not captured.")
    extended_labels = {
        str(name): str(definition["label"])
        for name, definition in _json_load(dataset.get("multiple_response_sets"), {}).items()
        if isinstance(definition, dict)
        and definition.get("use_category_labels")
        and definition.get("use_first_var_label")
        and definition.get("label")
    }
    write_extended_mrset_labels(
        destination, extended_labels, encoding=output_encoding,
    )
    write_compatible_names(
        destination,
        compatible_names,
        encoding=output_encoding,
    )

def _catalog_format(variable: dict[str, Any], key: str) -> tuple[int, int, int]:
    encoded = variable.get(key)
    if encoded:
        return format_tuple(_json_load(encoded, encoded))
    legacy = variable.get("format")
    if legacy:
        return format_tuple(legacy)
    if variable["storage_kind"] == "string":
        return 1, int(variable["string_width"] or 1), 0
    return 5, 8, 2


def _writer_metadata(dataset: dict[str, Any], variables: list[dict[str, Any]]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "case_weight_var": dataset.get("case_weight_variable") or None,
        "mrsets": _json_load(dataset.get("multiple_response_sets"), {}),
        "var_types": {
            item["source_name"]: int(item["string_width"] or 1)
            for item in variables if item["storage_kind"] == "string"
        },
        "var_labels": {
            item["source_name"]: item["label"] for item in variables if item.get("label")
        },
        "var_measure_levels": {
            item["source_name"]: item["measure"] for item in variables if item.get("measure")
        },
        "var_roles": {
            item["source_name"]: item["role"] for item in variables if item.get("role")
        },
        "var_alignments": {
            item["source_name"]: item["alignment"] for item in variables if item.get("alignment")
        },
        "var_column_widths": {
            item["source_name"]: int(item["display_width"])
            for item in variables if item.get("display_width") is not None
        },
        "var_value_labels": {
            item["source_name"]: _typed_value_labels(item)
            for item in variables if _json_load(item.get("value_labels"), {})
        },
        "var_missing_values": {
            item["source_name"]: missing
            for item in variables
            if (missing := _pyspssio_missing_rules(item.get("missing_ranges") or "[]"))
        },
    }
    return {key: value for key, value in metadata.items() if value not in ({}, None)}


def _typed_value_labels(variable: dict[str, Any]) -> dict[Any, str]:
    raw = _json_load(variable.get("value_labels"), {})
    if variable["storage_kind"] == "numeric":
        return {float(value): str(label) for value, label in raw.items()}
    return {str(value): str(label) for value, label in raw.items()}


def _engine_loss_report(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    if metadata.get("_documents") is None:
        events["documents-unreadable"] = {
            "code": "documents-unreadable",
            "detail": "The strict raw SAV dictionary reader could not inspect document text.",
            "details": {"engine_error": metadata.get("_documents_error", "unknown")},
        }
    variable_sets = metadata.get("_var_sets")
    if variable_sets is None:
        events["variable-sets-unobservable"] = {
            "code": "variable-sets-unobservable",
            "detail": "pyspssio could not inspect source variable sets; they cannot be preserved silently.",
            "details": {"engine_error": metadata.get("_var_sets_error", "unknown")},
        }
    if _is_non_utf8_encoding(metadata.get("encoding")):
        events["source-encoding-not-preserved"] = {
            "code": "source-encoding-not-preserved",
            "detail": "The pyspssio writer has no source-encoding preservation contract for this legacy code page.",
            "details": {"source_encoding": metadata.get("encoding")},
        }
    return events


def _export_loss_report(
    dataset: dict[str, Any], variables: list[dict[str, Any]], *, legacy_locale: str | None,
) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    if _is_non_utf8_encoding(dataset.get("source_encoding")) and legacy_locale is None:
        events.append({
            "code": "source-encoding-not-preserved",
            "detail": "Export requires an explicit legacy_locale for this non-UTF-8 source encoding.",
            "details": {"source_encoding": dataset.get("source_encoding")},
        })
    return tuple(events)


def _require_matching_legacy_encoding(
    source_encoding: str, emitted_encoding: str, legacy_output: bool,
) -> None:
    if legacy_output and _normalize_encoding(source_encoding) != _normalize_encoding(emitted_encoding):
        raise UnsupportedOperationError(
            "Configured legacy locale emitted "
            + emitted_encoding
            + " instead of required source encoding "
            + source_encoding
            + "."
        )


def _normalize_encoding(value: str) -> str:
    return str(value).strip().replace("_", "-").replace("WINDOWS-", "CP").upper()

def _merge_loss_reports(*reports: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    events: dict[str, dict[str, Any]] = {}
    for report in reports:
        for event in report:
            events.setdefault(event["code"], event)
    return tuple(events[code] for code in sorted(events))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _json_ordered(value: Any) -> str:
    """Legacy JSON copy for migration only; retain source insertion order."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_load(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _is_non_utf8_encoding(encoding: Any) -> bool:
    return encoding is not None and str(encoding).strip().replace("_", "-").upper() not in _UTF8_ENCODINGS


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_source(source_path: Path) -> None:
    if source_path.suffix.lower() not in {".sav", ".zsav"}:
        raise UnsupportedOperationError("Only unencrypted SAV and ZSAV sources are supported.")
