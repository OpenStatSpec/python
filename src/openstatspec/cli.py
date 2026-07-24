"""Command-line entry points for the database-connected workflow."""

import argparse
from collections.abc import Sequence

from .api import export_sav, import_sav


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openstatspec")
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="import one SAV/ZSAV file")
    importer.add_argument("source")
    importer.add_argument("--database-url", required=True)
    importer.add_argument("--dataset-id", required=True)
    exporter = commands.add_parser("export", help="export one dataset to SAV/ZSAV")
    exporter.add_argument("--database-url", required=True)
    exporter.add_argument("--dataset-id", required=True)
    exporter.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "import":
        import_sav(args.source, database_url=args.database_url, dataset_id=args.dataset_id)
    else:
        export_sav(database_url=args.database_url, dataset_id=args.dataset_id, destination=args.output)
    return 0
