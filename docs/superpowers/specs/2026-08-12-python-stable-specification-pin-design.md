# Python 0.7.1 stable specification pin

## Goal

Publish the Python adapter's first patch release after Python adapter `v0.7.0`
with its capability declarations and conformance fixtures bound to the
published OpenStatSpec specification `v0.3.0`.

## Scope

- Bump the Python distribution from `0.7.0` to `0.7.1`.
- Replace the release-candidate specification identity with:
  - commit `cd8f198c68b849eb8ed018a894670a0904c2181d`;
  - status `stable`;
  - release `v0.3.0`.
- Update unit expectations, CI fixture checkouts, release-readiness guidance,
  and the changelog so every release surface carries the same identity.
- Keep the required SPSS engine pin, specification companion package pin,
  database version matrix, and runtime behavior unchanged.
- Separately update the specification repository roadmap to mark the completed
  `v0.3.0` release gates and current public release.

## Release and verification

The Python change is reviewed through a pull request. Before tagging `v0.7.1`,
run the non-service suite, package build and wheel smoke test, and the full
GitHub CI matrix. The annotated `v0.7.1` tag must target the merged release
commit; its tag-context workflow must pass before the existing trusted-PyPI
workflow publishes the package artifacts.

## Non-goals

This work does not add transformation behavior, change SQL dialect support,
modify the normative specification, or publish a new specification package
version.
