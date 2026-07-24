from openstatspec import export_sav
from openstatspec.sql.wide import create_wide_dataset


def test_export_reports_unwritable_spss_dictionary_features(tmp_path) -> None:
    database = f"sqlite:///{tmp_path / 'dataset.sqlite'}"
    create_wide_dataset(
        database_url=database,
        dataset_id="dictionary-gap",
        source_name="fixture.sav",
        source_format="SAV",
        rows=[{"answer": 1.0}],
        variables=[{
            "ordinal": 1, "source_name": "answer", "physical_name": "answer",
            "storage_kind": "numeric", "string_width": None, "label": "",
            "format": "F8.0", "measure": "nominal", "alignment": "left",
            "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
        }],
        multiple_response_sets='{"set1": {"label": "example"}}',
    )

    result = export_sav(
        database_url=database, dataset_id="dictionary-gap", destination=tmp_path / "output.sav"
    )
    import sqlite3
    connection = sqlite3.connect(tmp_path / "dataset.sqlite")
    assert connection.execute("select set_name, member_ordinal, variable_name from multiple_response_set_catalog").fetchall() == [("set1", 1, None)]
    assert {event["code"] for event in result["loss_report"]} == {
        "multiple-response-sets-not-exported", "variable-alignment-not-exported"
    }