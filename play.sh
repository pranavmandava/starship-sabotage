#!/usr/bin/env bash
# play.sh — launch Starship Sabotage (server + one client per player) in tmux.
#
#   ./play.sh                              # isolated by default (cgroup v2, sudo)
#   ./play.sh --no-isolate                 # Project 1 style, no cgroups
#   ./play.sh --users "wolf1 crew1 crew2 crew3 crew4"   # one Linux user each
#   ./play.sh --names "ann bob cid"        # 3-player game
#   ./play.sh --names "ann bob cid" --expected 5   # 3 clients, wait for 5
#   ./play.sh --isolate                    # explicit; already the default
#   ./play.sh --memory 32M --pids 12 --cpu 25   # tune isolation limits
#   ./play.sh --session other              # separate tmux session name
#   ./play.sh --detach                     # start in background, don't attach
#   ./play.sh --stop                       # kill the session (+ cgroups if root)
#   ./play.sh --force                      # replace a server already on the socket
#
# Isolation tuning: PLAYER_MEMORY=64M PLAYER_PIDS=24 PLAYER_CPU=50 ./play.sh
# Extra tuning: TASKS_GOAL=20 MEETING_SECS=60 ./play.sh
# Socket override: SOCKET=/tmp/other.sock ./play.sh

set -euo pipefail
cd "$(dirname "$0")"

SESSION="${SESSION:-starship}"
SOCKET="${SOCKET:-/tmp/starship.sock}"
TASKS_GOAL="${TASKS_GOAL:-20}"
MEETING_SECS="${MEETING_SECS:-60}"
NAMES=(wolf1 crew1 crew2 crew3 crew4)
USERS=()
DETACH=0
FORCE=0
ISOLATE=1
STOP=0
CG_PARENT="${CG_PARENT:-starship-cgroup}"
PLAYER_MEMORY="${PLAYER_MEMORY:-64M}"
PLAYER_SWAP="${PLAYER_SWAP:-0}"
PLAYER_PIDS="${PLAYER_PIDS:-24}"
PLAYER_CPU="${PLAYER_CPU:-50}"
EXPECTED=0
ORIG_ARGS=("$@")

# A root run (needed for isolation) uses a separate tmux server so it never
# collides with the user's own tmux sessions/socket.
TMUX_BIN=(tmux)
if (( EUID == 0 )); then
    TMUX_SOCKET="${TMUX_SOCKET:-/tmp/starship-tmux.sock}"
    TMUX_BIN=(tmux -S "$TMUX_SOCKET")
fi

socket_in_use() {
    [[ -S "$SOCKET" ]] || return 1
    python3 - "$SOCKET" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect(sys.argv[1])
except OSError:
    sys.exit(1)
PY
}

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --users)       read -r -a USERS <<< "${2:-}"; shift 2 ;;
        --names)       read -r -a NAMES <<< "${2:-}"; shift 2 ;;
        --expected|--server-players) EXPECTED="$2"; shift 2 ;;
        --socket)      SOCKET="$2"; shift 2 ;;
        --session)     SESSION="$2"; shift 2 ;;
        --tasks-goal)  TASKS_GOAL="$2"; shift 2 ;;
        --meeting-secs) MEETING_SECS="$2"; shift 2 ;;
        --detach)      DETACH=1; shift ;;
        --force)       FORCE=1; shift ;;
        --isolate|--cgroup) ISOLATE=1; shift ;;
        --no-isolate|--no-cgroup) ISOLATE=0; shift ;;
        --memory)      PLAYER_MEMORY="$2"; shift 2 ;;
        --swap)        PLAYER_SWAP="$2"; shift 2 ;;
        --pids)        PLAYER_PIDS="$2"; shift 2 ;;
        --cpu)         PLAYER_CPU="$2"; shift 2 ;;
        --stop)
            STOP=1; shift ;;
        -h|--help)     usage 0 ;;
        *) echo "play.sh: unknown argument: $1" >&2; usage 1 ;;
    esac
done

if (( STOP )); then
    # Isolated runs live on a root-owned tmux server.  Only re-exec if this
    # exact session is actually there; a plain user session may share the name.
    ROOT_SOCKET="${TMUX_SOCKET:-/tmp/starship-tmux.sock}"
    if (( EUID != 0 )) && [[ -S "$ROOT_SOCKET" ]] \
            && sudo -n true 2>/dev/null \
            && sudo tmux -S "$ROOT_SOCKET" has-session -t "$SESSION" 2>/dev/null; then
        exec sudo -- "$0" "${ORIG_ARGS[@]}"
    fi
    "${TMUX_BIN[@]}" kill-session -t "$SESSION" 2>/dev/null || true
    for _ in $(seq 1 20); do
        socket_in_use || break
        sleep 0.1
    done
    if socket_in_use; then
        echo "play.sh: something is still listening on $SOCKET; left it alone" >&2
        exit 1
    fi
    rm -f "$SOCKET"
    if (( EUID == 0 )); then
        python3 cgroup.py cleanup --parent "$CG_PARENT" >/dev/null 2>&1 || true
    elif sudo -n true 2>/dev/null; then
        sudo -n python3 cgroup.py cleanup --parent "$CG_PARENT" >/dev/null 2>&1 || true
    fi
    echo "stopped session '$SESSION' and removed $SOCKET"
    exit 0
fi

# Isolation is the default.  It needs root only for cgroup writes; the clients
# still drop to their own Linux user.  Re-exec through sudo when possible rather
# than requiring a password prompt inside each tmux window.
if (( ISOLATE )); then
    if (( EUID != 0 )); then
        if sudo -n true 2>/dev/null; then
            exec sudo -- "$0" "${ORIG_ARGS[@]}"
        fi
        echo "play.sh: cgroup isolation is the default and needs root" >&2
        echo "  run: sudo ./play.sh ...   (or pass --no-isolate for the old mode)" >&2
        exit 1
    fi
    if [[ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" != "cgroup2fs" ]]; then
        echo "play.sh: this host does not mount cgroup v2 at /sys/fs/cgroup" >&2
        exit 1
    fi
fi

RUN_USER="${SUDO_USER:-$(id -un)}"

command -v tmux >/dev/null || { echo "play.sh: tmux not installed (sudo apt install tmux)" >&2; exit 1; }
command -v python3 >/dev/null || { echo "play.sh: python3 not found" >&2; exit 1; }
[[ -f server.py && -f client.py ]] || { echo "play.sh: run from the project directory" >&2; exit 1; }

# --users gives one username per player; names default to those usernames.
if [[ ${#USERS[@]} -gt 0 ]]; then
    NAMES=("${USERS[@]}")
    for u in "${USERS[@]}"; do
        if ! id "$u" >/dev/null 2>&1; then
            echo "play.sh: no such user: $u  (sudo useradd -m $u)" >&2
            exit 1
        fi
        if ! sudo -n -u "$u" test -r "$PWD/server.py" 2>/dev/null; then
            echo "play.sh: $u cannot read $PWD/server.py" >&2
            echo "  sudo cp -r '$PWD' /opt/starship-sabotage && sudo chmod -R a+rX /opt/starship-sabotage" >&2
            echo "  then run this script from /opt/starship-sabotage" >&2
            exit 1
        fi
    done
fi

PLAYERS=${#NAMES[@]}
(( PLAYERS >= 3 )) || { echo "play.sh: need at least 3 players (2 ends instantly)" >&2; exit 1; }
EXPECTED="${EXPECTED:-0}"
if (( EXPECTED == 0 )); then
    EXPECTED=$PLAYERS
fi
(( EXPECTED >= PLAYERS )) || { echo "play.sh: --expected cannot be less than the number of clients" >&2; exit 1; }
(( EXPECTED >= 3 )) || { echo "play.sh: need at least 3 players (2 ends instantly)" >&2; exit 1; }

if socket_in_use && (( ! FORCE )); then
    echo "play.sh: a server is already listening on $SOCKET" >&2
    echo "  stop it first, use SOCKET=/tmp/other.sock, or pass --force" >&2
    exit 1
fi

if (( ISOLATE )); then
    STATS_DIR="${STATS_DIR:-/tmp/starship-cgroup-$SESSION}"
    rm -rf "$STATS_DIR"
    mkdir -p "$STATS_DIR"
    chmod 0777 "$STATS_DIR" 2>/dev/null || true
    if ! python3 cgroup.py init --parent "$CG_PARENT" \
            --players "$EXPECTED" --memory "$PLAYER_MEMORY" \
            --swap "$PLAYER_SWAP" --pids "$PLAYER_PIDS" \
            --cpu "$PLAYER_CPU" >/dev/null; then
        echo "play.sh: cgroup setup failed (need cgroup v2 + root)" >&2
        exit 1
    fi
    echo "isolation: parent=$CG_PARENT memory=$PLAYER_MEMORY swap=$PLAYER_SWAP" \
         "pids=$PLAYER_PIDS cpu=${PLAYER_CPU}% stats=$STATS_DIR"
fi

"${TMUX_BIN[@]}" kill-session -t "$SESSION" 2>/dev/null || true
rm -f "$SOCKET"

SERVER_CMD="python3 server.py --socket '$SOCKET' --players '$EXPECTED' --tasks-goal '$TASKS_GOAL' --meeting-secs '$MEETING_SECS'"
if (( EUID == 0 )) && [[ "$RUN_USER" != "root" ]]; then
    # The server stays outside the player cgroups and runs as the real user.
    SERVER_CMD="sudo -u '$RUN_USER' -- $SERVER_CMD"
fi

TMUX_ARGS=(-d -s "$SESSION" -n server)
"${TMUX_BIN[@]}" new-session "${TMUX_ARGS[@]}"
"${TMUX_BIN[@]}" send-keys -t "$SESSION:server" "$SERVER_CMD" C-m

for i in "${!NAMES[@]}"; do
    name="${NAMES[$i]}"
    "${TMUX_BIN[@]}" new-window -d -t "$SESSION" -n "$name"
    wait_for_socket="while [ ! -S '$SOCKET' ]; do sleep 0.1; done;"
    if (( ISOLATE )); then
        client_user="$RUN_USER"
        [[ ${#USERS[@]} -gt 0 ]] && client_user="${USERS[$i]}"
        CLIENT_CMD="python3 cglaunch.py --name '$name' --user '$client_user' --parent '$CG_PARENT' --memory '$PLAYER_MEMORY' --swap '$PLAYER_SWAP' --pids '$PLAYER_PIDS' --cpu '$PLAYER_CPU' --stats '$STATS_DIR/$name.json' --replace -- python3 client.py --name '$name' --socket '$SOCKET'"
        if (( EUID != 0 )); then
            CLIENT_CMD="sudo -n $CLIENT_CMD"
        fi
    elif [[ ${#USERS[@]} -gt 0 ]]; then
        # Client runs as the player's own Linux user; tmux still shows its TTY.
        CLIENT_CMD="sudo -n -u '${USERS[$i]}' python3 client.py --name '$name' --socket '$SOCKET'"
    else
        CLIENT_CMD="python3 client.py --name '$name' --socket '$SOCKET'"
    fi
    "${TMUX_BIN[@]}" send-keys -t "$SESSION:$name" "$wait_for_socket $CLIENT_CMD" C-m
done

"${TMUX_BIN[@]}" select-window -t "$SESSION:server"

if (( DETACH )); then
    echo "session '$SESSION' running: 1 server + $PLAYERS clients"
    if (( EUID == 0 )); then
        echo "attach: sudo tmux -S $TMUX_SOCKET attach -t $SESSION    stop: sudo $0 --stop"
    else
        echo "attach: tmux attach -t $SESSION    stop: ./play.sh --stop"
    fi
elif [[ -n "${TMUX:-}" ]] && (( EUID != 0 )); then
    "${TMUX_BIN[@]}" switch-client -t "$SESSION"
else
    "${TMUX_BIN[@]}" attach-session -t "$SESSION"
fi
