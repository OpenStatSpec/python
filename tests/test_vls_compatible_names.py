import pandas as pd
import pyspssio
import pytest

import openstatspec
from openstatspec.spss import raw_dictionary


_SOURCE_NAME = "a05x_very_long_source_name"
_COMPATIBLE_NAME = "A05XM"
_VALUE = "\u00d5\U0001f642\u6f22\u5b57" * 30


def _write_vls_source(path) -> None:
    pyspssio.write_sav(
        str(path),
        pd.DataFrame({
            "before": [1.0, 2.0, 3.0],
            _SOURCE_NAME: [_VALUE, "", "tail"],
            "after": [4.0, 5.0, 6.0],
        }),
        metadata={"var_types": {_SOURCE_NAME: 360}},
    )


def _subtype_14_record(path):
    data = path.read_bytes()
    byte_order, records = raw_dictionary._records(data)
    matches = [
        record for record in records
        if record.record_type == 7
        and raw_dictionary._int(data, record.start + 4, byte_order) == 14
    ]
    assert len(matches) == 1
    return data, byte_order, matches[0]


def _subtype_14_entries(path) -> list[tuple[str, int]]:
    data, _, record = _subtype_14_record(path)
    entries, _ = raw_dictionary._very_long_string_entries(
        data[record.start + 16 : record.end],
    )
    return [(name, int(width)) for name, width in entries]


def _replace_subtype_14_payload(path, payload: bytes) -> None:
    data, byte_order, record = _subtype_14_record(path)
    header = bytearray(data[record.start : record.start + 16])
    header[8:12] = raw_dictionary._pack(1, byte_order)
    header[12:16] = raw_dictionary._pack(len(payload), byte_order)
    path.write_bytes(
        data[: record.start] + bytes(header) + payload + data[record.end :],
    )


@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_vls_round_trips_as_one_variable(tmp_path, suffix: str) -> None:
    source = tmp_path / f"source{suffix}"
    destination = tmp_path / f"destination{suffix}"
    database = f"sqlite:///{tmp_path / f'vls-{suffix[1:]}.sqlite'}"
    _write_vls_source(source)

    imported = openstatspec.import_sav(
        source, database_url=database, dataset_id=f"vls-{suffix[1:]}",
    )
    assert {diagnostic.code for diagnostic in imported.diagnostics} == {
        "compatible-variable-names-not-preserved",
    }
    exported = openstatspec.export_sav(
        database_url=database,
        dataset_id=f"vls-{suffix[1:]}",
        destination=destination,
        allow_loss=["compatible-variable-names-not-preserved"],
    )
    assert {diagnostic.code for diagnostic in exported.diagnostics} == {
        "compatible-variable-names-not-preserved",
    }

    metadata = pyspssio.read_metadata(str(destination))
    frame = pyspssio.read_sav(str(destination))[0]
    assert metadata["var_names"] == ["before", _SOURCE_NAME, "after"]
    assert metadata["var_types"][_SOURCE_NAME] == 360
    compatible_name = metadata["var_compat_names"][_SOURCE_NAME]
    assert list(frame.columns) == ["before", _SOURCE_NAME, "after"]
    assert frame[_SOURCE_NAME].tolist() == [_VALUE, "", "tail"]
    assert _subtype_14_entries(destination) == [(compatible_name, 360)]


@pytest.mark.parametrize("damage", ["malformed", "duplicate"])
def test_vls_rewrite_rejects_invalid_subtype_14_without_publishing(tmp_path, damage: str) -> None:
    source = tmp_path / "invalid.sav"
    _write_vls_source(source)
    data, _, record = _subtype_14_record(source)
    payload = data[record.start + 16 : record.end]
    if damage == "malformed":
        damaged = payload.replace(b"\x00", b"!", 1)
    else:
        entry = payload.removesuffix(b"\t")
        damaged = entry + b"\t" + entry
    _replace_subtype_14_payload(source, damaged)
    expected = source.read_bytes()

    with pytest.raises(raw_dictionary.RawDictionaryError):
        raw_dictionary.write_compatible_names(
            source, {_SOURCE_NAME: _COMPATIBLE_NAME}, encoding="UTF-8",
        )

    assert source.read_bytes() == expected
    assert list(tmp_path.glob(f".{source.name}.*.tmp")) == []
