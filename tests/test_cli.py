import json

import openstatspec.cli
import pandas as pd
import pyreadstat


def test_cli_import_inspect_validate_and_export_emit_json(tmp_path, capsys) -> None:
    source = tmp_path / "fixture.sav"
    database = f"sqlite:///{tmp_path / 'dataset.sqlite'}"
    output = tmp_path / "output.zsav"
    pyreadstat.write_sav(pd.DataFrame({"answer": [1.0]}), source)

    assert openstatspec.cli.main(["import", str(source), "--database-url", database, "--dataset-id", "fixture"]) == 0
    imported = json.loads(capsys.readouterr().out)
    preview = openstatspec.cli.main(["inspect", str(source)])
    assert preview == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["source_format"] == "SAV"
    assert inspected["source_sha256"]
    assert inspected["loss_report"] == []
    assert imported["case_count"] == 1

    assert openstatspec.cli.main(["validate", "--database-url", database, "--dataset-id", "fixture"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert openstatspec.cli.main(["export", "--database-url", database, "--dataset-id", "fixture", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["destination"] == str(output)
    assert pyreadstat.read_sav(output)[0]["answer"].tolist() == [1.0]

    assert openstatspec.cli.main(["inspect", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["variable_count"] == 1