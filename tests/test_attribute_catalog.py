import json
import sqlite3

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.wide import create_wide_dataset, read_wide_dataset


_REQUIRED_ENGINE_LOSS = [
    "file-label-and-documents-unobservable",
    "separate-write-format-unobservable",
]


@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_attribute_catalog_is_authoritative_for_sav_and_zsav_export(tmp_path, suffix: str) -> None:
    source = tmp_path / f"source{suffix}"
    destination = tmp_path / f"destination{suffix}"
    imported_again = tmp_path / f"again-{suffix[1:]}.sqlite"
    database_path = tmp_path / f"attributes-{suffix[1:]}.sqlite"
    database = f"sqlite:///{database_path}"
    pyspssio.write_sav(
        str(source), pd.DataFrame({"answer": [1.0]}),
        metadata={
            "file_attributes": {"Source": "source-file", "Order": "second"},
            "var_attributes": {"answer": {"Source": "source-variable", "Flag": "yes"}},
        },
    )

    openstatspec.import_sav(source, database_url=database, dataset_id="attributes")
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select scope, variable_ordinal, attribute_ordinal, value_ordinal, attribute_name, attribute_value "
        "from attribute_catalog where dataset_id = 'attributes' "
        "order by scope, variable_ordinal, attribute_ordinal, value_ordinal"
    ).fetchall() == [
        ("file", 0, 1, 1, "Source", "source-file"),
        ("file", 0, 2, 1, "Order", "second"),
        ("variable", 1, 1, 1, "Source", "source-variable"),
        ("variable", 1, 2, 1, "Flag", "yes"),
    ]
    # Deliberately corrupt the legacy copies. Export must use the normalized rows.
    connection.execute("update dataset_catalog set file_attributes = ? where dataset_id = 'attributes'", (json.dumps({"Source": "legacy-file"}),))
    connection.execute("update variable_catalog set attributes = ? where dataset_id = 'attributes'", (json.dumps({"Source": "legacy-variable"}),))
    connection.execute(
        "update attribute_catalog set attribute_value = 'catalog-file' "
        "where dataset_id = 'attributes' and scope = 'file' and attribute_name = 'Source'"
    )
    connection.execute(
        "update attribute_catalog set attribute_value = 'catalog-variable' "
        "where dataset_id = 'attributes' and scope = 'variable' and attribute_name = 'Source'"
    )
    connection.commit()

    openstatspec.export_sav(
        database_url=database, dataset_id="attributes", destination=destination,
        allow_loss=_REQUIRED_ENGINE_LOSS,
    )
    exported = pyspssio.read_metadata(str(destination))
    assert exported["file_attributes"] == {"Source": "catalog-file", "Order": "second"}
    assert exported["var_attributes"] == {
        "answer": {"Source": "catalog-variable", "Flag": "yes"},
    }
    openstatspec.import_sav(destination, database_url=f"sqlite:///{imported_again}", dataset_id="again")
    reimported = sqlite3.connect(imported_again)
    assert reimported.execute(
        "select attribute_name, attribute_value from attribute_catalog "
        "where dataset_id = 'again' and scope = 'file' order by attribute_ordinal, value_ordinal"
    ).fetchall() == [("Source", "catalog-file"), ("Order", "second")]
    assert reimported.execute(
        "select attribute_name, attribute_value from attribute_catalog "
        "where dataset_id = 'again' and scope = 'variable' order by attribute_ordinal, value_ordinal"
    ).fetchall() == [("Source", "catalog-variable"), ("Flag", "yes")]


def test_attribute_catalog_migrates_old_json_catalog_without_rewriting_it(tmp_path) -> None:
    source = tmp_path / "legacy.sav"
    destination = tmp_path / "legacy-out.sav"
    database_path = tmp_path / "legacy.sqlite"
    database = f"sqlite:///{database_path}"
    pyspssio.write_sav(
        str(source), pd.DataFrame({"answer": [1.0]}),
        metadata={"file_attributes": {"File": "legacy"}, "var_attributes": {"answer": {"Var": "legacy"}}},
    )
    openstatspec.import_sav(source, database_url=database, dataset_id="legacy")
    connection = sqlite3.connect(database_path)
    connection.execute("drop table attribute_catalog")
    connection.commit()

    # Opening an older catalog additively creates the new table and continues to
    # use legacy JSON only because that older dataset has no normalized rows.
    dataset, variables, _ = read_wide_dataset(database_url=database, dataset_id="legacy")
    assert json.loads(dataset["file_attributes"]) == {"File": "legacy"}
    assert json.loads(variables[0]["attributes"]) == {"Var": "legacy"}
    assert connection.execute(
        "select name from sqlite_master where type = 'table' and name = 'attribute_catalog'"
    ).fetchone() == ("attribute_catalog",)
    openstatspec.export_sav(
        database_url=database, dataset_id="legacy", destination=destination,
        allow_loss=_REQUIRED_ENGINE_LOSS,
    )
    assert pyspssio.read_metadata(str(destination))["file_attributes"] == {"File": "legacy"}


def test_attribute_catalog_preserves_ordered_arrays_and_fails_closed_for_pyspssio(tmp_path) -> None:
    database = f"sqlite:///{tmp_path / 'array.sqlite'}"
    create_wide_dataset(
        database_url=database, dataset_id="array", source_name="array.sav", source_format="SAV",
        rows=[{"answer": 1.0}],
        variables=[{
            "ordinal": 1, "source_name": "answer", "physical_name": "answer", "storage_kind": "numeric",
            "readstat_storage_type": "pyspssio:numeric", "string_width": None, "label": "", "format": "F8",
            "measure": "scale", "role": "input", "alignment": "right", "display_width": 8,
            "attributes": json.dumps({"legacy": "not-authoritative"}), "compat_name": "ANSWER",
            "value_labels": "{}", "missing_ranges": "[]",
        }],
        file_attributes=json.dumps({"legacy": "not-authoritative"}),
        file_attribute_values={"Array": ["one", "two"], "After": "three"},
        variable_attribute_values={"answer": {"Array": ["red", "blue"]}},
    )
    dataset, variables, _ = read_wide_dataset(database_url=database, dataset_id="array")
    assert json.loads(dataset["file_attributes"]) == {"Array": ["one", "two"], "After": "three"}
    assert json.loads(variables[0]["attributes"]) == {"Array": ["red", "blue"]}
    with pytest.raises(UnsupportedOperationError, match="multi-value SPSS custom attribute 'Array' on file"):
        openstatspec.export_sav(
            database_url=database, dataset_id="array", destination=tmp_path / "array.sav",
        )
