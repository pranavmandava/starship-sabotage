#!/usr/bin/env python3
"""Run one Starship Sabotage player inside a limited cgroup v2 leaf.

    sudo python3 cglaunch.py --name alice --user alice \
        --memory 64M --pids 24 --cpu 50 \
        --stats /tmp/starship-cgroup/alice.json -- \
        python3 client.py --name alice

The launcher must run as root so it can write /sys/fs/cgroup.  The child is
moved into its leaf before exec and then drops to --user, so the player itself
never runs as root.  A player that exhausts its memory or PID cap is killed by
the kernel inside its own cgroup; the server, which lives outside the player
subtree, is unaffected.

Exit status is the child's status (128+signal if it was killed).  A JSON file
written by --stats records limits, samples, peaks and OOM events.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import signal
import sys
import time

import cgroup


def default_user() -> str | None:
    """When invoked through sudo, default to the invoking user, not root."""
    uid = os.environ.get("SUDO_UID")
    if uid and uid.isdigit():
        try:
            return pwd.getpwuid(int(uid)).pw_name
        except KeyError:
            pass
    name = os.environ.get("SUDO_USER")
    if name:
        return name
    return None


def write_json(path: str, data: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="launch one process in a cgroup")
    ap.add_argument("--name", required=True, help="logical player name")
    ap.add_argument("--group", default=None,
                    help="cgroup leaf name (default: player-<name>)")
    ap.add_argument("--parent", default=cgroup.DEFAULT_PARENT)
    ap.add_argument("--user", default=None,
                    help="Linux user to run the child as (default: sudo user)")
    ap.add_argument("--memory", default="64M")
    ap.add_argument("--swap", default="0")
    ap.add_argument("--pids", type=int, default=24)
    ap.add_argument("--cpu", default="50")
    ap.add_argument("--oom-group", type=int, choices=(0, 1), default=1)
    # Aggregate limits only need to be supplied on the first launch.
    ap.add_argument("--parent-memory", default=None)
    ap.add_argument("--parent-swap", default=None)
    ap.add_argument("--parent-pids", type=int, default=None)
    ap.add_argument("--parent-cpu", default=None)
    ap.add_argument("--stats", default=None)
    ap.add_argument("--sample-ms", type=int, default=0)
    ap.add_argument("--replace", action="store_true",
                    help="kill and remove an existing leaf with this name")
    ap.add_argument("--keep", action="store_true",
                    help="leave the cgroup behind for inspection")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.error("missing command after --")

    if os.geteuid() != 0:
        print("cglaunch: run with sudo (cgroup files are root-owned)",
              file=sys.stderr)
        return 2

    info = cgroup.detect()
    if not info["cgroup2"]:
        print("cglaunch: cgroup v2 not mounted at %s" % cgroup.CGROUP_ROOT,
              file=sys.stderr)
        return 2

    user = args.user if args.user is not None else default_user()
    group_name = args.group or ("player-" + args.name)

    # Make sure the shared parent exists.  Aggregate limits are optional and
    # normally supplied once by play.sh's init step.
    try:
        cgroup.ensure_parent(
            args.parent,
            memory=args.parent_memory,
            swap=args.parent_swap,
            pids=args.parent_pids,
            cpu=args.parent_cpu,
        )
    except cgroup.CgroupError as exc:
        print("cglaunch: %s" % exc, file=sys.stderr)
        return 2

    leaf = cgroup.Cgroup(group_name, parent=args.parent)
    if leaf.exists():
        if not args.replace:
            print("cglaunch: %s already exists (use --replace to reset)"
                  % leaf.path, file=sys.stderr)
            return 2
        leaf.remove()

    try:
        leaf.create()
        leaf.set_limits(memory=args.memory, swap=args.swap, pids=args.pids,
                        cpu=args.cpu, oom_group=args.oom_group)
    except (cgroup.CgroupError, OSError, ValueError) as exc:
        print("cglaunch: cannot configure %s: %s" % (leaf.path, exc),
              file=sys.stderr)
        leaf.remove()
        return 2

    limits = {
        "memory": args.memory,
        "memory_bytes": cgroup.parse_size(args.memory),
        "swap": args.swap,
        "swap_bytes": cgroup.parse_size(args.swap),
        "pids": args.pids,
        "cpu": args.cpu,
        "cpu_max": cgroup.parse_cpu(args.cpu),
        "oom_group": args.oom_group,
        "user": user or "root",
        "group": group_name,
        "parent": args.parent,
    }
    print("[cgroup] %s: memory.max=%s swap.max=%s pids.max=%d cpu.max=%s user=%s"
          % (args.name, args.memory, args.swap, args.pids,
             limits["cpu_max"], limits["user"]), flush=True)

    started = time.time()
    try:
        proc = cgroup.spawn(cmd, leaf, user=user)
    except (cgroup.CgroupError, OSError) as exc:
        print("cglaunch: spawn failed: %s" % exc, file=sys.stderr)
        leaf.remove()
        return 2

    stopping = {"signal": None}

    def forward(signum, _frame):
        stopping["signal"] = signum
        if proc.poll() is None:
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)

    samples = []
    interval = max(0, args.sample_ms) / 1000.0
    while proc.poll() is None:
        if interval:
            samples.append({"t": round(time.time() - started, 3),
                            **leaf.stats()})
            time.sleep(interval)
        else:
            time.sleep(0.1)

    # If the launcher was asked to stop but the child ignored the signal,
    # make sure no descendant survives in the cgroup.
    if stopping["signal"] is not None and leaf.pids():
        leaf.kill()

    rc = proc.wait()
    final = leaf.stats()
    duration = round(time.time() - started, 3)
    result = {
        "name": args.name,
        "cmd": cmd,
        "limits": limits,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "duration_secs": duration,
        "exit_code": rc,
        "signal": -rc if rc < 0 else 0,
        "killed": rc < 0,
        "oom_killed": final.get("oom_group_kill", 0) > 0
                      or final.get("oom_kill", 0) > 0,
        "final": final,
        "samples": samples,
    }
    if args.stats:
        try:
            write_json(args.stats, result)
        except OSError as exc:
            print("cglaunch: cannot write stats: %s" % exc, file=sys.stderr)

    if final.get("oom_group_kill", 0) or final.get("oom_kill", 0):
        print("[cgroup] %s: OOM-killed inside its cgroup "
              "(memory.peak=%.1f MiB)"
              % (args.name, final.get("memory_peak", 0) / (1024 * 1024)),
              flush=True)
    else:
        print("[cgroup] %s: exit=%s peak_mem=%.1f MiB peak_pids=%s "
              "cpu=%.2fs throttled=%s"
              % (args.name, rc,
                 final.get("memory_peak", 0) / (1024 * 1024),
                 final.get("pids_peak", 0),
                 final.get("cpu_usage_usec", 0) / 1e6,
                 final.get("cpu_nr_throttled", 0)), flush=True)

    if not args.keep:
        try:
            leaf.remove()
        except OSError as exc:
            print("cglaunch: cleanup failed: %s" % exc, file=sys.stderr)

    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    raise SystemExit(main())
