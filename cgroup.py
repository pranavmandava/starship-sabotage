"""Single-file cgroup v2 isolation library for Starship Sabotage.

The game server is the trusted component and stays OUTSIDE the player cgroup
subtree.  Each player process is placed in its own leaf cgroup under a shared
parent:

    /sys/fs/cgroup/starship-cgroup/              <- parent (no processes)
    /sys/fs/cgroup/starship-cgroup/player-alice  <- one process tree per player
    /sys/fs/cgroup/starship-cgroup/player-bob

Limits applied to every leaf:
    memory.max       hard RSS+page-cache cap; exceeding it OOM-kills the leaf
    memory.swap.max  0, so the cap cannot be dodged by swapping
    memory.oom.group 1, so the whole player cgroup dies together
    pids.max         stops fork bombs
    cpu.max          CFS bandwidth quota, stops CPU hogs

The parent also gets an aggregate cap, so N misbehaving players cannot exhaust
the host even if each stays under its own limit.

Only the kernel's cgroup v2 filesystem is used: no external packages, no
systemd dependency.  Writing cgroup files requires root (or a delegated
subtree), so the CLI half of this file is meant to be run with sudo.

Library API
-----------
    detect() -> dict                     inspect the local cgroup v2 mount
    parse_size("64M") -> int             bytes
    parse_cpu("50") -> "50000 100000"    percent -> cpu.max
    Cgroup(name, parent)                 one cgroup directory
        .create() .set_limits(...) .add_process(pid)
        .stats() .kill() .remove()
    ensure_parent(name, ...) -> Cgroup  create + enable controllers
    spawn(cmd, cg, user=None)            fork/exec a process into a leaf
    cleanup(parent, keep_parent=False)   kill/remove every leaf

CLI
---
    sudo python3 cgroup.py detect
    sudo python3 cgroup.py init --parent starship-cgroup \
        --players 5 --memory 64M --pids 24 --cpu 50
    sudo python3 cgroup.py show   --parent starship-cgroup
    sudo python3 cgroup.py cleanup --parent starship-cgroup
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import signal
import subprocess
import sys
import time

CGROUP_ROOT = "/sys/fs/cgroup"
DEFAULT_PARENT = "starship-cgroup"
CONTROLLERS = ("cpu", "memory", "pids")


class CgroupError(RuntimeError):
    """A cgroup operation failed or the host cannot support it."""


# ---------------------------------------------------------------------------
# Host detection / parsing helpers
# ---------------------------------------------------------------------------

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _write(path: str, value) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(value))


def detect() -> dict:
    """Return what this host supports.  Never raises."""
    info = {
        "cgroup_root": CGROUP_ROOT,
        "cgroup2": False,
        "root_controllers": [],
        "euid": os.geteuid(),
        "is_root": os.geteuid() == 0,
        "writable": False,
    }
    controllers = os.path.join(CGROUP_ROOT, "cgroup.controllers")
    if not os.path.exists(controllers):
        return info
    info["cgroup2"] = True
    try:
        info["root_controllers"] = _read(controllers).split()
    except OSError:
        pass
    info["writable"] = os.access(CGROUP_ROOT, os.W_OK)
    return info


def require_cgroup2() -> dict:
    info = detect()
    if not info["cgroup2"]:
        raise CgroupError(
            "cgroup v2 not found at %s (need cgroup2fs)" % CGROUP_ROOT
        )
    return info


def require_root() -> None:
    if os.geteuid() != 0:
        raise CgroupError(
            "writing cgroups requires root; run with sudo "
            "(player processes still drop to their own UIDs)"
        )


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?\s*$", re.I)
_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def parse_size(value) -> int:
    """'64M', '1G', '512K', or an int -> bytes."""
    if isinstance(value, int):
        return value
    m = _SIZE_RE.match(str(value))
    if not m:
        raise ValueError("bad size: %r (use e.g. 64M, 1G, or bytes)" % value)
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).upper()])


def parse_cpu(value) -> str:
    """'50', '50%', '0.5' -> a cgroup v2 cpu.max value ('50000 100000').

    A value already in cpu.max form ('50000 100000') is passed through, and
    the special value 'max' removes the quota.
    """
    s = str(value).strip()
    if re.match(r"^(max|\d+)\s+\d+$", s):
        return s
    if s.lower() == "max":
        return "max 100000"
    if s.endswith("%"):
        percent = float(s[:-1])
    else:
        percent = float(s) * 100.0 if float(s) <= 1.0 else float(s)
    if percent <= 0:
        raise ValueError("cpu quota must be positive")
    return "%d 100000" % int(percent * 1000)


def _cpu_quota(value) -> int:
    """Return just the quota part of a cpu.max string."""
    return int(str(value).split()[0])


def _cpu_multiply(value: str, factor: int) -> str:
    parts = str(value).split()
    return "%d %s" % (_cpu_quota(parts[0]) * factor, parts[1])


def _sanitize(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", str(name))
    if not name or name in (".", ".."):
        raise CgroupError("bad cgroup name: %r" % name)
    return name


# ---------------------------------------------------------------------------
# Cgroup object
# ---------------------------------------------------------------------------

class Cgroup:
    """One cgroup directory (leaf or parent)."""

    def __init__(self, name: str, parent: str = DEFAULT_PARENT):
        self.name = _sanitize(name)
        self.parent = _sanitize(parent) if parent else ""
        self.path = os.path.join(CGROUP_ROOT, self.parent, self.name) \
            if self.parent else os.path.join(CGROUP_ROOT, self.name)

    # -- lifecycle ---------------------------------------------------------
    def exists(self) -> bool:
        return os.path.isdir(self.path)

    def create(self) -> "Cgroup":
        os.makedirs(self.path, exist_ok=True)
        return self

    def remove(self, timeout: float = 3.0) -> None:
        """Remove this cgroup, killing leftover processes first."""
        if not self.exists():
            return
        pids = self.pids()
        if pids:
            self.kill()
            deadline = time.time() + timeout
            while time.time() < deadline and self.pids():
                time.sleep(0.05)
        deadline = time.time() + timeout
        while True:
            try:
                os.rmdir(self.path)
                return
            except FileNotFoundError:
                return
            except OSError:
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)

    # -- attributes --------------------------------------------------------
    def _w(self, attr: str, value) -> None:
        """Write an attribute if the controller exposes it."""
        target = os.path.join(self.path, attr)
        if not os.path.exists(target):
            raise CgroupError(
                "%s missing: controller not available/enabled for %s"
                % (attr, self.path)
            )
        _write(target, value)

    def _r(self, attr: str, default=None):
        try:
            return _read(os.path.join(self.path, attr))
        except (FileNotFoundError, ProcessLookupError):
            return default

    def set_limits(self, memory=None, swap=None, pids=None, cpu=None,
                   oom_group: int | None = 1) -> None:
        if memory is not None:
            self._w("memory.max", parse_size(memory))
        if swap is not None:
            # memory.swap.max may be absent on kernels built without swap
            # accounting; the memory cap is still enforced.
            if os.path.exists(os.path.join(self.path, "memory.swap.max")):
                self._w("memory.swap.max", parse_size(swap))
        if oom_group is not None and os.path.exists(
                os.path.join(self.path, "memory.oom.group")):
            self._w("memory.oom.group", int(oom_group))
        if pids is not None:
            self._w("pids.max", int(pids))
        if cpu is not None:
            self._w("cpu.max", parse_cpu(cpu))

    def add_process(self, pid: int) -> None:
        self._w("cgroup.procs", int(pid))

    def pids(self) -> list[int]:
        raw = self._r("cgroup.procs", "") or ""
        return [int(x) for x in raw.split() if x.isdigit()]

    def kill(self) -> None:
        """Kill every process in this cgroup (kernel cgroup.kill)."""
        target = os.path.join(self.path, "cgroup.kill")
        if os.path.exists(target):
            try:
                _write(target, 1)
                return
            except OSError:
                pass
        for pid in self.pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # -- observability -----------------------------------------------------
    def stats(self) -> dict:
        def as_int(attr, default=0):
            val = self._r(attr)
            try:
                return int(val)
            except (TypeError, ValueError):
                return default

        events = {}
        for line in (self._r("memory.events", "") or "").splitlines():
            parts = line.split()
            if len(parts) == 2:
                events[parts[0]] = int(parts[1])
        cpu = {}
        for line in (self._r("cpu.stat", "") or "").splitlines():
            parts = line.split()
            if len(parts) == 2:
                cpu[parts[0]] = int(parts[1])

        return {
            "name": self.name,
            "path": self.path,
            "memory_current": as_int("memory.current"),
            "memory_peak": as_int("memory.peak"),
            "memory_max": self._r("memory.max"),
            "memory_swap_current": as_int("memory.swap.current"),
            "pids_current": as_int("pids.current"),
            "pids_peak": as_int("pids.peak"),
            "pids_max": self._r("pids.max"),
            "cpu_usage_usec": cpu.get("usage_usec", 0),
            "cpu_nr_throttled": cpu.get("nr_throttled", 0),
            "cpu_throttled_usec": cpu.get("throttled_usec", 0),
            "oom": events.get("oom", 0),
            "oom_kill": events.get("oom_kill", 0),
            "oom_group_kill": events.get("oom_group_kill", 0),
        }


# ---------------------------------------------------------------------------
# Parent setup / cleanup
# ---------------------------------------------------------------------------

def ensure_parent(name: str = DEFAULT_PARENT, controllers=CONTROLLERS,
                  memory=None, swap=None, pids=None, cpu=None,
                  oom_group: int | None = 1) -> Cgroup:
    """Create the parent cgroup and enable controllers in its subtree."""
    require_root()
    require_cgroup2()
    parent = Cgroup(name, parent="").create()

    # A cgroup with processes cannot enable controllers for its children
    # (the "no internal processes" rule).  The parent is only a container.
    if parent.pids():
        raise CgroupError(
            "parent %s contains processes; move them out before enabling "
            "controllers" % parent.path
        )

    control = os.path.join(parent.path, "cgroup.subtree_control")
    current = set(_read(control).split()) if os.path.exists(control) else set()
    want = [c for c in controllers if c not in current
            and os.path.exists(os.path.join(CGROUP_ROOT, "cgroup.controllers"))
            and c in _read(os.path.join(CGROUP_ROOT, "cgroup.controllers")).split()]
    if want:
        _write(control, " ".join("+" + c for c in want))

    parent.set_limits(memory=memory, swap=swap, pids=pids, cpu=cpu,
                      oom_group=oom_group)
    return parent


def cleanup(name: str = DEFAULT_PARENT, keep_parent: bool = False,
            timeout: float = 3.0) -> list[str]:
    """Kill and remove every leaf cgroup under *name*."""
    require_root()
    parent = Cgroup(name, parent="")
    if not parent.exists():
        return []
    removed = []
    for entry in os.listdir(parent.path):
        path = os.path.join(parent.path, entry)
        if not os.path.isdir(path) or entry.startswith("."):
            continue
        leaf = Cgroup(entry, parent=name)
        try:
            leaf.remove(timeout=timeout)
            removed.append(entry)
        except OSError as exc:
            print("[cgroup] could not remove %s: %s" % (path, exc),
                  file=sys.stderr)
    if not keep_parent:
        try:
            parent.remove(timeout=timeout)
        except OSError as exc:
            print("[cgroup] could not remove parent %s: %s" % (parent.path, exc),
                  file=sys.stderr)
    return removed


def show(name: str = DEFAULT_PARENT) -> list[dict]:
    parent = Cgroup(name, parent="")
    if not parent.exists():
        return []
    out = []
    for entry in sorted(os.listdir(parent.path)):
        path = os.path.join(parent.path, entry)
        if os.path.isdir(path):
            out.append(Cgroup(entry, parent=name).stats())
    return out


# ---------------------------------------------------------------------------
# Spawning a process inside a leaf
# ---------------------------------------------------------------------------

def spawn(cmd: list[str], cg: Cgroup, user: str | None = None,
          cwd: str | None = None, env: dict | None = None, **popen_kwargs):
    """fork/exec *cmd* inside *cg*, optionally dropping to *user*.

    The cgroup move happens in the child before exec, so no player code runs
    outside the cgroup.  Privileges are dropped after the move.
    """
    pw = None
    if user:
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            raise CgroupError("no such Linux user: %s" % user)

    def child_setup():
        # Still root here: enter the cgroup before any untrusted code runs.
        cg.add_process(os.getpid())
        if pw is not None:
            os.setgroups(os.getgrouplist(pw.pw_name, pw.pw_gid))
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)

    return subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        preexec_fn=child_setup,
        close_fds=True,
        **popen_kwargs,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_init(args) -> int:
    info = require_cgroup2()
    if not info["is_root"]:
        raise CgroupError("run with sudo")
    # Fresh start: remove stale leaves but keep the parent.
    cleanup(args.parent, keep_parent=True)
    mem = parse_size(args.memory) if args.memory else None
    swap = parse_size(args.swap) if args.swap is not None else None
    cpu = parse_cpu(args.cpu) if args.cpu else None
    parent_mem = mem * args.players if mem is not None else None
    parent_swap = swap * args.players if swap is not None else None
    parent_pids = int(args.pids) * args.players + 8 if args.pids else None
    parent_cpu = _cpu_multiply(cpu, args.players) if cpu else None

    ensure_parent(args.parent, memory=parent_mem, swap=parent_swap,
                  pids=parent_pids, cpu=parent_cpu)
    parent = Cgroup(args.parent, parent="")
    out = {
        "parent": parent.path,
        "players": args.players,
        "per_player": {
            "memory": args.memory,
            "swap": args.swap,
            "pids": args.pids,
            "cpu": args.cpu,
            "cpu_max": parse_cpu(args.cpu) if args.cpu else None,
        },
        "aggregate": {
            "memory": parent_mem,
            "swap": parent_swap,
            "pids": parent_pids,
            "cpu": parent_cpu,
        },
        "stats": parent.stats(),
    }
    print(json.dumps(out, indent=2))
    return 0


def _cmd_show(args) -> int:
    rows = show(args.parent)
    if args.table:
        print("%-18s %9s %9s %9s %7s %8s %5s %5s"
              % ("leaf", "mem_MiB", "peak_MiB", "max_MiB",
                 "pids", "cpu_sec", "oom", "kill"))
        for r in rows:
            mib = 1024 * 1024
            max_mem = r.get("memory_max")
            try:
                max_mem = "%.0f" % (int(max_mem) / mib)
            except (TypeError, ValueError):
                max_mem = str(max_mem)
            print("%-18s %9.1f %9.1f %9s %7s %8.2f %5d %5d"
                  % (r["name"], r["memory_current"] / mib,
                     r["memory_peak"] / mib, max_mem,
                     "%s/%s" % (r["pids_current"], r["pids_max"]),
                     r["cpu_usage_usec"] / 1e6,
                     r["oom"], r["oom_kill"]))
        return 0
    if not rows:
        print(json.dumps({"parent": Cgroup(args.parent, parent="").path,
                          "groups": []}, indent=2))
        return 0
    print(json.dumps({"parent": Cgroup(args.parent, parent="").path,
                      "groups": rows}, indent=2))
    return 0


def _cmd_cleanup(args) -> int:
    removed = cleanup(args.parent, keep_parent=args.keep_parent)
    print(json.dumps({"removed": removed, "parent": args.parent,
                      "kept_parent": bool(args.keep_parent)}, indent=2))
    return 0


def _cmd_detect(args) -> int:
    print(json.dumps(detect(), indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="cgroup v2 helpers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("detect", help="show cgroup v2 support")
    p.set_defaults(func=_cmd_detect)

    p = sub.add_parser("init", help="create the parent and aggregate caps")
    p.add_argument("--parent", default=DEFAULT_PARENT)
    p.add_argument("--players", type=int, required=True)
    p.add_argument("--memory", default="64M")
    p.add_argument("--swap", default="0")
    p.add_argument("--pids", type=int, default=24)
    p.add_argument("--cpu", default="50")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("show", help="print per-leaf statistics")
    p.add_argument("--parent", default=DEFAULT_PARENT)
    p.add_argument("--table", action="store_true",
                   help="human-readable one line per leaf")
    p.set_defaults(func=_cmd_show)

    p = sub.add_parser("cleanup", help="kill and remove leaves")
    p.add_argument("--parent", default=DEFAULT_PARENT)
    p.add_argument("--keep-parent", action="store_true")
    p.set_defaults(func=_cmd_cleanup)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except CgroupError as exc:
        print("cgroup: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
