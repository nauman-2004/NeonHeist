"""
commlib.py -- Neon Heist communications library (SINGLE FILE).

Every byte exchanged between players goes through this file. Nothing else in
the project touches sockets directly.

Primitive: UNIX stream sockets + asyncio, with length-prefixed JSON frames.

Why this primitive (the one-paragraph version for your writeup):
    Named pipes (FIFOs) are unidirectional and force awkward open-ordering
    between reader and writer, and one pipe per conversation scales badly.
    A Unix stream socket is bidirectional, filesystem-addressed (so "each
    player is their own Linux user" maps onto file permissions for the
    socket), and one listening socket accepts many concurrent clients via
    accept(). asyncio multiplexes all of those connections plus game timers
    on a single event loop -- no threads, no locks around game state.

Wire format
-----------
Each frame: 4-byte big-endian length N, then N bytes of UTF-8 JSON.
Envelope: {"type":..., "from":..., "to":..., "group":..., "payload":...}
  * "to": "<player_id>"       -> unicast   (exactly one recipient)
  * "group": "crew" | "moles" -> multicast (every member of the group)
  * neither set                -> broadcast (every connected player)

Topology: star. Each player holds ONE socket, to the moderator. A player
never addresses another player directly: send_to() / send_group() /
broadcast() only set routing hints in the envelope, and the moderator's
GameServer fans the message out. The server stamps "from" itself from the
authenticated hello, so a player cannot spoof another player's identity.

Concurrency model
-----------------
  * Server: one asyncio task per accepted connection (each runs
    _handle_conn: read frames, dispatch to on_message), plus whatever tasks
    the game loop needs (timers, phases). Player registry mutations happen
    only inside event-loop tasks, so no thread locks are needed; a
    per-writer asyncio.Lock serializes concurrent frame writes to the same
    player so frames never interleave.
  * Client: the caller typically runs one task pumping recv_msg() while the
    main task handles user input -- an incoming broadcast (e.g. an alarm)
    can interrupt typing, which is the visible concurrency demo.
"""

import asyncio
import json
import os
import struct

_HEADER = struct.Struct("!I")      # 4-byte big-endian frame length
MAX_FRAME = 1 << 20                # 1 MiB -- sanity cap against junk frames


# ---------------------------------------------------------------------------
# Framing helpers (module-private; the classes below are the public API)
# ---------------------------------------------------------------------------

async def _send_frame(writer, obj):
    """Write one length-prefixed JSON frame."""
    data = json.dumps(obj).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError("frame too large: %d bytes" % len(data))
    # Single write() call => the whole frame lands in the transport buffer
    # atomically; concurrent tasks cannot interleave bytes inside a frame.
    writer.write(_HEADER.pack(len(data)) + data)
    await writer.drain()


async def _recv_frame(reader):
    """Read one frame; return the dict, or None if the peer closed cleanly."""
    try:
        hdr = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError:
        return None
    (n,) = _HEADER.unpack(hdr)
    if n > MAX_FRAME:
        raise ValueError("frame too large: %d bytes" % n)
    try:
        data = await reader.readexactly(n)
    except asyncio.IncompleteReadError:
        return None
    return json.loads(data.decode("utf-8"))


def _envelope(msg_type, payload, frm, to=None, group=None):
    return {
        "type": msg_type,
        "from": frm,
        "to": to,
        "group": group,
        "payload": payload or {},
    }


# ---------------------------------------------------------------------------
# Client side: one connection from a player to the moderator
# ---------------------------------------------------------------------------

class PlayerConn:
    """A player's end of the star topology. One socket, to the moderator."""

    def __init__(self, reader, writer, player_id):
        self._reader = reader
        self._writer = writer
        self.player_id = player_id

    @classmethod
    async def connect(cls, sock_path, player_id):
        """Connect and perform the hello handshake. Raises on failure."""
        reader, writer = await asyncio.open_unix_connection(sock_path)
        conn = cls(reader, writer, player_id)
        await conn._send(_envelope(
            "hello", {"player_id": player_id}, player_id))
        return conn

    async def _send(self, msg):
        await _send_frame(self._writer, msg)

    async def send(self, msg_type, payload=None, to=None, group=None):
        """Send one message toward the moderator.

        to="some_player" routes as unicast, group="crew"/"moles" as
        multicast; with neither set the moderator broadcasts it.
        """
        await self._send(_envelope(msg_type, payload, self.player_id,
                                   to=to, group=group))

    async def send_to(self, to_id, msg_type, payload=None):
        """Unicast, relayed by the moderator to exactly one player."""
        await self.send(msg_type, payload, to=to_id)

    async def send_group(self, group, msg_type, payload=None):
        """Multicast, relayed by the moderator to every group member."""
        await self.send(msg_type, payload, group=group)

    async def broadcast(self, msg_type, payload=None):
        """Broadcast, relayed by the moderator to every player."""
        await self.send(msg_type, payload)

    async def recv_msg(self):
        """Next incoming message as a dict, or None on clean disconnect."""
        return await _recv_frame(self._reader)

    async def close(self):
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Server side: the moderator's relay. Owns the player registry and groups.
# ---------------------------------------------------------------------------

class GameServer:
    """Accepts player connections and fans messages out.

    Routing is deliberately dumb on purpose: unicast = one writer lookup,
    multicast = loop over the group's writers, broadcast = loop over all
    writers. The game logic (who may talk to whom) lives in moderator.py;
    this class only moves frames.
    """

    def __init__(self):
        self._players = {}   # player_id -> StreamWriter
        self._locks = {}     # player_id -> asyncio.Lock (serializes writes)
        self._groups = {}    # group name -> set(player_id)
        self._server = None
        self.on_join = None   # optional callback(pid), may be a coroutine
        self.on_leave = None  # optional callback(pid), may be a coroutine

    # -- registry -----------------------------------------------------------
    def player_ids(self):
        return list(self._players)

    def add_to_group(self, pid, group):
        self._groups.setdefault(group, set()).add(pid)

    def remove_from_group(self, pid, group):
        self._groups.get(group, set()).discard(pid)

    def group_members(self, group):
        return sorted(self._groups.get(group, ()))

    # -- sending --------------------------------------------------------------
    async def _send_to_writer(self, pid, msg):
        writer = self._players.get(pid)
        if writer is None:
            return False
        # Concurrent game tasks may target the same player at once (e.g. a
        # broadcast racing a unicast ack). The lock keeps whole frames
        # sequential so a receiver never sees half of two messages spliced.
        async with self._locks[pid]:
            try:
                await _send_frame(writer, msg)
                return True
            except (ConnectionError, OSError):
                return False

    async def unicast(self, to_id, msg_type, payload=None, frm="moderator"):
        """Exactly one recipient. Returns False if that player is gone."""
        return await self._send_to_writer(
            to_id, _envelope(msg_type, payload, frm, to=to_id))

    async def multicast(self, group, msg_type, payload=None, frm="moderator"):
        """Every member of `group`. Returns the recipients actually reached."""
        sent = []
        for pid in self.group_members(group):
            if await self._send_to_writer(
                    pid, _envelope(msg_type, payload, frm, group=group)):
                sent.append(pid)
        return sent

    async def broadcast(self, msg_type, payload=None, frm="moderator"):
        """Every connected player. Returns the recipients actually reached."""
        sent = []
        for pid in self.player_ids():
            if await self._send_to_writer(
                    pid, _envelope(msg_type, payload, frm)):
                sent.append(pid)
        return sent

    # -- accepting ------------------------------------------------------------
    async def _handle_conn(self, reader, writer, on_message):
        pid = None
        registered = False  # True only once THIS connection owns the pid
        try:
            hello = await _recv_frame(reader)
            if not hello or hello.get("type") != "hello":
                writer.close()
                return
            pid = str(hello.get("payload", {}).get("player_id")
                      or hello.get("from") or "anon")
            if pid in self._players:
                # Duplicate id: reject WITHOUT disturbing the existing player.
                await _send_frame(
                    writer, _envelope(
                        "error",
                        {"text": "player_id '%s' is already taken" % pid},
                        "moderator", to=pid))
                writer.close()
                return
            self._players[pid] = writer
            self._locks[pid] = asyncio.Lock()
            registered = True
            if self.on_join:
                res = self.on_join(pid)
                if asyncio.iscoroutine(res):
                    await res
            # One task per connection: pump frames and dispatch. The game
            # loop runs as separate task(s); all three mutate shared state
            # only inside the event loop, so no thread locks are needed.
            while True:
                msg = await _recv_frame(reader)
                if msg is None:      # clean disconnect
                    break
                # Authenticated identity: stamp "from" server-side so a
                # client cannot spoof another player's id.
                msg["from"] = pid
                res = on_message(self, pid, msg)
                if asyncio.iscoroutine(res):
                    await res
        finally:
            if registered:
                del self._players[pid]
                del self._locks[pid]
                for members in self._groups.values():
                    members.discard(pid)
                if self.on_leave:
                    res = self.on_leave(pid)
                    if asyncio.iscoroutine(res):
                        await res
            try:
                writer.close()
            except Exception:
                pass

    async def serve(self, sock_path, on_message):
        """Listen forever. Cancel the task to shut down."""
        try:
            os.unlink(sock_path)   # stale socket from a previous run
        except FileNotFoundError:
            pass
        async def _cb(reader, writer):
            await self._handle_conn(reader, writer, on_message)
        self._server = await asyncio.start_unix_server(_cb, path=sock_path)
        async with self._server:
            await self._server.serve_forever()
