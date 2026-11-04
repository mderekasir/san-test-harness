# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""Tests for the harness itself.  A harness bug corrupts every result (paper Sec. VI)."""
import os
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

from santh import SimulatedFCSwitch, Topology, fixture, wait_until
from santh.assertions import HarnessAssertionError
from santh.devices import DeviceError
from santh.fixtures import registry
from santh.reporting import parse_tap, tap_streams_to_summary, to_junit, to_tap
from santh.runner import ERROR, FAIL, FLAKY, PASS, SKIP, LabLock, Runner, TestCase, discover


def topo():
    return Topology.from_dict({"devices": [{"name": "sw", "driver": "simulated_fc_switch", "ports": 8}]})


class DeviceTests(unittest.TestCase):
    def test_snapshot_restore_roundtrip(self):
        sw = SimulatedFCSwitch("sw", ports=4)
        snap = sw.snapshot()
        sw.create_zone("z", ["a", "b"])
        sw.set_port_state(1, "Offline")
        sw.inject_fault(2, "crc")
        sw.restore(snap)
        self.assertEqual(sw.zones, {})
        self.assertEqual(sw.port_state(1), "Online")
        self.assertEqual(sw.active_faults, {})

    def test_fault_injection_counters(self):
        sw = SimulatedFCSwitch("sw", ports=4)
        sw.inject_fault(0, "congestion")
        sw.tick(1000)
        self.assertEqual(sw.counters(0)["bb_credit_zero"], 500)
        self.assertEqual(sw.counters(1)["bb_credit_zero"], 0)

    def test_unknown_fault_rejected(self):
        with self.assertRaises(DeviceError):
            SimulatedFCSwitch("sw").inject_fault(0, "gremlins")

    def test_unknown_driver_rejected(self):
        with self.assertRaises(DeviceError):
            Topology.from_dict({"devices": [{"name": "x", "driver": "nope"}]})


class RunnerTests(unittest.TestCase):
    def run_funcs(self, *funcs, **kw):
        cases = [TestCase(f"t::{f.__name__}", f) for f in funcs]
        return Runner(topo(), **kw).run(cases)

    def test_statuses(self):
        def test_pass(): pass
        def test_fail(): assert False, "boom"
        def test_error(): raise ValueError("bad")
        s = self.run_funcs(test_pass, test_fail, test_error)
        self.assertEqual([r.status for r in s.results], [PASS, FAIL, ERROR])
        self.assertFalse(s.ok)

    def test_flaky_detected_on_retry(self):
        calls = {"n": 0}
        def test_sometimes():
            calls["n"] += 1
            assert calls["n"] > 1
        r = self.run_funcs(test_sometimes, retries=2).results[0]
        self.assertEqual(r.status, FLAKY)
        self.assertEqual(r.attempts, 2)

    def test_quarantine_does_not_fail_build(self):
        def test_known_bad(): assert False
        s = self.run_funcs(test_known_bad, quarantine={"t::test_known_*"})
        self.assertTrue(s.ok)
        self.assertTrue(s.results[0].quarantined)

    def test_baseline_restores_state(self):
        def test_dirty(baseline): baseline["sw"].create_zone("leak", ["a"])
        def test_clean(baseline): assert baseline["sw"].zones == {}, baseline["sw"].zones
        s = self.run_funcs(test_dirty, test_clean)
        self.assertTrue(s.ok, [r.message for r in s.results])

    def test_device_lock_serialises_same_device(self):
        active, peak, lock = [0], [0], threading.Lock()
        def body(sw):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            wait_until(lambda: True)
            import time; time.sleep(0.05)
            with lock:
                active[0] -= 1
        def make(i):
            def f(sw): body(sw)
            f.__name__ = f"test_{i}"
            return f
        funcs = [make(i) for i in range(4)]
        s = self.run_funcs(*funcs, workers=4)
        self.assertTrue(s.ok)
        self.assertEqual(peak[0], 1)

    def test_fixture_teardown_runs(self):
        events = []
        @fixture(name="_tracked")
        def _tracked():
            events.append("setup"); yield 1; events.append("teardown")
        try:
            def test_uses(_tracked): events.append("body")
            self.run_funcs(test_uses)
            self.assertEqual(events, ["setup", "body", "teardown"])
        finally:
            registry().pop("_tracked", None)

    def test_fixture_failure_is_error(self):
        def test_missing(no_such_fixture): pass
        r = self.run_funcs(test_missing).results[0]
        self.assertEqual(r.status, ERROR)

    def test_unknown_flag_rejected(self):
        with self.assertRaises(ValueError):
            Runner(topo(), flags=["not_a_flag"])

    def test_transcript_captured(self):
        def test_cmd(sw): sw.create_zone("z", ["a"])
        r = self.run_funcs(test_cmd).results[0]
        self.assertTrue(any("zone create" in t["cmd"] for t in r.transcript))


class DiscoveryTests(unittest.TestCase):
    def test_discover_and_filter(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test_a.py").write_text(textwrap.dedent("""
                from santh import tags
                @tags("smoke")
                def test_one(): pass
                def test_two(): pass
                def helper(): pass
            """))
            self.assertEqual(len(discover([d])), 2)
            self.assertEqual([c.id for c in discover([d], tag_filter={"smoke"})], ["test_a::test_one"])
            self.assertEqual([c.id for c in discover([d], keyword="two")], ["test_a::test_two"])


class ReportingTests(unittest.TestCase):
    def test_tap_parse(self):
        s = parse_tap(["TAP version 13", "1..3", "ok 1 - a", "not ok 2 - b # TODO later",
                       "ok 3 - c # SKIP no hw"])
        self.assertEqual(s.plan, 3)
        self.assertTrue(s.passed)
        self.assertEqual(s.results[2].directive, "SKIP")

    def test_tap_plan_mismatch_fails(self):
        s = parse_tap(["1..2", "ok 1"])
        self.assertFalse(s.passed)
        summary = tap_streams_to_summary([s], 0)
        self.assertIn(ERROR, [r.status for r in summary.results])

    def test_tap_roundtrip(self):
        def test_ok(): pass
        def test_skip():
            from santh import skip; skip("no hw")
        summary = Runner(topo()).run([TestCase("t::ok", test_ok), TestCase("t::skip", test_skip)])
        parsed = parse_tap(to_tap(summary).splitlines())
        self.assertTrue(parsed.passed)
        self.assertEqual([r.directive for r in parsed.results], [None, "SKIP"])
        self.assertIn("<testsuite", to_junit(summary))


class LabLockTests(unittest.TestCase):
    def test_exclusive(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lab.lock")
            with LabLock(path):
                with self.assertRaises(RuntimeError):
                    with LabLock(path):
                        pass
            self.assertFalse(os.path.exists(path))


class WaitUntilTests(unittest.TestCase):
    def test_timeout(self):
        with self.assertRaises(HarnessAssertionError):
            wait_until(lambda: False, timeout=0.05, interval=0.01)


if __name__ == "__main__":
    unittest.main()
