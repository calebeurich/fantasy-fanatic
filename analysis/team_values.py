"""Dynasty value + win-window breakdown per team. Usage: python -m analysis.team_values <league_id>"""

import sys

from sources import sleeper, fantasycalc, contracts, player_roles, degraded

# (ascending_below, declining_at_or_above), by position. Dynasty community heuristics,
# not a model: RBs decline earliest, QBs/TEs age gracefully.
AGE_CURVE = {
    "QB": (27, 34),
    "RB": (24, 27),
    "WR": (25, 29),
    "TE": (25, 30),
}

# Role overrides on the curve (see player_roles.py). Mobility-dependent QBs decline
# earlier; a dual threat keeps the default because elite passing survives the legs; a
# pocket passer holds value far longer (37 - the curve should turn before the market
# does, and a 34-year-old pocket passer is fine but not a green light; tuned down from
# 38 on the author's eye test); receiving-down RBs age more like WRs. Tags are earned
# by measured usage, not reputation - LOGIC.md, "Age curves and runway".
# QB young cutoff is 27 everywhere, one later than the other positions' entries: the
# position takes years to develop, so the peak arrives later on both ends (the exit
# side is the pocket/rushing spread below).
AGE_CURVE_OVERRIDES = {
    "rushing_qb": (27, 32),
    "dual_threat_qb": (27, 34),
    "pocket_passer": (27, 37),
    "pass_catching_rb": (24, 29),
}

def prime_span(position: str, usage_role: str | None = None) -> float | None:
    """Length of the prime window on this player's OWN curve (role-aware) - the
    denominator for "how far into his prime is he", which the UI shades on."""
    young, old = AGE_CURVE_OVERRIDES.get(usage_role) or AGE_CURVE.get(position, (None, None))
    return old - young if young is not None else None


# The single definition of "has a future": seasons before his own decline cutoff, the
# horizon claims like "still there later" actually make. Buckets are only a discretization
# of this - nobody's value falls off a cliff on a birthday - so anywhere a boundary decides
# something, use the runway (LOGIC.md, "Age curves and runway").
MIN_MEANINGFUL_RUNWAY = 2.0

# Inside his final year: is the player at his own edge right now. Distinct from the buyer's
# two-season horizon above because curves differ in width - on the RB (24, 27) curve "under
# 2.0 of runway" means any RB over 25, which is a seller test gone wrong.
INSIDE_FINAL_YEAR = 1.0

# A declining-by-age player is contract-secure only with years AND guaranteed money - 461
# of 1,695 active contracts guarantee nothing, and those are deals a team walks away from
# for free.
SECURE_YEARS_REMAINING = 2

# How many future draft classes to count as pick capital. Three, because that is what
# actually exists: Sleeper lets leagues trade three drafts ahead and FantasyCalc prices
# all three classes (verified: 2027-2029 valued in August 2026) - at 2 this undercounted
# every roster's capital and hid a whole year of firsts from the league table. Classes
# FantasyCalc doesn't price are skipped by the flat-value guard, so a shorter market
# degrades gracefully. (Caveat for the in-season track: `owned_picks` starts at
# season+1, which is right once the current class has drafted and wrong in the spring
# before it has - the current class is the one with exact slot prices.)
FUTURE_DRAFT_YEARS = 3


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
    """How long until this player reaches his curve's decline cutoff; negative once past it.
    The distance `age_bucket` throws away, and the number a seller wants: Goff at 31.8 has
    6.2 years (pocket passer) while Hurts at 28.0 has 4.0, so age ordering inverts runway
    ordering. Reported rather than folded into any sort - two orderings inside one list is a
    documented failure mode here."""
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


def ppg(info: dict) -> float:
    """What a player PRODUCES per game this season (league-scored Sleeper projection,
    attached in `league.context`) - the unit for every sum or share of a lineup's
    production. What he is PRICED at stays `redraft_value`."""
    return info.get("projected_ppg") or 0


# Two values inside this band are the same value, on either axis and in either direction -
# a routine FantasyCalc refresh moves prices about this much, so any finding that needs a
# finer difference flickers between runs. Born in trade matching (one-sided use called a
# +0.12% production gain "strictly the better holding"); also the bar for whether a
# tertile boundary is real (`team_state`'s window_edge). Calibration: LOGIC.md,
# "Boundary noise".
NOISE_RETAINED = 0.98
NOISE_BAND = 1 - NOISE_RETAINED


# How far apart a player's two positional ranks must sit, as a share of his position's pool,
# before the two markets are saying different things about him. The median gap is 0% at every
# position - which is the property the raw ratio never had - so zero is genuinely neutral here
# and no per-position bar is needed. Known limit: a one-rank move is 3.4% of a 29-man TE pool
# against 1.3% of a 75-man WR pool, so this labels a larger share of tight ends than
# quarterbacks. Smaller than the ratio's distortion, not zero.
PRICED_FOR_GAP = 0.10


def priced_for(players: dict[str, dict]) -> dict[str, dict]:
    """Per player, whether the market prices him for LATER, NOW, or the same on both - from
    his rank within his position on each scale, because the raw redraft/dynasty ratio decays
    toward zero down the board and 1.0 is nowhere near neutral (LOGIC.md, "the
    dynasty/redraft measure"). Players missing either price get no verdict, which is
    correct: "priced for now or later" is meaningless without a now price."""
    out = {}
    for pos in {p["position"] for p in players.values()}:
        pool = {pid: p for pid, p in players.items()
                if p["position"] == pos and p.get("value") and p.get("redraft_value")}
        if not pool:
            continue
        dyn = rank_map({pid: p["value"] for pid, p in pool.items()})
        red = rank_map({pid: p["redraft_value"] for pid, p in pool.items()})
        for pid in pool:
            gap = (red[pid] - dyn[pid]) / len(pool)
            out[pid] = {
                "dynasty_rank": dyn[pid], "redraft_rank": red[pid], "of": len(pool),
                "gap": round(gap, 3),
                "priced_for": ("later" if gap > PRICED_FOR_GAP else
                               "now" if gap < -PRICED_FOR_GAP else "aligned"),
            }
    return out


def now_premium_bar(players: dict[str, dict], percentile: float = 0.9) -> dict[str, float]:
    """Per position, the `redraft_value / value` cutoff at `percentile` of that position's
    pool - how now-weighted a price has to be to be extreme FOR HIS POSITION. Per-position
    because the two scales' relationship differs sharply by position (an absolute bar once
    sat above the entire TE pool - LOGIC.md, "The dynasty/redraft measure"). Measures
    shape only; whether a player is worth having is `clears_relevance_floor`'s question."""
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
    # A usage role is an override on the age curve and absent means "no adjustment", so an
    # nflverse outage degrades rather than crashes - but never QUIETLY, because the role
    # curves have reversed a real recommendation. stderr reaches the author; `degraded`
    # reaches the answer.
    try:
        roles = player_roles.get_roles()
    except Exception as e:
        print(f"WARNING: usage roles unavailable ({type(e).__name__}) - age curves fall back to "
              f"position defaults this run, so runway is less accurate for quarterbacks and "
              f"pass-catching backs. Advice that turns on WHO to sell may differ.", file=sys.stderr)
        degraded.record("usage roles", "every age curve fell back to its position default, so "
                                      "runway is less precise for quarterbacks and pass-catching "
                                      "backs")
        roles = {}
    for player_id, role in roles.items():
        if player_id in players:
            players[player_id]["usage_role"] = role

    # Redraft values: same API, one flipped parameter. Dynasty prices production plus
    # future years; redraft prices production alone - the two currencies everything
    # downstream reasons with. Raw values only, never a ratio (unnormalized scales -
    # LOGIC.md). Coverage is ~200 redraft against ~400 dynasty, so missing entries get
    # None, which callers must treat as unknown rather than zero.
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
# The slot expectation stated as the contention fact behind it, not the internal label.
PICK_TIER_REASON = {"Early": "bottom third", "Mid": "middle third", "Late": "top third"}


def owned_picks(league_id: str, season: int, draft_rounds: int, roster_ids: list[int],
                pick_values: dict[str, int],
                strategy_by_roster: dict[int, str] | None = None) -> dict[int, list[dict]]:
    """The individual future picks each roster currently owns, priced by the ORIGINAL
    owner's window where the market publishes tiers (a rebuilder's 1st is an early pick
    whoever holds it, and Early/Late differ by nearly 2x). Only the next class has tiered
    prices - a window predicts next season's finish, not the one after - so later picks
    keep the flat round value and every pick records `slot_basis` saying which it got."""
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
                    "slot_basis": (f"expected {tier.lower()} - the originating team ranks in "
                                   f"the {PICK_TIER_REASON[tier]} of current production")
                                  if tiered_value else "flat round average (slot unknowable this far out)",
                })
    for picks in owned.values():
        picks.sort(key=lambda p: -p["value"])
    return owned


def pick_equivalent(value: float, pick_values: dict[str, int]) -> str | None:
    """The draft pick a player's value is closest to - "about a 2027 3rd" is legible where
    "worth 947" is not. A lookup on FantasyCalc's own pick prices, not a model, and
    approximate by nature since future classes are flat round averages."""
    if not pick_values or value <= 0:
        return None
    name, _ = min(pick_values.items(), key=lambda kv: abs(kv[1] - value))
    return name


def split_starters_bench(roster: dict, players: dict[str, dict],
                         starter_ids: set[str]) -> tuple[int, int]:
    """Dynasty value of the lineup vs the bench. `starter_ids` is the value-derived lineup
    (`LeagueContext.starters`), never Sleeper's current-week snapshot - see
    roster_needs.projected_starters."""
    all_ids = roster["players"] or []
    starter_value = sum(players[pid]["value"] for pid in starter_ids if pid in players)
    bench_value = sum(players[pid]["value"] for pid in all_ids if pid in players and pid not in starter_ids)
    return starter_value, bench_value


def pick_capital(owned: dict[int, list[dict]]) -> dict[int, int]:
    """Total future pick value per roster_id - a sum over `owned_picks`, deliberately not
    a second implementation of pick ownership, so the two numbers can never disagree."""
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
