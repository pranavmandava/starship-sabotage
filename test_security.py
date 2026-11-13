#!/usr/bin/env python3
"""Regression tests for the connection-security fixes.

    sudo python3 test_security.py

Covers:
  * --allow-users: only the expected Linux UID may join, and its name must
    match that user (SO_PEERCRED, not the JSON name).
  * Name ownership: once a UID claims a name, another UID cannot reuse it,
    even after the first connection closes.
  * Hello timeout: a silent connection is closed after HELLO_TIMEOUT.
  * Malformed hello: non-object JSON is rejected without taking down the server.
  * Socket mode: --socket-mode is applied.

The peer-UID tests need a second local user; pass --user NAME (default:
player0). They are skipped if that user does not exist.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
HELLO_TIMEOUT = 5.0

CLIENT = r"""
import json, socket, sys
path, name = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(8)
try:
    s.connect(path)
    try:
        s.sendall((json.dumps({"type": "hello", "name": name}) + "\n").encode())
    except OSError as exc:
        print("SENDERR", exc)
    try:
        data = s.recv(65536)
        print(data.decode("utf-8", "replace").strip())
    except Exception as exc:
        print("RECVERR", exc)
finally:
    s.close()
"""

SILENT = r"""
import socket, sys, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(10)
s.connect(sys.argv[1])
time.sleep(6.5)
try:
    data = s.recv(65536)
    print(data.decode("utf-8", "replace").strip())
except Exception as exc:
    print("ERR", exc)
s.close()
"""

HOLD = r"""
import json, socket, sys, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
s.sendall((json.dumps({"type": "hello", "name": sys.argv[2]}) + "\n").encode())
time.sleep(30)
s.close()
"""

BAD_HELLO = r"""
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(8)
s.connect(sys.argv[1])
s.sendall(b"5\n")
print(s.recv(65536).decode("utf-8", "replace").strip())
s.close()
"""


class Results:
    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail):
        self.rows.append((name, ok, detail))
        print("[%s] %-22s %s" % ("PASS" if ok else "FAIL", name, detail))

    @property
    def ok(self):
        return all(r[1] for r in self.rows)


def wait_socket(path, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return os.path.exists(path)


class Server:
    def __init__(self, path, *extra):
        self.path = path
        self.log = tempfile.NamedTemporaryFile(
            mode="w+", prefix="starship-sec-", suffix=".log")
        self.proc = subprocess.Popen(
            [PY, "server.py", "--socket", path, "--players", "5", *extra],
            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if not wait_socket(path):
            raise RuntimeError("server did not create socket %s" % path)

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, 9)
            self.proc.wait(timeout=5)
        self.log.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def run_client(script, path, name="", user=None):
    cmd = [PY, script, path, name] if name else [PY, script, path]
    if user:
        cmd = ["sudo", "-n", "-u", user] + cmd
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return out.stdout.strip(), out.stderr.strip()


def write_temp(name, text):
    path = os.path.join(tempfile.gettempdir(), name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


def test_allow_users(results, user):
    sock = "/tmp/starship-sec-allow-%d.sock" % os.getpid()
    script = write_temp("starship-sec-client.py", CLIENT)
    srv = Server(sock, "--allow-users", user)
    try:
        # Root is not in the allowlist.
        out, _ = run_client(script, sock, user)
        results.add("allow-users/uid", "not an authorized player" in out,
                    out or "(no response)")
        # The allowed user, with its matching name, is accepted.
        out, err = run_client(script, sock, user, user=user)
        results.add("allow-users/match", '"welcome"' in out,
                    out or err or "(no response)")
        # The allowed user cannot claim somebody else's name.
        out, _ = run_client(script, sock, "someone-else", user=user)
        results.add("allow-users/name", "does not match" in out,
                    out or "(no response)")
    finally:
        srv.close()


def test_name_owner(results, user):
    sock = "/tmp/starship-sec-owner-%d.sock" % os.getpid()
    script = write_temp("starship-sec-client.py", CLIENT)
    hold = write_temp("starship-sec-hold.py", HOLD)
    srv = Server(sock)
    first = None
    try:
        # Root claims the name and keeps the connection open.
        first = subprocess.Popen(
            [PY, hold, sock, "shared"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
        out, _ = run_client(script, sock, "shared", user=user)
        results.add("name/taken", "name taken" in out, out or "(no response)")
        first.kill()
        first.wait(timeout=5)
        time.sleep(0.3)
        out, _ = run_client(script, sock, "shared", user=user)
        results.add("name/owned-after-close",
                    "name owned by another user" in out,
                    out or "(no response)")
    finally:
        if first is not None and first.poll() is None:
            first.kill()
        srv.close()


def test_hello_timeout(results):
    sock = "/tmp/starship-sec-timeout-%d.sock" % os.getpid()
    script = write_temp("starship-sec-silent.py", SILENT)
    srv = Server(sock)
    try:
        started = time.time()
        out, _ = run_client(script, sock)
        elapsed = time.time() - started
        results.add("hello/timeout",
                    "hello timeout" in out and elapsed < HELLO_TIMEOUT + 3,
                    "%.1fs: %s" % (elapsed, out or "(no response)"))
    finally:
        srv.close()


def test_malformed_hello(results):
    sock = "/tmp/starship-sec-bad-%d.sock" % os.getpid()
    bad = write_temp("starship-sec-bad.py", BAD_HELLO)
    client = write_temp("starship-sec-client.py", CLIENT)
    srv = Server(sock)
    try:
        out, _ = run_client(bad, sock)
        results.add("hello/malformed", "first msg" in out, out or "(no response)")
        out, _ = run_client(client, sock, "still-alive")
        results.add("hello/after-malformed", '"welcome"' in out,
                    out or "(no response)")
    finally:
        srv.close()


def test_socket_mode(results):
    sock = "/tmp/starship-sec-mode-%d.sock" % os.getpid()
    srv = Server(sock, "--socket-mode", "0660")
    try:
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        results.add("socket/mode", mode == 0o660, "mode=0%o" % mode)
    finally:
        srv.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="player0",
                    help="second Linux user for peer-UID tests")
    args = ap.parse_args(argv)

    if os.geteuid() != 0:
        print("run as root: sudo python3 test_security.py", file=sys.stderr)
        return 2

    try:
        pwd.getpwnam(args.user)
        have_user = True
    except KeyError:
        have_user = False

    results = Results()
    test_socket_mode(results)
    test_hello_timeout(results)
    test_malformed_hello(results)
    if have_user:
        test_allow_users(results, args.user)
        test_name_owner(results, args.user)
    else:
        print("[SKIP] peer-UID tests: no such user %r" % args.user)
    print("RESULT: %s" % ("PASS" if results.ok else "FAIL"))
    return 0 if results.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
