"""Synthetic contract checks for the public value-redacting SAV comparator."""

import copy
import struct
from pathlib import Path

import pandas as pd

import openstatspec
import openstatspec.spss.semantics as semantic_module


def _metadata() -> dict:
    return {
        "encoding": "UTF-8",
        "file_label": "classified-file-label",
        "case_weight_var": "numeric_secret",
        "var_types": {"numeric_secret": 0, "string_secret": 16},
        "var_labels": {
            "numeric_secret": "classified-numeric-label",
            "string_secret": "classified-string-label",
        },
        "var_measure_levels": {
            "numeric_secret": "scale", "string_secret": "nominal",
        },
        "var_roles": {"numeric_secret": "target", "string_secret": "input"},
        "var_alignments": {"numeric_secret": "right", "string_secret": "left"},
        "var_column_widths": {"numeric_secret": 8, "string_secret": 16},
        "var_compat_names": {"numeric_secret": "NUMERIC", "string_secret": "STRING"},
        "var_value_labels": {
            "numeric_secret": {1.0: "classified-one", 2.0: "classified-two"},
        },
        "var_missing_values": {
            "numeric_secret": {"values": [-99.0, -98.0]},
        },
        "mrsets": {
            "$classified": {
                "label": "classified-mr-label",
                "counted_value": 1.0,
                "variable_list": ["numeric_secret", "string_secret"],
            },
        },
    }


def _dictionary() -> dict:
    return {
        "_documents": ["classified-document-one", "classified-document-two"],
        "_var_sets": {
            "classified-set-a": ["numeric_secret", "string_secret"],
            "classified-set-b": ["string_secret"],
        },
        "_print_format_tuples": {
            "numeric_secret": (5, 8, 1), "string_secret": (1, 16, 0),
        },
        "_write_format_tuples": {
            "numeric_secret": (3, 12, 3), "string_secret": (1, 16, 0),
        },
        "file_attributes": {
            "classified-scalar": "classified-value",
            "classified-array": ["first-secret", "second-secret"],
        },
        "var_attributes": {
            "numeric_secret": {
                "classified-origin": "classified-source",
                "classified-array": ["first-secret", "second-secret"],
            },
        },
    }


def _install_observations(
    monkeypatch, *, source_frame: pd.DataFrame, exported_frame: pd.DataFrame,
    source_metadata: dict | None = None, exported_metadata: dict | None = None,
    source_dictionary: dict | None = None, exported_dictionary: dict | None = None,
) -> None:
    frames = {"source.sav": source_frame, "exported.sav": exported_frame}
    metadata = {
        "source.sav": source_metadata or _metadata(),
        "exported.sav": exported_metadata or _metadata(),
    }
    dictionaries = {
        "source.sav": source_dictionary or _dictionary(),
        "exported.sav": exported_dictionary or _dictionary(),
    }

    def read_sav(path: str, **_options):
        name = Path(path).name
        return frames[name].copy(), copy.deepcopy(metadata[name])

    def read_dictionary(path: Path):
        dictionary = copy.deepcopy(dictionaries[path.name])
        loss_report = {}
        if dictionary.get("_documents") is None:
            loss_report["documents-unreadable"] = {"code": "documents-unreadable"}
        if dictionary.get("_var_sets") is None:
            loss_report["variable-sets-unobservable"] = {
                "code": "variable-sets-unobservable",
            }
        return dictionary, loss_report

    monkeypatch.setattr(semantic_module.pyspssio, "read_sav", read_sav)
    monkeypatch.setattr(semantic_module, "_dictionary", read_dictionary)


def _frame(numeric_values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "numeric_secret": numeric_values,
        "string_secret": ["classified-value", "", "classified-tail"][:len(numeric_values)],
    })


def test_public_comparison_treats_nan_payloads_as_system_missing_and_redacts_values(
    monkeypatch,
) -> None:
    source_nan = struct.unpack(">d", bytes.fromhex("7ff8000000000001"))[0]
    exported_nan = struct.unpack(">d", bytes.fromhex("7ff8000000001234"))[0]
    _install_observations(
        monkeypatch,
        source_frame=_frame([-0.0, source_nan, 2.5]),
        exported_frame=_frame([-0.0, exported_nan, 2.5]),
    )

    result = openstatspec.compare_sav_semantics("source.sav", "exported.sav")

    assert result["equivalent"] is True
    assert result["differences"] == []
    assert result["counts"]["source"] == {
        "cases": 3, "variables": 2, "numeric_variables": 1, "string_variables": 1,
    }
    assert result["components"]["numeric_nonmissing_binary64"]["source_count"] == 2
    assert result["components"]["numeric_system_missing_mask"]["source_count"] == 3
    serialized_result = repr(result)
    for secret in (
        "classified-value", "classified-file-label", "numeric_secret",
        "classified-document-one", "classified-set-a", "classified-one",
    ):
        assert secret not in serialized_result
    for component in result["components"].values():
        assert set(component) == {"status", "source_count", "exported_count"}

    receipt = openstatspec.compare_sav_semantics(
        "source.sav", "exported.sav", include_digests=True,
    )
    for component in receipt["components"].values():
        assert set(component) == {
            "status", "source_count", "exported_count", "source_sha256",
            "exported_sha256",
        }
        assert len(component["source_sha256"]) == 64
        assert len(component["exported_sha256"]) == 64


def test_public_comparison_distinguishes_signed_zero_bit_patterns(monkeypatch) -> None:
    _install_observations(
        monkeypatch,
        source_frame=_frame([-0.0]),
        exported_frame=_frame([0.0]),
    )

    result = openstatspec.compare_sav_semantics("source.sav", "exported.sav")

    assert result["equivalent"] is False
    assert result["differences"] == ["case_order", "numeric_nonmissing_binary64"]
    assert result["components"]["numeric_system_missing_mask"]["status"] == "equal"
    assert result["components"]["numeric_nonmissing_binary64"]["status"] == "different"


def test_public_comparison_covers_ordered_dictionary_semantics(monkeypatch) -> None:
    source_metadata = _metadata()
    exported_metadata = copy.deepcopy(source_metadata)
    exported_metadata["var_value_labels"]["numeric_secret"] = {
        2.0: "classified-two", 1.0: "classified-one",
    }
    exported_metadata["var_missing_values"]["numeric_secret"]["values"].reverse()
    exported_metadata["mrsets"]["$classified"]["variable_list"].reverse()
    source_dictionary = _dictionary()
    exported_dictionary = copy.deepcopy(source_dictionary)
    exported_dictionary["_documents"].reverse()
    exported_dictionary["_var_sets"]["classified-set-a"].reverse()
    exported_dictionary["_print_format_tuples"]["numeric_secret"] = (5, 9, 1)
    exported_dictionary["_write_format_tuples"]["numeric_secret"] = (3, 13, 3)
    exported_dictionary["file_attributes"]["classified-array"].reverse()
    exported_dictionary["var_attributes"]["numeric_secret"]["classified-array"].reverse()
    _install_observations(
        monkeypatch,
        source_frame=_frame([1.0]), exported_frame=_frame([1.0]),
        source_metadata=source_metadata, exported_metadata=exported_metadata,
        source_dictionary=source_dictionary, exported_dictionary=exported_dictionary,
    )

    result = openstatspec.compare_sav_semantics("source.sav", "exported.sav")

    assert set(result["differences"]) == {
        "documents", "file_attributes", "print_formats", "write_formats",
        "ordered_value_labels", "missing_rules", "variable_attributes",
        "variable_sets", "multiple_response_sets",
    }


def test_unobservable_dictionary_components_fail_closed_without_error_text(
    monkeypatch,
) -> None:
    unreadable = _dictionary()
    unreadable["_documents"] = None
    unreadable["_documents_error"] = {
        "type": "SyntheticReaderError",
        "code": None,
        "phase": "read_sav_documents",
        "message_sha256": "a" * 64,
    }
    unreadable["_var_sets"] = None
    unreadable["_var_sets_error"] = {
        "type": "SyntheticBridgeError",
        "code": None,
        "phase": "read_sav_variable_sets",
        "message_sha256": "b" * 64,
    }
    _install_observations(
        monkeypatch,
        source_frame=_frame([1.0]), exported_frame=_frame([1.0]),
        source_dictionary=unreadable, exported_dictionary=unreadable,
    )

    result = openstatspec.compare_sav_semantics("source.sav", "exported.sav")

    assert result["components"]["adapter_observability"]["status"] == "unavailable"
    assert result["equivalent"] is False
    assert result["components"]["documents"]["status"] == "unavailable"
    assert result["components"]["variable_sets"]["status"] == "unavailable"
    assert "a" * 64 not in repr(result)
    assert "b" * 64 not in repr(result)


def test_missing_source_encoding_fails_closed(monkeypatch) -> None:
    metadata = _metadata()
    metadata["encoding"] = None
    _install_observations(
        monkeypatch,
        source_frame=_frame([1.0]), exported_frame=_frame([1.0]),
        source_metadata=metadata, exported_metadata=metadata,
    )

    result = openstatspec.compare_sav_semantics("source.sav", "exported.sav")

    assert result["equivalent"] is False
    assert result["components"]["source_encoding"]["status"] == "unavailable"


def test_variable_order_is_reported_without_reclassifying_equal_properties(monkeypatch) -> None:
    source = _frame([1.0])
    exported = source[["string_secret", "numeric_secret"]]
    _install_observations(
        monkeypatch, source_frame=source, exported_frame=exported,
    )

    result = openstatspec.compare_sav_semantics("source.sav", "exported.sav")

    assert result["differences"] == ["variable_order"]
    assert result["components"]["variable_labels"]["status"] == "equal"
    assert result["components"]["numeric_nonmissing_binary64"]["status"] == "equal"


def test_public_comparison_covers_header_and_variable_dictionary(monkeypatch) -> None:
    source_metadata = _metadata()
    exported_metadata = copy.deepcopy(source_metadata)
    exported_metadata["encoding"] = "CP1252"
    exported_metadata["file_label"] = "changed-label"
    exported_metadata["case_weight_var"] = "string_secret"
    exported_metadata["var_types"]["string_secret"] = 32
    exported_metadata["var_labels"]["numeric_secret"] = "changed-variable-label"
    exported_metadata["var_measure_levels"]["numeric_secret"] = "ordinal"
    exported_metadata["var_roles"]["numeric_secret"] = "input"
    exported_metadata["var_alignments"]["numeric_secret"] = "left"
    exported_metadata["var_column_widths"]["numeric_secret"] = 12
    exported_metadata["var_compat_names"]["numeric_secret"] = "CHANGED"
    _install_observations(
        monkeypatch,
        source_frame=_frame([1.0]), exported_frame=_frame([1.0]),
        source_metadata=source_metadata, exported_metadata=exported_metadata,
    )

    result = openstatspec.compare_sav_semantics("source.sav", "exported.sav")

    assert set(result["differences"]) == {
        "source_encoding", "file_label", "case_weight_variable", "variable_types",
        "variable_labels", "measurement_levels", "variable_roles",
        "variable_alignments", "display_widths", "compatible_names",
    }
