# Starship Sabotage

Text-based five-player social-deduction game for CSE 536 Projects 1–3. One
impostor versus four crew on a damaged starship: the crew complete tasks and
repair O2/Reactor, the impostor kills, sabotages, and lies in meetings. Each
player is a separate process (ideally a separate Linux user) talking to an
authoritative server.

For Projects 2/3, every player also runs in its own **cgroup v2** leaf with hard
memory, PID, and CPU limits. The server stays outside the player cgroup subtree,
so one hostile player cannot OOM or fork-bomb the game server.

## Quick start

Requirements: Linux, Python ≥ 3.10, `tmux`, `sudo`, and cgroup v2
(`stat -fc %T /sys/fs/cgroup` should print `cgroup2fs`).

```bash
cd project-1
./play.sh                  # isolated by default (re-execs sudo)
./play.sh --no-isolate     # old Project 1 style, no cgroups
```

`play.sh` opens a tmux session: the server in window `0` and one client per
player. Switch windows with `Ctrl-b n` / `Ctrl-b p`; stop with `./play.sh --stop`.

Three-player demo:

```bash
./play.sh --names "ann bob cid"
```

One Linux user per player:

```bash
sudo cp -r "$PWD" /opt/starship-sabotage
sudo chmod -R a+rX /opt/starship-sabotage
cd /opt/starship-sabotage
./play.sh --users "wolf1 crew1 crew2 crew3 crew4"
```

The game starts automatically when the expected number of players joins. If
`--users` is omitted, all clients run as you.

## Isolation limits

Defaults per player:

| Limit | Value | Stops |
| --- | --- | --- |
| `memory.max` | `64M` | memory bombs / host OOM |
| `memory.swap.max` | `0` | swapping around the memory cap |
| `pids.max` | `24` | fork bombs |
| `cpu.max` | `50%` | CPU hogs |
| `memory.oom.group` | `1` | killing the whole offending player tree |

The parent cgroup has an aggregate cap (`players × per-player limit`). The
server runs outside it as the invoking user; player processes drop back to their
own UID before `exec`.

```bash
./play.sh --memory 32M --pids 12 --cpu 25        # tune limits
sudo python3 cgroup.py show --parent starship-cgroup --table
```

Writing cgroup files needs root; `./play.sh` re-execs through `sudo`. If your
sudo requires a password interactively, run `sudo ./play.sh`. Cgroups constrain
only the process tree the launcher starts; a manually started unconfined process
is outside the threat model. Run one isolated game at a time.

### Connection security

The server does not trust the name in the JSON hello message. On Linux it reads
the peer's UID with `SO_PEERCRED`, and in `--users` mode it passes an
`--allow-users` list so each connection must come from one of the expected
Linux users and the claimed name must match that user. A name is also bound to
the first UID that claimed it, so a different local user cannot reuse it after a
disconnect. Clients must send a valid hello within 5 seconds or the connection
is closed.

The socket itself defaults to mode `0777` so distinct users can connect without
a shared group; unlisted UIDs are rejected at the application layer. If you
create a dedicated group for the players, tighten the filesystem permissions
too:

```bash
./play.sh --users "wolf1 crew1 crew2 crew3 crew4" \
    --socket-mode 0660 --socket-group starship
```

## Commands

Type these in a client window:

| Command | Who | Effect |
| --- | --- | --- |
| `task` | everyone | Crew: +1 task. Impostor: fake task. |
| `fix o2` / `fix reactor` | everyone | Repair that system (+15, max 100). |
| `kill <name>` | impostor | Kill a living crew member (2-turn cooldown). |
| `sabotage o2\|reactor` | impostor | Drain that system by 20 (2-turn cooldown). |
| `meeting` / `report <name>` | alive | Start a vote. |
| `vote <name>` | alive, meeting | Cast your vote. |
| `chat <text>` | alive | Broadcast a message. |
| `help`, `quit` | local | Show commands / exit. |

Crew win by completing all tasks or ejecting the impostor. The impostor wins if
O2 or Reactor hits 0, living crew ≤ living impostors, or the crew never ejects
them. A tie vote ejects nobody. Bodies are not auto-announced.

## Testing and attack demo

```bash
sudo python3 test_isolation.py --game
sudo python3 test_security.py
```

`test_isolation.py` asserts on kernel counters (`memory.peak`, `pids.peak`,
`cpu.stat`, `memory.events`) and runs a real five-player game: four bots plus a
hostile attacker that is OOM-killed inside its own leaf while the server stays
alive. `test_security.py` checks peer-UID authorization, name binding, the hello
timeout, and socket mode.

Manual attack demo:

```bash
./play.sh --names "ann bob cid dex" --expected 5 --detach

sudo python3 cglaunch.py --name attacker --user "$USER" \
    --memory 32M --pids 8 --cpu 25 \
    -- python3 attack.py --name attacker --socket /tmp/starship.sock \
    --attack memory --size-mb 128
```

Other attack modes: `fork`, `cpu`, `flood`, `oversize`, `malformed`, `idle`
(`python3 attack.py --help`).

## Files and communications API

The communications library is **`commlib.py`**, a single file. It uses UNIX
domain `SOCK_STREAM` sockets with `asyncio` and JSON-lines framing:

- `encode_msg(msg) -> bytes` / `decode_msg(line) -> dict`
- `send_msg(writer, msg)` / `recv_msg(reader) -> dict`
- `connect_socket(path)` / `serve_unix(path, handler, mode=0o777, gid=None)`
- `peer_credentials(writer) -> dict | None` — Linux `SO_PEERCRED` (pid/uid/gid)
- `DEFAULT_SOCKET_PATH`

Unicast messages are the private `welcome` (role) and `result`; broadcast
messages are `state`, `event`, `meeting_start`, `meeting_end`, and `game_over`.
The server fans broadcasts out through bounded per-client queues, with one
reader and one writer coroutine per client plus meeting/O2 timer tasks.

Other files:

- `server.py` — authoritative state, lobby, phases, win checks.
- `client.py` — per-player terminal client.
- `play.sh` — tmux launcher; isolation on by default.
- `cgroup.py` — cgroup v2 library: parent/leaf setup, limits, stats, cleanup.
- `cglaunch.py` — run one player in a cgroup, then drop to its Linux user.
- `attack.py` — memory/fork/CPU/flood/oversize attacks.
- `bot.py` — auto-player used by the end-to-end test.
- `test_isolation.py` — kernel-counter tests and the five-player survival game.
- `test_security.py` — peer-UID, name-binding, hello-timeout, malformed-input, and socket-mode tests.
- `pyproject.toml` — packaging metadata (stdlib only).

The report, poster, and submission tarball are course deliverables kept local;
they are not required to run or test the game.
