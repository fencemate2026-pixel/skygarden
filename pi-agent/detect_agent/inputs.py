"""
Read-only GPIO input handling.

Backends:
  * LgpioBackend  - real hardware via lgpio (Pi 5 / RP1). Claims INPUT lines
                    only; there is no code path that claims an output.
  * FakeBackend   - in-memory levels for tests and bench simulation.

InputMonitor polls every configured input, applies a stable-time debounce,
and turns confirmed level changes into events:
  * mode LEVEL -> ASSERT on becoming active, CLEAR on becoming inactive
  * mode PULSE -> one PULSE event per assertion (e.g. Optex "Number of pass")
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from .config import InputSpec


class GpioBackend(Protocol):
    def claim_inputs(self, pins: list[int], pull_up: bool) -> None: ...
    def read(self, pin: int) -> int: ...
    def close(self) -> None: ...


class LgpioBackend:
    """Hardware backend. Input lines only; never claims an output."""

    def __init__(self, chip: int):
        import lgpio  # imported lazily so tests run without hardware
        self._lg = lgpio
        self._h = lgpio.gpiochip_open(chip)
        self._claimed: list[int] = []

    def claim_inputs(self, pins: list[int], pull_up: bool) -> None:
        flags = self._lg.SET_PULL_UP if pull_up else self._lg.SET_PULL_NONE
        for pin in pins:
            rc = self._lg.gpio_claim_input(self._h, pin, flags)
            if rc < 0:
                raise RuntimeError(f"lgpio could not claim BCM {pin} as input (rc={rc})")
            self._claimed.append(pin)

    def read(self, pin: int) -> int:
        v = self._lg.gpio_read(self._h, pin)
        if v < 0:
            raise RuntimeError(f"lgpio read failed on BCM {pin} (rc={v})")
        return 1 if v else 0

    def close(self) -> None:
        for pin in self._claimed:
            try:
                self._lg.gpio_free(self._h, pin)
            except Exception:  # best effort on shutdown
                pass
        self._claimed.clear()
        try:
            self._lg.gpiochip_close(self._h)
        except Exception:
            pass


class FakeBackend:
    """Test/bench backend. Levels are set by the test with set_level()."""

    def __init__(self, idle_level: int = 1):
        self._levels: dict[int, int] = {}
        self._idle = idle_level
        self._lock = threading.Lock()

    def claim_inputs(self, pins: list[int], pull_up: bool) -> None:
        with self._lock:
            for p in pins:
                self._levels.setdefault(p, self._idle)

    def set_level(self, pin: int, level: int) -> None:
        with self._lock:
            self._levels[pin] = 1 if level else 0

    def read(self, pin: int) -> int:
        with self._lock:
            return self._levels[pin]

    def close(self) -> None:
        pass


@dataclass
class _PinState:
    stable_active: bool       # last confirmed (debounced) state
    candidate_active: bool    # raw state currently being timed
    candidate_since_ms: float


EmitFn = Callable[[InputSpec, str, float, dict], None]  # (spec, state, t_ms, extra)


class InputMonitor:
    """Debounces configured inputs and emits state-change events."""

    def __init__(self, backend: GpioBackend, specs: tuple[InputSpec, ...],
                 pull_up: bool, emit: EmitFn):
        self._backend = backend
        self._specs = specs
        self._emit = emit
        self._state: dict[int, _PinState] = {}
        self._backend.claim_inputs([s.bcm for s in specs], pull_up)

    def _is_active(self, spec: InputSpec, level: int) -> bool:
        return level == (0 if spec.active_level == "LOW" else 1)

    def snapshot(self) -> dict[str, bool]:
        """Current debounced state of every input (for heartbeats)."""
        return {s.name: self._state[s.bcm].stable_active for s in self._specs if s.bcm in self._state}

    def prime(self, now_ms: float) -> None:
        """Record the starting state without emitting (avoids false events at boot).

        A LEVEL input that is already asserted at start-up (e.g. sensor error
        present at boot) is still reported, as an ASSERT with extra atStart=True.
        """
        for s in self._specs:
            active = self._is_active(s, self._backend.read(s.bcm))
            self._state[s.bcm] = _PinState(active, active, now_ms)
            if active and s.mode == "LEVEL":
                self._emit(s, "ASSERT", now_ms, {"atStart": True})

    def poll(self, now_ms: float) -> None:
        """Sample every input once. Call every pollMs."""
        for s in self._specs:
            st = self._state[s.bcm]
            active = self._is_active(s, self._backend.read(s.bcm))
            if active != st.candidate_active:
                st.candidate_active = active
                st.candidate_since_ms = now_ms
            if st.candidate_active == st.stable_active:
                continue
            if now_ms - st.candidate_since_ms >= s.debounce_ms:
                st.stable_active = st.candidate_active
                if s.mode == "LEVEL":
                    self._emit(s, "ASSERT" if st.stable_active else "CLEAR", now_ms, {})
                elif st.stable_active:  # PULSE: count rising (asserting) edges only
                    self._emit(s, "PULSE", now_ms, {})
