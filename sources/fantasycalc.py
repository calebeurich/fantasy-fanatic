"""Minimal FantasyCalc API client. Smoke test: python -m sources.fantasycalc"""

import requests

from .cache import ttl_cache, MARKET_TTL

BASE = "https://api.fantasycalc.com/values/current"


# TE-premium adjustment, applied here because **FantasyCalc applies it in the browser,
# not on the server**. Their site only ever requests `tep=none`; selecting TEP+ or TEP++
# fires no network call and rescales the TE column client-side. The API itself 404s on
# every `tep` value except `none`.
#
# Measured off their own rendered numbers: a flat multiplier on TE values only, identical
# to four decimal places across every TE, and unchanged between 10- and 12-team settings
# (Bowers 7,458 -> 8,569 -> 9,621; McBride 6,579 -> 7,559 -> 8,487). Non-TE values and
# pick values do not move.
#
# This is a *replicated* client transform, not an API contract - if FantasyCalc retunes
# it, we drift silently. `python -m sources.fantasycalc` re-prints the multipliers in use
# so a drift check is one command.
TEP_MULTIPLIER = {"none": 1.0, "tep": 1.1490, "teppp": 1.2900}


@ttl_cache(MARKET_TTL)
def get_players(num_qbs: int, num_teams: int, ppr: float, is_dynasty: bool = True,
                tep_tier: str = "none") -> dict[str, dict]:
    """Dynasty value + age + position for this league's format, keyed by Sleeper player_id.

    The format parameters that reach the API are `isDynasty`, `numQbs`, `numTeams` and
    `ppr`; `numQbs` has two settings, 1 and >=2 (see `sleeper.starting_qbs`). TE premium
    is applied locally - see `TEP_MULTIPLIER`."""
    params = {
        "isDynasty": str(is_dynasty).lower(),
        "numQbs": num_qbs,
        "numTeams": num_teams,
        "ppr": ppr,
    }
    resp = requests.get(BASE, params=params)
    resp.raise_for_status()
    entries = resp.json()

    te_multiplier = TEP_MULTIPLIER[tep_tier]
    players = {}
    for entry in entries:
        if entry["player"]["position"] == "PICK":
            continue
        sleeper_id = entry["player"].get("sleeperId")
        if sleeper_id:
            position = entry["player"].get("position")
            players[sleeper_id] = {
                "value": round(entry["value"] * te_multiplier) if position == "TE" else entry["value"],
                "age": entry["player"].get("maybeAge"),
                "position": position,
                "name": entry["player"].get("name"),
            }
    return players


@ttl_cache(MARKET_TTL)
def get_pick_values(num_qbs: int, num_teams: int, ppr: float, is_dynasty: bool = True) -> dict[str, int]:
    """Rookie pick values keyed by name, e.g. '2027 1st'. Only the current draft class
    (this season, before it happens) gets an exact slot like '2026 Pick 1.01' - future
    classes are valued as a flat round average since the slot isn't known yet."""
    params = {
        "isDynasty": str(is_dynasty).lower(),
        "numQbs": num_qbs,
        "numTeams": num_teams,
        "ppr": ppr,
    }
    resp = requests.get(BASE, params=params)
    resp.raise_for_status()
    entries = resp.json()

    return {
        entry["player"]["name"]: entry["value"]
        for entry in entries
        if entry["player"]["position"] == "PICK"
    }


def tep_drift_check(num_teams: int = 12, ppr: float = 1.0, num_qbs: int = 2) -> dict:
    """What TE values look like under each TEP tier, so the replicated multipliers can be
    eyeballed against fantasycalc.com's own TEP control. `TEP_MULTIPLIER` copies a
    transform their site does in the browser rather than an API contract, so this is the
    one-command way to notice if they retune it."""
    return {
        tier: {name: info["value"]
               for name, info in sorted(
                   ((i["name"], i) for i in get_players(num_qbs, num_teams, ppr, True, tier).values()
                    if i["position"] == "TE"),
                   key=lambda kv: -kv[1]["value"])[:3]}
        for tier in TEP_MULTIPLIER
    }


if __name__ == "__main__":
    print("TEP tiers (compare against fantasycalc.com's TEP dropdown):")
    for tier, tes in tep_drift_check().items():
        shown = ", ".join(f"{n}:{v}" for n, v in tes.items())
        print(f"  {tier:6} (x{TEP_MULTIPLIER[tier]}): {shown}")
    print()

    players = get_players(num_qbs=2, num_teams=12, ppr=1.0)
    top_5 = sorted(players.items(), key=lambda kv: kv[1]["value"], reverse=True)[:5]
    print(f"{len(players)} players with values")
    print("top 5 by value:", top_5)

    picks = get_pick_values(num_qbs=2, num_teams=12, ppr=1.0)
    print(f"\n{len(picks)} pick values, e.g. 2027 1st = {picks.get('2027 1st')}")
