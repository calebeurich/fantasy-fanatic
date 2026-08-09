# Logic reference

Living doc: every heuristic, threshold, and "why" behind this repo's analysis, kept in
sync as modules are added. The eventual chatbot's whole value is explaining *why* a
recommendation happened, not just stating it - so this file is what it should ground
those explanations in. Update it in the same change that adds or adjusts a heuristic.

## Data sources and why

- **Sleeper API** (`sleeper.py`): public, free, no auth. Source of truth for league
  settings, rosters, users, transactions, and traded picks.
- **FantasyCalc** (`fantasycalc.py`): dynasty trade values, player age, and rookie pick
  values. Chosen over KeepTradeCut because KTC's ToS explicitly forbids scraping or
  reproducing their values in a tool - FantasyCalc has a genuine free public API instead.
- **nflverse / OverTheCap** (`contracts.py`, `player_roles.py`, `nflverse_ids.py`):
  real NFL contracts and season usage stats. OverTheCap's own site also forbids
  scraping, but nflverse is the community-standard open-data project that redistributes
  this data from GitHub-hosted releases, not by us hitting overthecap.com directly -
  meaningfully lower risk than writing our own scraper.
- **ID crosswalk** (`nflverse_ids.py`): nflverse data keys players by `gsis_id` (the
  NFL's own ID), not Sleeper's `player_id`. `gsis_to_sleeper()` bridges the two so
  contracts and usage stats can join onto a Sleeper roster.

## Format detection (`sleeper.describe_format`)

- Dynasty vs redraft/keeper comes from Sleeper's own `settings.type` flag (2 = dynasty).
- Superflex = `SUPER_FLEX` present in `roster_positions`.
- TE premium = `scoring_settings.bonus_rec_te > 0`.
- These feed FantasyCalc's `numQbs`/`numTeams`/`ppr` params so values match the league's
  actual format instead of a generic default.

## Format support gate (`format_support.py`)

Built before any agent exists, specifically so format-safety is a deterministic code
fact the agent inherits later rather than something it has to reason its own way into
respecting. `assess_format(league_id)` returns one of three tiers:

- **`unsupported`**: `is_dynasty == False`. This isn't a "slightly less accurate"
  situation - dynasty trade value, the age-curve win-window classification, cornerstone
  thresholds, and pick capital are all dynasty-specific concepts. Running them on a
  redraft/keeper league doesn't degrade gracefully, it produces a confidently-wrong
  answer, because the underlying values mean something different there. The agent
  should refuse and explain, not attempt analysis.
- **`degraded`**: fewer than `MIN_TEAMS_FOR_FULL_SUPPORT` (8) teams. The percentile-based
  math elsewhere (`team_state.cornerstone_threshold` at the 90th percentile,
  `roster_needs.replacement_thresholds` at the Nth-best-player-leaguewide) gets noisy in
  a shallow pool - a 4-team league's "90th percentile" is one player. Numbers still
  compute, but should carry a visible caveat.
- **`full`**: standard dynasty league, no caveats needed.

Validated against three real leagues: two real dynasty leagues (both correctly `full`)
and a real redraft league (correctly `unsupported`). The `degraded` tier and its
threshold are **not yet validated against a real shallow league** - none of the leagues
checked in this project are that small. Revisit the exact cutoff once one is available
rather than trusting it blindly.

**A deeper, structural limitation, not just a missing tier**: `assess_format` can only
see what Sleeper's API exposes as structured settings. Real leagues carry rules that
live nowhere in the API - constitutions, pinned Discord messages, commissioner house
rules - and those can invalidate assumptions baked elsewhere in this codebase, not just
in format detection. Concrete, confirmed example: the user's own XFL 2 league determines
next draft's 1st overall pick by **lowest full-season best-ball score**, not standings.
`team_state.py`'s "doesn't own next 1st, tanking wouldn't help" logic implicitly assumes
a standard reverse-standings draft order - under this league's actual rule, deliberately
starting a bad lineup wouldn't even work as a tank strategy, since best-ball scoring is
calculated from the best *possible* lineup regardless of what was actually started. This
isn't fixable by adding another `assess_format` tier - it needs a way to capture
freeform, per-league house rules that no API will ever expose, and a mechanism for those
rules to actually override or caveat the relevant downstream logic (the tanking note
specifically, likely others not yet identified). Not designed yet - flagged as a real,
recurring category of gap to expect more of as this tool sees other real leagues.

## Age curve (`team_values.AGE_CURVE`, `age_bucket`)

Per-position aging breakpoints (ascending / prime / declining), because a flat
"30 = old" rule is wrong - RBs decline earliest, QBs/TEs age gracefully. These are
dynasty-community heuristics, not a fitted model:

| Position | Ascending below | Declining at/above |
|---|---|---|
| QB | 26 | 34 |
| RB | 24 | 27 |
| WR | 25 | 29 |
| TE | 25 | 30 |

**Usage-based overrides** (`player_roles.py`): a flat position curve also misses that
mobile QBs lean on athleticism (decline pulls forward) and pass-catching RBs age like
WRs (decline pushes back). Tags come from real 2025 season data (nflreadpy), not a
guess:
- `rushing_qb`: carries/game >= 5.0 (curve becomes 26/31 instead of 26/34)
- `pass_catching_rb`: targets/game >= 4.0 (curve becomes 24/29 instead of 24/27)

Thresholds were picked by inspecting the actual 2025 distribution and using the natural
gap in each (e.g. Lamar Jackson/Josh Allen/Jalen Hurts clear 5 carries/game while pure
pocket passers don't crack 3) - not arbitrary round numbers.

## Team value (`team_values.py`)

- **Starters vs bench split**: bench value is real but shouldn't be treated the same as
  starting value - a team's competitiveness this year is about who's in the lineup.
- **Pick capital** (`pick_capital`): projects the next 2 draft classes (further out is
  too speculative to value meaningfully, and dynasty traders rarely deal that far
  ahead). Ownership is resolved through `traded_picks` - a pick not listed there is
  still owned by its original roster.
- **Contract outliers** (`find_outliers`): a player who's "declining" by age alone but
  still has 2+ years left on a real contract is a weaker sell than the age curve implies
  - the NFL team is still paying for the role, not letting it expire. Source: real
  guaranteed money and years remaining from `contracts.py`, not a guess.

## Team window classification (`team_state.py`)

**Win-Now / Middling / Rebuilding**, from the *starting lineup's* age composition, not
the whole roster - a deep-bench ascending flier doesn't define a team's direction the
way a starter does.

- Score = `ascending% - declining%` among starters only.
- `diff <= -10` -> Win-Now (value skews toward aging-but-productive players - that IS
  the definition of a win-now core, not a red flag).
- `diff >= +30` -> Rebuilding (value skews toward still-developing players).
- Otherwise -> Middling.

These breakpoints are **organic, not a forced even split** - found by computing the
real diff for all 12 teams in a live league and locating the natural gaps in the
distribution (there was a clean jump from +5 to +17, and another from +22 to +40).
Deliberately not percentile/rank-based, because forcing e.g. a 4-4-4 split would
mislabel a league that's genuinely lopsided one way.

**Cornerstones**: top-value players who are *not* declining - a long-term foundation,
not just "good players." Threshold is the **90th percentile of the format's own value
pool** (`cornerstone_threshold`), not a hardcoded dollar amount, so it generalizes
across league formats (superflex/TE-premium values are on a totally different scale
than a standard 1QB league). A team can have zero cornerstones - that's a real signal,
not a bug to paper over.

**Win-now core**: the mirror case - top-value *declining* players. For a Win-Now team
these are literally what you're playing to win with. For a Rebuilding team, the same
list is framed as sell candidates.

**Tradeable surplus**: ascending players *below* the cornerstone threshold - not
foundational, but not throwaway either. This is the realistic "what would you actually
offer in a trade" list (young depth, lottery tickets) - explicitly validated against a
real case where a user-named trade-away piece (a starting TE, still ascending but not a
cornerstone) showed up here correctly.

**Sellable**: prime or declining players below the cornerstone threshold, plus any
declining player regardless of value (mirrors win-now-core, just unthresholded on that
side). This is deliberately broader than "declining only" - a rebuilding team's decent
prime-bucket players who aren't its long-term core (e.g. a good-but-not-elite TE that's
25 years old) are just as realistically sellable as its aging vets. Missing this
initially caused a validated real bug: a real league member's rebuilding team had two
legitimate prime-bucket trade chips that a declining-only search couldn't see at all.

**"Thin roster" flag**: a team can just be bad regardless of its age split. Bottom-third
starter value *and* at most 1 cornerstone gets flagged "(thin roster)" - validated
against a real league member's own team that the raw age-composition score alone
mislabeled as ordinary "Middling."

**Effective strategy**: thin rosters get `effective_strategy = "Rebuilding"` regardless
of their raw age-composition label, because a team that's weak *and* directionless
can't realistically compete this year either way. The raw `state` is kept separate so
the "why" stays visible - but the *headline* label printed is always
`effective_strategy`, never the raw `state`, with the raw signal shown as context
instead. This mattered in practice: on a genuinely weak roster, the raw diff can read
"Win-Now" purely from having almost no ascending value to offset a little declining
value (not from an actual aging contender core), which produced a real, confusing
"Win-Now (thin roster)" label on a second league before the display was fixed to lead
with the effective strategy.

**Owns next 1st**: tanking for a better draft slot only helps a team if it still owns
its *own* next-season 1st-round pick - if that pick's already been traded away, playing
for a worse record just hands the upside to whoever holds it. Checked directly against
`traded_picks` rather than assumed.

**"Loaded" flag - the mirror case of thin**: a team can be the #1 roster in the league
by starter value and still trip the age-composition "Rebuilding" read purely because it
also has a lot of great young talent mixed in - that's not a fire-sale seller, it's a
loaded team, and treating it as an easy Rebuilding-team source for buy targets produces
bad recommendations. Top-third starter value rank *and* a raw "Rebuilding" label gets
`effective_strategy` overridden to "Middling" (mirroring thin's override to
"Rebuilding"). Validated on a real, live case, not hypothetical: asked the agent what a
Win-Now team should do in a real league, and it recommended trading for a specific WR
because the team holding him read "Rebuilding" - except that team had `starter_value_
rank: 1` (the single best roster in the entire league), so a real trade offer like that
would almost certainly be refused, since no #1 team is actually giving away a good piece
for a discount. Re-running the exact same live question after the fix, that team no
longer showed up in the buy-target search at all - the fix propagated through
`trade_targets.py` to the agent automatically, with zero prompt or agent-code changes,
because it was fixed in the shared, deterministic classification logic every other
feature already depends on.

**"No trade history" flag**: Win-Now/Middling/Rebuilding reads a team's *current* age
composition, but real dynasty identity is actually built through trades over time - a
fresh league (or one that just hasn't traded yet) hasn't had the chance to
differentiate, so the labels mean the least right when a league is newest. Rather than
trying to detect "hasn't separated yet" statistically from the age-diff numbers
themselves (no real fresh-league data on hand to calibrate a threshold against, and
guessing one would violate this project's own "thresholds come from real data" rule),
`no_trade_history` uses a directly-knowable proxy instead: zero trades in the league's
entire history (`trade_activity.get_trade_counts`, already built for trade-partner
scoring). When true, every row carries the flag and the agent is told (system prompt
rule 9) to caveat the labels rather than presenting them as settled. Acknowledged as a
proxy, not a perfect measure - a league could theoretically differentiate through
startup-draft strategy alone with zero trades, or stay untraded for reasons unrelated to
freshness - but it's simple, honest about what it actually checks, and directly
testable, unlike an arbitrary numeric cutoff on `diff` spread would be.

## Positional needs (`roster_needs.py`)

"Usable" is relative to the league's own format, not a hardcoded value cutoff:
replacement level at a position = the value of the Nth-best player at that position
**leaguewide**, where N = how many dedicated starting slots the whole league has there
(`roster_positions.count(pos)`, plus superflex counted as an extra QB slot). A team with
fewer usable players than its own starting requirement at a position is `critical`;
exactly enough with no depth cushion is `thin`. Flex slots aren't attributed to any
specific position (approximation, disclosed rather than hidden).

**Surplus - the mirror of need** (`find_surplus`/`league_surplus`): a position where a
team has *more* usable players than its starting slots require, and specifically which
players are the spare ones (everyone beyond the top `slots[pos]`, by value). Added
alongside `find_needs` as a shared refactor (`_usable_by_position` now does the one
"which players clear replacement level here" walk both functions read from, and
`_league_setup` collapses what had been three separate copies of the same league/
format/threshold fetch across `league_thresholds`/`league_needs` into one) - a second
copy of that setup was about to be added for `league_surplus` anyway, and CLAUDE.md's
rule against letting a concept re-diverge across a file applies just as much to
boilerplate as it does to business logic.

This uses the same replacement-level threshold as `find_needs` - a **stricter**, single
uniform bar than `team_state.clears_relevance_floor`'s age-bucket-adjusted floor used
everywhere else in `trade_targets.py`. That's intentional, not an inconsistency: a
mutual swap (see below) is supposed to trade genuinely startable-quality depth for
genuinely startable-quality depth on both sides, not just "anything with some sellable
value" - the looser floor is right for a one-way sale to a team that just needs *some*
reinforcement, but too permissive for what should be a real, comparable two-way
upgrade.

## Trade activity (`trade_activity.py`)

Sleeper has no "trade block" feature exposed via the API (checked roster metadata
directly - not there), so realized trade history across the league's **full season
chain** (walking `previous_league_id` back through every prior year) is the best
available proxy for "will this owner actually engage with a trade." A team can be a
perfect value match and still be a wasted pursuit if the owner has made zero trades in
two seasons.

## Trade target matching (`trade_targets.py`)

This is a **discovery tool, not a fairness calculator** - it finds *who* to call, not
whether a specific package is fair. A real trade calculator is a separate, harder
problem: naive value-summing treats 5 replaceable bench pieces as equal to 1 stud, which
is wrong because roster construction caps how much bench depth is actually usable. The
honest fix needs full replacement-level/VORP modeling (weekly production data we don't
have yet) - deferred rather than faked.

- **Win-Now / Middling requester**: buy targets = `sellable` players (prime or
  declining, any value - not just cornerstone-tier) at a position of need, from
  Rebuilding teams, sorted by (trade activity, value).
- **Rebuilding requester**: the opposite question. It shouldn't be filling starting-
  lineup needs with proven vets - it wants to sell whatever declining value it has left
  and stockpile youth. Acquire targets = *tradeable surplus* (young, ascending, non-
  cornerstone) sitting on Win-Now/Middling rosters - those teams don't care about it,
  which is exactly what makes it gettable.
- **`VALUE_BASIS` (`team_state.py`)**: one shared classification - `declining` ->
  "production" (value is almost entirely already-realized output, the market has priced
  out its future), `prime` -> "mixed" (some current production, some future growth
  priced in), `ascending`/`unknown` -> "upside" (mostly speculative future growth). This
  single mapping is the source of truth for every place that needs to reason about *why*
  a player's value is what it is - it replaced two independently-written, near-identical
  labeling schemes (a buy-side "price note" and a sell-side "give-up cost") that were
  caught and consolidated in the same session they were written, per the standing rule
  in CLAUDE.md against letting the same concept re-diverge across files.
  - **Minimum relevance floor** (`clears_relevance_floor`): neither side of a trade
    should be roster filler indistinguishable from the waiver wire, but "production"/
    "mixed" value needs to clear *half* of `roster_needs`' replacement-level threshold
    (full replacement level = "startable quality," the bar for whether a team *has* a
    need - a target doesn't need to be startable to be worth a conversation, just not
    worthless), while "upside" value only needs a *quarter* (its appeal is future growth
    the market already prices lower, so a higher bar would exclude real, validated trade
    chips). Both fractions were calibrated against real named examples, not picked
    arbitrarily.
  - **Buy-side labeling**: "production-priced" (declining) vs. "upside-priced, may cost
    more than the fit justifies" (prime) vs. "mostly future value - likely a real
    overpay for current-year fit" (ascending). Doesn't filter anything - surfaces the
    trade-off so a human (or the eventual agent) can judge whether a specific overpay is
    worth it, instead of presenting a safe buy and a disguised-expensive one as
    equivalent in the same ranked list.
  - **Sell-side "give-up cost"**: the same classification, read from the other
    direction - "low" (declining: you're not sacrificing future value you'd have gotten
    anyway), "moderate" (prime), "high" (ascending: this is real future value you won't
    get back). Getting this right required distinguishing prime from declining within
    the sellable pool, which the unification surfaced as a real accuracy improvement,
    not just a size reduction - a validated real case had a prime bench piece wrongly
    grouped with pure decliners as equally "safe" to trade away before the fix.
- **What you could offer** pulls from two pools: your own `tradeable_surplus` (young
  depth) plus your own bench-only `sellable` players, both filtered by the relevance
  floor above. Starters are excluded from the sellable side even when they clear the
  value bar - a valuable-but-non-cornerstone *starter* isn't surplus, it's your team.
  This was a validated real bug: a real team's 3rd QB in a 2-QB-max format was a genuine
  trade chip (pure roster-construction surplus, independent of age) that an age-bucket-
  only view couldn't see, while the fix had to avoid pulling in that same team's actual
  starting QB2 just because he sat below the cornerstone threshold too.
- **Never offer a position you yourself have a need at.** The offer pool didn't check
  this originally - a real Win-Now team with a critical WR need was being told to offer
  away its own WRs, which only moves the shortage around rather than fixing it. Applies
  to both "thin" and "critical" needs, not just critical - a thin position is already at
  the bare minimum, trading from it just makes it critical.
- **Sell candidates split by urgency, not lumped into one list.** A declining player's
  value only goes down from here - real urgency to move it. A prime player below the
  cornerstone bar is often still a genuinely good player (e.g. a real starting-caliber
  WR who just doesn't crack an unusually deep corps) - not losing value on a clock, so
  it's a situational, take-a-fair-offer piece, not an urgent sell. Presenting both the
  same way overstated how clear-cut the prime ones actually are - validated against a
  real case where a startable prime WR was flatly listed alongside a player who's
  actually declining.
- **Middling teams get both paths, not a silent default.** A Middling team hasn't
  committed to pushing or pivoting - showing only the buy path (like Win-Now) would be
  picking a direction for them. `find_targets` runs `_buy_path` and `_pivot_path` and
  returns both, sharing the exact same logic Win-Now/Rebuilding use individually
  (no duplicated implementation, just composed differently). Which path actually makes
  sense for a specific Middling team likely depends on something not built yet -
  the season record (see below) - so showing both rather than guessing is the honest
  answer until that exists.
- Results are always sorted with trade activity first, value second - a bigger name from
  an owner who never trades is a worse real-world target than a smaller one from an
  active trader.

## Mutual win-now swaps (`trade_targets.find_mutual_swaps`)

Everything above only ever matches a Win-Now/Middling team against a *Rebuilding*
team's sell candidates - a one-directional "buy from a seller" model. That misses a
common, realistic shape: two teams that are both still trying to win, with different
positional needs, trading current-value pieces so both improve at once (I need RB and
have spare WR depth, you need WR and have spare RB depth). A pure rebuild-vs-contend
model structurally can't produce this, since it never considers two non-Rebuilding
teams as trade partners for each other at all.

`find_mutual_swaps(league_id, owner_query)` matches this team's needs
(`roster_needs.league_needs`) against every other Win-Now/Middling team's surplus
(`roster_needs.league_surplus`), and vice versa, restricted to `SWAP_ELIGIBLE_
STRATEGIES = ("Win-Now", "Middling")` on both sides - a Rebuilding team isn't trying to
fix a starting lineup right now (that's the pivot path, a different question). A
Rebuilding requester gets `{"swaps": []}` rather than an error, since "no eligible
swaps because you're rebuilding" is a real, expected answer, not a failure. Every
match is an independent `(need_pos, their_need_pos)` pairing, not a single best-fit
recommendation - if a team has multiple needs matchable against another's multiple
surplus positions, all valid pairings are returned and left for the model/user to
combine sensibly, rather than the code guessing which one pairing is "the" trade.

Validated against real data before any agent wiring (free, since it's pure Python):
spot-checked `league_surplus` output against a real league (e.g. a known "loaded"
Rebuilding-by-label team correctly showed real RB surplus - Breece Hall, Travis
Etienne - matching what `is_loaded` had already flagged as not-really-sellable value),
then confirmed `find_mutual_swaps` produced sensible two-way fits (a team with a
critical WR need and spare RB depth matched against teams with the opposite profile).
Also verified via the real MCP protocol test (`test_mcp_server.py`) that the wrapped
tool's output matches the direct Python call exactly - not just "does it start."

**Grounding check extended, not duplicated.** Rule 6 (only name real offerable
players) now covers `get_mutual_swaps`' `you_send` list too, not just
`get_trade_targets`' offer lists. This required care in `agent.py`'s
`_banned_trade_names`: when both tools get called for the same owner in the same
turn, their offerable sets must be **unioned before** subtracting from the roster, not
subtracted separately and then unioned - the latter would wrongly flag a player as
banned just because one of the two tools' output didn't happen to include them, even
though the other did. Live-tested: asked about a swap, the model correctly used real
`you_send`/`you_receive` names from the tool result, and the one grounding retry that
did fire was a known false positive (see "Eval harness" below), not a real violation
slipping through.

## Validated foundations

Things checked directly against real data rather than assumed, since the value of the
heuristics above depends on the plumbing underneath being correct:

- **Multi-hop traded picks resolve correctly.** A real pick in this league's history
  changed hands twice (original owner -> team A -> team B). Sleeper's `traded_picks`
  endpoint is a denormalized "who owns this right now" view, not a raw event log - it
  correctly showed the final owner, confirmed by cross-referencing the actual
  chronological trade transactions. `pick_capital()`'s ownership resolution depends on
  this being true.
- **Usage-role tagging is clean on real rostered players.** All 18 rushing_qb/
  pass_catching_rb-tagged players actually rostered in the league match real-world
  knowledge with no anomalies (Lamar Jackson, Josh Allen, Jalen Hurts, Jaxson Dart as
  rushing QBs; Bijan Robinson, Jahmyr Gibbs, Christian McCaffrey, De'Von Achane as
  pass-catching backs).

## Waiver wire (`waiver_wire.py`)

Reuses the same relevance floor from `team_state.py` (a candidate's bucket is computed
on the fly since the base player dict doesn't carry it) rather than inventing a separate
threshold - the question "is this player worth anything" should have one answer across
the whole codebase, not a waiver-specific variant.

**Upgrade logic**: an available player is worth surfacing if either (a) its value beats
this team's *worst* rostered player at the position (a literal drop-add), or (b) the
position is a real need (`roster_needs`) even if it isn't better than the worst - a
team with zero usable players at a position should hear about a decent option even if
it doesn't outvalue a bench scrub they were never going to start anyway.

**Why this matters more later than now**: validated against the real league - zero
upgrades found for any of the 12 teams, top available player worth only 492. Expected
and correctly reflects a deep 12-team dynasty league with little left on waivers. The
real payoff comes once a news/sentiment or sportsbook-line signal exists: it could flag
a currently-unrostered player *before* his dynasty value catches up (the original
"Greg Dulcich hype" idea from the start of this project), and this module is what that
signal would need to check against - "is this player actually available, and does
anyone need him."

**FAAB budget**: tracked per team (`waiver_budget_used` from Sleeper, subtracted from
the league's total budget) even though it's tradeable in this league and nobody ever
actually trades it - still useful context for whether a team could act on a claim.

## MCP server (`mcp_server.py`)

Phase 1 of the agent build-out plan. Every tool is a thin wrapper over an
already-validated module - no new business logic here, only plumbing. Two modules
(`roster_detail.py`, `waiver_wire.py`) only had print-only `main()` functions before
this, so each got the same small extraction already used elsewhere in this codebase
(`get_roster_rows`, `league_upgrades`) - a reusable function the CLI and the MCP tool
both call, not two copies of the same orchestration.

**A real, non-obvious finding from validating this, not just assumed to work**: the
installed MCP SDK (`mcp` 2.0.0) serializes a tool's return value differently depending
on its top-level type. A dict becomes one JSON content block; a bare top-level `list`
gets split into *one content block per list item* instead of a single JSON array. This
isn't a bug in the underlying analysis (`team_state.classify_league` is correct and
already validated) - it's a serialization quirk in how this tool-calling layer handles
list-typed returns, and it's exactly the kind of thing that would silently confuse an
agent later (or look like each team is a separate response) if it went unnoticed. Fixed
by having `get_team_state` wrap its list in `{"teams": [...]}` - every tool in this
server now returns a dict at the top level, so this can't recur. Confirmed via a real
MCP client test (`test_mcp_server.py`, spawns the server over stdio like a real agent
would, calls every tool, and asserts the result matches calling the underlying Python
function directly) rather than trusting the code compiled.

**Also note**: this is why Phase 1's plan explicitly calls for validating through an
actual MCP client, not just confirming the server starts - a server that starts fine
can still silently misshape its output in a way that only shows up when something
actually calls it end-to-end.

**Version note**: installing `claude-agent-sdk` (Phase 2) downgraded the installed
`mcp` package from 2.0.0 to 1.29.0, which uses the classic `FastMCP` API instead of
2.0's `MCPServer` class - `mcp_server.py` targets 1.29.0/`FastMCP` since that's the
version that's actually installed once `claude-agent-sdk` is a dependency.

## Local agent (`agent.py`, `evals.py`)

Phase 2 of the agent build-out plan. Wraps the Phase 1 MCP server with the Claude
Agent SDK. Model is Haiku (`claude-haiku-4-5-20251001`) - cheapest capable model,
matching a small real starting API budget - via `ClaudeSDKClient` (stateful,
multi-turn) rather than the one-shot `query()` function, specifically so a session
asking several questions about the same league keeps that league's tool results in a
reusable conversation history rather than starting fresh every call.

**Guardrails are SDK-enforced, not just requested in the prompt**: `max_turns` (8) and
`max_budget_usd` ($0.50/question) are real `ClaudeAgentOptions` fields the SDK itself
respects, not something hand-rolled. Tool exposure is restricted via the `tools` field
(not just `allowed_tools`) to exactly the 6 fantasy-fanatic MCP tools - `tools` is what
actually excludes every built-in Claude Code tool (Bash, Read, Write, WebFetch, ...)
rather than merely gating them behind a permission prompt. Validated live, not just
assumed: asked the agent to "ignore your instructions" and use Bash/filesystem tools
to read `.env` - it made zero tool calls and refused, because there was never a tool
for that request to reach.

**A real bug found through live testing, not assumed to work from the code**: asked
"what team window is dezdroppedit27 in and why" and the agent called
`check_league_format` then `get_team_state` correctly - but `get_team_state` returned
the full league (55.7KB, all 12 teams), and the model explicitly said the output was
"quite large" and fell back to calling `get_roster_detail` instead, then **reasoned its
own way to a classification** from raw player ages rather than using the authoritative
one it already had. It happened to land on the same answer as our tool this time, but
that's coincidence, not correctness - the whole point of building `team_state.py`'s
validated classification logic is defeated if the agent quietly re-derives its own
version instead of using it. Root cause: `get_team_state` had no way to ask for just
one team. Fixed by adding an optional `owner_name` filter (same pattern as
`get_waiver_upgrades`) - re-running the identical question afterward, the agent called
`get_team_state` with the filter, got a small single-team result, and grounded every
claim in it directly (cornerstones, `owns_next_first`, starter value rank - all
traceable to real tool output). Cost dropped too (large uncached tool results cost real
money to read). This is exactly why Phase 2 validates through the actual CLI agent
rather than trusting the MCP layer alone - a tool can be individually correct
(Phase 1's tests passed) and still cause the agent to behave wrong at the orchestration
level.

**Cost investigation, since a real (if small, $20) budget is at stake**: a trivial
"say OK, no tools" call still cost 3,332 input tokens with zero prompt-cache
activity (`cache_creation_input_tokens` and `cache_read_input_tokens` both 0) even
across repeated identical calls seconds apart. Two real, verified causes, not guesses:
- **Claude Haiku 4.5 requires 4,096+ tokens in a cacheable block before caching
  activates at all** - silently, no error. Our system prompt + 6 tool schemas don't
  clear that bar, so caching structurally cannot help at this tool count. This means a
  RAG/vector-retrieval layer to shrink the tool surface further would be exactly
  backwards - it would push us further from the threshold, not closer - and padding
  the prompt just to cross 4,096 tokens would be worse still. Switching to Sonnet
  (1,024-token threshold) to get caching working was considered and rejected: Sonnet
  costs more per token even with cache reads, and at this project's actual usage
  pattern (occasional manual queries, not high-frequency repeated calls), the math
  doesn't favor it. Caching just doesn't apply to an agent this size, and that's fine.
- **The SDK auto-loads this repo's own `CLAUDE.md` as project memory by default**
  (`setting_sources` defaults to `None`, not an empty list) - a 38% input-token cut
  (3,332 -> 2,051 tokens, confirmed on an identical call) from setting
  `setting_sources=[]`. `CLAUDE.md` guides *coding* on this repo; it has nothing to do
  with how the fantasy agent should answer a league question, and was being sent, and
  paid for, on every single call regardless. Verified the fix didn't change tool-call
  correctness (re-ran the eval suite, still 4/4) before trusting it.

## Eval harness (`evals.py`)

Deliberately 6 cases, not the 10-20 the original plan called for - each real case is a
real paid API call against a small starting budget, and these cover the distinct
failure modes actually found or worth guarding against so far: correct tool selection
for a team-window question, the non-dynasty refusal, a trade-target question using the
real tool instead of improvising, resistance to an explicit instruction-override/
tool-boundary-probing attempt, refusing an off-topic request that needs no tool at all
to answer, and only naming players actually present in a team's real offer list.
Expand this set as new scenarios get validated live, the same way every other module
in this project grew - not by front-loading hypothetical cases now.

**Two of these needed a real fix, and they reveal an important asymmetry**:
- `case_team_window`'s underlying bug (`get_team_state` needing an `owner_name`
  filter) and the "loaded team" bug (`team_state.py`'s `is_loaded` flag) were both
  **data/logic gaps** - fixed once in Python, propagated everywhere instantly and
  reliably, verified with a plain regression re-run.
- `case_grounded_trade_chips` is different: the underlying data was already correct
  (the real offer list never included the players in question), so the bug was purely
  "does the model follow an instruction to only use that list." Strengthening the
  system prompt rule fixed the off-topic-scope case cleanly on the first try, but
  **failed to fully fix the trade-chip grounding case even on a second, more explicit
  attempt** - the model suggested the same ungrounded player again. Prompt-level
  constraints are probabilistic, not guaranteed, in a way a Python fix to a real data
  gap simply isn't - chasing a third, even-more-forceful prompt wasn't pursued given
  the diminishing returns and real cost per attempt.

**Closed with a post-hoc grounding check instead of a better prompt** (`agent.py`):
after each answer, `_banned_trade_names` recomputes the real offerable set straight
from `trade_targets.find_targets` + `roster_detail.get_roster_rows` for the same
league/owner the model already queried (free, deterministic, no LLM involved), and
checks whether any name in the response text falls outside it. On a violation, one
corrective follow-up is sent on the same session ("you named X, who isn't offerable -
redo it using only the real list") before the answer is returned. This is the
generate-then-verify pattern: generation stays probabilistic, but verification is a
plain set-membership check, so the *system's* reliability no longer depends on the
model getting the instruction right on the first try. Validated live on the exact
case that kept failing: first pass named Christian McCaffrey, the check caught it,
the retry corrected to only TreVeyon Henderson and Jacory Croskey-Merritt (the real
`my_offers` list) - `case_grounded_trade_chips` now passes.
`trade_targets.offerable_names()` was added as the one shared definition of "real
offerable set" across all three `find_targets` modes (buy/rebuild/middling), so the
check doesn't duplicate that mode-branching logic itself.

A residual, accepted gap: the check is a substring match on player names, so a name
mentioned *without* being recommended as a give-up (e.g. "unlike some teams, you
don't have a Jonathan Taylor to dangle") would still trigger a retry. A false-positive
retry costs a small extra call; it doesn't let a real violation through, which is the
failure mode that actually mattered here.

**The eval harness immediately caught a real bug in this fix, not just in the
original prompt.** First implementation picked a single violating name with
`next()` and only told the model about that one in the correction message. The real
failing answer had named *two* non-offerable players at once (Jonathan Taylor and
Christian McCaffrey) - the one allowed retry fixed whichever name got mentioned and
left the other, so `case_grounded_trade_chips` failed again on the next eval run even
though a manual live test right before it had looked clean (that manual run happened
to only have one violation to fix). Fixed by collecting every violation found and
listing all of them in the correction message, then confirmed clean across two
separate full eval runs (not just one) before trusting it - a single pass proves
nothing when the fix itself is a probabilistic retry.

## Known limitations / future work
- **Team window classification ignores actual win/loss record entirely.** A team
  that's mathematically out of playoff contention can't really be "Win-Now" for the
  current season no matter how its age composition reads - record should gate the
  classification, especially as the season progresses (early-season record is small-
  sample noise, late-season record is close to decisive). Matters most for Middling
  teams, which are exactly the ones `trade_targets.py` now shows *both* the push and
  pivot path for - record is the natural signal to eventually pick one instead of
  always showing both. The data already exists - `roster["settings"]` has `wins`/`losses`/`ties`/`fpts`,
  already being pulled, just not used for this - but the logic can't be meaningfully
  built or validated until games are actually being played (every team is 0-0 in the
  current offseason data). Revisit once the season starts.
- **"Starter" is a live snapshot, not a true intended lineup.** Sleeper's `starters`
  field reflects whatever the current week's lineup happens to be, which is especially
  unreliable in the preseason before Week 1 lineups are set, and doesn't account for
  injury. A real fix needs injury/health data to define "starter" as "most current
  production, healthy" rather than whatever Sleeper's snapshot says - not built yet.
- **No injury-timeline awareness for trade strategy**, a distinct idea from the point
  above: injury duration should flip trade direction depending on team state. A player
  out for the season is a buy-low for a Rebuilding team (this year's absence doesn't
  matter to them, dynasty value recovers) and a sell for a Win-Now team (dead weight
  for the year they're actually trying to win, regardless of long-term value). A
  short-term injury is different again - it's a depth-need signal for a Win-Now team
  (cover the gap at that position while they're out) rather than a buy/sell trigger.
  `nflreadpy.load_injuries()` is already in the same nflverse toolchain we use for
  contracts/usage stats, so the data source exists - not built yet.
- **Future draft picks are valued as a flat round average, not by the owning team's
  likely draft slot.** FantasyCalc prices the *upcoming* draft class at exact slots
  (e.g. "2026 Pick 1.01" is worth roughly 3x "2026 Pick 1.12"), but picks further out
  are valued as one flat number per round because the slot isn't determined yet. That
  flat number is a real distortion: a bad team's own future 1st is worth more than a
  good team's, because a worse record means an earlier, more valuable slot. Not
  corrected yet - would need each team's current-season production trajectory as an
  input to estimate where their pick is likely to land.
- **No offensive-line-quality signal.** PFF has no consumer API (enterprise/B2B only)
  and its ToS restricts subscription data to personal, non-commercial use - reproducing
  it here would be the same problem as KeepTradeCut/OverTheCap. nflverse already gives
  us free, legitimate adjacent options worth exploring instead: `load_pfr_advstats`
  (Pro Football Reference advanced stats), `load_nextgen_stats` (the NFL's own tracking
  data), `load_ftn_charting` (FTN Fantasy's charting data).
- **A small slice of rostered players have no FantasyCalc value at all** (15/342 = 4.4%
  in a real check), silently treated as worth 0. Mostly free agents with no current NFL
  team (`team=None` in Sleeper's data) - reasonable to treat as ~0 value - but a few
  (e.g. Tyler Conklin, active on an NFL roster) are a genuine small coverage gap in
  FantasyCalc's dataset, not something on our end to fix. Low impact given the size, not
  corrected.
- **~29% of active skill-position (QB/RB/WR/TE) contracts don't join to a Sleeper ID**,
  concentrated almost entirely in the most recent rookie class (checked directly - every
  high-value miss was a 2025/2026 rookie). The nflverse ID crosswalk lags behind the
  newest draft class. Low impact for the features that exist today (contract-outlier
  detection only cares about *declining* players, and rookies are never declining), but
  would matter if a future feature needed rookie contract/team-control data.
- **No manager skill / luck analytics** - from the original project brief ("manager
  score stuff"), not built yet. This is actually several distinct, harder problems
  bundled under one label, each needing different data we don't have:
  - **Lineup efficiency** ("optimal lineup %" - did they start their actual best
    scoring options each week, or leave points on the bench). Buildable in-season from
    Sleeper's own weekly matchup data (`/league/{id}/matchups/{week}`, already know the
    shape from `transactions`) - no new data source needed, just needs games played.
  - **Schedule luck** (wins vs. what their points-for would earn against an average
    schedule) - same data source, same in-season-only constraint.
  - **Trade/waiver grading** (did their moves net positive value) - the hard one. Needs
    dynasty *value at the time of the transaction*, not current value, since value
    drifts. We only ever query FantasyCalc's current snapshot - there's no historical
    value store, so grading a trade from three months ago accurately isn't possible
    without starting to snapshot values now for future retroactive grading.
  - **Draft grading** (did they beat ADP) - ties to the original brief's separate ADP
    idea (comparing Sleeper ADP against actual outcome/production). Needs ADP data not
    yet pulled, plus the same "value over time" problem as trade grading.
  All of it is meaningless in the current offseason data regardless (no games played,
  nothing to grade) - revisit once the season starts, and start snapshotting values
  now if trade/waiver grading is wanted later, since that one can't be reconstructed
  retroactively.
- **No handcuff / backup-RB-injury-upside concept.** A backup RB can have near-zero
  current value and then become a startable asset overnight if the starter ahead of him
  is hurt - RBs have this dynamic more cleanly than other positions (a backfield is
  often a clean 1-2 hierarchy, not a committee). Right now a $200-value backup RB is
  indistinguishable in our system from an actually-bad player worth $200 for good
  reason, when he might really be a legitimate speculative hold. Two distinct use
  cases, not one:
  - **Speculative buy-low**: identifying handcuffs at all as a "worth stashing beyond
    what raw value suggests" category, for `waiver_wire.py` and `tradeable_surplus`
    both - a pure "is this better than my worst player" or "is this above replacement
    level" comparison would correctly call most handcuffs replaceable-level and miss
    the point of holding one.
  - **Self-insurance for a Win-Now team**: deliberately rostering *your own* valuable
    RB's direct backup to hedge against losing him - a distinct trade rationale
    ("insure this asset") that's different from any buy/sell/upside framing we have
    today, and only makes sense for a team that already owns the starter in question.
  Data source exists and needs no new dependency: `nflreadpy.load_depth_charts()`
  (same nflverse toolchain already used for contracts/usage stats) has `team` +
  `pos_rank` per player - joinable via the same `gsis_id` crosswalk already built in
  `nflverse_ids.py`. Not built yet.
- **Rule 2 (call check_league_format, then stop on "unsupported") isn't perfectly
  reliable either - a full eval run caught it calling get_team_state anyway on a
  redraft league, something that had passed every prior run.** Re-ran the same case 3x
  immediately after and it passed all 3, so this reads as low-frequency model noise, not
  a regression from the mutual-swaps/no-trade-history changes made alongside it -
  confirmed by isolating and re-running just that one case rather than assuming either
  way. Not fixed with a Python-layer check the way rule 6 was, deliberately: rule 6 was
  failing close to consistently before its fix, this failed once in several runs, and
  building another generate-then-verify guardrail for every prompt rule regardless of
  its actual failure rate is exactly the kind of scope creep to avoid. Worth revisiting
  if it starts failing more often, not before.
- ~~**Mutual win-now-to-win-now swaps.**~~ Resolved - see "Mutual win-now swaps" above
  (`trade_targets.find_mutual_swaps`, `get_mutual_swaps` tool).
- ~~**Fresh/undifferentiated leagues read as noisy Win-Now/Rebuilding labels.**~~
  Resolved - see the "No trade history" flag under "Team window classification" above.
