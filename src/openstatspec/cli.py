"""Command-line entry points for the database-connected workflow."""
import json

import argparse
from collections.abc import Sequence

from .api import capability_matrix, export_sav, import_sav, inspect, validate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openstatspec")
    commands = parser.add_subparsers(dest="command", required=True)
    capability_parser = commands.add_parser("capabilities", help="show supported and lossy feature matrix")
    capability_parser.add_argument("--database-url", help="include active connection limits")
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
    args = parser.parse_args(argv)
    if args.command == "capabilities":
        result = capability_matrix(database_url=args.database_url)
    elif args.command == "import":
        result = import_sav(args.source, database_url=args.database_url, dataset_id=args.dataset_id)
    elif args.command == "export":
        result = export_sav(database_url=args.database_url, dataset_id=args.dataset_id, destination=args.output, allow_loss=args.allow_loss, legacy_locale=args.legacy_locale)
    elif args.command == "inspect":
        result = inspect(args.source)
    else:
        result = validate(database_url=args.database_url, dataset_id=args.dataset_id)
    print(json.dumps(result.as_dict() if hasattr(result, "as_dict") else result, default=str, sort_keys=True))
    return 0
