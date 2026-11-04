# Author / Owner / Publisher: Derek Asir Muthurajan Caleb
# Copyright (c) 2026 Derek Asir Muthurajan Caleb
# SPDX-License-Identifier: Apache-2.0
"""Layer 1 - Device / service abstraction.

Test cases never talk to a system under test (SUT) directly. They receive a
``Device`` object from the harness and call intent-level methods on it.  A
change in the SUT's command syntax is absorbed here, in one place.

Two drivers ship with the package:

* ``SimulatedFCSwitch`` - an in-memory Fibre Channel switch model with ports,
  zoning, counters and a fault-injection hook.  It lets the whole harness (and
  its own unit tests) run on a laptop with no lab hardware.
* ``SSHCommandDevice`` - a thin adapter that runs CLI commands over the system
  ``ssh`` binary.  Subclass it to map intent-level calls onto a real CLI.
"""
from __future__ import annotations

import copy
import json
import shlex
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type


class DeviceError(RuntimeError):
    """Raised when a device operation fails."""


class Device(ABC):
    """Stable API every driver must provide."""

    def __init__(self, name: str, **params: Any) -> None:
        self.name = name
        self.params = params
        self.transcript: List[Dict[str, str]] = []  # command/response log
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> None:  # pragma: no cover - default is a no-op
        pass

    def disconnect(self) -> None:  # pragma: no cover - default is a no-op
        pass

    # -- state used by the deterministic-baseline fixture ----------------
    @abstractmethod
    def snapshot(self) -> Dict[str, Any]:
        """Return a serialisable copy of configuration state."""

    @abstractmethod
    def restore(self, state: Dict[str, Any]) -> None:
        """Return the device to a state previously captured by ``snapshot``."""

    # -- raw command channel (optional) ----------------------------------
    def run(self, command: str) -> str:
        raise DeviceError(f"{type(self).__name__} does not support raw commands")

    def _log(self, command: str, response: str) -> str:
        self.transcript.append({"device": self.name, "cmd": command, "out": response})
        return response


# ---------------------------------------------------------------------------
# Simulated Fibre Channel switch
# ---------------------------------------------------------------------------
@dataclass
class Port:
    index: int
    kind: str = "F"            # F (device), E (ISL), U (unused)
    state: str = "Online"
    speed_gbps: int = 32
    counters: Dict[str, int] = field(default_factory=lambda: {
        "tx_frames": 0, "rx_frames": 0, "crc_err": 0, "c3_discard": 0,
        "bb_credit_zero": 0, "link_reset": 0,
    })


class SimulatedFCSwitch(Device):
    """In-memory FC switch model.

    Supported fault types for :meth:`inject_fault`:

    ``congestion``   - BB-credit-zero and C3 discards climb on the port
    ``credit_loss``  - link resets occur as credit recovery fires
    ``crc``          - CRC errors (bad optic / cable)
    ``offline``      - port goes offline
    """

    FAULTS = ("congestion", "credit_loss", "crc", "offline")

    def __init__(self, name: str, ports: int = 16, domain: int = 1, **params: Any) -> None:
        super().__init__(name, **params)
        self.domain = domain
        self.ports: Dict[int, Port] = {i: Port(i) for i in range(ports)}
        self.zones: Dict[str, List[str]] = {}
        self.active_faults: Dict[int, str] = {}
        self.config: Dict[str, Any] = {"switch_name": name, "domain": domain}

    # -- intent-level API -----------------------------------------------
    def port_state(self, port: int) -> str:
        return self._port(port).state

    def set_port_state(self, port: int, state: str) -> None:
        with self._lock:
            self._port(port).state = state
            self._log(f"port {'enable' if state == 'Online' else 'disable'} {port}", "ok")

    def create_zone(self, zone: str, members: List[str]) -> None:
        with self._lock:
            self.zones[zone] = list(members)
            self._log(f'zone create {zone} {",".join(members)}', "ok")

    def delete_zone(self, zone: str) -> None:
        with self._lock:
            if zone not in self.zones:
                raise DeviceError(f"zone {zone!r} does not exist")
            del self.zones[zone]
            self._log(f'zone delete {zone}', "ok")

    def counters(self, port: int) -> Dict[str, int]:
        return dict(self._port(port).counters)

    def clear_counters(self, port: Optional[int] = None) -> None:
        with self._lock:
            targets = [self._port(port)] if port is not None else list(self.ports.values())
            for p in targets:
                for k in p.counters:
                    p.counters[k] = 0
            self._log(f"counters clear {'' if port is None else port}".strip(), "ok")

    def inject_fault(self, port: int, fault: str) -> None:
        """Harness-level fault injection (see paper Sec. IV-E)."""
        if fault not in self.FAULTS:
            raise DeviceError(f"unknown fault {fault!r}; choose from {self.FAULTS}")
        with self._lock:
            self._port(port)
            self.active_faults[port] = fault
            if fault == "offline":
                self.ports[port].state = "Offline"
            self._log(f"fault inject {port} {fault}", "ok")

    def clear_fault(self, port: int) -> None:
        with self._lock:
            self.active_faults.pop(port, None)
            if self.ports[port].state == "Offline":
                self.ports[port].state = "Online"

    def tick(self, frames: int = 1000) -> None:
        """Advance simulated time: traffic flows, active faults raise counters."""
        with self._lock:
            for p in self.ports.values():
                if p.state != "Online":
                    continue
                fault = self.active_faults.get(p.index)
                if fault == "congestion":
                    p.counters["bb_credit_zero"] += frames // 2
                    p.counters["c3_discard"] += frames // 100
                    p.counters["tx_frames"] += frames // 4
                elif fault == "credit_loss":
                    p.counters["link_reset"] += 1
                    p.counters["tx_frames"] += frames // 2
                elif fault == "crc":
                    p.counters["crc_err"] += frames // 50
                    p.counters["tx_frames"] += frames
                else:
                    p.counters["tx_frames"] += frames
                p.counters["rx_frames"] += frames

    # -- baseline support -----------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy({
                "config": self.config,
                "zones": self.zones,
                "ports": {i: (p.kind, p.state, p.speed_gbps) for i, p in self.ports.items()},
            })

    def restore(self, state: Dict[str, Any]) -> None:
        with self._lock:
            self.config = copy.deepcopy(state["config"])
            self.zones = copy.deepcopy(state["zones"])
            self.active_faults.clear()
            for i, (kind, st, speed) in state["ports"].items():
                p = self.ports[int(i)]
                p.kind, p.state, p.speed_gbps = kind, st, speed
            self.clear_counters()

    # -- raw channel, so recorded CLI can be replayed --------------------
    def run(self, command: str) -> str:
        parts = shlex.split(command)
        if not parts:
            return self._log(command, "")
        if parts[:2] == ["show", "switch"]:
            lines = [f"switchName: {self.name}", f"switchDomain: {self.domain}"]
            lines += [f"{p.index:4d}  {p.speed_gbps}G  {p.state:8s} {p.kind}-Port"
                      for p in self.ports.values()]
            return self._log(command, "\n".join(lines))
        if parts[:2] == ["show", "zones"]:
            return self._log(command, json.dumps(self.zones, indent=2))
        raise DeviceError(f"simulator: unsupported command {parts[0]!r}")

    def _port(self, port: int) -> Port:
        if port not in self.ports:
            raise DeviceError(f"{self.name}: no such port {port}")
        return self.ports[port]


# ---------------------------------------------------------------------------
# SSH adapter for real equipment
# ---------------------------------------------------------------------------
class SSHCommandDevice(Device):
    """Runs commands through the OpenSSH client (key-based auth expected).

    Override ``snapshot``/``restore`` for your platform, e.g. using
    your platform's configuration backup/restore commands.
    """

    def __init__(self, name: str, host: str, user: str = "admin",
                 port: int = 22, timeout: int = 60, **params: Any) -> None:
        super().__init__(name, host=host, user=user, **params)
        self.host, self.user, self.port, self.timeout = host, user, port, timeout

    def run(self, command: str) -> str:
        argv = ["ssh", "-o", "BatchMode=yes", "-p", str(self.port),
                f"{self.user}@{self.host}", command]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
        if proc.returncode != 0:
            raise DeviceError(f"{self.name}: {command!r} failed: {proc.stderr.strip()}")
        return self._log(command, proc.stdout)

    def snapshot(self) -> Dict[str, Any]:
        return {}

    def restore(self, state: Dict[str, Any]) -> None:
        pass


# ---------------------------------------------------------------------------
# Topology: configuration-driven device construction
# ---------------------------------------------------------------------------
DRIVERS: Dict[str, Type[Device]] = {
    "simulated_fc_switch": SimulatedFCSwitch,
    "ssh": SSHCommandDevice,
}


def register_driver(kind: str) -> Callable[[Type[Device]], Type[Device]]:
    """Decorator for plug-in drivers: ``@register_driver("my_array")``."""
    def deco(cls: Type[Device]) -> Type[Device]:
        DRIVERS[kind] = cls
        return cls
    return deco


class Topology:
    """A named set of devices built from a JSON description::

        {"devices": [{"name": "sw1", "driver": "simulated_fc_switch", "ports": 24}]}
    """

    def __init__(self, devices: Dict[str, Device]) -> None:
        self.devices = devices

    @classmethod
    def from_dict(cls, spec: Dict[str, Any]) -> "Topology":
        devices: Dict[str, Device] = {}
        for entry in spec.get("devices", []):
            entry = dict(entry)
            name, kind = entry.pop("name"), entry.pop("driver")
            if kind not in DRIVERS:
                raise DeviceError(f"unknown driver {kind!r}; registered: {sorted(DRIVERS)}")
            devices[name] = DRIVERS[kind](name, **entry)
        return cls(devices)

    @classmethod
    def from_file(cls, path: str) -> "Topology":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def __getitem__(self, name: str) -> Device:
        return self.devices[name]

    def __iter__(self):
        return iter(self.devices.values())

    def connect_all(self) -> None:
        for d in self:
            d.connect()

    def disconnect_all(self) -> None:
        for d in self:
            d.disconnect()
