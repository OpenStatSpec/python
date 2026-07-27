"""Small, fail-closed helpers for the SPSS type-6 document record.

This is not a second SAV engine: pyspssio still owns values and the normal
dictionary. IBM I/O exposes copying document records but not their text.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
    if documents:
        current = documents[0]
        target.write_bytes(data[: current.start] + replacement + data[current.end :])
        return
    extension = next((record for record in records if record.record_type == 7), None)
    terminator = next(record for record in records if record.record_type == 999)
    insertion = extension.start if extension else terminator.start
    target.write_bytes(data[:insertion] + replacement + data[insertion:])


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


def _round4(value: int) -> int:
    return (value + 3) & ~3


def _round8(value: int) -> int:
    return (value + 7) & ~7


def _need(data: bytes, offset: int, size: int) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise RawDictionaryError("Truncated SAV dictionary record.")
