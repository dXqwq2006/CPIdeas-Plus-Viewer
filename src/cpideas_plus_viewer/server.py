from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

import bleach
import markdown as markdown_lib

JsonDict = dict[str, Any]

TEXT_SUFFIXES = {
    ".cpp",
    ".h",
    ".hpp",
    ".json",
    ".log",
    ".md",
    ".txt",
    ".in",
    ".ans",
}
MAX_TEXT_BYTES = 512 * 1024
LOG_TAIL_BYTES = 64 * 1024
MAX_SUBMISSION_BYTES = 256 * 1024
INTERNAL_VIEWER_DIRS = frozenset(
    {"prompts", "ai_outputs", "checkpoints", "repairs", "ui_jobs", "submissions"}
)
LOCALE_DISPLAY_NAMES = {
    "en": "English",
    "zh-CN": "简体中文",
    "zh-TW": "繁體中文",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "ru": "Русский",
}


class UIError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class JobConflict(UIError):
    def __init__(self, run_name: str) -> None:
        super().__init__(409, f"run {run_name!r} already has a running job")


def serve_ui(
    runs_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    action_backend: UIActionBackend | None = None,
    actions_enabled: bool | None = None,
    internal_files_enabled: bool | None = None,
) -> None:
    server = create_ui_server(
        runs_dir=runs_dir,
        host=host,
        port=port,
        action_backend=action_backend,
        actions_enabled=actions_enabled,
        internal_files_enabled=internal_files_enabled,
    )
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    print(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def create_ui_server(
    runs_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    action_backend: UIActionBackend | None = None,
    actions_enabled: bool | None = None,
    internal_files_enabled: bool | None = None,
) -> ThreadingHTTPServer:
    runs_root = runs_dir.resolve()
    runs_root.mkdir(parents=True, exist_ok=True)
    actions_available = (
        bool(action_backend)
        if actions_enabled is None
        else bool(actions_enabled and action_backend)
    )
    expose_internal_files = (
        actions_available if internal_files_enabled is None else internal_files_enabled
    )

    class Handler(CPIdeasUIHandler):
        pass

    Handler.runs_dir = runs_root
    Handler.action_backend = action_backend
    Handler.actions_enabled = actions_available
    Handler.internal_files_enabled = expose_internal_files

    return ThreadingHTTPServer((host, port), Handler)


# Directory names that live *inside* a run; if we descend into one of them we
# are inside an existing run and must stop discovering further runs along that
# path, otherwise the package/verification/etc. subdirectories would be
# mistaken for nested runs.
_RUN_INTERNAL_DIRS = frozenset(
    {
        "package",
        "verification",
        "prompts",
        "ai_outputs",
        "checkpoints",
        "repairs",
        ".git",
        "__pycache__",
    }
)
# How many directory levels below ``runs_dir`` we walk when discovering runs.
# Depth 1 = the historic single-level layout (``runs/<name>/run.json``).
# Depth 2 = batch layout (``runs/<batch>/<NNNN_seed_id>/run.json``).
# Depth 3 leaves headroom for a future ``runs/<group>/<batch>/<seed>/`` shape
# without code changes.
_RUN_SCAN_MAX_DEPTH = 3


def scan_runs(runs_dir: Path) -> list[JsonDict]:
    """Recursively enumerate every run directory under ``runs_dir``.

    A "run directory" is any directory containing either ``run.json`` (finished
    run) or a ``checkpoints/`` sub-directory (in-progress / interrupted run).
    The walker descends up to ``_RUN_SCAN_MAX_DEPTH`` levels and refuses to
    enter directories listed in ``_RUN_INTERNAL_DIRS`` so the contents of a run
    cannot be mistaken for nested runs. Once a directory is identified as a
    run we **also stop descending** into it, since per-seed run directories
    are themselves the leaves of the layout.

    Identifying batches is purely informational: we also emit a synthetic
    ``batch`` summary for any directory containing ``batch_index.json`` so the
    UI can group seeds under their parent in the sidebar. Batch summaries do
    not point at a runnable directory; the client recognises them via
    ``"kind": "batch"``.
    """
    root = runs_dir.resolve()
    if not root.exists():
        return []
    runs: list[JsonDict] = []
    if (root / "run.json").exists() or (root / "checkpoints").exists():
        return [_run_summary(root, root)]

    def walk(directory: Path, depth: int) -> None:
        if depth > _RUN_SCAN_MAX_DEPTH:
            return
        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or child.name in _RUN_INTERNAL_DIRS:
                continue
            run_json = child / "run.json"
            checkpoints = child / "checkpoints"
            if run_json.exists() or checkpoints.exists():
                # This directory is a run; emit its summary and do NOT recurse
                # so package/verification subdirs are never visited.
                runs.append(_run_summary(child, root))
                continue
            # Not itself a run; expose batch grouping if present and keep
            # descending.
            batch_index = child / "batch_index.json"
            if batch_index.exists():
                runs.append(_batch_summary(child, root))
            walk(child, depth + 1)

    walk(root, 1)
    runs.sort(
        key=lambda item: (item.get("updated_at") or 0, item.get("name") or ""),
        reverse=True,
    )
    return runs


def build_run_detail(
    runs_dir: Path, run_name: str, *, include_internal_files: bool = True
) -> JsonDict:
    root = runs_dir.resolve()
    run_dir = _run_dir(root, run_name)
    if not run_dir.exists():
        raise UIError(404, f"run {run_name!r} was not found")

    run_data = _read_json(run_dir / "run.json")
    verification = _read_json(run_dir / "verification" / "cpideas_report.json")
    generate_report = _read_json(
        run_dir / "verification" / "cpideas_generate_report.json"
    )
    review = _read_text(run_dir / "review.md", max_bytes=MAX_TEXT_BYTES)
    preview = _read_text(run_dir / "preview.md", max_bytes=MAX_TEXT_BYTES)
    run_log = _read_text_tail(run_dir / "run.log")

    # Use the relative path as the canonical name so the client's
    # selectedKey survives a refresh on batched runs.
    relative_name = _entry_name(run_dir, root)
    return {
        "name": relative_name,
        "display_name": run_dir.name,
        "path": _display_path(run_dir),
        "summary": _run_summary(run_dir, root),
        "statement": _statement_from_run(run_data, run_dir),
        "run": run_data,
        "human_review": _dict_or_empty(
            run_data.get("human_review") if run_data else None
        ),
        "verification": verification,
        "generate_report": generate_report,
        "election": _dict_or_empty(
            verification.get("election") if verification else None
        ),
        "seed_similarity": _dict_or_empty(
            (run_data.get("human_review") or {}).get("seed_similarity")
            if run_data
            else None
        ),
        "seed": _dict_or_empty(run_data.get("seed") if run_data else None),
        "dropped_tests": verification.get("dropped_tests", []) if verification else [],
        "bruteforce_votes": verification.get("bruteforce_votes", {})
        if verification
        else {},
        "review_markdown": review,
        "review_html": render_markdown_html(review) if review else "",
        "preview_markdown": preview,
        "preview_html": render_markdown_html(preview) if preview else "",
        "localized_review": _localized_markdown_map(run_dir, "review"),
        "localized_preview": _localized_markdown_map(run_dir, "preview"),
        "run_log_tail": run_log,
        "files": list_run_files(run_dir, include_internal_files=include_internal_files),
        "submission_templates": list_submission_templates(run_dir),
        "submissions": list_submissions(run_dir) if include_internal_files else [],
    }


def read_run_file(
    runs_dir: Path,
    run_name: str,
    relative_path: str,
    *,
    include_internal_files: bool = True,
) -> JsonDict:
    root = runs_dir.resolve()
    run_dir = _run_dir(root, run_name)
    if not include_internal_files and _is_internal_viewer_file(relative_path):
        raise UIError(403, "internal run file is not available in viewer mode")
    file_path = _safe_file_in_run(run_dir, relative_path)
    if not file_path.exists() or not file_path.is_file():
        raise UIError(404, f"file {relative_path!r} was not found")
    if not _is_text_file(file_path):
        raise UIError(415, f"file {relative_path!r} is not a supported text file")
    content = _read_text(file_path, max_bytes=MAX_TEXT_BYTES)
    return {
        "path": relative_path,
        "content": content,
        "truncated": file_path.stat().st_size > MAX_TEXT_BYTES,
    }


def list_run_files(
    run_dir: Path, *, include_internal_files: bool = True
) -> list[JsonDict]:
    candidates: list[Path] = []
    for name in ("run.json", "review.md", "preview.md", "run.log"):
        path = run_dir / name
        if path.exists():
            candidates.append(path)
    base_names = ["package", "verification"]
    if include_internal_files:
        base_names.extend(
            ["prompts", "ai_outputs", "checkpoints", "repairs", "ui_jobs", "submissions"]
        )
    for base_name in base_names:
        base = run_dir / base_name
        if not base.exists():
            continue
        candidates.extend(path for path in base.rglob("*") if path.is_file())
    files: list[JsonDict] = []
    for path in sorted(candidates):
        if not _is_text_file(path):
            continue
        rel = path.relative_to(run_dir).as_posix()
        if _is_hidden_or_secret(rel):
            continue
        files.append({"path": rel, "size": path.stat().st_size})
    return files


def list_submission_templates(run_dir: Path) -> list[JsonDict]:
    solutions_dir = run_dir / "package" / "solutions"
    if not solutions_dir.exists():
        return []
    templates: list[JsonDict] = []
    for path in sorted(solutions_dir.glob("*.cpp")):
        rel = path.relative_to(run_dir).as_posix()
        templates.append({"path": rel, "label": path.name, "size": path.stat().st_size})
    return templates


def list_submissions(run_dir: Path) -> list[JsonDict]:
    submissions_dir = run_dir / "submissions"
    if not submissions_dir.exists():
        return []
    rows: list[JsonDict] = []
    for item in submissions_dir.iterdir():
        if not item.is_dir():
            continue
        report_path = item / "submission_report.json"
        report = _read_json(report_path)
        source_path = item / "main.cpp"
        status, detail = _submission_status(run_dir, item, report)
        sort_time = _submission_sort_time(item, report_path, source_path, report)
        rows.append(
            {
                "id": item.name,
                "source_path": source_path.relative_to(run_dir).as_posix()
                if source_path.exists()
                else "",
                "report_path": report_path.relative_to(run_dir).as_posix()
                if report_path.exists()
                else "",
                "has_report": report is not None,
                "status": status,
                "verdict": report.get("verdict") if report else status,
                "passed": report.get("passed") if report else None,
                "total": report.get("total") if report else None,
                "created_at": report.get("created_at") if report else None,
                "sort_time": sort_time,
                "detail": report.get("detail", detail) if report else detail,
                "failed_test": report.get("failed_test") if report else None,
                "compile": report.get("compile") if report else None,
                "tests": report.get("tests", []) if report else [],
            }
        )
    rows.sort(
        key=lambda row: (
            1 if row.get("has_report") else 0,
            float(row.get("sort_time") or 0),
            str(row.get("id", "")),
        ),
        reverse=True,
    )
    return rows


def _submission_status(
    run_dir: Path, submission_dir: Path, report: JsonDict | None
) -> tuple[str, str]:
    if report is not None:
        return str(report.get("verdict", "DONE")), str(report.get("detail", ""))
    job_log = run_dir / "ui_jobs" / f"{submission_dir.name}.log"
    if job_log.exists():
        text = _read_text_tail(job_log)
        if "cpideas: error:" in text or "UI job failed:" in text:
            return "FAILED", _last_nonempty_line(text)
        return "RUNNING", "Submission job has a log but no report yet."
    return "INCOMPLETE", "Submission directory has no report."


def _submission_sort_time(
    item: Path, report_path: Path, source_path: Path, report: JsonDict | None
) -> float:
    created_at = report.get("created_at") if report else None
    if isinstance(created_at, str) and created_at:
        try:
            return datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    for path in (report_path, source_path, item):
        if path.exists():
            return path.stat().st_mtime
    return 0.0


def render_markdown_html(text: str) -> str:
    html = markdown_lib.markdown(
        text,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html",
    )
    allowed_tags = set(bleach.sanitizer.ALLOWED_TAGS) | {
        "p",
        "pre",
        "span",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "hr",
        "br",
    }
    allowed_attributes = {
        **bleach.sanitizer.ALLOWED_ATTRIBUTES,
        "a": ["href", "title", "rel"],
        "code": ["class"],
        "th": ["align"],
        "td": ["align"],
    }
    return bleach.clean(
        html,
        tags=allowed_tags,
        attributes=allowed_attributes,
        protocols=["http", "https", "mailto"],
        strip=True,
    )


@dataclass
class UIJob:
    id: str
    run_name: str
    action: str
    command: list[str]
    log_path: Path
    submission_id: str | None = None
    submission_report_path: Path | None = None
    status: str = "running"
    return_code: int | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> JsonDict:
        return {
            "id": self.id,
            "run_name": self.run_name,
            "action": self.action,
            "command": self.command,
            "log_path": _display_path(self.log_path),
            "status": self.status,
            "return_code": self.return_code,
            "error": self.error,
            "submission_id": self.submission_id,
            "submission_report": _read_json(self.submission_report_path)
            if self.submission_report_path
            else None,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_tail": _read_text_tail(self.log_path),
        }


class UIActionBackend(Protocol):
    def start_action(self, run_name: str, action: str) -> UIJob:
        ...

    def start_submission(self, run_name: str, code: str) -> UIJob:
        ...

    def get_job(self, job_id: str) -> UIJob:
        ...

    def jobs_for_run(self, run_name: str) -> list[JsonDict]:
        ...


class CPIdeasUIHandler(BaseHTTPRequestHandler):
    runs_dir: Path
    action_backend: UIActionBackend | None
    actions_enabled: bool
    internal_files_enabled: bool

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                self._send_html(UI_HTML)
                return
            if path == "/api/capabilities":
                self._send_json(self._capabilities())
                return
            if path == "/api/runs":
                self._send_json({"runs": scan_runs(self.runs_dir)})
                return
            if path.startswith("/api/runs/"):
                self._handle_run_get(path, parsed.query)
                return
            if path.startswith("/api/jobs/"):
                job_id = unquote(path.removeprefix("/api/jobs/"))
                backend = self._require_action_backend()
                self._send_json({"job": backend.get_job(job_id).to_dict()})
                return
            raise UIError(404, "not found")
        except UIError as exc:
            self._send_json({"error": exc.message}, status=exc.status)
        except Exception as exc:  # pragma: no cover - safety boundary
            self._send_json({"error": str(exc)}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/actions/mock-demo":
                backend = self._require_action_backend()
                job = backend.start_action("ui-demo", "mock-demo")
                self._send_json({"job": job.to_dict()}, status=202)
                return
            if path.startswith("/api/runs/"):
                parts = path.strip("/").split("/")
                if (
                    len(parts) == 5
                    and parts[0] == "api"
                    and parts[1] == "runs"
                    and parts[3] == "actions"
                ):
                    run_name = unquote(parts[2])
                    action = parts[4]
                    backend = self._require_action_backend()
                    if action == "submit":
                        payload = self._read_json_body()
                        code = payload.get("code")
                        if not isinstance(code, str):
                            raise UIError(
                                400, "submit action requires string field 'code'"
                            )
                        job = backend.start_submission(run_name, code)
                        self._send_json({"job": job.to_dict()}, status=202)
                        return
                    if action not in {"preview", "verify", "resume"}:
                        raise UIError(404, f"unknown action {action!r}")
                    job = backend.start_action(run_name, action)
                    self._send_json({"job": job.to_dict()}, status=202)
                    return
            raise UIError(404, "not found")
        except UIError as exc:
            self._send_json({"error": exc.message}, status=exc.status)
        except Exception as exc:  # pragma: no cover - safety boundary
            self._send_json({"error": str(exc)}, status=500)

    def _capabilities(self) -> JsonDict:
        actions_enabled = bool(self.actions_enabled and self.action_backend)
        return {
            "actions_enabled": actions_enabled,
            "submission_enabled": actions_enabled,
            "internal_files_enabled": self.internal_files_enabled,
            "mode": "workbench" if actions_enabled else "viewer",
        }

    def _require_action_backend(self) -> UIActionBackend:
        if not self.actions_enabled or self.action_backend is None:
            raise UIError(403, "UI actions are disabled in viewer mode")
        return self.action_backend

    def _read_json_body(self) -> JsonDict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_SUBMISSION_BYTES + 4096:
            raise UIError(413, "request body is too large")
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise UIError(400, "request body must be JSON") from exc
        if not isinstance(data, dict):
            raise UIError(400, "request body must be a JSON object")
        return data

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_run_get(self, path: str, query: str) -> None:
        suffix = path.removeprefix("/api/runs/")
        if suffix.endswith("/file"):
            run_name = unquote(suffix[: -len("/file")])
            params = parse_qs(query)
            rel = params.get("path", [""])[0]
            self._send_json(
                read_run_file(
                    self.runs_dir,
                    run_name,
                    rel,
                    include_internal_files=self.internal_files_enabled,
                )
            )
            return
        run_name = unquote(suffix)
        detail = build_run_detail(
            self.runs_dir,
            run_name,
            include_internal_files=self.internal_files_enabled,
        )
        detail["jobs"] = (
            self.action_backend.jobs_for_run(run_name)
            if self.actions_enabled and self.action_backend
            else []
        )
        self._send_json(detail)

    def _send_html(self, text: str, status: int = 200) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, data: JsonDict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _run_summary(run_dir: Path, runs_root: Path) -> JsonDict:
    """Build the sidebar/list payload for a single run directory.

    ``name`` is the run's path **relative to** ``runs_root`` (POSIX-style with
    ``/`` separators) so the client can route to runs nested inside a batch
    folder. The client URL-encodes it before sending. ``relative_path`` is the
    same value kept for backward compatibility with older UI builds.
    """
    run_data = _read_json(run_dir / "run.json")
    idea = _dict_or_empty(run_data.get("idea") if run_data else None)
    human_review = _dict_or_empty(run_data.get("human_review") if run_data else None)
    plan = _dict_or_empty(run_data.get("artifact_prompt_plan") if run_data else None)
    prompts = plan.get("prompts") if isinstance(plan.get("prompts"), list) else []
    checkpoints = run_dir / "checkpoints"
    if not idea and checkpoints.exists():
        idea = _dict_or_empty(_read_json(checkpoints / "idea.json"))

    verification = _read_json(run_dir / "verification" / "cpideas_report.json")
    solutions = verification.get("solutions", []) if verification else []
    repair_history = human_review.get("repair_history", [])
    relative_name = _entry_name(run_dir, runs_root)
    return {
        "kind": "run",
        "name": relative_name,
        "display_name": run_dir.name,
        "path": _display_path(run_dir),
        "relative_path": relative_name,
        "parent": _parent_name(run_dir, runs_root),
        "title": idea.get("title") or run_dir.name,
        "status": "complete" if (run_dir / "run.json").exists() else "checkpoint",
        "risk": human_review.get("risk_level"),
        "artifact_prompt_count": len(prompts),
        "repair_count": len(repair_history) if isinstance(repair_history, list) else 0,
        "solution_count": len(solutions) if isinstance(solutions, list) else 0,
        "has_package": (run_dir / "package").exists(),
        "has_verification": (run_dir / "verification" / "cpideas_report.json").exists(),
        "has_review": (run_dir / "review.md").exists(),
        "has_preview": (run_dir / "preview.md").exists(),
        "updated_at": _latest_mtime(run_dir),
    }


def _batch_summary(batch_dir: Path, runs_root: Path) -> JsonDict:
    """Build a sidebar entry summarising a ``batch_index.json``-bearing folder.

    Batches are not directly runnable; the entry exists so the UI can collapse
    every per-seed run under a single header. The client renders this purely
    from the ``kind`` and ``children`` hints; clicking a batch should jump to
    the first child run.
    """
    index_path = batch_dir / "batch_index.json"
    payload = _read_json(index_path)
    summary = _dict_or_empty(payload.get("summary") if payload else None)
    entries_raw = payload.get("entries") if isinstance(payload, dict) else None
    entries = entries_raw if isinstance(entries_raw, list) else []
    relative_name = _entry_name(batch_dir, runs_root)
    return {
        "kind": "batch",
        "name": relative_name,
        "display_name": batch_dir.name,
        "path": _display_path(batch_dir),
        "relative_path": relative_name,
        "parent": _parent_name(batch_dir, runs_root),
        "title": f"batch · {batch_dir.name}",
        "summary": summary,
        "entry_count": len(entries),
        "updated_at": index_path.stat().st_mtime if index_path.exists() else None,
    }


def _entry_name(path: Path, runs_root: Path) -> str:
    try:
        relative = path.relative_to(runs_root)
    except ValueError:
        return path.name
    if relative == Path("."):
        return path.name
    return relative.as_posix()


def _parent_name(run_dir: Path, runs_root: Path) -> str | None:
    """Return the relative parent path (or ``None`` for top-level entries).

    Used by the UI sidebar to indent batched runs under their parent so the
    structure mirrors the on-disk layout.
    """
    try:
        relative = run_dir.relative_to(runs_root)
    except ValueError:
        return None
    if relative == Path("."):
        return None
    parent = relative.parent
    if parent == Path("."):
        return None
    return parent.as_posix()


def _statement_from_run(run_data: JsonDict | None, run_dir: Path) -> JsonDict:
    """Build the statement payload shipped to the UI.

    Always returns the canonical English fields plus a ``translations`` map
    keyed by BCP-47 tag. The UI uses the map to populate the language
    switcher; entries that are missing (or that the translator could not fill
    for a particular field) fall back to the English copy on the client.
    """
    idea = _dict_or_empty(run_data.get("idea") if run_data else None)
    if not idea:
        idea = _dict_or_empty(_read_json(run_dir / "checkpoints" / "idea.json"))
    raw_translations = idea.get("translations") or {}
    translations: dict[str, JsonDict] = {}
    if isinstance(raw_translations, dict):
        for tag, payload in raw_translations.items():
            if isinstance(tag, str) and isinstance(payload, dict):
                translations[tag] = payload
    available_locales = [
        {
            "tag": "en",
            "display_name": LOCALE_DISPLAY_NAMES["en"],
            "default": True,
        }
    ]
    for tag in sorted(translations):
        available_locales.append(
            {
                "tag": tag,
                "display_name": LOCALE_DISPLAY_NAMES.get(tag, tag),
                "default": False,
            }
        )
    return {
        "title": idea.get("title", ""),
        "statement": idea.get("statement", ""),
        "input_format": idea.get("input_format", ""),
        "output_format": idea.get("output_format", ""),
        "constraints": idea.get("constraints", ""),
        "samples": idea.get("samples", []),
        "expected_solution": idea.get("expected_solution", ""),
        "target_difficulty": idea.get("target_difficulty", ""),
        "translations": translations,
        "available_locales": available_locales,
    }


def _localized_markdown_map(run_dir: Path, stem: str) -> dict[str, str]:
    """Find ``<stem>.<locale>.md`` files and return a ``locale -> html`` map.

    This lets the UI switch the review/preview tab content by locale without
    making an extra HTTP round-trip: we ship every known translation inline
    as pre-rendered HTML in the detail payload.
    """
    result: dict[str, str] = {}
    for path in sorted(run_dir.glob(f"{stem}.*.md")):
        tag = path.name.removeprefix(f"{stem}.").removesuffix(".md")
        if not tag or tag == stem:
            continue
        text = _read_text(path, max_bytes=MAX_TEXT_BYTES)
        if text:
            result[tag] = render_markdown_html(text)
    return result


def _run_key(run_dir: Path, runs_root: Path) -> str:
    """Return the canonical key used by UI action backends.

    Two batches can each hold a directory named ``0001_xxx``; keying on the
    leaf name alone would let an action in one batch block an action in the
    other. We use the path relative to ``runs_root`` (or the absolute path as
    a fallback) so each on-disk directory has a unique key.
    """
    try:
        return run_dir.resolve().relative_to(runs_root.resolve()).as_posix()
    except ValueError:
        return str(run_dir.resolve())


def _run_dir(root: Path, run_name: str) -> Path:
    """Translate a client-supplied ``run_name`` into a directory under ``root``.

    Accepts forward-slash separated relative paths so batched runs (e.g.
    ``lcb-smoke-batch/0001_2053B``) can be addressed by the same URL shape as
    flat top-level runs. Each path segment is validated against directory
    traversal, backslashes, and leading dots; the resolved path is then
    asserted to still live inside ``root`` as a belt-and-braces check against
    symlink escapes.
    """
    if not run_name:
        raise UIError(400, "invalid run name")
    if run_name == root.name and (
        (root / "run.json").exists() or (root / "checkpoints").exists()
    ):
        return root
    parts = run_name.split("/")
    for part in parts:
        if not part or part in {".", ".."} or "\\" in part or part.startswith("."):
            raise UIError(400, "invalid run name")
    path = (root.joinpath(*parts)).resolve()
    if not path.is_relative_to(root):
        raise UIError(403, "run path escapes runs directory")
    return path


def _safe_file_in_run(run_dir: Path, relative_path: str) -> Path:
    if (
        not relative_path
        or Path(relative_path).is_absolute()
        or _is_hidden_or_secret(relative_path)
    ):
        raise UIError(403, "file path is not allowed")
    path = (run_dir / relative_path).resolve()
    run_root = run_dir.resolve()
    if not path.is_relative_to(run_root):
        raise UIError(403, "file path escapes run directory")
    return path


def _is_internal_viewer_file(relative_path: str) -> bool:
    parts = Path(relative_path).parts
    return bool(parts) and parts[0] in INTERNAL_VIEWER_DIRS


def _is_hidden_or_secret(relative_path: str) -> bool:
    parts = Path(relative_path).parts
    return any(part.startswith(".") for part in parts) or any(
        part.lower() in {"env", ".env"} for part in parts
    )


def _is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in {"run.log"}


def _read_json(path: Path) -> JsonDict | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_text(path: Path, *, max_bytes: int) -> str:
    if not path.exists() or not path.is_file():
        return ""
    data = path.read_bytes()[:max_bytes]
    return data.decode("utf-8", errors="replace")


def _read_text_tail(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > LOG_TAIL_BYTES:
            handle.seek(size - LOG_TAIL_BYTES)
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _dict_or_empty(value: Any) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    for candidate in (
        "run.json",
        "review.md",
        "preview.md",
        "run.log",
        "verification/cpideas_report.json",
    ):
        item = path / candidate
        if item.exists():
            latest = max(latest, item.stat().st_mtime)
    return latest


def _display_path(path: Path) -> str:
    try:
        return os.fspath(path.relative_to(Path.cwd()))
    except ValueError:
        return os.fspath(path)


UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CPIdeas Plus</title>
  <!--
    KaTeX (LaTeX-style math) is rendered on the client. We pull the CSS / JS
    bundle from a jsDelivr CDN to keep the Python package free of binary
    assets. The render call is triggered after every Markdown panel update
    (see renderMath() below). If the CDN is unreachable the rest of the UI
    still works; math just shows as raw "$...$".
  -->
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.css" crossorigin="anonymous">
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.js" crossorigin="anonymous"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/contrib/auto-render.min.js" crossorigin="anonymous"></script>
  <style>
    /* KaTeX containers should inherit the host's color/font baseline. */
    .katex-display { margin: 0.5em 0; overflow-x: auto; overflow-y: hidden; }
    .katex { font-size: 1.02em; }
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #657282;
      --line: #d9dee7;
      --accent: #1b6ac9;
      --accent-soft: #e7f0fb;
      --bad: #b42318;
      --good: #067647;
      --warn: #b54708;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input, select { font: inherit; }
    .app {
      display: grid;
      grid-template-columns: minmax(240px, 300px) minmax(0, 1fr);
      min-height: 100vh;
    }
    aside {
      border-right: 1px solid var(--line);
      background: var(--panel);
      display: flex;
      flex-direction: column;
      min-width: 0;
    }
    header {
      height: 54px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: 0; }
    .run-list { overflow: auto; padding: 8px; }
    .run-item {
      width: 100%;
      text-align: left;
      border: 1px solid transparent;
      background: transparent;
      border-radius: 6px;
      padding: 10px;
      cursor: pointer;
      display: grid;
      gap: 4px;
    }
    .run-item:hover, .run-item.active { background: var(--accent-soft); border-color: #bdd6f5; }
    .run-item[data-depth="1"] { margin-left: 14px; border-left: 2px solid #e2e8f0; padding-left: 12px; }
    .run-item[data-depth="2"] { margin-left: 28px; border-left: 2px solid #e2e8f0; padding-left: 12px; }
    .run-item.batch { font-weight: 600; background: #f1f5f9; border-color: #cbd5e1; cursor: default; }
    .run-item.batch:hover { background: #e2e8f0; }
    .run-title { font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .run-meta { color: var(--muted); font-size: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
    .run-meta .badge { background: var(--accent-soft); color: #1e3a8a; padding: 1px 6px; border-radius: 10px; font-size: 11px; }
    main { min-width: 0; display: flex; flex-direction: column; }
    .toolbar {
      height: 54px;
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 0 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      flex-wrap: wrap;
    }
    .title-block { min-width: 200px; flex: 1; overflow: hidden; }
    .title-block strong { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .title-block span { color: var(--muted); font-size: 12px; }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      border-radius: 6px;
      padding: 7px 10px;
      cursor: pointer;
      min-height: 32px;
    }
    button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
    button:disabled { opacity: .55; cursor: default; }
    .tabs {
      display: flex;
      gap: 4px;
      padding: 8px 16px 0;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      overflow-x: auto;
    }
    .tab {
      border-bottom-left-radius: 0;
      border-bottom-right-radius: 0;
      border-bottom: 0;
      background: transparent;
    }
    .tab.active { background: var(--bg); border-color: var(--line); }
    .content { padding: 16px; overflow: auto; flex: 1; min-height: 0; }
    .section { margin-bottom: 20px; }
    .section h2 { font-size: 15px; margin: 0 0 8px; letter-spacing: 0; }
    /* Statement sections used to live in <pre> blocks which KaTeX skips on
       purpose. ".prose" keeps the visual rhythm (whitespace, line breaks)
       but lets KaTeX render inline / display math. */
    .prose {
      white-space: pre-wrap;
      word-break: normal;
      overflow-wrap: anywhere;
      font-family: var(--font-base, system-ui), sans-serif;
      background: #f8fafc;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      font-size: 14px;
      line-height: 1.55;
    }
    .locale-row {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin: 0 0 12px;
      padding: 6px 0;
    }
    .locale-pill {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      border-radius: 999px;
      padding: 4px 12px;
      font-size: 12px;
      cursor: pointer;
    }
    .locale-pill:hover { background: var(--accent-soft); }
    .locale-pill.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .seed-comparison {
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      background: #fefce8;
      padding: 14px;
      margin-bottom: 16px;
    }
    .seed-comparison h2 { margin: 0 0 8px; }
    .seed-comparison p { margin: 4px 0; font-size: 13px; }
    .kv {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 8px;
      margin-bottom: 16px;
    }
    .metric {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
      padding: 10px;
      min-width: 0;
    }
    .metric span { color: var(--muted); display: block; font-size: 12px; }
    .metric strong { display: block; overflow-wrap: anywhere; }
    pre {
      margin: 0;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
      padding: 12px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      max-width: 100%;
    }
    .markdown {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
      padding: 16px;
      max-width: 1100px;
      overflow: auto;
    }
    .markdown h1, .markdown h2, .markdown h3 {
      margin: 14px 0 8px;
      letter-spacing: 0;
      line-height: 1.25;
    }
    .markdown h1:first-child, .markdown h2:first-child, .markdown h3:first-child { margin-top: 0; }
    .markdown h1 { font-size: 22px; }
    .markdown h2 { font-size: 18px; }
    .markdown h3 { font-size: 15px; }
    .markdown p { margin: 8px 0; }
    .markdown ul { margin: 8px 0 8px 20px; padding: 0; }
    .markdown li { margin: 4px 0; }
    .markdown code {
      background: #eef2f7;
      border: 1px solid #dce3ed;
      border-radius: 4px;
      padding: 1px 4px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
    }
    .markdown pre {
      margin: 10px 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .markdown table { margin: 10px 0; }
    .markdown a { color: var(--accent); }
    .table-scroll {
      overflow: auto;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
    }
    .table-scroll table { border: 0; border-radius: 0; min-width: 820px; }
    .matrix-table th:first-child,
    .matrix-table td:first-child {
      position: sticky;
      left: 0;
      z-index: 1;
      background: var(--panel);
    }
    .matrix-cell {
      display: grid;
      gap: 2px;
      min-width: 110px;
    }
    .matrix-meta { color: var(--muted); font-size: 12px; white-space: nowrap; }
    table {
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
    }
    th, td {
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      text-align: left;
      overflow-wrap: anywhere;
    }
    th { font-size: 12px; color: var(--muted); font-weight: 650; }
    tr:last-child td { border-bottom: 0; }
    .pill {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 650;
      background: #eef2f7;
      color: var(--muted);
      white-space: nowrap;
    }
    .pill.good { color: var(--good); background: #e7f6ee; }
    .pill.bad { color: var(--bad); background: #fbe9e7; }
    .pill.warn { color: var(--warn); background: #fff1dc; }
    .file-layout {
      display: grid;
      grid-template-columns: minmax(220px, 320px) minmax(0, 1fr);
      gap: 12px;
      min-height: 420px;
    }
    .submit-layout {
      display: grid;
      grid-template-columns: minmax(320px, 1fr) minmax(260px, 360px);
      gap: 12px;
      align-items: start;
    }
    textarea.code-editor {
      width: 100%;
      min-height: 460px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 12px;
      font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      background: var(--panel);
      color: var(--ink);
      tab-size: 4;
    }
    .form-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
    select {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      min-height: 32px;
      padding: 4px 8px;
      max-width: 100%;
    }
    .file-list {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
      overflow: auto;
      max-height: 70vh;
    }
    .file-list button {
      display: block;
      width: 100%;
      border: 0;
      border-radius: 0;
      border-bottom: 1px solid var(--line);
      text-align: left;
      background: transparent;
    }
    .file-list button:hover { background: var(--accent-soft); }
    .status { color: var(--muted); padding: 12px 16px; }
    @media (max-width: 760px) {
      .app { grid-template-columns: 1fr; }
      aside { max-height: 260px; border-right: 0; border-bottom: 1px solid var(--line); }
      .toolbar { height: auto; min-height: 54px; padding-top: 8px; padding-bottom: 8px; }
      .file-layout { grid-template-columns: 1fr; }
      .submit-layout { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <header><h1>CPIdeas Plus</h1><button id="refreshRuns">Refresh</button></header>
      <div id="runList" class="run-list"></div>
    </aside>
    <main>
      <div class="toolbar">
        <div class="title-block">
          <strong id="runTitle">No run selected</strong>
          <span id="runSubtitle"></span>
        </div>
        <button id="mockDemo" disabled>Run Mock Demo</button>
        <button id="previewBtn" disabled>Generate Preview</button>
        <button id="verifyBtn" disabled>Verify Package</button>
        <button id="resumeBtn" class="primary" disabled>Resume Run</button>
      </div>
      <nav class="tabs" id="tabs"></nav>
      <div id="content" class="content"><div class="status">Loading...</div></div>
    </main>
  </div>
  <script>
    const state = { runs: [], selected: null, detail: null, tab: "statement", activeJob: null, locale: "en", capabilities: { actions_enabled: true } };
    const tabs = ["statement", "review", "preview", "verification", "tests", "submit", "files", "logs"];
    const tabLabels = { statement: "Statement", review: "Review", preview: "Preview", verification: "Verification", tests: "Tests", submit: "Submit", files: "Files", logs: "Logs" };

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch]));
    }
    function pill(value) {
      const text = String(value ?? "unknown");
      const cls = text === "AC" || text === "succeeded" ? "good" : (text.includes("WA") || text.includes("RE") || text.includes("failed") ? "bad" : "warn");
      return `<span class="pill ${cls}">${esc(text)}</span>`;
    }
    async function api(path, options) {
      const response = await fetch(path, options);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }
    async function loadCapabilities() {
      state.capabilities = await api("/api/capabilities");
      updateActionButtons(state.detail);
    }
    function actionsEnabled() {
      return state.capabilities.actions_enabled !== false;
    }
    function updateActionButtons(d) {
      const enabled = actionsEnabled();
      const mock = document.getElementById("mockDemo");
      const preview = document.getElementById("previewBtn");
      const verify = document.getElementById("verifyBtn");
      const resume = document.getElementById("resumeBtn");
      if (mock) mock.disabled = !enabled;
      if (preview) preview.disabled = !enabled || !d || (!d.summary.has_package && !d.run);
      if (verify) verify.disabled = !enabled || !d || !d.summary.has_package;
      if (resume) resume.disabled = !enabled || !d;
    }
    async function loadRuns() {
      const data = await api("/api/runs");
      state.runs = data.runs || [];
      if (!state.selected && state.runs.length) state.selected = state.runs[0].name;
      renderRuns();
      if (state.selected) await loadDetail(state.selected);
      else renderEmpty();
    }
    async function loadDetail(name) {
      state.selected = name;
      state.detail = await api(`/api/runs/${encodeURIComponent(name)}`);
      renderRuns();
      renderTabs();
      renderDetail();
    }
    function renderRuns() {
      const box = document.getElementById("runList");
      if (!state.runs.length) {
        box.innerHTML = `<div class="status">No runs found</div>`;
        return;
      }
      // Group by parent so batches show above their children. The backend
      // already sorts by updated_at descending; we just need to keep that
      // ordering between sibling batches while pulling each batch's children
      // up to sit underneath it.
      const indexByName = new Map(state.runs.map((run, i) => [run.name, i]));
      const groupOrder = [];
      const groupChildren = new Map();
      for (const run of state.runs) {
        const parent = run.parent || "";
        if (!groupChildren.has(parent)) {
          groupChildren.set(parent, []);
          groupOrder.push(parent);
        }
        groupChildren.get(parent).push(run);
      }
      const rendered = [];
      const seen = new Set();
      function emit(name) {
        if (seen.has(name)) return;
        const run = state.runs[indexByName.get(name)];
        if (!run) return;
        seen.add(name);
        rendered.push(run);
        const kids = groupChildren.get(run.name) || [];
        for (const kid of kids) emit(kid.name);
      }
      // Top-level entries first (parent === "" or parent not in our runs map).
      for (const run of state.runs) {
        const parent = run.parent || "";
        const parentHasEntry = parent && indexByName.has(parent);
        if (!parentHasEntry) emit(run.name);
      }
      // Fallback: any entries we somehow missed (defensive).
      for (const run of state.runs) emit(run.name);
      box.innerHTML = rendered.map(run => {
        const depth = (run.name.match(/\//g) || []).length;
        const isBatch = run.kind === "batch";
        const cls = `run-item${run.name === state.selected ? " active" : ""}${isBatch ? " batch" : ""}`;
        if (isBatch) {
          const summary = run.summary || {};
          const okCount = summary.succeeded || 0;
          const failCount = summary.failed || 0;
          const total = summary.total || run.entry_count || 0;
          return `<div class="${cls}" data-run="${esc(run.name)}" data-depth="${depth}" data-kind="batch">
            <span class="run-title">📦 ${esc(run.display_name || run.name)}</span>
            <span class="run-meta">
              <span class="badge">batch</span>
              <span>${okCount} ok</span>${failCount ? `<span>${failCount} failed</span>` : ""}
              <span>${total} total</span>
            </span>
          </div>`;
        }
        return `<button class="${cls}" data-run="${esc(run.name)}" data-depth="${depth}">
          <span class="run-title">${esc(run.title || run.display_name || run.name)}</span>
          <span class="run-meta"><span>${esc(run.display_name || run.name)}</span><span>${esc(run.status)}</span><span>${esc(run.risk || "")}</span></span>
        </button>`;
      }).join("");
      box.querySelectorAll("button[data-run]").forEach(button => {
        button.onclick = () => loadDetail(button.dataset.run);
      });
      // Clicking a batch header jumps to its first child run so users don't
      // have to also hunt the child entry.
      box.querySelectorAll("div.run-item.batch").forEach(node => {
        node.onclick = () => {
          const children = state.runs.filter(r => r.parent === node.dataset.run && r.kind !== "batch");
          if (children.length) loadDetail(children[0].name);
        };
        node.style.cursor = "pointer";
      });
    }
    function renderTabs() {
      const box = document.getElementById("tabs");
      box.innerHTML = tabs.map(tab => `<button class="tab ${tab === state.tab ? "active" : ""}" data-tab="${tab}">${tabLabels[tab]}</button>`).join("");
      box.querySelectorAll("button[data-tab]").forEach(button => {
        button.onclick = () => { state.tab = button.dataset.tab; renderTabs(); renderDetail(); };
      });
    }
    function renderDetail() {
      const d = state.detail;
      if (!d) return renderEmpty();
      document.getElementById("runTitle").textContent = d.summary.title || d.name;
      document.getElementById("runSubtitle").textContent = `${d.name} · ${d.summary.path}`;
      updateActionButtons(d);
      if (state.tab === "statement") renderStatement(d);
      if (state.tab === "review") renderLocalizedMarkdown("review", d);
      if (state.tab === "preview") renderLocalizedMarkdown("preview", d);
      if (state.tab === "verification") renderVerification(d);
      if (state.tab === "tests") renderTests(d);
      if (state.tab === "submit") renderSubmit(d);
      if (state.tab === "files") renderFiles(d);
      if (state.tab === "logs") renderLogs(d);
      // Run KaTeX over the freshly-rendered content panel. We re-run on every
      // tab switch because the inserted HTML changes; KaTeX is idempotent on
      // already-rendered <span class="katex">…</span> nodes.
      renderMath();
    }
    function renderMath() {
      const target = document.getElementById("content");
      if (!target || typeof renderMathInElement !== "function") return;
      renderMathInElement(target, {
        delimiters: [
          {left: "$$", right: "$$", display: true},
          {left: "$", right: "$", display: false},
          {left: "\\(", right: "\\)", display: false},
          {left: "\\[", right: "\\]", display: true},
        ],
        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
        throwOnError: false,
      });
    }
    function renderEmpty() {
      document.getElementById("runTitle").textContent = "No run selected";
      document.getElementById("runSubtitle").textContent = "";
      document.getElementById("tabs").innerHTML = "";
      document.getElementById("content").innerHTML = `<div class="status">No runs found</div>`;
      updateActionButtons(null);
    }
    function renderStatement(d) {
      const s = d.statement || {};
      // Pick the active locale (default English). The switcher row is the
      // very first child of the statement panel so users can flip languages
      // without scrolling. Fields that the translator did not provide fall
      // back to the English copy so the UI never shows undefined.
      const available = (s.available_locales && s.available_locales.length)
        ? s.available_locales
        : [{tag: "en", display_name: "English"}];
      if (!state.locale || !available.some(loc => loc.tag === state.locale)) {
        state.locale = "en";
      }
      const view = localizedStatement(s, state.locale);
      const localeButtons = available.map(loc => `
        <button class="locale-pill ${loc.tag === state.locale ? "active" : ""}" data-locale="${esc(loc.tag)}">${esc(loc.display_name)}</button>`).join("");
      const similarity = d.seed_similarity || {};
      const seed = d.seed || {};
      const simBlock = similarity.similarity_score != null ? `
        <div class="section seed-comparison">
          <h2>Seed Comparison</h2>
          <div class="kv" style="margin-bottom:10px">
            <div class="metric"><span>Similarity</span><strong>${esc(similarity.similarity_score)} / 10</strong></div>
            <div class="metric"><span>Verdict</span><strong>${esc(similarity.verdict)}</strong></div>
            <div class="metric"><span>Recommendation</span><strong>${esc(similarity.recommendation)}</strong></div>
          </div>
          ${seed.title ? `<p><strong>Seed:</strong> ${esc(seed.title)} (${esc(seed.source || "unknown")})</p>` : ""}
          ${similarity.shared_techniques ? `<p><strong>Shared techniques:</strong> ${esc((similarity.shared_techniques || []).join(", "))}</p>` : ""}
          ${similarity.key_differences ? `<p><strong>Key differences:</strong> ${esc((similarity.key_differences || []).join(", "))}</p>` : ""}
          ${similarity.transferability_assessment ? `<p><strong>Transferability:</strong> ${esc(similarity.transferability_assessment)}</p>` : ""}
          ${seed.statement ? `<details><summary>Seed statement excerpt</summary><div class="prose" style="margin-top:8px">${esc((seed.statement || "").substring(0, 800))}</div></details>` : ""}
        </div>` : (seed.title ? `
        <div class="section seed-comparison">
          <h2>Seed</h2>
          <p><strong>${esc(seed.title)}</strong> (${esc(seed.source || "unknown")})</p>
          <details><summary>Seed statement</summary><div class="prose" style="margin-top:8px">${esc((seed.statement || "").substring(0, 800))}</div></details>
        </div>` : "");
      // Wrap each statement section in a div with white-space: pre-wrap rather
      // than <pre>: KaTeX's auto-render skips <pre> (since competitive
      // programming statements still need code-style line breaks while
      // allowing inline `$x$` math to render).
      document.getElementById("content").innerHTML = `
        <div class="kv">
          <div class="metric"><span>Risk</span><strong>${esc(d.summary.risk || "unknown")}</strong></div>
          <div class="metric"><span>Artifact prompts</span><strong>${esc(d.summary.artifact_prompt_count)}</strong></div>
          <div class="metric"><span>Repairs</span><strong>${esc(d.summary.repair_count)}</strong></div>
          <div class="metric"><span>Solutions</span><strong>${esc(d.summary.solution_count)}</strong></div>
        </div>
        <div class="locale-row">${localeButtons}</div>
        ${simBlock}
        <div class="section"><h2>${esc(view.title || d.summary.title)}</h2><div class="prose">${esc(view.statement)}</div></div>
        <div class="section"><h2>Input</h2><div class="prose">${esc(view.input_format)}</div></div>
        <div class="section"><h2>Output</h2><div class="prose">${esc(view.output_format)}</div></div>
        <div class="section"><h2>Constraints</h2><div class="prose">${esc(formatConstraints(view.constraints))}</div></div>
        <div class="section"><h2>Expected Solution</h2><div class="prose">${esc(view.expected_solution)}</div></div>`;
      document.querySelectorAll(".locale-pill").forEach(btn => {
        btn.onclick = () => { state.locale = btn.dataset.locale; renderDetail(); };
      });
    }
    function localizedStatement(s, locale) {
      // English uses the canonical fields directly; other locales pull from
      // s.translations[locale] and fall back per-field so partial translations
      // still display rather than blank the section.
      if (locale === "en" || !s.translations || !s.translations[locale]) {
        return {
          title: s.title,
          statement: s.statement,
          input_format: s.input_format,
          output_format: s.output_format,
          constraints: s.constraints,
          expected_solution: s.expected_solution,
        };
      }
      const t = s.translations[locale];
      return {
        title: t.title || s.title,
        statement: t.statement || s.statement,
        input_format: t.input_format || s.input_format,
        output_format: t.output_format || s.output_format,
        constraints: (Array.isArray(t.constraints) && t.constraints.length)
          ? t.constraints
          : s.constraints,
        // Expected-solution sketches are intentionally NOT translated (they
        // describe the algorithm, not the problem) so always show the source.
        expected_solution: s.expected_solution,
      };
    }
    function formatConstraints(value) {
      // Constraints can be a list (preferred) or a free-form string. Render
      // lists as a bullet block so the prose div displays them readably.
      if (Array.isArray(value)) return value.map(line => `- ${line}`).join("\n");
      return value || "";
    }
    function renderLocalizedMarkdown(docType, d) {
      // docType is "review" or "preview". d carries:
      //   - <docType>_markdown (English source)
      //   - <docType>_html (pre-rendered English)
      //   - localized_<docType> (map: locale -> pre-rendered HTML)
      const locale = state.locale || "en";
      const localizedMap = d[`localized_${docType}`] || {};
      let html = "";
      let filename = `${docType}.md`;
      let text = d[`${docType}_markdown`] || "";
      if (locale !== "en" && localizedMap[locale]) {
        html = localizedMap[locale];
        filename = `${docType}.${locale}.md`;
      } else {
        html = d[`${docType}_html`] || "";
      }
      renderMarkdown(text, filename, html);
    }
    function renderMarkdown(text, name, html) {
      document.getElementById("content").innerHTML = text ? `<div class="markdown">${html || fallbackMarkdownToHtml(text)}</div>` : `<div class="status">${esc(name)} not found</div>`;
    }
    function fallbackMarkdownToHtml(markdown) {
      const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
      const out = [];
      let paragraph = [];
      let list = [];
      let inCode = false;
      let code = [];

      function flushParagraph() {
        if (!paragraph.length) return;
        out.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
        paragraph = [];
      }
      function flushList() {
        if (!list.length) return;
        out.push(`<ul>${list.map(item => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`);
        list = [];
      }
      function flushTable(index) {
        if (index + 1 >= lines.length || !isTableSeparator(lines[index + 1])) return false;
        const header = splitTableRow(lines[index]);
        const body = [];
        let cursor = index + 2;
        while (cursor < lines.length && isTableRow(lines[cursor])) {
          body.push(splitTableRow(lines[cursor]));
          cursor += 1;
        }
        out.push(`<table><thead><tr>${header.map(cell => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>${body.map(row => `<tr>${header.map((_, i) => `<td>${inlineMarkdown(row[i] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
        return cursor - index;
      }
      for (let i = 0; i < lines.length; i += 1) {
        const line = lines[i];
        if (line.trim().startsWith("```")) {
          flushParagraph(); flushList();
          if (inCode) {
            out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
            code = [];
            inCode = false;
          } else {
            inCode = true;
          }
          continue;
        }
        if (inCode) {
          code.push(line);
          continue;
        }
        if (!line.trim()) {
          flushParagraph(); flushList();
          continue;
        }
        const tableAdvance = isTableRow(line) ? flushTable(i) : false;
        if (tableAdvance) {
          flushParagraph(); flushList();
          i += tableAdvance - 1;
          continue;
        }
        const heading = line.match(/^(#{1,3})\s+(.*)$/);
        if (heading) {
          flushParagraph(); flushList();
          out.push(`<h${heading[1].length}>${inlineMarkdown(heading[2])}</h${heading[1].length}>`);
          continue;
        }
        const bullet = line.match(/^\s*[-*]\s+(.*)$/);
        if (bullet) {
          flushParagraph();
          list.push(bullet[1]);
          continue;
        }
        paragraph.push(line.trim());
      }
      flushParagraph(); flushList();
      if (inCode) out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      return out.join("\n");
    }
    function inlineMarkdown(text) {
      let html = esc(text);
      html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
      html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, url) => {
        const safeUrl = String(url).startsWith("http://") || String(url).startsWith("https://") ? url : "#";
        return `<a href="${esc(safeUrl)}" target="_blank" rel="noreferrer">${label}</a>`;
      });
      return html;
    }
    function isTableRow(line) {
      return line.includes("|") && line.trim().length > 2;
    }
    function isTableSeparator(line) {
      return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
    }
    function splitTableRow(line) {
      let text = line.trim();
      if (text.startsWith("|")) text = text.slice(1);
      if (text.endsWith("|")) text = text.slice(0, -1);
      return text.split("|").map(cell => cell.trim());
    }
    function renderVerification(d) {
      const report = d.verification || {};
      const rows = (report.solutions || []).map(sol => {
        const tests = sol.tests || [];
        const failed = sol.failed_test ? JSON.stringify(sol.failed_test) : "";
        return `<tr>
          <td>${esc(sol.source_path)}</td><td>${esc(sol.expected)}</td><td>${pill(sol.verdict)}</td>
          <td>${esc(tests.length)}</td><td>${esc(maxTime(tests))}</td><td>${esc(maxMemory(tests))}</td><td>${esc(failed || sol.detail || "")}</td>
        </tr>`;
      }).join("");
      document.getElementById("content").innerHTML = `
        <div class="section"><h2>Election</h2><pre>${esc(JSON.stringify(d.election || {}, null, 2))}</pre></div>
        <div class="section"><h2>Solutions</h2>
          <table><thead><tr><th>Source</th><th>Expected</th><th>Verdict</th><th>Tests</th><th>Max ms</th><th>Max KB</th><th>Detail</th></tr></thead><tbody>${rows || "<tr><td colspan='7'>No verification report</td></tr>"}</tbody></table>
        </div>
        <div class="section"><h2>Dropped Tests</h2><pre>${esc(JSON.stringify(d.dropped_tests || [], null, 2))}</pre></div>
        <div class="section"><h2>Bruteforce Votes</h2><pre>${esc(JSON.stringify(d.bruteforce_votes || {}, null, 2))}</pre></div>`;
    }
    function renderTests(d) {
      const report = d.verification || {};
      const solutions = report.solutions || [];
      document.getElementById("content").innerHTML = `
        <div class="section"><h2>Solution Test Matrix</h2>
          ${resultMatrix(solutions, { idKey: "source_path", titleKey: "source_path", empty: "No per-test results" })}
        </div>`;
    }
    function renderSubmit(d) {
      const templates = d.submission_templates || [];
      const submissions = d.submissions || [];
      const latest = submissions.find(item => item.has_report) || submissions[0] || null;
      const submitDisabled = actionsEnabled() ? "" : " disabled";
      const templateOptions = templates.map(file => `<option value="${esc(file.path)}">${esc(file.label || file.path)}</option>`).join("");
      const history = submissions.map(item => `<tr>
        <td>${esc(item.id)}</td>
        <td>${pill(item.verdict || item.status)}</td>
        <td>${esc(item.passed ?? "")}/${esc(item.total ?? "")}</td>
        <td>${esc(item.status || "")}</td>
        <td>${esc(item.created_at || "")}</td>
        <td>${esc(item.detail || "")}</td>
      </tr>`).join("");
      document.getElementById("content").innerHTML = `
        <div class="submit-layout">
          <div>
            <div class="form-row">
              <select id="templateSelect"><option value="">Blank</option>${templateOptions}</select>
              <button id="loadTemplateBtn">Load Template</button>
              <button id="clearCodeBtn">Clear</button>
              <button id="submitCodeBtn" class="primary"${submitDisabled}>Submit C++17</button>
            </div>
            <textarea id="submitCode" class="code-editor" spellcheck="false"></textarea>
          </div>
          <div class="section">
            <h2>Submissions</h2>
            <table><thead><tr><th>ID</th><th>Verdict</th><th>Passed</th><th>Status</th><th>Created</th><th>Detail</th></tr></thead><tbody>${history || "<tr><td colspan='6'>No submissions</td></tr>"}</tbody></table>
          </div>
        </div>
        <div class="section"><h2>Submission Matrix</h2>${submissionMatrix(submissions)}</div>
        <div class="section"><h2>Selected Detail</h2>${submissionDetail(latest)}</div>`;
      document.getElementById("loadTemplateBtn").onclick = loadSelectedTemplate;
      document.getElementById("clearCodeBtn").onclick = () => { document.getElementById("submitCode").value = ""; };
      document.getElementById("submitCodeBtn").onclick = () => {
        if (actionsEnabled()) startSubmit().catch(alert);
      };
      if (templates.length) {
        document.getElementById("templateSelect").value = templates[0].path;
        loadSelectedTemplate().catch(() => {});
      }
    }
    function submissionMatrix(submissions) {
      if (!submissions.length) return `<div class="status">No submissions</div>`;
      return resultMatrix(submissions, { idKey: "id", titleKey: "id", empty: "No completed submission report" });
    }
    function submissionDetail(submission) {
      if (!submission) return `<div class="status">No submission selected</div>`;
      const compile = submission.compile || {};
      const stderr = compile.stderr || submission.detail || "";
      return `<div class="kv">
          <div class="metric"><span>ID</span><strong>${esc(submission.id)}</strong></div>
          <div class="metric"><span>Verdict</span><strong>${esc(submission.verdict || submission.status)}</strong></div>
          <div class="metric"><span>Passed</span><strong>${esc(submission.passed ?? "")}/${esc(submission.total ?? "")}</strong></div>
          <div class="metric"><span>First Failed</span><strong>${esc(submission.failed_test || "")}</strong></div>
        </div>
        <pre>${esc(stderr || "No detail")}</pre>`;
    }
    function resultMatrix(entries, options) {
      const tests = collectMatrixTests(entries);
      if (!entries.length || !tests.length) return `<div class="status">${esc(options.empty || "No per-test results")}</div>`;
      const header = entries.map(entry => {
        const id = entry[options.titleKey] || entry[options.idKey] || "";
        const verdict = entry.verdict || entry.status || "";
        const passed = entry.passed != null || entry.total != null ? `${entry.passed ?? ""}/${entry.total ?? ""}` : "";
        return `<th title="${esc(id)}">${esc(shortLabel(id))}<br>${pill(verdict)}<br><span class="matrix-meta">${esc(passed)}</span></th>`;
      }).join("");
      const rows = tests.map(test => {
        const cells = entries.map(entry => matrixCell(entry, test.key)).join("");
        return `<tr><td><strong>${esc(test.index)}</strong><br><span class="matrix-meta">${esc(test.path)}</span></td>${cells}</tr>`;
      }).join("");
      return `<div class="table-scroll"><table class="matrix-table"><thead><tr><th>Test</th>${header}</tr></thead><tbody>${rows}</tbody></table></div>`;
    }
    function collectMatrixTests(entries) {
      const seen = new Map();
      entries.forEach(entry => {
        (entry.tests || []).forEach(test => {
          const key = String(test.index ?? test.input_path ?? "");
          if (!key) return;
          if (!seen.has(key)) seen.set(key, { key, index: test.index ?? "", path: test.input_path || "" });
        });
      });
      return Array.from(seen.values()).sort((a, b) => Number(a.index || 0) - Number(b.index || 0) || String(a.path).localeCompare(String(b.path)));
    }
    function matrixCell(entry, key) {
      const tests = entry.tests || [];
      const test = tests.find(item => String(item.index ?? item.input_path ?? "") === key);
      if (!test) {
        const status = entry.verdict || entry.status || "";
        const detail = entry.detail || "";
        return `<td title="${esc(detail)}"><div class="matrix-cell">${pill(status || "N/A")}<span class="matrix-meta">${esc(detail)}</span></div></td>`;
      }
      const memory = test.memory_kb ?? test.memory_bytes ?? "";
      const detail = test.detail || "";
      return `<td title="${esc(detail)}"><div class="matrix-cell">${pill(test.status || "")}<span class="matrix-meta">${esc(test.time_ms ?? "")} ms · ${esc(memory)} KB</span></div></td>`;
    }
    function shortLabel(value) {
      const text = String(value || "");
      if (text.length <= 18) return text;
      const parts = text.split("/");
      return parts[parts.length - 1] || text.slice(0, 18);
    }
    async function loadSelectedTemplate() {
      const select = document.getElementById("templateSelect");
      const editor = document.getElementById("submitCode");
      if (!select || !editor || !select.value) {
        if (editor) editor.value = "";
        return;
      }
      const data = await api(`/api/runs/${encodeURIComponent(state.selected)}/file?path=${encodeURIComponent(select.value)}`);
      editor.value = data.content || "";
    }
    async function startSubmit() {
      if (!state.selected || !actionsEnabled()) return;
      const editor = document.getElementById("submitCode");
      const data = await api(`/api/runs/${encodeURIComponent(state.selected)}/actions/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: editor ? editor.value : "" })
      });
      state.activeJob = data.job.id;
      state.tab = "logs";
      await loadDetail(data.job.run_name);
      pollJob(data.job.id, true);
    }
    function maxTime(tests) {
      if (!tests.length) return "";
      return Math.max(...tests.map(test => Number(test.time_ms || 0)));
    }
    function maxMemory(tests) {
      if (!tests.length) return "";
      return Math.max(...tests.map(test => Number(test.memory_kb ?? test.memory_bytes ?? 0)));
    }
    function renderFiles(d) {
      const files = d.files || [];
      document.getElementById("content").innerHTML = `
        <div class="file-layout">
          <div class="file-list">${files.map(file => `<button data-file="${esc(file.path)}">${esc(file.path)}<br><span class="run-meta">${esc(file.size)} bytes</span></button>`).join("") || "<div class='status'>No files</div>"}</div>
          <pre id="fileContent"></pre>
        </div>`;
      document.querySelectorAll("button[data-file]").forEach(button => {
        button.onclick = async () => {
          const path = button.dataset.file;
          const data = await api(`/api/runs/${encodeURIComponent(state.selected)}/file?path=${encodeURIComponent(path)}`);
          document.getElementById("fileContent").textContent = data.content || "";
        };
      });
    }
    function renderLogs(d) {
      const jobs = (d.jobs || []).map(job => `<tr><td>${esc(job.id)}</td><td>${esc(job.action)}</td><td>${pill(job.status)}</td><td>${esc(job.return_code ?? "")}</td><td>${esc(job.log_path)}</td></tr>`).join("");
      document.getElementById("content").innerHTML = `
        <div class="section"><h2>Run Log</h2><pre>${esc(d.run_log_tail || "")}</pre></div>
        <div class="section"><h2>UI Jobs</h2><table><thead><tr><th>ID</th><th>Action</th><th>Status</th><th>Code</th><th>Log</th></tr></thead><tbody>${jobs || "<tr><td colspan='5'>No UI jobs</td></tr>"}</tbody></table></div>
        <div class="section"><h2>Latest Job Log</h2><pre id="jobLog"></pre></div>`;
      if (d.jobs && d.jobs.length) pollJob(d.jobs[0].id, false);
    }
    async function startAction(action) {
      if (!actionsEnabled()) return;
      if (!state.selected && action !== "mock-demo") return;
      const path = action === "mock-demo"
        ? "/api/actions/mock-demo"
        : `/api/runs/${encodeURIComponent(state.selected)}/actions/${action}`;
      const data = await api(path, { method: "POST" });
      state.activeJob = data.job.id;
      state.tab = "logs";
      await loadDetail(data.job.run_name);
      pollJob(data.job.id, true);
    }
    async function pollJob(jobId, reloadWhenDone) {
      try {
        const data = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        const log = document.getElementById("jobLog");
        if (log) log.textContent = data.job.log_tail || "";
        if (data.job.status === "running") {
          setTimeout(() => pollJob(jobId, reloadWhenDone), 1200);
        } else if (reloadWhenDone) {
          await loadRuns();
        }
      } catch (err) {
        const log = document.getElementById("jobLog");
        if (log) log.textContent = String(err);
      }
    }
    document.getElementById("refreshRuns").onclick = loadRuns;
    document.getElementById("previewBtn").onclick = () => startAction("preview").catch(alert);
    document.getElementById("verifyBtn").onclick = () => startAction("verify").catch(alert);
    document.getElementById("resumeBtn").onclick = () => startAction("resume").catch(alert);
    document.getElementById("mockDemo").onclick = () => startAction("mock-demo").catch(alert);
    renderTabs();
    loadCapabilities().then(loadRuns).catch(err => {
      document.getElementById("content").innerHTML = `<div class="status">${esc(err)}</div>`;
    });
  </script>
</body>
</html>
"""
