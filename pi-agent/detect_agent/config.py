"""
Site configuration loader and validator for the MagicKeys Detect agent.

Safety interlocks enforced here (the agent refuses to start if any fail):
  * The config may not contain an "outputs" section, or any key that implies
    driving hardware (relay, pulse, unlock, output). This agent is READ-ONLY.
  * Every input pin must be in ALLOWED_INPUT_PINS and must be unique.
  * Pins reserved for I2C / UART / ID EEPROM are forbidden.
  * No raw secret may appear in the config file. Secrets come from the
    environment only (see .env.example).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0.0"

# BCM pins the agent may claim as inputs. Excludes 0/1 (ID EEPROM),
# 2/3 (I2C, used by RTC), 14/15 (UART console).
ALLOWED_INPUT_PINS = frozenset({4, 5, 6, 12, 13, 16, 17, 19, 20, 21, 22, 23, 24, 25, 26, 27})
FORBIDDEN_PINS = frozenset({0, 1, 2, 3, 14, 15})

# Event types the backend accepts from a device (must match the SQL CHECK).
EVENT_TYPES = frozenset({"TAILGATE", "MULTIPLE", "SENSOR_ERROR", "PASS", "DOOR_OPEN"})

# Any of these substrings in any key name means someone tried to configure
# actuation. Refuse outright rather than ignore.
_ACTUATION_KEY_MARKERS = ("output", "relay", "pulse", "unlock", "actuat")
_SECRET_KEY_MARKERS = ("secret", "token", "password", "hmac", "privatekey", "apikey")


class ConfigError(ValueError):
    """Raised when the site config is invalid or unsafe."""


@dataclass(frozen=True)
class InputSpec:
    name: str            # human label, e.g. "tailgating_1"
    bcm: int             # BCM GPIO number on the Pi
    active_level: str    # "LOW" or "HIGH": GPIO level that means "asserted"
    debounce_ms: int     # level must be stable this long to count
    event: str           # event type sent to the backend
    mode: str            # "LEVEL" (assert + clear events) or "PULSE" (one event per assertion)


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    device_id: str
    location: str
    gpio_chip: int
    pull: str
    poll_ms: int
    heartbeat_seconds: int
    batch_max: int
    inputs: tuple[InputSpec, ...] = field(default_factory=tuple)


def _walk_keys(obj: Any, path: str = ""):
    """Yield (dotted_path, key) for every dict key in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, str(k)
            yield from _walk_keys(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_keys(v, f"{path}[{i}]")


def _require(d: dict, key: str, typ, where: str):
    if key not in d:
        raise ConfigError(f"{where}: missing '{key}'")
    val = d[key]
    if typ is int and isinstance(val, bool):
        raise ConfigError(f"{where}.{key}: expected int, got bool")
    if not isinstance(val, typ):
        raise ConfigError(f"{where}.{key}: expected {typ.__name__}, got {type(val).__name__}")
    return val


def parse_config(raw: dict) -> SiteConfig:
    """Validate a decoded JSON config and return a SiteConfig."""
    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")

    # Interlock 1: no actuation keys anywhere.
    for path, key in _walk_keys(raw):
        low = key.lower()
        if any(m in low for m in _ACTUATION_KEY_MARKERS):
            raise ConfigError(f"actuation key '{path}' is not permitted: this agent is read-only")
        if any(m in low for m in _SECRET_KEY_MARKERS):
            raise ConfigError(f"secret-like key '{path}' is not permitted in the config file; use the environment")

    if raw.get("schemaVersion") != SCHEMA_VERSION:
        raise ConfigError(f"schemaVersion must be '{SCHEMA_VERSION}'")

    site = _require(raw, "site", dict, "root")
    device = _require(raw, "device", dict, "root")
    gpio = _require(raw, "gpio", dict, "root")
    uplink = _require(raw, "uplink", dict, "root")

    site_id = _require(site, "id", str, "site")
    device_id = _require(device, "id", str, "device")
    location = _require(device, "location", str, "device")
    if not device_id or len(device_id) > 64 or not all(c.isalnum() or c in "-_" for c in device_id):
        raise ConfigError("device.id must be 1-64 chars of [A-Za-z0-9-_]")

    chip = _require(gpio, "chip", int, "gpio")
    pull = _require(gpio, "pull", str, "gpio").upper()
    if pull not in ("UP", "NONE"):
        raise ConfigError("gpio.pull must be 'UP' or 'NONE'")
    poll_ms = _require(gpio, "pollMs", int, "gpio")
    if not 2 <= poll_ms <= 100:
        raise ConfigError("gpio.pollMs must be between 2 and 100")

    heartbeat = _require(raw, "heartbeatSeconds", int, "root")
    if not 15 <= heartbeat <= 900:
        raise ConfigError("heartbeatSeconds must be between 15 and 900")
    batch_max = _require(uplink, "batchMax", int, "uplink")
    if not 1 <= batch_max <= 200:
        raise ConfigError("uplink.batchMax must be between 1 and 200")

    raw_inputs = _require(gpio, "inputs", list, "gpio")
    if not raw_inputs:
        raise ConfigError("gpio.inputs must list at least one input")

    seen_pins: set[int] = set()
    seen_names: set[str] = set()
    specs: list[InputSpec] = []
    for i, item in enumerate(raw_inputs):
        where = f"gpio.inputs[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: must be an object")
        name = _require(item, "name", str, where)
        bcm = _require(item, "bcm", int, where)
        level = _require(item, "activeLevel", str, where).upper()
        debounce = _require(item, "debounceMs", int, where)
        event = _require(item, "event", str, where).upper()
        mode = _require(item, "mode", str, where).upper()

        if bcm in FORBIDDEN_PINS:
            raise ConfigError(f"{where}: BCM {bcm} is reserved (I2C/UART/EEPROM)")
        if bcm not in ALLOWED_INPUT_PINS:
            raise ConfigError(f"{where}: BCM {bcm} is not in the allowed input set")
        if bcm in seen_pins:
            raise ConfigError(f"{where}: BCM {bcm} used twice")
        if name in seen_names:
            raise ConfigError(f"{where}: name '{name}' used twice")
        if level not in ("LOW", "HIGH"):
            raise ConfigError(f"{where}: activeLevel must be LOW or HIGH")
        if not 0 <= debounce <= 2000:
            raise ConfigError(f"{where}: debounceMs must be 0-2000")
        if debounce < poll_ms and debounce != 0:
            raise ConfigError(f"{where}: debounceMs ({debounce}) shorter than pollMs ({poll_ms})")
        if event not in EVENT_TYPES:
            raise ConfigError(f"{where}: event '{event}' not in {sorted(EVENT_TYPES)}")
        if mode not in ("LEVEL", "PULSE"):
            raise ConfigError(f"{where}: mode must be LEVEL or PULSE")

        seen_pins.add(bcm)
        seen_names.add(name)
        specs.append(InputSpec(name, bcm, level, debounce, event, mode))

    return SiteConfig(
        site_id=site_id,
        device_id=device_id,
        location=location,
        gpio_chip=chip,
        pull=pull,
        poll_ms=poll_ms,
        heartbeat_seconds=heartbeat,
        batch_max=batch_max,
        inputs=tuple(specs),
    )


def load_config(path: str) -> SiteConfig:
    """Read and validate the JSON config at `path`."""
    if not os.path.isfile(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        try:
            raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config is not valid JSON: {exc}") from exc
    return parse_config(raw)


@dataclass(frozen=True)
class Env:
    """Runtime settings from the environment. Secret never logged."""
    ingest_url: str
    device_secret: str
    config_path: str
    state_dir: str
    gpio_backend: str

    def __repr__(self) -> str:  # never leak the secret in logs/tracebacks
        return (f"Env(ingest_url={self.ingest_url!r}, device_secret=<redacted>, "
                f"config_path={self.config_path!r}, state_dir={self.state_dir!r}, "
                f"gpio_backend={self.gpio_backend!r})")


def load_env(environ: dict | None = None) -> Env:
    e = os.environ if environ is None else environ
    url = e.get("MKD_INGEST_URL", "").strip()
    secret = e.get("MKD_DEVICE_SECRET", "").strip()
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        raise ConfigError("MKD_INGEST_URL must be https:// (http only allowed for 127.0.0.1 tests)")
    if len(secret) < 32:
        raise ConfigError("MKD_DEVICE_SECRET missing or shorter than 32 characters")
    backend = e.get("MKD_GPIO_BACKEND", "lgpio").strip().lower()
    if backend not in ("lgpio", "fake"):
        raise ConfigError("MKD_GPIO_BACKEND must be 'lgpio' or 'fake'")
    return Env(
        ingest_url=url,
        device_secret=secret,
        config_path=e.get("MKD_CONFIG_PATH", "/etc/magickeys-detect/site.json"),
        state_dir=e.get("MKD_STATE_DIR", "/var/lib/magickeys-detect"),
        gpio_backend=backend,
    )
