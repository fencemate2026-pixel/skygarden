"""
Tests for the MagicKeys Detect agent. Run from pi-agent/:  python3 -m pytest -q
Hardware is never touched: every test uses FakeBackend.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import http.server
import json
import logging
import os
import pathlib
import threading
import time

import pytest

from detect_agent import config as cfgmod
from detect_agent import outbox as outboxmod
from detect_agent.agent import Agent
from detect_agent.config import ConfigError, Env, parse_config
from detect_agent.inputs import FakeBackend, InputMonitor
from detect_agent.outbox import Outbox
from detect_agent.uplink import Backoff, Uplink, sign

REPO = pathlib.Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config" / "site.skygarden-foyer.example.json"
FIXTURE = REPO / "tests" / "fixtures" / "hmac_vector.json"
SECRET = "s" * 40


def example() -> dict:
    return json.loads(EXAMPLE.read_text())


# --------------------------------------------------------------- config ----
def test_example_config_is_valid():
    cfg = parse_config(example())
    assert cfg.device_id == "skygarden-foyer-01"
    assert [i.bcm for i in cfg.inputs] == [5, 6, 13, 19]


@pytest.mark.parametrize("key", ["outputs", "relayMap", "pulseMs", "unlockPin", "actuator"])
def test_actuation_keys_rejected(key):
    raw = example()
    raw["gpio"][key] = 1
    with pytest.raises(ConfigError, match="not permitted"):
        parse_config(raw)


@pytest.mark.parametrize("key", ["deviceSecret", "hmacKey", "apiToken", "password"])
def test_secret_keys_rejected(key):
    raw = example()
    raw["uplink"][key] = "x"
    with pytest.raises(ConfigError, match="secret-like"):
        parse_config(raw)


@pytest.mark.parametrize("pin", [0, 1, 2, 3, 14, 15])
def test_forbidden_pins_rejected(pin):
    raw = example()
    raw["gpio"]["inputs"][0]["bcm"] = pin
    with pytest.raises(ConfigError, match="reserved"):
        parse_config(raw)


def test_duplicate_pin_rejected():
    raw = example()
    raw["gpio"]["inputs"][1]["bcm"] = raw["gpio"]["inputs"][0]["bcm"]
    with pytest.raises(ConfigError, match="used twice"):
        parse_config(raw)


def test_bad_schema_version_and_debounce():
    raw = example()
    raw["schemaVersion"] = "0.9"
    with pytest.raises(ConfigError, match="schemaVersion"):
        parse_config(raw)
    raw = example()
    raw["gpio"]["inputs"][0]["debounceMs"] = 5  # shorter than pollMs 10
    with pytest.raises(ConfigError, match="shorter than pollMs"):
        parse_config(raw)


def test_env_requires_https_and_long_secret():
    with pytest.raises(ConfigError, match="https"):
        cfgmod.load_env({"MKD_INGEST_URL": "http://example.com", "MKD_DEVICE_SECRET": SECRET})
    with pytest.raises(ConfigError, match="32"):
        cfgmod.load_env({"MKD_INGEST_URL": "https://x.supabase.co/functions/v1/ingest", "MKD_DEVICE_SECRET": "short"})
    env = cfgmod.load_env({"MKD_INGEST_URL": "https://x.supabase.co/functions/v1/ingest", "MKD_DEVICE_SECRET": SECRET})
    assert SECRET not in repr(env)


def test_no_output_code_path_in_hardware_backend():
    """Static interlock: the hardware backend must never call an lgpio output API."""
    src = (REPO / "pi-agent" / "detect_agent" / "inputs.py").read_text()
    for forbidden in ("gpio_claim_output", "gpio_write", "tx_pulse", "tx_pwm", "group_write"):
        assert forbidden not in src, forbidden


# --------------------------------------------------------------- inputs ----
def _monitor(raw: dict | None = None):
    cfg = parse_config(raw or example())
    be = FakeBackend(idle_level=1)  # pull-up idle = HIGH = inactive for activeLevel LOW
    events: list[tuple[str, str, dict]] = []
    mon = InputMonitor(be, cfg.inputs, True, lambda s, st, t, ex: events.append((s.event, st, ex)))
    mon.prime(0)
    return be, mon, events


def test_debounce_ignores_short_glitch():
    be, mon, events = _monitor()
    be.set_level(5, 0)
    for t in (10, 20):          # 20 ms low, debounce is 30 ms
        mon.poll(t)
    be.set_level(5, 1)
    for t in (30, 40, 50, 60):
        mon.poll(t)
    assert events == []


def test_level_assert_and_clear():
    be, mon, events = _monitor()
    be.set_level(5, 0)
    for t in range(10, 60, 10):
        mon.poll(t)
    be.set_level(5, 1)
    for t in range(60, 120, 10):
        mon.poll(t)
    assert events == [("TAILGATE", "ASSERT", {}), ("TAILGATE", "CLEAR", {})]


def test_pulse_counts_each_assertion_once():
    be, mon, events = _monitor()
    t = 0
    for _ in range(3):
        be.set_level(19, 0)
        for _ in range(4):
            t += 10
            mon.poll(t)
        be.set_level(19, 1)
        for _ in range(4):
            t += 10
            mon.poll(t)
    assert events == [("PASS", "PULSE", {})] * 3


def test_error_present_at_boot_is_reported():
    cfg = parse_config(example())
    be = FakeBackend(idle_level=1)
    be.claim_inputs([13], True)
    be.set_level(13, 0)  # sensor error asserted before the agent starts
    events = []
    mon = InputMonitor(be, cfg.inputs, True, lambda s, st, t, ex: events.append((s.event, st, ex)))
    mon.prime(0)
    assert events == [("SENSOR_ERROR", "ASSERT", {"atStart": True})]


# --------------------------------------------------------------- outbox ----
def test_seq_is_monotonic_across_restart(tmp_path):
    ob = Outbox(str(tmp_path))
    assert [ob.put({"type": "PASS"}, 1) for _ in range(3)] == [1, 2, 3]
    ob.ack([1, 2, 3])
    ob.close()
    ob2 = Outbox(str(tmp_path))
    assert ob2.put({"type": "PASS"}, 2) == 4   # never reuses a sequence number
    assert ob2.depth() == 1


def test_cap_drops_pass_counts_never_alarms(tmp_path, monkeypatch):
    monkeypatch.setattr(outboxmod, "MAX_ROWS", 5)
    ob = Outbox(str(tmp_path))
    for _ in range(3):
        ob.put({"type": "TAILGATE"}, 1)
    for _ in range(6):
        ob.put({"type": "PASS"}, 1)
    kinds = [e["type"] for e in ob.peek(100)]
    assert kinds.count("TAILGATE") == 3
    assert len(kinds) == 5


# --------------------------------------------------------------- uplink ----
def test_signature_vector_and_fixture():
    """Known vector, also written as a fixture the Deno test verifies (cross-language check)."""
    body = b'{"deviceId":"skygarden-foyer-01","events":[],"heartbeat":null}'
    ts = 1790500000
    expected = hmac.new(SECRET.encode(), b"1790500000." + body, hashlib.sha256).hexdigest()
    assert sign(SECRET, ts, body) == expected
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps({"secret": SECRET, "timestamp": ts,
                                   "body": body.decode(), "signature": expected}, indent=2) + "\n")


def test_backoff_sequence():
    b = Backoff()
    assert [b.fail() for _ in range(10)] == [2, 4, 8, 16, 32, 64, 128, 256, 300, 300]
    b.reset()
    assert b.failures == 0


class _Server:
    """Local ingest stand-in that VERIFIES the signature exactly as the backend does."""

    def __init__(self):
        self.received: list[dict] = []
        self.seen_seqs: set[int] = set()
        self.fail = False
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                ts = int(self.headers["X-MK-Timestamp"])
                good = sign(SECRET, ts, body)
                if outer.fail:
                    self.send_response(503); self.end_headers(); return
                if not hmac.compare_digest(good, self.headers["X-MK-Signature"]) or abs(time.time() - ts) > 300:
                    self.send_response(401); self.end_headers(); return
                data = json.loads(body)
                accepted = []
                for e in data["events"]:
                    if e["seq"] not in outer.seen_seqs:   # idempotent like UNIQUE(device_id, seq)
                        outer.seen_seqs.add(e["seq"])
                        outer.received.append(e)
                    accepted.append(e["seq"])
                out = json.dumps({"accepted": accepted}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/ingest"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def test_uplink_rejected_signature_is_failure():
    srv = _Server()
    try:
        bad = Uplink(srv.url, "skygarden-foyer-01", "x" * 40)
        res = bad.send([{"seq": 1, "type": "PASS"}], None)
        assert not res.ok and res.status == 401
    finally:
        srv.close()


def test_uplink_network_down_is_failure():
    up = Uplink("http://127.0.0.1:9/ingest", "d", SECRET, timeout_s=1)
    res = up.send([{"seq": 1, "type": "PASS"}], None)
    assert not res.ok and res.status == 0


# ------------------------------------------------------------ end to end ----
def _agent(tmp_path, url):
    cfg = parse_config(example())
    env = Env(ingest_url=url, device_secret=SECRET, config_path=str(EXAMPLE),
              state_dir=str(tmp_path), gpio_backend="fake")
    be = FakeBackend(idle_level=1)
    log = logging.getLogger(f"test-{tmp_path.name}")
    log.addHandler(logging.NullHandler())
    return Agent(cfg, env, be, log), be


def _wait(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_end_to_end_delivery_outage_and_recovery(tmp_path):
    srv = _Server()
    agent, be = _agent(tmp_path, srv.url)
    th = threading.Thread(target=agent.run, daemon=True)
    th.start()
    try:
        # 1. a tailgate is delivered, signed and verified by the stand-in server
        be.set_level(5, 0)
        assert _wait(lambda: any(e["type"] == "TAILGATE" and e["state"] == "ASSERT" for e in srv.received))
        be.set_level(5, 1)
        assert _wait(lambda: any(e["state"] == "CLEAR" for e in srv.received))

        # 2. outage: events queue locally, nothing lost
        srv.fail = True
        for _ in range(3):
            be.set_level(19, 0); time.sleep(0.08)
            be.set_level(19, 1); time.sleep(0.08)
        assert _wait(lambda: agent.outbox.depth() >= 3)
        assert _wait(lambda: agent.backoff.failures >= 1)

        # 3. recovery: backlog drains once the backoff window passes (first retry is 2 s)
        srv.fail = False
        agent._wake.set()
        assert _wait(lambda: sum(1 for e in srv.received if e["type"] == "PASS") == 3, timeout=10)
        assert _wait(lambda: agent.outbox.depth() == 0)
        seqs = [e["seq"] for e in srv.received]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
        assert agent.healthy()
    finally:
        agent.stop()
        th.join(timeout=25)
        srv.close()
    assert not th.is_alive()
