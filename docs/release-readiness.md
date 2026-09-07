# 0.7.1 release readiness

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
| Dolt writes | `mysql+pymysql://…` | Exactly 2.2.2 and 2.2.3 | 2.2.2 and 2.2.3 |

Separate service matrices test the shared MySQL/MariaDB profile contract while
retaining distinct active-server identities and version claims. Dolt is an
independent core profile with a packaged exact-version write policy; normal
callers supply no conformance files. An explicit external declaration source
remains a strict override. Read-only validation/export has no write-version gate.
The optional Transformation Workflow remains SQLite-only. This does not claim
coverage for every server configuration or proven Dolt native limit ceilings.
Dolt capabilities report adapter safety budgets and active packet constraints;
full boundary conformance remains pending.
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

## Transformation release gates

The canonical transformation core and SPSS syntax frontend are separate public
surfaces. A release must run the specification-owned canonical-plan and SPSS
frontend conformance fixtures, then exercise both the generic plan apply API
and the SPSS compatibility apply path.

The gate must prove that:

- a TransformationPlan object and its strict JSON mapping produce the same
  plan hash and in-place result;
- the exact bounded `COMPUTE`/`IF` program compiles to all seven ordered
  operations without dropping `FORMATS`, `VARIABLE LEVEL`, or `EXECUTE`;
- bounded `STRING` declarations and `DELETE VARIABLES` operations preserve
  the resulting schema and physical-column identity on supported profiles;
- boolean data results match the equivalent expression and the target's label,
  0/1 value labels, `F1.0` print/write format, and nominal level exist in the
  normative catalog;
- injected schema, data, catalog, and audit failures leave no partial apply;
- compensation tracks only newly created targets and never drops or rewrites a
  pre-existing target;
- MySQL, MariaDB, and Dolt reject create-target plans before mutation; their
  service evidence covers assignment to a separately provisioned physical and
  catalog target without schema DDL;
- top-level SPSS compiler imports and legacy openstatspec.transform re-exports
  still load from an installed wheel;
- install-in-place-schema, apply-plan, and apply-spss execute their documented
  CLI workflows;
- invalid or unsupported plans fail before the first data or metadata mutation;
- successful applies retain the same dataset ID, physical schema/table
  identity, dataset count, and persistent physical data-table count;
- audit rows distinguish canonical plans from SPSS syntax, preserve correct
  source/plan hashes and frontend contract, and contain no copied data;
- no OpenStatSpec rollback, snapshot, staging, copy, derived-dataset, or
  parallel history artifacts are created; and
- Dolt checks expected branch, HEAD, and a clean working set; success changes
  neither HEAD nor branch and never commits or resets; state is rechecked after
  the dataset lock and success must leave an inspectable working-set diff;
  other supported SQL connections remain allowed; and
- string comparisons and v0.2 string assignments fail closed until exact

The built wheel must contain the generic openstatspec.transform modules and the
implemented openstatspec.frontends.spss package. Stata and SAS remain empty
source-tree placeholders and must expose no compiler, apply API, CLI choice,
capability claim, or implied support.

## Default Dolt write verification

Local release verification used the exact 2.2.2 and 2.2.3 image digests in
`.github/workflows/ci.yml`, isolated ports 13482/13483, and disposable `/tmp`
database directories. With `PYTHONPATH=src:../pyspssio`,
`OPENSTATSPEC_SPECIFICATION_DIR=/tmp/oss-php-spec-cd8f198`, and each matching
`OPENSTATSPEC_DOLT_URL` / `OPENSTATSPEC_EXPECTED_DOLT_VERSION`:

- `../python/.venv/bin/python -m pytest`: **366 passed, 49 skipped per pin**.
- CI selection `-m 'services and not candidate_evidence'`: **7 passed,
  41 skipped per pin**; other database services were not configured locally.
- Dolt 2.3.0 on port 13387: read-only export and unknown-version write rejection
  checks passed (**19 passed**) using disposable fixture databases/users.
- `python -m compileall -q src tests` and `git diff --check` passed.

Live failures exposed and now cover two previously dormant write blockers:
owned table additions being misclassified as unrelated diffs, and Dolt retaining
`@@autocommit=1` despite the driver's setting. Writes now explicitly begin their
transaction. The candidate storage/identifier/column smoke probe also passed on
both pins; it does **not** establish full value/row/statement limit conformance.

## Maintainer checks before tagging

1. Publish the pinned `openstatspec-pyspssio==0.5.1.post2` engine distribution
   first and confirm that a clean environment can download it from PyPI. The
   main package has no fallback SPSS engine.
2. Run `python -m pytest -m "not services"`.
3. Confirm the GitHub Actions matrix is green for exact PostgreSQL 17.10/18.4,
   MySQL 8.4.11/9.7.2, MariaDB 11.4.12/11.8.8/12.3.2, and exact Dolt
   2.2.2/2.2.3 service evidence from the immutable image pins in CI. The Dolt
   jobs must perform default import/validate/SAV+ZSAV export, in-place recode
   and labels with unchanged dataset/table counts and HEAD, injected data,
   catalog, and audit rollback checks, and failed-import cleanup. Release/CI
   owns this evidence, not per-user runtime declaration files. Candidate limit
   probes remain non-claiming and separate from these write gates.
4. Build with `python -m build` and install the generated wheel in a clean
   environment.
5. Confirm `openstatspec capabilities` reflects the intended support boundary.
6. Confirm CI, release fixtures, and capabilities use the published OpenStatSpec
   specification `v0.3.0` at exact commit
   `cd8f198c68b849eb8ed018a894670a0904c2181d`, publish
   `specification_status=stable`, and set `specification_release` to `v0.3.0`.
7. Review this document, the README, and CHANGELOG for accurate scope.

The tag-triggered release workflow repeats the non-service test suite, builds
the distributions, and installs the wheel with the exact required SPSS engine
in a clean environment before it can reach the protected `pypi` environment.
It fails closed if that exact engine version is not already downloadable from
PyPI, preventing publication of an uninstallable OpenStatSpec release.
