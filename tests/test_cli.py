import json

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
    assert inspected["source_sha256"]
    assert {event["code"] for event in inspected["loss_report"]} == {
        "file-label-and-documents-unobservable", "separate-write-format-unobservable",
    }
    assert imported["case_count"] == 1

    from openstatspec.core.results import OperationResult
    assert isinstance(openstatspec.import_sav(source, database_url=f"sqlite:///{tmp_path / 'typed.sqlite'}", dataset_id="typed"), OperationResult)

    assert openstatspec.cli.main(["validate", "--database-url", database, "--dataset-id", "fixture"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert openstatspec.cli.main([
        "export", "--database-url", database, "--dataset-id", "fixture", "--output", str(output),
        "--allow-loss", "file-label-and-documents-unobservable", "--allow-loss", "separate-write-format-unobservable",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["destination"] == str(output)
    assert pyspssio.read_sav(str(output), convert_datetimes=False)[0]["answer"].tolist() == [1.0]