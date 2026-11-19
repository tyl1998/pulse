"""P1.9 run evidence: isolated run directory, JSON snapshots, attachments and reports.

Every ``run_case`` attempt owns one run directory below an evidence root and never shares a
mutable directory with another task. The recorder is the only writer of that directory:

* ``result.json``  machine-readable run result with expected/actual/source/environment;
* ``result.html``  self-contained readable report (inline CSS, no script, no external URL);
* ``attachments.json``  closed manifest of file attachments with sizes and digests;
* ``snapshots/*.json``  bounded step snapshots captured while the run is active;
* ``attachments/*.png``  screenshots requested from the driver inside the run.

Evidence failures are fail-closed for the evidence only: a write error is recorded as an
``evidence`` issue in the report and never turns a business verdict into a pass. Sensitive
keys and the run's known secret values are redacted before a report or snapshot is written.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import os
import string
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

MAX_SNAPSHOTS = 64
MAX_ATTACHMENTS = 24
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024
_SNAPSHOT_BYTES = 512 * 1024
_SAFE_CHARS = frozenset(string.ascii_letters + string.digits + "_-.")
_HEX = frozenset("0123456789abcdef")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
REDACTED = "[redacted]"
_MIN_SECRET_CHARS = 4
_SENSITIVE_WORDS = (
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "apikey",
    "api_key",
    "api-key",
)


def _sensitive(key: str) -> bool:
    return any(word in key.lower() for word in _SENSITIVE_WORDS)


def _secret_values(values: object) -> tuple[str, ...]:
    """Known secret strings, longest first so a secret containing another is replaced whole."""
    if not isinstance(values, Mapping):
        return ()
    unique = {
        item
        for item in values.values()
        if type(item) is str and len(item.strip()) >= _MIN_SECRET_CHARS
    }
    return tuple(sorted(unique, key=len, reverse=True))


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        value = value.replace(secret, REDACTED)
    return value


CHECK_OPERATORS = frozenset(
    {
        "exists",
        "page_is",
        "not_exists",
        "visible",
        "enabled",
        "checked",
        "text_equals",
        "contains",
        "regex",
        "semantic",
        "count_eq",
        "count_gt",
        "count_lt",
        "number_cmp",
    }
)
EVIDENCE_CONTRACT = "pulse-runtime-run-evidence-draft"
REPORT_CONTRACT = "pulse-runtime-run-report-draft"


class EvidenceError(Exception):
    """A bounded evidence failure that must not change the run verdict."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX for char in value)


def _safe_name(value: object) -> str | None:
    """Full identifier: match must cover the whole string, never a prefix."""
    if not isinstance(value, str):
        return None
    if any(char not in _SAFE_CHARS for char in value):
        return None
    return value


def _strip(mapping: object, secrets: tuple[str, ...] = ()) -> dict[str, object]:
    """Copy a mapping for the report: sensitive keys and known secret values never survive."""
    result: dict[str, object] = {}
    items = mapping.items() if isinstance(mapping, Mapping) else ()
    for key, value in items:
        if not isinstance(key, str):
            continue
        if _sensitive(key):
            result[key] = REDACTED
        elif isinstance(value, Mapping):
            result[key] = _strip(value, secrets)
        elif isinstance(value, list | tuple):
            result[key] = [
                _strip(item, secrets) if isinstance(item, Mapping) else _clean_scalar(item, secrets)
                for item in value
            ]
        else:
            result[key] = _clean_scalar(value, secrets)
    return result


def _clean_scalar(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str) and secrets:
        return _redact_text(value, secrets)
    return value


def expectation(condition: object) -> dict[str, object]:
    """Expected side of a checkpoint, reconstructed from the contract predicate."""
    body: dict[str, object] = {}
    operator = getattr(condition, "operator", None)
    if isinstance(operator, str) and operator in CHECK_OPERATORS:
        body["operator"] = operator
    for name in ("expected", "comparison", "unit", "precision", "observation_window_ms"):
        value = getattr(condition, name, None)
        if value is None:
            continue
        if isinstance(value, BaseModel):
            body["expected" if name == "expected" else name] = _strip(value.model_dump())
        elif type(value) in (bool, int, float, str):
            body["expected" if name == "expected" else name] = value
    source = getattr(condition, "source", None)
    if isinstance(source, str):
        body["source"] = source
    target = _target(condition)
    if target:
        body["target"] = target
    return body


def _target(condition: object) -> dict[str, object] | None:
    for name in ("target", "scope"):
        target = getattr(condition, name, None)
        if target is None:
            continue
        ref = getattr(target, "locator_ref", None)
        if isinstance(ref, str):
            return {name: {"locator_ref": _safe_name(ref) or "[invalid]"}}
        inline = getattr(target, "locator", None)
        if inline is not None:
            candidates = getattr(inline, "candidates", ())
            by = sorted({str(getattr(item, "by", "?")) for item in candidates})
            return {"target": {"candidates": len(candidates), "by": by}}
    return None


def check_evidence(
    actual: Mapping[str, object], condition: object, *, secrets: tuple[str, ...] = ()
) -> dict[str, object]:
    """One check's expected/actual/source, with redaction and any nested image payload removed."""
    source = getattr(condition, "source", None)
    cleaned = _strip(actual, secrets)
    return {
        "expected": expectation(condition),
        "actual": cleaned,
        "source": source if isinstance(source, str) else None,
    }


@dataclass
class _Entry:
    kind: str
    file: Path
    size: int
    digest: str
    step_id: str | None = None
    source: str | None = None


@dataclass
class EvidenceRecorder:
    """Owns one run directory; every dependency is injected, so it is offline testable."""

    run_id: str
    case_id: str
    profile_id: str
    root: Path
    environment: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)
    entries: list[_Entry] = field(default_factory=list)
    issues: list[dict[str, str]] = field(default_factory=list)
    secrets: tuple[str, ...] = ()
    snapshot_ordinal: int = 0

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        run_id: str,
        case_id: str,
        profile_id: str,
        environment: Mapping[str, object] | None = None,
        metadata: Mapping[str, object] | None = None,
        secrets: Mapping[str, str] | None = None,
    ) -> EvidenceRecorder:
        """Create ``root/<run_id>`` exclusively; an existing run directory is never reused."""
        if _safe_name(run_id) is None:
            raise EvidenceError("run_id_not_path_safe")
        if _safe_name(case_id) is None or _safe_name(profile_id) is None:
            raise EvidenceError("identifier_not_path_safe")
        root.mkdir(parents=True, exist_ok=True)
        directory = root / run_id
        try:
            directory.mkdir()
            for name in ("snapshots", "attachments"):
                (directory / name).mkdir()
        except FileExistsError:
            raise EvidenceError("evidence_run_directory_exists") from None
        return cls(
            run_id,
            case_id,
            profile_id,
            directory,
            _strip(environment),
            _strip(metadata),
            secrets=_secret_values(secrets),
        )

    @property
    def directory(self) -> Path:
        return self.root

    def _count(self, kind: str) -> int:
        return sum(1 for entry in self.entries if entry.kind == kind)

    def _issue(self, code: str, name: str) -> None:
        item = {"code": code, "file": name}
        if item not in self.issues:
            self.issues.append(item)

    def _write(self, name: str, payload: bytes, *, limit: int = MAX_JSON_BYTES) -> Path:
        """Atomic, bounded write inside this run directory; refuse symlinks and escapes."""
        parts = name.split("/")
        if (
            len(parts) > 2
            or any(part in ("", ".", "..") for part in parts)
            or any(Path(part).name != part for part in parts)
            or (len(parts) == 2 and parts[0] not in {"snapshots", "attachments"})
        ):
            raise EvidenceError("attachment_name_invalid")
        if len(payload) > limit:
            raise EvidenceError("attachment_too_large")
        target = self.root.joinpath(*parts)
        if target.is_symlink():
            raise EvidenceError("attachment_path_unsafe")
        if not target.parent.resolve().is_relative_to(self.root.resolve()):
            raise EvidenceError("attachment_path_unsafe")
        fd, tmp = tempfile.mkstemp(prefix=".evidence-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    def snapshot(self, name: str, payload: Mapping[str, object]) -> bool:
        """Bounded intermediate step snapshot; never contains values or secrets.

        The file name carries a run-local ordinal so a loop iteration or a repeated flow call
        cannot overwrite an earlier snapshot of the same checkpoint.
        """
        if _safe_name(name) is None:
            self._issue("snapshot_name_invalid", "snapshots")
            return False
        if self._count("snapshot") >= MAX_SNAPSHOTS:
            self._issue("snapshot_limit", name)
            return False
        try:
            encoded = json.dumps(
                _strip(payload, self.secrets), ensure_ascii=False, allow_nan=False
            ).encode()
        except (TypeError, ValueError):
            self._issue("snapshot_invalid", name)
            return False
        if len(encoded) > _SNAPSHOT_BYTES:
            self._issue("snapshot_too_large", name)
            return False
        body = encoded + b"\n"
        ordinal = self.snapshot_ordinal
        try:
            path = self._write(f"snapshots/{ordinal:04d}-{name}.json", body, limit=_SNAPSHOT_BYTES)
        except (EvidenceError, OSError) as exc:
            self._issue(getattr(exc, "code", "snapshot_write_failed"), name)
            return False
        self.snapshot_ordinal += 1
        self.entries.append(_Entry("snapshot", path, len(body), hashlib.sha256(body).hexdigest()))
        return True

    def attach_screenshot(self, payload: Mapping[str, object], *, step_id: str) -> bool:
        """Persist one decoded PNG returned by the driver, or record why it is refused."""
        if self._count("screenshot") >= MAX_ATTACHMENTS:
            self._issue("attachment_limit", "attachments")
            return False
        content = payload.get("content")
        digest = payload.get("sha256")
        size = payload.get("byte_length")
        name = payload.get("name")
        if (
            type(content) is not bytes
            or not content
            or content[:8] != _PNG_MAGIC
            or len(content) > MAX_PNG_BYTES
            or not _is_digest(digest)
            or type(size) is not int
            or size != len(content)
            or _safe_name(name) is None
        ):
            self._issue("screenshot_invalid", "attachments")
            return False
        if hashlib.sha256(content).hexdigest() != digest:
            self._issue("screenshot_digest_mismatch", "attachments")
            return False
        try:
            path = self._write(f"attachments/{name}", content, limit=MAX_PNG_BYTES)
        except (EvidenceError, OSError) as exc:
            self._issue(getattr(exc, "code", "attachment_write_failed"), "attachments")
            return False
        source = payload.get("source")
        self.entries.append(
            _Entry(
                "screenshot",
                path,
                len(content),
                digest,
                _safe_name(step_id),
                source if type(source) is str else None,
            )
        )
        return True

    def finish(
        self,
        result: Mapping[str, object],
        *,
        started_at_ms: int,
        finished_at_ms: int,
        elapsed_ms: float,
        suite_id: str | None = None,
    ) -> dict[str, object]:
        """Write ``attachments.json``, ``result.json`` and ``result.html``; return the report."""
        # Reports are written before the manifest so the manifest can list them.
        report = build_report(
            result,
            run_id=self.run_id,
            case_id=self.case_id,
            profile_id=self.profile_id,
            suite_id=suite_id,
            environment=self.environment,
            metadata=self.metadata,
            manifest={"files": self._manifest()["files"]},
            issues=self.issues,
            secrets=self.secrets,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            elapsed_ms=elapsed_ms,
        )
        self._write_report_file(
            "result.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n", "report_json"
        )
        self._write_report_file("result.html", render_html(report), "report_html")
        manifest = self._manifest()
        try:
            self._write(
                "attachments.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode() + b"\n",
            )
        except (EvidenceError, OSError) as exc:
            self._issue(getattr(exc, "code", "attachment_write_failed"), "attachments.json")
        evidence = report.get("evidence")
        if isinstance(evidence, dict):
            evidence["manifest"] = manifest["files"]
            evidence["issues"] = list(self.issues)
        return report

    def _write_report_file(self, name: str, body: str, kind: str) -> bool:
        encoded = body.encode()
        try:
            path = self._write(name, encoded)
        except (EvidenceError, OSError) as exc:
            self._issue(getattr(exc, "code", "report_write_failed"), name)
            return False
        digest = hashlib.sha256(encoded).hexdigest()
        self.entries.append(_Entry(kind, path, len(encoded), digest))
        return True

    def _manifest(self) -> dict[str, object]:
        return {
            "contract": EVIDENCE_CONTRACT,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "profile_id": self.profile_id,
            "directory": self.run_id,
            "files": [
                {
                    "kind": entry.kind,
                    "file": entry.file.relative_to(self.root).as_posix(),
                    "byte_length": entry.size,
                    "sha256": entry.digest,
                    "step_id": entry.step_id,
                    "source": entry.source,
                }
                for entry in self.entries
            ],
            "issues": list(self.issues),
        }


def _rows(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def build_report(
    result: Mapping[str, object],
    *,
    run_id: str,
    case_id: str,
    profile_id: str,
    suite_id: str | None,
    environment: Mapping[str, object],
    metadata: Mapping[str, object],
    manifest: Mapping[str, object],
    issues: list[dict[str, str]],
    secrets: tuple[str, ...] = (),
    started_at_ms: int,
    finished_at_ms: int,
    elapsed_ms: float,
) -> dict[str, object]:
    """Report structure: run facts, environment, per-item expected/actual/source, evidence."""
    report: dict[str, object] = {
        "contract": REPORT_CONTRACT,
        "run": {
            "run_id": run_id,
            "case_id": case_id,
            "profile_id": profile_id,
            "suite_id": suite_id,
            "status": result.get("status"),
            "reason": result.get("reason"),
            "business_success": result.get("business_success"),
            "started_at_ms": started_at_ms,
            "finished_at_ms": finished_at_ms,
            "elapsed_ms": round(elapsed_ms, 3),
        },
        "environment": {
            **_strip(environment, secrets),
            "runtime_time_zone": os.environ.get("TZ") or "local",
        },
        "project": _strip(metadata, secrets),
        "preconditions": _strip_rows(result.get("preconditions"), secrets),
        "steps": _strip_rows(result.get("steps"), secrets),
        "checks": _strip_rows(result.get("checks"), secrets),
        "cleanup": _strip(result.get("cleanup"), secrets)
        if isinstance(result.get("cleanup"), Mapping)
        else {},
        "preflight_issues": _strip_rows(result.get("preflight_issues"), secrets),
    }
    report["evidence"] = {
        "manifest": _strip_rows(manifest.get("files"), secrets),
        "issues": _strip_rows(issues, secrets),
        "outputs": {
            "json": "result.json",
            "html": "result.html",
            "manifest": "attachments.json",
        },
    }
    return _strip(report, secrets)


def _strip_rows(value: object, secrets: tuple[str, ...]) -> list[object]:
    return [_strip(item, secrets) for item in _rows(value)]


def _short(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value if len(value) <= 160 else value[:157] + "..."
    if isinstance(value, Mapping | list):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return rendered if len(rendered) <= 160 else rendered[:157] + "..."
    return str(value)


_CSS = """
:root{color-scheme:light dark;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
body{margin:0;padding:2rem;max-width:72rem;line-height:1.5}
h1{font-size:1.4rem;margin:0 0 .5rem}h2{font-size:1.1rem;margin:2rem 0 .5rem}
table{border-collapse:collapse;width:100%;font-size:.875rem}
th,td{border:1px solid #8884;padding:.35rem .5rem;text-align:left;vertical-align:top}
th{background:#8881}code{white-space:pre-wrap;word-break:break-word}
.badge{display:inline-block;padding:0 .4rem;border-radius:.25rem;
  border:1px solid currentColor;font-size:.8rem}
details>summary{cursor:pointer}
"""


def render_html(report: Mapping[str, object]) -> str:
    """Self-contained report: inline CSS only, no scripts, no external references."""
    run = report.get("run")
    run = run if isinstance(run, Mapping) else {}
    environment = report.get("environment")
    environment = environment if isinstance(environment, Mapping) else {}
    parts: list[str] = []
    title = html.escape(f"Pulse report {run.get('run_id')}")
    parts.append(
        f'<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>{_CSS}</style></head><body>"
        f"<h1>{title}</h1>"
    )
    parts.append(
        '<p><span class="badge">status</span> '
        f"{html.escape(_short(run.get('status')))} "
        f'<span class="badge">reason</span> {html.escape(_short(run.get("reason")))} '
        f'<span class="badge">case</span> {html.escape(_short(run.get("case_id")))} '
        f'<span class="badge">profile</span> {html.escape(_short(run.get("profile_id")))}</p>'
    )
    parts.append("<h2>运行</h2><table><tbody>")
    for key in (
        "run_id",
        "suite_id",
        "started_at_ms",
        "finished_at_ms",
        "elapsed_ms",
        "business_success",
    ):
        parts.append(f"<tr><th>{key}</th><td>{html.escape(_short(run.get(key)))}</td></tr>")
    parts.append("</tbody></table>")

    parts.append("<h2>环境</h2><table><tbody>")
    flat: list[tuple[str, object]] = []
    for key, value in environment.items():
        if isinstance(value, Mapping):
            flat.extend((f"{key}.{inner}", item) for inner, item in value.items())
        else:
            flat.append((str(key), value))
    for key, value in flat:
        parts.append(f"<tr><th>{html.escape(key)}</th><td>{html.escape(_short(value))}</td></tr>")
    parts.append("</tbody></table>")

    project = report.get("project")
    if isinstance(project, Mapping) and project:
        parts.append("<h2>工程</h2><table><tbody>")
        for key, value in project.items():
            parts.append(
                f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(_short(value))}</td></tr>"
            )
        parts.append("</tbody></table>")

    for section, key in (
        ("前置条件", "preconditions"),
        ("检查", "checks"),
        ("步骤", "steps"),
    ):
        rows = report.get(key)
        rows = rows if isinstance(rows, list) else []
        parts.append(f"<h2>{section} [{len(rows)}]</h2>")
        if not rows:
            parts.append("<p>—</p>")
            continue
        parts.append(
            "<table><thead><tr><th>id</th><th>status</th><th>expected</th>"
            "<th>actual</th><th>source</th><th>detail</th></tr></thead><tbody>"
        )
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            status = row.get("status")
            parts.append(
                f"<tr><td>{html.escape(_short(row.get('id')))}</td>"
                f'<td><span class="badge">{html.escape(_short(status))}</span></td>'
                f"<td><code>{html.escape(_short(row.get('expected')))}</code></td>"
                f"<td><code>{html.escape(_short(row.get('actual')))}</code></td>"
                f"<td>{html.escape(_short(row.get('source')))}</td>"
                f"<td><code>{html.escape(_short(row.get('detail')))}</code></td></tr>"
            )
        parts.append("</tbody></table>")

    issues = report.get("preflight_issues")
    issues = issues if isinstance(issues, list) else []
    if issues:
        parts.append("<h2>预检问题</h2><table><tbody>")
        for issue in issues:
            parts.append(f"<tr><td><code>{html.escape(_short(issue))}</code></td></tr>")
        parts.append("</tbody></table>")

    evidence = report.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    files = evidence.get("manifest")
    files = files if isinstance(files, list) else []
    parts.append(f"<h2>证据附件 [{len(files)}]</h2>")
    if files:
        parts.append(
            "<table><thead><tr><th>kind</th><th>file</th><th>bytes</th><th>sha256</th>"
            "<th>step</th></tr></thead><tbody>"
        )
        for item in files:
            if not isinstance(item, Mapping):
                continue
            parts.append(
                f"<tr><td>{html.escape(_short(item.get('kind')))}</td>"
                f"<td><code>{html.escape(_short(item.get('file')))}</code></td>"
                f"<td>{html.escape(_short(item.get('byte_length')))}</td>"
                f"<td><code>{html.escape(_short(item.get('sha256')))}</code></td>"
                f"<td>{html.escape(_short(item.get('step_id')))}</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<p>—</p>")
    evidence_issues = evidence.get("issues")
    evidence_issues = evidence_issues if isinstance(evidence_issues, list) else []
    if evidence_issues:
        parts.append("<h2>证据问题</h2><table><tbody>")
        for issue in evidence_issues:
            parts.append(f"<tr><td><code>{html.escape(_short(issue))}</code></td></tr>")
        parts.append("</tbody></table>")
    raw = json.dumps(report, ensure_ascii=False, indent=2)
    parts.append(
        "<h2>原始 JSON</h2><details><summary>展开 result.json 内容</summary>"
        f"<pre><code>{html.escape(raw)}</code></pre></details>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


__all__ = [
    "CHECK_OPERATORS",
    "EVIDENCE_CONTRACT",
    "MAX_ATTACHMENTS",
    "MAX_PNG_BYTES",
    "MAX_SNAPSHOTS",
    "REDACTED",
    "REPORT_CONTRACT",
    "EvidenceError",
    "EvidenceRecorder",
    "build_report",
    "check_evidence",
    "decode_png_payload",
    "expectation",
    "render_html",
]


def decode_png_payload(base64_png: object, *, name: object) -> dict[str, object]:
    """Strictly decode one PNG attachment payload; never infer a file name or bytes."""
    if _safe_name(name) is None or type(base64_png) is not str or not base64_png:
        raise EvidenceError("screenshot_invalid")
    try:
        content = base64.b64decode(base64_png, validate=True)
    except (ValueError, binascii.Error):
        raise EvidenceError("screenshot_invalid") from None
    if not content or content[:8] != _PNG_MAGIC or len(content) > MAX_PNG_BYTES:
        raise EvidenceError("screenshot_invalid")
    if base64.b64encode(content).decode("ascii") != base64_png:
        raise EvidenceError("screenshot_invalid")
    return {
        "name": name,
        "content": content,
        "byte_length": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
