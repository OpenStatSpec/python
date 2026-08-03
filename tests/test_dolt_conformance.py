from __future__ import annotations

import inspect
import tomllib
from pathlib import Path

import pandas as pd
import pytest

import openstatspec.api as api_module
import openstatspec.spss as spss_module
import openstatspec.spss.sav as sav_module
from openstatspec.core import UnsupportedOperationError
import openstatspec.sql.capabilities as capability_module
import openstatspec.sql.wide as wide
from openstatspec.sql.dolt_conformance import (
    ADAPTER_IMPLEMENTATION_ID,
    ADAPTER_VERSION,
    DoltConformanceSource,
)


def test_packaged_source_is_fail_closed_without_concrete_declarations() -> None:
    status = DoltConformanceSource.packaged().status()
    assert status["write_enabled"] is False
    assert status["declaration_count"] == 0


def test_directory_source_is_explicit_and_invalid_root_fails_closed(
    tmp_path: Path,
) -> None:
    source = DoltConformanceSource.from_directory(tmp_path)
    assert source.directory == tmp_path
    status = source.status()
    assert status["write_enabled"] is False
    assert status["status"] == "blocked_invalid_or_unavailable_declaration_source"


def test_packaged_companion_missing_is_reported_without_opening_write_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_companion() -> tuple[object, object, object, object]:
        raise UnsupportedOperationError("companion missing")

    monkeypatch.setattr(
        "openstatspec.sql.dolt_conformance._shared_api", missing_companion,
    )
    declaration = capability_module.profile_declarations()["dolt"]
    assert declaration["operational_write_enabled"] is False
    assert declaration["write_conformance"]["write_enabled"] is False
    assert declaration["claimed_server_versions"] == []
    assert declaration["ci_tested_server_versions"] == []


def test_exact_match_binds_active_product_adapter_and_specification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matching = {
        "declaration_id": "dolt-2.2.2-python-0.1.0",
        "active_product_version": "2.2.2",
        "adapter_implementation_id": ADAPTER_IMPLEMENTATION_ID,
        "adapter_version": ADAPTER_VERSION,
        "specification_commit": "a" * 40,
    }
    monkeypatch.setattr(
        DoltConformanceSource,
        "validated_declarations",
        lambda self: (matching,),
    )
    selected = DoltConformanceSource.packaged().require_exact_match(
        active_product_version="2.2.2",
        specification_commit="a" * 40,
    )
    assert selected["declaration_id"] == matching["declaration_id"]


def test_exact_match_rejects_multiple_adapter_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = {
        "active_product_version": "2.2.2",
        "adapter_implementation_id": ADAPTER_IMPLEMENTATION_ID,
        "adapter_version": ADAPTER_VERSION,
        "specification_commit": "a" * 40,
    }
    monkeypatch.setattr(
        DoltConformanceSource,
        "validated_declarations",
        lambda self: (
            {**binding, "declaration_id": "one"},
            {**binding, "declaration_id": "two"},
        ),
    )
    with pytest.raises(UnsupportedOperationError, match="unique exact"):
        DoltConformanceSource.packaged().require_exact_match(
            active_product_version="2.2.2",
            specification_commit="a" * 40,
        )


def test_effective_profile_requires_bound_spec_commit_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class UnexpectedSource:
        def require_exact_match(
            self, *, active_product_version: str, specification_commit: str,
        ) -> None:
            calls.append((active_product_version, specification_commit))

    monkeypatch.setattr(
        capability_module,
        "active_connection",
        lambda database_url, **kwargs: {
            "profile": "dolt",
            "raw_product_version": "2.2.2",
            "server_version": "2.2.2",
            "claimed_supported": False,
            "observed": {"max_allowed_packet": 1_000_000},
        },
    )
    monkeypatch.setattr(capability_module, "SPECIFICATION_COMMIT", None)
    assert capability_module.SPECIFICATION_COMMIT is None
    with pytest.raises(UnsupportedOperationError, match="not bound"):
        capability_module.effective_profile(
            "mysql+pymysql://example.invalid/catalog",
            dolt_conformance_source=UnexpectedSource(),
        )
    assert calls == []


def test_every_sql_mutation_entrypoint_accepts_explicit_conformance_source() -> None:
    mutation_entrypoints = (
        wide.initialize_wide_catalog,
        wide.create_wide_dataset,
        wide.record_export_cleanup_failure,
        wide.record_export_operation,
        wide.finish_export_operation,
        wide.fail_export_operation,
        wide.record_export_backup_retained,
    )
    for entrypoint in mutation_entrypoints:
        parameter = inspect.signature(entrypoint).parameters[
            "dolt_conformance_source"
        ]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None


def test_adapter_binding_version_matches_distribution_metadata() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    assert pyproject["project"]["version"] == ADAPTER_VERSION


def test_validate_wide_dataset_propagates_explicit_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    calls: list[object] = []

    def stop_after_read_preflight(**kwargs: object) -> tuple[object, object, object]:
        calls.append(kwargs["dolt_conformance_source"])
        raise UnsupportedOperationError("stop after propagation check")

    monkeypatch.setattr(wide, "read_wide_dataset", stop_after_read_preflight)
    with pytest.raises(UnsupportedOperationError, match="propagation check"):
        wide.validate_wide_dataset(
            database_url="mysql+pymysql://example.invalid/catalog",
            dataset_id="synthetic",
            dolt_conformance_source=sentinel,
        )
    assert calls == [sentinel]


def test_public_and_spss_dispatch_propagate_explicit_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    calls: list[tuple[str, object]] = []

    def capture_api_import(*args: object, **kwargs: object) -> dict[str, bool]:
        calls.append(("api_import", kwargs["dolt_conformance_source"]))
        return {"ok": True}

    def capture_api_export(**kwargs: object) -> dict[str, bool]:
        calls.append(("api_export", kwargs["dolt_conformance_source"]))
        return {"ok": True}

    monkeypatch.setattr(api_module, "import_dataset", capture_api_import)
    monkeypatch.setattr(api_module, "export_dataset", capture_api_export)
    monkeypatch.setattr(api_module, "result", lambda value: value)
    api_module.import_sav(
        "synthetic.sav", database_url="sqlite://", dataset_id="synthetic",
        dolt_conformance_source=sentinel,
    )
    api_module.export_sav(
        database_url="sqlite://", dataset_id="synthetic",
        destination="synthetic.sav", dolt_conformance_source=sentinel,
    )

    def capture_spss_import(**kwargs: object) -> dict[str, bool]:
        calls.append(("spss_import", kwargs["dolt_conformance_source"]))
        return {"ok": True}

    def capture_spss_export(**kwargs: object) -> dict[str, bool]:
        calls.append(("spss_export", kwargs["dolt_conformance_source"]))
        return {"ok": True}

    monkeypatch.setattr(spss_module, "import_sav_dataset", capture_spss_import)
    monkeypatch.setattr(spss_module, "export_sav_dataset", capture_spss_export)
    spss_module.import_dataset(
        "synthetic.sav", database_url="sqlite://", dataset_id="synthetic",
        dolt_conformance_source=sentinel,
    )
    spss_module.export_dataset(
        database_url="sqlite://", dataset_id="synthetic",
        destination="synthetic.sav", dolt_conformance_source=sentinel,
    )
    assert calls == [
        ("api_import", sentinel),
        ("api_export", sentinel),
        ("spss_import", sentinel),
        ("spss_export", sentinel),
    ]


def test_sav_import_propagates_explicit_source_to_mutation_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    calls: list[object] = []
    monkeypatch.setattr(sav_module, "_require_source", lambda path: None)
    monkeypatch.setattr(
        sav_module.pyspssio, "read_sav",
        lambda *args, **kwargs: (pd.DataFrame(), {}),
    )
    monkeypatch.setattr(sav_module, "_dictionary", lambda path: ({}, {}))
    monkeypatch.setattr(sav_module, "_sha256", lambda path: "0" * 64)

    def capture_create(**kwargs: object) -> dict[str, str]:
        calls.append(kwargs["dolt_conformance_source"])
        return {"dataset_id": "synthetic"}

    monkeypatch.setattr(sav_module, "create_wide_dataset", capture_create)
    sav_module.import_sav_dataset(
        source="synthetic.sav", database_url="sqlite://",
        dataset_id="synthetic", dolt_conformance_source=sentinel,
    )
    assert calls == [sentinel]


def test_sav_export_propagates_explicit_source_to_read_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    calls: list[object] = []

    def stop_after_read(**kwargs: object) -> tuple[object, object, object]:
        calls.append(kwargs["dolt_conformance_source"])
        raise UnsupportedOperationError("stop after SAV read propagation")

    monkeypatch.setattr(sav_module, "read_wide_dataset", stop_after_read)
    with pytest.raises(UnsupportedOperationError, match="SAV read propagation"):
        sav_module.export_sav_dataset(
            database_url="sqlite://", dataset_id="synthetic",
            destination="synthetic.sav",
            dolt_conformance_source=sentinel,
        )
    assert calls == [sentinel]


def test_export_recovery_helpers_propagate_explicit_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    sentinel = object()
    calls: list[tuple[str, object]] = []

    def capture_cleanup(**kwargs: object) -> str:
        calls.append(("cleanup", kwargs["dolt_conformance_source"]))
        return "cleanup-operation"

    def capture_failure(**kwargs: object) -> None:
        calls.append(("failure", kwargs["dolt_conformance_source"]))

    monkeypatch.setattr(
        sav_module, "record_export_cleanup_failure", capture_cleanup,
    )
    monkeypatch.setattr(sav_module, "fail_export_operation", capture_failure)
    destination = tmp_path / "destination.sav"
    backup = tmp_path / "backup.sav"
    staged = tmp_path / "staged.sav"
    with pytest.raises(sav_module.ExportRecoveryError, match="cleanup_failed"):
        sav_module._raise_export_cleanup_failed(
            original_error=RuntimeError("synthetic export failure"),
            cleanup_error=RuntimeError("synthetic cleanup failure"),
            phase="synthetic", destination=destination, backup=backup,
            staged=staged, had_previous=False, database_url="sqlite://",
            dolt_conformance_source=sentinel,
        )
    sav_module._mark_export_failed_after_restore(
        database_url="sqlite://", operation_id="synthetic-operation",
        error=RuntimeError("synthetic export failure"), phase="synthetic",
        destination=destination, backup=backup, had_previous=False,
        dolt_conformance_source=sentinel,
    )
    assert calls == [("cleanup", sentinel), ("failure", sentinel)]

