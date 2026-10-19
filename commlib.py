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
    serve_unix(path, handler) -> AsyncServer (async, removes stale socket,
        chmods socket to 0o777 so distinct Linux users can connect)

Unicast = send_msg to one writer. Broadcast/multicast = loop over writers.
Concurrency = asyncio: one coroutine per client + background timer tasks.
"""

import asyncio
import json
import os
import stat

DEFAULT_SOCKET_PATH = "/tmp/starship.sock"


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


async def serve_unix(path: str, handler):
    # Remove stale socket so restart works. Parent dir (/tmp) is world-writable.
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = await asyncio.start_unix_server(handler, path=path)
    # Allow distinct Linux users (each player = own user) to connect.
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass
    return server
