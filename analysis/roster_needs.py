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

from sources import injuries, degraded
from .team_values import age_bucket, rank_map, tertile

POSITIONS = ["QB", "RB", "WR", "TE"]


def dedicated_slots(roster_positions: list[str]) -> dict[str, int]:
    """How many of each position a team must own to fill its lineup. SUPER_FLEX is folded
    into QB - it's the position that realistically fills it - and counted, not treated as
    a boolean, so a league with two of them needs two extra QBs. Unlike
    `sleeper.starting_qbs` this is deliberately unclamped: it counts real roster slots."""
    return {
        "QB": roster_positions.count("QB") + roster_positions.count("SUPER_FLEX"),
        "RB": roster_positions.count("RB"),
        "WR": roster_positions.count("WR"),
        "TE": roster_positions.count("TE"),
    }


def replacement_thresholds(players: dict[str, dict], slots: dict[str, int], num_teams: int,
                           metric: str) -> dict[str, float]:
    """Replacement level per position: the Nth-best player, N = every starting slot in the
    league at that position. A win-now lens - read a rebuilder's needs as what a contending
    version of the roster would be short of. `metric` is required because the two questions
    use different currencies: "can I field a lineup" is redraft, "is this a trade chip" is
    dynasty, and conflating them is this file's central documented bug."""
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
                        metric: str, starters: set[str] | None = None) -> dict[str, list[dict]]:
    """Every rostered player at each position clearing this league's replacement level,
    best to worst - sorted by the same metric it filters on, because two metrics inside
    one ordering is the currency conflation again."""
    by_pos = {pos: [] for pos in POSITIONS}
    for pid in roster["players"] or []:
        info = players.get(pid)
        if info and info["position"] in by_pos and (info.get(metric) or 0) >= thresholds[info["position"]]:
            by_pos[info["position"]].append({"name": info["name"], "position": info["position"],
                                             "value": info["value"],
                                             "redraft_value": info.get("redraft_value"),
                                             "is_starter": pid in (starters or set())})
    for entries in by_pos.values():
        entries.sort(key=lambda e: -(e.get(metric) or 0))
    return by_pos


# A group can rank mid-table and still be badly short in absolute terms (positional
# distributions are skewed, TE especially) - half the median production is a starter's
# worth of scoring given up weekly, wherever it sorts.
WEAK_VS_MEDIAN = 0.5

# Below this, tertiles and medians describe a sample too small to mean anything, so
# quality isn't assessed and a shortage falls back to `critical` rather than a confident
# label derived from nothing.
MIN_TEAMS_FOR_QUALITY = 4

# Which shortage to go fix first. A position you can't field at all outranks one you can
# field badly.
NEED_PRIORITY = {"critical": 0, "top-heavy": 1, "weak": 2}

# A bench player marginally better than the worst starter is ordinary depth; one producing
# double is a starter's worth of scoring the lineup never collects. The live cases sit at
# 5.3x and 1.5x, so the exact figure is not doing delicate work.
STRANDED_MULTIPLE = 2.0

# Everything in this module is a win-now measurement, and a third of any league is not
# playing that game - so a rebuilder's entries carry this note. Two things flip for them:
# a need becomes descriptive, and exposure stops being a risk at all.
REBUILD_LENS = (
    "NOT A TO-DO. This team is rebuilding, so it is not trying to field the best lineup it "
    "can this season. Read this as what a CONTENDING version of this roster would be short "
    "of - useful for valuing the roster, misleading as advice. Being light at a position can "
    "be a deliberate allocation rather than a hole, and the exposure figure above is not a "
    "risk to this team: losing a starter costs it nothing it is currently playing for."
)


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


def _injury_drop(roster: dict, players: dict[str, dict], pos: str, starters: set[str],
                 dedicated: dict[str, int], flex: list[tuple[str, ...]]) -> float | None:
    """Production lost if the WEAKEST starter at `pos` goes down, by removing him and
    refilling the lineup optimally - flex slots mean the replacement can come from any
    eligible position, and the marginal lineup spot is what everyone shuffling up actually
    exposes. Magnitude if it happens, not an expected loss. None when nothing is started
    there, which is a need, not exposure."""
    mine = [p for p in starters if p in players and players[p]["position"] == pos]
    if not mine:
        return None
    weakest = min(mine, key=lambda p: players[p].get("redraft_value") or 0)
    return production_lost_without(roster, players, weakest, starters, dedicated, flex)


def weakest_starter(players: dict[str, dict], starters: set[str]) -> str | None:
    """The starter producing least - the marginal lineup spot, the same one `_injury_drop`
    reasons about. Shared so `stranded_starters` and its caller's prose read the bar off one
    definition instead of each computing their own."""
    lineup = [p for p in starters if p in players]
    return min(lineup, key=lambda p: players[p].get("redraft_value") or 0) if lineup else None


def stranded_starters(roster: dict, players: dict[str, dict], starters: set[str]) -> list[str]:
    """Bench players producing multiples of the weakest starter - the most valuable thing
    a roster owns that it cannot use (a superflex QB3, say). A magnitude test, not
    "capacity vs quality": every bench player is blocked by some mix of the two, and what
    makes an entry is the size of the idle production, kept apart from ordinary depth by
    `STRANDED_MULTIPLE`."""
    weakest = weakest_starter(players, starters)
    if weakest is None:
        return []
    bar = (players[weakest].get("redraft_value") or 0) * STRANDED_MULTIPLE
    bench = [p for p in (roster["players"] or [])
             if p not in starters and p in players
             and (players[p].get("redraft_value") or 0) > bar]
    return sorted(bench, key=lambda p: -(players[p].get("redraft_value") or 0))


def would_start_if_one_out(roster: dict, players: dict[str, dict], candidate_id: str,
                           starters: set[str], dedicated: dict[str, int],
                           flex: list[tuple[str, ...]]) -> bool:
    """Would adding this player put him in the lineup once ONE starter above him is out?
    Depth as a third state, since binary needs leave it invisible. Simulated by removing
    the weakest current starter at his position and refilling optimally, so flex
    eligibility is respected. Says nothing about price - callers pair this with "don't
    overpay", never a need."""
    position = players[candidate_id]["position"] if candidate_id in players else None
    if position is None:
        return False
    at_position = [p for p in starters if p in players and players[p]["position"] == position]
    if not at_position:
        return False  # nothing to be behind - that's a need, and needs are handled elsewhere
    weakest = min(at_position, key=lambda p: players[p].get("redraft_value") or 0)
    hypothetical = {**roster,
                    "players": [p for p in (roster["players"] or []) if p != weakest] + [candidate_id]}
    return candidate_id in projected_starters(hypothetical, players, dedicated, flex)


def replacement_is_unpriced(roster: dict, players: dict[str, dict], pos: str,
                            starters: set[str], dedicated: dict[str, int],
                            flex: list[tuple[str, ...]]) -> bool:
    """Whether the replacement after an absence at `pos` has no redraft price at all,
    which makes the drop-off an upper bound rather than a measurement - redraft coverage
    runs out well before dynasty rosters do, and saying so beats implying an empty bench."""
    mine = [p for p in starters if p in players and players[p]["position"] == pos]
    if not mine:
        return False
    weakest = min(mine, key=lambda p: players[p].get("redraft_value") or 0)
    without = {**roster, "players": [p for p in (roster["players"] or []) if p != weakest]}
    promoted = projected_starters(without, players, dedicated, flex) - starters
    return any(players[p].get("redraft_value") is None for p in promoted if p in players)


def production_lost_without(roster: dict, players: dict[str, dict], player_id: str,
                            starters: set[str], dedicated: dict[str, int],
                            flex: list[tuple[str, ...]]) -> float:
    """Current production lost if `player_id` is gone and the lineup refills optimally;
    zero means the bench covers him for free. One function because "how bad is losing him"
    and "can I afford to trade him" are the same question asked from opposite directions."""

    def produced(ids):
        return sum(players[p].get("redraft_value") or 0 for p in ids if p in players)

    without = {**roster, "players": [p for p in (roster["players"] or []) if p != player_id]}
    refilled = projected_starters(without, players, dedicated, flex)
    return produced(starters) - produced(refilled)


def backfill_for(roster: dict, players: dict[str, dict], player_id: str, starters: set[str],
                 dedicated: dict[str, int], flex: list[tuple[str, ...]]) -> dict | None:
    """WHO steps into the lineup if `player_id` leaves - the reason
    `production_lost_without` is small whenever it is small. The highest-producing
    promotion is the direct replacement; the rest is the lineup reshuffling behind."""
    without = {**roster, "players": [p for p in (roster["players"] or []) if p != player_id]}
    promoted = projected_starters(without, players, dedicated, flex) - starters
    entries = [players[p] for p in promoted if p in players]
    if not entries:
        return None
    best = max(entries, key=lambda e: e.get("redraft_value") or 0)
    return {"name": best["name"], "position": best["position"],
            "redraft_value": best.get("redraft_value") or 0}


def assess_positions(rosters: list[dict], players: dict[str, dict], slots: dict[str, int],
                     thresholds: dict[str, float],
                     starters: dict[str, set[str]] | None = None,
                     lineup: tuple[dict, list] | None = None,
                     position_rates: dict[str, float] | None = None) -> dict[str, dict[str, dict]]:
    """Every roster's standing at every position, keyed owner_id -> position. Count and
    quality are separate problems with opposite fixes, so the level names the SHAPE of the
    problem:

    - `critical`   - can't field the slots AND the group is weak. A real hole.
    - `top-heavy`  - can't field the slots, but what's there is good. Wants bodies.
    - `weak`       - slots fillable, group bottom-tertile or under `WEAK_VS_MEDIAN` of the
                     median. Wants an upgrade, not depth.
    - `ok`         - neither; middle-of-the-league with no star is not a need.

    Injury exposure (`drop_if_injured`, ranked) is measured but is deliberately NOT a
    need - it is a separate axis, and the drop-off sidesteps the replacement bar, which by
    construction leaves almost nobody with "startable bench". Numbers ship with the
    sentence that interprets them. Why the bare count this replaced was nearly inverted:
    LOGIC.md, "Positional needs"."""
    groups = {r["owner_id"]: _starting_group(r, players, slots) for r in rosters}
    usable = {r["owner_id"]: _usable_by_position(r, players, thresholds, "redraft_value")
              for r in rosters}
    by_owner = {r["owner_id"]: r for r in rosters}

    num_teams = len(rosters)
    top_third = num_teams / 3
    bottom_third = num_teams - num_teams / 3

    out: dict[str, dict[str, dict]] = {owner_id: {} for owner_id in groups}
    for pos in POSITIONS:
        totals = {owner_id: sum(g[pos]) for owner_id, g in groups.items()}
        ranks = {owner_id: i for i, owner_id
                 in enumerate(sorted(totals, key=lambda o: -totals[o]), start=1)}
        median = statistics.median(totals.values()) if totals else 0

        # Exposure is ranked worst-first: the biggest drop is rank 1, the most exposed.
        unpriced = ({oid: replacement_is_unpriced(by_owner[oid], players, pos, starters[oid], *lineup)
                     for oid in by_owner} if lineup and starters else {})
        drops = ({oid: _injury_drop(by_owner[oid], players, pos, starters[oid], *lineup)
                  for oid in groups} if starters and lineup else {})
        drop_rank = rank_map({o: d for o, d in drops.items() if d is not None})

        # Quality PER STARTABLE BODY, for judging count-short groups: ranking the group
        # TOTAL let the empty slot drag the verdict on the players who exist - a
        # superflex room of Mahomes-plus-nobody ranked "among the league's worst" and
        # read as critical, when the honest label is top-heavy (a body, NOT an upgrade).
        # The hole is the count problem; quality is about the bodies.
        per_body = {oid: totals[oid] / len(usable[oid][pos]) if usable[oid][pos] else 0
                    for oid in groups}
        body_ranks = {oid: i for i, oid
                      in enumerate(sorted(per_body, key=lambda o: -per_body[o]), start=1)}
        body_median = statistics.median(per_body.values()) if per_body else 0

        # Raw roster bodies, because "1 startable QB for 2 slots" read as "they only have
        # one QB" about a room holding FOUR (Love, Willis, Rodgers, Penix - only Love
        # clears the startable bar). The count claim is about the bar; a manager who
        # knows the roster hears it as a count of players and catches the tool "wrong".
        bodies = {oid: sum(1 for pid in (by_owner[oid]["players"] or [])
                           if pid in players and players[pid]["position"] == pos)
                  for oid in groups}

        quality_known = num_teams >= MIN_TEAMS_FOR_QUALITY
        for owner_id, group in groups.items():
            total, rank = totals[owner_id], ranks[owner_id]
            count, required = len(usable[owner_id][pos]), slots[pos]
            is_weak = quality_known and (rank > bottom_third or total < median * WEAK_VS_MEDIAN)

            if count < required:
                # Judged on the bodies that exist, not the hole-dragged total. Without a
                # quality read there's no basis to call a shortage merely top-heavy, so
                # it stays `critical` - the old, conservative label.
                body_weak = (body_ranks[owner_id] > bottom_third
                             or per_body[owner_id] < body_median * WEAK_VS_MEDIAN)
                level = "top-heavy" if (quality_known and not body_weak) else "critical"
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
                "rostered_bodies": bodies[owner_id],
                "note": _position_note(pos, level, count, required, total, rank, num_teams,
                                       median, top_third, body_ranks[owner_id],
                                       bodies[owner_id]),
            }
            drop = drops.get(owner_id)
            entry = out[owner_id][pos]
            entry["drop_if_injured"] = round(drop) if drop is not None else None
            entry["exposure"] = entry["exposure_rank"] = None
            if drop is not None:
                entry["exposure_rank"] = drop_rank[owner_id]
                entry["exposure"] = {"top": "high", "middle": "typical", "bottom": "low"}[
                    tertile(drop_rank[owner_id], len(drop_rank))]
                # The likelihood half: QBs miss ~11% of their weeks against ~19% for RBs,
                # so an equal drop at two positions is not the same problem - said with a
                # number instead of a disclaimer.
                rate = position_rates.get(pos) if position_rates else None
                likelihood = (
                    f" Likelihood: {pos}s have missed {rate:.0%} of their roster weeks over "
                    f"the last three seasons, so weigh this against positions with a "
                    f"different rate rather than against the raw number alone."
                    if rate is not None else
                    " Likelihood is not modelled here, so compare this only against the same "
                    "position.")
                entry["position_miss_rate"] = rate
                entry["replacement_unpriced"] = unpriced.get(owner_id, False)
                caveat = (
                    " NOTE: the player who would replace him has no redraft price in the "
                    "market data, so this figure treats him as producing nothing and is an "
                    "upper bound rather than a measurement - redraft coverage runs out well "
                    "before dynasty rosters do."
                    if unpriced.get(owner_id) else "")
                # Low exposure at a position you cannot field is a SYMPTOM, not comfort. It
                # measures the drop from the last starter to his replacement, so when the last
                # starter is already below replacement level the drop is small *because the
                # slot is already lost*. Read straight, "low exposure" on a critical need
                # reassured a manager about the hole the same paragraph had just described.
                consolation = (
                    " Low only because the last starter here is already so weak that replacing "
                    "him costs little - that is the hole above restated, not comfort about it."
                    if entry["exposure"] == "low"
                    and entry["level"] in ("critical", "top-heavy") else "")
                entry["note"] += (
                    f" Depth: losing the last {pos} in this lineup costs {round(drop):,} of "
                    f"production before a replacement starts, {entry['exposure_rank']} of "
                    f"{len(drop_rank)} in the league - {entry['exposure']} exposure.{consolation}"
                    f" This is the magnitude IF it happens, not an expected loss.{likelihood}"
                    f"{caveat} Separate from the need above, and not one.")
    return out


def _position_note(pos: str, level: str, count: int, required: int, total: float, rank: int,
                   num_teams: int, median: float, top_third: float,
                   body_rank: int | None = None, bodies: int | None = None) -> str:
    have = f"No startable {pos}s" if count == 0 else f"{count} startable {pos}{'' if count == 1 else 's'}"
    short = f"{have} for {required} slot{'' if required == 1 else 's'}"
    # "Startable" is a bar, not a headcount - without saying so, "1 startable QB" about a
    # four-QB room reads as a count of players and gets caught as wrong by anyone who
    # knows the roster. The gap is the QUALITY of the extra bodies, not their existence.
    if bodies is not None and bodies > count:
        short += (f" ({bodies} {pos}s rostered - the other {bodies - count} sit below the "
                  f"startable bar, so the gap is the quality of the next body, not an "
                  f"empty room)")
    standing = (f"Starting {pos} production ranks {rank} of {num_teams} "
                f"({round(total):,} against a league median of {round(median):,}).")

    if level == "critical":
        return (f"{short}, and the group is among the league's worst. {standing} "
                f"A real hole - needs both bodies and quality.")
    if level == "top-heavy":
        # The total is dragged by the hole, and saying so is the whole point of this
        # label: "ranks 9 of 12" about a Mahomes-plus-nobody room reads as a weak group,
        # and a manager who knows the room will (rightly) call that wrong.
        strength = ("among the league's best per starter" if body_rank and body_rank <= top_third
                    else "solid per starter")
        return (f"{short}, but the {pos}{'' if count == 1 else 's'} actually started "
                f"{'is' if count == 1 else 'are'} {strength} "
                f"(rank {body_rank} of {num_teams} per body). Group total ranks {rank} of "
                f"{num_teams} ({round(total):,} against a median of {round(median):,}) - "
                f"dragged by the empty slot, not by the players. Needs a body to fill the "
                f"slot, not an upgrade at the top - the good players are already here.")
    if level == "weak":
        return (f"{have} covers all {required} slot{'' if required == 1 else 's'}, so this "
                f"isn't a shortage of bodies. {standing} The group itself is the problem - "
                f"this wants an upgrade (consolidating depth into one better starter), not "
                f"more depth.")
    return f"{have} for {required} slot{'' if required == 1 else 's'}, and no quality shortfall. {standing} Not a need."


def needs_only(assessed: dict[str, dict]) -> dict[str, dict]:
    """The not-`ok` positions from one roster's assessment. Deliberately no single-roster
    entry point exists: quality is league-relative, and a per-roster version would quietly
    degrade to a 1-of-1 ranking."""
    return {pos: entry for pos, entry in assessed.items() if entry["level"] != "ok"}


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
    """Player ids a team would actually start - the single definition of "starter" in
    this project (`LeagueContext.starters` calls it once; everything else reads that).
    Not Sleeper's snapshot, which is whatever the current week's lineup happens to be and
    once classed a superflex QB2 as spare parts. Ranked by redraft value (a lineup is "who
    scores most this week", in every window; missing prices sort last, safely below every
    replacement level), with flex slots filled properly - dedicated first, then flex, most
    restrictive first."""
    return {pid for _, pid in fill_lineup(roster, players, slots, flex)}


# Reverse of FLEX_ELIGIBILITY, so a filled flex slot can say which kind it was.
_FLEX_NAME = {positions: name for name, positions in FLEX_ELIGIBILITY.items()}


def fill_lineup(roster: dict, players: dict[str, dict], slots: dict[str, int],
                flex: list[tuple[str, ...]] | None = None) -> list[tuple[str, str]]:
    """The lineup as `[(slot_label, player_id)]` - `projected_starters` keeping which slot
    each player occupies, which is the whole answer to "what happens if X goes down".
    Exposed as a tool so the model never fills flex slots in prose, which it gets subtly
    wrong (a vacated FLEX correctly went to a tight end, not the assumed WR)."""
    by_pos: dict[str, list[tuple[float, str]]] = {pos: [] for pos in POSITIONS}
    for pid in roster["players"] or []:
        info = players.get(pid)
        if info and info["position"] in by_pos:
            by_pos[info["position"]].append((info.get("redraft_value") or 0, pid))

    filled: list[tuple[str, str]] = []
    remaining: dict[str, list[tuple[float, str]]] = {}
    for pos, entries in by_pos.items():
        entries.sort(reverse=True)
        take = slots.get(pos, 0)
        filled += [(pos, pid) for _, pid in entries[:take]]
        remaining[pos] = entries[take:]

    # Then flex, most restrictive slot first - otherwise a SUPER_FLEX (any position)
    # can take a player that a narrower FLEX (RB/WR/TE only) was the sole home for.
    for eligible in sorted(flex or [], key=len):
        pool = [(v, p) for pos in eligible for v, p in remaining.get(pos, [])]
        if not pool:
            continue
        _, pid = max(pool)
        filled.append((_FLEX_NAME.get(tuple(eligible), "FLEX"), pid))
        for pos in eligible:
            remaining[pos] = [(v, p) for v, p in remaining.get(pos, []) if p != pid]
    return filled


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
    # Second unguarded nflverse dependency, found the same way as the first: a GitHub outage
    # took the whole audit down. Miss rates are the *likelihood* half of the exposure note and
    # `assess_positions` already treats them as optional, so passing None omits that sentence -
    # honest, where crashing is not.
    try:
        rates = injuries.position_miss_rates()
    except Exception as e:
        print(f"WARNING: injury miss rates unavailable ({type(e).__name__}) - exposure notes "
              f"state magnitude without likelihood this run.", file=sys.stderr)
        degraded.record("injury miss rates", "exposure notes give the magnitude of losing a "
                                            "starter without the likelihood of it happening")
        rates = None
    return assess_positions(ctx.rosters, ctx.players, ctx.needs_slots, ctx.start_thresholds,
                            ctx.starters, (ctx.lineup_dedicated, ctx.lineup_flex), rates)


def league_needs(league_id: str) -> dict[str, dict]:
    """Positional needs for every roster, keyed by owner_id.

    Rebuilding teams' entries are marked `applies_this_season: False` and carry
    `REBUILD_LENS`, because every number in this module is a win-now measurement and a
    rebuilder is not playing that game."""
    from . import team_state
    windows = {row["owner_id"]: row["window"] for row in team_state.classify_league(league_id)}
    out = {}
    for owner_id, assessed in league_assessment(league_id).items():
        needs = needs_only(assessed)
        rebuilding = windows.get(owner_id) == "Rebuild"
        for entry in needs.values():
            entry["applies_this_season"] = not rebuilding
            if rebuilding:
                entry["note"] += f" {REBUILD_LENS}"
        out[owner_id] = needs
    return out


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
