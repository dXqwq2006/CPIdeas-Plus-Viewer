import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

import cpideas_plus_viewer.cli as viewer_cli
import cpideas_plus_viewer.server as viewer_server
from cpideas_plus_viewer.server import (
    PRODUCT_BUNDLE_SCHEMA,
    UIError,
    build_run_detail,
    create_ui_server,
    read_run_file,
    render_markdown_html,
    scan_runs,
)


class PublicViewerTest(unittest.TestCase):
    def test_viewer_modules_do_not_import_private_package(self):
        for module in (viewer_server, viewer_cli):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("from cpideas_plus", source)
            self.assertNotIn("import cpideas_plus", source)

    def test_cli_starts_read_only_viewer(self):
        calls: list[dict[str, object]] = []
        original = viewer_cli.serve_ui

        def fake_serve_ui(**kwargs: object) -> None:
            calls.append(kwargs)

        try:
            viewer_cli.serve_ui = fake_serve_ui
            code = viewer_cli.main(
                ["--runs-dir", "imported/demo", "--host", "0.0.0.0", "--port", "9876"]
            )
        finally:
            viewer_cli.serve_ui = original

        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["runs_dir"], Path("imported/demo"))
        self.assertEqual(calls[0]["host"], "0.0.0.0")
        self.assertEqual(calls[0]["port"], 9876)
        self.assertFalse(calls[0]["actions_enabled"])
        self.assertFalse(calls[0]["internal_files_enabled"])

    def test_scan_and_detail_for_product_batch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            batch = root / "customer-task"
            _write_product_batch(batch)

            summaries = {item["name"]: item for item in scan_runs(root)}
            detail = build_run_detail(root, "customer-task/0001_demo")

            self.assertEqual(summaries["customer-task"]["kind"], "batch")
            self.assertEqual(summaries["customer-task/0001_demo"]["kind"], "run")
            self.assertEqual(summaries["customer-task/0001_demo"]["parent"], "customer-task")
            self.assertEqual(detail["statement"]["title"], "Public Viewer Demo")
            self.assertIn("package/solutions/main.cpp", {file["path"] for file in detail["files"]})

    def test_scan_and_detail_accept_single_run_as_runs_dir(self):
        with TemporaryDirectory() as directory:
            run_dir = Path(directory) / "product_single"
            _write_product_run(run_dir)

            summaries = scan_runs(run_dir)
            detail = build_run_detail(run_dir, "product_single")

            self.assertEqual([item["name"] for item in summaries], ["product_single"])
            self.assertEqual(summaries[0]["display_name"], "product_single")
            self.assertEqual(detail["name"], "product_single")
            self.assertEqual(detail["statement"]["title"], "Public Viewer Demo")

    def test_default_server_is_read_only_and_hides_internal_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            run_dir = root / "demo"
            _write_product_run(run_dir)
            _write_text(run_dir / "prompts" / "solution_probe_1.md", "private prompt\n")
            _write_text(run_dir / "ai_outputs" / "solution_probe_1.json", "{}\n")
            server = create_ui_server(root, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                base = f"http://{host}:{port}"
                capabilities = _get_json(f"{base}/api/capabilities")
                detail = _get_json(f"{base}/api/runs/demo")
                preview_error = _post_expect_error(f"{base}/api/runs/demo/actions/preview")
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual(capabilities["supported_bundle_schema"], PRODUCT_BUNDLE_SCHEMA)
            self.assertFalse(capabilities["actions_enabled"])
            self.assertFalse(capabilities["submission_enabled"])
            self.assertFalse(capabilities["internal_files_enabled"])
            self.assertEqual(capabilities["mode"], "viewer")
            self.assertEqual(preview_error[0], 403)
            self.assertNotIn(
                "prompts/solution_probe_1.md",
                {file["path"] for file in detail["files"]},
            )
            self.assertEqual(detail["jobs"], [])
            self.assertEqual(detail["submissions"], [])

            with self.assertRaises(UIError):
                read_run_file(
                    root,
                    "demo",
                    "prompts/solution_probe_1.md",
                    include_internal_files=False,
                )

    def test_markdown_renderer_sanitizes_html(self):
        html = render_markdown_html(
            "# Title\n\n<script>alert(1)</script>\n\n| A | B |\n|---|---|\n| `x` | **y** |\n"
        )

        self.assertIn("<h1>Title</h1>", html)
        self.assertIn("<table>", html)
        self.assertIn("<code>x</code>", html)
        self.assertNotIn("<script>", html)


def _write_product_batch(batch_dir: Path) -> None:
    batch_dir.mkdir(parents=True)
    (batch_dir / "batch_index.json").write_text(
        json.dumps(
            {
                "schema_version": "cpideas.product_batch.v1",
                "export_mode": "product",
                "summary": {"total": 1, "succeeded": 1, "skipped": 0, "failed": 0},
                "entries": [{"ordinal": 1, "run_dir": "0001_demo", "status": "exported"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_product_run(batch_dir / "0001_demo")


def _write_product_run(run_dir: Path) -> None:
    run_dir.mkdir(parents=True)
    run_json = {
        "schema_version": "cpideas.product_run.v1",
        "export_mode": "product",
        "seed": {
            "id": "public-viewer-demo",
            "title": "Public Viewer Demo",
            "statement": "Solve the public viewer demo.",
            "source": "test-fixture",
            "metadata": {},
        },
        "formal_statement": {
            "title": "Public Viewer Demo",
            "statement": "Solve the public viewer demo.",
            "input_format": "The first line contains T.",
            "output_format": "Print one answer per case.",
            "constraints": ["1 <= T <= 3"],
            "notes": [],
        },
        "idea": {
            "title": "Public Viewer Demo",
            "statement": "Solve the public viewer demo.",
            "input_format": "The first line contains T.",
            "output_format": "Print one answer per case.",
            "constraints": ["1 <= T <= 3"],
            "samples": [],
            "expected_solution": "Use direct simulation.",
            "target_difficulty": "demo",
        },
        "verification_plan": {},
        "artifacts": {},
        "polygon": {
            "name": "public-viewer-demo",
            "time_limit_ms": 2000,
            "memory_limit_mb": 1024,
            "standard": "cpp17",
        },
        "native_package_path": "package",
    }
    package_json = {
        "schema_version": "cpideas.package.v1",
        "id": "public-viewer-demo",
        "title": "Public Viewer Demo",
        "time_limit_ms": 2000,
        "memory_limit_mb": 1024,
        "language": "cpp17",
        "statements": {"default": "statements/statement.md"},
    }
    _write_text(run_dir / "run.json", json.dumps(run_json, indent=2) + "\n")
    _write_text(run_dir / "package" / "config" / "package.json", json.dumps(package_json, indent=2) + "\n")
    _write_text(run_dir / "package" / "statements" / "statement.md", "# Public Viewer Demo\n")
    _write_text(run_dir / "package" / "solutions" / "main.cpp", "int main(){return 0;}\n")
    _write_text(run_dir / "package" / "generator" / "generator.cpp", "int main(){return 0;}\n")
    _write_text(run_dir / "package" / "generator_script" / "generate.json", "{}\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _get_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_expect_error(url: str) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    raise AssertionError("POST unexpectedly succeeded")


if __name__ == "__main__":
    unittest.main()
