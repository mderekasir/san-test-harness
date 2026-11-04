# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""Layer 2 - Fixtures and data provisioning.

Fixtures are plain functions (optionally generators) registered with
``@fixture``.  A test asks for a fixture simply by naming it as a parameter.
Generator fixtures run their code after ``yield`` as teardown.

Scopes:
  ``test``     - built fresh for every test (default)
  ``session``  - built once per harness run and shared

Built-in fixtures (provided by the runner):
  ``topology``  - the :class:`~santh.devices.Topology` for this run
  ``baseline``  - restores every device to the session-start snapshot before
                  the test (deterministic starting state, paper Sec. IV-D)
  ``flags``     - the set of enabled feature flags
  ``log``       - a per-test logger whose lines land in the report
  ``<device>``  - every device in the topology is injectable by its name
"""
from __future__ import annotations

import inspect
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

SCOPES = ("test", "session")


@dataclass
class FixtureDef:
    name: str
    func: Callable[..., Any]
    scope: str = "test"

    @property
    def params(self) -> List[str]:
        return list(inspect.signature(self.func).parameters)


_REGISTRY: Dict[str, FixtureDef] = {}


def fixture(func: Optional[Callable] = None, *, scope: str = "test", name: Optional[str] = None):
    """Register a fixture.  Usable as ``@fixture`` or ``@fixture(scope="session")``."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")

    def deco(f: Callable) -> Callable:
        fname = name or f.__name__
        _REGISTRY[fname] = FixtureDef(fname, f, scope)
        return f

    return deco(func) if func is not None else deco


def registry() -> Dict[str, FixtureDef]:
    return _REGISTRY


class FixtureError(RuntimeError):
    pass


class FixtureResolver:
    """Builds fixture values on demand, honouring scope and teardown order."""

    def __init__(self, defs: Dict[str, FixtureDef], session_stack: ExitStack,
                 static: Optional[Dict[str, Any]] = None) -> None:
        self.defs = defs
        self.session_stack = session_stack
        self.session_values: Dict[str, Any] = dict(static or {})
        self._session_lock = threading.RLock()

    def resolve(self, names: Iterable[str], test_stack: ExitStack,
                test_values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        values: Dict[str, Any] = dict(test_values or {})
        for n in names:
            self._get(n, values, test_stack, chain=())
        return values

    def _get(self, name: str, values: Dict[str, Any], test_stack: ExitStack, chain: tuple) -> Any:
        if name in values:
            return values[name]
        if name in self.session_values:
            values[name] = self.session_values[name]
            return values[name]
        if name in chain:
            raise FixtureError(f"fixture cycle: {' -> '.join(chain + (name,))}")
        fdef = self.defs.get(name)
        if fdef is None:
            raise FixtureError(f"unknown fixture {name!r}")
        if fdef.scope == "session":
            with self._session_lock:  # parallel workers build it only once
                if name not in self.session_values:
                    self._build(fdef, values, test_stack, chain)
                values[name] = self.session_values[name]
                return values[name]
        return self._build(fdef, values, test_stack, chain)

    def _build(self, fdef: FixtureDef, values: Dict[str, Any], test_stack: ExitStack, chain: tuple) -> Any:
        name = fdef.name
        stack = self.session_stack if fdef.scope == "session" else test_stack
        kwargs = {p: self._get(p, values, test_stack, chain + (name,)) for p in fdef.params}
        result = fdef.func(**kwargs)
        if inspect.isgenerator(result):
            gen = result
            value = next(gen)

            def _teardown(g=gen):
                try:
                    next(g)
                except StopIteration:
                    pass
            stack.callback(_teardown)
        else:
            value = result
        if fdef.scope == "session":
            self.session_values[name] = value
        values[name] = value
        return value
