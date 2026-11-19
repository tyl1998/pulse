import asyncio
import hashlib
import importlib.util
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from pulse_runtime.drivers.android import agent_transport as at
from pulse_runtime.drivers.android.scrcpy_video import SourceError


@pytest.fixture(autouse=True)
def no_physical_adb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(at, "adb", AsyncMock(side_effect=AssertionError("physical ADB forbidden")))
    monkeypatch.setattr(at, "find_adb", lambda: "fake-adb")


def health_body() -> dict[str, object]:
    return {
        "protocol_version": 1,
        "request_id": str(uuid4()),
        "session_id": str(uuid4()),
        "operation": "health",
        "success": True,
        "terminal": True,
        "result": {
            "status": "ok",
            "agent_version": "0.1.0",
            "capabilities": {
                "accessibility": "available",
                "screenshot": "available",
                "ocr": "available",
                "ocr_engine": "mlkit_chinese_bundled",
                "ocr_languages": ["zh", "en"],
                "actions": ["tap", "swipe", "set_text", "node_click"],
                "platform_operation_pending": False,
                "recovery_required": False,
                "minimum_android_api": 30,
                "display_scope": "default_only",
                "error_log_session_id": str(uuid4()),
            },
        },
        "error": None,
    }


def test_health_parser_returns_only_typed_capabilities() -> None:
    body = health_body()
    body["private_input"] = "do-not-store-this"
    body["result"]["capabilities"]["raw_sensitive_output"] = "do-not-store-this"
    health = at.parse_health(json.dumps(body).encode())
    assert health.capabilities["accessibility"] == "available"
    assert health.lifecycle_id == body["result"]["capabilities"]["error_log_session_id"]
    assert "do-not-store-this" not in repr(health)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("protocol_version", 2, "wire_incompatible"),
        ("protocol_version", True, "wire_incompatible"),
        ("terminal", False, "health_rejected"),
        ("terminal", 1, "health_rejected"),
        ("success", 1, "health_rejected"),
        ("operation", "observe", "invalid_message"),
        ("session_id", "not-a-uuid", "invalid_message"),
        ("request_id", None, "invalid_message"),
        ("result", None, "invalid_message"),
        ("error", {"code": "raw-sensitive-output"}, "invalid_message"),
    ],
)
def test_health_parser_rejects_invalid_envelopes(field: str, value: object, code: str) -> None:
    body = health_body()
    body[field] = value
    with pytest.raises(at.ConnectionFault, match=code):
        at.parse_health(json.dumps(body).encode())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("accessibility", True),
        ("accessibility", []),
        ("screenshot", "failed"),
        ("ocr", None),
        ("recovery_required", 0),
        ("platform_operation_pending", "false"),
        ("minimum_android_api", True),
        ("minimum_android_api", 29),
        ("display_scope", "all_displays"),
        ("ocr_engine", "untrusted"),
        ("actions", ["shell"]),
        ("actions", ["tap", "tap"]),
        ("actions", [{}]),
        ("ocr_languages", ["private-input"]),
        ("error_log_session_id", "UPPERCASE-INVALID"),
    ],
)
def test_health_parser_rejects_invalid_capabilities(field: str, value: object) -> None:
    body = health_body()
    body["result"]["capabilities"][field] = value
    with pytest.raises(at.ConnectionFault, match="invalid_message"):
        at.parse_health(json.dumps(body).encode())


@pytest.mark.parametrize("code", ["action_uncertain", "recovery_required", "private-secret", {}])
def test_health_error_keeps_only_quarantine_categories(code: object) -> None:
    body = health_body()
    body["success"] = False
    body["error"] = {"code": code}
    with pytest.raises(at.ConnectionFault) as caught:
        at.parse_health(json.dumps(body).encode())
    assert caught.value.code == (
        code
        if isinstance(code, str) and code in {"action_uncertain", "recovery_required"}
        else "health_rejected"
    )
    assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "data", [b"{}{}", b"[]", b"\xff", b'{"protocol_version":1,"protocol_version":1}']
)
def test_health_rejects_invalid_utf8_duplicate_json_and_trailing_documents(data: bytes) -> None:
    with pytest.raises(at.ConnectionFault):
        at.parse_health(data)


@pytest.mark.parametrize("serial", ["", "../device", "A B", "A\nB", "A;id", "x" * 65])
async def test_no_implicit_or_injected_serial(serial: str, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = AsyncMock()
    monkeypatch.setattr(at, "adb", runner)
    with pytest.raises(at.ConnectionFault, match="invalid_serial"):
        await at.AndroidBackend().command(serial, "get-state")
    runner.assert_not_called()


async def test_two_serial_commands_reuse_explicit_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = AsyncMock(return_value="device")
    monkeypatch.setattr(at, "adb", runner)
    backend = at.AndroidBackend()
    await asyncio.gather(backend.command("S1", "get-state"), backend.command("S2", "get-state"))
    assert {call.args for call in runner.call_args_list} == {
        ("S1", "get-state"),
        ("S2", "get-state"),
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("device unauthorized: Bearer private-token", "adb_unauthorized"),
        ("device offline: private-input", "adb_offline"),
        ("timed out: raw-sensitive-output", "timeout"),
        ("device not found: raw-sensitive-output", "device_disconnected"),
        ("unknown failure: Bearer private-token", "adb_command_failed"),
    ],
)
async def test_command_errors_never_expose_original_output(
    raw: str, expected: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(at, "adb", AsyncMock(side_effect=SourceError(raw)))
    with caplog.at_level(logging.DEBUG), pytest.raises(at.ConnectionFault) as caught:
        await at.AndroidBackend().command("S1", "get-state")
    assert caught.value.code == expected
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert "private-token" not in str(caught.value) + caplog.text
    assert "private-input" not in str(caught.value) + caplog.text
    assert "raw-sensitive-output" not in str(caught.value) + caplog.text


async def test_inventory_only_is_host_global(monkeypatch: pytest.MonkeyPatch) -> None:
    output = AsyncMock(
        return_value=(
            "List of devices attached\nS1 device model:Phone_1 transport_id:7\n"
            "S2 unauthorized transport_id:8\nS3 offline transport_id:9\n"
        )
    )
    monkeypatch.setattr(at, "host_output", output)
    rows = await at.AndroidBackend().inventory()
    assert [(row.serial, row.state, row.transport_id) for row in rows] == [
        ("S1", "device", "7"),
        ("S2", "unauthorized", "8"),
        ("S3", "offline", "9"),
    ]
    output.assert_awaited_once_with("fake-adb", "devices", "-l")


async def test_duplicate_inventory_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(at, "host_output", AsyncMock(return_value="S1 device\nS1 offline\n"))
    with pytest.raises(at.ConnectionFault, match="invalid_inventory"):
        await at.AndroidBackend().inventory()


@pytest.mark.parametrize(
    "value", ["600\n" + "a" * 63, "600\n" + "A" * 64, "600\n" + "a" * 64 + "\n", "644\n" + "a" * 64]
)
async def test_token_requires_complete_private_lowercase_bytes(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = at.AndroidBackend()
    monkeypatch.setattr(backend, "private", AsyncMock(return_value=value))
    with pytest.raises(at.ConnectionFault, match="credential_unavailable"):
        await backend.token("S1")


async def test_complete_token_is_read_not_passed_in_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = at.AndroidBackend()
    private = AsyncMock(return_value="600\n" + "b" * 64)
    monkeypatch.setattr(backend, "private", private)
    assert await backend.token("S1") == "b" * 64
    assert "b" * 64 not in repr(private.call_args)
    private.return_value = "missing"
    assert await backend.token("S1") is None


@pytest.mark.parametrize("value", ["2000:1\r\n", "2000:0", "2000:2147483648", "1000:42", "2000:01"])
async def test_invalid_private_peer_is_not_used(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = at.AndroidBackend()
    monkeypatch.setattr(backend, "private", AsyncMock(return_value=value))
    with pytest.raises(at.ConnectionFault, match="peer_configuration_invalid"):
        await backend.peer("S1")


@pytest.mark.parametrize(
    "service", [at.ACCESSIBILITY_COMPONENT, "com.pulse.agent/.service.AgentAccessibilityService"]
)
def test_accessibility_alias_and_preservation(service: str) -> None:
    original = at.AccessibilitySettings(f"other.pkg/.Service:{service}", "0")
    assert original.agent_selected and not original.active
    target = original.with_agent()
    assert target.services == original.services and target.active
    assert at.AccessibilitySettings("other.pkg/.Service", "1").with_agent().services == (
        f"other.pkg/.Service:{at.ACCESSIBILITY_COMPONENT}"
    )


async def test_partial_settings_write_is_restored_without_touching_foreign_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = at.AndroidBackend()
    original = at.AccessibilitySettings("other/.Service", "0")
    target = original.with_agent()
    current = at.AccessibilitySettings(target.services, "0")
    monkeypatch.setattr(backend, "permissions", AsyncMock(return_value=current))
    setter = AsyncMock(return_value=True)
    monkeypatch.setattr(backend, "set_permissions", setter)
    assert await backend.restore_permissions("S1", original, target)
    setter.assert_awaited_once_with("S1", current, original)
    setter.reset_mock()
    backend.permissions.return_value = at.AccessibilitySettings("foreign/.Service", "1")
    assert await backend.restore_permissions("S1", original, target)
    setter.assert_not_called()


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("settings_changed", "accessibility_settings_changed"),
        ("SecurityException: private-input", "accessibility_setting_denied"),
        ("blocked by administrator policy: raw-sensitive-output", "accessibility_policy_blocked"),
    ],
)
async def test_auto_enable_has_closed_policy_and_concurrent_edit_reasons(
    output: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = at.AndroidBackend()
    monkeypatch.setattr(backend, "command", AsyncMock(return_value=output))
    original = at.AccessibilitySettings("null", "0")
    with pytest.raises(at.ConnectionFault, match=expected):
        await backend.set_permissions("S1", original, original.with_agent())


class PrivateShell(at.AndroidBackend):
    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory

    async def private(self, serial: str, script: str) -> str:
        at.validate_serial(serial)
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            script,
            cwd=self.directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _error = await proc.communicate()
        if proc.returncode != 0:
            raise at.ConnectionFault("adb_command_failed")
        return output.decode()


@pytest.mark.parametrize("original", [None, b"2000:7\n"])
async def test_actual_private_lease_scripts_restore_original_only(
    tmp_path: Path, original: bytes | None
) -> None:
    files = tmp_path / "files"
    files.mkdir()
    peer = files / "adb-peer"
    if original is not None:
        peer.write_bytes(original)
    backend = PrivateShell(tmp_path)
    owner = str(uuid4())
    await backend.claim_peer("S1", owner, at.AdbdPeer(2000, 42))
    assert peer.read_bytes() == b"2000:42\n"
    assert peer.stat().st_mode & 0o777 == 0o600
    assert (files / "pulse-runtime-peer").stat().st_mode & 0o777 == 0o700
    with pytest.raises(at.ConnectionFault, match="configuration_in_use"):
        await backend.claim_peer("S1", str(uuid4()), at.AdbdPeer(2000, 99))
    assert await backend.release_peer("S1", str(uuid4()))
    assert peer.read_bytes() == b"2000:42\n"
    await backend.update_peer("S1", owner, at.AdbdPeer(2000, 43))
    assert peer.read_bytes() == b"2000:43\n"
    assert await backend.release_peer("S1", owner)
    assert (peer.read_bytes() if peer.exists() else None) == original
    assert not (files / "pulse-runtime-peer").exists()


async def test_private_lease_never_overwrites_changed_configuration(tmp_path: Path) -> None:
    backend = PrivateShell(tmp_path)
    owner = str(uuid4())
    await backend.claim_peer("S1", owner, at.AdbdPeer(2000, 42))
    peer = tmp_path / "files" / "adb-peer"
    peer.write_bytes(b"foreign-private-configuration")
    with pytest.raises(at.ConnectionFault):
        await backend.update_peer("S1", owner, at.AdbdPeer(2000, 43))
    assert await backend.release_peer("S1", owner)
    assert peer.read_bytes() == b"foreign-private-configuration"


async def test_private_lease_refuses_a_preexisting_symlink(tmp_path: Path) -> None:
    files = tmp_path / "files"
    files.mkdir()
    foreign = files / "foreign"
    foreign.write_bytes(b"leave-unchanged")
    peer = files / "adb-peer"
    peer.symlink_to(foreign)
    backend = PrivateShell(tmp_path)
    with pytest.raises(at.ConnectionFault):
        await backend.claim_peer("S1", str(uuid4()), at.AdbdPeer(2000, 42))
    assert peer.is_symlink() and foreign.read_bytes() == b"leave-unchanged"


async def test_interrupted_peer_update_still_has_owned_cleanup_proof(tmp_path: Path) -> None:
    files = tmp_path / "files"
    files.mkdir()
    peer = files / "adb-peer"
    peer.write_bytes(b"2000:7\n")
    backend = PrivateShell(tmp_path)
    owner = str(uuid4())
    await backend.claim_peer("S1", owner, at.AdbdPeer(2000, 42))
    # The atomic publication completed, but rename(next, expected) has not completed yet.
    (files / "pulse-runtime-peer" / "next").write_bytes(b"2000:43\n")
    peer.write_bytes(b"2000:43\n")
    assert await backend.release_peer("S1", owner)
    assert peer.read_bytes() == b"2000:7\n"


async def test_private_cleanup_preserves_a_foreign_symlink(tmp_path: Path) -> None:
    backend = PrivateShell(tmp_path)
    owner = str(uuid4())
    await backend.claim_peer("S1", owner, at.AdbdPeer(2000, 42))
    files = tmp_path / "files"
    foreign = files / "foreign"
    foreign.write_bytes(b"2000:42\n")
    peer = files / "adb-peer"
    peer.unlink()
    peer.symlink_to(foreign)
    assert await backend.release_peer("S1", owner)
    assert peer.is_symlink() and foreign.read_bytes() == b"2000:42\n"


async def test_installed_identity_comes_from_selected_package_and_exact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = at.AndroidBackend()
    command = AsyncMock(
        side_effect=[
            "package:/data/app/~~test/com.pulse.agent-test/base.apk\n",
            "versionCode=1 minSdk=30\nversionName=0.1.0\npkgFlags=[ HAS_CODE DEBUGGABLE ]\n",
            at.SUPPORTED_BUILD.apk_sha256 + "  /data/app/~~test/com.pulse.agent-test/base.apk\n",
        ]
    )
    monkeypatch.setattr(backend, "command", command)
    installed = await backend.installed("S2")
    assert installed == at.InstalledAgent("0.1.0", 1, True, at.SUPPORTED_BUILD.apk_sha256)
    assert all(call.args[0] == "S2" for call in command.call_args_list)
    assert command.call_args_list[0].args[2] == "pm path --user 0 com.pulse.agent || true"
    assert command.call_args_list[1].args[2] == "dumpsys package com.pulse.agent"
    command.reset_mock()
    command.side_effect = [""]
    assert await backend.installed("S1") is None
    assert command.await_count == 1


@pytest.mark.parametrize(
    "path",
    ["package:/data/app/a/base.apk\npackage:/data/app/a/split.apk\n", "../../x", "private-input"],
)
async def test_unknown_package_paths_are_never_hashed(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = at.AndroidBackend()
    command = AsyncMock(return_value=path)
    monkeypatch.setattr(backend, "command", command)
    with pytest.raises(at.ConnectionFault, match="build_incompatible"):
        await backend.installed("S1")
    assert command.await_count == 1


@pytest.mark.parametrize(
    "row",
    [
        "UID PID NAME\n2000 43 adbd\n",
        "UID PID NAME\n1000 42 adbd\n",
        "UID PID NAME\n2000 42 shell\n",
        "INVALID HEADER\n2000 42 adbd\n",
    ],
)
async def test_adbd_identity_rejects_wrong_uid_pid_name_or_header(
    row: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = at.AndroidBackend()
    monkeypatch.setattr(backend, "command", AsyncMock(side_effect=["42\n", row]))
    with pytest.raises(at.ConnectionFault, match="adbd_identity_unavailable"):
        await backend.adbd_peer("S1")


async def test_wrong_artifact_is_never_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apk = tmp_path / "untrusted.apk"
    apk.write_bytes(b"not-the-supported-signed-build")
    backend = at.AndroidBackend()
    command = AsyncMock()
    monkeypatch.setattr(backend, "command", command)
    with pytest.raises(at.ConnectionFault, match="build_signature_incompatible"):
        await backend.install("S1", apk)
    command.assert_not_called()


@pytest.mark.parametrize(
    ("platform", "output", "valid"),
    [
        ("darwin", "p123\nn127.0.0.1:1234\nn[::1]:1234\n", True),
        ("darwin", "p123\nn*:1234\n", False),
        ("darwin", "p123\nn192.168.1.2:1234\n", False),
        ("darwin", "", False),
        ("linux", "LISTEN 0 128 127.0.0.1:1234 0.0.0.0:*\n", True),
        ("linux", "LISTEN 0 128 0.0.0.0:1234 0.0.0.0:*\n", False),
        ("win32", "TCP 127.0.0.1:1234 0.0.0.0:0 LISTENING 99\n", True),
        ("win32", "TCP 0.0.0.0:1234 0.0.0.0:0 LISTENING 99\n", False),
    ],
)
async def test_only_loopback_listener_is_accepted(
    platform: str, output: str, valid: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(at.sys, "platform", platform)
    monkeypatch.setattr(at, "host_output", AsyncMock(return_value=output))
    if valid:
        await at.AndroidBackend().loopback(1234)
    else:
        with pytest.raises(at.ConnectionFault, match="forward_not_loopback"):
            await at.AndroidBackend().loopback(1234)


async def test_host_timeout_and_cancellation_reap_owned_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    killed = []

    async def communicate() -> tuple[bytes, bytes]:
        entered.set()
        await asyncio.Future()
        return b"", b""

    process = SimpleNamespace(
        returncode=None, communicate=communicate, wait=AsyncMock(return_value=0)
    )
    process.kill = lambda: killed.append(True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(at.host_output("fake-adb", "devices", timeout=60))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [True] and process.wait.await_count == 1
    with pytest.raises(at.ConnectionFault, match="timeout"):
        await at.host_output("fake-adb", "devices", timeout=0.001)
    assert killed == [True, True] and process.wait.await_count == 2


async def test_repeated_cancel_finishes_one_mutation_without_replay() -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def mutate() -> None:
        calls.append("issued")
        entered.set()
        await finish.wait()
        calls.append("finished")

    task = asyncio.create_task(at.complete_mutation(mutate()))
    await entered.wait()
    task.cancel()
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["issued", "finished"]


@pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8"])
async def test_real_loopback_health_framing_and_bearer(content_type: str) -> None:
    body = json.dumps(health_body()).encode()
    requests = []
    closed = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        result = await at.read_health(port, "d" * 64)
        await closed.wait()
    assert result.capabilities["recovery_required"] is False
    assert requests[0].startswith(b"GET /v1/health HTTP/1.1\r\n")
    assert b"Authorization: Bearer " + b"d" * 64 + b"\r\n" in requests[0]
    assert b"/v1/action" not in requests[0]


@pytest.mark.parametrize(
    "head",
    [
        b"HTTP/1.1 401 Unauthorized\r\n\r\n",
        b"HTTP/1.1 403 Forbidden\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\nContent-Type: application/json\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n",
    ],
)
async def test_real_health_rejects_auth_and_unsafe_framing(head: bytes) -> None:
    handlers = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.append(asyncio.current_task())
        await reader.readuntil(b"\r\n\r\n")
        writer.write(head)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        with pytest.raises(at.ConnectionFault):
            await at.read_health(server.sockets[0].getsockname()[1], "c" * 64)
    await asyncio.gather(*handlers)


async def test_health_cancel_closes_client_socket() -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()
    handlers = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.append(asyncio.current_task())
        await reader.readuntil(b"\r\n\r\n")
        entered.set()
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
        closed.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        task = asyncio.create_task(at.read_health(server.sockets[0].getsockname()[1], "e" * 64))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), 2)
    await asyncio.gather(*handlers)


@pytest.mark.parametrize("installed", [None, at.InstalledAgent("0.1.0", 1, False, "f" * 64)])
async def test_evidence_baseline_does_not_run_as_before_install_or_for_release(
    installed: at.InstalledAgent | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path(__file__).parents[2] / "scripts" / "p1_device_connect.py"
    spec = importlib.util.spec_from_file_location("device_connect_acceptance", path)
    assert spec is not None and spec.loader is not None
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    backend = at.AndroidBackend()
    monkeypatch.setattr(client, "AndroidBackend", lambda: backend)
    settings = at.AccessibilitySettings("other/.Service", "0")
    monkeypatch.setattr(backend, "permissions", AsyncMock(return_value=settings))
    monkeypatch.setattr(backend, "installed", AsyncMock(return_value=installed))
    peer = AsyncMock(side_effect=AssertionError("run-as before authorized installation"))
    monkeypatch.setattr(backend, "peer", peer)
    monkeypatch.setattr(backend, "command", AsyncMock(return_value=""))
    assert await client.baseline("S1") == (settings, None, set())
    peer.assert_not_called()


async def test_install_explicitly_forbids_android_default_replacement_and_targets_owner_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = b"supported-test-artifact"
    apk = tmp_path / "supported.apk"
    apk.write_bytes(contents)
    build = replace(at.SUPPORTED_BUILD, apk_sha256=hashlib.sha256(contents).hexdigest())
    backend = at.AndroidBackend(build)
    command = AsyncMock(return_value="Success\n")
    monkeypatch.setattr(backend, "command", command)
    await backend.install("S1", apk)
    assert command.call_args.args[1:6] == ("install", "--no-streaming", "-R", "--user", "0")
    assert "-r" not in command.call_args.args


@pytest.mark.parametrize("output", ["", "\n", "\r\n", "  \n"])
async def test_empty_forward_listing_ignores_adb_blank_lines(
    output: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = at.AndroidBackend()
    monkeypatch.setattr(backend, "command", AsyncMock(return_value=output))
    assert await at.ForwardLease(backend, "S1").mappings() == set()
