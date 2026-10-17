#!/usr/bin/env bash
# package.sh — build the Neon Heist submission tarball (neon-heist.tgz).
#
# The assignment requires a *.tgz containing the PDF writeup plus the full
# source code and a README. This script fails with a clear message if any
# required file is missing (so you never submit a tarball without the PDF),
# and packages exactly the intended files (so __pycache__/ etc. can't sneak in).
#
# Usage:
#   1. Build your writeup PDF from WRITEUP_DRAFT.md (pandoc, editor, ...) and
#      save it here as writeup.pdf.
#   2. Run:  ./package.sh
#   3. Submit the resulting neon-heist.tgz.
set -euo pipefail
cd "$(dirname "$0")"

REQUIRED=(commlib.py game_state.py moderator.py player.py selftest.py setup_users.sh README.md writeup.pdf)

missing=()
for f in "${REQUIRED[@]}"; do
  [ -f "$f" ] || missing+=("$f")
done

if [ "${#missing[@]}" -gt 0 ]; then
  echo "Cannot build tarball — missing required submission file(s):" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  echo "Build the PDF from WRITEUP_DRAFT.md, save it as writeup.pdf, then rerun." >&2
  exit 1
fi

tar czf neon-heist.tgz "${REQUIRED[@]}"
echo "Built neon-heist.tgz. Contents:"
tar tzf neon-heist.tgz
