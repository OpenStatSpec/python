# Python 0.7.1 Stable Specification Pin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release Python adapter `0.7.1` with stable, exact provenance for specification `v0.3.0`.

**Architecture:** Keep the provenance values centralized in `src/openstatspec/sql/capabilities.py`; tests and capability declarations consume those values. CI and release-readiness documentation pin the same immutable specification commit, while the package version changes only in packaging metadata.

**Tech Stack:** Python 3.11–3.14, pytest, GitHub Actions, `python -m build`, Twine, PyPI Trusted Publishing.

## Global Constraints

- Specification commit: `cd8f198c68b849eb8ed018a894670a0904c2181d`.
- Specification status: `stable`.
- Specification release: `v0.3.0`.
- Package version: `0.7.1`.
- Keep the SPSS engine pin, companion package pin, SQL profile matrix, and runtime behavior unchanged.
- Do not publish or change the normative specification package in this task.

---

### Task 1: Update provenance contract tests first

**Files:**
- Modify: `tests/test_cli.py:44-48`
- Modify: `tests/test_sql_profiles.py:32-40`

**Interfaces:**
- Consumes: public `openstatspec.capability_matrix()` and SQL profile declarations.
- Produces: executable expectations for stable `v0.3.0` provenance.

- [ ] **Step 1: Change the assertions to the stable identity**

Replace the candidate expectations with:

```python
assert matrix["specification_status"] == "stable"
assert matrix["specification_release"] == "v0.3.0"
assert matrix["specification_commit"] == "cd8f198c68b849eb8ed018a894670a0904c2181d"
```

Apply the same three values to each profile declaration assertion.

- [ ] **Step 2: Run the focused tests and confirm the expected red state**

Run:

```bash
python -m pytest -q tests/test_cli.py::test_capability_matrix_is_public_and_cli_matches_engine_boundary tests/test_sql_profiles.py::test_profile_declarations_publish_stable_specification_provenance
```

Expected: both tests fail because production constants still expose the old release-candidate identity.

- [ ] **Step 3: Commit the failing-test update**

```bash
git add tests/test_cli.py tests/test_sql_profiles.py
git commit -m "test: expect stable specification v0.3.0"
```

### Task 2: Bind production provenance and release metadata

**Files:**
- Modify: `src/openstatspec/sql/capabilities.py:20-23`
- Modify: `pyproject.toml:7`
- Modify: `.github/workflows/ci.yml:23`
- Modify: `.github/workflows/release.yml:51`
- Modify: `docs/release-readiness.md:1,116-123`
- Modify: `CHANGELOG.md` after the `Unreleased` section

**Interfaces:**
- Consumes: the exact specification commit from the published `v0.3.0` tag.
- Produces: capability output and release automation identifying `stable`, `v0.3.0`, and `cd8f198c...`.

- [ ] **Step 1: Update the centralized capability constants**

Set the declarations to:

```python
SPECIFICATION_COMMIT = "cd8f198c68b849eb8ed018a894670a0904c2181d"
SPECIFICATION_STATUS = "stable"
SPECIFICATION_RELEASE: str | None = "v0.3.0"
```

- [ ] **Step 2: Bump the package and CI fixture pin**

Set `project.version` in `pyproject.toml` to `0.7.1`. Replace the old specification checkout ref in both workflows with the exact commit `cd8f198c68b849eb8ed018a894670a0904c2181d`; leave the companion package ref unchanged.

- [ ] **Step 3: Update release documentation and changelog**

Rename the readiness heading to `0.7.1 release readiness`, change its specification checklist to the stable commit/release identity, and add a dated `0.7.1` entry explaining that the adapter now pins published specification `v0.3.0`.

- [ ] **Step 4: Run focused tests and confirm green**

Run the focused command from Task 1. Expected: both tests pass.

- [ ] **Step 5: Commit the implementation and metadata**

```bash
git add src/openstatspec/sql/capabilities.py pyproject.toml .github/workflows/ci.yml .github/workflows/release.yml docs/release-readiness.md CHANGELOG.md
git commit -m "release: pin Python adapter to specification v0.3.0"
```

### Task 3: Verify the complete Python release candidate

**Files:**
- Verify: all tracked files in the Python repository.

- [ ] **Step 1: Check the diff and stale current references**

Run:

```bash
git diff --check
rg -n 'f2fdf687d8cb32b944ca55a3e9e7215ffc603019|specification_status.*release_candidate|specification_release.*None|version = "0\.7\.0"' src tests .github docs pyproject.toml
```

Expected: no stale current references; historical changelog entries may retain their original release evidence.

- [ ] **Step 2: Run the non-service test suite**

```bash
python -m pytest -m "not services"
```

Expected: zero failures.

- [ ] **Step 3: Build and smoke-test the package**

```bash
rm -rf /tmp/openstatspec-v071-dist /tmp/openstatspec-v071-smoke
python -m build --outdir /tmp/openstatspec-v071-dist
python -m twine check /tmp/openstatspec-v071-dist/*
python -m venv /tmp/openstatspec-v071-smoke
/tmp/openstatspec-v071-smoke/bin/python -m pip install ./openstatspec-pyspssio
/tmp/openstatspec-v071-smoke/bin/python -m pip install /tmp/openstatspec-v071-dist/openstatspec-0.7.1-*.whl
/tmp/openstatspec-v071-smoke/bin/openstatspec capabilities
```

Expected: build and Twine checks pass, and capabilities reports `stable`, `v0.3.0`, and the exact commit.

### Task 4: Review, publish, and release

- [ ] **Step 1: Run the local review loop and fix every actionable finding**

Review the complete branch diff against `origin/main`; repeat the tests after each fix until no actionable findings remain.

- [ ] **Step 2: Push the branch and open a draft PR**

Push `agent/stable-spec-v071` to `OpenStatSpec/python`, open a PR targeting `main`, mark it ready for review, and add `@codex review`.

- [ ] **Step 3: Iterate on GitHub review and CI**

Inspect every Codex review finding and failing check, fix actionable issues locally, push, and repeat until the review and required checks are clean.

- [ ] **Step 4: After merge, create and push annotated tag `v0.7.1`**

Verify the tag targets the merge commit and wait for tag-context CI to pass before relying on the existing PyPI workflow.
