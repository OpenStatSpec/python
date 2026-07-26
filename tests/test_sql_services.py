"""Real-service conformance checks for PostgreSQL, MySQL, and MariaDB profiles."""

import os

import pandas as pd
import pyreadstat
import pytest
from sqlalchemy import create_engine, text

import openstatspec
from conformance import compare_sav_semantics, write_supported_semantics_fixture


pytestmark = pytest.mark.services


@pytest.fixture
def source_sav(tmp_path):
    source = tmp_path / "service-fixture.sav"
    pyreadstat.write_sav(
        pd.DataFrame({"age": [34.0, None], "name": ["Ada", ""]}),
        source,
        file_label="SQL service fixture",
        column_labels={"age": "Age", "name": "Name"},
        variable_value_labels={"age": {34.0: "thirty-four"}},
        variable_format={"age": "F8.0", "name": "A12"},
        missing_ranges={"age": [34.0]},
        note=["Service fixture note"],
    )
    return source


@pytest.mark.parametrize(
    ("environment_name", "dataset_id"),
    [
        ("OPENSTATSPEC_POSTGRES_URL", "profile_pg"),
        ("OPENSTATSPEC_MYSQL_URL", "profile_mysql"),
        ("OPENSTATSPEC_MARIADB_URL", "profile_mariadb"),
    ],
)
def test_live_profile_import_validate_and_export(environment_name, dataset_id, source_sav, tmp_path):
    database_url = os.environ.get(environment_name)
    if not database_url:
        pytest.skip(f"{environment_name} is not configured")

    imported = openstatspec.import_sav(source_sav, database_url=database_url, dataset_id=dataset_id)
    assert imported["case_count"] == 2
    assert openstatspec.validate(database_url=database_url, dataset_id=dataset_id)["valid"] is True

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text(f'SELECT COUNT(*) FROM {imported["data_table"]}')).scalar_one() == 2
        assert connection.execute(
            text("SELECT COUNT(*) FROM variable_catalog WHERE dataset_id = :dataset_id"),
            {"dataset_id": dataset_id},
        ).scalar_one() == 2

    destination = tmp_path / f"{dataset_id}.sav"
    exported = openstatspec.export_sav(
        database_url=database_url,
        dataset_id=dataset_id,
        destination=destination,
        allow_loss=["unobservable-source-dictionary-features"],
    )
    assert destination.exists()
    assert exported["loss_report"]
    frame, metadata = pyreadstat.read_sav(destination, user_missing=True)
    assert frame["age"].iloc[0] == 34.0
    assert pd.isna(frame["age"].iloc[1])
    assert frame["name"].tolist() == ["Ada", ""]
    assert metadata.column_labels == ["Age", "Name"]
    assert metadata.variable_value_labels == {"age": {34.0: "thirty-four"}}

@pytest.mark.parametrize(
    ("environment_name", "dataset_id"),
    [
        ("OPENSTATSPEC_POSTGRES_URL", "semantics_pg"),
        ("OPENSTATSPEC_MYSQL_URL", "semantics_mysql"),
        ("OPENSTATSPEC_MARIADB_URL", "semantics_mariadb"),
    ],
)
@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_live_profile_preserves_supported_sav_semantics(
    environment_name, dataset_id, suffix, tmp_path,
):
    """Exercise every supported SPSS semantic on each real SQL family.

    The narrow contract has no alternate long-form fallback: every profile
    must preserve the exact wide table and supported dictionary metadata for
    both uncompressed SAV and compressed ZSAV inputs.
    """
    database_url = os.environ.get(environment_name)
    if not database_url:
        pytest.skip(f"{environment_name} is not configured")

    source = tmp_path / f"{dataset_id}{suffix}"
    destination = tmp_path / f"{dataset_id}-roundtrip{suffix}"
    write_supported_semantics_fixture(source)

    imported = openstatspec.import_sav(
        source, database_url=database_url, dataset_id=f"{dataset_id}_{suffix[1:]}",
    )
    assert imported["case_count"] == 4
    assert openstatspec.validate(
        database_url=database_url, dataset_id=f"{dataset_id}_{suffix[1:]}",
    )["valid"] is True

    openstatspec.export_sav(
        database_url=database_url,
        dataset_id=f"{dataset_id}_{suffix[1:]}",
        destination=destination,
        allow_loss=["unobservable-source-dictionary-features"],
    )
    assert compare_sav_semantics(source, destination) == {
        "equivalent": True,
        "differences": [],
    }
