#!/usr/bin/env python3
"""Automated proof that the cgroup v2 layer actually contains a bad player.

    sudo python3 test_isolation.py            # memory, pids, cpu
    sudo python3 test_isolation.py --game     # + a real 5-player game

The first three tests are deterministic: a child is put in a small cgroup and
asked to allocate too much memory, fork too many processes, or burn too much
CPU.  We assert on the kernel's own counters (memory.peak, pids.peak,
cpu.stat, memory.events), not on the child's self-report.

--game starts the real server, four bots and a hostile fifth player.  The
attacker is capped at 32 MiB and tries to allocate 128 MiB.  The test passes
only if the attacker is OOM-killed inside its own cgroup and the server is
still accepting connections afterwards.

Run as root: cgroup files are root-owned.  The script re-execs itself through
sudo when passwordless sudo is available.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import cgroup

PARENT = "starship-test-cgroup"
ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

MEM_CODE = """
x = bytearray(128 * 1024 * 1024)
print(len(x), flush=True)
"""

PIDS_CODE = """
import json, os, time
ok = fail = 0
for _ in range(40):
    try:
        pid = os.fork()
    except OSError:
        fail += 1
        continue
    if pid == 0:
        time.sleep(10)
        os._exit(0)
    ok += 1
print(json.dumps({"ok": ok, "fail": fail}), flush=True)
time.sleep(0.2)
"""

CPU_CODE = """
import time
t = time.time()
n = 0
while time.time() - t < 2:
    n += 1
print(n, flush=True)
"""


class Results:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail, stats=None):
        self.rows.append({"test": name, "pass": bool(ok),
                          "detail": detail, "stats": stats or {}})
        print("[%s] %-18s %s" % ("PASS" if ok else "FAIL", name, detail),
              flush=True)

    @property
    def ok(self):
        return all(r["pass"] for r in self.rows)


def wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def run_limited(name, code, memory, pids, cpu, timeout=20.0, keep=False):
    cg = cgroup.Cgroup(name, parent=PARENT)
    if cg.exists():
        cg.remove()
    cg.create()
    cg.set_limits(memory=memory, swap="0", pids=pids, cpu=cpu, oom_group=1)
    with tempfile.TemporaryFile(mode="w+") as out:
        proc = cgroup.spawn(
            [PY, "-c", code], cg,
            stdout=out, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            cg.kill()
            rc = proc.wait(timeout=5)
        out.seek(0)
        output = out.read()
    stats = cg.stats()
    stats["configured"] = {"memory": memory, "pids": pids, "cpu": cpu}
    if not keep:
        cg.remove()
    return rc, stats, output


def test_memory(results, keep):
    rc, stats, _ = run_limited("t-memory", MEM_CODE, "32M", 8, "25",
                               keep=keep)
    peak = stats.get("memory_peak", 0)
    limit = 32 * 1024 * 1024
    ok = (rc < 0 and stats.get("oom_kill", 0) >= 1
          and peak <= limit + 2 * 1024 * 1024)
    results.add("memory.max", ok,
                "exit=%s peak=%.1fMiB limit=32MiB oom_kill=%s"
                % (rc, peak / 1024 / 1024, stats.get("oom_kill", 0)),
                stats)


def test_pids(results, keep):
    rc, stats, output = run_limited("t-pids", PIDS_CODE, "64M", 8, "25",
                                    keep=keep)
    try:
        counts = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        counts = {"ok": -1, "fail": -1}
    peak = stats.get("pids_peak", 0)
    ok = counts.get("fail", 0) > 0 and peak <= 8
    results.add("pids.max", ok,
                "forks ok=%s fail=%s pids_peak=%s limit=8"
                % (counts.get("ok"), counts.get("fail"), peak),
                stats)


def test_cpu(results, keep):
    rc, stats, _ = run_limited("t-cpu", CPU_CODE, "64M", 8, "25",
                               keep=keep)
    used = stats.get("cpu_usage_usec", 0) / 1e6
    throttled = stats.get("cpu_nr_throttled", 0)
    ok = rc == 0 and 0.2 <= used <= 1.3 and throttled > 0
    results.add("cpu.max", ok,
                "cpu=%.2fs in 2s wall (25%% quota) throttled=%d"
                % (used, throttled), stats)


def logger(path):
    return open(path, "a", encoding="utf-8")


def tail(path, n=40):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def read_all(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def test_game(results, keep):
    sock = "/tmp/starship-isolation-test-%d.sock" % os.getpid()
    log_path = "/tmp/starship-isolation-test-%d.log" % os.getpid()
    server = None
    attackers = []
    bots = []
    log = None
    try:
        for path in (sock, log_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        log = logger(log_path)
        server = subprocess.Popen(
            [PY, "server.py", "--socket", sock, "--players", "5",
             "--tasks-goal", "999", "--meeting-secs", "5"],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if not wait_until(lambda: os.path.exists(sock), 5):
            results.add("game/server", False, "server never created the socket")
            return
        if server.poll() is not None:
            results.add("game/server", False, "server exited early:\n" + tail(log_path))
            return

        for i in range(4):
            cg = cgroup.Cgroup("game-bot-%d" % i, parent=PARENT)
            cg.create()
            cg.set_limits(memory="64M", swap="0", pids=24, cpu="50",
                          oom_group=1)
            p = cgroup.spawn(
                [PY, "bot.py", "--name", "bot%d" % i, "--socket", sock,
                 "--interval", "0.2"],
                cg, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            bots.append((p, cg))

        if not wait_until(
                lambda: read_all(log_path).count("joined as role=") >= 4, 8):
            results.add("game/bots", False,
                        "four bots did not join:\n" + tail(log_path, 20))
            return
        results.add("game/bots", True, "4 bots joined the lobby")

        acg = cgroup.Cgroup("game-attacker", parent=PARENT)
        acg.create()
        acg.set_limits(memory="32M", swap="0", pids=8, cpu="25",
                       oom_group=1)
        attack = cgroup.spawn(
            [PY, "attack.py", "--name", "attacker", "--socket", sock,
             "--attack", "memory", "--size-mb", "128", "--hold", "10"],
            acg, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        attackers.append((attack, acg))

        started = wait_until(
            lambda: "joined as attacker" in read_all(log_path), 5)
        results.add("game/start", started,
                    "attacker joined as the fifth player" if started
                    else "attacker never joined:\n" + tail(log_path, 20))

        try:
            rc = attack.wait(timeout=20)
        except subprocess.TimeoutExpired:
            acg.kill()
            rc = attack.wait(timeout=5)
        astats = acg.stats()
        oom = astats.get("oom_kill", 0) >= 1
        results.add("game/attacker-oom", rc < 0 and oom,
                    "attacker exit=%s oom_kill=%s peak=%.1fMiB"
                    % (rc, astats.get("oom_kill", 0),
                       astats.get("memory_peak", 0) / 1024 / 1024),
                    astats)

        server_alive = server.poll() is None
        probe_ok = False
        if server_alive:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(2)
            try:
                probe.connect(sock)
                probe_ok = True
            except OSError:
                pass
            finally:
                probe.close()
        traceback = "Traceback" in tail(log_path, 300)
        alive_bots = sum(1 for p, _ in bots if p.poll() is None)
        results.add("game/server-survives",
                    server_alive and probe_ok and not traceback,
                    "server_alive=%s accept_probe=%s traceback=%s bots_alive=%d"
                    % (server_alive, probe_ok, traceback, alive_bots))
    finally:
        for p, cg in attackers + bots:
            try:
                p.kill()
            except ProcessLookupError:
                pass
            cg.remove()
        if server is not None and server.poll() is None:
            try:
                os.killpg(server.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            server.wait(timeout=5)
        if log is not None:
            log.close()
        for path in (sock, log_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="cgroup isolation tests")
    ap.add_argument("--game", action="store_true",
                    help="also run the end-to-end 5-player survival test")
    ap.add_argument("--keep", action="store_true",
                    help="leave cgroups for inspection after the run")
    ap.add_argument("--json", default=os.path.join(ROOT, "report",
                                                   "isolation-results.json"))
    args = ap.parse_args(argv)

    if os.geteuid() != 0:
        if shutil.which("sudo") and subprocess.run(
                ["sudo", "-n", "true"], capture_output=True).returncode == 0:
            os.execvp("sudo", ["sudo", PY, os.path.abspath(__file__), *sys.argv[1:]])
        print("run as root: sudo python3 test_isolation.py", file=sys.stderr)
        return 2

    info = cgroup.detect()
    if not info["cgroup2"]:
        print("cgroup v2 is not mounted at %s" % cgroup.CGROUP_ROOT,
              file=sys.stderr)
        return 2
    missing = [c for c in ("memory", "pids", "cpu")
               if c not in info["root_controllers"]]
    if missing:
        print("missing controllers: %s" % ", ".join(missing), file=sys.stderr)
        return 2

    cgroup.cleanup(PARENT, keep_parent=True)
    cgroup.ensure_parent(PARENT)
    results = Results()
    try:
        test_memory(results, args.keep)
        test_pids(results, args.keep)
        test_cpu(results, args.keep)
        if args.game:
            test_game(results, args.keep)
    finally:
        if not args.keep:
            cgroup.cleanup(PARENT, keep_parent=False)

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"parent": PARENT, "pass": results.ok,
                       "cgroup": info, "results": results.rows},
                      f, indent=2)
            f.write("\n")
        print("wrote %s" % args.json)
    print("RESULT: %s" % ("PASS" if results.ok else "FAIL"))
    return 0 if results.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
