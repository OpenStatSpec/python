from pathlib import Path
import shutil
import sqlite3
import subprocess

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.spss.raw_dictionary import (
    read_document_lines,
    write_compatible_names,
    write_document_lines,
)


def _source_with_documents(path: Path, lines: list[str]) -> None:
    pyspssio.write_sav(str(path), pd.DataFrame({"answer": [1.0, 2.0]}))
    write_document_lines(path, lines, encoding="UTF-8")


@pytest.mark.parametrize("destination_suffix", [".sav", ".zsav"])
def test_document_lines_round_trip_through_sqlite(destination_suffix: str, tmp_path: Path) -> None:
    source = tmp_path / "source.sav"
    destination = tmp_path / f"destination{destination_suffix}"
    database = f"sqlite:///{tmp_path / 'documents.sqlite'}"
    openstatspec.initialize_catalog(database_url=database)
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
    openstatspec.initialize_catalog(database_url=database)
    expected = ["ZSAV document line."]
    _zsav_with_documents(source, expected, tmp_path / "temporary.sav")

    imported = openstatspec.import_sav(source, database_url=database, dataset_id="documents-zsav")
    assert imported.diagnostics == ()
    openstatspec.export_sav(
        database_url=database, dataset_id="documents-zsav", destination=destination,
    )
    assert read_document_lines(destination, encoding="UTF-8") == expected
    assert pyspssio.read_sav(str(destination))[0]["answer"].tolist() == [1.0, 2.0]


def test_document_and_compatible_name_round_trip_to_zsav(tmp_path: Path) -> None:
    source = tmp_path / "source.sav"
    destination = tmp_path / "destination.zsav"
    database = f"sqlite:///{tmp_path / 'combined.sqlite'}"
    openstatspec.initialize_catalog(database_url=database)
    source_name = "long_variable_name"
    pyspssio.write_sav(str(source), pd.DataFrame({source_name: [7.0]}))
    write_document_lines(source, ["Combined dictionary fixture."], encoding="UTF-8")
    write_compatible_names(source, {source_name: "ANSWER"}, encoding="UTF-8")

    openstatspec.import_sav(source, database_url=database, dataset_id="combined")
    openstatspec.export_sav(database_url=database, dataset_id="combined", destination=destination)

    assert read_document_lines(destination, encoding="UTF-8") == ["Combined dictionary fixture."]
    assert pyspssio.read_metadata(str(destination))["var_compat_names"][source_name] == "ANSWER"
    assert pyspssio.read_sav(str(destination))[0][source_name].tolist() == [7.0]


def test_windows_1252_values_and_documents_round_trip_when_locale_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    localedef = shutil.which("localedef")
    charmap = Path("/usr/share/i18n/charmaps/CP1252.gz")
    source_definition = Path("/usr/share/i18n/locales/en_US")
    if localedef is None or not charmap.exists() or not source_definition.exists():
        pytest.skip("CP1252 locale source is unavailable on this host")
    locale_root = tmp_path / "locales"
    locale_root.mkdir()
    locale_name = "en_US.CP1252"
    subprocess.run(
        [localedef, "--no-archive", "-i", "en_US", "-f", "CP1252", str(locale_root / locale_name)],
        check=True,
    )
    monkeypatch.setenv("LOCPATH", str(locale_root))
    source = tmp_path / "source.sav"
    destination = tmp_path / "destination.zsav"
    database = f"sqlite:///{tmp_path / 'cp1252.sqlite'}"
    openstatspec.initialize_catalog(database_url=database)
    value = "Müller €"
    documents = ["Töö €"]
    pyspssio.write_sav(
        str(source), pd.DataFrame({"name": [value]}),
        unicode=False, locale=locale_name,
    )
    write_document_lines(source, documents, encoding="CP1252")

    imported = openstatspec.import_sav(source, database_url=database, dataset_id="cp1252")
    assert {item.code for item in imported.diagnostics} == {"source-encoding-not-preserved"}
    exported = openstatspec.export_sav(
        database_url=database, dataset_id="cp1252", destination=destination,
        legacy_locale=locale_name,
    )
    assert exported.diagnostics == ()
    assert pyspssio.read_metadata(str(destination))["encoding"].casefold() == "windows-1252"
    assert pyspssio.read_sav(str(destination))[0]["name"].tolist() == [value]
    assert read_document_lines(destination, encoding="CP1252") == documents
