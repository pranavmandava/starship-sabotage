#!/usr/bin/env python3
"""Hostile player used to demonstrate the cgroup v2 isolation layer.

Every mode is meant to be launched through cglaunch.py, e.g.:

    sudo python3 cglaunch.py --name attacker --user attacker \
        --memory 64M --pids 24 --cpu 50 -- \
        python3 attack.py --name attacker --attack memory

Modes:
    memory    allocate --size-mb MiB in 1 MiB chunks and hold it
    fork      fork children until pids.max refuses (fork bomb)
    cpu       spin for --seconds (cpu.max throttles it)
    flood     join the game and send chat as fast as possible
    oversize  send one huge JSON line (input-validation probe)
    malformed send invalid JSON frames
    idle      join and stay connected

With --socket, the attacker first joins the game as a normal player and then
attacks, so the server log shows the player disconnecting while the game
continues.  Without --socket it just performs the resource attack, which is
what the automated cgroup tests use.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time

import commlib


def log(text: str) -> None:
    print("[attack] %s" % text, flush=True)


async def drain(reader) -> None:
    """Keep the server's outbox from filling up while we attack."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                return
    except (ConnectionError, asyncio.CancelledError, OSError):
        return


async def join(name: str, socket_path: str):
    reader, writer = await commlib.connect_socket(socket_path)
    await commlib.send_msg(writer, {"type": "hello", "name": name})
    msg = await asyncio.wait_for(commlib.recv_msg(reader), timeout=5)
    log("joined as %s (role=%s) first=%s"
        % (name, msg.get("role", "?"), msg.get("type")))
    asyncio.create_task(drain(reader))
    return writer


# ---------------------------------------------------------------------------
# Resource attacks
# ---------------------------------------------------------------------------

def attack_memory(size_mb: int, hold: float) -> None:
    log("allocating %d MiB in 1 MiB chunks" % size_mb)
    chunks = []
    try:
        for i in range(size_mb):
            chunk = bytearray(1024 * 1024)
            # Touch every page so the kernel cannot satisfy this lazily.
            chunk[::4096] = b"x" * ((len(chunk) + 4095) // 4096)
            chunks.append(chunk)
            if i % 16 == 15:
                log("allocated %d MiB" % (i + 1))
        log("allocated all %d MiB; holding for %.1fs" % (size_mb, hold))
        time.sleep(hold)
    except MemoryError:
        log("MemoryError after %d MiB" % len(chunks))


def attack_fork(seconds: float) -> None:
    log("starting fork storm for %.1fs" % seconds)
    children = []
    ok = fail = 0
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            try:
                pid = os.fork()
            except OSError as exc:
                fail += 1
                if fail in (1, 10, 100) or fail % 500 == 0:
                    log("fork refused after %d successes: %s" % (ok, exc))
                time.sleep(0.01)
                continue
            if pid == 0:
                # Child: stay alive so it actually consumes a PID slot.
                try:
                    time.sleep(seconds + 10)
                finally:
                    os._exit(0)
            children.append(pid)
            ok += 1
    finally:
        log("fork storm done: ok=%d fail=%d" % (ok, fail))
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def attack_cpu(seconds: float) -> None:
    log("spinning CPU for %.1fs" % seconds)
    start = time.time()
    iterations = 0
    while time.time() - start < seconds:
        iterations += 1
    log("cpu spin done: %d iterations in %.2fs"
        % (iterations, time.time() - start))


async def attack_flood(writer, name: str, seconds: float) -> None:
    log("flooding chat for %.1fs" % seconds)
    deadline = time.time() + seconds
    sent = 0
    try:
        while time.time() < deadline:
            await commlib.send_msg(writer, {
                "type": "chat",
                "text": "flood %d from %s" % (sent, name),
            })
            sent += 1
    except (ConnectionError, BrokenPipeError, OSError):
        log("server closed the flooding connection after %d messages" % sent)
        return
    log("flood sent %d messages (%.0f/s)" % (sent, sent / seconds))


async def attack_oversize(writer, size_mb: int) -> None:
    line = b'{"type":"chat","text":"' + b"A" * (size_mb * 1024 * 1024) + b'"}\n'
    log("sending one %.1f MiB frame" % (len(line) / (1024 * 1024)))
    try:
        writer.write(line)
        await writer.drain()
        log("frame accepted by the transport")
    except (ConnectionError, BrokenPipeError, OSError) as exc:
        log("transport rejected the frame: %s" % exc)


async def attack_malformed(writer, count: int) -> None:
    log("sending %d malformed frames" % count)
    for _ in range(count):
        try:
            writer.write(b"this is not json\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError, OSError) as exc:
            log("connection closed after malformed frame: %s" % exc)
            return
    log("malformed frames sent")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args) -> int:
    writer = None
    if args.socket:
        try:
            writer = await join(args.name, args.socket)
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            log("could not join %s: %s" % (args.socket, exc))
            return 1

    if args.attack == "idle":
        log("idling")
        await asyncio.sleep(args.seconds)
    elif args.attack == "memory":
        attack_memory(args.size_mb, args.hold)
    elif args.attack == "fork":
        attack_fork(args.seconds)
    elif args.attack == "cpu":
        attack_cpu(args.seconds)
    elif args.attack == "flood":
        if writer is None:
            log("flood needs --socket")
            return 1
        await attack_flood(writer, args.name, args.seconds)
    elif args.attack == "oversize":
        if writer is None:
            log("oversize needs --socket")
            return 1
        await attack_oversize(writer, args.size_mb)
    elif args.attack == "malformed":
        if writer is None:
            log("malformed needs --socket")
            return 1
        await attack_malformed(writer, args.count)

    if writer is not None:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
    log("done")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hostile Starship Sabotage player")
    ap.add_argument("--name", default="attacker")
    ap.add_argument("--socket", default=None,
                    help="join this server first (otherwise resource-only)")
    ap.add_argument("--attack", default="idle",
                    choices=("idle", "memory", "fork", "cpu", "flood",
                             "oversize", "malformed"))
    ap.add_argument("--size-mb", type=int, default=256)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--hold", type=float, default=30.0)
    ap.add_argument("--count", type=int, default=100)
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
