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
| SQLite | `sqlite:///dataset.sqlite` | Python 3.11–3.14 local reference jobs |
| PostgreSQL | `postgresql+psycopg://…` | PostgreSQL 17 and 18 service jobs |
| MySQL | `mysql+pymysql://…` | MySQL 8.4 and 9.7 service jobs |
| MariaDB | `mysql+pymysql://…` | MariaDB 11.4, 11.8, and 12.3 service jobs |

Separate service matrices test the shared MySQL/MariaDB profile contract while
retaining distinct active-server identities and version claims. This does not
claim coverage for every server configuration. The machine-readable capability
declaration reports both the theoretical profile boundaries and effective
limits observed from the active connection.

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

1. Publish the pinned `openstatspec-pyspssio==0.5.1.post2` engine distribution
   first and confirm that a clean environment can download it from PyPI. The
   main package has no fallback SPSS engine.
2. Run `python -m pytest`.
3. Confirm the GitHub Actions matrix is green for PostgreSQL 17/18, MySQL
   8.4/9.7, and MariaDB 11.4/11.8/12.3.
4. Build with `python -m build` and install the generated wheel in a clean
   environment.
5. Confirm `openstatspec capabilities` reflects the intended support boundary.
6. Review this document, the README, and CHANGELOG for accurate scope.

The tag-triggered release workflow repeats the non-service test suite, builds
the distributions, and installs the wheel with the exact required SPSS engine
in a clean environment before it can reach the protected `pypi` environment.
It fails closed if that exact engine version is not already downloadable from
PyPI, preventing publication of an uninstallable OpenStatSpec release.
