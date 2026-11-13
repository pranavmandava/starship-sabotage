"""Single-file communications library for Starship Sabotage.

Primitive: UNIX domain SOCK_STREAM sockets with asyncio (async I/O).
All messages are JSON + newline framing: one dict per line, UTF-8.

This file is the ONLY communications library. Both server.py and client.py
import it. API:
    DEFAULT_SOCKET_PATH : str
    encode_msg(msg) -> bytes
    decode_msg(line) -> dict
    send_msg(writer, msg) -> None (async)
    recv_msg(reader) -> dict (async, raises ConnectionError on EOF)
    connect_socket(path) -> (reader, writer) (async)
    peer_credentials(writer) -> dict | None (Linux SO_PEERCRED)
    serve_unix(path, handler, mode=0o777, gid=None) -> AsyncServer
        (async, removes stale socket, sets mode and optional group so
        distinct Linux users can connect)

Unicast = send_msg to one writer. Broadcast/multicast = loop over writers.
Concurrency = asyncio: one coroutine per client + background timer tasks.
"""

import asyncio
import json
import os
import socket
import struct

DEFAULT_SOCKET_PATH = "/tmp/starship.sock"
_UCRED_SIZE = struct.calcsize("3i")


def encode_msg(msg: dict) -> bytes:
    return (json.dumps(msg) + "\n").encode("utf-8")


def decode_msg(line: bytes) -> dict:
    return json.loads(line.decode("utf-8").strip())


async def send_msg(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write(encode_msg(msg))
    await writer.drain()


async def recv_msg(reader: asyncio.StreamReader) -> dict:
    line = await reader.readline()
    if not line:
        raise ConnectionError("peer closed connection")
    return decode_msg(line)


async def connect_socket(path: str = DEFAULT_SOCKET_PATH):
    reader, writer = await asyncio.open_unix_connection(path)
    return reader, writer


def peer_credentials(writer: asyncio.StreamWriter):
    """Return {'pid', 'uid', 'gid'} for the peer, or None if unavailable.

    Linux exposes the connecting process's credentials through SO_PEERCRED on
    a UNIX domain socket.  The server uses this to bind a player name to a
    Linux UID instead of trusting the name in the JSON hello message.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED_SIZE)
        pid, uid, gid = struct.unpack("3i", raw)
        return {"pid": pid, "uid": uid, "gid": gid}
    except (AttributeError, OSError, ValueError):
        return None


async def serve_unix(path: str, handler, mode: int = 0o777, gid=None):
    # Remove stale socket so restart works. Parent dir (/tmp) is world-writable.
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = await asyncio.start_unix_server(handler, path=path)
    # Default 0777 keeps the no-group multi-user demo working; the server still
    # authenticates peers with SO_PEERCRED.  Tighten with mode/gid when a
    # dedicated shared group exists.
    try:
        os.chmod(path, mode)
        if gid is not None:
            os.chown(path, -1, gid)
    except OSError:
        pass
    return server
