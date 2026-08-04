import json
import sqlite3

import openstatspec
import openstatspec.cli
import pandas as pd
import pyspssio


def test_cli_import_inspect_validate_and_export_emit_json(tmp_path, capsys) -> None:
    source = tmp_path / "fixture.sav"
    database = f"sqlite:///{tmp_path / 'dataset.sqlite'}"
    openstatspec.initialize_catalog(database_url=database)
    output = tmp_path / "output.zsav"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))

    assert openstatspec.cli.main(["import", str(source), "--database-url", database, "--dataset-id", "fixture"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert openstatspec.cli.main(["inspect", str(source)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["source_format"] == "SAV"
    assert inspected["engine"]["package"] == "openstatspec-pyspssio"
    assert inspected["engine"]["pinned_commit"] == "e069adf33c70bcd9e8e6ee495106479463a84fa2"
    assert inspected["source_sha256"]
    assert inspected["loss_report"] == []
    assert imported["case_count"] == 1

    from openstatspec.core.results import OperationResult
    typed_database = f"sqlite:///{tmp_path / 'typed.sqlite'}"
    openstatspec.initialize_catalog(database_url=typed_database)
    assert isinstance(openstatspec.import_sav(source, database_url=typed_database, dataset_id="typed"), OperationResult)

    assert openstatspec.cli.main(["validate", "--database-url", database, "--dataset-id", "fixture"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert openstatspec.cli.main([
        "export", "--database-url", database, "--dataset-id", "fixture", "--output", str(output),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["destination"] == str(output)
    assert pyspssio.read_sav(str(output), convert_datetimes=False)[0]["answer"].tolist() == [1.0]


def test_capability_matrix_is_public_and_cli_matches_engine_boundary(capsys) -> None:
    matrix = openstatspec.capability_matrix()
    assert matrix["specification_status"] == "release_candidate"
    assert matrix["specification_release"] is None
    assert matrix["specification_commit"] == "8f1f750fb38a2be87be0a7431a14fa2d3130f873"
    assert matrix["directions"] == ["import", "export", "semantic_round_trip"]
    assert matrix["active_connection"] is None
    assert matrix["engine"]["package"] == "openstatspec-pyspssio"
    assert matrix["engine"]["pinned_commit"] == "e069adf33c70bcd9e8e6ee495106479463a84fa2"

    assert matrix["spss"] == {
        "values": "supported",
        "variable_labels": "supported",
        "value_labels": "supported",
        "print_format": "supported",
        "write_format": "supported",
        "file_label": "supported",
        "documents": "supported",
        "source_encoding": {
            "utf8": "supported",
            "legacy_code_pages": "requires-explicit-legacy-locale",
        },
        "measurement_level": "supported",
        "user_missing_rules": "supported",
        "multiple_response_sets": "supported",
        "variable_alignment": "supported",
        "variable_sets": "supported",
        "compatible_variable_names": "supported",
        "custom_attributes": {
            "scalar_values": "supported",
            "ordered_value_arrays": "supported",
        },
        "variable_role": "supported",
    }

    assert openstatspec.cli.main(["capabilities"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered == matrix


def test_capability_matrix_reports_active_sqlite_limits(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'capabilities.sqlite'}"
    matrix = openstatspec.capability_matrix(database_url=database_url)
    active = matrix["active_connection"]
    assert active["profile"] == "sqlite"
    assert active["claimed_supported"] is True
    assert active["catalog_binding"]["mode"] == "dedicated_database_file"
    assert active["catalog_binding"]["namespace"].endswith("capabilities.sqlite")
    profile = matrix["sql_profiles"]["sqlite"]
    assert profile["effective_limits"] is not None
    assert profile["effective_limits"]["maximum_source_variables"] <= 1999
    assert profile["theoretical_limits"]["identifier_limit"] == {
        "value": 255,
        "unit": "bytes",
        "source": "OpenStatSpec profile boundary; SQLite has no fixed native identifier limit",
        "repertoire": "generated ASCII [a-z0-9_] identifiers",
    }


def test_cli_installs_schema_and_applies_plan_or_spss(
    tmp_path, capsys,
) -> None:
    source = tmp_path / "transform-source.sav"
    database_path = tmp_path / "transform.sqlite"
    database_url = f"sqlite:///{database_path}"
    openstatspec.initialize_catalog(database_url=database_url)
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    openstatspec.import_sav(
        source,
        database_url=database_url,
        dataset_id="transform-cli",
    )
    connection = sqlite3.connect(database_path)
    live_dataset_id = connection.execute(
        "SELECT dataset_id FROM dataset"
    ).fetchone()[0]

    assert openstatspec.cli.main([
        "install-in-place-schema",
        "--database-url", database_url,
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "installed"}

    plan = openstatspec.compile_spss_syntax(
        "RECODE answer (1 = 2).",
        openstatspec.VariableSchema((
            openstatspec.VariableDefinition("answer", "numeric"),
        )),
    ).plan
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps(plan.as_dict()),
        encoding="utf-8",
    )
    assert openstatspec.cli.main([
        "apply-plan",
        "--database-url", database_url,
        "--dataset-id", live_dataset_id,
        "--actor", "cli-test",
        "--plan-file", str(plan_file),
    ]) == 0
    generic = json.loads(capsys.readouterr().out)
    assert generic["source_kind"] == "canonical_plan"

    assert openstatspec.cli.main([
        "apply-spss",
        "--database-url", database_url,
        "--dataset-id", live_dataset_id,
        "--actor", "cli-test",
        "--syntax", "RECODE answer (2 = 3).",
    ]) == 0
    spss = json.loads(capsys.readouterr().out)
    assert spss["source_kind"] == "spss_syntax"

    table_name = connection.execute(
        "SELECT physical_table_name FROM dataset"
    ).fetchone()[0]
    assert connection.execute(
        f'SELECT answer FROM "{table_name}"'
    ).fetchone() == (3.0,)
