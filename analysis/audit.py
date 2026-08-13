"""Recommendation audits: does the advice make sense, given data we agree is correct?

Every real bug here was a wrong recommendation produced by correct arithmetic, which unit
tests pass straight through - these check whether the output is *defensible*. Two rules
keep it trustworthy: every check derives from a bug that actually shipped (no speculative
invariants - a noisy audit gets muted), and checks run against real leagues, because the
failures were all shaped by real distributions no synthetic fixture would contain.

Not part of `pytest`: this needs the network, and `tests/` is free and offline by design.

Run: python -m analysis.audit [league_id ...]
"""

import io
import sys
from contextlib import redirect_stdout

from . import roster_needs, team_state, trade_targets
from .league import context

# The leagues to audit when none are named. Three real dynasty leagues, all 12-team
# superflex - which is itself a finding worth stating, since it means format variation is
# untested (see LOGIC.md on the relevance floor).
DEFAULT_LEAGUES = [
    "1315386978904084480",   # XFL 2
    "1319727865188593664",   # [Insert Sh*t League Name Here]
    "1313999558660857856",   # God Bless The Plug
]

# Blocks that name players and must never name one the asking team already owns. Also the
# pool `check_best_available_is_surfaced` searches, which is why `long_shots` belongs here:
# splitting unreachable targets into their own list is a change of *placement*, not of
# coverage, and the check asks whether the best available is named anywhere in the advice.
PLAYER_BLOCKS = ("targets", "long_shots", "acquire_targets", "depth_adds", "persuasion_targets")


def _blocks(result: dict) -> dict:
    """A Middling result nests its buy side under `push`; flatten so checks read one shape."""
    merged = dict(result)
    merged.update(result.get("push", {}))
    return merged


def _entries(result: dict, key: str) -> list[dict]:
    return _blocks(result).get(key) or []


def check_never_recommends_your_own_players(league_id, ctx, results) -> list[str]:
    """A rebuilding team searching rebuilding teams includes itself. Live: a manager was
    advised to acquire two players already on his roster, invisible until the *asking* team
    was a rebuilder - a state neither development league could produce."""
    problems = []
    for owner, result in results.items():
        owned = {ctx.players[p]["name"] for p in (ctx.roster_for(owner)["players"] or [])
                 if p in ctx.players}
        for key in PLAYER_BLOCKS:
            for entry in _entries(result, key):
                if entry.get("name") in owned:
                    problems.append(f"{owner}: {key} names {entry['name']}, already on the roster")
    return problems


def check_best_available_is_surfaced(league_id, ctx, results) -> list[str]:
    """For a count-shaped need, the best current producer available from a *selling* team has
    to appear somewhere in the advice. Live: the top RB recommendation to a pushing team
    produced 738 redraft while the second-best available (1,883) sat off the end of the
    default list, because its owner had never made a trade and activity outranked value."""
    problems = []
    states = team_state.classify_league(league_id)
    for owner, result in results.items():
        me = ctx.roster_for(owner)
        # Compared against what the buy path can actually see (sellable, not whole
        # rosters) - an audit calibrated against a pool the code was never meant to reach
        # reports noise, and a noisy audit gets muted.
        for position, need in _blocks(result).get("needs", {}).items():
            if need["level"] not in ("critical", "top-heavy"):
                continue  # a quality need wants an upgrade, which has its own bar
            available = [
                (entry.get("redraft_value") or 0, entry["name"])
                for other in states
                if other["window"] == "Rebuild" and other["owner_id"] != me["owner_id"]
                for entry in other["sellable"]
                if entry["position"] == position
            ]
            if not available:
                continue
            best_value, best_name = max(available)
            if best_value <= 0:
                continue
            named = {e["name"] for key in PLAYER_BLOCKS for e in _entries(result, key)}
            if best_name not in named:
                problems.append(
                    f"{owner}: {need['level']} need at {position}, but the best SELLABLE "
                    f"player from a rebuilding team ({best_name}, {best_value:,} redraft) is "
                    f"not named anywhere")
    return problems


def check_claims_match_the_data(league_id, ctx, results) -> list[str]:
    """A note may not assert something the entry's own fields contradict. Live: a player 0.3
    years from his decline cutoff was offered as value that would "still be there in two",
    because the test used `bucket` instead of the runway that sentence claims. Also live: a
    `cost_note` said filling an owner's *critical* need meant persuading him to change
    direction, contradicting the fit line printed beside it."""
    problems = []
    for owner, result in results.items():
        for entry in _entries(result, "persuasion_targets"):
            why, cost = entry.get("why_it_fits") or "", entry.get("cost_note") or ""
            if "need at" in why and "change direction" in cost:
                problems.append(f"{owner}: {entry['name']} - cost_note says 'change direction' "
                                f"while why_it_fits says the owner has a need to fill")
        for entry in _entries(result, "stranded"):
            if entry.get("wanted_by") is None:
                problems.append(f"{owner}: stranded {entry['name']} does not say who wants him")
    return problems


def check_one_player_is_not_described_two_ways(league_id, ctx, results) -> list[str]:
    """The same player may not appear in two blocks that say incompatible things about him -
    a live target who is also "never worth a real asset", a long shot doubling as cheap
    depth, or one flavor computed from two rules (`needs_a_pivot` disagreed about Kelce in
    one run). Three live instances between three different pairs of lists made this a check
    rather than a fourth patch."""
    problems = []
    for owner, result in results.items():
        blocks = _blocks(result)
        returns = {u["name"]: u for m in blocks.get("value_upgrades") or []
                   for u in m["returns"] if not u.get("already_mine")}
        buy_side = {e["name"] for key in ("targets", "long_shots") for e in _entries(result, key)}
        for entry in _entries(result, "depth_adds"):
            if entry["name"] in buy_side or entry["name"] in returns:
                problems.append(f"{owner}: {entry['name']} is cheap depth 'never worth a real "
                                f"asset' and also a real target elsewhere in the same report")
        for entry in _entries(result, "persuasion_targets"):
            them = returns.get(entry["name"])
            if them is None:
                continue
            # Both flavors are the same underlying boolean ("no hole you can fill") -
            # which spelling an entry gets is the seller's window, not a second concept.
            tagged = any(f["flavor"] in ("needs_a_pivot", "holds_to_win")
                         for f in them.get("friction") or [])
            if tagged != entry["needs_a_pivot"]:
                problems.append(f"{owner}: {entry['name']} needs_a_pivot is "
                                f"{entry['needs_a_pivot']} in persuasion and {tagged} in the "
                                f"upgrade block - one concept, two answers")
    return problems


def check_everything_computed_is_printed(league_id, ctx, results) -> list[str]:
    """A block attached to the result and rendered by nothing - six live instances in one
    module, each shipping silently because the data was right and only the CLI was blind.
    Checked by rendering the report and looking for entry names and note openings, the only
    way to catch it. NOTE: cannot catch an early `return` that skips a populated block."""
    problems = []
    for owner, result in results.items():
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            trade_targets._print_report(result)
        printed = buffer.getvalue()
        blocks = _blocks(result)
        for key in COVERAGE_BLOCKS:
            entries = _entries(result, key)
            if entries and not any((e.get("name") or e.get("move_off", "")) in printed
                                   for e in entries):
                problems.append(f"{owner}: {key} has {len(entries)} entries and none of their "
                                f"names appear in the printed report")
        for key, note in blocks.items():
            if key.endswith("_note") and isinstance(note, str) and note[:40] not in printed:
                problems.append(f"{owner}: {key} is computed but never printed")
    return problems


def check_every_window_gets_what_applies(league_id, ctx, results) -> list[str]:
    """Blocks that apply in every window must be computed in every window. Live: depth and
    stranded were computed inside the buy branch, which `Rebuild` returns before - so the team
    with the worst RB room in its league, and six qualifying cheap bodies available, got
    neither."""
    problems = []
    for owner, result in results.items():
        if "me" not in result:
            problems.append(f"{owner}: result has no team state")
            continue
        if result["mode"] == "rebuild" and "sell_candidates" not in result:
            problems.append(f"{owner}: rebuild result is missing its sell lists")
    return problems


CHECKS = [
    check_never_recommends_your_own_players,
    check_best_available_is_surfaced,
    check_claims_match_the_data,
    check_one_player_is_not_described_two_ways,
    check_everything_computed_is_printed,
    check_every_window_gets_what_applies,
]

# Blocks whose emptiness across *every* team in *every* league means the feature is dead
# rather than honestly quiet. This check has now retired two features rather than tuned them:
# `efficiency_swaps` sat here reporting DEAD until that turned out to be structural, and
# mutual swaps returned nothing for 36 consecutive team-reads before being deleted outright
# for a different reason (it was package math this project cannot price). `value_upgrades` is
# here because it shipped computed and unprinted - a block nobody counts is a block nobody
# notices is empty.
COVERAGE_BLOCKS = ("targets", "long_shots", "persuasion_targets", "stranded", "depth_adds",
                   "my_offers", "acquire_targets", "value_upgrades")


def audit(league_ids: list[str]) -> int:
    coverage = {block: 0 for block in COVERAGE_BLOCKS}
    failures = 0

    for league_id in league_ids:
        ctx = context(league_id)
        name = ctx.league.get("name")
        results = {}
        for roster in ctx.rosters:
            owner = ctx.owner_names.get(roster["owner_id"])
            if not owner:
                continue
            results[owner] = trade_targets.find_targets(league_id, owner)

        print(f"\n=== {name} ({len(results)} teams)")
        for check in CHECKS:
            problems = check(league_id, ctx, results)
            failures += len(problems)
            status = "PASS" if not problems else f"FAIL ({len(problems)})"
            print(f"  {status:<10} {check.__name__}")
            for problem in problems[:6]:
                print(f"             - {problem}")

        for block in COVERAGE_BLOCKS:
            coverage[block] += sum(len(_entries(r, block)) for r in results.values())

    print("\n=== coverage (a block empty across every team in every league is a dead feature)")
    for block, count in coverage.items():
        print(f"  {'DEAD' if count == 0 else 'ok  '}  {block:<20}{count:>5} entries")
        failures += 1 if count == 0 else 0

    print(f"\n{'AUDIT CLEAN' if not failures else f'AUDIT: {failures} problem(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(audit(sys.argv[1:] or DEFAULT_LEAGUES))
