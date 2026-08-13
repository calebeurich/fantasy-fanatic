# Logic reference

Every heuristic, threshold, and "why" behind this repo's analysis - the material the
eventual chatbot grounds its explanations in. Update it in the same change that adds or
adjusts a heuristic. This is a reference to the CURRENT rules, organized by concept;
the full history of how each rule was arrived at lives in git (`git log` reads as a
narrative on purpose).

## The model in one page

1. **Two currencies.** Dynasty value prices production plus future years; redraft value
   prices this season alone. Confusing them is this project's most common bug, and the two
   scales are unnormalized - never compare or ratio them absolutely; compare ranks within a
   position, or pairwise at the same position where the skew cancels.
2. **Three states, each with flavors.** Contending (`Push`/`Contend` - only the clock
   differs), Middling (`rising`/`falling`/`steady`/`convertible`), Rebuilding
   (`ascending`/`stalled`/`convertible`). Contenders and rebuilders complement each other
   in trades; same-state pairs don't; Middling is a real position, not an unmade decision.
3. **Age is a distance, not a category.** `years_to_decline` (runway to the player's own
   curve cutoff) is the measure; buckets are only its sign. Anywhere a boundary decides
   something, use the runway. Curves differ in width per position, so "two years of
   runway" means different ages at RB than QB.
4. **Value is not additive across players.** Nothing here prices a package; every
   comparison is one player against one player, and the tools are built so that stays
   structural rather than a prompt instruction.
5. **Everything is league-relative** (tertiles, percentiles, replacement levels), so
   nothing is a constant tuned to one league. The cost is hard breakpoints on continuous
   measures: a value refresh can flip a label. Known, and the next behavioral fix.
6. **A recommendation needs a plausible counterparty.** Every suggestion carries why the
   other owner would say yes, in his own window's terms.

## Data sources and why

- **Sleeper API** (`sources/sleeper.py`): public, free, no auth. League settings,
  rosters, users, transactions, traded picks.
- **FantasyCalc** (`sources/fantasycalc.py`): dynasty + redraft values, ages, pick
  values. Chosen over KeepTradeCut because KTC's ToS forbids scraping or reproducing
  their values; FantasyCalc has a genuine public API.
- **nflverse** (`sources/contracts.py`, `player_roles.py`, `injuries.py`,
  `nflverse_ids.py`): contracts, usage stats, weekly availability - the community
  open-data redistribution, not scraping OverTheCap directly. Keyed by `gsis_id`;
  `nflverse_ids.gsis_to_sleeper()` is the crosswalk.
- **Degraded feeds** (`sources/degraded.py`): both nflverse call sites fall back rather
  than crash (a role is an override; absent means no adjustment) but never quietly -
  stderr warns the author, `degraded.record()` reaches the ANSWER: the MCP layer stamps
  `data_gap` on every tool result and agent rule 12 makes the model say it in one
  sentence. With roles missing, every curve is a position default - that once moved a
  QB's runway from 6.2 to 2.1 years and reversed a sell recommendation, so a degraded
  run's runway advice is materially less precise.

### Source caching (`sources/cache.py`)

~20 lines of TTL memoization, not a library. The TTL split is a correctness decision:
stale rosters give confidently wrong advice, so live state (rosters, transactions,
traded picks) gets 60s - just enough to collapse one question's 2-4 tool calls into one
fetch - league config 10m, FantasyCalc values 1h, nflverse reference 6h. Measured win:
`classify_league` 6.85s cold / 0.00s warm. The cache lives in the MCP server subprocess;
persistent sessions (below) are what keep it warm across questions.

## Shared league context (`analysis/league.py`)

One `LeagueContext` built per league (TTL-cached), because five modules had copy-pasted
the same setup and kept picking the wrong near-twin concept. The disambiguation table:

- `needs_slots` folds SUPER_FLEX into an extra QB - "how many of this position must I own".
- `lineup_dedicated` + `lineup_flex` model the real lineup - "who actually starts".
- `start_thresholds` (redraft): can this player start? `trade_thresholds` (dynasty): is
  he a real chip? Conflating them once marked a team with three startable WRs critical.
- `starters` is THE definition of a lineup: value-derived (`projected_starters`), never
  Sleeper's current-week snapshot, which is meaningless preseason (it once listed one QB
  for a superflex team, so the QB2 was offered away as spare parts).
- Owner lookup matches on handle AND team name, normalized to letters/digits only -
  team names are free text full of characters nobody retypes ("Where's the Lamb
  Sauce???" stores a curly apostrophe).

## Format detection and FantasyCalc's parameters (probed, not assumed)

`sleeper.describe_format`: dynasty = Sleeper's `settings.type == 2`; superflex =
`SUPER_FLEX` in `roster_positions`; TEP maps to FantasyCalc's three bands.

What the FantasyCalc parameters actually do, measured by pulling and diffing:

- **`numQbs` has exactly two settings** - 1, and >=2 (byte-identical for 2, 3, 0).
  Superflex and 2QB are one market. The format move is four per-position scalars:
  QB x1.883, RB x0.923, WR x1.007, TE x1.101, constant across each whole position;
  only picks genuinely vary (x1.026-1.148). Consequences: within-position comparisons
  are format-independent (the scalar cancels), cross-position comparisons rest entirely
  on those four numbers, and superflex QB scarcity is NOT steepened - the real cliff at
  QB12-13 is invisible to a flat scalar, so the marginal QB2 is probably worth more than
  these values say.
- **`ppr` is a flat per-position scalar and nearly invisible**: 0->1.0 PPR moves RB
  x0.9943, WR x1.0180, TE x1.0232, QB x1.0114. It cannot distinguish a receiving back
  from an early-down back (McCaffrey and Henry move identically), which is what full PPR
  most changes. Nothing to fix at this layer - there is no per-player PPR data to apply,
  and a guessed multiplier would have nothing to calibrate against. Recorded so the
  passthrough isn't mistaken for precision.
- **TE premium is applied by FantasyCalc in the browser, not the server** - the API 404s
  on every `tep` value except `none`, and their site rescales the TE column client-side
  by a flat multiplier. `fantasycalc.TEP_MULTIPLIER` replicates it: TEP+ x1.1490, TEP++
  x1.2900, TEs only; bands verbatim from their UI (Off <=0.25, TEP+ 0.5-1.0, TEP++
  start-2-TE or >1.0). Both real leagues here score 0.5, so every TE was ~15%
  undervalued before this. Caveat: a replicated client transform can drift silently -
  `python -m sources.fantasycalc` prints the multipliers for a one-command check.
  (An earlier note here called TEP unfixable after reading the documented parameter
  list; watching what the site actually sends found both the mechanism and the
  calibration. Checking documentation and stopping was the mistake.)

## Format support gate (`analysis/format_support.py`)

`assess_format` returns `unsupported` (not dynasty - the concepts mean different things
there, so the agent refuses rather than degrades), `degraded` (< 8 teams - percentile
math gets noisy; not yet validated against a real shallow league), or `full`.

**Structural limitation:** real leagues carry house rules no API exposes. Confirmed live:
XFL 2 awards the 1st overall pick by lowest full-season BEST-BALL score, not standings -
under that rule tanking a lineup doesn't even work, and `team_state`'s tanking note
assumes reverse-standings order. Capturing freeform per-league rules and letting them
caveat downstream logic is an unbuilt design, and more of these gaps should be expected.

## Age curves and runway (`team_values.py`, `sources/player_roles.py`)

Per-position breakpoints (ascending below / declining at-or-above), dynasty-community
heuristics: QB 26/34, RB 24/27, WR 25/29, TE 25/30. Usage-based overrides from measured
season data (nflreadpy), thresholds picked from natural gaps in the real distribution:

- `rushing_qb` (carries/game >= 5.0, not an elite passer): 26/32
- `dual_threat_qb` (>= 5.0 carries AND elite passer): 26/34 - the point is the absence
  of the rushing discount, not a bonus; elite passing survives the legs
- `pocket_passer` (elite passer, not a runner): 26/38 - not 40, because the curve should
  turn before the market does; the top passing-EPA tier over three seasons is all pocket
  throwers, so the tag is earned by production
- `pass_catching_rb` (targets/game >= 4.0): 24/29

"Elite passer" = top third of passing EPA/game over three seasons. EPA over CPOE
despite CPOE being better isolated, because CPOE penalises aggressive downfield throwing
(Stafford: 5.33 EPA, -0.47 CPOE). The pocket/rushing spread (38 vs 32) is the only
constant in this project changed on an outside opinion - a sports-modelling data
scientist argued the pocket end was too pessimistic - and it was changed because the
measured data backed it: the top three-season EPA tier (Goff 6.87, Purdy 6.62,
Stafford, Burrow, Mahomes) is all pocket throwers. `dual_threat_qb` exists because the
rushing discount assumed nothing to fall back on: Allen (6.05 EPA) and Lamar (5.25)
clear the elite-passer bar while Hurts (2.99, 8.5 carries/g) does not - visibly
different in the same data.

**Runway (`years_to_decline`)** is the distance to the player's own cutoff, negative past
it - the single definition of "has a future" (`MIN_MEANINGFUL_RUNWAY = 2.0`, the horizon
claims like "still there later" actually make). Buckets are its sign and nothing more;
age ordering can invert runway ordering (Goff 31.8 -> 6.2 years vs Hurts 28.0 -> 4.0),
which both a human expert and the agent once got backwards. `INSIDE_FINAL_YEAR = 1.0` is
the seller-side question - is he at his own edge - kept separate because on the RB curve
the buyer's 2.0 horizon means "any RB over 25".

**Contract outliers** (`find_outliers`): declining-by-age but 2+ years remaining AND
guaranteed money > 0 (461 of 1,695 active contracts guarantee $0, so years alone
self-refutes). Display-only by design - the market's own pricing already carries "how
long does he have left" continuously, while a contract encodes what a team believed at
signing, possibly years stale.

**Open decision - contract signal vs age curve:** whether NFL contract years should feed
decline math at all. Evidence against: see the display-only reasoning above; revisit only
with calibration data.

## Pick valuation (`team_values.owned_picks`, `pick_capital`)

Two future classes only (`FUTURE_DRAFT_YEARS` - further out is too speculative and
rarely traded). Ownership resolves through `traded_picks`; a pick not listed is still
its original roster's. Picks are priced by the ORIGINAL owner's window where the market
publishes tiers - a rebuilder's 1st is an early pick whoever holds it, and Early/Late
differ by ~2x (2027 1st: 4,487 / 2,955 / 2,263 vs flat 2,853). Only the next class has
tiered prices (a window predicts one season's finish, not two); later picks keep the
flat round value with `slot_basis` saying so. `pick_capital` is a sum over `owned_picks`,
deliberately not a second ownership implementation. `pick_equivalent` maps a player value
to the nearest pick ("about a 2027 3rd") because a raw number is hard to feel.

`team_state.classify_league` prices pick capital WITHOUT the window keying, because the
window is what that measure helps produce - the label must not feed its own input.

## The dynasty/redraft measure (`priced_for`, `now_premium_bar`)

The relationship between the two prices is read via **positional rank gap**
(`priced_for`: rank within position on each scale, compare, ±10% of pool size decides
later/now/aligned). The raw ratio cannot answer this: half the dynasty pool has no
redraft price, measured ratio medians are QB 0.36 / RB 0.05 / WR 0.00 / TE 0.00, so 1.0
is nowhere near neutral - it once mislabelled 111 entries against 32. Rank gets ten
known-answer players right where the ratio got six wrong. Known limit: ±10% of a 29-man
TE pool is coarser than of a 75-man WR pool.

`now_premium_bar` (percentile of the ratio WITHIN each position, 0.9) survives for one
job: picking the price *sentence* in `_cliff_case`/`_conversion_candidates` (discounted
to harvest vs pure window mismatch). It gates nothing, and its old absolute form sat
above the entire TE pool - treat any absolute threshold on these two scales as a bug on
sight.

### Alternatives tested and rejected - do not re-derive

- **Replacement multiples** (value / replacement on each scale): the denominators are in
  different currencies whose ratio varies by position, so it reintroduces the distortion;
  called known cases backwards.
- **Sleeper `search_rank`**: format-agnostic, so it under-ranks the scarcest asset in
  superflex (median QB rank move +43 vs a superflex value ordering). FantasyCalc already
  takes `numQbs`; our values are format-correct and it is not.
- **Positional scarcity inflating a TE-heavy lineup**: tested three ways; normalizing
  moved the TE-heavy roster UP (Bowers is 9.6x TE replacement). The scarcity premium in
  that league is at QB. The allocation thesis has no support in these numbers.

### The axis is misnamed (backlog)

`starting_production` is a sum of redraft TRADE VALUES of projected starters, not points.
Defensible measurement (best available, format-correct), overclaiming name - it is the
most load-bearing axis in the project and its notes say "production" about market value.
Rename to "lineup market value" or similar.

## Team windows (`analysis/team_state.py`)

Two measured axes, each cut into league tertiles so no constant is tuned to one league:
**contention** = the projected lineup's total REDRAFT value ranked against the league
(dynasty value prices future seasons - swapping to redraft moved teams 4-5 places on real
leagues, including a roster that was "old but genuinely close" being told it was
mid-pack), and **trajectory** = ascending minus declining share of that production
(production, not dynasty value, which would double-count - ascending players are PRICED
on the growth being measured). This replaced an age-only model plus two patch flags that
were crude proxies for the missing contention axis.

| state | flavors | wants | can spare |
|---|---|---|---|
| **Contending** | `Push` (clock) / `Contend` (none) | production now | future years |
| **Middling** | `rising` / `falling` / `steady` / `convertible` | information | nothing freely |
| **Rebuilding** | `ascending` / `stalled` / `convertible` | future years | production now |

- `Push` = contender + falling: waiting costs value, buy production, spend picks.
  Pivoting stays available but returns poorly. `prefer_production` ordering is Push-only.
- `Contend` = contender, no clock: never pay a premium. A Contend team tilting ascending
  additionally gets `conversion_candidates` + `choice_note` (stack vs convert - a choice
  about HOW to contend, so `window` deliberately stays singular).
- `Middling` is a window, not a leftover (`Rebuild` used to be the else branch and told a
  team with the league's best QB room to sell decline it didn't have). Trajectory sets
  the note only: rising = patience is free; steady/falling = waiting buys information.
- `Rebuild` = bottom third. A young rebuild with `dec_pct == 0` gets
  `REBUILD_NOTHING_DECLINING` instead of "sell what's declining".

**Flavors** are computed from fields that already existed (`trajectory`, `leverage`).
`convertible` outranks trajectory - a weak lineup on a top-third war chest is not
described by its tilt. The rebuild split is ABSOLUTE (`ascending_pct > declining_pct`),
not the tertile: "is the rebuild working" is about the roster, and in a league full of
ascending rebuilds the tertile called the clearest working rebuild "stalled". Middling
keeps the tertile because whether waiting is free is genuinely league-relative.

### Boundary noise and the hedge (`window_edge`)

The tertiles are hard breakpoints on continuous measures, and the core labels used to
flip a team's whole identity when a value refresh nudged one rank ("ask twice, get two
windows" - it happened live twice, once mid-refactor when an nflverse role flap moved
one runway). The fix keeps the tertile as the label and ships the label's *stability*
next to it: the two teams straddling a tertile line, when their scores are within
refresh noise of each other, each carry `window_edge` - prose naming the tier across
the line, saying the flip would be pricing noise, and telling the reader to hold both
tiers' advice live.

Calibrated, not guessed - every player jittered +/-2% (a refresh's observed size), 300
trials on each of three real leagues:

| measure | median move | p95 | max |
|---|---|---|---|
| lineup production total | 0.33% | ~0.9% | 1.5% |
| trajectory score | 0 pts | 1 pt | 3 pts |

A straddling pair can therefore close a production gap of about 2 x 0.9% - which is the
existing `NOISE_BAND` (2%), reused rather than re-invented (it moved to `team_values`).
The measured flip boundary agrees exactly: the one pair 0.5% apart flipped windows on
20% of simulated refreshes; the pair 2.4% apart never flipped in 300; no other team in
any league flipped at all. Trajectory gets its own band in POINTS
(`TRAJECTORY_NOISE_POINTS = 2`) because the score crosses zero, where a relative band
means nothing - one live line ran through a literal 37-37 tie, i.e. the tier assignment
was dict order.

Two deliberate asymmetries:

- **The label still comes from the tertile, not from the largest gap.** Cutting at
  natural gaps was considered (one league has clean 11.8%/12.6% gaps at both lines) and
  rejected: gaps aren't reliably there (another league's line lands mid-cluster), and a
  gap-cut moves the *boundary* under noise instead of one team - group sizes swing and
  several teams relabel at once. Tertiles flip at most the straddling pair, and the
  hedge covers exactly that pair.
- **An edge is only an edge if crossing the line changes the message.** A contention
  flip always changes the window. A trajectory flip only matters where trajectory
  decides something: the Push/Contend clock, or a plain Middling flavor. A Rebuild's
  flavor is absolute and `convertible` outranks trajectory, so those hedge nothing -
  noise about a tier that isn't being used is not information.

What the hedge does NOT cover: an nflverse role flap moves a whole player between
buckets and can jump the trajectory score by tens of points - far past any honest band.
That is a data-feed event, and it is `data_gap`'s job to disclose, not the hedge's to
absorb. Hysteresis (labels that stick until the boundary is cleared by a margin) was the
alternative design; it needs persisted prior state the stateless per-request pipeline
doesn't have, and the hedge makes the instability honest without it - revisit only if
testers still find the flips confusing with the hedge in place.

**The `ascending` quality floor is `asset_rank`, not the relevance floor.**
`ascending_pct` is a share of the team's OWN production, so it is scale-free: a roster
whose young players are all bad still reads ascending on ratio (live: 25/7 while ranking
12th of 12 in both contention and assets - the owner: "his ascending assets are just
bad"). The backlogged fix was `clears_relevance_floor`, and the data rejected it: every
rebuild in all three leagues holds multiple ascending pieces clearing that floor - it is
a "real chip vs waiver filler" bar, far too low to separate a working rebuild from a bad
one. What actually separates them is accumulation: the documented offender was 12/12 in
total assets while every working rebuild sat 5-6/12 or better. So `ascending` now
requires the tilt AND not-bottom-tertile assets (`_assets_bottom`, same
`MIN_TEAMS_FOR_LEVERAGE` guard as leverage); across three leagues this changes exactly
one label - the complained-about one. Deliberate tension, documented rather than hidden:
the tilt stays absolute (a league of ascending rebuilds must not relabel the best one)
while the floor is league-relative (accumulation is only measurable comparatively) - in
a league where every rebuild is loaded, the bottom-third one still reads stalled, which
is the honest comparative claim.

**Leverage** (`convertible` / `mortgaged` / None): `asset_rank` (every player plus every
pick) against `contention_rank`, tertile-cut. The state the window could not express - a
roster 9th in production holding the 2nd war chest is not "bad", it is unspent.
Deliberately not a fifth window: window says what to do with this roster, leverage how
much rope there is to change it. `pick_share` rides along, reported not weighted - HOW
much more liquid a pick is than a player is nothing this project can calibrate. Not
computed under `MIN_TEAMS_FOR_LEVERAGE` (6).

**Per-roster lists** (`classify`): `cornerstones` = top 10% of the format's value pool
(`CORNERSTONE_PERCENTILE`) AND at least `MIN_MEANINGFUL_RUNWAY` of runway - runway, not
bucket, so an elite back months from his cutoff is a sell surface, not a foundation.
Cornerstones ALSO appear in `sellable`, tagged: the hardest ask is a price, not a veto.
`win_now_core` = valuable but short-runway - and these carry `price_note`
("cornerstone-priced"), because a piece that missed the tag only on the CLOCK still
sells at the tag's price. Found by the owner's eye test: a roster read "cornerstones:
none" while holding a top-6 receiver at ~1.8 years, and the tool framed its premium
asset as ordinary - the hard-breakpoint family applied to the seller's ask at the 2.0
bar. The market still paying the top-10% price IS the selling argument, said on the
entry. `sellable` = everything a team could
realistically be asked about (prime/declining below the threshold, plus the two above).
`tradeable_surplus` = ascending below the threshold - lottery tickets and young depth.

**`owns_next_first`** ships as a note whose meaning depends on the window: a real
constraint for a rebuilder (tanking pays nothing without the pick), the window working as
intended for a contender. **`no_trade_history`** flags a league with zero trades ever -
dynasty identity is built through trades, so the labels are least reliable exactly when a
league is newest; agent rule 9 caveats them.

**Load-bearing rule, stated once:** if the question is "this season", the metric is
`redraft_value`; if it is "what is this worth", the metric is `value`. Every one of this
project's worst bugs was one of these used for the other's question.

## One definition of "who starts" (`LeagueContext.starters`)

`roster_needs.projected_starters`, called once per league: value-derived (redraft - a
lineup is who scores most this week, in every window), flex slots filled properly
(dedicated first, then flex most-restrictive-first, so SUPER_FLEX doesn't take a player
only a narrower FLEX could use), missing redraft prices sorting last (safe: the highest
dynasty value without one measured far below every replacement level). Player IDS, not
names, so `is_starter` is stamped on entries at the source instead of a set threaded
through callers. Nothing reads Sleeper's current-week snapshot. `fill_lineup` keeps
slot labels and is exposed as `get_optimal_lineup` because filling flex slots in prose
is a deterministic optimisation an LLM gets subtly wrong (a vacated FLEX correctly went
to a TE, not the assumed WR).

## Positional needs (`analysis/roster_needs.py`)

Needs are measured on current production, trade relevance on dynasty value - same
`replacement_thresholds`, explicit `metric` required. Replacement level = the Nth-best
player leaguewide, N = the league's starting slots at the position; a win-now lens, so a
rebuilder's needs are "what a contending version would be short of" (`REBUILD_LENS` ships
on their entries, and exposure is explicitly not a risk for them).

**Count and quality are different problems with opposite fixes** - a bare count was close
to inverted on a real league (the 2nd-best WR room read critical because its WR3 sat
below the bar; the 9th-best read fine because four bodies barely cleared it):

- `critical` - can't field the slots AND the group is weak: bodies and quality.
- `top-heavy` - can't field the slots, but what's there is good: a body, NOT an upgrade.
- `weak` - slots fillable, group bottom-tertile or under `WEAK_VS_MEDIAN` (0.5) of the
  league median (the median test catches skewed positions rank alone hides): an
  upgrade, NOT depth. Had no representation at all under the old rule.
- `ok` - includes mid-league with no star, which is not a need.

Quality is not asserted below `MIN_TEAMS_FOR_QUALITY` (4) - a shortage falls back to
`critical` rather than a label derived from nothing. Downstream, the shape decides the
fix: the buy path applies the weakest-starter upgrade bar, waivers only fill count-shaped
needs, and the persuasion tier requires beating the weakest starter at every need level.

**Injury exposure is measured and is NOT a need** (`drop_if_injured`, `exposure`,
ranked): magnitude if it happens, computed by removing the weakest starter and refilling
optimally (flex-aware, so a superflex QB3 build reads sound rather than exposed), with
`position_miss_rate` (measured: QBs miss ~11% of roster weeks, RBs ~19%) supplying the
likelihood half and `replacement_is_unpriced` flagging when the number is only an upper
bound. "Low exposure" on a critical need carries the consolation caveat - it is the hole
restated, not comfort.

**Stranded production** (`stranded_starters`): bench players producing at least
`STRANDED_MULTIPLE` (2.0) of the weakest starter - a magnitude test (live cases sit at
5.3x and 1.5x, so the figure is not doing delicate work). Their whole value to this
roster is what they fetch, in any window; the report leads with them.

**Depth as a third state** (`would_start_if_one_out`): binary needs leave depth
invisible. A body who steps in when the weakest starter at his position is out has real
value at a nominal price - stated with "don't overpay", never as a need.

## Trade activity (`trade_activity.py`)

Sleeper exposes no trade block, so realized trade counts across the league's full season
chain are the proxy for "will this owner engage". A zero is only informative when someone
ELSE in the league has traded (`Board.others_have_traded`), and never about the asking
team itself. Activity is a flag and a last-resort tiebreak, never a ranking - sorting on
it once hid the second-best production available behind an active trader's scraps.

## Trade target matching (`analysis/trade_targets/`)

A **discovery tool, not a fairness calculator**: it finds who to call, never whether a
package is fair - value-summing treats five bench pieces as a stud, and a real
calculator needs weekly-production VORP modelling this project doesn't have. Split by
surface: `board` (league facts + shared vocabulary), `counterparty` (why the other side
moves), `buy`, `pivot`, `upgrades`, `report` (the audited printer), with `find_targets`
composing per window - buy for Push/Contend, pivot for Rebuild, both for Middling.

### Shared vocabulary (`board.py`)

- **`Board`**: every league-wide fact, built once per report. The asking team stays an
  argument.
- **`_sells_him` - seller-ness is a property of the (owner, player) pair**, not the team.
  A Rebuild team sells everything; a RISING Middling team sells exactly its aging pieces
  (`years_to_decline < INSIDE_FINAL_YEAR` - the seller's own edge, not the buyer's
  horizon, which on the RB curve would put a rising team's whole backfield up for sale).
  Treating seller-ness as a team fact hid the best win-now RB reachable by a team whose
  critical need was RB. The pivot path applies the mirror: a rising middling team's
  YOUNG value is excluded from acquire targets, because accumulating it IS its plan.
- **`friction`** - one {flavor, why} vocabulary on both sides of the table; an empty
  list means easy. Buy side: `cornerstone`, `beyond_your_best_chip` (above the asking
  team's biggest SINGLE chip - the only comparison this project can make),
  `never_trades`, `needs_a_pivot`, `holds_to_win`. Sell side: `cornerstone`,
  `costs_you_production` (the lineup notices only outside the noise band). Flavors, not
  a score, because they call for different responses and because lists group by them.
  None is a price. The no-hole ask splits by the seller's WINDOW (`_no_hole_friction`):
  "change direction" (`needs_a_pivot`) is only honest when there is a direction to
  change; a CONTENDER with no hole gets `holds_to_win` - he could sell an aging piece
  and stay a contender, which is exactly why he probably won't, and only an overwhelming
  offer opens it. The owner flagged the same misread twice, months apart ("shiv is win
  now and could choose to move off the aging value but doesn't have to"; then of a #1
  lineup tagged needs_a_pivot: "that team is just nasty and competing... probably hangs
  onto them to win now") - the second time is what split the flavor.
- **`_best_chip`**: max single value in a pool - "out of reach" always means above one
  piece, never above a sum.
- Noise bands: `NOISE_RETAINED = 0.98` / `NOISE_BAND` - two values inside the band are
  the same value, both axes, both directions (one-sided use made findings flicker
  between refreshes and called +0.12% a lineup upgrade). Lives in `team_values` now:
  the same band decides whether a tertile boundary is real ("Boundary noise", above).

### Value basis and the relevance floor

`team_state.VALUE_BASIS` maps bucket -> what a price is made of (declining=production,
prime=mixed, ascending=upside), with `value_basis()` overriding to "production" inside
the player's final year - the bucket is only the sign. One mapping feeds buy-side price
notes, sell-side give-up cost, and `clears_relevance_floor`: production/mixed value must
clear HALF the positional replacement level, upside a QUARTER (its appeal is priced
lower; both fractions calibrated against real named chips). One floor across the whole
codebase - waivers reuse it rather than inventing another.

### The buy path (`buy.py`)

- **Offer pool** (`_my_offer_pool`): bench sellable + young surplus + any starter the
  bench covers for free (`production_lost_without == 0`) + ascending starters for Push
  only (a closing window exists to spend future value; prime/declining starters ARE the
  production and stay protected). Never a position the team itself needs. A starter's
  `lineup_cost` is stated with WHO backfills (`backfill_for`) - a cost, never a veto.
  Tiered by value over replacement (below-replacement depth is "real but discounted, a
  sweetener" - not zero, injuries and byes are real), with `pick_equivalent` for feel.
  Friction sorts last so the reader's eye lands on moves the owner hasn't ruled out.
- **Targets**: `sellable` players from teams passing `_sells_him`, at positions of
  need, worst-shaped need first (`NEED_PRIORITY`), with a weakest-starter upgrade bar at
  `weak` needs (count-shaped needs have an empty slot - any relevant body helps, and
  sub-weakest entries are labelled DEPTH rather than hidden). Ranked on the metric the
  window is buying: production for Push (declining breaks ties - the price-per-unit
  argument is a tiebreak, not an absolute ordering that once put 70 redraft above
  3,439), value otherwise, activity last.
- **Reachable vs long shots are different LISTS, not different ranks** - attainability
  must not compete with quality for one ordering ("who do I ring first" is not "who is
  best"). The cap applies per half so a blocked name never displaces a reachable one.
- **Picks**: `picks_to_trade_away` for buyers (currency, not production - a first
  becomes a rookie, the opposite of what a contender needs), `picks_to_acquire` for the
  pivot (a contender's future 1st is worth strictly more to the rebuilder asking).
- **Depth adds** (`_depth_adds`): cheap bodies on rebuilding rosters who'd start for
  this team if one starter were out. The "only depth" bar depends on the asking window -
  redraft (replacement production) when filling a lineup, dynasty when holding lottery
  tickets (`DEPTH_NOTE_REBUILD`: a body who inherits a job becomes sellable) -
  deliberately NOT `clears_relevance_floor`, whose tiered fractions opened a crack that
  hid players from both lists at once. Whether he also beats the weakest starter is
  stated per line.

### The counterparty (`counterparty.py`)

- **`_seller_case`** (team-level): the roster is falling (quoted with its
  league-relative trajectory rank - "falling" is a tertile, and it once asserted decline
  off a two-point gap), or this core missed the playoffs with >= 60% continuity
  (`prior_season` - last season is only allowed to describe the roster that produced
  it).
- **`_cliff_case`** (player-level): the owner's window and the player's don't line up -
  a starter inside `MIN_MEANINGFUL_RUNWAY` on a roster tilting ascending (absolute
  tilt). Runway, not bucket: measured across three leagues, runway-qualifying starters
  are a strict superset (24 both / 30 runway-only / 0 bucket-only). The now-premium bar
  picks the price SENTENCE (discounted to harvest vs pure window mismatch), never
  whether the case exists - as a gate it turned existence on the fourth decimal place.
  Team case COMPOSES with the player case rather than silencing the more actionable one.
  Deliberately no replacement-behind-him check (that is the owner's side of the table)
  and no champion veto (the tilt rejects an aging contender on its merits).
- **`_why_they_would_move_him`**: rebuilding = already selling; otherwise the two cases
  above; otherwise the honest "nothing says seller". `never_trades` overrides all three -
  evidence about what an owner does beats arguments about what he should want.
- **`_counterparty_fit`**: what I hold that THIS owner wants - a positional hole I can
  fill (`fills_a_hole: True`, the single definition behind the `needs_a_pivot` flavor,
  which once had two definitions disagreeing about the same player in one run), or a
  rising roster starting aging players, which wants now-and-later value (above
  replacement, real current price, not past his own cliff; runway RANKS the pool and
  the claim softens when the nearest offer is inside the two-season bar - the bar
  decides what the sentence claims, not whether the entry exists). Annotation, not
  ranking.
- **`wanted_by`**: who wants a player this team is moving - a positional need he would
  actually improve (`_would_actually_help` - a need is not the same as wanting THIS
  player), or a falling roster short of ascending value at any position.
- **Persuasion tier** (`_persuasion_targets`): aging production held by non-sellers -
  the tier the buy path structurally cannot see, and where the best production usually
  sits. Sourced from `sellable` (not the cornerstone-gated `win_now_core`), runway
  defines the tier, the relevance floor and the weakest-starter bar apply at every need
  level, `_sells_him` pairs are excluded (they're the buy path's job), ranked by
  production per unit of cost - the cheapest name is often better than the biggest,
  because the market discounts age the buyer isn't paying for. Implausible sellers are
  excluded, not ranked last.

### Better holdings and the mirror (`upgrades.py`)

- **`find_value_upgrades`**: which single holding beats one of my starters at his own
  position for less dynasty value - one player against one at the same position, the
  only pairing where the two scales cancel; candidates from every roster INCLUDING my
  own bench (an `already_mine` return needs no trade and is never capped away).
  Organised as MOVES around the starter being replaced (matched against the weakest
  starter he beats), each with `wanted_by`, `their_reason`, `priced_for` (rank-based -
  a relative measure prints a relative claim), and a `kind`: `upgrade` (clears the
  noise band both ways), `value_decision` (lineup unchanged - the value released is the
  whole gain), `conversion` (down to `MIN_PRODUCTION_RETAINED` 0.90, real production
  sold for real value - never shown to Push). `MIN_VALUE_FREED` (300) keeps churn out.
  This absorbed a within-roster swap finder whose one live case its own window gate made
  unreachable (~160 same-position pairs measured, zero qualifying - within a position
  the two prices correlate, so retention usually frees nothing).
- **`_conversion_candidates`**: `_cliff_case` pointed at your own roster - the same rule
  the league is handed about you, end to end (it drifted into two rules once: the mirror
  kept a gate the cliff case had dropped, so a starter was pitched to eleven managers
  while absent from his own report). Floor decides who is worth calling about, cliff
  decides whether the argument exists, bar picks the price sentence.

### The pivot path (`pivot.py`)

Sell lists split by runway (urgent vs situational - splitting on bucket once filed the
most valuable asset on a rebuilding roster under "no urgency"), situational ordered most
now-weighted first (a seller converts present into future). Cornerstones are IN the
lists, tagged (`CORNERSTONE_SELL`, keyed by `committed`): for a committed team the
hardest defined move; for a Middling team converting the core IS the choice of
direction, stated as a decision, never an instruction. `sell_clock_note` differs the
same way - urgency for a committed seller, the cost of waiting for a Middling one (one
report once carried both claims about the same eight players). Acquire targets get the
buy path's full treatment (floor, friction, per-position cap, cleanest first,
rising-middling owners excluded); the shared "why any of these owners would sell youth"
lives once in `ACQUIRE_NOTE`. Picks print BEFORE the empty-acquire guard - an early
return once hid the cleaner currency exactly when it was the whole plan.

**`SITUATIONAL_NOTE` and `RUNWAY INVERSION` exist because a rule that lives only in a
docstring does not reach the answer** - measured in three rounds on one eval case (which
of five QBs should a rebuilder trade; runway order Goff 6.1 > Herbert 5.6 > Darnold 4.8 >
Hurts 4.0, so the short-runway piece is a STARTING cornerstone and the easy sale is not
the deep one):

1. **Baseline 0/6.** `situational` was the one block without a note - the CLI printed a
   header the agent never received - and "years_to_decline picks the sale, never age"
   lived solely in `get_team_state`'s docstring, read at tool-selection time and gone by
   the time the entries were on the table. Every run followed the one instruction it did
   have (`stranded`: "lead with these") to the easier Goff sale. What had logged as eval
   flakiness for weeks was inverted: the rare passes were the noise.
2. **Note at the data: 4/6.** The failures now quoted the rule verbatim and applied it -
   but only WITHIN the bench (Darnold vs Goff), never extending it to the starter the
   question didn't name. So the roster's own counterexample was computed onto the entry
   (`_runway_inversion`: "Hurts starts with 4.0 years while Goff holds 6.1 behind him"),
   one tag per position, only when a real inversion exists. **Compute the instance,
   don't instruct the comparison** - the model repeats an attached fact far more
   reliably than it extends an imperative.
3. **Checker calibration.** With the tag in place the "failures" were the eval
   under-crediting the right answer: the runtime grounding regex's negation skip (correct
   for banning) discards "you'd sell Hurts and keep Goff - but he's a cornerstone", which
   is weighing a sale, not negating one (`_weighs_as_sale`, sentence-scoped, colons not
   boundaries, validated against all seven collected transcripts). And the premise can
   die inside the agent's own subprocess (nflverse outage -> default curves -> Goff at
   2.1 years), where recommending Goff IS the right runway answer: the case retries once
   when the answer itself discloses the data gap, and calls a second gap PREMISE GONE.

End state (2026-08-12): full suite 9/9; in isolation the case passes ~4-5 of 6 runs
against 0/6 at baseline. The residual failure is its own, smaller defect: the model
READS the inversion tag (one failing run quoted Hurts's 4.0 years back) and still files
him under "anchors" without weighing the sale - an emphasis problem, so the next lever
if it matters is where the tag sits in the entry, not more words in it.

Same lesson as the lineup note riding on every `get_team_state` result: instructions
survive when attached to the data they govern, not to the tool that fetched it - and
what a computed fact governs best is the single entry it is true of.

### Deleted: mutual win-now swaps

Removed outright with `find_surplus`/`league_surplus` and its MCP tool: it was package
math (summing both sides and declaring 0.6 "balance" comparable), and this project has
no tool that can price a package. Supply above replacement equals demand by construction
(measured exactly: 24/24, 24/24, 36/36, 12/12), so it returned nothing for 36
consecutive team-reads before the deeper objection landed. Replaced by nothing, on
purpose - `find_value_upgrades` and `depth_adds` answer the real questions one player at
a time, and agent rule 8 says the rest is a negotiation the tool cannot price.

### Validated foundations

- Multi-hop traded picks resolve correctly: `traded_picks` is a denormalized
  current-owner view, confirmed against chronological transactions.
- Usage-role tags on all 18 rostered tagged players match real-world knowledge.

## Waiver wire (`analysis/waiver_wire.py`)

Same relevance floor as everywhere else. Surfaces an available player who beats a
team's worst rostered player at the position, or fills a real count-shaped need.
Correctly finds ~nothing in deep 12-team dynasty leagues today; it is the baseline a
future news/sentiment signal would check against ("is he actually available, who needs
him"). FAAB tracked per team.

## Prior season (`analysis/prior_season.py`)

Walks `previous_league_id` for final standings and the bracket. Exists for one job:
whether a non-seller could be talked into selling ("this core hasn't won"). Gated on
`MIN_CONTINUITY` (0.6 of current starting production carried over) so a result can only
describe the roster that produced it; keyed by owner_id because roster_ids don't cross
league boundaries. Deliberately kept OUT of the window classification - "he just won and
will run it back" is an inference about a person, which belongs in how an ask is framed,
not in whether a team is measured as contending.

## Player availability (`sources/injuries.py`)

Miss rates measured from nflverse weekly rosters + injury reports over three completed
seasons: **QB 0.107, TE 0.177, WR 0.195, RB 0.200** of roster weeks - QBs miss half as
often as skill players. Rules that make the number honest:

- A missed week = injury reserve, injury inactive, or `Out` (Questionable/Doubtful are
  uncertainty, not absence; IR weeks appear on the weekly report only 5% of the time, so
  the report alone undercounts the absences that matter most).
- **Suspension is not fragility**: reserve codes classified empirically by how often each
  co-occurs with the injury report (R40/R30/R06 at 0% = suspended/left squad), kept as an
  ALLOWLIST so unknown codes count as not-injury - understating beats calling a healthy
  suspension fragile. Suspended weeks leave numerator AND denominator, surfaced
  separately as `weeks_suspended`.
- Denominator = weeks actually on an NFL roster (a flat 17 rates a player who wasn't in
  the league as durable); practice-squad weeks excluded; `MIN_SEASONS = 2` so rookies
  read None-for-unknown rather than 0.000-for-durable; pooled over player-weeks, not
  averaged over players, because the question is what happens to a lineup slot.

## Recurring defect families, and the guards (`analysis/audit.py`)

Every real bug here was a wrong recommendation produced by correct arithmetic. The
families, named so the next instance is recognized rather than re-patched:

1. **Two currencies conflated** - dynasty value answering a current-season question
   (found five separate times: league ranking, lineup ranking, needs thresholds, buy
   ordering, the efficiency comparison).
2. **Bugs are labels, not math** - prose drifting from the code it describes (a
   docstring claiming a capacity test the code never performed; "urgency" stamped on a
   list whose mode was optional; an absolute claim printed from a relative measure).
   The threshold decides what the sentence CLAIMS, never whether the entry exists:

   | gate | what it excluded | now |
   |---|---|---|
   | `_cliff_case` on the premium bar | a ratio of 0.8790 against a bar of 0.8790 | case stands on runway; the sentence says whether a discount exists |
   | `_holding_kind` on strictly-less value | 3,619 vs 3,616 for +535 of production | `NOISE_BAND`, both axes |
   | `_counterparty_fit` on runway | pieces at 1.6 and 0.3 years unofferable | runway ranks; only past-the-cliff drops |

3. **Hard breakpoints on continuous measures** - 24% of starters sit within one year of
   the 2.0 runway bar, and `years_to_decline` itself degrades to position defaults when
   nflverse flaps. Fixed for ages (runway everywhere a boundary decides), and at the
   core by `window_edge` (Team windows, "Boundary noise"): a label within refresh noise
   of a tertile line now says so, instead of flipping silently between runs.
4. **Computed, attached, rendered by nothing** - six live instances in one module.
   Guarded by `check_everything_computed_is_printed` (renders through `_print_report`
   and greps); note it CANNOT catch an early return that skips a populated block.
5. **One player described two ways** - incompatible claims across blocks, or one flavor
   computed from two rules. Guarded by `check_one_player_is_not_described_two_ways`.
6. **An unlabelled number gets a meaning invented for it** - a bare `{"diff": -11}` was
   narrated as games underperformed in the preseason. Numbers ship with the sentence
   that interprets them; what every entry shares goes in the block note, only what
   varies rides on the line (the repeat-per-entry mistake has been made three times in
   one day).

`audit.py` runs the checks against real leagues (the failures were all shaped by real
distributions), every check derives from a shipped bug (a noisy audit gets muted), plus
a coverage table where a block empty across every team in every league is a dead
feature - it has retired two features. Not in pytest: it needs the network.

**Spot-check techniques that outperform reading top-down**: render the whole result as a
grid and check by quadrant (finds contradictions BETWEEN blocks), and run the same
report twice and diff (a value refresh reshuffles whatever sits on a threshold). Most
real finds came from a human reading output and saying "that doesn't make sense".

**The suite itself was measured, not assumed** (2026-08: 30 hand-picked realistic
mutations - resurrected shipped bugs, off-by-ones on documented boundaries, swapped
labels). 23/30 killed on first run; each kill was exactly ONE failing test, so the suite
has almost no redundancy and no obvious deletions. The 7 survivors shared two shapes,
both now closed (30/30): **one-sided boundary tests** (a value weak under any bar from
25% to 90% of the median proves nothing about where the bar sits - pin both sides), and
**constants read only inside network orchestration** (`classify_league`'s noise bands),
where the fix is a pure-function test that exercises the constant directly. One survivor
was a shipped, already-fixed bug the suite would have let back in (`value_basis` using
the buyer horizon instead of the final-year clock). Statement coverage is the wrong
lens here: the pure-rule modules sit at 80-95% and the plumbing at 0-60% by design -
audit covers orchestration on real leagues, evals cover agent behavior, and neither can
pin a threshold.

## The agent stack (`agent/`)

**MCP server** (`mcp_server.py`): thin wrappers over validated modules, stdio, no new
logic. Every tool returns a dict at top level (the installed SDK splits a bare list into
one content block per item). `mcp.tool` is wrapped once so every result carries
`data_gap` when a feed degraded - a tool added later cannot forget it. Tool docstrings
carry only what the model must know BEFORE reading a result; the block notes in the
payload are the per-block instructions (three copies of the same prose had begun to
drift). Validated through a real MCP client (`test_mcp_server.py`), because a server
that starts fine can still misshape output.

**Agent** (`agent.py`): Haiku via `ClaudeSDKClient`, guardrails SDK-enforced rather than
requested - `tools` restricted to exactly the 7 MCP tools (which is what excludes
built-ins, verified by a live prompt-injection attempt that had no tool to reach),
`max_turns=8`, `max_budget_usd=$0.50/question`, `setting_sources=[]` (the SDK otherwise
ships this repo's CLAUDE.md on every call - a measured 38% input-token cut). The system
prompt carries the doctrine no single tool result states (five principles: pick an end,
two currencies, age is a distance, value is not additive, a trade needs a counterparty)
plus numbered rules, each added for an observed failure.

**The grounding check is the pattern worth keeping**: prompt rules are probabilistic,
so rule 6 (only name offerable players as trade-aways) is enforced by
generate-then-verify - `_banned_trade_names` recomputes the real offerable set from the
same Python the tools ran (`offerable_names` is the one shared definition across modes),
`_trade_violations` fires only on a banned name sharing a line with trade-action
language and no negation (the blunt version fired on every roster description; the
negation skip can miss "don't trade X, but do trade Y", accepted - a false positive
costs money and contradicts correct advice, a miss costs one ungrounded name), one
retry naming EVERY violation (naming one fixed one and left the other). **Fixes that
live in data hold; fixes that live in a prompt leak** - every durable correction went
into a field, a note on a field, or a deterministic check. Rules with rare failures
(stop-on-error, non-dynasty refusal) deliberately do NOT get their own verify machinery;
building it for every rule regardless of failure rate is scope creep.

**Sessions** (`sessions.py`): a live client per client-supplied session id (idle TTL,
LRU cap of 2 - each session holds two subprocesses against a 2 GiB container, per-session
lock so concurrent turns can't interleave). Never shared between callers - the
context-leak trap. Fixes three measured things at once: no conversation memory, the
~4,700-token prompt-cache prefix re-CREATED at 1.25x on every fresh client (a persistent
session re-reads at 0.1x, ~92% off the prefix per follow-up), and the MCP data cache
living in the per-client subprocess. `cost_delta` exists because
`ResultMessage.total_cost_usd` is cumulative per client - billing the running total
would have drained the daily budget twice as fast as real spend.

**Cost anatomy (measured layer by layer)**: bare SDK floor 136 tokens; our system prompt
+683; MCP framing + 7 tool schemas +1,876. ~65% of the baseline is our own content, so
dropping the Agent SDK for the raw API would save ~a hundred tokens - if it is ever
replaced it should be for control, not cost. Tool results are small once
`get_team_state` takes `owner_name` (unfiltered was ~7,846 tokens and made the model
fall back to re-deriving windows from roster_detail - the fix was a filter, not a
prompt). Haiku needs a 4,096+ token cacheable block before caching activates at all.

**Payload diet (2026-08)**: model quality is NOT the cost gate - Haiku follows every
note it is given, so scaling is a token problem, and the tokens were duplication. A
field-weight histogram over live payloads found the two offenders: entry-level
`wanted_by` shipped the same buyers as full dicts on every same-position entry (21-26%
of a sell report; no reader used `rank` or `reason_count`) - now one composed string
per entry (`wanted_line`), which also fixed a CLI/JSON drift where the
contender-premium clause existed only in the printer; and `need_note` stamped the
asker's OWN need paragraph on every buy target at the position (14% of a live buy
payload, 90% literal duplication) - deleted, it ships once in `result["needs"]`.
Result: every measured report 13-17% smaller (a Middling two-path report 22.3K -> 19.3K
estimated tokens). Remaining known duplication, deliberately left: `their_reason` /
`why_they_might_listen` repeat one owner's team case across his players (~40%
dup) - deduping means cross-entry references, order coupling, and a contract change,
for less than half the win the first two cuts bought.

**HTTP API + budget** (`api.py`, `budget.py`): plain FastAPI, `/ask` calling the same
`run_query` as CLI and evals. The daily ceiling is what makes a public endpoint safe -
two ceilings (dollars + a request-count backstop, since a failed call reports
`cost_usd: None`), in-process with no database because `max-instances=1` +
`concurrency=1` make a counter exact (and this workload is memory-heavy, not
horizontal). Check-then-record can overshoot by at most one call ($0.50). Those Cloud
Run flags are load-bearing and live in the workflow, not console click-state.

**Container + CI/CD**: five real build failures recorded in git history; the durable
lessons - pin every dependency (an unpinned `mcp` resolved to a version where `FastMCP`
moved, and the resulting ModuleNotFoundError reached the user as the agent CONFIDENTLY
CONFABULATING an answer with zero tools: a system that confabulates instead of crashing
is worse than one that crashes, so `/diagnostics` spawns the MCP server and reports its
stderr, and stays), run as non-root (the CLI refuses bypassPermissions as root), Node
22+ for the CLI transport, secrets from Secret Manager never the image. Deploys gate on
tests via `workflow_run` (checking `conclusion` and `head_sha`); auth is Workload
Identity Federation pinned to this repo and `refs/heads/main` - no long-lived key; the
runtime identity holds exactly one permission (`secretAccessor` on the one secret) so
the blast radius of the inherent CD risk is one prepaid API balance. CI excludes evals
(paid) and the MCP protocol test (live third-party APIs); no API key reaches the
workflow, so a test that silently starts needing one fails loudly.

**Observability** (`observability.py`): one JSON line per run - question, outcome,
latency, turns, cost, token/cache breakdown, leagues, format tier, tool errors
(captured from `ToolResultBlock`, the structured signal for a failed call) - to stdout
always (Cloud Run pipes it to Cloud Logging; the container filesystem is tmpfs, so a
file-only log dies with the instance) and a local file off-cloud. One `try/finally`
around all of `run_query`, variables initialized before the `try`.

**Tests** (102+, offline, ~2s): assert the boundaries the heuristics turn on plus
regression guards for every real bug. Verified they bite (breaking a constant fails
tests). **Evals** (`evals.py`, 9 cases, real API calls): deliberately few, each guarding
a distinct observed failure mode. A failing eval is not automatically an agent
regression - read the failing output first (three separate times the eval itself was
wrong: a stale assertion after a design change, a word-list match on model phrasing, and
a premise that inverts when nflverse role tags are unreachable - that one now checks its
premise and says PREMISE GONE before spending an API call). Known flake signature: a
prompt-rule case failing once and passing 3/3 on isolated re-run is model noise, not a
regression.

**Hosting - decided: Google Cloud Run.** Best technical fit (a real container - the
agent spawns subprocesses, so edge functions are out), one built-in HTTPS endpoint,
larger permanent free tier than Lambda (whose common API Gateway pairing is not free),
and the AWS keyword match wasn't worth the extra services since postings treat clouds
as interchangeable.

## Frontend (`agent/static/index.html`)

One static page, vanilla JS, no build step. League data renders directly from
`GET /api/league/{id}` rather than being recited by the model - the analysis layer
already computes it exactly, and recitation is where confabulation creeps in. The agent
is reserved for reasoning. Client-generated session ids, so an expired session degrades
to a new conversation rather than an error.

## Known limitations / backlog

Measured or confirmed, none urgent, kept so nobody re-derives them:

- **The window hedge discloses instability rather than removing it** - `window_edge`
  says when a label is one refresh from flipping, but the label still flips.
  Hysteresis would pin it and needs persisted prior state the stateless pipeline
  doesn't have; build it only if testers find the hedged flips confusing. Role-flap
  jumps in trajectory (tens of points) remain `data_gap`'s problem, not the band's.
- **`redraft_value` is a trade price, not a weekly projection**, and the difference has
  a direction: prices bake in positional scarcity, so `fill_lineup` is biased toward
  TEs in flex slots, and the redraft tail collapses to a floor below ~the 30th player
  at a position (a rostered WR at 1 is a price, not a forecast), which effectively caps
  the persuasion tier at the top ~30 per position. A real projections source is the
  single highest-value external addition - it also closes the PPR gap.
- **The market's own age curve could calibrate `AGE_CURVE`**: cornerstone-share by age
  shows a WR cliff at 28 (ours says 29 - possibly a year late; 13 cornerstones, none
  28+), while the apparent TE cliff at 27 was an artifact of a four-player cell (Kittle
  at 32.9 is the 11th most valuable TE - the curve stays 30). Live counterweight from
  the owner (2026-08, Justin Jefferson at ~27.2 reading 1.8 years): "top 6 receiver
  this year and shouldn't fall off for at least 2 more" - a domain read that puts the
  cliff LATER than 29 for elite receivers, opposite the cornerstone-share signal. One
  anecdote moves no curve, but if elite WRs keep out-living the bar the curve may need
  a tier the way QBs got one. Cornerstone value is
  decline TIMES remaining years and survivorship-biased, so the general version needs a
  bigger pool tracked across seasons.
- **The market refuses our rushing-QB discount on Josh Allen** (10,415 at 29.5, 7.1
  carries/game; our curve gives him 1.5 years) - `dual_threat_qb` absorbs most of this,
  but an elite thrower-and-runner forced onto the rushing curve may still be underrated.
- **`starting_production` is misnamed** (see the axis section).
- **`miss_rate` is attached to players who could never play**; `would_start_if_one_out`
  could gate it. Depth is not weighted by injury risk (needs severity/duration by injury
  type - a real modelling exercise, deliberately not half-built). Availability is
  binary; playing-hurt has no state.
- **The relevance floor ignores league size/roster depth** and cannot be calibrated from
  three same-shape leagues.
- **A rebuild's timeline isn't checked against its own core's runway** - a roster built
  on two 28-year-old QBs cannot afford a three-year teardown; nothing computes when a
  rebuilding core expires.
- **Record, playoff spots, and window LENGTH are unmodelled** - all waiting on games
  being played. Lane supply too: contending is worth more when nobody else is, and a
  Middling team is an optional seller who should price above a committed one.
- **House rules live outside the API** (see Format support) - expect more of these.
- **Handcuff/backup-upside concept not built** (depth charts exist in nflverse);
  O-line quality (PFF is closed; nflverse alternatives unexplored); ~4.4% of rostered
  players carry no FantasyCalc value (treated as 0); ~29% of skill contracts don't join
  to a Sleeper ID, almost all newest rookies.
- **Manager skill/luck analytics** (lineup efficiency, schedule luck, trade/waiver
  grading, draft grading) need in-season data - and trade grading needs value AT
  TRANSACTION TIME, so start snapshotting values before wanting it.
- **Future analyst signals** (projections, X/Twitter sentiment, sportsbook lines): X's
  API pricing needs a real check before assuming buildable; odds APIs have usable free
  tiers, unverified against actual needs.
- **LOGIC.md is invisible to the agent.** Reasoning reaches it via block notes and tool
  docstrings only. Cheapest next step: a comparative field on the team row (gap to the
  best lineup and whether that team is rising - "you cannot out-wait him" is currently
  an inference nothing prompts). The full version is this file as a queryable MCP
  resource, which needs chunking design.
