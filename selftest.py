#!/usr/bin/env python3
"""
selftest.py -- end-to-end test of the real moderator.py + commlib.py.

Spawns moderator.py as a subprocess, connects scripted players over the
real Unix socket, and plays three scripted games:

  1. crew_wins : crew drills (one watches to hold the alert down), the mole
                 sabotages twice, then the crew votes the mole out.
  2. moles_win : same, but the crew votes out an innocent -> mole survives.
  3. stall     : nobody acts -> the alert maxes out -> moles win.

Also asserts the multicast rules during planning:
  * a moles-channel message reaches ONLY moles
  * a crew-channel message reaches everyone (moles are undercover)
  * a non-mole attempting the moles channel is rejected server-side

Run:  python3 selftest.py
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import time

import commlib

HERE = os.path.dirname(os.path.abspath(__file__))


class Bot:
    """Scripted player speaking real commlib frames."""

    def __init__(self, sock, pid):
        self.sock = sock
        self.pid = pid
        self.conn = None
        self.role = None
        self.inbox = asyncio.Queue()
        self.seen_chat = []    # (channel, from, text)
        self.rejections = 0    # server-side "not a mole" rejections

    async def start(self):
        self.conn = await commlib.PlayerConn.connect(self.sock, self.pid)
        asyncio.create_task(self._pump())

    async def _pump(self):
        while True:
            m = await self.conn.recv_msg()
            if m is None:
                break
            if m["type"] == "role_assign":
                self.role = m["payload"]["role"]
            elif m["type"] == "chat":
                p = m["payload"]
                self.seen_chat.append((p["channel"], p["from"], p["text"]))
            elif m["type"] == "action_result" and not m["payload"].get("ok", True):
                self.rejections += 1
            await self.inbox.put(m)

    async def wait_for(self, mtype, timeout=15):
        """Next queued message of type mtype (others stay queued)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("%s timed out waiting for %s" % (self.pid, mtype))
            m = await asyncio.wait_for(self.inbox.get(), remaining)
            if m["type"] == mtype:
                return m

    async def wait_for_any(self, mtypes, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("%s timed out waiting for %s" % (self.pid, mtypes))
            m = await asyncio.wait_for(self.inbox.get(), remaining)
            if m["type"] in mtypes:
                return m

    async def wait_for_phase(self, name, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            m = await self.wait_for("phase", timeout=max(0.5, deadline - time.monotonic()))
            if m["payload"]["name"] == name:
                return m

    async def close(self):
        await self.conn.close()


async def launch_game():
    """Start moderator.py; return (proc, sock, bots). Bots are connected and
    know their roles; the game is still in planning."""
    tmp = tempfile.mkdtemp(prefix="heist-test-")
    sock = os.path.join(tmp, "test.sock")
    proc = subprocess.Popen(
        [sys.executable, "moderator.py",
         "--sock", sock,
         "--min-players", "4", "--max-players", "4",
         "--lobby-timeout", "10", "--planning-time", "3",
         "--tick", "0.05", "--max-ticks", "25", "--vote-timeout", "5"],
        cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # Wait for the moderator to bind the socket (Popen returns immediately).
    deadline = time.monotonic() + 10
    while not os.path.exists(sock):
        if time.monotonic() > deadline:
            proc.terminate()
            raise TimeoutError("moderator never created the socket")
        if proc.poll() is not None:
            raise RuntimeError("moderator exited early:\n" + (proc.stdout.read() or ""))
        await asyncio.sleep(0.1)
    bots = {}
    try:
        for i in range(1, 5):
            b = Bot(sock, "p%d" % i)
            await b.start()
            bots["p%d" % i] = b
        for b in bots.values():
            await b.wait_for("role_assign")
        return proc, sock, bots
    except Exception:
        proc.terminate()
        raise


async def end_game(proc, bots, sock):
    for b in bots.values():
        try:
            await b.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    out = proc.stdout.read() if proc.stdout else ""
    try:
        os.unlink(sock)
    except OSError:
        pass
    if "Traceback" in out:
        print("  MODERATOR TRACEBACK:\n" + out)
        raise AssertionError("moderator crashed")


async def check_multicast(bots):
    """Planning-phase multicast rules."""
    moles = [b for b in bots.values() if b.role == "mole"]
    crew = [b for b in bots.values() if b.role != "mole"]
    assert len(moles) == 1, "expected 1 mole, got %s" % [b.role for b in bots.values()]

    mole, innocent = moles[0], crew[0]
    for b in bots.values():
        b.seen_chat.clear()

    # 1. mole whisper reaches ONLY moles
    await mole.conn.send_group("moles", "chat", {"text": "s3cr3t", "channel": "moles"})
    await asyncio.sleep(0.5)
    assert ("moles", mole.pid, "s3cr3t") in mole.seen_chat, "mole missed own whisper"
    for b in crew:
        assert not any(c == "moles" for c, _, _ in b.seen_chat), \
            "crew member %s saw the mole channel!" % b.pid

    # 2. crew chat reaches everyone (moles are undercover)
    await innocent.conn.send_group("crew", "chat", {"text": "hello crew", "channel": "crew"})
    await asyncio.sleep(0.5)
    for b in bots.values():
        assert ("crew", innocent.pid, "hello crew") in b.seen_chat, \
            "%s missed the crew message" % b.pid

    # 3. non-mole attempting the mole channel is rejected server-side
    before = innocent.rejections
    await innocent.conn.send_group("moles", "chat", {"text": "let me in", "channel": "moles"})
    await asyncio.sleep(0.5)
    assert innocent.rejections > before, "server did not reject non-mole channel use"
    print("  [ok] multicast: moles-only secrecy, crew broadcast, rejection")


async def play_execution(bots, sabotage_rounds=2):
    """Drill the vault to 100%. One crew member watches (holds the alert
    down); the mole sabotages early, then blends in by drilling.

    Returns the message that ended execution: the extraction "phase" or an
    early "game_over" (alert maxed out).
    """
    moles = [b for b in bots.values() if b.role == "mole"]
    crew = [b for b in bots.values() if b.role != "mole"]
    mole, watcher, drillers = moles[0], crew[0], crew[1:]

    await bots["p1"].wait_for_phase("execution")
    tick = 0
    while True:
        m = await bots["p1"].wait_for_any(("vault_update", "phase", "game_over"))
        if m["type"] != "vault_update":
            return m
        tick += 1
        for b in drillers:
            await b.conn.send("action", {"kind": "drill"})
        await watcher.conn.send("action", {"kind": "watch"})
        if tick <= sabotage_rounds:
            await mole.conn.send("action", {"kind": "sabotage"})
        else:
            await mole.conn.send("action", {"kind": "drill"})  # blend in


async def vote_and_finish(bots, target):
    await bots["p1"].wait_for("vote_request", timeout=15)
    for b in bots.values():
        await b.conn.send("vote", {"target": target})
    over = await bots["p1"].wait_for("game_over", timeout=15)
    return over["payload"]["winner"]


async def scenario(name, vote_target=None, sabotage_rounds=2, stall=False):
    print("scenario: %s" % name)
    proc, sock, bots = await launch_game()
    try:
        await check_multicast(bots)
        if stall:
            # Nobody acts: guard patrols max out the alert -> moles win.
            await bots["p1"].wait_for_phase("execution")
            over = await bots["p1"].wait_for("game_over", timeout=20)
            winner = over["payload"]["winner"]
        else:
            end = await play_execution(bots, sabotage_rounds)
            if end["type"] == "game_over":   # alert maxed out early
                winner = end["payload"]["winner"]
            else:                            # extraction phase -> vote
                moles = [b.pid for b in bots.values() if b.role == "mole"]
                winner = await vote_and_finish(bots, vote_target(bots, moles))
        print("  [ok] winner = %s" % winner)
        return winner
    finally:
        await end_game(proc, bots, sock)


async def main():
    w1 = await scenario("crew catches the mole",
                        vote_target=lambda bots, moles: moles[0])
    assert w1 == "crew", "expected crew win, got %s" % w1

    w2 = await scenario("crew votes out an innocent",
                        vote_target=lambda bots, moles:
                            next(p for p in bots if p not in moles))
    assert w2 == "moles", "expected moles win, got %s" % w2

    w3 = await scenario("stall: nobody acts", stall=True)
    assert w3 == "moles", "expected moles win on stall, got %s" % w3

    print("\nALL SELFTESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())