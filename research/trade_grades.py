"""Grade every crawled two-team trade in its real currency: the change in expected titles
for each side, using only what was known when it was made (owner: a trade for Achane is
not a bad trade because he tore his ACL; trading for Goff and Kittle after Hurts went down
is a good one even if it loses value on paper).

- this season (in-season trades): the season simulator run before and after, each team's
  strength its optimal lineup at points-per-game-to-date; a player rostered but scoreless
  the last two weeks is out (the injury the trade may be answering)
- next season (every trade): the roster-strength curve (research/roster_strength.py) on
  the change in league-relative PLAYER value; picks (priced at their round's draft-day mean)
  are reported as `pick_margin` and kept out of titles - they never helped a next season
- both turned into titles: this season by the simulator, next season through a title curve
  fitted on the crawl's own brackets (where the non-linearity lives - a 1 seed wins far
  more than a 5 seed)

Writes data/research/trade_grades.jsonl (one row per trade side) for the feature analysis.
Run: python -m research.trade_grades
"""

import datetime
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np

from analysis.season_sim import simulate
from research.draft_skill import Values, is_superflex, sleeper_to_fp
from research.roster_strength import fit, season_rosters
from research.schedule_luck import luck_by_owner, season_luck
from sources.nflverse_ids import sleeper_names

CRAWL_DIR = Path("data/crawl")
OUT = Path("data/research/trade_grades.jsonl")
RUNS = 1000
PICK_AGE = 21.5           # a rookie pick, for value-weighted age
PRIOR_GAMES = 3          # how many games of 'typical starter' each player's average starts from
MIN_ROSTER_VALUE = 5000   # an average roster priced below this is a mapping gap, not a league
ELIGIBLE = {"QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
            "FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
            "REC_FLEX": {"WR", "TE"}, "WRRB_FLEX": {"WR", "RB"}}


def lineup_ppg(roster: set, ppg: dict, position: dict, slots: list) -> float:
    """Optimal lineup points per game: fill the narrowest slots first, best player each."""
    left = sorted(roster, key=lambda p: -ppg.get(p, 0))
    total = 0.0
    for slot in sorted((s for s in slots if s in ELIGIBLE), key=lambda s: len(ELIGIBLE[s])):
        pick = next((p for p in left if position.get(p) in ELIGIBLE[slot]), None)
        if pick is not None:
            left.remove(pick)
            total += ppg.get(pick, 0)
    return total


def title_curve(seasons: list) -> tuple:
    """Logistic P(title | regular-season all-play win%), fitted on completed brackets."""
    xs, ys = [], []
    for d in seasons:
        luck = season_luck(d)
        champ = next((m["w"] for m in d.get("winners_bracket") or [] if m.get("p") == 1), None)
        owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
        if champ is None:
            continue
        for rid, o in owner.items():
            if o in luck:
                xs.append(luck[o]["allplay"])
                ys.append(1.0 if rid == champ else 0.0)
    x, y, w = np.array(xs), np.array(ys), np.zeros(2)
    for _ in range(50):   # Newton's method
        p = 1 / (1 + np.exp(-(w[0] + w[1] * x)))
        g = np.array([np.sum(y - p), np.sum((y - p) * x)])
        h = np.array([[np.sum(p * (1 - p)), np.sum(p * (1 - p) * x)],
                      [np.sum(p * (1 - p) * x), np.sum(p * (1 - p) * x * x)]])
        w = w + np.linalg.solve(h, g)
    return tuple(w)


def pick_prices(seasons: list, values: Values, xwalk: dict) -> dict:
    """(draft season, superflex, round) -> mean draft-day value of that round's picks."""
    sums = defaultdict(list)
    for d in seasons:
        sf = is_superflex(d["league"])
        for dr in d.get("drafts") or []:
            if (dr.get("status") != "complete" or not dr.get("picks") or not dr.get("start_time")
                    or ((dr.get("settings") or {}).get("rounds") or 99) > 6):
                continue
            day = datetime.date.fromtimestamp(dr["start_time"] / 1000).isoformat()
            for p in dr["picks"]:
                v = values.at(xwalk.get(str(p.get("player_id"))), day, sf)
                if v is not None:
                    sums[(int(dr["season"]), sf, p["round"])].append(v)
    return {k: statistics.mean(v) for k, v in sums.items()}


def pick_price(prices: dict, season: int, sf: bool, rnd: int) -> float:
    """A future pick's price: its own class if drafted, else the latest class we have."""
    known = sorted(s for s, f, r in prices if f == sf and r == rnd)
    if not known:
        return 0.0
    return prices[(season if season in known else known[-1], sf, rnd)]


def grade_season(d: dict, ctx: dict) -> list[dict]:
    lg = d["league"]
    st = lg["settings"]
    weeks = (st.get("playoff_week_start") or 15) - 1
    sf = is_superflex(lg)
    slots = lg.get("roster_positions") or []
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    division = {r["roster_id"]: (r.get("settings") or {}).get("division") for r in d["rosters"]}
    mu = {w: d["matchups"].get(str(w)) or [] for w in range(1, weeks + 1)}
    out = []
    for txs in (d.get("transactions") or {}).values():
        for t in txs or []:
            if t.get("type") != "trade" or t.get("status") != "complete" or len(t.get("roster_ids") or []) != 2:
                continue
            leg = t.get("leg") or 1   # Sleeper files every offseason trade as week 1
            day = datetime.date.fromtimestamp(t["created"] / 1000).isoformat()
            out += grade_trade(t, d, ctx, leg, day, 2 <= leg <= weeks - 1, weeks,
                               sf, slots, owner, division, mu)
    return out


def grade_trade(t, d, ctx, leg, day, in_season, weeks, sf, slots, owner, division, mu) -> list:
    a, b = t["roster_ids"]
    gets = {r: {p for p, to in (t.get("adds") or {}).items() if to == r} for r in (a, b)}
    gives = {r: {p for p, frm in (t.get("drops") or {}).items() if frm == r} for r in (a, b)}
    picks_in = {r: [p for p in t.get("draft_picks") or [] if p.get("owner_id") == r] for r in (a, b)}
    picks_out = {r: [p for p in t.get("draft_picks") or [] if p.get("previous_owner_id") == r] for r in (a, b)}
    week = min(max(leg, 1), weeks)
    rosters = {m["roster_id"]: set(m.get("players") or []) for m in mu[week]}
    if a not in rosters or b not in rosters:
        return []
    before = {r: (rosters[r] - gets[r]) | gives[r] for r in (a, b)}
    after = {r: (rosters[r] - gives[r]) | gets[r] for r in (a, b)}
    val = lambda p: ctx["values"].at(ctx["xwalk"].get(str(p)), day, sf) or 0.0

    def age_of(players, picks):
        """Value-weighted age of a side's haul; a pick is a PICK_AGE asset."""
        weighted = [(val(p), (ctx["values"].rows_at(ctx["xwalk"].get(str(p)), day) or {}).get("age"))
                    for p in players] + [(price(pk), PICK_AGE) for pk in picks]
        known = [(v, float(a)) for v, a in weighted if v > 0 and a not in (None, "", "NA")]
        total = sum(v for v, _ in known)
        return sum(v * a for v, a in known) / total if total else None
    price = lambda pk: pick_price(ctx["prices"], int(pk["season"]), sf, pk["round"])
    league_mean = statistics.mean(sum(val(p) for p in r) for r in rosters.values())
    if league_mean < MIN_ROSTER_VALUE:
        return []   # rosters this far back barely map to DP ids - no honest price

    sim = None
    if in_season:
        sim = this_season(d, leg, weeks, slots, owner, division, mu, rosters, before, after, ctx)
    rows = []
    for me, them in ((a, b), (b, a)):
        got = sum(val(p) for p in gets[me]) + sum(price(p) for p in picks_in[me])
        gave = sum(val(p) for p in gives[me]) + sum(price(p) for p in picks_out[me])
        total_before = sum(val(p) for p in before[me])
        ap_now = ctx["allplay_now"](d, owner[me], leg)
        base = ctx["curve"][0] + ctx["curve"][1] * ap_now + ctx["curve"][2] * total_before / league_mean
        picks = (sum(price(p) for p in picks_in[me]) - sum(price(p) for p in picks_out[me])) / league_mean
        # Next season moves on PLAYER value only: pick value won never helped the next season
        # (offseason weight -0.40, in-season ~0 - trade_analysis), so picks are reported in
        # value, not priced into titles until a 2+ season horizon can be validated.
        moved = min(max(base + ctx["curve"][2] * ((got - gave) / league_mean - picks), 0.0), 1.0)
        base = min(max(base, 0.0), 1.0)
        next_titles = ctx["p_title"](moved) - ctx["p_title"](base)
        rows.append({
            "league": d["league"]["league_id"], "season": int(d["league"]["season"]),
            "prev_league": d["league"].get("previous_league_id"), "trade": t["transaction_id"],
            "week": leg, "in_season": bool(in_season), "manager": owner[me], "partner": owner[them],
            "superflex": sf, "got_value": got, "gave_value": gave,
            "value_margin": (got - gave) / league_mean,
            "pick_margin": picks,
            "got_age": age_of(gets[me], picks_in[me]), "gave_age": age_of(gives[me], picks_out[me]),
            "n_got": len(gets[me]) + len(picks_in[me]),
            "n_gave": len(gives[me]) + len(picks_out[me]),
            "picks_got": [(int(p["season"]) - int(d["league"]["season"]), p["round"]) for p in picks_in[me]],
            "picks_gave": [(int(p["season"]) - int(d["league"]["season"]), p["round"]) for p in picks_out[me]],
            "allplay_now": ap_now, "value_rank_before": total_before / league_mean,
            "now_titles": sim[me]["title"] if sim else 0.0,
            "now_playoffs": sim[me]["playoffs"] if sim else 0.0,
            "now_lineup": sim[me]["lineup"] if sim else 0.0,
            "title_before": sim[me]["title_before"] if sim else None,
            "next_titles": next_titles,
            "titles": (sim[me]["title"] if sim else 0.0) + next_titles,
        })
    return rows


def this_season(d, leg, weeks, slots, owner, division, mu, rosters, before, after, ctx) -> dict:
    """Change in this season's title and playoff odds (and lineup PPG) for both sides."""
    played = range(1, leg)
    scored = defaultdict(list)
    for w in played:
        for m in mu[w]:
            for p, pts in (m.get("players_points") or {}).items():
                scored[p].append(pts or 0)
    position = {p: v["position"] for p, v in ctx["names"].items()}
    for m in mu[1]:
        for p in m.get("players") or []:
            position.setdefault(p, "DEF" if not p.isdigit() else None)
    # One week of points is a coin flip: each player's games are shrunk toward the typical
    # starter at his position, PRIOR_GAMES games' worth, so a week-2 trade isn't priced on
    # one big Sunday. Two straight scoreless rostered weeks = out (the injury a trade may answer).
    games = {p: [x for x in s if x > 0] for p, s in scored.items()}
    by_pos = defaultdict(list)
    for p, g in games.items():
        if g:
            by_pos[position.get(p)].append(statistics.mean(g))
    prior = {pos: statistics.median(v) for pos, v in by_pos.items()}
    ppg = {p: (sum(g) + PRIOR_GAMES * prior.get(position.get(p), 0)) / (len(g) + PRIOR_GAMES)
           if g else 0.0 for p, g in games.items()}
    for p, s in scored.items():
        if len(s) >= 3 and s[-1] == 0 and s[-2] == 0 and any(s):
            ppg[p] = 0.0

    wins, pf, sched = defaultdict(float), defaultdict(float), defaultdict(dict)
    for w in range(1, weeks + 1):
        games = defaultdict(list)
        for m in mu[w]:
            games[m.get("matchup_id")].append(m)
        for g in games.values():
            if len(g) != 2:
                continue
            x, y = g
            if w < leg:
                wins[x["roster_id"]] += (x["points"] or 0) > (y["points"] or 0)
                wins[y["roster_id"]] += (y["points"] or 0) > (x["points"] or 0)
                pf[x["roster_id"]] += x["points"] or 0
                pf[y["roster_id"]] += y["points"] or 0
            else:
                sched[x["roster_id"]][w], sched[y["roster_id"]][w] = y["roster_id"], x["roster_id"]
    settings = {**d["league"]["settings"], "playoff_week_start": weeks + 1}

    def odds(lineups):
        teams = [{"owner_id": r, "wins": wins[r], "losses": 0, "ties": 0, "points_for": pf[r],
                  "eppg": lineups[r], "division": division.get(r)} for r in rosters]
        return simulate(teams, settings, leg - 1, dict(sched), runs=RUNS)

    base_lineups = {r: lineup_ppg(rosters[r] if r not in before else before[r], ppg, position, slots)
                    for r in rosters}
    new_lineups = {**base_lineups, **{r: lineup_ppg(after[r], ppg, position, slots) for r in after}}
    o0, o1 = odds(base_lineups), odds(new_lineups)
    if not o0:
        return {r: {"title": 0.0, "playoffs": 0.0, "lineup": 0.0, "title_before": 0.0} for r in before}
    return {r: {"title": o1[r]["title"] - o0[r]["title"], "playoffs": o1[r]["playoffs"] - o0[r]["playoffs"],
                "lineup": new_lineups[r] - base_lineups[r], "title_before": o0[r]["title"]}
            for r in before}


def build_context() -> tuple[dict, list]:
    """Everything grading needs that is learned from the crawl: the future curve, the title
    curve, pick prices, values. Returns (ctx, completed seasons)."""
    values, xwalk = Values(), sleeper_to_fp()
    seasons = []
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if season_luck(d):
            seasons.append(d)
    # The future curve: all-play + league-relative total value -> next season's all-play.
    by_id = {d["league"]["league_id"]: (d["league"].get("previous_league_id"),
                                       season_rosters(d, values, xwalk)) for d in seasons}
    pairs = [(by_id[prev][1][o], now[o]["allplay"]) for prev, now in by_id.values()
             if prev in by_id for o in now if o in by_id[prev][1]]
    curve = fit(pairs, ["allplay", "total"])
    tc = title_curve(seasons)
    luck_cache = {}

    def allplay_now(d, o, leg):
        """All-play to date for an in-season trade, else last season's (0.5 if unknown)."""
        key = (d["league"]["league_id"], leg)
        if key not in luck_cache:
            if leg >= 3:
                mu = {w: d["matchups"][str(w)] for w in range(1, leg)}
                luck_cache[key] = luck_by_owner(mu, d["rosters"], leg - 1)
            else:
                prev = by_id.get(d["league"].get("previous_league_id"))
                luck_cache[key] = {k: {"allplay": v["allplay"]} for k, v in (prev[1] if prev else {}).items()}
        return luck_cache[key].get(o, {}).get("allplay", 0.5)

    ctx = {"values": values, "xwalk": xwalk, "names": sleeper_names(), "curve": curve,
           "prices": pick_prices(seasons, values, xwalk), "allplay_now": allplay_now,
           "p_title": lambda ap: 1 / (1 + math.exp(-(tc[0] + tc[1] * ap)))}
    print(f"future curve: next all-play = {curve[0]:+.3f} + {curve[1]:.3f} x all-play + "
          f"{curve[2]:.3f} x relative value; title curve: P = logistic({tc[0]:.2f} + {tc[1]:.2f} x all-play) "
          f"-> {ctx['p_title'](0.5):.1%} at .500, {ctx['p_title'](0.7):.1%} at .700")
    return ctx, seasons


def live_season(league_id: str) -> dict:
    """A live league in the crawl's shape, through the current NFL week."""
    from sources import sleeper
    league = sleeper.get_league(league_id)
    weeks = (league["settings"].get("playoff_week_start") or 15) - 1
    return {"league": league, "rosters": sleeper.get_rosters(league_id),
            "matchups": {str(w): sleeper.get_matchups(league_id, w) or [] for w in range(1, weeks + 1)},
            "transactions": {str(w): sleeper.get_transactions(league_id, w) or []
                             for w in range(1, int(sleeper.get_nfl_state()["week"]) + 1)}}


def main(argv: list) -> None:
    ctx, seasons = build_context()
    if argv:   # grade a live league's trades: python -m research.trade_grades <league_id>
        for r in grade_season(live_season(argv[0]), ctx):
            print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}))
        return
    rows = []
    for d in seasons:
        rows += grade_season(d, ctx)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"{len(rows)} trade sides graded ({sum(r['in_season'] for r in rows)} in-season) -> {OUT}")


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
