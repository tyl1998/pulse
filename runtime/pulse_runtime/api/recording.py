"""Raw local recording sessions (P2.2).

A session records only confirmed actions that the caller explicitly submitted with
``source=recorder``. It does not normalize actions into semantic steps, capture page snapshots
for locator ranking, or claim that a draft has business assertions; those are P2.8.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pulse_runtime.api.contracts import (
    RecordedAction,
    RecordingDetail,
    RecordingStartRequest,
    RecordingSummary,
)
from pulse_runtime.api.errors import ApiRefusal
from pulse_runtime.api.events import EventBus
from pulse_runtime.drivers.android.actions import ActionBody


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class RecordingRecord:
    summary: RecordingSummary
    events: list[RecordedAction] = field(default_factory=list)
    draft: dict[str, Any] | None = None


class RecordingManager:
    def __init__(self, events: EventBus) -> None:
        self.events = events
        self._recordings: dict[str, RecordingRecord] = {}

    def start(self, request: RecordingStartRequest) -> RecordingSummary:
        for record in self._recordings.values():
            if record.summary.serial == request.serial and record.summary.state in (
                "recording",
                "paused",
            ):
                raise ApiRefusal(
                    "recording_active",
                    409,
                    serial=request.serial,
                    recording_id=record.summary.recording_id,
                )
        now = _now_ms()
        summary = RecordingSummary(
            recording_id=str(uuid4()),
            serial=request.serial,
            session_id=request.session_id,
            state="recording",
            created_at_ms=now,
            updated_at_ms=now,
        )
        record = RecordingRecord(summary)
        self._recordings[summary.recording_id] = record
        self._publish(record, "recording.started")
        return summary.model_copy(deep=True)

    def get(self, recording_id: str) -> RecordingRecord:
        record = self._recordings.get(recording_id)
        if record is None:
            raise ApiRefusal("recording_not_found", 404, recording_id=recording_id)
        return record

    def detail(self, recording_id: str) -> RecordingDetail:
        record = self.get(recording_id)
        return RecordingDetail(
            **record.summary.model_dump(),
            events=[item.model_copy(deep=True) for item in record.events],
            draft=copy.deepcopy(record.draft),
        )

    def list(self) -> list[RecordingSummary]:
        return [
            record.summary.model_copy(deep=True)
            for record in sorted(
                self._recordings.values(),
                key=lambda item: item.summary.created_at_ms,
                reverse=True,
            )
        ]

    def pause(self, recording_id: str) -> RecordingSummary:
        record = self.get(recording_id)
        if record.summary.state != "recording":
            raise ApiRefusal("recording_not_recording", 409, state=record.summary.state)
        record.summary.state = "paused"
        record.summary.updated_at_ms = _now_ms()
        self._publish(record, "recording.paused")
        return record.summary.model_copy(deep=True)

    def resume(self, recording_id: str) -> RecordingSummary:
        record = self.get(recording_id)
        if record.summary.state != "paused":
            raise ApiRefusal("recording_not_paused", 409, state=record.summary.state)
        record.summary.state = "recording"
        record.summary.updated_at_ms = _now_ms()
        self._publish(record, "recording.resumed")
        return record.summary.model_copy(deep=True)

    def stop(self, recording_id: str) -> RecordingDetail:
        record = self.get(recording_id)
        if record.summary.state == "stopped":
            return self.detail(recording_id)
        record.summary.state = "stopped"
        record.summary.updated_at_ms = _now_ms()
        record.draft = {
            "contract": "pulse-recording-draft",
            "recording_id": record.summary.recording_id,
            "serial": record.summary.serial,
            "session_id": record.summary.session_id,
            "normalized": False,
            "events": [item.model_dump(mode="json") for item in record.events],
            "caveats": [
                "raw recorder actions only",
                "no page snapshots for locator ranking",
                "no checkpoints or parameterization",
                "raw input text may be present; review before sharing",
            ],
        }
        self._publish(record, "recording.stopped")
        return self.detail(recording_id)

    def capture_action(
        self, serial: str, body: ActionBody, result: dict[str, object]
    ) -> RecordedAction | None:
        """Append one confirmed recorder action; never changes the device result."""
        if body.source != "recorder":
            return None
        if result.get("status") != "completed" or result.get("action_completed") is not True:
            return None
        for record in self._recordings.values():
            if (
                record.summary.serial != serial
                or record.summary.session_id != body.session_id
                or record.summary.state != "recording"
            ):
                continue
            after = result.get("post_observation")
            post_observation_id = after.get("request_id") if isinstance(after, dict) else None
            action = RecordedAction(
                action_id=body.request_id,
                operation_id=body.operation_id,
                kind=body.kind,
                source="recorder",
                target_source=body.target.source,
                target_observation_id=body.target.observation_id,
                params=copy.deepcopy(body.params),
                status="completed",
                action_completed=True,
                recorded_at_ms=_now_ms(),
                post_observation_id=post_observation_id
                if isinstance(post_observation_id, str)
                else None,
            )
            record.events.append(action)
            record.summary.updated_at_ms = action.recorded_at_ms
            record.summary.event_count = len(record.events)
            self.events.publish(
                "recording.action",
                action.model_dump(mode="json"),
                device_id=serial,
            )
            return action
        return None

    def _publish(self, record: RecordingRecord, event_type: str) -> None:
        self.events.publish(
            event_type,
            record.summary.model_dump(mode="json"),
            device_id=record.summary.serial,
        )
