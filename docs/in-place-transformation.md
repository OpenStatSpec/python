# In-place SPSS-like transformation

`openstatspec.apply_spss_in_place` is the public execution path for the
SPSS-like frontend. It accepts `RECODE`, `VARIABLE LABELS`, and `VALUE LABELS`,
binds them to the existing core dataset, and applies the canonical plan to that
same SQL wide table and metadata catalog.

The caller supplies a supported SQL URL, the existing normative `dataset_id`,
and a non-empty actor identity. For Dolt, the caller also supplies the expected
active branch and current `HEAD` hash.

Install the compact audit relation once with
`openstatspec.install_in_place_transformation_schema(database_url=...)` before
the first apply. Apply never creates schema-management objects itself.

The adapter uses the engine's ordinary transaction behavior. On Dolt it first
checks branch and HEAD and requires `dolt_status` to be empty. Existing-target
recodes are one direct `UPDATE`. SQLite and PostgreSQL can add a new numeric
`INTO` target to the same table. MySQL, MariaDB, and Dolt require that target
column and variable metadata to exist before apply because their DDL can commit
independently of the following data and metadata changes. Label commands
update/replace the same dataset's normative and compatibility metadata rows.

One compact `transformation_apply` row records operation identity, canonical
plan/source hashes, actor, status, timestamps, and the observed Dolt branch and
HEAD. It contains no row values and points to no copied table.

The adapter does not create `derived_dataset` rows, persistent output tables,
full-table copies, staging datasets, snapshots, rollback tables, retirement
records, or a recovery/version catalog. It does not call `DOLT_COMMIT`, change
branches, merge, reset, or tag. After success, the caller reviews `dolt diff`
and independently decides whether to commit or restore the working set.

The local SQLite tests exercise the public mutation path and assert that
dataset/table counts and identities do not change. Live PostgreSQL/MySQL/MariaDB and
exact-version Dolt service evidence remains required before release execution
claims for those engines.

```text
openstatspec apply-spss --database-url mysql+pymysql://user:password@host/database --dataset-id ... --actor agent@example.org --expected-branch feature/recode --expected-head ... --syntax-file transform.sps
```
