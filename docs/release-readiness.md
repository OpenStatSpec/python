# 0.9.0 release readiness

This page records the expected release contract, not a publication event.
Creating a version tag remains a separate maintainer action.

## Minor release scope

Version 0.9.0, dated 2026-09-10, adds explicitly selected official SPSS Frontend
0.3 over unchanged Plan 0.1/0.2 and namespaces new Python schema-extension
output, as described in the [release notes](../CHANGELOG.md). The minor bump
reflects the optional frontend and old-reader incompatibility: upgrade consumers
before emitting the new Python IDs. Legacy plans retain their canonical JSON,
hashes and semantics; stored audits are not migrated. Recompiling schema-changing
syntax changes its plan hash. See [compatibility and migration](transformations.md#contract-ownership-and-legacy-compatibility).
There is no catalog migration, dependency change, new database support or
specification pin update. The required codec remains
`openstatspec-pyspssio==0.5.1.post2`.
The local 0.8.0/0.8.1 evidence below is historical, not verification of 0.9.0.
Re-run the gates below on the exact selected 0.9.0 release commit.

The release selects SAV/ZSAV 1.0 with the optional Database I/O Execution
Policy `openstatspec-database-io-v1`. Reads and exports, including failures,
write no database audit records; export results omit `operation_id` and return
diagnostics to the caller. Reads must not create missing SQLite files or apply
Dolt write-variable-count ceilings. This release does not claim implementation
of the optional Transformation Workflow 0.3 profile.

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
frontend conformance fixtures, including `tests/test_frontend_v03.py` with all
90 effective official Frontend 0.3 cases (35 declared plus inherited cases with
published overrides/supersessions) from exact specification commit
`864e84479f554b8ee250ffed44c4dfb963750d4a`. Then exercise the generic plan apply
API, SPSS compatibility apply path, and explicitly selected official 0.3 apply.
Python schema-extension and legacy acceptance tests are separate compatibility
evidence, not official Plan 0.3 conformance; no official Plan 0.3 exists.

The gate must prove that:

- official Frontend 0.3 requests preserve exact Plan 0.1/0.2 objects and hashes,
  source hashes, diagnostics and declared metadata across all 90 effective cases;
- strict request boundaries reject invalid input, default API/CLI support and
  rejections remain unchanged, and official 0.3 rejects Python schema extensions;
- explicit official 0.3 SQLite apply preserves data/metadata semantics, dataset
  and table identity, audit provenance and the no-copy/no-history boundary;
- new Python schema-extension IDs are emitted while legacy plans preserve their
  canonical JSON, hashes and supplied identifiers without rewriting audits;
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
- string comparisons and v0.2 string assignments remain fail-closed.

The built wheel must contain the generic openstatspec.transform modules and the
implemented openstatspec.frontends.spss package. Stata and SAS remain empty
source-tree placeholders and must expose no compiler, apply API, CLI choice,
capability claim, or implied support.

## Prior default Dolt write verification (before the 0.8.0 pin)

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

## 0.8.0 local verification

Using `../python/.venv/bin/python`, `PYTHONPATH=src:../pyspssio`, and
`OPENSTATSPEC_SPECIFICATION_DIR=/tmp/oss-python-spec-v050-5dSTna` (an archive
of exact `864e84479f554b8ee250ffed44c4dfb963750d4a`):

- `python -m pytest -ra`: **381 passed, 49 skipped**, with
  `OPENSTATSPEC_TEST_DOLT_ADMIN_URL=mysql+pymysql://root@127.0.0.1:13387/`
  exercising live Dolt 2.3.0 through disposable SELECT-only users.
- Both immutable CI images, exact Dolt 2.2.2/2.2.3, bootstrapped with the
  unchanged service user and separate global fixture admin on ports 13582/13583:
  `python -m pytest -m 'services and not candidate_evidence'`: **7 passed,
  41 skipped, 382 deselected per pin**; explicit
  `python -m pytest tests/test_read_only_export.py`: **33 passed per pin**.
- `python -m compileall -q src tests`, `git diff --check`, wheel/sdist build,
  and `python -m twine check /tmp/oss-python-v080-dist/*` passed.
- A clean wheel install resolved the required engine from PyPI. Isolated
  imports came only from the smoke environment's `site-packages`; installed
  capabilities reported adapter 0.8.0, the selected policy, released v0.5.0
  provenance, and exactly 2.2.2/2.2.3 for default Dolt writes.

Other database services were not configured locally. This is local evidence,
not a claim that the updated remote CI or release publication has completed.

## 0.8.1 local preparation verification

On Python 3.13.2 with SQLAlchemy 2.0.52 and the exact v0.5.0 specification
checkout, without configured database services:

- `python -m pytest -p no:cacheprovider -m 'not services'`: **414 passed,
  9 skipped, 53 deselected**. The release-ref guard tests also passed (**5**).
- `compileall`, `git diff --check`, wheel/sdist build and `twine check` passed.
- A clean wheel install outside the checkout resolved the required engine
  `openstatspec-pyspssio==0.5.1.post2` from PyPI. Installed capabilities reported
  0.8.1, released specification v0.5.0 at the expected commit, and the selected
  database I/O policy. Core, SPSS frontend and legacy compiler re-exports loaded
  from `site-packages` without a source-path override.

This is local candidate evidence, not final release-commit service CI or
publication evidence. No v0.8.1 tag or registry upload was made by these checks.

## 0.9.0 local preparation verification

Using `../python-release-081/.venv/bin/python` (Python 3.13.2), with
`PYTHONPATH=src` and
`OPENSTATSPEC_SPECIFICATION_DIR=/tmp/openstatspec-alignment-spec` at exact
`864e84479f554b8ee250ffed44c4dfb963750d4a`:

- The updated capability version assertion failed first on 0.8.1, then passed
  on 0.9.0. Focused capability, release-ref and `test_frontend_v03.py` checks:
  **169 passed**, including all **90 effective** official cases.
- `python -m pytest -p no:cacheprovider -m 'not services' -ra`:
  **590 passed, 9 skipped, 55 deselected**. The skips require a live Dolt admin;
  no database services were configured for this run.
- `python -m compileall -q src tests .github/verify_release_ref.py` and
  `git diff --check` passed.
- `python -m build --outdir /tmp/openstatspec-v090-yhklqZ/dist .` and
  `python -m twine check /tmp/openstatspec-v090-yhklqZ/dist/*` passed for the
  0.9.0 wheel and sdist.
- A fresh `/tmp/openstatspec-v090-yhklqZ/venv` installed that wheel with
  dependencies from PyPI, including unchanged `openstatspec-pyspssio==0.5.1.post2`.
  From `/tmp` with `PYTHONPATH` unset, CLI/API capabilities agreed on 0.9.0,
  released v0.5.0 at the exact pin, and the database I/O policy. Core, frontend,
  legacy compiler re-exports and codec imports came from `site-packages`.
  An explicit official 0.3 comment/open-range RECODE request compiled to Plan
  0.1 without a database connection.

This is local preparation evidence, not final release-commit service CI or
publication evidence. No push, PR, tag or publication was performed.

## Maintainer release checklist

1. Publish the pinned `openstatspec-pyspssio==0.5.1.post2` engine distribution
   first and confirm that a clean environment can download it from PyPI. The
   main package has no fallback SPSS engine.
2. Run `python -m pytest -m "not services"`, including the 90 effective official
   Frontend 0.3 cases in `tests/test_frontend_v03.py` and existing canonical-plan,
   frontend, legacy compatibility and in-place suites.
3. Confirm the GitHub Actions matrix is green for exact PostgreSQL 17.10/18.4,
   MySQL 8.4.11/9.7.2, MariaDB 11.4.12/11.8.8/12.3.2, and exact Dolt
   2.2.2/2.2.3 service evidence from the immutable image pins in CI. The Dolt
   jobs must perform default import/validate/SAV+ZSAV export, in-place recode
   and labels with unchanged dataset/table counts and HEAD, injected data,
   catalog, and audit rollback checks, and failed-import cleanup. Release/CI
   owns this evidence, not per-user runtime declaration files. Candidate limit
   probes remain non-claiming and separate from these write gates.
   Each Dolt job must also run `python -m pytest tests/test_read_only_export.py`
   explicitly, outside the `services` marker filter. Bootstrap a separate
   `openstatspec_test_admin` on the isolated CI server with global database/user
   creation and grant privileges, and set `OPENSTATSPEC_TEST_DOLT_ADMIN_URL`.
   The fixture creates its own database and SELECT-only reader and removes
   both afterward; the normal service user remains unchanged.
4. Build with `python -m build` and install the generated wheel in a clean
   environment, resolving `openstatspec-pyspssio==0.5.1.post2` from PyPI.
   Run the installed CLI outside the source checkout with `PYTHONPATH` unset;
   verify adapter version `0.9.0`, the selected database I/O policy, public and
   legacy imports, and a simple explicitly selected official 0.3 compilation
   without a database connection.
5. Confirm `openstatspec capabilities` reflects the intended support boundary.
6. Confirm CI, release fixtures, and capabilities use the published OpenStatSpec
   specification `v0.5.0` at exact commit
   `864e84479f554b8ee250ffed44c4dfb963750d4a`, publish
   `specification_status=released`, and set `specification_release` to `v0.5.0`.
   Use an exact checkout via `OPENSTATSPEC_SPECIFICATION_DIR` for local tests.
7. Review this document, the README, and CHANGELOG for accurate scope. Finalize
   the 0.9.0 changelog date as 2026-09-10 before selecting the final release commit.
8. Confirm the exact release commit's CI matrix and package smoke passed, and
   verify the protected `pypi` environment and Trusted Publishing setup before
   pushing a new annotated/protected `v0.9.0` tag. A `v*` tag push starts the
   publishing workflow; it is not a preparation-only check. Do not move an
   existing tag. The workflow also rebuilds the unchanged specification companion
   package 0.1.0 at its existing pin and uses `skip-existing`; it does not release
   the newer specification validator changes.
9. After publication, verify the tag resolves to the intended commit, the
   tag-triggered workflow succeeded, and PyPI installs `openstatspec==0.9.0` in a
   clean environment. Record the tag, CI and registry evidence before claiming
   the release is published.

The tag-triggered release workflow repeats the non-service test suite, builds
the distributions, and installs the wheel with the exact required SPSS engine
in a clean environment before it can reach the protected `pypi` environment.
It fails closed if that exact engine version is not already downloadable from
PyPI, preventing publication of an uninstallable OpenStatSpec release.
