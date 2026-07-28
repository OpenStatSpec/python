"""Official OpenStatSpec SPSS SAV/ZSAV 1.0 manifest conformance."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

import openstatspec
from conformance import compare_sav_semantics


def _specification_root() -> Path:
    configured = os.environ.get("OPENSTATSPEC_SPECIFICATION_DIR")
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[2] / "specification",
    ]
    for candidate in candidates:
        if candidate and (candidate / "conformance/spss-sav-zsav-1.0.json").is_file():
            return candidate
    raise RuntimeError(
        "The official OpenStatSpec specification checkout is required; "
        "set OPENSTATSPEC_SPECIFICATION_DIR."
    )


def _official_fixtures() -> list[tuple[str, Path]]:
    root = _specification_root()
    manifest = json.loads(
        (root / "conformance/spss-sav-zsav-1.0.json").read_text(encoding="utf-8")
    )
    assert manifest["manifest_version"] == "1.0"
    fixtures = []
    for fixture in manifest["fixtures"]:
        if fixture["id"] == "preflight-failure":
            continue
        source = root / "conformance" / fixture["source"]
        assert source.is_file(), f"Missing official fixture: {fixture['id']}"
        fixtures.append((fixture["id"], source))
    return fixtures


def _assert_round_trip(
    *, fixture_id: str, source: Path, database_url: str, tmp_path: Path, profile: str,
) -> None:
    token = uuid4().hex[:10]
    dataset_id = f"official_{profile}_{fixture_id}_{token}".replace("-", "_")
    destination = tmp_path / f"{dataset_id}{source.suffix}"

    imported = openstatspec.import_sav(
        source, database_url=database_url, dataset_id=dataset_id,
    )
    assert imported.diagnostics == ()
    assert openstatspec.validate(
        database_url=database_url, dataset_id=dataset_id,
    )["valid"] is True

    exported = openstatspec.export_sav(
        database_url=database_url, dataset_id=dataset_id, destination=destination,
    )
    assert exported.diagnostics == ()
    assert compare_sav_semantics(source, destination) == {
        "equivalent": True,
        "differences": [],
    }


@pytest.mark.parametrize(("fixture_id", "source"), _official_fixtures())
def test_official_manifest_round_trips_through_sqlite(
    fixture_id: str, source: Path, tmp_path: Path,
) -> None:
    _assert_round_trip(
        fixture_id=fixture_id,
        source=source,
        database_url=f"sqlite:///{tmp_path / 'official.sqlite'}",
        tmp_path=tmp_path,
        profile="sqlite",
    )


@pytest.mark.services
@pytest.mark.parametrize(
    ("environment_name", "profile"),
    [
        ("OPENSTATSPEC_POSTGRES_URL", "postgresql"),
        ("OPENSTATSPEC_MYSQL_URL", "mysql"),
        ("OPENSTATSPEC_MARIADB_URL", "mariadb"),
    ],
)
@pytest.mark.parametrize(("fixture_id", "source"), _official_fixtures())
def test_official_manifest_round_trips_through_server_profiles(
    environment_name: str, profile: str, fixture_id: str, source: Path, tmp_path: Path,
) -> None:
    database_url = os.environ.get(environment_name)
    if not database_url:
        pytest.skip(f"{environment_name} is not configured")
    _assert_round_trip(
        fixture_id=fixture_id,
        source=source,
        database_url=database_url,
        tmp_path=tmp_path,
        profile=profile,
    )
