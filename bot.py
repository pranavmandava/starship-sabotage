#!/usr/bin/env python3
"""Tiny auto-playing Starship Sabotage client, used by the isolation tests.

    python3 bot.py --name bot1 --socket /tmp/starship.sock --interval 0.5

It joins, does tasks during TASK, votes during MEETING, and exits on game_over.
It deliberately never kills or sabotages, so a test game lasts long enough for
an attacker to join and misbehave.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys

import commlib


class Bot:
    def __init__(self, name: str, socket_path: str, interval: float,
                 quiet: bool = False):
        self.name = name
        self.socket_path = socket_path
        self.interval = interval
        self.quiet = quiet
        self.role = "?"
        self.phase = "LOBBY"
        self.alive = []
        self.latest = {}
        self.voted = False

    def log(self, text: str) -> None:
        if not self.quiet:
            print("[bot %s] %s" % (self.name, text), flush=True)

    async def recv_loop(self, reader) -> None:
        while True:
            msg = await commlib.recv_msg(reader)
            kind = msg.get("type")
            if kind == "welcome":
                self.role = msg.get("role", "?")
                self.log("joined as role=%s %s" % (self.role, msg.get("note", "")))
            elif kind == "state":
                self.latest = msg
                self.phase = msg.get("phase", self.phase)
                self.alive = msg.get("alive", [])
                if self.phase != "MEETING":
                    self.voted = False
            elif kind == "meeting_start":
                self.phase = "MEETING"
                self.voted = False
                self.log("meeting: %s" % msg.get("reason", ""))
            elif kind == "meeting_end":
                self.phase = "TASK"
                self.voted = False
            elif kind == "game_over":
                self.log("game over: winner=%s (%s)"
                         % (msg.get("winner"), msg.get("reason")))
                return

    async def act_loop(self, writer) -> None:
        while True:
            await asyncio.sleep(self.interval)
            alive = self.alive
            if self.phase == "TASK" and self.name in alive:
                if random.random() < 0.15:
                    await commlib.send_msg(writer, {"type": "chat",
                                                    "text": "working on it"})
                else:
                    action = random.choice(("do-task", "fix-o2", "fix-reactor"))
                    await commlib.send_msg(writer,
                                           {"type": "action", "action": action})
            elif self.phase == "MEETING" and not self.voted \
                    and self.name in alive:
                targets = [n for n in alive if n != self.name]
                if targets:
                    target = random.choice(targets)
                    await commlib.send_msg(writer,
                                           {"type": "vote", "target": target})
                    self.voted = True
                    self.log("voted %s" % target)

    async def run(self) -> int:
        reader, writer = await commlib.connect_socket(self.socket_path)
        await commlib.send_msg(writer, {"type": "hello", "name": self.name})
        recv = asyncio.create_task(self.recv_loop(reader))
        act = asyncio.create_task(self.act_loop(writer))
        done, pending = await asyncio.wait(
            {recv, act}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        exc = None
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                exc = error
        if exc and not isinstance(exc, (ConnectionError, asyncio.CancelledError)):
            print("[bot %s] %s" % (self.name, exc), file=sys.stderr)
            return 1
        try:
            writer.close()
        except Exception:
            pass
        return 0


async def main_async(args) -> int:
    return await Bot(args.name, args.socket, args.interval, args.quiet).run()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="auto-playing game client")
    ap.add_argument("--name", required=True)
    ap.add_argument("--socket", default=commlib.DEFAULT_SOCKET_PATH)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
