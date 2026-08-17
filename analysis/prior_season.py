"""How last season actually went, and whether it still describes this team.

The current season's record is useless in the preseason - every team is 0-0 - which is why
this project wrote win/loss record off entirely. But the *previous* season is finished and
sitting there: `sleeper.get_season_chain` already walks `previous_league_id`, the prior
league carries final `wins`/`losses`/`fpts` on every roster, and `winners_bracket` names
the actual champion.

It exists for one job: judging whether a team that isn't currently a seller could be
*talked into* becoming one (`trade_targets` persuasion targets). Two contenders in a real
league each hold an elite aging RB, and nothing in the current-season data separates them
convincingly - trajectory splits them only -3 to -11. Last season splits them decisively:

    rjl22         won the title, 10-4, most points in the league  -> not selling
    kierankieran  9th, 5-9, with an aging core that didn't win    -> real reason to listen

**Deliberately kept out of the window classification.** "This manager just won and will
run it back" is a behavioural inference about a person, not a fact about a roster. It
belongs in how a trade suggestion is framed and ranked, not in whether a team is a
contender - which is measured, and should stay that way.

Smoke test: python -m analysis.prior_season <league_id>
"""

import sys

from sources import sleeper

# Below this share of current starting production carried over from last season's roster,
# last season describes a different team and its results say nothing about this one.
#
# A judgment call, and flagged as one: the leagues on hand all sit at 83-100%, so there is
# no observed case near the boundary to calibrate against. That number is itself an
# artifact of *when* it was measured - a dynasty preseason before the rookie draft, which
# is peak continuity. Mid-season, post-draft, or in a league with heavy trade volume it
# would be materially lower, and in a redraft league it is zero by construction. So the
# gate matters even though it currently never fires.
MIN_CONTINUITY = 0.6


def _placements(bracket: list[dict]) -> tuple[int | None, set[int]]:
    """(champion roster_id, roster_ids that appeared in the playoffs) from a bracket."""
    champion = next((m["w"] for m in bracket if m.get("p") == 1 and m.get("w")), None)
    played = {rid for m in bracket for rid in (m.get("t1"), m.get("t2"), m.get("w"), m.get("l"))
              if isinstance(rid, int)}
    return champion, played


def results(league_id: str) -> dict[str, dict]:
    """Last season per owner_id, or `{}` if there is no prior season to read.

    Keyed by owner_id rather than roster_id because roster_ids are per-league and this
    crosses a league boundary - the same manager can hold a different roster_id in the
    prior season's league.
    """
    from .league import context
    from .team_values import ppg

    chain = sleeper.get_season_chain(league_id)
    if len(chain) < 2:
        return {}
    prev_id = chain[1]
    prev_league = sleeper.get_league(prev_id)
    if prev_league.get("status") != "complete":
        # An abandoned or in-progress prior season isn't a result to reason from.
        return {}

    ctx = context(league_id)
    prev_rosters = sleeper.get_rosters(prev_id)
    champion_rid, played = _placements(sleeper.get_winners_bracket(prev_id))
    playoff_teams = prev_league["settings"].get("playoff_teams") or 0

    standings = sorted(
        prev_rosters,
        key=lambda r: (-(r["settings"].get("wins") or 0),
                       -(r["settings"].get("fpts") or 0)),
    )
    finish = {r["roster_id"]: i for i, r in enumerate(standings, start=1)}
    prev_players = {r["owner_id"]: set(r["players"] or []) for r in prev_rosters}

    out = {}
    for roster in ctx.rosters:
        owner_id = roster["owner_id"]
        prev_roster = next((r for r in prev_rosters if r["owner_id"] == owner_id), None)
        if prev_roster is None:
            continue  # a manager who wasn't in the league last season
        settings = prev_roster["settings"]
        rid = prev_roster["roster_id"]

        # Continuity is measured on *current starting production*, not raw player counts:
        # the question is whether the players who make this team what it is today were
        # here for last season's result, and a swapped-out deep bench doesn't change that.
        starters = ctx.starters_for(roster)
        kept = starters & prev_players.get(owner_id, set())
        produced = lambda ids: sum(ppg(ctx.players[p]) for p in ids if p in ctx.players)
        total = produced(starters)
        continuity = produced(kept) / total if total else 0.0

        out[owner_id] = {
            "season": prev_league["season"],
            "finish": finish.get(rid),
            "wins": settings.get("wins") or 0,
            "losses": settings.get("losses") or 0,
            "points_for": round((settings.get("fpts") or 0) + (settings.get("fpts_decimal") or 0) / 100, 1),
            "champion": rid == champion_rid,
            "made_playoffs": rid in played or (finish.get(rid) or 99) <= playoff_teams,
            "continuity": round(continuity, 2),
            "describes_this_team": continuity >= MIN_CONTINUITY,
        }
    for entry in out.values():
        entry["note"] = _note(entry, len(prev_rosters))
    return out


def _note(entry: dict, num_teams: int) -> str:
    outcome = ("won the title" if entry["champion"]
               else "made the playoffs" if entry["made_playoffs"]
               else "missed the playoffs")
    base = (f"{entry['season']}: {outcome}, finished {entry['finish']} of {num_teams} at "
            f"{entry['wins']}-{entry['losses']} with {entry['points_for']:,.1f} points for.")
    if entry["describes_this_team"]:
        return (f"{base} {round(entry['continuity'] * 100)}% of this team's current starting "
                f"production was on that roster, so the result still describes it.")
    return (f"{base} Only {round(entry['continuity'] * 100)}% of this team's current starting "
            f"production was on that roster, so last season says little about it now.")


def main(league_id: str) -> None:
    rows = results(league_id)
    if not rows:
        print("no completed prior season for this league")
        return
    from .league import context
    names = context(league_id).owner_names
    for owner_id, e in sorted(rows.items(), key=lambda kv: kv[1]["finish"] or 99):
        flag = " [CHAMPION]" if e["champion"] else ""
        print(f"  {e['finish']:2}. {names.get(owner_id, '?')[:18]:18} "
              f"{e['wins']:2}-{e['losses']:<2} {e['points_for']:8,.1f} PF  "
              f"continuity {e['continuity']:.0%}{flag}")


if __name__ == "__main__":
    main(sys.argv[1])
