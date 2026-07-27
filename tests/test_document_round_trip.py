from pathlib import Path
import sqlite3

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.spss.raw_dictionary import read_document_lines, write_document_lines


def _source_with_documents(path: Path, lines: list[str]) -> None:
    pyspssio.write_sav(str(path), pd.DataFrame({"answer": [1.0, 2.0]}))
    write_document_lines(path, lines, encoding="UTF-8")


@pytest.mark.parametrize("destination_suffix", [".sav", ".zsav"])
def test_document_lines_round_trip_through_sqlite(destination_suffix: str, tmp_path: Path) -> None:
    source = tmp_path / "source.sav"
    destination = tmp_path / f"destination{destination_suffix}"
    database = f"sqlite:///{tmp_path / 'documents.sqlite'}"
    expected = ["Imported from a validated source.", "A second document line."]
    _source_with_documents(source, expected)

    imported = openstatspec.import_sav(
        source, database_url=database, dataset_id=f"documents-{destination_suffix[1:]}",
    )
    assert imported.diagnostics == ()
    connection = sqlite3.connect(tmp_path / "documents.sqlite")
    assert connection.execute(
        "select ordinal, text from document_catalog order by ordinal"
    ).fetchall() == [(1, expected[0]), (2, expected[1])]

    openstatspec.export_sav(
        database_url=database, dataset_id=f"documents-{destination_suffix[1:]}",
        destination=destination,
    )
    assert read_document_lines(destination, encoding="UTF-8") == expected
    assert pyspssio.read_sav(str(destination))[0]["answer"].tolist() == [1.0, 2.0]


def _zsav_with_documents(path: Path, lines: list[str], temporary_source: Path) -> None:
    _source_with_documents(temporary_source, lines)
    with pyspssio.Reader(str(temporary_source), mode="r") as reader, pyspssio.Writer(
        str(path), mode="w"
    ) as writer:
        writer.compression = 2
        writer._add_var("answer", 0)
        writer.copy_documents_from(reader)
        writer.commit_header()
        writer.write_data(pd.DataFrame({"answer": [1.0, 2.0]}))


def test_document_lines_import_from_zsav_and_export_to_sav(tmp_path: Path) -> None:
    source = tmp_path / "source.zsav"
    destination = tmp_path / "destination.sav"
    database = f"sqlite:///{tmp_path / 'documents.sqlite'}"
    expected = ["ZSAV document line."]
    _zsav_with_documents(source, expected, tmp_path / "temporary.sav")

    imported = openstatspec.import_sav(source, database_url=database, dataset_id="documents-zsav")
    assert imported.diagnostics == ()
    openstatspec.export_sav(
        database_url=database, dataset_id="documents-zsav", destination=destination,
    )
    assert read_document_lines(destination, encoding="UTF-8") == expected
    assert pyspssio.read_sav(str(destination))[0]["answer"].tolist() == [1.0, 2.0]
