"""Initial unencrypted SAV/ZSAV adapter using pyreadstat."""

import json
import math
from pathlib import Path
from typing import Any

import pyreadstat

from ..core import LossReport, UnsupportedOperationError
from ..sql.wide import create_wide_dataset, physical_name, read_wide_dataset


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
    result = []
    for ordinal, source_name in enumerate(names, start=1):
        kind = "string" if str(formats.get(source_name, "")).upper().startswith("A") else "numeric"
        result.append({
            "ordinal": ordinal, "source_name": source_name,
            "physical_name": physical_name(source_name, used), "storage_kind": kind,
            "string_width": widths.get(source_name) if kind == "string" else None,
            "label": labels.get(source_name, "") or "", "format": formats.get(source_name),
            "measure": measures.get(source_name), "alignment": alignments.get(source_name),
            "display_width": displays.get(source_name),
            "value_labels": json.dumps(value_labels.get(source_name, {}), sort_keys=True),
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
        "variable_count": len(meta.column_names), "variables": _variables(meta, list(meta.column_names)),
    }


def import_sav_dataset(*, source: str | Path, database_url: str, dataset_id: str) -> dict[str, Any]:
    source_path = Path(source)
    _require_source(source_path)
    frame, meta = pyreadstat.read_sav(source_path, user_missing=True, disable_datetime_conversion=True)
    variables = _variables(meta, list(frame.columns))
    result = create_wide_dataset(
        database_url=database_url, dataset_id=dataset_id, source_name=source_path.name,
        source_format=source_path.suffix[1:].upper(), rows=_rows(frame, variables),
        variables=variables, file_label=getattr(meta, "file_label", "") or "",
        source_encoding=getattr(meta, "file_encoding", None),
        documents=json.dumps(list(getattr(meta, "notes", []) or [])),
    )
    return {**result, "loss_report": LossReport().events}


def export_sav_dataset(*, database_url: str, dataset_id: str, destination: str | Path) -> dict[str, Any]:
    destination_path = Path(destination)
    if destination_path.suffix.lower() != ".sav":
        raise UnsupportedOperationError("This initial profile exports SAV only; ZSAV output is not yet supported.")
    dataset, variables, rows = read_wide_dataset(database_url=database_url, dataset_id=dataset_id)
    import pandas as pd
    frame = pd.DataFrame(
        [{item["source_name"]: row[item["physical_name"]] for item in variables} for row in rows],
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
    pyreadstat.write_sav(
        frame, destination_path, file_label=dataset["file_label"], column_labels=labels,
        variable_format=formats, variable_measure=measures,
        variable_display_width=displays, variable_value_labels=value_labels,
        missing_ranges=missing_ranges, note=json.loads(dataset["documents"]),
    )
    return {
        "dataset_id": dataset_id, "destination": str(destination_path),
        "loss_report": LossReport().events,
    }


def _require_source(source_path: Path) -> None:
    if source_path.suffix.lower() not in {".sav", ".zsav"}:
        raise UnsupportedOperationError("Only unencrypted SAV and ZSAV sources are supported.")

