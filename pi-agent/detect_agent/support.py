"""
Audit logging (JSON lines, rotated) and systemd sd_notify without dependencies.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import time
from typing import Any


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "audit", None)
        if isinstance(extra, dict):
            entry.update(extra)
        return json.dumps(entry, separators=(",", ":"), default=str)


def build_logger(state_dir: str, name: str = "magickeys-detect") -> logging.Logger:
    """Logger writing JSON lines to <state_dir>/audit.log (5 MB x 5) and stderr (journald)."""
    os.makedirs(state_dir, exist_ok=True)
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(state_dir, "audit.log"), maxBytes=5 * 1024 * 1024, backupCount=5)
    fh.setFormatter(_JsonFormatter())
    sh = logging.StreamHandler()
    sh.setFormatter(_JsonFormatter())
    log.addHandler(fh)
    log.addHandler(sh)
    log.propagate = False
    return log


def audit(log: logging.Logger, msg: str, level: int = logging.INFO, **fields: Any) -> None:
    log.log(level, msg, extra={"audit": fields})


def sd_notify(state: str) -> bool:
    """Send a notification to systemd if NOTIFY_SOCKET is set. Returns True if sent."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode("utf-8"))
        return True
    except OSError:
        return False


def cpu_temp_c() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r", encoding="ascii") as fh:
            return round(int(fh.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None
