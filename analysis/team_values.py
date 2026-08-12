"""Dynasty value + win-window breakdown per team. Usage: python -m analysis.team_values <league_id>"""

import sys

from sources import sleeper, fantasycalc, contracts, player_roles

# (ascending_below, declining_at_or_above), by position. Dynasty community heuristics,
# not a model: RBs decline earliest, QBs/TEs age gracefully.
AGE_CURVE = {
    "QB": (26, 34),
    "RB": (24, 27),
    "WR": (25, 29),
    "TE": (25, 30),
}

# Role-based overrides (see player_roles.py). Mobile QBs lean on athleticism, so their
# decline pulls forward; receiving-down RBs age more like WRs, so theirs pushes back; and a
# *good* pocket passer trades on arm talent and processing, which hold far longer than legs.
#
# The QB spread is now 31 / 34 / 38 rather than 31 / 34, after a sports-modelling data
# scientist argued the pocket end was too pessimistic - that a genuine pocket passer can hold
# value to nearly 40, so the gap between the two archetypes is much wider than three years.
# That is the only constant in this project changed on an outside opinion, and it was changed
# because the data backed it: over three seasons the top passing-EPA tier is Goff, Purdy,
# Stafford, Burrow and Mahomes, all pocket throwers, and the tag is earned by measured
# production rather than reputation.
#
# 38, not 40. The claim under test is that these players hold *dynasty trade value*, and a
# 39-year-old quarterback is priced on one more season however well he is playing - the curve
# should turn before the market does, not with it.
#
# **`dual_threat_qb` exists because the rushing discount assumed a QB has nothing to fall back
# on.** A quarterback who runs *and* throws at an elite level does: when the legs go he is
# still a good passer, so he should not be marked down like one whose game is only mobility.
# The two are visibly different in the same data - over three seasons Allen posts 6.05 passing
# EPA per game and Lamar 5.25, both clearing the elite-passer bar, while Hurts sits at 2.99
# with the heaviest carry rate of the three.
#
# Its cutoff is 34, which is simply the default QB curve - the point is the *absence* of a
# discount, not a new bonus, and saying so with a named tag beats leaving it implicit in an
# untagged player. Not 38: their value still leans on mobility, so they should not be priced
# like a pure pocket passer either. That the market pays 10,415 for a 29.5-year-old Allen -
# while the old curve gave him 1.5 years of runway - is the disagreement this resolves.
AGE_CURVE_OVERRIDES = {
    "rushing_qb": (26, 32),
    "dual_threat_qb": (26, 34),
    "pocket_passer": (26, 38),
    "pass_catching_rb": (24, 29),
}

# How many seasons before his own decline cutoff a player needs for his future to be worth
# anything to a plan. **The single definition of "has a future"**, shared by every caller that
# used to ask `bucket != "declining"` instead.
#
# `age_bucket` is a *discretization* of a continuous thing, and treating it as the answer
# failed three separate times in one day: a receiver 0.3 years from his cutoff offered as
# value that would "still be there in two"; an elite back 0.1 years from his read as a
# franchise cornerstone while an identical player one month older would have been a sell
# candidate; and the same boundary hiding a short-runway starter from the conversion path.
# Nobody's value falls off a cliff on a birthday - the buckets are a convenience for talking
# about age, and `years_to_decline` is the quantity underneath them.
#
# Two seasons because that is the horizon the claims actually make ("still there later", "a
# piece to build on"). Buckets are kept for the coarse questions - what kind of value is this,
# how is a roster trending - where a category is genuinely what's wanted.
MIN_MEANINGFUL_RUNWAY = 2.0

# A "declining" player still on a multi-year deal is a weaker sell than the age curve
# alone suggests - a team is still paying for the role, not just letting it expire.
#
# Years alone doesn't measure that, and reading them as if they did printed "contract-secure:
# J.K. Dobbins (2yr/$0.0M gtd)" - a line that refutes itself. 461 of 1,695 active contracts
# guarantee nothing at all, 44 of them with 2+ years left, and at ~$1M APY those are
# veteran-minimum deals a team walks away from for free. Guaranteed money is the part that
# binds a team to the role, so the flag requires both.
SECURE_YEARS_REMAINING = 2

# How many future draft classes to count as pick capital. Beyond this, picks are too
# speculative to value meaningfully and dynasty traders rarely deal that far out anyway.
FUTURE_DRAFT_YEARS = 2


def age_bucket(position: str, age: float | None, role: str | None = None) -> str:
    if age is None:
        return "unknown"
    if role in AGE_CURVE_OVERRIDES:
        young_cutoff, old_cutoff = AGE_CURVE_OVERRIDES[role]
    elif position in AGE_CURVE:
        young_cutoff, old_cutoff = AGE_CURVE[position]
    else:
        return "unknown"
    if age < young_cutoff:
        return "ascending"
    if age >= old_cutoff:
        return "declining"
    return "prime"


def years_to_decline(position: str, age: float | None, role: str | None = None) -> float | None:
    """How long until this player reaches his curve's decline cutoff. Negative once past it.

    `age_bucket` answers "what is he now" and throws away the distance to the boundary, which
    is the number a dynasty seller actually wants. Two quarterbacks can both read `prime` and
    be in completely different situations, and on one live roster they were:

        Justin Herbert   28.4  rushing_qb      2.6 years left
        Jalen Hurts      28.0  rushing_qb      3.0
        Jared Goff       31.8  pocket_passer   6.2

    The tool's sell list ranked Goff first, because it ranks on how now-weighted the market's
    price is - which is a real signal and a different question. A domain expert reading the
    same roster said sell Hurts and keep Goff: the older man throws from the pocket and has
    twice the runway, and the market has not priced the rushing decline. Both answers are
    defensible; only one of them was computable before this.

    Deliberately **not** folded into any existing sort. Two orderings competing inside one
    list is how `_buy_path` ended up ranking trade activity above value. This is reported so
    a caller can weigh runway against price, and it is the input the rebuild-timeline work
    needs: a roster whose core turns in three years cannot run a four-year teardown."""
    if age is None:
        return None
    if role in AGE_CURVE_OVERRIDES:
        _, old_cutoff = AGE_CURVE_OVERRIDES[role]
    elif position in AGE_CURVE:
        _, old_cutoff = AGE_CURVE[position]
    else:
        return None
    return round(old_cutoff - age, 1)


ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = ORDINAL_SUFFIXES.get(n % 10, "th")
    return f"{n}{suffix}"


def tertile(rank: int, count: int) -> str:
    """Which third of a ranked field a 1-based rank falls in: "top", "middle", "bottom".

    Lives here with `ordinal` and `age_bucket` as a shared pure helper. Both
    `roster_needs.assess_positions` (is this position group bottom-of-the-league?) and
    `team_state` (is this team a contender?) cut the league into thirds, and had begun
    doing it with their own inline arithmetic."""
    if rank <= count / 3:
        return "top"
    if rank > count - count / 3:
        return "bottom"
    return "middle"


def rank_map(scores: dict, high_is_first: bool = True) -> dict:
    """{key: score} -> {key: 1-based rank}. Ties break arbitrarily but stably."""
    order = sorted(scores, key=lambda k: -scores[k] if high_is_first else scores[k])
    return {key: i for i, key in enumerate(order, start=1)}


def now_premium_bar(players: dict[str, dict], percentile: float = 0.9) -> dict[str, float]:
    """Per position, the `redraft_value / value` cutoff at `percentile` of that position's
    pool - i.e. how now-weighted a player's price has to be to be extreme *for his position*.

    **This must be per-position; an absolute cutoff is a bug.** Dynasty and redraft are two
    unnormalized scales whose relationship differs sharply by position. Measured across the
    whole pool of one real league:

    | pos | p10 | median | p90 | max |
    |-----|-----|--------|-----|-----|
    | QB  | 0.26| 0.97   | 1.31| 1.60|
    | RB  | 0.07| 0.49   | 1.05| 1.54|
    | TE  | 0.03| 0.25   | 0.81| 1.01|
    | WR  | 0.03| 0.37   | 0.89| 1.07|

    A single 1.25 bar is not "strict for TEs" - it is *unreachable* for TEs and WRs, whose
    entire pools top out at 1.01 and 1.07. It silently restricts any rule using it to QBs
    and RBs. `find_value_upgrades` documents making this exact mistake once already and
    solved it by comparing pairwise within a position; this is the same fix for a rule that
    has only one player to look at, so it needs the position's distribution instead of a
    partner. Ranked against his own position, a 36.9-year-old TE at 0.83 raw is the second
    most now-weighted declining starter in the league, not a rounding error below the bar.

    A percentile, not a tuned constant, so it recalibrates with the market and with format -
    the same reasoning behind the league tertiles in `team_state`. It cannot by itself say
    "nobody qualifies", since ~10% of each position always clears it; that is deliberate.
    This measures *shape* only. Whether a player is worth having at all is an absolute
    question already answered upstream by `team_state.clears_relevance_floor`."""
    bars = {}
    for player in players.values():
        if player.get("redraft_value") and player.get("value"):
            bars.setdefault(player["position"], []).append(
                player["redraft_value"] / player["value"])
    return {pos: sorted(ratios)[int(percentile * (len(ratios) - 1))]
            for pos, ratios in bars.items()}


def team_breakdown(player_ids: list[str], players: dict[str, dict]) -> dict:
    totals = {"ascending": 0, "prime": 0, "declining": 0, "unknown": 0}
    for player_id in player_ids:
        info = players.get(player_id)
        if info is None:
            continue
        bucket = age_bucket(info["position"], info["age"], info.get("usage_role"))
        totals[bucket] += info["value"]
    return totals


def find_outliers(player_ids: list[str], players: dict[str, dict], contract_data: dict[str, dict]) -> list[str]:
    """Declining-by-age players whose contract says otherwise."""
    outliers = []
    for player_id in player_ids:
        info = players.get(player_id)
        contract = contract_data.get(player_id)
        if info is None or contract is None:
            continue
        if age_bucket(info["position"], info["age"], info.get("usage_role")) != "declining":
            continue
        if contract["years_remaining"] >= SECURE_YEARS_REMAINING and contract["guaranteed"] > 0:
            outliers.append(f"{info['name']} ({contract['years_remaining']}yr/${contract['guaranteed']:.1f}M gtd)")
    return outliers


def get_players_with_roles(num_qbs: int, num_teams: int, ppr: float, is_dynasty: bool,
                           tep_tier: str = "none") -> dict[str, dict]:
    players = fantasycalc.get_players(num_qbs, num_teams, ppr, is_dynasty, tep_tier)
    for player_id, role in player_roles.get_roles().items():
        if player_id in players:
            players[player_id]["usage_role"] = role

    # Redraft values from the same API with one flipped parameter - free, and previously
    # unused. Dynasty value prices *current production plus future years*; redraft prices
    # current production alone. The ratio between them is the share of a player's price
    # that's future potential, which is exactly what a win-now team is overpaying for.
    #
    # Real case that motivated this: a superflex team's QB2 (C.J. Stroud, dynasty 3,288,
    # redraft 2,744, premium 1.20) and QB3 (Sam Darnold, dynasty 2,735, redraft 2,704,
    # premium 1.01) produce within 1.5% of each other *this season*, but Stroud costs 553
    # more in trade value. For a win-now roster that's arbitrage - sell the premium, keep
    # the production. Ranking by dynasty value alone can't see it.
    #
    # Deliberately NOT exposing a dynasty/redraft *ratio*. A first version did, and it
    # was the `diff` mistake again: 1.0 reads as neutral, but the measured median ratio
    # across the 200 players in both pools is 2.22 (p10 0.93, p90 18.1). So a 2.01 looked
    # like "100% future premium" while actually sitting *below* typical - it flagged
    # production-oriented veterans as speculative assets. Raw redraft_value is
    # unambiguous (a price on a known scale, both pools topping out near 10,400);
    # comparisons are made pairwise within a position by find_value_upgrades, where the
    # skew cancels.
    #
    # Coverage is partial by nature of the source: redraft carries ~200 players against
    # dynasty's ~400, since deep dynasty-only assets (rookies, prospects) have no redraft
    # market. Missing entries get redraft_value=None and future_premium=None rather than a
    # fabricated number - callers must handle absence, not silently treat it as zero.
    if is_dynasty:
        # Same tep_tier: a TE-premium league scores TEs higher this season too, so the
        # redraft pull needs the adjustment as much as the dynasty one does.
        redraft = fantasycalc.get_players(num_qbs, num_teams, ppr, False, tep_tier)
        for player_id, info in players.items():
            r = redraft.get(player_id)
            info["redraft_value"] = r["value"] if r else None
    return players


# A pick's slot depends on how good the team it *originally* belongs to turns out to be,
# so a window maps to a rough draft position. FantasyCalc publishes Early/Mid/Late prices
# for the next class, which is exactly this distinction already priced by the market.
WINDOW_TO_PICK_TIER = {"Rebuild": "Early", "Middling": "Mid", "Contend": "Late", "Push": "Late"}


def owned_picks(league_id: str, season: int, draft_rounds: int, roster_ids: list[int],
                pick_values: dict[str, int],
                strategy_by_roster: dict[int, str] | None = None) -> dict[int, list[dict]]:
    """The individual future picks each roster currently owns, not just a total.

    `pick_capital` already resolves ownership through trades but sums it into one number,
    which is enough for "who has draft capital" and useless for "what could actually
    change hands". Trade suggestions need the picks themselves: a rebuilding team wants
    to *acquire* them, a win-now team should be willing to *spend* them, and neither
    conversation can happen against a single aggregate.

    **Priced by the original owner's window where possible.** A "2028 1st" is not one
    thing: a rebuilding team's first is an early pick, a contender's is a late one, and
    the market prices that difference at nearly 2x (2027 1st: Early 4,487 / Mid 2,955 /
    Late 2,263, against a flat 2,853). What decides it is how good the team the pick
    *originally* belongs to turns out to be - not who currently holds it - so a contender
    who acquired a rebuilder's first is holding an early pick and should be valued as
    such. `strategy_by_roster` supplies each roster's effective_strategy for that lookup.

    Only the *next* class has Early/Mid/Late prices published, which is the honest limit:
    a team's window is a reasonable guide to where it finishes next season, and a poor
    one two years out. Later picks keep the flat round value, and every pick records
    `slot_basis` so the distinction is visible rather than implied.

    Same two-year horizon as `pick_capital` (FUTURE_DRAFT_YEARS) - beyond that, picks are
    too speculative to price and dynasty managers rarely deal that far out.
    """
    traded = sleeper.get_traded_picks(league_id)
    traded_map = {(int(t["season"]), t["round"], t["roster_id"]): t["owner_id"] for t in traded}

    owned: dict[int, list[dict]] = {rid: [] for rid in roster_ids}
    for year_offset in range(1, FUTURE_DRAFT_YEARS + 1):
        pick_season = season + year_offset
        for round_num in range(1, draft_rounds + 1):
            name = f"{pick_season} {ordinal(round_num)}"
            flat_value = pick_values.get(name, 0)
            if not flat_value:
                continue
            for rid in roster_ids:
                current_owner = traded_map.get((pick_season, round_num, rid), rid)

                # Tier by the ORIGINAL owner's window, not the holder's.
                tier = WINDOW_TO_PICK_TIER.get((strategy_by_roster or {}).get(rid))
                tiered_value = pick_values.get(f"{name} ({tier})") if tier else None

                # Indexed, not setdefault: every owner is a roster in this league, so an
                # id that isn't already a key means the traded-pick feed disagrees with
                # the roster list and should surface, not silently create a phantom team.
                owned[current_owner].append({
                    "pick": name if not tiered_value else f"{name} ({tier})",
                    "value": tiered_value or flat_value,
                    "round": round_num,
                    "season": pick_season,
                    "originally": rid,  # whose pick it was, so "their own 1st" is visible
                    "slot_basis": (f"expected {tier.lower()} - originating team is "
                                   f"{(strategy_by_roster or {}).get(rid)}")
                                  if tiered_value else "flat round average (slot unknowable this far out)",
                })
    for picks in owned.values():
        picks.sort(key=lambda p: -p["value"])
    return owned


def pick_equivalent(value: float, pick_values: dict[str, int]) -> str | None:
    """The draft pick a player's value is closest to, e.g. "about a 2027 3rd (Late)".

    Exists because a raw number is hard to feel. Told that a bench piece is "worth 947",
    nobody knows whether that's a real asset; told it's "about a 2027 3rd", every dynasty
    manager immediately does - and the honest read of a low-value depth player is closer
    to a late pick than to a piece a trade is built around. FantasyCalc prices picks on
    the same scale as players, so this is a lookup, not a model.

    Future classes are priced as flat round averages (see get_pick_values), so the match
    is approximate by nature - hence "about".
    """
    if not pick_values or value <= 0:
        return None
    name, _ = min(pick_values.items(), key=lambda kv: abs(kv[1] - value))
    return name


def split_starters_bench(roster: dict, players: dict[str, dict],
                         starter_ids: set[str]) -> tuple[int, int]:
    """Dynasty value of the lineup vs the bench.

    `starter_ids` comes from `LeagueContext.starters` (value-derived), never from
    Sleeper's `roster["starters"]`. This function used to read that snapshot, and its
    output is what ranks the entire league (`team_state.starter_value_rank`), which in
    turn drives `is_thin`/`is_loaded` and therefore `effective_strategy` - the label every
    downstream decision branches on. The snapshot is whatever the current week's lineup
    happens to be: in a real superflex league it listed one QB, and for one team only 8
    of 10 slots were set at all."""
    all_ids = roster["players"] or []
    starter_value = sum(players[pid]["value"] for pid in starter_ids if pid in players)
    bench_value = sum(players[pid]["value"] for pid in all_ids if pid in players and pid not in starter_ids)
    return starter_value, bench_value


def pick_capital(owned: dict[int, list[dict]]) -> dict[int, int]:
    """Total future pick value per roster_id, from `owned_picks`.

    This used to be a second full copy of the traded-pick resolution loop, differing from
    `owned_picks` only in summing flat round values instead of returning the picks
    themselves. Two implementations of "who owns which future picks" meant two numbers for
    draft capital could disagree - and once `owned_picks` learned to price by the
    originating team's window, they did. Summing the same list is the one answer."""
    return {rid: sum(p["value"] for p in picks) for rid, picks in owned.items()}


def main(league_id: str) -> None:
    from .league import context
    ctx = context(league_id)
    league, fmt, players = ctx.league, ctx.fmt, ctx.players

    pick_values = fantasycalc.get_pick_values(fmt["num_qbs"], fmt["num_teams"],
                                              fmt["ppr"], fmt["is_dynasty"])
    contract_data = contracts.get_contracts()
    owner_names = ctx.owner_names

    pick_totals = pick_capital(owned_picks(
        league_id, int(league["season"]), league["settings"]["draft_rounds"],
        [r["roster_id"] for r in ctx.rosters], pick_values,
    ))

    standings = []
    for roster in ctx.rosters:
        owner = owner_names.get(roster["owner_id"], "Unknown")
        player_ids = roster["players"] or []
        starter_value, bench_value = split_starters_bench(roster, players, ctx.starters_for(roster))
        pick_value = pick_totals.get(roster["roster_id"], 0)
        breakdown = team_breakdown(player_ids, players)
        outliers = find_outliers(player_ids, players, contract_data)
        total = starter_value + bench_value + pick_value
        standings.append((owner, total, starter_value, bench_value, pick_value, breakdown, outliers))

    standings.sort(key=lambda row: row[1], reverse=True)
    print(f"{league['name']} - dynasty value standings:")
    for rank, (owner, total, starters, bench, picks, breakdown, outliers) in enumerate(standings, start=1):
        player_total = starters + bench
        pct = {b: round(100 * v / player_total) for b, v in breakdown.items()} if player_total else breakdown
        print(f"  {rank}. {owner}: {total} total  (starters {starters} / bench {bench} / picks {picks})")
        print(f"       age mix: ascending {pct['ascending']}% / prime {pct['prime']}% / declining {pct['declining']}% / unknown {pct['unknown']}%")
        if outliers:
            print(f"       declining-by-age but contract-secure: {', '.join(outliers)}")


if __name__ == "__main__":
    main(sys.argv[1])
