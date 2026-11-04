# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point.

    santh run  examples/tests --topology examples/topology.json -j 4 --retries 1 \
               --junit results.xml --tap results.tap
    santh tap  ./legacy_test.sh other_test.py saved.tap
    santh flags
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from . import __version__
from .devices import Topology
from .reporting import (console_line, console_summary, run_tap_source, tap_streams_to_summary,
                        to_json, to_junit, to_tap, write)
from .runner import KNOWN_FLAGS, LabLock, Runner, discover, load_quarantine


def _add_outputs(p: argparse.ArgumentParser) -> None:
    p.add_argument("--junit", metavar="FILE", help="write JUnit XML report")
    p.add_argument("--tap", metavar="FILE", help="write TAP v13 report ('-' for stdout)")
    p.add_argument("--json", metavar="FILE", help="write JSON report")
    p.add_argument("--no-color", action="store_true")


def _emit(args, summary) -> int:
    print(console_summary(summary))
    if args.junit:
        write(args.junit, to_junit(summary))
    if args.tap:
        write(args.tap, to_tap(summary))
    if args.json:
        write(args.json, to_json(summary))
    return 0 if summary.ok else 1


def cmd_run(args) -> int:
    topology = Topology.from_file(args.topology) if args.topology else Topology({})
    cases = discover(args.paths, keyword=args.keyword, tag_filter=set(args.tag) if args.tag else None)
    if not cases:
        print("no tests collected", file=sys.stderr)
        return 5
    color = sys.stdout.isatty() and not args.no_color
    runner = Runner(topology, workers=args.jobs, retries=args.retries,
                    quarantine=load_quarantine(args.quarantine), flags=args.enable, seed=args.seed,
                    on_result=lambda r: print(console_line(r, color), flush=True))
    print(f"santh {__version__}: {len(cases)} test(s), {len(topology.devices)} device(s), "
          f"{args.jobs} worker(s), retries={args.retries}, flags={sorted(args.enable) or 'none'}")
    if args.lab_lock:
        with LabLock(args.lab_lock, wait=args.lock_wait):
            summary = runner.run(cases)
    else:
        summary = runner.run(cases)
    return _emit(args, summary)


def cmd_tap(args) -> int:
    started = time.time()
    streams = []
    for source in args.sources:
        s = run_tap_source(source.split() if " " in source else [source], timeout=args.timeout)
        print(f"{'PASS' if s.passed else 'FAIL':4s} {s.source}: {len(s.results)} result(s)")
        streams.append(s)
    return _emit(args, tap_streams_to_summary(streams, started))


def cmd_flags(_args) -> int:
    for name, desc in sorted(KNOWN_FLAGS.items()):
        print(f"{name:12s} (off by default) {desc}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="santh", description="Layered test harness for SAN / systems automation")
    ap.add_argument("--version", action="version", version=f"santh {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="discover and run python test functions")
    r.add_argument("paths", nargs="+", help="test files or directories")
    r.add_argument("--topology", "-t", help="JSON topology file")
    r.add_argument("--jobs", "-j", type=int, default=1, help="parallel workers")
    r.add_argument("--retries", type=int, default=0, help="retries before a failure is final")
    r.add_argument("--quarantine", help="file of test ids/globs that may fail without failing the build")
    r.add_argument("--enable", action="append", default=[], metavar="FLAG", help="enable a feature flag")
    r.add_argument("--seed", type=int, help="seed for the 'shuffle' flag")
    r.add_argument("-k", dest="keyword", help="only tests whose id contains this text")
    r.add_argument("--tag", action="append", help="only tests carrying this tag")
    r.add_argument("--lab-lock", metavar="FILE", help="reserve the lab via an exclusive lock file")
    r.add_argument("--lock-wait", type=float, default=0, help="seconds to wait for the lab lock")
    _add_outputs(r)
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("tap", help="run TAP producers (executables, .py, .tap files)")
    t.add_argument("sources", nargs="+")
    t.add_argument("--timeout", type=float)
    _add_outputs(t)
    t.set_defaults(func=cmd_tap)

    f = sub.add_parser("flags", help="list harness feature flags")
    f.set_defaults(func=cmd_flags)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
