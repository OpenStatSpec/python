"""Small, contained bridge to pyspssio's IBM I/O dictionary functions.

The convenient read_metadata/write_sav helpers deliberately flatten some
dictionary information. OpenStatSpec needs separate print/write formats and
ordered repeated custom attributes. Keeping ctypes calls here makes that
boundary explicit and testable.
"""

from __future__ import annotations

from ctypes import POINTER, c_char_p, c_int
from typing import Any, Iterable, Mapping

from pyspssio.header import spss_formats_simple, varformat_to_tuple, warn_or_raise

FormatTuple = tuple[int, int, int]
AttributePair = tuple[str, str]


def format_tuples(header: Any) -> tuple[dict[str, FormatTuple], dict[str, FormatTuple]]:
    """Return raw SPSS print and write format tuples by variable."""
    return (
        {name: _get_format(header, name, "spssGetVarPrintFormat") for name in header.var_names},
        {name: _get_format(header, name, "spssGetVarWriteFormat") for name in header.var_names},
    )


def _get_format(header: Any, name: str, function_name: str) -> FormatTuple:
    function = getattr(header.spssio, function_name)
    function.argtypes = [c_int, c_char_p, POINTER(c_int), POINTER(c_int), POINTER(c_int)]
    kind, decimals, width = c_int(), c_int(), c_int()
    retcode = function(header.fh, name.encode(header.encoding), kind, decimals, width)
    warn_or_raise(retcode, function, name)
    return kind.value, width.value, decimals.value


def format_string(value: FormatTuple) -> str:
    kind, width, decimals = value
    return f"{spss_formats_simple[kind]}{width}" + (f".{decimals}" if decimals else "")


def format_tuple(value: Any) -> FormatTuple:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return int(value[0]), int(value[1]), int(value[2])
    return tuple(int(part) for part in varformat_to_tuple(value))  # type: ignore[return-value]


def set_format_tuples(
    header: Any, *, name: str, print_format: FormatTuple, write_format: FormatTuple,
) -> None:
    """Set print and write formats independently before dictionary commit."""
    for function_name, value in (
        ("spssSetVarPrintFormat", print_format),
        ("spssSetVarWriteFormat", write_format),
    ):
        function = getattr(header.spssio, function_name)
        function.argtypes = [c_int, c_char_p, c_int, c_int, c_int]
        kind, width, decimals = value
        retcode = function(
            header.fh, name.encode(header.encoding), c_int(kind), c_int(decimals), c_int(width),
        )
        warn_or_raise(retcode, function, name, value)


def file_attribute_pairs(header: Any) -> list[AttributePair]:
    return _get_attributes(header, function_name="spssGetFileAttributes")


def variable_attribute_pairs(header: Any, name: str) -> list[AttributePair]:
    return _get_attributes(header, function_name="spssGetVarAttributes", name=name)


def _get_attributes(header: Any, *, function_name: str, name: str | None = None) -> list[AttributePair]:
    """Read ordered attribute pairs without the public dict(zip()) loss."""
    function = getattr(header.spssio, function_name)

    def call(size: int) -> tuple[Any, Any, Any, int]:
        names = POINTER(c_char_p * size)()
        texts = POINTER(c_char_p * size)()
        count = c_int()
        argtypes = [c_int]
        arguments: list[Any] = [header.fh]
        if name is not None:
            argtypes.append(c_char_p)
            arguments.append(name.encode(header.encoding))
        argtypes.extend([
            POINTER(POINTER(c_char_p * size)), POINTER(POINTER(c_char_p * size)), POINTER(c_int),
        ])
        arguments.extend([names, texts, count])
        function.argtypes = argtypes
        retcode = function(*arguments)
        warn_or_raise(retcode, function, *([name] if name is not None else []))
        return names, texts, count, count.value

    names, texts, count, size = call(0)
    _free_attributes(header, names, texts, count)
    if not size:
        return []
    names, texts, count, size = call(size)
    try:
        return [
            (names[0][index].decode(header.encoding), texts[0][index].decode(header.encoding))
            for index in range(size)
        ]
    finally:
        _free_attributes(header, names, texts, count)


def _free_attributes(header: Any, names: Any, texts: Any, count: Any) -> None:
    function = header.spssio.spssFreeAttributes
    retcode = function(names, texts, count)
    warn_or_raise(retcode, function)


def attribute_values(pairs: Iterable[AttributePair]) -> dict[str, Any]:
    """Decode SPSS ``Name[1]`` array members to ordered catalog arrays."""
    import re

    scalar: dict[str, Any] = {}
    arrays: dict[str, list[tuple[int, str]]] = {}
    for name, value in pairs:
        match = re.fullmatch(r"(.+)\[(\d+)\]", name)
        if match and int(match.group(2)) > 0:
            arrays.setdefault(match.group(1), []).append((int(match.group(2)), value))
        else:
            scalar[name] = value
    for name, members in arrays.items():
        members.sort(key=lambda item: item[0])
        # A real SPSS attribute array begins at one.  Preserve a malformed or
        # non-contiguous spelling as literal attributes instead of guessing.
        if [index for index, _ in members] == list(range(1, len(members) + 1)):
            scalar.pop(name, None)
            scalar[name] = [value for _, value in members]
        else:
            scalar.update({f"{name}[{index}]": value for index, value in members})
    return scalar


def attribute_pairs(values: Mapping[str, Any]) -> list[AttributePair]:
    """Encode catalog arrays as IBM SPSS ``Name[1]`` attribute members."""
    pairs: list[AttributePair] = []
    for name, value in values.items():
        if isinstance(value, (list, tuple)):
            pairs.extend(
                (f"{name}[{index}]", "" if item is None else str(item))
                for index, item in enumerate(value, start=1)
            )
        else:
            pairs.append((str(name), "" if value is None else str(value)))
    return pairs


def set_file_attribute_pairs(header: Any, pairs: Iterable[AttributePair]) -> None:
    _set_attributes(header, function_name="spssSetFileAttributes", pairs=pairs)


def set_variable_attribute_pairs(header: Any, name: str, pairs: Iterable[AttributePair]) -> None:
    _set_attributes(header, function_name="spssSetVarAttributes", pairs=pairs, name=name)


def _set_attributes(
    header: Any, *, function_name: str, pairs: Iterable[AttributePair], name: str | None = None,
) -> None:
    values = list(pairs)
    if not values:
        return
    size = len(values)
    names = (c_char_p * size)(*(key.encode(header.encoding) for key, _ in values))
    texts = (c_char_p * size)(*(value.encode(header.encoding) for _, value in values))
    function = getattr(header.spssio, function_name)
    argtypes = [c_int]
    arguments: list[Any] = [header.fh]
    if name is not None:
        argtypes.append(c_char_p)
        arguments.append(name.encode(header.encoding))
    argtypes.extend([POINTER(c_char_p * size), POINTER(c_char_p * size), c_int])
    arguments.extend([names, texts, c_int(size)])
    function.argtypes = argtypes
    retcode = function(*arguments)
    warn_or_raise(retcode, function, *([name] if name is not None else []))
