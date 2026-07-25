# 0.1.0 release readiness

This page records the expected release contract, not a publication event.
Creating a version tag remains a separate maintainer action.

## Supported workflow

For a supported unencrypted SAV or ZSAV source, the adapter imports one source
dataset into exactly one dedicated wide data table plus catalog tables. It can
validate that representation and export it back to SAV or ZSAV.

## SQL profiles

| Profile | Connection URL | Verification |
| --- | --- | --- |
| SQLite | `sqlite:///dataset.sqlite` | Local reference fixture |
| PostgreSQL | `postgresql+psycopg://…` | GitHub Actions PostgreSQL service |
| MySQL/MariaDB | `mysql+pymysql://…` | GitHub Actions MySQL service |

The MySQL service test establishes the shared MySQL/MariaDB profile contract.
It is not a claim that every MariaDB release or configuration has separately
been tested.

## Export-loss policy

`capability_matrix()` and `openstatspec capabilities` expose the current
feature state. Before export, the adapter combines source and writer
diagnostics. An export with known loss fails unless the caller passes the
relevant diagnostic codes through `allow_loss` or repeats `--allow-loss CODE`
in the CLI.

Known boundaries include multiple-response sets, variable alignment, variable
sets, and custom attributes. A caller that supplies consent receives the
machine-readable loss report with the export result.

## Maintainer checks before tagging

1. Run `python -m pytest`.
2. Run the service-backed profile checks with configured PostgreSQL and MySQL
   URLs, or confirm the GitHub Actions SQL-services job is green.
3. Build with `python -m build` and install the generated wheel in a clean
   environment.
4. Confirm `openstatspec capabilities` reflects the intended support boundary.
5. Review this document, the README, and CHANGELOG for accurate scope.
