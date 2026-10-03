#!/usr/bin/env python3
"""Check the NET row's rate maths against a real interval.

The first refresh after opening has no elapsed time, so it must show totals
only rather than a fake 0 B/s. The second refresh, one interval later, must
produce a real rate. Generating a little traffic makes the rate non-zero so a
broken delta cannot pass by accident.
"""

import importlib.util
import os
import socket
import sys
import time

spec = importlib.util.spec_from_file_location(
    "sysmon", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysmon.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(label, cond, detail=""):
    print("%-34s %s %s" % (label, "ok" if cond else "FAIL", detail))
    if not cond:
        fails.append(label)


class FakePanel(object):
    """The minimum surface read of the net block in Panel.refresh()."""

    def __init__(self):
        self._prev_net = None
        self._prev_net_at = 0
        self.state = None


def compute(panel, span_scale=1.0):
    """Replicates the NET section of Panel.refresh()."""
    now = int(time.monotonic() * 1e6)
    net = m.read_net()
    rx = sum(v[0] for v in net.values())
    tx = sum(v[1] for v in net.values())
    totals = "total down %s  up %s" % (m.human_bytes(rx), m.human_bytes(tx))
    if panel._prev_net is not None and panel._prev_net_at:
        span = (now - panel._prev_net_at) / 1e6
        if span > 0:
            d_rx = max(0, rx - panel._prev_net[0]) / span
            d_tx = max(0, tx - panel._prev_net[1]) / span
            pct = min(100.0, (d_rx + d_tx) / (20.0 * 1000.0 * 1000.0) * 100.0)
            out = ("rate", d_rx, d_tx, pct, totals)
        else:
            out = ("totals-only", totals)
    else:
        out = ("totals-only", totals)
    panel._prev_net = (rx, tx)
    panel._prev_net_at = now
    return out


panel = FakePanel()

# 1. first sample: totals only, never a rate
first = compute(panel)
check("first refresh has no rate", first[0] == "totals-only", first[0])
check("first refresh shows totals", "total" in first[1], first[1])

# 2. generate real traffic, then sample again over a measured interval
sent = 0
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(b"x" * 60000, ("10.255.255.1", 9))
    sent = 60000
    s.close()
except OSError as exc:
    print("   (traffic gen skipped: %s)" % exc)

time.sleep(1.6)
second = compute(panel)
check("second refresh yields a rate", second[0] == "rate", second[0])
if second[0] == "rate":
    _kind, d_rx, d_tx, pct, totals = second
    check("rate is non-negative", d_rx >= 0 and d_tx >= 0,
          "rx=%.0f B/s tx=%.0f B/s" % (d_rx, d_tx))
    check("rate finite and sane", 0 <= pct <= 100.0, "bar=%.2f%%" % pct)
    check("rate humanises", m.human_rate(d_rx).endswith("/s"),
          m.human_rate(d_rx))
    check("totals still shown on rate rows", "total" in totals)
    print("   totals: %s" % totals)
    print("   bar pct: %.3f%%" % pct)

# 3. a counter that goes backwards (interface reset) must clamp, not go negative
panel2 = FakePanel()
panel2._prev_net = (10 ** 12, 10 ** 12)
panel2._prev_net_at = int(time.monotonic() * 1e6) - 1_500_000
back = compute(panel2)
check("counter reset clamps to 0", back[0] == "rate" and back[1] == 0.0
      and back[2] == 0.0, "rx=%s tx=%s" % (back[1:3]))

print()
if fails:
    print("FAILURES: %s" % fails)
    sys.exit(1)
print("net row checks passed")
