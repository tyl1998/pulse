# ruff: noqa: E501, RUF001
"""Summarize old and incremental stream-bench results.

Usage: uv run python runtime/packaging/p07_report.py <result.json>...
S1: host adb input send -> canvas drawImage return (draw submission, not optical display).
S2: composition PTS mapped by device/host clock -> draw submission (an estimate).
PTS -> Runtime still combines capture/encoding and adb/USB; these cannot be separated here.
Relay queue is a monotonic duration. Transport minus queue estimates the two network legs
together, not each leg separately. Runtime and GUI host timestamps assume the same host.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from typing import Any

from pulse_observability import redact_text


def numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def count(value: Any) -> int | None:
    n = numeric(value)
    return int(n) if n is not None and n >= 0 and n.is_integer() else None


def items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def fmt(value: Any, digits: int = 1) -> str:
    n = numeric(value)
    return f"{n:.{digits}f}" if n is not None else "-"


def pct(values: list[float], p: float) -> float | None:
    v = sorted(n for value in values if (n := numeric(value)) is not None)
    if not v:
        return None
    k = (len(v) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def row(name: str, values: list[float], total: int) -> str:
    return (
        f"| {name} | {len(values)} | {total - len(values)} | {fmt(pct(values, 50))} | "
        f"{fmt(pct(values, 95))} | {fmt(pct(values, 99))} | {fmt(max(values) if values else None)} |"
    )


def valid_clock(frame: dict[str, Any]) -> bool:
    if numeric(frame.get("devMs")) is None:
        return False
    if (
        frame.get("clockValid") is False
        or frame.get("clockFresh") is False
        or frame.get("clockStale") is True
        or frame.get("clockError")
    ):
        return False
    if "clockRttMs" in frame:
        rtt = numeric(frame["clockRttMs"])
        if rtt is None or rtt < 0:
            return False
    if "clockAgeMs" in frame:
        age = numeric(frame["clockAgeMs"])
        maximum = numeric(frame.get("clockMaxAgeMs"))
        if age is None or age < 0 or age > (maximum if maximum is not None else 15000):
            return False
    # Legacy samples have no freshness/RTT field. Preserve them, but disclose that limitation.
    return True


def delta(frame: dict[str, Any], end: str, start: str, *, device: bool = False) -> float | None:
    a, b = numeric(frame.get(end)), numeric(frame.get(start))
    if a is None or b is None or (device and not valid_clock(frame)):
        return None
    value = a - b
    return value if math.isfinite(value) and value >= 0 else None


def status(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else "unknown (legacy/not recorded)"


def report(sc: dict[str, Any]) -> str:
    frames = [obj(frame) for frame in items(sc.get("frames"))]
    specs = [
        ("S2 capture/encode + adb/USB", "rtRecvMs", "devMs", True),
        ("Runtime forward", "rtSendMs", "rtRecvMs", False),
        ("transport total (network + relay)", "guiRecvMs", "rtSendMs", False),
        ("worker wait", "decodeStartMs", "guiRecvMs", False),
        ("decode", "decodedMs", "decodeStartMs", False),
        ("render submit", "drawnMs", "decodedMs", False),
        ("S2 PTS->draw submit", "drawnMs", "devMs", True),
    ]
    segments = {
        name: [
            value
            for frame in frames
            if (value := delta(frame, end, start, device=device)) is not None
        ]
        for name, end, start, device in specs
    }
    queues = [
        q for frame in frames if (q := numeric(frame.get("relayQueueMs"))) is not None and q >= 0
    ]
    if queues:
        segments["relay queue (monotonic)"] = queues
        segments["transport - relay queue (network estimate)"] = [
            max(0.0, total - queue)
            for frame in frames
            if (total := delta(frame, "guiRecvMs", "rtSendMs")) is not None
            and (queue := numeric(frame.get("relayQueueMs"))) is not None
            and 0 <= queue <= total + (numeric(frame.get("timingResolutionMs")) or 0)
        ]
    e2e = segments["S2 PTS->draw submit"]
    lines = [
        f"### {sc.get('name') or 'benchmark'}",
        "",
        "S1 = adb input 发送 -> canvas draw 提交；S2 = PTS 时钟映射估算 -> draw 提交。均非光学上屏时间。",
        "采集/编码 + adb/USB 仍为合并段，不伪造拆分；Runtime/GUI 时间戳要求同主机。单位 ms。",
        f"complete={status(sc.get('complete'))}；error={sc.get('error') or '-'}",
        f"S2 frames={len(frames)}，success={len(e2e)}，missing/invalid={len(frames) - len(e2e)}，target={fmt(sc.get('frameTarget'), 0)}。",
        "",
        "| 分段 | n | missing/invalid | p50 | p95 | p99 | max |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [row(name, values, len(frames)) for name, values in segments.items()]
    if queues:
        lines.append(
            "\nrelay queue 为接收 -> 开始发帧的单调耗时。扣除后是两段网络合计估算（直连为单段），不是各跳独立测量；缺值或超出时间戳量化误差的负差不参与扣减。"
        )
    if any(frame.get("timingResolutionMs") == 1 for frame in frames):
        lines.append(
            "跨进程边界采用同机 realtime/Date.now()，分辨率 1ms；decode/render 用同一 Worker 单调时钟测量。传输扣除 relay 若落在 [-1,0)ms，显示为 0（低于分辨率，不代表实际零延迟）。"
        )
    legacy_clock = sum(
        not any(
            key in frame
            for key in (
                "clockRttMs",
                "clockValid",
                "clockFresh",
                "clockStale",
                "clockError",
                "clockAgeMs",
            )
        )
        and numeric(frame.get("devMs")) is not None
        for frame in frames
    )
    if legacy_clock:
        lines.append(
            f"旧格式 clock freshness 未记录：{legacy_clock} 帧；沿用 devMs，但无法事后证明其时钟未过期。"
        )
    lines.append(
        "各阶段独立过滤 null/NaN/非有限值、负耗时；clock 无效/过期只排除依赖 PTS 的阶段，不丢弃可用的 host/relay 测量。"
    )

    change = obj(sc.get("change"))
    if change:
        trials = items(change.get("trials"))
        samples = [
            {**obj(obj(trial).get("sample")), "sentMs": obj(trial).get("sentMs")}
            for trial in trials
        ]
        s1_specs = [
            ("S1 adb input->draw submit", "drawnMs", "sentMs", False),
            ("S1 input->PTS estimate", "devMs", "sentMs", True),
            ("S1 PTS->draw submit", "drawnMs", "devMs", True),
        ]
        lines += [
            "",
            "| 分段 | n | missing/invalid | p50 | p95 | p99 | max |",
            "|---|---|---|---|---|---|---|",
        ]
        success = 0
        for name, end, start, device in s1_specs:
            values = [
                value
                for sample in samples
                if (value := delta(sample, end, start, device=device)) is not None
            ]
            lines.append(row(name, values, len(trials)))
            if not device:
                success = len(values)
        misses = count(change.get("misses"))
        attempts = count(change.get("attempts"))
        if attempts is None and misses is not None:
            attempts = len(trials) + misses
        invalid = len(trials) - success + len(items(change.get("invalidTrials")))
        lines.append(
            f"\nS1 success={success}，target={fmt(change.get('requested'), 0)}，attempts={fmt(attempts, 0)}，"
            f"misses={fmt(misses, 0)}，unconfirmed={fmt(change.get('unconfirmed'), 0)}，invalid trials={invalid}；"
            f"complete={status(change.get('complete'))}；error={change.get('error') or '-'}"
        )
        if "unconfirmed" in change:
            lines.append(
                "attempts 为已派发的尝试槽位；unconfirmed 为中断批次中无法确认是否已执行的槽位，不冒充 success 或 misses。invalidTrials 已计入新格式 misses。"
            )
        if "triggeredAttempts" in change:
            lines.append(
                f"实际触发次数 triggeredAttempts={fmt(change.get('triggeredAttempts'), 0)}；旧端未提供或批次失联时未知。aborted={status(change.get('aborted'))}。"
            )
        stored = count(change.get("successes"))
        if stored is not None and stored != success:
            lines.append(f"警告：保存的 successes={stored} 与有效 S1 trials={success} 不一致。")

    metrics = [obj(m) for m in items(sc.get("metrics")) if isinstance(m, dict)]
    if metrics:
        fps = [value for m in metrics if (value := numeric(m.get("fps"))) is not None]
        kbps = [value for m in metrics if (value := numeric(m.get("kbps"))) is not None]
        last = metrics[-1]
        lines.append(
            f"\nfps 中位 {fmt(statistics.median(fps) if fps else None)}，码率中位 "
            f"{fmt(statistics.median(kbps) if kbps else None, 0)} kbps，"
            f"{fmt(last.get('width'), 0)}x{fmt(last.get('height'), 0)} {last.get('codec') or '-'}，"
            f"丢帧 worker/上游 {fmt(last.get('droppedWorker'), 0)}/{fmt(last.get('droppedUpstream'), 0)}，"
            f"时钟 RTT {fmt(last.get('clockRttMs'), 2)} ms"
        )
    keys = [
        value
        for frame in frames
        if frame.get("key") and (value := delta(frame, "drawnMs", "devMs", device=True)) is not None
    ]
    if keys:
        lines.append(f"关键帧 S2 PTS->draw submit p50 {fmt(pct(keys, 50))} ms（n={len(keys)}）")
    lines.append(
        f"状态事件数={len(items(sc.get('events')))}；cleanup errors={sc.get('cleanupErrors') or '-'}"
    )
    return "\n".join(lines) + "\n"


def report_soak(data: dict[str, Any]) -> str:
    rows = [obj(value) for value in items(data.get("rows"))]
    windows = [obj(value) for value in items(data.get("windows"))] or rows
    lines = [
        f"### {data.get('name') or 'soak'}",
        "",
        "S2 为 PTS 估算 -> canvas draw 提交，不是光学上屏；clock 无效样本不参与分位数。",
        f"complete={status(data.get('complete'))}；error={data.get('error') or '-'}",
        f"requested seconds={fmt(data.get('seconds'))}，sampled seconds={fmt(data.get('actualSeconds'))}；process complete={status(data.get('processComplete'))}",
        "新格式为标称 10 秒窗口（尾窗可短），fps=drawn/实测窗口秒数；不对窗口分位数再求整体分位数。",
        "旧 soak 数组未保存有效样本数、完整性和精确窗口长度时保留 '-'，旧 fps 仍是原 metrics.fps。",
        "",
        "| window (s) | frames | S2 n | S2 miss | fps | p50 | p95 | p99 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    previous = 0.0
    for window in windows:
        end = numeric(window.get("toSeconds", window.get("t")))
        start = numeric(window.get("fromSeconds"))
        lines.append(
            f"| {fmt(start if start is not None else previous)}..{fmt(end)} | {fmt(window.get('frames'), 0)} | "
            f"{fmt(window.get('e2eN'), 0)} | {fmt(window.get('e2eMisses'), 0)} | {fmt(window.get('fps'))} | "
            f"{fmt(window.get('e2eP50'))} | {fmt(window.get('e2eP95'))} | {fmt(window.get('e2eP99'))} |"
        )
        if end is not None:
            previous = end
    totals = {
        key: sum(n for window in windows if (n := numeric(window.get(key))) is not None)
        for key in ["frames", "e2eN", "e2eMisses"]
    }
    known = all(count(window.get("e2eN")) is not None for window in windows)
    lines.append(
        f"\nframes={fmt(totals['frames'], 0)}，S2 success={fmt(totals['e2eN'] if known else None, 0)}，misses={fmt(totals['e2eMisses'] if known else None, 0)}；未纳入计时窗口的 cleanup frames={len(items(data.get('tailFrames')))}。"
    )

    resources: dict[str, dict[str, list[float]]] = {}
    for snapshot in rows:
        aggregate: dict[str, dict[str, float]] = {}
        for process in items(snapshot.get("ps")):
            process = obj(process)
            role = str(
                process.get("role")
                or process.get("group")
                or f"PID {fmt(process.get('pid'), 0)} (legacy)"
            )
            values = aggregate.setdefault(role, {})
            for key in ["rssMb", "cpuWindow", "cpu"]:
                value = numeric(process.get(key))
                if value is not None:
                    values[key] = values.get(key, 0) + value
        for role, values in aggregate.items():
            target = resources.setdefault(role, {"rssMb": [], "cpuWindow": [], "cpu": []})
            for key, value in values.items():
                target[key].append(value)
    if resources:
        lines += [
            "\n仅统计指定 PID 树；renderer 包含其 Worker 线程，GPU 单列。不把所有 Electron/VSCode 汇总。",
            "CPU window 是进程累计 CPU 时间差/采样间隔，ps %cpu 是 OS 原值，二者不混用。",
            "",
            "| role | first RSS MB | last RSS MB | peak RSS MB | CPU window n | CPU window p50 % | CPU window max % | ps %cpu p50 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for role, values in sorted(resources.items()):
            rss, cpu, raw = values["rssMb"], values["cpuWindow"], values["cpu"]
            lines.append(
                f"| {role} | {fmt(rss[0] if rss else None)} | {fmt(rss[-1] if rss else None)} | {fmt(max(rss) if rss else None)} | {len(cpu)} | {fmt(pct(cpu, 50))} | {fmt(max(cpu) if cpu else None)} | {fmt(pct(raw, 50))} |"
            )
    metrics = items(data.get("metrics"))
    if metrics:
        last = obj(metrics[-1])
        lines.append(
            f"最终丢帧 Worker/Runtime/relay={fmt(last.get('droppedWorker'), 0)}/{fmt(last.get('droppedUpstream'), 0)}/{fmt(last.get('droppedRelay'), 0)}。"
        )
    lines.append(
        f"process roots={data.get('processRoots') or '-'}；missing={data.get('processMissing') or '-'}；errors={data.get('processErrors') or '-'}"
    )
    lines.append(
        f"状态事件数={len(items(data.get('events')))}；cleanup errors={data.get('cleanupErrors') or '-'}"
    )
    return "\n".join(lines) + "\n"


def reports(data: Any) -> list[str]:
    if isinstance(data, list):
        if data and all(
            isinstance(value, dict) and "t" in value and not isinstance(value.get("frames"), list)
            for value in data
        ):
            result = [report_soak({"rows": data})]
        else:
            result = [report(obj(value)) for value in data]
    else:
        value = obj(data)
        if "scenarios" in value:
            result = [report(obj(sc)) for sc in items(value["scenarios"])]
        elif value.get("kind") == "soak" or "rows" in value:
            result = [report_soak(value)]
        else:
            result = [report(value)]
    # Historical inputs stay untouched. Unlike a log record, a report must retain every table row.
    return [redact_text(text, limit=None) for text in result]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: p07_report.py <result.json>...", file=sys.stderr)
        return 2
    for path in sys.argv[1:]:
        try:
            with open(path, encoding="utf-8") as source:
                data = json.load(source)
        except (OSError, ValueError) as error:
            print(redact_text(f"{path}: {error}"), file=sys.stderr)
            return 1
        for text in reports(data):
            print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
