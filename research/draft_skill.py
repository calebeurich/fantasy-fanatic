"""Rookie drafting: does a manager pick better than the slot he picks from? Track 3's
second channel. Every rookie pick in the crawl is scored against its slot, inside the same
rookie class and QB format, twice:

- outcome: DynastyProcess value 12 months after the draft, minus the slot's expected value
- draft-day edge: the player's value on draft day, minus the slot's draft-day expectation

Tests the hypotheses pre-registered in research/README.md "Rookie drafting".

Run: python -m research.draft_skill
"""

import bisect
import datetime
import json
import statistics
from collections import defaultdict
from pathlib import Path

import nflreadpy as nfl

from sources import dp_values

CRAWL_DIR = Path("data/crawl")
SEASONS = range(2021, 2026)   # a full 12 months must have passed since the draft
MAX_ROOKIE_ROUNDS = 6         # deeper drafts are startups - a different channel
SLOT_WINDOW = 2               # a slot's expectation pools picks within +/-2 of it
MIN_DRAFTS = 3                # a class/format with fewer drafts has no honest baseline


class Values:
    """Point-in-time DP values: the first snapshot on or after a date."""

    def __init__(self):
        snaps = sorted(dp_values.load(), key=lambda s: s["scrape_date"])
        self.dates = [s["scrape_date"] for s in snaps]
        self.rows = [{r["fp_id"]: r for r in s["rows"]} for s in snaps]

    def rows_at(self, fp_id, date: str):
        """The player's DP row in that snapshot, None if he isn't in it."""
        i = bisect.bisect_left(self.dates, date)
        return self.rows[i].get(fp_id) if fp_id and i < len(self.dates) else None

    def at(self, fp_id, date: str, superflex: bool):
        if bisect.bisect_left(self.dates, date) == len(self.dates):
            return None                       # past the end of the archive
        row = self.rows_at(fp_id, date)
        return float(row["value_2qb" if superflex else "value_1qb"] or 0) if row else 0.0


def is_superflex(league: dict) -> bool:
    positions = league.get("roster_positions") or []
    return "SUPER_FLEX" in positions or positions.count("QB") >= 2


def sleeper_to_fp() -> dict:
    ids = nfl.load_ff_playerids().select(["sleeper_id", "fantasypros_id"]).drop_nulls()
    return {str(s): str(int(float(f))) for s, f in ids.iter_rows()}


def rookie_picks(values: Values, xwalk: dict) -> list[dict]:
    picks = []
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        lg = d["league"]
        superflex = is_superflex(lg)
        for dr in d.get("drafts") or []:
            rounds = (dr.get("settings") or {}).get("rounds") or 99
            if (dr.get("status") != "complete" or not dr.get("picks") or rounds > MAX_ROOKIE_ROUNDS
                    or int(dr["season"]) not in SEASONS or not dr.get("start_time")):
                continue
            day0 = datetime.date.fromtimestamp(dr["start_time"] / 1000)
            d0, d12 = day0.isoformat(), (day0 + datetime.timedelta(days=365)).isoformat()
            for p in dr["picks"]:
                fp = xwalk.get(str(p.get("player_id")))
                v0, v12 = values.at(fp, d0, superflex), values.at(fp, d12, superflex)
                if v0 is None or v12 is None or not p.get("picked_by"):
                    continue
                picks.append({"season": int(dr["season"]), "superflex": superflex,
                              "draft": dr["draft_id"], "league": lg["league_id"],
                              "prev_league": lg.get("previous_league_id"),
                              "manager": p["picked_by"], "pick": p["pick_no"],
                              "mapped": fp is not None, "v0": v0, "v12": v12})
    return picks


def score(picks: list[dict]) -> list[dict]:
    """Attach `edge` and `outcome` (value over the slot's expectation) to every pick whose
    class/format has enough drafts to set the expectation."""
    groups = defaultdict(list)
    for p in picks:
        groups[(p["season"], p["superflex"])].append(p)
    scored = []
    for group in groups.values():
        if len({p["draft"] for p in group}) < MIN_DRAFTS:
            continue
        for p in group:
            near = [q for q in group if abs(q["pick"] - p["pick"]) <= SLOT_WINDOW]
            p["edge"] = p["v0"] - statistics.mean(q["v0"] for q in near)
            p["outcome"] = p["v12"] - statistics.mean(q["v12"] for q in near)
            scored.append(p)
    return scored


def main():
    picks = rookie_picks(Values(), sleeper_to_fp())
    scored = score(picks)
    print(f"{len({p['draft'] for p in picks})} rookie drafts, {len(picks)} picks "
          f"({100 * sum(p['mapped'] for p in picks) / len(picks):.0f}% mapped to a DP id); "
          f"{len(scored)} scored in classes with {MIN_DRAFTS}+ drafts")

    per = defaultdict(list)   # (league, manager) -> that draft's picks
    for p in scored:
        per[(p["league"], p["manager"])].append(p)
    mean = lambda xs, k: statistics.mean(x[k] for x in xs)
    pairs = [(per[(p_lg, m)], per[(lg, m)]) for (lg, m), ps in per.items()
             for p_lg in [ps[0]["prev_league"]] if (p_lg, m) in per]
    print(f"{len(pairs)} manager pairs (same manager, same league, back-to-back rookie drafts)")
    corr = statistics.correlation
    print(f"D1 outcome per pick, year-over-year r = "
          f"{corr([mean(a, 'outcome') for a, _ in pairs], [mean(b, 'outcome') for _, b in pairs]):+.3f}")
    print(f"D2 draft-day edge per pick, year-over-year r = "
          f"{corr([mean(a, 'edge') for a, _ in pairs], [mean(b, 'edge') for _, b in pairs]):+.3f}")
    print(f"D3 edge -> outcome, per pick: r = {corr([p['edge'] for p in scored], [p['outcome'] for p in scored]):+.3f}")


if __name__ == "__main__":
    main()
