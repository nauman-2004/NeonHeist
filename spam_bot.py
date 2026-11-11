#!/usr/bin/env python3
"""
spam_bot.py -- a deliberately misbehaving Neon Heist player, for testing
the isolation work in Projects 2/3.

Modes (pick one):
  flood : connect, then send chat frames as fast as possible. The
          moderator's per-player rate limiter should drop the excess and
          disconnect the bot after 50 dropped frames, while the game
          goes on for everyone else.
  burn  : connect, then burn CPU in a tight loop (one legal chat per
          second to prove it is alive). Run it inside a cgroup with
          CPUQuota=25% and watch its CPU% stay capped (measure_cpu.sh).
  hog   : connect, then allocate 1MB chunks in a loop until the cgroup's
          MemoryMax kills it (or --max-mb is reached). The moderator
          should see a clean disconnect and the game should go on.

Run:  python3 spam_bot.py --sock /tmp/neon-heist/heist.sock --id spam1
          --mode flood [--seconds 10]
"""

import argparse
import asyncio
import hashlib
import sys
import time

import commlib


async def flood(conn, seconds):
    """Send chat frames as fast as the loop allows.
    Returns (frames_sent, disconnected)."""
    sent = 0
    disconnected = asyncio.Event()

    async def pump():
        while True:
            m = await conn.recv_msg()
            if m is None:          # moderator closed the connection
                disconnected.set()
                return

    pump_task = asyncio.create_task(pump())
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end and not disconnected.is_set():
            await conn.send_group("crew", "chat",
                                  {"text": "spam " * 25, "channel": "crew"})
            sent += 1
    except (ConnectionError, OSError):
        disconnected.set()
    pump_task.cancel()
    return sent, disconnected.is_set()


async def burn(conn, seconds):
    """Burn CPU in a tight loop, one legal chat per second.
    Returns True if still connected at the end."""
    end = time.monotonic() + seconds
    last_chat = 0.0
    while time.monotonic() < end:
        # ~1-2ms of hashing, then yield so chats and the pump still run.
        for _ in range(2000):
            hashlib.sha256(b"neon heist" * 64).digest()
        await asyncio.sleep(0)
        now = time.monotonic()
        if now - last_chat >= 1.0:
            last_chat = now
            try:
                await conn.send_group("crew", "chat",
                                      {"text": "still here", "channel": "crew"})
            except (ConnectionError, OSError):
                return False
    return True


async def hog(conn, seconds, max_mb):
    """Allocate 1MB chunks until killed or the cap is reached.
    Returns MB allocated."""
    chunks = []
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end and len(chunks) < max_mb:
            chunks.append(bytearray(1024 * 1024))
            if len(chunks) % 16 == 0:
                print(f"[hog] allocated {len(chunks)} MB", flush=True)
            await asyncio.sleep(0.01)
    except (ConnectionError, OSError):
        pass
    return len(chunks)


async def main():
    ap = argparse.ArgumentParser(description="Neon Heist misbehaving player")
    ap.add_argument("--sock", default="/tmp/neon-heist.sock")
    ap.add_argument("--id", default="spam1")
    ap.add_argument("--mode", choices=["flood", "burn", "hog"], required=True)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--max-mb", type=int, default=512,
                    help="hog mode: stop after this many MB (safety cap)")
    args = ap.parse_args()

    conn = await commlib.PlayerConn.connect(args.sock, args.id)
    print(f"[spam] connected as {args.id}, mode={args.mode}", flush=True)
    try:
        if args.mode == "flood":
            t0 = time.monotonic()
            sent, disconnected = await flood(conn, args.seconds)
            dt = time.monotonic() - t0
            print(f"[flood] sent {sent} frames in {dt:.1f}s "
                  f"({sent / dt:.0f}/s), disconnected={disconnected}", flush=True)
        elif args.mode == "burn":
            alive = await burn(conn, args.seconds)
            print(f"[burn] ran {args.seconds:.0f}s, stayed connected={alive}", flush=True)
        else:
            mb = await hog(conn, args.seconds, args.max_mb)
            print(f"[hog] allocated {mb} MB then stopped", flush=True)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
