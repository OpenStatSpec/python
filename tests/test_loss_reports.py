import sqlite3

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.wide import create_wide_dataset
from openstatspec.spss import sav as sav_module
from openstatspec.spss.raw_dictionary import write_compatible_names


_REQUIRED_ENGINE_LOSS = []


def test_persisted_import_fidelity_events_require_consent_after_reopen(tmp_path) -> None:
    source = tmp_path / "source.sav"
    database_path = tmp_path / "persisted.sqlite"
    database = f"sqlite:///{database_path}"
    blocked = tmp_path / "blocked.sav"
    approved = tmp_path / "approved.sav"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))

    imported = openstatspec.import_sav(source, database_url=database, dataset_id="persisted")
    assert {diagnostic.code for diagnostic in imported.diagnostics} == set(_REQUIRED_ENGINE_LOSS)
    connection = sqlite3.connect(database_path)
    assert {
        row[0] for row in connection.execute(
            "select event_code from fidelity_event where severity != 'info'"
        )
    } == set(_REQUIRED_ENGINE_LOSS)
    assert connection.execute(
        "select operation_kind, status from operation order by started_at limit 1"
    ).fetchone() == ("import", "succeeded")

    openstatspec.export_sav(database_url=database, dataset_id="persisted", destination=blocked)
    assert blocked.exists()

    exported = openstatspec.export_sav(database_url=database, dataset_id="persisted", destination=approved, allow_loss=_REQUIRED_ENGINE_LOSS)
    assert approved.exists()
    assert connection.execute(
        "select operation_kind, status from operation where operation_id = ?", (exported["operation_id"],)
    ).fetchone() == ("export", "succeeded")
    assert {diagnostic.code for diagnostic in exported.diagnostics} == set(_REQUIRED_ENGINE_LOSS)


def test_loss_allowed_export_persists_accepted_diagnostics(tmp_path) -> None:
    database_path = tmp_path / "accepted-loss.sqlite"
    database = f"sqlite:///{database_path}"
    source = tmp_path / "source.sav"
    destination = tmp_path / "accepted.sav"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    openstatspec.import_sav(source, database_url=database, dataset_id="accepted")
    result = openstatspec.export_sav(database_url=database, dataset_id="accepted", destination=destination, allow_loss=_REQUIRED_ENGINE_LOSS)

    connection = sqlite3.connect(database_path)
    rows = connection.execute("select direction, severity, event_code, detail_json from fidelity_event where operation_id = ? order by event_code", (result["operation_id"],)).fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [("export", "warning", code) for code in _REQUIRED_ENGINE_LOSS]
    assert all('"accepted_by_user": true' in row[3] for row in rows)


def test_non_utf8_source_encoding_is_explicit_export_loss(tmp_path) -> None:
    database_path = tmp_path / "legacy-encoding.sqlite"
    database = f"sqlite:///{database_path}"
    destination = tmp_path / "legacy-encoding.sav"
    create_wide_dataset(
        database_url=database, dataset_id="legacy-encoding", source_name="legacy.sav",
        source_format="SAV", source_encoding="WINDOWS-1252", rows=[{"name": "Muller"}],
        variables=[{
            "ordinal": 1, "source_name": "name", "physical_name": "name", "storage_kind": "string",
            "string_width": 8, "label": "", "format": "A8", "measure": "nominal", "role": None,
            "alignment": None, "display_width": 8, "attributes": "{}", "compat_name": None,
            "value_labels": "{}", "missing_ranges": "[]",
        }],
    )
    with pytest.raises(UnsupportedOperationError, match="source-encoding-not-preserved"):
        openstatspec.export_sav(database_url=database, dataset_id="legacy-encoding", destination=destination, allow_loss=[])
    exported = openstatspec.export_sav(database_url=database, dataset_id="legacy-encoding", destination=destination, allow_loss=["source-encoding-not-preserved"])
    assert {diagnostic.code for diagnostic in exported.diagnostics} == {"source-encoding-not-preserved"}
    metadata = pyspssio.read_metadata(str(destination))
    assert metadata["encoding"] == "UTF-8"


def test_explicit_legacy_locale_selects_the_single_engine_route(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "legacy-locale.sqlite"
    database = f"sqlite:///{database_path}"
    destination = tmp_path / "legacy-locale.sav"
    create_wide_dataset(
        database_url=database, dataset_id="legacy-locale", source_name="legacy.sav",
        source_format="SAV", source_encoding="WINDOWS-1252", rows=[{"name": "Muller"}],
        variables=[{
            "ordinal": 1, "source_name": "name", "physical_name": "name", "storage_kind": "string",
            "string_width": 8, "label": "", "format": "A8", "measure": "nominal", "role": None,
            "alignment": None, "display_width": 8, "attributes": "{}", "compat_name": None,
            "value_labels": "{}", "missing_ranges": "[]",
        }],
        fidelity_events=[{
            "code": "source-encoding-not-preserved",
            "detail": "Legacy encoding needs a locale.",
            "details": {"source_encoding": "WINDOWS-1252"},
        }],
    )
    observed = {}

    def writer(destination_path, frame, dataset, variables, *, legacy_locale=None):
        destination_path.touch()
        observed["locale"] = legacy_locale
        observed["encoding"] = dataset["source_encoding"]
        observed["values"] = frame["name"].tolist()

    monkeypatch.setattr(sav_module, "_write_with_dictionary_bridge", writer)
    result = openstatspec.export_sav(
        database_url=database, dataset_id="legacy-locale", destination=destination,
        legacy_locale="en_US.cp1252",
    )
    assert result.diagnostics == ()
    assert observed == {
        "locale": "en_US.cp1252", "encoding": "WINDOWS-1252", "values": ["Muller"],
    }


def test_legacy_locale_must_emit_the_exact_source_encoding() -> None:
    sav_module._require_matching_legacy_encoding("WINDOWS-1252", "CP1252", True)
    with pytest.raises(UnsupportedOperationError, match="instead of required"):
        sav_module._require_matching_legacy_encoding("WINDOWS-1252", "UTF-8", True)


def test_compatible_variable_names_are_explicit_imported_loss(tmp_path) -> None:
    source = tmp_path / "compatible-name.sav"
    destination = tmp_path / "compatible-name-out.sav"
    database = f"sqlite:///{tmp_path / 'compatible-name.sqlite'}"
    source_name = "long_variable_name"
    loss_code = "compatible-variable-names-not-preserved"

    pyspssio.write_sav(str(source), pd.DataFrame({source_name: [1.0]}))
    write_compatible_names(source, {source_name: "ANSWER"}, encoding="UTF-8")

    imported = openstatspec.import_sav(
        source, database_url=database, dataset_id="compatible-name",
    )
    assert {diagnostic.code for diagnostic in imported.diagnostics} == {loss_code}
    assert imported.diagnostics[0].details == {"variable_names": [source_name]}

    with pytest.raises(UnsupportedOperationError, match=loss_code):
        openstatspec.export_sav(
            database_url=database, dataset_id="compatible-name",
            destination=destination,
        )

    exported = openstatspec.export_sav(
        database_url=database, dataset_id="compatible-name",
        destination=destination, allow_loss=[loss_code],
    )
    assert {diagnostic.code for diagnostic in exported.diagnostics} == {loss_code}
    assert (
        pyspssio.read_metadata(str(destination))["var_compat_names"][source_name]
        != "ANSWER"
    )
