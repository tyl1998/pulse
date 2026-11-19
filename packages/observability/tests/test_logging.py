import asyncio
import io
import json
import logging
import math
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

import pytest

from pulse_observability import (
    DurationStats,
    JsonFormatter,
    bind_context,
    configure_logging,
    current_context,
    metrics_interval,
    new_trace_id,
    redact,
    redact_text,
)


@pytest.fixture
def output() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("runtime"))
    logger = logging.Logger("test.observability", logging.DEBUG)
    logger.addHandler(handler)
    return logger, stream


@pytest.fixture
def logging_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    loggers = [
        logging.getLogger(),
        *[
            logging.getLogger(name)
            for name in (
                "uvicorn",
                "uvicorn.error",
                "uvicorn.access",
                "websockets",
                "websockets.server",
            )
        ],
    ]
    saved = [
        (logger, logger.handlers[:], logger.level, logger.propagate, logger.disabled)
        for logger in loggers
    ]
    raised = logging.raiseExceptions
    hooks = sys.excepthook, threading.excepthook
    for name in (
        "PULSE_LOG_LEVEL",
        "PULSE_LOG_STDERR",
        "PULSE_LOG_MAX_BYTES",
        "PULSE_LOG_BACKUP_COUNT",
    ):
        monkeypatch.delenv(name, raising=False)
    try:
        yield
    finally:
        for logger, handlers, level, propagate, disabled in saved:
            for handler in logger.handlers:
                if handler not in handlers:
                    handler.close()
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled
        logging.raiseExceptions = raised
        sys.excepthook, threading.excepthook = hooks


def test_json_contract_and_units(output: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = output
    with bind_context(trace_id="a" * 32, serial="device-1", session_id="session-1"):
        logger.info("finished", extra={"event": "stream.ready", "duration_ms": 1.25, "frames": 2})
    value = json.loads(stream.getvalue())
    assert value["schema_version"] == 1
    assert value["timestamp"].endswith("Z") and len(value["timestamp"]) == 24
    assert value["level"] == "INFO"
    assert value["component"] == "runtime"
    assert value["logger"] == "test.observability"
    assert value["event"] == "stream.ready"
    assert value["pid"] == os.getpid()
    assert value["duration_ms"] == 1.25
    assert value["trace_id"] == "a" * 32 and value["serial"] == "device-1"
    assert value["run_id"] is None and value["channel"] is None
    assert value["session_id"] == "session-1"
    assert value["error_category"] is None and value["exception"] is None
    assert value["data"] == {"frames": 2}


async def test_concurrent_context_isolation_and_exception_reset(
    output: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = output
    entered = [asyncio.Event(), asyncio.Event()]

    async def request(index: int) -> None:
        with bind_context(
            trace_id=str(index + 1) * 32, serial=f"S{index}", session_id=f"C{index}", run_id=None
        ):
            entered[index].set()
            await entered[1 - index].wait()
            await asyncio.to_thread(logger.info, "thread")
            with pytest.raises(ValueError), bind_context(channel=f"CH{index}"):
                logger.info("nested")
                raise ValueError("test scope exit")
            logger.info("restored")

    await asyncio.gather(request(0), request(1))
    assert current_context() == {}
    entries = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(entries) == 6
    for entry in entries:
        index = int(entry["trace_id"][0]) - 1
        assert entry["serial"] == f"S{index}"
        assert entry["session_id"] == f"C{index}"
        assert entry["run_id"] is None
        assert entry["channel"] == (f"CH{index}" if entry["message"] == "nested" else None)


@pytest.mark.parametrize(
    "text",
    [
        "Bearer hidden-token",
        "bEaReR hidden-token",
        "Authorization: Bearer hidden-token",
        "Authorization: Basic hidden-token",
        '{"Authorization": "Bearer hidden-token"}',
        "{'headers': {'Proxy-Authorization': 'Bearer hidden-token'}}",
        "failed wss://localhost/ws?TOKEN=hidden-token&other=1",
        '{"url": "ws://localhost/ws?token=hidden-token&token=hidden-token"}',
        "ws://localhost/ws?%74%6f%6b%65%6e=hidden-token",
        "ws%3A%2F%2Flocalhost%2Fws%3Ftoken%3Dhidden-token",
        "ws://user:hidden-token@localhost/ws",
        "{'nested': [{'client_SECRET': 'hidden-token'}]}",
        "X-Api-Key: hidden-token",
        "Cookie: first=hidden-token; second=hidden-token",
        "--token hidden-token --host localhost",
        "-----BEGIN PRIVATE KEY-----\nhidden-token\n-----END PRIVATE KEY-----",
        r"failed {\"token\":\"hidden-token\"}",
        'malformed {"password": "hidden-token',
        "Password: hidden-token with spaces",
        "ws://example/ws?token=prefix%26hidden-token&trace_id=public",
        "ws://user:prefix%2Fhidden-token@example/ws",
        "ws://example/ws?token=prefix%22hidden-token%3Dsuffix",
        "ws%3A%2F%2Fexample%2Fws%3Ftoken%3Dprefix%2526hidden-token",
        r'{"password": "prefix\"hidden-token"}',
        r"failed {\"token\":\"prefix\\\"hidden-token\"}",
        '{"credentials": {"value": "hidden-token"}, "after": 1}',
        '{"token": ["hidden-token"], "after": 1}',
        "error {'Credentials': {'items': [('value', 'hidden-token')]}}",
        'credentials={\n"unlabelled": "hidden-token"\n}',
    ],
)
def test_string_redaction_is_idempotent(
    text: str, output: tuple[logging.Logger, io.StringIO]
) -> None:
    safe = redact_text(text)
    assert "hidden-token" not in unquote(safe)
    assert "[REDACTED]" in unquote(safe)
    assert redact_text(safe) == safe
    logger, stream = output
    try:
        raise RuntimeError(text)
    except RuntimeError:
        logger.exception("third-party failure")
    entry = json.loads(stream.getvalue())
    assert "hidden-token" not in unquote(stream.getvalue())
    assert entry["exception"]["type"] == "RuntimeError"
    assert "Traceback" in entry["exception"]["stack"]


def test_nested_extras_bytes_and_nonfinite_values(
    output: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = output
    secret = "hidden-token"
    value = {
        "Token": secret,
        "headers": {"AUTHORIZATION": f"Bearer {secret}"},
        "list": [{"api_KEY": secret}],
    }
    logger.info(
        "args=%s",
        value,
        extra={
            "credentials": {"not_a_known_key": secret},
            "body": {"page": "private phone content"},
            "binary": b"private phone content",
            "metric": math.nan,
            "duration_ms": -1,
        },
    )
    serialized = stream.getvalue()
    assert secret not in serialized and "private phone content" not in serialized
    parsed = json.loads(serialized)
    assert parsed["data"]["metric"] is None and parsed["duration_ms"] is None
    assert value["Token"] == secret  # no mutation of credentials passed to real clients
    assert redact([("Cookie", "unlabelled-secret")]) == [["Cookie", "[REDACTED]"]]
    cycle: list[object] = []
    cycle.append(cycle)
    assert "DEPTH LIMIT" in json.dumps(redact(cycle))


def test_exception_stack_and_classification_are_redacted(
    output: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = output
    secret = "hidden-token"
    try:
        raise ValueError(f"failed ws://localhost/ws?token={secret}")
    except ValueError:
        logger.exception(
            "connection failed", extra={"event": "ws.failed", "error_category": "transport"}
        )
    value = json.loads(stream.getvalue())
    assert secret not in stream.getvalue()
    assert value["exception"]["type"] == "ValueError"
    assert "Traceback" in value["exception"]["stack"]
    assert "test_logging.py" in value["exception"]["stack"]
    assert value["error_category"] == "transport"


def test_bad_logging_format_does_not_dump_original_args(
    output: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = output
    logger.info("%d %d", "Bearer hidden-token")
    assert json.loads(stream.getvalue())["message"] == "[UNFORMATTABLE LOG MESSAGE]"
    assert "hidden-token" not in stream.getvalue()


def test_third_party_handlers_use_common_redacted_sink(
    tmp_path: Path, logging_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    bypass = io.StringIO()
    third_party = logging.getLogger("uvicorn.error")
    third_party.addHandler(logging.StreamHandler(bypass))
    third_party.propagate = False
    monkeypatch.setenv("PULSE_LOG_LEVEL", "DEBUG")
    configure_logging("runtime", tmp_path)
    logging.getLogger("uvicorn.access").info(
        '%s "%s %s HTTP/1.1" %s', "127.0.0.1", "GET", "/ws?token=hidden-token", 101
    )
    third_party.info("WebSocket /ws?ToKeN=hidden-token [accepted]")
    logging.getLogger("websockets.server").warning("Authorization: Bearer hidden-token")
    third_party.setLevel(logging.DEBUG)  # the sink also prevents accidentally re-enabled dumps
    third_party.debug("BINARY private phone content")
    logging.getLogger("websockets.server").debug("TEXT private phone content")
    raw = (tmp_path / "runtime.log").read_text()
    entries = [json.loads(line) for line in raw.splitlines()]
    assert len(entries) == 3
    assert {entry["component"] for entry in entries} == {"runtime"}
    assert "hidden-token" not in raw and "private phone content" not in raw
    assert bypass.getvalue() == ""


def test_file_rotation_retention_permissions_and_no_duplicate_console(
    tmp_path: Path, logging_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    console = io.StringIO()
    monkeypatch.setattr(sys, "stderr", console)
    monkeypatch.setenv("PULSE_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("PULSE_LOG_BACKUP_COUNT", "2")
    (tmp_path / "runtime.log").write_text("legacy text\n")
    configure_logging("runtime", tmp_path)
    logger = logging.getLogger("rotation")
    for i in range(30):
        logger.info("entry %s", i)
    paths = sorted(tmp_path.glob("runtime.log*"))
    assert len(paths) == 3
    for path in paths:
        assert path.stat().st_size < 1024
        assert all(
            json.loads(line)["component"] == "runtime" for line in path.read_text().splitlines()
        )
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
    assert console.getvalue() == ""
    configure_logging("runtime", tmp_path)
    logger.info("one copy")
    assert len((tmp_path / "runtime.log").read_text().splitlines()) == 1
    assert len(logging.getLogger().handlers) == 1


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PULSE_LOG_LEVEL", "TRACE"),
        ("PULSE_LOG_MAX_BYTES", "0"),
        ("PULSE_LOG_BACKUP_COUNT", "0"),
        ("PULSE_LOG_STDERR", "maybe"),
    ],
)
def test_invalid_lifecycle_config_rejected(
    name: str, value: str, tmp_path: Path, logging_state: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        configure_logging("runtime", tmp_path)


def test_trace_validation_and_duration_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    assert new_trace_id("A" * 32) == "a" * 32
    for candidate in (None, "bad", "0" * 32, "x" * 200, "a" * 32 + "\n"):
        trace = new_trace_id(candidate)
        assert len(trace) == 32 and int(trace, 16) > 0 and trace != candidate
    stats = DurationStats()
    assert stats.snapshot() == {"count": 0, "total_ms": 0.0, "max_ms": None, "last_ms": None}
    for duration in (0.5, 1.25, math.nan, -1, math.inf):
        stats.observe(duration)
    assert stats.snapshot() == {"count": 2, "total_ms": 1.75, "max_ms": 1.25, "last_ms": 1.25}
    monkeypatch.setenv("PULSE_METRICS_INTERVAL_S", "30")
    assert metrics_interval() == 30.0
    monkeypatch.setenv("PULSE_METRICS_INTERVAL_S", "0")
    with pytest.raises(ValueError):
        metrics_interval()


def test_unhandled_exception_uses_redacted_file_sink(tmp_path: Path, logging_state: None) -> None:
    configure_logging("runtime", tmp_path)
    try:
        raise RuntimeError("Bearer hidden-token")
    except RuntimeError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    raw = (tmp_path / "runtime.log").read_text()
    assert "hidden-token" not in raw
    entry = json.loads(raw)
    assert entry["event"] == "process.failed"
    assert entry["exception"]["type"] == "RuntimeError"
