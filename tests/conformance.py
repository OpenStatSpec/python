"""Reusable fixtures and semantic comparators for supported SAV/ZSAV round trips.

These helpers deliberately compare only metadata that the Python adapter and
its current SAV writer expose. Attributes, variable sets, and
multiple-response sets are not asserted here because they are explicitly
reported as unsupported fidelity losses.
"""

import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pyreadstat
from pandas.testing import assert_frame_equal


def write_supported_semantics_fixture(destination: str | Path) -> dict[str, Any]:
    """Write a fixture spanning every currently supported dictionary semantic.

    -sys.float_info.max and sys.float_info.max are how ReadStat represents
    SPSS LOWEST and HIGHEST missing-range endpoints on write. ReadStat exposes
    them as NaN and +inf when the SAV/ZSAV file is read back; the comparator
    canonicalizes those markers.
    """
    destination = Path(destination)
    long_text = "Õ🙂漢字" * 90
    frame = pd.DataFrame({
        "lowest_range": [-2.0, -1.0, 0.0, 7.0],
        "highest_range": [-99.0, 0.0, 1.0, 42.0],
        "code": [3.0, 1.0, 2.0, 1.0],
        "status": ["NA", "DK", "ok", ""],
        "comment": [long_text, "", "näide", long_text],
        "interview_date": [23123.0, 23124.0, 23125.0, 23126.0],
    })
    pyreadstat.write_sav(
        frame,
        destination,
        compress=destination.suffix.lower() == ".zsav",
        file_label="OpenStatSpec supported-semantics fixture",
        column_labels={
            "lowest_range": "LOWEST-style missing range",
            "highest_range": "HIGHEST-style missing range",
            "code": "Ordered numeric code",
            "status": "String missing code",
            "comment": "Long UTF-8 comment",
            "interview_date": "SPSS numeric date",
        },
        variable_value_labels={
            # Deliberately non-sorted insertion order is part of the fixture.
            "code": {3.0: "third", 1.0: "first", 2.0: "second"},
            "status": {"DK": "don't know", "NA": "not answered", "ok": "valid"},
        },
        missing_ranges={
            "lowest_range": [{"lo": -sys.float_info.max, "hi": -1.0}, 7.0],
            "highest_range": [{"lo": 1.0, "hi": sys.float_info.max}, -99.0],
            "status": ["NA", "DK"],
        },
        variable_measure={
            "lowest_range": "scale",
            "highest_range": "scale",
            "code": "ordinal",
            "status": "nominal",
            "comment": "nominal",
            "interview_date": "scale",
        },
        variable_display_width={
            "lowest_range": 12,
            "highest_range": 12,
            "code": 8,
            "status": 12,
            "comment": 48,
            "interview_date": 11,
        },
        variable_format={
            "lowest_range": "F12.1",
            "highest_range": "F12.1",
            "code": "F8.0",
            "status": "A8",
            "comment": "A1024",
            "interview_date": "DATE11",
        },
        note=["First ordered document", "Teine dokument: Õ🙂"],
    )
    return {"long_text": long_text}


def _canonical_missing_endpoint(value: Any, *, endpoint: str) -> Any:
    """Normalize ReadStat's public sentinels for SPSS LOWEST/HIGHEST."""
    if isinstance(value, float):
        if math.isnan(value):
            return "<LOWEST>" if endpoint == "lo" else "<SYSTEM-NAN>"
        if math.isinf(value):
            return "<HIGHEST>" if value > 0 else "<LOWEST>"
    return value


def _canonical_missing_ranges(metadata: Any) -> dict[str, tuple[tuple[Any, Any], ...]]:
    result: dict[str, tuple[tuple[Any, Any], ...]] = {}
    for variable, rules in (getattr(metadata, "missing_ranges", {}) or {}).items():
        result[variable] = tuple(
            (
                _canonical_missing_endpoint(rule.get("lo"), endpoint="lo"),
                _canonical_missing_endpoint(rule.get("hi"), endpoint="hi"),
            )
            for rule in rules
        )
    return result


def _ordered_value_labels(metadata: Any) -> dict[str, tuple[tuple[Any, str], ...]]:
    return {
        variable: tuple((value, str(label)) for value, label in labels.items())
        for variable, labels in (getattr(metadata, "variable_value_labels", {}) or {}).items()
    }


def compare_sav_semantics(source: str | Path, exported: str | Path) -> dict[str, Any]:
    source_frame, source_meta = pyreadstat.read_sav(source, user_missing=True, disable_datetime_conversion=True)
    exported_frame, exported_meta = pyreadstat.read_sav(exported, user_missing=True, disable_datetime_conversion=True)
    failures: list[str] = []
    if list(source_frame.columns) != list(exported_frame.columns):
        failures.append("variable-order")
    try:
        assert_frame_equal(source_frame, exported_frame, check_dtype=True, check_like=False)
    except AssertionError:
        failures.append("values-or-case-order")
    for attribute in (
        "column_labels",
        "original_variable_types",
        "variable_measure",
        "variable_display_width",
        "variable_storage_width",
        "notes",
        "file_label",
        "file_encoding",
    ):
        if getattr(source_meta, attribute, None) != getattr(exported_meta, attribute, None):
            failures.append(attribute)
    if _canonical_missing_ranges(source_meta) != _canonical_missing_ranges(exported_meta):
        failures.append("missing_ranges")
    if _ordered_value_labels(source_meta) != _ordered_value_labels(exported_meta):
        failures.append("variable_value_labels")
    return {"equivalent": not failures, "differences": failures}
