"""Small, fail-closed helpers for selected raw SPSS dictionary records.

This is not a second SAV engine: pyspssio still owns values and the normal
dictionary. IBM I/O exposes copying document records but not their text.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable


class RawDictionaryError(ValueError):
    """Raw dictionary data cannot be handled without risking silent loss."""


_HEADER_SIZE = 176


@dataclass(frozen=True)
class _Record:
    record_type: int
    start: int
    end: int


def read_document_lines(path: str | Path, *, encoding: str) -> list[str]:
    """Read standard 80-byte SAV document lines without touching case data."""
    data = Path(path).read_bytes()
    byte_order, records = _records(data)
    documents = [record for record in records if record.record_type == 6]
    if len(documents) > 1:
        raise RawDictionaryError("SAV dictionary contains more than one document record.")
    if not documents:
        return []
    document = documents[0]
    count = _int(data, document.start + 4, byte_order)
    start = document.start + 8
    return [
        data[offset : offset + 80].decode(encoding).rstrip(" ")
        for offset in range(start, start + (count * 80), 80)
    ]


def write_document_lines(path: str | Path, lines: Iterable[str], *, encoding: str) -> None:
    """Replace the type-6 record and retain all non-document bytes."""
    target = Path(path)
    data = target.read_bytes()
    byte_order, records = _records(data)
    documents = [record for record in records if record.record_type == 6]
    if len(documents) > 1:
        raise RawDictionaryError("SAV dictionary contains more than one document record.")
    replacement = _document_record(lines, encoding=encoding, byte_order=byte_order)
    terminator = next(record for record in records if record.record_type == 999)
    if documents:
        current = documents[0]
        updated = data[: current.start] + replacement + data[current.end :]
    else:
        extension = next((record for record in records if record.record_type == 7), None)
        insertion = extension.start if extension else terminator.start
        updated = data[:insertion] + replacement + data[insertion:]
    target.write_bytes(_shift_zsav_offsets(
        updated,
        original_data_start=terminator.end,
        delta=len(updated) - len(data),
        byte_order=byte_order,
    ))


def write_compatible_names(
    path: str | Path, names: dict[str, str], *, encoding: str,
) -> None:
    """Set exact legacy short names consistently in type-2/13/14 records.

    IBM I/O has no compatible-name setter. Every requested source name must
    have a long-name record, and every very-long-string key must agree with the
    type-2 and subtype-13 records. The fully rebuilt dictionary is reparsed
    before an atomic replacement is published.
    """
    if not names:
        return
    target = Path(path)
    data = target.read_bytes()
    byte_order, records = _records(data)
    terminator = next(record for record in records if record.record_type == 999)
    long_name_records = [
        record for record in records
        if record.record_type == 7 and _int(data, record.start + 4, byte_order) == 13
    ]
    if not long_name_records:
        raise RawDictionaryError("SAV dictionary has no long-variable-name record.")
    if len(long_name_records) != 1:
        raise RawDictionaryError("SAV dictionary has more than one long-variable-name record.")
    long_names = long_name_records[0]
    if _int(data, long_names.start + 8, byte_order) != 1:
        raise RawDictionaryError("Invalid SAV long-variable-name record dimensions.")
    pairs = _long_name_pairs(data[long_names.start + 16 : long_names.end], encoding)
    replacements: dict[str, str] = {}
    updated_pairs: list[tuple[str, str]] = []
    unresolved = set(names)
    for short_name, long_name in pairs:
        replacement = names.get(long_name)
        if replacement is None:
            updated_pairs.append((short_name, long_name))
            continue
        replacements[short_name] = _validated_compatible_name(replacement)
        updated_pairs.append((replacements[short_name], long_name))
        unresolved.remove(long_name)
    if unresolved:
        missing = ", ".join(sorted(unresolved))
        raise RawDictionaryError(
            "Compatible-name update requires a long-name record for: " + missing
        )
    updated_short_names = [short_name.casefold() for short_name, _ in updated_pairs]
    if len(updated_short_names) != len(set(updated_short_names)):
        raise RawDictionaryError("Compatible-name update would create duplicate short names.")

    type_2_names = _type_2_names(data, records, byte_order)
    for short_name in replacements:
        if type_2_names.count(short_name) != 1:
            raise RawDictionaryError(
                f"Expected exactly one type-2 record for compatible name {short_name!r}."
            )

    long_name_by_short = {short_name: long_name for short_name, long_name in pairs}
    vls_records = [
        record for record in records
        if record.record_type == 7 and _int(data, record.start + 4, byte_order) == 14
    ]
    parsed_vls: dict[int, tuple[list[tuple[str, bytes]], bool]] = {}
    seen_vls: set[str] = set()
    vls_replacements: dict[str, str] = {}
    vls_source_names: set[str] = set()
    for record in vls_records:
        if _int(data, record.start + 8, byte_order) != 1:
            raise RawDictionaryError("Invalid SAV very-long-string record dimensions.")
        entries, trailing_tab = _very_long_string_entries(data[record.start + 16 : record.end])
        parsed_vls[record.start] = (entries, trailing_tab)
        for short_name, _ in entries:
            normalized = short_name.casefold()
            if normalized in seen_vls:
                raise RawDictionaryError("Duplicate SAV very-long-string entry.")
            seen_vls.add(normalized)
            if short_name not in long_name_by_short or type_2_names.count(short_name) != 1:
                raise RawDictionaryError(
                    "SAV type-2/subtype-13/subtype-14 names are inconsistent."
                )
            replacement = replacements.get(short_name)
            if replacement is not None:
                vls_replacements[short_name] = replacement
                vls_source_names.add(long_name_by_short[short_name])

    mutable = bytearray(data)
    for record in records:
        if record.record_type != 2:
            continue
        short_name = bytes(mutable[record.start + 24 : record.start + 32]).decode(encoding).rstrip(" ")
        replacement = replacements.get(short_name)
        if replacement is not None:
            mutable[record.start + 24 : record.start + 32] = replacement.encode("ascii").ljust(8, b" ")

    new_payload = b"\t".join(
        short_name.encode("ascii") + b"=" + long_name.encode(encoding)
        for short_name, long_name in updated_pairs
    )
    replacement_records = {
        long_names.start: _extension_record(
            mutable, long_names, new_payload, byte_order=byte_order,
        ),
    }
    for record in vls_records:
        entries, trailing_tab = parsed_vls[record.start]
        rewritten = [
            (vls_replacements.get(short_name, short_name), width)
            for short_name, width in entries
        ]
        vls_payload = _very_long_string_payload(rewritten, trailing_tab=trailing_tab)
        replacement_records[record.start] = _extension_record(
            mutable, record, vls_payload, byte_order=byte_order,
        )

    chunks: list[bytes] = []
    cursor = 0
    for record in sorted(
        (record for record in records if record.start in replacement_records),
        key=lambda item: item.start,
    ):
        chunks.append(bytes(mutable[cursor : record.start]))
        chunks.append(replacement_records[record.start])
        cursor = record.end
    chunks.append(bytes(mutable[cursor:]))
    updated = b"".join(chunks)
    updated = _shift_zsav_offsets(
        updated,
        original_data_start=terminator.end,
        delta=len(updated) - len(data),
        byte_order=byte_order,
    )
    _assert_compatible_name_consistency(
        updated,
        requested=names,
        vls_source_names=vls_source_names,
        encoding=encoding,
    )
    _atomic_write_bytes(target, updated)


def write_extended_mrset_labels(
    path: str | Path, labels: dict[str, str], *, encoding: str,
) -> None:
    """Restore explicit labels that IBM I/O drops from extended MR sets.

    When an extended dichotomy set uses the first variable label, the IBM
    writer can emit a zero-length set label even if the source dictionary
    carried both pieces of metadata.  Rewrite only subtype-19 label fields;
    all flags, counted values, member names, and case data remain untouched.
    """
    if not labels:
        return
    target = Path(path)
    data = target.read_bytes()
    byte_order, records = _records(data)
    terminator = next(record for record in records if record.record_type == 999)
    extensions = [
        record for record in records
        if record.record_type == 7 and _int(data, record.start + 4, byte_order) == 19
    ]
    if len(extensions) != 1:
        raise RawDictionaryError("Expected exactly one extended multiple-response record.")
    record = extensions[0]
    payload = data[record.start + 16 : record.end]
    unresolved = set(labels)
    output: list[bytes] = []
    for line in payload.rstrip(b"\x00\n").splitlines():
        raw_name, raw_definition = line.split(b"=", 1)
        name = raw_name.decode(encoding)
        label = labels.get(name)
        if label is None:
            output.append(line)
            continue
        fields = raw_definition.split(b" ", 5)
        if len(fields) != 6 or fields[0] != b"E":
            raise RawDictionaryError(
                f"Multiple-response set {name!r} is not an extended subtype-19 definition."
            )
        try:
            old_length = int(fields[4].decode("ascii"))
        except (UnicodeDecodeError, ValueError) as error:
            raise RawDictionaryError("Invalid extended multiple-response label length.") from error
        remainder = fields[5][old_length:]
        if remainder.startswith(b" "):
            remainder = remainder[1:]
        encoded = label.encode(encoding)
        definition = b" ".join([
            fields[0], fields[1], fields[2], fields[3],
            str(len(encoded)).encode("ascii"), encoded,
        ])
        if remainder:
            definition += b" " + remainder
        output.append(raw_name + b"=" + definition)
        unresolved.remove(name)
    if unresolved:
        raise RawDictionaryError(
            "Extended multiple-response labels not found: " + ", ".join(sorted(unresolved))
        )
    new_payload = b"\n".join(output) + b"\n"
    header = bytearray(data[record.start : record.start + 16])
    header[8:12] = _pack(1, byte_order)
    header[12:16] = _pack(len(new_payload), byte_order)
    updated = data[: record.start] + bytes(header) + new_payload + data[record.end :]
    target.write_bytes(_shift_zsav_offsets(
        updated,
        original_data_start=terminator.end,
        delta=len(updated) - len(data),
        byte_order=byte_order,
    ))


def _long_name_pairs(payload: bytes, encoding: str) -> list[tuple[str, str]]:
    if not payload:
        raise RawDictionaryError("Empty SAV long-variable-name record.")
    pairs: list[tuple[str, str]] = []
    short_names: set[str] = set()
    long_names: set[str] = set()
    for raw_pair in payload.split(b"\t"):
        try:
            raw_short, raw_long = raw_pair.split(b"=", maxsplit=1)
            short_name = _dictionary_short_name(raw_short)
            long_name = raw_long.decode(encoding)
        except (UnicodeDecodeError, ValueError) as error:
            raise RawDictionaryError("Invalid SAV long-variable-name record.") from error
        normalized_short = short_name.casefold()
        normalized_long = long_name.casefold()
        if not long_name or normalized_short in short_names or normalized_long in long_names:
            raise RawDictionaryError("Duplicate or empty SAV long-variable-name entry.")
        short_names.add(normalized_short)
        long_names.add(normalized_long)
        pairs.append((short_name, long_name))
    return pairs


def _very_long_string_entries(payload: bytes) -> tuple[list[tuple[str, bytes]], bool]:
    """Parse subtype-14 entries without accepting ambiguous separators or widths."""
    if not payload:
        raise RawDictionaryError("Empty SAV very-long-string record.")
    raw_entries = payload.split(b"\t")
    trailing_tab = raw_entries[-1] == b""
    if trailing_tab:
        raw_entries.pop()
    if not raw_entries or any(not entry for entry in raw_entries):
        raise RawDictionaryError("Invalid SAV very-long-string separators.")
    entries: list[tuple[str, bytes]] = []
    for raw_entry in raw_entries:
        if not raw_entry.endswith(b"\x00"):
            raise RawDictionaryError("SAV very-long-string entry is missing its NUL terminator.")
        try:
            raw_short, raw_width = raw_entry[:-1].split(b"=", maxsplit=1)
            short_name = _dictionary_short_name(raw_short)
            if not raw_width or not raw_width.isdigit():
                raise ValueError
            width = int(raw_width.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as error:
            raise RawDictionaryError("Invalid SAV very-long-string entry.") from error
        if not 256 <= width <= 32767:
            raise RawDictionaryError("SAV very-long-string width is outside 256..32767.")
        entries.append((short_name, raw_width))
    return entries, trailing_tab


def _very_long_string_payload(
    entries: Iterable[tuple[str, bytes]], *, trailing_tab: bool,
) -> bytes:
    payload = b"\t".join(
        short_name.encode("ascii") + b"=" + width + b"\x00"
        for short_name, width in entries
    )
    return payload + (b"\t" if trailing_tab else b"")


def _dictionary_short_name(raw_name: bytes) -> str:
    try:
        name = raw_name.decode("ascii")
    except UnicodeDecodeError as error:
        raise RawDictionaryError("SAV dictionary short name is not ASCII.") from error
    allowed_first = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz@#$"
    allowed_rest = allowed_first + b"0123456789_."
    if not 1 <= len(raw_name) <= 8 or raw_name[:1] not in allowed_first:
        raise RawDictionaryError("Invalid SAV dictionary short name.")
    if any(character not in allowed_rest for character in raw_name[1:]):
        raise RawDictionaryError("Invalid SAV dictionary short name.")
    return name


def _type_2_names(data: bytes, records: list[_Record], byte_order: str) -> list[str]:
    names: list[str] = []
    for record in records:
        if record.record_type != 2 or _int(data, record.start + 4, byte_order) < 0:
            continue
        raw_name = data[record.start + 24 : record.start + 32].rstrip(b" ")
        names.append(_dictionary_short_name(raw_name))
    return names


def _extension_record(
    data: bytes, record: _Record, payload: bytes, *, byte_order: str,
) -> bytes:
    header = bytearray(data[record.start : record.start + 16])
    header[8:12] = _pack(1, byte_order)
    header[12:16] = _pack(len(payload), byte_order)
    return bytes(header) + payload


def _assert_compatible_name_consistency(
    data: bytes,
    *,
    requested: dict[str, str],
    vls_source_names: set[str],
    encoding: str,
) -> None:
    byte_order, records = _records(data)
    type_2_names = _type_2_names(data, records, byte_order)
    normalized_type_2 = [name.casefold() for name in type_2_names]
    long_name_records = [
        record for record in records
        if record.record_type == 7 and _int(data, record.start + 4, byte_order) == 13
    ]
    if len(long_name_records) != 1:
        raise RawDictionaryError("Rewritten SAV has an inconsistent long-variable-name record.")
    long_name_record = long_name_records[0]
    if _int(data, long_name_record.start + 8, byte_order) != 1:
        raise RawDictionaryError("Rewritten SAV has invalid long-variable-name dimensions.")
    pairs = _long_name_pairs(
        data[long_name_record.start + 16 : long_name_record.end], encoding,
    )
    pair_by_long = {long_name: short_name for short_name, long_name in pairs}
    normalized_pair_names = {short_name.casefold() for short_name, _ in pairs}
    for short_name, _ in pairs:
        if normalized_type_2.count(short_name.casefold()) != 1:
            raise RawDictionaryError("Rewritten type-2 and subtype-13 names are inconsistent.")

    vls_names: list[str] = []
    for record in records:
        if record.record_type != 7 or _int(data, record.start + 4, byte_order) != 14:
            continue
        if _int(data, record.start + 8, byte_order) != 1:
            raise RawDictionaryError("Rewritten SAV has invalid very-long-string dimensions.")
        entries, _ = _very_long_string_entries(data[record.start + 16 : record.end])
        vls_names.extend(short_name for short_name, _ in entries)
    normalized_vls = [name.casefold() for name in vls_names]
    if len(normalized_vls) != len(set(normalized_vls)):
        raise RawDictionaryError("Rewritten SAV has duplicate very-long-string entries.")
    for short_name in normalized_vls:
        if short_name not in normalized_pair_names or normalized_type_2.count(short_name) != 1:
            raise RawDictionaryError(
                "Rewritten type-2/subtype-13/subtype-14 names are inconsistent."
            )

    for source_name, requested_name in requested.items():
        replacement = _validated_compatible_name(requested_name)
        if pair_by_long.get(source_name) != replacement:
            raise RawDictionaryError("Requested compatible name is absent after dictionary rewrite.")
        if normalized_type_2.count(replacement.casefold()) != 1:
            raise RawDictionaryError("Requested compatible name is inconsistent with type-2 records.")
        if (
            source_name in vls_source_names
            and normalized_vls.count(replacement.casefold()) != 1
        ):
            raise RawDictionaryError("Requested compatible name is inconsistent with subtype-14.")


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Publish validated dictionary bytes without exposing a partial replacement."""
    mode = stat.S_IMODE(target.stat().st_mode)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validated_compatible_name(value: str) -> str:
    encoded = str(value).encode("ascii")
    if not 1 <= len(encoded) <= 8:
        raise RawDictionaryError("SPSS compatible variable names must contain one to eight ASCII bytes.")
    first = encoded[:1]
    rest = encoded[1:]
    if not (first.isalpha() or first == b"@"):
        raise RawDictionaryError("SPSS compatible variable names must start with an ASCII letter or @.")
    if any(character not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.$#"
           for character in rest):
        raise RawDictionaryError("SPSS compatible variable name contains an unsupported character.")
    return encoded.decode("ascii")


def _shift_zsav_offsets(
    data: bytes, *, original_data_start: int, delta: int, byte_order: str,
) -> bytes:
    """Shift all absolute ZSAV data offsets after a dictionary-size rewrite."""
    if not delta or data[:4] != b"$FL3":
        return data
    adjusted = bytearray(data)
    data_start = original_data_start + delta
    _need(adjusted, data_start, 24)
    original_zheader = _int64(adjusted, data_start, byte_order)
    original_ztrailer = _int64(adjusted, data_start + 8, byte_order)
    trailer_length = _int64(adjusted, data_start + 16, byte_order)
    if original_zheader != original_data_start:
        raise RawDictionaryError("Unexpected ZSAV data-header offset.")
    _set_int64(adjusted, data_start, original_zheader + delta, byte_order)
    _set_int64(adjusted, data_start + 8, original_ztrailer + delta, byte_order)
    trailer_start = original_ztrailer + delta
    if trailer_length < 24:
        raise RawDictionaryError("Invalid ZSAV data-trailer length.")
    _need(adjusted, trailer_start, 24)
    block_count = _int(adjusted, trailer_start + 20, byte_order)
    if block_count < 0 or trailer_length != 24 + (block_count * 24):
        raise RawDictionaryError("Invalid ZSAV data-trailer block metadata.")
    _need(adjusted, trailer_start + 24, block_count * 24)
    for index in range(block_count):
        descriptor = trailer_start + 24 + (index * 24)
        _set_int64(adjusted, descriptor, _int64(adjusted, descriptor, byte_order) + delta, byte_order)
        _set_int64(
            adjusted, descriptor + 8,
            _int64(adjusted, descriptor + 8, byte_order) + delta,
            byte_order,
        )
    return bytes(adjusted)


def _document_record(lines: Iterable[str], *, encoding: str, byte_order: str) -> bytes:
    encoded_lines: list[bytes] = []
    for line in lines:
        encoded = str(line).encode(encoding)
        if len(encoded) > 80:
            raise RawDictionaryError("Each SPSS document line must be at most 80 encoded bytes.")
        encoded_lines.append(encoded.ljust(80, b" "))
    if not encoded_lines:
        return b""
    return _pack(6, byte_order) + _pack(len(encoded_lines), byte_order) + b"".join(encoded_lines)


def _records(data: bytes) -> tuple[str, list[_Record]]:
    if len(data) < _HEADER_SIZE or data[:4] not in {b"$FL2", b"$FL3"}:
        raise RawDictionaryError("Not a supported SAV or ZSAV system file.")
    byte_order = _byte_order(data)
    offset = _HEADER_SIZE
    records: list[_Record] = []
    while True:
        _need(data, offset, 4)
        record_type = _int(data, offset, byte_order)
        if record_type == 2:
            end = _variable_end(data, offset, byte_order)
        elif record_type == 3:
            end = _value_label_end(data, offset, byte_order)
        elif record_type == 4:
            _need(data, offset, 8)
            end = offset + 8 + (_int(data, offset + 4, byte_order) * 4)
        elif record_type == 6:
            _need(data, offset, 8)
            count = _int(data, offset + 4, byte_order)
            if count < 0:
                raise RawDictionaryError("Negative SAV document line count.")
            end = offset + 8 + (count * 80)
        elif record_type == 7:
            _need(data, offset, 16)
            size = _int(data, offset + 8, byte_order)
            count = _int(data, offset + 12, byte_order)
            if size < 0 or count < 0:
                raise RawDictionaryError("Negative SAV extension-record dimensions.")
            end = offset + 16 + (size * count)
        elif record_type == 999:
            _need(data, offset, 8)
            records.append(_Record(record_type, offset, offset + 8))
            return byte_order, records
        else:
            raise RawDictionaryError(
                f"Unsupported SAV dictionary record type {record_type}; refusing raw rewrite."
            )
        _need(data, offset, end - offset)
        records.append(_Record(record_type, offset, end))
        offset = end


def _variable_end(data: bytes, offset: int, byte_order: str) -> int:
    _need(data, offset, 32)
    has_label = _int(data, offset + 8, byte_order)
    missing_count = _int(data, offset + 12, byte_order)
    if has_label not in {0, 1} or missing_count < -3 or missing_count > 3:
        raise RawDictionaryError("Invalid SAV variable dictionary record.")
    end = offset + 32
    if has_label:
        _need(data, end, 4)
        label_size = _int(data, end, byte_order)
        if label_size < 0:
            raise RawDictionaryError("Negative SAV variable-label size.")
        end += 4 + _round4(label_size)
    return end + (abs(missing_count) * 8)


def _value_label_end(data: bytes, offset: int, byte_order: str) -> int:
    _need(data, offset, 8)
    count = _int(data, offset + 4, byte_order)
    if count < 0:
        raise RawDictionaryError("Negative SAV value-label count.")
    end = offset + 8
    for _ in range(count):
        _need(data, end, 9)
        end += 8 + _round8(1 + data[end + 8])
    return end


def _byte_order(data: bytes) -> str:
    layout = data[64:68]
    if int.from_bytes(layout, "little", signed=True) in {2, 3}:
        return "little"
    if int.from_bytes(layout, "big", signed=True) in {2, 3}:
        return "big"
    raise RawDictionaryError("Cannot determine SAV integer byte order.")


def _pack(value: int, byte_order: str) -> bytes:
    return int(value).to_bytes(4, byte_order, signed=True)


def _int(data: bytes, offset: int, byte_order: str) -> int:
    return int.from_bytes(data[offset : offset + 4], byte_order, signed=True)


def _int64(data: bytes, offset: int, byte_order: str) -> int:
    return int.from_bytes(data[offset : offset + 8], byte_order, signed=True)


def _set_int64(data: bytearray, offset: int, value: int, byte_order: str) -> None:
    data[offset : offset + 8] = int(value).to_bytes(8, byte_order, signed=True)


def _round4(value: int) -> int:
    return (value + 3) & ~3


def _round8(value: int) -> int:
    return (value + 7) & ~7


def _need(data: bytes, offset: int, size: int) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise RawDictionaryError("Truncated SAV dictionary record.")
