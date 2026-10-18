"""Monte Carlo season: play the rest of the regular season and the bracket out RUNS times
under the league's own playoff rules, and count how often each team makes the field,
gets a bye, and wins it all. Fixed seed, so the same inputs always print the same odds.
The rules were read off 131 completed crawled brackets - LOGIC.md "Season simulator"."""

import random
import statistics

RUNS = 5000

# Both MEASURED (2026-10-18) on 131 completed crawled seasons, as shares of the league's
# average weekly score so they carry across scoring formats (SD tracks the mean at r=0.9).
WEEKLY_SD_SHARE = 0.18     # a team's week around its own mean: median 18%, 10-90th 15-22%
STRENGTH_SD_SHARE = 0.065  # how far a team's true level drifts over the rest of a season


def simulate(teams: list[dict], settings: dict, weeks_played: int, schedule: dict | None = None,
             strengths: dict | None = None, strength_sd: float | None = None,
             runs: int = RUNS, seed: int = 0) -> dict:
    """owner_id -> {"playoffs", "bye", "title"} as probabilities.

    `teams`: owner_id, wins (median games included), ties, points_for, eppg, division.
    `weeks_played` is passed, not read off the record: a median league plays two a week.
    `schedule`: owner_id -> {week: opponent}; `strengths`: owner_id -> {week: projected
    points} (regular season AND playoff weeks); any gap falls back to eppg.
    `strength_sd` overrides the drift - the backtest's input is noisier than ePPG."""
    slots = settings.get("playoff_teams") or 0
    if not slots or slots >= len(teams):
        return {}
    first_playoff = settings.get("playoff_week_start") or 15
    ids = [t["owner_id"] for t in teams]
    eppg = {t["owner_id"]: t["eppg"] for t in teams}
    level = statistics.mean(eppg.values())
    weekly_sd = WEEKLY_SD_SHARE * level
    drift_sd = STRENGTH_SD_SHARE * level if strength_sd is None else strength_sd
    expect = lambda me, w: (strengths or {}).get(me, {}).get(w, eppg[me])
    rng = random.Random(seed)
    counts = {me: {"playoffs": 0, "bye": 0, "title": 0} for me in ids}

    for _ in range(runs):
        drift = {me: rng.gauss(0, drift_sd) for me in ids}
        score = lambda me, w: expect(me, w) + drift[me] + rng.gauss(0, weekly_sd)
        wins = {t["owner_id"]: t["wins"] + 0.5 * t["ties"] for t in teams}
        pf = {t["owner_id"]: t["points_for"] for t in teams}
        for w in range(weeks_played + 1, first_playoff):
            week = {me: score(me, w) for me in ids}
            for me in ids:
                opp = (schedule or {}).get(me, {}).get(w)
                them = week[opp] if opp in week else score(rng.choice([o for o in ids if o != me]), w)
                wins[me] += week[me] > them
                pf[me] += week[me]
            if settings.get("league_average_match"):
                median = statistics.median(week.values())
                for me in ids:
                    wins[me] += week[me] > median
        seeds = _seed(teams, wins, pf, settings)[:slots]
        for me in seeds:
            counts[me]["playoffs"] += 1
        champ, byes = _bracket(seeds, settings.get("playoff_seed_type") == 1,
                               lambda me, rnd: score(me, first_playoff + rnd))
        for me in byes:
            counts[me]["bye"] += 1
        counts[champ]["title"] += 1
    return {me: {k: n / runs for k, n in c.items()} for me, c in counts.items()}


def _seed(teams: list[dict], wins: dict, pf: dict, settings: dict) -> list:
    """Every team in seed order: most wins, then points for. With divisions, the division
    leaders take the top seeds (17/17 crawled division brackets)."""
    order = sorted(wins, key=lambda me: (wins[me], pf[me]), reverse=True)
    if not settings.get("divisions"):
        return order
    division = {t["owner_id"]: t.get("division") for t in teams}
    leaders = [next(me for me in order if division[me] == d) for d in dict.fromkeys(
        division[me] for me in order)]
    return leaders + [me for me in order if me not in leaders]


def _bracket(seeds: list, reseed: bool, play) -> tuple:
    """(champion, the seeds on a round-1 bye). The field fills a power-of-two bracket in
    standard order (1v8, 4v5, 2v7, 3v6) and the top seeds take the missing slots as byes.
    Fixed brackets keep that order; re-seeding sends the best seed left against the worst
    (Sleeper playoff_seed_type 0 / 1, each confirmed on every crawled bracket)."""
    size = 1
    while size < len(seeds):
        size *= 2
    order = [1]
    while len(order) < size:
        order = [s for x in order for s in (x, 2 * len(order) + 1 - x)]
    alive = [seeds[s - 1] if s <= len(seeds) else None for s in order]
    byes = [a or b for a, b in zip(alive[::2], alive[1::2]) if a is None or b is None]
    rnd = 0
    while len(alive) > 1:
        alive = [a if b is None else b if a is None else
                 (a if play(a, rnd) > play(b, rnd) else b)
                 for a, b in zip(alive[::2], alive[1::2])]
        rnd += 1
        if reseed and len(alive) > 1:
            alive.sort(key=seeds.index)
            alive = [s for pair in zip(alive[:len(alive) // 2], alive[::-1]) for s in pair]
    return alive[0], byes
