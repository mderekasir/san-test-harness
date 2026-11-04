# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""A stand-alone TAP producer: shows that any program emitting TAP can be run by
``santh tap`` with no import of the harness at all."""
print("TAP version 13")
print("1..3")
print("ok 1 - switch reachable")
print("ok 2 - firmware version matches target")
print("ok 3 - telemetry collector configured # SKIP no collector in this lab")
