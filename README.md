# A layered test harness for SAN automation

Companion code for the article **"Harnessing Automation: Test Harness Architectures for
Scalable, Repeatable Software Verification"** (Derek Asir Muthurajan Caleb, 2026).

`santh` is a small Python harness with no dependencies. It puts the five-layer design from the
paper into code. Test cases state only *what* should be true. The harness handles *how*: device
access, setup and teardown, scheduling, retries, and reporting.

| Paper layer (Table I) | Module | What it gives you |
|---|---|---|
| Abstraction | `santh/devices.py` | `Device` API, a simulated Fibre Channel switch with fault injection, an SSH adapter, and JSON topologies |
| Fixtures | `santh/fixtures.py` | `@fixture` with `test`/`session` scope, generator teardown, and a built-in `baseline` restore |
| Execution | `santh/runner.py` | Parallel workers with per-device locks, retries with FLAKY detection, a quarantine list, a lab lock, and feature flags |
| Assertion | `santh/assertions.py` | `expect_*` helpers, and `wait_until` for state polling instead of fixed sleeps |
| Reporting | `santh/reporting.py` | Console, TAP v13, JUnit XML, and JSON output, plus a TAP bridge that runs any TAP-producing program |

## Quick start

```bash
pip install .                       # or run in place with: python -m santh ...
santh run examples/tests --topology examples/topology.json -j 2 --junit results.xml
santh tap examples/legacy_tap_test.py      # any TAP producer: script, binary, or .tap file
santh flags                                # list feature flags (all off by default)
python -m unittest discover -s tests       # the harness's own tests
```

You need Python 3.9 or later. There are no third-party packages.

## Writing a test

```python
from santh import expect_at_least, wait_until, tags

@tags("congestion")
def test_slow_drain(baseline, log):           # 'baseline' = every device reset to its start state
    sw = baseline["edge1"]
    sw.inject_fault(5, "congestion")          # the harness owns how the fault is produced
    c = wait_until(lambda: sw.counters(5)["bb_credit_zero"] > 0 and sw.counters(5),
                   timeout=10, on_poll=sw.tick)
    log(f"counters: {c}")
    expect_at_least(c["c3_discard"], 1, "C3 discards")
```

A test requests what it needs by naming parameters:

- `baseline`, `topology`, `flags`, and `log`
- any device name from the topology (for example `edge1`)
- any fixture registered in a `conftest.py`

## Key behaviours

- **Deterministic start state.** Every device is snapshotted at session start. The `baseline`
  fixture restores that snapshot before each test, and the run restores it again at the end.
- **Per-device locking.** The runner walks each test's fixture graph to find which devices it
  touches. Tests that share a device run one at a time. Tests on separate devices run in parallel.
- **Flaky detection.** With `--retries N`, a test that fails and then passes is reported as
  `FLAKY`, not `PASS`, so the instability stays visible.
- **Quarantine.** In `--quarantine FILE`, list one test id or glob per line. Those tests still run
  and report, but cannot fail the build. In TAP output they are marked `# TODO`, and in JUnit
  output they are marked `skipped`.
- **Feature flags.** New harness behaviour (`shuffle`, `fail_fast`) stays off unless you turn it
  on with `--enable FLAG`. Tests can be gated with `@requires_flag("...")`.
- **Lab reservation.** `--lab-lock /shared/lab42.lock` takes an exclusive lock file for the
  whole run.
- **Command transcripts.** Every device command and response is recorded for each test and
  written to the JUnit `system-out` field.

## Adding a real device driver

```python
from santh import register_driver, SSHCommandDevice

@register_driver("my_fc_switch")
class MySwitch(SSHCommandDevice):
    def port_state(self, port):
        return "Online" if "Online" in self.run(f"show port {port}") else "Offline"
    def snapshot(self):
        return {"cfg": self.run("show config")}
    def restore(self, state):
        ...  # push state["cfg"] back with your platform's restore mechanism
```

Then reference the driver from the topology file:
`{"name": "sw1", "driver": "my_fc_switch", "host": "10.0.0.5"}`.

## Contributing

The harness follows the contribution rules in Section VI of the paper:

- every harness change comes with a test in `tests/`
- new behaviour ships behind a flag that is off by default
- harness changes are reviewed separately from test-case changes

## Author

**Derek Asir Muthurajan Caleb**: author, owner, and publisher.

This is an independent implementation of the ideas in the article. It contains no proprietary
code, data, or command syntax from any vendor. The switch simulator uses generic,
vendor-neutral commands.

## License

Apache License 2.0. See [LICENSE](LICENSE).
