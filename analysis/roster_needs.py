"""Which positions a team is actually short at, in the two different ways that can be
true: **count** (not enough bodies clearing the bar) and **quality** (enough bodies, but
the group is bad compared to the rest of the league). These are separate problems with
opposite fixes - one wants any warm body, the other wants an upgrade - and collapsing
them into one "thin" label was actively misleading. See `assess_positions`.

"Usable" is relative to the league's own format: the Nth-best player at a position
leaguewide, where N = how many starting slots the whole league has at that position,
sets the bar - not a hardcoded value cutoff.

Smoke test: python -m analysis.roster_needs <league_id>
"""

import statistics
import sys

from sources import sleeper

POSITIONS = ["QB", "RB", "WR", "TE"]


def dedicated_slots(roster_positions: list[str], is_superflex: bool) -> dict[str, int]:
    return {
        "QB": roster_positions.count("QB") + (1 if is_superflex else 0),
        "RB": roster_positions.count("RB"),
        "WR": roster_positions.count("WR"),
        "TE": roster_positions.count("TE"),
    }


def replacement_thresholds(players: dict[str, dict], slots: dict[str, int], num_teams: int,
                           metric: str = "value") -> dict[str, float]:
    """Replacement level per position: the Nth-best player, N = every starting slot in
    the league at that position.

    **`metric` matters, because two different questions use this.** "Can I field a
    lineup?" is about *current production* (`redraft_value`); "is this a real trade
    chip?" is about *dynasty value*. Using dynasty value for the first was badly wrong:
    it asks whether a player beats the 36th-most-*valuable* WR, a pool full of young
    prospects priced on upside, rather than the 36th-best current producer. Measured on
    a real league the gap is 2.5x at WR (2,126 vs 855) and 3.2x at TE (2,013 vs 630),
    which marked a team with three startable WRs and two startable TEs as *critical* at
    both."""
    thresholds = {}
    for pos, starters_needed in slots.items():
        pos_values = sorted((info.get(metric) for info in players.values()
                             if info["position"] == pos and info.get(metric)), reverse=True)
        if not pos_values:
            thresholds[pos] = 0
            continue
        rank = min(num_teams * starters_needed, len(pos_values)) - 1
        thresholds[pos] = pos_values[max(rank, 0)]
    return thresholds


def _usable_by_position(roster: dict, players: dict[str, dict], thresholds: dict[str, float],
                        metric: str = "redraft_value") -> dict[str, list[dict]]:
    """Every rostered player at each position that clears this league's replacement
    level, best to worst. The one shared walk `assess_positions` and `find_surplus` both
    read from, so "usable" means exactly the same thing in a need (too few of them) as it
    does in a surplus (more than the starting slots require).

    **Sorted by the same metric it filters on.** It previously filtered on redraft value
    and then sorted on dynasty value, so `find_surplus` - which calls the top `slots[pos]`
    entries your starters and everything after them spare - could hand a better current
    producer to the surplus pile while keeping a pricier prospect. Two metrics inside one
    ordering is the same conflation `replacement_thresholds` documents at length."""
    by_pos = {pos: [] for pos in POSITIONS}
    for pid in roster["players"] or []:
        info = players.get(pid)
        if info and info["position"] in by_pos and (info.get(metric) or 0) >= thresholds[info["position"]]:
            by_pos[info["position"]].append({"name": info["name"], "position": info["position"],
                                             "value": info["value"],
                                             "redraft_value": info.get("redraft_value")})
    for entries in by_pos.values():
        entries.sort(key=lambda e: -(e.get(metric) or 0))
    return by_pos


# A group can rank mid-table and still be badly short in absolute terms, because
# positional distributions are skewed - TE especially. In a real league the 8th-best TE
# room (648) was 39% of the league median (1,667) and 10% of the best (6,670); rank alone
# called that "average". Half the league's median production from a position means giving
# up roughly a full starter's worth of scoring against a typical opponent every week,
# which is a real need regardless of where it happens to sort.
WEAK_VS_MEDIAN = 0.5

# Below this, "bottom tertile" and "league median" describe a sample too small to carry
# either meaning - in a 1-team league every rank is simultaneously first and last, which
# made the naive tertile test call *every* position weak. Quality simply isn't assessed
# there; the count test stands alone and a shortage falls back to `critical`, rather than
# emitting a confident label derived from nothing. `format_support` already flags leagues
# under 8 teams as degraded for the same underlying reason.
MIN_TEAMS_FOR_QUALITY = 4

# Which shortage to go fix first. A position you can't field at all outranks one you can
# field badly.
NEED_PRIORITY = {"critical": 0, "top-heavy": 1, "weak": 2}


def _starting_group(roster: dict, players: dict[str, dict], slots: dict[str, int]) -> dict[str, list[float]]:
    """Each position's best `slots[pos]` players by **redraft** value.

    Deliberately a uniform top-N per position rather than `projected_starters`' real
    lineup. Comparing WR rooms across teams has to compare the same shape - a team that
    happens to flex two WRs would otherwise show a bigger "WR room" than one that flexes
    RBs, which measures lineup construction, not receiving talent. `projected_starters`
    remains the right answer for "who do I actually start"; this is for "who's better at
    this position".

    Redraft, not dynasty: "is my WR room good" is a current-production question. Using
    dynasty value here would rate a room of prospects above a room of producers.
    """
    by_pos: dict[str, list[float]] = {pos: [] for pos in POSITIONS}
    for pid in roster["players"] or []:
        info = players.get(pid)
        if info and info["position"] in by_pos:
            by_pos[info["position"]].append(info.get("redraft_value") or 0)
    return {pos: sorted(vals, reverse=True)[:slots[pos]] for pos, vals in by_pos.items()}


def assess_positions(rosters: list[dict], players: dict[str, dict], slots: dict[str, int],
                     thresholds: dict[str, float]) -> dict[str, dict[str, dict]]:
    """Every roster's standing at every position, keyed owner_id -> position.

    **Why this replaced a bare count.** The old rule was purely "how many players clear
    replacement level": fewer than the starting slots = critical, exactly = thin. Measured
    against a real 12-team superflex league, that rule was close to *inverted*:

    - The team with the **2nd-best WR room in the league** (13,141 of starting production,
      Nacua + Nabers) read `critical` at WR, because its WR3 sat below the bar.
    - The team with the **10th-best WR room** (6,308) read as having no WR need at all,
      because four players cleared a low bar (794) by a little.
    - One team was told it was "thin at WR" - where it ranked a perfectly ordinary 7th of
      12 - while its genuinely bad positions, QB (9th) and TE (8th, at 39% of the league
      median), were reported as fine. In a superflex league, no less.

    Replacement level answers "can this player start", which is a floor. It cannot answer
    "is this group good", and a floor test applied to a quality question passes teams that
    are merely numerous and fails teams that are merely top-heavy.

    So each position now carries both readings, and the level names the shape of the
    problem rather than its severity alone:

    - `critical`   - can't field the slots AND the group is weak. A real hole.
    - `top-heavy`  - can't field the slots, but what's there is good. Wants *bodies*;
                     the stars are already in place.
    - `weak`       - can field the slots, but the group is bottom-tertile or under
                     `WEAK_VS_MEDIAN` of the league median. Wants an *upgrade*, not depth.
                     This is the case that had no representation at all before.
    - `ok`         - neither. Notably includes "middle of the league with no star", which
                     is not a need; it's an average position, and calling it one sent
                     teams shopping for problems they didn't have.

    Numbers ship with the sentence that interprets them, for the reason documented on
    `team_state.age_mix_note`: an unlabelled number in a tool result gets a meaning
    invented for it.
    """
    groups = {r["owner_id"]: _starting_group(r, players, slots) for r in rosters}
    usable = {r["owner_id"]: _usable_by_position(r, players, thresholds) for r in rosters}

    num_teams = len(rosters)
    top_third = num_teams / 3
    bottom_third = num_teams - num_teams / 3

    out: dict[str, dict[str, dict]] = {owner_id: {} for owner_id in groups}
    for pos in POSITIONS:
        totals = {owner_id: sum(g[pos]) for owner_id, g in groups.items()}
        ranks = {owner_id: i for i, owner_id
                 in enumerate(sorted(totals, key=lambda o: -totals[o]), start=1)}
        median = statistics.median(totals.values()) if totals else 0

        quality_known = num_teams >= MIN_TEAMS_FOR_QUALITY
        for owner_id, group in groups.items():
            total, rank = totals[owner_id], ranks[owner_id]
            count, required = len(usable[owner_id][pos]), slots[pos]
            is_weak = quality_known and (rank > bottom_third or total < median * WEAK_VS_MEDIAN)

            if count < required:
                # Without a quality read there's no basis to call a shortage merely
                # top-heavy, so it stays `critical` - the old, conservative label.
                level = "top-heavy" if (quality_known and not is_weak) else "critical"
            elif is_weak:
                level = "weak"
            else:
                level = "ok"

            out[owner_id][pos] = {
                "level": level,
                "startable": count,
                "slots": required,
                "starting_production": round(total),
                "rank": rank,
                "of": num_teams,
                "league_median": round(median),
                "best": round(group[pos][0]) if group[pos] else 0,
                # The bar an acquisition has to clear to actually improve the starting
                # group rather than just join it - what `weak` needs, by definition.
                "weakest_starter": round(group[pos][-1]) if group[pos] else 0,
                "note": _position_note(pos, level, count, required, total, rank, num_teams,
                                       median, top_third),
            }
    return out


def _position_note(pos: str, level: str, count: int, required: int, total: float, rank: int,
                   num_teams: int, median: float, top_third: float) -> str:
    have = f"No startable {pos}s" if count == 0 else f"{count} startable {pos}{'' if count == 1 else 's'}"
    short = f"{have} for {required} slot{'' if required == 1 else 's'}"
    standing = (f"Starting {pos} production ranks {rank} of {num_teams} "
                f"({round(total):,} against a league median of {round(median):,}).")

    if level == "critical":
        return (f"{short}, and the group is among the league's worst. {standing} "
                f"A real hole - needs both bodies and quality.")
    if level == "top-heavy":
        strength = "one of the league's best" if rank <= top_third else "solidly mid-league"
        return (f"{short}, but what's there is {strength}. {standing} Needs a body to fill "
                f"the slot, not an upgrade at the top - the good players are already here.")
    if level == "weak":
        return (f"{have} covers all {required} slot{'' if required == 1 else 's'}, so this "
                f"isn't a shortage of bodies. {standing} The group itself is the problem - "
                f"this wants an upgrade (consolidating depth into one better starter), not "
                f"more depth.")
    return f"{have} for {required} slot{'' if required == 1 else 's'}, and no quality shortfall. {standing} Not a need."


def needs_only(assessed: dict[str, dict]) -> dict[str, dict]:
    """The not-`ok` positions from one roster's assessment.

    There is deliberately no single-roster `find_needs` any more. Quality is measured
    against the rest of the league, so a per-roster entry point would have had to either
    take the league as an argument anyway or quietly degrade to a 1-of-1 ranking - and a
    function that silently answers a different question than the one asked is how the old
    count-only rule survived as long as it did."""
    return {pos: entry for pos, entry in assessed.items() if entry["level"] != "ok"}


def find_surplus(roster: dict, players: dict[str, dict], slots: dict[str, int],
                 thresholds: dict[str, float], projected: set[str] | None = None) -> dict:
    """The mirror of a need: positions where a team has MORE usable players than its
    starting slots require, and which players specifically are the spare ones. Only
    players beyond the required starter count count as surplus - the top `slots[pos]`
    are the actual starting group and never get offered here. This is what makes a
    win-now-to-win-now swap real: a team's true extra depth at a position it doesn't
    need, not just any valuable player on the roster.

    **`projected` removes anyone who actually starts.** `slots` here is `needs_slots`,
    which folds SUPER_FLEX into a QB and ignores FLEX entirely - so in a league running
    RB 2 / FLEX 2, a team's third RB sits outside `slots["RB"]` and was being offered as
    spare depth while starting every week. A real case: rjl22's RB3 (Ashton Jeanty, a
    genuine asset) was offered in a mutual swap for a fringe QB on exactly this basis.
    Same snapshot-vs-projected distinction `_my_offer_pool` already respects; this path
    never got it."""
    usable = _usable_by_position(roster, players, thresholds)
    spare = {pos: [e for e in usable[pos][slots[pos]:]
                   if projected is None or e["name"] not in projected]
             for pos in POSITIONS if len(usable[pos]) > slots[pos]}
    return {pos: entries for pos, entries in spare.items() if entries}


# Which positions each flex-type slot can be filled with. Sleeper names these in
# `roster_positions` alongside the dedicated ones.
FLEX_ELIGIBILITY = {
    "FLEX": ("RB", "WR", "TE"),
    "WRRB_FLEX": ("RB", "WR"),
    "REC_FLEX": ("WR", "TE"),
    "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
}


def lineup_slots(roster_positions: list[str]) -> tuple[dict[str, int], list[tuple[str, ...]]]:
    """Split a league's roster_positions into dedicated slots and flex slots.

    `dedicated_slots` deliberately ignores flex (a disclosed approximation), which is
    fine for "how many of this position must I have" but wrong for building a lineup.
    A real league here runs QB 1 / RB 2 / WR 3 / TE 1 / FLEX 2 / SUPER_FLEX 1 - ten
    starters, of which three are flexible. Ignoring those three both under-counts the
    lineup and, by folding SUPER_FLEX into a second dedicated QB, asserts a QB must fill
    it when any position can.
    """
    dedicated = {pos: roster_positions.count(pos) for pos in POSITIONS}
    flex = [FLEX_ELIGIBILITY[p] for p in roster_positions if p in FLEX_ELIGIBILITY]
    return dedicated, flex


def projected_starters(roster: dict, players: dict[str, dict], slots: dict[str, int],
                       flex: list[tuple[str, ...]] | None = None) -> set[str]:
    """Names of the players a team would actually start, derived from value and the
    league's own slot counts.

    Deliberately *not* Sleeper's `starters` field. That reflects whatever the current
    week's lineup happens to be, which is meaningless before Week 1 - in a real
    superflex league (2 QB slots) the snapshot listed exactly one QB as a starter, so
    the team's obvious QB2 (C.J. Stroud, ascending, 3,288 value) was classed as bench
    and offered up as spare parts. In superflex especially, a second QB is among the
    most valuable things on a roster, not dead weight.

    **Ranked by redraft value, not dynasty value.** A lineup is purely "who scores most
    this week", which is what redraft prices measure; dynasty value governs who you keep
    or trade, not who you start. Ranking by dynasty put the wrong player in the lineup -
    a real league showed a TE whose bench alternative produced *102%* of his current
    output, i.e. the better current player was sitting. This holds for every window, not
    just Win-Now: a rebuilding team still starts its best scorers.

    Players with no redraft price sort last. That's safe rather than lossy: redraft
    covers the top ~200, and the highest-dynasty rostered player missing one across a
    real 12-team league was 1,350 - far below every positional replacement level, so
    nobody actually startable is affected.

    **Flex slots are filled properly**, which matters more than it sounds. A real league
    runs QB 1 / RB 2 / WR 3 / TE 1 / FLEX 2 / SUPER_FLEX 1 - so a team with three
    excellent RBs starts all three (two at RB, one at FLEX), and a superflex QB2 occupies
    the SUPER_FLEX. Modelling only dedicated slots claimed 8 starters where there are 10
    and treated the third RB as spare parts. Dedicated slots fill first, then flex, most
    restrictive first so a SUPER_FLEX doesn't take a player only a narrower FLEX could use.
    """
    by_pos: dict[str, list[tuple[float, str]]] = {pos: [] for pos in POSITIONS}
    for pid in roster["players"] or []:
        info = players.get(pid)
        if info and info["position"] in by_pos:
            by_pos[info["position"]].append((info.get("redraft_value") or 0, info["name"]))

    starters: set[str] = set()
    remaining: dict[str, list[tuple[float, str]]] = {}
    for pos, entries in by_pos.items():
        entries.sort(reverse=True)
        take = slots.get(pos, 0)
        starters.update(name for _, name in entries[:take])
        remaining[pos] = entries[take:]

    # Then flex, most restrictive slot first - otherwise a SUPER_FLEX (any position)
    # can take a player that a narrower FLEX (RB/WR/TE only) was the sole home for.
    for eligible in sorted(flex or [], key=len):
        pool = [(v, n) for pos in eligible for v, n in remaining.get(pos, [])]
        if not pool:
            continue
        value, name = max(pool)
        starters.add(name)
        for pos in eligible:
            remaining[pos] = [(v, n) for v, n in remaining.get(pos, []) if n != name]
    return starters




def league_projected_starters(league_id: str) -> dict[str, set[str]]:
    """Projected starting lineup per roster, keyed by owner_id - the value-derived
    version of "who's actually in the lineup", used by trade_targets so it never
    offers away a real starter on the strength of a stale preseason snapshot."""
    from .league import context
    ctx = context(league_id)
    # lineup_* rather than needs_slots: the real lineup has FLEX slots and a SUPER_FLEX
    # that takes any position, where needs_slots folds SUPER_FLEX into a second QB.
    return {
        r["owner_id"]: projected_starters(r, ctx.players, ctx.lineup_dedicated, ctx.lineup_flex)
        for r in ctx.rosters
    }


def league_thresholds(league_id: str) -> dict[str, float]:
    """Dynasty-value replacement level - the bar for "is this a real trade chip", used by
    trade_targets for the relevance floor and value-over-replacement tiering. Kept
    separate from the redraft-based startability bar used by find_needs."""
    from .league import context
    return context(league_id).trade_thresholds


def league_assessment(league_id: str) -> dict[str, dict[str, dict]]:
    """Every roster's standing at every position, keyed by owner_id. The full picture,
    including the positions that are fine - `league_needs` is the filtered view."""
    from .league import context
    ctx = context(league_id)
    return assess_positions(ctx.rosters, ctx.players, ctx.needs_slots, ctx.start_thresholds)


def league_needs(league_id: str) -> dict[str, dict]:
    """Positional needs for every roster, keyed by owner_id."""
    return {owner_id: needs_only(assessed)
            for owner_id, assessed in league_assessment(league_id).items()}


def league_surplus(league_id: str) -> dict[str, dict]:
    """Positional surplus for every roster, keyed by owner_id - the mirror of
    league_needs, reused by trade_targets.find_mutual_swaps to match one team's
    spare depth against another's need."""
    from .league import context
    ctx = context(league_id)
    projected = league_projected_starters(league_id)
    return {
        r["owner_id"]: find_surplus(r, ctx.players, ctx.needs_slots, ctx.start_thresholds,
                                    projected.get(r["owner_id"]))
        for r in ctx.rosters
    }


def main(league_id: str) -> None:
    needs_by_owner_id = league_needs(league_id)
    owner_names = {u["user_id"]: u["display_name"] for u in sleeper.get_users(league_id)}

    for owner_id, needs in needs_by_owner_id.items():
        owner = owner_names.get(owner_id, "Unknown")
        if not needs:
            print(f"  {owner}: no positional needs")
            continue
        summary = ", ".join(f"{pos} ({e['level']}, {e['rank']}/{e['of']})" for pos, e in needs.items())
        print(f"  {owner}: {summary}")
        for pos, entry in needs.items():
            print(f"       {entry['note']}")


if __name__ == "__main__":
    main(sys.argv[1])
