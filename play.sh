#!/usr/bin/env bash
# play.sh — launch Starship Sabotage (server + one client per player) in tmux.
#
#   ./play.sh                              # all clients as YOU (quick demo)
#   ./play.sh --users "wolf1 crew1 crew2 crew3 crew4"   # one Linux user each
#   ./play.sh --names "ann bob cid"        # 3-player game
#   ./play.sh --detach                     # start in background, don't attach
#   ./play.sh --stop                       # kill the session
#   ./play.sh --force                      # replace a server already on the socket
#
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
        --socket)      SOCKET="$2"; shift 2 ;;
        --tasks-goal)  TASKS_GOAL="$2"; shift 2 ;;
        --meeting-secs) MEETING_SECS="$2"; shift 2 ;;
        --detach)      DETACH=1; shift ;;
        --force)       FORCE=1; shift ;;
        --stop)
            tmux kill-session -t "$SESSION" 2>/dev/null || true
            for _ in $(seq 1 20); do
                socket_in_use || break
                sleep 0.1
            done
            if socket_in_use; then
                echo "play.sh: something is still listening on $SOCKET; left it alone" >&2
                exit 1
            fi
            rm -f "$SOCKET"
            echo "stopped session '$SESSION' and removed $SOCKET"
            exit 0 ;;
        -h|--help)     usage 0 ;;
        *) echo "play.sh: unknown argument: $1" >&2; usage 1 ;;
    esac
done

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

if socket_in_use && (( ! FORCE )); then
    echo "play.sh: a server is already listening on $SOCKET" >&2
    echo "  stop it first, use SOCKET=/tmp/other.sock, or pass --force" >&2
    exit 1
fi

tmux kill-session -t "$SESSION" 2>/dev/null || true
rm -f "$SOCKET"

TMUX_ARGS=(-d -s "$SESSION" -n server)
tmux new-session "${TMUX_ARGS[@]}"
tmux send-keys -t "$SESSION:server" \
    "python3 server.py --socket '$SOCKET' --players '$PLAYERS' --tasks-goal '$TASKS_GOAL' --meeting-secs '$MEETING_SECS'" C-m

for i in "${!NAMES[@]}"; do
    name="${NAMES[$i]}"
    tmux new-window -d -t "$SESSION" -n "$name"
    wait_for_socket="while [ ! -S '$SOCKET' ]; do sleep 0.1; done;"
    if [[ ${#USERS[@]} -gt 0 ]]; then
        # Client runs as the player's own Linux user; tmux still shows its TTY.
        tmux send-keys -t "$SESSION:$name" \
            "$wait_for_socket sudo -n -u '${USERS[$i]}' python3 client.py --name '$name' --socket '$SOCKET'" C-m
    else
        tmux send-keys -t "$SESSION:$name" \
            "$wait_for_socket python3 client.py --name '$name' --socket '$SOCKET'" C-m
    fi
done

tmux select-window -t "$SESSION:server"

if (( DETACH )); then
    echo "session '$SESSION' running: 1 server + $PLAYERS clients"
    echo "attach: tmux attach -t $SESSION    stop: ./play.sh --stop"
elif [[ -n "${TMUX:-}" ]]; then
    tmux switch-client -t "$SESSION"
else
    tmux attach-session -t "$SESSION"
fi
