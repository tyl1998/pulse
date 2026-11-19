"""P1.5 single-device physical probe: real Agent tree through an independent Runtime.

Covers native/WebView pages, XPath variants, OCR, bitmap template and rotation. Uses only the
fixture's exported activities for navigation, no blind taps, no force-stop, no data clear.
Rotation is restored in a finally block; the pre-existing background Runtime is never touched.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from uuid import uuid4

import cv2
import httpx2 as httpx
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from pulse_runtime.drivers.android.agent_transport import AndroidBackend, validate_serial
from pulse_runtime.paths import connection_file, read_connection

SERIAL = "10AEBB06T0001A0"
PACKAGE = "com.pulse.target"
EVIDENCE_ROOT = Path(__file__).resolve().parents[1] / "build" / "p1-locator-20261105"


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    node_path: list[int] | None
    bounds: dict[str, int] | None
    visible: bool | None
    enabled: bool | None
    source: str


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    by: str
    status: str
    code: str | None
    match_count: int
    action_target: bool
    samples: list[Sample] = Field(default_factory=list)


def loc(*candidates: dict[str, object], unique=True, visible=True) -> dict[str, object]:
    return {"candidates": list(candidates), "expect": {"unique": unique, "visible": visible}}


def by(kind: str, value: object, **extra: object) -> dict[str, object]:
    return {"by": kind, "value": value, **extra}


async def observe(client: httpx.AsyncClient, session: str) -> dict[str, Any]:
    response = await client.post(
        "/devices/" + quote(SERIAL, safe="") + "/agent/observe",
        json={
            "session_id": session,
            "request_id": str(uuid4()),
            "tree": True,
            "screenshot": True,
            "ocr": True,
        },
    )
    return cast(dict[str, Any], response.json())


async def locate(
    client: httpx.AsyncClient,
    session: str,
    locator: dict[str, object],
    *,
    locators: dict[str, object] | None = None,
    expected: str = PACKAGE,
) -> dict[str, Any]:
    body = {
        "serial": SERIAL,
        "session_id": session,
        "request_id": str(uuid4()),
        "expected_package": expected,
        "target": {"locator": locator},
        "locators": locators or {},
    }
    async with asyncio.timeout(40):
        response = await client.post(
            "/devices/" + quote(SERIAL, safe="") + "/agent/locate", json=body
        )
    return cast(dict[str, Any], response.json())


async def stable(client: httpx.AsyncClient, session: str, **kwargs: Any) -> dict[str, Any]:
    """Recollect on a genuine page change; never mask other indeterminate causes."""
    result: dict[str, Any] = {}
    for _ in range(5):
        result = await locate(client, session, **kwargs)
        if (
            result.get("status") != "indeterminate"
            or (result.get("error") or {}).get("code") != "page_changed"
        ):
            return result
        await asyncio.sleep(1.0)
    return result


def summarize(name: str, kind: str, result: dict[str, Any]) -> Case:
    error = result.get("error")
    samples = [
        Sample(
            node_path=item["node_path"],
            bounds=item["bounds"],
            visible=item["visible"],
            enabled=(item["states"] or {}).get("enabled"),
            source=item["source"],
        )
        for item in result.get("matches", [])[:3]
    ]
    return Case(
        name=name,
        by=kind,
        status=str(result.get("status")),
        code=error["code"] if isinstance(error, dict) else None,
        match_count=int(result.get("match_count", 0)),
        action_target=result.get("action_target") is not None,
        samples=samples,
    )


def point_for(result: dict[str, Any], *, width: int | None = None, height: int | None = None):
    bounds = result["matches"][0]["bounds"]
    geometry = (result.get("observation", {}).get("action_context") or {}).get("geometry") or {}
    return {
        "x": (bounds["left"] + bounds["right"]) // 2,
        "y": (bounds["top"] + bounds["bottom"]) // 2,
        "width": width or geometry.get("width"),
        "height": height or geometry.get("height"),
    }


async def point_case(
    client: httpx.AsyncClient,
    session: str,
    name: str,
    *,
    anchor_result: dict[str, Any],
    miss_text: str,
    scope_name: str,
    scope_locator: dict[str, object],
    width: int | None = None,
    height: int | None = None,
) -> Case:
    if not anchor_result.get("matches"):
        return Case(
            name=name,
            by="point",
            status="skipped",
            code="anchor_unresolved",
            match_count=0,
            action_target=False,
        )
    return summarize(
        name,
        "point",
        await stable(
            client,
            session,
            locator=loc(
                by("text", miss_text),
                by("point", point_for(anchor_result, width=width, height=height), scope=scope_name),
            ),
            locators={scope_name: scope_locator},
        ),
    )


def crop_template(observation: dict[str, Any], text: str) -> str | None:
    blocks = observation["sources"]["ocr"]["data"]["blocks"]
    data = observation["sources"]["screenshot"]["data"]
    block = next((item for item in blocks if item["text"] == text), None)
    if block is None or block["bounds"] is None:
        return None
    image = cv2.imdecode(
        np.frombuffer(base64.b64decode(data["base64"]), dtype=np.uint8), cv2.IMREAD_UNCHANGED
    )
    rect = block["bounds"]
    crop = image[rect["top"] : rect["bottom"], rect["left"] : rect["right"]]
    success, encoded = cv2.imencode(".png", crop)
    return base64.b64encode(encoded).decode("ascii") if success else None


def show(cases: list[Case], title: str) -> None:
    print(f"\n== {title} ==")
    for index, item in enumerate(cases, 1):
        print(
            f"{index}) {item.name}  by={item.by}  status={item.status}"
            f"  matches={item.match_count}  code={item.code}  action_target={item.action_target}"
        )
        for sample in item.samples:
            print(
                f"     node={sample.node_path} bounds={sample.bounds}"
                f" visible={sample.visible} enabled={sample.enabled} source={sample.source}"
            )


async def run(args: argparse.Namespace) -> int:
    validate_serial(args.serial)
    directory = args.evidence.resolve()
    if directory.exists() or not directory.parent.is_dir():
        raise SystemExit("evidence directory must be new")
    runtime = args.runtime_dir.resolve()
    if runtime == connection_file().parent.resolve():
        raise SystemExit("use an independent Runtime directory")
    info = read_connection(runtime / "runtime.json")
    previous = read_connection(connection_file())
    if info is None or (previous is not None and info.pid == previous.pid):
        print(json.dumps({"error": "independent_runtime_unavailable"}))
        return 1
    report: dict[str, Any] = {
        "serial": args.serial,
        "runtime_pid": info.pid,
        "pre_existing_runtime_pid": previous.pid if previous is not None else None,
        "pages": {},
        "rotation": {},
    }
    backend = AndroidBackend()
    started = time.monotonic()
    session: str | None = None
    cases: dict[str, list[Case]] = {}
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{info.port}",
        headers={"Authorization": f"Bearer {info.token}"},
        timeout=90,
        trust_env=False,
    ) as client:
        path = "/devices/" + quote(args.serial, safe="") + "/agent"
        inventory = (await client.get("/devices")).json()
        report["device"] = next(
            (item for item in inventory.get("devices", []) if item.get("serial") == args.serial),
            {},
        )
        prepared = await client.post(
            path + "/prepare",
            json={
                "install": False,
                "enable_accessibility": True,
                "timeout_ms": 60_000,
                "operation_id": str(uuid4()),
            },
        )
        prepared_body = prepared.json()
        session = (prepared_body.get("connection") or {}).get("session_id")
        if prepared.status_code != 200 or not prepared_body.get("ready") or not session:
            report["prepare"] = prepared_body
            directory.mkdir(parents=True)
            (directory / "result.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps({"error": "prepare_failed", "status": prepared.status_code}))
            return 1

        async def go(activity: str) -> None:
            await backend.command(
                args.serial,
                "shell",
                "am",
                "start",
                "-W",
                "-n",
                f"{PACKAGE}/{activity}",
                timeout=15,
            )
            await asyncio.sleep(1.2)

        # --- ListActivity: duplicate ids, duplicate XPath, coordinate fallback.
        await go(".ListActivity")
        rows = await stable(client, session, locator=loc(by("res_id", "android:id/text1")))
        count = await stable(
            client, session, locator=loc(by("res_id", f"{PACKAGE}:id/tv_list_count"))
        )
        report["pages"]["list"] = {"rows": rows, "count": count}
        cases["ListActivity"] = [
            summarize("duplicate res_id rows", "res_id", rows),
            summarize("unique count label", "res_id", count),
            summarize(
                "duplicate xpath rows",
                "xpath",
                await stable(
                    client,
                    session,
                    locator=loc(
                        by("xpath", "//android.widget.TextView[@resource-id='android:id/text1']")
                    ),
                ),
            ),
            await point_case(
                client,
                session,
                "point inside enabled label",
                anchor_result=count,
                miss_text="无此行",
                scope_name="count",
                scope_locator=loc(by("res_id", f"{PACKAGE}:id/tv_list_count")),
            ),
            summarize(
                "absent text (valid tree)",
                "text",
                await stable(client, session, locator=loc(by("text", "无此行"))),
            ),
        ]

        # --- WebViewActivity: DOM is not exposed; absence must not become not_found.
        await go(".WebViewActivity")
        webview_view = await observe(client, session)
        for _ in range(4):
            nodes = (webview_view["sources"]["tree"]["data"] or {}).get("nodes") or []
            if len(nodes) >= 5:
                break
            await asyncio.sleep(0.9)
            webview_view = await observe(client, session)
        report["pages"]["webview_observe"] = {
            "tree_reasons": webview_view["sources"]["tree"]["completeness"]["reasons"],
            "node_count": len((webview_view["sources"]["tree"]["data"] or {}).get("nodes") or []),
            "nodes": [
                [
                    item["node_path"],
                    item["class_name"],
                    item["view_id"],
                    item["text"],
                ]
                for item in (webview_view["sources"]["tree"]["data"] or {}).get("nodes") or []
            ],
            "ocr_texts": [
                item["text"]
                for item in (webview_view["sources"]["ocr"]["data"] or {}).get("blocks") or []
            ][:12],
        }
        web_cases = [
            summarize(
                "native container id after DOM attach",
                "res_id",
                await stable(
                    client, session, locator=loc(by("res_id", f"{PACKAGE}:id/wv_content"))
                ),
            ),
            summarize(
                "DOM text exposed by WebView",
                "text",
                await stable(client, session, locator=loc(by("text", "订单号"))),
            ),
            summarize(
                "DOM id used as native res_id",
                "res_id",
                await stable(client, session, locator=loc(by("res_id", "btn-query"))),
            ),
            summarize(
                "xpath inside WebView DOM",
                "xpath",
                await stable(
                    client,
                    session,
                    locator=loc(by("xpath", "//android.widget.Button[@resource-id='btn-query']")),
                ),
            ),
            summarize(
                "OCR query button",
                "ocr_text",
                await stable(client, session, locator=loc(by("ocr_text", "查询"))),
            ),
        ]
        template = crop_template(webview_view, "查询")
        if template:
            body = {
                "serial": SERIAL,
                "session_id": session,
                "request_id": str(uuid4()),
                "expected_package": PACKAGE,
                "target": {"locator": loc(by("image", "query"))},
                "resources": {"query": {"png_base64": template}},
            }
            response = await client.post(
                "/devices/" + quote(args.serial, safe="") + "/agent/locate", json=body
            )
            result = cast(dict[str, Any], response.json())
            report["pages"]["webview_image"] = result
            web_cases.append(summarize("image template from real screenshot", "image", result))
        else:
            web_cases.append(
                Case(
                    name="image template from real screenshot",
                    by="image",
                    status="skipped",
                    code="ocr_block_missing",
                    match_count=0,
                    action_target=False,
                )
            )
        report["pages"]["webview"] = [item.model_dump() for item in web_cases]
        cases["WebViewActivity"] = web_cases

        # --- FormActivity: XPath variants, OCR, point semantics, disabled target.
        await go(".FormActivity")
        submit = await stable(
            client, session, locator=loc(by("res_id", f"{PACKAGE}:id/btn_submit"))
        )
        report["pages"]["form"] = {"submit": submit}
        cases["FormActivity"] = [
            summarize(
                "xpath absolute path",
                "xpath",
                await stable(
                    client,
                    session,
                    locator=loc(
                        by("xpath", "/hierarchy/node/node/node/node/node/node[@text='提交']")
                    ),
                ),
            ),
            summarize(
                "xpath class+resource-id",
                "xpath",
                await stable(
                    client,
                    session,
                    locator=loc(
                        by(
                            "xpath",
                            "//android.widget.EditText[@resource-id='com.pulse.target:id/et_name']",
                        )
                    ),
                ),
            ),
            summarize(
                "xpath contains+starts-with+and",
                "xpath",
                await stable(
                    client,
                    session,
                    locator=loc(
                        by("xpath", "//node[contains(@text,'协议') and starts-with(@text,'同意')]")
                    ),
                ),
            ),
            summarize(
                "xpath per-parent position",
                "xpath",
                await stable(
                    client, session, locator=loc(by("xpath", "//android.widget.RadioButton[1]"))
                ),
            ),
            summarize(
                "xpath unknown attribute",
                "xpath",
                await stable(client, session, locator=loc(by("xpath", "//node[@checked='true']"))),
            ),
            summarize("unique submit (disabled)", "res_id", submit),
            summarize(
                "ocr form title",
                "ocr_text",
                await stable(client, session, locator=loc(by("ocr_text", "接收通知"))),
            ),
            await point_case(
                client,
                session,
                "point on disabled submit",
                anchor_result=submit,
                miss_text="无此按钮",
                scope_name="submit",
                scope_locator=loc(by("res_id", f"{PACKAGE}:id/btn_submit")),
            ),
        ]

        # --- Rotation: geometry and point applicability on a real orientation change.
        original_accel = (
            await backend.command(
                args.serial, "shell", "settings", "get", "system", "accelerometer_rotation"
            )
        ).strip()
        original_rotation = (
            await backend.command(
                args.serial, "shell", "settings", "get", "system", "user_rotation"
            )
        ).strip()
        report["rotation"] = {
            "original_accelerometer": original_accel,
            "original_user_rotation": original_rotation,
        }
        portrait = ((submit.get("observation") or {}).get("action_context") or {}).get(
            "geometry"
        ) or {}
        try:
            await backend.command(
                args.serial,
                "shell",
                "settings",
                "put",
                "system",
                "accelerometer_rotation",
                "0",
                timeout=10,
            )
            await backend.command(
                args.serial, "shell", "settings", "put", "system", "user_rotation", "1", timeout=10
            )
            await asyncio.sleep(1.8)
            title = await stable(
                client, session, locator=loc(by("res_id", f"{PACKAGE}:id/tv_form_title"))
            )
            landscape = ((title.get("observation") or {}).get("action_context") or {}).get(
                "geometry"
            ) or {}
            report["rotation"]["landscape_geometry"] = landscape
            rotation_cases = [
                summarize("landscape native res_id", "res_id", title),
                await point_case(
                    client,
                    session,
                    "landscape point (current size)",
                    anchor_result=title,
                    miss_text="无此标题",
                    scope_name="title",
                    scope_locator=loc(by("res_id", f"{PACKAGE}:id/tv_form_title")),
                ),
                summarize(
                    "landscape point with portrait size",
                    "point",
                    await stable(
                        client,
                        session,
                        locator=loc(
                            by("text", "无此标题"),
                            by(
                                "point",
                                {
                                    "x": 60,
                                    "y": 250,
                                    "width": portrait.get("width"),
                                    "height": portrait.get("height"),
                                },
                                scope="title",
                            ),
                        ),
                        locators={"title": loc(by("res_id", f"{PACKAGE}:id/tv_form_title"))},
                    )
                    if title.get("matches") and portrait.get("width") and portrait.get("height")
                    else Case(
                        name="landscape point with portrait size",
                        by="point",
                        status="skipped",
                        code="anchor_or_portrait_geometry_unresolved",
                        match_count=0,
                        action_target=False,
                    ),
                ),
            ]
            cases["Rotation"] = rotation_cases
        finally:
            await backend.command(
                args.serial,
                "shell",
                "settings",
                "put",
                "system",
                "user_rotation",
                original_rotation or "0",
                timeout=10,
            )
            await backend.command(
                args.serial,
                "shell",
                "settings",
                "put",
                "system",
                "accelerometer_rotation",
                original_accel or "1",
                timeout=10,
            )
            await asyncio.sleep(1)

    report["duration_s"] = round(time.monotonic() - started, 2)
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    model = report["device"].get("model")
    for page, page_cases in cases.items():
        show(page_cases, f"P1.5 {page} {args.serial} ({model})")
    print(f"\nevidence: {directory / 'result.json'}  runtime_pid={info.pid}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", default=SERIAL)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    return asyncio.run(run(parser.parse_args(sys.argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main())
