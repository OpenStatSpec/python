from pathlib import Path
import re
import shutil
import sqlite3
import subprocess

import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.spss import raw_dictionary as raw_dictionary_module
from openstatspec.spss.raw_dictionary import (
    RawDictionaryError,
    read_document_lines,
    write_compatible_names,
    write_document_lines,
)
from openstatspec.sql.wide import initialize_wide_catalog


def _source_with_documents(path: Path, lines: list[str]) -> None:
    pyspssio.write_sav(str(path), pd.DataFrame({"answer": [1.0, 2.0]}))
    write_document_lines(path, lines, encoding="UTF-8")


def _reference_payloads(path: Path, subtype: int) -> tuple[bytes, ...]:
    data = path.read_bytes()
    byte_order, records = raw_dictionary_module._records(data)  # pylint: disable=protected-access
    payloads = []
    for record in records:
        if record.record_type != 7:
            continue
        observed_subtype = raw_dictionary_module._int(  # pylint: disable=protected-access
            data, record.start + 4, byte_order,
        )
        if observed_subtype != subtype:
            continue
        assert raw_dictionary_module._int(  # pylint: disable=protected-access
            data, record.start + 8, byte_order,
        ) == 1
        payloads.append(data[record.start + 16 : record.end])
    return tuple(payloads)


def _replace_expected_member_tokens(
    payloads: tuple[bytes, ...], replacements: dict[str, str],
) -> tuple[bytes, ...]:
    updated = []
    for payload in payloads:
        for old_name, new_name in replacements.items():
            pattern = rb"(?i)(?<!\S)" + re.escape(old_name.encode("ascii")) + rb"(?!\S)"
            matches = list(re.finditer(pattern, payload))
            assert len(matches) <= 1
            if matches:
                match = matches[0]
                payload = payload[:match.start()] + new_name.encode("ascii") + payload[match.end():]
        updated.append(payload)
    return tuple(updated)


def _change_reference_subtype(path: Path, old_subtype: int, new_subtype: int) -> None:
    data = bytearray(path.read_bytes())
    byte_order, records = raw_dictionary_module._records(data)  # pylint: disable=protected-access
    matches = [
        record for record in records
        if record.record_type == 7
        and raw_dictionary_module._int(  # pylint: disable=protected-access
            data, record.start + 4, byte_order,
        ) == old_subtype
    ]
    assert len(matches) == 1
    record = matches[0]
    data[record.start + 4 : record.start + 8] = raw_dictionary_module._pack(  # pylint: disable=protected-access
        new_subtype, byte_order,
    )
    path.write_bytes(data)


def _replace_reference_payload_bytes(
    path: Path, subtype: int, old: bytes, new: bytes,
) -> None:
    assert len(old) == len(new)
    data = bytearray(path.read_bytes())
    byte_order, records = raw_dictionary_module._records(data)  # pylint: disable=protected-access
    matches = [
        record for record in records
        if record.record_type == 7
        and raw_dictionary_module._int(  # pylint: disable=protected-access
            data, record.start + 4, byte_order,
        ) == subtype
    ]
    assert len(matches) == 1
    record = matches[0]
    payload = bytes(data[record.start + 16 : record.end])
    matches = list(re.finditer(re.escape(old), payload, flags=re.IGNORECASE))
    assert len(matches) == 1
    match = matches[0]
    data[record.start + 16 : record.end] = (
        payload[:match.start()] + new + payload[match.end():]
    )
    path.write_bytes(data)


@pytest.mark.parametrize("destination_suffix", [".sav", ".zsav"])
def test_document_lines_round_trip_through_sqlite(destination_suffix: str, tmp_path: Path) -> None:
    source = tmp_path / "source.sav"
    destination = tmp_path / f"destination{destination_suffix}"
    database = f"sqlite:///{tmp_path / 'documents.sqlite'}"
    expected = ["Imported from a validated source.", "A second document line."]
    _source_with_documents(source, expected)

    initialize_wide_catalog(database_url=database)
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

    initialize_wide_catalog(database_url=database)
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
    source_name = "long_variable_name"
    pyspssio.write_sav(str(source), pd.DataFrame({source_name: [7.0]}))
    write_document_lines(source, ["Combined dictionary fixture."], encoding="UTF-8")
    write_compatible_names(source, {source_name: "ANSWER"}, encoding="UTF-8")

    initialize_wide_catalog(database_url=database)
    openstatspec.import_sav(source, database_url=database, dataset_id="combined")
    openstatspec.export_sav(database_url=database, dataset_id="combined", destination=destination)

    assert read_document_lines(destination, encoding="UTF-8") == ["Combined dictionary fixture."]
    assert pyspssio.read_metadata(str(destination))["var_compat_names"][source_name] == "ANSWER"
    assert pyspssio.read_sav(str(destination))[0][source_name].tolist() == [7.0]


@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_vls_long_name_and_custom_compatible_name_round_trip(
    tmp_path: Path, suffix: str,
) -> None:
    source = tmp_path / f"vls-source{suffix}"
    destination = tmp_path / f"vls-destination{suffix}"
    database = f"sqlite:///{tmp_path / f'vls-{suffix[1:]}.sqlite'}"
    source_name = "long_text_response_variable"
    compatible_name = "TXTRESP"
    payload = "Õ🙂漢字" * 90
    pyspssio.write_sav(str(source), pd.DataFrame({source_name: [payload, "short"]}))
    write_compatible_names(source, {source_name: compatible_name}, encoding="UTF-8")

    source_metadata = pyspssio.read_metadata(str(source))
    assert source_metadata["var_types"][source_name] == len(payload.encode("utf-8"))
    assert source_metadata["var_compat_names"][source_name] == compatible_name

    initialize_wide_catalog(database_url=database)
    openstatspec.import_sav(
        source, database_url=database, dataset_id=f"vls-{suffix[1:]}",
    )
    openstatspec.export_sav(
        database_url=database, dataset_id=f"vls-{suffix[1:]}", destination=destination,
    )

    frame, metadata = pyspssio.read_sav(str(destination), convert_datetimes=False)
    assert list(frame.columns) == [source_name]
    assert frame[source_name].tolist() == [payload, "short"]
    assert metadata["var_types"][source_name] == len(payload.encode("utf-8"))
    assert metadata["var_compat_names"][source_name] == compatible_name


def test_two_vls_requested_compatible_names_must_be_case_insensitively_unique(
    tmp_path: Path,
) -> None:
    source = tmp_path / "duplicate-requested-vls.sav"
    source_names = ["first_long_text_variable", "second_long_text_variable"]
    pyspssio.write_sav(
        str(source),
        pd.DataFrame({source_name: ["x" * 300] for source_name in source_names}),
    )
    original = source.read_bytes()

    with pytest.raises(RawDictionaryError, match="Duplicate compatible variable"):
        write_compatible_names(
            source,
            {source_names[0]: "Shared", source_names[1]: "sHARED"},
            encoding="UTF-8",
        )

    assert source.read_bytes() == original


def test_compatible_name_rewrite_rejects_non_unique_type_2_name_without_writing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "duplicate-compatible-name.sav"
    source_name = "long_numeric_response_variable"
    pyspssio.write_sav(
        str(source), pd.DataFrame({source_name: [1.0], "taken": [2.0]}),
    )
    original = source.read_bytes()

    with pytest.raises(RawDictionaryError, match="Duplicate compatible variable"):
        write_compatible_names(source, {source_name: "TAKEN"}, encoding="UTF-8")

    assert source.read_bytes() == original


def test_compatible_name_rewrite_rejects_malformed_subtype_14_without_writing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "malformed-vls.sav"
    source_name = "long_text_response_variable"
    pyspssio.write_sav(str(source), pd.DataFrame({source_name: ["x" * 300]}))
    metadata = pyspssio.read_metadata(str(source))
    short_name = metadata["var_compat_names"][source_name].encode("ascii")
    valid_entry = short_name + b"=300\x00	"
    malformed_entry = short_name + b"=300X	"
    data = source.read_bytes()
    assert data.count(valid_entry) == 1
    source.write_bytes(data.replace(valid_entry, malformed_entry))
    corrupted = source.read_bytes()

    with pytest.raises(RawDictionaryError, match="entry terminator"):
        write_compatible_names(source, {source_name: "TEXTRESP"}, encoding="UTF-8")

    assert source.read_bytes() == corrupted


def test_compatible_name_rewrite_rejects_duplicate_subtype_14_names(
    tmp_path: Path,
) -> None:
    source = tmp_path / "duplicate-vls.sav"
    source_names = ["first_long_text_variable", "second_long_text_variable"]
    pyspssio.write_sav(
        str(source),
        pd.DataFrame({source_name: ["x" * 300] for source_name in source_names}),
    )
    metadata = pyspssio.read_metadata(str(source))
    first_short = metadata["var_compat_names"][source_names[0]].encode("ascii")
    second_short = metadata["var_compat_names"][source_names[1]].encode("ascii")
    data = source.read_bytes()
    second_entry = second_short + b"=300\x00	"
    duplicate_entry = first_short + b"=300\x00	"
    assert len(second_entry) == len(duplicate_entry)
    assert data.count(second_entry) == 1
    source.write_bytes(data.replace(second_entry, duplicate_entry))
    corrupted = source.read_bytes()

    with pytest.raises(RawDictionaryError, match="Duplicate subtype-14"):
        write_compatible_names(
            source, {source_names[0]: "FIRSTTXT"}, encoding="UTF-8",
        )

    assert source.read_bytes() == corrupted


def test_compatible_name_rewrite_updates_subtype_5_variable_set_members(
    tmp_path: Path,
) -> None:
    source = tmp_path / "variable-set-reference.sav"
    source_names = ["long_analysis_variable", "second_analysis_variable"]
    with pyspssio.Writer(str(source), mode="w") as writer:
        for name in source_names:
            writer._add_var(name, 0)  # pylint: disable=protected-access
        writer.var_sets = {"Analysis": source_names}
        writer.commit_header()
        writer.write_data(pd.DataFrame({name: [1.0] for name in source_names}))
    before = pyspssio.read_metadata(str(source))
    replacements = {
        before["var_compat_names"][source_names[0]]: "ANALYZE",
        before["var_compat_names"][source_names[1]]: "SECOND",
    }
    before_payloads = _reference_payloads(source, 5)

    write_compatible_names(
        source,
        {source_names[0]: "ANALYZE", source_names[1]: "SECOND"},
        encoding="UTF-8",
    )

    with pyspssio.Reader(str(source), mode="r") as reader:
        assert reader.var_compat_names[source_names[0]] == "ANALYZE"
        assert reader.var_compat_names[source_names[1]] == "SECOND"
        assert reader.var_sets == {"Analysis": source_names}
    assert _reference_payloads(source, 5) == _replace_expected_member_tokens(
        before_payloads, replacements,
    )


def test_compatible_name_rewrite_updates_subtype_7_c_and_d_members(
    tmp_path: Path,
) -> None:
    source = tmp_path / "standard-mrsets.sav"
    source_names = [
        "long_category_first", "long_category_second",
        "long_dichotomy_first", "long_dichotomy_second",
    ]
    frame = pd.DataFrame({name: [1.0, 0.0] for name in source_names})
    expected_mrsets = {
        "$categories": {
            "label": "Category choices",
            "variable_list": source_names[:2],
        },
        "$dichotomies": {
            "label": "Dichotomy choices",
            "counted_value": 1,
            "variable_list": source_names[2:],
        },
    }
    pyspssio.write_sav(str(source), frame, metadata={"mrsets": expected_mrsets})
    before = pyspssio.read_metadata(str(source))
    requested = {
        source_names[0]: "CATONE",
        source_names[2]: "DICHONE",
    }
    replacements = {
        before["var_compat_names"][name]: compatible
        for name, compatible in requested.items()
    }
    before_payloads = _reference_payloads(source, 7)
    assert before_payloads

    write_compatible_names(source, requested, encoding="UTF-8")

    after = pyspssio.read_metadata(str(source))
    assert after["mrsets"] == before["mrsets"]
    assert {name: after["var_compat_names"][name] for name in requested} == requested
    assert _reference_payloads(source, 7) == _replace_expected_member_tokens(
        before_payloads, replacements,
    )


def test_compatible_name_rewrite_updates_subtype_19_e_members(
    tmp_path: Path,
) -> None:
    source = tmp_path / "extended-mrset.sav"
    source_names = ["long_text_first", "long_text_second"]
    frame = pd.DataFrame({
        source_names[0]: ["yes", "no"],
        source_names[1]: ["no", "yes"],
    })
    pyspssio.write_sav(
        str(source), frame,
        metadata={
            "var_types": {name: 8 for name in source_names},
            "mrsets": {
                "$contact_text": {
                    "label": "Text contact choices",
                    "is_dichotomy": True,
                    "counted_value": "yes",
                    "use_category_labels": True,
                    "use_first_var_label": False,
                    "variable_list": source_names,
                },
            },
        },
    )
    before = pyspssio.read_metadata(str(source))
    requested = {source_names[0]: "TEXTONE", source_names[1]: "TEXTTWO"}
    replacements = {
        before["var_compat_names"][name]: compatible
        for name, compatible in requested.items()
    }
    before_payloads = _reference_payloads(source, 19)
    assert before_payloads

    write_compatible_names(source, requested, encoding="UTF-8")

    after = pyspssio.read_metadata(str(source))
    assert after["mrsets"] == before["mrsets"]
    assert {name: after["var_compat_names"][name] for name in requested} == requested
    assert _reference_payloads(source, 19) == _replace_expected_member_tokens(
        before_payloads, replacements,
    )


@pytest.mark.parametrize(
    ("is_dichotomy", "set_name", "new_compatible"),
    [
        (False, "$category19", "CAT19"),
        (True, "$dichotomy19", "DICH19"),
    ],
)
def test_compatible_name_rewrite_updates_subtype_19_c_and_d_members(
    tmp_path: Path, is_dichotomy: bool, set_name: str, new_compatible: str,
) -> None:
    source = tmp_path / f"subtype-19-{'d' if is_dichotomy else 'c'}.sav"
    source_names = ["long_member_first", "long_member_second"]
    definition = {
        "label": "Subtype 19 definition",
        "variable_list": source_names,
    }
    if is_dichotomy:
        definition["counted_value"] = 1
    pyspssio.write_sav(
        str(source),
        pd.DataFrame({name: [1.0, 0.0] for name in source_names}),
        metadata={"mrsets": {set_name: definition}},
    )
    _change_reference_subtype(source, 7, 19)
    before = pyspssio.read_metadata(str(source))
    old_compatible = before["var_compat_names"][source_names[0]]
    before_payloads = _reference_payloads(source, 19)
    assert before_payloads

    write_compatible_names(
        source, {source_names[0]: new_compatible}, encoding="UTF-8",
    )

    after = pyspssio.read_metadata(str(source))
    assert after["mrsets"] == before["mrsets"]
    assert after["var_compat_names"][source_names[0]] == new_compatible
    assert _reference_payloads(source, 19) == _replace_expected_member_tokens(
        before_payloads, {old_compatible: new_compatible},
    )


def test_duplicate_subtype_19_set_name_fails_without_source_change(
    tmp_path: Path,
) -> None:
    source = tmp_path / "duplicate-subtype-19-set.sav"
    source_names = ["long_member_first", "long_member_second"]
    pyspssio.write_sav(
        str(source),
        pd.DataFrame({name: [1.0] for name in source_names}),
        metadata={
            "mrsets": {
                "$first": {"label": "First", "variable_list": source_names},
                "$secon": {"label": "Second", "variable_list": source_names},
            },
        },
    )
    _change_reference_subtype(source, 7, 19)
    _replace_reference_payload_bytes(source, 19, b"$secon=", b"$first=")
    original = source.read_bytes()

    with pytest.raises(RawDictionaryError, match="Duplicate subtype-19 set name"):
        write_compatible_names(
            source, {source_names[0]: "MEMBER1"}, encoding="UTF-8",
        )

    assert source.read_bytes() == original


def test_unknown_subtype_19_member_fails_without_source_change(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unknown-subtype-19-member.sav"
    source_names = ["long_member_first", "long_member_second"]
    pyspssio.write_sav(
        str(source),
        pd.DataFrame({name: [1.0] for name in source_names}),
        metadata={
            "mrsets": {
                "$members": {"label": "Members", "variable_list": source_names},
            },
        },
    )
    _change_reference_subtype(source, 7, 19)
    metadata = pyspssio.read_metadata(str(source))
    old_member = metadata["var_compat_names"][source_names[0]]
    known_names = {value.casefold() for value in metadata["var_compat_names"].values()}
    unknown_member = "U" * len(old_member)
    if unknown_member.casefold() in known_names:
        unknown_member = "Z" * len(old_member)
    assert unknown_member.casefold() not in known_names
    _replace_reference_payload_bytes(
        source, 19, old_member.encode("ascii"), unknown_member.encode("ascii"),
    )
    original = source.read_bytes()

    with pytest.raises(RawDictionaryError, match="references unknown variable"):
        write_compatible_names(
            source, {source_names[1]: "MEMBER2"}, encoding="UTF-8",
        )

    assert source.read_bytes() == original


def test_combined_zsav_reference_rewrite_preserves_offsets_and_semantics(
    tmp_path: Path,
) -> None:
    source = tmp_path / "combined-references.zsav"
    numeric_names = [
        "long_category_first", "long_category_second",
        "long_dichotomy_first", "long_dichotomy_second",
    ]
    text_names = ["long_text_first", "long_text_second"]
    vls_name = "long_very_long_text_variable"
    all_names = numeric_names + text_names + [vls_name]
    with pyspssio.Writer(str(source), mode="w") as writer:
        writer.compression = 2
        for name in numeric_names:
            writer._add_var(name, 0)  # pylint: disable=protected-access
        for name in text_names:
            writer._add_var(name, 8)  # pylint: disable=protected-access
        writer._add_var(vls_name, 300)  # pylint: disable=protected-access
        writer.var_sets = {"Analysis": all_names}
        writer.mrsets = {
            "$categories": {"label": "Categories", "variable_list": numeric_names[:2]},
            "$dichotomies": {
                "label": "Dichotomies", "counted_value": 1,
                "variable_list": numeric_names[2:],
            },
            "$extended": {
                "label": "Extended", "is_dichotomy": True,
                "counted_value": "yes", "use_category_labels": True,
                "use_first_var_label": False, "variable_list": text_names,
            },
        }
        writer.commit_header()
        writer.write_data(pd.DataFrame({
            **{name: [1.0, 0.0] for name in numeric_names},
            text_names[0]: ["yes", "no"],
            text_names[1]: ["no", "yes"],
            vls_name: ["x" * 300, "short"],
        }))
    before = pyspssio.read_metadata(str(source))
    with pyspssio.Reader(str(source), mode="r") as reader:
        before_variable_sets = reader.var_sets
    requested = {
        numeric_names[0]: "CAT",
        numeric_names[2]: "DICH",
        text_names[0]: "TEXT",
        vls_name: "VLS",
    }

    write_compatible_names(source, requested, encoding="UTF-8")

    frame, after = pyspssio.read_sav(str(source), convert_datetimes=False)
    assert list(frame.columns) == all_names
    assert frame[vls_name].tolist() == ["x" * 300, "short"]
    assert after["mrsets"] == before["mrsets"]
    with pyspssio.Reader(str(source), mode="r") as reader:
        assert reader.var_sets == before_variable_sets
    assert {name: after["var_compat_names"][name] for name in requested} == requested
    assert _reference_payloads(source, 5)
    assert _reference_payloads(source, 7)
    assert _reference_payloads(source, 19)


def test_reader_readback_failure_preserves_source_and_removes_private_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "readback-failure.sav"
    source_name = "long_analysis_variable"
    with pyspssio.Writer(str(source), mode="w") as writer:
        writer._add_var(source_name, 0)  # pylint: disable=protected-access
        writer.var_sets = {"Analysis": [source_name]}
        writer.commit_header()
        writer.write_data(pd.DataFrame({source_name: [1.0]}))
    original = source.read_bytes()
    real_reader = raw_dictionary_module._read_dictionary_semantics  # pylint: disable=protected-access

    def fail_candidate_readback(path):
        if Path(path) != source:
            assert Path(path).stat().st_mode & 0o777 == 0o600
            raise RawDictionaryError("synthetic Reader readback failure")
        return real_reader(path)

    monkeypatch.setattr(
        raw_dictionary_module, "_read_dictionary_semantics", fail_candidate_readback,
    )
    with pytest.raises(RawDictionaryError, match="synthetic Reader readback failure"):
        write_compatible_names(source, {source_name: "ANALYZE"}, encoding="UTF-8")

    assert source.read_bytes() == original
    assert list(tmp_path.glob(f".{source.stem}.*.tmp{source.suffix}")) == []


def test_final_shifted_bytes_are_validated_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "invalid-shift-result.zsav"
    source_name = "long_text_response_variable"
    pyspssio.write_sav(str(source), pd.DataFrame({source_name: ["x" * 300]}))
    original = source.read_bytes()

    monkeypatch.setattr(
        raw_dictionary_module, "_shift_zsav_offsets",
        lambda *_args, **_kwargs: b"invalid shifted bytes",
    )
    with pytest.raises(RawDictionaryError, match="supported SAV or ZSAV"):
        write_compatible_names(source, {source_name: "TXT"}, encoding="UTF-8")

    assert source.read_bytes() == original


def test_atomic_publish_failure_preserves_source_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "partial-publish.sav"
    source_name = "long_text_response_variable"
    pyspssio.write_sav(str(source), pd.DataFrame({source_name: ["x" * 300]}))
    original = source.read_bytes()

    def partial_write_then_fail(stream, data):
        stream.write(data[:32])
        raise OSError("synthetic partial temporary write")

    monkeypatch.setattr(raw_dictionary_module, "_write_all", partial_write_then_fail)
    with pytest.raises(OSError, match="synthetic partial temporary write"):
        write_compatible_names(source, {source_name: "TXT"}, encoding="UTF-8")

    assert source.read_bytes() == original
    assert list(tmp_path.glob(f".{source.stem}.*.tmp{source.suffix}")) == []


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
    value = "Müller €"
    documents = ["Töö €"]
    pyspssio.write_sav(
        str(source), pd.DataFrame({"name": [value]}),
        unicode=False, locale=locale_name,
    )
    write_document_lines(source, documents, encoding="CP1252")

    initialize_wide_catalog(database_url=database)
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


def test_legacy_source_unicode_export_uses_actual_output_encoding_for_raw_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    localedef = shutil.which("localedef")
    charmap = Path("/usr/share/i18n/charmaps/CP1252.gz")
    source_definition = Path("/usr/share/i18n/locales/en_US")
    if localedef is None or not charmap.exists() or not source_definition.exists():
        pytest.skip("CP1252 locale source is unavailable on this host")
    locale_root = tmp_path / "unicode-export-locales"
    locale_root.mkdir()
    locale_name = "en_US.CP1252"
    subprocess.run(
        [localedef, "--no-archive", "-i", "en_US", "-f", "CP1252", str(locale_root / locale_name)],
        check=True,
    )
    monkeypatch.setenv("LOCPATH", str(locale_root))
    source = tmp_path / "legacy-source.sav"
    destination = tmp_path / "unicode-output.sav"
    database = f"sqlite:///{tmp_path / 'legacy-to-unicode.sqlite'}"
    source_names = ["kusimuse_pikk_nimi", "teine_pikk_nimi"]
    mr_label = "Mitmikvalik – küsimus"
    frame = pd.DataFrame({source_names[0]: [1.0, 0.0], source_names[1]: [0.0, 1.0]})
    pyspssio.write_sav(
        str(source), frame, unicode=False, locale=locale_name,
        metadata={
            "var_labels": {source_names[0]: "Esimene vastus"},
            "mrsets": {
                "$vastused": {
                    "label": mr_label,
                    "counted_value": 1,
                    "use_category_labels": True,
                    "use_first_var_label": True,
                    "variable_list": source_names,
                },
            },
        },
    )
    source_encoding = str(pyspssio.read_metadata(str(source))["encoding"])
    raw_dictionary_module.write_extended_mrset_labels(
        source, {"$vastused": mr_label}, encoding=source_encoding,
    )
    write_compatible_names(
        source, {source_names[0]: "CUSTOM"}, encoding=source_encoding,
    )

    initialize_wide_catalog(database_url=database)
    imported = openstatspec.import_sav(
        source, database_url=database, dataset_id="legacy-to-unicode",
    )
    assert {diagnostic.code for diagnostic in imported.diagnostics} == {
        "source-encoding-not-preserved",
    }
    exported = openstatspec.export_sav(
        database_url=database,
        dataset_id="legacy-to-unicode",
        destination=destination,
        allow_loss=["source-encoding-not-preserved"],
    )

    assert {diagnostic.code for diagnostic in exported.diagnostics} == {
        "source-encoding-not-preserved",
    }
    output_frame, output_metadata = pyspssio.read_sav(
        str(destination), convert_datetimes=False,
    )
    assert output_metadata["encoding"].casefold() in {"utf-8", "utf8"}
    assert output_metadata["var_names"] == source_names
    assert output_metadata["var_compat_names"][source_names[0]] == "CUSTOM"
    assert output_metadata["mrsets"]["$vastused"]["label"] == mr_label
    assert output_metadata["mrsets"]["$vastused"]["variable_list"] == source_names
    assert output_frame[source_names[0]].tolist() == [1.0, 0.0]
