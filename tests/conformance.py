"""Reusable pyspssio fixtures and semantic comparators for SAV/ZSAV round trips."""

from pathlib import Path
from typing import Any

import pandas as pd
import pyspssio
from pandas.testing import assert_frame_equal


def write_supported_semantics_fixture(destination: str | Path) -> dict[str, Any]:
    """Write a real SAV/ZSAV fixture for every pyspssio-supported core feature.

    SPSS time-like values are deliberately stored as numeric values. Their
    interpretation belongs to the accompanying SPSS format, never to a SQL
    temporal coercion. The fixture also covers every legal user-missing
    shape: three discrete values, a range alone, and a range plus one value.
    ``pyspssio`` exposes SPSS LOWEST/HIGHEST again as negative/positive
    infinity, which is the representation the strict SQL catalog preserves.
    """
    destination = Path(destination)
    long_text = "Õ🙂漢字" * 90
    frame = pd.DataFrame({
        "discrete_missing": [1.0, 2.0, 3.0, 4.0],
        "range_only": [-1.0, 0.0, 1.0, 2.0],
        "lowest_range": [-2.0, -1.0, 0.0, 7.0],
        "highest_range": [-99.0, 0.0, 1.0, 42.0],
        "code": [3.0, 1.0, 2.0, 1.0],
        "status": ["NA", "DK", "ok", ""],
        "comment": [long_text, "", "näide", long_text],
        "interview_date": [23123.0, 23124.0, 23125.0, 23126.0],
        "interview_time": [3661.25, 0.0, 86399.5, 12.0],
        "interview_datetime": [23123.5, 23124.0, 23125.75, 23126.125],
        "interview_dtime": [90061.5, 0.0, 172800.25, 12.0],
        "formatted_comma": [12345.67, -12345.67, 0.0, 1.25],
        "formatted_dot": [12345.67, -12345.67, 0.0, 1.25],
        "formatted_pct": [12.5, 0.0, 100.0, -2.5],
        "resp_a": [1.0, 0.0, 1.0, 0.0],
        "resp_b": [0.0, 1.0, 1.0, 0.0],
    })
    metadata = {
        "var_types": {"status": 8, "comment": 1024},
        "var_formats": {
            "discrete_missing": "F8.0", "range_only": "F8.0",
            "lowest_range": "F12.1", "highest_range": "F12.1", "code": "F8.0",
            "status": "A8", "comment": "A1024", "interview_date": "DATE11",
            "interview_time": "TIME8", "interview_datetime": "DATETIME20",
            "interview_dtime": "DTIME10", "formatted_comma": "COMMA12.2",
            "formatted_dot": "DOT12.2", "formatted_pct": "PCT8.1",
        },
        "var_labels": {
            "discrete_missing": "Three numeric user-missing codes",
            "range_only": "Range-only numeric user missing",
            "lowest_range": "LOWEST-style missing range", "highest_range": "HIGHEST-style missing range",
            "code": "Ordered numeric code", "status": "String missing code",
            "comment": "Long UTF-8 comment", "interview_date": "SPSS numeric date",
            "interview_time": "SPSS numeric time", "interview_datetime": "SPSS numeric datetime",
            "interview_dtime": "SPSS numeric duration", "formatted_comma": "SPSS comma format",
            "formatted_dot": "SPSS dot format", "formatted_pct": "SPSS percent format",
        },
        "var_value_labels": {
            "discrete_missing": {3.0: "third", 1.0: "first", 2.0: "second"},
            "code": {3.0: "third", 1.0: "first", 2.0: "second"},
            "status": {"DK": "don't know", "NA": "not answered", "ok": "valid"},
        },
        "var_missing_values": {
            "discrete_missing": {"values": [1.0, 2.0, 3.0]},
            "range_only": {"lo": -1.0, "hi": 1.0},
            "lowest_range": {"lo": -sys_float_max(), "hi": -1.0, "values": [7.0]},
            "highest_range": {"lo": 1.0, "hi": sys_float_max(), "values": [-99.0]},
            "status": {"values": ["NA", "DK"]},
        },
        "var_measure_levels": {
            "discrete_missing": "scale", "range_only": "scale", "lowest_range": "scale",
            "highest_range": "scale", "code": "scale",
            "status": "nominal", "comment": "nominal", "interview_date": "scale",
        },
        "var_alignments": {column: "left" for column in frame.columns},
        "var_column_widths": {
            "discrete_missing": 8, "range_only": 8, "lowest_range": 12, "highest_range": 12,
            "code": 8, "status": 12, "comment": 48, "interview_date": 11,
            "interview_time": 8, "interview_datetime": 20, "interview_dtime": 10,
            "formatted_comma": 12, "formatted_dot": 12, "formatted_pct": 8,
        },
        "var_roles": {"code": "target", "status": "input"},
        "var_attributes": {"code": {"Origin": "fixture"}},
        "mrsets": {"$responses": {"label": "Responses", "counted_value": 1, "variable_list": ["resp_a", "resp_b"]}},
        "file_attributes": {"Fixture": "OpenStatSpec"},
        "case_weight_var": "code",
    }
    pyspssio.write_sav(str(destination), frame, metadata=metadata)
    return {"long_text": long_text}


def sys_float_max() -> float:
    return float.fromhex("0x1.fffffffffffffp+1023")


def _canonical_missing(metadata: dict[str, Any]) -> dict[str, tuple[tuple[Any, Any, tuple[Any, ...]], ...]]:
    result: dict[str, tuple[tuple[Any, Any, tuple[Any, ...]], ...]] = {}
    for name, rule in (metadata.get("var_missing_values") or {}).items():
        rule = rule or {}
        if "lo" in rule or "hi" in rule:
            result[name] = ((rule.get("lo"), rule.get("hi"), tuple(rule.get("values") or ())),)
        else:
            result[name] = ((None, None, tuple(rule.get("values") or ())),)
    return result


def _ordered_value_labels(metadata: dict[str, Any]) -> dict[str, tuple[tuple[Any, str], ...]]:
    """Retain the label order exposed by the SPSS engine, not merely its mapping."""
    return {
        name: tuple((value, str(label)) for value, label in labels.items())
        for name, labels in (metadata.get("var_value_labels") or {}).items()
    }


def compare_sav_semantics(source: str | Path, exported: str | Path) -> dict[str, Any]:
    source_frame, source_metadata = pyspssio.read_sav(str(source), convert_datetimes=False, include_user_missing=True)
    exported_frame, exported_metadata = pyspssio.read_sav(str(exported), convert_datetimes=False, include_user_missing=True)
    failures: list[str] = []
    if list(source_frame.columns) != list(exported_frame.columns):
        failures.append("variable-order")
    try:
        assert_frame_equal(source_frame, exported_frame, check_dtype=True, check_like=False)
    except AssertionError:
        failures.append("values-or-case-order")
    for attribute in (
        "encoding", "case_weight_var", "file_attributes", "mrsets", "var_types", "var_formats",
        "var_labels", "var_alignments", "var_column_widths", "var_measure_levels", "var_roles",
        "var_value_labels", "var_attributes",
    ):
        if source_metadata.get(attribute) != exported_metadata.get(attribute):
            failures.append(attribute)
    if _canonical_missing(source_metadata) != _canonical_missing(exported_metadata):
        failures.append("var_missing_values")
    if _ordered_value_labels(source_metadata) != _ordered_value_labels(exported_metadata):
        failures.append("var_value_label_order")
    return {"equivalent": not failures, "differences": failures}
