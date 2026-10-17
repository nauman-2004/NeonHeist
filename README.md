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
| `moderator.py` | Game server: lobby → planning → execution → extraction. Run as its own user. |
| `player.py` | Terminal client. Run one per player, each as its own Linux user. |
| `setup_users.sh` | Creates `moderator`, `player1..6` users + group socket dir (run with sudo). |
| `selftest.py` | End-to-end test: real `moderator.py` + 4 scripted players, 3 scenarios. |
| `COMMS_EXPLAINED.md` | Plain-English walkthrough of the comms design (writeup material). |

## Realistic test (each player their own Linux user)

```
sudo ./setup_users.sh
# terminal 1:
sudo -u moderator python3 moderator.py --sock /tmp/neon-heist/heist.sock
# terminals 2-5:
sudo -u player1 python3 player.py --sock /tmp/neon-heist/heist.sock --id player1
sudo -u player2 python3 player.py --sock /tmp/neon-heist/heist.sock --id player2
...
```

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
- `await server.serve(sock_path, on_message)` — accept loop; `on_message(server, pid, msg)`
- `await server.unicast(to_id, msg_type, payload=None, frm="moderator")`
- `await server.multicast(group, msg_type, payload=None, frm="moderator")` → recipients
- `await server.broadcast(msg_type, payload=None, frm="moderator")` → recipients
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

## Run the tests

```
python3 selftest.py
```
Spawns the real `moderator.py`, connects 4 scripted players over a real
Unix socket, and plays three full games (crew wins / moles win by bad vote /
moles win by stalled heist), asserting the multicast secrecy rules.

