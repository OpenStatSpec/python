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
        database_url=database, dataset_id="dictionary-gap", destination=tmp_path / "output.sav", allow_loss=["unobservable-source-dictionary-features", "multiple-response-sets-not-exported", "variable-alignment-not-exported"]
    )
    import sqlite3
    connection = sqlite3.connect(tmp_path / "dataset.sqlite")
    assert connection.execute("select set_name, member_ordinal, variable_name from multiple_response_set_catalog").fetchall() == [("set1", 1, None)]
    assert {event["code"] for event in result["loss_report"]} == {
        "multiple-response-sets-not-exported", "variable-alignment-not-exported", "unobservable-source-dictionary-features"
    }


def test_persisted_import_fidelity_events_require_consent_after_reopen(tmp_path) -> None:
    import pandas as pd
    import pyreadstat
    import pytest

    from openstatspec.core import UnsupportedOperationError

    source = tmp_path / "source.sav"
    database_path = tmp_path / "persisted.sqlite"
    database = f"sqlite:///{database_path}"
    blocked = tmp_path / "blocked.sav"
    approved = tmp_path / "approved.sav"
    pyreadstat.write_sav(pd.DataFrame({"answer": [1.0]}), source)

    imported = __import__("openstatspec").import_sav(
        source, database_url=database, dataset_id="persisted"
    )
    assert {event["code"] for event in imported["loss_report"]} == {
        "unobservable-source-dictionary-features"
    }

    import sqlite3
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select dataset_id, code from fidelity_event_catalog"
    ).fetchall() == [("persisted", "unobservable-source-dictionary-features")]
    connection.close()

    with pytest.raises(
        UnsupportedOperationError, match="unobservable-source-dictionary-features"
    ):
        __import__("openstatspec").export_sav(
            database_url=database, dataset_id="persisted", destination=blocked
        )
    assert not blocked.exists()

    exported = __import__("openstatspec").export_sav(
        database_url=database,
        dataset_id="persisted",
        destination=approved,
        allow_loss=["unobservable-source-dictionary-features"],
    )
    assert approved.exists()
    assert {event["code"] for event in exported["loss_report"]} == {
        "unobservable-source-dictionary-features"
    }


def test_loss_allowed_export_persists_accepted_diagnostics(tmp_path) -> None:
    import sqlite3

    database_path = tmp_path / "accepted-loss.sqlite"
    database = f"sqlite:///{database_path}"
    create_wide_dataset(
        database_url=database, dataset_id="accepted", source_name="fixture.sav", source_format="SAV",
        rows=[{"answer": 1.0}], variables=[{
            "ordinal": 1, "source_name": "answer", "physical_name": "answer",
            "storage_kind": "numeric", "string_width": None, "label": "", "format": "F8.0",
            "measure": "nominal", "alignment": None, "display_width": 8,
            "value_labels": "{}", "missing_ranges": "[]",
        }],
    )
    result = export_sav(
        database_url=database, dataset_id="accepted", destination=tmp_path / "accepted.sav",
        allow_loss=["unobservable-source-dictionary-features"],
    )

    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select direction, status, dataset_id from operation_catalog where operation_id = ?",
        (result["operation_id"],),
    ).fetchone() == ("export", "succeeded", "accepted")
    direction, severity, code, details = connection.execute(
        "select direction, severity, code, details from fidelity_event_catalog where operation_id = ?",
        (result["operation_id"],),
    ).fetchone()
    assert (direction, severity, code) == ("export", "warning", "unobservable-source-dictionary-features")
    assert '"accepted_by_user": true' in details


def test_non_utf8_source_encoding_is_explicit_export_loss(tmp_path) -> None:
    """The writer has no output-encoding argument, so it must fail closed."""
    import pyreadstat
    import pytest

    from openstatspec.api import capability_matrix
    from openstatspec.core import UnsupportedOperationError

    database_path = tmp_path / "legacy-encoding.sqlite"
    database = f"sqlite:///{database_path}"
    destination = tmp_path / "legacy-encoding.sav"
    create_wide_dataset(
        database_url=database, dataset_id="legacy-encoding", source_name="legacy.sav",
        source_format="SAV", source_encoding="WINDOWS-1252",
        rows=[{"name": "Muller"}], variables=[{
            "ordinal": 1, "source_name": "name", "physical_name": "name",
            "storage_kind": "string", "string_width": 8, "label": "",
            "format": "A8", "measure": "nominal", "alignment": None,
            "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
        }],
    )

    with pytest.raises(UnsupportedOperationError, match="source-encoding-not-preserved"):
        export_sav(
            database_url=database, dataset_id="legacy-encoding", destination=destination,
            allow_loss=["unobservable-source-dictionary-features"],
        )
    assert not destination.exists()

    exported = export_sav(
        database_url=database, dataset_id="legacy-encoding", destination=destination,
        allow_loss=[
            "unobservable-source-dictionary-features",
            "source-encoding-not-preserved",
        ],
    )
    assert {event["code"] for event in exported["loss_report"]} == {
        "source-encoding-not-preserved",
        "unobservable-source-dictionary-features",
    }
    _, metadata = pyreadstat.read_sav(destination, metadataonly=True)
    assert metadata.file_encoding == "UTF-8"
    assert capability_matrix()["spss"]["print_format"] == "supported"
    assert capability_matrix()["spss"]["write_format"] == "unobservable"
