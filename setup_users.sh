#!/bin/bash
# setup_users.sh -- create one Linux user per player, plus a shared group
# directory for the Unix socket. Run once with sudo.
#
# Usage: sudo ./setup_users.sh
#
# After this, open 5 terminals and run:
#   terminal 1: sudo -u moderator python3 moderator.py --sock /tmp/neon-heist/heist.sock
#   terminal 2: sudo -u player1  python3 player.py --sock /tmp/neon-heist/heist.sock
#   ... player2, player3, player4 ...
# The 5th terminal is your "screenshot terminal" for the writeup.

set -e

SOCKDIR=/tmp/neon-heist
GROUP=heist

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo ./setup_users.sh"
  exit 1
fi

# One group for everyone at the table; the socket dir is group-writable.
getent group "$GROUP" >/dev/null || groupadd "$GROUP"
mkdir -p "$SOCKDIR"
chgrp "$GROUP" "$SOCKDIR"
chmod 2770 "$SOCKDIR"   # setgid: files created here inherit the group

for u in moderator player1 player2 player3 player4 player5 player6; do
  if ! id "$u" >/dev/null 2>&1; then
    useradd -m -s /bin/bash -G "$GROUP" "$u"
    echo "created user $u"
  else
    usermod -aG "$GROUP" "$u" 2>/dev/null || true
    echo "user $u already exists (added to $GROUP)"
  fi
done

# Players need to read the game code. Adjust this path to where you keep it.
CODE_DIR="$(cd "$(dirname "$0")" && pwd)"
chmod -R a+rX "$CODE_DIR"

echo
echo "Done. Socket dir: $SOCKDIR (group $GROUP, mode 2770)"
echo "Example: sudo -u moderator python3 $CODE_DIR/moderator.py --sock $SOCKDIR/heist.sock"
