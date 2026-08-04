import json
import sqlite3

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.sql.wide import create_wide_dataset, read_wide_dataset


_REQUIRED_ENGINE_LOSS = []


@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_normative_attributes_are_authoritative_for_sav_and_zsav_export(tmp_path, suffix: str) -> None:
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
        "select attribute_name, array_ordinal, attribute_value "
        "from dataset_attribute order by rowid"
    ).fetchall() == [
        ("Source", 1, "source-file"),
        ("Order", 1, "second"),
    ]
    assert connection.execute(
        "select a.attribute_name, a.array_ordinal, a.attribute_value "
        "from variable_attribute a order by a.rowid"
    ).fetchall() == [
        ("Source", 1, "source-variable"),
        ("Flag", 1, "yes"),
    ]
    connection.execute(
        "update dataset_attribute set attribute_value = 'catalog-file' "
        "where attribute_name = 'Source'"
    )
    connection.execute(
        "update variable_attribute set attribute_value = 'catalog-variable' "
        "where attribute_name = 'Source'"
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
        "select attribute_name, attribute_value from dataset_attribute order by attribute_name"
    ).fetchall() == [("Order", "second"), ("Source", "catalog-file")]
    assert reimported.execute(
        "select attribute_name, attribute_value from variable_attribute order by attribute_name"
    ).fetchall() == [("Flag", "yes"), ("Source", "catalog-variable")]


def test_normative_attributes_preserve_ordered_arrays_through_raw_pyspssio_bridge(tmp_path) -> None:
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
    destination = tmp_path / "array.sav"
    openstatspec.export_sav(
        database_url=database, dataset_id="array", destination=destination,
        allow_loss=_REQUIRED_ENGINE_LOSS,
    )
    from openstatspec.spss.dictionary import (
        attribute_values, file_attribute_pairs, variable_attribute_pairs,
    )
    with pyspssio.Reader(str(destination), mode="r") as reader:
        assert attribute_values(file_attribute_pairs(reader)) == {
            "Array": ["one", "two"], "After": "three",
        }
        assert attribute_values(variable_attribute_pairs(reader, "answer")) == {
            "Array": ["red", "blue"],
        }
