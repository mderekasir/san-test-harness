# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the example suite."""
from santh import fixture


@fixture
def clean_edge(edge1):
    """Test-scoped: counters zeroed before the test, faults cleared after."""
    edge1.clear_counters()
    yield edge1
    for port in list(edge1.active_faults):
        edge1.clear_fault(port)


@fixture(scope="session")
def lab_info(topology):
    """Session-scoped: computed once per run."""
    return {name: len(dev.ports) for name, dev in topology.devices.items()}
