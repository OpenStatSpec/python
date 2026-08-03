import json
import sqlite3
from pathlib import Path

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.wide import create_wide_dataset, initialize_wide_catalog
from openstatspec.spss.raw_dictionary import RawDictionaryError, write_compatible_names
from openstatspec.spss import sav as sav_module


_REQUIRED_ENGINE_LOSS = []


def test_persisted_import_fidelity_events_require_consent_after_reopen(tmp_path) -> None:
    source = tmp_path / "source.sav"
    database_path = tmp_path / "persisted.sqlite"
    database = f"sqlite:///{database_path}"
    blocked = tmp_path / "blocked.sav"
    approved = tmp_path / "approved.sav"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))

    initialize_wide_catalog(database_url=database)
    imported = openstatspec.import_sav(source, database_url=database, dataset_id="persisted")
    assert {diagnostic.code for diagnostic in imported.diagnostics} == set(_REQUIRED_ENGINE_LOSS)
    connection = sqlite3.connect(database_path)
    assert {row[0] for row in connection.execute("select code from fidelity_event_catalog")} == set(_REQUIRED_ENGINE_LOSS)
    import_details = json.loads(connection.execute("select details from operation_catalog order by created_at limit 1").fetchone()[0])
    assert import_details["engine"]["package"] == "openstatspec-pyspssio"
    assert import_details["engine"]["pinned_commit"] == "e069adf33c70bcd9e8e6ee495106479463a84fa2"

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
    initialize_wide_catalog(database_url=database)
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
    initialize_wide_catalog(database_url=database)
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
    initialize_wide_catalog(database_url=database)
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


def test_dictionary_failures_use_path_free_error_identities(
    tmp_path, monkeypatch,
) -> None:
    source = tmp_path / "confidential-source.sav"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))

    document_secret = f"classified document failure at {tmp_path}"
    monkeypatch.setattr(
        sav_module,
        "read_document_lines",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RawDictionaryError(document_secret)
        ),
    )
    document_metadata, document_loss = sav_module._dictionary(source)
    document_error = document_metadata["_documents_error"]
    assert set(document_error) == {"type", "code", "phase", "message_sha256"}
    assert document_error["phase"] == "read_sav_documents"
    assert document_loss["documents-unreadable"]["details"]["engine_error"] == document_error
    assert document_secret not in repr((document_metadata, document_loss))

    variable_set_secret = f"classified variable-set failure at {tmp_path}"
    monkeypatch.setattr(
        sav_module,
        "format_tuples",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(variable_set_secret)
        ),
    )
    variable_metadata, variable_loss = sav_module._dictionary(source)
    variable_error = variable_metadata["_var_sets_error"]
    assert set(variable_error) == {"type", "code", "phase", "message_sha256"}
    assert variable_error["phase"] == "read_sav_variable_sets"
    assert variable_loss["variable-sets-unobservable"]["details"]["engine_error"] == variable_error
    assert variable_set_secret not in repr((variable_metadata, variable_loss))

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

    initialize_wide_catalog(database_url=database)
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


def test_dictionary_rewrite_failure_preserves_existing_destination_and_removes_stage(
    tmp_path, monkeypatch,
) -> None:
    source = tmp_path / "source.sav"
    destination = tmp_path / "partial.sav"
    database = f"sqlite:///{tmp_path / 'partial.sqlite'}"
    source_name = "long_variable_name"
    pyspssio.write_sav(str(source), pd.DataFrame({source_name: [1.0]}))
    write_compatible_names(source, {source_name: "ANSWER"}, encoding="UTF-8")
    initialize_wide_catalog(database_url=database)
    openstatspec.import_sav(source, database_url=database, dataset_id="partial")
    original_destination = b"existing destination"
    destination.write_bytes(original_destination)

    def fail_dictionary_rewrite(*_args, **_kwargs):
        raise RawDictionaryError("synthetic dictionary rewrite failure")

    monkeypatch.setattr(sav_module, "write_compatible_names", fail_dictionary_rewrite)
    with pytest.raises(RawDictionaryError, match="synthetic dictionary rewrite failure"):
        openstatspec.export_sav(
            database_url=database, dataset_id="partial", destination=destination,
        )

    assert destination.read_bytes() == original_destination
    assert list(tmp_path.glob(f".{destination.name}.*")) == []


def test_export_audit_failure_restores_previous_destination(tmp_path, monkeypatch) -> None:
    source = tmp_path / "audit-source.sav"
    destination = tmp_path / "audit-destination.sav"
    database = f"sqlite:///{tmp_path / 'audit.sqlite'}"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    initialize_wide_catalog(database_url=database)
    openstatspec.import_sav(source, database_url=database, dataset_id="audit")
    previous = b"previous destination bytes"
    destination.write_bytes(previous)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("synthetic export audit failure")

    monkeypatch.setattr(sav_module, "record_export_operation", fail_audit)
    with pytest.raises(RuntimeError, match="synthetic export audit failure"):
        openstatspec.export_sav(
            database_url=database, dataset_id="audit", destination=destination,
        )

    assert destination.read_bytes() == previous
    assert list(tmp_path.glob(f".{destination.name}.*")) == []


def test_export_restore_failure_persists_null_dataset_cleanup_audit(
    tmp_path, monkeypatch,
) -> None:
    source = tmp_path / "restore-source.sav"
    destination = tmp_path / "restore-destination.sav"
    database_path = tmp_path / "restore.sqlite"
    database = f"sqlite:///{database_path}"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    initialize_wide_catalog(database_url=database)
    openstatspec.import_sav(source, database_url=database, dataset_id="restore")
    destination.write_bytes(b"previous destination bytes")

    original_replace = sav_module.os.replace

    def fail_publish(source, target):
        if ".staging." in str(Path(source).parent):
            raise RuntimeError("synthetic publish failure")
        return original_replace(source, target)

    monkeypatch.setattr(sav_module.os, "replace", fail_publish)
    monkeypatch.setattr(
        sav_module, "_restore_export_destination",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic restore failure")),
    )

    with pytest.raises(sav_module.ExportRecoveryError) as error:
        openstatspec.export_sav(
            database_url=database, dataset_id="restore", destination=destination,
        )

    assert error.value.code == "cleanup_failed"
    assert error.value.details["audit_fault"] is None
    evidence = error.value.details["deterministic_recovery_evidence"]
    assert evidence["cleanup_failed_audit_persisted"] is True
    assert evidence["cleanup_failed_audit_operation_id"]
    backup = error.value.details["residual_object_inventory"]["backup"]
    assert set(backup) == {"role", "path_sha256"}
    assert backup["role"] == "durable_backup"
    assert error.value.details["residual_object_inventory"]["backup_exists"] is True
    backups = list(tmp_path.glob(f".{destination.name}.*.previous"))
    assert len(backups) == 1
    assert str(tmp_path) not in json.dumps(error.value.details, sort_keys=True)
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select status, dataset_id from operation_catalog "
        "where direction = 'export' order by created_at desc limit 1"
    ).fetchone() == ("failed", "restore")
    assert connection.execute(
        "select code, dataset_id from fidelity_event_catalog "
        "where direction = 'export' order by rowid desc limit 1"
    ).fetchone() == ("cleanup_failed", None)
    persisted_details = json.loads(connection.execute(
        "select details from fidelity_event_catalog "
        "where direction = 'export' order by rowid desc limit 1"
    ).fetchone()[0])
    assert set(persisted_details) == {
        "original_cause", "cleanup_fault",
        "residual_object_inventory", "deterministic_recovery_evidence",
    }
    connection.close()


def test_export_finalization_failure_restores_previous_destination_and_fails_audit(
    tmp_path, monkeypatch,
) -> None:
    source = tmp_path / "finalize-source.sav"
    destination = tmp_path / "finalize-destination.sav"
    database_path = tmp_path / "finalize.sqlite"
    database = f"sqlite:///{database_path}"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    initialize_wide_catalog(database_url=database)
    openstatspec.import_sav(source, database_url=database, dataset_id="finalize")
    previous = b"previous destination bytes"
    destination.write_bytes(previous)

    monkeypatch.setattr(
        sav_module, "finish_export_operation",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic finalization failure")
        ),
    )
    with pytest.raises(RuntimeError, match="synthetic finalization failure"):
        openstatspec.export_sav(
            database_url=database, dataset_id="finalize", destination=destination,
        )

    assert destination.read_bytes() == previous
    assert list(tmp_path.glob(f".{destination.name}.*")) == []
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select status, dataset_id from operation_catalog "
        "where direction = 'export' order by created_at desc limit 1"
    ).fetchone() == ("failed", "finalize")
    assert connection.execute(
        "select code, dataset_id from fidelity_event_catalog "
        "where direction = 'export' order by rowid desc limit 1"
    ).fetchone() == ("export_failed", "finalize")
    connection.close()


@pytest.mark.parametrize("had_previous", [False, True])
def test_staging_cleanup_failure_compensates_published_destination_and_fails_audit(
    tmp_path, monkeypatch, had_previous: bool,
) -> None:
    source = tmp_path / "cleanup-source.sav"
    destination = tmp_path / "cleanup-destination.sav"
    database_path = tmp_path / "cleanup.sqlite"
    database = f"sqlite:///{database_path}"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    initialize_wide_catalog(database_url=database)
    openstatspec.import_sav(source, database_url=database, dataset_id="cleanup")
    previous = b"previous destination bytes"
    if had_previous:
        destination.write_bytes(previous)

    real_temporary_directory = sav_module.TemporaryDirectory

    class CleanupFailureTemporaryDirectory:
        def __init__(self, *args, **kwargs):
            self._delegate = real_temporary_directory(*args, **kwargs)

        def __enter__(self):
            return self._delegate.__enter__()

        def __exit__(self, exc_type, exc_value, traceback):
            result = self._delegate.__exit__(exc_type, exc_value, traceback)
            if exc_type is None:
                raise RuntimeError("synthetic staging cleanup failure")
            return result

    monkeypatch.setattr(
        sav_module, "TemporaryDirectory", CleanupFailureTemporaryDirectory,
    )
    with pytest.raises(RuntimeError, match="synthetic staging cleanup failure"):
        openstatspec.export_sav(
            database_url=database, dataset_id="cleanup",
            destination=destination,
        )

    if had_previous:
        assert destination.read_bytes() == previous
    else:
        assert not destination.exists()
    assert list(tmp_path.glob(f".{destination.name}.*")) == []
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select status from operation_catalog "
        "where direction = 'export' order by created_at desc limit 1"
    ).fetchone() == ("failed",)
    assert connection.execute(
        "select code from fidelity_event_catalog "
        "where direction = 'export' order by rowid desc limit 1"
    ).fetchone() == ("export_failed",)
    details = connection.execute(
        "select details from operation_catalog "
        "where direction = 'export' order by created_at desc limit 1"
    ).fetchone()[0]
    assert str(tmp_path) not in details
    connection.close()


def test_export_finalization_commit_after_send_is_idempotent_success(
    tmp_path, monkeypatch,
) -> None:
    source = tmp_path / "commit-source.sav"
    destination = tmp_path / "commit-destination.sav"
    database_path = tmp_path / "commit.sqlite"
    database = f"sqlite:///{database_path}"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    initialize_wide_catalog(database_url=database)
    openstatspec.import_sav(source, database_url=database, dataset_id="commit")
    destination.write_bytes(b"previous destination bytes")
    real_finish = sav_module.finish_export_operation

    def commit_then_disconnect(**kwargs):
        real_finish(**kwargs)
        raise ConnectionError("synthetic disconnect after commit")

    monkeypatch.setattr(
        sav_module, "finish_export_operation", commit_then_disconnect,
    )
    result = openstatspec.export_sav(
        database_url=database, dataset_id="commit", destination=destination,
    )

    assert result["operation_id"]
    assert pyspssio.read_sav(str(destination))[0]["answer"].tolist() == [1.0]
    assert list(tmp_path.glob(f".{destination.name}.*")) == []
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select status from operation_catalog where operation_id = ?",
        (result["operation_id"],),
    ).fetchone() == ("succeeded",)
    assert connection.execute(
        "select status from operation where operation_id = ?",
        (result["operation_id"],),
    ).fetchone() == ("succeeded",)
    connection.close()


def test_export_finalization_mismatch_preserves_published_file_and_backup(
    tmp_path, monkeypatch,
) -> None:
    source = tmp_path / "mismatch-source.sav"
    destination = tmp_path / "mismatch-destination.sav"
    database_path = tmp_path / "mismatch.sqlite"
    database = f"sqlite:///{database_path}"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    initialize_wide_catalog(database_url=database)
    openstatspec.import_sav(source, database_url=database, dataset_id="mismatch")
    previous = b"previous destination bytes"
    destination.write_bytes(previous)

    def commit_legacy_only_then_disconnect(*, operation_id, **_kwargs):
        connection = sqlite3.connect(database_path)
        connection.execute(
            "update operation_catalog set status = 'succeeded' "
            "where operation_id = ?",
            (operation_id,),
        )
        connection.commit()
        connection.close()
        raise ConnectionError("synthetic ambiguous finalization")

    monkeypatch.setattr(
        sav_module, "finish_export_operation",
        commit_legacy_only_then_disconnect,
    )
    with pytest.raises(sav_module.ExportRecoveryError) as error:
        openstatspec.export_sav(
            database_url=database, dataset_id="mismatch",
            destination=destination,
        )

    assert error.value.code == "audit_finalization_ambiguous"
    assert pyspssio.read_sav(str(destination))[0]["answer"].tolist() == [1.0]
    backups = list(tmp_path.glob(f".{destination.name}.*.previous"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == previous
    serialized_error = json.dumps(error.value.details, sort_keys=True)
    assert str(tmp_path) not in serialized_error
    evidence = error.value.details["deterministic_recovery_evidence"]
    assert evidence["automatic_filesystem_recovery_performed"] is False
    assert evidence["manual_reconciliation_required"] is True

    connection = sqlite3.connect(database_path)
    destination_audit = connection.execute(
        "select destination from operation_catalog "
        "where direction = 'export' order by created_at desc limit 1"
    ).fetchone()[0]
    assert str(tmp_path) not in destination_audit
    assert json.loads(destination_audit)["role"] == "destination"
    connection.close()
