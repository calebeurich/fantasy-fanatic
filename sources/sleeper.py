"""Minimal Sleeper API client. Smoke test: python -m sources.sleeper <username> [year] [league_id]"""

import sys
import requests

from .cache import ttl_cache, LIVE_TTL, LEAGUE_CONFIG_TTL, MARKET_TTL, REFERENCE_TTL

BASE = "https://api.sleeper.app/v1"

# Sleeper's own league type flag: 0 = redraft, 1 = keeper, 2 = dynasty
DYNASTY_TYPE = 2

# One connection pool for every call. `requests.get` opens a fresh TLS connection each
# time, and the handshake was half the cost of each of the ~40 small fetches a cold
# page load makes.
_http = requests.Session()


def _get(path: str):
    resp = _http.get(f"{BASE}/{path}")
    resp.raise_for_status()
    return resp.json()


@ttl_cache(LEAGUE_CONFIG_TTL)
def get_user_id(username: str) -> str:
    return _get(f"user/{username}")["user_id"]


@ttl_cache(LEAGUE_CONFIG_TTL)
def get_leagues(user_id: str, year: str) -> list[dict]:
    return _get(f"user/{user_id}/leagues/nfl/{year}")


@ttl_cache(LEAGUE_CONFIG_TTL)
def get_league(league_id: str) -> dict:
    return _get(f"league/{league_id}")


@ttl_cache(LIVE_TTL)
def get_rosters(league_id: str) -> list[dict]:
    return _get(f"league/{league_id}/rosters")


@ttl_cache(LEAGUE_CONFIG_TTL)
def get_users(league_id: str) -> list[dict]:
    return _get(f"league/{league_id}/users")


@ttl_cache(LEAGUE_CONFIG_TTL)
def get_matchups(league_id: str, week: int) -> list[dict]:
    """One week's head-to-head pairings: rosters sharing a `matchup_id` play each other.
    Sleeper posts the whole season's assignments up front, so future weeks answer "who
    do I still play" - `points` fills in as the week is scored."""
    return _get(f"league/{league_id}/matchups/{week}")


@ttl_cache(LIVE_TTL)
def get_traded_picks(league_id: str) -> list[dict]:
    """Future picks that have changed hands at least once. A pick not listed here is
    still owned by the roster whose original pick it is."""
    return _get(f"league/{league_id}/traded_picks")


def get_transactions(league_id: str, week: int) -> list[dict]:
    """A completed season's transactions never change again, so they are held for the
    long TTL; only the live season is refreshed on the short one. Before this split the
    36-fetch trade-history walk expired every 60s and was the single biggest cost of a
    page load."""
    if get_league(league_id).get("status") == "complete":
        return _past_transactions(league_id, week)
    return _live_transactions(league_id, week)


@ttl_cache(REFERENCE_TTL)
def _past_transactions(league_id: str, week: int) -> list[dict]:
    return _get(f"league/{league_id}/transactions/{week}")


@ttl_cache(LIVE_TTL)
def _live_transactions(league_id: str, week: int) -> list[dict]:
    return _get(f"league/{league_id}/transactions/{week}")


@ttl_cache(LEAGUE_CONFIG_TTL)
def get_winners_bracket(league_id: str) -> list[dict]:
    """The playoff bracket. Each match carries `w`/`l` (winning/losing roster_id) and, for
    placement games, `p` - the place being played for. `p == 1` is the championship, so
    its `w` is the champion. Only meaningful for a completed season."""
    return _get(f"league/{league_id}/winners_bracket")


PROJECTION_POSITIONS = ("QB", "RB", "WR", "TE")


@ttl_cache(MARKET_TTL)
@ttl_cache(MARKET_TTL)
def get_projections(season: str, week: int | None = None) -> dict[str, dict]:
    """Sleeper's projections (undocumented, but the same numbers the app shows):
    player_id -> raw projected stat line (pass_yd, rec, rush_td, ...). Season totals by
    default; a week gives that week's line. Raw stats rather than Sleeper's pts_ppr so
    each league's own scoring_settings can price them - see `score`."""
    path = f"projections/nfl/{season}" + (f"/{week}" if week else "")
    query = "season_type=regular&" + "&".join(f"position[]={p}" for p in PROJECTION_POSITIONS)
    resp = _http.get(f"https://api.sleeper.app/{path}?{query}")
    resp.raise_for_status()
    return {r["player_id"]: r["stats"] for r in resp.json() if r.get("stats")}


def score(stats: dict, scoring_settings: dict) -> float:
    """Fantasy points for a stat line under a league's scoring - the same dot product
    Sleeper uses (verified: reproduces its pts_ppr exactly for a PPR league)."""
    return sum(v * scoring_settings[k] for k, v in stats.items() if k in scoring_settings)


def get_season_chain(league_id: str) -> list[str]:
    """This league's own league_id plus every prior season's, oldest dynasty history
    included, most recent first."""
    chain = []
    # Chains end as null OR the string "0", league by league - both mean "no prior
    # season", and chasing "0" is a 404 (found crawling leagues beyond our own).
    while league_id and league_id != "0":
        chain.append(league_id)
        league_id = get_league(league_id).get("previous_league_id")
    return chain


def describe_format(league: dict) -> dict:
    """Pull out the format details that drive dynasty value lookups."""
    positions = league["roster_positions"]
    scoring = league["scoring_settings"]
    return {
        "is_dynasty": league["settings"]["type"] == DYNASTY_TYPE,
        "num_teams": league["settings"]["num_teams"],
        "is_superflex": "SUPER_FLEX" in positions,
        "num_qbs": starting_qbs(positions),
        "ppr": scoring.get("rec", 0),
        "tep_tier": tep_tier(scoring.get("bonus_rec_te", 0), positions.count("TE")),
    }


# FantasyCalc's own three bands, taken verbatim from the labels on their TEP control:
#   Off    - "No/minimal TEP (<=0.25)"
#   TEP+   - "+0.5 to 1.0 TEP"
#   TEP++  - "Start 2 TE or >1.0 TEP"
TEP_MINIMAL = 0.25
TEP_HEAVY = 1.0


def tep_tier(bonus_rec_te: float, te_slots: int) -> str:
    """Which of FantasyCalc's three TE-premium bands this league falls in.

    Replaces `is_te_premium`, a bare boolean that was computed and - as LOGIC.md recorded
    - consumed by nothing, because at the time there was believed to be no way to apply
    it. There is (see `fantasycalc.TEP_MULTIPLIER`), and it matters here: both real
    leagues in this project score `bonus_rec_te = 0.5`, which is TEP+, so every TE value
    was ~15% low and every TE comparison inherited that."""
    if bonus_rec_te > TEP_HEAVY or te_slots >= 2:
        return "teppp"
    if bonus_rec_te > TEP_MINIMAL:
        return "tep"
    return "none"


def starting_qbs(roster_positions: list[str]) -> int:
    """How many QBs a team in this league actually starts - FantasyCalc's `numQbs`.

    **FantasyCalc publishes exactly two QB markets**, verified against the live API:
    `numQbs=1`, and `numQbs>=2` which returns identical data for 2, 3, and 0. There is no
    separate superflex market. So this is a binary decision, and getting it wrong is the
    single largest mispricing available in this project - the entire QB market moves by a
    flat **1.88x** between the two (Josh Allen 5,502 -> 10,361; the multiplier is the same
    for every QB), while RBs shift 0.92x and WRs 1.01x as the pool renormalizes.

    Superflex is *technically* one QB slot plus a flex that accepts one. It prices like a
    2QB league because starting two QBs is generally the best projection when you can, so
    the second QB is near-mandatory in practice without being mandatory in the rules -
    which is exactly why FantasyCalc serves both from one market. Mapping superflex to 1
    because it has one dedicated QB slot would be the worst available error here.

    The old form was `2 if is_superflex else 1`, keyed on a flag that answers "can a
    non-QB fill the second slot" - a different question from "how many QBs start", and
    only the second prices the market. It got superflex right by luck and a true 2QB
    league (two literal QB slots, no SUPER_FLEX) wrong. That format is close to extinct in
    practice, so this is correctness rather than an impactful bug.

    Clamped to 2 to say out loud that everything at or above 2 is one market;
    `roster_needs.dedicated_slots` is deliberately unclamped, since it counts real roster
    slots where a third QB genuinely is required.

    *Not modelled*: superflex's optionality. A real superflex manager can start a RB in
    the flex, so elite QBs are marginally less mandatory there than in true 2QB. The
    market doesn't price that distinction and neither can we."""
    return min(2, roster_positions.count("QB") + roster_positions.count("SUPER_FLEX"))


if __name__ == "__main__":
    username = sys.argv[1]
    year = sys.argv[2] if len(sys.argv) > 2 else "2026"

    user_id = get_user_id(username)
    leagues = get_leagues(user_id, year)

    print(f"user_id: {user_id}")
    print(f"{len(leagues)} league(s) for {year}:")
    for league in leagues:
        print(f"  - {league['name']} (league_id={league['league_id']})")

    if len(sys.argv) > 3:
        league = get_league(sys.argv[3])
        print(f"\nformat for {league['name']}: {describe_format(league)}")
