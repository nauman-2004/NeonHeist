#!/usr/bin/env python3
"""
moderator.py -- Neon Heist game server.

Run:  python3 moderator.py [--sock PATH] [--min-players N] [--max-players N]
                         [--lobby-timeout S] [--planning-time S]
                         [--tick S] [--max-ticks N] [--vote-timeout S]

Game flow:
  lobby     wait for 4-6 players to connect (broadcast roster as they join)
  planning  roles are dealt via UNICAST; crew plans on the public channel,
            moles scheme on their secret MULTICAST channel
  execution timed loop: vault progress ticks up as the crew drills, the alert
            meter rises on guard patrols, moles secretly sabotage.
            BROADCAST vault_update every tick.
  extraction everyone votes (UNICAST) on who to cut; moles win if any mole
            survives, crew wins if the vault hit 100% and all moles are cut.

All messaging goes through commlib.GameServer. Game rules live in
game_state.py; this file is I/O, phases, and timers.
"""

import argparse
import asyncio
import os
import sys

import commlib
from game_state import assign_roles, tally_votes, decide_winner, ROLE_BRIEFS

# Tuning: what each action does per execution tick.
DRILL_CREW = 4          # vault progress per drill (non-hacker)
DRILL_HACKER = 10       # vault progress per drill (hacker)
SABOTAGE_HIT = 12       # vault progress destroyed per mole sabotage
PATROL_ALERT = 5        # alert gained per tick from guard patrols
WATCH_RELIEF = 8        # alert removed per watch action
ALERT_MAX = 100         # alert >= this -> heist blown, moles win
VAULT_TARGET = 100


class Moderator:
    def __init__(self, args):
        self.args = args
        self.server = commlib.GameServer()
        # Actions arrive on connection tasks; the game loop drains this dict
        # once per tick. Single event loop => no thread-safety issues.
        self.actions_this_tick = {}
        self.phase = "lobby"
        self.roles = {}
        self.vault = 0
        self.alert = 0
        self.tick = 0
        self.votes = {}

    # -- message handling (runs on per-connection tasks) --------------------

    async def on_message(self, server, pid, msg):
        # The server already stamped msg["from"] = pid (authenticated).
        if self.phase == "lobby":
            return
        mtype = msg.get("type")
        if mtype == "chat":
            await self.handle_chat(pid, msg)
        elif mtype == "action":
            kind = msg["payload"].get("kind")
            if kind in ("drill", "watch", "lift", "sabotage"):
                self.actions_this_tick[pid] = kind
        elif mtype == "vote" and self.phase == "extraction":
            target = msg["payload"].get("target")
            if pid not in self.votes and target in self.roles:
                self.votes[pid] = target
                await server.unicast(pid, "action_result",
                                     {"ok": True, "text": f"Vote recorded for {target}."})

    async def handle_chat(self, pid, msg):
        text = str(msg["payload"].get("text", ""))[:500]
        channel = msg["payload"].get("channel", "crew")
        if channel == "moles":
            # Enforce secrecy server-side: only moles may use the mole channel.
            if self.roles.get(pid) != "mole":
                await self.server.unicast(
                    pid, "action_result",
                    {"ok": False, "text": "You are not a mole."})
                return
            await self.server.multicast(
                "moles", "chat",
                {"from": pid, "channel": "moles", "text": text})
        elif channel == "all":
            await self.server.broadcast(
                "chat", {"from": pid, "channel": "all", "text": text})
        else:  # "crew": public channel -- moles are undercover, so they hear it too
            await self.server.multicast(
                "crew", "chat", {"from": pid, "channel": "crew", "text": text})

    # -- phases ---------------------------------------------------------------

    async def lobby(self):
        """Wait for players: start at max_players, or min_players after timeout."""
        print(f"[lobby] waiting on {self.args.sock} "
              f"(need {self.args.min_players}-{self.args.max_players}) ...", flush=True)
        seen = -1
        waited = 0.0
        while True:
            n = len(self.server.player_ids())
            if n != seen:
                seen = n
                await self.server.broadcast("roster", {"players": self.server.player_ids()})
                print(f"[lobby] {n} player(s): {self.server.player_ids()}", flush=True)
            if n >= self.args.max_players:
                break
            if waited >= self.args.lobby_timeout:
                break
            await asyncio.sleep(0.5)
            waited += 0.5
        return self.server.player_ids()

    async def deal_roles(self, players):
        self.roles = assign_roles(players)
        for pid in players:
            self.server.add_to_group(pid, "crew")   # everyone hears the public channel
        for pid, role in self.roles.items():
            if role == "mole":
                self.server.add_to_group(pid, "moles")  # secret channel
            await self.server.unicast(
                pid, "role_assign",
                {"role": role, "brief": ROLE_BRIEFS[role]})
        print(f"[planning] roles dealt (secret): {self.roles}", flush=True)
        await self.server.broadcast(
            "phase", {"name": "planning",
                      "text": "Plan the heist. Crew: coordinate on the public channel "
                              "('say ...'). Moles: scheme secretly ('mole ...'). "
                              f"Execution begins in {self.args.planning_time}s."})

    async def execution(self):
        self.phase = "execution"
        await self.server.broadcast(
            "phase", {"name": "execution",
                      "text": "The heist is live! Hacker: 'drill'. Lookout: 'watch'. "
                              "Muscle: 'lift' when obstacles hit. Everyone acts at once -- "
                              "the vault timer does not wait."})
        for self.tick in range(1, self.args.max_ticks + 1):
            # Drain this tick's actions (written by connection tasks).
            actions = self.actions_this_tick
            self.actions_this_tick = {}

            drill = watch = sabotage = lifts = 0
            for pid, kind in actions.items():
                role = self.roles.get(pid)
                if kind == "drill":
                    drill += DRILL_HACKER if role == "hacker" else DRILL_CREW
                elif kind == "watch":
                    watch += 1
                elif kind == "lift":
                    lifts += 1
                elif kind == "sabotage" and role == "mole":
                    sabotage += 1
                    # Silent: the crew must NOT learn who sabotaged from traffic.
                    await self.server.unicast(
                        pid, "action_result",
                        {"ok": True, "text": "Sabotage applied. No one saw a thing."})

            # Every 7th tick the casino resets its camera grid: the vault is
            # frozen for that tick unless the muscle lifts through it. The
            # freeze lasts exactly one tick -- a permanent freeze would make
            # the game unwinnable whenever nobody lifts.
            event = None
            if self.tick % 7 == 0:
                if lifts > 0:
                    event = "Muscle smashed through the camera grid reset. Drilling continues."
                else:
                    event = ("OBSTACLE: camera grid reset! Vault frozen this tick "
                             "-- 'lift' to punch through next time.")
                    drill = 0

            self.vault = min(VAULT_TARGET, max(0, self.vault + drill - sabotage * SABOTAGE_HIT))
            self.alert = max(0, self.alert + PATROL_ALERT - watch * WATCH_RELIEF)

            await self.server.broadcast(
                "vault_update",
                {"tick": self.tick, "max_ticks": self.args.max_ticks,
                 "progress": self.vault, "alert": self.alert,
                 "event": event})
            if self.tick % 5 == 0 or self.vault >= VAULT_TARGET:
                print(f"[execution] tick {self.tick}: vault={self.vault}% "
                      f"alert={self.alert} actions={actions}", flush=True)

            if self.alert >= ALERT_MAX:
                await self.finish("moles", "The alert maxed out -- security swarmed the casino.")
                return
            if self.vault >= VAULT_TARGET:
                break
            await asyncio.sleep(self.args.tick)

    async def extraction(self):
        self.phase = "extraction"
        self.votes = {}
        players = self.server.player_ids()
        await self.server.broadcast(
            "phase", {"name": "extraction",
                      "text": "Vault cracked. One of you is cut before the getaway. "
                              "Vote with: vote <player> -- highest count is left behind. "
                              "A tie cuts nobody."})
        await self.server.broadcast("vote_request", {"players": players})
        waited = 0.0
        while len(self.votes) < len(players) and waited < self.args.vote_timeout:
            await asyncio.sleep(0.5)
            waited += 0.5
        eliminated, counts = tally_votes(self.votes)
        winner, reason = decide_winner(self.roles, self.vault, eliminated)
        await self.finish(winner,
                          f"{reason} Votes: {counts}. "
                          f"{'Nobody was cut.' if eliminated is None else f'{eliminated} was left behind.'}")

    async def finish(self, winner, reason):
        self.phase = "over"
        print(f"[over] winner={winner}: {reason}", flush=True)
        await self.server.broadcast(
            "game_over",
            {"winner": winner, "reason": reason,
             "roles": self.roles, "vault": self.vault})

    # -- main -----------------------------------------------------------------

    async def run(self):
        serve_task = asyncio.create_task(
            self.server.serve(self.args.sock, self.on_message))
        await asyncio.sleep(0.2)  # let the socket come up
        try:
            players = await self.lobby()
            if len(players) < self.args.min_players:
                print("[lobby] not enough players -- shutting down.", flush=True)
                return
            self.phase = "planning"
            await self.deal_roles(players)
            await asyncio.sleep(self.args.planning_time)
            await self.execution()
            if self.phase != "over":  # execution may end the game early (alert max)
                await self.extraction()
            await asyncio.sleep(1.0)  # let game_over flush to players
        finally:
            serve_task.cancel()
            try:
                await serve_task
            except asyncio.CancelledError:
                pass
            try:
                os.unlink(self.args.sock)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description="Neon Heist game server (moderator)")
    ap.add_argument("--sock", default="/tmp/neon-heist.sock")
    ap.add_argument("--min-players", type=int, default=4)
    ap.add_argument("--max-players", type=int, default=6)
    ap.add_argument("--lobby-timeout", type=float, default=60)
    ap.add_argument("--planning-time", type=float, default=20)
    ap.add_argument("--tick", type=float, default=2.0)
    ap.add_argument("--max-ticks", type=int, default=30)
    ap.add_argument("--vote-timeout", type=float, default=30)
    args = ap.parse_args()
    asyncio.run(Moderator(args).run())


if __name__ == "__main__":
    main()
