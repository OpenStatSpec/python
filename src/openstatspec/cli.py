"""Command-line entry points for the database-connected workflow."""
import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .api import (
    apply_spss_in_place, apply_transformation_plan_in_place,
    capability_matrix, derive_sql_dataset, dolt_state_snapshot,
    execute_sql_transformation,
    export_sav, get_dataset, import_sav, initialize_catalog, inspect, list_datasets,
    install_in_place_transformation_schema, register_sql_transformation,
    validate, validate_derived,
)


def _json(value: str | None, fallback):
    return fallback if value is None else json.loads(value)


def _add_parent(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--parent-dataset-id", required=True)
    parser.add_argument("--parent-kind", choices=["core", "derived"], default="core")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openstatspec")
    commands = parser.add_subparsers(dest="command", required=True)
    capability_parser = commands.add_parser("capabilities", help="show supported and lossy feature matrix")
    capability_parser.add_argument("--database-url", help="include active connection limits")
    dolt_state = commands.add_parser("dolt-state", help="show read-only Dolt branch, HEAD, status, and diff evidence")
    dolt_state.add_argument("--database-url", required=True)
    initializer = commands.add_parser("init", help="initialize or migrate a dedicated catalog")
    initializer.add_argument("--database-url", required=True)
    importer = commands.add_parser("import", help="import one SAV/ZSAV file")
    importer.add_argument("source")
    importer.add_argument("--database-url", required=True)
    importer.add_argument("--dataset-id", required=True)
    inspector = commands.add_parser("inspect", help="inspect one SAV/ZSAV dictionary")
    inspector.add_argument("source")
    validator = commands.add_parser("validate", help="validate one imported dataset")
    validator.add_argument("--database-url", required=True)
    validator.add_argument("--dataset-id", required=True)
    exporter = commands.add_parser("export", help="export one dataset to SAV/ZSAV")
    exporter.add_argument("--database-url", required=True)
    exporter.add_argument("--dataset-id", required=True)
    exporter.add_argument("--output", required=True)
    exporter.add_argument("--allow-loss", action="append", default=[])
    exporter.add_argument("--legacy-locale", help="OS locale for a non-UTF-8 source encoding")

    catalog_list = commands.add_parser("catalog-list", help="list public catalog datasets")
    catalog_list.add_argument("--database-url", required=True)
    catalog_list.add_argument("--kind", choices=["core", "derived"])
    catalog_show = commands.add_parser("catalog-show", help="show one public catalog dataset")
    catalog_show.add_argument("--database-url", required=True)
    catalog_show.add_argument("--dataset-id", required=True)
    catalog_show.add_argument("--kind", choices=["core", "derived"], required=True)

    register = commands.add_parser("transform-register", help="register immutable SELECT SQL")
    _add_parent(register)
    register.add_argument("--sql", required=True)
    register.add_argument("--columns-json", required=True)
    register.add_argument("--mode", choices=["materialized"], default="materialized")
    register.add_argument("--name")

    run = commands.add_parser("transform-run", help="execute a registered SQL version")
    _add_parent(run)
    run.add_argument("--transformation-version-id", required=True)
    run.add_argument("--parameters-json")
    run.add_argument("--dataset-name")
    run.add_argument("--weight-variable")

    derive = commands.add_parser("derive", help="register and execute SELECT SQL")
    _add_parent(derive)
    derive.add_argument("--sql", required=True)
    derive.add_argument("--columns-json", required=True)
    derive.add_argument("--parameters-json")
    derive.add_argument("--mode", choices=["materialized"], default="materialized")
    derive.add_argument("--name")
    derive.add_argument("--dataset-name")
    derive.add_argument("--weight-variable")

    apply_spss = commands.add_parser(
        "apply-spss",
        help="compile supported SPSS syntax and apply it in-place",
    )
    apply_spss.add_argument("--database-url", required=True)
    apply_spss.add_argument("--dataset-id", required=True)
    apply_spss.add_argument("--actor", required=True)
    apply_spss.add_argument("--expected-branch")
    apply_spss.add_argument("--expected-head")
    syntax_source = apply_spss.add_mutually_exclusive_group(required=True)
    syntax_source.add_argument("--syntax")
    syntax_source.add_argument("--syntax-file")

    apply_plan = commands.add_parser(
        "apply-plan",
        help="apply a canonical transformation plan in-place",
    )
    apply_plan.add_argument("--database-url", required=True)
    apply_plan.add_argument("--dataset-id", required=True)
    apply_plan.add_argument("--actor", required=True)
    apply_plan.add_argument("--expected-branch")
    apply_plan.add_argument("--expected-head")
    apply_plan.add_argument("--plan-file", required=True)

    install_in_place = commands.add_parser(
        "install-in-place-schema",
        help="install or upgrade the compact in-place apply audit schema",
    )
    install_in_place.add_argument("--database-url", required=True)

    derived_validator = commands.add_parser("validate-derived", help="validate a derived dataset")
    derived_validator.add_argument("--database-url", required=True)
    derived_validator.add_argument("--derived-dataset-id", required=True)

    args = parser.parse_args(argv)
    if args.command == "capabilities":
        output = capability_matrix(database_url=args.database_url)
    elif args.command == "dolt-state":
        output = dolt_state_snapshot(database_url=args.database_url)
    elif args.command == "init":
        output = initialize_catalog(database_url=args.database_url)
    elif args.command == "import":
        output = import_sav(args.source, database_url=args.database_url, dataset_id=args.dataset_id)
    elif args.command == "export":
        output = export_sav(
            database_url=args.database_url, dataset_id=args.dataset_id,
            destination=args.output, allow_loss=args.allow_loss,
            legacy_locale=args.legacy_locale,
        )
    elif args.command == "inspect":
        output = inspect(args.source)
    elif args.command == "validate":
        output = validate(database_url=args.database_url, dataset_id=args.dataset_id)
    elif args.command == "catalog-list":
        output = list_datasets(database_url=args.database_url, kind=args.kind)
    elif args.command == "catalog-show":
        output = get_dataset(
            database_url=args.database_url, dataset_id=args.dataset_id, kind=args.kind,
        )
    elif args.command == "transform-register":
        output = register_sql_transformation(
            database_url=args.database_url, parent_dataset_id=args.parent_dataset_id,
            parent_kind=args.parent_kind, query_sql=args.sql,
            columns=_json(args.columns_json, []), output_mode=args.mode,
            transformation_name=args.name,
        )
    elif args.command == "transform-run":
        output = execute_sql_transformation(
            database_url=args.database_url,
            transformation_version_id=args.transformation_version_id,
            parent_dataset_id=args.parent_dataset_id, parent_kind=args.parent_kind,
            parameters=_json(args.parameters_json, {}), dataset_name=args.dataset_name,
            weight_variable=args.weight_variable,
        )
    elif args.command == "derive":
        output = derive_sql_dataset(
            database_url=args.database_url, parent_dataset_id=args.parent_dataset_id,
            parent_kind=args.parent_kind, query_sql=args.sql,
            columns=_json(args.columns_json, []),
            parameters=_json(args.parameters_json, {}), output_mode=args.mode,
            transformation_name=args.name, dataset_name=args.dataset_name,
            weight_variable=args.weight_variable,
        )
    elif args.command == "apply-spss":
        source_text = (
            args.syntax
            if args.syntax is not None
            else Path(args.syntax_file).read_text(encoding="utf-8")
        )
        output = apply_spss_in_place(
            database_url=args.database_url,
            dataset_id=args.dataset_id,
            source_text=source_text,
            actor=args.actor,
            expected_branch=args.expected_branch,
            expected_head=args.expected_head,
        )
    elif args.command == "apply-plan":
        plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        output = apply_transformation_plan_in_place(
            database_url=args.database_url,
            dataset_id=args.dataset_id,
            plan=plan,
            actor=args.actor,
            expected_branch=args.expected_branch,
            expected_head=args.expected_head,
        )
    elif args.command == "install-in-place-schema":
        install_in_place_transformation_schema(database_url=args.database_url)
        output = {"status": "installed"}
    else:
        output = validate_derived(
            database_url=args.database_url, derived_dataset_id=args.derived_dataset_id,
        )
    print(json.dumps(output.as_dict() if hasattr(output, "as_dict") else output, default=str, sort_keys=True))
    return 0
