# Logic reference

Every heuristic, threshold, and "why" behind this repo's analysis - the material the
eventual chatbot grounds its explanations in. Update it in the same change that adds or
adjusts a heuristic. This is a reference to the CURRENT rules, organized by concept;
the full history of how each rule was arrived at lives in git (`git log` reads as a
narrative on purpose).

## The model in one page

1. **Two currencies, and one measure of points.** Dynasty value prices production plus
   future years; redraft value prices this season alone. Confusing them is this project's
   most common bug, and the two scales are unnormalized - never compare or ratio them
   absolutely; compare ranks within a position, or pairwise at the same position where the
   skew cancels. What a lineup actually SCORES is neither: it is projected points a game
   (Sleeper's season projection under the league's scoring), and every sum or share of a
   lineup's production is measured in it - prices are convex in points, so summing them
   made one star look like a lineup.
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
  rosters, users, transactions, traded picks, and season/weekly projections (the
  undocumented `/projections` endpoint the app itself reads - raw stat lines, priced
  by each league's own scoring).
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
heuristics: QB 27/34, RB 24/27, WR 25/29, TE 25/30 (QB enters prime a year later than the entry ages of other positions - the position takes years to develop, so the peak arrives later on both ends). Usage-based overrides from measured
season data (nflreadpy), thresholds picked from natural gaps in the real distribution:

- `rushing_qb` (carries/game >= 5.0, not an elite passer): 27/32
- `dual_threat_qb` (>= 5.0 carries AND elite passer): 27/34 - the point is the absence
  of the rushing discount, not a bonus; elite passing survives the legs
- `pocket_passer` (elite passer, not a runner): 27/37 - not 40, because the curve should
  turn before the market does; the top passing-EPA tier over three seasons is all pocket
  throwers, so the tag is earned by production. (Tuned 38 -> 37 on the author's eye
  test - a 34-year-old pocket passer is fine but not a green light - paired with the
  UI's QB decline tail of 3, which puts full red at exactly 40: any QB is done by 40.)
- `pass_catching_rb` (targets/game >= 4.0): 24/29

"Elite passer" = top third of passing EPA/game over three seasons. EPA over CPOE
despite CPOE being better isolated, because CPOE penalises aggressive downfield throwing
(Stafford: 5.33 EPA, -0.47 CPOE). The pocket/rushing spread (37 vs 32) is the only
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

## Production is projected points (`league.context`, `team_values.ppg`)

`starting_production` used to be a sum of redraft TRADE VALUES of the projected
starters. That was the wrong measure and the owner caught it live (2026-08-16): trade
values are convex in points - Gibbs was priced 3.3x Chase Brown and projected 1.6x - so
one star made a lineup of holes read as a middle-third team ("Ben would be losing most
games because most of his team is bad", yet he read `wait` at 59% of best).

Now: **what a lineup PRODUCES is Sleeper's season projection under the league's own
scoring, per game** (`projected_ppg`, attached to every player in `league.context`;
`sources.sleeper.get_projections` returns the raw stat lines and `score` is the same
dot product Sleeper uses - reproduces its `pts_ppr` exactly for a PPR league; season
total / 17). **What a player is PRICED at stays `redraft_value`.** The split, by
question:

- Sums and shares of a lineup -> ppg: contention rank and `pct_of_best`, the
  ascending/declining/expiring shares, position clocks, the framer's
  `lineup_production_delta`, `production_lost_without` (`lineup_cost`, injury drop),
  `optimal_lineup` totals, prior-season continuity, and the Pickens comparison
  (lineup cost vs what the target brings, both in ppg).
- Positional bars, per-body quality, flex ranks, cornerstones, value basis, give-up
  cost, every price -> market values (owner: "trades and positional ranking based on
  redraft values are good, but production pcts were the issue").
- **Who starts -> projected points too (2026-08-17).** The split, in the owner's words:
  ePPG is what he DOES, redraft is what he COSTS, dynasty is what he's WORTH - and
  filling a slot is a what-he-does question. Decided by a diagnostic, not a guess
  (`scratch_fill_diag.py`): market-first and ePPG-first disagreed on 27/48 lineups,
  40 swaps, mean +1.3 ppg, nearly all the last slot, with one shape - the market
  starts youth (Corum, RJ Harvey, Travis Hunter, 22-25) where projections start
  volume (Schultz, Hunter Henry, Hockenson, Geno Smith at ZERO market value, 30-36).
  Effect: 3 paths (Brad 7->4 wait->contend, Paulyt on-a-clock->decide across the
  tertile line, HiThirsty sell->build), 9 rank swaps. GUARD against trusting one
  projection source (Sleeper's = Rotowire's) too far: two players within NOISE_BAND
  of each other are the same projection and the MARKET picks between them - 15 of the
  40 swaps were under 1 ppg. Bars stay market so a bad projection cannot both bench a
  player and call his position a hole. Real season PPG (Sleeper fpts / games) is a
  display-only column and feeds nothing; the accessor is `team_values.eppg` so it
  cannot be misread.

Measured effect on the 48-team corpus: `pct_of_best` now spans ~70-100 instead of
28-100 (no lineup scores zero) and 13 of 48 paths moved - kb wait -> contend at 93% of
best, Ben wait -> build, Paulyt wait -> contend on a clock (the read the owner expected
from his aging starters), shivvv contend -> press. Declining shares rose almost
everywhere: the market's convexity favours the young elite, so value-shares had been
squeezing old producers (Henry projects real points at a collapsed price). Open
follow-ups: BARBELL_MIN_PCT / ARRIVING_MARGIN_PCT were calibrated on value shares and
want re-verifying on ppg shares; the tertiles can now be a distance from best rather
than fixed thirds (ROADMAP). Weekly projections come from the same endpoint (`week=`)
and are an in-season consumer.

## Team windows (`analysis/team_state.py`)

Two measured axes, each cut into league tertiles so no constant is tuned to one league:
**contention** = the projected lineup's total REDRAFT value ranked against the league
(dynasty value prices future seasons - swapping to redraft moved teams 4-5 places on real
leagues, including a roster that was "old but genuinely close" being told it was
mid-pack), and **trajectory** = ascending minus declining share of that production
(production, not dynasty value, which would double-count - ascending players are PRICED
on the growth being measured). This replaced an age-only model plus two patch flags that
were crude proxies for the missing contention axis.

**The window/flavor algebra** (settled 2026-08-14, testing "firesale" as a candidate
flavor): the STATE answers "what game am I playing", the FLAVOR answers "what is my
tempo or leverage in that game", and a flavor earns existence only if it changes the
VERB of the advice, not the adverb. Push vs Contend changes the verb (pay the premium
now vs never pay one) - which is why the clock is promoted into the window tag itself.
"Firesale" (a rebuilder in the falling tertile, holding value that is aging out) fails
the test: stalled says *sell*, firesale says *sell faster* - same verb - and the
urgency already lives at the right resolution, the per-piece runway on every sell
entry. It landed as a clause on the Rebuild window_note instead ("conversion has a
deadline, not just a direction"), gated by the same falling tertile Push uses - the
existing instrument, no new threshold.

**Misaligned, the barbell case** (settled 2026-08-14, from the owner's read of a real
roster): "steady" is a NET number, and a barbell roster nets to zero - hard-aging
assets cancelled out by arriving youth. The owner's articulation is the rule: genuine
steadiness makes *inaction* efficient, while a cancelled-out roster is a live
efficiency loss with an action attached (convert one side into the other's timeline) -
so it passes the verb test and earns its own flavor rather than a note variant.
`misaligned` replaces `steady` for a Middling team when at least `BARBELL_MIN_PCT`
(25%) of started production is moving in EACH direction - a quarter of the lineup
aging out while a quarter arrives is cancellation, not calm. Middling-only by
construction: Push/Contend and Rebuild flavors resolve before trajectory is consulted,
and their machinery (clock_mismatch, the sell lists) already names their aging pieces.

**The three-tier read** (`alignment` / `path` / `path_reason`, owner-designed
2026-08-15): tier 1 is the contention tertile and is shown by RANK ORDER, not a tag -
a "Rebuild" tag on the last row says nothing the row's position doesn't. Tier 2 asks
whether the roster's composition agrees with the game that rank implies; tier 3 is
the path: a continue verb when aligned (hold / buy-on-a-clock / wait / keep
accumulating), both paths with a lean when not. **The lean comes from tier 1**: the
side of the roster already delivering rank is the side to keep (contender barbell →
lean push; rebuilder barbell → lean pivot; middle rank → genuinely no lean, "let the
season decide" - the old Middling doctrine falling out of the structure). Signals are
the existing instruments, no scale blending: production tilt for the players (a
dynasty-value version was tried and washed out - 44 of 46 teams read "arriving"
because dynasty value is future-weighted by construction), `leverage == convertible`
for the middle rank's unspent-option case (the MEASURED war-chest concept; a second
pick-share bar here once let Vicdank read "aligned - wait" at assets #3 on rank 7
because 22% picks missed an arbitrary 25 - same concept, two instruments, caught in
the 2026-08-15 audit), `pick_share >= PICKS_HEAVY_PCT 25` only where picks
specifically are the accumulation currency (a rebuild counts a real pick pile as
arriving - no roster spot, no injury, the year ladder prices their appreciation),
the `assets_bottom` guard on rebuilds (an ascending tilt that isn't accumulating
value is not a working rebuild), and ARRIVING_MARGIN_PCT (15) so "arriving" means a
real tilt, not 25/20 noise. An arriving-tilt convertible stays ALIGNED (bergenjay:
war chest plus an incoming wave is a plan, not an undecided option). A
young CONTENDER is aligned, not a pending decision: an ascending starter delivers
now AND later, so "stack or convert - decide" advice there would tell the best spot
on the board to unwind the point of competing (the owner's RJL test: two young stud
redraft-relevant RBs are not a barbell side, they are the rank). The tilt counts
STARTERS only - a deep bench aging out or arriving does not shape a team, so it
never enters tier 2. Anchors: kierankieran XFL 2
(rank 3, 37/35 barbell → unaligned lean push - invisible to the Middling-only
misaligned flavor, which this supersedes conceptually) and spugz13 (rank 9, 8/11, no
war chest → uncommitted, "the first trade is for a direction"). The old
window/flavor fields remain computed and shipped alongside during the migration.

**Two piece-level overrides (owner, 2026-08-17: "we'd rather have people labeled
unaligned than aligned for edge cases").** Shares can be calm while ONE asset is in
the wrong currency for the rank, and that asset is the decision pending:
- A rebuild (also-ran) holding an **aging chip** - a piece inside `MIN_MEANINGFUL_RUNWAY`
  that still clears its position's trade-relevance bar - reads unaligned `sell`, named
  ("one clear sell on a rebuilding roster: Kenneth Walker"). Prime pieces with runway
  (Gibbs, Burrow, Lamar on a rebuild) are NOT clear sells; the aging bar is the same
  two-season horizon everything else uses. Fired on 4 of 10 aligned rebuilds in the
  corpus (Walker/Waddle, DeVonta Smith, Kyren, Walker again).
- A contender holding **idle youth** - a cornerstone in the ascending bucket who is not
  in the lineup - reads unaligned `press`: value priced on runway that a winning lineup
  isn't using should become production. Fires on nobody in the corpus today; kept
  because it is the mirror and the owner asked for both.
`aging_chips` and `idle_youth` ship on every row as facts; only the tier-1 branch that
owns the lean interprets them (a contender rides its aging chips). The middle has no
lean, so neither fact moves a fringe read.
**Two clocks, resolved (2026-08-17): the declining side of the tilt is the final-year
clock, not the age bucket.** The bucket flips at the position breakpoint, so a starter
0.1 years from it read prime and kb's lineup 0% declining while its RB1 was expiring
(Paulyt101 was the original case: 47% aging out by runway, `wait` by bucket). Now a
starter inside `INSIDE_FINAL_YEAR` of his breakpoint counts as declining whatever his
birthday says (the same clock `value_basis` uses to call a piece production-priced).
The buyer's two-season horizon was measured and rejected for the tilt: it calls a
26-year-old WR declining, puts 16 of 18 contenders on a clock, and would need the
25/15 bars retuned to reproduce today's reads (median declining share 31 vs the
bucket's 18); the final-year clock lands at 23 and the bars keep their meaning. Effect:
2 paths (MSpoto contend -> on a clock; g0ldyb3rg wait -> decide); kb reads 28/11
contend - Cook (0.1) trips it, DeVonta Smith (1.2) does not, which the owner called
right "for another .2 years". The two-season horizon stays the buyer's clock: aging
chips, expiring shares, position clocks.
**Wait is earned by ascending (owner, 2026-08-17).** "The wait and build tags were
supposed to mean you are ascending, while the contend tag is fine if you are aligned
because you're at the top. If you were aligned descending and bad you should not get
build, you still need sell." The bottom already worked that way (flat/leaving ->
sell); the middle did not - a flat fringe roster read aligned "wait" ("nothing arriving
and nothing aging out - waiting costs nothing"), which is holding, not waiting
(Insert-qkiernat 0/22, 42% expiring). Now the aligned middle reads are
"wait - production is arriving" and plain "wait" ONLY when nothing is aging out at all
(expiring share below AGING_WORTH_NOTING_PCT - the runway clock, so a flat roster
quietly expiring still reads decide; owner: "Bergen is textbook wait, driven by the
fact that he is 0% descending"); every other middle roster is `decide`. Effect: 9 of 11
fringe teams read decide, bergenjay waits (15/0, 0% expiring), g0ldyb3rg waits on an
arriving wave - the middle as a place to pass through, which was the doctrine.
**Arrived youth is prime, not ascending (owner, 2026-08-17).** "Cornerstones should
not count truly as ascending - they are ascending but just prime. Fannin is the
ascending archetype, Pickens is fine for a contender to have." An ascending starter
already producing at core level (redraft value clearing the top-10% production bar)
has arrived: he delivers now AND later, so for the TILT he counts as prime; a young
starter below that bar is the wave still coming. Price and value basis are untouched.
Effect on the corpus: ascending shares deflate everywhere (median 32 -> ~17); shivvv
40/27 -> 17/27, press -> contend on a clock (JSN and Lawrence were the "wave");
bergenjay/teomilner lose the "production is arriving" flavor (Nabers, Love, Purdy,
Bowers have arrived); obamagg build -> sell. 4 paths.
**Bars re-verified on ePPG shares (2026-08-17)**: 26 arriving / 15 leaving / 4 barbell /
3 flat of 48; the four barbells (shivvv, kieran, freethepenguins, woozer) and the nine
on-a-clock contenders all pass the smell test, so BARBELL_MIN_PCT 25 and
ARRIVING_MARGIN_PCT 15 stand. The one defect visible in the table is Insert-qkiernat
(0/22 by bucket, 42% expiring by runway -> `wait`) - that is the two-clocks item, not
the bars.

**The direction gate** (owner's rule, 2026-08-15, verbatim: "Good teams never trade
for future weighted assets by default, bad teams never trade for production leaning
assets by default, middling teams could choose either... cut out all this nonsense
systematically"): trade suggestions are generated only along each side's default
direction - contenders acquire production and part with future; rebuilds acquire
future and part with production; the middle swings both ways. Violations are CUT,
never surfaced with a friction label - "shivvv might sell Henry (holds_to_win)" and
"offer Stafford to a rebuilder (they're short at QB)" are nonsense by construction,
and labelled nonsense is still noise. Three cuts, one named rule
(`acquires_by_default` + a contender skip in `_persuasion_targets`): the persuasion
tier never lists contender-held production (this deliberately retires the
cliff-case-on-a-contender behavior the tier was once rebuilt for - that read
survives seller-side as clock_mismatch, and in get_player_outlook when a user asks
about the piece BY NAME); `wanted_by` never lists a rebuild as wanting a rental
(the hole is real, the direction is nonsense); `_counterparty_fit` never dangles a
rental at a rebuilding counterparty. A rental = a player inside
MIN_MEANINGFUL_RUNWAY; picks are never rentals. Contender/middle asker pools were
already direction-correct by mode structure.

**The stance override** (2026-08-15, from the spugz hypothetical: "the tool says I'm
right on the cutoff of Middling, what if I actually want to press this season?"):
the direction gate is a DEFAULT and the manager outranks it - the chip says what the
position calls for, never what its manager is doing, and that cuts both ways.
`find_targets(..., stance=)` accepts press/contend/buy/decide/wait/sell/build and
runs the declared side's full paths regardless of label; `stance_note` instructs the
model to present the choice as the manager's, next to the unchanged measured read.
The agent passes it ONLY when the user declares a direction, never on its own.

**The offer floor** (owner: "I don't think anyone cares about the sell list of
Nailor, Washington or Dulcich at all"): an offer must be market-relevant in at least
ONE currency - dynasty value above the position's trade-replacement bar, or redraft
at/above the startable bar (the Tony Pollard shape: cheap in dynasty, real this
season, a legitimate depth chip). Below both bars is waiver fodder and is cut from
`my_offers` (and therefore from every `offer_any_one_of`). Depth-adds keep the
production-body class for lineup-filling askers; the rebuild lottery tier already
cuts rentals via the direction gate.

**Payload prose obeys the tiers** (2026-08-15 agent audit, Caleb-triggered): the
model reasons FROM the notes it is handed, so any note whose wording predates the
tier structure can invert an answer. Three data-level rules from the audit: (1)
piece-level price_notes are PRICING facts ("the ask IF he moves"), never directives
- unconditional "now is the selling window" on three pieces made an answer tell a
contend-on-a-clock team to liquidate its rank-carrying RB room; the docstring adds
the precedence rule (path outranks piece notes) and its exception (clock_mismatch's
NAMED pieces are the path-sanctioned conversions - present them as live, not
"monitor"). (2) A barbell contender never carries Contend's "not declining, no
clock" window_note - both clauses are false of it; it gets a barbell-aware note.
(3) CONTEND_CHOICE_NOTE's "neither is urgent, no clock" routes only to ALIGNED
contenders; unaligned ones get PRESS_CHOICE_NOTE (same two paths, states the lean,
"the aging half sheds value while you deliberate - waiting applies to the ask,
never the decision"). Also: no contract data exists in any payload, and the
docstring now says so (an answer dressed runway numbers as "expiring contracts").

**The table is deterministic; the logic lives in the agent** (owner's deciding rule,
2026-08-15): the UI renders measured facts - chips, values, colors, counts - and
carries NO prose judgment beyond a hover tooltip. Every "should" sentence belongs to
the agent, which holds all the payloads and can fuse them per question. This rule
settled three disputes in one evening: the expanded panel's blurbs (removed - the
grey line contradicted the orange one on shivvv), the needs-x-path interaction
(not wired - the two chips sit adjacent and the agent fuses them), and the key
(compressed to six short cells). The proof case: Travis Etienne is never named
anywhere, and his orange age still sticks out - which is the point. When a fact
matters, make it visible; when it needs a sentence, it is the agent's sentence.

**Alignment is a dial, not a switch** (2026-08-15, owner's Vicdank/Smith reads):
aligned means NO FORCED MOVE, not perfection - a team can always trim toward better
alignment. Aligned reasons carry a brief clause when the trim is real: measured on
the RUNWAY bar (share of started production inside MIN_MEANINGFUL_RUNWAY - the same
bar position_clocks uses), not the declining bucket, because a 27.8 WR with 1.2
years is late-prime by bucket but expiring by any honest read (DeVonta Smith).
Floor AGING_WORTH_NOTING_PCT=10: below it, "nothing aging out" is honest wording.
Emphasis is asymmetric by design (owner's rule): UNALIGNED rows carry specifics BY
NAME ("the pieces on the clock: ...") because telling them how to become aligned is
the bright chip's whole point; aligned rows stay brief - their specifics are the
agent's job. Related fix: clock_mismatch checks runway explicitly rather than
inheriting it from win_now_core (either-currency membership added long-runway
production-priced pieces - a 5.1-year pocket QB was being flagged as aging core),
and production-priced pieces WITH runway get their own price_note (the Goff shape:
the market doubts the asset, not this season - fine to hold, nothing forces a sale).

**The dial cuts both ways at the ends** (2026-08-16, owner: "I fear this agent misses
the concept that the secondary tier is less real at the top end and the bottom end...
contending teams aren't buying AJ Brown, only Pushing teams - I don't think that is
true. Build teams should be trying to sell their aging assets too, it's just not as
clearly urgent. Just because you are contending doesn't mean you wouldn't try to win
harder"). The old Contend note said "nothing needs to be bought at a premium, and
nothing needs selling", and the model heard the first clause as "isn't buying". Now:
the `contend` posture says a contender is STILL A BUYER of production that upgrades a
slot, at fair prices, without spending its future - what the rank removes is the
premium, never the direction; the `build` posture says a working rebuild STILL SELLS
its aging pieces at the market's pace - the difference from `sell` is tempo, not
direction. System-prompt principle F states the dial rule generally.

**The waiting window runs to the trade deadline** (2026-08-16, owner: "the general
waiting window could be until the trade deadline, not week 5 like it says - often
closer to week 10 irl"). The wait/decide postures and the middling timing notes say
the decision has until the deadline (a few weeks of results usually settle it sooner);
rule 15(b) still bans invented week numbers but names the deadline as the one timing
anchor the model may use (principle G).

**The window-label retirement** (2026-08-16): the model no longer sees `window` at
all. Every model-visible payload (get_team_state rows, the trade_targets me-block,
wanted_by lines, seller/owner fields, pick notes, trade_eval sides) ships path +
`posture_note` + `path_edge` instead of window + window_note + path_edge. The
regenerated 12-team slate found the residue's ROOT in the system prompt itself
("team windows (Push/Contend/Middling/Rebuild)"), echoed by five of twelve answers,
and Push (an aligned contend-on-a-clock) vs press (an unaligned barbell) are
DIFFERENT cells, so the echo was sometimes wrong, not just old. `posture_note` is
keyed by path (POSTURE dict) and carries the premium/tempo doctrine plus the measured
numbers; `path_edge` names the PATH across a contention tertile line, computed from
the same composition (trajectory edges are no longer hedged - the path never reads
trajectory; TRAJECTORY_NOISE_POINTS and the EDGE_CLOCK/EDGE_FLAVOR templates went
with it). Windows stay computed forever: `window` still dispatches the trade paths
(Rebuild -> pivot, Middling -> both) and keys the pick-slot tier; it is the
measurement layer, just no longer spoken. The eval `case_team_window` became
`case_team_read`: the path word must appear, no retired label may, and the two
rule-15 invention classes are asserted absent. Redline #2 of trade-eval landed in
the same change: `_side_read` reasons from `path` (a press team taking back futures
is doing its own path and is no longer scolded for it; only aligned `contend` gets
the mirror warning). Correction recorded in passing: "NO CONTRACT DATA" was FALSE as
a global rule - get_roster_detail ships real nflverse contract terms - so the rule
is now "runway is never a contract; cite contract terms from that field or not at
all" (BenSimonds' "durable contracts" was mostly real data, wrong only about Kraft).

**Core membership is either-currency** (2026-08-15, the Walker-but-not-Henry note):
a piece is core-sized if EITHER its dynasty value or its redraft value clears the
league's top-10% bar (`cornerstone_threshold`, same percentile both currencies).
Dynasty value alone made the deepest producers invisible - Derrick Henry's price had
already collapsed, so he missed `win_now_core` and every note built on it while
carrying more of his team's production than anyone; shivvv's aging-core note named
Walker and skipped Henry, which read as wrong to the owner because it was. Two
price_note variants say which door a piece came in: "cornerstone-priced" (value
clears the bar, only the clock keeps the tag off - the sell window) and
"production-priced" (production clears the bar, price no longer does - the market
already discounted the future, so his buyer is buying this season only: the rental
market shape). Production-only qualifiers can never be cornerstones - "build around"
is a dynasty-price claim the market is explicitly not making about them - so they
always land in `win_now_core` regardless of runway. The UI's Core column shows the
split directly: bold = build-around, orange = production-priced (the James Cook /
JT+Barkley / Jefferson / Henry row the owner asked to SEE).

**Clock mismatch** (`clock_mismatch` + note, per team): `_cliff_case` pointed at
one's own roster - an ascending-tilt team STARTING a short-runway premium piece
(win_now_core's starters) is holding seasons it isn't built for. Exists because the
window answer kept describing a rising team's young core without naming the one
starter whose clock disagrees with it (live: a 45%-ascending Middling roster starting
a 0.1-year RB the market still prices as a cornerstone - the textbook sell, invisible
to the reader). The note tells the model to raise it whenever describing the window,
not only when asked about selling. Gated on tilt: an aging team starting aging pieces
is consistent, not conflicted.

**Positional clocks** (`position_clocks` + note): the split's sharpest challenge -
"this Contender's RBs are all old, so should Push and Contend merge?" - resolved by
getting more precise instead of less. The clock can be positional: a durable roster
whose ENTIRE started RB production sits inside the buyer's two-season bar (live:
Walker 1.2 / Henry -5.6 / Swift -0.6 under young QBs and WRs) reads "Contend, steady"
because the young positions swamp the aggregate tilt. Per position: the share of
started production inside MIN_MEANINGFUL_RUNWAY; a majority means the room ages out
together. The NOTE fires only on Contend - the one label that claims "no clock" -
and says to act like Push at that position while Contending everywhere else. Merging
the flavors instead would have told this team to pay Push premiums at positions where
patience is free. clock_mismatch catches the premium-priced individual; this catches
the room whose pieces are individually below that bar.

| state | flavors | wants | can spare |
|---|---|---|---|
| **Contending** | `Push` (clock) / `Contend` (none) | production now | future years |
| **Middling** | `rising` / `falling` / `steady` / `misaligned` / `convertible` | information | nothing freely |
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

### Tier 1 is a distance (`contention_tiers`)

Tier 1 used to be the count/3 tertile of contention rank: every league 4/4/4 whatever
its shape. On the projected-points measure the shape is visible and the count was
wrong at the seams - BBD's #5 sat 0.8 ppg behind #4 and read "fringe"; Plug's #5 was
1.0 behind #4 and read "decide". Now (owner-approved 2026-08-17, "more dynamic for
sure"): **contender = at or above the league's median lineup +3%; also-ran = at or
below the median -4%; the rest is the middle** (`CONTENDER_ABOVE_MEDIAN` 1.03,
`ALSO_RAN_BELOW_MEDIAN` 0.96). Anchored on the median, not the best, so one runaway
roster cannot demote the whole field. Calibrated on the 48-team corpus against a
best-anchored band (>= 92% / <= 85% of best): both fixed the four clear misses
(DerekGeter, SeanCenter contenders; obamagg48, jmcgrath77 also-rans); the median band
keeps Ben in the bottom (owner's read) and its only "misses" are teams on the line
(Paulyt +2.8%, kieran +2.2%), which the hedge names rather than a threshold nudged to
fit four leagues. Sizes came out 4/3/5, 4/4/4, 5/2/5, 5/2/5. XFL walk-through: median
133.2 -> lines 137.2 / 127.9; kb 138.8 contends, kieran 136.1 is fringe (hedged toward
press), spugz 127.7 is an also-ran (hedged toward decide). `leverage` reads the tier,
not a rank tertile.

### Boundary noise and the hedge (`path_edge`)

The tertiles are hard breakpoints on continuous measures, and the core labels used to
flip a team's whole identity when a value refresh nudged one rank ("ask twice, get two
windows" - it happened live twice, once mid-refactor when an nflverse role flap moved
one runway). The fix keeps the tier as the label and ships the label's *stability*
next to it: any team within refresh noise (NOISE_BAND) of a tier LINE - the median-band
lines since 2026-08-17; before that, the two teams straddling a rank tertile - carries
`path_edge`: prose naming the tier across the line, saying the flip would be pricing
noise, and telling the reader to hold both tiers' advice live.

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

- `critical` - can't field the slots: a body is needed NOW. Whether what's started is
  good rides in `body_solid` and the note, never in the level.
- `weak` - slots fillable, group bottom-tertile or under `WEAK_VS_MEDIAN` (0.5) of the
  league median (the median test catches skewed positions rank alone hides): an
  upgrade eventually, NOT depth. Had no representation at all under the old rule.
- `ok` - includes mid-league with no star, which is not a need.

**Why count-short is one level** - this took three rounds of tester pushback to settle.
Ranking the hole-dragged group total called a Mahomes-plus-nobody superflex room "among
the league's worst" (round one). The quality-split level invented to fix that
("top-heavy") failed both ways: its boundary could not be placed - every league-relative
bar scored a fine lone QB (Love, 0.87x the median started body) and a fringe lone RB
(a 28yo RB2, 0.68x) identically - and its NAME read back to the manager as "RB-rich"
(round two). The resolution is the manager's own articulation (round three): *needing an
RB2 badly is different from having two mid RBs and no slot need* - the level is the
count shape, and per-body quality (averaged over the USABLE bodies' own values, a
numerator/denominator match its first version got wrong) picks the sentence: "the good
players are already here" vs "this hole needs both a body and quality".

Quality is not asserted below `MIN_TEAMS_FOR_QUALITY` (4) - `body_solid` stays None,
which is not False. Downstream, the shape decides the fix: the buy path applies the
weakest-starter upgrade bar, waivers only fill count-shaped needs, and the persuasion
tier requires beating the weakest starter at every need level.

**The flex is an open upgrade slot** (`flex_bars`, `flex_occupants`, the `FLEX` entry) -
the positional bars cannot judge flex bodies, by arithmetic: replacement level counts
only dedicated demand, so leaguewide supply above the bar roughly equals dedicated slots
and the flex starts (24 of them in a 2-FLEX 12-teamer) are fed by definitionally
"below-bar" players. Applying the positional grammar to real flex occupants flagged 9 of
12 teams critical, including the league's rank-1 flex (Ashton Jeanty + Rico Dowdle). So
the FLEX gets its own bars from the **flex tier**: the next `num_teams x flex_slots`
players by redraft value once every dedicated slot has taken its bodies, position-blind
within eligibility - which position fills a flex is an outcome of `fill_lineup`'s
residual, never an assertion (distributing flex demand into per-position slots was tried
and flagged a team critical at RB while its WR surplus covered the flex fine).

Same critical/weak/ok grammar, two bars:

- `critical` = an occupant below the tier's last player (444 in XFL - can't field a
  flex-startable body at all). Fired for exactly the four genuinely thin rosters.
- `weak` = fielded, but an occupant below the **top third of the tier**
  (`FLEX_COMPETITIVE_FRACTION`; J.K. Dobbins, 796, in XFL). The midpoint was rejected by
  the owner ("Tony Pollard is still a bad flex for a competing team"); anything from
  top-third to top-sixth (796-952) labels identically on the live league, and above 952
  the bar starts calling the #1 production team flex-weak (Dowdle is its actual flex), so
  top third is the defensible sentence. Owner sign-off: "Dobbins honestly feels about
  right. Good enough but for sure upgradable."
- Rejected: the league-median real flex start (1,028) as a bar - half the league sits
  below its own median by definition, so it flags six teams forever. Also rejected:
  excluding the bottom-25% teams' demand from the tier ("incomplete rosters") - chopping
  their demand without chopping their supply leaks their startable players (Etienne,
  Swift) into the tier as phantom flex bodies, and the consistent version read ZERO teams
  ok. The incomplete-roster intuition is already priced in: those teams still start their
  startable players; what they lack is the flex depth the all-teams tier measures.

The entry's `weakest_starter` is the **displacement bar** - the roster's own weakest flex
occupant, per-team and self-calibrating, and ANY eligible position above it improves the
lineup. That is the payoff downstream: the buy path fills a FLEX need from every eligible
position that is not itself a need (owner, on a team reading ok at RB with Kyren + a
floor-value TE in the flex: "another RB could help him out"), with the margin stated "vs
your weakest FLEX starter" and `for_slot` marking which need a target fills. QB never
enters any of this: SUPER_FLEX is folded into QB everywhere, unchanged.

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

**The nearest door** (`nearest_door`, on every non-starter offered as sellable and on
stranded entries): WHO holds this player's cheapest reachable slot (dedicated or an
eligible flex) and the production margin to it. "Every slot is held by someone better"
was true but absolute-sounding - a TE 61 points behind the last FLEX read back to a
tester as "you have 5 WR slots so he can't start"; the model invented slot mechanics
because the payload stated a verdict without its margin. The margin self-defends in
both directions: 61 is a competition, 2,764 is a wall, and no wording has to pick
which. Skipped when the margin comes out negative - declared starters and the optimal
lineup can disagree, and a player who would beat the door isn't locked out at all.

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

**Production adds - the rental market for buyers** (2026-08-16, `_production_adds`).
Owner: "shivvv should know that trading a second for Evans or McLaurin is generally
directionally sound and possible" even with no WR hole - and the need-gated buy list gave
a no-need contender nothing, while `value_upgrades` (ranked by dynasty value, capped at
4 per move) let the deepest-discount vets fall off. The list: sellers' PRODUCTION-PRICED
pieces (`value_basis == "production"` - declining, or inside the final year) that would
start for this team today - beat the weakest starter at their position or the weakest
flex occupant - ranked by that margin, cheapest in dynasty among equals, capped at 6.
Studs a seller holds are deliberately excluded (a first cut ranked purely by production
gain listed Chase, Jefferson, Achane - those belong to value_upgrades/persuasion, and
they crowded out exactly the cheap production this list is for). Both sides of the
direction gate are satisfied by construction (buyer acquires production, seller parts
with it), which is what makes "a 2nd for one of these" ordinary. It is also the natural
second leg of a plan (see Sequences).

**Bench surplus at a needed position is still an offer (2026-08-17).** The pool used to
exclude every piece at a position the team is short at ("trading it just moves the
shortage"). That was blunter than the situation: rjl starts Mayfield and Darnold at a
weak QB room and holds Stroud as a QB3 - moving Stroud costs his lineup nothing and is
exactly what a QB-critical seller (spugz) wants for Chase Brown or Montgomery+Sutton,
yet the blanket rule hid the whole idea. Now only STARTERS at a needed position are
out; bench pieces there stay offerable. Flows to the grounding check and the composer
through offerable_names.

### The counterparty (`counterparty.py`)

**Offers cannot cost the buyer more than the target brings** (2026-08-16, the
Pickens-for-Goff case). The buy list paired rjl's Goff target with Pickens - an
ascending starter, offerable because a contend-on-a-clock team spends future - and the
framer read that exact pairing as lineup -3,215 with a new WR hole. Owner's diagnosis:
"it's just hard to move cornerstones, because they are both ascending/prime AND
producing big time right now, so often not a clear reason to move them that helps
both sides." Rule: `_counterparty_fit` drops any offer whose `lineup_cost` (production
lost after the lineup refills) exceeds the target's redraft value - in both the
fills-a-hole branch and the accumulating branch. A target may therefore ship with no
paired offer, which is honest: he is still who to ring, and the price is a negotiation
(picks, or a cornerstone-for-cornerstone conversation) rather than a pretended
one-for-one.

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
  player), or a falling roster short of ascending value at any position. The composed
  line carries a window clause in two directions: Push/Contend buyers pay a premium
  for production (worth more there than here), and a Middling buyer reads as
  *undecided* - his need is real, but he hasn't committed to contending, so this buy
  would BE the commitment. Presented bare, his interest got a contender's urgency,
  which overstates both his motivation and the price he'll pay.
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

## Judging a proposed trade (`analysis/trade_eval.py`)

The question testers actually bring: "I'm being offered X and Y for Z - good deal?"
Every other surface discovers trades; none could judge one the user arrived with.

This is NOT the deleted `find_surplus` returning: that tool *generated* packages and
priced their balance by summing both sides, and the summing is why it died. Here the
package is the user's premise - the module never constructs one, never totals one, and
declares no winner by margin. Values ride per piece; judgment comes from four facts
this codebase already knows how to compute:

- **Best single piece** (`_best_chip`'s logic on the deal itself), players and picks
  alike: which side gets it. Consolidation favors that side; the side sending it needs
  the rest of the deal to buy something specific. One piece against one piece stays
  the only comparison.
- **Need changes against the real bar**: the whole league re-assessed
  (`assess_positions`) with the two rosters swapped, before vs. after, so the diff can
  only come from the trade. Declared starters are omitted from BOTH runs (stale in a
  hypothetical); that costs only the injury-exposure notes. "Closes the QB need
  (critical -> ok)" is a recomputed fact, not a position-label guess.
- **Lineup production delta** via `fill_lineup` - the cascade, not the traded pieces'
  raw values, because a vacated FLEX does not refill the way a manager assumes.
- **Timeline fit on what each side takes back**, with the bar matched to the window:
  a true Rebuild is judged on the buyer's two-season horizon (`MIN_MEANINGFUL_RUNWAY` -
  the next competitive season is past a 1.4-year piece, which is exactly the Higgins
  case), while a merely-accumulating roster is flagged only for a piece at his own edge
  (`INSIDE_FINAL_YEAR`, the `_sells_him` clock pointed the other way). Both bars
  already existed; the module introduces no threshold of its own.

**Picks are the main case, not an extra**: replaying every completed trade of the
current season across the three validation leagues, 25 of 28 included a draft pick.
A pick resolves against the sender's owned picks (`picks_by_owner` - keyed by
roster_id despite the name) under the same fuzzy-match-or-name-the-candidates
contract as players, and rides as a piece with its value and `slot_basis`. It never
enters the needs or lineup math (it fills no slot this season), it never trips the
short-runway flag (no runway is not zero runway - it is the longest-dated asset
there is), and a Push/Contend side taking one back is told the value pays after its
window.

Each side's `read` is composed from that side's own seat, and a trade can read well
for both - which is what a real trade usually is. Validated two ways: reconstructing
jwall's actual Higgins+Thornton-for-Fannin deal in reverse (the reversal reads as bad
for jwall on all three axes, which is the market agreeing with the trade he made),
and replaying the six current-season trades whose pieces are still where they landed -
no crashes, and every read survives an eye-check against how the league actually
discussed those deals.

**The framer (2026-08-16, exposed as the `evaluate_trade` MCP tool).** Owner's ruling on
what a trade calculator should be here: NOT one - "assets on both sides + roster-based
needs", no exposed math, "no calculator can ever be that good since things are so
situational". Two additions and a lens:

- **Each side is judged by the lens its own PATH sets** (`lens`, `goal`). Owner: "the
  goal is to end up with a better starting lineup for pressing path types, and more
  dynasty value overall (with package concerns instead of roster concerns) for
  production-selling paths." Buying paths (`contend`, `press`) -> the lineup lens:
  production after the lineup re-settles, holes closed and - owner's worry, "we want to
  be careful of creating new holes with the offers" - holes newly OPENED, which the
  goal line names in capitals. Selling paths (`sell`, `build`) -> the value lens: dynasty
  value in vs out across N pieces, judged with package concerns. `wait`/`decide` -> both
  lenses, both doors. Correction recorded on the way: press means BUY production now;
  selling the aging half is a press team's PIVOT branch, and the framer judges the
  chip's lean only (entertaining the pivot branch = scope for later, owner: "probably
  too much scope for now").
- **The shape ballpark** (`RETURN_SHAPES`, `_package_read`), owner: "bake in the
  discovered premium package numbers for ballparking, then start spot checking" -
  then, on seeing 3 RBs for Jeanty and 4-for-1 for JSN read as plausible: "the packages
  really never work like that... trading for studs like those would require a good
  player like Drake London and 2 first round picks. Can you check real league data?"
  Checked (research/stud_returns.py, 461 crawled trades with the best piece alone on
  his side, DP point-in-time values): a stud's return is a CENTERPIECE PLUS PICKS, not
  a pile - top-5% pieces come back as 2-3 pieces whose best is ~half the stud
  (q1-q3 0.43-0.66; London-for-JSN is 0.57), with a 1st in ~54% of returns, summing to
  ~parity; four-plus-piece returns are rare and pick-inclusive (1 of 18 was all
  players); firsts are a stud currency that vanishes below the top ~10% (41% -> 3%);
  below the top fifth, trades are 1-for-1 swaps. The fc_trades premium (n~93, all
  tiers: 2-for-1 at 1.36x) does NOT describe stud deals - the consolidation premium is
  a mid-tier phenomenon; at the top the binding constraint is centerpiece quality. So
  the ballpark is a lookup keyed by the best piece's value percentile in the league's
  pool (top-2/5/10/20/35%, mid), and it speaks SHAPE: what pieces of that tier have
  fetched (pieces back, centerpiece band, summed multiple, share with a 1st, share
  with no picks) and then what THIS return looks like against it - centerpiece share
  inside/below/above the band, picks and 1sts counted, a rarity clause for 4+ pieces
  and a "usually brings back a 1st; this has none" clause for pick-less stud returns.
  The one place this project sums values, and only because the benchmark was measured
  that way; system-prompt rule D/8 carve it out and forbid extending it. Owner's
  spectrum request ("make it more of a spectrum and look for other distinct shapes")
  is the tiering; position was measured and found to matter a little (RB studs most
  pick-inclusive, QB least - QB-for-QB swaps), not enough to key on.
- **Redline #2 closed by the window retirement**: `_side_read` reasons from `path`
  (a press team taking back futures is doing its own path; only aligned `contend` gets
  the mirror warning; the rental bar keys on sell/build).

Spot check on the five XFL 2 sample trades: JT<->Lamb reads +295 for kieran but "OPENS A
NEW HOLE at FLEX" (the exact caution the owner raised); Tet+Tate->JSN gets "1.07x,
lighter than 1.36x" and a -6,099 lineup for shivvv; the Henry-to-a-rebuild control fails
both seats; Barkley-for-a-2nd fails kieran's lineup lens and serves Vicdank's both. Live
through the agent: both answers lead with the goal lines, quote the ballpark without
extending it, and reason from path (bigbuttboi's read even names the tension between
his sell chip and a contention-shaped move).

**Sequences (2026-08-16, `evaluate_trade_sequence`).** Owner's question: "what if
shivvv and Ben did that trade, then shiv also traded some seconds to jq for Evans and
McLaurin?" A plan is not a trade: leg 2 is judged on the rosters leg 1 produced (players
AND picks chain - a pick sent in leg 1 cannot be sent again, one received can), and
`cumulative` is each team's net against TODAY. The value is the join neither tool made
alone: the Gibbs deal is +6,546 but opens WR critical; two 2nds to jq for Evans +
McLaurin closes it for +1,484 more; net +8,030, no holes - "the consolidation move plus
its backfill", which is how managers actually operate and which single-trade evaluation
structurally cannot say. Capped at TWO legs (`MAX_SEQUENCE_LEGS` - owner: "we don't want
it reasoning infinite or even long and difficult chains"); longer plans are judged two at
a time. Owner's follow-up ruling: this does not FIND plans - the agent composes them and
the tool checks; the "patch finder" (get_trade_targets on a post-trade roster) and
three-way trades are backlogged. Related gap noted: for a no-need contender the finder's
win-harder path is `value_upgrades`, ranked by dynasty value and capped at 4 per move,
so the deepest-discount vets from sellers (Evans, McLaurin) can fall off - a
production-ranked view for buying paths is the follow-up.

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
   core by `path_edge` (Team windows, "Boundary noise"): a label within refresh noise
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
   one day). **An unlabelled LIST is the same defect**: `you_could_offer` shipped as a
   bare array of alternatives, and the first hour of public use turned it into a priced
   bundle ("Offer: Fannin 3,650 + Shough 3,379" against a 4,473 target) - the exact
   additive-value error the tools refuse to make, with the rule sitting unheeded in the
   system prompt TWICE. Renamed `offer_any_one_of`, both list sites carry the no-bundle
   sentence, `my_offers` gained the note it never had. The field NAME is the cheapest
   place to put a constraint: it rides on every entry and cannot be skimmed past.
   **Coda**: those notes did not stop it either - because on that league the payload was
   over the wire limit and the model never received them (see "The tool result the model
   never saw"). A label cannot fix a defect in prose the reader is not given.

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

### Zero tools, again - and the guard for it (2026-08-17)

The first Cloud Run deploy's worst failure came back on staging for one session: the
MCP server never registered, the model had NO tool definitions (the log shows it -
1 turn, 2,381 input tokens, cache_read 0, format_tier null), so Haiku wrote
`<function_calls>` XML as text with parameters that don't exist and confabulated a
"mild win" verdict. Cause this time: the day-old warm-up thread inside the MCP server
started BEFORE serving, and cold nflverse parsing on a fresh container starved the
stdio handshake past the SDK's patience. Two fixes, both deterministic: the MCP
server's warm-up now waits 5s (after the handshake, never competing with it), and
`run_query` raises `ToolsUnavailable` when an answer contains tool-call XML and no
tool was actually called - real tool calls never appear in answer text - on which
the API drops that session (its client is the broken part) and answers once more on
a fresh one, and if that also has no tools says "the tools didn't load, nothing was
answered, no verdict was invented" instead of showing the fake. Logged as
outcome=no_tools so it can be counted.

### The counterparty: the other side of the table (2026-08-17)

The advisor speaks for one side of a trade; the composer's "Ask" now also runs the
OTHER manager - `agent.COUNTERPARTY_PROMPT`, a persona over the same tools and caps:
"you are {owner}, a trade has been proposed to you, decide it as {owner} would - ACCEPT
/ COUNTER / NO, the two or three tool facts that decide it, and if COUNTER exactly what
you'd need." One-shot on its own client (never the asker's conversation), streamed as
its own card ("how jq likely sees it"). This is the one multi-agent shape that earns
its place here: two agents with opposing stances over one deterministic referee (the
framer computes both sides' facts); the coordination is a Python function, not a third
agent. Live check: shivvv offers a late 2nd for Evans -> jq: "COUNTER - I'm on sell,
Evans is exactly the piece I should move, but that's not the price; I'd need a 2027
1st." The demo tier skips it (it costs an ask).

### Two tiers on one deploy: friends and the public demo (2026-08-17)

The friends link carries one shared key (`FF_LINK_KEY`) - not real auth, a brake that
keeps bots off the friends' budget and rotates by changing one secret. It cannot be the
public link: anyone holding it can burn the friends' day. So the bare URL is a second
tier, DEMO, that exists only when `DEMO_BUDGET_USD > 0`: the league table, rosters and
composer are free (they cost nothing but Sleeper/FantasyCalc fetches); model questions
are allowed but per-visitor capped (`DEMO_ASKS_PER_VISITOR`, by X-Forwarded-For) from
a separate small `budget.demo` ledger with its own friendly over-message. Sessions
carry a tier: a friend evicts the least-recently-used session of any kind, a demo
visitor may only evict other demo sessions and is told "busy" if the slots are all
friends - a stranger never kills a friend's live conversation. `/sessions`,
`/diagnostics`, `/activity` stay friends-only. A key-less visitor lands on
`DEMO_LEAGUE` (a real league to look at without owning one) with a banner saying what
the demo allows. Everything is env vars on the service; the friends experience is
unchanged. Owner: "rather live let people use it" than a demo video.

### The first friends-test feedback, and what each complaint actually was

One tester (the league's sports-modeling guy) produced three complaints in one session,
and each turned out to be a different class of defect - worth recording because the
surface read ("the agent was annoying") hid all three.

**"It kept saying Rice isn't a trade target."** A question SHAPE we could not answer:
every surface was team-directed ("who should I target") and he asked a player-directed
question ("how do I get X"). Absence from a ranked, capped, need-filtered list was read
back as a verdict, when absence has five meanings (not a scored need, capped out, below
the floor, non-seller, wire-trimmed) and none is "unavailable". Fix: `player_outlook` /
`get_player_outlook` - one named player, both sides of the call, composed entirely from
existing machinery (`_sells_him`, `_why_they_would_move_him`, `_counterparty_fit`).
Live, its answer landed on the tester's own plan: Rice's owner is a stalled Rebuild
(seller), and `offer_any_one_of` came back Goff/Darnold - "dump a QB", confirmed with
reasons. Plus one docstring rule on get_trade_targets: list absence is never a verdict
on a named player.

**"It told me to sell a QB to teams with great QB rooms, then said oops my bad."** The
advice was RIGHT and the label made it indefensible. Superflex: those rooms (Mahomes
plus nobody, Love plus nobody) genuinely need a second BODY - but group quality ranked
the group TOTAL, the empty slot dragged a one-stud room to "among the league's worst",
and the room read as "bodies AND quality" when only the body was missing. The hole
contaminated the verdict on the players. Count-short groups are now judged per
startable body (the read lives in `body_solid` and the note - see "Why count-short is
one level" under Positional needs for where the label design finally landed), the note
says "dragged by the empty slot, not by the players", and the `wanted_by` why carries
the shape ("1 startable for 2 slots - what they start is good") so the model can
survive the pushback it is guaranteed to get. The capitulation itself ("oops my
bad") is addressed as rule 13: re-check the disputed claim, then stand corrected with
data or explain what the number measures - most disputes are a label read differently
than it was measured.

**"Rice is undervalued" (his own model).** Not a defect to fix - a thesis to engage.
Rule 14: never argue the market price back, never adopt the user's number (no
projections here); state what the market says and what the price is made of, then
reason CONDITIONALLY on the thesis - is the owner a seller, does the timeline fit, does
being right make the buy better. A sharp user's model can beat FantasyCalc; this
product's job is the league context around that bet, not the bet itself.

### A target without a price is half a trade

Buy targets shipped "here is who to ring" beside a separate list of everything this team
could give up, and **no join between them** - so the pairing was left to whoever read it.
The model paired badly and the owner caught it instantly: a 28.7-year-old WR with 0.3
years of runway offered to a team 55% ascending against 8% declining ("buttboi would not
want DK Metcalf"). The machinery to answer this already existed and simply never ran on
this block - `_counterparty_fit` had been wired to the persuasion tier only.

Two filters now bound `offer_any_one_of`, each mirroring a rule already in the codebase
rather than inventing a new one:

- **Timeline, not just position.** A team whose tilt is ascending is *accumulating*, and
  `_sells_him` already says that team sells its own pieces inside `INSIDE_FINAL_YEAR` -
  so offering it one is the trade backwards. Same clock, other direction.
- **Proportion, both ways.** The buy side already refused targets above the asking team's
  biggest single chip (`beyond_your_best_chip`); the give side had no ceiling and proposed
  a 7,321 cornerstone QB for a 2,006 back. `OVERPAY_LIMIT` (1.5x the target's own price)
  is that same one-against-one comparison pointed the other way.

`targets_note` states what the field is and is not: a starting point for the
conversation, never a claim that the two pieces are worth the same. This project finds
who to call and what they want; it does not price trades, and a payload that lists a give
beside a get has to say so or it will be read as a valuation.

### The tool result the model never saw

The most expensive bug in this project, and the one worth showing other people, because
nothing about it looks like an AI bug until you measure it.

**Symptom.** Answers on the biggest leagues went subtly wrong in ways the small ones
never did: a three-player package priced as a bundle (the one thing every tool here
refuses to do), a manager addressed as "Owner 637083353878695936", meta-narration
("let me pull that with better visibility"), and once, a request that the USER paste
tool output back into the chat.

**The false diagnosis, twice.** Each answer also said some version of *"the trade
targets output was too large to fully display."* Both times this was written off as
confabulation, on what looked like solid evidence: `tool_errors` was empty, the run log
showed the call succeeding, and models do invent excuses. The reasoning was backwards -
absence of an *error* was taken as presence of the *data*.

**The measurement that settled it.** Compare what the tool returns against what arrives
on the model's `ToolResultBlock` for the same call:

| | bytes |
|---|---|
| `get_trade_targets` returned | 43,225 chars |
| model received | **2,271 chars (5%)** |

The harness replaces any tool result over ~50KB on the wire with this:

```
<persisted-output>
Output too large (52.6KB). Full output saved to: .../tool-results/<id>.json
Preview (first 2KB): [ ... ]
</persisted-output>
```

A file path the model cannot open, and the first 2KB of JSON. **The model was telling
the truth every time.** The 2KB happened to contain `me.cornerstones`,
`me.win_now_core` and `me.tradeable_surplus` - which is precisely where the packaged
names came from. It was not hallucinating: it was reasoning correctly over the only
data it had been given, and the missing 95% included every no-bundle note written to
prevent exactly that answer.

Why it stayed hidden: it is silent (no tool error, nothing in the log), it is
threshold-based (every eval fixture sat under the limit - jwall567 is ~31KB on the wire
and has never misbehaved across dozens of runs), and the failure presents as a
*reasoning* defect, which sends you to the prompt instead of the transport.

**The fixes**, in order of how much they matter:

1. **A wire-size guard** (`mcp_server._within_wire_limit`). Serialize, compare against a
   budget with margin, and if it does not fit, shrink `max_per_position` - the knob the
   tool already documents - then trim the longest remaining lists until it does. Every
   block survives at its best-ranked entries, and a `truncation_note` tells the model
   the lists are shortened so it cannot present them as the whole market. Worst case
   across three real leagues went from 92KB (undeliverable) to 42.7KB.
2. **Stop re-shipping what another tool already sent.** `get_trade_targets` included the
   asking team's entire `team_state` row - every cornerstone, sell candidate and surplus
   piece that `get_team_state` had just returned - 19% of the largest payload. Roster
   lists now have exactly one home.
3. **Bound the unbounded block.** `value_upgrades` had one move per beatable starter with
   no ceiling: 41% of a Middling report. It takes the same per-position cap, best gain
   first.

**Transferable lessons**, which is why this is written up at length:

- **A model claiming something about its own inputs is data, not noise.** "The output was
  too large" was a factual report about the transport, dismissed twice because it sounded
  like an excuse.
- **Absence of an error is not presence of the data.** Nothing in this system failed. The
  result was delivered, successfully, mangled.
- **Measure the boundary, not the endpoints.** The tool was correct and the model was
  reasonable; only the gap between them was broken, and nothing on either side could
  show it.
- **Thresholds hide in fixtures.** Every eval and every unit test sat below the limit, so
  a 100% green suite coexisted with a production-breaking bug for as long as the biggest
  league went unasked. The first friend to load a deep roster would have hit it.
- **A payload has a size budget like any other resource.** It belongs in the transport
  layer, enforced with margin, with the degradation labelled rather than silent.

**The tripwire that lied, twice.** The eval written to catch the packaging shipped with
a detector matching `"A + B"` on one line. It passed immediately - and the very next live
answer packaged three pieces across three sentences ("Lead with Fannin... Add Shough...
Sweeten with picks"). Rewritten as a whole-answer window, it then failed answers that
correctly offered one piece each to two DIFFERENT targets. Three versions, two of them
confidently wrong in opposite directions, each discovered by a paid live call. It is now
high-precision by design (a conjunction inside one clause; "A or B" never fires) with the
recall limit stated, and - the actual fix - **verified offline against the recorded live
answers**, so the tripwire is tested for free and can never go vacuous. When a check
passes right after the fix it was written for, prove it can still fail on the original
defect before believing it.

**The grounding check is the pattern worth keeping**: prompt rules are probabilistic,
so rule 6 (only name offerable players as trade-aways) is enforced by
generate-then-verify - `_banned_trade_names` recomputes the real offerable set from the
same Python the tools ran (`offerable_names` is the one shared definition across modes),
`_trade_violations` fires only on a banned name sharing a line with trade-action
language and no negation (the blunt version fired on every roster description; the
negation skip can miss "don't trade X, but do trade Y", accepted - a false positive
costs money and contradicts correct advice, a miss costs one ungrounded name), one
retry naming EVERY violation (naming one fixed one and left the other). **The retry is
stagecraft the reader must never see**: a live retry answered the CORRECTION instead of
the friend - opened "You're absolutely right - I apologize", claimed the tool output had
been "too large to display" (confabulated; the run's own log shows tool_errors empty and
every payload cached in full), and asked the user to paste tool results back. The
correction now carries its own voice rules (fresh complete reply, no apology, no
mechanics, never ask the user for data the run already holds), and the substantive eval
cases assert the punt phrases never appear (`_ask`). **Fixes that
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

Each row carries two labels, matching how a manager thinks: the window tag and one
shade word (flavor when it beats the trajectory - "stalled" over "steady"). The
contention tier was a third vocabulary restating what rank order and %-of-best
already show, and reading "Push" beside "contender" looked like two systems
disagreeing about one fact. Pick capital surfaces as `pick_share` ("19% in picks")
plus a sold-own-next-1st flag, and the Dynasty column shows starter dynasty value with
the actual future firsts held ("1sts: '27×2, '28") - the detail behind the percentage,
already computed inside classify_league and previously discarded after summing.

**The color single-source rule**: the UI computes no ages, buckets, or runways - it
receives `bucket`, `years_to_decline`, and `prime_span` from the same team_values
functions every trade tool calls, and only maps them to hue (life-stage bands: green
ascending, yellows across the player's own prime span, oranges across a
position-specific decline tail, dark red past it). Colors therefore cannot drift from
the trade age logic; a curve change server-side recolors the page automatically. The
one display-only judgment is the decline-tail shading, which lives past the
breakpoint where the analysis only says "negative runway" - nothing to contradict -
and is queued for measurement against the DP archive. The bands read as
value-trajectory claims, in the owner's words: green is gaining value, yellow is
holding it (which is genuinely what primes do - peak production, flat price), orange
is losing it, red is a short-term production loaner. Stated that way each band is a
testable prediction against the DP value history, same queue as the tails.

Clicking a team expands its full roster inline - position groups with dynasty values
and ages (ascending green, declining red), cornerstones starred, projected starters
bold, the picks with their market prices, and the clock-mismatch warning leading when
it exists. One deterministic fetch per team, cached client-side - the same
no-tokens-to-recite-a-roster doctrine as the table itself.

The table's "Core" column merges cornerstones with `win_now_core`, cornerstones in
bold. Cornerstones alone made the column lie by omission: a roster showed only
"Lamar" while holding CeeDee Lamb, because Lamb misses the tag on the CLOCK, not on
value. The reader wants the roster's headline pieces; which of them are young enough
to build around is a flag on the name, not a filter on the list.

### Trade ideas: the door into the composer (2026-08-17)

Owner: "a lot of info burying the agent to auto open the league table and the trade
thing at once ... populate 1-3 trade suggestions to the right of picks for the team you
click on, click to open the trade helper with that loaded." So the composer is collapsed
behind one obvious bar, and each expanded team row carries up to three STARTING POINTS
across the league (`/team/{owner}/ideas`, cached per team): from what trade_targets
already computes for the team and for every partner - a buyer's targets paired with the
piece that owner would take (`offer_any_one_of`), a rebuild's wish-list pieces paired
with its best sellable production, mirrored. The band is DIRECTIONAL, in the buyer's
view: he sends 0.9x-1.5x of what he gets in dynasty value (a hair under, 0.04, is not
worth stapling a 4th on; overpaying is what contenders do). One floor for every piece:
the aging discount is already in a production piece's dynasty price, so a second
discount there is double-counting - the earlier 0.7x floor for "production-priced"
pieces produced Cam Ward alone for Jonathan Taylor (0.69x; owner: "a slap in the
face") and Price + a 2nd for Taylor ("probably wants to be a first"). Below the floor
the light side tops up from its 1sts and 2nds only (3rds and 4ths aren't currency for
a real chip): singles then pairs, cheapest ROUND that lands first ("at least a 2nd" -
not a 1st when a 2nd does it) and within a round the NEAREST year first (a '27 1st is
what people actually offer, not the '29 - owner); beyond that nothing. Ranked by what
comes back, one per partner, no repeated outgoing piece; a pick tops up a side only
when the RECEIVER wants picks (rebuild/middle - a contender wants production); and for a
top-tier piece the sum is not enough - the return's centerpiece must clear the framer's
measured band (`RETURN_SHAPES` q1; Sadiq + a 1st for Jefferson summed to 0.85x and was
still 0.43 on the centerpiece against 0.64-0.84 - the owner's gut said "Jefferson might
be worth more" and the study agrees). Never a price verdict: click one and
the framer's impact appears, the assistant judges on Ask. Deterministic and ~0.3s once
the board is warm.

### The trade composer (staging only, 2026-08-17 v2)

Owner's verdict on v1 (two dropdowns + checkbox lists): "works but looks bad, doesn't
present info, options, and impact easily." v2, per his direction: **the two rosters
overlaid, with everything a team would NOT move greyed out** - the roster IS the option
list. Greying is `trade_targets.offerable_names` (plus `picks_to_trade_away`), the same
set the agent's grounding check uses, served by `/api/league/{id}/team/{owner}/movable`;
a stance toggle per team (a contender "if selling", a rebuild "if pressing", the middle
"as buyer / as seller") re-greys under a declared stance - the spugz case. Tap a row or
a pick chip to put it in the trade; the tray shows both sides and, on every change,
`/api/league/{id}/evaluate` (the framer's `evaluate_from_board`) returns the FACTS:
each side's goal line (lineup delta or value in/out, holes opened or closed) and the
best single piece - free per tap, no model. One button pins the structured question
to chat, carrying declared stances. The deciding rule holds: facts render, every
"should" is the agent's. Greyed rows stay tappable ("tap anyway if you insist") - the
user may know something the path doesn't.

## Known limitations / backlog

Measured or confirmed, none urgent, kept so nobody re-derives them:

- **The window hedge discloses instability rather than removing it** - `path_edge`
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
