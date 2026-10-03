#!/usr/bin/env python3
"""Headless check of the data layer: no GTK, no D-Bus, no display."""

import importlib.util
import os
import sys
import time

spec = importlib.util.spec_from_file_location(
    "sysmon",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysmon.py"),
)
# import the module without running main(); GTK imports work without DISPLAY,
# only window creation does not.
sysmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sysmod)

fails = []


def check(label, cond, detail=""):
    print("%-26s %s %s" % (label, "ok" if cond else "FAIL", detail))
    if not cond:
        fails.append(label)


busy, total = sysmod.read_cpu_times()
check("read_cpu_times", total > busy > 0, "busy=%d total=%d" % (busy, total))

used, tot = sysmod.read_mem()
check("read_mem", tot > 0 and 0 <= used <= tot,
      "%s / %s" % (sysmod.human_bytes(used), sysmod.human_bytes(tot)))

sensors = sysmod.read_sensors()
check("read_sensors", isinstance(sensors, dict), "%d entries: %s"
      % (len(sensors), list(sensors)[:4]))

disks = sysmod.read_disks()
check("read_disks", len(disks) > 0, "%s" % [
    "%s %d%%" % (d["mount"], round(d["pct"])) for d in disks])
for d in disks:
    check("  disk sane", 0 <= d["pct"] <= 100 and d["used"] <= d["size"],
          "%s %.1f%%" % (d["mount"], d["pct"]))

net = sysmod.read_net()
check("read_net", len(net) > 0 and "lo" not in net, "%s" % list(net))
check("read_loadavg", sysmod.read_loadavg() is not None,
      "%.2f" % (sysmod.read_loadavg() or 0))

usage, temp, note = sysmod.read_gpu(sensors)
check("read_gpu", isinstance(usage, (int, float, type(None))), "usage=%s temp=%s" % (usage, temp))
check("read_gpu explains itself", isinstance(note, str) and note != "",
      "note=%r" % note)

check("human_bytes", sysmod.human_bytes(1536) == "1.5 KB", sysmod.human_bytes(1536))
check("human_rate", sysmod.human_rate(2048).endswith("/s"), sysmod.human_rate(2048))

# sampler: cpu pct must land in a sane band and track real load
s = sysmod.Sampler()
check("sampler init", 0 <= s.cpu_pct <= 100 and 0 <= s.mem_pct <= 100,
      "cpu=%.1f mem=%.1f" % (s.cpu_pct, s.mem_pct))

# Spin a core in a child process and confirm the /proc/stat delta tracks it.
# Use a plain subprocess-free busy loop via os.fork so the burn is a real
# userspace CPU hog on one core of this machine.
read1 = sysmod.Sampler()
pid = os.fork()
if pid == 0:
    end = time.time() + 1.5
    x = 0
    while time.time() < end:
        x += 1
    os._exit(0)
time.sleep(0.5)                       # let the child get going
busy_pct = read1.sample_cpu()
os.waitpid(pid, 0)
time.sleep(1.2)                       # interval with no load at all
idle_pct = read1.sample_cpu()
check("cpu delta rises under load", busy_pct > 4.0,
      "%.1f%% while one core busy" % busy_pct)
check("cpu delta falls when idle", idle_pct < busy_pct,
      "%.1f%% busy -> %.1f%% idle" % (busy_pct, idle_pct))
check("cpu pct in range", 0.0 <= busy_pct <= 100.0 and 0.0 <= idle_pct <= 100.0)

cs, ms = s.icon_levels()
check("icon_levels", 0 <= cs <= 9 and 0 <= ms <= 9, "cpu=%d mem=%d" % (cs, ms))
check("icon_name", sysmod.icon_name(3, 7) == "sysmon-c3-r7-symbolic",
      sysmod.icon_name(3, 7))

svg = sysmod._icon_svg(0.0, 1.0)
check("icon svg", svg.startswith("<svg") and svg.rstrip().endswith("</svg>"),
      "%d bytes" % len(svg))
half = sysmod._icon_svg(0.5, 0.0)
check("icon svg half", 'width="8.00"' in half,
      [l for l in half.splitlines() if "fill=\"#ffffff\"" in l][:1])

# the icon must be a valid set of rects/circle -- catch malformed geometry
from xml.etree import ElementTree
ElementTree.fromstring(svg)
ElementTree.fromstring(half)
check("icon svg parses", True)

print()
if fails:
    print("FAILURES: %s" % fails)
    sys.exit(1)
print("all data-layer checks passed")
