"""Local HTTP/WS API for the GUI (loopback only, token protected).

Auth:
- HTTP: ``Authorization: Bearer <token>`` (CORS preflight is exempt, it carries no data).
- WS: ``?token=<token>`` query parameter (browsers cannot set WS headers).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import Context
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from anyio import CancelScope
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pulse_observability import metrics_interval, new_trace_id, redact_text
from pulse_runtime import __version__
from pulse_runtime.api.contracts import (
    DebugBreakpointsRequest,
    DebugSnapshot,
    EventPage,
    HealthResponse,
    LocateHttpError,
    LocateRejected,
    LocateRequest,
    LocateResult,
    PreflightRequest,
    PreflightResponse,
    ProjectDetail,
    ProjectList,
    ProjectOpenRequest,
    RecordingDetail,
    RecordingList,
    RecordingStartRequest,
    RecordingSummary,
    RunDetail,
    RunList,
    RunRequest,
    RunResultResponse,
    RunSummary,
    TargetAppDiagnostic,
    TargetAppResponse,
)
from pulse_runtime.api.errors import ApiRefusal
from pulse_runtime.api.events import EventBus
from pulse_runtime.api.projects import ProjectRegistry
from pulse_runtime.api.recording import RecordingManager
from pulse_runtime.api.runs import RunManager
from pulse_runtime.api.telemetry import ApiMetrics, RequestTelemetry
from pulse_runtime.drivers.android.actions import ActionBody
from pulse_runtime.drivers.android.agent_observe import (
    _finite_float,
    _reject_constant,
    _unique_object,
)
from pulse_runtime.drivers.android.agent_transport import (
    TARGET_APP_PACKAGE,
    ConnectionFault,
    finish_cleanup,
    validate_serial,
)
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.drivers.android.scrcpy_video import SourceError, VideoOptions, adb
from pulse_runtime.stream.hub import CaptureOrientationConflict, StreamHub
from pulse_runtime.stream.relay_publisher import RelayPublisher

# Renderer origins: Vite dev server on localhost, or a packaged file:// page (Origin: null).
ALLOWED_ORIGIN_REGEX = r"^(http://(localhost|127\.0\.0\.1)(:\d+)?|null)$"
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_BAD_REQUEST = 4400
SERIAL_RE = r"^[A-Za-z0-9._:-]{1,64}$"
log = logging.getLogger(__name__)


def video_options(params: dict[str, str]) -> VideoOptions:
    """Parse video limits and the finite ``capture_orientation`` query profile."""
    base = VideoOptions()
    return VideoOptions(
        max_size=int(params.get("max_size", base.max_size)),
        video_bit_rate=int(params.get("bit_rate", base.video_bit_rate)),
        max_fps=int(params.get("max_fps", base.max_fps)),
        i_frame_interval=int(params.get("i_frame_interval", base.i_frame_interval)),
        capture_orientation=cast(
            "Literal['display', 'natural']",
            params.get("capture_orientation", base.capture_orientation),
        ),
    )


class TriggerBody(BaseModel):
    """Debug-only visible change for latency measurement (no general control channel: P5)."""

    kind: str = Field(pattern="^(tap|keyevent)$")
    x: int = Field(default=0, ge=0, le=10000)
    y: int = Field(default=0, ge=0, le=10000)
    code: int = Field(default=0, ge=0, le=400)


class RelayBody(BaseModel):
    relay_url: str = Field(pattern=r"^wss?://[^\s]+$")
    channel: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    token: str = Field(min_length=1, max_length=512)
    max_size: int = Field(default=1080, ge=0, le=8192)
    bit_rate: int = Field(default=4_000_000, ge=100_000, le=100_000_000)
    max_fps: int = Field(default=60, ge=0, le=240)


class RelayKeepalive(BaseModel):
    publisher_id: str = Field(min_length=1, max_length=64)


class AgentConnectBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    timeout_ms: int = Field(default=30_000, ge=1000, le=120_000)
    operation_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    manual_recovery_confirmed: bool = False


class AgentPrepareBody(AgentConnectBody):
    install: bool = False
    enable_accessibility: bool = True


class ControlAcquireBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    source: str = Field(pattern="^(auto|manual|recorder|ai)$")
    run_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )


class ControlTakeoverBody(ControlAcquireBody):
    timeout_ms: int = Field(default=30_000, ge=1000, le=60_000)


class ControlReleaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    control_epoch: int = Field(ge=0, le=2**31 - 1)
    wait_for_idle_ms: int = Field(default=0, ge=0, le=60_000)
    run_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )


class ControlRecoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    confirmed: bool


class AgentObserveBody(BaseModel):
    """Observation-only draft, not a case schema or a general device operation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    request_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    tree: bool = True
    screenshot: bool = False
    ocr: bool = False
    display_id: int = Field(default=0, ge=0, le=255)
    timeout_ms: int = Field(default=15_000, ge=1000, le=30_000)
    for_action: bool = False

    @model_validator(mode="after")
    def require_source(self) -> AgentObserveBody:
        if not (self.tree or self.screenshot or self.ocr):
            raise ValueError("source_required")
        return self


def token_matches(expected: str, provided: str | None) -> bool:
    return provided is not None and hmac.compare_digest(expected.encode(), provided.encode())


def bearer(header: str | None) -> str | None:
    if header and header.startswith("Bearer "):
        return header[len("Bearer ") :]
    return None


def create_app(
    token: str,
    *,
    heartbeat_interval: float = 2.0,
    hub: StreamHub | None = None,
    device_manager: DeviceConnectionManager | None = None,
    project_registry: ProjectRegistry | None = None,
    event_bus: EventBus | None = None,
    recording_manager: RecordingManager | None = None,
    run_manager: RunManager | None = None,
    evidence_root: Path | None = None,
) -> FastAPI:
    started = time.monotonic()
    streams = hub if hub is not None else StreamHub()
    connections = device_manager if device_manager is not None else DeviceConnectionManager()
    projects = project_registry if project_registry is not None else ProjectRegistry()
    events = event_bus if event_bus is not None else EventBus()
    recordings = recording_manager if recording_manager is not None else RecordingManager(events)
    runs = (
        run_manager
        if run_manager is not None
        else RunManager(projects, events, connections=connections, evidence_root=evidence_root)
    )
    publishers: dict[str, RelayPublisher] = {}
    publisher_lock = asyncio.Lock()
    api_metrics = ApiMetrics()

    def report_metrics() -> None:
        log.info(
            "Runtime metrics",
            extra={
                "event": "runtime.metrics",
                "scope": "process",
                "trace_id": None,
                "run_id": None,
                "serial": None,
                "session_id": None,
                "channel": None,
                "metrics": api_metrics.snapshot(),
                "active_captures": len(streams.streams),
                "active_publishers": sum(
                    p.task is not None and not p.task.done() for p in publishers.values()
                ),
            },
        )
        for device in streams.streams.values():
            device.log_metrics()
        for publisher in publishers.values():
            publisher.log_metrics()

    async def metrics_loop(interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            report_metrics()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        metrics_task = asyncio.create_task(metrics_loop(metrics_interval()), context=Context())
        connections.start()
        try:
            yield
        finally:
            metrics_task.cancel()
            await asyncio.gather(metrics_task, return_exceptions=True)
            report_metrics()
            for pub in list(publishers.values()):
                await pub.stop()
            try:
                await runs.close()
            finally:
                events.close()
                try:
                    await connections.close()
                finally:
                    await streams.close()

    app = FastAPI(
        title="Pulse Runtime", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.device_connections = connections
    app.state.projects = projects
    app.state.events = events
    app.state.recordings = recordings
    app.state.runs = runs

    @app.middleware("http")
    async def require_token(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method != "OPTIONS" and not token_matches(
            token, bearer(request.headers.get("authorization"))
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    # Added after the auth middleware so it wraps it: 401s still carry CORS headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=ALLOWED_ORIGIN_REGEX,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.add_middleware(RequestTelemetry, metrics=api_metrics)

    @app.exception_handler(ApiRefusal)
    async def api_refusal(_request: Request, exc: ApiRefusal) -> Response:
        return JSONResponse(exc.as_dict(), status_code=exc.status_code)

    @app.exception_handler(ConnectionFault)
    async def connection_error(_request: Request, exc: ConnectionFault) -> Response:
        status = 400 if exc.code in {"invalid_serial", "invalid_message"} else 409
        return JSONResponse({"error": exc.code}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        if request.url.path.startswith("/devices"):
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, _exc: Exception) -> Response:
        # Starlette generates uncaught 500s outside user middleware. Keep its default body and
        # attach the same scope-local trace even after the ContextVar has been reset.
        trace = new_trace_id(request.scope.get("state", {}).get("pulse_trace_id"))
        return PlainTextResponse(
            "Internal Server Error", status_code=500, headers={"X-Trace-ID": trace}
        )

    @app.get("/health", response_model=HealthResponse, operation_id="getHealth")
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            version=__version__,
            pid=os.getpid(),
            uptime_s=round(time.monotonic() - started, 3),
        )

    @app.websocket("/ws/heartbeat")
    async def heartbeat(ws: WebSocket) -> None:
        if not token_matches(token, ws.query_params.get("token")):
            await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return
        await ws.accept()
        seq = 0

        async def receive_pings() -> None:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await ws.send_json({"type": "pong", "id": msg.get("id"), "ts": time.time()})

        receiver = asyncio.create_task(receive_pings())
        try:
            while not receiver.done():
                seq += 1
                await ws.send_json(
                    {"type": "heartbeat", "seq": seq, "ts": time.time(), "pid": os.getpid()}
                )
                await asyncio.wait({receiver}, timeout=heartbeat_interval)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

    @app.websocket("/ws/events")
    async def event_stream(ws: WebSocket) -> None:
        if not token_matches(token, ws.query_params.get("token")):
            await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return
        if any(
            len(ws.query_params.getlist(key)) != 1
            for key in ("token", "since", "run_id")
            if key in ws.query_params
        ):
            await ws.close(code=WS_CLOSE_BAD_REQUEST, reason="duplicate query parameter")
            return
        raw_since = ws.query_params.get("since", "0")
        run_id = ws.query_params.get("run_id")
        try:
            since = int(raw_since)
            if since < 0:
                raise ValueError("negative since")
            if (
                run_id is not None
                and re.fullmatch(
                    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", run_id
                )
                is None
            ):
                raise ValueError("bad run_id")
        except ValueError as exc:
            await ws.close(code=WS_CLOSE_BAD_REQUEST, reason=str(exc)[:100])
            return
        await ws.accept()
        subscriber = events.subscribe(run_id)
        page = events.page(since=since, run_id=run_id, limit=5000)

        async def drain_incoming() -> None:
            while True:
                await ws.receive_text()

        async def send_events() -> None:
            await ws.send_json(
                {
                    "v": 1,
                    "type": "stream.hello",
                    "msg_id": str(uuid4()),
                    "seq": 0,
                    "ts": time.time(),
                    "run_id": run_id,
                    "device_id": None,
                    "body": {
                        "oldest_seq": page.oldest_seq,
                        "latest_seq": page.latest_seq,
                        "replayed": len(page.events),
                        "gap": page.gap,
                    },
                }
            )
            for replay in page.events:
                await ws.send_json(replay.model_dump(mode="json"))
            while True:
                event = await subscriber.next()
                if event is None:
                    return
                await ws.send_json(event.model_dump(mode="json"))

        reader = asyncio.create_task(drain_incoming())
        sender = asyncio.create_task(send_events())
        try:
            done, _ = await asyncio.wait({reader, sender}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            with CancelScope(shield=True):
                reader.cancel()
                sender.cancel()
                await asyncio.gather(reader, sender, return_exceptions=True)
                events.unsubscribe(subscriber)
                with contextlib.suppress(WebSocketDisconnect, RuntimeError, TimeoutError):
                    async with asyncio.timeout(1):
                        await ws.close()

    # ------------------------------------------------------------ projects (P2.2)

    @app.get("/projects", response_model=ProjectList, operation_id="listProjects")
    async def list_projects() -> ProjectList:
        return ProjectList(projects=projects.list())

    @app.post(
        "/projects", response_model=ProjectDetail, status_code=201, operation_id="openProject"
    )
    async def open_project(body: ProjectOpenRequest) -> ProjectDetail:
        return await asyncio.to_thread(projects.open, body.path)

    @app.get("/projects/{project_id}", response_model=ProjectDetail, operation_id="getProject")
    async def get_project(project_id: str) -> ProjectDetail:
        return await asyncio.to_thread(projects.detail, project_id)

    @app.delete("/projects/{project_id}", operation_id="closeProject")
    async def close_project(project_id: str) -> dict[str, object]:
        return {"closed": projects.close(project_id), "project_id": project_id}

    @app.get("/projects/{project_id}/cases", operation_id="listCases")
    async def list_cases(project_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(projects.cases, project_id)

    @app.get("/projects/{project_id}/cases/{case_id}", operation_id="getCase")
    async def get_case(project_id: str, case_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(projects.case, project_id, case_id)

    @app.get("/projects/{project_id}/profiles/{profile_id}", operation_id="getProfile")
    async def get_profile(project_id: str, profile_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(projects.profile, project_id, profile_id)

    @app.get("/projects/{project_id}/flows/{flow_id}", operation_id="getFlow")
    async def get_flow(project_id: str, flow_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(projects.flow, project_id, flow_id)

    @app.get("/projects/{project_id}/data/{data_id}", operation_id="getDataSet")
    async def get_data(project_id: str, data_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(projects.data, project_id, data_id)

    @app.post(
        "/projects/{project_id}/preflight",
        response_model=PreflightResponse,
        operation_id="runPreflight",
    )
    async def preflight_project(project_id: str, body: PreflightRequest) -> PreflightResponse:
        return await asyncio.to_thread(projects.preflight, project_id, body)

    # ------------------------------------------------------------ runs (P2.2)

    @app.post("/runs", response_model=RunSummary, status_code=202, operation_id="startRun")
    async def start_run(body: RunRequest) -> RunSummary:
        return runs.start(body)

    @app.get("/runs", response_model=RunList, operation_id="listRuns")
    async def list_runs() -> RunList:
        return RunList(runs=runs.list())

    @app.get("/runs/{run_id}", response_model=RunDetail, operation_id="getRun")
    async def get_run(run_id: str) -> RunDetail:
        return runs.detail(run_id)

    @app.get("/runs/{run_id}/result", response_model=RunResultResponse, operation_id="getRunResult")
    async def get_run_result(run_id: str) -> RunResultResponse:
        return runs.result(run_id)

    @app.post("/runs/{run_id}/stop", response_model=RunSummary, operation_id="stopRun")
    async def stop_run(run_id: str) -> RunSummary:
        return await runs.stop(run_id)

    @app.get("/runs/{run_id}/events", response_model=EventPage, operation_id="getRunEvents")
    async def get_run_events(
        run_id: str,
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> EventPage:
        runs.get(run_id)
        return events.page(since=since, run_id=run_id, limit=limit)

    @app.get("/runs/{run_id}/report", operation_id="getRunReport")
    async def get_run_report(run_id: str) -> dict[str, Any]:
        record = runs.get(run_id)
        directory = record.summary.evidence.directory
        if directory is None:
            raise ApiRefusal("report_unavailable", 404, run_id=run_id)
        path = Path(directory) / "result.json"
        if not path.is_file():
            raise ApiRefusal("report_unavailable", 404, run_id=run_id)
        return cast(dict[str, Any], json.loads(await asyncio.to_thread(path.read_text, "utf-8")))

    @app.get(
        "/runs/{run_id}/report/html",
        response_class=HTMLResponse,
        operation_id="getRunReportHtml",
    )
    async def get_run_report_html(run_id: str) -> HTMLResponse:
        record = runs.get(run_id)
        directory = record.summary.evidence.directory
        if directory is None:
            raise ApiRefusal("report_unavailable", 404, run_id=run_id)
        path = Path(directory) / "result.html"
        if not path.is_file():
            raise ApiRefusal("report_unavailable", 404, run_id=run_id)
        return HTMLResponse(await asyncio.to_thread(path.read_text, "utf-8"))

    # ------------------------------------------------------------ debugger (P2.2 transport)

    @app.get("/runs/{run_id}/debug", response_model=DebugSnapshot, operation_id="getDebug")
    async def get_debug(run_id: str) -> DebugSnapshot:
        return runs.snapshot_debug(run_id)

    @app.put(
        "/runs/{run_id}/debug/breakpoints",
        response_model=DebugSnapshot,
        operation_id="setBreakpoints",
    )
    async def set_breakpoints(run_id: str, body: DebugBreakpointsRequest) -> DebugSnapshot:
        return runs.set_breakpoints(run_id, body)

    @app.post("/runs/{run_id}/debug/pause", response_model=DebugSnapshot, operation_id="pauseRun")
    async def pause_run(run_id: str) -> DebugSnapshot:
        return runs.pause(run_id)

    @app.post("/runs/{run_id}/debug/resume", response_model=DebugSnapshot, operation_id="resumeRun")
    async def resume_run(run_id: str) -> DebugSnapshot:
        return runs.resume(run_id)

    @app.post("/runs/{run_id}/debug/step", response_model=DebugSnapshot, operation_id="stepRun")
    async def step_run(run_id: str) -> DebugSnapshot:
        return runs.step(run_id)

    # ------------------------------------------------------------ raw recording (P2.2)

    @app.get("/recordings", response_model=RecordingList, operation_id="listRecordings")
    async def list_recordings() -> RecordingList:
        return RecordingList(recordings=recordings.list())

    @app.post(
        "/recordings",
        response_model=RecordingSummary,
        status_code=201,
        operation_id="startRecording",
    )
    async def start_recording(body: RecordingStartRequest) -> RecordingSummary:
        return recordings.start(body)

    @app.get(
        "/recordings/{recording_id}", response_model=RecordingDetail, operation_id="getRecording"
    )
    async def get_recording(recording_id: str) -> RecordingDetail:
        return recordings.detail(recording_id)

    @app.post(
        "/recordings/{recording_id}/pause",
        response_model=RecordingSummary,
        operation_id="pauseRecording",
    )
    async def pause_recording(recording_id: str) -> RecordingSummary:
        return recordings.pause(recording_id)

    @app.post(
        "/recordings/{recording_id}/resume",
        response_model=RecordingSummary,
        operation_id="resumeRecording",
    )
    async def resume_recording(recording_id: str) -> RecordingSummary:
        return recordings.resume(recording_id)

    @app.post(
        "/recordings/{recording_id}/stop",
        response_model=RecordingDetail,
        operation_id="stopRecording",
    )
    async def stop_recording(recording_id: str) -> RecordingDetail:
        return recordings.stop(recording_id)

    # ------------------------------------------------------------ event backfill (P2.2)

    @app.get("/events", response_model=EventPage, operation_id="getEvents")
    async def get_events(
        since: int = Query(default=0, ge=0),
        run_id: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> EventPage:
        return events.page(since=since, run_id=run_id, limit=limit)

    @app.get("/devices", operation_id="listDevices")
    async def devices() -> dict[str, object]:
        return await connections.list_devices()

    @app.get("/devices/{serial}", operation_id="getDevice")
    async def device_status(serial: str) -> dict[str, object]:
        return await connections.status(serial)

    @app.get(
        "/devices/{serial}/target-app",
        response_model=TargetAppResponse,
        operation_id="getTargetApp",
    )
    async def target_app(serial: str, request: Request) -> TargetAppResponse:
        validate_serial(serial)
        if (
            request.query_params
            or request.headers.get("content-length", "0") != "0"
            or ("transfer-encoding" in request.headers)
        ):
            raise ApiRefusal("invalid_request", 422)
        installed = None
        status: Literal["installed", "missing", "unavailable", "failed"] = "failed"
        code = "target_app_query_failed"
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if chunk:
                        raise ApiRefusal("invalid_request", 422)
                installed = await connections.backend.target_app(serial)
            status = "installed" if installed is not None else "missing"
            code = (
                "target_app_missing"
                if installed is None
                else "target_app_version_unavailable"
                if installed.version_name is None or installed.version_code is None
                else "target_app_installed"
            )
        except TimeoutError:
            status, code = "unavailable", "timeout"
        except ConnectionFault as exc:
            unavailable = {
                "adb_unavailable",
                "adb_offline",
                "adb_unauthorized",
                "adb_permission_denied",
                "device_disconnected",
                "device_unavailable",
                "unsupported_android_user",
                "timeout",
            }
            if exc.code in unavailable:
                status, code = "unavailable", exc.code
            elif exc.code == "target_app_output_invalid":
                code = exc.code
        except ApiRefusal:
            raise
        except Exception:
            installed = None
            status, code = "failed", "target_app_query_failed"
        guidance = {
            "target_app_installed": "Installation was observed for Android user 0 only; this does "
            "not verify readiness, target cases, or an exact build.",
            "target_app_version_unavailable": "Installation was observed for Android user 0, but "
            "some version information is unavailable. Readiness and target cases are not verified.",
            "target_app_missing": "The target app was not listed for Android user 0. Install the "
            "fixture manually before checking again; Runtime does not install or start it.",
            "unsupported_android_user": "This check only supports current Android user 0. "
            "Runtime does not switch users or infer installation for another user.",
            "adb_unauthorized": "Authorize this computer on the selected device, then retry. "
            "Runtime never accepts device prompts.",
            "adb_permission_denied": "ADB access was denied. Check device access manually.",
            "adb_offline": "The selected device is offline. Bring it online before retrying.",
            "device_disconnected": "The selected device was not found. "
            "Reconnect it before retrying.",
            "device_unavailable": "The selected device is not in normal Android mode.",
            "adb_unavailable": "ADB is unavailable. Check the local ADB setup before retrying.",
            "timeout": "The read-only check timed out. Retry when the selected device responds.",
        }.get(code, "The package information could not be read safely. Retry the read-only check.")
        return TargetAppResponse(
            serial=serial,
            package=TARGET_APP_PACKAGE,
            status=status,
            version_name=installed.version_name if installed else None,
            version_code=installed.version_code if installed else None,
            observed_at_ms=time.time_ns() // 1_000_000,
            diagnostic=TargetAppDiagnostic(code=code, guidance=guidance),
        )

    @app.post("/devices/{serial}/agent/prepare", operation_id="prepareAgent")
    async def prepare_agent(serial: str, body: AgentPrepareBody) -> dict[str, object]:
        return await connections.prepare(
            serial,
            install=body.install,
            enable_accessibility=body.enable_accessibility,
            manual_recovery_confirmed=body.manual_recovery_confirmed,
            timeout=body.timeout_ms / 1000,
            operation_id=body.operation_id,
        )

    @app.post("/devices/{serial}/agent/connect", operation_id="connectAgent")
    async def connect_agent(serial: str, body: AgentConnectBody) -> dict[str, object]:
        return await connections.connect(
            serial,
            manual_recovery_confirmed=body.manual_recovery_confirmed,
            timeout=body.timeout_ms / 1000,
            operation_id=body.operation_id,
        )

    @app.get("/devices/{serial}/agent/health", operation_id="getAgentHealth")
    async def agent_health(serial: str, session_id: str) -> dict[str, object]:
        return await connections.health(serial, session_id)

    @app.get("/devices/{serial}/agent/geometry", operation_id="getAgentGeometry")
    async def agent_geometry(serial: str, session_id: str) -> dict[str, object]:
        return await connections.geometry(serial, session_id)

    @app.get("/devices/{serial}/agent/control", operation_id="getControl")
    async def control_status(serial: str, session_id: str | None = None) -> dict[str, object]:
        return await connections.control(serial, session_id)

    @app.get("/devices/{serial}/agent/ledger", operation_id="listLedgerActions")
    async def ledger_actions(
        serial: str, limit: int = Query(default=50, ge=1, le=200)
    ) -> dict[str, object]:
        return connections.ledger_actions(serial, limit=limit)

    @app.post("/devices/{serial}/agent/control/acquire", operation_id="acquireControl")
    async def acquire_control(serial: str, body: ControlAcquireBody) -> dict[str, object]:
        return await connections.acquire_control(
            serial,
            session_id=body.session_id,
            source=body.source,
            run_id=body.run_id,
        )

    @app.post("/devices/{serial}/agent/control/takeover", operation_id="takeOverControl")
    async def takeover_control(serial: str, body: ControlTakeoverBody) -> dict[str, object]:
        return await connections.take_over_control(
            serial,
            session_id=body.session_id,
            source=body.source,
            run_id=body.run_id,
            timeout=body.timeout_ms / 1000,
        )

    @app.post("/devices/{serial}/agent/control/release", operation_id="releaseControl")
    async def release_control(serial: str, body: ControlReleaseBody) -> dict[str, object]:
        if body.wait_for_idle_ms:
            return await connections.release_control(
                serial,
                session_id=body.session_id,
                control_epoch=body.control_epoch,
                run_id=body.run_id,
                wait_for_idle_ms=body.wait_for_idle_ms,
            )
        return await connections.release_control(
            serial,
            session_id=body.session_id,
            control_epoch=body.control_epoch,
            run_id=body.run_id,
        )

    @app.post("/devices/{serial}/agent/control/recovery", operation_id="acknowledgeRecovery")
    async def recovery_control(serial: str, body: ControlRecoveryBody) -> dict[str, object]:
        return await connections.acknowledge_recovery(
            serial, session_id=body.session_id, confirmed=body.confirmed
        )

    @app.get("/devices/{serial}/agent/capabilities", operation_id="getAgentCapabilities")
    async def agent_capabilities(serial: str, session_id: str) -> dict[str, object]:
        snapshot = await connections.health(serial, session_id)
        return {
            "serial": serial,
            "session_id": session_id,
            "protocol_version": 1,
            "status": snapshot["status"],
            "capabilities": snapshot["capabilities"],
        }

    @app.post("/devices/{serial}/agent/observe", operation_id="observeAgent")
    async def observe_agent(
        serial: str, body: AgentObserveBody, request: Request
    ) -> dict[str, object]:
        worker = asyncio.create_task(
            connections.observe(
                serial,
                body.session_id,
                body.request_id,
                tree=body.tree,
                screenshot=body.screenshot,
                ocr=body.ocr,
                display_id=body.display_id,
                timeout=body.timeout_ms / 1000,
                for_action=body.for_action,
            )
        )

        async def peer_disconnect() -> None:
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    worker.cancel()
                    return

        peer = asyncio.create_task(peer_disconnect())
        try:
            return await worker
        finally:
            with CancelScope(shield=True):
                if not worker.done():
                    worker.cancel()
                peer.cancel()
                await finish_cleanup(asyncio.gather(worker, peer, return_exceptions=True))

    @app.post("/devices/{serial}/agent/observe/cancel", operation_id="cancelObservation")
    async def cancel_observe(serial: str, session_id: str, request_id: str) -> dict[str, object]:
        return await connections.cancel_observation(serial, session_id, request_id)

    @app.post(
        "/devices/{serial}/agent/locate",
        operation_id="locate",
        response_model=LocateResult,
        responses={
            400: {"model": LocateHttpError},
            401: {"model": LocateHttpError},
            409: {"model": LocateHttpError},
            413: {"model": LocateRejected},
            422: {"model": LocateRejected},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/LocateRequest"}}
                },
            }
        },
    )
    async def locate_agent(serial: str, request: Request) -> Response:
        try:
            if request.query_params or request.headers.get("content-type", "").lower() not in {
                "application/json",
                "application/json; charset=utf-8",
            }:
                raise ValueError("invalid_request")
            data = bytearray()
            async with asyncio.timeout(2):
                async for part in request.stream():
                    data.extend(part)
                    if len(data) > 4_194_304:
                        return JSONResponse(
                            {"status": "rejected", "error": "request_too_large"}, status_code=413
                        )
            parsed = json.loads(
                data.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
            body = LocateRequest.model_validate(parsed)
        except (
            ValueError,
            UnicodeError,
            RecursionError,
            ValidationError,
            ConnectionFault,
            TimeoutError,
        ):
            return JSONResponse({"status": "rejected", "error": "invalid_request"}, status_code=422)
        worker = asyncio.create_task(connections.locate(serial, body))

        async def peer_disconnect() -> None:
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    worker.cancel()
                    return

        peer = asyncio.create_task(peer_disconnect())
        try:
            return JSONResponse(await worker)
        finally:
            with CancelScope(shield=True):
                if not worker.done():
                    worker.cancel()
                peer.cancel()
                await finish_cleanup(asyncio.gather(worker, peer, return_exceptions=True))

    @app.post("/devices/{serial}/agent/locate/cancel", operation_id="cancelLocate")
    async def cancel_locate(
        serial: str, session_id: str, request_id: str, request: Request
    ) -> dict[str, object]:
        if set(request.query_params) != {"session_id", "request_id"} or any(
            len(request.query_params.getlist(key)) != 1 for key in request.query_params
        ):
            raise ConnectionFault("invalid_message")
        return await connections.cancel_observation(serial, session_id, request_id)

    @app.post("/devices/{serial}/agent/action", operation_id="performAction")
    async def action_agent(serial: str, request: Request) -> Response:
        try:
            if request.query_params or request.headers.get("content-type", "").lower() not in {
                "application/json",
                "application/json; charset=utf-8",
            }:
                raise ValueError("invalid_request")
            data = bytearray()
            async with asyncio.timeout(2):
                async for part in request.stream():
                    data.extend(part)
                    if len(data) > 65536:
                        return JSONResponse(
                            {"status": "rejected", "error": "request_too_large"}, status_code=413
                        )
            parsed = json.loads(
                data.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
            body = ActionBody.model_validate(parsed)
        except (
            ValueError,
            UnicodeError,
            RecursionError,
            ValidationError,
            ConnectionFault,
            TimeoutError,
        ):
            return JSONResponse({"status": "rejected", "error": "invalid_request"}, status_code=422)
        worker = asyncio.create_task(connections.action(serial, body))

        async def peer_disconnect() -> None:
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    await connections.cancel_action(
                        serial,
                        body.session_id,
                        body.request_id,
                        body.operation_id,
                        reason="disconnected",
                    )
                    if not worker.done():
                        worker.cancel()
                    return

        peer = asyncio.create_task(peer_disconnect())
        try:
            result = await worker
            try:
                recordings.capture_action(serial, body, result)
            except Exception:
                log.warning(
                    "Recording capture failed",
                    exc_info=True,
                    extra={
                        "event": "recording.capture_failed",
                        "serial": serial,
                        "error_category": "internal",
                    },
                )
            return JSONResponse(result)
        finally:
            with CancelScope(shield=True):
                if not worker.done():
                    worker.cancel()
                peer.cancel()
                await finish_cleanup(asyncio.gather(worker, peer, return_exceptions=True))

    @app.post("/devices/{serial}/agent/action/cancel", operation_id="cancelAction")
    async def cancel_action(
        serial: str, session_id: str, request_id: str, operation_id: str, request: Request
    ) -> dict[str, object]:
        if set(request.query_params) != {"session_id", "request_id", "operation_id"} or any(
            len(request.query_params.getlist(key)) != 1 for key in request.query_params
        ):
            raise ConnectionFault("invalid_message")
        return await connections.cancel_action(serial, session_id, request_id, operation_id)

    @app.post("/devices/{serial}/agent/cancel", operation_id="cancelConnection")
    async def cancel_connection(serial: str, operation_id: str) -> dict[str, object]:
        return {"cancelled": await connections.cancel(serial, operation_id)}

    @app.delete("/devices/{serial}/agent/connection", operation_id="disconnectAgent")
    async def disconnect_agent(serial: str, session_id: str) -> dict[str, object]:
        return await connections.disconnect(serial, session_id)

    @app.get("/debug/stream", operation_id="getStreamDebug")
    async def stream_status() -> dict[str, object]:
        return {
            "api": api_metrics.snapshot(),
            "streams": {
                serial: {
                    "status": st.status,
                    "session": st.session,
                    "clock": st.clock,
                    "options": st.options.as_dict(),
                    "trace_id": st.trace_id,
                    "metrics": st.metrics_snapshot(),
                    "subscribers": [
                        {
                            "queue": len(s.queue),
                            "dropped": s.queue.stats.dropped,
                            "sent": s.sent,
                            "subscriber_id": s.subscriber_id,
                            "trace_id": s.trace_id,
                            "resyncs": s.queue.stats.resyncs,
                        }
                        for s in st.subscribers
                    ],
                }
                for serial, st in streams.streams.items()
            },
            "publishers": {
                k: {
                    **p.state,
                    "publisher_id": p.publisher_id,
                    "trace_id": p.trace_id,
                    "metrics": p.metrics_snapshot(),
                }
                for k, p in publishers.items()
            },
        }

    @app.post("/debug/stream/{serial}/trigger", operation_id="triggerStream")
    async def trigger(serial: str, body: TriggerBody) -> dict[str, object]:
        if not re.match(SERIAL_RE, serial):
            raise HTTPException(400, "bad serial")
        args = (
            ["input", "tap", str(body.x), str(body.y)]
            if body.kind == "tap"
            else ["input", "keyevent", str(body.code)]
        )
        t0 = time.time_ns() // 1_000_000
        try:
            await adb(serial, "shell", *args, timeout=10)
        except SourceError as exc:
            log.warning(
                "Debug trigger failed",
                exc_info=True,
                extra={
                    "event": "device.trigger_failed",
                    "serial": serial,
                    "error_category": "device",
                },
            )
            raise HTTPException(502, redact_text(str(exc))) from exc
        return {"sent_ms": t0, "done_ms": time.time_ns() // 1_000_000}

    @app.post("/debug/stream/{serial}/relay", operation_id="startRelay")
    async def start_relay(serial: str, body: RelayBody) -> dict[str, object]:
        if not re.match(SERIAL_RE, serial):
            raise HTTPException(400, "bad serial")
        opts = VideoOptions(
            max_size=body.max_size, video_bit_rate=body.bit_rate, max_fps=body.max_fps
        )
        async with publisher_lock:
            try:
                streams.check_capture_orientation(serial, opts)
            except CaptureOrientationConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            old = publishers.pop(serial, None)
            if old is not None:
                await old.stop()
            pub = RelayPublisher(streams, serial, opts, body.relay_url, body.channel, body.token)
            pub.start()
            publishers[serial] = pub
        return {
            "started": True,
            "publisher_id": pub.publisher_id,
            "lease_ms": int(pub.lease_seconds * 1000),
        }

    @app.post("/debug/stream/{serial}/relay/keepalive", operation_id="renewRelay")
    async def renew_relay(serial: str, body: RelayKeepalive) -> dict[str, object]:
        pub = publishers.get(serial)
        if pub is None or pub.publisher_id != body.publisher_id or not pub.renew():
            raise HTTPException(404, "publisher lease no longer exists")
        return {"renewed": True}

    @app.delete("/debug/stream/{serial}/relay", operation_id="stopRelay")
    async def stop_relay(serial: str, publisher_id: str) -> dict[str, object]:
        async with publisher_lock:
            pub = publishers.get(serial)
            if pub is None or pub.publisher_id != publisher_id:
                return {"stopped": False}
            del publishers[serial]
            await pub.stop()
        return {"stopped": True}

    @app.websocket("/ws/stream/{serial}")
    async def stream(ws: WebSocket, serial: str) -> None:
        if not token_matches(token, ws.query_params.get("token")):
            await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return
        try:
            if not re.match(SERIAL_RE, serial):
                raise ValueError("bad serial")
            if len(ws.query_params.getlist("capture_orientation")) > 1:
                raise ValueError("duplicate capture_orientation")
            opts = video_options(dict(ws.query_params))
        except ValueError as exc:
            await ws.close(code=WS_CLOSE_BAD_REQUEST, reason=str(exc)[:100])
            return
        await ws.accept()
        try:
            streams.check_capture_orientation(serial, opts)
            async with publisher_lock:
                publisher = publishers.get(serial)
                # A publisher reserves its profile before its forwarding task subscribes.
                if (
                    publisher is not None
                    and publisher.task is not None
                    and not publisher.task.done()
                    and publisher.options.capture_orientation != opts.capture_orientation
                ):
                    raise CaptureOrientationConflict("capture_orientation_conflict")
                device, sub = streams.subscribe(serial, opts)
        except CaptureOrientationConflict as exc:
            await ws.close(code=1008, reason=str(exc))
            return

        async def drain_incoming() -> None:
            while True:
                await ws.receive_text()

        async def send_frames() -> None:
            async for msg in sub.messages():
                async with asyncio.timeout(5):
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

        reader = asyncio.create_task(drain_incoming())
        sender = asyncio.create_task(send_frames())
        try:
            done, _ = await asyncio.wait({reader, sender}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except TimeoutError:
            log.warning(
                "Stream subscriber send timed out",
                exc_info=True,
                extra={
                    "event": "stream.subscriber_timeout",
                    "serial": serial,
                    "subscriber_id": sub.subscriber_id,
                    "error_category": "timeout",
                },
            )
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            # Starlette may cancel the endpoint as soon as the peer disconnects. Finish device
            # cleanup even in an already-cancelled AnyIO scope.
            with CancelScope(shield=True):
                reader.cancel()
                sender.cancel()
                await asyncio.gather(reader, sender, return_exceptions=True)
                await streams.unsubscribe(device, sub)
                with contextlib.suppress(WebSocketDisconnect, RuntimeError, TimeoutError):
                    async with asyncio.timeout(1):
                        await ws.close()

    default_openapi = app.openapi

    def locate_openapi() -> dict[str, Any]:
        document = default_openapi()
        schemas = document["components"]["schemas"]
        if "LocateRequest" not in schemas:
            # Keep bounded, duplicate-key-rejecting reads instead of FastAPI's body parser.
            request_schema = LocateRequest.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
            schemas.update(request_schema.pop("$defs", {}))
            schemas["LocateRequest"] = request_schema
        return document

    app.openapi = locate_openapi  # type: ignore[method-assign]
    return app
