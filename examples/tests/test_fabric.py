# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""Example suite: test cases state intent only; the harness owns the mechanics."""
from santh import expect, expect_at_least, expect_equal, requires_flag, tags, wait_until


@tags("smoke")
def test_all_ports_online(baseline, lab_info):
    for name, dev in baseline.devices.items():
        for port in range(lab_info[name]):
            expect_equal(dev.port_state(port), "Online", f"{name} port {port}")


@tags("smoke")
def test_zone_lifecycle(baseline):
    sw = baseline["core1"]
    sw.create_zone("z_host1_array1", ["10:00:00:00:c9:00:00:01", "50:00:00:00:00:00:00:01"])
    expect("z_host1_array1" in sw.zones, "zone was created")
    sw.delete_zone("z_host1_array1")
    expect("z_host1_array1" not in sw.zones, "zone was deleted")


def test_baseline_undoes_previous_test(baseline):
    # test_zone_lifecycle (or any other test) must not leak state into this one
    expect_equal(baseline["core1"].zones, {}, "zoning at baseline")


@tags("congestion")
def test_slow_drain_raises_bb_credit_zero(clean_edge, log):
    """Harness-level fault injection reproduces a slow-drain F-Port on demand."""
    clean_edge.inject_fault(5, "congestion")
    counters = wait_until(lambda: clean_edge.counters(5)["bb_credit_zero"] > 10_000 and clean_edge.counters(5),
                          timeout=5, interval=0, on_poll=clean_edge.tick)
    log(f"port 5 counters after injection: {counters}")
    expect_at_least(counters["c3_discard"], 1, "C3 discards on slow-drain port")
    expect_equal(clean_edge.counters(6)["bb_credit_zero"], 0, "neighbour port unaffected")


@tags("optics")
def test_crc_fault_isolated_to_port(clean_edge):
    clean_edge.inject_fault(2, "crc")
    for _ in range(3):
        clean_edge.tick()
    expect_at_least(clean_edge.counters(2)["crc_err"], 1, "CRC errors on faulted port")
    expect_equal(clean_edge.counters(3)["crc_err"], 0, "CRC errors on clean port")


@requires_flag("shuffle")
def test_only_when_shuffle_enabled():
    """Demonstrates a test gated behind a harness feature flag."""
    expect(True)
