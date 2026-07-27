import json
import sqlite3

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.wide import create_wide_dataset


_REQUIRED_ENGINE_LOSS = ["documents-unobservable"]


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

    with pytest.raises(UnsupportedOperationError, match="documents-unobservable"):
        openstatspec.export_sav(database_url=database, dataset_id="persisted", destination=blocked)
    assert not blocked.exists()

    exported = openstatspec.export_sav(database_url=database, dataset_id="persisted", destination=approved, allow_loss=_REQUIRED_ENGINE_LOSS)
    assert approved.exists()
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

@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_compatible_variable_name_requires_explicit_audited_export_loss(tmp_path, suffix: str) -> None:
    """A long SPSS name exposes a legacy compatible name the writer cannot set."""
    source = tmp_path / f"compat-source{suffix}"
    database_path = tmp_path / f"compat-{suffix[1:]}.sqlite"
    database = f"sqlite:///{database_path}"
    blocked = tmp_path / f"compat-blocked{suffix}"
    approved = tmp_path / f"compat-approved{suffix}"
    source_name = "long_variable_name"

    pyspssio.write_sav(str(source), pd.DataFrame({source_name: [1.0]}))
    assert pyspssio.read_metadata(str(source))["var_compat_names"][source_name] != source_name

    imported = openstatspec.import_sav(source, database_url=database, dataset_id=f"compat-{suffix[1:]}")
    diagnostic = next(
        item for item in imported.diagnostics
        if item.code == "compatible-variable-name-not-exportable"
    )
    assert diagnostic.details == {
        "source_name": source_name,
        "compatible_name": "LONG_VAR",
        "physical_name": source_name,
    }

    # Export must also assess the current normalized SQL catalog. Clear the
    # import event and alter the catalog value to prove that no persisted
    # diagnostic can mask an unguarded re-export or silent renaming path.
    connection = sqlite3.connect(database_path)
    connection.execute(
        "delete from fidelity_event_catalog where dataset_id = ? and code = ?",
        (f"compat-{suffix[1:]}", "compatible-variable-name-not-exportable"),
    )
    connection.execute(
        "update variable_catalog set compat_name = ? where dataset_id = ? and source_name = ?",
        ("CUSTOM_NAME", f"compat-{suffix[1:]}", source_name),
    )
    connection.commit()
    expected_catalog_detail = {
        "source_name": source_name,
        "compatible_name": "CUSTOM_NAME",
        "physical_name": source_name,
    }

    with pytest.raises(UnsupportedOperationError, match="compatible-variable-name-not-exportable"):
        openstatspec.export_sav(
            database_url=database, dataset_id=f"compat-{suffix[1:]}", destination=blocked,
            allow_loss=_REQUIRED_ENGINE_LOSS,
        )
    assert not blocked.exists()

    result = openstatspec.export_sav(
        database_url=database, dataset_id=f"compat-{suffix[1:]}", destination=approved,
        allow_loss=[*_REQUIRED_ENGINE_LOSS, "compatible-variable-name-not-exportable"],
    )
    accepted = next(item for item in result.diagnostics if item.code == "compatible-variable-name-not-exportable")
    assert accepted.details == expected_catalog_detail
    persisted = connection.execute(
        "select details from fidelity_event_catalog where operation_id = ? and code = ?",
        (result["operation_id"], "compatible-variable-name-not-exportable"),
    ).fetchone()[0]
    assert json.loads(persisted) == {**expected_catalog_detail, "accepted_by_user": True}
