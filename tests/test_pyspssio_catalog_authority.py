import json
import sqlite3

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.spss import sav as sav_module
from openstatspec.sql.wide import create_wide_dataset, read_fidelity_events


_REQUIRED_ENGINE_LOSS = []


def test_import_retains_observed_variable_sets_as_source_extension(tmp_path, monkeypatch) -> None:
    source = tmp_path / "variables.sav"
    database_path = tmp_path / "variables.sqlite"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    original = sav_module._dictionary

    def dictionary_with_variable_set(path):
        metadata, _ = original(path)
        metadata["_var_sets"] = {"Analysis": ["answer"]}
        return metadata, sav_module._engine_loss_report(metadata)

    monkeypatch.setattr(sav_module, "_dictionary", dictionary_with_variable_set)
    openstatspec.initialize_catalog(database_url=f"sqlite:///{database_path}")
    imported = openstatspec.import_sav(source, database_url=f"sqlite:///{database_path}", dataset_id="variables")
    assert {diagnostic.code for diagnostic in imported.diagnostics} == set()
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select extension_key, payload from source_extension_catalog where dataset_id = 'variables'"
    ).fetchone() == ("spss.variable_sets", json.dumps({"Analysis": ["answer"]}))


def test_normalized_mr_catalog_is_authoritative_for_export(tmp_path) -> None:
    source = tmp_path / "mr.sav"
    database_path = tmp_path / "mr.sqlite"
    database = f"sqlite:///{database_path}"
    destination = tmp_path / "mr-out.sav"
    frame = pd.DataFrame({
        "md_a": [1.0, 0.0], "md_b": [0.0, 1.0],
        "mc_a": [1.0, 2.0], "mc_b": [2.0, 1.0],
        "ex_a": [1.0, 0.0], "ex_b": [0.0, 1.0],
    })
    pyspssio.write_sav(
        str(source), frame,
        metadata={
            "var_labels": {"ex_a": "Extended A", "ex_b": "Extended B"},
            "mrsets": {
                "$md": {"label": "MD source", "counted_value": 1, "variable_list": ["md_a", "md_b"]},
                "$mc": {"label": "MC source", "variable_list": ["mc_a", "mc_b"]},
                "$extended": {"counted_value": 1, "use_category_labels": True, "use_first_var_label": True, "variable_list": ["ex_a", "ex_b"]},
            },
        },
    )
    openstatspec.initialize_catalog(database_url=database)
    openstatspec.import_sav(source, database_url=database, dataset_id="mr")
    connection = sqlite3.connect(database_path)
    rows = connection.execute(
        "select set_name, kind, is_dichotomy, use_category_labels, use_first_var_label, counted_value_type, counted_numeric, variable_name "
        "from multiple_response_set_catalog order by set_name, member_ordinal"
    ).fetchall()
    assert rows == [
        ("$extended", "MD", 1, 1, 1, "numeric", 1.0, "ex_a"),
        ("$extended", "MD", 1, 1, 1, "numeric", 1.0, "ex_b"),
        ("$mc", "MC", 0, 0, 0, None, None, "mc_a"),
        ("$mc", "MC", 0, 0, 0, None, None, "mc_b"),
        ("$md", "MD", 1, 0, 0, "numeric", 1.0, "md_a"),
        ("$md", "MD", 1, 0, 0, "numeric", 1.0, "md_b"),
    ]
    # The JSON is only legacy compatibility now; normalized rows must drive the writer.
    connection.execute("update dataset_catalog set multiple_response_sets = '{}' where dataset_id = 'mr'")
    connection.execute("update multiple_response_set_catalog set label = 'MD catalog' where dataset_id = 'mr' and set_name = '$md'")
    connection.commit()
    openstatspec.export_sav(database_url=database, dataset_id="mr", destination=destination, allow_loss=_REQUIRED_ENGINE_LOSS)
    exported = pyspssio.read_metadata(str(destination))["mrsets"]
    assert exported["$md"]["label"] == "MD catalog"
    assert exported["$md"]["counted_value"] == 1
    assert exported["$mc"]["is_dichotomy"] is False
    assert exported["$extended"]["use_category_labels"] is True
    assert exported["$extended"]["use_first_var_label"] is True


def test_fidelity_event_details_survive_reopening_database(tmp_path) -> None:
    database = f"sqlite:///{tmp_path / 'events.sqlite'}"
    openstatspec.initialize_catalog(database_url=database)
    create_wide_dataset(
        database_url=database, dataset_id="events", source_name="events.sav", source_format="SAV",
        rows=[{"answer": 1.0}],
        variables=[{
            "ordinal": 1, "source_name": "answer", "physical_name": "answer", "storage_kind": "numeric",
            "readstat_storage_type": "pyspssio:numeric", "string_width": None, "label": "", "format": "F8",
            "measure": "scale", "role": "input", "alignment": "right", "display_width": 8,
            "attributes": "{}", "compat_name": "ANSWER", "value_labels": "{}", "missing_ranges": "[]",
        }],
        fidelity_events=[{"code": "variable-sets-not-exportable", "detail": "fixture", "details": {"set_count": 2}}],
    )
    assert read_fidelity_events(database_url=database, dataset_id="events") == (
        {"code": "variable-sets-not-exportable", "detail": "fixture", "details": {"set_count": 2}},
    )