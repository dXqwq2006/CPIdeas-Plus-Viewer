# CPIdeas Plus Viewer

[简体中文](README.zh-CN.md)

CPIdeas Plus Viewer is a read-only local web viewer for delivered CPIdeas Plus product results. It is intended for reviewers, customers, and stakeholders who need to browse generated problem packages without accessing the private AI generation pipeline.

## What It Supports

- Browse one delivered problem result or a batch of problem results.
- Read the problem statement, input/output format, constraints, samples, and expected solution notes.
- View localized statement/review/preview files when they are included in the result.
- Inspect verification summaries when `verification/cpideas_report.json` is included.
- Inspect text files inside the delivered `package/`, such as statements, solutions, generators, validators, checkers, and package config.
- Run entirely on localhost; no external service or network backend is required after dependencies are installed.

## What It Does Not Include

- No AI generation prompts or raw model outputs.
- No checkpoints, repair traces, batch runners, LiveCodeBench importers, or private workbench actions.
- No preview/verify/resume/submit buttons in the standalone public viewer.
- No server-side mutation of product results.

The standalone viewer is intentionally read-only. Its capability endpoint reports:

```json
{
  "actions_enabled": false,
  "submission_enabled": false,
  "internal_files_enabled": false,
  "mode": "viewer"
}
```

## Product Result Layout

A single delivered problem result usually looks like:

```text
product_single/
  run.json
  package/
    config/package.json
    statements/statement.md
    solutions/main.cpp
    generator/generator.cpp
    generator_script/generate.json
```

A delivered batch contains `batch_index.json` plus one subdirectory per problem.

See `docs/PRODUCT_RESULT.md` for the full contract.

## Run With uv

```bash
uv sync
uv run cpideas-viewer --runs-dir examples/product_single --host 127.0.0.1 --port 8765
```

Open the printed URL in your browser.

For the batch-style example:

```bash
uv run cpideas-viewer --runs-dir examples/product_batch
```

`--runs-dir` may point either to a single run directory containing `run.json`, or to a parent directory containing multiple runs/batches.

`uv sync` may create an environment without `pip`; use `uv run ...` or `uv pip install -e .` instead of `uv run python -m pip install -e .`.

## Run With pip

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cpideas-viewer --runs-dir examples/product_single --host 127.0.0.1 --port 8765
```

## Customer Delivery Checklist

- Deliver only product results, not private generation artifacts.
- Confirm each result contains `run.json` and `package/config/package.json`.
- Confirm the viewer opens the result locally before delivery.
- If sharing a batch, include `batch_index.json` at the batch root.
- If localized Markdown is included, keep filenames such as `review.zh-CN.md` and `preview.zh-CN.md` next to the English files.

## Development Checks

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall src/cpideas_plus_viewer
git diff --check
```

## License

MIT. See `LICENSE`.
