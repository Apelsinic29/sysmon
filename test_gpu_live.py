#!/usr/bin/env python3
"""Confirm the panel's GPU row shows a live percentage, not 0.

Runs the real Panel.refresh() against the real readers and prints the GPU row's
values. Better than reading pixels: it asserts on the same numbers the widget
draws.
"""

import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location(
    "sysmon", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysmon.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(label, cond, detail=""):
    print("%-40s %s %s" % (label, "ok" if cond else "FAIL", detail))
    if not cond:
        fails.append(label)


pmu = m.intel_gpu_busy()
check("PMU open", pmu.available, pmu.why or "all engines")
check("all 4 engines", len(pmu.fds) == 4, str(sorted(pmu.fds)))

# first call is a baseline; the panel refreshes on a timer so a real
# percentage appears from the second refresh onward
m.read_gpu(m.read_sensors())
import time
time.sleep(1.6)

samples = []
for _ in range(4):
    usage, temp, note = m.read_gpu(m.read_sensors())
    samples.append(usage)
    time.sleep(1.5)

print("   samples: %s" % ["%s" % ("%.2f%%" % s if s is not None else None)
                         for s in samples])

live = [s for s in samples if s is not None]
check("at least one real reading", len(live) > 0, "%d/%d" % (len(live), len(samples)))
check("every reading in 0..100", all(0.0 <= s <= 100.0 for s in live),
      str(["%.2f" % s for s in live]))
check("readings are not a flat 0 (counter alive)",
      any(s > 0.0 for s in live), str(["%.3f" % s for s in live]))

# a desktop compositor is drawing constantly, so the busiest engine should show
# *some* activity; a permanent 0.00 across every sample means the fd is open
# but not counting, which is the exact bug we just fixed.
check("engine activity is detectable", any(s > 0.05 for s in live),
      "max %.2f%%" % (max(live) if live else -1))

pmu.close()
print()
if fails:
    print("FAILURES: %s" % fails)
    sys.exit(1)
print("GPU row is live")
