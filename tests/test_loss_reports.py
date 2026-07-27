import sqlite3

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.wide import create_wide_dataset


_REQUIRED_ENGINE_LOSS = ["file-label-and-documents-unobservable", "separate-write-format-unobservable"]


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

    with pytest.raises(UnsupportedOperationError, match="file-label-and-documents-unobservable"):
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