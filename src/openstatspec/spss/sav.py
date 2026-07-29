"""Strict SAV/ZSAV adapter backed exclusively by pyspssio.

The adapter keeps SPSS data in one physical wide table and preserves every
metadata feature exposed by pyspssio plus standard type-6 document records. A source feature that neither path can
observe or write is a durable fidelity event and blocks export unless the
caller explicitly accepts that exact loss.
"""

import hashlib
import json
import math
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd
import pyspssio

from .raw_dictionary import (
    RawDictionaryError,
    read_document_lines,
    write_compatible_names,
    write_document_lines,
    write_extended_mrset_labels,
)
from .dictionary import (
    attribute_pairs,
    attribute_values,
    file_attribute_pairs,
    format_string,
    format_tuple,
    format_tuples,
    set_file_attribute_pairs,
    set_format_tuples,
    set_variable_attribute_pairs,
    variable_attribute_pairs,
)
from ..core import UnsupportedOperationError
from ..sql.wide import (
    create_wide_dataset,
    physical_name,
    read_fidelity_events,
    read_wide_dataset,
    record_export_operation,
    validate_spss_catalog,
)

_UTF8_ENCODINGS = {"UTF-8", "UTF8"}
def engine_identity() -> dict[str, str]:
    """Return the exact pinned SPSS engine identity for audit records."""
    return {
        "package": "openstatspec-pyspssio",
        "module": "pyspssio",
        "repository": "TonisOrmisson/pyspssio",
        "pinned_commit": "e069adf",
        "installed_version": str(pyspssio.__version__),
    }


def _dictionary(source_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read public pyspssio metadata plus the exposed variable-set property."""
    metadata = dict(pyspssio.read_metadata(str(source_path)))
    try:
        with pyspssio.Reader(str(source_path), mode="r") as reader:
            metadata["_var_sets"] = dict(reader.var_sets or {})
            print_formats, write_formats = format_tuples(reader)
            metadata["_print_format_tuples"] = print_formats
            metadata["_write_format_tuples"] = write_formats
            metadata["file_attributes"] = attribute_values(file_attribute_pairs(reader))
            metadata["var_attributes"] = {
                name: attribute_values(variable_attribute_pairs(reader, name))
                for name in reader.var_names
                if variable_attribute_pairs(reader, name)
            }
        try:
            metadata["_documents"] = read_document_lines(
                source_path, encoding=str(metadata.get("encoding") or "UTF-8"),
            )
        except RawDictionaryError as error:
            metadata["_documents"] = None
            metadata["_documents_error"] = str(error)
    except Exception as error:  # The source stays importable, but not silently faithful.
        metadata["_var_sets"] = None
        metadata["_var_sets_error"] = str(error)
    return metadata, _engine_loss_report(metadata)


def _variables(metadata: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    used = {"__case_ordinal"}
    types = dict(metadata.get("var_types") or {})
    labels = dict(metadata.get("var_labels") or {})
    formats = dict(metadata.get("var_formats") or {})
    print_formats = dict(metadata.get("_print_format_tuples") or {})
    write_formats = dict(metadata.get("_write_format_tuples") or {})
    measures = dict(metadata.get("var_measure_levels") or {})
    roles = dict(metadata.get("var_roles") or {})
    alignments = dict(metadata.get("var_alignments") or {})
    displays = dict(metadata.get("var_column_widths") or {})
    value_labels = dict(metadata.get("var_value_labels") or {})
    missing = dict(metadata.get("var_missing_values") or {})
    attributes = dict(metadata.get("var_attributes") or {})
    compat_names = dict(metadata.get("var_compat_names") or {})
    variables: list[dict[str, Any]] = []
    for ordinal, source_name in enumerate(names, start=1):
        width = int(types.get(source_name) or 0)
        kind = "string" if width else "numeric"
        variables.append({
            "ordinal": ordinal,
            "source_name": source_name,
            "physical_name": physical_name(source_name, used),
            "storage_kind": kind,
            "readstat_storage_type": f"pyspssio:{kind}",
            "string_width": width if kind == "string" else None,
            "label": str(labels.get(source_name) or ""),
            "format": format_string(print_formats[source_name]) if source_name in print_formats else formats.get(source_name),
            "print_format": _json(list(print_formats[source_name])) if source_name in print_formats else None,
            "write_format": _json(list(write_formats[source_name])) if source_name in write_formats else None,
            "measure": measures.get(source_name),
            "role": roles.get(source_name),
            "alignment": alignments.get(source_name),
            "display_width": displays.get(source_name),
            "attributes": _json_ordered(attributes.get(source_name, {})),
            "compat_name": compat_names.get(source_name),
            "value_labels": _json(value_labels.get(source_name, {})),
            "missing_ranges": _json(_missing_ranges(missing.get(source_name))),
        })
    return variables


def _missing_ranges(rule: Any) -> list[Any]:
    """Map pyspssio's {values, lo, hi} form to the catalog's ordered rules."""
    if not rule:
        return []
    if not isinstance(rule, dict):
        return list(rule) if isinstance(rule, (list, tuple)) else [rule]
    result: list[Any] = []
    if "lo" in rule or "hi" in rule:
        result.append({"lo": rule.get("lo"), "hi": rule.get("hi")})
    result.extend(rule.get("values") or [])
    return result


def _pyspssio_missing_rules(encoded: str) -> dict[str, Any] | None:
    rules = json.loads(encoded or "[]")
    if not rules:
        return None
    discrete: list[Any] = []
    range_rule: dict[str, Any] | None = None
    for rule in rules:
        if isinstance(rule, dict):
            lower, upper = rule.get("lo"), rule.get("hi")
            if lower == upper:
                discrete.append(lower)
            else:
                if range_rule is not None:
                    raise UnsupportedOperationError(
                        "SPSS permits at most one user-missing range per variable; catalog contains more."
                    )
                range_rule = {"lo": lower, "hi": upper}
        else:
            discrete.append(rule)
    if range_rule is not None:
        if len(discrete) > 1:
            raise UnsupportedOperationError(
                "SPSS permits one discrete user-missing value alongside a range; catalog contains more."
            )
        if discrete:
            range_rule["values"] = discrete
        return range_rule
    if len(discrete) > 3:
        raise UnsupportedOperationError(
            "SPSS permits at most three discrete user-missing values per variable."
        )
    return {"values": discrete}


def _rows(frame: pd.DataFrame, variables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for input_row in frame.to_dict(orient="records"):
        output: dict[str, Any] = {}
        for item in variables:
            value = input_row[item["source_name"]]
            if item["storage_kind"] == "numeric" and pd.isna(value):
                value = None
            elif item["storage_kind"] == "string" and (value is None or pd.isna(value)):
                value = ""
            output[item["physical_name"]] = value
        records.append(output)
    return records


def inspect_sav(source: str | Path) -> dict[str, Any]:
    source_path = Path(source)
    _require_source(source_path)
    metadata, loss_report = _dictionary(source_path)
    names = list(metadata.get("var_names") or [])
    variables = _variables(metadata, names)
    loss_report = _merge_loss_reports(tuple(loss_report.values()))
    return {
        "source_format": source_path.suffix[1:].upper(),
        "engine": engine_identity(),
        "source_name": source_path.name,
        "source_sha256": _sha256(source_path),
        "source_encoding": metadata.get("encoding"),
        "file_label": str(metadata.get("file_label") or ""),
        "documents": list(metadata.get("_documents") or []),
        "file_attributes": dict(metadata.get("file_attributes") or {}),
        "case_weight_variable": metadata.get("case_weight_var") or None,
        "multiple_response_sets": dict(metadata.get("mrsets") or {}),
        "loss_report": loss_report,
        "variable_count": len(names),
        "variables": variables,
    }


def import_sav_dataset(*, source: str | Path, database_url: str, dataset_id: str) -> dict[str, Any]:
    source_path = Path(source)
    _require_source(source_path)
    frame, metadata = pyspssio.read_sav(
        str(source_path), convert_datetimes=False, include_user_missing=True,
    )
    metadata = dict(metadata)
    dictionary, loss_report = _dictionary(source_path)
    # read_sav and read_metadata expose the same header fields; retain the
    # frame read's values where a future pyspssio version exposes more there.
    metadata = {**metadata, **dictionary}
    variables = _variables(metadata, list(frame.columns))
    loss_report = _merge_loss_reports(tuple(loss_report.values()))
    result = create_wide_dataset(
        database_url=database_url,
        dataset_id=dataset_id,
        source_name=source_path.name,
        source_format=source_path.suffix[1:].upper(),
        rows=_rows(frame, variables),
        variables=variables,
        file_label=str(metadata.get("file_label") or ""),
        source_encoding=metadata.get("encoding"),
        source_sha256=_sha256(source_path),
        imported_at=datetime.now(UTC).isoformat(),
        documents=_json_ordered(metadata.get("_documents") or []),
        file_attributes=_json_ordered(metadata.get("file_attributes") or {}),
        file_attribute_values=dict(metadata.get("file_attributes") or {}),
        variable_attribute_values=dict(metadata.get("var_attributes") or {}),
        case_weight_variable=metadata.get("case_weight_var") or None,
        multiple_response_sets=_json(metadata.get("mrsets") or {}),
        source_extensions=(
            {"spss.variable_sets": metadata["_var_sets"]}
            if metadata.get("_var_sets") else {}
        ),
        fidelity_events=loss_report,
        operation_details={"engine": engine_identity()},
    )
    return {**result, "loss_report": loss_report}


def export_sav_dataset(
    *, database_url: str, dataset_id: str, destination: str | Path,
    allow_loss: tuple[str, ...] = (), legacy_locale: str | None = None,
) -> dict[str, Any]:
    destination_path = Path(destination)
    if destination_path.suffix.lower() not in {".sav", ".zsav"}:
        raise UnsupportedOperationError("Export destinations must use the .sav or .zsav extension.")
    dataset, variables, rows = read_wide_dataset(database_url=database_url, dataset_id=dataset_id)
    validate_spss_catalog(
        variables,
        case_weight_variable=dataset.get("case_weight_variable"),
        multiple_response_sets=dataset.get("multiple_response_sets"),
    )
    persisted_events = read_fidelity_events(database_url=database_url, dataset_id=dataset_id)
    if legacy_locale is not None:
        persisted_events = tuple(
            event for event in persisted_events
            if event["code"] != "source-encoding-not-preserved"
        )
    loss_report = _merge_loss_reports(
        _export_loss_report(dataset, variables, legacy_locale=legacy_locale),
        persisted_events,
    )
    rejected = [event["code"] for event in loss_report if event["code"] not in allow_loss]
    if rejected:
        raise UnsupportedOperationError("Export requires explicit allow_loss for: " + ", ".join(rejected))

    frame = pd.DataFrame(
        [
            {
                variable["source_name"]: (
                    math.nan if row[variable["physical_name"]] is None else float(row[variable["physical_name"]])
                ) if variable["storage_kind"] == "numeric" else row[variable["physical_name"]]
                for variable in variables
            }
            for row in rows
        ],
        columns=[variable["source_name"] for variable in variables],
    )
    try:
        _write_with_dictionary_bridge(
            destination_path, frame, dataset, variables, legacy_locale=legacy_locale,
        )
    except Exception:
        destination_path.unlink(missing_ok=True)
        raise
    operation_id = record_export_operation(
        database_url=database_url,
        dataset_id=dataset_id,
        destination=str(destination_path),
        allowed_fidelity_events=tuple(
            event for event in loss_report if event["code"] in allow_loss
        ),
        operation_details={"engine": engine_identity(), "legacy_locale": legacy_locale},
    )
    return {
        "dataset_id": dataset_id,
        "destination": str(destination_path),
        "operation_id": operation_id,
        "loss_report": loss_report,
    }


def _write_with_dictionary_bridge(
    destination: Path, frame: pd.DataFrame, dataset: dict[str, Any], variables: list[dict[str, Any]],
    *, legacy_locale: str | None = None,
) -> None:
    """Write a dictionary through pyspssio and preserve its document record.

    IBM I/O can copy document records but cannot expose their lines.  The
    strict helper reads and writes a temporary UTF-8 SAV source; the selected
    pyspssio engine then copies it into the real SAV or ZSAV writer, which
    keeps any ZSAV internal dictionary offsets valid.
    """
    metadata = _writer_metadata(dataset, variables)
    documents = _json_load(dataset.get("documents"), [])
    source_encoding = str(dataset.get("source_encoding") or "UTF-8")
    legacy_output = _is_non_utf8_encoding(source_encoding) and legacy_locale is not None
    with ExitStack() as stack:
        document_source = None
        if documents:
            temporary_directory = Path(stack.enter_context(TemporaryDirectory()))
            temporary_source = temporary_directory / "documents.sav"
            pyspssio.write_sav(
                str(temporary_source), pd.DataFrame({"document_source": [0.0]}),
                unicode=not legacy_output, locale=legacy_locale,
            )
            temporary_encoding = str(pyspssio.read_metadata(str(temporary_source)).get("encoding") or "UTF-8")
            _require_matching_legacy_encoding(source_encoding, temporary_encoding, legacy_output)
            write_document_lines(temporary_source, documents, encoding=temporary_encoding)
            document_source = stack.enter_context(
                pyspssio.Reader(
                    str(temporary_source), mode="r",
                    unicode=not legacy_output, locale=legacy_locale,
                )
            )
        writer = stack.enter_context(
            pyspssio.Writer(
                str(destination), mode="w", unicode=not legacy_output, locale=legacy_locale,
            )
        )
        writer.compression = 2 if destination.suffix.lower() == ".zsav" else 1
        writer.file_label = str(dataset.get("file_label") or "")
        for variable in variables:
            writer._add_var(  # pylint: disable=protected-access
                variable["source_name"],
                int(variable["string_width"] or 0) if variable["storage_kind"] == "string" else 0,
            )
        for variable in variables:
            set_format_tuples(
                writer, name=variable["source_name"],
                print_format=_catalog_format(variable, "print_format"),
                write_format=_catalog_format(variable, "write_format"),
            )
        for name in (
            "var_labels", "var_measure_levels", "var_roles", "var_alignments",
            "var_column_widths", "var_value_labels", "var_missing_values", "mrsets",
        ):
            value = metadata.get(name)
            if value:
                setattr(writer, name, value)
        if dataset.get("case_weight_variable"):
            writer.case_weight_var = dataset["case_weight_variable"]
        set_file_attribute_pairs(
            writer, attribute_pairs(_json_load(dataset.get("file_attributes"), {})),
        )
        for variable in variables:
            values = _json_load(variable.get("attributes"), {})
            if values:
                set_variable_attribute_pairs(
                    writer, variable["source_name"], attribute_pairs(values),
                )
        variable_sets = (dataset.get("source_extensions") or {}).get("spss.variable_sets")
        if variable_sets:
            writer.var_sets = variable_sets
        if document_source is not None:
            writer.copy_documents_from(document_source)
        writer.commit_header()
        _require_matching_legacy_encoding(source_encoding, str(writer.file_encoding), legacy_output)
        writer.write_data(frame)
    write_compatible_names(
        destination,
        {
            str(variable["source_name"]): str(variable["compat_name"])
            for variable in variables
            if variable.get("compat_name")
            and str(variable["compat_name"]).casefold() != str(variable["source_name"]).casefold()
        },
        encoding="UTF-8",
    )
    extended_labels = {
        str(name): str(definition["label"])
        for name, definition in _json_load(dataset.get("multiple_response_sets"), {}).items()
        if isinstance(definition, dict)
        and definition.get("use_category_labels")
        and definition.get("use_first_var_label")
        and definition.get("label")
    }
    write_extended_mrset_labels(
        destination, extended_labels, encoding=source_encoding,
    )

def _catalog_format(variable: dict[str, Any], key: str) -> tuple[int, int, int]:
    encoded = variable.get(key)
    if encoded:
        return format_tuple(_json_load(encoded, encoded))
    legacy = variable.get("format")
    if legacy:
        return format_tuple(legacy)
    if variable["storage_kind"] == "string":
        return 1, int(variable["string_width"] or 1), 0
    return 5, 8, 2


def _writer_metadata(dataset: dict[str, Any], variables: list[dict[str, Any]]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "case_weight_var": dataset.get("case_weight_variable") or None,
        "mrsets": _json_load(dataset.get("multiple_response_sets"), {}),
        "var_types": {
            item["source_name"]: int(item["string_width"] or 1)
            for item in variables if item["storage_kind"] == "string"
        },
        "var_labels": {
            item["source_name"]: item["label"] for item in variables if item.get("label")
        },
        "var_measure_levels": {
            item["source_name"]: item["measure"] for item in variables if item.get("measure")
        },
        "var_roles": {
            item["source_name"]: item["role"] for item in variables if item.get("role")
        },
        "var_alignments": {
            item["source_name"]: item["alignment"] for item in variables if item.get("alignment")
        },
        "var_column_widths": {
            item["source_name"]: int(item["display_width"])
            for item in variables if item.get("display_width") is not None
        },
        "var_value_labels": {
            item["source_name"]: _typed_value_labels(item)
            for item in variables if _json_load(item.get("value_labels"), {})
        },
        "var_missing_values": {
            item["source_name"]: missing
            for item in variables
            if (missing := _pyspssio_missing_rules(item.get("missing_ranges") or "[]"))
        },
    }
    return {key: value for key, value in metadata.items() if value not in ({}, None)}


def _typed_value_labels(variable: dict[str, Any]) -> dict[Any, str]:
    raw = _json_load(variable.get("value_labels"), {})
    if variable["storage_kind"] == "numeric":
        return {float(value): str(label) for value, label in raw.items()}
    return {str(value): str(label) for value, label in raw.items()}


def _engine_loss_report(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    if metadata.get("_documents") is None:
        events["documents-unreadable"] = {
            "code": "documents-unreadable",
            "detail": "The strict raw SAV dictionary reader could not inspect document text.",
            "details": {"engine_error": metadata.get("_documents_error", "unknown")},
        }
    variable_sets = metadata.get("_var_sets")
    if variable_sets is None:
        events["variable-sets-unobservable"] = {
            "code": "variable-sets-unobservable",
            "detail": "pyspssio could not inspect source variable sets; they cannot be preserved silently.",
            "details": {"engine_error": metadata.get("_var_sets_error", "unknown")},
        }
    if _is_non_utf8_encoding(metadata.get("encoding")):
        events["source-encoding-not-preserved"] = {
            "code": "source-encoding-not-preserved",
            "detail": "The pyspssio writer has no source-encoding preservation contract for this legacy code page.",
            "details": {"source_encoding": metadata.get("encoding")},
        }
    return events


def _export_loss_report(
    dataset: dict[str, Any], variables: list[dict[str, Any]], *, legacy_locale: str | None,
) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    if _is_non_utf8_encoding(dataset.get("source_encoding")) and legacy_locale is None:
        events.append({
            "code": "source-encoding-not-preserved",
            "detail": "Export requires an explicit legacy_locale for this non-UTF-8 source encoding.",
            "details": {"source_encoding": dataset.get("source_encoding")},
        })
    return tuple(events)


def _require_matching_legacy_encoding(
    source_encoding: str, emitted_encoding: str, legacy_output: bool,
) -> None:
    if legacy_output and _normalize_encoding(source_encoding) != _normalize_encoding(emitted_encoding):
        raise UnsupportedOperationError(
            "Configured legacy locale emitted "
            + emitted_encoding
            + " instead of required source encoding "
            + source_encoding
            + "."
        )


def _normalize_encoding(value: str) -> str:
    return str(value).strip().replace("_", "-").replace("WINDOWS-", "CP").upper()

def _merge_loss_reports(*reports: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    events: dict[str, dict[str, Any]] = {}
    for report in reports:
        for event in report:
            events.setdefault(event["code"], event)
    return tuple(events[code] for code in sorted(events))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _json_ordered(value: Any) -> str:
    """Legacy JSON copy for migration only; retain source insertion order."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_load(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _is_non_utf8_encoding(encoding: Any) -> bool:
    return encoding is not None and str(encoding).strip().replace("_", "-").upper() not in _UTF8_ENCODINGS


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_source(source_path: Path) -> None:
    if source_path.suffix.lower() not in {".sav", ".zsav"}:
        raise UnsupportedOperationError("Only unencrypted SAV and ZSAV sources are supported.")
