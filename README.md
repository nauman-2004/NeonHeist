# Neon Heist

A Werewolves-like text game: a crew of 4–6 plans a casino heist, but 1–2 of
them are moles. Plan, execute the heist against a timer, then vote someone
out before the getaway. Moles win if any mole survives extraction (or the
heist fails); the crew wins if the vault is cracked and every mole is cut.

## Files

| File | What it is |
|---|---|
| `commlib.py` | **The communications library (single file).** Unix stream sockets + asyncio, length-prefixed JSON frames. Client (`PlayerConn`) and server (`GameServer`) halves. |
| `game_state.py` | Pure game logic: role dealing, vote tallying, win check. No I/O. |
| `moderator.py` | Game server: lobby → planning → execution → extraction. Run as its own user. Includes the per-player rate limiter (Project 2/3). |
| `player.py` | Terminal client. Run one per player, each as its own Linux user. |
| `setup_users.sh` | Creates `moderator`, `player1..6` users + group socket dir (run with sudo). |
| `selftest.py` | End-to-end test: real `moderator.py` + 4 scripted players, 6 scenarios. |
| `spam_bot.py` | Load-test bot: flood / burn / hog modes for the rate limiter and cgroup caps (Project 2/3). |
| `run_isolated.sh` | Launches moderator, players, and bots in systemd-run cgroup scopes with CPU/memory caps (Project 2/3). |
| `measure_cpu.sh` | Samples /proc/PID/stat into a CSV for the poster CPU graph (Project 2/3). |
| `COMMS_EXPLAINED.md` | Plain-English walkthrough of the comms design (writeup material). |

## Project 2/3 — what changed

Headline: **per-player isolation.** Every player (and the moderator) runs
in its own cgroup v2 scope with CPU and memory caps, and the moderator
rate-limits each player, so one misbehaving client cannot stall or starve
the game. The game itself, the wire format, and the comms library API are
unchanged.

### New mechanism: cgroups isolation + per-player rate limiting

- **cgroup scopes (`run_isolated.sh`).** Each process is launched with
  `systemd-run --scope` under its own cap. Moderator: CPU 50% / memory
  256M / 64 tasks. Each player and bot: CPU 25% / memory 128M / 32 tasks.
  A player that burns CPU or hogs memory is throttled inside its own
  scope; the moderator and the other players are untouched.
- **Rate limiter (`moderator.py`).** 30 frames per second per player
  (1s sliding window). Frames over the limit are dropped; after 50
  dropped frames the player is disconnected. Legitimate play is about
  1 frame per game tick, so normal players never trip it. The disconnect
  prints exactly one `[ratelimit]` line.
- **`GameServer.disconnect(pid)` (`commlib.py`).** Closes a player's
  writer, which triggers the normal EOF → prune → `on_leave` path, so a
  forced disconnect cleans up exactly like a voluntary one.
- **Load-test bot (`spam_bot.py`).** Three modes: `flood` (chat frames
  as fast as possible), `burn` (tight CPU loop, one legal chat per
  second), `hog` (allocates 1MB chunks into a list it holds).
- **`measure_cpu.sh`.** Samples a process's CPU% every 0.5s from
  `/proc/PID/stat` into a CSV (`t,cpu`).

### Bug fixes (from the Project 1 self-audit and classmate feedback)

1. **Planning actions no longer fire on execution tick 1.** Actions sent
   during planning were applied on the first execution tick. Now
   actions are gated on `phase == "execution"`.
2. **Disconnect mid-vote no longer stalls extraction.** The vote waited
   out the full `--vote-timeout` when a player left mid-vote. The
   player roster is now refreshed inside the vote loop, so the vote
   resolves as soon as the remaining players have voted.
3. **A disconnect fully removes the ghost (Option A).** A disconnected
   player used to linger in `roles` and `votes`, so a ghost mole still
   counted as alive in the win check and stayed a valid vote target
   (reported by Nick Mastorakis). The moderator now registers the
   server's `on_leave` hook, and `handle_leave` removes the player from
   actions, votes, *and* roles. Replaying Nick's scenario — the mole
   disconnects mid-game — now ends in a crew win.
4. **Rate-limiter log spam.** The `[ratelimit]` line fired once per
   dropped frame after the 50th, so one flooder printed 107 lines. It
   now prints exactly once per disconnected player.
5. **Socket directory recreated on startup.** `/tmp` is tmpfs, so
   `/tmp/neon-heist` vanishes on reboot and the moderator sat in a
   dead lobby printing "[lobby] waiting" with no socket file.
   `GameServer.serve()` now recreates the socket directory before
   binding (best-effort group/mode fix-up, a no-op when
   `setup_users.sh` already created it), and the moderator fails fast
   with an `[error]` line if the server cannot start instead of
   waiting forever.

### Verified results (UTM Linux VM, 2026-11-13)

- **Rate limiter (T1):** flood bot sent 107 frames in 0.0s
  (~194,590/s) and was disconnected; the moderator printed exactly
  one `[ratelimit] disconnecting spam1 (50 frames dropped)` line, and
  the game continued.
- **CPU cap (T2):** burn bot uncapped pinned ~100% of one core
  (98–102%); inside its 25% scope it held 23.3–27.6% (avg ~25%).
- **Memory cap (T3):** the hog bot's cgroup `memory.current` climbed
  to exactly 134,217,728 bytes (128M) and flatlined there — the cap
  holds on real memory. (The bot itself is not OOM-killed: its
  "allocated" counter counts bytearray objects created, not physical
  RAM, so it finishes cleanly at its 512MB safety cap.)
- **Regression (T4):** `python3 selftest.py` — all 6 scenarios pass
  in the VM.

## Setup

```
sudo ./setup_users.sh
```

This creates the `heist` group, the `moderator` and `player1..player6`
users, and the socket directory `/tmp/neon-heist` (mode 2770). It does
*not* add the users to the `heist` group — if a user needs to connect
to the socket, add them by hand:

```
sudo usermod -aG heist <user>   # e.g. your own login, so you can run a bot
```

Group membership only applies to shells started after the change, so
open a fresh terminal after running `usermod`. In the VM the game runs
from `/opt/neon-heist` (a copy of this repo) because the game users
cannot read a home directory; `run_isolated.sh` defaults to
`HEIST_DIR=/opt/neon-heist` for the same reason.

## Running the game (direct, no isolation)

Run the commands below from the directory holding the files (in the VM,
`/opt/neon-heist`). One terminal per process. The moderator first, then
one player per terminal. The game starts when 4 players are in the lobby
(or the lobby timeout expires).

```
# terminal 1 — moderator (lobby 300s, planning 20s, 2s ticks):
sudo -u moderator python3 moderator.py --sock /tmp/neon-heist/heist.sock \
  --min-players 4 --max-players 5 --lobby-timeout 300 --planning-time 20 --tick 2

# terminals 2–5 — one player each:
sudo -u player1 python3 player.py --sock /tmp/neon-heist/heist.sock --id player1
sudo -u player2 python3 player.py --sock /tmp/neon-heist/heist.sock --id player2
sudo -u player3 python3 player.py --sock /tmp/neon-heist/heist.sock --id player3
sudo -u player4 python3 player.py --sock /tmp/neon-heist/heist.sock --id player4
```

Gameplay: planning (discuss in crew chat; the mole has a secret
channel), execution (`drill` / `watch` / `lift` / `sabotage` each tick),
then extraction (vote one player out). The crew wins if the vault
reaches 100% and no mole survives the vote; the moles win if alert
reaches 100 or a mole survives extraction. The hacker drills 10
vault/tick, others 4; `watch` lowers alert by 8; patrols add 5
alert/tick.

## Running isolated (cgroups, Project 2/3)

Linux VM only — macOS has no cgroups. Run with `sudo`, from the repo
dir. Check the gate first; it verifies cgroup v2 and that a scoped
unit with a quota actually runs.

```
sudo ./run_isolated.sh check
# check ok: cgroup v2, systemd-run scopes work, CPUQuota applies

# moderator in its own scope (50% CPU / 256M / 64 tasks):
sudo ./run_isolated.sh moderator --sock /tmp/neon-heist/heist.sock \
  --min-players 4 --max-players 5 --lobby-timeout 3600 --planning-time 20 --tick 2

# one player per terminal, each in its own scope (25% / 128M / 32 tasks):
sudo ./run_isolated.sh player player1 --sock /tmp/neon-heist/heist.sock
sudo ./run_isolated.sh player player2 --sock /tmp/neon-heist/heist.sock
...
```

For a long test session use a long `--lobby-timeout` (e.g. 3600), or
the moderator exits after 300s and unlinks the socket. Note that
`run_isolated.sh` execs `systemd-run`, which exits immediately — `$!`
is the wrapper's PID, not the game's. To find a bot's real PID for
`measure_cpu.sh`, use `pgrep -f`.

## Communications library API (`commlib.py`)

Wire format: `uint32 big-endian length + JSON`. Envelope keys:
`type`, `from`, `to` (unicast), `group` (multicast), `payload`.

**Client — `PlayerConn`**
- `await PlayerConn.connect(sock_path, player_id)` → connection (sends `hello`)
- `await conn.send(msg_type, payload=None, to=None, group=None)` — raw send;
  `to=` = unicast hint, `group=` = multicast hint, neither = broadcast hint
- `await conn.send_to(to_id, msg_type, payload=None)` — unicast via moderator
- `await conn.send_group(group, msg_type, payload=None)` — multicast via moderator
- `await conn.broadcast(msg_type, payload=None)` — broadcast via moderator
- `await conn.recv_msg()` → dict, or `None` on clean disconnect
- `await conn.close()`

**Server — `GameServer`**
- `await server.serve(sock_path, on_message)` — accept loop; `on_message(server, pid, msg)`. Recreates the socket directory if it is missing (e.g. after a reboot wipes /tmp).
- `await server.unicast(to_id, msg_type, payload=None, frm="moderator")`
- `await server.multicast(group, msg_type, payload=None, frm="moderator")` → recipients
- `await server.broadcast(msg_type, payload=None, frm="moderator")` → recipients
- `server.disconnect(pid)` — close a player's connection (triggers the normal EOF → prune → `on_leave` cleanup)
- `server.add_to_group(pid, group)` / `server.remove_from_group(pid, group)`
- `server.player_ids()`, `server.group_members(group)`
- `server.on_join` / `server.on_leave` — optional callbacks (may be coroutines)

**Message types** (Neon Heist protocol on top of the library):
client→moderator: `chat`, `action` (`drill|watch|lift|sabotage`), `vote`
moderator→client: `welcome`, `roster`, `phase`, `role_assign`, `chat`,
`vault_update`, `action_result`, `vote_request`, `game_over`, `error`

## Concurrency

- Moderator: one asyncio task per player connection + the phase/timer tasks,
  all on one event loop. Per-writer locks keep frames atomic.
- Player: a listener task prints broadcasts immediately while the main task
  blocks on `input()` — alarms interrupt typing.
- Rate limiting runs per-player in `on_message`, before the message is
  handled: a 1s sliding window, excess frames dropped, disconnect after
  50 drops.

## Testing

### Regression suite — `selftest.py`

```
python3 selftest.py
```

Spawns the real `moderator.py`, connects 4 scripted players over a real
Unix socket, and plays six scenarios:

1. crew catches the mole
2. crew votes out an innocent
3. stall: nobody acts (moles win by alert)
4. action sent during planning is ignored (vault 0% after tick 1)
5. disconnect mid-vote does not stall extraction (`game_over` in 0.5s, not the 5s timeout)
6. mole disconnects mid-game — Nick's scenario (crew wins; the ghost does not steal it)

All 6 pass on macOS and in the UTM VM.

### T1 — rate limiter (flood)

Start the moderator and 4 players (direct launch above), then flood it
from a fresh terminal. Run it as a user in the `heist` group (your own
login, or `sudo -u player5`); the bot's in-game id can be anything.

```
python3 spam_bot.py --sock /tmp/neon-heist/heist.sock --id spam1 --mode flood --seconds 5
```

Expect `[flood] sent 107 frames in 0.0s (194590/s), disconnected=True`,
and exactly one `[ratelimit] disconnecting spam1 (50 frames dropped)`
line on the moderator. The flood's crew-chat frames appear in the
player terminals (crew chat is multicast; the first ~30 get through,
the 30/s allowance) — that is expected, not a bug. The game continues
for the other players.

### T2 — CPU cap (burn)

Run the moderator with a long lobby timeout (3600) and no players —
the burn bot only needs a live socket. First the uncapped baseline
(direct, as your user), then the capped run inside a scope. Use an id
no player is using; here the lobby is empty, so `player5` is free.

```
# uncapped baseline (~100% of one core):
python3 spam_bot.py --sock /tmp/neon-heist/heist.sock --id player5 --mode burn --seconds 15 &
./measure_cpu.sh $! uncapped.csv 10

# capped run (25% scope). Cache sudo first so the backgrounded sudo
# does not freeze on the password prompt:
sudo -v
sudo ./run_isolated.sh bot player5 --sock /tmp/neon-heist/heist.sock --mode burn --seconds 15 &
sleep 1
BOT_PID=$(pgrep -f "spam_bot.py.*--id player5")
./measure_cpu.sh $BOT_PID capped.csv 10
```

Expect `uncapped.csv` flat around 98–102% and `capped.csv` flat
around 23–28% (avg ~25%). The `pgrep` step matters: `$!` after
`run_isolated.sh` is the `systemd-run` wrapper's PID, not the bot's.
The two CSVs are the data for the poster's CPU graph.

### T3 — memory cap (hog)

```
sudo -v
sudo ./run_isolated.sh bot player6 --sock /tmp/neon-heist/heist.sock --mode hog --seconds 30
```

Expect the bot's "allocated" counter to climb, while the scope's
`memory.current`
(`/sys/fs/cgroup/system.slice/heist-player6.scope/memory.current`)
climbs to exactly 134,217,728 bytes (128M) and flatlines there — the
cap holds on real memory. The bot is *not* OOM-killed: its counter
counts bytearray objects created, not physical RAM (zero-filled
bytearrays share pages), so it finishes cleanly at its 512MB safety
cap. The moderator logs a clean disconnect for the hog.

### T4 — regression in the VM

```
python3 selftest.py
```

All 6 scenarios pass in the VM (same as on macOS).
