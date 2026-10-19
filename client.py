"""Starship Sabotage client. One process per player (distinct Linux user ideal).

Usage: uv run python client.py --name alice [--socket /tmp/starship.sock]
Commands (type at prompt):
  task | fix o2 | fix reactor | kill <name> | sabotage <o2|reactor>
  meeting | report <name> | vote <name> | chat <text> | status | help | quit
"""

import argparse
import asyncio

import commlib


async def recv_loop(reader):
    try:
        while True:
            msg = await commlib.recv_msg(reader)
            t = msg.get("type")
            if t == "welcome":
                print(f"[you are {msg.get('name')} | role={msg.get('role')}] {msg.get('note','')}")
            elif t == "state":
                print(f"--- T{msg['turn']} [{msg['phase']}] O2:{msg['o2']}% R:{msg['reactor']}% "
                      f"Tasks:{msg['tasks']}/{msg['tasks_goal']} alive:{','.join(msg['alive'])} ---")
            elif t == "event":
                print(f"[event] {msg.get('text')}")
            elif t == "result":
                print(f"[{'ok' if msg.get('ok') else 'FAIL'}] {msg.get('reason')}")
            elif t == "meeting_start":
                print(f"=== MEETING: {msg.get('reason')} ({msg.get('secs')}s) — vote <name> ===")
            elif t == "meeting_end":
                print(f"=== MEETING END ejected={msg.get('ejected') or 'none'} tally={msg.get('tally')} ===")
            elif t == "game_over":
                print(f"*** GAME OVER winner={msg.get('winner')} ({msg.get('reason')}) ***")
                return
            else:
                print(msg)
    except ConnectionError:
        print("[disconnected]")


def to_wire(line: str):
    s = line.strip()
    if not s:
        return None
    low = s.lower()
    if low in ("task", "do task", "t"):
        return {"type": "action", "action": "do-task"}
    if low in ("fix o2", "fix-o2", "o2"):
        return {"type": "action", "action": "fix-o2"}
    if low in ("fix reactor", "fix-reactor", "reactor"):
        return {"type": "action", "action": "fix-reactor"}
    if low.startswith("kill "):
        return {"type": "action", "action": "kill", "target": s.split(None, 1)[1].strip()}
    if low.startswith("sabotage "):
        return {"type": "action", "action": "sabotage", "system": s.split(None, 1)[1].strip().lower()}
    if low == "meeting":
        return {"type": "call-meeting"}
    if low.startswith("report "):
        return {"type": "report", "target": s.split(None, 1)[1].strip()}
    if low.startswith("vote "):
        return {"type": "vote", "target": s.split(None, 1)[1].strip()}
    if low.startswith("chat "):
        return {"type": "chat", "text": s[5:]}
    if low in ("status", "help", "quit", "q"):
        return {"type": "_local", "cmd": low}
    # default: treat as chat
    return {"type": "chat", "text": s}


async def send_loop(writer, name):
    print("commands: task | fix o2 | fix reactor | kill <n> | sabotage <o2|reactor> | meeting | report <n> | vote <n> | chat <t> | quit")
    while True:
        line = await asyncio.to_thread(input, f"{name}> ")
        wire = to_wire(line)
        if wire is None:
            continue
        if wire.get("type") == "_local":
            if wire["cmd"] in ("quit", "q"):
                writer.close()
                return
            print("task | fix o2 | fix reactor | kill <n> | sabotage <o2|reactor> | meeting | report <n> | vote <n> | chat <t>")
            continue
        try:
            await commlib.send_msg(writer, wire)
        except (ConnectionError, BrokenPipeError):
            print("[send failed]")
            return


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--socket", default=commlib.DEFAULT_SOCKET_PATH)
    args = ap.parse_args()
    reader, writer = await commlib.connect_socket(args.socket)
    await commlib.send_msg(writer, {"type": "hello", "name": args.name})
    await asyncio.gather(recv_loop(reader), send_loop(writer, args.name))


if __name__ == "__main__":
    asyncio.run(main())
