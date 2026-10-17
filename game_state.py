"""
game_state.py -- pure Neon Heist game logic. No I/O, no sockets.

Kept separate from moderator.py so the rules (roles, voting, win check)
can be unit-tested and reasoned about without running a server.
"""

import random

CREW_ROLES = ("hacker", "lookout", "muscle")
PHASES = ("lobby", "planning", "execution", "extraction", "over")

ROLE_BRIEFS = {
    "hacker":  "You crack the vault. Your 'drill' actions are worth double.",
    "lookout": "You watch the guards. Your 'watch' actions cut the alert meter fast.",
    "muscle":  "You clear obstacles. Use 'lift' when the crew hits a snag.",
    "mole":    "You are the mole. Sabotage quietly ('sabotage'), blend in by "
               "drilling with the crew, and survive the extraction vote. You "
               "have a secret 'moles' chat channel the crew cannot see.",
}


def assign_roles(player_ids, rng=None):
    """Secretly deal roles. 1 mole for <=4 players, else 2.

    Returns {player_id: role}. The moderator unicasts each player's own role
    and never reveals the full mapping until game over.
    """
    rng = rng or random.Random()
    ids = list(player_ids)
    rng.shuffle(ids)
    n_moles = 1 if len(ids) <= 4 else 2
    roles = {}
    for i, pid in enumerate(ids):
        if i < n_moles:
            roles[pid] = "mole"
        else:
            roles[pid] = CREW_ROLES[(i - n_moles) % len(CREW_ROLES)]
    return roles


def tally_votes(votes):
    """votes: {voter_id: target_id} -> (eliminated_id or None, {target: count}).

    Highest count is eliminated. A tie eliminates nobody -- the crew failed
    to coordinate, which the win rule below punishes.
    """
    counts = {}
    for target in votes.values():
        counts[target] = counts.get(target, 0) + 1
    if not counts:
        return None, {}
    top = max(counts.values())
    leaders = [p for p, c in counts.items() if c == top]
    if len(leaders) > 1:
        return None, counts
    return leaders[0], counts


def decide_winner(roles, vault_progress, eliminated):
    """Return (winner, reason). winner is 'crew' or 'moles'."""
    moles = {p for p, r in roles.items() if r == "mole"}
    if vault_progress < 100:
        return "moles", "The vault was never cracked -- the heist failed."
    survivors = set(roles) - ({eliminated} if eliminated else set())
    if moles & survivors:
        return "moles", "A mole walked out with the crew."
    return "crew", "Every mole was cut before extraction. Clean getaway."
