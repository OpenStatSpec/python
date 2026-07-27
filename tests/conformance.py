"""Reusable pyspssio fixtures and semantic comparators for SAV/ZSAV round trips."""

import math
from pathlib import Path
from typing import Any

import pandas as pd
import pyspssio
from pandas.testing import assert_frame_equal


def write_supported_semantics_fixture(destination: str | Path) -> dict[str, Any]:
    """Write the metadata pyspssio demonstrably exposes and restores."""
    destination = Path(destination)
    long_text = "Õ🙂漢字" * 90
    frame = pd.DataFrame({
        "lowest_range": [-2.0, -1.0, 0.0, 7.0],
        "highest_range": [-99.0, 0.0, 1.0, 42.0],
        "code": [3.0, 1.0, 2.0, 1.0],
        "status": ["NA", "DK", "ok", ""],
        "comment": [long_text, "", "näide", long_text],
        "interview_date": [23123.0, 23124.0, 23125.0, 23126.0],
        "resp_a": [1.0, 0.0, 1.0, 0.0],
        "resp_b": [0.0, 1.0, 1.0, 0.0],
    })
    metadata = {
        "var_types": {"status": 8, "comment": 1024},
        "var_formats": {
            "lowest_range": "F12.1", "highest_range": "F12.1", "code": "F8.0",
            "status": "A8", "comment": "A1024", "interview_date": "DATE11",
        },
        "var_labels": {
            "lowest_range": "LOWEST-style missing range", "highest_range": "HIGHEST-style missing range",
            "code": "Ordered numeric code", "status": "String missing code",
            "comment": "Long UTF-8 comment", "interview_date": "SPSS numeric date",
        },
        "var_value_labels": {
            "code": {3.0: "third", 1.0: "first", 2.0: "second"},
            "status": {"DK": "don't know", "NA": "not answered", "ok": "valid"},
        },
        "var_missing_values": {
            "lowest_range": {"lo": -sys_float_max(), "hi": -1.0, "values": [7.0]},
            "highest_range": {"lo": 1.0, "hi": sys_float_max(), "values": [-99.0]},
            "status": {"values": ["NA", "DK"]},
        },
        "var_measure_levels": {
            "lowest_range": "scale", "highest_range": "scale", "code": "ordinal",
            "status": "nominal", "comment": "nominal", "interview_date": "scale",
        },
        "var_alignments": {column: "left" for column in frame.columns},
        "var_column_widths": {"lowest_range": 12, "highest_range": 12, "code": 8, "status": 12, "comment": 48, "interview_date": 11},
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
    return {"equivalent": not failures, "differences": failures}