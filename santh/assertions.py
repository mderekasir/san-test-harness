# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""Layer 4 - Assertions and synchronisation primitives.

``wait_until`` exists so tests poll for a state instead of sleeping a fixed
time.  Fixed sleeps are the main cause of timing-flaky lab tests (paper Sec. V).
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional


class HarnessAssertionError(AssertionError):
    """Assertion failure raised by harness helpers (counts as FAIL, not ERROR)."""


class SkipTest(Exception):
    """Raise (or call :func:`skip`) to mark a test as skipped."""


def skip(reason: str) -> None:
    raise SkipTest(reason)


def expect(condition: Any, message: str = "expectation failed") -> None:
    if not condition:
        raise HarnessAssertionError(message)


def expect_equal(actual: Any, expected: Any, what: str = "value") -> None:
    if actual != expected:
        raise HarnessAssertionError(f"{what}: expected {expected!r}, got {actual!r}")


def expect_at_least(actual: float, minimum: float, what: str = "value") -> None:
    if actual < minimum:
        raise HarnessAssertionError(f"{what}: expected >= {minimum!r}, got {actual!r}")


def expect_at_most(actual: float, maximum: float, what: str = "value") -> None:
    if actual > maximum:
        raise HarnessAssertionError(f"{what}: expected <= {maximum!r}, got {actual!r}")


def wait_until(predicate: Callable[[], Any], timeout: float = 30.0, interval: float = 0.5,
               message: Optional[str] = None, on_poll: Optional[Callable[[], None]] = None) -> Any:
    """Poll ``predicate`` until it returns a truthy value or ``timeout`` expires.

    ``on_poll`` is called before each check; simulators use it to advance time.
    Returns the truthy value so callers can use the observed state.
    """
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        if on_poll is not None:
            on_poll()
        last = predicate()
        if last:
            return last
        if time.monotonic() >= deadline:
            raise HarnessAssertionError(message or f"condition not met within {timeout}s (last={last!r})")
        time.sleep(interval)
