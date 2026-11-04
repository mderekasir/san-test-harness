# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""Layer 5 - Reporting and telemetry, plus a Test Anything Protocol (TAP) bridge.

Writers: console, TAP version 13, JUnit XML (for CI dashboards), JSON.

The TAP bridge (paper Sec. III-C) lets the harness run *any* executable that
emits TAP - a shell script, a C binary, a saved ``.tap`` file - and fold its
results into the same report.  The protocol, not a shared codebase, is the
contract between test and harness.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import IO, Iterable, Iterator, List, Optional, Sequence

from .runner import ERROR, FAIL, FLAKY, PASS, SKIP, RunSummary, TestResult

_COLORS = {PASS: "\033[32m", FLAKY: "\033[33m", SKIP: "\033[36m", FAIL: "\033[31m", ERROR: "\033[35m"}


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------
def console_line(r: TestResult, color: bool = False) -> str:
    st = r.status + (" [quarantined]" if r.quarantined else "")
    if color:
        st = f"{_COLORS.get(r.status, '')}{st}\033[0m"
    msg = f" - {r.message}" if r.message else ""
    return f"{st:8s} {r.id} ({r.duration:.2f}s){msg}"


def console_summary(s: RunSummary) -> str:
    parts = [f"{s.count(x)} {x.lower()}" for x in (PASS, FLAKY, FAIL, ERROR, SKIP) if s.count(x)]
    q = sum(1 for r in s.results if r.quarantined and r.status in (FAIL, ERROR))
    if q:
        parts.append(f"{q} quarantined failure(s) ignored")
    verdict = "OK" if s.ok else "FAILED"
    return f"{verdict}: {len(s.results)} tests in {s.finished - s.started:.1f}s - " + ", ".join(parts or ["nothing ran"])


# ---------------------------------------------------------------------------
# TAP writer
# ---------------------------------------------------------------------------
def _yaml_block(r: TestResult) -> List[str]:
    out = ["  ---", f"  duration_ms: {r.duration * 1000:.1f}", f"  attempts: {r.attempts}"]
    if r.message:
        out.append(f"  message: {json.dumps(r.message)}")
    if r.quarantined:
        out.append("  quarantined: true")
    out.append("  ...")
    return out


def to_tap(s: RunSummary) -> str:
    lines = ["TAP version 13", f"1..{len(s.results)}"]
    for i, r in enumerate(s.results, 1):
        ok = r.status in (PASS, FLAKY, SKIP) or r.quarantined
        directive = ""
        if r.status == SKIP:
            directive = f" # SKIP {r.message}"
        elif r.quarantined and r.status in (FAIL, ERROR):
            directive = " # TODO quarantined"
        lines.append(f"{'ok' if ok else 'not ok'} {i} - {r.id}{directive}")
        if r.status != SKIP:
            lines += _yaml_block(r)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# JUnit XML writer
# ---------------------------------------------------------------------------
def to_junit(s: RunSummary, suite_name: str = "santh") -> str:
    suite = ET.Element("testsuite", name=suite_name, tests=str(len(s.results)),
                       failures=str(s.count(FAIL)), errors=str(s.count(ERROR)),
                       skipped=str(s.count(SKIP)), time=f"{s.finished - s.started:.3f}",
                       timestamp=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s.started)))
    for r in s.results:
        module, _, name = r.id.partition("::")
        case = ET.SubElement(suite, "testcase", classname=module, name=name or module, time=f"{r.duration:.3f}")
        if r.status == FAIL and not r.quarantined:
            ET.SubElement(case, "failure", message=r.message).text = "\n".join(r.log)
        elif r.status == ERROR and not r.quarantined:
            ET.SubElement(case, "error", message=r.message).text = "\n".join(r.log)
        elif r.status == SKIP or r.quarantined:
            ET.SubElement(case, "skipped", message=r.message or "quarantined")
        props = ET.SubElement(case, "properties")
        ET.SubElement(props, "property", name="status", value=r.status)
        ET.SubElement(props, "property", name="attempts", value=str(r.attempts))
        if r.transcript:
            ET.SubElement(case, "system-out").text = "\n".join(
                f"[{t['device']}] $ {t['cmd']}\n{t['out']}" for t in r.transcript)
    ET.indent(suite)
    return ET.tostring(suite, encoding="unicode", xml_declaration=True)


def to_json(s: RunSummary) -> str:
    return json.dumps({
        "ok": s.ok, "started": s.started, "finished": s.finished,
        "counts": {x: s.count(x) for x in (PASS, FLAKY, FAIL, ERROR, SKIP)},
        "flaky": [r.id for r in s.results if r.status == FLAKY],
        "results": [r.__dict__ for r in s.results],
    }, indent=2)


# ---------------------------------------------------------------------------
# TAP parser / bridge
# ---------------------------------------------------------------------------
_TAP_LINE = re.compile(r"^(not )?ok\b\s*(\d+)?\s*-?\s*([^#]*?)\s*(?:#\s*(SKIP|TODO)\b\s*(.*))?$", re.I)
_PLAN = re.compile(r"^1\.\.(\d+)")


@dataclass
class TapResult:
    number: int
    ok: bool
    description: str
    directive: Optional[str] = None
    reason: str = ""


@dataclass
class TapStream:
    source: str
    plan: Optional[int] = None
    results: List[TapResult] = field(default_factory=list)
    bail_out: Optional[str] = None
    exit_code: int = 0

    @property
    def passed(self) -> bool:
        if self.bail_out is not None or self.exit_code != 0:
            return False
        if self.plan is not None and self.plan != len(self.results):
            return False
        return all(r.ok or (r.directive or "").upper() == "TODO" for r in self.results)


def parse_tap(lines: Iterable[str], source: str = "<stream>") -> TapStream:
    stream = TapStream(source)
    counter = 0
    for raw in lines:
        line = raw.rstrip("\n")
        if m := _PLAN.match(line):
            stream.plan = int(m.group(1))
            continue
        if line.startswith("Bail out!"):
            stream.bail_out = line[9:].strip()
            break
        if m := _TAP_LINE.match(line):
            counter += 1
            stream.results.append(TapResult(
                int(m.group(2) or counter), m.group(1) is None, m.group(3).strip(),
                m.group(4).upper() if m.group(4) else None, (m.group(5) or "").strip()))
    return stream


def run_tap_source(command: Sequence[str], timeout: Optional[float] = None) -> TapStream:
    """Execute a TAP producer; if ``command`` is one ``.tap`` file, read it instead."""
    if len(command) == 1 and command[0].endswith(".tap"):
        with open(command[0], encoding="utf-8") as fh:
            return parse_tap(fh, command[0])
    argv = [sys.executable, *command] if command[0].endswith(".py") else list(command)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    stream = parse_tap(proc.stdout.splitlines(), " ".join(command))
    stream.exit_code = proc.returncode
    return stream


def tap_streams_to_summary(streams: Sequence[TapStream], started: float) -> RunSummary:
    results: List[TestResult] = []
    for s in streams:
        for r in s.results:
            if r.directive == "SKIP":
                status = SKIP
            else:
                status = PASS if r.ok or r.directive == "TODO" else FAIL
            results.append(TestResult(f"{s.source}::{r.number} {r.description}".strip(), status, 0.0, r.reason))
        if s.plan is not None and s.plan != len(s.results):
            results.append(TestResult(f"{s.source}::plan", ERROR, 0.0,
                                      f"planned {s.plan} tests, saw {len(s.results)}"))
        if s.bail_out is not None:
            results.append(TestResult(f"{s.source}::bail_out", ERROR, 0.0, s.bail_out))
        elif s.exit_code:
            results.append(TestResult(f"{s.source}::exit", ERROR, 0.0, f"exit code {s.exit_code}"))
    return RunSummary(results, started, time.time())


def write(path: Optional[str], text: str, default: IO[str] = sys.stdout) -> None:
    if path in (None, "-"):
        default.write(text)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
