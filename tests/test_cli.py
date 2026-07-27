import json

import openstatspec
import openstatspec.cli
import pandas as pd
import pyspssio


def test_cli_import_inspect_validate_and_export_emit_json(tmp_path, capsys) -> None:
    source = tmp_path / "fixture.sav"
    database = f"sqlite:///{tmp_path / 'dataset.sqlite'}"
    output = tmp_path / "output.zsav"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))

    assert openstatspec.cli.main(["import", str(source), "--database-url", database, "--dataset-id", "fixture"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert openstatspec.cli.main(["inspect", str(source)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["source_format"] == "SAV"
    assert inspected["engine"]["pinned_commit"] == "6a0f9fa"
    assert inspected["source_sha256"]
    assert inspected["loss_report"] == []
    assert imported["case_count"] == 1

    from openstatspec.core.results import OperationResult
    assert isinstance(openstatspec.import_sav(source, database_url=f"sqlite:///{tmp_path / 'typed.sqlite'}", dataset_id="typed"), OperationResult)

    assert openstatspec.cli.main(["validate", "--database-url", database, "--dataset-id", "fixture"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert openstatspec.cli.main([
        "export", "--database-url", database, "--dataset-id", "fixture", "--output", str(output),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["destination"] == str(output)
    assert pyspssio.read_sav(str(output), convert_datetimes=False)[0]["answer"].tolist() == [1.0]


def test_capability_matrix_is_public_and_cli_matches_engine_boundary(capsys) -> None:
    matrix = openstatspec.capability_matrix()
    assert matrix["engine"]["pinned_commit"] == "6a0f9fa"

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
