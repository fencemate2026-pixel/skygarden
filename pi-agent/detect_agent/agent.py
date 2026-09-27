"""
MagicKeys Detect agent - main process.

Threads:
  * main    : polls inputs every pollMs, writes events to the outbox, and pings
              the systemd watchdog ONLY while both loops are healthy.
  * sender  : drains the outbox in signed batches, sends heartbeats, backs off
              on failure. Never drops an unacknowledged event.

This process drives nothing. It claims GPIO lines as inputs only and has no
output code path (see config.py interlocks and inputs.LgpioBackend).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any, Callable

from . import __version__
from .config import Env, InputSpec, SiteConfig, load_config, load_env
from .inputs import FakeBackend, GpioBackend, InputMonitor, LgpioBackend
from .outbox import Outbox
from .support import audit, build_logger, cpu_temp_c, sd_notify
from .uplink import Backoff, Uplink

SENDER_STALL_S = 120.0   # sender loop silent this long => stop feeding the watchdog
POLL_STALL_S = 5.0       # poll loop silent this long => stop feeding the watchdog


class Agent:
    def __init__(self, cfg: SiteConfig, env: Env, backend: GpioBackend,
                 log: logging.Logger, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.cfg = cfg
        self.env = env
        self.log = log
        self._clock = clock
        self._wall = wall
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.outbox = Outbox(env.state_dir)
        self.uplink = Uplink(env.ingest_url, cfg.device_id, env.device_secret)
        self.backoff = Backoff()
        self._started_wall = wall()
        self._last_poll = clock()
        self._last_sender_tick = clock()
        self._last_send_ok_wall: float | None = None
        self._last_error: str | None = None
        self.monitor = InputMonitor(backend, cfg.inputs, cfg.pull == "UP", self._on_input)
        self._backend = backend
        self._sender = threading.Thread(target=self._sender_loop, name="sender", daemon=True)

    # ---- event creation ---------------------------------------------------
    def _on_input(self, spec: InputSpec, state: str, _t_ms: float, extra: dict) -> None:
        now = self._wall()
        event: dict[str, Any] = {
            "type": spec.event,
            "state": state,
            "input": spec.name,
            "location": self.cfg.location,
            "occurredAt": now,
        }
        if extra:
            event["extra"] = extra
        seq = self.outbox.put(event, int(now * 1000))
        audit(self.log, "event", seq=seq, type=spec.event, state=state, input=spec.name, **extra)
        self._wake.set()

    # ---- heartbeat ----------------------------------------------------------
    def heartbeat(self) -> dict[str, Any]:
        return {
            "agentVersion": __version__,
            "uptimeS": int(self._wall() - self._started_wall),
            "queueDepth": self.outbox.depth(),
            "lastSeq": self.outbox.last_seq(),
            "inputs": self.monitor.snapshot(),
            "cpuTempC": cpu_temp_c(),
            "sendFailures": self.backoff.failures,
            "lastError": self._last_error,
        }

    # ---- sender ---------------------------------------------------------------
    def send_once(self, include_heartbeat: bool) -> bool:
        """Send one batch (and optionally a heartbeat). Returns True on success."""
        batch = self.outbox.peek(self.cfg.batch_max)
        if not batch and not include_heartbeat:
            return True
        hb = self.heartbeat() if include_heartbeat else None
        res = self.uplink.send(batch, hb, now=self._wall())
        if res.ok:
            self.outbox.ack(res.accepted)
            if self.backoff.failures:
                audit(self.log, "uplink recovered", after_failures=self.backoff.failures)
            self.backoff.reset()
            self._last_send_ok_wall = self._wall()
            self._last_error = None
            return True
        self._last_error = res.error
        audit(self.log, "uplink failed", logging.WARNING, status=res.status, error=res.error,
              queued=self.outbox.depth())
        return False

    def _sender_loop(self) -> None:
        next_hb = 0.0
        next_try = 0.0
        while not self._stop.is_set():
            self._last_sender_tick = self._clock()
            now = self._clock()
            if now >= next_try:
                want_hb = now >= next_hb
                pending = self.outbox.depth() > 0
                if pending or want_hb:
                    ok = self.send_once(include_heartbeat=want_hb)
                    if ok:
                        if want_hb:
                            next_hb = now + self.cfg.heartbeat_seconds
                        # keep draining quickly if more is queued
                        next_try = now if self.outbox.depth() > 0 else now + 1.0
                    else:
                        next_try = now + self.backoff.fail()
            self._wake.wait(timeout=1.0)
            self._wake.clear()

    # ---- health -----------------------------------------------------------------
    def healthy(self) -> bool:
        now = self._clock()
        return (now - self._last_poll) < POLL_STALL_S and \
               (now - self._last_sender_tick) < SENDER_STALL_S and \
               self._sender.is_alive()

    # ---- lifecycle ----------------------------------------------------------------
    def stop(self, *_args: Any) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> int:
        audit(self.log, "agent start", version=__version__, device=self.cfg.device_id,
              site=self.cfg.site_id, inputs=[s.name for s in self.cfg.inputs])
        self.monitor.prime(self._clock() * 1000)
        self._sender.start()
        sd_notify("READY=1")
        period = self.cfg.poll_ms / 1000.0
        last_wd = 0.0
        try:
            while not self._stop.is_set():
                t = self._clock()
                try:
                    self.monitor.poll(t * 1000)
                    self._last_poll = t
                except Exception as exc:  # a read fault must not kill the process silently
                    audit(self.log, "input poll error", logging.ERROR, error=str(exc))
                if t - last_wd >= 5.0:
                    if self.healthy():
                        sd_notify("WATCHDOG=1")
                    else:
                        audit(self.log, "unhealthy: withholding watchdog", logging.ERROR)
                    last_wd = t
                self._stop.wait(period)
        finally:
            sd_notify("STOPPING=1")
            self._sender.join(timeout=20)
            # final attempt to flush anything queued during shutdown
            if self.outbox.depth():
                self.send_once(include_heartbeat=False)
            self._backend.close()
            self.outbox.close()
            audit(self.log, "agent stop")
        return 0


def main() -> int:
    env = load_env()
    log = build_logger(env.state_dir)
    try:
        cfg = load_config(env.config_path)
    except Exception as exc:
        audit(log, "config rejected", logging.CRITICAL, error=str(exc))
        return 2
    backend: GpioBackend = FakeBackend() if env.gpio_backend == "fake" else LgpioBackend(cfg.gpio_chip)
    agent = Agent(cfg, env, backend, log)
    signal.signal(signal.SIGTERM, agent.stop)
    signal.signal(signal.SIGINT, agent.stop)
    return agent.run()
