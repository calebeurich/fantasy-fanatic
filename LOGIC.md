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
- `is_dynasty`/`is_superflex` (-> `numQbs`)/`num_teams`/`ppr` feed FantasyCalc's own
  `values/current` API params directly, so QB inflation in superflex, PPR-vs-standard
  scoring, and dynasty-vs-redraft pricing all come from FantasyCalc's own value model
  recalculating at the source - not something this project approximates itself.
- **`is_te_premium` is computed here but never used downstream - confirmed via a full
  grep, not an oversight to fix.** FantasyCalc's `values/current` endpoint has no
  TE-premium parameter at all (checked their documented param list: `ppr`, `num_qbs`,
  `num_teams`, `is_dynasty` only) - there's nothing to feed it into. In a TE-premium
  league, every TE value pulled here is priced as standard scoring, so TEs are likely
  undervalued relative to what they're actually worth in that league. No clean fix:
  inventing a manual TE multiplier with no real data to calibrate it against would be
  exactly the kind of guessed heuristic this project avoids everywhere else (same
  reasoning as the punted offensive-line-quality gap below). Logged as a real,
  source-level limitation, not a bug in this codebase.

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
  activates at all** - silently, no error. A trivial no-tools call doesn't clear that
  bar, which is why the original measurement showed zero cache activity.

  > **Correction (measured later, and the original conclusion here was wrong).** This
  > section previously concluded "caching structurally cannot help at this tool count."
  > That was an over-generalization from a *trivial* call to *all* calls, and the
  > Anthropic console contradicted it - showing a real 26% cache hit rate. Measured
  > directly on a real tool-using question: the cacheable prefix is **~4,714 tokens**,
  > which *does* clear the 4,096 bar, and caching genuinely works. A controlled test
  > made this unambiguous - turn 1 of a session: `cache_creation=4,714`,
  > `cache_read=0`; turn 2 of the *same* session: `cache_read=9,766` (a large, real
  > hit); a **brand-new session seconds later**: `cache_read=0` again.
  >
  > So the accurate picture is: **caching works within a question and is thrown away
  > between questions.** Each `run_query` opens a fresh `ClaudeSDKClient`, and
  > something session-unique in the CLI transport's prefix breaks the cache key, so
  > every question re-pays ~4,700 tokens of cache *creation* - which bills at 1.25x,
  > i.e. more than plain input. The agent still comes out ahead within a single
  > question (3-5 turns, later turns reading the cache), which is exactly the ~26% hit
  > rate the console reports. Only the 5-minute TTL is used; the 1-hour TTL shows
  > `ephemeral_1h_input_tokens: 0` throughout.
  >
  > **Deliberately not "fixed" by reusing sessions across questions.** That would
  > recover the ~4,700-token-per-question creation cost, but conversation history would
  > accumulate into the prefix (growing cost per question until it exceeds the saving),
  > and on a public endpoint one user's context would leak into another's session -
  > a privacy problem, not just a cost one. The current per-question isolation is the
  > right default; this is a documented cost, not an outstanding bug.
  >
  > The lesson worth keeping: the original claim was measured, but measured on the
  > wrong thing, and then stated more broadly than the evidence supported. An external
  > signal (the console dashboard) is what caught it.

  A RAG/vector-retrieval layer to shrink the tool surface would still be backwards
  here - it would push the prefix back *below* the caching threshold, not above it.
  Switching to Sonnet (1,024-token threshold) was considered and rejected on cost:
  Sonnet is more expensive per token even with cache reads, and this project's usage
  pattern (occasional queries, not high-frequency repeated calls) doesn't favor it.
- **The SDK auto-loads this repo's own `CLAUDE.md` as project memory by default**
  (`setting_sources` defaults to `None`, not an empty list) - a 38% input-token cut
  (3,332 -> 2,051 tokens, confirmed on an identical call) from setting
  `setting_sources=[]`. `CLAUDE.md` guides *coding* on this repo; it has nothing to do
  with how the fantasy agent should answer a league question, and was being sent, and
  paid for, on every single call regardless. Verified the fix didn't change tool-call
  correctness (re-ran the eval suite, still 4/4) before trusting it.

## Hosting platform evaluation (Phase 5) - decided: Google Cloud Run

The original plan defaulted to AWS Lambda behind API Gateway without seriously
comparing alternatives. Revisited properly rather than treated as already decided,
and one real problem turned up: **Lambda itself has a genuine permanent free tier
(1M requests + 400K GB-seconds/month), but API Gateway does not** - new accounts get
a 6-month credit, then it's real per-request billing on top of CloudWatch Logs
ingestion charges. "Lambda + API Gateway" quietly becomes a 2-3-service bill, not the
free/near-free setup the plan assumed.

Compared four real options against what this agent actually needs (a Python process
that spawns a child process over stdio for MCP - needs a real container/OS
environment, not a constrained edge function):

- **AWS Lambda + Function URL** (a built-in HTTPS endpoint on the function itself,
  skipping API Gateway entirely) - closes the gap above while staying 100% AWS.
  Genuine permanent free tier. More moving parts than the alternatives below (IAM,
  container-image packaging, Secrets Manager, DynamoDB for a request counter).
- **Google Cloud Run** - best technical fit: it's a real container, so nothing about
  this codebase needs to change to run there, one HTTPS endpoint is built into the
  service (no separate gateway piece), and its always-free tier is larger (2M
  requests + 360K GB-seconds + 180K vCPU-seconds/month, permanent).
- **Firebase** - its Python compute (2nd-gen Cloud Functions) *is* Cloud Run under
  the hood, wrapped in a friendlier single CLI (`firebase deploy`). Same technical
  outcome as Cloud Run, easier on-ramp, weaker "AWS" keyword match for a resume.
- **Render** - simplest possible deploy (connect a GitHub repo, no cloud console),
  real free tier, but free services sleep after 15 minutes idle and cold-start
  ~1 minute on the next request - a real risk for a portfolio link someone clicks
  cold. Weakest cloud-provider resume signal of the four.

**Decided: Cloud Run.** Checked real postings and hiring-trend data across several
top companies before deciding rather than guessing: AWS is the single most-named
individual cloud platform in data science/AI engineering postings, but it's almost
always listed as "AWS, Azure, or GCP" - interchangeable cloud familiarity, not an
AWS-exclusive requirement, except at companies whose own product is that cloud
(Amazon's own postings skew AWS, Google's skew GCP). Amazon itself isn't a specific
target employer here, so there's no reason to eat the extra complexity and the
API-Gateway cost gap above just for a marginal keyword match. Cloud Run wins cleanly
on its own merits: best technical fit (real container, nothing to restructure),
larger genuine free tier, and one fewer service to reason about.

## HTTP API wrapper (`agent/api.py`)

Built ahead of the platform decision above, deliberately - it's the one piece every
option on that list needs regardless of which wins (none of them can invoke a
one-shot CLI directly), so building it first makes real progress without committing
to a provider. Plain FastAPI, no provider-specific code: two routes, `/health` and
`POST /ask` (question in, `{text, cost_usd, num_turns, grounding_retries}` out),
calling `agent.run_query` directly - the exact same function the CLI and the eval
harness already use, so there's no second code path to keep in sync.

Validated locally end-to-end before trusting it: started the server with `uvicorn`,
sent a real question via `curl`, got a real grounded answer back, and confirmed
`observability.py` logged the run identically to a CLI-triggered one with zero extra
wiring - the logging lives inside `run_query` itself, so it doesn't care who called it.

**Daily budget ceiling (`agent/budget.py`)** - the thing that actually makes a public
endpoint safe to expose, and a sequencing error caught only on a deliberate re-review
of the plan: `agent.py`'s `MAX_BUDGET_USD` caps a *single call*, which is the wrong
unit for a public URL. At ~$0.015-0.05 a call, roughly 40 uncapped requests would
drain the entire project API budget, and a bot scanning for open endpoints does that
in under a minute. The plan had "create the GCP account and connect a public
endpoint" scheduled *before* this existed - exactly backwards.

**In-process counter, no database - a real simplification over the original plan.**
That plan called for DynamoDB/Firestore to hold the counter, but persistent storage is
only necessary when several instances each hold a partial count. Pinning Cloud Run to
`max-instances=1` makes a plain in-process counter exactly accurate with zero extra
infrastructure - and this service wants that anyway, since every request spawns both a
`claude` CLI process and an MCP server subprocess, making it memory-heavy rather than a
horizontal-scaling workload. `concurrency=1` alongside it removes any check-then-record
race, so the ceiling can't be overshot by parallel requests. Giving up horizontal
scaling is a real tradeoff, and the right one for a demo.

Two ceilings, because one isn't enough: a dollar ceiling (the thing actually being
protected) plus a request-count backstop, since a failed call can report `cost_usd:
None` and would otherwise never move the dollar counter at all. Failed calls still
count against the day rather than being free to retry in a loop. Bounded, accepted
imprecision: a call's real cost isn't known until after Claude has been called, so the
check is "has the ceiling already been passed?" - it can be exceeded by at most one
call's worth ($0.50 worst case) before the next request is refused. Pre-estimating
token cost to close that gap would be a guess, so it isn't done.

Verified end-to-end rather than assumed, and cheaply: both ceilings were tripped in a
free unit-style check first (no API calls), then the real integration was tested with a
deliberately tiny `DAILY_BUDGET_USD` so one real call exhausted it - the first request
went through and recorded $0.015, and the next was refused **in 41ms with no API call
at all**, returning a static message. A `/budget` route exposes the counter so the cap
is externally verifiable rather than something to trust is working.

**Still open, and gating public exposure**: the cap above is only sound while
`max-instances=1` actually holds - it's an assumption enforced by Cloud Run service
config, not by the code, so that setting has to be verified at deploy time rather than
assumed. Deploy sequence is therefore: deploy with **authentication required** first
(private, zero abuse surface) to prove the container builds and the Node/CLI/MCP
subprocess stack works at all, confirm `/budget` behaves against the real service, and
only then make it public.

## Container image (`Dockerfile`)

**Local machine doesn't need to run any of this** - a real, worth-stating-explicitly
point that almost got lost in planning: Cloud Run builds and runs the container in
Google's cloud, not locally. The `Dockerfile` only needs to exist as a text file in
the repo; Google Cloud Build can build it directly from the GitHub repo through the
browser console, with no Docker install, no `gcloud` CLI, and no local build step
required at all for the core path. Local Docker only becomes useful later as a faster
local-iteration/debugging loop, not a requirement.

**It took five real failures to get this working**, none of which could be caught
locally (no Docker here or on the dev machine). The sequence is worth keeping,
because four of the five were invisible in exactly the same way:

1. **`pip install` died on a corrupted wheel** ("PACKAGES DO NOT MATCH THE HASHES").
   `pip-system-certs` was installing unconditionally - it exists only to work around
   Norton's TLS inspection on the Windows dev machine, and it patches Python's SSL
   handling, which is meaningless in a Linux container and a plausible cause of a
   truncated download. Gated behind `sys_platform == "win32"`.
2. **Secret Manager permission denied.** The compute service account had project
   **Editor**, which looks sufficient but deliberately excludes secret *payload*
   access - `roles/secretmanager.secretAccessor` has to be granted explicitly.
3. **`--dangerously-skip-permissions cannot be used with root/sudo privileges`.**
   `permission_mode="bypassPermissions"` becomes that CLI flag, and the CLI refuses it
   as root. Cloud Run runs as root by default - fixed with a non-root `appuser`, which
   is better container practice anyway.
4. **A wrong fix, honestly recorded**: the MCP subprocess was spawned as
   `python -m agent.mcp_server`, which assumes `python` on PATH *and* the repo root as
   CWD. Both were changed to be assumption-free (absolute `sys.executable` + absolute
   script path + `sys.path` bootstrap). This was a **guess, and it was wrong** - the
   later diagnostics showed `cwd` was `/app` and the interpreter path was fine all
   along. The change is still an improvement, but it fixed nothing that was broken.
5. **The actual cause: an unpinned dependency.** `requirements.txt` had no version
   pins. Locally `mcp` sits at 1.29.0 (which has `mcp.server.fastmcp.FastMCP`) only
   because installing `claude-agent-sdk` downgraded it from 2.0.0 earlier in the
   project's history. A clean container install resolved to a newer `mcp` where
   `FastMCP` moved, so `mcp_server.py` died on its import line. Everything is now
   pinned to versions verified working locally.

**The failure mode is the real lesson, not the version bug.** A `ModuleNotFoundError` -
about as diagnosable an error as exists - reached the user as an agent confidently
asserting *"dezdroppedit27 is in a Win-Now window. They likely have a strong current
roster"*, with no data behind it. Three layers each did something locally defensible:
the subprocess crashed and wrote to a stderr nobody read; the SDK swallowed the failure
and handed the model an empty toolset; the model, told by its system prompt that it was
a fantasy football assistant, produced a plausible answer rather than admitting it had
nothing. Earlier it had claimed to have "tools for design systems and background
monitoring," and later it emitted `<function_calls>` blocks as literal text - all
symptoms of the same silent void.

**A system that confabulates instead of crashing is worse than one that crashes**, and
this one did it while every individual component behaved "correctly." Two guessed fixes
went out before `/diagnostics` (`agent/api.py`) was built to spawn the MCP server
directly and report the captured subprocess stderr - after which the diagnosis took
seconds. The lesson recorded here for next time: when a failure is invisible, stop
guessing and spend the effort on visibility first. That endpoint is kept, not deleted,
for exactly that reason.

**One dependency that had to be gotten right without being able to test it**: the
Claude Agent SDK shells out to the `claude` CLI as its transport (`agent/agent.py`),
which needs Node.js - not just Python. Checked the actual current requirement rather
than guessing: `@anthropic-ai/claude-code` needs **Node 22+** as of mid-2026, so the
image installs Node via NodeSource on top of a `python:3.12-slim` base rather than
assuming whatever Debian's default `apt` Node package version happens to be (often
older than what's actually required).

`ANTHROPIC_API_KEY` is never baked into the image - `.env` is gitignored, so it isn't
even present in what Cloud Build pulls from GitHub. It has to be set as a real Cloud
Run environment variable (bound to a Secret Manager secret) when the service is
configured - `load_dotenv()` in `agent.py` already no-ops harmlessly if no `.env`
file exists and reads straight from the real environment either way, so no code
changes needed for this to work once that binding exists.

## Observability (`agent/observability.py`, `agent/log_summary.py`)

Phase 3. Every `run_query` call previously printed to the console and then vanished -
no durable record of what got asked, what it cost, which tools fired, or whether
anything errored once the process exited. `log_run` appends one JSON line per call to
a local, gitignored `logs/agent_runs.jsonl`: timestamp, the question (truncated to 300
chars), outcome (`ok`/`error`), latency, `num_turns`, `cost_usd`, `grounding_retries`,
which league_id(s) got touched, what format tier `check_league_format` found (if
called), and any tool-level errors.

**Plain JSONL over SQLite, deliberately** - no schema, no migrations, and still
trivially summarizable (`log_summary.py`, a ~30-line script reading the same
`read_runs()` helper) at the scale this project actually runs at. Converts to SQLite
in one `sqlite3` import later if real query power is ever needed - not a dead end,
just not built before there's a reason to.

**The file-only version was silently broken for the hosted case** - found by
re-reviewing the deployment plan rather than by anything failing locally. Cloud Run's
container filesystem is in-memory tmpfs: appending to `logs/agent_runs.jsonl` there
would quietly consume the memory limit and then lose the entire file when the instance
scales to zero. Correct locally, useless hosted. Now every record goes to **stdout**
always (Cloud Run pipes stdout straight into Cloud Logging - durable and queryable for
free, no client library or extra service), with the local file written only when not on
Cloud Run. The environment identifies itself via `K_SERVICE`, which Cloud Run always
sets, so there's no deploy-time config to remember to flip. A file-write failure logs
to stderr instead of taking the whole record down with it - stdout has already
succeeded by that point.

**Logging lives in a single `try/finally` around all of `run_query`, not scattered
print-style calls** - it fires exactly once per call whether the call succeeds,
partially succeeds (a tool errored but the model recovered gracefully), or an
exception reaches all the way out, since all three are real outcomes worth a record.
Variables the `finally` block reads (`total_turns`, `retries`, `result`,
`all_tool_calls`, `all_tool_results`) are all initialized before the `try`, not just
inside it - otherwise an exception on the very first `_run_turn` call would hit an
`UnboundLocalError` in the logging code itself, on top of whatever the original
exception was.

**Format tier and tool errors both required capturing the SDK's tool-*result*
messages, not just tool *calls*** (`ToolResultBlock` inside `UserMessage`, matched
back to a tool name via the `ToolUseBlock.id` recorded when the call was made) - the
existing code only ever looked at what the model *asked for*, never what a tool
actually *returned*. This is also what makes the malformed-league_id eval case
possible: `ToolResultBlock.is_error` is the real, structured signal for "this call
failed," confirmed against a real 404 (see "Format support gate" for the live-tested
shape: FastMCP turns a raw `HTTPError` into `isError=True` on the client side, not a
crash).

**A real, live-found gap while testing this**: with a nonexistent league_id, the
model called `check_league_format` (which errored), and then called `get_team_state`
anyway against the same broken league_id (which also errored) before finally
explaining the league didn't exist - a real if minor wasted call, since rules 1-3 only
ever covered the *tier-based* branches ("unsupported"/"degraded"), never a hard tool
*error*. Fixed with rule 10 ("if check_league_format itself errors, stop for that
league_id - don't retry with a different tool"), confirmed live: identical question
afterward made exactly one tool call. Like rule 2, this isn't perfectly reliable
either - see "Eval harness" below.

## Eval harness (`evals.py`)

Deliberately 7 cases, not the 10-20 the original plan called for - each real case is a
real paid API call against a small starting budget, and these cover the distinct
failure modes actually found or worth guarding against so far: correct tool selection
for a team-window question, the non-dynasty refusal, a trade-target question using the
real tool instead of improvising, resistance to an explicit instruction-override/
tool-boundary-probing attempt, refusing an off-topic request that needs no tool at all
to answer, only naming players actually present in a team's real offer list, and a
nonexistent league_id failing gracefully instead of crashing or wasting extra calls.
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

**That false-positive turned out to be common, not rare, in practice - it fired on
nearly every real "what should I do" question**, because answering that question
naturally involves describing the team's *current* roster (cornerstones, starting RB
room, etc.), which necessarily mentions plenty of non-offerable players. `_trade_
violations` narrows the check to only fire when a banned name appears on the same
*line* as trade-action language (send/offer/trade/sell/give up/package/dangle/swap),
not anywhere in the whole response - live-tested on the exact question that had been
tripping it: flagged names dropped from 5 to 1 on an identical re-run, and the eval
suite still passed with the real-violation case still triggering a genuine retry (not
silently defeated by the narrower trigger). The remaining miss on that live test
("...without giving up Lamar Jackson") is a **negation** case a keyword-proximity
check structurally can't catch - the line explicitly says the player *isn't* being
given up, and telling that apart from a real "give up X" recommendation needs actual
phrase-level parsing, not just a better keyword list. Deliberately not chased further:
a much bigger jump in complexity for an already-narrow, further-shrinking edge case,
and a false-positive retry still never lets a real violation through - it just costs
one extra small call.

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

**The eval's own assertion went stale the moment the narrower `_trade_violations`
check shipped, and this needed the exact same discipline to catch as a real agent
bug would.** After narrowing rule 6's trigger (above), `case_grounded_trade_chips`
started failing again - but investigating the actual failing text (not just trusting
the FAIL line) showed the model saying *"the system isn't flagging Jonathan Taylor as
tradeable"* - correctly explaining why he ISN'T a real option, not recommending
trading him. The eval's original assertion (`name not in result["text"]`, a blunt
whole-text check) was written under the old zero-tolerance philosophy and never
updated when that philosophy deliberately changed to "only a trade-context mention
counts." Fixed by rewriting the assertion to call the exact same `_trade_violations`
function production uses, so the eval checks the real invariant ("never recommended")
instead of a stricter proxy ("never mentioned") that the intentional design change
had already made obsolete. The lesson: a failing eval isn't automatically evidence of
an agent regression - reading the actual failing output before concluding either way
is what told these two apart.

**Malformed league_id case, added alongside the observability work above**: the
first live run of `case_malformed_league_graceful` surfaced the redundant-tool-call
gap that became rule 10 (see "Observability"), and after that fix the eval passed
immediately. It then failed once on a later full-suite run with the exact same
pre-rule-10 symptom (an extra `get_team_state` call) - re-running the isolated case
3x immediately after came back clean 3/3, the same low-frequency-noise signature
already seen with rule 2. Logged as another data point that even a rule with a real,
confirmed fix isn't pushed to 0% failure by a prompt change alone - only the
generate-then-verify pattern used for rule 6 gets an invariant genuinely close to
guaranteed, and building that same machinery for every rule regardless of how rarely
it actually fails would be its own form of the scope creep this project keeps
deliberately avoiding.

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
- **TE-premium leagues get standard-scoring TE values, undervaluing TEs relative to
  their actual worth there.** See "Format detection" above - FantasyCalc's API has no
  TE-premium parameter to feed at all, and a manually-guessed correction multiplier
  isn't a real fix without data to calibrate it against. No path forward until
  FantasyCalc adds format support, or a different value source is found for this
  specific case.
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
- **No conversation memory - the agent is single-turn, and it doesn't look it.**
  `run_query` opens a fresh `ClaudeSDKClient` per call and closes it at the end, so
  nothing carries between requests. Tested live: asked "hey can you help me with my
  team" with no league info, and the model handled it *well* - asked for the league ID
  and owner name, called no tools, cost $0.004. But the follow-up is where it breaks:
  when the user answers, the model has no memory of having asked. A reply that
  restates everything ("league 1315386978904084480, I'm dezdroppedit27") works by
  accident because it stands alone; a natural reply ("sure, it's 1315386978904084480",
  or just "dezdroppedit27") lands on a model with zero context. It *presents* as
  conversational and cannot hold a conversation, which is the worst combination for a
  demo visitor, who will treat it like a chatbot.

  Two practical consequences beyond the UX: every clarification round-trip is a real
  paid API call, and it counts against `budget.py`'s daily request ceiling - a user who
  takes three messages to supply a league ID burns three of the 50.

  **Not a quick fix, for a reason already documented above** (see the prompt-caching
  correction): naively reusing one session across requests would recover cache and give
  continuity, but on a public endpoint one user's conversation would leak into
  another's. It needs a session identifier from the client plus per-session server-side
  state - not a shared client. With `max-instances=1` an in-memory dict would actually
  be correct, but it dies on instance recycle, and accumulated history grows the prefix
  (and so the per-turn cost) with every exchange. This is the same "statefulness"
  question the Phase 5 plan flagged as genuinely unresolved; this is the concrete
  version of it.

  **Sequencing decision: solve this after there's a UI**, not before. The right shape
  of session handling depends on what the client actually is, and guessing at it now
  risks building the wrong thing twice.
- **Future analyst agent: real statistical projections, social sentiment, and
  sportsbook data.** Not scoped or started - a bigger idea than a single heuristic,
  bundling several distinct new capabilities, each with its own data-source question
  not yet checked:
  - **Statistical projections** - actual weekly/season point forecasting, not just
    dynasty trade value. `nflreadpy` (already in the stack for contracts/usage roles)
    also exposes play-by-play and weekly stats, so the raw data likely doesn't need a
    new source - the modeling approach does.
  - **Social sentiment (e.g. Twitter/X)** - beat-reporter injury news, snap-count
    hints, hype/sentiment as a leading signal ahead of official stats. Real, unchecked
    constraint: X's API has gotten materially more restrictive and expensive since the
    ownership change - free-tier read access is small-volume - so this needs a real
    pricing/access check before assuming it's buildable at all, not just a "figure it
    out later."
  - **Sportsbook data** - Vegas win totals, player props, over/unders as a signal for
    team strength or usage projections. Likely more viable than X: several odds APIs
    (e.g. The Odds API) have a usable free tier, but not yet verified against this
    project's actual needs (coverage, rate limits, real cost past free tier).
  Same discipline as everywhere else in this project applies before building any of
  these: check what the data source actually costs and covers first, don't assume,
  and don't guess at a modeling approach without real data to validate it against.
