# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""santh - a small, dependency-free, layered test harness for SAN and systems automation.

Companion code for "Harnessing Automation: Test Harness Architectures for
Scalable, Repeatable Software Verification" (D. A. M. Caleb, 2026).

The five layers described in the paper map to modules:

=============  =========================
Layer          Module
=============  =========================
Abstraction    :mod:`santh.devices`
Fixtures       :mod:`santh.fixtures`
Execution      :mod:`santh.runner`
Assertion      :mod:`santh.assertions`
Reporting      :mod:`santh.reporting`
=============  =========================
"""
__version__ = "0.1.0"
__author__ = "Derek Asir Muthurajan Caleb"
__copyright__ = "Copyright (c) 2026 Derek Asir Muthurajan Caleb"

from .assertions import (HarnessAssertionError, SkipTest, expect, expect_at_least, expect_at_most,
                         expect_equal, skip, wait_until)
from .devices import Device, DeviceError, SimulatedFCSwitch, SSHCommandDevice, Topology, register_driver
from .fixtures import fixture
from .runner import LabLock, Runner, discover, requires_flag, tags

__all__ = [
    "Device", "DeviceError", "SimulatedFCSwitch", "SSHCommandDevice", "Topology", "register_driver",
    "fixture", "Runner", "LabLock", "discover", "tags", "requires_flag",
    "expect", "expect_equal", "expect_at_least", "expect_at_most", "wait_until", "skip",
    "SkipTest", "HarnessAssertionError", "__version__",
]
