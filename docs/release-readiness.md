# Next release readiness

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

| Profile | Connection URL | Claimed versions | Exact CI evidence |
| --- | --- | --- | --- |
| SQLite | `sqlite:///dataset.sqlite` | `>=3.24.0,<4.0.0` | Active Python 3.11–3.14 runner version |
| PostgreSQL | `postgresql+psycopg://…` | 17.x and 18.x | 17.10 and 18.4 |
| MySQL | `mysql+pymysql://…` | 8.4.x and 9.7.x | 8.4.11 and 9.7.2 |
| MariaDB | `mysql+pymysql://…` | 11.4.x, 11.8.x, and 12.3.x | 11.4.12, 11.8.8, and 12.3.2 |
| Dolt | `mysql+pymysql://…` | 2.2.x with `>=2.2.2,<2.3.0` | 2.2.2 and 2.2.3 |

Separate service matrices test the shared MySQL/MariaDB profile contract while
retaining distinct active-server identities and version claims. Dolt is an
independent core profile that accepts only canonical stable versions in
`>=2.2.2,<2.3.0`; the optional Transformation Workflow remains SQLite-only. This does not
claim coverage for every server configuration. The machine-readable capability
declaration reports both the theoretical profile boundaries and effective
limits observed from the active connection.
Every server service job compares the normalized live product version with its
exact matrix entry, so a moved or mismatched image cannot substantiate the CI
declaration. SQLite's optional Transformation Workflow retains its narrower
`>=3.35.0,<4.0.0` live preflight alongside the core `>=3.24.0,<4.0.0` policy.
Microsoft SQL Server remains unsupported and must not appear in runtime
capabilities; future implementation requirements are documented in the
specification's
[MSSQL dialect roadmap](https://github.com/OpenStatSpec/specification/blob/main/docs/mssql-dialect-roadmap.md).


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
2. Run `python -m pytest -m "not services"`.
3. Confirm the GitHub Actions matrix is green for exact PostgreSQL 17.10/18.4,
   MySQL 8.4.11/9.7.2, MariaDB 11.4.12/11.8.8/12.3.2, and exact Dolt
   2.2.2/2.2.3 service evidence from the immutable image pins in CI.
4. Build with `python -m build` and install the generated wheel in a clean
   environment.
5. Confirm `openstatspec capabilities` reflects the intended support boundary.
6. Confirm the release tag matches the package version and that CI, release
   fixtures, and capabilities use OpenStatSpec specification release `v0.2.0`
   at exact commit `79339ec3d8f8aa81789b7e85f6b8afa6f1374e50`.
7. Review this document, the README, and CHANGELOG for accurate scope.

The tag-triggered release workflow repeats the non-service test suite, builds
the distributions, and installs the wheel with the exact required SPSS engine
in a clean environment before it can reach the protected `pypi` environment.
It fails closed if that exact engine version is not already downloadable from
PyPI, preventing publication of an uninstallable OpenStatSpec release.
