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

## Shared league context (`analysis/league.py`)

A consolidation pass after a long run of individually-small fixes, which is exactly the
situation CLAUDE.md's anti-bloat rule describes: when patches for related issues start
duplicating the same concept, stop and unify before continuing.

What had accumulated: the same four-line setup - fetch league, describe format, derive
`num_qbs`, load the player pool - copy-pasted in **five** modules (`roster_detail`,
`roster_needs`, `team_state`, `team_values`, `waiver_wire`), plus an eight-line preamble
in `find_targets` that fetched the league twice. `roster_needs._league_setup` already was
this function; being private, everyone else re-derived it. That had already cost real
effort - adding `redraft_value` to the player pool meant hunting down every copy.

`context(league_id)` returns one `LeagueContext` carrying all of it, TTL-cached like
everything else. This is a **maintainability** fix, not a performance one: `sources/cache.py`
had already made the repetition nearly free. The win is that there's now one place to add
a field, and one place that names which of several similar-looking concepts is which:

- `needs_slots` folds SUPER_FLEX into an extra QB - right for "how many of this position
  must I own", which is what replacement level and needs ask.
- `lineup_dedicated` + `lineup_flex` model the real lineup, where SUPER_FLEX takes any
  position - right for "who actually starts".
- `start_thresholds` (redraft) answers "can this player start"; `trade_thresholds`
  (dynasty) answers "is this a real trade chip". Conflating those two made a team with
  three startable WRs read as critically short.

Those pairs existed before and were distinguishable only by reading the call site
carefully - which is how the wrong one kept getting used. The refactor removed
`_league_setup` and four now-dead imports; all 44 tests, every module smoke test, and the
MCP protocol test pass unchanged.

## Source caching (`sources/cache.py`)

Every data-source call originally did a fresh HTTP request or nflverse download, and a
single agent question makes 2-4 tool calls that each re-derive the same league state -
so the same FantasyCalc values and nflverse datasets were pulled several times to answer
one question. Measured on a real league: `classify_league` took 6.85s cold and 0.00s
warm, with identical results, and a `find_targets` call immediately after went from ~7s
to instant.

~20 lines rather than a caching library: this needs a TTL and nothing else.

**The TTL split is a correctness decision, not just performance.** Roster data changes
the moment someone trades or claims a player, and serving a stale roster means giving
confidently wrong advice - strictly worse than being slow. So:

| Data | TTL | Why |
|---|---|---|
| Rosters, transactions, traded picks | 60s | Long enough to collapse one question's tool calls into a single fetch, short enough that a stale roster can't outlive the question |
| League config (settings, users) | 10m | Can change mid-season; effectively never does |
| FantasyCalc values | 1h | Recomputed periodically, not continuously |
| nflverse reference data (contracts, id crosswalk, usage roles) | 6h | Updates weekly at most, and is the slowest pull here |

Fixed a real bug found while adding this: `contracts.py` called `nfl.load_contracts()`
twice in one expression, downloading the entire contracts dataset a second time on
every call.

**Scope limit worth knowing:** the cache lives in the MCP server subprocess. Before
per-session clients existed, that subprocess was spawned fresh per question, so the
benefit was confined to *within* a question. Persistent sessions (below) keep it warm
across questions too.

## Format detection (`sleeper.describe_format`)

- Dynasty vs redraft/keeper comes from Sleeper's own `settings.type` flag (2 = dynasty).
- Superflex = `SUPER_FLEX` present in `roster_positions`.
- TE premium maps to one of FantasyCalc's three bands (`sleeper.tep_tier`).
- `is_dynasty`/`num_qbs`/`num_teams`/`ppr` feed FantasyCalc's `values/current` API params
  directly, so PPR-vs-standard scoring and dynasty-vs-redraft pricing come from their
  value model at the source rather than anything approximated here.

### What FantasyCalc's format parameters actually do (probed, not assumed)

**`numQbs` has exactly two settings.** `numQbs=1`, and `numQbs>=2` which returns byte-
identical data for 2, 3, and 0. There is no separate superflex market - superflex and 2QB
are one market, which matches how the format actually plays: starting two QBs is generally
the best projection when you can, so the second QB is near-mandatory in practice without
being mandatory in the rules.

**And that market is four per-position scalars, not a re-derivation.** Comparing every
player present in both pulls (excluding sub-500 values, where integer rounding dominates):

| position | 1QB -> SF/2QB ratio | spread across the position |
|---|---|---|
| QB | **1.883** | 1.8819 - 1.8845 |
| RB | 0.923 | 0.9221 - 0.9237 |
| WR | 1.007 | 1.0059 - 1.0078 |
| TE | 1.101 | 1.1000 - 1.1015 |
| PICK | ~1.066 | 1.026 - 1.148 (genuinely varies) |

Josh Allen and the QB40 move by the same 1.883. Two consequences worth holding onto:
*within*-position comparisons are format-independent (the scalar cancels, so
`find_efficiency_swaps` comparing two QBs is unaffected), while *cross*-position
comparisons rest entirely on those four numbers. **Not modelled by the source: superflex
QB scarcity is not steepened.** The real cliff in a superflex league is around QB12-QB13,
where the last startable QB2s go - a flat scalar cannot express that, so the marginal QB2
is probably worth more than these values say. Picks are the one thing that genuinely
varies, which makes sense: a pick's worth depends on the player pool it converts into.

**`ppr` is a flat per-position scalar too, and a nearly-invisible one.** Measured 0 PPR
-> 1.0 PPR: RB x0.9943, WR x1.0180, TE x1.0232, QB x1.0114, each constant across its whole
position to four decimals. The single largest scoring setting in fantasy football moves RB
values by 0.6%.

Worse, it cannot distinguish a receiving back from an early-down back, which is precisely
what full PPR most changes:

| player | 0 PPR | 1.0 PPR | ratio |
|---|---|---|---|
| Christian McCaffrey | 4,462 | 4,437 | **x0.9944** |
| Derrick Henry | 2,995 | 2,978 | **x0.9943** |

A pure receiving back and a pure between-the-tackles back move identically. In a real
full-PPR league McCaffrey's edge over Henry is far larger than in standard scoring, and
none of that is in these numbers. Nothing to fix at this layer: there's no per-player PPR
data here to apply, and inventing a multiplier off `player_roles.pass_catching_rb` would be
a guessed heuristic with nothing to calibrate against - unlike TEP, where FantasyCalc's own
UI supplied the calibration. Recorded so the `ppr` passthrough isn't mistaken for format
precision it doesn't have.

**TE premium is applied by FantasyCalc in the browser, not on the server.** Their site
only ever requests `tep=none`; the API 404s on every other `tep` value. Selecting TEP+ or
TEP++ on their page fires no network request and rescales the TE column client-side, by a
flat multiplier - identical to four decimals across every TE, and unchanged between 10-
and 12-team settings. So `fantasycalc.TEP_MULTIPLIER` replicates it: **TEP+ x1.1490,
TEP++ x1.2900**, TEs only. Bands are theirs, verbatim from their control labels: Off
(<=0.25), TEP+ (0.5 to 1.0), TEP++ (start 2 TE or >1.0).

**This corrects a wrong conclusion previously recorded here**, which said TE premium was
unfixable because the endpoint had no parameter for it and there was "no real data to
calibrate a manual multiplier against." Both halves were wrong, and the reason is worth
keeping: that conclusion came from reading FantasyCalc's *documented parameter list*
instead of watching what their site actually sends. The calibration data was sitting in
their own UI the whole time. Checking the documentation and stopping was the mistake.

It matters here rather than being academic - **both real leagues in this project score
`bonus_rec_te = 0.5`**, which is TEP+, so every TE was ~15% undervalued. Being a flat
scalar, it changes no TE-vs-TE comparison (ranks, thresholds, and needs are identical);
what it fixes is TE-vs-everything-else, which is exactly what trade valuation runs on.

*Caveat*: this replicates a client-side transform, not an API contract. If FantasyCalc
retunes it we drift silently, so `python -m sources.fantasycalc` prints the multipliers
in use next to the resulting TE values for a one-command check against their site.

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

## Backlog: this document is invisible to the agent

**The stated end goal is a chatbot that explains its recommendations, and the reasoning it
would explain from lives in a file it cannot read.** Reasoning currently reaches the
assistant through exactly two surfaces:

1. **Note strings inside tool output** - `window_note`, `depth_note`, `value_upgrade_note`,
   `why_they_might_listen`. These work well and are why so much prose is embedded next to
   the data rather than kept here. They only carry reasoning attached to a specific field.
2. **MCP tool descriptions** (`agent/mcp_server.py`) - always in context, so they carry the
   "how to read this block" instructions.

LOGIC.md itself - every *why* behind every constant, every rejected alternative, every live
case that forced a change - reaches the agent through neither. A concrete miss: asked about
a Push team 76% of the best lineup against a leader at 100% who is *also* rising, the right
read is "you cannot out-wait him, so push harder or don't push at all." Every number needed
is in `get_team_state`'s league rows. Nothing tells the agent to make the comparison, and
`window_note` describes a team in isolation.

Three steps, cheapest first, none built:

- **A comparative field on the team row** - the gap to the league's best lineup and whether
  that team is rising - so the read above becomes data with a note attached rather than an
  inference the agent may or may not make. Same pattern as everything else here.
- **Doctrine in the system prompt** *(done - `agent/agent.py`)*. Five principles that are not
  about any single field: pick an end of the spectrum, don't confuse the two currencies, age
  is a distance, value is not additive, and a trade needs a plausible counterparty. These
  are the ideas the tools encode and no single tool result states.
- **LOGIC.md as an MCP resource** the agent can query - "why is this a Push team", "why isn't
  contract length in the age curve". This is the version where the assistant wields the whole
  picture instead of a summary, and where the document and the agent cannot drift apart.
  Needs chunking that survives this file's length, which is the actual design work.

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
mobile QBs lean on athleticism (decline pulls forward), that a *good* pocket passer
trades on arm talent and processing (decline pushes back a long way), and that
pass-catching RBs age like WRs. Tags come from real season data (nflreadpy), not a guess:
- `rushing_qb`: carries/game >= 5.0 and *not* an elite passer -> 26/32
- `dual_threat_qb`: carries/game >= 5.0 *and* an elite passer -> 26/34 (the default; the
  point is the **absence** of a discount, not a bonus)
- `pocket_passer`: elite passer, not a runner -> 26/38
- `pass_catching_rb`: targets/game >= 4.0 -> 24/29 instead of 24/27

"Elite passer" is the top third of passing EPA per game over three seasons
(`ELITE_PASSER_PERCENTILE`), a measured tier rather than a reputation one. Carry and
target thresholds were picked from the natural gap in the real distribution (Lamar
Jackson/Josh Allen/Jalen Hurts clear 5 carries/game while pure pocket passers don't crack
3) - not arbitrary round numbers. Full reasoning for the three-way QB split, including
why the pocket end stops at 38 rather than 40, is in `team_values.AGE_CURVE_OVERRIDES`.

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
  **Both conditions, not just years**: 461 of 1,695 active contracts guarantee $0, 44 of
  them with 2+ years remaining, and at ~$1M APY those are veteran-minimum deals carrying
  no commitment at all. Testing years alone printed "contract-secure: J.K. Dobbins
  (2yr/$0.0M gtd)", which refutes itself on the same line. Guaranteed money is the part
  that binds, so the flag requires `guaranteed > 0` as well.
  *Known limitation*: `> 0` is a categorical line (is the team committed at all), not a
  calibrated one - $0.3M guaranteed clears it. A dollar threshold would need calibration
  this project doesn't have, and the categorical version fixes the contradiction.

  Note this flag is **display-only** by design and feeds no downstream math. See the age
  curve above: the market's own dynasty/redraft split already prices "how long does he
  have left" continuously, while a contract encodes what a team believed at signing,
  possibly years stale. Kittle (3yr/$35M) and Mark Andrews (2yr/$20.9M) trip this flag
  identically while the market prices them nothing alike.

## Team windows: two measured axes (`team_state.py`)

A team's window comes from two things that are actually measured, not from age alone.

**Axis 1 - contention: can this team compete *this season*?** The total **redraft** value
of its projected starting lineup, ranked against the league.

**Axis 2 - trajectory: where does the roster go on its own?** Ascending minus declining
share of that same current production.

Both are cut into league tertiles (`team_values.tertile`), so neither axis carries a
constant tuned to one league. The raw numbers - rank, % of the league's best lineup,
ascending/declining shares - ship in `window_note` alongside the label.

### Why this replaced the age-only model

The predecessor read Win-Now / Middling / Rebuilding purely off the age split, then bolted
on two overrides to fix the answers age got wrong: `is_thin` ("bottom-third starter value
with barely any cornerstones") and `is_loaded` ("top-third but reads Rebuilding"). Both
were crude proxies for a missing contention axis, and measuring contention properly
subsumes them exactly - **two special cases deleted, one honest axis added.**

And the contention proxy those overrides used was the recurring bug in this project one
more time: it ranked the league by **dynasty** starter value, which prices future seasons
that score no points now. Swapping to redraft moved teams four and five places on two real
leagues:

| team | dynasty rank | production rank | reads |
|---|---|---|---|
| dezdroppedit27 | 8th of 12 | **4th** (80% of the best lineup) | old, but genuinely close |
| bergenjay | 2nd of 12 | **6th** (75%) | price is mostly potential |
| tchoezin (league 2) | 4th of 12 | **9th** (53%) | 87% of production is ascending |
| mgibbons612 (league 2) | 10th of 12 | **6th** (62%) | 40% declining, better than it looked |

The first row is the case that motivated the change - a manager who described his own team
as *"close, not just old and bad"* was being told he was mid-pack by a metric that
discounted him for being old.

**Trajectory is measured on production, not dynasty value**, for a related reason. Dynasty
value would double-count the very effect being measured: ascending players are *priced* on
the growth in question, so weighting by it inflates every young roster's ascending share
and reports the market's opinion back as though it were a roster fact.

### Three core states, with flavors

**The v1 core is three states: contending, middling, rebuilding.** In dynasty a team is
winning now or rebuilding, and the honest answer for anyone in between is "both directions
are open, and waiting on how the season starts is allowed." Everything below is a *flavor*
of one of those three - a different reason to be in the state, which changes what the team
is told without changing what it is. Flavors are deliberately notes rather than labels: a
fourth and fifth label would make the model harder to hold in your head, which is the one
thing this project won't trade away.

| core | flavor | separated by | how it ships |
|---|---|---|---|
| **Contending** | `Push` - there is a clock | trajectory falling | its own label |
| | `Contend` - no clock | steady or rising | its own label |
| **Middling** | patience is free | rising | `MIDDLING_TIMING_NOTE_RISING` |
| | patience buys information | steady or falling | `MIDDLING_TIMING_NOTE` |
| **Rebuilding** | nothing left to sell | `dec_pct == 0` | `REBUILD_NOTHING_DECLINING` |
| | *working vs stalled* | — | **not built** |

Contending is the one core state whose flavors are separate labels, because the two
genuinely want opposite actions - `Push` buys production and spends picks, `Contend` does
nothing at a premium. Middling and Rebuilding flavors want the *same* actions for different
reasons, so they change the wording and nothing else.

**The unbuilt one, recorded rather than guessed at.** A rebuild that is working and one that
is stalled currently read identically. In XFL 2, BartolosHeroes (40% ascending, 3% declining)
gets the same note as spugz13 (9% / 12%) - the first is young and getting better on its own,
the second is going nowhere in either direction, and the difference is the whole question for
a rebuilder. The existing `REBUILD_NOTHING_DECLINING` flavor doesn't cover it: that one asks
"do you have inventory to sell", which is a different question and correctly leaves
BartolosHeroes on the generic note, since he really does have four sell candidates.

### The four windows

- **`Push`** - contender whose roster is falling. The window is open and closing on its
  own, so waiting costs value. Buy production, spend picks. Pivoting stays *available*, it
  just returns poorly: the production making the team competitive is priced on
  already-realized value, so selling it converts a lot of what wins games into
  comparatively little dynasty value. Being decent now is itself the argument against
  tearing down.
- **`Contend`** - contender that is steady or rising. Good now with no clock, so nothing
  needs buying at a premium and nothing needs selling.
- **`Middling`** - middle third of the league, *either trajectory*. Both paths are shown
  with the cost difference stated, and waiting to see how the season actually starts is
  treated as a legitimate choice rather than an unmade decision. Trajectory sets the
  **note**, not the window:
  - *rising* (`MIDDLING_TIMING_NOTE_RISING`) - this roster's own ascending players supply
    next season's production for free, so pushing now pays a market premium for one extra
    year of contention. **Waiting is the cheaper default.**
  - *steady or falling* (`MIDDLING_TIMING_NOTE`) - nothing arrives for free, so waiting
    does not lower the price of contending; what it buys is **information**, and a few weeks
    of real results settle whether this team is closer than the standings say. Pivot if the
    season opens badly, while the aging production still prices well.

  Either way: push when the price is below market, or when a need is *count*-shaped - an
  empty starting slot costs points every week and no amount of patience fills it.
- **`Rebuild`** - bottom third in current production. Sell what's declining, accumulate
  youth and picks.

**`Rebuild` stopped being the else branch**, which is what this window was renamed for.
Previously only `fringe` **and** `rising` reached the both-paths window, so a middle-of-the-
league team that merely wasn't rising fell through to `Rebuild` and was told to sell. In XFL
2 the team that hit it was **3rd of 12 in total dynasty value** by this project's own
`team_values`, 5th overall on an outside dynasty site, average age 26.0, holding the best QB
room in the league - and `team_state` called it "not in contention and not rising fast enough
to change that." The owner's framing is the rule now: *in dynasty you are either winning now
or rebuilding, and if you are in the middle you should see the options in both directions and
be free to wait on how the season starts.*

Renamed from `Ascend` rather than kept, because routing falling teams into a window called
*Ascend* is a label asserting something the data denies - the failure mode this document
records over and over. The name "Middling" is not new; it is what the pre-two-axis model
called this tier, and comments in `trade_targets.py` never stopped using it.

Downstream routing follows the window rather than a parallel vocabulary: `prefer_production`
and `find_efficiency_swaps` are **Push-only** (converting future premium into capital only
makes sense when the future you're selling is further out than your window), sellers are
`Rebuild` teams, and `WINDOW_TO_PICK_TIER` prices picks by where the originating team
finishes.

### Owning your own next 1st is a constraint on the pivot, not a fourth tier

Tanking only pays if you hold the pick your bad season earns. Without it, a losing season
buys nothing, so the case for selling has to stand on trade returns alone. That **lowers
the return on pivoting rather than removing the option** - and it doesn't change the
window, since a rebuild is still a rebuild (you acquire young assets by trade instead of by
finishing last). So it ships as a note. Both real leagues have several teams in this spot.

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

**Three analysis bugs found by reading a real answer critically** (all fixed in Python,
so they propagate everywhere rather than needing a prompt rule):

- **A bare `diff` field was an invitation to confabulate.** The tool returned
  `{"diff": -11}` with no units or meaning, and the model reliably invented one -
  describing teams as "below their expected win total", "underperforming by 25 points",
  "13 points below median". None of that exists; it's `ascending% - declining%` of
  starter value, and in the preseason there are no wins to be below. Replaced with
  `age_mix_score`, `ascending_pct`, `declining_pct` and an `age_mix_note` that states
  outright: *"This is a roster age-composition measure only - it says nothing about
  wins, points scored, or performance."* The general lesson: an unlabelled number in a
  tool result will get a meaning attached to it, so the label has to ship with the value.

- **`is_starter` came from Sleeper's live snapshot, which is meaningless in the
  preseason.** In a real superflex league (2 QB slots) the current-week lineup listed
  exactly one QB, so the team's obvious QB2 - C.J. Stroud, ascending, 3,288 value - was
  classed as bench and offered away as spare parts. In superflex a second QB is among
  the most valuable things on a roster, not dead weight.
  `roster_needs.projected_starters` derives the lineup from value and the league's own
  slot counts instead, and `trade_targets` uses that. Precise rather than blunt after
  the fix: Stroud disappears from the offer pool while Sam Darnold, genuinely QB3 in a
  2-QB league, correctly remains.

- **Buy targets ignored window fit.** Targets were sorted by `(trade activity, value)`
  only, which contradicted this project's own pricing model - `BUY_PRICE_NOTE` calls
  declining players "production-priced" and prime ones "upside-priced, may cost more
  than the fit justifies", because prime value bakes in future growth a win-now team
  isn't buying. A real Win-Now team was handed six buy targets, every one of them prime,
  and none of the cheaper production it actually needed. Win-Now buyers now sort
  production-priced first; Rebuilding and Middling buyers keep the old ordering, having
  no reason to prefer aging players.

**Dynasty value vs. current production** (`redraft_value`, `future_premium`,
`find_efficiency_swaps`): FantasyCalc's API takes an `isDynasty` flag, and this project
had only ever asked for `true`. Flipping it returns redraft values - the same market
pricing *this season's production alone*. Free, already available, previously unused.

The gap between the two prices is what a win-now team is overpaying for. Real case:
a superflex roster's QB2 (C.J. Stroud, 3,288 dynasty / 2,744 redraft) and QB3 (Sam
Darnold, 2,735 / 2,704) produce within **1.5%** of each other this season, yet Stroud
costs **553 more** in trade value. Ranking by dynasty value alone cannot see that;
selling Stroud and starting Darnold converts future premium into trade capital at
almost no cost to the current lineup.

**The first implementation of this was wrong and the numbers say exactly how.** It
exposed a `future_premium` = dynasty/redraft ratio and flagged anything above an
absolute threshold. Measured across the 200 players present in both pools: **median
ratio 2.22, p10 0.93, p90 18.1**. The two pools *are* anchored to the same top of scale
(dynasty max 10,380, redraft max 10,452 - so the intuition that both run "out of 10,000"
is right), but dynasty spreads its total across ~400 players against redraft's ~200, and
the ratio's distribution is heavily right-skewed by young players with no current role.

So 1.0 is not neutral - typical is 2.22. A player at 2.01 was being labelled "100%
future potential" while actually sitting *below* median, and the flag fired on
production-oriented veterans like Chuba Hubbard, who is early-prime and should be
roughly production-priced. That is the `diff` mistake repeated: an unlabelled ratio
invites a wrong reading. `future_premium` was removed entirely rather than relabelled;
`redraft_value` stays because a price on a known scale is unambiguous.
`find_efficiency_swaps` compares players **pairwise within a position** instead, where
both values come from the same two scales and the skew cancels.

**Trade value is not linear in raw value** (`value_over_replacement`, `tier`). A real
offer list led with Christian McCaffrey (+1,783 over positional replacement) and then
listed Ollie Gordon (947 raw, but **1,637 below** replacement) as though they were
comparable pieces, which is the kind of suggestion that makes a tool look naive - real
managers discount a below-replacement name heavily, because anyone can get that guy off
waivers, while value above replacement is scarce and hard to ascend to. Offers now carry
their surplus over positional replacement (already computed by `roster_needs`, just
never used here) and sort by it.

The labels deliberately avoid overcorrecting: below-replacement depth is *"real but
discounted, a sweetener not a centerpiece"*, not "worthless". Injuries and byes are real,
and a cheap backup RB can spike overnight (see the handcuff item under "Known
limitations") - the raw number overstates what it fetches in a trade, but it isn't zero.
Thresholds are deliberately strict (≥90% of current production retained, ≥300 dynasty
value freed) because this trades real production for trade capital and shouldn't be
suggested on noise.

Coverage is partial by nature: redraft carries ~200 players against dynasty's ~400,
since deep dynasty-only assets have no redraft market. Those get `redraft_value: None`
and are skipped rather than treated as zero production.

**Lineups are ranked by redraft value, for every window.** The follow-on above turned
out to be simpler than "do this for Win-Now teams": a lineup is *who scores most this
week*, which is exactly what redraft prices measure, while dynasty value governs who you
keep or trade. A rebuilding team still starts its best scorers.

The correction it produced is significant, not cosmetic. rjl22's backfield:

| RB | dynasty | redraft |
|---|---|---|
| Bijan Robinson | 10,255 | 10,004 |
| Ashton Jeanty (rookie) | 7,008 | 6,290 |
| Christian McCaffrey | 4,367 | **6,518** |

Ranked by dynasty value, McCaffrey is RB3 and was being offered away - by current
production he's the second-best back on the roster, and the rookie is who sits. Telling
a win-now team to trade its RB2 was a real, confident, wrong recommendation. The TE
efficiency swap that had been reporting *102% of production retained* also disappeared,
because the lineup now starts the better current player in the first place - fixed at
the source rather than surfaced as a suggested swap.

Missing redraft prices sort last, which is safe rather than lossy: across a real 12-team
league the highest-dynasty rostered player without one was 1,350, far below every
positional replacement level.

**Flex slots are now modelled** (`lineup_slots`, and the flex-filling half of
`projected_starters`). `dedicated_slots` ignores flex - a disclosed approximation that's
fine for "how many of this position must I have", and wrong for building a lineup. The
real league shape is QB 1 / RB 2 / WR 3 / TE 1 / **FLEX 2 / SUPER_FLEX 1**: ten starters,
three of them flexible. Modelling only dedicated slots claimed *eight*, and separately
folded SUPER_FLEX into a second dedicated QB - asserting a QB must fill a slot any
position can.

The consequence was concrete: a team with three excellent backs starts all three (two at
RB, one at FLEX), but the model saw the third as spare parts and offered him. rjl22's
projected lineup now comes out as all of Bijan / McCaffrey / Jeanty, with the superflex
QB2 in SUPER_FLEX - ten players, matching the league's ten slots. Dedicated slots fill
first, then flex most-restrictive-first, so a SUPER_FLEX doesn't take a player only a
narrower FLEX could have used.

*Framing note on near-equal swaps*: when `find_efficiency_swaps` reports 99-102%
production retained, that is explicitly **not** "start A, bench B". Two players that
close trade places week to week on matchups, and keeping both is legitimate depth rather
than redundancy. The note says so - it's a value decision about which one to sell, not a
lineup upgrade.

**Pick equivalents** (`team_values.pick_equivalent`): FantasyCalc prices rookie picks on
the same scale as players, and this project already fetched them for `pick_capital` -
so translating a value into "about a 2028 3rd" is a lookup, not a model. Added because
a raw number is hard to feel: told a bench piece is "worth 947", nobody knows whether
that's an asset; told it's *about a 2028 3rd*, every dynasty manager immediately does.
It also lands on the right intuition for depth - late-pick-shaped, real but not what a
deal is built around. Approximate by nature, since future classes are priced as flat
round averages.

## One definition of "who starts" (`LeagueContext.starters`)

Sleeper's `roster["starters"]` is a snapshot of whatever the current week's lineup happens
to be, which is meaningless in the preseason. This was **known**, documented at length on
`projected_starters`, and fixed - on exactly one code path. Three others kept reading the
snapshot:

| reader | what it fed |
|---|---|
| `team_values.split_starters_bench` | `starter_value` -> the league-wide rank -> `is_thin`/`is_loaded` -> `effective_strategy` |
| `team_state.classify` | the age-mix buckets that set Win-Now / Middling / Rebuilding, and every entry's `is_starter` |
| `roster_detail.build_rows` | the starter/bench label a user reads on screen |

So the single most load-bearing label in the project was derived from data the project
itself documents as unreliable. Measured on the real league the effect is mostly small -
rank order moves by one place - but **not nil**: rjl22's snapshot listed 1 QB and 5 RBs in
a superflex league, and his classification changed once the lineup was derived properly.
BenSimonds had only 8 of 10 slots set at all.

`LeagueContext.starters` (owner_id -> set of player_ids) is now the one answer, computed
once per league. **Nothing reads the snapshot.**

**Ids, not names.** `projected_starters` used to return names, so every consumer was
handed a `projected` set and did string comparison - a `projected` argument threaded
through five functions, plus `league_projected_starters` to build it. With ids,
`_usable_by_position` and `team_state.classify` stamp `is_starter` on entries as they
build them and callers just read the field. That deleted the whole apparatus: the
argument, the helper, and `_my_offer_pool`'s `is_lineup` shim. **Fixing a flag at its
source beat threading the correction through the callers** - the general form of the
recurring bug in this file.

## Duplication removed alongside it

- **`pick_capital` was a second copy of `owned_picks`'s traded-pick resolution**,
  differing only in summing flat round values rather than returning the picks. Two
  implementations of "who owns which future picks" meant two draft-capital numbers could
  disagree, and once `owned_picks` learned to price by the originating team's window, they
  did. `pick_capital` is now a one-line sum over `owned_picks`.
- **Owner lookup by substring existed in six places.** `LeagueContext.roster_for` had been
  added for exactly this and had **zero callers**. Now `roster_for` (rosters) and
  `pick_owner` (already-computed per-team rows, which is what the trade paths hold) share
  one `_match`, so the rule and its error message can't drift.
- **`sleeper.starting_qbs` replaces `NUM_QBS = {True: 2, False: 1}`.** The old form was
  keyed on `is_superflex`, which answers "can a non-QB fill the second slot" - a different
  question from "how many QBs start", and the one that prices the market. A true 2QB
  league (two literal QB slots, no SUPER_FLEX) therefore fetched 1QB values. Practically
  theoretical, since superflex has replaced 2QB in real leagues; kept because it's the
  same amount of code and removes a second place that computed how many QBs a team
  starts. Clamped to 2 for FantasyCalc, which publishes nothing beyond;
  `dedicated_slots` is deliberately unclamped since it counts real roster slots.
- **`replacement_thresholds` no longer defaults its `metric`.** It defaulted to `"value"`
  while `_usable_by_position` defaulted to `"redraft_value"` - two functions that must
  agree, shipping opposite defaults, in the file whose central documented bug is that
  exact conflation. Callers now state which question they're asking.
- **`_usable_by_position` sorted on a different metric than it filtered on** (redraft in,
  dynasty out), so `find_surplus` could call a better current producer spare while keeping
  a pricier prospect.

**Replacement level is a win-now lens**, worth stating because it bounds all of the above:
"is there a startable player here" is only the operative question for a team trying to
field its best lineup this season. A rebuilding team isn't shopping above replacement level
at all - it wants ascending value and picks, which is why `find_targets` routes it to the
pivot path. Read a rebuilder's positional needs as "what a contending version of this
roster would be short of", not a to-do list.

## Positional needs (`roster_needs.py`)

**Needs are measured on current production, trade relevance on dynasty value** - the same
`replacement_thresholds` function, called with a different `metric`, because they answer
different questions. "Can I field a lineup?" is about production now; "is this a real
trade chip?" is about what it fetches.

Using dynasty value for the first was badly wrong. It asks whether a player beats the
36th-most-*valuable* WR - a pool full of young prospects priced on upside - rather than
the 36th-best current *producer*. Measured on a real league the bar was **2.5x too strict
at WR (2,126 vs 855) and 3.2x at TE (2,013 vs 630)**, so a team with three startable WRs
and two startable TEs was reported as *critical at both*, and the buy path went shopping
for positions it didn't need. After the fix that team shows a single "WR: thin" and
nothing critical, which matches how its own manager reads the roster.

This is the third instance of one root cause: dynasty value was being used for questions
about the current season. The others were lineup ranking and the win-now efficiency
comparison. Worth stating as a rule - **if the question is "this season", the metric is
`redraft_value`; if it's "what is this worth", the metric is `value`.**

"Usable" is relative to the league's own format, not a hardcoded value cutoff:
replacement level at a position = the value of the Nth-best player at that position
**leaguewide**, where N = how many dedicated starting slots the whole league has there
(`roster_positions.count(pos)`, plus superflex counted as an extra QB slot). Flex slots
aren't attributed to any specific position (approximation, disclosed rather than hidden).

### Count vs quality: why "thin" was replaced (`assess_positions`)

The rule above was originally the *whole* rule: fewer usable players than starting slots
= `critical`, exactly enough = `thin`, more = no need. That is purely a **count** of
bodies clearing a floor, and measured against a real 12-team superflex league it was
close to **inverted**:

| team | WR room (starting production) | rank | old label | new label |
|---|---|---|---|---|
| bergenjay | 13,116 (Nacua + Nabers) | **2nd of 12** | `critical` | `top-heavy` |
| rjl22 | 10,081 | 7th | `thin` | *(not a need)* |
| bigbuttboi | 6,322 (four bodies just over the bar) | **9th** | *(no need)* | `weak` |
| BenSimonds | 2,251 | 12th | `critical` | `critical` |

The second-best WR room in the league read `critical` because its WR3 sat below the bar;
the 9th-best read as no need at all because four players cleared a low bar (794) by a
little. Replacement level answers *"can this player start"* - a floor. Applied to *"is
this group good"* it passes teams that are merely numerous and fails teams that are
merely top-heavy.

Worse, it pointed at the wrong position entirely. rjl22 was told "thin at WR", where he
ranked an unremarkable 7th of 12, while his genuinely bad positions were invisible: **QB
9th of 12 in a superflex league**, and TE 8th at 39% of the league median. Both read as
fine, because he owned enough warm bodies at each.

So a position now carries both readings, and the level names the **shape** of the problem
rather than its severity alone - because the shapes have opposite fixes:

- `critical` - can't field the slots *and* the group is weak. Needs bodies and quality.
- `top-heavy` - can't field the slots, but what's there is good. Wants a **body**; the
  stars are already in place. (bergenjay: Nacua and Nabers don't need upgrading.)
- `weak` - can field the slots, but the group is bottom-tertile or below
  `WEAK_VS_MEDIAN` of the league median. Wants an **upgrade**, not depth - this is the
  consolidation case, and it had no representation at all before.
- `ok` - neither. Notably includes "mid-league with no star", which is *not* a need.
  Calling that "thin" sent teams shopping for problems they didn't have.

**Why a median test as well as a rank test.** Positional distributions are skewed, TE
especially, so rank alone hides real gaps: rjl22's TE room ranked a middling 8th of 12
while sitting at 648 against a league median of 1,667 - 10% of the best room in the
league. Half the league's median production from a position means giving up roughly a
full starter's worth of scoring against a typical opponent every week, which is a need
wherever it happens to sort. A team is weak if *either* test fires.

**Quality isn't asserted below `MIN_TEAMS_FOR_QUALITY` (4).** In a 1-team league every
rank is simultaneously first and last, which the naive tertile test read as bottom-tertile
- i.e. every position weak, from no evidence. Below the cutoff the count test stands alone
and a shortage falls back to `critical`, the old conservative label. Same reasoning as
`format_support`'s degraded tier, applied one level down.

**Downstream, the shape decides the fix**, or the split would be cosmetic:
- The buy path applies an **upgrade bar** to `weak` positions only - a target must beat
  the current worst starter there, since anyone who wouldn't displace him is not a fix,
  however cheap. Count-shaped needs have an empty slot, so any relevant body helps.
- Waiver claims only fill `critical`/`top-heavy`. The best name on waivers is by
  definition not an upgrade over a startable player, so claiming one at a `weak`
  position is churn.
- Efficiency swaps are suppressed at any need position. Selling a starter to promote his
  backup raises capital you'd have to spend straight back on the same position - and the
  swap injection adds that player to `my_offers`, re-introducing the very position the
  offer pool excludes for being a need.

There is deliberately **no single-roster `find_needs`** any more. Quality is measured
against the rest of the league, so a per-roster entry point would have had to either take
the league as an argument anyway or quietly degrade to a 1-of-1 ranking. A function that
silently answers a different question than the one asked is how the count-only rule
survived this long.

**Surplus - the mirror of need** (`find_surplus`/`league_surplus`): a position where a
team has *more* usable players than its starting slots require, and specifically which
players are the spare ones (everyone beyond the top `slots[pos]`, by value, **minus
anyone who actually starts**). Added
alongside `find_needs` as a shared refactor (`_usable_by_position` now does the one
"which players clear replacement level here" walk both functions read from, and
`_league_setup` collapses what had been three separate copies of the same league/
format/threshold fetch across `league_thresholds`/`league_needs` into one) - a second
copy of that setup was about to be added for `league_surplus` anyway, and CLAUDE.md's
rule against letting a concept re-diverge across a file applies just as much to
boilerplate as it does to business logic.

The `projected` exclusion was a real bug, not a precaution. `slots` here is `needs_slots`,
which folds SUPER_FLEX into a QB and **ignores FLEX entirely** - so in a league running
RB 2 / FLEX 2, a team's third RB falls outside `slots["RB"]` and was offered as spare
depth while starting every week. Live case: rjl22's RB3 (Ashton Jeanty, a genuine asset)
was offered in a mutual swap for a fringe backup QB on exactly that basis. This is the
same snapshot-vs-projected distinction `_my_offer_pool` already respected - the fix had
been applied on one path and not the other, which is the recurring failure mode in this
file.

This uses the same replacement-level threshold as the need assessment - a **stricter**, single
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
- **"Is he a starter" was a proxy, and it hid the biggest chip on the board.** The rule
  excluded every starter, which is right for the case it was written for (a team told to
  offer its own starting QB2) and wrong for the question it was standing in for: *what does
  moving him actually cost*. The live example is a `Push` team whose two largest trade
  pieces were an ascending TE (3,660 dynasty against 1,035 redraft) and a backup QB. Its
  owner named the TE first when asked what he'd move. The tool could not list him at all -
  while a bench TE covers most of the production and the rest is future value, which is
  precisely what a closing window exists to spend.

  A starter is now offerable when either: the bench **covers him for free**
  (`roster_needs.production_lost_without` returns 0 - he's in the lineup only because
  somebody has to be), or he is **ascending and the team is `Push`**. Declining and prime
  starters stay protected in every window; they *are* the current production a pushing team
  is trying to keep. On the live roster this correctly offers the TE at 1,035-against-3,660
  and never the best WR at 3,961-against-3,773.

  The free-cover test alone would still have missed him: replacing him costs 230 of current
  production, which is real. So the cost is **reported** (`lineup_cost`) rather than used as
  a veto - whether it's worth paying depends on the return, which this module deliberately
  doesn't price. The efficiency-swap writeback attaches the same field, so both routes into
  `my_offers` answer the question in the same units.

  `production_lost_without` is `_injury_drop`'s guts, extracted rather than reimplemented.
  "How bad is it if I lose him" and "can I afford to trade him" are the same computation
  asked in opposite directions, and two copies would eventually have disagreed about the
  same player on the same roster.
- **Never offer a position you yourself have a need at.** The offer pool didn't check
  this originally - a real Win-Now team with a critical WR need was being told to offer
  away its own WRs, which only moves the shortage around rather than fixing it. Applies
  to both "thin" and "critical" needs, not just critical - a thin position is already at
  the bare minimum, trading from it just makes it critical.
- **Sell candidates split by urgency, not lumped into one list.** A player inside
  `MIN_MEANINGFUL_RUNWAY` of his decline cutoff only loses value from here - real urgency
  to move it. A player below the cornerstone bar with years still on him is often still
  genuinely good (e.g. a real starting-caliber WR who just doesn't crack an unusually deep
  corps) - not losing value on a clock, so it's a situational, take-a-fair-offer piece, not
  an urgent sell. Presenting both the same way overstated how clear-cut the second group is.
  **Split on runway, not on `bucket`.** Splitting on `bucket == "declining"` put Justin
  Jefferson - 6,828, the most valuable asset on a rebuilding roster, 1.8 years from his
  cutoff - under "take a fair offer, no urgency", while a 2,145 back at -2.2 years read as
  urgent. That is the fourth site of the same defect `MIN_MEANINGFUL_RUNWAY` was introduced
  to fix (see the age curve above for the first three); the constant was already imported
  into `trade_targets.py` and this path simply never used it. The printed labels and the
  MCP tool's own description of `sell_candidates` were corrected in the same change, since
  both had said "declining" and the block now holds prime-age players too.
- **Middling teams get both paths, not a silent default.** A Middling team hasn't
  committed to pushing or pivoting - showing only the buy path (like Win-Now) would be
  picking a direction for them. `find_targets` runs `_buy_path` and `_pivot_path` and
  returns both, sharing the exact same logic Win-Now/Rebuilding use individually
  (no duplicated implementation, just composed differently). Which path actually makes
  sense for a specific Middling team likely depends on something not built yet -
  the season record (see below) - so showing both rather than guessing is the honest
  answer until that exists.
- **Trade activity is a flag, not a ranking.** It used to sort *first*, on the reasoning
  that a smaller name from an active trader beats a bigger name from someone who never
  trades. That reasoning is fine and the implementation was not: how often an owner trades
  ended up deciding which players a manager was shown at all. On the live league the #1 RB
  recommendation to a `Push` team produced **738** redraft and came from the most active
  trader, while the second-best current production available (**1,883**) sat 5th - off the
  end of the default three - because its owner had never traded. Activity says something
  about whether a call gets returned; it says nothing about whether the player helps. It is
  now the last tiebreak and stays fully visible (`from_owner_trades`, rendered as
  "NEVER TRADES" in the text output) so the caller can weigh it themselves. Applied to all
  three sorts - buy targets, acquire targets, pick targets - rather than the one that
  surfaced it, since siblings keeping the old behaviour is this project's second-most
  common bug.
- **Rank on the metric the window is buying.** The same sort then broke its own tiebreak by
  ordering on dynasty value, directly contradicting the line above it that puts declining
  players first *because* current production is the point. A `Push` team now orders by
  `redraft_value`; everyone else still orders by dynasty value, which is correct for them.
  This is the recurring root cause (dynasty value answering a current-season question)
  found for the fifth separate time.
- **Draft picks move in the direction the window implies** (`team_values.owned_picks`).
  Picks were previously used only as an aggregate - `pick_capital` for "who has draft
  capital" and `owns_next_first` for whether tanking is even coherent - which is enough
  to describe a team and useless for suggesting a trade. `owned_picks` resolves ownership
  through trades and returns the individual picks, so both sides of the conversation
  become possible:
  - **Win-Now** gets `picks_you_could_spend`. Converting future value into current
    production *is* the window; a contender hoarding its own 2028 1st is holding an asset
    it's less positioned to use than almost anyone else in the league.
  - **Rebuilding** gets `picks_to_acquire`, restricted to picks held by Win-Now and
    Middling teams. A pick is worth more to a rebuilder than to the contender holding it,
    which is exactly what makes the ask realistic - another rebuilder's pick isn't going
    anywhere. Sorted by trade activity first, same as player targets.
  - **Middling** sees both, consistent with getting both the push and pivot paths.

  Same two-year horizon as `pick_capital` (`FUTURE_DRAFT_YEARS`), since further-out picks
  are too speculative to price and rarely trade. Validated against a real consistency
  check: rjl22 shows a 2028 1st but no 2027 1st, matching the `owns_next_first: False`
  that `team_state` derives independently from the same traded-pick data.
- **Pick slots are estimated from the *originating* team's window.** A "2027 1st" isn't
  one thing - a rebuilding team's is an early pick, a contender's is a late one, and the
  market prices that gap at nearly **2x**: Early 4,487 / Mid 2,955 / Late 2,263, against
  a flat 2,853. Treating them as equal understates a rebuilder's first by ~57% and
  overstates a contender's by ~26%.

  No new data was needed. FantasyCalc already publishes Early/Mid/Late prices for the
  next class - the market has priced this distinction all along - and `owned_picks`
  already tracked `originally`, since a pick's slot is decided by *whose pick it is*, not
  who currently holds it. `STRATEGY_TO_PICK_TIER` maps Rebuilding/Middling/Win-Now onto
  Early/Mid/Late, keyed on `effective_strategy` so a "Rebuilding" team that's actually
  loaded doesn't get its pick priced as an early one.

  Demonstrated on real data: a Middling team held a 2027 3rd originating from a Win-Now
  team, and it correctly priced as **(Late) 956** rather than the holder's own (Mid)
  1,051.

  **Only the next class gets tiered**, which is the honest limit rather than a gap - a
  team's current window is a fair guide to where it finishes next season and a poor one
  two years out. Later picks keep the flat round value, and every pick carries a
  `slot_basis` string saying which it is, so the distinction is visible rather than
  implied.

  *Not modelled, and league-specific*: the actual draft order. Real leagues use their own
  tiebreakers - one here orders by best-ball scoring and then playoff finish - and those
  rules live in league documents, not the Sleeper API. Late in a season, or in the
  offseason before the rookie draft, the real slot is often knowable exactly, which would
  beat any window-based estimate.

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

**Both sides are filtered by whether they actually fix the need** (`_fills`), which the
count-vs-quality split made necessary. Spare depth fills an *empty slot* fine, so a
`critical`/`top-heavy` need takes any usable body. A `weak` position already has its
slots covered and only improves if the incoming player beats the current worst starter -
otherwise the swap list offers a fringe backup as the cure for a bottom-third room, which
is churn dressed up as a fit. Live case before the filter: rjl22, weak at QB and TE, was
offered Cam Ward and Isaiah Likely. After it, he correctly has no mutual swaps at all -
his needs are upgrade-shaped and nobody's *bench* upgrades him. (Likely is the precise
case: 634 redraft against rjl22's worst TE starter at 660, so strictly a downgrade.)

**Both sides must also be within `MIN_SWAP_BALANCE` (0.6) on value.** Both being spare
startable depth doesn't make a trade proposable - the cartesian match happily offered a
genuine RB3 for a fringe backup QB, and nobody accepts that, so surfacing it is noise.
This is a sanity bound, not the fairness calculator this project deliberately doesn't
build: the returned `balance` block carries both totals and says in words that this is a
*shape that could work*, not a priced offer.

**Note on what this returns today.** Across the real league it now finds **zero** swaps,
and that's the correct answer rather than a broken path: excluding actual starters leaves
only four teams with any spare startable depth at all, one player each, and the single
plausible pairing fails the upgrade test on the merits. In a 12-team superflex league with
ten starting slots across four positions, genuine startable surplus is genuinely rare -
which was always true and was previously hidden by counting starters as spares. Because an
empty result is indistinguishable from a broken one, the swap path is covered by unit
tests that construct a league where a swap *does* fire, one where the balance check
rejects it, and one where the upgrade bar does.

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

  **Where the tokens actually go** (measured, after noticing ~1.4M tokens over ~45
  questions looked far larger than the information those questions contain):

  > **First attempt at this breakdown was wrong, and the correction matters.** It
  > estimated our tool cost from docstring length (586 tokens) and attributed the
  > remaining ~3,400 to "`claude` CLI overhead we don't control - 72%". Both parts were
  > wrong. Tool definitions are sent as full JSON Schema, not docstrings, so the real
  > figure is 1,044. And the CLI floor was never measured, just inferred by subtraction.
  > Measuring it directly (bare options: one-line system prompt, no tools, no MCP
  > server) came back at **136 tokens** - essentially nothing.

  Measured by adding one layer at a time, each a real API call:

  | Configuration | Total tokens | Added by that layer |
  |---|---|---|
  | Bare CLI (1-line prompt, no tools) | 136 | — (the actual SDK floor) |
  | + our real system prompt | 819 | **+683** (ours) |
  | + MCP server and 7 tools | 2,695 | **+1,876** (1,044 tool schemas + ~830 MCP framing) |

  So roughly **65% of the baseline is our own content** - the system prompt and the
  tool descriptions - not framework overhead. The SDK is close to free; we are the
  expensive part.

  Tool *results* were the intuitive suspect and turned out not to be the problem -
  measured on a real league, the owner-filtered `get_team_state` is ~742 tokens,
  `get_trade_targets` ~1,074, `get_roster_needs` ~201, `check_league_format` ~16. Only
  unfiltered `get_team_state` is large (~7,846), which is why the `owner_name` filter
  added earlier matters more than it first appeared.

  **This kills the cost argument for dropping the Agent SDK.** The earlier (wrong)
  version of this section suggested calling the Anthropic API directly to escape ~3,400
  tokens of framework overhead. That overhead does not exist - switching to the raw API
  would carry the same system prompt and the same tool schemas and save on the order of
  a hundred tokens. If the SDK is ever replaced it should be for a different reason
  (control, dependencies, portability), not this one.

  **What is actually trimmable, and the reason not to rush it:** the system prompt is 10
  rules and the tool descriptions are deliberately wordy - and nearly every bit of that
  verbosity was added to fix a real observed failure (rule 6's grounding constraint,
  rule 8's tool-choice guidance, rule 10's stop-on-error, `get_team_state`'s "this IS
  the authoritative classification, don't re-derive it from roster_detail"). Cutting
  them would save maybe 500-800 tokens per question and risk regressing bugs that took
  real debugging to find. With per-session prompt caching now in place, that prefix is
  re-read at 0.1x rather than re-created anyway, so the remaining upside is small.
  Trimming is a real option, but it should be driven by the eval suite, not by
  eyeballing which sentences look long.

  **Per-session clients (`agent/sessions.py`) are the available mitigation**, and this
  is the strongest argument for that work: a fresh client per question re-*creates* the
  4,714-token prefix at 1.25x cache-creation pricing, while a persistent session
  re-*reads* it at 0.1x. That turns roughly 5,900 token-equivalents per follow-up
  question into roughly 470 - about a 92% reduction on the prefix for every question
  after the first in a conversation.

  A RAG/vector-retrieval layer to shrink the tool surface would still be backwards
  here - it would push the prefix back *below* the caching threshold, not above it,
  and our tool schemas are only 586 tokens anyway, so there is nearly nothing to win.
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

## Conversation sessions and UI (`agent/sessions.py`, `agent/static/index.html`)

The agent presented as conversational but was strictly single-turn: every question
opened a fresh `ClaudeSDKClient`, so after asking "what's your league ID?" it had no
memory of asking. Verified live before and after - turn 1 "hey can you help me with my
team" produces the clarifying question, and turn 2 "league <id>, I'm <owner>", with no
restatement, now continues correctly instead of landing on a model with zero context.
*Looking* conversational while being unable to hold a conversation is the worst
combination for a demo visitor, who will treat it like a chatbot.

`SessionManager` keeps a live client per client-supplied session id, with an idle TTL,
an LRU cap, and a per-session `asyncio.Lock` so two concurrent requests on one session
can't interleave on the same client and corrupt the conversation. **Sessions are never
shared between callers** - that was the context-leak trap identified during the caching
investigation, where naively reusing one client would have leaked one user's
conversation into another's.

**The extra complexity earns its place by fixing two measured inefficiencies as a side
effect**: both the Anthropic prompt cache and the MCP data cache live inside the
per-session client, so a fresh client per question meant re-paying ~4,700 tokens of
cache *creation* (billed at 1.25x) and re-downloading FantasyCalc and nflverse data
every single time. A persistent session re-*reads* that prefix at 0.1x instead - roughly
a 92% reduction on the prefix for every question after the first.

`MAX_SESSIONS` defaults to **2** deliberately: each live session holds two subprocesses
(the Node CLI and the Python MCP server with polars/pandas loaded) on top of the uvicorn
parent, against a 2 GiB container. Raise only alongside the memory limit.

**A bug this introduced, caught by asking what the UI would display**:
`ResultMessage.total_cost_usd` is cumulative for the client's lifetime, not per-question
- harmless when every question got a fresh client, but on a persistent session it keeps
growing. The UI would have shown each question costing progressively more, and worse,
`budget.record()` would have charged the running total every turn - three questions
costing $0.015/$0.016/$0.013 billed as $0.090 against real spend of $0.044, draining the
daily ceiling roughly twice as fast as actual usage. `Session.cost_delta()` tracks the
baseline and returns the difference.

The UI is one static HTML page with vanilla JS served by FastAPI - no build step, no
npm, ships in the same container. Session ids are generated client-side, so an expired
session starts a new conversation rather than erroring, and one browser tab's
conversation is unreachable from another.

**League data is rendered directly, not described by the model** (`GET
/api/league/{id}`). This is the frontend expression of the project's core split: the
analysis layer already computes team windows, ranks, needs and cornerstones exactly, so
paying Claude tokens to *recite* them is both wasteful and the one place confabulation
can creep in - the model inventing a rank or a player name. The agent is reserved for
reasoning ("should I trade for him", "why is this team Win-Now"), not for reading a
table aloud. It also makes every analysis flag visible that was previously buried in
JSON: `is_loaded`, `is_thin` and `owns_next_first` now show as badges on the teams they
apply to. Measured: 6.97s cold, **0.045s warm** off `sources/cache.py` - a 150x
difference that makes browsing several teams feel instant.

A small markdown renderer (~6 lines of regex, no library) handles the model's `##` and
`**`, which previously rendered as literal characters.

**Three real bugs surfaced within minutes of having a browser-driven dev loop**, none
of which the container or the CLI had exposed:
1. **`load_dotenv()` searched the working directory**, so running the app from anywhere
   but the repo root silently left `ANTHROPIC_API_KEY` unset. Masked in production,
   where Cloud Run injects the key as a real env var and no `.env` exists at all. Now
   an explicit path relative to the module.
2. **`api.py`'s `except Exception:` swallowed the error without logging it** - the same
   debugging dead-end this project already hit with the MCP subprocess. Failures in
   `sessions.acquire()` happen outside `run_query`, so its `try/finally` never saw them
   and the only copy of the traceback was discarded. Now logged before being swallowed;
   the caller still gets a generic message.
3. **`uvicorn --reload` is unusable for this app on Windows.** Reload puts the worker on
   asyncio's `SelectorEventLoop`, which cannot spawn subprocesses at all (bare
   `NotImplementedError` from `_make_subprocess_transport`), and this agent spawns two.
   Every request died with an opaque "Failed to start Claude Code". Setting the event
   loop policy in the app module does *not* fix it - uvicorn creates the loop before
   importing the module, so the policy applies too late; that was tried and removed
   rather than left in as code that looks like a fix. Plain `uvicorn` works. Linux is
   unaffected, which is why the container never hit it.

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

## CI/CD (`.github/workflows/`)

Deployment originally ran through a Cloud Build trigger created by the Cloud Run
console, which built and deployed on every push to `main`. When unit tests were added,
CI and CD ended up running **independently** - GitHub Actions ran the tests while Cloud
Build deployed regardless of the result, so a push that broke tests still shipped.

`deploy.yml` closes that: it triggers on `workflow_run` after `tests` completes and
explicitly checks `conclusion == 'success'`, because `workflow_run` fires on failure
too. It checks out `workflow_run.head_sha` rather than current `main`, since those can
differ if something lands while a test run is in flight.

**Authentication is Workload Identity Federation, not a service account key.** The
common approach is a service account JSON key stored in GitHub secrets, which works and
is still widespread - but it's a long-lived credential sitting on someone else's
server, and every third-party action in a workflow runs arbitrary code with access to
`secrets.*`. WIF instead has GCP trust GitHub's OIDC issuer and exchange a per-run
token for a short-lived GCP one. The provider's `attribute-condition` pins that
exchange to this repository; without it, *any* GitHub repo could impersonate the
deployer service account.

**Remaining risk, and what was done about it.** A pipeline that deploys arbitrary code,
running code that needs a secret, can always be made to leak that secret - that part is
inherent to CD and can't be engineered away. What *was* fixable: the service originally
ran as the **default compute service account, which carries project Editor**, so a
compromised deploy would have inherited control of the whole project rather than just
the one secret. It now runs as a dedicated `fantasy-fanatic-run` identity holding
exactly one permission - `secretmanager.secretAccessor` on `anthropic-api-key` - and no
project-level roles at all.

The deployer's permissions were narrowed the same way, in the safe order (add the
tighter grant, verify, then remove the broad one, with the rollback command in hand
since Actions is now the only deploy path). It ended up with exactly three project
roles - `run.admin`, `artifactregistry.writer`, `cloudbuild.builds.editor` - plus
`storage.admin` scoped to the single `run-sources-fantasy-fanatic-us-central1` staging
bucket rather than project-wide. The WIF condition also pins to
`refs/heads/main`, so a workflow on any other ref can't mint a token at all, and the
old default compute account's leftover access to the secret was revoked once the
dedicated runtime identity was proven working.

Deliberately imprecise in one place: the bucket grant is `storage.admin` scoped to that
bucket, not a more minimal `objectAdmin`. Finding the true minimum would take several
break-and-fix deploy cycles, and the gap between "one bucket" and "one bucket, fewer
verbs" is small next to the gap from "every bucket in the project."

**What remains is inherent, not an oversight**: a pipeline that deploys arbitrary code,
running code that needs a secret, can always be made to leak that secret. The blast
radius is now one prepaid API balance rather than the whole project. Writing this down
rather than hiding it is deliberate - the workflow file is public, so the permissions
are inspectable regardless. Obscurity would buy nothing, and an unexamined risk is
worse than a documented one.

**The Cloud Run settings live in the workflow, not only in console click-state.**
`--concurrency 1` and `--max-instances 1` are load-bearing (`budget.py`'s daily cap is
only exactly accurate with a single instance, and each request spawns two subprocesses),
`--memory 2Gi` covers polars/pandas in both the parent and the MCP subprocess, and
`gen2` is needed for subprocess spawning. Those were previously invisible configuration
that a stray console edit could silently undo.

CI deliberately excludes `agent/evals.py` (real paid API calls) and
`agent/test_mcp_server.py` (hits live third-party APIs, so it would go red on someone
else's outage rather than on a real regression). No `ANTHROPIC_API_KEY` is provided to
the workflow, so a test that silently started needing one would fail loudly.

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

## Unit tests (`tests/`)

Added late, and the gap they closed is worth recording: the project had an eval harness
and **no unit tests at all** - pytest wasn't even installed. That left the most
carefully-reasoned part of the codebase with the least protection. Age curves,
team-window thresholds, relevance floors, and need/surplus logic had been validated by
eyeballing real league output once, and were asserted nowhere.

The concrete trigger: the caching change touched every data source and the eval suite
passed 7/7 - but those evals only detect *agent misbehavior*. They would have passed
just as happily if a TTL had served stale rosters or a threshold had been mistyped.

26 tests, **free and offline**, because almost every rule in `analysis/` is already a
pure function taking plain data - no fixtures, no network, no API spend, ~1.5s. They
assert the *boundaries* the heuristics turn on (exact age cutoffs per position, the
usage-role overrides, the 50%/25% relevance split, need-vs-surplus symmetry) plus
regression guards for real bugs found during development: never offer a starter, never
offer a position you need, report every grounding violation rather than the first, and
bill the per-question cost delta rather than the cumulative session total.

**Verified the tests actually bite** rather than assuming: deliberately changing the RB
decline age from 27 to 99 fails 2 tests instead of passing quietly. Also caught one test
of my own that passed vacuously - an `all()` over an empty list - and rewrote it to
compare two otherwise-identical players differing only in starter status, so it can't
succeed by accident.

`pytest` lives in a separate `requirements-dev.txt` so it doesn't ship in the Cloud Run
image, where Artifact Registry storage is billed past 0.5 GB.

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
- **NEXT UP: `redraft_value` is a trade price, not a weekly point projection, and the
  difference has a direction.** A trade price bakes in *positional scarcity* - a TE is
  valued partly because you are forced to start one and the good ones are scarce, which is
  a real advantage in the dedicated TE slot. But a FLEX decision is not about scarcity, it
  is about raw expected points, and an RB or WR priced the same as a TE will usually
  project higher week to week.

  So `fill_lineup` is systematically biased toward putting tight ends in flex slots. The
  live case is the one that prompted this: with the RB2 lost, the vacated FLEX went to
  Dallas Goedert (627) over Wan'Dale Robinson (259) - and in reality Robinson is the more
  likely start. The magnitude looks small enough to live with for now ("probably close
  enough"), and TEP scaling pushes in the right direction for TE-premium leagues, but the
  bias is directional rather than random so it will not average out.

  A real fix needs actual weekly projections rather than trade values, which is a new data
  source rather than a tweak. Note the same root cause is already documented for PPR -
  FantasyCalc's `ppr` parameter is a flat 0.6% per-position scalar that cannot tell a
  receiving back from an early-down back - so a projections source would close two gaps at
  once and is probably the single highest-value external addition left.
- **Injury rates are reported for players who could never play.** `miss_rate` is attached to
  every roster row, so a deep bench body with a 44% rate reads as a risk when he was not going
  to see the field either way - his availability changes nothing. The tool description now
  says to weigh it only for starters and the bodies immediately behind them, but the data
  itself doesn't distinguish. `would_start_if_one_out` already answers "could he plausibly
  reach the lineup" and could gate it properly.
- **The relevance floor ignores league size and roster depth.** `MIN_RELEVANCE_FRACTION` is a
  flat 0.5 / 0.25 of a leaguewide top-N bar, and neither number knows how many teams there
  are, how deep the benches go, or whether there is a taxi squad. Those settings decide how
  much of the player pool is rosterable at all, which is exactly what "does this player have
  real value" depends on: in a 10-team league with short benches, good players sit on waivers
  and a bench piece is genuinely replaceable; in a 14-team league with 35-man rosters and a
  taxi, almost nothing is.

  The three real leagues here are 12 teams, 10 starters, 14-15 bench, 4 taxi - roughly 336
  players rostered against 120 starting slots, so **two thirds of every roster is bench**.
  A flat fraction is being asked to describe all of that.

  This got more load-bearing today, not less: moving `find_surplus` onto the relevance floor
  took surplus from ~4 entries per league to ~100, so the constant now drives a much larger
  number than when it was chosen.

  **It cannot be calibrated from what we have** - all three leagues are the same shape, so
  there is no variation to fit against. Same problem `format_support` handles by flagging
  shallow leagues as degraded rather than pretending to know. Needs either more league formats
  or a derivation from first principles (rostered players per team against valued players
  available), not another guessed constant.
- **Depth is not yet *weighted* by injury risk.** The rates now exist (`sources/injuries.py`)
  and are reported, but nothing multiplies them together: how much a bench body is worth
  should depend on the starter's own miss rate, the position's rate, and how long a typical
  absence lasts. That last one isn't measured at all - a rate treats a one-week hamstring
  and a torn ACL identically, and they are not the same problem for a roster. Doing this
  properly means severity and duration by injury type, which is a real modelling exercise
  and a genuine rabbit hole; noted as somewhere to go deliberately rather than to drift into.
- **Freak accidents and chronic fragility are counted identically.** A rate cannot tell a
  player who tore a ligament in a pile-up from one whose soft tissue keeps failing, though
  only the second predicts anything. Injury *type* is in the report data
  (`report_primary_injury`) and is not used yet.
- **Availability is binary here, and playing hurt isn't.** Hamstring injuries in particular
  linger for weeks after a player returns and depress production the whole time, so a
  manager sees a healthy-looking `ACT` week and a diminished player. Nothing in this model
  has a state between available and out, and adding one means joining injury designations to
  weekly production - which is also the join that would let severity be measured rather than
  assumed.
- **Dynasty values are the market's own age curve, and we should probably calibrate against
  them.** `AGE_CURVE` is documented as "dynasty community heuristics, not a model" while the
  values we already pull encode thousands of traders' view of exactly the same question. Share
  of players at each age carrying cornerstone value, measured on the live pool:

  | age | QB | RB | WR | TE |
  |---|---|---|---|---|
  | 26-27 | 21% | 8% | 15% | 7% |
  | 27-28 | 18% | 5% | **17%** | **0%** |
  | 28-29 | 36% | 0% | **0%** | - |
  | 31-32 | **0%** | - | 0% | 0% |

  WR shows a clean cliff at **28** against our 29, and TE appeared plainly wrong - the market
  looked like it stops paying at ~27 where our curve says **30**.

  **The TE half of that reading was an artifact, and it is now resolved: the curve stays at
  30.** The cornerstone bar is a single leaguewide 90th percentile, and the entire TE
  cornerstone population is **four players** (Bowers 23.7, McBride 26.7, Loveland 22.4, Warren
  24.2) against 13 QB, 13 WR and 10 RB. All four happen to be recent first-round picks, so
  every cell from 27 up contains **zero** players and reads 0% - which says nothing about
  aging and everything about there being no one in the cell. Tight end is a position with two
  or three elite players at a time; a percentile cut shared with QB and WR will nearly always
  find its TE cells empty.

  The direct evidence points the other way. Kittle at **32.9** is the 11th most valuable TE in
  the pool (2,424 dynasty / 2,053 redraft) and Kelce at **36.9** is 18th (1,805 / 1,473),
  ahead of tight ends a decade younger; both carry redraft/dynasty ratios above 0.8, which is
  exactly what our model should say about a productive declining asset - real now-value, no
  future. And the curve reproduces the domain read without being tuned to it: McBride at 26.7
  against a cutoff of 30 yields **3.3 years of runway**, matching the independent judgement
  that he is good for about three more years.

  **The age cutoff and the now-premium ratio do separate jobs, which is why neither needs to
  move.** Age alone would lump Kittle (32.9, ratio 0.85), Kelce (36.9, 0.82) and Mark Andrews
  (31.0, **0.39**) together as declining tight ends; the ratio alone would lump Kittle with
  Bowers (23.7, 0.82). Read together they separate correctly: Kittle and Kelce are old and
  still being paid for *now*, Andrews is old and no longer paid for anything, Bowers is young
  and already producing. Andrews is the TE to move here and the only one the tool pushes -
  which is the right answer and not one an age cutoff could reach by itself.

  So this is the same confound already flagged for QB, caught a second time: cornerstone value
  is decline **times remaining years**, and a small cell makes it unreadable. **WR is the one
  cell where the signal survives** - 13 cornerstones, oldest 27.4, none at 28+ - so 29 may
  genuinely be a year late. That one is still open. (RB's oldest cornerstone is McCaffrey at
  30.2 against our cutoff of 27, which is one player and proves nothing.)

  Three reasons the general version is not yet actionable. Cells hold 5-55 players, mostly 10-20, so one or
  two names move a column (RB "recovering" at 29-31 is two backs). It measures a *different*
  quantity: cornerstone value is decline **times remaining years**, so a 33-year-old elite QB
  reads 0% because the market won't pay for years he lacks, not because he has declined -
  which is why this shows QB ending at 31 while the passing-EPA work says 38, with no
  contradiction. And it is survivorship-biased, since players who fall off leave the pool.

  A real version needs a larger pool than the ~400 FantasyCalc values, and ideally the same
  players tracked across seasons. Worth doing - it would replace four hand-set constants with
  a measurement, which is this project's stated preference everywhere else.
- **The market disagrees with our rushing-QB discount on its most valuable player.** Josh Allen
  is 10,415 dynasty at ~29.5 with 7.1 carries a game. Our curve puts him on the rushing
  schedule (decline at 31, so 1.5 years of runway); the market has explicitly declined to
  discount him. One of the two is wrong about the single most valuable asset in the pool, and
  the tag is the thing making the stronger claim.
- **The pocket/rushing split may still be too coarse.** An elite QB who both throws and runs
  (top passing EPA *and* 7+ carries a game) is forced onto the rushing curve by the
  mutual-exclusion rule, which may badly underrate the passing half of his value. Resolving it
  needs decline data split by archetype rather than another judgement call.
- **Superseded: the QB age curve was wrong at both ends, per a domain expert.** `AGE_CURVE["QB"]` is
  (26, 34), pulled to 31 for a tagged `rushing_qb`. A sports-modelling data scientist argued
  that a pure pocket passer can hold value to nearly 40 while a rushing QB slows sharply
  closer to 30 - so the real spread between the two roles is much wider than 34-vs-31, and
  the pocket end is too pessimistic. This has teeth: it inverts a live sell recommendation.
  The tool ranked a 31.8-year-old pocket passer as the top sell (highest now-weighting) where
  he would sell the 28-year-old rushing QB, on the grounds that the market has not priced the
  rushing decline. Both are defensible and they answer different questions - "who is the
  market overpaying for now" versus "whose dynasty price erodes fastest" - and only the first
  is modelled. Not changed on one opinion, but it is the best-argued challenge to a curve
  constant so far and worth calibrating against real production-by-age data.
- **Deliberate positional strategy is read as a flaw.** The same manager stacks WR and TE on
  purpose - cheaper than QB, and value-insulated compared to RB - and stays light at RB
  knowingly, because RB value decays fastest. The tool reports his RB room as a `critical`
  need. `replacement_thresholds` already documents that a rebuilder's needs should be read as
  "what a contending version of this roster would lack", but nothing in the output says so,
  so a deliberate construction reads as a hole.
- **A rebuild's timeline isn't checked against the age of its own assets.** The window model
  says rebuild or contend; it never says *how long*. A roster whose best pieces are two
  28-year-old quarterbacks - one of them a rushing QB, whose curve declines at 31 rather than
  34 - cannot afford a three-year teardown, because the assets it is rebuilding around expire
  inside the rebuild. Nothing currently computes when a rebuilding roster's own core stops
  being good, which is the number that should set the urgency.
- **Suspension risk is measured but not used.** `weeks_suspended` is reported and
  deliberately excluded from `miss_rate`. Whether a past suspension predicts a future one is
  a genuine question, and if it does it belongs in availability forecasting as its own term
  rather than smuggled in under injury.
- **Dynasty rosters are deeper than the replacement-level bar assumes.** Dynasty formats
  carry far more players than redraft, so plenty of low-redraft-value players are
  genuinely starter-relevant in a way a value-derived threshold does not reflect. This
  interacts with `start_thresholds` and with "startable" everywhere it appears - and with
  the depth finding above, where the bar is already known to define surplus out of
  existence. Worth revisiting the whole "usable" concept once projections exist.
- **The redraft tail collapses to noise, and it caps the persuasion tier.** Not scattered
  outliers - a systematic floor effect below roughly the 30th-ranked player at a position.
  Real WR board: Brandon Aiyuk **1**, Jerry Jeudy 70, Jalen Nailor 76, Tank Dell 114,
  Khalil Shakir 140. Those are rostered NFL receivers whose weekly points are plainly not
  ~0; a redraft value of 1 means "nobody would trade anything for him", which is a *price*,
  not a forecast. (Wan'Dale Robinson at 259 was the first sighting of this, recorded then
  as a possible one-off.)

  The consequence is structural rather than cosmetic. `now_premium_bar` divides by these
  numbers, so any mid-tier veteran's ratio is pushed toward zero and the whole persuasion
  tier is effectively limited to the top ~30 per position. The live case: a 28.7-year-old
  WR at WR36 reads 0.43 - "priced more for the future than for now" - which is not a
  believable description of him, and his owner flagged it by eye. The bar is not too tight;
  the denominator is unreliable down there. Loosening the bar to compensate would import the
  noise instead of fixing it, so it is deliberately left alone until projections exist.
  Same root cause as the two entries above.
## Injury exposure (`roster_needs._injury_drop`)

Replacement level **cannot express depth at all**, which is not a gap so much as a
definition problem. `start_thresholds` is the Nth-best player leaguewide where N is every
starting slot in the league, so by construction only about enough players clear it to fill
everyone's lineups. Measured on two real leagues, **10 of 12 teams had zero startable
bench** - an artifact of the bar, not a fact about their rosters. A manager who is
genuinely one injury from disaster could not be told so.

Drop-off sidesteps the bar by asking about magnitude instead of counting bodies:
`drop_if_injured` is the production lost when the *last* starter at a position goes down,
ranked into league tertiles like everything else. The marginal spot is the right one to
price - if a team starts four RBs and its RB1 is hurt, everyone shuffles up and what
actually enters the lineup is the best bench RB, so the loss is (worst starter - best
bench). On a real roster that is 1,043, not the 4,298 a best-starter reading would give.

**Deliberately not a need.** Depth and lineup quality are separate questions with opposite
fixes, and folding exposure into `needs` would tell a perfectly healthy team it has four
problems. A real case: a team with **no positional needs at all** ranks 2nd, 3rd and 4th
worst in the league for QB, WR and RB exposure - the lineup is fine, and one injury ends
that. Absent rather than zero when no lineup is supplied, since 0 would read as "perfectly
deep".

**Flex slots are honoured, which was a real bug in the first version.** It looked up the
next player *at the same position*, so a QB lost from a SUPER_FLEX was priced against the
team's QB3 - when in fact the slot backfills from any position. The drop is now computed by
removing the weakest starter at the position and **refilling the lineup optimally**, which
handles SUPER_FLEX and FLEX for free. Correcting it moved real numbers (one team's RB
exposure 1,043 -> 900, TE 417 -> 232).

**It is the magnitude if it happens, not an expected loss**, and that distinction changed
the conclusion on the roster that motivated the feature. Its marginal losses are
Herbert 6,602 / Hurts 6,030 / McCaffrey 6,026 / Taylor 6,001 - QB is *not* uniquely
exposed, the whole top of the roster is, because the bench is uniformly barren. Combined
with QBs being injured less often than RBs, and with a superflex slot that any position can
fill, **two good QBs plus a cheap third is a sound build rather than a hole** - the initial
read of "QB is the disaster here" was wrong, and came from comparing position groups
instead of marginal risk.

*Backlogged*: position-specific injury rates, which would turn magnitude into expected
loss and is the missing half. nflverse publishes weekly injury reports, so unlike the PPR
gap there is a real source to calibrate against rather than a guess.

## Optimal lineup as a tool (`roster_detail.optimal_lineup`, `roster_needs.fill_lineup`)

Filling FLEX and SUPER_FLEX is a small deterministic optimisation with exactly one right
answer, and asking a language model to do it in prose is precisely the kind of thing it
will confidently get subtly wrong. The code already existed (`projected_starters`); what
was missing was any way for the agent to *reach* it, so "what if my RB2 gets hurt" was
being reasoned through instead of computed.

`fill_lineup` is `projected_starters` keeping **which slot** each player occupies, and
`optimal_lineup(league_id, owner, without=[names])` exposes it as `get_optimal_lineup`.
Slot assignments matter because the visible effect of an injury is players *moving*, not
one name vanishing. On a real roster, losing the RB2:

```
   RB    Christian McCaffrey   6,653      RB    Christian McCaffrey  6,653
   RB    Jonathan Taylor       6,628  ->  RB    TreVeyon Henderson   2,330   <- moved from FLEX
   FLEX  TreVeyon Henderson    2,330      FLEX  D'Andre Swift        1,527
   FLEX  D'Andre Swift         1,527      FLEX  Dallas Goedert         627   <- promoted
```

The manager's own expectation was that a WR would fill the vacated FLEX. It goes to a
**tight end** (627) instead of the backup WR (259), because FLEX accepts RB/WR/TE and the
bench TE simply produced more. That is the whole argument for the tool: the cascade is
mechanical, cheap to compute exactly, and easy to get wrong by hand.

System-prompt rule 10 forbids working a lineup out by hand and routes every "what would I
start / who replaces X / what does this injury cost" question here. Same pattern as the
trade-chip grounding check: where a deterministic answer exists, compute it rather than
instructing the model to be careful.

## Efficiency swaps: which windows, and what they are blind to

Extended from Push-only to **Push and Contend**, since the mechanic means different things
depending on whether there is a clock (`SWAP_FRAMING`): a closing window converts future
premium into capital, while a healthy contender is taking profit with no urgency. Middling
and Rebuild still want the premium this converts away.

Also corrected: swaps were suppressed at *any* need position. That is right for
count-shaped needs (critical/top-heavy), where promoting the backup fills the empty slot
but spends the last body you had - and wrong for `weak`, which has its slots covered and
wants a better starter, exactly what the freed value buys at flat production. The blanket
rule silenced the only two swaps in the league: a Contend team weak at QB and TE could
free 564 and 380 while its lineup barely moved.

**What this cannot see, and why it wasn't stretched to fit.** The mechanic requires a
replacement already on the roster producing >=90%, so it finds "sell the premium, promote
the backup" and is blind to "sell an aging starter you have no replacement for". The
clearest sell-high case in the league is invisible to it: a Contend team starting a
33-year-old RB1 (4,597) and a 37-year-old TE (1,504) with Pacheco at 146 and Juwan Johnson
at 318 behind them, while its entire WR room is 24. Selling both at peak is obviously
right and the production genuinely leaves - it has to be replaced externally, which is a
different trade. **Next up:** an age-cliff sell-high path keyed on age rather than the
redraft/dynasty ratio, for contenders whose young core carries them.

## Persuasion targets (`trade_targets._persuasion_targets`, `analysis/prior_season.py`)

`_buy_path` only searches `Rebuild` teams, so the best available production at a need
position could be structurally invisible. On the real league a `Push` team needing RB was
offered Rachaad White (449 redraft) and Tony Pollard (697), while Jonathan Taylor (6,649)
and Saquon Barkley (5,081) sat on a contender that is the most steeply falling team in the
league. A 15x gap in current production, hidden by a binary seller/non-seller split.

The fix is a separate, clearly-labelled tier - not a wider seller pool, because those
teams genuinely aren't selling. Three choices, each of which was a trap found while
scoping it:

**1. Sourced from `sellable`, not `win_now_core`.** The latter is gated on the cornerstone
threshold (4,289 here), so it holds Taylor (5,240) and drops Barkley (3,746) - the same
roster's *better* target. The output would have looked entirely reasonable while missing
the best name available.

**2. Ranked by current production per unit of trade cost** (`redraft_value / value`), not
by value. The normal buy path sorts by dynasty value descending, which is backwards for a
win-now buyer:

| player | held by | dynasty | redraft | prod/cost |
|---|---|---|---|---|
| Derrick Henry | shivvv (Contend) | 2,978 | 4,603 | 1.55x |
| Christian McCaffrey | rjl22 (Contend) | 4,437 | 6,585 | 1.48x |
| **Saquon Barkley** | kierankieran (Push) | 3,746 | 5,081 | **1.36x** |
| Jonathan Taylor | kierankieran (Push) | 5,240 | 6,649 | 1.27x |

Barkley beats Taylor on the ratio *and* costs 1,494 less outright, because at 29.5 the
market discounts him for seasons a pushing team isn't buying. That discount is the entire
point - the same arbitrage `find_efficiency_swaps` exploits *within* a roster, applied to
acquisitions. Anything not actually discounted is dropped - otherwise you'd be paying a
future premium to a team that doesn't even want to sell, the worst of both. "Discounted"
has to be judged **within the player's own position** (see `now_premium_bar` below); the
absolute bar this originally used was wrong in a way that took a live spot-check to catch.
An empty list is the honest answer when nobody's aging production is on sale to you.

**3. Implausible sellers are excluded, not ranked last.** Listing a name nobody can get
puts it at the top of a list sorted by ratio and makes the feature worse than nothing. What
counts as implausible is the subtle part, and the first answer was wrong - see below.

### What makes a non-seller plausible (`_seller_case`)

A falling trajectory (aging out is the one thing that turns a contender into a seller), or
a core that missed the playoffs with the roster it still has. Both are properties of the
**team**, and that was originally the whole test: no team-level reason, no suggestion.

### Why a team-level test wasn't enough (`_cliff_case`)

A trajectory is an average, and an average hides the individual. The league's **best** team
(contention rank 1) reads `Contend`/`steady` at 26% ascending against 16% declining - a
young core diluting the signal - while starting a 32.6-year-old RB the market prices at
1.54x. The team gate rejected that roster before any player on it was examined, so the
single best production-per-cost target in the league was unreachable.

The fix is a per-player fallback, and it is **not** "the player is old". Old is half of it.
The real condition is that **the owner's window and the player's don't overlap**:

| held by | contention rank | ascending | declining | tilt | verdict |
|---|---|---|---|---|---|
| shivvv | **1** | 26% | 16% | **+10** | surfaced |
| rjl22 | 2 | 21% | 23% | **−2** | not surfaced |

Two `Contend` teams, the same aging-elite-RB profile, opposite answers. The first contends
now *and* later, so its aging starter is surplus to a future that arrives without him. The
second contends now and is aging into it - that player is aligned with its window, and
keeping him is correct. `ascending_pct > declining_pct` is the entire discriminator, with
no constant to calibrate.

Two conditions guard it: the window tilt above, and **declining and starting** - a
declining player on the *bench* is just a bad asset, since his owner already stopped
relying on him and there's nothing to talk him out of. The now-weighting bar below is not a
third condition; every candidate has already cleared it upstream.

### The bar has to be per-position, and wasn't (`team_values.now_premium_bar`)

The first version of the cliff rule used an absolute `1.25` on `redraft_value / value`,
picked from a gap in the observed numbers. That was a bug wearing a threshold's clothes,
and the same bug the persuasion tier already had. Measured over a whole league pool:

| pos | n | p10 | median | p90 | **max** |
|---|---|---|---|---|---|
| QB | 39 | 0.26 | 0.97 | 1.31 | 1.60 |
| RB | 56 | 0.07 | 0.49 | 1.05 | 1.54 |
| TE | 30 | 0.03 | 0.25 | 0.81 | **1.01** |
| WR | 75 | 0.03 | 0.37 | 0.89 | **1.07** |

Dynasty and redraft are two unnormalized scales whose relationship differs sharply by
position. An absolute bar is therefore not "strict" - it is *unreachable* for some
positions. 1.25 could never be cleared by a TE or WR in any league. Worse, the pre-existing
`MIN_PRODUCTION_PER_COST = 1.0` had the same defect and had been silently closing the
entire persuasion tier to tight ends, and nearly closing it to receivers, since it was
written - justified in the code by "below 1.0 he costs more in dynasty value than he
delivers in current production", which reads as neutral and is nothing of the kind.

**This is the third recorded instance of the same mistake.** `find_efficiency_swaps`
documents it (fixed by comparing pairwise within a position) and `get_players_with_roles`
documents it (fixed by not exposing a ratio at all). Treat an absolute threshold on these
two scales as a bug on sight.

Both bars are now `NOW_PREMIUM_PERCENTILE = 0.9` of the ratio **within the player's own
position** - top-decile now-weighting. A percentile, not a tuned constant, so it
recalibrates with the market and with league format, the same reasoning behind the tertiles
in `team_state`. Ranked this way, a 36.9-year-old TE at 0.83 raw is the second most
now-weighted declining starter in the league rather than a rounding error below the bar,
which matches how the league's managers actually see him.

**One bar, not two.** A separate looser floor for the team-level path was tried at the
median and dropped: it admitted players who are merely typical (a WR at 0.43 against a 0.37
median), which is not "age-discounted" in any sense a manager would recognise. So the cliff
path needs no bar of its own, and what distinguishes it is solely the window mismatch.

The bar measures *shape*, not quality - it says the market prices a player for now rather
than later, not that he's any good. Whether he's worth having at all is an absolute
question, and `clears_relevance_floor` already answers it upstream. A percentile cannot by
itself return "nobody qualifies", since ~10% of each position always clears it; the honest
empty answer comes from the other conditions, which is where it belongs.

Across both 12-team leagues the cliff path adds **two** names, each to the teams with a
real need at that position. That is the intended volume - the tier is for the rare case a
team-level read structurally cannot see, not a second opinion on every roster.

**The reigning-champion veto was removed by this change.** It existed to stop exactly the
aging-contender case the tilt now rejects on its merits, and it was already redundant on
the team path (a non-falling champion made the playoffs, so `_seller_case` returned `None`
anyway). Keeping both would be two mechanisms for one job. The tilt is also the better
reason: a title says less about whether an owner should sell than the shape of their roster
does, and a champion tilting ascending is a team that can afford to sell, trophy or not.

This tier deliberately does **not** check whether the owner has a replacement behind the
player. That question - *should* they do this - belongs to `find_efficiency_swaps`. This one
only answers *is it worth asking*, and `cost_note` says an ask is all it is.

## How often players actually miss games (`sources/injuries.py`)

`drop_if_injured` measured magnitude and disclaimed likelihood in its own note - "injury
rates differ by position and are not modelled, so an equal number at QB and at RB is not
equally worrying." That told a reader the number wasn't comparable across positions without
giving them any way to compare it. Now it is measured, from nflverse weekly rosters plus the
weekly injury report, over the last three completed seasons:

| position | share of roster weeks missed |
|---|---|
| QB | **0.107** |
| TE | 0.177 |
| WR | 0.195 |
| RB | 0.200 |

Quarterbacks miss roughly **half** as often as skill players, which is what makes the old
disclaimer real rather than theoretical, and independently matches what the league's manager
assumed when he built two good QBs plus a cheap third in superflex.

**A missed week is an *injury* reserve week, an injury inactive, or an `Out` report - and
the reserve half matters most.** Season-ending injuries live on IR, and a player on IR often
stops appearing on the weekly report altogether: R01 weeks show up on the report only **5%**
of the time, so reading the report alone would undercount precisely the absences a manager
most needs to plan for. `Questionable` and `Doubtful` are excluded, since they describe
uncertainty rather than absence and plenty of questionable players play a full game.

**Suspension is not fragility, and the first version said it was.** Counting all of status
`RES` scored suspensions as injuries. A receiver came out at 0.451, six weeks of which were a
suspension served in perfect health - caught by his owner reading the output, within minutes
of the module shipping. `status_description_abbr` carries the reason, and the codes were
classified **empirically** - by how often each also appears on the injury report, and by who
is in it - rather than by guessing at the NFL's vocabulary:

| code | weeks | on injury report | reading |
|---|---|---|---|
| R01 | 12,309 | 5% | Reserve/Injured |
| R48 | 1,162 | 47% | IR, designated to return |
| R04 / R05 | 1,328 | ~11% | PUP / non-football injury |
| I01 | 1,872 | - | inactive, injury |
| **R40** | 177 | **0%** | **suspended** |
| **R30** | 51 | **0%** | **suspended, indefinite** |
| **R06** | 53 | **0%** | **did not report / left squad** |

An **allowlist**, so an unfamiliar or newly-added code counts as not-injury. That is the safe
direction: understating a rate is a smaller error than telling someone a player is fragile
when he was suspended. Non-injury absences are dropped from the numerator *and* the
denominator - leaving them in the denominator would quietly reward being suspended with a
lower miss rate - and surfaced separately as `weeks_suspended`, because being unavailable is
still a real fact and suspension arguably predicts itself. What it must not do is arrive
wearing an injury label.

The correction moves real numbers: 0.451 to 0.378 for the receiver above, and 0.022 for a
different one whose only absences were a six-week suspension.

**The denominator is weeks actually on an NFL roster, not a flat 17 per season.** The flat
version silently rates a player who wasn't in the league as perfectly durable. Practice-squad
weeks are excluded too - those players aren't expected to dress, so counting them would read
as availability nobody wanted.

**Two sample floors, and the second was found by looking at the output.** With only a week
floor, the tail was nonsense at both ends: players who spent one season on IR and were never
otherwise rostered scored exactly 1.000 (17 of 17), and *every rookie* scored 0.000. The
second error is the more dangerous one, since rookies are what a dynasty manager is most
often asked to price, and "never been hurt" is a very different claim from "we have watched
him for four months." `MIN_SEASONS = 2` is the point at which the number describes a player
rather than a year. It costs coverage - 257 of 398 pool players carry a rate - and the
missing ones are reported as **None for unknown, which is not zero**, in line with how this
project handles every other absent number.

Face validity on the live pool: Jonathon Brooks 0.912, Deshaun Watson 0.725, J.K. Dobbins
0.529, Anthony Richardson 0.510. Dobbins is a starter on the roster whose owner described it
as "short and injury-prone" before any of this was measured - and the bench body behind him
sits at 0.441, so the cover is itself fragile.

Pooled over player-*weeks* rather than averaged over players, because the question is what
happens to a lineup slot, not what the average résumé looks like - a per-player mean lets a
fringe body who spent one year hurt count as much as a decade-long starter.

Deliberately shallow: this says *whether* a player was available, never the severity, type,
or recency of what kept him out, and it forecasts nothing. Weighting depth by injury type and
expected duration is logged under future work rather than half-built here.

## The QB curve by archetype, and runway (`player_roles`, `years_to_decline`)

The QB age curve was 34, pulled to 31 for a tagged `rushing_qb`. A sports-modelling data
scientist reviewing the tool argued the pocket end was badly pessimistic: a genuine pocket
passer trades on arm talent and processing, which hold into the late thirties, while a rushing
QB leans on legs that slow near 30. **The only constant in this project changed on an outside
opinion** - and changed because the data backed it.

Quality is the operative part. A *mediocre* pocket passer does not age gracefully; he gets
replaced. Measured over three seasons of passing EPA per game, the top tier is exactly the
archetype:

| QB | EPA/g | CPOE | carries/g |
|---|---|---|---|
| Jared Goff | **6.87** | 3.66 | 1.7 |
| Brock Purdy | 6.62 | 3.97 | 3.7 |
| Matthew Stafford | 5.33 | −0.47 | 1.6 |
| Joe Burrow | 4.51 | 4.68 | 2.5 |
| Patrick Mahomes | 4.30 | 2.40 | 4.6 |
| *Jalen Hurts* | 2.99 | 5.30 | **8.5** |
| *Justin Herbert* | **1.66** | 0.41 | 4.5 (5.47 in one season) |

**EPA rather than CPOE, despite CPOE being the better-isolated statistic.** Completion
percentage over expected penalises aggressive downfield throwing, and Stafford posts −0.47
CPOE against 5.33 EPA per game - requiring positive CPOE would drop one of the clearest
examples of the archetype the tag exists to capture. Top *third* rather than a fixed EPA
number, so it recalibrates with the league's passing environment.

**One window for every tag, and a long one.** These tags feed an *age curve* - a claim about
how a player holds up over years - so the question is always what kind of player he is, never
what he did last autumn. Splitting quality over three seasons and usage over one seemed
defensible and immediately cost something real: a quarterback ran 5.47 times a game in a
single season against 4.46 across three, which flipped him across the rushing bar and onto a
curve declining three years earlier. His own manager called it wrong on sight. He is untagged
now - the honest answer, since the evidence points nowhere in particular.

That is the general principle: **tags are hard to earn and most players get none.** A wrong
tag costs far more than a missing one, because it substitutes a confident claim for a neutral
default. It is also where this project stops - projecting an individual's decline is sports
modelling, done properly by people who do it for a living, and these tags exist only to keep
the age curve from being obviously wrong about broad archetypes. `rushing_qb` and `pocket_passer` are mutually exclusive and rushing wins -
a QB who runs enough to clear that bar carries the rushing risk whatever his arm does. That
is the conservative reading and a real judgement call, since it puts the league's best
run-and-throw QBs on a curve that may be too pessimistic for them.

**38, not 40.** The claim is that these players hold dynasty *trade value*, and a 39-year-old
is priced on one more season however well he is playing. The curve should turn before the
market does, not with it.

### Three archetypes, because running and throwing are separate measurements

Making rushing and passing mutually exclusive - with rushing winning - forced the league's
best run-and-throw quarterbacks onto the curve built for players whose game is *only*
mobility. The market disagreed loudly: **Josh Allen is 10,415 dynasty at 30.2 with 7.1 carries
a game**, and the old curve gave him 0.8 years of runway. It is hard to hold both views.

The two measurements are independent, so there is a genuine third case rather than a tie to
break. A quarterback who runs *and* passes at an elite level has something to fall back on
when the legs go; one who only runs does not.

| tag | cutoff | who | why |
|---|---|---|---|
| `rushing_qb` | **32** | Hurts (2.99 EPA/g, 8.5 car/g), Daniels, Nix, Murray | mobility-only, marked down |
| `dual_threat_qb` | **34** | Allen (6.05, 7.1), Lamar (5.25, 7.9), Maye | no discount - the *default* curve |
| `pocket_passer` | **38** | Goff, Purdy, Stafford, Burrow, Mahomes | arm and processing outlast legs |

`dual_threat_qb`'s cutoff is just the default QB curve: the point is the **absence** of a
discount, not a new bonus, and a named tag says that where an untagged player would leave it
implicit. Not 38 either - their value still leans on mobility.

Runway moves accordingly: Allen 0.8 → **3.8**, Lamar 1.4 → **4.4**, while Hurts stays on the
rushing curve at 4.0. Pure rushing also moved 31 → 32.

**This also cleared the overshoot recorded above.** Allen and Lamar were "sellable" only
because a discount they should never have carried put them under `MIN_MEANINGFUL_RUNWAY`;
both are cornerstones again, and the audit stays clean. Two findings that arrived separately
turned out to be one bug.

### The change is currently a no-op, and that is worth stating

Nine QBs across three real leagues carry the tag. **Zero of them change bucket**, because none
sits between 34 and 38 - Goff is 31.8 and Dak 33.1 (prime either way), Stafford 38.5
(declining either way). It is a forward-looking correction that will start mattering within a
year, not a fix to anything visible today. Recording that rather than letting a green test
suite imply otherwise.

### Runway (`years_to_decline`)

What *did* resolve the underlying disagreement. `age_bucket` answers "which side of the line"
and discards the distance to it, which is the number a dynasty seller wants:

| | age | role | years to decline |
|---|---|---|---|
| Justin Herbert | 28.4 | rushing_qb | **2.6** |
| Jalen Hurts | 28.0 | rushing_qb | **3.0** |
| Sam Darnold | 29.2 | - | 4.8 |
| Jared Goff | 31.8 | pocket_passer | **6.2** |

All four read `prime`. The tool's sell list ranked Goff first, because it ranks on how
now-weighted the market's price is. The expert said sell Hurts and keep Goff - the older man
throws from the pocket and has twice the runway, and the market has not priced the rushing
decline. Both answers are defensible and they answer different questions; only one of them was
computable before this.

Deliberately **not** folded into any existing sort. Two orderings competing inside one list is
exactly how the buy path ended up ranking trade activity above value. It is reported so a
caller can weigh runway against price - and it is the input the rebuild-timeline backlog item
needs, since a roster whose core turns in three years cannot run a four-year teardown.

## Win-now measurements handed to teams that aren't playing (`REBUILD_LENS`)

Everything `roster_needs` computes is a win-now measurement, and about a third of any league
is not playing that game. `replacement_thresholds` has said so in its docstring since it was
written - "read a rebuilder's positional needs as what a contending version of this roster
would be short of, not a to-do list" - and **nothing in the output ever said it**, so the tool
reported a deliberate allocation as a hole.

The live case: a manager who stacks receivers and tight ends on purpose and stays light at
running back knowingly. The tool called his RB room `critical`. It is not wrong about the
lineup; it is answering a question he is not asking.

Two things flip for a rebuilding team, not one:

- **A need becomes descriptive.** Useful for valuing the roster, misleading as advice.
- **Exposure stops being a risk at all.** A team not playing for this season loses nothing it
  wants when a starter goes down. Presenting high exposure to a rebuilder as a concern is not
  merely mistimed, it is backwards - and that had never been said anywhere.

Needs entries now carry `applies_this_season: False` plus `REBUILD_LENS`, and `depth_adds`
switches note: for a contender those cheap bodies are insurance for a lineup worth
protecting, for a rebuilder they are lottery tickets, because a back who inherits a starting
job becomes a sellable asset. Same players, different reason, and the note has to say which
or it recommends the right thing for a reason that doesn't apply.

## Positional market structure, and the limits of testing an allocation thesis

The same manager's stated strategy is that WR and TE are *cheaper than QB* and more
value-insulated than RB, so he accumulates there and stays light at running back. Worth
recording what could and could not be checked, because the first attempt got the reasoning
wrong.

**Raw dynasty values ARE comparable across positions** - that is the entire premise of a
trade calculator, one currency for every player. What is *not* comparable across positions is
the `redraft / dynasty` ratio, since those are two unnormalized scales (see
`now_premium_bar`). Extending "the ratio isn't cross-comparable" to "the values aren't
cross-comparable" was simply wrong, and it briefly ruled out a measurement that is perfectly
sound.

Dynasty cost of the Nth-best player at each position, in a 12-team superflex TE-premium
league (2 QB slots, 2 RB, 2 WR, 1 TE, 3 flex):

| rank | QB | RB | WR | TE |
|---|---|---|---|---|
| 1 | 10,423 | 10,189 | 9,929 | 8,406 |
| 12 | **4,671** | 3,848 | 4,368 | **2,329** |
| 24 (replacement) | 2,698 | 2,599 | **3,281** | 2,329 |
| 36 | 1,285 | 1,663 | 2,118 | 781 |
| 48 | **291** | 1,317 | 1,784 | 270 |

**The thesis is half right.** TE is genuinely cheap - TE12 costs 2,329 against QB12 at 4,671,
less than half. But WR is the *most* expensive position at replacement level (3,281), above
QB, because receivers fill the flex slots too. "WR is cheaper than QB" does not survive
contact with this league's own market.

The more interesting structure is the **cliff**: QB48 is 291 against WR48 at 1,784. Elite QBs
cost about what elite receivers cost, and then QB depth simply vanishes. In superflex the
scarcity is in the tail, not the top - which is a fact about format, not about players, and is
exactly the kind of thing this tool could report.

**The value-insulation half remains untestable here.** Measuring decay needs the same players
tracked across time; a cross-section of current values is survivorship-biased, because backs
who fall off leave the valued pool entirely, so the ones still in it are those who didn't.
Doing it properly means snapshotting the market periodically - the first persistent state this
project would own, and a deliberate decision rather than a casual one.

## Why there was never any surplus (`find_surplus`)

`find_mutual_swaps` returned nothing for **36 consecutive team-reads across three leagues**.
Not a tuning problem - the quantity it depended on could barely exist.

Surplus was defined as players above `replacement_thresholds` and beyond `slots[pos]`. But
replacement level is *defined* as the Nth-best player leaguewide where N is every starting
slot at that position, so above-replacement supply equals demand **by construction**. Measured,
and it is exact:

| | QB | RB | WR | TE |
|---|---|---|---|---|
| starting slots leaguewide | 24 | 24 | 36 | 12 |
| rostered players above the bar | **24** | **24** | **36** | **12** |

So surplus could only ever be one team's hoard against another team's deficit, summing to
zero across the league. Only 3 of 12 teams had any. A *mutual* swap needs two teams to each
hold surplus the other is short at - a double coincidence on a near-zero quantity.

Deep flex made it worse: with three FLEX and a SUPER_FLEX, ten starters absorb almost
everyone above replacement, so "usable but not starting" is nearly empty before the slot
arithmetic even runs.

**Spare is now measured against the team's own lineup**: not in the projected starters, and
worth something in a trade.

Both halves had to change, and the second was caught only because the first wasn't enough.
Swapping the redraft bar for the dynasty one still left a top-24-leaguewide test - the same
zero-sum shape in the *value* check. On a real roster **only 2 of 18 receivers** cleared the
raw dynasty bar; a 3,039-value receiver missed by 242, and a 1,620-value young one his owner
rates a future starter was nowhere near.

The manager's framing is the correction: **replacement level is a win-now idea.** A player
below it isn't replaceable to a team that will be good in two years - he is a starter who
hasn't arrived. `clears_relevance_floor` already encodes exactly that, scaling the bar by what
kind of value the player carries (ascending clears at 25% of replacement, realised production
at 50%), which is why `_my_offer_pool` has always used it. Using it here makes the two
genuinely one concept instead of two that happened to agree.

| | teams with surplus | mutual swaps |
|---|---|---|
| original (redraft bar + slot arithmetic) | 3/12 | **0 across all three leagues** |
| lineup-relative + raw dynasty bar | 7/12, 4/12 | 4 |
| lineup-relative + relevance floor | **12/12** | **10 / 0 / 6** |

The quality question - does he actually help the receiving team - was always asked separately
by `_fills`, which is why a permissive candidate bar is safe here. `slots` is kept for
signature compatibility and deliberately unused; it encoded the zero-sum arithmetic.

**Three definitions of spare value now rest on one predicate** - not in the lineup -
specialising only where they must: `find_surplus` keys by position for matching against
another team's need, `stranded_starters` picks out the subset that beats the weakest starter
(capacity-blocked, lead with these), and `_my_offer_pool` adds tiering and the covered-starter
case. The shared predicate is what stops them contradicting each other.

**A known limit, surfaced by the fix.** Both remaining zero-swap leagues are legitimate, but
one shape is structurally unreachable: a team with *no needs* can never appear in a mutual
swap even holding exactly the piece another team wants. Two live examples. Mutuality is the
feature's premise, so this is a boundary rather than a bug - but a one-way "they hold what you
need and want value back" path would catch it.

## What the market source already gives us and we never read

`values/current` returns more per player than the four fields this project reads. Verified by
inspecting the payload rather than guessing endpoint names - which is how `tep` was found, and
guessing had already failed here (four 404s on invented history paths).

- **`redraftValue` is embedded in the dynasty response**, and is *identical* to the value from
  the separate `isDynasty=false` call - 192 of 192 players matched exactly, zero differences.
  The project makes that second HTTP request for data it already has.
- **`trend30Day`** exists but is **not** the decay signal the value-insulation thesis needs.
  Medians sit at or near zero for every position (QB −34, RB −2, TE −5, WR 0) with roughly
  half of each position negative. That is a month of market drift, not an aging curve; reading
  positional decay into it would be exactly the kind of plausible-looking mistake this file
  exists to prevent.
- **`maybeTradeFrequency`** measures how often a player is actually traded - a real liquidity
  measure, and the honest version of the "picks are easier to move" intuition that `pick_share`
  currently reports without weighting.

## Joining facts the tool already had (`_counterparty_fit`)

Persuasion targets were ranked purely on production-per-cost and never looked at the other
side of the table. On a live roster that put **Derrick Henry (1.54x) first, held by the one
team in the league with no needs at all** - unattainable - above **Saquon Barkley (1.37x),
whose owner had a *critical* QB need for the exact quarterback the asking team could not
play**. Stranded knew about the quarterback. `league_needs` knew about the critical need.
Persuasion knew about the back. Nothing joined them.

Two ways an owner is interested, and the second is the one that mattered:

1. **He is short at a position I can offer.** The obvious case, and the one that surfaces the
   Barkley trade.
2. **He should be converting aging production, and I hold what he'd convert into.** A team
   contending now *and* tilting ascending - the `_cliff_case` shape - has no positional hole,
   but wants value that scores this season *and* is still there later. Reading "he needs
   nothing, so there is no deal" misses the trade that is the entire reason his aging starter
   appeared on the list. The manager put it exactly: *"shiv may not need a position strictly,
   but could still get off his old players with generally non-cliff-facing assets."*

**Annotation, not ranking.** Cheap targets from teams already selling need no persuasion at
all, and re-sorting this tier by fit would push the low-friction options down in favour of a
bigger ask. Two orderings inside one list is a mistake this module already made once, when
trade activity outranked value.

Three corrections landed the moment a manager read the first output, and each was the tool
asserting something it had not checked:

- **"Still there later" was tested with `bucket != "declining"`**, which passed a 28.7-year-old
  receiver **0.3 years** from his own cutoff - offered as value that would last two seasons.
  `years_to_decline` is the number that sentence actually claims, so it is now the test, and
  it is carried on every player entry rather than only in `roster_detail`.
- **"Scores this season" admitted anyone above zero**, padding the list to three names with a
  33-redraft tight end. `_my_offer_pool` already separates "core piece - above replacement"
  from "depth - a sweetener not a centerpiece"; only the first is worth restructuring around.
  With both fixes the list is exactly the two players the manager named unprompted.
- **`cost_note` contradicted `why_it_fits` printed beside it**, telling a reader that filling
  an owner's *critical* need meant "persuading them to change direction". Where the owner has
  a hole this team can fill, the trade serves his existing plan and the note now says so;
  where there is no hole, the pivot framing is right and stays.

And a fourth of the same kind: `from_owner_trades: 0` was a bare integer in the dict an agent
reads, while only the CLI printed "NEVER TRADES". That is precisely the `{"diff": -11}`
failure this project already documented - an unlabelled number gets a meaning invented for it -
so a zero now carries `never_trades` explaining what it does and doesn't imply.

### Why this was a tool bug and not a prompting problem

Worth recording, because it cuts against the temptation to fix things in the system prompt.
The join above was first made **by a human and an assistant sharing a very long session**,
with every intermediate result in front of them. A single agent run does not have that. It
calls a handful of tools, gets a handful of JSON blobs, and would have to notice unprompted
that a stranded QB on one roster answers a critical need on another whose owner holds the
back it wants - across three separate tool results, none of which references the others.

That is exactly the reasoning a smaller or cheaper orchestrating model will not do reliably,
and this project's stated position is that a deterministic Python check beats a prompt
instruction every time. **If two facts have to be combined to make a recommendation
actionable, the combining belongs in the tool.** The prompt's job is to report what the tool
found, not to rediscover it.

## Auditing the advice, not the arithmetic (`analysis/audit.py`)

84 unit tests never caught a single real bug in this project. That is not a failure of the
tests - they check that functions compute what they were written to compute, and they do.
Every real bug was a **wrong recommendation produced by correct arithmetic**: a buy list
burying the second-best available back beneath one producing a quarter as much, a tier whose
bar no tight end could clear in any league, a feature returning empty for 36 consecutive
teams, a suggestion that a manager acquire two players he already owned. Correct functions,
indefensible output.

`audit.py` checks the output instead, against **real leagues**, and every check is derived
from a bug that actually shipped. That is the entry requirement: no speculative invariants,
because an audit nobody trusts gets muted.

| check | the bug it comes from |
|---|---|
| never recommends your own players | a rebuilder searching rebuilders included itself |
| best available is surfaced | trade activity outranked value, hiding a 1,883-redraft back |
| no tier is structurally unreachable | an absolute 1.0 bar sat above the entire TE pool |
| claims match the data | a player 0.3 years from his cliff sold as "still there in two" |
| every window gets what applies | depth and stranded ran only in the buy branch |
| coverage | a block empty across every team in every league is a dead feature |

**It had to be calibrated before it was worth anything.** The first run reported 12 problems,
and the first six were false: it compared against every player on a rebuilding roster, so it
"failed" on Jahmyr Gibbs, Josh Allen and Ja'Marr Chase - all cornerstones, which `_buy_path`
deliberately never searches because no rebuilding team sells its elite young core. An audit
calibrated against a pool the code was never meant to reach reports noise. Pointed at the
actual candidate pool, 12 problems became 3.

**And the 3 were one real bug, found on its first honest run.** For a `Push` team, "is
declining" was the *hard first sort key* - so every declining player outranked every prime
one regardless of production. A live team with a WR need was shown **Jauan Jennings at 70
redraft above Chris Olave at 3,439**, a 49x gap, with the default cap of three then hiding
Olave, Garrett Wilson and Jaylen Waddle entirely.

The reasoning behind declining-first was about **price per unit of production**, which is a
real effect and belongs in `production_per_cost` - not in an absolute ordering. Age now breaks
ties *beneath* the thing being bought, and at equal production the declining player still wins
because he is the cheaper asset. One change, and all three audit failures cleared.

Not part of `pytest`: it needs the network, and `tests/` is free and offline by design. The
two layers answer different questions and both are needed - the unit tests would have caught
none of this, and the audit would catch none of the boundary conditions they guard.

## Runway, not buckets, wherever a boundary decides something

`age_bucket` is a **discretization**, and treating it as the answer failed three separate
times in one day - a receiver 0.3 years from his cutoff offered as value that would "still be
there in two"; an elite back **0.1 years** from his classified as a franchise cornerstone
while the same player one month older would have been a sell candidate; and that same boundary
hiding a 1.2-year starter from the conversion path on the one team already being told to
convert. Nobody's value falls off a cliff on a birthday.

`MIN_MEANINGFUL_RUNWAY = 2.0` in `team_values` is now the single definition of "has a future
worth building on", shared by every caller that used to ask `bucket != "declining"`. Buckets
stay for the coarse questions - what kind of value is this, how is a roster trending - where a
category is genuinely what's wanted.

**Cornerstone now routes on runway.** A cornerstone is a piece to build several seasons
around, so the test is whether he has several seasons. Windows did not move (trajectory reads
buckets, not cornerstones), but the reachable pool changed a lot - a pushing team's RB targets
went from 1,850 / 1,139 / 688 redraft to **4,314 / 3,284 / 2,570**.

**It overshoots at the top, and that is a real cost.** Josh Allen (10,415 dynasty, 0.8 years
of runway) is now "sellable", along with Lamar, Jefferson and Lamb - because runway alone
ignores magnitude, and a player 2.4x the cornerstone bar is a franchise asset whatever his
birthday. The audit came back clean and those names surface only as *targets* for teams that
need them, never as casual offers, so no bad recommendation results. Left as-is rather than
adding a second tuned threshold to rescue it, but recorded as a known distortion.

**What the annotation fixes instead.** The manager's own reaction to seeing Allen listed was
that these are "reasonable but harder to get and less production value efficient" - which is
two measurements, neither of which the buy list carried. `production_per_cost` (already
reported by the persuasion tier, absent here) and `cost_share`, a player's price as a
percentage of everything this team could put on the table:

| target | dynasty | prod/cost | cost share |
|---|---|---|---|
| Chase Brown | 4,069 | 1.06 | **25%** |
| Josh Allen | 10,415 | 1.00 | **67%** |

67% of a roster's entire tradeable value is technically available and practically not, which
is a different statement from "expensive" and the one a reader needs.

## Leverage: what a team could become (`team_state.leverage`)

The window model answers *what should this team do with the roster it has*. It had nothing
to say about **how much rope a team has to change that roster**, and collapsing both into
one label produced a badly wrong read.

The case came from a sports-modelling data scientist describing his own team, in a league
neither development roster resembles. His roster ranked **9th of 12 in starting production
and 2nd of 12 in total tradeable value** (every player plus every pick). The tool labelled
him `also-ran` / `Rebuild`, which a reader hears as "bad". His own summary was that he
doesn't expect to win, but if the season opens well he has the assets to convert into a
contender - an option with real value, priced at zero by the model.

| team | production rank | asset rank | reading |
|---|---|---|---|
| dkwnsepw | 12 | **1** | convertible |
| jwall567 | 9 | **2** | convertible |
| ryann28 | **1** | 8 | mortgaged |

The mirror falls out of the same comparison and is just as real: a team 1st in production and
8th in assets is winning now on borrowed time with nothing left to reload from.

**Not a fifth window.** `window` says what to do with the current roster; `leverage` says how
much capacity there is to change it. One number cannot carry both, and making leverage a
window would force it to - the same reasoning that kept `Contend` singular for a team with
two live paths. Additive, so nothing downstream that reads `window` changes.

**Tertiles, not a tuned gap** - top third on one axis and not the other, matching how
contention and trajectory are already cut. Teams whose two ranks agree get nothing, which is
most of them: 2 to 4 flags per 12-team league across three real leagues.

**Composition is reported, not weighted (`pick_share`).** Two teams with the same
`asset_value` are not equally convertible. A pick is **position-agnostic** - it fits any
deal, where a surplus of young receivers needs a partner who happens to want receivers - and
it is **value-insulated**, carrying none of the age, injury or lost-role decay a player does.
So the same number held in picks converts more easily than the same number held in players.

By *how much* is not calibratable here, and a guessed multiplier buried inside the ranking
would be worse than an honest number printed beside it. The observed spread carries the point
unaided: **3% to 41%** across three real leagues, with the most mortgaged contender at the
bottom and the deepest rebuild at the top. The owner's own two teams sit at 4% and 10%, which
is his unprompted "I don't even have my future first" appearing as a measurement.

**Picks are priced flat here, deliberately.** `owned_picks` can price a pick by the window of
the team it originated from, and the window is what this measure helps describe - letting
that in would make the label feed its own input.

Independent validation across the other two leagues: it flagged the owner's own teams as
`mortgaged` in both, matching his unprompted "I'm already all in at this point", and flagged
a rival as `convertible` that he had separately described as able to push now but more
efficiently next season.

## Rebuilding rosters, and five things only a stranger's league exposed

Both development leagues are win-now teams owned by the same manager. Running a third
league - a rebuilding roster, read by someone who models sports for a living - broke five
things at once, four of which had shipped earlier the same day. Worth recording as a
method: **the bugs live in the states your own data never enters.**

### 1. Stranded production (`roster_needs.stranded_starters`)

The roster held **four startable quarterbacks in superflex with two QB-capable slots**. Its
QB3 priced at 4,880 of current production sat on the bench while a receiver producing 420
started - and that QB3 alone out-produced the team's entire starting RB room by more than
three times. Every number was already computed. Nothing put them next to each other, so he
appeared as an ordinary trade chip in a list sorted by dynasty value.

`stranded_starters` returns bench players who beat the **weakest starter** and are held out
by positional capacity alone. Capacity, not quality, is the distinction: these are not
surplus because they're mediocre, they're surplus because the lineup physically cannot field
them, so their entire value to this roster is what they fetch. True in every window - a
contender converts one into the position it's short at, a rebuilder into futures.

It immediately found the same shape on a league already examined all session: the owner's
own QB3, whom he had independently named as his second-biggest trade piece.

### 2. Sell lists sorted by dynasty value

The same bug fixed for buy targets earlier that day, untouched on the selling side. A
31-year-old QB priced at **1.36x** current-to-dynasty - the market paying for this season and
writing off the rest, on a team with no this-season - ranked *below* a 25-year-old receiver
who is exactly what a rebuild should keep. `situational` now sorts by that ratio, which is
`_persuasion_targets`' buy-side signal read from the selling side. Unpriced players sort
last: unknown, not zero.

### 3. Advice that contradicted the roster

`window_note` told a team with **0% declining production** and an empty sell list to "sell
what's declining while it still has value." Keyed on the window, not on the roster it was
describing - the tell of a template. `REBUILD_NOTHING_DECLINING` covers the young-rebuild
case, and the teams most likely to hit it are the ones furthest into a rebuild.

### 4. Depth and stranded never ran for rebuilders

Both were computed inside the buy branch, and `Rebuild` returns before it. The team with the
**worst RB room in its league** and six qualifying cheap bodies available got neither. Both
now run before the window dispatch.

Cheap depth is arguably worth *more* to a rebuilder, which the original placement had exactly
backwards: a moonshot back is one injury from being a real asset and costs a late pick to
hold. That is a rebuilding team's cheapest source of upside.

### 5. `_depth_adds` recommended the team its own players

It searched every rebuilding roster including the asking team's. Invisible until the asking
team was itself a rebuilder - a state neither development league could produce.

### Unpriced replacements (`roster_needs.replacement_is_unpriced`)

Not a rebuild issue, but found the same way. Exposure said losing this roster's TE cost
3,848 - **100%** of its TE production - with a rostered NFL tight end behind him carrying
`redraft_value = None`, which the arithmetic reads as zero. Redraft coverage runs out around
the 30th player at a position while dynasty rosters keep going, so this is structural on deep
rosters: **80 of 153 receivers** in that league's pool have no redraft price at all. The
figure is unanswerable rather than wrong, and the note now says which.

## Depth as a third state (`roster_needs.would_start_if_one_out`, `_depth_adds`)

Needs are binary - a position is a hole or it's fine - and that left depth invisible. A team
starting **five** receivers (3 dedicated + 2 flex) and one starting three look identical at
WR once both are filled, though only one is a single absence from an empty slot. Byes are
certain and injuries close to it, so a body who steps straight in has real value at a
nominal price, and nothing in the model could say so.

**Measured by refilling, not by counting.** `would_start_if_one_out` removes the *weakest*
current starter at the candidate's position - the marginal lineup spot, the same choice
`_injury_drop` makes - adds the candidate, and refills optimally so flex eligibility is
respected. Counting bodies cannot distinguish the two WR rooms above; a real refill can.

### Strictly better holdings (`find_value_upgrades`)

Someone on another roster who produces **more** this season than one of my starters **and**
costs **less** in dynasty value. Acquiring him raises the lineup and frees trade capital at
once, and the starter he replaces drops to depth - which is where a below-replacement starter
belongs.

**The external twin of `find_efficiency_swaps`.** That one compares a starter against my own
*bench*, so it returns nothing for a roster whose bench produces nothing - exactly the roster
that most needs the answer. On a live Push team it found zero swaps while that lineup started
a receiver producing 925 and carried two upside-priced starters. Its owner named the move it
couldn't see: *"I should definitely be looking for some way to trade those two guys and have
more redraft value in my lineup than I do now... I want Robinson and Sutton to be the depth at
a good price, not the starters."*

**Strict dominance on both axes**, so there is no threshold to tune and no lateral move can
qualify. Same-position pairs only, for the reason `find_efficiency_swaps` documents: the
redraft and dynasty scales are not normalized to each other, and only a same-position
comparison cancels that out.

**One line per upgradeable starter, not a ranked list with a cap.** The finding is
two-dimensional - production gained *and* value freed - and ranking on either axis hides
winners on the other. Sorted by production and capped at six, it dropped the exact swap that
roster's owner had already identified himself: a tight end worth **+233** of production but
the largest value release on the board at **1,073**. Keyed by the starter being replaced, it
answers the question actually being asked - *which of my starters can I do better than, and by
whom* - and needs no cap, since the lineup bounds it.

**It prices nothing.** One player against one player is the whole claim. Value is not additive
across players, so there is no package here - see "discovery tool, not a fairness calculator"
above. Candidates are usually older, which is *why* they are cheap, so the note tells the
reader to weigh the age against their own timeline before paying.

**Which bar makes someone "only depth" depends on what the asking team is doing**, and it is
the same two-metric split `replacement_thresholds` documents: *filling a lineup is a redraft
question, holding a lottery ticket is a dynasty one.*

| asking team | bar | why |
|---|---|---|
| Push / Contend / Middling | replacement-level **production** (`start_thresholds`) | above it he is a real fix, not insurance |
| Rebuild | the **dynasty** trade-value floor (`trade_thresholds`) | production now is beside the point - the value is a body who inherits a job and *becomes* sellable (`DEPTH_NOTE_REBUILD`) |

Testing dynasty value for a lineup-filler answered a question nobody asked. David Montgomery
- 2,145 dynasty, 1,779 redraft against RB replacement of 1,708 - was filed "never worth a
real asset" on a roster whose second starting RB produces **633**. He is a +1,146 upgrade to
the weakest slot in that lineup, and calling him insurance was simply wrong. The roster's
owner made the call on the split: "when trying to fill your roster it should be redraft value
... rebuilding teams don't care about filling roster yet."

The case that forced this list into existence missed the bar by **3 dynasty points** on a
roster its owner described as two deep.

**Against the full threshold, not `clears_relevance_floor`** - fixed after a live spot check.
The relevance floor is *tiered*: a production-priced player clears it at **half**. Testing it
here therefore opened a crack between the two lists instead of partitioning them. On the
roster with XFL 2's second-worst RB room, Tony Pollard (1,493) and Jaylen Warren (1,948) both
cleared half of RB's 2,576 and were dropped as "the buy path already owns him" - while
`_buy_path`'s `max_per_position` cap of three ranked them 4th and 5th on production and never
showed them either. The cheapest and most obviously gettable help in the league was invisible
in both lists. The team's owner named those two players unprompted as what he expected to see
first, which is how it was caught; the same cap that once hid Chris Olave for being *prime*
was hiding these two for being *cheap*.

This mattered more than one roster: `depth_adds` carried **22** entries across all three
leagues before the fix and **213** after, against a ceiling of 216 (`DEPTH_LIMIT` of 6 x 36
teams). It had been a near-dead feature. Near-saturation is acceptable here precisely because
the tier is weak by design - six cheap insurance bodies per team is a menu, not a
recommendation - but it is the reason the note leads with "DEPTH, NOT NEEDS".

Deliberately a *weak* signal. `DEPTH_NOTE` tells the caller not to overpay, because the
failure mode is paying a real asset for insurance, and the tier is capped at
`DEPTH_LIMIT = 6` and sorted cheapest first - at this level price is the entire point.

The live results are the validation. In one league the tool returns nothing for its owner,
correctly: the candidate he had in mind is *fifth* at his position behind two bench players
who outproduce him, so he would never see the field. In his other league - the one he
independently described as "one injury away from disaster" - it returns two backup
quarterbacks in a superflex format. Different answers from the same rule, both matching what
the manager already believed.

One limit, stated because it is the obvious next question: this models **one** absence. A
thin room whose players are individually injury-prone is a different risk, and that needs
the per-player injury history logged under future work.

### `is_starter` is a claim about value, not intent (`starter_caveat`)

The value-derived lineup marks a best-eleven for every team, including one openly tanking -
where the "starter" is just its least-bad player at that position. Left unsaid, a buy target
reads as "you'd have to prise away someone he's relying on", which inverts the actual
conversation: those are precisely the players a rebuilder most wants to turn into picks.
Buy targets who start for a `Rebuild` team now carry `starter_caveat` saying so. Presentation
only - no logic reads `is_starter` differently, because as a *value* claim it was never wrong.

### Both paths for a contender still rising (`_conversion_candidates`)

The tilt that decides whether *another* team's aging starter is gettable decides the same
thing about your own. A contender whose production is still tilting ascending has two live
plays, and reporting only one is a false choice:

- **Stack** - buy more current production. It already has the strongest lineup, so the
  marginal win is cheaper for it than for anyone else, and nothing is given up on.
- **Convert** - move the aging starters into value that matches the seasons the rest of the
  roster is built for.

`window` is deliberately **not** made plural for these teams. Making it a list was the more
"honest-looking" option and the wrong one: a window says *whether* a team competes, and this
one competes on either path - the choice is about how. Plural windows would also have
touched `_buy_path`, `_pivot_path`, the agent prompt, the MCP tool description and an eval
asserting the exact string, to express something none of them are asking about. So the block
is additive, exactly like the `Middling` push/pivot split it copies.

It reuses `_cliff_case` rather than reimplementing the test. If the rest of the league is
told a manager's 32-year-old RB is the one piece worth calling about, that manager must be
told the same thing in the same terms - two rules would eventually disagree, and the
disagreement would surface as the tool contradicting itself between two questions.

On the live league this fires for one team, listing the two players its owner had already
identified by eye as the ones to consider moving.

### Last season's results (`prior_season.py`)

The current record is useless in the preseason (everyone is 0-0), which is why record was
written off entirely - but the *previous* season is finished and sitting there:
`get_season_chain` walks `previous_league_id`, the prior league carries final
`wins`/`losses`/`fpts`, and `winners_bracket` names the champion.

Nothing in the current-season data separates the two contenders holding elite aging RBs
convincingly - trajectory splits them only -3 to -11. Last season adds the missing reason:

| | 2025 | points for | trajectory | verdict |
|---|---|---|---|---|
| rjl22 | **won the title** (10-4) | **2,260 - most in the league** | steady | not surfaced |
| kierankieran | 9th (5-9) | 1,864 | falling | surfaced, *and* "this core hasn't won" |

Last season's role here is now purely additive. It supplies the second sentence of
kierankieran's case - a team that missed with the roster it still has is more open than the
standings suggest - and it no longer *stops* anything, since the champion veto it used to
power was removed in favour of the window tilt above.

**Gated on measured roster continuity**, because a result describes a *roster*. Matched by
`owner_id` across the season chain and measured on current starting production, this league
retains 83-100%, and both teams above return 10 of 10 starters at 100%. That will not hold
generally - continuity is near-total because this is a dynasty preseason before the rookie
draft, and is zero by construction in redraft - so `MIN_CONTINUITY` is a required gate even
though it currently never fires. Its exact value (0.6) is an uncalibrated judgment call:
there is no observed case near the boundary.

**Deliberately kept out of the window classification.** "This manager just won and will run
it back" is a behavioural inference about a person, not a fact about a roster. It belongs
in how a suggestion is ranked and framed, never in whether a team is a contender - that is
measured, and stays measured.

- **Choosing a lane should account for how many others have chosen it.** Contending is
  worth more when almost nobody else is contending, and rebuilding is worth more when you
  own your pick and last place is uncontested. Both are supply effects the current model
  can't see: `contention` and `trajectory` are measured per team, but the *value* of a
  window depends on the league-wide distribution of windows. Related: `Middling` teams are
  **optional** sellers, not motivated ones - they can pivot if the price is right but have
  no need to, which should raise what they'd demand rather than putting them in the same
  bucket as a committed rebuilder.
- **Playoff spots are in the Sleeper settings and unused.** `league["settings"]` carries
  the number of playoff teams, which is the real definition of "in contention" - being 6th
  of 12 means something very different in a 6-team playoff than a 4-team one. Mid-season
  this changes the advice materially: a team close to the last spot should usually try to
  sneak in *without* mortgaging the future, because making the playoffs at all buys a real
  chance at the title. Needs the *current* season to have started before it can gate live
  advice.
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
- ~~**No conversation memory - the agent is single-turn.**~~ Resolved - see
  "Conversation sessions and UI" above (`agent/sessions.py`). Kept as a pointer rather
  than deleted because the *sequencing* call is the interesting part: this was
  deliberately deferred until a UI existed, on the grounds that the right shape of
  session handling depends on what the client turns out to be. That held up - the final
  design (client-generated session ids, so an expired session degrades to a new
  conversation instead of an error) is a direct consequence of knowing the client was a
  browser tab.
- **Window *length* isn't modelled - only window direction.** A team that's comfortably
  the best roster in its league can afford players who won't fall off immediately,
  stretching contention across several seasons rather than maximising one. A team that's
  barely contending can't - it has to buy the cheapest current production it can find and
  accept the cliff afterwards. Both currently read as "Win-Now" and get identical advice.
  This is a real distinction and a luxury most teams don't have, which is exactly why the
  tool shouldn't hand it out uniformly.

  Hard for a specific reason: it needs a measure of *how far ahead* a team is, not just
  which direction it leans. Starter-value rank is a weak proxy (rank 1 of 12 could be a
  runaway or a coin flip), and the honest version probably needs the season record plus
  some notion of the gap to the next-best roster - the same in-season data the
  record-based limitations above are waiting on. Deliberately not half-built: a
  confident-sounding "you can afford to stay good for three years" derived from a
  starter-value rank would be exactly the kind of unfounded claim this project keeps
  finding and removing.
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
