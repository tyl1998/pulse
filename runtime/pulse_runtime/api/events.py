"""In-process, ordered event stream with HTTP/WS backfill (P2.2).

Events are process-local and bounded. A subscriber that loses queued events can backfill by
the last sequence number it applied; the bus never invents missing events.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pulse_runtime.api.contracts import EventEnvelope, EventPage

DEFAULT_MAX_EVENTS = 4096
DEFAULT_SUBSCRIBER_QUEUE = 1024


@dataclass
class EventSubscriber:
    subscriber_id: str
    run_id: str | None
    queue: asyncio.Queue[EventEnvelope | None] = field(
        default_factory=lambda: asyncio.Queue(DEFAULT_SUBSCRIBER_QUEUE)
    )
    dropped: int = 0

    async def next(self) -> EventEnvelope | None:
        return await self.queue.get()


class EventBus:
    def __init__(self, *, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        self._max_events = max_events
        self._events: deque[EventEnvelope] = deque(maxlen=max_events)
        self._seq = 0
        self._subscribers: dict[str, EventSubscriber] = {}
        self._closed = False
        self._lock = threading.Lock()

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def publish(
        self,
        event_type: str,
        body: dict[str, Any],
        *,
        run_id: str | None = None,
        device_id: str | None = None,
    ) -> EventEnvelope:
        with self._lock:
            self._seq += 1
            event = EventEnvelope(
                type=event_type,
                msg_id=str(uuid4()),
                seq=self._seq,
                ts=time.time(),
                run_id=run_id,
                device_id=device_id,
                body=body,
            )
            self._events.append(event)
            subscribers = list(self._subscribers.values())
        for subscriber in subscribers:
            if subscriber.run_id is not None and subscriber.run_id != run_id:
                continue
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    subscriber.queue.get_nowait()
                subscriber.dropped += 1
                with contextlib.suppress(asyncio.QueueFull):
                    subscriber.queue.put_nowait(event)
        return event

    def page(self, *, since: int = 0, run_id: str | None = None, limit: int = 500) -> EventPage:
        with self._lock:
            events = list(self._events)
            latest = self._seq
        oldest = events[0].seq if events else 0
        matching = [
            event
            for event in events
            if event.seq > since and (run_id is None or event.run_id == run_id)
        ][:limit]
        gap = bool(oldest and since < oldest - 1)
        return EventPage(events=matching, oldest_seq=oldest, latest_seq=latest, gap=gap)

    def subscribe(self, run_id: str | None = None) -> EventSubscriber:
        subscriber = EventSubscriber(str(uuid4()), run_id)
        with self._lock:
            closed = self._closed
            if not closed:
                self._subscribers[subscriber.subscriber_id] = subscriber
        if closed:
            subscriber.queue.put_nowait(None)
        return subscriber

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        with self._lock:
            self._subscribers.pop(subscriber.subscriber_id, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
        for subscriber in subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                subscriber.queue.put_nowait(None)
