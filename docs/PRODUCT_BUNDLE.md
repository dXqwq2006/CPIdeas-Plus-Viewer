# Product Viewer Bundle Contract

This document defines the artifact shape consumed by the read-only CPIdeas Plus viewer. It is the seam between the private AI/workbench repository and a public viewer package.

## Schema

Current viewer bundle schema:

```text
cpideas-plus-product-v1
```

The HTTP capability endpoint returns this value as `supported_bundle_schema`.

## Directory Shapes

A single product run bundle contains:

```text
<bundle>/
  run.json
  package/
    config/package.json
    statements/statement.md
    ... native CPIdeas package files ...
```

A product batch bundle contains:

```text
<bundle>/
  batch_index.json
  <run-dir>/
    run.json
    package/
      config/package.json
      statements/statement.md
      ... native CPIdeas package files ...
```

`cpideas export-run --mode product` writes this shape. `cpideas import-run` unpacks it into any viewer runs directory.

## Required Files

`run.json` must be a JSON object with:

- `schema_version`: `cpideas.product_run.v1`.
- `export_mode`: `product`.
- `seed`: lightweight source metadata for display.
- `formal_statement`: statement fields compatible with existing `PipelineRun` readers.
- `idea`: product statement payload used by the viewer.
- `native_package_path`: usually `package`.

`package/config/package.json` must follow `docs/CPIDEAS_PACKAGE.md`. The viewer uses it to find statement files and display package metadata.

`package/statements/statement.md` or the `statements.default` path from package config must exist for product statement fallback.

## Optional Files

The viewer tolerates missing optional review and verification files. If present, these files are displayed read-only:

- `review.md` and `review.<locale>.md`.
- `preview.md` and `preview.<locale>.md`.
- `verification/cpideas_report.json`.
- `verification/cpideas_generate_report.json`.
- text files under `package/`.

## Excluded Internal Files

Product bundles must not require or expose private generation artifacts:

- `prompts/`.
- `ai_outputs/`.
- `checkpoints/`.
- `repairs/`.
- `ui_jobs/`.
- `submissions/`.

The read-only viewer hides and rejects these paths even if they are present.

## Viewer Capabilities

Public viewer mode starts with no action backend. Its capability payload is:

```json
{
  "actions_enabled": false,
  "submission_enabled": false,
  "internal_files_enabled": false,
  "mode": "viewer",
  "supported_bundle_schema": "cpideas-plus-product-v1"
}
```

Private workbench mode may inject an action backend. In that mode the same UI can expose preview, verify, resume, mock-demo, and submit actions while also showing internal debug files.

## Compatibility Rules

- Additive fields are allowed in `run.json`, `batch_index.json`, and package config.
- Existing fields should keep their meaning for the lifetime of `cpideas-plus-product-v1`.
- Schema-breaking changes require a new `supported_bundle_schema` value and contract tests.
- The viewer should ignore unknown fields and fail clearly on unsafe paths or invalid JSON.
