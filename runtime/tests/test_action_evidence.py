"""Read-only script imports and in-memory stubs only: no devices or live Runtime."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

SERIAL = "controlled-fixture"
SESSION = "11111111-1111-4111-8111-111111111111"
LIFECYCLE = "22222222-2222-4222-8222-222222222222"
PACKAGE = "com.pulse.target"
PREFIX = PACKAGE + ":id/"
TOKEN = "PRIVATE_RUNTIME_TOKEN_" + "a" * 64
OLD_TOKEN = "PRIVATE_OLD_RUNTIME_TOKEN_" + "b" * 64
PRIVATE_NAME = "ORIGINAL_PRIVATE_NAME_NOT_EVIDENCE"
ORIGINAL = {"name": PRIVATE_NAME, "agree": True, "notify": True}


@pytest.fixture
def script(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "action_evidence_client", scripts / "p1_action.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Device/backend or live Runtime access is forbidden in script tests")

    async def forbidden_baseline(*_args, **_kwargs):
        forbidden()

    monkeypatch.setattr(module, "AndroidBackend", forbidden)
    monkeypatch.setattr(module, "AsyncClient", forbidden)
    monkeypatch.setattr(module, "baseline", forbidden_baseline)
    monkeypatch.setattr(sys.modules["p1_observe"], "AndroidBackend", forbidden)
    return module


def observation(request_id=None, state=None, *, foreground=True):
    state = dict(ORIGINAL if state is None else state)

    def node(path, view_id, *, text=None, checked=None, editable=False, enabled=True):
        index = path[0] if path else 0
        return {
            "node_path": path,
            "view_id": view_id,
            "package_name": PACKAGE if foreground else "com.android.launcher3",
            "text": text,
            "content_description": None,
            "text_redacted": False,
            "bounds": {
                "left": 20,
                "top": 20 + index * 100,
                "right": 300,
                "bottom": 80 + index * 100,
            },
            "states": {
                "enabled": enabled,
                "visible": True,
                "editable": editable,
                "checked": checked,
                "clickable": checked is not None,
                "showing_hint_text": editable and not bool(text),
            },
        }

    root = node([], None)
    root["bounds"] = {"left": 0, "top": 0, "right": 400, "bottom": 800}
    values = [root]
    if foreground:
        values += [
            node([0], PREFIX + "tv_form_title", text="CONTROLLED_TITLE_NOT_EVIDENCE"),
            node([1], PREFIX + "et_name", text=state["name"], editable=True),
            node([2], PREFIX + "cb_agree", checked=state["agree"]),
            node([3], PREFIX + "sw_notify", checked=state["notify"]),
            node([4], PREFIX + "btn_submit", enabled=state["agree"]),
        ]
    return {
        "contract": "pulse-runtime-observation-draft",
        "serial": SERIAL,
        "session_id": SESSION,
        "request_id": request_id or str(uuid4()),
        "agent_lifecycle_id": LIFECYCLE,
        "agent_http_session_id": str(uuid4()),
        "requested": {"tree": True, "screenshot": False, "ocr": False},
        "success": True,
        "status": "partial",
        "atomic_snapshot": False,
        "framing": {
            "validated": True,
            "terminal_received": True,
            "eof_received": True,
            "source_data_validated": True,
        },
        "sources": {
            "tree": {
                "source": "native",
                "requested": True,
                "status": "available",
                "coordinates": {"coordinate_space": "display_pixels", "display_id": 0},
                "data": {"nodes": values},
            },
            "screenshot": {"requested": False, "status": "unavailable", "data": None},
            "ocr": {"requested": False, "status": "unavailable", "data": None},
        },
        "action_context": {
            "status": "available",
            "source": "adb_input_viewport",
            "atomic_with_observation": False,
            "geometry": {"display_id": 0, "width": 400, "height": 800, "rotation": 0},
        },
    }


def action_reply(body, post=None, *, cancelled=False):
    kind = body["kind"]
    backend = "runtime_adb" if kind in {"key", "launch", "stop"} else "agent_v1"
    completion = (
        "adb_command_return"
        if backend == "runtime_adb"
        else "gesture_callback"
        if kind in {"tap", "long", "swipe"}
        else "accessibility_action_return"
    )
    return {
        "contract": "pulse-runtime-action-draft",
        **{
            key: body[key] for key in ("serial", "session_id", "request_id", "operation_id", "kind")
        },
        "backend": backend,
        "target_source": "tree",
        "target_observation_id": body["target"]["observation_id"],
        "agent_lifecycle_id": LIFECYCLE,
        "agent_http_session_id": str(uuid4()),
        "status": "rejected" if cancelled else "completed",
        "action_completed": not cancelled,
        "business_success": None,
        "automatic_replay": False,
        "recovery_required": False,
        "completion_source": None if cancelled else completion,
        "error": {"code": "cancelled"} if cancelled else None,
        "receipts": [{"status": "rejected", "source": "runtime"}]
        if cancelled
        else [
            {"status": "dispatched", "source": backend},
            {"status": "completed", "source": backend},
        ],
        "pre_observation_id": str(uuid4()),
        "post_observation": None if cancelled else post or observation(),
        "verification": {
            "status": "unavailable" if cancelled else "available",
            "business_status": "not_evaluated",
        },
        "framing": {
            "validated": not cancelled,
            "request_sent": not cancelled,
            "dispatched_received": not cancelled,
            "terminal_received": not cancelled,
            "eof_received": not cancelled,
        },
        "cleanup": {"connection_closed": True},
    }


def body_and_reply(script):
    view = observation()
    node = script.selected_node(view, PREFIX + "tv_form_title")
    body = script.action_body(
        SERIAL, SESSION, view, "tap", node, script.gesture_params(node, "tap")
    )
    return body, action_reply(body)


def keys(value):
    if isinstance(value, dict):
        return set(value).union(*(keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(item) for item in value))
    return set()


def test_safe_action_strips_parameters_private_inputs_three_sources_and_exception_prose(script):
    body, value = body_and_reply(script)
    value.update(
        params={"text": "PRIVATE_ACTION_INPUT"},
        target=body["target"],
        token=TOKEN,
        private_input="PRIVATE_TOP_LEVEL_INPUT",
        traceback="PRIVATE_EXCEPTION_PROSE",
    )
    value["error"] = {"code": "action_failed", "message": "PRIVATE_ERROR_PROSE"}
    value["receipts"][0].update(
        params={"text": "PRIVATE_RECEIPT_INPUT"},
        error={"code": "cancelled", "message": "PRIVATE_RECEIPT_ERROR"},
    )
    post = value["post_observation"]
    post["sources"]["tree"]["data"]["nodes"][0]["text"] = "PRIVATE_TREE_PAYLOAD"
    post["sources"]["ocr"]["data"] = {"blocks": [{"text": "PRIVATE_OCR_PAYLOAD"}]}
    post["sources"]["screenshot"]["data"] = {
        "base64": "PRIVATE_SCREENSHOT_PAYLOAD",
        "private_input": "PRIVATE_IMAGE_INPUT",
    }
    post["sources"]["tree"]["error"] = {"code": "action_failed", "message": "PRIVATE_SOURCE_ERROR"}
    post["action_context"].update(
        error={"code": "action_failed", "message": "PRIVATE_CONTEXT_ERROR"},
        params={"text": "PRIVATE_CONTEXT_INPUT"},
    )
    value["verification"]["error"] = {
        "code": "action_failed",
        "message": "PRIVATE_VERIFICATION_ERROR",
    }
    original = copy.deepcopy(value)
    summary = script.safe_action(value)
    output = json.dumps(summary)
    for secret in (
        TOKEN,
        PRIVATE_NAME,
        "PRIVATE_ACTION_INPUT",
        "PRIVATE_TOP_LEVEL_INPUT",
        "PRIVATE_EXCEPTION_PROSE",
        "PRIVATE_ERROR_PROSE",
        "PRIVATE_RECEIPT_INPUT",
        "PRIVATE_RECEIPT_ERROR",
        "PRIVATE_TREE_PAYLOAD",
        "PRIVATE_OCR_PAYLOAD",
        "PRIVATE_SCREENSHOT_PAYLOAD",
        "PRIVATE_IMAGE_INPUT",
        "PRIVATE_SOURCE_ERROR",
        "PRIVATE_CONTEXT_ERROR",
        "PRIVATE_CONTEXT_INPUT",
        "PRIVATE_VERIFICATION_ERROR",
    ):
        assert secret not in output
    assert not keys(summary) & {
        "params",
        "target",
        "node_path",
        "data",
        "nodes",
        "blocks",
        "base64",
        "text",
        "content_description",
        "token",
        "message",
        "traceback",
    }
    assert summary["post_observation"]["sourcecounts"]["tree"]["node_count"] == 6
    assert summary["post_observation"]["sourcecounts"]["ocr"]["block_count"] == 1
    assert summary["post_observation"]["sourcecounts"]["screenshot"]["png_base64_char_count"] == 26
    assert summary["error_code"] == "action_failed"
    assert value == original


def test_safe_action_closes_nested_metadata_and_unknown_error_codes(script):
    _, value = body_and_reply(script)
    secret = "PRIVATE_METADATA_OR_EXCEPTION"
    value.update(
        status=secret,
        completion_source=secret,
        kind=secret,
        error={"code": secret},
        cleanup={"connection_closed": {"text": secret}},
    )
    value["receipts"] = [{"status": secret, "source": secret, "error": {"code": secret}}]
    value["verification"] = {"status": secret, "business_status": secret, "error": {"code": secret}}
    value["post_observation"]["action_context"]["geometry"]["text"] = secret
    value["post_observation"]["sources"]["tree"]["scope"] = {"text": secret}
    summary = script.safe_action(value)
    assert secret not in json.dumps(summary)
    assert summary["error_code"] == "unrecognized_error"
    assert summary["cleanup"]["connection_closed"] is None


def test_safe_action_handles_missing_or_malformed_source_data_without_retaining_it(script):
    value = {"post_observation": {"sources": {"tree": {"data": {"nodes": None}}}}}
    summary = script.safe_action(value)
    assert summary["post_observation"]["sourcecounts"] == {
        name: {"data_present": False} for name in script.SOURCES
    }
    assert summary["receipts"] == [] and summary["business_success"] is None


def test_action_body_has_exact_contract_and_copies_direct_observed_path(script):
    view = observation()
    node = script.selected_node(view, PREFIX + "et_name")
    params = {"text": "PRIVATE_TEST_ASCII"}
    first = script.action_body(SERIAL, SESSION, view, "set_text", node, params)
    second = script.action_body(SERIAL, SESSION, view, "set_text", node, params, 90_000)
    assert set(first) == {
        "serial",
        "session_id",
        "request_id",
        "operation_id",
        "kind",
        "target",
        "params",
        "timeout_ms",
        "agent_timeout_ms",
    }
    assert first["target"] == {
        "observation_id": view["request_id"],
        "source": "tree",
        "coordinate_space": "display_pixels",
        "display_id": 0,
        "expected_package": PACKAGE,
        "node_path": node["node_path"],
    }
    assert first["target"]["node_path"] is not node["node_path"]
    assert first["params"] is not params
    assert first["timeout_ms"] == 60_000 and first["agent_timeout_ms"] == 5000
    assert second["timeout_ms"] == 90_000
    assert first["request_id"] != second["request_id"]
    assert first["operation_id"] != second["operation_id"]


@pytest.mark.parametrize(
    "kind,params,timeout",
    [
        ("clear", {"package": PACKAGE, "confirmed": True}, 60_000),
        ("tap", {"x": 50, "y": 50, "duration_ms": 80, "private_input": "PRIVATE"}, 60_000),
        ("long", {"x": 50, "y": 50, "duration_ms": 499}, 60_000),
        ("long", {"x": 50, "y": 50, "duration_ms": 1001}, 60_000),
        ("key", {"code": 26}, 60_000),
        ("key", {"code": True}, 60_000),
        ("launch", {"package": PACKAGE, "activity": ".MainActivity"}, 60_000),
        ("stop", {"package": "com.pulse.agent"}, 60_000),
        ("node_click", {}, 90_001),
        ("node_click", {}, True),
    ],
)
def test_action_builder_refuses_unsafe_physical_parameters(script, kind, params, timeout):
    view = observation()
    node = script.selected_node(view, PREFIX + "tv_form_title")
    with pytest.raises(script.ConnectionFault) as caught:
        script.action_body(SERIAL, SESSION, view, kind, node, params, timeout)
    assert caught.value.code in {"physical_clear_disabled", "invalid_action_arguments"}


@pytest.mark.parametrize("code", [3, 4, 66, 67, 82, 187])
def test_action_builder_key_allowlist(script, code):
    view = observation()
    body = script.action_body(
        SERIAL,
        SESSION,
        view,
        "key",
        script.selected_node(view, PREFIX + "tv_form_title"),
        {"code": code},
    )
    assert body["params"] == {"code": code}


def test_title_gestures_stay_in_observed_title_bounds_and_short_swipe(script):
    node = script.selected_node(observation(), PREFIX + "tv_form_title")
    for kind in ("tap", "long", "swipe"):
        params = script.gesture_params(node, kind)
        bounds = node["bounds"]
        assert bounds["left"] <= params["x"] < bounds["right"]
        assert bounds["top"] <= params["y"] < bounds["bottom"]
        if kind == "long":
            assert 500 <= params["duration_ms"] <= 1000
        if kind == "swipe":
            assert 0 < params["end_x"] - params["x"] <= 6
            assert params["end_y"] == params["y"]
            assert params["end_x"] < bounds["right"]


def test_hint_is_not_saved_as_original_name_and_sensitive_or_missing_page_is_rejected(script):
    view = observation(state={**ORIGINAL, "name": ""})
    view["sources"]["tree"]["data"]["nodes"][2]["text"] = "PRIVATE_HINT_NOT_A_VALUE"
    assert script.form_state(view)["name"] == ""
    view["sources"]["tree"]["data"]["nodes"][2]["text_redacted"] = True
    with pytest.raises(script.ConnectionFault, match="manual_prepare_required"):
        script.form_state(view)
    with pytest.raises(script.ConnectionFault, match="manual_prepare_required"):
        script.form_state(observation(foreground=False))


@pytest.mark.parametrize(
    "mutation",
    [
        "business_success",
        "business_status",
        "verification_unavailable",
        "dispatched_only",
        "missing_receipt",
        "missing_eof",
        "post_reuses_target",
        "post_reuses_action",
        "post_reuses_pre",
        "missing_pre",
        "missing_post",
        "post_without_action_context",
        "wrong_owner",
    ],
)
def test_completion_never_passes_without_fresh_protocol_and_business_evidence(script, mutation):
    body, value = body_and_reply(script)
    assert script.verified_action(value, body, {"business_field_equals_expected": True})
    if mutation == "business_success":
        value["business_success"] = True
    elif mutation == "business_status":
        value["verification"]["business_status"] = "passed"
    elif mutation == "verification_unavailable":
        value["verification"]["status"] = "unavailable"
    elif mutation == "dispatched_only":
        value["status"] = "dispatched"
    elif mutation == "missing_receipt":
        value["receipts"].pop(0)
    elif mutation == "missing_eof":
        value["framing"]["eof_received"] = False
    elif mutation.startswith("post_reuses_"):
        source = mutation.removeprefix("post_reuses_")
        value["post_observation"]["request_id"] = {
            "target": body["target"]["observation_id"],
            "action": body["request_id"],
            "pre": value["pre_observation_id"],
        }[source]
    elif mutation == "missing_pre":
        value["pre_observation_id"] = None
    elif mutation == "missing_post":
        value["post_observation"] = None
    elif mutation == "post_without_action_context":
        value["post_observation"].pop("action_context")
    else:
        value["operation_id"] = str(uuid4())
    assert not script.verified_action(value, body, {"business_field_equals_expected": True})


@pytest.mark.parametrize("checks", [{}, {"name_matches": False}, {"name_matches": 1}])
def test_completion_with_no_actual_boolean_business_comparison_is_not_passed(script, checks):
    body, value = body_and_reply(script)
    assert not script.verified_action(value, body, checks)


@pytest.mark.parametrize(
    "phase,sent,owned,expected",
    [
        ("checking", False, True, True),
        ("pre_observation", False, True, True),
        ("pre_dispatch", False, True, False),
        ("dispatching", True, True, False),
        ("pre_observation", True, True, False),
        ("checking", None, True, False),
        ("checking", False, False, False),
    ],
)
def test_cancel_requires_exact_ownership_and_observed_pre_submission_window(
    script, phase, sent, owned, expected
):
    body, _ = body_and_reply(script)
    state = {key: body[key] for key in ("session_id", "request_id", "operation_id")}
    state.update(phase=phase, request_sent=sent)
    if not owned:
        state["operation_id"] = str(uuid4())
    assert script.owned_cancellable(state, body) is expected
    url = urlsplit(script.cancel_path("/devices/controlled-fixture/agent", body))
    assert url.path.endswith("/agent/action/cancel")
    assert parse_qs(url.query) == {
        key: [body[key]] for key in ("session_id", "request_id", "operation_id")
    }


def test_log_scan_includes_rotations_tokens_private_test_input_and_raw_keys(script, tmp_path):
    marker = "PRIVATE_ACTION_TEST_CANARY"
    assert script.log_privacy(tmp_path, TOKEN, [marker])["missing_runtime_logs"] == 1
    (tmp_path / "runtime.log").write_text("safe metadata only\n", encoding="utf-8")
    (tmp_path / "runtime.log.1").write_text(
        TOKEN + '\n{"nodes": [], "params": {}}\n', encoding="utf-8"
    )
    (tmp_path / "runtime.log.20261104").write_text(
        OLD_TOKEN + "\n" + marker + '\n{"text": "raw"}\n', encoding="utf-8"
    )
    counts = script.log_privacy(tmp_path, TOKEN, [marker], (OLD_TOKEN,))
    assert counts["missing_runtime_logs"] == 0
    assert counts["runtime_token_occurrences"] == 1
    assert counts["other_runtime_token_occurrences"] == 1
    assert counts["private_test_input_occurrences"] == 1
    assert counts["raw_observation_keys"] == 1
    assert counts["raw_action_or_private_keys"] == 2
    assert TOKEN not in json.dumps(counts) and marker not in json.dumps(counts)


@pytest.fixture
def locations(script, monkeypatch, tmp_path):
    root, private, default = (tmp_path / name for name in ("action-evidence", "private", "default"))
    private.mkdir()
    (private / "runtime.log").write_text("safe-runtime-metadata-only\n", encoding="utf-8")
    monkeypatch.setattr(script, "EVIDENCE_ROOT", root)
    monkeypatch.setattr(script, "OBSERVE_EVIDENCE_ROOT", tmp_path / "old-observe-evidence")
    monkeypatch.setattr(script, "runtime_dir", lambda **_kwargs: default)
    monkeypatch.setattr(
        script, "connection_file", lambda directory=None: (directory or default) / "runtime.json"
    )
    return SimpleNamespace(root=root, private=private, default=default)


def test_fresh_attempt_keeps_failures_and_never_reuses_evidence(script, locations):
    first = script.fresh_attempt(None, locations.private)
    old = first / "result.json"
    old.write_text('{"status": "failed"}\n', encoding="utf-8")
    second = script.fresh_attempt(None, locations.private)
    assert first.name == "physical-1" and second.name == "physical-2"
    assert old.read_text(encoding="utf-8") == '{"status": "failed"}\n'
    with pytest.raises(SystemExit, match="fresh"):
        script.fresh_attempt(first, locations.private)
    with pytest.raises(SystemExit, match="fresh"):
        script.fresh_attempt(locations.root / "attempt", locations.private)
    with pytest.raises(SystemExit, match="fresh"):
        script.fresh_attempt(
            locations.root.parent / "old-observe-evidence" / "physical-1", locations.private
        )


@pytest.mark.parametrize(
    "private_location", ["default", "evidence", "evidence_parent", "old_evidence"]
)
def test_private_runtime_must_be_disjoint_from_default_and_evidence(
    script, locations, private_location
):
    private = {
        "default": locations.default,
        "evidence": locations.root / "private",
        "evidence_parent": locations.root.parent,
        "old_evidence": script.OBSERVE_EVIDENCE_ROOT / "private",
    }[private_location]
    with pytest.raises(SystemExit, match="independent private Runtime"):
        script.fresh_attempt(None, private)
    assert not locations.root.exists()


@pytest.mark.parametrize("clear_faults", [False, True])
async def test_prepare_target_uses_only_existing_component_without_restart_or_fault_changes(
    script, monkeypatch, clear_faults
):
    calls = []

    class Backend:
        async def command(self, serial, *args, **kwargs):
            calls.append((serial, args, kwargs))
            return "package:/data/app/fixture/base.apk" if "path" in args else "Status: ok\n"

    monkeypatch.setattr(script, "AndroidBackend", Backend)
    await script.prepare_target(SERIAL, clear_faults=clear_faults)
    assert calls[0][1] == ("shell", "pm", "path", "--user", "0", PACKAGE)
    expected = ("shell", "am", "start", "-W", "--user", "0", "-n", PACKAGE + "/.FormActivity")
    if clear_faults:
        expected += ("--ez", "clear_faults", "true")
    assert calls[1][1] == expected
    assert calls[1][2] == {"timeout": 10}
    assert not any(
        value in {"-S", "force-stop", "monkey", "install", "clear"}
        for _, args, _ in calls
        for value in args
    )


class Harness:
    """Only the acceptance script runs; every transport and device dependency is a stub."""

    def __init__(
        self,
        script,
        monkeypatch,
        locations,
        *,
        api=True,
        form=True,
        cancel_sent=False,
        wrong_business=False,
        prepare_error=None,
        previous=False,
    ):
        self.script, self.locations = script, locations
        self.api, self.foreground, self.cancel_sent = api, form, cancel_sent
        self.wrong_business, self.prepare_error = wrong_business, prepare_error
        self.state = dict(ORIGINAL)
        self.calls, self.constructors, self.baselines, self.bodies = [], [], [], []
        self.session = None
        self.pending = None
        self.gate = None
        self.cancelled = False
        self.stops, self.launches = 0, 0
        self.info = SimpleNamespace(port=1234, pid=999, token=TOKEN)
        self.previous = SimpleNamespace(port=1235, pid=998, token=OLD_TOKEN) if previous else None
        monkeypatch.setattr(
            script,
            "read_connection",
            lambda path: self.info if path.parent == locations.private else self.previous,
        )

        async def baseline(serial):
            self.baselines.append(serial)
            return "private-accessibility-settings", "private-adbd-peer", {"private-forward"}, 100

        monkeypatch.setattr(script, "baseline", baseline)
        harness = self

        class Response:
            def __init__(self, code, payload):
                self.status_code, self.payload = code, payload

            async def aiter_bytes(self, _size):
                yield json.dumps(self.payload).encode()

            def json(self):
                return self.payload

        class Stream:
            def __init__(self, method, path, kwargs):
                self.method, self.path, self.kwargs = method, path, kwargs

            async def __aenter__(self):
                code, payload = await harness.respond(self.method, self.path, self.kwargs)
                return Response(code, payload)

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **kwargs):
                harness.constructors.append(kwargs)
                self.old = kwargs["base_url"].endswith(":1235")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, method, path, **kwargs):
                return Stream(method, path, kwargs)

            async def get(self, path):
                assert self.old and path == "/health"
                return Response(200, {"pid": 998})

        monkeypatch.setattr(script, "AsyncClient", Client)

    def view(self, request_id=None):
        return observation(request_id, self.state, foreground=self.foreground)

    async def respond(self, method, path, kwargs):
        import asyncio

        self.calls.append((method, path, kwargs))
        suffix = urlsplit(path).path
        if path == "/health":
            return 200, {"pid": self.info.pid}
        if path == "/openapi.json":
            return 200, {
                "paths": {
                    "/devices/{serial}/agent/action": {},
                    "/devices/{serial}/agent/action/cancel": {},
                }
                if self.api
                else {}
            }
        if kwargs.get("headers"):
            return 401, {"error": "unauthorized"}
        if path == "/devices":
            return 200, {"devices": [{"serial": SERIAL, "state": "device"}]}
        if suffix.endswith("/prepare"):
            if self.prepare_error is not None:
                raise self.prepare_error
            self.session = SESSION
            return 200, {
                "ready": True,
                "connection": {"session_id": SESSION},
                "agent": {"build_verified": True, "signature_verified_by_exact_artifact": True},
            }
        if suffix.endswith("/agent/health"):
            return 200, {
                "ready": True,
                "connection": {"session_id": SESSION, "health_authenticated": True},
                "capabilities": {"platform_operation_pending": False, "recovery_required": False},
            }
        if suffix.endswith("/observe"):
            assert kwargs["json"]["for_action"] is True
            assert kwargs["json"]["screenshot"] is False and kwargs["json"]["ocr"] is False
            return 200, self.view(kwargs["json"]["request_id"])
        if suffix.endswith("/action/cancel"):
            query = parse_qs(urlsplit(path).query)
            assert self.pending is not None
            assert query == {
                key: [self.pending[key]] for key in ("session_id", "request_id", "operation_id")
            }
            assert not self.cancel_sent
            self.cancelled = True
            self.gate.set()
            return 200, {
                "cancelled": True,
                "cleanup_complete": True,
                "rollback_guaranteed": False,
                "private_input": "PRIVATE_CANCEL_RESPONSE",
            }
        if suffix.endswith("/action"):
            body = kwargs["json"]
            self.bodies.append(body)
            kind = body["kind"]
            if kind == "tap" and self.stops == 1 and self.launches == 2:
                self.pending, self.gate = body, asyncio.Event()
                await self.gate.wait()
                value = action_reply(body, self.view(), cancelled=self.cancelled)
                self.pending = None
                return 200, value
            if kind == "set_text":
                self.state["name"] = body["params"]["text"]
                if self.wrong_business:
                    self.state["name"] = "WRONG_PRIVATE_BUSINESS_VALUE"
                    self.wrong_business = False
            elif kind == "node_click":
                field = {2: "agree", 3: "notify"}[body["target"]["node_path"][0]]
                self.state[field] = not self.state[field]
            elif kind in {"key", "stop"}:
                self.foreground = False
                if kind == "stop":
                    assert body["params"] == {"package": PACKAGE}
                    self.stops += 1
                    self.state = {"name": "", "agree": False, "notify": False}
            elif kind == "launch":
                assert body["params"] == {"package": PACKAGE, "activity": ".FormActivity"}
                self.launches += 1
                self.foreground = True
            assert kind != "clear"
            return 200, action_reply(body, self.view())
        if suffix.endswith("/connection") and method == "DELETE":
            self.session = None
            return 200, {
                "connection": {"session_id": None},
                "diagnostic": {"cleanup_pending": False},
            }
        if suffix.endswith("/cancel"):
            return 200, {"cancelled": True}
        if suffix == "/devices/" + SERIAL:
            action = None
            if self.pending is not None:
                action = {
                    key: self.pending[key] for key in ("session_id", "request_id", "operation_id")
                }
                action.update(
                    phase="dispatching" if self.cancel_sent else "pre_observation",
                    request_sent=self.cancel_sent,
                )
                if self.cancel_sent:
                    self.gate.set()
            return 200, {
                "connection": {"session_id": self.session},
                "diagnostic": {"action": action},
            }
        raise AssertionError("Unexpected stub route")

    def args(self, **kwargs):
        return argparse.Namespace(
            serial=SERIAL,
            runtime_dir=self.locations.private,
            evidence=self.locations.root / "physical-1",
            prepare_target=False,
            timeout_ms=60_000,
            clear_faults=False,
            **kwargs,
        )

    def result(self):
        return json.loads(
            (self.locations.root / "physical-1" / "result.json").read_text(encoding="utf-8")
        )


async def test_stubbed_acceptance_restores_original_values_and_keeps_only_allowlisted_evidence(
    script, monkeypatch, locations, capsys
):
    harness = Harness(script, monkeypatch, locations, previous=True)
    assert await script.run(harness.args()) == 0
    result = harness.result()
    assert result["status"] == "single_device_action_verified"
    assert result["passed_required"] == result["total_required"]
    assert result["required_sequence_completed"] is True
    assert harness.state == ORIGINAL and harness.foreground and harness.session is None
    assert harness.baselines == [SERIAL, SERIAL]
    assert result["cancellation"] == {
        "phase_at_cancel": "pre_observation",
        "request_sent_at_cancel": False,
        "cancelled": True,
        "cleanup_complete": True,
        "rollback_guaranteed": False,
    }
    assert all(body["kind"] != "clear" for body in harness.bodies)
    assert result["unverified"][0]["kind"] == "clear"
    output = json.dumps(result) + capsys.readouterr().out
    canary = next(body["params"]["text"] for body in harness.bodies if body["kind"] == "set_text")
    for secret in (
        TOKEN,
        OLD_TOKEN,
        PRIVATE_NAME,
        canary,
        "CONTROLLED_TITLE_NOT_EVIDENCE",
        "PRIVATE_CANCEL_RESPONSE",
        "private-accessibility-settings",
        "private-adbd-peer",
        "private-forward",
    ):
        assert secret not in output
    assert not keys(result) & {
        "params",
        "data",
        "nodes",
        "node_path",
        "blocks",
        "base64",
        "text",
        "token",
    }
    preparation = next(
        kwargs["json"] for _, path, kwargs in harness.calls if path.endswith("/prepare")
    )
    assert preparation["install"] is False
    assert "manual_recovery_confirmed" not in preparation
    assert harness.constructors[0]["timeout"] >= 75
    ids = [body["request_id"] for body in harness.bodies]
    operations = [body["operation_id"] for body in harness.bodies]
    assert len(ids) == len(set(ids)) and len(operations) == len(set(operations))


async def test_missing_form_exits_manual_prepare_without_navigation_or_actions(
    script, monkeypatch, locations
):
    harness = Harness(script, monkeypatch, locations, form=False)
    assert await script.run(harness.args()) == 1
    assert harness.result()["blocker"] == "manual_prepare_required"
    assert harness.bodies == [] and harness.launches == 0
    assert harness.session is None


async def test_missing_action_api_is_reported_without_preparing_or_touching_device(
    script, monkeypatch, locations
):
    harness = Harness(script, monkeypatch, locations, api=False)
    assert await script.run(harness.args()) == 1
    assert harness.result()["blocker"] == "action_api_unavailable"
    assert harness.baselines == [] and harness.bodies == []
    assert not any(path.endswith("/prepare") for _, path, _ in harness.calls)


async def test_absent_agent_requires_manual_prepare_without_starting_service(
    script, monkeypatch, locations
):
    harness = Harness(script, monkeypatch, locations)

    async def absent_process_baseline(_serial):
        return "settings", "peer", set(), None

    monkeypatch.setattr(script, "baseline", absent_process_baseline)
    assert await script.run(harness.args()) == 1
    assert harness.result()["blocker"] == "manual_prepare_required"
    assert not any(path.endswith("/prepare") for _, path, _ in harness.calls)


async def test_missed_cancel_window_does_not_cancel_already_submitted_action(
    script, monkeypatch, locations
):
    harness = Harness(script, monkeypatch, locations, cancel_sent=True)
    assert await script.run(harness.args()) == 1
    result = harness.result()
    assert result["blocker"] == "cancel_window_not_observed"
    assert not any("/action/cancel?" in path for _, path, _ in harness.calls)
    assert harness.state == ORIGINAL and harness.session is None
    assert any(
        check["name"] == "uncancelled_title_action_post_business" and check["passed"]
        for check in result["checks"]
    )


async def test_completed_action_with_wrong_business_value_fails_and_restores_state(
    script, monkeypatch, locations
):
    harness = Harness(script, monkeypatch, locations, wrong_business=True)
    assert await script.run(harness.args()) == 1
    result = harness.result()
    action = next(action for action in result["actions"] if action["name"] == "set_text_ascii")
    assert action["action_completed"] is True
    assert action["business_checks"]["name_equals_expected"] is False
    assert result["blocker"] == "action_receipt_or_business_failed"
    assert harness.state == ORIGINAL and harness.session is None
    assert "WRONG_PRIVATE_BUSINESS_VALUE" not in json.dumps(result)


async def test_unexpected_missing_page_is_not_recovered_with_home_launch_or_blind_navigation(
    script, monkeypatch, locations
):
    harness = Harness(script, monkeypatch, locations)
    respond = harness.respond

    async def changed_page(method, path, kwargs):
        code, value = await respond(method, path, kwargs)
        if path.endswith("/action") and kwargs.get("json", {}).get("kind") == "tap":
            harness.foreground = False
            value["post_observation"] = harness.view()
        return code, value

    monkeypatch.setattr(harness, "respond", changed_page)
    assert await script.run(harness.args()) == 1
    result = harness.result()
    assert result["manual_restore_required"] is True
    assert [body["kind"] for body in harness.bodies] == ["tap"]
    assert harness.launches == 0 and harness.stops == 0


async def test_uncertain_action_is_not_replayed_or_followed_by_mutating_recovery(
    script, monkeypatch, locations
):
    harness = Harness(script, monkeypatch, locations)
    respond = harness.respond

    async def uncertain(method, path, kwargs):
        code, value = await respond(method, path, kwargs)
        if path.endswith("/action") and kwargs.get("json", {}).get("kind") == "tap":
            value.update(
                status="uncertain",
                action_completed=False,
                recovery_required=True,
                post_observation=None,
                error={"code": "action_uncertain"},
            )
        return code, value

    monkeypatch.setattr(harness, "respond", uncertain)
    assert await script.run(harness.args()) == 1
    assert harness.result()["manual_restore_required"] is True
    assert [body["kind"] for body in harness.bodies] == ["tap"]
    assert not any("/action/cancel?" in path for _, path, _ in harness.calls)
    assert sum(path.endswith("/prepare") for _, path, _ in harness.calls) == 1


async def test_existing_foreign_session_is_not_prepared_disconnected_or_cancelled(
    script, monkeypatch, locations
):
    harness = Harness(script, monkeypatch, locations)
    foreign = harness.session = str(uuid4())
    assert await script.run(harness.args()) == 1
    assert harness.result()["blocker"] == "independent_runtime_session_already_owned"
    assert harness.session == foreign and harness.bodies == []
    assert not any(
        path.endswith("/prepare") or method == "DELETE" or "/cancel?" in path
        for method, path, _ in harness.calls
    )


async def test_changed_agent_pid_cannot_pass_and_does_not_trigger_restart(
    script, monkeypatch, locations
):
    harness = Harness(script, monkeypatch, locations)
    count = 0

    async def changed_baseline(_serial):
        nonlocal count
        count += 1
        return "settings", "peer", set(), 100 if count == 1 else 101

    monkeypatch.setattr(script, "baseline", changed_baseline)
    assert await script.run(harness.args()) == 1
    result = harness.result()
    assert any(
        check["name"] == "existing_agent_pid_not_restarted" and not check["passed"]
        for check in result["checks"]
    )
    assert result["status"] == "blocked_or_failed"
    assert sum(path.endswith("/prepare") for _, path, _ in harness.calls) == 1


async def test_cancel_cleanup_failure_is_reported_as_api_blocker_not_success(
    script, monkeypatch, locations
):
    harness = Harness(script, monkeypatch, locations)
    respond = harness.respond

    async def incomplete_cleanup(method, path, kwargs):
        code, value = await respond(method, path, kwargs)
        if "/action/cancel?" in path:
            value["cleanup_complete"] = False
        return code, value

    monkeypatch.setattr(harness, "respond", incomplete_cleanup)
    assert await script.run(harness.args()) == 1
    result = harness.result()
    assert result["blocker"] == "cancel_api_cleanup_unverified"
    assert result["cancellation"]["cleanup_complete"] is False
    assert result["status"] == "blocked_or_failed"


@pytest.mark.parametrize("error_kind", ["timeout", "private_fault", "private_exception"])
async def test_prepare_failure_preserves_attempt_and_cancels_only_known_prepare_operation(
    script, monkeypatch, locations, error_kind
):
    secret = "PRIVATE_EXCEPTION_INPUT_NEVER_RETAINED"
    error = {
        "timeout": TimeoutError(),
        "private_fault": script.ConnectionFault(secret),
        "private_exception": RuntimeError(secret),
    }[error_kind]
    harness = Harness(script, monkeypatch, locations, prepare_error=error)
    assert await script.run(harness.args()) == 1
    result = harness.result()
    preparation = next(
        kwargs["json"] for _, path, kwargs in harness.calls if path.endswith("/prepare")
    )
    assert any(
        path.endswith("/cancel?operation_id=" + preparation["operation_id"])
        for _, path, _ in harness.calls
    )
    assert result["required_sequence_completed"] is False
    assert result["status"] == "blocked_or_failed"
    assert secret not in json.dumps(result) and TOKEN not in json.dumps(result)


async def test_missing_independent_runtime_keeps_failure_without_live_calls(
    script, monkeypatch, locations
):
    monkeypatch.setattr(script, "read_connection", lambda _path: None)
    args = argparse.Namespace(
        serial=SERIAL,
        runtime_dir=locations.private,
        evidence=None,
        prepare_target=False,
        timeout_ms=60_000,
    )
    assert await script.run(args) == 1
    result = json.loads((locations.root / "physical-1" / "result.json").read_text(encoding="utf-8"))
    assert result["blocker"] == "independent_runtime_unavailable"
    assert result["actions"] == [] and result["status"] == "blocked_or_failed"


async def test_rotated_runtime_log_leak_cannot_pass_acceptance(script, monkeypatch, locations):
    harness = Harness(script, monkeypatch, locations)
    (locations.private / "runtime.log.1").write_text(TOKEN + '\n{"params": {}}\n', encoding="utf-8")
    assert await script.run(harness.args()) == 1
    result = harness.result()
    assert result["log_privacy_counts"]["runtime_token_occurrences"] == 1
    assert result["log_privacy_counts"]["raw_action_or_private_keys"] == 1
    assert result["status"] == "blocked_or_failed"
    assert TOKEN not in json.dumps(result)


def test_log_scan_ignores_structured_null_exception_but_flags_real_prose(script, tmp_path):
    (tmp_path / "runtime.log").write_text(
        '{"message":"Runtime listening","exception":null,"traceback":null}\n'
        '{"message":"request completed","exception":null}\n',
        encoding="utf-8",
    )
    assert script.log_privacy(tmp_path, TOKEN, ["CANARY"])["raw_action_or_private_keys"] == 0
    (tmp_path / "runtime.log").write_text(
        '{"exception":"private platform failure","traceback":null}\n',
        encoding="utf-8",
    )
    counts = script.log_privacy(tmp_path, TOKEN, ["CANARY"])
    assert counts["raw_action_or_private_keys"] == 1
