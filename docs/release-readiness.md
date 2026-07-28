# 0.1.0 release readiness

This page records the expected release contract, not a publication event.
Creating a version tag remains a separate maintainer action.

## Supported workflow

For a supported unencrypted SAV or ZSAV source, the adapter imports one source
dataset into exactly one dedicated wide data table plus the complete singular,
UUID-keyed normative catalog from the SPSS profile. It can validate that
representation and export it back to SAV or ZSAV. Historical `*_catalog`
tables remain private compatibility storage and do not replace the normative
database contract.

## SQL profiles

| Profile | Connection URL | Verification |
| --- | --- | --- |
| SQLite | `sqlite:///dataset.sqlite` | Local reference fixture |
| PostgreSQL | `postgresql+psycopg://…` | GitHub Actions PostgreSQL service |
| MySQL/MariaDB | `mysql+pymysql://…` | GitHub Actions MySQL service |

Separate MySQL 8.4 and MariaDB 11.4 services test the shared MySQL/MariaDB
profile contract. This does not claim coverage for every server configuration.

## Export-loss policy

`capability_matrix()` and `openstatspec capabilities` expose the current
feature state. Before export, the adapter combines source and writer
diagnostics. An export with known loss fails unless the caller passes the
relevant diagnostic codes through `allow_loss` or repeats `--allow-loss CODE`
in the CLI.

The concrete pyspssio boundaries are documented in the [SAV profile](sav-profile.md).
The pinned OpenStatSpec fork preserves document text, file labels, legacy
compatible names, separate print/write formats, variable sets,
multiple-response sets, alignment, scalar attributes, and ordered
custom-attribute arrays. Non-UTF-8 output requires an explicit matching locale;
otherwise the strict export policy reports and blocks the loss. A caller
that supplies consent for an available loss code receives the
machine-readable loss report with the export result.

## Maintainer checks before tagging

1. Run `python -m pytest`.
2. Run the service-backed profile checks with configured PostgreSQL, MySQL, and
   MariaDB URLs, or confirm the GitHub Actions SQL-services job is green.
3. Build with `python -m build` and install the generated wheel in a clean
   environment.
4. Confirm `openstatspec capabilities` reflects the intended support boundary.
5. Review this document, the README, and CHANGELOG for accurate scope.
