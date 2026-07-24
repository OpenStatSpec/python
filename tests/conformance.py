"""Semantic comparison helpers for supported SAV/ZSAV round trips."""

from pathlib import Path
from typing import Any

import pandas as pd
import pyreadstat


def compare_sav_semantics(source: str | Path, exported: str | Path) -> dict[str, Any]:
    source_frame, source_meta = pyreadstat.read_sav(source, user_missing=True, disable_datetime_conversion=True)
    exported_frame, exported_meta = pyreadstat.read_sav(exported, user_missing=True, disable_datetime_conversion=True)
    failures: list[str] = []
    if list(source_frame.columns) != list(exported_frame.columns):
        failures.append("variable-order")
    if not source_frame.equals(exported_frame):
        failures.append("values-or-case-order")
    for attribute in ("column_labels", "original_variable_types", "variable_measure", "missing_ranges", "notes", "file_label"):
        if getattr(source_meta, attribute, None) != getattr(exported_meta, attribute, None):
            failures.append(attribute)
    if getattr(source_meta, "variable_value_labels", {}) != getattr(exported_meta, "variable_value_labels", {}):
        failures.append("variable_value_labels")
    return {"equivalent": not failures, "differences": failures}