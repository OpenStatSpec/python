import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from openstatspec.sql.wide import create_wide_dataset


def test_failed_row_insert_leaves_no_catalog_or_data_table(tmp_path) -> None:
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]

    with pytest.raises(IntegrityError):
        create_wide_dataset(
            database_url=database, dataset_id="broken", source_name="fixture.sav",
            source_format="SAV", rows=[{"name": None}], variables=variables,
        )

    connection = sqlite3.connect(database_path)
    tables = [row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")]
    assert "data_broken" not in tables
    assert "dataset_catalog" in tables
    assert connection.execute("select count(*) from dataset_catalog").fetchone() == (0,)
    assert "variable_catalog" in tables
    assert connection.execute("select count(*) from variable_catalog").fetchone() == (0,)