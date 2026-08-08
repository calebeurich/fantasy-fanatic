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
- **Minimum trade relevance**: a target still has to clear *half* of `roster_needs`'
  replacement-level threshold for its position - full replacement level means "startable
  quality" (the bar for whether a team *has* a need), but a target doesn't need to be
  startable to be worth a real trade conversation, just not worthless. Without this
  floor at all, a near-zero-value washed-up veteran showed up as a "critical need" buy
  target in testing (technically declining-bucket, but useless to anyone) - the fix
  needed a real floor, just a much lower one than full replacement level.
- Results are always sorted with trade activity first, value second - a bigger name from
  an owner who never trades is a worse real-world target than a smaller one from an
  active trader.
