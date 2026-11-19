"""Fixed-fixture installation evidence, with no physical ADB, Runtime or Agent startup."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from pulse_runtime.api import app as app_module
from pulse_runtime.api.app import create_app
from pulse_runtime.drivers.android import agent_transport as at
from pulse_runtime.drivers.android import scrcpy_video
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.drivers.android.scrcpy_video import SourceError
from runtime.tests.test_device_connections import Gate

SERIAL = "PULSE_TARGET"
OTHER_SERIAL = "192.0.2.1:5555"
API_TOKEN = "target-app-api-secret"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
PATH = f"/devices/{SERIAL}/target-app"
SECRET = "private-output-and-token"
USER_COMMAND = "am get-current-user 2>&1"
LIST_COMMAND = (
    "pm list packages --user 0 com.pulse.target 2>&1 "
    "&& printf '\\npulse-target-app-list-complete\\n'"
)
LIST_COMPLETE = "\npulse-target-app-list-complete\n"
VERSION_COMMAND = "dumpsys package com.pulse.target 2>&1"
PACKAGE_HEADER = "Packages:\n  Package [com.pulse.target] (123abc):\n"
DETAILS = PACKAGE_HEADER + "    versionCode=42 minSdk=30 targetSdk=36\n    versionName=4.2.1\n"
INVENTORY = f"List of devices attached\n{SERIAL} device\n{OTHER_SERIAL} device\n"
OUTPUTS = {
    USER_COMMAND: "0\n",
    LIST_COMMAND: "package:com.pulse.target\n" + LIST_COMPLETE,
    VERSION_COMMAND: DETAILS,
}
REAL_ADB = scrcpy_video.adb


@pytest.fixture(autouse=True)
def forbid_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Target-app tests must not use physical ADB, host processes or Agent sockets")

    async def forbidden_async(*args: object, **kwargs: object) -> None:
        forbidden(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "open_connection", forbidden_async)
    monkeypatch.setattr(at, "adb", forbidden_async)
    monkeypatch.setattr(at, "find_adb", forbidden)
    monkeypatch.setattr(at, "host_output", forbidden_async)
    monkeypatch.setattr(at, "read_health", forbidden_async)
    monkeypatch.setattr(scrcpy_video, "adb", forbidden_async)
    monkeypatch.setattr(scrcpy_video, "find_adb", forbidden)
    monkeypatch.setattr(app_module, "adb", forbidden_async)


@dataclass
class TargetRig:
    app: FastAPI
    backend: at.AndroidBackend
    manager: DeviceConnectionManager
    client: AsyncClient
    runner: AsyncMock
    inventory: AsyncMock


@pytest.fixture
async def rig(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TargetRig]:
    async def output(serial: str, *args: str, timeout: float = 5.0) -> str:  # noqa: ASYNC109
        assert serial in {SERIAL, OTHER_SERIAL}
        assert args[0] == "shell" and len(args) == 2
        assert 0 < timeout <= 5
        assert args[1] in OUTPUTS
        return OUTPUTS[args[1]]

    runner = AsyncMock(side_effect=output)
    inventory = AsyncMock(return_value=INVENTORY)
    monkeypatch.setattr(at, "find_adb", lambda: "fake-adb")
    monkeypatch.setattr(at, "host_output", inventory)
    monkeypatch.setattr(at, "adb", runner)
    backend = at.AndroidBackend()
    manager = DeviceConnectionManager(backend, ledger_path=":memory:")
    app = create_app(API_TOKEN, device_manager=manager)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://pulse.test"
        ) as client:
            yield TargetRig(app, backend, manager, client, runner, inventory)
    finally:
        await manager.close()


def test_openapi_exposes_a_typed_fixed_package_read_only_contract(rig: TargetRig) -> None:
    schema = rig.app.openapi()
    route = schema["paths"]["/devices/{serial}/target-app"]
    assert set(route) == {"get"}
    assert route["get"]["operationId"] == "getTargetApp"
    assert route["get"]["parameters"] == [
        {
            "name": "serial",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "title": "Serial"},
        }
    ]
    assert "requestBody" not in route["get"]
    assert route["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/TargetAppResponse"
    }
    response = schema["components"]["schemas"]["TargetAppResponse"]
    assert response["additionalProperties"] is False
    assert set(response["required"]) == {
        "serial",
        "package",
        "status",
        "version_name",
        "version_code",
        "observed_at_ms",
        "diagnostic",
    }
    assert response["properties"]["package"]["const"] == "com.pulse.target"
    assert response["properties"]["status"]["enum"] == [
        "installed",
        "missing",
        "unavailable",
        "failed",
    ]
    for field in ("version_name", "version_code", "observed_at_ms"):
        assert {"type": "null"} in response["properties"][field]["anyOf"]
    diagnostic = schema["components"]["schemas"]["TargetAppDiagnostic"]
    assert diagnostic["additionalProperties"] is False
    assert set(diagnostic["required"]) == {"code", "guidance"}


@pytest.mark.parametrize("authorization", ["missing", "wrong", "query-only"])
async def test_bearer_auth_precedes_every_check(rig: TargetRig, authorization: str) -> None:
    headers = {"Authorization": "Bearer wrong"} if authorization == "wrong" else {}
    path = PATH + f"?token={API_TOKEN}" if authorization == "query-only" else PATH
    response = await rig.client.get(path, headers=headers)
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
    assert API_TOKEN not in response.text
    rig.inventory.assert_not_called()
    rig.runner.assert_not_called()
    assert rig.manager.slots == {}


@pytest.mark.parametrize("serial", ["space serial", "serial;id", "serial\n", "x" * 65])
async def test_invalid_serial_is_rejected_before_any_io(rig: TargetRig, serial: str) -> None:
    response = await rig.client.get(f"/devices/{quote(serial, safe='')}/target-app", headers=AUTH)
    assert response.status_code == 400 and response.json() == {"error": "invalid_serial"}
    rig.inventory.assert_not_called()
    rig.runner.assert_not_called()
    assert rig.manager.slots == {}


@pytest.mark.parametrize("serial", ["", "../device", "A;id", "A\nB", "x" * 65])
async def test_backend_also_validates_serial_before_inventory(rig: TargetRig, serial: str) -> None:
    with pytest.raises(at.ConnectionFault, match=r"^invalid_serial$"):
        await rig.backend.target_app(serial)
    rig.inventory.assert_not_called()
    rig.runner.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        "package=com.pulse.agent",
        "package=com.pulse.target",
        "apk=/private/secret.apk",
        "path=/private/secret",
        "user=10",
        "timeout_ms=1",
        "token=private-token",
        "package=com.pulse.target&package=com.pulse.agent",
    ],
)
async def test_no_query_parameters_are_accepted(rig: TargetRig, query: str) -> None:
    response = await rig.client.get(PATH + "?" + query, headers=AUTH)
    assert response.status_code == 422 and response.json() == {"error": "invalid_request"}
    assert query not in response.text
    rig.inventory.assert_not_called()
    rig.runner.assert_not_called()


@pytest.mark.parametrize(
    "body", [b'{"package":"com.pulse.agent"}', b"null", b"/private/secret.apk"]
)
@pytest.mark.parametrize("declared_empty", [False, True])
async def test_get_body_is_rejected_before_any_io(
    rig: TargetRig, body: bytes, declared_empty: bool
) -> None:
    headers = {**AUTH, "Content-Length": "0"} if declared_empty else AUTH
    response = await rig.client.request("GET", PATH, content=body, headers=headers)
    assert response.status_code == 422 and response.json() == {"error": "invalid_request"}
    rig.inventory.assert_not_called()
    rig.runner.assert_not_called()


async def test_installed_is_evidence_not_readiness_or_exact_build(
    rig: TargetRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module.time, "time_ns", lambda: 1_234_567_890_000_000)
    response = await rig.client.get(PATH, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "serial": SERIAL,
        "package": "com.pulse.target",
        "status": "installed",
        "version_name": "4.2.1",
        "version_code": 42,
        "observed_at_ms": 1_234_567_890,
        "diagnostic": {
            "code": "target_app_installed",
            "guidance": "Installation was observed for Android user 0 only; this does "
            "not verify readiness, target cases, or an exact build.",
        },
    }
    assert rig.manager.slots == {} and rig.manager.watcher is None
    rig.inventory.assert_awaited_once_with("fake-adb", "devices", "-l")
    assert [call.args for call in rig.runner.call_args_list] == [
        (SERIAL, "shell", USER_COMMAND),
        (SERIAL, "shell", LIST_COMMAND),
        (SERIAL, "shell", VERSION_COMMAND),
    ]


@pytest.mark.parametrize("listing", ["", "package:com.pulse.target.helper\n"])
async def test_only_successful_enumeration_without_exact_package_is_missing(
    rig: TargetRig, listing: str
) -> None:
    rig.runner.side_effect = ["0\n", listing + LIST_COMPLETE]
    response = await rig.client.get(PATH, headers=AUTH)
    body = response.json()
    assert response.status_code == 200 and body["status"] == "missing"
    assert body["diagnostic"]["code"] == "target_app_missing"
    assert body["version_name"] is None and body["version_code"] is None
    assert isinstance(body["observed_at_ms"], int)
    assert rig.runner.await_count == 2 and rig.manager.slots == {}


@pytest.mark.parametrize(
    ("inventory", "code"),
    [
        (f"{SERIAL} offline\n", "adb_offline"),
        (f"{SERIAL} unauthorized\n", "adb_unauthorized"),
        (f"{SERIAL} no permissions\n", "adb_permission_denied"),
        (f"{SERIAL} recovery\n", "device_unavailable"),
        (f"{OTHER_SERIAL} device\n", "device_disconnected"),
    ],
)
async def test_unavailable_device_is_never_missing_or_implicitly_addressed(
    rig: TargetRig, inventory: str, code: str
) -> None:
    rig.inventory.return_value = inventory
    response = await rig.client.get(PATH, headers=AUTH)
    assert response.status_code == 200 and response.json()["status"] == "unavailable"
    assert response.json()["diagnostic"]["code"] == code
    rig.runner.assert_not_called()
    assert rig.manager.slots == {}


async def test_missing_adb_has_a_closed_unavailable_reason(
    rig: TargetRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> str:
        raise SourceError(f"adb not found /private/{SECRET}")

    monkeypatch.setattr(at, "find_adb", missing)
    response = await rig.client.get(PATH, headers=AUTH)
    assert response.status_code == 200 and response.json()["status"] == "unavailable"
    assert response.json()["diagnostic"]["code"] == "adb_unavailable"
    assert SECRET not in response.text and "/private/" not in response.text
    rig.inventory.assert_not_called()
    rig.runner.assert_not_called()


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (SourceError(f"unauthorized: Bearer {SECRET}"), "unavailable", "adb_unauthorized"),
        (SourceError(f"offline: {SECRET}"), "unavailable", "adb_offline"),
        (SourceError(f"device not found: {SECRET}"), "unavailable", "device_disconnected"),
        (SourceError(f"timed out: {SECRET}"), "unavailable", "timeout"),
        (OSError(f"/private/{SECRET}"), "unavailable", "adb_unavailable"),
        (SourceError(f"Permission Denial: {SECRET}"), "failed", "target_app_query_failed"),
        (ValueError(f"/private/{SECRET}"), "failed", "target_app_query_failed"),
        (at.ConnectionFault(SECRET), "failed", "target_app_query_failed"),
    ],
)
async def test_query_errors_cannot_become_missing_or_expose_raw_details(
    rig: TargetRig, error: Exception, status: str, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig.runner.side_effect = ["0\n", error]
    response = await rig.client.get(PATH, headers=AUTH)
    body = response.json()
    assert response.status_code == 200 and body["status"] == status
    assert body["diagnostic"]["code"] == code
    assert body["version_name"] is None and body["version_code"] is None
    assert SECRET not in response.text + caplog.text
    assert "/private/" not in response.text + caplog.text
    assert API_TOKEN not in response.text + caplog.text
    assert rig.runner.await_count == 2


@pytest.mark.parametrize(
    "listing",
    [
        f"Permission Denial: {SECRET}\n",
        f"package:/private/{SECRET}.apk\n",
        "package:com.pulse.agent\n",
        "package:com.pulse.target\npackage:com.pulse.target\n",
        f"package:com.pulse.target\nError: {SECRET}\n",
        " \n",
    ],
)
async def test_anomalous_enumeration_fails_closed(rig: TargetRig, listing: str) -> None:
    rig.runner.side_effect = ["0\n", listing + LIST_COMPLETE]
    response = await rig.client.get(PATH, headers=AUTH)
    assert response.status_code == 200 and response.json()["status"] == "failed"
    assert response.json()["diagnostic"]["code"] == "target_app_output_invalid"
    assert SECRET not in response.text
    assert rig.runner.await_count == 2


@pytest.mark.parametrize("listing", ["", "package:com.pulse.target\n", LIST_COMPLETE + SECRET])
async def test_no_success_marker_means_no_missing_or_installed_claim(
    rig: TargetRig, listing: str
) -> None:
    rig.runner.side_effect = ["0\n", listing]
    response = await rig.client.get(PATH, headers=AUTH)
    assert response.status_code == 200 and response.json()["status"] == "failed"
    assert response.json()["diagnostic"]["code"] == "target_app_output_invalid"
    assert rig.runner.await_count == 2


@pytest.mark.parametrize(
    ("details", "name", "code"),
    [
        (PACKAGE_HEADER, None, None),
        (PACKAGE_HEADER + "    versionCode=42 minSdk=30\n", None, 42),
        (PACKAGE_HEADER + "    versionName=4.2.1\n", "4.2.1", None),
        (PACKAGE_HEADER + "    versionName=null\n", None, None),
    ],
)
async def test_missing_version_metadata_is_explicitly_nullable(
    rig: TargetRig, details: str, name: str | None, code: int | None
) -> None:
    rig.runner.side_effect = ["0\n", "package:com.pulse.target\n" + LIST_COMPLETE, details]
    response = await rig.client.get(PATH, headers=AUTH)
    body = response.json()
    assert response.status_code == 200 and body["status"] == "installed"
    assert body["diagnostic"]["code"] == "target_app_version_unavailable"
    assert body["version_name"] == name and body["version_code"] == code


@pytest.mark.parametrize(
    "details",
    [
        f"Permission Denial: {SECRET}",
        DETAILS.replace("com.pulse.target", "com.pulse.agent"),
        DETAILS + "    versionName=duplicate\n",
        DETAILS.replace("versionCode=42", "versionCode=-1"),
        DETAILS.replace("versionCode=42", "versionCode=unknown"),
        DETAILS.replace("versionCode=42", f"versionCode={2**63}"),
        DETAILS.replace("4.2.1", f"/private/{SECRET}"),
        DETAILS.replace("4.2.1", f"Bearer {SECRET}"),
        DETAILS.replace("4.2.1", "a" * 129),
        DETAILS.replace("4.2.1", "4.2.1\x00secret"),
    ],
)
async def test_unrecognized_or_unsafe_version_output_is_not_installation_evidence(
    rig: TargetRig, details: str
) -> None:
    rig.runner.side_effect = ["0\n", "package:com.pulse.target\n" + LIST_COMPLETE, details]
    response = await rig.client.get(PATH, headers=AUTH)
    body = response.json()
    assert response.status_code == 200 and body["status"] == "failed"
    assert body["diagnostic"]["code"] == "target_app_output_invalid"
    assert body["version_name"] is None and body["version_code"] is None
    assert SECRET not in response.text and "/private/" not in response.text


async def test_metadata_outside_the_exact_package_block_is_not_returned(rig: TargetRig) -> None:
    details = (
        f"    versionName={SECRET}\n    versionCode=99\n"
        + DETAILS
        + f"Dexopt state:\n    versionName={SECRET}\n    versionCode=99\n"
    )
    rig.runner.side_effect = ["0\n", "package:com.pulse.target\n" + LIST_COMPLETE, details]
    response = await rig.client.get(PATH, headers=AUTH)
    body = response.json()
    assert response.status_code == 200 and body["status"] == "installed"
    assert body["version_name"] == "4.2.1" and body["version_code"] == 42
    assert SECRET not in response.text


@pytest.mark.parametrize("user", ["10\n", "-1\n", "null\n", "0\n10\n", "0;id\n"])
async def test_current_user_must_be_explicitly_supported_before_package_queries(
    rig: TargetRig, user: str
) -> None:
    rig.runner.side_effect = [user]
    response = await rig.client.get(PATH, headers=AUTH)
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == ("unavailable" if user == "10\n" else "failed")
    assert body["diagnostic"]["code"] == (
        "unsupported_android_user" if user == "10\n" else "target_app_output_invalid"
    )
    assert rig.runner.await_count == 1


@pytest.mark.parametrize("stage", ["inventory", USER_COMMAND, LIST_COMMAND, VERSION_COMMAND])
async def test_whole_check_has_a_five_second_deadline_and_cancels_blocked_io(
    rig: TargetRig, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = Gate()
    deadlines: list[asyncio.Timeout] = []
    real_timeout = asyncio.timeout

    def capture_timeout(delay: float | None) -> asyncio.Timeout:
        deadline = real_timeout(delay)
        if delay == 5:
            deadlines.append(deadline)
        return deadline

    async def inventory(*args: str) -> str:
        if stage == "inventory":
            await gate.wait()
        return INVENTORY

    async def output(serial: str, *args: str, **kwargs: object) -> str:
        if args[1] == stage:
            await gate.wait()
        return OUTPUTS[args[1]]

    monkeypatch.setattr(asyncio, "timeout", capture_timeout)
    rig.inventory.side_effect = inventory
    rig.runner.side_effect = output
    task = asyncio.create_task(rig.client.get(PATH, headers=AUTH))
    try:
        await asyncio.wait_for(gate.entered.wait(), 5)
        assert len(deadlines) == 2
        now = asyncio.get_running_loop().time()
        assert all(
            deadline.when() is not None and 0 < deadline.when() - now <= 5 for deadline in deadlines
        )
        deadlines[-1].reschedule(now)
        response = await asyncio.wait_for(task, 5)
        assert response.status_code == 200 and response.json()["status"] == "unavailable"
        assert response.json()["diagnostic"]["code"] == "timeout"
        assert gate.cancelled.is_set() and gate.finished.is_set()
        assert rig.manager.slots == {}
    finally:
        gate.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_adb_runner_qualifies_every_read_only_device_command(
    rig: TargetRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def process(*args: str, **kwargs: object) -> SimpleNamespace:
        assert args[:2] == ("fake-adb", "-s")
        assert args[2] in {SERIAL, OTHER_SERIAL}
        assert args[3] == "shell" and len(args) == 5
        assert args[4] in OUTPUTS
        return SimpleNamespace(
            returncode=0,
            communicate=AsyncMock(return_value=(OUTPUTS[args[4]].encode(), b"")),
        )

    create_process = AsyncMock(side_effect=process)
    monkeypatch.setattr(at, "adb", REAL_ADB)
    monkeypatch.setattr(scrcpy_video, "find_adb", lambda: "fake-adb")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    responses = await asyncio.gather(
        rig.client.get(PATH, headers=AUTH),
        rig.client.get(f"/devices/{OTHER_SERIAL}/target-app", headers=AUTH),
    )
    assert [response.json()["serial"] for response in responses] == [SERIAL, OTHER_SERIAL]
    assert all(
        response.status_code == 200 and response.json()["status"] == "installed"
        for response in responses
    )
    assert {call.args for call in create_process.call_args_list} == {
        ("fake-adb", "-s", serial, "shell", command)
        for serial in (SERIAL, OTHER_SERIAL)
        for command in OUTPUTS
    }
    assert create_process.await_count == 6
    rig.runner.assert_not_called()
    assert rig.manager.slots == {} and rig.manager.watcher is None
