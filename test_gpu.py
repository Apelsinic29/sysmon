#!/usr/bin/env python3
"""Exercise the IntelGpuBusy class in sysmon.py directly.

Checks that it (a) reports the precise errno when blocked, (b) opens all four
engine counters or none, (c) yields no percentage on the first sample (a delta
needs two), and (d) produces a sane 0-100 value once two samples exist.
"""

import importlib.util
import os
import sys
import time

spec = importlib.util.spec_from_file_location(
    "sysmon", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysmon.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(label, cond, detail=""):
    print("%-38s %s %s" % (label, "ok" if cond else "FAIL", detail))
    if not cond:
        fails.append(label)


check("_PerfAttr is VER0 (64 bytes)",
      __import__("ctypes").sizeof(m._PerfAttr) == 64,
      "%d bytes" % __import__("ctypes").sizeof(m._PerfAttr))

g = m.IntelGpuBusy()
g._open()

print("   available=%s why=%r engines=%s"
      % (g.available, g.why, sorted(g.fds)))

if g.available:
    # all-or-nothing: every engine present or none
    check("all engines opened", set(g.fds) == set(g.ENGINES),
          str(sorted(g.fds)))
    # first sample can only be a baseline
    first = g.sample()
    check("first sample has no percentage", first[0] is None, str(first))
    time.sleep(1.2)
    second = g.sample()
    check("second sample yields a percentage", second[0] is not None, str(second))
    if second[0] is not None:
        check("percentage in 0..100", 0.0 <= second[0] <= 100.0,
              "%.2f%%" % second[0])
    time.sleep(0.6)
    third = g.sample()
    check("third sample still sane", third[0] is None or 0.0 <= third[0] <= 100.0,
          str(third))
    g.close()
    check("close releases fds", not g.fds and not g.available)
else:
    # Blocked at paranoid=2: the message must name the real errno.
    check("blocked with a precise reason",
          "Permission denied" in g.why or "perf_event_open" in g.why, g.why)
    check("no fds leaked when blocked", not g.fds)
    check("sample() is safe when unavailable", g.sample() == (None, None))

# the panel-facing path must never raise, whatever the permission state
try:
    usage, temp, note = m.read_gpu(m.read_sensors())
    check("read_gpu returns 3-tuple", True,
          "usage=%s temp=%s note=%r" % (usage, temp, note))
    check("note explains the gap", isinstance(note, str) and note != "", note)
except Exception as exc:
    check("read_gpu returns 3-tuple", False, "raised %r" % exc)

print()
if fails:
    print("FAILURES: %s" % fails)
    sys.exit(1)
print("i915 PMU reader checks passed")
