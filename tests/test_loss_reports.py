import json
import sqlite3

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.wide import create_wide_dataset
from openstatspec.spss.raw_dictionary import write_compatible_names
from openstatspec.spss import sav as sav_module


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
    assert {row[0] for row in connection.execute("select code from fidelity_event_catalog")} == set(_REQUIRED_ENGINE_LOSS)
    import_details = json.loads(connection.execute("select details from operation_catalog order by created_at limit 1").fetchone()[0])
    assert import_details["engine"]["package"] == "openstatspec-pyspssio"
    assert import_details["engine"]["pinned_commit"] == "e069adf"

    openstatspec.export_sav(database_url=database, dataset_id="persisted", destination=blocked)
    assert blocked.exists()

    exported = openstatspec.export_sav(database_url=database, dataset_id="persisted", destination=approved, allow_loss=_REQUIRED_ENGINE_LOSS)
    assert approved.exists()
    export_details = json.loads(connection.execute("select details from operation_catalog where operation_id = ?", (exported["operation_id"],)).fetchone()[0])
    assert export_details["engine"]["installed_version"] == pyspssio.__version__
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
    rows = connection.execute("select direction, severity, code, details from fidelity_event_catalog where operation_id = ? order by code", (result["operation_id"],)).fetchall()
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

@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_compatible_variable_name_round_trips_from_current_sql_catalog(tmp_path, suffix: str) -> None:
    source = tmp_path / f"compat-source{suffix}"
    database_path = tmp_path / f"compat-{suffix[1:]}.sqlite"
    database = f"sqlite:///{database_path}"
    destination = tmp_path / f"compat-destination{suffix}"
    source_name = "long_variable_name"

    pyspssio.write_sav(str(source), pd.DataFrame({source_name: [1.0]}))
    write_compatible_names(source, {source_name: "ANSWER"}, encoding="UTF-8")
    assert pyspssio.read_metadata(str(source))["var_compat_names"][source_name] == "ANSWER"

    imported = openstatspec.import_sav(source, database_url=database, dataset_id=f"compat-{suffix[1:]}")
    assert imported.diagnostics == ()
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select compat_name from variable_catalog where dataset_id = ? and source_name = ?",
        (f"compat-{suffix[1:]}", source_name),
    ).fetchone() == ("ANSWER",)

    # The normalized catalog is authoritative: a legitimate 8-byte short name
    # edited there must be written into both SAV dictionary locations.
    connection.execute(
        "update variable_catalog set compat_name = ? where dataset_id = ? and source_name = ?",
        ("EXAMPLE", f"compat-{suffix[1:]}", source_name),
    )
    connection.commit()
    result = openstatspec.export_sav(
        database_url=database, dataset_id=f"compat-{suffix[1:]}", destination=destination,
    )
    assert result.diagnostics == ()
    metadata = pyspssio.read_metadata(str(destination))
    assert metadata["var_compat_names"][source_name] == "EXAMPLE"
    assert pyspssio.read_sav(str(destination))[0][source_name].tolist() == [1.0]
