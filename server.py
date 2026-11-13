"""Starship Sabotage server. Authoritative game state, asyncio + UNIX sockets.

Run on Ubuntu VM (not macOS test): python3 server.py [--socket PATH] [--players N]
Game: 1 impostor vs (N-1) crew. Shared resources O2 / Reactor / Tasks.
See README.md for protocol and rules.

Concurrency model:
  * one reader coroutine per client (handle_client)
  * one writer coroutine per client (sender_task), fed by a bounded queue
  * background timers: meeting_timeout(), leak_task()
  * one asyncio.Lock guards STATE; no socket I/O is awaited while it is held
"""

import argparse
import asyncio
import random

import commlib

# Bounded per-client outbox. If a peer stops reading, its queue fills and the
# connection is dropped instead of stalling the game.
SEND_QUEUE_MAX = 256

# ---- Game state (single process, guarded by asyncio.Lock) ----
STATE = {
    "phase": "LOBBY",  # LOBBY -> TASK <-> MEETING -> OVER
    "turn": 0,
    "o2": 100,
    "reactor": 100,
    "tasks": 0,
    "tasks_goal": 20,
    # name -> {"writer", "role", "alive", "queue", "sender"}
    "players": {},
    "votes": {},  # voter -> target (per meeting)
    "meeting_reason": "",
    "winner": "",
    "win_reason": "",
    "kill_cooldown": 2,   # global turns between kills
    "sab_cooldown": 2,    # global turns between sabotages
    "last_kill_turn": -10,
    "last_sab_turn": -10,
    "expected_players": 5,
    "meeting_secs": 60,
}
LOCK = asyncio.Lock()
MEETING_TIMER = None


def alive_names():
    return [n for n, p in STATE["players"].items() if p["alive"]]


def impostors_alive():
    return [n for n, p in STATE["players"].items() if p["alive"] and p["role"] == "impostor"]


def crew_alive():
    return [n for n, p in STATE["players"].items() if p["alive"] and p["role"] == "crew"]


# ---- Outbound messaging -------------------------------------------------
# All sends are queue-only: these helpers never wait on a socket, so callers
# may run them while holding LOCK.

async def sender_task(writer, queue):
    """Drain one client's outbox. A blocked drain stalls only this client."""
    try:
        while True:
            msg = await queue.get()
            await commlib.send_msg(writer, msg)
    except (ConnectionError, BrokenPipeError, asyncio.CancelledError):
        pass
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def enqueue(name, msg):
    p = STATE["players"].get(name)
    if p is None:
        return
    try:
        p["queue"].put_nowait(msg)
    except asyncio.QueueFull:
        # Peer stopped reading: drop it, do not block the game.
        try:
            p["writer"].transport.abort()
        except Exception:
            pass


def unicast(name, msg):
    enqueue(name, msg)


def broadcast(msg, exclude=()):
    for name in list(STATE["players"]):
        if name not in exclude:
            enqueue(name, msg)


def broadcast_state():
    snap = {
        "type": "state",
        "phase": STATE["phase"],
        "turn": STATE["turn"],
        "o2": STATE["o2"],
        "reactor": STATE["reactor"],
        "tasks": STATE["tasks"],
        "tasks_goal": STATE["tasks_goal"],
        "alive": alive_names(),
        "players": {n: {"alive": p["alive"]} for n, p in STATE["players"].items()},
        # roles NOT included; each client learns its own role at join.
    }
    broadcast(snap)


# ---- State transitions (all synchronous; caller holds LOCK) -------------

def check_win_locked():
    """Call with LOCK held. Sets OVER + queues game_over. Returns True if over."""
    if STATE["phase"] == "OVER":
        return True
    if STATE["tasks"] >= STATE["tasks_goal"]:
        STATE["phase"] = "OVER"
        STATE["winner"] = "crew"
        STATE["win_reason"] = "all tasks complete"
    elif STATE["o2"] <= 0:
        STATE["phase"] = "OVER"
        STATE["winner"] = "impostor"
        STATE["win_reason"] = "O2 depleted"
    elif STATE["reactor"] <= 0:
        STATE["phase"] = "OVER"
        STATE["winner"] = "impostor"
        STATE["win_reason"] = "reactor destroyed"
    elif not impostors_alive() and STATE["phase"] != "LOBBY":
        STATE["phase"] = "OVER"
        STATE["winner"] = "crew"
        STATE["win_reason"] = "impostor ejected"
    elif impostors_alive() and len(crew_alive()) <= len(impostors_alive()) and STATE["phase"] != "LOBBY":
        STATE["phase"] = "OVER"
        STATE["winner"] = "impostor"
        STATE["win_reason"] = "parity with crew"
    if STATE["phase"] == "OVER":
        broadcast({"type": "game_over", "winner": STATE["winner"], "reason": STATE["win_reason"]})
        broadcast_state()
        return True
    return False


def start_meeting_locked(reason):
    global MEETING_TIMER
    if STATE["phase"] != "TASK":
        return False
    STATE["phase"] = "MEETING"
    STATE["votes"] = {}
    STATE["meeting_reason"] = reason
    broadcast({"type": "meeting_start", "reason": reason, "secs": STATE["meeting_secs"]})
    broadcast_state()
    if MEETING_TIMER:
        MEETING_TIMER.cancel()
    MEETING_TIMER = asyncio.create_task(meeting_timeout())
    return True


async def meeting_timeout():
    await asyncio.sleep(STATE["meeting_secs"])
    async with LOCK:
        if STATE["phase"] == "MEETING":
            end_meeting_locked()


def end_meeting_locked():
    global MEETING_TIMER
    tally = {}
    for voter, target in STATE["votes"].items():
        if voter in alive_names() and target in alive_names():
            tally[target] = tally.get(target, 0) + 1
    ejected = ""
    if tally:
        top = max(tally.values())
        leaders = [n for n, v in tally.items() if v == top]
        if len(leaders) == 1:
            ejected = leaders[0]
            STATE["players"][ejected]["alive"] = False
    STATE["phase"] = "TASK"
    STATE["votes"] = {}
    # Do not cancel ourselves when called from meeting_timeout().
    if MEETING_TIMER and MEETING_TIMER is not asyncio.current_task():
        MEETING_TIMER.cancel()
    MEETING_TIMER = None
    broadcast({"type": "meeting_end", "ejected": ejected, "tally": tally})
    broadcast_state()
    check_win_locked()


async def leak_task():
    """Background concurrency demo: slow O2 leak during TASK phase."""
    while True:
        await asyncio.sleep(15)
        async with LOCK:
            if STATE["phase"] == "TASK" and STATE["players"]:
                STATE["o2"] = max(0, STATE["o2"] - 1)
                if STATE["o2"] <= 0:
                    check_win_locked()
                else:
                    broadcast_state()


async def handle_action(name, msg):
    async with LOCK:
        p = STATE["players"].get(name)
        if p is None or STATE["phase"] == "OVER":
            return
        if STATE["phase"] == "LOBBY":
            if msg.get("type") == "chat":
                broadcast({"type": "event", "text": f"{name}: {msg.get('text','')[:300]}"})
                return
            unicast(name, {"type": "result", "ok": False, "reason": "waiting for players"})
            return
        if not p["alive"]:
            unicast(name, {"type": "result", "ok": False, "reason": "you are dead"})
            return

        mtype = msg.get("type")

        if mtype == "chat":
            broadcast({"type": "event", "text": f"{name}: {msg.get('text','')[:300]}"})
            return

        if STATE["phase"] == "MEETING":
            if mtype == "vote":
                target = msg.get("target", "")
                if target not in alive_names():
                    unicast(name, {"type": "result", "ok": False, "reason": "invalid vote target"})
                    return
                STATE["votes"][name] = target
                unicast(name, {"type": "result", "ok": True, "reason": f"voted {target}"})
                if set(STATE["votes"]) >= set(alive_names()):
                    end_meeting_locked()
            else:
                unicast(name, {"type": "result", "ok": False, "reason": "meeting in progress, vote or chat"})
            return

        # ---- TASK phase actions ----
        if mtype == "action":
            act = msg.get("action")
            if act == "do-task":
                if p["role"] == "impostor":
                    # fake-task: looks the same, no progress (bluffing)
                    STATE["turn"] += 1
                    unicast(name, {"type": "result", "ok": True, "reason": "task done (fake)"})
                    broadcast({"type": "event", "text": f"{name} did a task."})
                else:
                    STATE["tasks"] = min(STATE["tasks_goal"], STATE["tasks"] + 1)
                    STATE["turn"] += 1
                    unicast(name, {"type": "result", "ok": True,
                                   "reason": f"tasks {STATE['tasks']}/{STATE['tasks_goal']}"})
                    broadcast({"type": "event",
                               "text": f"{name} did a task ({STATE['tasks']}/{STATE['tasks_goal']})."})
                broadcast_state()
                check_win_locked()
            elif act in ("fix-o2", "fix-reactor"):
                key = "o2" if act == "fix-o2" else "reactor"
                STATE[key] = min(100, STATE[key] + 15)
                STATE["turn"] += 1
                unicast(name, {"type": "result", "ok": True, "reason": f"{key} now {STATE[key]}"})
                broadcast({"type": "event", "text": f"{name} fixed {key} ({STATE[key]}%)."})
                broadcast_state()
            elif act == "kill":
                target = msg.get("target", "")
                if p["role"] != "impostor":
                    unicast(name, {"type": "result", "ok": False, "reason": "only impostor can kill"})
                    return
                if target not in crew_alive():
                    unicast(name, {"type": "result", "ok": False, "reason": "target must be living crew"})
                    return
                if STATE["turn"] - STATE["last_kill_turn"] < STATE["kill_cooldown"]:
                    left = STATE["kill_cooldown"] - (STATE["turn"] - STATE["last_kill_turn"])
                    unicast(name, {"type": "result", "ok": False, "reason": f"kill cooldown: {left} turn(s) left"})
                    return
                STATE["last_kill_turn"] = STATE["turn"]
                STATE["turn"] += 1
                STATE["players"][target]["alive"] = False
                unicast(name, {"type": "result", "ok": True, "reason": f"killed {target}"})
                # Body NOT auto-announced: found via report/meeting (impostor cover)
                broadcast({"type": "event", "text": f"{name} was seen near {target}..."})
                broadcast_state()
                check_win_locked()
            elif act == "sabotage":
                sys = msg.get("system", "o2")
                if p["role"] != "impostor":
                    unicast(name, {"type": "result", "ok": False, "reason": "only impostor can sabotage"})
                    return
                if sys not in ("o2", "reactor"):
                    unicast(name, {"type": "result", "ok": False, "reason": "sabotage o2|reactor"})
                    return
                if STATE["turn"] - STATE["last_sab_turn"] < STATE["sab_cooldown"]:
                    left = STATE["sab_cooldown"] - (STATE["turn"] - STATE["last_sab_turn"])
                    unicast(name, {"type": "result", "ok": False, "reason": f"sabotage cooldown: {left} turn(s) left"})
                    return
                STATE["last_sab_turn"] = STATE["turn"]
                STATE["turn"] += 1
                STATE[sys] = max(0, STATE[sys] - 20)
                unicast(name, {"type": "result", "ok": True, "reason": f"sabotaged {sys} ({STATE[sys]}%)"})
                broadcast({"type": "event", "text": f"!!! {sys.upper()} SABOTAGED ({STATE[sys]}%) !!! Fix it!"})
                broadcast_state()
                check_win_locked()
            else:
                unicast(name, {"type": "result", "ok": False, "reason": "unknown action"})
        elif mtype == "call-meeting":
            if not start_meeting_locked(f"{name} called a meeting"):
                unicast(name, {"type": "result", "ok": False, "reason": "cannot start meeting now"})
        elif mtype == "report":
            target = msg.get("target", "")
            if not start_meeting_locked(f"{name} reported {target}'s body"):
                unicast(name, {"type": "result", "ok": False, "reason": "cannot start meeting now"})
        else:
            unicast(name, {"type": "result", "ok": False, "reason": "unknown message type"})


async def reject_connection(writer, reason):
    """Send one error and close. Not under LOCK and bounded by a timeout."""
    try:
        await asyncio.wait_for(
            commlib.send_msg(writer, {"type": "result", "ok": False, "reason": reason}),
            timeout=2,
        )
    except (asyncio.TimeoutError, ConnectionError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_client(reader, writer):
    name = ""
    sender = None
    joined = False
    try:
        hello = await commlib.recv_msg(reader)
        name = str(hello.get("name", ""))[:20]
        if hello.get("type") != "hello" or not name:
            await reject_connection(writer, "first msg must be hello+name")
            return

        reason = ""
        async with LOCK:
            # The lobby closes permanently once the game starts.
            if STATE["phase"] != "LOBBY":
                reason = "game already in progress" if STATE["phase"] != "OVER" else "game over"
            elif name in STATE["players"]:
                reason = "name taken"
            elif len(STATE["players"]) >= STATE["expected_players"]:
                reason = "lobby is full"
            else:
                queue = asyncio.Queue(maxsize=SEND_QUEUE_MAX)
                STATE["players"][name] = {
                    "writer": writer, "role": "crew", "alive": True,
                    "queue": queue, "sender": None,
                }
                joined = True
                sender = asyncio.create_task(sender_task(writer, queue))
                STATE["players"][name]["sender"] = sender

                # Assign roles once the lobby fills.
                if len(STATE["players"]) == STATE["expected_players"]:
                    names = list(STATE["players"])
                    random.shuffle(names)
                    STATE["players"][names[0]]["role"] = "impostor"
                    STATE["phase"] = "TASK"
                    for n, pl in STATE["players"].items():
                        unicast(n, {"type": "welcome", "name": n, "role": pl["role"]})
                    broadcast({"type": "event",
                               "text": f"Game start! {STATE['expected_players']} players. "
                                       f"Tasks to {STATE['tasks_goal']}. Discuss and vote!"})
                    broadcast_state()
                else:
                    unicast(name, {"type": "welcome", "name": name, "role": "crew",
                                   "note": f"waiting {len(STATE['players'])}/{STATE['expected_players']}"})
                    broadcast({"type": "event",
                               "text": f"{name} joined ({len(STATE['players'])}/{STATE['expected_players']})"})

        if reason:
            await reject_connection(writer, reason)
            return

        # Main loop: one reader coroutine per client.
        while True:
            try:
                msg = await commlib.recv_msg(reader)
            except ValueError:
                unicast(name, {"type": "result", "ok": False, "reason": "malformed message"})
                continue
            await handle_action(name, msg)
    except (ConnectionError, asyncio.IncompleteReadError, ValueError):
        pass
    finally:
        if joined:
            async with LOCK:
                STATE["players"].pop(name, None)
                broadcast({"type": "event", "text": f"{name} disconnected."})
        if sender:
            sender.cancel()
        try:
            writer.close()
        except Exception:
            pass


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default=commlib.DEFAULT_SOCKET_PATH)
    ap.add_argument("--players", type=int, default=5)
    ap.add_argument("--tasks-goal", type=int, default=20)
    ap.add_argument("--meeting-secs", type=int, default=60)
    args = ap.parse_args()
    STATE["expected_players"] = args.players
    STATE["tasks_goal"] = args.tasks_goal
    STATE["meeting_secs"] = args.meeting_secs
    server = await commlib.serve_unix(args.socket, handle_client)
    print(f"serving on {args.socket}, waiting {args.players} players...")
    asyncio.create_task(leak_task())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
