"""Service-independent JSON logs. No request bodies, frame payloads, or engine semantics."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from urllib.parse import quote, unquote

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

CONTEXT_FIELDS = ("trace_id", "run_id", "serial", "session_id", "channel")
_context: ContextVar[dict[str, str | None] | None] = ContextVar("pulse_log_context", default=None)
_REDACTED = "[REDACTED]"
_OMITTED = "[CONTENT OMITTED]"
_STANDARD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_RESERVED_FIELDS = frozenset(CONTEXT_FIELDS) | {
    "event",
    "duration_ms",
    "error_category",
    "_pulse_context",
}
_KEY_PREFIX = re.compile(
    r"(?<![A-Za-z0-9_%.-])(?P<quote>\\*[\"']|)(?P<key>[A-Za-z%][A-Za-z0-9_%.-]*)"
    r"(?P=quote)\s*[:=]\s*"
)
_QUOTED_START = re.compile(r"(?P<slashes>\\*)(?P<quote>[\"'])")
_AUTH = re.compile(
    r"(?<![A-Za-z])((?:Bearer|Basic)(?:\s|%(?:25)*(?:20|09|0a|0d))+)"
    r"(?:\[REDACTED\]|[^\s,;\"'<>}\])]+)",
    re.IGNORECASE,
)
_QUERY_VALUE = re.compile(r"([?&])([^=&#\s\"'<>]+)=([^&#\s\"'<>]*)")
_ENCODED_URL = re.compile(
    r"\b(?:https?|wss?)%(?:25)*3a%(?:25)*2f%(?:25)*2f[^\s\"'<>]+", re.IGNORECASE
)
_USERINFO = re.compile(r"(\b(?:https?|wss?)://)[^\s/@]+@", re.IGNORECASE)
_CLI_SECRET = re.compile(
    r"(--(?:token|password|api[-_]key|secret|client[-_]secret)(?:\s+|=))"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;)]+)",
    re.IGNORECASE,
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    re.DOTALL,
)


def current_context() -> dict[str, str | None]:
    return dict(_context.get() or {})


@contextmanager
def bind_context(**values: str | None) -> Iterator[None]:
    """Copy, never mutate, a task-local scope; reset even on exceptions/cancellation."""
    if values.keys() - set(CONTEXT_FIELDS):
        raise ValueError("unknown logging context field")
    token = _context.set({**current_context(), **values})
    try:
        yield
    finally:
        _context.reset(token)


def new_trace_id(candidate: str | None = None) -> str:
    """Untrusted correlation only, never an authentication or authorization decision."""
    if candidate and re.fullmatch(r"[0-9a-fA-F]{32}", candidate) and int(candidate, 16):
        return candidate.lower()
    return secrets.token_hex(16)


def _key_name(key: str) -> str:
    for _ in range(8):
        decoded = unquote(key)
        if decoded == key:
            break
        key = decoded
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _sensitive(key: str) -> bool:
    key = _key_name(key)
    return key in {
        "auth",
        "authentication",
        "tokens",
        "secrets",
        "passwords",
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "pwd",
        "signature",
        "passphrase",
        "clientassertion",
    } or key.endswith(
        (
            "token",
            "password",
            "passwd",
            "secret",
            "apikey",
            "accesskey",
            "accesskeyid",
            "secretkey",
            "privatekey",
            "credential",
            "credentials",
            "authorization",
            "cookie",
        )
    )


def _quoted_end(text: str, opening: re.Match[str]) -> int:
    # Preserve escape boundaries, including JSON strings embedded in another JSON string.
    escaped = len(opening["slashes"])
    slashes = 0
    for i in range(opening.end(), len(text)):
        if text[i] == "\\":
            slashes += 1
            continue
        if text[i] == opening["quote"] and slashes % (2 * (escaped + 1)) == escaped:
            return i + 1
        slashes = 0
    return len(text)


def _value_end(text: str, start: int, *, header: bool) -> int:
    quoted = _QUOTED_START.match(text, start)
    if quoted:
        return _quoted_end(text, quoted)
    closers = {"{": "}", "[": "]", "(": ")"}
    stack: list[str] = []
    if start < len(text) and text[start] in closers:
        stack.append(closers[text[start]])
        i = start + 1
        while i < len(text):
            quoted = _QUOTED_START.match(text, i)
            if quoted:
                i = _quoted_end(text, quoted)
                continue
            char = text[i]
            if char in closers:
                stack.append(closers[char])
            elif char in "}])":
                if char != stack.pop():
                    return len(text)  # Malformed credential containers fail closed.
                if not stack:
                    return i + 1
            i += 1
        return len(text)
    stop = "\r\n{}[]()<>\"'" + ("" if header else ",;&")
    end = start
    while end < len(text) and text[end] not in stop:
        end += 1
    return end


def _redact_text(text: str, depth: int) -> str:
    def encoded_url(match: re.Match[str]) -> str:
        if depth >= 8:
            return _REDACTED
        decoded = unquote(match[0])
        safe = _redact_text(decoded, depth + 1)
        return quote(safe, safe="") if safe != decoded else match[0]

    text = _ENCODED_URL.sub(encoded_url, text)
    # Only decode query *names*. Decoding %26/%2F/%22 inside a credential before masking it
    # changes its boundaries and leaks the suffix (or the entire URL userinfo).
    text = _QUERY_VALUE.sub(
        lambda m: m[1] + m[2] + "=" + _REDACTED if _sensitive(m[2]) else m[0], text
    )

    text = _PRIVATE_KEY.sub(_REDACTED, text)
    text = _USERINFO.sub(r"\1[REDACTED]@", text)
    parts: list[str] = []
    cursor = 0
    for match in _KEY_PREFIX.finditer(text):
        if match.start() < cursor or not _sensitive(match["key"]):
            continue
        start = match.end()
        end = _value_end(
            text, start, header=_key_name(match["key"]).endswith(("cookie", "authorization"))
        )
        opening = _QUOTED_START.match(text, start)
        marker = opening[0] if opening else ""
        parts.extend((text[cursor:start], marker + _REDACTED + marker))
        cursor = end
    text = "".join(parts) + text[cursor:]
    text = _AUTH.sub(r"\1[REDACTED]", text)
    text = _CLI_SECRET.sub(r"\1[REDACTED]", text)
    return text


def redact_text(text: str, *, limit: int | None = 8192) -> str:
    text = _redact_text(text, 0)
    if limit is None:
        return text
    return text[:limit] + ("[TRUNCATED]" if len(text) > limit else "")


def redact(value: object, *, _depth: int = 0) -> JsonValue:
    """Bounded JSON-safe copy, also for third-party args/extra; unknown objects aren't repr'd."""
    if _depth > 8:
        return "[DEPTH LIMIT]"
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return _OMITTED
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= 64:
                result["_truncated"] = True
                break
            name = key if isinstance(key, str) else type(key).__name__
            normalized = re.sub(r"[^a-z]", "", name.lower())
            if _sensitive(name):
                result[name] = _REDACTED
            elif normalized in {
                "body",
                "requestbody",
                "responsebody",
                "payload",
                "videodata",
                "framedata",
                "screenshot",
                "pagecontent",
                "elements",
            }:
                result[name] = _OMITTED
            else:
                result[redact_text(name)] = redact(item, _depth=_depth + 1)
        return result
    if isinstance(value, list | tuple):
        if len(value) == 2 and isinstance(value[0], str) and _sensitive(value[0]):
            return [value[0], _REDACTED]
        return [redact(item, _depth=_depth + 1) for item in value[:64]]
    if isinstance(value, BaseException):
        return redact_text(str(value))
    return f"<{type(value).__name__}>"


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        context = record.__dict__.setdefault("_pulse_context", current_context())
        try:
            if isinstance(record.msg, str):
                args = record.args
                safe_args = (
                    tuple(redact(arg) for arg in args) if isinstance(args, tuple) else redact(args)
                )
                message = record.msg % safe_args if args else record.msg
            else:
                message = json.dumps(redact(record.msg))
        except (TypeError, ValueError, KeyError):
            # Logging's own fallback prints original args to stderr. Never use it for bad formats.
            message = "[UNFORMATTABLE LOG MESSAGE]"
        duration = record.__dict__.get("duration_ms")
        if (
            not isinstance(duration, int | float)
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or duration < 0
        ):
            duration = None
        event = record.__dict__.get("event", "log")
        if not isinstance(event, str) or not re.fullmatch(r"[a-z][a-z0-9_.]*", event):
            event = "log"
        data = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_FIELDS | _RESERVED_FIELDS
        }
        document: dict[str, object] = {
            "schema_version": 1,
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "component": self.component,
            "event": event,
            "logger": record.name,
            "pid": record.process,
            "message": message,
            "duration_ms": duration,
            "error_category": record.__dict__.get("error_category"),
            "exception": None,
            "data": data,
        }
        for name in CONTEXT_FIELDS:
            value = record.__dict__.get(name, context.get(name))
            document[name] = value if isinstance(value, str) else None
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            document["error_category"] = document["error_category"] or "internal"
            document["exception"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "stack": "".join(traceback.format_exception(*record.exc_info)),
            }
        elif record.exc_text:
            document["exception"] = {"stack": record.exc_text}
        if record.stack_info:
            data["stack"] = record.stack_info
        return json.dumps(
            redact(document), ensure_ascii=True, allow_nan=False, separators=(",", ":")
        )


class _TransportFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # websockets uses uvicorn.error too. DEBUG/TRACE dump headers and frame contents.
        return not (
            record.name.startswith(("uvicorn", "websockets")) and record.levelno < logging.INFO
        )


class _PrivateRotatingHandler(RotatingFileHandler):
    def _open(self) -> TextIOWrapper:
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        return TextIOWrapper(os.fdopen(fd, "ab"), encoding="utf-8")


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if value > 0:
            return value
    except ValueError:
        pass
    raise ValueError(f"{name} must be a positive integer")


def metrics_interval() -> float:
    return float(_positive_int("PULSE_METRICS_INTERVAL_S", 30))


def _unhandled(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    logging.getLogger("pulse").critical(
        "Unhandled exception",
        exc_info=(exc_type, exc, tb),
        extra={"event": "process.failed", "error_category": "internal"},
    )


def configure_logging(
    component: str, directory: Path | None = None, *, stderr: bool | None = None
) -> None:
    """Entrypoint-owned configuration. File sink is single-writer and size/startup rotated."""
    level = os.environ.get("PULSE_LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("PULSE_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    max_bytes = _positive_int("PULSE_LOG_MAX_BYTES", 5 * 1024 * 1024)
    backups = _positive_int("PULSE_LOG_BACKUP_COUNT", 3)
    stderr_value = (
        str(int(stderr))
        if stderr is not None
        else os.environ.get("PULSE_LOG_STDERR", "0" if directory else "1")
    )
    if stderr_value not in {"0", "1"} or (directory is None and stderr_value == "0"):
        raise ValueError("PULSE_LOG_STDERR must be 0 or 1, and requires a file sink when 0")
    handlers: list[logging.Handler] = []
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)
        file_handler = _PrivateRotatingHandler(
            directory / f"{component}.log",
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        if Path(file_handler.baseFilename).stat().st_size:
            # Archive legacy text too; each new active file is JSON lines only.
            file_handler.doRollover()
        handlers.append(file_handler)
    if stderr_value == "1" and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    formatter = JsonFormatter(component)
    for handler in handlers:
        handler.setLevel(level)
        handler.setFormatter(formatter)
        handler.addFilter(_TransportFilter())
    root = logging.getLogger()
    for old in root.handlers[:]:
        root.removeHandler(old)
        old.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    # Remove Uvicorn's default console handlers, including already-created children. Do not let
    # a propagation=False logger bypass the common final sink or re-enable wire dumps at DEBUG.
    names = set(logging.Logger.manager.loggerDict) | {
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "websockets",
    }
    for name in names:
        if name.startswith(("uvicorn", "websockets")):
            logger = logging.getLogger(name)
            for old in logger.handlers[:]:
                logger.removeHandler(old)
                old.close()
            logger.propagate = True
            logger.disabled = False
            logger.setLevel(max(logging.INFO, logging.getLevelNamesMapping()[level]))
    # stdlib handler failures must not dump unredacted original args to stderr.
    logging.raiseExceptions = False
    sys.excepthook = _unhandled

    def thread_exception(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            _unhandled(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = thread_exception


@dataclass
class DurationStats:
    """Cumulative monotonic-clock durations, O(1) memory. Not a percentile estimator."""

    count: int = 0
    total_ms: float = 0.0
    max_ms: float | None = None
    last_ms: float | None = None

    def observe(self, duration_ms: float) -> None:
        if not math.isfinite(duration_ms) or duration_ms < 0:
            return
        self.count += 1
        self.total_ms += duration_ms
        self.max_ms = max(self.max_ms or 0.0, duration_ms)
        self.last_ms = duration_ms

    def snapshot(self) -> dict[str, int | float | None]:
        return {
            "count": self.count,
            "total_ms": self.total_ms,
            "max_ms": self.max_ms,
            "last_ms": self.last_ms,
        }
