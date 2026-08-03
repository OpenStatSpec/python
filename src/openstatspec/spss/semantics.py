"""Public, value-redacting semantic comparison for SPSS SAV/ZSAV files.

The comparison deliberately reports no observed values or dictionary names.
By default it returns only cardinalities, fixed component names, and statuses.
A caller may explicitly request deterministic SHA-256 receipts; those hashes
are dictionary-guessable for low-entropy metadata and are therefore opt-in.
"""

from __future__ import annotations

import hashlib
import math
import numbers
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pyspssio

from .sav import _dictionary


@dataclass(frozen=True)
class _Component:
    digest: str
    count: int
    available: bool = True


@dataclass(frozen=True)
class _Snapshot:
    case_count: int
    variable_count: int
    numeric_variable_count: int
    string_variable_count: int
    components: Mapping[str, _Component]


_COMPONENT_ORDER = (
    "adapter_observability",
    "variable_order",
    "case_order",
    "numeric_nonmissing_binary64",
    "numeric_system_missing_mask",
    "string_values",
    "source_encoding",
    "file_label",
    "case_weight_variable",
    "documents",
    "file_attributes",
    "variable_types",
    "variable_labels",
    "print_formats",
    "write_formats",
    "measurement_levels",
    "variable_roles",
    "variable_alignments",
    "display_widths",
    "compatible_names",
    "ordered_value_labels",
    "missing_rules",
    "variable_attributes",
    "variable_sets",
    "multiple_response_sets",
)


def compare_sav_semantics(
    source: str | Path, exported: str | Path, *,
    include_digests: bool = False,
) -> dict[str, Any]:
    """Compare all SPSS semantics supported by the adapter.

    Numeric nonmissing values are compared by their float64 bit patterns, so
    signed zero is significant.  Every NaN payload exposed by ``pyspssio`` is
    instead the same semantic SPSS system-missing marker.  Ordered dictionary
    features retain their order, including documents, value labels, variable
    sets, multiple-response sets, their members, and attribute-array members.

    The result never contains case values, labels, names, paths, encodings, or
    other source content.  ``unavailable`` means the adapter could not observe
    a required dictionary component, and therefore equivalence is false.
    Deterministic digests are returned only with ``include_digests=True``.
    """
    source_snapshot = _snapshot(Path(source))
    exported_snapshot = _snapshot(Path(exported))
    differences: list[str] = []
    components: dict[str, dict[str, Any]] = {}
    for name in _COMPONENT_ORDER:
        left = source_snapshot.components[name]
        right = exported_snapshot.components[name]
        if not left.available or not right.available:
            status = "unavailable"
        elif left.count == right.count and left.digest == right.digest:
            status = "equal"
        else:
            status = "different"
        if status != "equal":
            differences.append(name)
        component_result = {
            "status": status,
            "source_count": left.count,
            "exported_count": right.count,
        }
        if include_digests:
            component_result["source_sha256"] = left.digest
            component_result["exported_sha256"] = right.digest
        components[name] = component_result
    return {
        "equivalent": not differences,
        "differences": differences,
        "counts": {
            "source": {
                "cases": source_snapshot.case_count,
                "variables": source_snapshot.variable_count,
                "numeric_variables": source_snapshot.numeric_variable_count,
                "string_variables": source_snapshot.string_variable_count,
            },
            "exported": {
                "cases": exported_snapshot.case_count,
                "variables": exported_snapshot.variable_count,
                "numeric_variables": exported_snapshot.numeric_variable_count,
                "string_variables": exported_snapshot.string_variable_count,
            },
        },
        "components": components,
    }


def _snapshot(path: Path) -> _Snapshot:
    frame, observed = pyspssio.read_sav(
        str(path), convert_datetimes=False, include_user_missing=True,
    )
    dictionary, loss_report = _dictionary(path)
    metadata = {**dict(observed), **dictionary}
    variable_names = list(frame.columns)
    types = dict(metadata.get("var_types") or {})
    numeric_indexes = tuple(
        index for index, name in enumerate(variable_names)
        if int(types.get(name) or 0) == 0
    )
    string_indexes = tuple(
        index for index, name in enumerate(variable_names)
        if int(types.get(name) or 0) != 0
    )
    numeric_comparison_indexes = tuple(
        sorted(numeric_indexes, key=lambda index: variable_names[index])
    )
    string_comparison_indexes = tuple(
        sorted(string_indexes, key=lambda index: variable_names[index])
    )
    case_comparison_indexes = tuple(
        sorted(range(len(variable_names)), key=lambda index: variable_names[index])
    )
    numeric_index_set = frozenset(numeric_indexes)
    reader_bridge_available = metadata.get("_var_sets") is not None
    documents_available = metadata.get("_documents") is not None

    encoding = metadata.get("encoding")
    components = {
        "variable_order": _component(variable_names, count=len(variable_names)),
        "adapter_observability": _component(
            tuple(sorted(loss_report)), count=len(loss_report), available=not loss_report,
        ),
        # Without an external case identifier, order is observed as the ordered
        # sequence of complete case digests.  Per-type components below make
        # value-class differences independently visible.
        "case_order": _sequence_component(
            _case_digest(
                row, variable_names, case_comparison_indexes, numeric_index_set,
            )
            for row in frame.itertuples(index=False, name=None)
        ),
        "numeric_nonmissing_binary64": _sequence_component(
            (variable_names[index], _binary64(value))
            for index in numeric_comparison_indexes
            for row in frame.itertuples(index=False, name=None)
            if not _is_system_missing(value := row[index])
        ),
        "numeric_system_missing_mask": _sequence_component(
            (variable_names[index], _is_system_missing(row[index]))
            for index in numeric_comparison_indexes
            for row in frame.itertuples(index=False, name=None)
        ),
        "string_values": _sequence_component(
            (variable_names[index], row[index])
            for index in string_comparison_indexes
            for row in frame.itertuples(index=False, name=None)
        ),
        "source_encoding": _component(
            encoding, available=bool(str(encoding or "").strip()),
        ),
        "file_label": _component(metadata.get("file_label")),
        "case_weight_variable": _component(metadata.get("case_weight_var")),
        "documents": _component(
            list(metadata.get("_documents") or ()),
            count=len(metadata.get("_documents") or ()),
            available=documents_available,
        ),
        "file_attributes": _component(
            _attribute_mapping(metadata.get("file_attributes")),
            count=len(metadata.get("file_attributes") or {}),
            available=reader_bridge_available,
        ),
        "variable_types": _variable_property(metadata, "var_types", variable_names),
        "variable_labels": _variable_property(metadata, "var_labels", variable_names),
        "print_formats": _variable_property(
            metadata, "_print_format_tuples", variable_names,
            available=reader_bridge_available,
        ),
        "write_formats": _variable_property(
            metadata, "_write_format_tuples", variable_names,
            available=reader_bridge_available,
        ),
        "measurement_levels": _variable_property(
            metadata, "var_measure_levels", variable_names,
        ),
        "variable_roles": _variable_property(metadata, "var_roles", variable_names),
        "variable_alignments": _variable_property(
            metadata, "var_alignments", variable_names,
        ),
        "display_widths": _variable_property(
            metadata, "var_column_widths", variable_names,
        ),
        "compatible_names": _variable_property(
            metadata, "var_compat_names", variable_names,
        ),
        "ordered_value_labels": _component(
            _ordered_value_labels(metadata, variable_names),
            count=sum(
                len(labels)
                for labels in (metadata.get("var_value_labels") or {}).values()
            ),
        ),
        "missing_rules": _component(
            _missing_rules(metadata, variable_names),
            count=len(metadata.get("var_missing_values") or {}),
        ),
        "variable_attributes": _component(
            _variable_attributes(metadata, variable_names),
            count=sum(
                len(attributes)
                for attributes in (metadata.get("var_attributes") or {}).values()
            ),
            available=reader_bridge_available,
        ),
        "variable_sets": _component(
            _ordered_sets(metadata.get("_var_sets")),
            count=len(metadata.get("_var_sets") or {}),
            available=reader_bridge_available,
        ),
        "multiple_response_sets": _component(
            _ordered_mrsets(metadata.get("mrsets")),
            count=len(metadata.get("mrsets") or {}),
        ),
    }
    return _Snapshot(
        case_count=len(frame.index),
        variable_count=len(variable_names),
        numeric_variable_count=len(numeric_indexes),
        string_variable_count=len(string_indexes),
        components=components,
    )


def _variable_property(
    metadata: Mapping[str, Any], key: str, variable_names: Sequence[str],
    *, available: bool = True,
) -> _Component:
    values = dict(metadata.get(key) or {})
    names = sorted(set(variable_names) | set(values), key=str)
    ordered = [
        (name, name in values, values.get(name))
        for name in names
    ]
    return _component(ordered, count=len(values), available=available)


def _ordered_value_labels(
    metadata: Mapping[str, Any], variable_names: Sequence[str],
) -> list[tuple[Any, list[tuple[Any, Any]]]]:
    values = dict(metadata.get("var_value_labels") or {})
    names = sorted(set(variable_names) | set(values), key=str)
    return [
        (name, list((values.get(name) or {}).items()))
        for name in names
        if name in values
    ]


def _missing_rules(
    metadata: Mapping[str, Any], variable_names: Sequence[str],
) -> list[tuple[Any, tuple[Any, Any, tuple[Any, ...]]]]:
    rules = dict(metadata.get("var_missing_values") or {})
    names = sorted(set(variable_names) | set(rules), key=str)
    result: list[tuple[Any, tuple[Any, Any, tuple[Any, ...]]]] = []
    for name in names:
        if name not in rules:
            continue
        rule = rules[name] or {}
        result.append((
            name,
            (rule.get("lo"), rule.get("hi"), tuple(rule.get("values") or ())),
        ))
    return result


def _attribute_mapping(value: Any) -> list[tuple[Any, Any]]:
    return sorted(dict(value or {}).items(), key=lambda item: str(item[0]))


def _variable_attributes(
    metadata: Mapping[str, Any], variable_names: Sequence[str],
) -> list[tuple[Any, list[tuple[Any, Any]]]]:
    values = dict(metadata.get("var_attributes") or {})
    names = sorted(set(variable_names) | set(values), key=str)
    return [
        (name, _attribute_mapping(values[name]))
        for name in names
        if name in values
    ]


def _ordered_sets(value: Any) -> list[tuple[Any, tuple[Any, ...]]]:
    return [
        (name, tuple(members or ()))
        for name, members in dict(value or {}).items()
    ]


def _ordered_mrsets(value: Any) -> list[tuple[Any, Any]]:
    return [
        (name, _mapping_with_ordered_members(definition))
        for name, definition in dict(value or {}).items()
    ]


def _mapping_with_ordered_members(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    return [
        (key, tuple(item) if key == "variable_list" and isinstance(item, (list, tuple)) else item)
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
    ]


def _case_digest(
    row: Sequence[Any], variable_names: Sequence[str],
    comparison_indexes: Sequence[int], numeric_indexes: frozenset[int],
) -> bytes:
    values: list[Any] = []
    for index in comparison_indexes:
        value = row[index]
        name = variable_names[index]
        if index in numeric_indexes:
            values.append(
                (name, "numeric-system-missing")
                if _is_system_missing(value)
                else (name, "numeric-binary64", _binary64(value))
            )
        else:
            values.append((name, "string", value))
    return bytes.fromhex(_digest(values))


def _is_system_missing(value: Any) -> bool:
    if value is None or value is pd.NA:
        return True
    if isinstance(value, numbers.Real):
        return math.isnan(float(value))
    return False


def _binary64(value: Any) -> bytes:
    return struct.pack(">d", float(value))


def _component(
    value: Any, *, count: int = 1, available: bool = True,
) -> _Component:
    return _Component(
        digest=_digest(value if available else ("adapter-component-unavailable",)),
        count=count,
        available=available,
    )


def _sequence_component(values: Iterable[Any]) -> _Component:
    digest = hashlib.sha256()
    digest.update(b"openstatspec-semantic-sequence-v1\x00")
    count = 0
    for value in values:
        encoded = _canonical_bytes(value)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    digest.update(b"\xff")
    digest.update(count.to_bytes(8, "big"))
    return _Component(digest.hexdigest(), count)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        b"openstatspec-semantic-component-v1\x00" + _canonical_bytes(value)
    ).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    if value is None:
        return b"N"
    if value is pd.NA:
        return b"Q"
    if isinstance(value, bool):
        return b"B1" if value else b"B0"
    if isinstance(value, numbers.Integral):
        encoded = str(int(value)).encode("ascii")
        return b"I" + len(encoded).to_bytes(8, "big") + encoded
    if isinstance(value, numbers.Real):
        numeric = float(value)
        return b"Q" if math.isnan(numeric) else b"F" + struct.pack(">d", numeric)
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return b"S" + len(encoded).to_bytes(8, "big") + encoded
    if isinstance(value, bytes):
        return b"Y" + len(value).to_bytes(8, "big") + value
    if isinstance(value, Mapping):
        items = sorted(
            ((_canonical_bytes(key), _canonical_bytes(item)) for key, item in value.items()),
            key=lambda pair: pair[0],
        )
        return b"M" + _framed(pair for item in items for pair in item)
    if isinstance(value, (list, tuple)):
        return b"L" + _framed(_canonical_bytes(item) for item in value)
    if hasattr(value, "item"):
        return _canonical_bytes(value.item())
    raise TypeError(
        "SPSS semantic comparison encountered an unsupported observed value type: "
        + type(value).__name__
    )


def _framed(values: Iterable[bytes]) -> bytes:
    output = bytearray()
    count = 0
    for value in values:
        output.extend(len(value).to_bytes(8, "big"))
        output.extend(value)
        count += 1
    return count.to_bytes(8, "big") + bytes(output)
