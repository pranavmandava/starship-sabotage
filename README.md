# Starship Sabotage — Among Us-like for CSE 536 Project 1

Text-based 5-player social-deduction game: **1 impostor vs 4 crew** on a damaged
starship. Crew complete tasks and repair sabotage; the impostor kills, sabotages
O2/Reactor, and lies in meetings. Only stdlib — **no external packages**.

## What the game is

Every player is a separate process, ideally a separate Linux user, talking to an
authoritative server over UNIX domain sockets. The server tracks three shared
resources — `O2`, `Reactor`, and `Tasks` — and a turn counter. Crew win by
finishing all tasks or ejecting the impostor; the impostor wins by destroying
O2/Reactor, reaching parity with the crew, or simply surviving the votes.
Bodies are not announced: a living player must `report` or call a `meeting`,
then everyone argues and votes.

## Quick start 
```bash
cd project-1
python3 server.py --players 5 --tasks-goal 20 --meeting-secs 60
```

In four more terminals (same machine/user is fine for a first test):

```bash
python3 client.py --name crew1
python3 client.py --name crew2
python3 client.py --name crew3
python3 client.py --name crew4
```

Using `uv` instead is optional:

```bash
uv run python server.py --players 5 --tasks-goal 20 --meeting-secs 60
uv run python client.py --name crew1
```

If you change `--socket`, pass the **same** `--socket` to every client.

## Multi-user setup (assignment requirement)

Each player should be its own Linux user, and each must be able to read the code
and connect to the socket. Home directories are usually `750`, so copy the
project somewhere world-readable first:

```bash
sudo cp -r "$PWD" /opt/starship-sabotage
sudo chmod -R a+rX /opt/starship-sabotage
```

(Alternative: `chmod o+x "$HOME"` grants traversal without making your home
directory listable.)

Create the five players once:

```bash
for u in wolf1 crew1 crew2 crew3 crew4; do sudo useradd -m "$u"; done
```

Terminal 1 — server (your user):

```bash
cd /opt/starship-sabotage
rm -f /tmp/starship.sock
python3 server.py --players 5 --tasks-goal 20 --meeting-secs 60
```

Terminals 2–6 — one per player, each as its own user:

```bash
sudo -u wolf1 -i
cd /opt/starship-sabotage
python3 client.py --name wolf1
```

Repeat for `crew1`…`crew4`. The socket is chmodded `0777` by `serve_unix()` so
all users can connect via `/tmp/starship.sock` (override with `--socket`).

## How to play

### Start everything with tmux

`play.sh` opens a tmux session with one window for the server and one per
player:

```bash
./play.sh                                         # all clients as you (demo)
./play.sh --users "wolf1 crew1 crew2 crew3 crew4" # one Linux user per client
./play.sh --names "ann bob cid"                   # 3-player game
./play.sh --detach                                # background, no attach
./play.sh --stop                                  # kill session + socket
./play.sh --force                                 # replace a server on the socket
```

`--users` requires the code to be readable by those users (see the multi-user
setup above); the script checks this and tells you how to fix it. Switch windows
with `Ctrl-b n` / `Ctrl-b p`, or `Ctrl-b 0` for the server.

### By hand

1. **Lobby.** Start the server, then connect the clients. The game begins
   automatically when `--players N` people have joined (default 5). Roles are
   assigned randomly at that moment; your client prints
   `[you are NAME | role=crew|impostor]`. The last `welcome` line is
   authoritative — earlier lobby lines are just "waiting n/5".
2. **Task phase.** Type one command per turn. Crew do `task` and repair
   sabotage; the impostor pretends to do tasks, kills, and sabotages.
3. **Meetings.** Anyone alive can `meeting`, or `report <name>` over a body.
   During a meeting, type `vote <name>` (and `chat <text>` to argue). The
   meeting ends when every living player has voted or the timer expires.
4. **Result.** A unique top vote is ejected (role is not revealed). A tie means
   nobody is ejected. Play continues until a win condition fires.

### Commands

| Command | Who | Effect |
| --- | --- | --- |
| `task` (or `t`) | everyone | Crew: tasks +1. Impostor: fake task, no progress. |
| `fix o2` | everyone | O2 +15 (max 100). |
| `fix reactor` | everyone | Reactor +15 (max 100). |
| `kill <name>` | impostor | Kill a living crew member. 2-turn cooldown. |
| `sabotage o2\|reactor` | impostor | Drain that system by 20. 2-turn cooldown. |
| `meeting` | alive | Start a vote. |
| `report <name>` | alive | Start a vote over a body. |
| `vote <name>` | alive, meeting | Cast your vote. |
| `chat <text>` | alive (also lobby) | Broadcast a message. |
| `help`, `quit` | local | Show commands / exit. |

Anything unrecognised is sent as chat. Every action advances the turn counter,
which is what the cooldowns count against.

## Rules

- Shared: `O2 100%`, `Reactor 100%`, `Tasks 0/20`.
- Crew win: all tasks complete, or the impostor is ejected.
- Impostor wins: O2 or Reactor hits 0, or living crew ≤ living impostors.
- O2 also leaks `-1` every 15 seconds during the task phase.
- Meeting length is `--meeting-secs` (default 60), or ends early once all living
  players have voted. Tie = no eject.
- Bodies are not auto-announced; use `report`/`meeting`.
- 3+ players recommended. With 2 players, parity ends the game immediately.

## Files

- `commlib.py` — **communications library (single file)**. UNIX domain
  `SOCK_STREAM` + `asyncio`. JSON-lines framing. API: `send_msg(writer, dict)`,
  `recv_msg(reader) -> dict`, `connect_socket(path)`, `serve_unix(path, handler)`,
  `encode_msg/decode_msg`, `DEFAULT_SOCKET_PATH`.
- `server.py` — authoritative state, lobby, task/meeting/over phases, win checks,
  cooldowns.
- `client.py` — one process per player. Line commands, async recv+send.
- `play.sh` — tmux launcher: server + one client per player, single- or multi-user.
- `pyproject.toml` — uv packaging metadata (stdlib only, Python >=3.10).

## Comms primitive

UNIX domain stream sockets with async I/O (`asyncio.start_unix_server` /
`open_unix_connection`). Unicast: private `welcome` (role), `result`
(ack/failure). Broadcast: `state`, `event` (sabotage/chat/join), `meeting_start`,
`meeting_end`, `game_over`. Concurrency: one coroutine per client plus
`meeting_timeout()` and `leak_task()` (O2 −1/15s), all mutating shared state
under a single `asyncio.Lock`.
