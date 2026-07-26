"""Initial unencrypted SAV/ZSAV adapter using pyreadstat."""

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyreadstat

from ..core import LossReport, UnsupportedOperationError
from ..sql.wide import create_wide_dataset, physical_name, read_fidelity_events, read_wide_dataset, record_export_operation


def _variables(meta: Any, names: list[str]) -> list[dict[str, Any]]:
    used = {"__case_ordinal"}
    labels = dict(getattr(meta, "column_names_to_labels", {}) or {})
    formats = dict(getattr(meta, "original_variable_types", {}) or {})
    measures = dict(getattr(meta, "variable_measure", {}) or {})
    alignments = dict(getattr(meta, "variable_alignment", {}) or {})
    displays = dict(getattr(meta, "variable_display_width", {}) or {})
    value_labels = dict(getattr(meta, "variable_value_labels", {}) or {})
    missing_ranges = dict(getattr(meta, "missing_ranges", {}) or {})
    widths = dict(getattr(meta, "variable_storage_width", {}) or {})
    readstat_types = dict(getattr(meta, "readstat_variable_types", {}) or {})
    result = []
    for ordinal, source_name in enumerate(names, start=1):
        kind = "string" if str(formats.get(source_name, "")).upper().startswith("A") else "numeric"
        result.append({
            "ordinal": ordinal, "source_name": source_name,
            "physical_name": physical_name(source_name, used), "storage_kind": kind,
            "readstat_storage_type": readstat_types.get(source_name),
            "string_width": widths.get(source_name) if kind == "string" else None,
            "label": labels.get(source_name, "") or "", "format": formats.get(source_name),
            "measure": measures.get(source_name), "alignment": alignments.get(source_name),
            "display_width": displays.get(source_name),
            "value_labels": json.dumps(value_labels.get(source_name, {}), ensure_ascii=False),
            "missing_ranges": json.dumps(missing_ranges.get(source_name, []), default=str),
        })
    return result


def _rows(frame: Any, variables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for input_row in frame.to_dict(orient="records"):
        output_row = {}
        for item in variables:
            value = input_row[item["source_name"]]
            if item["storage_kind"] == "numeric" and isinstance(value, float) and math.isnan(value):
                value = None
            elif item["storage_kind"] == "string" and value is None:
                value = ""
            output_row[item["physical_name"]] = value
        result.append(output_row)
    return result


def inspect_sav(source: str | Path) -> dict[str, Any]:
    source_path = Path(source)
    _require_source(source_path)
    _, meta = pyreadstat.read_sav(source_path, metadataonly=True, user_missing=True)
    return {
        "source_format": source_path.suffix[1:].upper(), "source_name": source_path.name,
        "source_sha256": _sha256(source_path), "source_encoding": getattr(meta, "file_encoding", None),
        "file_label": getattr(meta, "file_label", "") or "",
        "documents": list(getattr(meta, "notes", []) or []),
        "multiple_response_sets": dict(getattr(meta, "mr_sets", {}) or {}),
        "loss_report": _import_loss_report(meta),
        "variable_count": len(meta.column_names), "variables": _variables(meta, list(meta.column_names)),
    }


def import_sav_dataset(*, source: str | Path, database_url: str, dataset_id: str) -> dict[str, Any]:
    source_path = Path(source)
    _require_source(source_path)
    frame, meta = pyreadstat.read_sav(source_path, user_missing=True, disable_datetime_conversion=True)
    variables = _variables(meta, list(frame.columns))
    import_loss_report = _import_loss_report(meta)
    result = create_wide_dataset(
        database_url=database_url, dataset_id=dataset_id, source_name=source_path.name,
        source_format=source_path.suffix[1:].upper(), rows=_rows(frame, variables),
        variables=variables, file_label=getattr(meta, "file_label", "") or "",
        source_encoding=getattr(meta, "file_encoding", None),
        source_table_name=getattr(meta, "table_name", None),
        source_sha256=_sha256(source_path),
        source_created_at=_iso_datetime(getattr(meta, "creation_time", None)),
        source_modified_at=_iso_datetime(getattr(meta, "modification_time", None)),
        imported_at=datetime.now(UTC).isoformat(),
        documents=json.dumps(list(getattr(meta, "notes", []) or [])),
        multiple_response_sets=json.dumps(dict(getattr(meta, "mr_sets", {}) or {}), default=str),
        fidelity_events=import_loss_report,
    )
    return {**result, "loss_report": import_loss_report}


def export_sav_dataset(*, database_url: str, dataset_id: str, destination: str | Path, allow_loss: tuple[str, ...] = ()) -> dict[str, Any]:
    destination_path = Path(destination)
    if destination_path.suffix.lower() not in {".sav", ".zsav"}:
        raise UnsupportedOperationError("Export destinations must use the .sav or .zsav extension.")
    dataset, variables, rows = read_wide_dataset(database_url=database_url, dataset_id=dataset_id)
    import pandas as pd
    frame = pd.DataFrame(
        [
            {
                item["source_name"]: (
                    None
                    if row[item["physical_name"]] is None
                    else float(row[item["physical_name"]])
                )
                if item["storage_kind"] == "numeric"
                else row[item["physical_name"]]
                for item in variables
            }
            for row in rows
        ],
        columns=[item["source_name"] for item in variables],
    )
    labels = {item["source_name"]: item["label"] for item in variables if item["label"]}
    formats = {item["source_name"]: item["format"] for item in variables if item["format"]}
    measures = {item["source_name"]: item["measure"] for item in variables if item["measure"]}
    displays = {item["source_name"]: item["display_width"] for item in variables if item["display_width"]}
    missing_ranges = {
        item["source_name"]: json.loads(item["missing_ranges"])
        for item in variables if item["missing_ranges"] != "[]"
    }
    value_labels = {
        item["source_name"]: {(float(key) if item["storage_kind"] == "numeric" else key): value for key, value in json.loads(item["value_labels"]).items()}
        for item in variables if item["value_labels"] != "{}"
    }
    loss_report = _merge_loss_reports(
        read_fidelity_events(database_url=database_url, dataset_id=dataset_id),
        _export_loss_report(dataset, variables),
    )
    rejected = [event["code"] for event in loss_report if event["code"] not in allow_loss]
    if rejected:
        raise UnsupportedOperationError("Export requires explicit allow_loss for: " + ", ".join(rejected))
    pyreadstat.write_sav(
        frame, destination_path, file_label=dataset["file_label"], column_labels=labels,
        compress=destination_path.suffix.lower() == ".zsav",
        variable_format=formats, variable_measure=measures,
        variable_display_width=displays, variable_value_labels=value_labels,
        missing_ranges=missing_ranges, note=json.loads(dataset["documents"]),
    )
    operation_id = record_export_operation(
        database_url=database_url, dataset_id=dataset_id, destination=str(destination_path),
        allowed_fidelity_events=tuple(event for event in loss_report if event["code"] in allow_loss),
    )
    return {
        "dataset_id": dataset_id, "destination": str(destination_path), "operation_id": operation_id,
        "loss_report": loss_report,
    }






def _iso_datetime(value: Any) -> str | None:
    return value.isoformat() if value is not None else None

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _import_loss_report(meta: Any) -> tuple[dict[str, str], ...]:
    events = [{"code": "unobservable-source-dictionary-features", "detail": "The reader does not expose SPSS variable sets or custom file/variable attributes, so their presence cannot be established or preserved."}]
    if getattr(meta, "mr_sets", {}) or {}:
        events.append({"code": "multiple-response-sets-not-exportable", "detail": "Multiple-response sets are catalogued but pyreadstat cannot write them back to SAV."})
    return tuple(events)


def _export_loss_report(dataset: dict[str, Any], variables: list[dict[str, Any]]) -> tuple[dict[str, str], ...]:
    events = [{"code": "unobservable-source-dictionary-features", "detail": "Variable sets and custom file/variable attributes are not observable through the reader and therefore cannot be restored by export."}]
    if dataset["multiple_response_sets"] != "{}":
        events.append({"code": "multiple-response-sets-not-exported", "detail": "The SAV writer has no multiple-response-set output capability."})
    if any(item.get("alignment") not in (None, "unknown") for item in variables):
        events.append({"code": "variable-alignment-not-exported", "detail": "The SAV writer has no variable-alignment output capability."})
    return tuple(events)


def _merge_loss_reports(*reports: tuple[dict[str, str], ...]) -> tuple[dict[str, str], ...]:
    """Keep one deterministic diagnostic per loss code across import and export."""
    events: dict[str, dict[str, str]] = {}
    for report in reports:
        for event in report:
            events.setdefault(event["code"], event)
    return tuple(events[code] for code in sorted(events))


def _require_source(source_path: Path) -> None:
    if source_path.suffix.lower() not in {".sav", ".zsav"}:
        raise UnsupportedOperationError("Only unencrypted SAV and ZSAV sources are supported.")

