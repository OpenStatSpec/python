"""Official OpenStatSpec SPSS SAV/ZSAV 1.0 manifest conformance."""

import json
import os
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

import pytest
import pandas as pd
import pyspssio
from sqlalchemy import create_engine, text
from sqlalchemy import inspect as inspect_database

import openstatspec
from conformance import compare_sav_semantics
from openstatspec.sql.profiles import profile_for_url


SEMANTIC_EXPECTATIONS = {
    "case_order", "variable_order", "binary64_values", "string_values",
    "numeric_system_missing", "blank_string_not_missing", "file_label",
    "documents_order", "variable_labels", "print_write_formats",
    "measurement_level", "variable_role", "display_width",
    "display_alignment", "discrete_numeric_missing",
    "discrete_string_missing", "numeric_range_missing",
    "lowest_highest_missing", "range_plus_discrete_missing",
    "raw_user_missing_values", "utf8_source_encoding",
    "string_over_255_bytes", "no_string_truncation",
    "long_string_value_labels", "zsav_zlib_decode", "zsav_zlib_encode",
    "dictionary_preserved", "values_preserved",
}
CATALOG_EXPECTATIONS = {
    "value_labels_typed_ordered", "dataset_attribute_arrays",
    "variable_attribute_arrays", "weight_variable", "variable_sets_ordered",
    "multiple_response_md", "multiple_response_mc",
    "multiple_response_members_ordered", "multiple_response_counted_value",
    "multiple_response_string_counted_value",
    "multiple_response_category_label_behavior",
    "multiple_response_label_source",
}
PREFLIGHT_EXPECTATIONS = {
    "atomic_failure", "no_dataset_row", "no_data_table", "operation_record",
    "fidelity_event_null_dataset_id", "target_capability_exceeded",
}


def _specification_root() -> Path:
    configured = os.environ.get("OPENSTATSPEC_SPECIFICATION_DIR")
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[2] / "specification",
    ]
    for candidate in candidates:
        if candidate and (candidate / "conformance/spss-sav-zsav-1.0.json").is_file():
            return candidate
    raise RuntimeError(
        "The official OpenStatSpec specification checkout is required; "
        "set OPENSTATSPEC_SPECIFICATION_DIR."
    )


def _manifest() -> dict:
    return json.loads(
        (_specification_root() / "conformance/spss-sav-zsav-1.0.json").read_text(
            encoding="utf-8"
        )
    )


def _fixture_source(fixture: dict) -> Path:
    source = _specification_root() / "conformance" / fixture["source"]
    assert source.is_file(), f"Missing official fixture: {fixture['id']}"
    return source


def _round_trip_fixtures() -> list[tuple[dict, Path]]:
    manifest = _manifest()
    assert manifest["manifest_version"] == "1.0"
    return [
        (fixture, _fixture_source(fixture))
        for fixture in manifest["fixtures"]
        if fixture["id"] != "preflight-failure"
    ]


def _rows(connection, statement: str, parameters: dict | None = None) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(text(statement), parameters or {}).mappings().all()
    ]


def _catalog_snapshot(database_url: str, dataset_name: str) -> dict:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        dataset_id = connection.execute(
            text("select dataset_id from dataset where dataset_name = :name"),
            {"name": dataset_name},
        ).scalar_one()
        labels = _rows(connection, """
            select v.source_name as variable, l.ordinal, l.code_kind as kind,
                   l.numeric_code, l.string_code, l.label
              from value_label l
              join value_label_set s on s.value_label_set_id = l.value_label_set_id
              join variable_value_label_set x on x.value_label_set_id = s.value_label_set_id
              join variable v on v.variable_id = x.variable_id
             where v.dataset_id = :dataset_id
             order by v.source_ordinal, l.ordinal
        """, {"dataset_id": dataset_id})
        dataset_attributes = _rows(connection, """
            select attribute_name, array_ordinal, attribute_value
              from dataset_attribute where dataset_id = :dataset_id
             order by attribute_name, array_ordinal
        """, {"dataset_id": dataset_id})
        variable_attributes = _rows(connection, """
            select v.source_name as variable, a.attribute_name,
                   a.array_ordinal, a.attribute_value
              from variable_attribute a join variable v on v.variable_id = a.variable_id
             where v.dataset_id = :dataset_id
             order by v.source_ordinal, a.attribute_name, a.array_ordinal
        """, {"dataset_id": dataset_id})
        variable_sets = _rows(connection, """
            select s.source_ordinal as set_ordinal, s.set_name,
                   m.source_ordinal as member_ordinal, v.source_name as member
              from variable_set s
              join variable_set_member m on m.variable_set_id = s.variable_set_id
              join variable v on v.variable_id = m.variable_id
             where s.dataset_id = :dataset_id
             order by s.source_ordinal, m.source_ordinal
        """, {"dataset_id": dataset_id})
        mr_sets = _rows(connection, """
            select s.source_ordinal as set_ordinal, s.set_name, s.set_label,
                   s.set_kind, s.counted_value_kind, s.counted_numeric_value,
                   s.counted_string_value, s.category_label_behavior,
                   s.label_source, m.source_ordinal as member_ordinal,
                   v.source_name as member
              from multiple_response_set s
              left join multiple_response_member m
                on m.multiple_response_set_id = s.multiple_response_set_id
              left join variable v on v.variable_id = m.variable_id
             where s.dataset_id = :dataset_id
             order by s.source_ordinal, m.source_ordinal
        """, {"dataset_id": dataset_id})
        weight = connection.execute(text("""
            select v.source_name
              from dataset_weight_variable w
              join variable v on v.variable_id = w.variable_id
             where w.dataset_id = :dataset_id
        """), {"dataset_id": dataset_id}).scalar_one_or_none()
    return {
        "value_labels": labels,
        "dataset_attributes": dataset_attributes,
        "variable_attributes": variable_attributes,
        "variable_sets": variable_sets,
        "multiple_response_sets": mr_sets,
        "weight_variable": weight,
    }


def _group_attributes(rows: list[dict], *, variable: bool) -> list[dict]:
    grouped: dict[tuple, list[str]] = defaultdict(list)
    for row in rows:
        key = (row["variable"], row["attribute_name"]) if variable else (row["attribute_name"],)
        grouped[key].append(row["attribute_value"])
    return [
        ({"variable": key[0], "name": key[1], "values": values}
         if variable else {"name": key[0], "values": values})
        for key, values in grouped.items()
    ]


def _group_sets(rows: list[dict]) -> list[dict]:
    grouped: dict[int, dict] = {}
    for row in rows:
        item = grouped.setdefault(row["set_ordinal"], {
            "ordinal": row["set_ordinal"], "name": row["set_name"], "members": [],
        })
        item["members"].append(row["member"])
    return list(grouped.values())


def _group_mr_sets(rows: list[dict]) -> list[dict]:
    grouped: dict[int, dict] = {}
    for row in rows:
        counted_value = (
            row["counted_numeric_value"]
            if row["counted_value_kind"] == "numeric"
            else row["counted_string_value"]
        )
        item = grouped.setdefault(row["set_ordinal"], {
            "ordinal": row["set_ordinal"], "name": row["set_name"],
            "kind": row["set_kind"], "label": row["set_label"],
            "counted_kind": row["counted_value_kind"],
            "counted_value": counted_value,
            "category_labels": row["category_label_behavior"],
            "label_source": row["label_source"], "members": [],
        })
        if row["member"] is not None:
            item["members"].append(row["member"])
    return list(grouped.values())


def _assert_expected_catalog(database_url: str, dataset_name: str, expected: dict) -> None:
    actual = _catalog_snapshot(database_url, dataset_name)
    if "weight_variable" in expected:
        assert actual["weight_variable"] == expected["weight_variable"]
    if "value_labels" in expected:
        normalized = [{
            "variable": row["variable"], "ordinal": row["ordinal"],
            "kind": row["kind"],
            "value": row["numeric_code"] if row["kind"] == "numeric" else row["string_code"],
            "label": row["label"],
        } for row in actual["value_labels"]]
        assert normalized == expected["value_labels"]
    if "dataset_attributes" in expected:
        assert _group_attributes(actual["dataset_attributes"], variable=False) == expected["dataset_attributes"]
    if "variable_attributes" in expected:
        assert _group_attributes(actual["variable_attributes"], variable=True) == expected["variable_attributes"]
    if "variable_sets" in expected:
        assert _group_sets(actual["variable_sets"]) == expected["variable_sets"]
    if "multiple_response_sets" in expected:
        assert _group_mr_sets(actual["multiple_response_sets"]) == expected["multiple_response_sets"]


def _assert_round_trip(
    *, fixture: dict, source: Path, database_url: str, tmp_path: Path, profile: str,
) -> None:
    assert fixture["directions"] == ["import", "export", "semantic_round_trip"]
    assert set(fixture["expects"]) <= SEMANTIC_EXPECTATIONS | CATALOG_EXPECTATIONS
    token = uuid4().hex[:10]
    dataset_id = f"official_{profile}_{fixture['id']}_{token}".replace("-", "_")
    destination = tmp_path / f"{dataset_id}{source.suffix}"

    imported = openstatspec.import_sav(
        source, database_url=database_url, dataset_id=dataset_id,
    )
    assert imported.diagnostics == ()
    assert openstatspec.validate(
        database_url=database_url, dataset_id=dataset_id,
    )["valid"] is True
    _assert_expected_catalog(database_url, dataset_id, fixture.get("expected_catalog", {}))

    exported = openstatspec.export_sav(
        database_url=database_url, dataset_id=dataset_id, destination=destination,
    )
    assert exported.diagnostics == ()
    assert compare_sav_semantics(source, destination) == {
        "equivalent": True,
        "differences": [],
    }


def test_manifest_required_capabilities_are_declared() -> None:
    manifest = _manifest()
    assert manifest["semantic_round_trip"] is True
    assert manifest["byte_identical_output_required"] is False
    assert openstatspec.capability_matrix()["required_capabilities"] == manifest["required_capabilities"]


@pytest.mark.parametrize(("fixture", "source"), _round_trip_fixtures())
def test_official_manifest_round_trips_through_sqlite(
    fixture: dict, source: Path, tmp_path: Path,
) -> None:
    _assert_round_trip(
        fixture=fixture, source=source,
        database_url=f"sqlite:///{tmp_path / 'official.sqlite'}",
        tmp_path=tmp_path, profile="sqlite",
    )


@pytest.mark.services
@pytest.mark.parametrize(
    ("environment_name", "profile"),
    [
        ("OPENSTATSPEC_POSTGRES_URL", "postgresql"),
        ("OPENSTATSPEC_MYSQL_URL", "mysql"),
        ("OPENSTATSPEC_MARIADB_URL", "mariadb"),
    ],
)
@pytest.mark.parametrize(("fixture", "source"), _round_trip_fixtures())
def test_official_manifest_round_trips_through_server_profiles(
    environment_name: str, profile: str, fixture: dict, source: Path, tmp_path: Path,
) -> None:
    database_url = os.environ.get(environment_name)
    if not database_url:
        pytest.skip(f"{environment_name} is not configured")
    _assert_round_trip(
        fixture=fixture, source=source, database_url=database_url,
        tmp_path=tmp_path, profile=profile,
    )


def _assert_official_preflight_failure(
    *, database_url: str, profile: str, tmp_path: Path,
) -> None:
    fixture = next(
        item for item in _manifest()["fixtures"] if item["id"] == "preflight-failure"
    )
    assert fixture["directions"] == ["import"]
    assert set(fixture["expects"]) == PREFLIGHT_EXPECTATIONS
    maximum = profile_for_url(database_url).max_physical_variables
    source = tmp_path / f"preflight-too-wide-{profile}-{uuid4().hex[:8]}.sav"
    columns = [f"v{ordinal:05d}" for ordinal in range(1, maximum + 2)]
    pyspssio.write_sav(
        str(source),
        pd.DataFrame([[float(ordinal) for ordinal in range(1, maximum + 2)]], columns=columns),
    )
    dataset_name = f"official_preflight_failure_{profile}_{uuid4().hex[:8]}"

    with pytest.raises(Exception, match="Target capability exceeded"):
        openstatspec.import_sav(
            source, database_url=database_url, dataset_id=dataset_name,
        )

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text("select count(*) from dataset where dataset_name = :name"),
            {"name": dataset_name},
        ).scalar_one() == 0
        event = connection.execute(text("""
            select f.operation_id, f.dataset_id, f.direction, f.severity,
                   f.event_code, f.source_item, f.created_at
              from fidelity_event f
             where f.source_item = :source_item
             order by f.created_at desc
        """), {"source_item": source.name}).mappings().one()
        operation = connection.execute(text("""
            select operation_kind, status, source_format, started_at, completed_at
              from operation where operation_id = :operation_id
        """), {"operation_id": event["operation_id"]}).mappings().one()
    assert not any(
        name.startswith(f"data_{dataset_name}")
        for name in inspect_database(engine).get_table_names()
    )
    assert tuple(operation[key] for key in (
        "operation_kind", "status", "source_format",
    )) == ("import", "failed", "SAV")
    assert operation["started_at"] and operation["completed_at"]
    assert tuple(event[key] for key in (
        "dataset_id", "direction", "severity", "event_code", "source_item",
    )) == (None, "import", "error", "target_capability_exceeded", source.name)
    assert event["created_at"]


def test_official_preflight_failure_is_atomic_and_diagnostic(tmp_path: Path) -> None:
    _assert_official_preflight_failure(
        database_url=f"sqlite:///{tmp_path / 'preflight.sqlite'}",
        profile="sqlite", tmp_path=tmp_path,
    )


@pytest.mark.services
@pytest.mark.parametrize(
    ("environment_name", "profile"),
    [
        ("OPENSTATSPEC_POSTGRES_URL", "postgresql"),
        ("OPENSTATSPEC_MYSQL_URL", "mysql"),
        ("OPENSTATSPEC_MARIADB_URL", "mariadb"),
    ],
)
def test_official_preflight_failure_through_server_profiles(
    environment_name: str, profile: str, tmp_path: Path,
) -> None:
    database_url = os.environ.get(environment_name)
    if not database_url:
        pytest.skip(f"{environment_name} is not configured")
    _assert_official_preflight_failure(
        database_url=database_url, profile=profile, tmp_path=tmp_path,
    )
