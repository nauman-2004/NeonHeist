#!/bin/bash
# run_isolated.sh -- launch the moderator, a player, or the spam bot inside
# its own cgroup v2 scope (CPU + memory caps) via systemd-run.
#
# Linux VM only -- macOS has no cgroups. Run with sudo, from the repo dir:
#   ./run_isolated.sh check
#   ./run_isolated.sh moderator --sock /tmp/neon-heist/heist.sock [args...]
#   ./run_isolated.sh player player1 --sock /tmp/neon-heist/heist.sock
#   ./run_isolated.sh bot spam1 --sock /tmp/neon-heist/heist.sock --mode burn
#
# Caps: moderator CPU 50% / memory 256M; players and bots CPU 25% /
# memory 128M, max 32 tasks. The caps are the point: a player that
# floods or hogs cannot take the moderator (or the machine) down with it.
#
# HEIST_DIR: where the code lives (default /opt/neon-heist in the VM).
set -euo pipefail

HEIST_DIR="${HEIST_DIR:-/opt/neon-heist}"

die() { echo "run_isolated: $*" >&2; exit 1; }

check() {
    # 1. unified cgroup hierarchy?
    fstype=$(stat -fc %T /sys/fs/cgroup/ 2>/dev/null || echo "?")
    [ "$fstype" = "cgroup2fs" ] \
        || die "not cgroup v2 (got $fstype) -- need Ubuntu 22.04+ with systemd"
    # 2. can systemd actually run a scoped unit with a quota?
    #    (In a container this silently does nothing, so verify, don't assume.)
    # NOTE: systemd-run --scope stays in the foreground until its command
    # exits, so run it in the background and check while the sleep is
    # still running -- otherwise the scope is already done when we look.
    systemd-run --scope --unit=heist-check --property=CPUQuota=10% sleep 30 >/dev/null &
    check_pid=$!
    sleep 1
    state=$(systemctl show heist-check.scope -p ActiveState --value)
    # Read the limit from the cgroup itself -- the ground truth. (systemctl
    # prints CPUQuotaPerSecUSec as a timespan like "100ms", and 10% is
    # 100000 us/s, not 10000, so comparing its output is fragile.)
    cpu_max=$(cat /sys/fs/cgroup/system.slice/heist-check.scope/cpu.max 2>/dev/null || echo "?")
    systemctl stop heist-check.scope >/dev/null 2>&1 || true
    wait "$check_pid" 2>/dev/null || true
    [ "$state" = "active" ] \
	|| die "smoke scope is '$state', not active -- systemd-run scopes do not work here"
    # cpu.max is "MAX PERIOD" in microseconds; systemd uses a 100ms period,
    # so 10% reads "10000 100000", not "100000 1000000" -- the ratio is
    # what matters: MAX*10 must equal PERIOD.
    echo "$cpu_max" | awk '{ if ($1 == "max" || $2 == 0 || $1 * 10 != $2) exit 1 }' \
	|| die "smoke scope cpu.max is '$cpu_max', expected a 10% quota (MAX*10 == PERIOD)"

    echo "check ok: cgroup v2, systemd-run scopes work, CPUQuota applies"
}

stop_old() { systemctl stop "$1" >/dev/null 2>&1 || true; }

case "${1:-}" in
    check)
        check
        ;;
    moderator)
        shift
        stop_old heist-moderator.scope
        exec systemd-run --scope --unit=heist-moderator \
            --property=CPUQuota=50% --property=MemoryMax=256M --property=TasksMax=64 \
            --uid=moderator python3 "$HEIST_DIR/moderator.py" "$@"
        ;;
    player)
        id="${2:?usage: run_isolated.sh player <id> --sock ...}"; shift 2
        stop_old "heist-$id.scope"
        exec systemd-run --scope --unit="heist-$id" \
            --property=CPUQuota=25% --property=MemoryMax=128M --property=TasksMax=32 \
            --uid="$id" python3 "$HEIST_DIR/player.py" --id "$id" "$@"
        ;;
    bot)
        id="${2:?usage: run_isolated.sh bot <id> --sock ... --mode ...}"; shift 2
        stop_old "heist-$id.scope"
        exec systemd-run --scope --unit="heist-$id" \
            --property=CPUQuota=25% --property=MemoryMax=128M --property=TasksMax=32 \
            --uid="$id" python3 "$HEIST_DIR/spam_bot.py" --id "$id" "$@"
        ;;
    *)
        die "usage: run_isolated.sh check | moderator [args...] | player <id> [args...] | bot <id> [args...]"
        ;;
esac