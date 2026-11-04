# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""Layer 3 - Execution engine.

Responsibilities (paper Sec. III-A and IV-B):

* discovery of ``test_*`` functions in ``test_*.py`` files
* parallel execution with per-device locking, so two tests never drive the
  same device at the same time while independent devices run concurrently
* retries, with tests that pass only on retry reported as FLAKY
* a quarantine list: quarantined tests still run and report, but never fail
  the build
* an exclusive lab reservation lock for shared hardware
* feature flags: new harness behaviour ships disabled by default (Sec. VI)
"""
from __future__ import annotations

import fnmatch
import importlib.util
import inspect
import os
import random
import socket
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from .assertions import SkipTest
from .devices import Topology
from .fixtures import FixtureDef, FixtureError, FixtureResolver, registry

PASS, FAIL, ERROR, SKIP, FLAKY = "PASS", "FAIL", "ERROR", "SKIP", "FLAKY"

# Harness feature flags.  Every flag is OFF unless enabled with --enable.
KNOWN_FLAGS: Dict[str, str] = {
    "shuffle": "randomise test order to expose hidden inter-test dependencies",
    "fail_fast": "stop scheduling new tests after the first FAIL/ERROR",
}


# ---------------------------------------------------------------------------
# Decorators for test authors
# ---------------------------------------------------------------------------
def tags(*names: str) -> Callable:
    def deco(f: Callable) -> Callable:
        f.__santh_tags__ = set(getattr(f, "__santh_tags__", set())) | set(names)
        return f
    return deco


def requires_flag(flag: str) -> Callable:
    """Skip the test unless the given feature flag is enabled."""
    def deco(f: Callable) -> Callable:
        f.__santh_requires_flag__ = flag
        return f
    return deco


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class TestCase:
    id: str
    func: Callable
    tags: Set[str] = field(default_factory=set)

    @property
    def params(self) -> List[str]:
        return list(inspect.signature(self.func).parameters)


@dataclass
class TestResult:
    id: str
    status: str
    duration: float
    message: str = ""
    attempts: int = 1
    quarantined: bool = False
    log: List[str] = field(default_factory=list)
    transcript: List[Dict[str, str]] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """True if this result should fail the build."""
        return self.status in (FAIL, ERROR) and not self.quarantined


@dataclass
class RunSummary:
    results: List[TestResult]
    started: float
    finished: float

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def ok(self) -> bool:
        return not any(r.blocking for r in self.results)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _load_module(path: Path):
    mod_name = f"santh_tests_{abs(hash(str(path.resolve())))}_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def discover(paths: Sequence[str], keyword: Optional[str] = None,
             tag_filter: Optional[Set[str]] = None) -> List[TestCase]:
    files: List[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(p.rglob("test_*.py"))
        elif p.is_file():
            files.append(p)
        else:
            raise FileNotFoundError(p)
    # conftest.py files (fixtures only) are loaded first
    for conf in sorted({f.parent / "conftest.py" for f in files}):
        if conf.exists():
            _load_module(conf)
    cases: List[TestCase] = []
    for f in files:
        mod = _load_module(f)
        for name, obj in vars(mod).items():
            if name.startswith("test_") and inspect.isfunction(obj) and obj.__module__ == mod.__name__:
                tc = TestCase(f"{f.stem}::{name}", obj, set(getattr(obj, "__santh_tags__", set())))
                if keyword and not fnmatch.fnmatch(tc.id, f"*{keyword}*"):
                    continue
                if tag_filter and not (tc.tags & tag_filter):
                    continue
                cases.append(tc)
    return cases


def load_quarantine(path: Optional[str]) -> Set[str]:
    """One test id (or glob) per line; ``#`` starts a comment."""
    if not path or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {ln.split("#", 1)[0].strip() for ln in fh if ln.split("#", 1)[0].strip()}


# ---------------------------------------------------------------------------
# Lab reservation
# ---------------------------------------------------------------------------
class LabLock:
    """Exclusive reservation of a shared lab, implemented as a lock file.

    Uses atomic ``O_CREAT | O_EXCL`` so it works on any OS and on shared
    network filesystems that honour exclusive create.
    """

    def __init__(self, path: str, owner: str = "", wait: float = 0.0) -> None:
        self.path, self.wait = path, wait
        self.owner = owner or f"{os.getpid()}@{socket.gethostname()}"
        self._held = False

    def __enter__(self) -> "LabLock":
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as fh:
                    fh.write(f"{self.owner}\n{time.time()}\n")
                self._held = True
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    holder = Path(self.path).read_text(encoding="utf-8").splitlines()[:1]
                    raise RuntimeError(f"lab is reserved by {holder[0] if holder else 'unknown'} ({self.path})")
                time.sleep(1.0)

    def __exit__(self, *exc: Any) -> None:
        if self._held:
            try:
                os.remove(self.path)
            finally:
                self._held = False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
class TestLogger:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(f"{time.strftime('%H:%M:%S')} {msg}")

    info = __call__


class Runner:
    def __init__(self, topology: Topology, *, workers: int = 1, retries: int = 0,
                 quarantine: Iterable[str] = (), flags: Iterable[str] = (),
                 seed: Optional[int] = None,
                 on_result: Optional[Callable[[TestResult], None]] = None) -> None:
        unknown = set(flags) - set(KNOWN_FLAGS)
        if unknown:
            raise ValueError(f"unknown feature flag(s): {sorted(unknown)}; known: {sorted(KNOWN_FLAGS)}")
        self.topology = topology
        self.workers = max(1, workers)
        self.retries = max(0, retries)
        self.quarantine = set(quarantine)
        self.flags = frozenset(flags)
        self.seed = seed
        self.on_result = on_result
        self._device_locks = {name: threading.Lock() for name in topology.devices}
        self._stop = threading.Event()
        self._baseline: Dict[str, Dict[str, Any]] = {}

    # -- helpers ----------------------------------------------------------
    def _is_quarantined(self, test_id: str) -> bool:
        return any(fnmatch.fnmatch(test_id, pat) for pat in self.quarantine)

    def _devices_needed(self, tc: TestCase, defs: Dict[str, FixtureDef]) -> List[str]:
        """Walk the fixture graph to find which devices a test touches."""
        needed: Set[str] = set()
        seen: Set[str] = set()
        todo = list(tc.params)
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            if n in ("topology", "baseline"):
                return sorted(self._device_locks)
            if n in self._device_locks:
                needed.add(n)
            elif n in defs:
                todo += defs[n].params
        return sorted(needed)

    def _builtin_defs(self) -> Dict[str, FixtureDef]:
        def baseline(topology):
            for name, dev in topology.devices.items():
                dev.restore(self._baseline[name])
            return topology
        defs = dict(registry())
        defs["baseline"] = FixtureDef("baseline", baseline, "test")
        return defs

    # -- execution --------------------------------------------------------
    def _run_once(self, tc: TestCase, resolver: FixtureResolver, defs) -> TestResult:
        logger = TestLogger()
        marks = {name: len(d.transcript) for name, d in self.topology.devices.items()}
        t0 = time.perf_counter()
        status, message = PASS, ""
        with ExitStack() as stack:
            try:
                kwargs = resolver.resolve(tc.params, stack, {"log": logger})
            except SkipTest as exc:
                status, message = SKIP, str(exc)
            except Exception as exc:  # fixture failure = ERROR, not FAIL
                status, message = ERROR, f"fixture setup: {type(exc).__name__}: {exc}"
            else:
                try:
                    tc.func(**{p: kwargs[p] for p in tc.params})
                except SkipTest as exc:
                    status, message = SKIP, str(exc)
                except AssertionError as exc:
                    status, message = FAIL, str(exc) or "assertion failed"
                    logger(traceback.format_exc(limit=4))
                except Exception as exc:
                    status, message = ERROR, f"{type(exc).__name__}: {exc}"
                    logger(traceback.format_exc(limit=6))
        transcript = []
        for name, d in self.topology.devices.items():
            transcript += d.transcript[marks[name]:]
        return TestResult(tc.id, status, time.perf_counter() - t0, message,
                          log=logger.lines, transcript=transcript)

    def _run_case(self, tc: TestCase, resolver: FixtureResolver, defs) -> TestResult:
        flag = getattr(tc.func, "__santh_requires_flag__", None)
        early = None
        if flag and flag not in self.flags:
            early = TestResult(tc.id, SKIP, 0.0, f"feature flag {flag!r} not enabled")
        elif self._stop.is_set():
            early = TestResult(tc.id, SKIP, 0.0, "fail_fast: earlier failure")
        if early is not None:
            if self.on_result:
                self.on_result(early)
            return early
        locks = [self._device_locks[n] for n in self._devices_needed(tc, defs)]
        for lk in locks:          # sorted order => no deadlock
            lk.acquire()
        try:
            attempts, result = 0, None
            while True:
                attempts += 1
                result = self._run_once(tc, resolver, defs)
                if result.status not in (FAIL, ERROR) or attempts > self.retries:
                    break
            if attempts > 1 and result.status == PASS:
                result.status = FLAKY
                result.message = f"passed on attempt {attempts} of {self.retries + 1}"
            result.attempts = attempts
        finally:
            for lk in reversed(locks):
                lk.release()
        result.quarantined = self._is_quarantined(tc.id)
        if result.blocking and "fail_fast" in self.flags:
            self._stop.set()
        if self.on_result:
            self.on_result(result)
        return result

    def run(self, cases: Sequence[TestCase]) -> RunSummary:
        cases = list(cases)
        if "shuffle" in self.flags:
            random.Random(self.seed).shuffle(cases)
        started = time.time()
        self.topology.connect_all()
        self._baseline = {n: d.snapshot() for n, d in self.topology.devices.items()}
        defs = self._builtin_defs()
        static = {"topology": self.topology, "flags": self.flags, **self.topology.devices}
        try:
            with ExitStack() as session_stack:
                resolver = FixtureResolver(defs, session_stack, static)
                if self.workers == 1:
                    results = [self._run_case(tc, resolver, defs) for tc in cases]
                else:
                    with ThreadPoolExecutor(self.workers) as pool:
                        results = list(pool.map(lambda tc: self._run_case(tc, resolver, defs), cases))
        finally:
            # leave the lab as we found it
            for n, d in self.topology.devices.items():
                try:
                    d.restore(self._baseline[n])
                except Exception:
                    pass
            self.topology.disconnect_all()
        return RunSummary(results, started, time.time())
