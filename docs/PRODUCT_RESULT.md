# Product Result Contract

This document defines the artifact shape consumed by the read-only CPIdeas Plus Viewer. It is the seam between the private AI/workbench repository and the public viewer package.

## Directory Shapes

A single product result contains:

```text
<product-result>/
  run.json
  package/
    config/package.json
    statements/statement.md
    ... native CPIdeas package files ...
```

A product batch result contains:

```text
<product-result>/
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
- `formal_statement`: statement fields compatible with existing run readers.
- `idea`: product statement payload used by the viewer.
- `native_package_path`: usually `package`.

`batch_index.json`, when present, uses `schema_version: cpideas.product_batch.v1` and lists product run directories.

`package/config/package.json` describes package metadata. The viewer uses it to find statement files and display package metadata.

`package/statements/statement.md` or the `statements.default` path from package config should exist for product statement fallback.

## Optional Files

The viewer tolerates missing optional review and verification files. If present, these files are displayed read-only:

- `review.md` and `review.<locale>.md`.
- `preview.md` and `preview.<locale>.md`.
- `verification/cpideas_report.json`.
- `verification/cpideas_generate_report.json`.
- text files under `package/`.

## Excluded Internal Files

Product results must not require or expose private generation artifacts:

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
  "mode": "viewer"
}
```

Private workbench mode may inject an action backend. In that mode the same UI can expose preview, verify, resume, mock-demo, and submit actions while also showing internal debug files.

## Compatibility Rules

- Additive fields are allowed in `run.json`, `batch_index.json`, and package config.
- Existing fields in `cpideas.product_run.v1` and `cpideas.product_batch.v1` should keep their meaning.
- Schema-breaking changes require new product run/batch schema values and contract tests.
- The viewer should ignore unknown fields and fail clearly on unsafe paths or invalid JSON.
