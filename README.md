# CPIdeas Plus Viewer

Read-only local web viewer for CPIdeas Plus product bundles.

This repository contains only the public viewer surface. It does not include AI generation, prompts, checkpoints, batch runners, LiveCodeBench importers, verifier repair loops, or private workbench actions.

## Run From Source With uv

```bash
uv sync
uv run cpideas-viewer --runs-dir examples/product_single --host 127.0.0.1 --port 8765
```

`uv sync` may create an environment without `pip`; use `uv run ...` or `uv pip install -e .` instead of `uv run python -m pip install -e .`.

## Run After pip Install

If you prefer standard pip, create a venv with pip first:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cpideas-viewer --runs-dir examples/product_single --host 127.0.0.1 --port 8765
```

Then open the printed URL.

For a batch-style fixture:

```bash
uv run cpideas-viewer --runs-dir examples
```

## Bundle Contract

The viewer supports product bundle schema:

```text
cpideas-plus-product-v1
```

See `docs/PRODUCT_BUNDLE.md` for the file layout and compatibility rules.

## Capabilities

The standalone viewer is intentionally read-only:

- `actions_enabled=false`
- `submission_enabled=false`
- `internal_files_enabled=false`
- `mode=viewer`

Private CPIdeas Plus workbench integrations can inject their own action backend, but that backend is not part of this public package.

## Development

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall src/cpideas_plus_viewer
git diff --check
```

## License

MIT. See `LICENSE`.
