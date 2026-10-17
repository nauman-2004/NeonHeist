#!/usr/bin/env python3
"""
player.py -- Neon Heist terminal client.

Run:  python3 player.py [--sock PATH] [--id NAME]
      (defaults: /tmp/neon-heist.sock, $USER)

Concurrency demo: one asyncio task pumps incoming messages and prints them
immediately, while the main task blocks on input(). An alarm broadcast can
interrupt you mid-typing -- try it during execution.

Commands:
  say <text>            crew (public) channel -- moles hear it too, they're undercover
  mole <text>           secret mole channel (moles only; enforced server-side)
  all <text>            broadcast to everyone, labelled as public
  drill | watch | lift  crew actions during execution
  sabotage              mole action during execution (secret)
  vote <player>         extraction vote
  who                   ask the server for the roster
  help                  this list
  quit                  disconnect
"""

import argparse
import asyncio
import os
import sys

import commlib


class Player:
    def __init__(self, conn):
        self.conn = conn
        self.role = None
        self.players = []

    # -- incoming ------------------------------------------------------------
    def show(self, msg):
        t, p = msg.get("type"), msg.get("payload", {})
        if t == "roster":
            self.players = p["players"]
            print(f"\n[roster] {', '.join(self.players)}")
        elif t == "phase":
            print(f"\n===== {p['name'].upper()} =====\n{p['text']}")
        elif t == "role_assign":
            self.role = p["role"]
            print(f"\n*** YOUR ROLE: {self.role.upper()} ***\n{p['brief']}")
        elif t == "chat":
            print(f"\n[{p['channel']}] {p['from']}: {p['text']}")
        elif t == "vault_update":
            bar = "#" * (p["progress"] // 5) + "-" * (20 - p["progress"] // 5)
            line = (f"\n[vault {bar} {p['progress']}%] "
                    f"[alert {p['alert']}] tick {p['tick']}/{p['max_ticks']}")
            if p.get("event"):
                line += f"\n!!! {p['event']}"
            print(line)
        elif t == "action_result":
            print(f"\n({'ok' if p.get('ok') else 'nope'}) {p.get('text')}")
        elif t == "vote_request":
            print("\n>>> VOTE NOW: type:  vote <player>")
        elif t == "game_over":
            print(f"\n{'#' * 50}\nGAME OVER -- {p['winner'].upper()} WIN\n"
                  f"{p['reason']}\nRoles were: {p['roles']}\n{'#' * 50}")
        elif t == "error":
            print(f"\n! server: {p.get('text')}")
        print("> ", end="", flush=True)

    async def listener(self):
        """Pump incoming frames forever. This is the concurrency half:
        broadcasts arrive even while input() blocks the other task."""
        while True:
            msg = await self.conn.recv_msg()
            if msg is None:
                print("\n[disconnected from moderator]")
                break
            self.show(msg)

    # -- outgoing ------------------------------------------------------------
    async def handle(self, line):
        parts = line.strip().split(None, 1)
        if not parts:
            return True
        cmd, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if cmd == "say":
            await self.conn.send_group("crew", "chat", {"text": rest, "channel": "crew"})
        elif cmd == "mole":
            await self.conn.send_group("moles", "chat", {"text": rest, "channel": "moles"})
        elif cmd == "all":
            await self.conn.broadcast("chat", {"text": rest, "channel": "all"})
        elif cmd in ("drill", "watch", "lift", "sabotage"):
            await self.conn.send("action", {"kind": cmd})
        elif cmd == "vote" and rest:
            await self.conn.send("vote", {"target": rest})
        elif cmd == "who":
            print(f"players: {', '.join(self.players) or '(unknown yet)'}")
        elif cmd == "help":
            print(__doc__)
        elif cmd in ("quit", "exit"):
            return False
        else:
            # Bare text = crew chat, like the original Werewolves terminals.
            await self.conn.send_group("crew", "chat", {"text": line.strip(), "channel": "crew"})
        return True

    async def input_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                # input() blocks; run it in a thread so the listener keeps pumping.
                line = await loop.run_in_executor(None, input, "> ")
            except EOFError:
                break
            if not await self.handle(line):
                break

    async def run(self):
        listener_task = asyncio.create_task(self.listener())
        await self.input_loop()
        listener_task.cancel()
        await self.conn.close()


async def main():
    ap = argparse.ArgumentParser(description="Neon Heist player client")
    ap.add_argument("--sock", default="/tmp/neon-heist.sock")
    ap.add_argument("--id", default=os.environ.get("USER", "player"))
    args = ap.parse_args()
    conn = await commlib.PlayerConn.connect(args.sock, args.id)
    print(f"Connected as {args.id}. Type 'help' for commands.")
    await Player(conn).run()


if __name__ == "__main__":
    asyncio.run(main())
