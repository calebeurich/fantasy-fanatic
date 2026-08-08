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

## Positional needs (`roster_needs.py`)

"Usable" is relative to the league's own format, not a hardcoded value cutoff:
replacement level at a position = the value of the Nth-best player at that position
**leaguewide**, where N = how many dedicated starting slots the whole league has there
(`roster_positions.count(pos)`, plus superflex counted as an extra QB slot). A team with
fewer usable players than its own starting requirement at a position is `critical`;
exactly enough with no depth cushion is `thin`. Flex slots aren't attributed to any
specific position (approximation, disclosed rather than hidden).

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
- Results are always sorted with trade activity first, value second - a bigger name from
  an owner who never trades is a worse real-world target than a smaller one from an
  active trader.

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

## Known limitations / future work

- **Team window classification ignores actual win/loss record entirely.** A team
  that's mathematically out of playoff contention can't really be "Win-Now" for the
  current season no matter how its age composition reads - record should gate the
  classification, especially as the season progresses (early-season record is small-
  sample noise, late-season record is close to decisive). Matters most for Middling
  teams, which are exactly the ones sitting on the fence between "push" and "sell."
  The data already exists - `roster["settings"]` has `wins`/`losses`/`ties`/`fpts`,
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
- Results are always sorted with trade activity first, value second - a bigger name from
  an owner who never trades is a worse real-world target than a smaller one from an
  active trader.
