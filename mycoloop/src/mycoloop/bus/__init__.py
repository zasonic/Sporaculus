"""Messaging hardware-abstraction layer.

The backend never imports paho outside this package. Two interchangeable
implementations of the same ``MessageBus`` protocol:

* ``LoopbackBus`` — in-process, deterministic, zero services. Used by tests and
  by ``make demo``. Supports retained messages and partition injection so
  "device went offline" is provable without unplugging anything.
* ``MqttBus`` — real MQTT 5 (Mosquitto/EMQX) with last-will, QoS 1 and reconnect.

Swapping transports is a config change (MYCOLOOP_BUS), not a code change.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

Handler = Callable[["Message"], None]


@dataclass(frozen=True)
class Message:
    topic: str
    payload: bytes
    qos: int = 1
    retain: bool = False

    def json(self) -> dict[str, Any]:
        return json.loads(self.payload.decode() or "{}")


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT wildcard matching: '+' one level, '#' the remainder."""
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(p) == len(t)


class MessageBus(ABC):
    name: str = "abstract"

    @abstractmethod
    def start(self) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...
    @abstractmethod
    def publish(self, topic: str, payload: dict | bytes, qos: int = 1, retain: bool = False) -> bool: ...
    @abstractmethod
    def subscribe(self, pattern: str, handler: Handler) -> None: ...

    def publish_json(self, topic: str, payload: dict, qos: int = 1, retain: bool = False) -> bool:
        return self.publish(topic, json.dumps(payload, default=str).encode(), qos, retain)


class LoopbackBus(MessageBus):
    """In-process broker. ``inline=True`` makes delivery synchronous (tests)."""

    name = "loopback"

    def __init__(self, inline: bool = False) -> None:
        self.inline = inline
        self._subs: list[tuple[str, Handler]] = []
        self._retained: dict[str, Message] = {}
        self._q: queue.Queue[Message] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.partitioned: set[str] = set()
        self.published: list[Message] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.inline or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="loopback-bus", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._q.put(Message(topic="__stop__", payload=b"{}"))
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if msg.topic == "__stop__":
                break
            self._deliver(msg)

    # -- transport ---------------------------------------------------------
    def publish(self, topic: str, payload: dict | bytes, qos: int = 1, retain: bool = False) -> bool:
        data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
        msg = Message(topic=topic, payload=data, qos=qos, retain=retain)
        with self._lock:
            self.published.append(msg)
            if retain:
                self._retained[topic] = msg
        if self._is_partitioned(topic):
            log.debug("loopback partition drop %s", topic)
            return False
        if self.inline:
            self._deliver(msg)
        else:
            self._q.put(msg)
        return True

    def subscribe(self, pattern: str, handler: Handler) -> None:
        with self._lock:
            self._subs.append((pattern, handler))
            retained = [m for t, m in self._retained.items() if topic_matches(pattern, t)]
        for m in retained:
            self._safe_call(handler, m)

    def _deliver(self, msg: Message) -> None:
        with self._lock:
            handlers = [h for p, h in self._subs if topic_matches(p, msg.topic)]
        for h in handlers:
            self._safe_call(h, msg)

    @staticmethod
    def _safe_call(handler: Handler, msg: Message) -> None:
        try:
            handler(msg)
        except Exception:
            log.exception("bus handler failed on %s", msg.topic)

    def _is_partitioned(self, topic: str) -> bool:
        parts = topic.split("/")
        device_id = parts[2] if len(parts) > 2 else ""
        return device_id in self.partitioned or "*" in self.partitioned

    # -- test / chaos helpers ---------------------------------------------
    def set_partition(self, device_id: str, enabled: bool = True) -> None:
        if enabled:
            self.partitioned.add(device_id)
        else:
            self.partitioned.discard(device_id)

    def drain(self, timeout: float = 2.0) -> None:
        """Block until queued messages are delivered (thread mode)."""
        if self.inline:
            return
        import time

        deadline = time.monotonic() + timeout
        while not self._q.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.02)  # allow the in-flight handler to finish


class MqttBus(MessageBus):
    """MQTT 5 transport (paho). Backend last-will marks the backend offline."""

    name = "mqtt"

    def __init__(self, settings) -> None:
        import paho.mqtt.client as mqtt

        self._mqtt = mqtt
        self.settings = settings
        self._subs: list[tuple[str, Handler]] = []
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
            protocol=mqtt.MQTTv5,
        )
        if settings.mqtt_username:
            self._client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        self._client.will_set(
            "farm/backend/status",
            json.dumps({"state": "offline", "client_id": settings.mqtt_client_id}),
            qos=1,
            retain=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._connected = threading.Event()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):  # pragma: no cover
        log.info("MQTT connected rc=%s", reason_code)
        for pattern, _ in self._subs:
            client.subscribe(pattern, qos=1)
        client.publish(
            "farm/backend/status",
            json.dumps({"state": "online", "client_id": self.settings.mqtt_client_id}),
            qos=1,
            retain=True,
        )
        self._connected.set()

    def _on_message(self, client, userdata, msg):  # pragma: no cover
        m = Message(topic=msg.topic, payload=msg.payload, qos=msg.qos, retain=msg.retain)
        for pattern, handler in self._subs:
            if topic_matches(pattern, m.topic):
                LoopbackBus._safe_call(handler, m)

    def start(self) -> None:  # pragma: no cover - needs a broker
        self._client.connect(self.settings.mqtt_host, self.settings.mqtt_port, self.settings.mqtt_keepalive)
        self._client.loop_start()
        self._connected.wait(timeout=5)

    def stop(self) -> None:  # pragma: no cover
        self._client.publish("farm/backend/status", json.dumps({"state": "offline"}), qos=1, retain=True)
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, topic: str, payload: dict | bytes, qos: int = 1, retain: bool = False) -> bool:  # pragma: no cover
        data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
        info = self._client.publish(topic, data, qos=qos, retain=retain)
        return info.rc == self._mqtt.MQTT_ERR_SUCCESS

    def subscribe(self, pattern: str, handler: Handler) -> None:  # pragma: no cover
        self._subs.append((pattern, handler))
        if self._connected.is_set():
            self._client.subscribe(pattern, qos=1)


def make_bus(settings, *, inline: bool = False) -> MessageBus:
    if settings.bus == "mqtt":
        return MqttBus(settings)
    return LoopbackBus(inline=inline)
