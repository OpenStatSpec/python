"""Small, fail-closed helpers for selected raw SPSS dictionary records.

This is not a second SAV engine: pyspssio still owns values and the normal
dictionary. IBM I/O exposes copying document records but not their text.
"""
from __future__ import annotations

import os
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkstemp
from typing import Callable, Iterable

import pyspssio


class RawDictionaryError(ValueError):
    """Raw dictionary data cannot be handled without risking silent loss."""


_HEADER_SIZE = 176


@dataclass(frozen=True)
class _Record:
    record_type: int
    start: int
    end: int


@dataclass(frozen=True)
class _DictionarySemantics:
    compatible_names: dict[str, str]
    variable_sets: dict[str, list[str]]
    multiple_response_sets: dict[str, dict]


@dataclass(frozen=True)
class _ReferenceLine:
    set_name: bytes
    segments: tuple[bytes, ...]
    members: tuple[bytes, ...]

    def serialize(self) -> bytes:
        output = bytearray(self.segments[0])
        for member, segment in zip(self.members, self.segments[1:]):
            output.extend(member)
            output.extend(segment)
        return bytes(output)


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
    """Set exact legacy short names and rewrite every standard short-name reference.

    IBM I/O has no compatible-name setter.  This narrowly rewrites the fixed
    type-2 names and standard subtype-13/14, variable-set, and MR-set records.
    The complete candidate is raw-validated and IBM Reader-validated before a
    same-directory atomic publish.
    """
    if not names:
        return
    target = Path(path)
    data = target.read_bytes()
    byte_order, records = _records(data)
    terminator = next(record for record in records if record.record_type == 999)
    primary_variables, long_names, pairs, very_long_records = _compatible_name_dictionary(
        data, byte_order=byte_order, records=records, encoding=encoding,
    )
    _require_unique_names(list(names), "requested source variable")
    validated_names = {
        source_name: _validated_compatible_name(value)
        for source_name, value in names.items()
    }
    replacements: dict[str, str] = {}
    updated_pairs: list[tuple[str, str]] = []
    unresolved = set(names)
    for short_name, long_name in pairs:
        replacement = validated_names.get(long_name)
        if replacement is None:
            updated_pairs.append((short_name, long_name))
            continue
        replacements[short_name.casefold()] = replacement
        updated_pairs.append((replacement, long_name))
        unresolved.remove(long_name)
    if unresolved:
        missing = ", ".join(sorted(unresolved))
        raise RawDictionaryError(
            "Compatible-name update requires a long-name record for: " + missing
        )

    expected_primary_names = [
        replacements.get(short_name.casefold(), short_name)
        for _record, short_name in primary_variables
    ]
    _require_unique_names(expected_primary_names, "compatible variable")
    mutable = bytearray(data)
    for record, short_name in primary_variables:
        replacement = replacements.get(short_name.casefold())
        if replacement is not None:
            mutable[record.start + 24 : record.start + 32] = (
                replacement.encode("ascii").ljust(8, b" ")
            )

    long_name_payload = b"	".join(
        short_name.encode("ascii") + b"=" + long_name.encode(encoding)
        for short_name, long_name in updated_pairs
    )
    header = bytearray(mutable[long_names.start : long_names.start + 16])
    header[12:16] = _pack(len(long_name_payload), byte_order)
    record_replacements = {
        long_names.start: bytes(header) + long_name_payload,
    }
    expected_very_long: list[tuple[tuple[str, bytes], ...]] = []
    for record, very_long_pairs in very_long_records:
        updated_very_long = tuple(
            (replacements.get(short_name.casefold(), short_name), width)
            for short_name, width in very_long_pairs
        )
        expected_very_long.append(updated_very_long)
        payload = _very_long_string_payload(updated_very_long)
        very_long_header = bytearray(mutable[record.start : record.start + 16])
        very_long_header[8:12] = _pack(1, byte_order)
        very_long_header[12:16] = _pack(len(payload), byte_order)
        record_replacements[record.start] = bytes(very_long_header) + payload
    reference_replacements, expected_reference_payloads = _rewrite_reference_records(
        data,
        byte_order=byte_order,
        records=records,
        encoding=encoding,
        compatible_replacements=replacements,
        known_compatible_names=[name for _record, name in primary_variables],
    )
    record_replacements.update(reference_replacements)

    semantics_before = _read_dictionary_semantics(target)
    expected_compatible_names = dict(semantics_before.compatible_names)
    for source_name, compatible_name in validated_names.items():
        if source_name not in expected_compatible_names:
            raise RawDictionaryError(
                "IBM Reader did not expose the requested source variable: "
                + source_name
            )
        expected_compatible_names[source_name] = compatible_name
    _require_unique_names(
        expected_compatible_names.values(), "Reader-compatible variable",
    )

    chunks: list[bytes] = []
    cursor = 0
    for record in records:
        replacement = record_replacements.get(record.start)
        if replacement is None:
            continue
        chunks.extend((bytes(mutable[cursor : record.start]), replacement))
        cursor = record.end
    chunks.append(bytes(mutable[cursor:]))
    updated = b"".join(chunks)

    _assert_compatible_name_rewrite(
        updated, byte_order=byte_order, encoding=encoding,
        expected_primary_names=expected_primary_names,
        expected_pairs=updated_pairs, expected_very_long=expected_very_long,
        expected_reference_payloads=expected_reference_payloads,
    )
    shifted = _shift_zsav_offsets(
        updated,
        original_data_start=terminator.end,
        delta=len(updated) - len(data),
        byte_order=byte_order,
    )
    _assert_compatible_name_rewrite(
        shifted, byte_order=byte_order, encoding=encoding,
        expected_primary_names=expected_primary_names,
        expected_pairs=updated_pairs, expected_very_long=expected_very_long,
        expected_reference_payloads=expected_reference_payloads,
    )
    _atomic_write_bytes(
        target,
        shifted,
        validator=lambda candidate: _assert_dictionary_semantics(
            candidate,
            expected_compatible_names=expected_compatible_names,
            expected_variable_sets=semantics_before.variable_sets,
            expected_multiple_response_sets=semantics_before.multiple_response_sets,
        ),
    )


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


def _compatible_name_dictionary(
    data: bytes, *, byte_order: str, records: list[_Record], encoding: str,
):
    primary_variables: list[tuple[_Record, str]] = []
    for record in records:
        if record.record_type != 2 or _int(data, record.start + 4, byte_order) < 0:
            continue
        raw_name = data[record.start + 24 : record.start + 32]
        try:
            short_name = raw_name.decode("ascii").rstrip(" ")
        except UnicodeDecodeError as error:
            raise RawDictionaryError("Invalid type-2 compatible variable name.") from error
        _validated_compatible_name(short_name)
        primary_variables.append((record, short_name))
    _require_unique_names(
        [short_name for _record, short_name in primary_variables],
        "type-2 compatible variable",
    )

    long_name_records = [
        record for record in records
        if record.record_type == 7
        and _int(data, record.start + 4, byte_order) == 13
    ]
    if len(long_name_records) != 1:
        raise RawDictionaryError("Expected exactly one long-variable-name record.")
    long_names = long_name_records[0]
    if _int(data, long_names.start + 8, byte_order) != 1:
        raise RawDictionaryError("Invalid SAV long-variable-name record element size.")
    pairs = _long_name_pairs(data[long_names.start + 16 : long_names.end], encoding)

    primary_keys = {
        short_name.casefold() for _record, short_name in primary_variables
    }
    long_name_keys = {short_name.casefold() for short_name, _long_name in pairs}
    missing_type_2 = sorted(
        short_name for short_name, _long_name in pairs
        if short_name.casefold() not in primary_keys
    )
    if missing_type_2:
        raise RawDictionaryError(
            "Subtype-13 names have no matching type-2 record: "
            + ", ".join(missing_type_2)
        )

    very_long_records: list[tuple[_Record, tuple[tuple[str, bytes], ...]]] = []
    all_very_long_names: list[str] = []
    for record in records:
        if record.record_type != 7 or _int(data, record.start + 4, byte_order) != 14:
            continue
        if _int(data, record.start + 8, byte_order) != 1:
            raise RawDictionaryError("Invalid SAV very-long-string record element size.")
        very_long_pairs = tuple(
            _very_long_string_pairs(data[record.start + 16 : record.end])
        )
        very_long_records.append((record, very_long_pairs))
        all_very_long_names.extend(short_name for short_name, _width in very_long_pairs)
    _require_unique_names(all_very_long_names, "subtype-14 compatible variable")
    missing_long_names = sorted(
        short_name for short_name in all_very_long_names
        if short_name.casefold() not in long_name_keys
    )
    if missing_long_names:
        raise RawDictionaryError(
            "Subtype-14 names have no matching subtype-13 entry: "
            + ", ".join(missing_long_names)
        )
    return primary_variables, long_names, pairs, very_long_records


def _read_dictionary_semantics(path: Path) -> _DictionarySemantics:
    try:
        with pyspssio.Reader(str(path), mode="r") as reader:
            compatible_names = deepcopy(reader.var_compat_names)
            variable_sets = deepcopy(reader.var_sets)
            multiple_response_sets = deepcopy(reader.mrsets)
    except Exception as error:
        raise RawDictionaryError(
            "IBM Reader could not inspect compatible-name references."
        ) from error
    if not isinstance(compatible_names, dict):
        raise RawDictionaryError("Invalid IBM Reader compatible-name dictionary.")
    if not isinstance(variable_sets, dict):
        raise RawDictionaryError("Invalid IBM Reader variable-set dictionary.")
    if not isinstance(multiple_response_sets, dict):
        raise RawDictionaryError("Invalid IBM Reader multiple-response dictionary.")
    for members in variable_sets.values():
        if not isinstance(members, list):
            raise RawDictionaryError("Invalid IBM Reader variable-set member list.")
    for definition in multiple_response_sets.values():
        if not isinstance(definition, dict) or not isinstance(
            definition.get("variable_list"), list,
        ):
            raise RawDictionaryError("Invalid IBM Reader multiple-response definition.")
    return _DictionarySemantics(
        compatible_names=compatible_names,
        variable_sets=variable_sets,
        multiple_response_sets=multiple_response_sets,
    )


def _assert_dictionary_semantics(
    path: Path, *, expected_compatible_names: dict[str, str],
    expected_variable_sets: dict[str, list[str]],
    expected_multiple_response_sets: dict[str, dict],
) -> None:
    observed = _read_dictionary_semantics(path)
    if observed.compatible_names != expected_compatible_names:
        raise RawDictionaryError("Compatible-name Reader readback differs from candidate.")
    if observed.variable_sets != expected_variable_sets:
        raise RawDictionaryError("Variable-set Reader readback changed semantics.")
    if observed.multiple_response_sets != expected_multiple_response_sets:
        raise RawDictionaryError("Multiple-response Reader readback changed semantics.")


def _rewrite_reference_records(
    data: bytes, *, byte_order: str, records: list[_Record], encoding: str,
    compatible_replacements: dict[str, str], known_compatible_names: Iterable[str],
) -> tuple[dict[int, bytes], dict[int, tuple[bytes, ...]]]:
    known_names = list(known_compatible_names)
    _require_unique_names(known_names, "reference-compatible variable")
    known = {name.casefold(): name for name in known_names}
    encoded_replacements = {
        key: value.encode("ascii") for key, value in compatible_replacements.items()
    }
    replacements: dict[int, bytes] = {}
    expected: dict[int, list[bytes]] = {5: [], 7: [], 19: []}
    seen_variable_sets: dict[str, str] = {}
    seen_mrsets: dict[str, str] = {}
    for record in records:
        if record.record_type != 7:
            continue
        subtype = _int(data, record.start + 4, byte_order)
        if subtype not in expected:
            continue
        if _int(data, record.start + 8, byte_order) != 1:
            raise RawDictionaryError(
                f"Subtype-{subtype} reference record must have element size 1."
            )
        payload = data[record.start + 16 : record.end]
        lines, suffix = _reference_payload_lines(payload, subtype=subtype)
        updated_lines: list[_ReferenceLine] = []
        seen = seen_variable_sets if subtype == 5 else seen_mrsets
        for raw_line in lines:
            parsed = _parse_reference_line(raw_line, subtype=subtype, encoding=encoding)
            try:
                set_name = parsed.set_name.decode(encoding)
            except UnicodeDecodeError as error:
                raise RawDictionaryError(
                    f"Subtype-{subtype} set name does not match file encoding."
                ) from error
            set_key = set_name.casefold()
            if set_key in seen:
                raise RawDictionaryError(
                    f"Duplicate subtype-{subtype} set name: {seen[set_key]!r} and {set_name!r}."
                )
            seen[set_key] = set_name
            members: list[bytes] = []
            for raw_member in parsed.members:
                try:
                    member = raw_member.decode("ascii")
                    _validated_compatible_name(member)
                except (UnicodeDecodeError, RawDictionaryError) as error:
                    raise RawDictionaryError(
                        f"Invalid subtype-{subtype} compatible member token."
                    ) from error
                member_key = member.casefold()
                if member_key not in known:
                    raise RawDictionaryError(
                        f"Subtype-{subtype} references unknown compatible variable {member!r}."
                    )
                members.append(encoded_replacements.get(member_key, raw_member))
            updated_lines.append(_ReferenceLine(
                set_name=parsed.set_name,
                segments=parsed.segments,
                members=tuple(members),
            ))
        new_payload = b"\n".join(line.serialize() for line in updated_lines) + suffix
        header = bytearray(data[record.start : record.start + 16])
        header[8:12] = _pack(1, byte_order)
        header[12:16] = _pack(len(new_payload), byte_order)
        replacements[record.start] = bytes(header) + new_payload
        expected[subtype].append(new_payload)
    return replacements, {
        subtype: tuple(payloads) for subtype, payloads in expected.items()
    }


def _reference_payload_lines(payload: bytes, *, subtype: int) -> tuple[list[bytes], bytes]:
    if not payload:
        raise RawDictionaryError(f"Empty subtype-{subtype} reference record.")
    body_end = len(payload)
    while body_end and payload[body_end - 1] == 0:
        body_end -= 1
    suffix = payload[body_end:]
    body = payload[:body_end]
    if body.endswith(b"\n"):
        body = body[:-1]
        suffix = b"\n" + suffix
    if not body or body.endswith(b"\n") or b"\x00" in body or b"\r" in body:
        raise RawDictionaryError(f"Invalid subtype-{subtype} line framing.")
    lines = body.split(b"\n")
    if any(not line for line in lines):
        raise RawDictionaryError(f"Invalid empty subtype-{subtype} definition.")
    return lines, suffix


def _parse_reference_line(line: bytes, *, subtype: int, encoding: str) -> _ReferenceLine:
    separator = line.find(b"=")
    if separator <= 0:
        raise RawDictionaryError(f"Invalid subtype-{subtype} set definition.")
    set_name = line[:separator]
    try:
        set_name.decode(encoding)
    except UnicodeDecodeError as error:
        raise RawDictionaryError(f"Invalid subtype-{subtype} set name.") from error
    definition = line[separator + 1:]
    member_start = 0 if subtype == 5 else _mrset_member_start(
        definition, subtype=subtype, encoding=encoding,
    )
    absolute_start = separator + 1 + member_start
    member_region = line[absolute_start:]
    matches = list(re.finditer(rb"[^ \t]+", member_region))
    if not matches:
        raise RawDictionaryError(f"Subtype-{subtype} definition has no members.")
    segments: list[bytes] = []
    members: list[bytes] = []
    cursor = 0
    for match in matches:
        segments.append(line[cursor : absolute_start + match.start()])
        members.append(match.group())
        cursor = absolute_start + match.end()
    segments.append(line[cursor:])
    return _ReferenceLine(set_name, tuple(segments), tuple(members))


def _mrset_member_start(definition: bytes, *, subtype: int, encoding: str) -> int:
    if definition.startswith(b"C "):
        if subtype not in {7, 19}:
            raise RawDictionaryError("C multiple-response definition has invalid subtype.")
        label_length, label_start = _ascii_length_field(definition, 2)
        return _length_delimited_end(
            definition, label_start, label_length, encoding=encoding,
            description="multiple-response label",
        )
    if definition.startswith(b"D"):
        if subtype not in {7, 19}:
            raise RawDictionaryError("D multiple-response definition has invalid subtype.")
        first_space = definition.find(b" ")
        if first_space < 2 or not definition[1:first_space].isdigit():
            raise RawDictionaryError("Invalid D multiple-response counted-value length.")
        value_length = int(definition[1:first_space])
        label_field = _length_delimited_end(
            definition, first_space + 1, value_length, encoding=encoding,
            description="multiple-response counted value", return_after_separator=True,
        )
        label_length, label_start = _ascii_length_field(definition, label_field)
        return _length_delimited_end(
            definition, label_start, label_length, encoding=encoding,
            description="multiple-response label",
        )
    if subtype == 19 and definition.startswith(b"E "):
        flags, position = _ascii_token(definition, 2, "extended flags")
        if not flags.isdigit():
            raise RawDictionaryError("Invalid extended multiple-response flags.")
        value_length, value_start = _ascii_length_field(definition, position)
        label_field = _length_delimited_end(
            definition, value_start, value_length, encoding=encoding,
            description="extended counted value", return_after_separator=True,
        )
        label_length, label_start = _ascii_length_field(definition, label_field)
        return _length_delimited_end(
            definition, label_start, label_length, encoding=encoding,
            description="extended multiple-response label",
        )
    raise RawDictionaryError(f"Unsupported subtype-{subtype} multiple-response definition.")


def _ascii_token(data: bytes, start: int, description: str) -> tuple[bytes, int]:
    end = data.find(b" ", start)
    if end <= start:
        raise RawDictionaryError(f"Invalid {description} field.")
    return data[start:end], end + 1


def _ascii_length_field(data: bytes, start: int) -> tuple[int, int]:
    token, following = _ascii_token(data, start, "byte-length")
    if not token.isdigit():
        raise RawDictionaryError("Invalid multiple-response byte-length field.")
    return int(token), following


def _length_delimited_end(
    data: bytes, start: int, length: int, *, encoding: str, description: str,
    return_after_separator: bool = False,
) -> int:
    end = start + length
    if length < 0 or end >= len(data) or data[end:end + 1] != b" ":
        raise RawDictionaryError(f"Invalid {description} byte length.")
    try:
        data[start:end].decode(encoding)
    except UnicodeDecodeError as error:
        raise RawDictionaryError(f"Invalid {description} encoding.") from error
    following = end + 1
    if return_after_separator:
        return following
    if following >= len(data):
        raise RawDictionaryError(f"{description.capitalize()} has no member list.")
    return following


def _long_name_pairs(payload: bytes, encoding: str) -> list[tuple[str, str]]:
    if not payload:
        raise RawDictionaryError("Empty SAV long-variable-name record.")
    pairs: list[tuple[str, str]] = []
    for raw_pair in payload.split(b"	"):
        try:
            raw_short, raw_long = raw_pair.split(b"=", maxsplit=1)
            short_name = raw_short.decode("ascii")
            long_name = raw_long.decode(encoding)
        except (UnicodeDecodeError, ValueError) as error:
            raise RawDictionaryError("Invalid SAV long-variable-name record.") from error
        _validated_compatible_name(short_name)
        if not long_name or any(character in long_name for character in "\x00	="):
            raise RawDictionaryError("Invalid SAV long-variable-name record.")
        pairs.append((short_name, long_name))
    _require_unique_names(
        [short_name for short_name, _long_name in pairs],
        "subtype-13 compatible variable",
    )
    _require_unique_names(
        [long_name for _short_name, long_name in pairs],
        "subtype-13 long variable",
    )
    return pairs


def _very_long_string_pairs(payload: bytes) -> list[tuple[str, bytes]]:
    if not payload or not payload.endswith(b"\x00	"):
        raise RawDictionaryError("Invalid SAV very-long-string record terminator.")
    raw_pairs = payload.split(b"	")
    if raw_pairs[-1] != b"":
        raise RawDictionaryError("Invalid SAV very-long-string record separator.")
    pairs: list[tuple[str, bytes]] = []
    for raw_pair in raw_pairs[:-1]:
        if not raw_pair.endswith(b"\x00"):
            raise RawDictionaryError("Invalid SAV very-long-string entry terminator.")
        try:
            raw_short, raw_width = raw_pair[:-1].split(b"=", maxsplit=1)
            short_name = raw_short.decode("ascii")
        except (UnicodeDecodeError, ValueError) as error:
            raise RawDictionaryError("Invalid SAV very-long-string record.") from error
        _validated_compatible_name(short_name)
        if not raw_width or not raw_width.isdigit() or int(raw_width) <= 255:
            raise RawDictionaryError("Invalid SAV very-long-string width.")
        pairs.append((short_name, raw_width))
    _require_unique_names(
        [short_name for short_name, _width in pairs],
        "subtype-14 compatible variable",
    )
    return pairs


def _very_long_string_payload(pairs: Iterable[tuple[str, bytes]]) -> bytes:
    return b"".join(
        short_name.encode("ascii") + b"=" + width + b"\x00	"
        for short_name, width in pairs
    )


def _require_unique_names(names: Iterable[str], description: str) -> None:
    seen: dict[str, str] = {}
    for name in names:
        key = name.casefold()
        if key in seen:
            raise RawDictionaryError(
                f"Duplicate {description} name: {seen[key]!r} and {name!r}."
            )
        seen[key] = name


def _assert_compatible_name_rewrite(
    data: bytes, *, byte_order: str, encoding: str,
    expected_primary_names: list[str], expected_pairs: list[tuple[str, str]],
    expected_very_long: list[tuple[tuple[str, bytes], ...]],
    expected_reference_payloads: dict[int, tuple[bytes, ...]],
) -> None:
    observed_byte_order, observed_records = _records(data)
    if observed_byte_order != byte_order:
        raise RawDictionaryError("Compatible-name rewrite changed SAV byte order.")
    primary, _long_record, pairs, very_long = _compatible_name_dictionary(
        data, byte_order=byte_order, records=observed_records, encoding=encoding,
    )
    if [short_name for _record, short_name in primary] != expected_primary_names:
        raise RawDictionaryError(
            "Compatible-name rewrite did not update type-2 records consistently."
        )
    if pairs != expected_pairs:
        raise RawDictionaryError(
            "Compatible-name rewrite did not update subtype-13 consistently."
        )
    if [items for _record, items in very_long] != expected_very_long:
        raise RawDictionaryError(
            "Compatible-name rewrite did not update subtype-14 consistently."
        )
    _unused, observed_reference_payloads = _rewrite_reference_records(
        data,
        byte_order=byte_order,
        records=observed_records,
        encoding=encoding,
        compatible_replacements={},
        known_compatible_names=expected_primary_names,
    )
    if observed_reference_payloads != expected_reference_payloads:
        raise RawDictionaryError(
            "Compatible-name rewrite changed a set reference outside member tokens."
        )


def _atomic_write_bytes(
    target: Path, data: bytes, *, validator: Callable[[Path], None] | None = None,
) -> None:
    descriptor, temporary_name = mkstemp(
        dir=target.parent,
        prefix=f".{target.stem}.",
        suffix=f".tmp{target.suffix}",
    )
    temporary = Path(temporary_name)
    try:
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            _write_all(stream, data)
            stream.flush()
            os.fsync(stream.fileno())
        if validator is not None:
            validator(temporary)
        os.chmod(temporary, target.stat().st_mode & 0o7777)
        os.replace(temporary, target)
        _best_effort_fsync_directory(target.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _best_effort_fsync_directory(directory: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_all(stream, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = stream.write(remaining)
        if written is None or written <= 0:
            raise OSError("Temporary SAV publish made no write progress.")
        remaining = remaining[written:]


def _validated_compatible_name(value: str) -> str:
    try:
        encoded = str(value).encode("ascii")
    except UnicodeEncodeError as error:
        raise RawDictionaryError(
            "SPSS compatible variable names must contain one to eight ASCII bytes."
        ) from error
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
