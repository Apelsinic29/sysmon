#!/usr/bin/env python3
"""SysMon — a light tray system monitor for Omarchy.

Tray icon    -> two bars drawn to the current CPU / RAM load
Left click   -> a small panel: CPU, RAM, GPU (usage + temp, if the hardware
                exposes it), disk usage, network activity

Cost model, which is the whole point of this app:

* A background sampler wakes every BACKGROUND_INTERVAL_MS and reads exactly two
  tiny files, /proc/stat and /proc/meminfo. That is a few kB and microseconds of
  work; it exists only so the tray icon is not frozen. Everything else -- disk,
  network, GPU, sensors -- is read ONLY while the panel is visible, on the
  REFRESH_INTERVAL_MS tick, and the tick is torn down when the panel closes.
* No psutil, no subprocess in the idle path. nvidia-smi is the only exec and it
  is reached at most once per panel refresh, and not at all on machines without
  NVIDIA.

Click routing matches the shell: ItemIsMenu=false so the shell forwards a left
click straight to Activate(). Right click and Escape close the panel.
"""

import ctypes
import os
import signal
import subprocess
import time
import traceback

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import Gdk, Gio, GLib, GLibUnix, Gtk  # noqa: E402


class _PerfAttr(ctypes.Structure):
    """perf_event_attr, VER0 -- exactly 64 bytes.

    The kernel's perf_copy_attr() rejects any struct smaller than
    PERF_ATTR_SIZE_VER0 with E2BIG *before* it looks at permissions, so a
    truncated declaration is indistinguishable from "perf is blocked
    entirely". VER0 ends at bp_addr; declaring config1/bp_len as well
    overshoots to 72 and fails the same way.
    """
    _fields_ = [
        ("type", ctypes.c_uint32),            # 0
        ("size", ctypes.c_uint32),            # 4
        ("config", ctypes.c_uint64),          # 8
        ("sample_period", ctypes.c_uint64),   # 16
        ("sample_type", ctypes.c_uint64),     # 24
        ("read_format", ctypes.c_uint64),     # 32
        ("flags", ctypes.c_uint64),           # 40
        ("wakeup", ctypes.c_uint32),          # 48
        ("bp_type", ctypes.c_uint32),         # 52
        ("bp_addr", ctypes.c_uint64),         # 56
    ]

APP_ID = "sysmon"
TRAY_ID = "sysmon"
# Reverse-DNS bus/app id. Deliberately NOT tied to a username: the published
# repo is installed by other people, and a hardcoded com.<user>.<app> both leaks
# the author's name and collides if two users share a machine.
BUS_NAME = os.environ.get("SYSMON_BUS_NAME", "com.github.bigzo.sysmon")
LOG_FILE = os.environ.get("SYSMON_LOG", "")

ICON_DIR = os.path.expanduser("~/.local/share/icons/hicolor/scalable/status")

# Panel refresh while visible. 1.5s reads as live without looking like a
# benchmark; nothing here is expensive enough for a shorter tick to matter.
REFRESH_INTERVAL_MS = int(os.environ.get("SYSMON_REFRESH_MS", 1500))
# Idle sampler. Only /proc/stat + /proc/meminfo, so this can be slow.
BACKGROUND_INTERVAL_MS = 3000

BAR_HEIGHT = 44  # Omarchy bar height + breathing room

FG = "#e6edf3"
DIM = "#8b949e"
LINE = "#232a31"
BG = "#101418"
ACCENT = "#7cc4ff"
WARN = "#ffc477"
TRACK = "#2b333c"

DISK_FSTYPES = {
    "ext4", "ext3", "btrfs", "xfs", "f2fs", "jfs", "reiserfs",
    "ntfs", "ntfs3", "vfat", "exfat", "ufs", "apfs",
}
DISK_SKIP_PREFIX = ("/snap", "/run", "/var/lib/docker/", "/tmp/.X11")


def log(*parts):
    if not LOG_FILE:
        return
    try:
        with open(LOG_FILE, "a") as fh:
            fh.write(" ".join(str(p) for p in parts) + "\n")
    except OSError:
        pass


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000.0:
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1000.0
    return "%.1f EB" % n


def human_rate(n):
    return human_bytes(n) + "/s"


# --------------------------------------------------------------------------
# /proc readers. Each one is a single file read and nothing else.
# --------------------------------------------------------------------------
def read_cpu_times():
    """(busy_jiffies, total_jiffies) from /proc/stat. Two ints, one line."""
    with open("/proc/stat") as fh:
        parts = fh.readline().split()
    vals = [int(v) for v in parts[1:9]]  # user nice system idle iowait irq softirq
    idle = vals[3] + vals[4]
    total = sum(vals)
    return total - idle, total


def read_mem():
    """(used_bytes, total_bytes) from /proc/meminfo, MemAvailable aware."""
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable", "Cached", "Buffers"):
                info[key] = int(rest.split()[0]) * 1024
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable")
    if avail is None:
        avail = (info.get("Cached", 0) + info.get("Buffers", 0)
                 + (total - info.get("MemTotal", 0)))
    return max(0, total - avail), total


def read_sensors():
    """{label: degrees C} from hwmon. One read per hwmon node, no exec."""
    out = {}
    try:
        bases = sorted(
            os.path.join("/sys/class/hwmon", d)
            for d in os.listdir("/sys/class/hwmon")
        )
    except OSError:
        return out
    for base in bases:
        try:
            with open(os.path.join(base, "name")) as fh:
                name = fh.read().strip()
        except OSError:
            continue
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for entry in entries:
            if not entry.startswith("temp") or not entry.endswith("_input"):
                continue
            label = entry[: -len("_input")]
            text = ""
            try:
                with open(os.path.join(base, label + "_label")) as fh:
                    text = fh.read().strip()
            except OSError:
                pass
            try:
                with open(os.path.join(base, entry)) as fh:
                    milli = int(fh.read().strip())
            except (OSError, ValueError):
                continue
            celsius = milli / 1000.0
            key = "%s %s" % (name, text) if text else name
            key = key.strip().capitalize().replace("Ctl ", "")
            out[key] = celsius
    return out


def read_disks():
    """[{mount, size, used, pct}] for real mounted filesystems, biggest first."""
    seen = set()
    out = []
    try:
        with open("/proc/self/mounts") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        dev, mount, fstype = fields[0], fields[1], fields[2]
        if fstype not in DISK_FSTYPES:
            continue
        if mount.startswith(DISK_SKIP_PREFIX) or dev in seen:
            continue
        seen.add(dev)
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        if st.f_blocks == 0:
            continue
        size = st.f_blocks * st.f_frsize
        # df's "used" = size - available-to-you. f_bavail excludes the ~5%
        # reserved for root, which is exactly the number a user cares about.
        avail = st.f_bavail * st.f_frsize
        used = size - avail
        pct = (used / size * 100.0) if size else 0.0
        out.append({
            "mount": mount,
            "size": size,
            "used": used,
            "pct": pct,
        })
    out.sort(key=lambda d: d["size"], reverse=True)
    return out


def read_net():
    """{iface: (rx_bytes, tx_bytes)} from /proc/net/dev, loopback excluded."""
    out = {}
    try:
        with open("/proc/net/dev") as fh:
            lines = fh.readlines()[2:]
    except OSError:
        return out
    for line in lines:
        name, _, rest = line.partition(":")
        name = name.strip()
        if name == "lo":
            continue
        vals = rest.split()
        if len(vals) < 9:
            continue
        try:
            out[name] = (int(vals[0]), int(vals[8]))
        except ValueError:
            continue
    return out


def read_i915_pmudir():
    return "/sys/bus/event_source/devices/i915"


class IntelGpuBusy(object):
    """Whole-GPU busy percentage from the i915 perf PMU.

    Holds one perf_event fd per engine, opened once and read as deltas, so the
    per-refresh cost is four 8-byte reads rather than four ioctls. i915 only
    exposes CPU-wide (pid=-1, cpu=N) events for these counters, which is why
    this needs kernel.perf_event_paranoid=0; without it every open returns
    EACCES and `available` is False with `why` set.

    Engines: rcs0 (render) and bcs0 (copy) are the ones that carry real 3D and
    compositing work; vcs0/vecs0 are video decode/encode. The reported figure is
    the busiest single engine, because summing them can exceed 100% and would
    read as a broken number.
    """

    ENGINES = ("rcs0", "bcs0", "vcs0", "vecs0")

    def __init__(self):
        self._libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self._libc.syscall.restype = ctypes.c_long
        self.fds = {}
        self.prev = {}
        self.prev_at = None
        self.available = False
        self.why = "i915 PMU not opened"

    def _open(self):
        """Try to open every engine counter. All-or-nothing: a partial set
        would report a number that silently omits an engine."""
        try:
            with open(os.path.join(read_i915_pmudir(), "type")) as fh:
                pmu_type = int(fh.read().strip())
        except (OSError, ValueError) as exc:
            self.why = "i915 PMU absent (%s)" % exc
            return
        opened = {}
        for engine in self.ENGINES:
            path = os.path.join(read_i915_pmudir(), "events", "%s-busy" % engine)
            try:
                with open(path) as fh:
                    raw = fh.read().strip()
                config = int(dict(kv.split("=") for kv in raw.split(","))["config"], 16)
            except (OSError, ValueError, KeyError):
                continue
            fd, err = self._perf_event_open(pmu_type, config)
            if fd < 0:
                for f in opened.values():
                    os.close(f)
                self.why = "%s: perf_event_open %s" % (engine, os.strerror(err))
                return
            opened[engine] = fd
        if not opened:
            self.why = "no i915 engine counters"
            return
        # No ioctl: the fds are already counting (see _attr).
        self.fds = opened
        self.available = True
        self.why = ""

    def _attr(self, pmu_type, config):
        a = _PerfAttr()
        a.type = pmu_type
        a.size = ctypes.sizeof(a)
        a.config = config
        # exclude_kernel | exclude_hv -- and deliberately NOT the DISABLED bit.
        # Opening already-enabled is the only form the i915 PMU honours here:
        # with DISABLED set, every PERF_EVENT_IOC_ENABLE (0x2400) returns EINVAL
        # for every attr variant, and the counter then sits at a flat zero, which
        # the panel renders as a confident, wrong 0%. Verified: the same open
        # without DISABLED reports live engine time with no ioctl at all.
        a.flags = (1 << 5) | (1 << 6)
        return a

    def _perf_event_open(self, pmu_type, config):
        a = self._attr(pmu_type, config)
        ctypes.set_errno(0)
        fd = self._libc.syscall(
            ctypes.c_long(298),            # __NR_perf_event_open, x86_64
            ctypes.byref(a),
            ctypes.c_long(-1),              # pid  = all processes
            ctypes.c_long(0),               # cpu  = CPU 0
            ctypes.c_long(-1),              # group
            ctypes.c_long(0),
        )
        return fd, ctypes.get_errno()

    def _read(self, fd):
        val = ctypes.c_uint64(0)
        ctypes.set_errno(0)
        n = self._libc.read(ctypes.c_int(fd), ctypes.byref(val), ctypes.c_long(8))
        if n != 8:
            return None
        return val.value

    def sample(self):
        """Return (busy_pct, None) on the first successful interval, or
        (None, None) when the counters are not readable."""
        if not self.available:
            return None, None
        now = time.monotonic()
        current = {}
        for engine, fd in self.fds.items():
            v = self._read(fd)
            if v is None:
                continue
            current[engine] = v
        if not current:
            return None, None
        # First pass establishes the baseline; a percentage needs two samples.
        if self.prev_at is None or not current.keys() >= self.prev.keys():
            self.prev = current
            self.prev_at = now
            return None, None
        span = now - self.prev_at
        if span <= 0:
            return None, None
        worst = 0.0
        for engine, v in current.items():
            before = self.prev.get(engine)
            if before is None:
                continue
            delta = v - before
            if delta < 0:
                continue
            worst = max(worst, delta / (span * 1e9) * 100.0)
        self.prev = current
        self.prev_at = now
        return max(0.0, min(100.0, worst)), None

    def close(self):
        for fd in self.fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = {}
        self.available = False


_GPU_BUSY = None


def intel_gpu_busy():
    """Singleton wrapper: the fds must persist across refreshes to be useful."""
    global _GPU_BUSY
    if _GPU_BUSY is None:
        _GPU_BUSY = IntelGpuBusy()
        _GPU_BUSY._open()
        log("i915 PMU:", "available" if _GPU_BUSY.available else _GPU_BUSY.why)
    return _GPU_BUSY


def read_gpu(sensors=None):
    """(usage_pct, temp_c, note) for the GPU row.

    note is a short human-readable reason, non-empty only when nothing could
    be read -- the panel shows it so an empty row explains itself instead of
    looking broken.

    Probes, cheapest first: the amdgpu/i915/xe sysfs counter, then a GPU hwmon
    node for temperature, then nvidia-smi. The exec is last so a machine with a
    sysfs counter never pays for it. `sensors` is passed in because the caller
    has already read the hwmon tree for CPU temperature.
    """
    # 1. sysfs busy percent -- amdgpu and newer i915/xe drivers expose this
    try:
        cards = sorted(os.listdir("/sys/class/drm"))
    except OSError:
        cards = []
    for card in cards:
        if not card.startswith("card") or "-" in card:
            continue
        path = "/sys/class/drm/%s/device/gpu_busy_percent" % card
        try:
            with open(path) as fh:
                usage = float(fh.read().strip())
        except (OSError, ValueError):
            continue
        return usage, None, ""

    # 2. hwmon node whose driver name is a GPU one
    if sensors is None:
        sensors = read_sensors()
    temp = None
    for key, value in sensors.items():
        low = key.lower()
        if low.startswith(("amdgpu", "i915", "xe", "nvidia", "radeon")):
            temp = value
            break

    # 3b. Intel iGPU via the perf PMU. Reached only when kernel
    #     .perf_event_paranoid=0; otherwise every open fails and `why` says so.
    pmu = intel_gpu_busy()
    if pmu.available:
        usage, _ = pmu.sample()
        if usage is not None:
            return usage, temp, ""
        # Counters are open and counting, but a percentage needs two samples
        # separated by an interval. Say "measuring" rather than falling through
        # to the blocked-by-paranoid message, which would be plainly wrong now
        # that the fds are open.
        return None, temp, "measuring…"

    # 4. nvidia-smi -- the only subprocess in the app, and only for NVIDIA
    usage = None
    if any(low.startswith("nvidia") for low in sensors):
        try:
            proc = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, timeout=1.5, text=True,
            )
            first = proc.stdout.strip().splitlines()[0]
            u, t = (p.strip() for p in first.split(","))
            usage = float(u)
            temp = float(t)
        except (subprocess.SubprocessError, ValueError, IndexError, OSError) as exc:
            log("nvidia-smi failed:", exc)

    # Nothing readable. Say why, specifically. Prefer the PMU's own errno --
    # it is the precise cause -- and fall back to the driver-level hint.
    pmu = intel_gpu_busy()
    note = pmu.why if pmu.why else "no GPU sensor found"
    for card in cards:
        if not card.startswith("card") or "-" in card:
            continue
        dev = "/sys/class/drm/%s/device" % card
        # the driver is a symlink target under the PCI device
        driver = None
        try:
            driver = os.path.basename(os.path.realpath(os.path.join(dev, "driver")))
        except OSError:
            pass
        low = (driver or "").lower()
        if low in ("i915", "xe"):
            try:
                paranoid = int(open("/proc/sys/kernel/perf_event_paranoid").read())
            except (OSError, ValueError):
                paranoid = -1
            # NOTE: 0, not 1. The kernel's perf_paranoid_cpu() is simply
            # `paranoid > 0`, and it gates CPU-wide (pid=-1, cpu=N) events --
            # which is the only form i915 accepts. So level 1 still returns
            # EACCES for an unprivileged process; only 0 (or CAP_PERFMON) opens
            # the counter. Verified empirically at level 2; the level-1 case is
            # from the kernel logic, not measured.
            note = ("%s: needs perf_event_paranoid=0 (now %d)" % (driver, paranoid))
            break
        if low == "amdgpu":
            note = "amdgpu counter unreadable"
            break
    return usage, temp, note


def read_loadavg():
    try:
        with open("/proc/loadavg") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


# --------------------------------------------------------------------------
# the sampler: one cheap background read plus a richer on-demand read
# --------------------------------------------------------------------------
class Sampler(object):
    """Holds the last CPU reading so a percentage can be derived from deltas."""

    def __init__(self):
        self._prev = read_cpu_times()
        self._prev_at = GLib.get_monotonic_time()
        self.cpu_pct = 0.0
        self.mem_used, self.mem_total = read_mem()
        self.mem_pct = 0.0
        self._recompute()

    def _recompute(self):
        if self.mem_total:
            self.mem_pct = self.mem_used / self.mem_total * 100.0

    def tick_cheap(self):
        """Background read: /proc/stat + /proc/meminfo only."""
        self.sample_cpu()

    def sample_cpu(self):
        """Usage from the /proc/stat delta since the previous call.

        The first call after start has no meaningful interval (the constructor
        read happened microseconds earlier), so the delta is skipped and the
        previous value is kept rather than flashing a bogus spike.
        """
        try:
            now_busy, now_total = read_cpu_times()
        except (OSError, ValueError, IndexError):
            return self.cpu_pct
        prev_busy, prev_total = self._prev
        self._prev = (now_busy, now_total)
        d_total = now_total - prev_total
        d_busy = now_busy - prev_busy
        if d_total > 0 and d_busy >= 0:
            self.cpu_pct = max(0.0, min(100.0, 100.0 * d_busy / d_total))
        self._recompute()
        return self.cpu_pct

    def sample_mem(self):
        try:
            self.mem_used, self.mem_total = read_mem()
        except (OSError, ValueError, IndexError):
            pass
        self._recompute()
        return self.mem_pct

    def icon_levels(self):
        """Ten steps per metric; the icon name is quantised so the shell only
        re-reads the pixmap when a bar visibly moves."""
        cpu = max(0, min(9, int(round(self.cpu_pct / 10.0))))
        mem = max(0, min(9, int(round(self.mem_pct / 10.0))))
        return cpu, mem


# --------------------------------------------------------------------------
# icon: two bars, quantised to 10 steps each
# --------------------------------------------------------------------------
def icon_name(cpu_step, mem_step):
    return "%s-c%d-r%d-symbolic" % (APP_ID, cpu_step, mem_step)


def _icon_svg(cpu_frac, mem_frac):
    """Two horizontal bars on a shared 22x22 grid.

    Kept *-symbolic so the host tints it to the bar foreground and it stays
    visible under any theme. That means colour cannot carry the value, only
    geometry and alpha: the filled part is opaque, the track behind it is a
    faint outline. Both bars share the same track so the empty state still
    reads as a meter rather than a missing glyph.
    """
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" '
             'viewBox="0 0 22 22">']
    for i, frac in enumerate((cpu_frac, mem_frac)):
        top = 7.5 + i * 6.0
        parts.append(
            '<rect x="3" y="%.1f" width="16" height="3.2" rx="1.6" '
            'fill="none" stroke="#ffffff" stroke-opacity="0.28" '
            'stroke-width="1"/>' % top
        )
        filled = max(0.0, min(1.0, frac)) * 16.0
        if filled > 0.6:
            # A rounded cap needs the width clamped, otherwise a tiny value
            # renders as a lozenge wider than it is tall.
            parts.append(
                '<rect x="3" y="%.1f" width="%.2f" height="3.2" rx="1.6" '
                'fill="#ffffff"/>' % (top, max(1.6, filled))
            )
        else:
            # sub-1-step value: a single tick at the origin so it reads as
            # "almost nothing" instead of "no data"
            parts.append('<circle cx="3.8" cy="%.1f" r="1.1" fill="#ffffff"/>'
                         % (top + 1.6))
    parts.append("</svg>\n")
    return "".join(parts)


def paint_icons(sampler):
    """Write the 100 icon files once, then just the two in use.

    A symbolic IconName is cached by the host per name, so rewriting a file in
    place would not repaint; the name carries the value instead.
    """
    theme_dir = os.path.dirname(ICON_DIR)
    try:
        os.makedirs(ICON_DIR, exist_ok=True)
        index = os.path.join(theme_dir, "index.theme")
        if not os.path.exists(index):
            import shutil
            try:
                shutil.copyfile("/usr/share/icons/hicolor/index.theme", index)
            except OSError as exc:
                log("index.theme copy failed:", exc)
    except OSError as exc:
        log("icon dir failed:", exc)
        return

    cpu_step, mem_step = sampler.icon_levels()
    wanted = set()
    for cpu in range(10):
        for mem in range(10):
            name = icon_name(cpu, mem)
            wanted.add(name + ".svg")
            path = os.path.join(ICON_DIR, name + ".svg")
            if not os.path.exists(path):
                try:
                    with open(path, "w") as fh:
                        fh.write(_icon_svg(cpu / 10.0, mem / 10.0))
                except OSError as exc:
                    log("icon write failed:", exc)
    try:
        subprocess.run(["gtk-update-icon-cache", "-f", "-t", theme_dir],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log("icon cache failed:", exc)
    return icon_name(cpu_step, mem_step)


# --------------------------------------------------------------------------
# panel widgets
# --------------------------------------------------------------------------
class Bar(Gtk.DrawingArea):
    """A thin usage bar. Drawn rather than assembled from widgets so the track,
    the fill and the text all sit on one baseline."""

    def __init__(self):
        super().__init__()
        self.set_size_request(-1, 30)
        self.pct = 0.0
        self.text_left = ""
        self.text_right = ""
        self.sub = ""
        self.color = ACCENT
        self.unknown = False

    def set_state(self, pct, left, right, sub="", color=ACCENT, unknown=False):
        """`unknown` draws the track with no fill at all, so a value that is
        genuinely absent (an unreadable sensor, a counter still measuring its
        first interval) never looks like a confident 0%."""
        self.pct = max(0.0, min(100.0, pct or 0.0))
        self.text_left = left
        self.text_right = right
        self.sub = sub
        self.color = color
        self.unknown = unknown
        self.queue_draw()

    def do_draw(self, cr):
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        if w <= 0:
            return
        # text line on top
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(10.5)
        cr.set_source_rgb(*hex_rgb(FG))
        cr.move_to(0, 11)
        cr.show_text(self.text_left)
        ext = cr.text_extents(self.text_right)
        cr.set_source_rgb(*hex_rgb(DIM))
        cr.move_to(w - ext.width, 11)
        cr.show_text(self.text_right)

        if self.sub:
            cr.set_font_size(9.5)
            cr.set_source_rgb(*hex_rgb(DIM))
            cr.move_to(0, 23)
            cr.show_text(self.sub)

        bar_y = 22.0 if self.sub else 18.0
        bar_h = 4.0
        radius = bar_h / 2.0
        cr.set_source_rgb(*hex_rgb(TRACK))
        cr.rectangle(0, bar_y, w, bar_h)
        cr.fill()
        filled = (w - 2) * (self.pct / 100.0)
        if filled > 0.5 and not self.unknown:
            cr.set_source_rgb(*hex_rgb(self.color))
            cr.rectangle(1, bar_y + 1, max(1.5, filled), bar_h - 2)
            cr.fill()


def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


class Panel(Gtk.Window):
    """The click-opened panel. Own popup so it can be skinned and, unlike the
    shell-drawn tray menu, laid out tightly."""

    def __init__(self, app):
        # TOPLEVEL + NORMAL hint: a Gtk.WindowType.POPUP never maps reliably
        # under Hyprland/XWayland, and POPUP_MENU/DOCK ignore move().
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.app = app
        self.sampler = app.sampler
        self._tick_id = 0
        self._prev_net = None
        self._prev_net_at = 0
        self._armed = False

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.set_keep_above(True)
        self.set_accept_focus(True)

        # The card is styled on this inner box, NOT on the Gtk.Window. With
        # set_app_paintable(True) the toplevel has an RGBA visual and GTK stops
        # painting the window background itself, so a background-color rule on
        # .smpanel on the window renders nothing at all -- the panel comes out
        # as floating text and bars over whatever is behind it. A child box
        # always paints its own background, so the card lives here.
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        root.set_border_width(11)
        root.get_style_context().add_class("smpanel")
        self.add(root)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        root.pack_start(head, False, False, 0)

        title = Gtk.Label(label="System")
        title.set_xalign(0)
        title.get_style_context().add_class("smlab-strong")
        head.pack_start(title, True, True, 0)

        self.uptime = Gtk.Label(label="")
        self.uptime.set_xalign(1)
        self.uptime.get_style_context().add_class("smlab-dim")
        head.pack_end(self.uptime, False, False, 0)

        self.bars = {}
        for key in ("cpu", "ram", "gpu", "net", "disk"):
            if key == "disk":
                self._disk_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                         spacing=4)
                root.pack_start(self._disk_box, False, False, 0)
                self._disk_bars = []
                for _ in range(2):
                    bar = Bar()
                    self._disk_box.pack_start(bar, False, False, 0)
                    self._disk_bars.append(bar)
                continue
            bar = Bar()
            root.pack_start(bar, False, False, 0)
            self.bars[key] = bar

        sep = Gtk.Box()
        sep.set_size_request(-1, 1)
        sep.get_style_context().add_class("smsep")
        root.pack_start(sep, False, False, 1)

        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        root.pack_start(foot, False, False, 0)
        self.foot = Gtk.Label(label="click outside or press Esc to close")
        self.foot.set_xalign(0)
        self.foot.get_style_context().add_class("smlab-dim")
        foot.pack_start(self.foot, True, True, 0)

        quit_btn = Gtk.Button(label="Quit")
        quit_btn.get_style_context().add_class("smghost")
        quit_btn.connect("clicked", lambda *_a: self.app.quit())
        foot.pack_end(quit_btn, False, False, 0)

        self.connect("focus-out-event", self.on_focus_out)
        self.connect("map", lambda *_a: self._place())
        self._apply_css()

    def _apply_css(self):
        css = (
            # .smpanel is the inner box, which does paint its own background --
            # see the note where the box is built.
            ".smpanel {{ background-color: {bg}; background-image: none;"
            " border: 1px solid {line}; border-radius: 10px; }}"
            ".smlab-strong {{ color: {fg}; font-size: 12px; font-weight: 700; }}"
            ".smlab-dim {{ color: {dim}; font-size: 10px; }}"
            ".smsep {{ background-color: {line}; min-height: 1px; }}"
            ".smghost {{ background-image: none; background-color: transparent;"
            " color: {dim}; border: none; padding: 2px 7px;"
            " font-size: 10px; border-radius: 5px; }}"
            ".smghost:hover {{ color: {accent}; background-color: {line}; }}"
        ).format(fg=FG, dim=DIM, line=LINE, accent=ACCENT, bg=BG)
        prov = Gtk.CssProvider()
        prov.load_from_data(css.encode())
        # Screen level: a provider on the window's own style context does not
        # reach child widgets, so they would keep the desktop theme styling.
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # --- positioning ---
    def _place(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        self.get_preferred_size()
        width, height = self.get_size()
        margin = 10
        self.move(geo.x + geo.width - width - margin, geo.y + BAR_HEIGHT)

    # --- data ---
    def refresh(self):
        s = self.sampler
        s.sample_cpu()
        s.sample_mem()

        # CPU
        load = read_loadavg()
        sensors = read_sensors()
        cpu_temp = None
        for key, value in sensors.items():
            if key.lower().startswith(("coretemp", "k10temp", "zenpower")):
                cpu_temp = value
                break
        cpu_core = os.cpu_count() or 1
        sub = "%d cores · load %.2f" % (cpu_core, load if load else 0.0)
        if cpu_temp is not None:
            sub += " · %d°C" % round(cpu_temp)
        self.bars["cpu"].set_state(
            s.cpu_pct, "CPU", "%d%%" % round(s.cpu_pct), sub,
            WARN if s.cpu_pct >= 85 else ACCENT,
        )

        # RAM
        self.bars["ram"].set_state(
            s.mem_pct, "RAM",
            "%s / %s" % (human_bytes(s.mem_used), human_bytes(s.mem_total)),
            "%d%%" % round(s.mem_pct),
            WARN if s.mem_pct >= 85 else ACCENT,
        )

        # GPU -- only present if the hardware says something
        usage, temp, note = read_gpu(sensors)
        gpu_bar = self.bars["gpu"]
        if usage is None and temp is None:
            # Nothing readable at all: empty bar, and the reason beside it.
            gpu_bar.set_state(0, "GPU", "not available", note, unknown=True)
        else:
            temp_txt = ("%d°C" % round(temp)) if temp is not None else ""
            if usage is None:
                # Counters open but still on their first interval.
                gpu_bar.set_state(0, "GPU", note or "measuring", temp_txt,
                                  unknown=True)
            else:
                gpu_bar.set_state(
                    usage, "GPU", "%d%%" % round(usage), temp_txt,
                    WARN if usage >= 90 else ACCENT)

        # Network -- throughput needs an interval, so the rate line only exists
        # from the second refresh after the panel opened. The first one shows
        # totals alone rather than a fake 0 B/s.
        now = GLib.get_monotonic_time()
        net = read_net()
        rx = sum(v[0] for v in net.values())
        tx = sum(v[1] for v in net.values())
        totals = "total ↓ %s  ↑ %s" % (human_bytes(rx), human_bytes(tx))
        if self._prev_net is not None and self._prev_net_at:
            span = (now - self._prev_net_at) / 1e6
            if span > 0:
                d_rx = max(0, rx - self._prev_net[0]) / span
                d_tx = max(0, tx - self._prev_net[1]) / span
                # The bar is a rough "how busy is the link" hint, saturating at
                # 20 MB/s combined; it is not a scale the user reads numbers off.
                self.bars["net"].set_state(
                    min(100.0, (d_rx + d_tx) / (20.0 * 1000.0 * 1000.0) * 100.0),
                    "NET", "↓ %s  ↑ %s" % (human_rate(d_rx), human_rate(d_tx)),
                    totals, ACCENT,
                )
            else:
                self.bars["net"].set_state(0, "NET", "↓  ↑", totals)
        else:
            self.bars["net"].set_state(0, "NET", "↓  ↑", totals)
        self._prev_net = (rx, tx)
        self._prev_net_at = now

        # Disk -- the two biggest real filesystems
        disks = read_disks()
        for i, bar in enumerate(self._disk_bars):
            if i < len(disks):
                d = disks[i]
                bar.show()
                bar.set_state(
                    d["pct"], d["mount"],
                    "%s / %s" % (human_bytes(d["used"]), human_bytes(d["size"])),
                    "%d%% used" % round(d["pct"]),
                    WARN if d["pct"] >= 85 else ACCENT,
                )
            else:
                bar.hide()

        try:
            with open("/proc/uptime") as fh:
                seconds = int(float(fh.read().split()[0]))
            self.uptime.set_text("up %dd %dh" % (seconds // 86400,
                                                (seconds % 86400) // 3600))
        except (OSError, ValueError, IndexError):
            self.uptime.set_text("")

    # --- visibility ---
    def open_now(self):
        self._prev_net = None
        self._prev_net_at = 0
        self.refresh()
        self.show_all()
        for bar in self._disk_bars:
            bar.queue_draw()
        self._place()
        self.present_with_time(Gdk.CURRENT_TIME)
        # move() during map is ignored by the compositor; re-anchor once mapped
        GLib.timeout_add(150, self._place_once)
        if self._tick_id:
            GLib.source_remove(self._tick_id)
        self._tick_id = GLib.timeout_add(REFRESH_INTERVAL_MS, self._tick)
        # Dismiss-on-focus-out must stay disarmed until the window has actually
        # settled. XWayland delivers a focus-out during the map itself when the
        # WM has not yet granted focus, and honouring that closes the panel
        # instantly on every open.
        self._armed = False
        GLib.timeout_add(350, self._arm_dismiss)
        log("panel open: visible=%s tick=%s" % (self.get_visible(), self._tick_id))

    def _arm_dismiss(self):
        self._armed = True
        log("panel dismiss armed (has_focus=%s)"
            % (self.has_toplevel_focus() if hasattr(self, "has_toplevel_focus")
               else self.is_active()))
        return False

    def describe(self):
        """Log every row's geometry and values.

        Pixel-inspecting a screenshot cannot tell you which row is missing --
        a bar that drew 2px tall and one that never drew look identical in a
        colour histogram. Asking the widgets what they hold is exact. Used by
        the verification pass, cheap enough to leave in.
        """
        lines = []
        for key in ("cpu", "ram", "gpu", "net"):
            bar = self.bars.get(key)
            if bar is None:
                continue
            a = bar.get_allocation()
            lines.append("%-4s %3d%%  L=%-22r R=%-30r sub=%-28r alloc=%dx%d"
                         % (key, round(bar.pct), bar.text_left,
                            bar.text_right, bar.sub, a.width, a.height))
        for i, bar in enumerate(self._disk_bars):
            a = bar.get_allocation()
            lines.append("disk%d %3d%%  L=%-22r R=%-30r sub=%-28r alloc=%dx%d "
                         "visible=%s"
                         % (i, round(bar.pct), bar.text_left, bar.text_right,
                            bar.sub, a.width, a.height, bar.get_visible()))
        self.get_preferred_size()
        log("panel state (w=%d h=%d):\n  %s"
            % (self.get_size()[0], self.get_size()[1], "\n  ".join(lines)))

    def _place_once(self):
        self._place()
        log("panel placed: size=%s pos=%s" % (self.get_size(), self.get_position()))
        if os.environ.get("SYSMON_DESCRIBE"):
            self.describe()
        self._snapshot()
        return False

    def _snapshot(self):
        """Save the panel's own pixels to SYSMON_SNAP.

        A whole-screen screenshot cannot settle whether this panel rendered
        correctly: it sits over a busy terminal, and cropping by eye lands on
        the editor more often than not. Reading the window's own surface is
        unambiguous. Off unless SYSMON_SNAP is set.
        """
        target = os.environ.get("SYSMON_SNAP", "")
        if not target:
            return
        try:
            win = self.get_window()
            if win is None:
                log("snapshot: no Gdk window")
                return
            width, height = self.get_size()
            pixbuf = Gdk.pixbuf_get_from_window(win, 0, 0, width, height)
            if pixbuf is None:
                log("snapshot: pixbuf_get_from_window returned None")
                return
            pixbuf.savev(target, "png", [], [])
            log("snapshot saved %s (%dx%d)" % (target, width, height))
        except GLib.Error as exc:
            log("snapshot failed:", exc.message)

    def _tick(self):
        """Panel-only refresh. This timer exists exactly as long as the panel
        is visible -- it is the 'read the rest only when asked' half."""
        try:
            self.refresh()
        except Exception:
            log("panel refresh raised:", traceback.format_exc())
        return True

    def close(self):
        log("panel close requested (visible=%s)" % self.get_visible())
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        self.hide()

    def on_focus_out(self, *_a):
        # A click elsewhere takes focus away: dismiss, like a real popup. The
        # _armed gate drops the spurious focus-out XWayland sends during map.
        if not getattr(self, "_armed", False):
            log("focus-out ignored (not armed yet)")
            return False
        log("focus-out -> dismiss")
        GLib.idle_add(self.close)
        return False


# --------------------------------------------------------------------------
# StatusNotifierItem
# --------------------------------------------------------------------------
SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <!-- ToolTip is declared as a plain string, not the spec's
         (sa(iiay)ss) struct: Quickshell's demarshaller rejects the struct form,
         and a mismatch here is what makes hover tooltips dead. Signatures must
         match what the shell actually calls: Activate(x,y) is two ints, NOT
         the optional third timestamp arg. -->
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="ToolTip" type="s" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="h" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewTitle"/>
  </interface>
</node>
"""


class TrayIcon(object):
    def __init__(self, app):
        self.app = app
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        self.conn.register_object(
            "/StatusNotifierItem", node.interfaces[0],
            self._method_call, self._get_property, None)
        log("SNI exported")

    def _get_property(self, _c, _s, _p, _i, name):
        s = self.app.sampler
        cpu_step, mem_step = s.icon_levels()
        # Quickshell's tooltip demarshaller rejects the spec struct form, so
        # ToolTip is a plain string here; that is what the shell actually reads.
        tip = "CPU %d%%   RAM %d%%   RAM %s" % (
            round(s.cpu_pct), round(s.mem_pct),
            "%s/%s" % (human_bytes(s.mem_used), human_bytes(s.mem_total)))
        values = {
            "Category": GLib.Variant("s", "SystemServices"),
            "Id": GLib.Variant("s", TRAY_ID),
            "Title": GLib.Variant("s", "System Monitor"),
            "Status": GLib.Variant("s", "Active"),
            # MUST be false: with true the shell renders a dbusmenu and never
            # sends Activate, so the click is dropped.
            "ItemIsMenu": GLib.Variant("b", False),
            "IconName": GLib.Variant("s", icon_name(cpu_step, mem_step)),
            "ToolTip": GLib.Variant("s", tip),
        }
        return values.get(name)

    def _method_call(self, _c, _s, _p, _i, method, params, invocation):
        log("SNI method:", method)
        try:
            if method in ("Activate", "SecondaryActivate", "ContextMenu"):
                self.app.on_tray_click()
            elif method == "Scroll":
                self.app.on_tray_click()
            else:
                invocation.return_dbus_error(
                    "org.freedesktop.DBus.Error.UnknownMethod", method)
                return
            invocation.return_value(None)
        except Exception:
            log("SNI handler raised:", traceback.format_exc())
            invocation.return_value(None)

    WATCHER_NAME = "org.kde.StatusNotifierWatcher"
    WATCHER_PATH = "/StatusNotifierWatcher"

    def register_with_watcher(self, bus_name):
        self.bus_name = bus_name
        self.entry = "%s/StatusNotifierItem" % bus_name
        GLib.timeout_add_seconds(2, self.ensure_registered)
        GLib.timeout_add_seconds(15, self.ensure_registered)

    def _watcher_call(self, method, params):
        return self.conn.call_sync(
            self.WATCHER_NAME, self.WATCHER_PATH,
            "org.freedesktop.DBus.Properties", method, params,
            None, Gio.DBusCallFlags.NONE, 4000, None)

    def ensure_registered(self):
        try:
            try:
                reply = self._watcher_call(
                    "Get", GLib.Variant("(ss)", (self.WATCHER_NAME,
                                                "RegisteredStatusNotifierItems")))
                if self.entry in [str(v) for v in reply.unpack()[0]]:
                    return True
            except GLib.Error:
                pass
            self.conn.call_sync(
                self.WATCHER_NAME, self.WATCHER_PATH,
                "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self.bus_name,)), None,
                Gio.DBusCallFlags.NONE, 4000, None)
            log("registered with watcher as", self.bus_name)
        except GLib.Error as exc:
            log("watcher registration failed:", exc.message)
        return True

    def refresh_icon(self):
        """Push a PropertiesChanged for IconName. The shell caches the pixmap
        per name, so only a genuine step change is worth an emit."""
        cpu_step, mem_step = self.app.sampler.icon_levels()
        name = icon_name(cpu_step, mem_step)
        if name == getattr(self, "_last_icon", None):
            return
        self._last_icon = name
        try:
            self.conn.emit_signal(
                None, "/StatusNotifierItem", "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (
                    "org.kde.StatusNotifierItem",
                    {"IconName": GLib.Variant("s", name)},
                    [],
                )),
            )
            log("icon ->", name)
        except GLib.Error as exc:
            log("icon refresh failed:", exc.message)


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------
class App(Gtk.Application):
    def __init__(self):
        # IS_SERVICE is required, not cosmetic. A GApplication with no service
        # flag and no command-line actions exits from run() immediately without
        # ever registering on the bus, so do_activate never fires and the app
        # dies silently with status 0 -- which is exactly what systemd showed.
        super().__init__(application_id=BUS_NAME,
                         flags=Gio.ApplicationFlags.IS_SERVICE)

    def do_activate(self):
        self.sampler = Sampler()
        self.panel = Panel(self)
        self.tray = TrayIcon(self)
        self.tray.register_with_watcher(BUS_NAME)

        name = paint_icons(self.sampler)
        self.tray._last_icon = name
        log("start icon", name)

        # The only always-on work: two tiny proc reads. Nothing else runs
        # until the panel is opened.
        GLib.timeout_add(BACKGROUND_INTERVAL_MS, self._idle_tick)

        # Escape while the panel has focus.
        self.accels = Gtk.AccelGroup()
        self.panel.add_accel_group(self.accels)
        key, mod = Gtk.accelerator_parse("Escape")
        self.accels.connect(key, mod, Gtk.AccelFlags.VISIBLE,
                            lambda *_a: self.on_escape())

        self.hold()  # tray-only app: no window keeps the mainloop alive

    def _idle_tick(self):
        try:
            self.sampler.tick_cheap()
            self.tray.refresh_icon()
        except Exception:
            log("idle tick raised:", traceback.format_exc())
        return True

    def on_tray_click(self):
        if self.panel.get_visible():
            self.panel.close()
        else:
            self.panel.open_now()

    def on_escape(self):
        self.panel.close()
        return True


def main():
    app = App()
    # Gtk.Application's default shutdown handler calls exit(); replacing it is
    # what lets the mainloop unwind so systemd's SIGTERM and the Quit button
    # both take the same path.
    app.connect("shutdown", lambda *_a: None)
    # A GApplication never activates itself: run() only starts the mainloop and
    # waits for something to activate it (a second instance, or D-Bus). For a
    # tray app started by systemd there is no second instance, so without this
    # explicit activate do_activate never runs and nothing is ever built. The
    # hold() inside do_activate then keeps the loop alive with no windows.
    app.register(None)
    app.activate()
    # GLibUnix.signal_add, not GLib.unix_signal_add: the latter is deprecated
    # and emits a PyGIDeprecationWarning on every start.
    def on_signal(*_a):
        # Release the perf_event fds before the loop unwinds; leaking them
        # would keep the counters pinned to this dying process.
        if _GPU_BUSY is not None:
            _GPU_BUSY.close()
        app.quit()
        return GLib.SOURCE_REMOVE

    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, on_signal)
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, on_signal)
    app.run([])


if __name__ == "__main__":
    main()
