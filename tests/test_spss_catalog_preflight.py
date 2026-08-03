import json
import sqlite3

import pytest

import openstatspec
from openstatspec.spss import sav as sav_module
from openstatspec.sql.wide import CatalogPreflightError, create_wide_dataset


def _variables() -> list[dict[str, object]]:
    return [
        {
            "ordinal": 1, "source_name": "weight", "physical_name": "weight",
            "storage_kind": "numeric", "string_width": None, "label": "",
            "format": "F8", "measure": "scale", "alignment": "right",
            "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
        },
        {
            "ordinal": 2, "source_name": "answer", "physical_name": "answer",
            "storage_kind": "numeric", "string_width": None, "label": "",
            "format": "F8", "measure": "nominal", "alignment": "right",
            "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
        },
        {
            "ordinal": 3, "source_name": "text", "physical_name": "text",
            "storage_kind": "string", "string_width": 8, "label": "",
            "format": "A8", "measure": "nominal", "alignment": "left",
            "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
        },
    ]


@pytest.mark.parametrize(
    ("weight_name", "mutate", "expected_code"),
    [
        ("missing", lambda _variables: None, "case-weight-variable-not-found"),
        ("text", lambda _variables: None, "case-weight-variable-not-numeric"),
    ],
)
def test_import_catalog_preflight_rejects_invalid_weight_atomically(
    tmp_path, weight_name, mutate, expected_code,
) -> None:
    database_path = tmp_path / "weight.sqlite"
    database = f"sqlite:///{database_path}"
    openstatspec.initialize_catalog(database_url=database)
    variables = _variables()
    mutate(variables)

    with pytest.raises(CatalogPreflightError) as error:
        create_wide_dataset(
            database_url=database, dataset_id="weight",
            source_name="weight.sav", source_format="SAV",
            rows=[{"weight": 1.0, "answer": 1.0, "text": "ok"}],
            variables=variables, case_weight_variable=weight_name,
        )

    assert error.value.code == expected_code
    assert error.value.details["reason"] == expected_code
    connection = sqlite3.connect(database_path)
    assert connection.execute("select count(*) from dataset_catalog").fetchone() == (0,)
    assert "data_weight" not in {
        row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")
    }
    assert connection.execute(
        "select status, dataset_id from operation_catalog"
    ).fetchall() == [("failed", None)]
    code, details = connection.execute(
        "select code, details from fidelity_event_catalog"
    ).fetchone()
    assert code == expected_code
    assert json.loads(details)["reason"] == expected_code
    assert connection.execute("select count(*) from dataset").fetchone() == (0,)
    operation = connection.execute(
        "select operation_kind, status, source_format, started_at, completed_at "
        "from operation"
    ).fetchone()
    assert operation[:3] == ("import", "failed", "SAV")
    assert operation[3] and operation[4]
    event = connection.execute(
        "select dataset_id, direction, severity, event_code, source_item, "
        "detail_json, created_at from fidelity_event"
    ).fetchone()
    assert event[:5] == (None, "import", "error", expected_code, "weight.sav")
    assert json.loads(event[5])["reason"] == expected_code
    assert event[6]


def test_numeric_weight_does_not_require_scale_measurement_level(tmp_path) -> None:
    database = f"sqlite:///{tmp_path}/weight-nominal.sqlite"
    openstatspec.initialize_catalog(database_url=database)
    variables = _variables()
    variables[1] = {**variables[1], "measure": "nominal"}
    result = create_wide_dataset(
        database_url=database,
        dataset_id="weight_nominal", source_name="weight.sav", source_format="SAV",
        rows=[{"weight": 1.0, "answer": 2.0, "text": "ok"}],
        variables=variables, case_weight_variable="answer",
    )
    assert result["case_count"] == 1


def _create_valid_dataset(database: str) -> None:
    openstatspec.initialize_catalog(database_url=database)
    create_wide_dataset(
        database_url=database, dataset_id="mr", source_name="mr.sav", source_format="SAV",
        rows=[{"weight": 1.0, "answer": 1.0, "text": "yes"}],
        variables=_variables(), case_weight_variable="weight",
        multiple_response_sets=json.dumps({
            "$answers": {
                "counted_value": 1,
                "variable_list": ["weight", "answer"],
            },
        }),
    )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda connection: connection.execute(
                "update dataset_catalog set case_weight_variable = 'text' where dataset_id = 'mr'"
            ),
            "case-weight-variable-not-numeric",
        ),
        (
            lambda connection: connection.execute(
                "update multiple_response_set_catalog set variable_name = 'missing' "
                "where dataset_id = 'mr' and member_ordinal = 1"
            ),
            "multiple-response-member-not-found",
        ),
        (
            lambda connection: connection.execute(
                "update multiple_response_set_catalog set variable_name = 'text' "
                "where dataset_id = 'mr' and member_ordinal = 2"
            ),
            "multiple-response-member-type-mismatch",
        ),
        (
            lambda connection: connection.execute(
                "update multiple_response_set_catalog "
                "set counted_value_type = 'text', counted_numeric = null, counted_text = 'yes' "
                "where dataset_id = 'mr'"
            ),
            "multiple-response-counted-value-type-mismatch",
        ),
    ],
)
def test_export_preflights_catalog_before_pyspssio_writer(
    tmp_path, monkeypatch, mutation, expected_code,
) -> None:
    database_path = tmp_path / "mr.sqlite"
    database = f"sqlite:///{database_path}"
    destination = tmp_path / "mr.sav"
    _create_valid_dataset(database)
    connection = sqlite3.connect(database_path)
    mutation(connection)
    connection.commit()

    def writer_must_not_run(*_args, **_kwargs):
        raise AssertionError("pyspssio writer must not run after catalog preflight failure")

    monkeypatch.setattr(sav_module.pyspssio, "write_sav", writer_must_not_run)
    with pytest.raises(CatalogPreflightError) as error:
        openstatspec.export_sav(
            database_url=database, dataset_id="mr", destination=destination,
        )

    assert error.value.code == expected_code
    assert error.value.details["reason"] == expected_code
    assert not destination.exists()
