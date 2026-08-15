# Roadmap

Where this project is going and why, so the grand plan survives any one
conversation. `CLAUDE.md` says how we build, `LOGIC.md` says why shipped things
work the way they do; this file holds what is NOT built yet. Move an item's
reasoning into `LOGIC.md` when it ships.

## Where we are (2026-08-13)

Live for friends: a dynasty advisor agent over deterministic analysis (windows,
needs, trade targets, single-player outlooks), grounded by generate-then-verify
checks, hard budget caps, evals, and a web UI. In flight on branch `trade-eval`:
a judgment-not-price trade evaluator and the beginnings of a real-trade market
dataset.

## North star

A fantasy assistant that knows the league better than its sharpest manager and
can *show its work*: every recommendation grounded in either deterministic math
or measured market behavior — never vibes. Dynasty first; redraft is the far
future state once the dynasty machinery earns it.

**Sharpened (2026-08-14): the globally optimizable metric is championship equity —
expected titles over the next ~5 seasons.** That is how dynasty pays, so every move
(trade, waiver, pick, hold) ultimately scores as ΔEquity. The architecture is three
components, each already in flight as its own track: (1) a season-level title model
(roster strength → placement → title odds, calibrated on the crawl's real
league-seasons; playoff variance is Track 4's math), (2) a roster TRANSITION model —
which is exactly the measured drift tables: tier-conditioned value/production
trajectories by position x age x archetype plus exit rates, (3) opponent symmetry:
every rival roster ages under the same transition, so equity is a share recomputed
per season, and "windows" become emergent (Push = equity front-loaded in season 1).
Uncertainty compounding self-discounts distant seasons. Two standing guards: ship
distributions and deltas with ranges, never point estimates - and validate against
the trade dataset by scoring thousands of REAL historical trades on ΔEquity and
checking that positive-delta sides actually went on to win more. Doctrine note: this
is the planned evolution of judgment-not-price, named - the rule was never "no
numbers", it was "market values don't add"; championship equity is an OUTCOME
currency, which is what the judgment was always approximating in words.

Three consequences (2026-08-14, owner's extensions): **strategy becomes
computable** - timeline alignment is win-positive because production moved into
your highest-share seasons compounds; league competitiveness sets the price of
pushing (the marginal equity of buying depends on rivals' equity curves); and
stabilize-vs-push while ahead is a choice between two equity DISTRIBUTIONS with the
same total (front-loaded vs back-to-back-shaped) - the model shows the trade-off,
the owner picks the risk appetite. **The hard breakpoints dissolve** - the recurring
cliff-edge defect family (window tertiles, need levels, noise bands) becomes
emergent descriptions of a continuous equity curve: labels stay for humans,
decisions ride the gradient. **The model self-grades** - anything we forecast that
the outside world also prices (player props and season totals at books, expert
consensus values) turns disagreement into data: our error or our alpha, refereed
only by a tracked proper-scoring calibration record, out of sample. Epistemic
default: a liquid book is calibrated by money, so disagreement starts as evidence
we're wrong. If the record ever shows edge, exploitation order is dynasty buys
first (home field, zero cost), real-money surfaces only with receipts - and if it
never does, we got free tuning against the sharpest calibrators there are.

---

## Track 1: Trade evaluation (branch `trade-eval`, active)

Judge a user-proposed trade per side without pricing packages. Built: best
single piece (players and picks), needs recomputed league-wide with rosters
swapped, lineup-cascade production delta, window-matched runway bars. Next:
Caleb's redline of the reads → **two surfaces off the one core**: a
calculator-style UI panel rendering the per-side reads straight from Python
(free per use, no retries, shareable — same pattern as the league table), and
an MCP tool where the agent adds the team-analysis flavor in conversation.
The UI promise "not a trade calculator" stays true on both: it judges, it
never prices. Training never touches the LLM.

## Track 2: The trade-market dataset

Real trades, to ground trade_eval's sentences in measured behavior and
eventually to learn from.

- **FantasyCalc stream** (`sources/fc_trades.py`, built): rolling ~49-trade
  window per stream (superflex + 1QB), no history, ~500-700/day/stream. Pieces
  carry point-in-time values resolved at poll time — FC has no public value
  history (probed thoroughly), so poll-time resolution is the only point-in-time
  there will ever be. **Demoted to nice-to-have (2026-08-13):** the Sleeper
  crawl also yields trades — historical, with roster context, valued via the
  DP archive — so this stream's remaining niche is a dense real-time market
  pulse with exact FC values attached and zero crawl infrastructure. Run a
  poller if it stays nearly free (a small cron); nothing downstream depends
  on it.
- **Early findings (n≈93, keep re-measuring):** consolidation premium is real
  and monotonic (2-for-1 clears at median 1.36x the single piece, 3-for-1 1.50x,
  4-for-1 1.64x — the eyeballed OVERPAY_LIMIT 1.5 sits inside the band); the
  median accepted trade is 23% imbalanced by additive math (quartiles
  10/23/38%), so "sides aren't equal" alone means nothing — the strongest
  empirical defense of judgment-not-price yet; pick-heavy packages clear cheaper
  than body-heavy ones (1.38x vs 1.57x) — extra bodies cost roster spots, extra
  picks don't.
- **Sleeper crawl** (not built): league graph compounds fast (3 seed leagues →
  15 dynasty leagues one hop out, 49 new users in 6 of them). Gives what FC
  can't: full multi-season trade history WITH roster context, actual weekly
  lineups and points, and picks that resolve to the exact players they became.
  Scale target: 2-3 hops ≈ hundreds of leagues, thousands of manager-seasons.
  Needs politeness, resumability, storage beyond a desktop JSONL.
- **DynastyProcess values archive** (verified): weekly per-player 1QB/2QB values
  with scrape_date, in git history back years — the retro point-in-time value
  source for historical trades. **GPL-3.0: fetch at analysis time, never vendor
  the CSVs into this repo.** Coarser format-conditioning than FC (only a
  1QB/2QB axis; no teams/ppr) — measured acceptable: on FC's side both missing
  axes are flat per-position scalars (ppr ~0.6-2%; team count ≤3.2%, worst
  case QBs in 10-team, near-zero spread within position), so they cannot
  reorder players within a position and are absorbed by the per-position
  calibration. The mesh comparison below matched the one axis DP has
  (value_2qb vs numQbs=2).
  **Meshes with FC (measured 2026-08-14, same-day scrapes):** FC's universe is
  a strict subset of DP's (397/399 join by name+pos, FC's entire top-200
  covered). But the headline Spearman 0.968 is tail-flattered — stratified:
  top-25 by FC value **0.476**, top-50 0.752, top-100 0.915, while
  within-position among the top-100 every position holds ~0.93. So the elite
  disagreement is about CROSS-POSITION ordering (is Bowers worth more than a
  QB2), and it is not necessarily miscalibration — rank consensus and market
  clearing prices are different instruments, possibly with era effects in
  position-class strength. The bridge translates, it does not correct.
  Consequences: (1) **per-position percentile space is mandatory** (inherits
  the 0.93s, sidesteps the 0.476); (2) elite cross-position value labels carry
  real uncertainty — prefer the production ledger (nflverse actuals, no
  translation problem) as primary signal for big-piece trades; (3)
  era-stability is unfalsifiable backward (no FC history) but bounded forward
  by re-fitting the calibration weekly from the fc_trades snapshots; (4) DP's
  picks file carries ECR only — derive pick values from DP's own ECR-to-value
  curve. **Bonus (2026-08-14): DP's `db_fpecr` archive
  carries BOTH dynasty and redraft FantasyPros ECR per scrape_date, back
  years** — point-in-time redraft rankings exist after all. Two uses: the fair
  lineup-decision baseline for manager scores ("start who consensus would have
  started AT THE TIME" — season-to-date stats are noisy in September and absent
  in week 1), and the eventual redraft track's data foundation.

**The falloff question (settled 2026-08-14):** no value-decay model in the product -
both currencies are live market measurements and dynasty value already prices the
market's own falloff forecast; a DIY decay curve would be us against the market with
no calibration (same reasoning that keeps contract years out). Runway answers the one
thing values don't: WHEN the slide starts. The UI's position-specific decline tails
(RB 2yr / WR-TE 3 / QB 5 to full red) are display intuition only. The one falloff
claim the product already makes - the cornerstone-priced sell-window note, "the
market has not discounted the remaining years yet" - is now TESTABLE against the DP
archive: measure realized value decay past the breakpoint by position; validates or
corrects the sell-window doctrine, and could graduate the display tails into
measured constants.

**The curve-validation study** (spec'd 2026-08-14, not yet run): for every
player-season in the DP archive, value at age A vs one year later, by position x
archetype - tags computed from THAT season's nflverse usage, never career hindsight.
Grades every claim the product makes: the breakpoints (RB 24/27, WR 25/29, QB
27/34-37), the pass-catcher +2 and pocket-passer extension, the display decline
tails (RB 2 / WR-TE 3 / QB 3), and the color-band predictions (green gains, prime
HOLDS, orange loses, red is a loaner). Methodological guards, the owner's first:
**survivorship** - players who crater vanish from the values list, so exits count as
value-to-floor and exit RATES by age ship alongside the medians (an age where 40% of
RBs disappear IS the falloff); bottom-censoring at DP's ~700-player floor reported,
not interpolated; injury-wrecked seasons flagged via nflverse rather than read as
aging; era drift checked (early vs late halves) before pooling. Findings graduate
into constants; misses correct them - either way the curves stop being vibes.
**FIRST RESULTS (run 2026-08-14, 8,328 player-year observations, 22 cohorts
2020-2026):** DP values are ECR-derived and roughly pool-conserved, so every rookie
class dilutes every incumbent - raw one-year ratios sit below 1.0 almost everywhere,
and the honest age effect is each age cell relative to its own position baseline.
Normalized: **market value peaks 2-4 years BEFORE the production breakpoint at every
position** - WR value turns at ~26-27 (production curve says 29), TE at ~26 (30), QB
at ~31 (34-37), and RB value has no plateau at all, descending steadily from 23 with
the steepening near 27. The market discounts ahead of the field. This VALIDATES the
sell-early direction of the sell-window doctrine while sharpening its wording: on
AVERAGE the market is already discounting from the mid-20s, so "the market has not
discounted him yet" is a claim about specific outliers (exactly what cornerstone-
priced flags), not about typical aging players. Product implication recorded, not yet
acted on: the codebase's runway is a PRODUCTION clock; the market runs an earlier
VALUE clock; both are real and the gap between them is the sell window itself.
QB detail: value growth is BEST at ages 25-30 (rel 1.14-1.26) and young QBs (21-23)
gain no faster than baseline - supporting prime-entry at 27 and cautioning against
reading a 22-year-old QB's green as market-safe.

**Pre-registered experience hypothesis: REJECTED by the held-out era.** In 2020-22,
same-age (22-23) rookie RBs/WRs crushed 3+year vets (1.07-1.25 vs 0.60-0.79); in
2023-25 the effect vanished or inverted (RB rookies 0.53 vs vets 0.64). Either a
rookie-mania era artifact or the market learning to price entry hype - both readings
mean it does not graduate. The overcook guard caught a plausible intuition exactly as
designed; the old-rookie (24+) case specifically never reached sample size and stays
open. Original hypothesis text kept below for the record.

Pre-registered hypothesis for the same study: **experience as a separate axis from
age** - an older rookie (RJ Harvey type) is age-late but experience-early, and the
NFL-adjustment period may make him behave like an ascender. Testable because DP
carries draft_year: among SAME-AGE players, do rookies/sophomores gain value while
third-year vets hold or fade? Graduates only if it survives held-out years. Overcook
guard, in the owner's words ("this feels like it could easily get overcooked"): each
archetype slices the sample thinner and offers another chance to find noise that
flatters an intuition, so hypotheses are named before looking - never fished for -
and one at a time.

**Representative, not sharp:** trade data describes what the market accepts, not
what is correct. It feeds realism framing ("a gap like this is the median
accepted trade"), never normative advice ("people do it, so should you").

## Track 3: Manager scores — the umbrella

Decompose "who is actually good at this" into channels, each measurable from
Sleeper's own logs, each with a fair-baseline correction. The trade dataset is
just this track's trading channel.

- **Lineups**: actual vs baseline points. NOT hindsight-optimal (that punishes
  good benches — more contestable slots, more chances to "lose") — the baseline
  is what a sensible default would start, and only contestable slots count. A
  manager with no decisions gets no credit and no blame.
- **Waivers**: downstream nflverse production per FAAB spent (% of budget, never
  raw dollars).
- **Trades**: two ledgers per trade — value (DP point-in-time, marked at +6/+12
  months) and production (nflverse over the window) — and the manager is graded
  on **the ledger their own window said to maximize**: rebuilders on value
  accrual, contenders on conversion efficiency. A contender "losing" on value
  while winning on production did their job. Window classified at trade time
  via our own classifier over the reconstructed roster.
- **Drafting**: value added over slot expectation, startup and rookie drafts.
- **Schedule luck**: all-play record vs actual record, so fortune is separated
  before anything is called skill.

**Three normalizations, always:** within-league (leaguemates faced the same
wire, budget, scoring — cross-league comparison happens in percentiles),
objective-conditional (graded against what the manager's window implies), and
horizon-explicit (every ledger marks when it is measured).

**Channel bleed:** a waiver steal that gets traded, a draft hit that lifts the
lineup denominator — value must be attributed to the channel that *sourced* it.
Per-player provenance from the transaction log: tedious, not hard.

**Validation set:** our own leagues first. Small n, every channel computable
today (lineup efficiency and schedule luck need NOTHING new — historical
seasons are in-season data), and Caleb knows who the sharp managers are — if
the scores disagree with reality, the metrics are wrong, not reality.

**Product form:** the league report card — "elite at trades, bottom-quartile at
waivers, +2 wins of schedule luck", with receipts. The most shareable thing we
could give testers, and it personalizes the agent's advice per manager.

**"What converts to wins":** regression of placement (and all-play win%) on
channel scores across manager-seasons. Placement is the headline but the
noisiest label; all-play and points-for carry the skill signal. Needs crawl
scale (~thousands of manager-seasons).

## Track 4: In-season (time-triggered — the 2026 season starts in September)

Everything shipped is preseason math, and says so. Real games change what the
right answer IS, not just its precision — this track is dated by the calendar,
not by preference:

- **Windows and needs re-weighted by results**: a "Contend" roster that starts
  1-5 is not contending; actual points and standings join roster composition in
  the classification, with the preseason basis phased down as weeks accumulate.
- **Playoff math**: all-play strength, remaining schedule, seeding scenarios,
  and playoff odds — the input that flips trade advice at the deadline
  (playoff-bound teams buy production, eliminated teams are sellers with a
  clock; "holds_to_win" becomes literal).
- **Playoff-week schedules**: a player's weeks 15-17 matchups/bye reality
  affects what win-now production is worth to a contender in December.
- **Season-adjusted trade evaluation**: trade_eval's production ledger switches
  from redraft-market proxy to actual rest-of-season outlook; the two-ledger
  window grading (Track 3) inherits the same adjustment.
- **Live manager scores**: lineup efficiency and schedule luck start computing
  for the current season as it happens — the report card gets a weekly pulse.
- **Projections stop being backlog**: in-season, "who should I start" questions
  arrive immediately, and roster values alone cannot answer them.
- **Pick-timeline fit** (owner-backlogged 2026-08-15, flagged as a rabbit hole): a
  pick's distance is its own window question - a 3-years-out 1st may be "almost too
  future to fit a rebuild window", and there is a world where a REBUILDER should be
  selling far-out picks (they mature after the rebuild should already be competing).
  The rental-market year-ladder math (a held 2029 1st drifts up ~55% becoming a 2027
  1st) is the same object seen from the other side. Too complex to encode now; the
  ruling that stands meanwhile: middling teams' picks stay askable in a rebuild's
  picks_to_acquire (they are close enough to converting that selling a pick fits
  their contend/sell path - the rising-Middling exclusion deliberately does NOT
  extend from players to picks, because picks are also the easiest trading material
  there is and belong wherever they make any sense), and rebuilds' own picks are
  never listed as acquirable (already true by construction).
- **Gap-grouping instead of tertiles** (owner-backlogged 2026-08-15): the
  contention tertile cuts at fixed ranks, but leagues cluster naturally - if
  teams 4 and 5 sit just under the top three and well above the middle, they
  belong WITH the top group; the honest boundary is the biggest gap in starting
  production, not count/3. window_edge already hedges the symptom (labels within
  refresh noise of the line); gap-grouping removes the cause. Applies to tier 1
  and to the asset tertiles leverage uses. Guard: small leagues need a minimum
  group size, and the grouping must be stable under weekly value refreshes
  (the same stability test the tertile hard-breakpoint fix already passes).
- **Contend harder vs contend longer** (owner-backlogged 2026-08-15): the aligned
  contender's real question - convert future into a stronger NOW, or keep the
  young core and contend more consistently now AND later - is a durability
  trade-off the label system deliberately does not answer (its chip just says
  "buy"). It becomes computable exactly when championship equity does: two
  5-year equity curves, one peaked and one flat, compared honestly. Until then
  the nuance stays in the hover, not in a heuristic.
- **The rental market for deep-decline producers** (owner-articulated 2026-08-15,
  from the kierankieran barbell case): a deep-decline producer's value is not a
  smooth melt, because he always has a buyer class - any team whose path says
  "buy" pays the discounted price for production it intends to consume now (the
  two-clocks surplus bought as a rental; the owner's own Push team would buy
  Barkley in August). The market runs year-round wherever contenders exist,
  PEAKS at the trade deadline (playoff-bound buyers get desperate and specific),
  and then closes for the season - which is what makes the deadline the deadline.
  Consequence for an unaligned push-or-pivot team: the branch decides
  which pieces move first. Push -> sell the mispriced-future piece you have
  redundancy in (the JT shape: barely past breakpoint, still cornerstone-priced);
  pivot -> sell the deep-decline producers FIRST (the Barkley shape: already
  discounted, but rentable) because their last real market expires with the
  playoffs - and realistically, a true pivot sells both, the rental at the
  deadline bid and the mispriced piece before its own clock catches up.
  Measurable from the crawl before it becomes advice: do deep-decline producers
  on losing teams fetch a premium near the deadline vs the same profile in the
  offseason? The rental premium either shows up in real trades or it doesn't.

## Engineering discipline for the model era (2026-08-14, owner's spec)

- **The crawl's full shopping list**: per season chain - league object (scoring +
  roster settings), users, rosters, ALL transactions, ALL matchups, drafts, traded
  picks. Matchups carry per-player LEAGUE-SCORED points - production in the exact
  format, no conversion - which is the ground truth the simulations stand on.
  Point-in-time NFL stats come from nflverse (already historical, gsis-keyed);
  point-in-time values from the DP archive.
- **Scale discipline**: the historicals are RANKS (FantasyPros ECR) and 1QB/2QB
  values, NOT league-format-calibrated and not FantasyCalc's scale. Never mix
  scales: rank/percentile space per position internally, the measured per-position
  DP-to-FC calibration at the bridge, and league-scored matchup points wherever the
  question is production. Anything cross-scale states which bridge it crossed.
- **Manager execution in the simulations**: rosters don't play themselves - Track
  3's measured manager scores (lineup efficiency, waiver capture) become an
  execution parameter, which also tests whether good managers are more predictable.
- **One box at a time**: every model component is a separate module with a frozen
  interface and versioned measured constants (the market_drift.py pattern - table +
  provenance + regeneration command). Tune one box against its own tests; no
  component reads another's internals. The alternative is the cascading refactor
  this project has avoided for a month by catching label drift early.
- **Drift detection as a framework**: every measured table gets a cheap re-fit
  check (the tep_drift_check pattern, generalized) - re-run the measurement, diff
  against the frozen constants, and a threshold breach is a loud failure, never a
  silent one. Calibration records are append-only so degradation is visible as a
  trend, not discovered in a bug report.
- **Serving stays thin**: heavy math runs offline (research scripts, batch jobs,
  the crawler) and ships frozen artifacts - tables, constants, calibration files -
  that the Cloud Run service only READS. The single-instance in-process design
  keeps working precisely because simulation compute never moves into the request
  path. Scale the batch plane, not the serving plane.

## The format-conditioning rule (applies to every track)

Anything that enters a training or measurement set carries its league's
settings AS OF THE EVENT: ppr, superflex, teams, roster slots, TE premium.
Sleeper makes this tractable — each season in a chain is its own league object
with its own scoring_settings and roster_positions, so settings are historical
per season by construction. Values must be format-matched too (FC per-format;
DP only 1QB/2QB — use the right column, record the residual as noise). Pooling
across formats without conditioning would blend incompatible markets and every
number would be quietly wrong.

## Farther out

- **Projections source**: still the biggest single upgrade to the live agent's
  advice (jwall's sessions are the standing argument).
- **Sportsbook lines / news-flow value arb** (ordering decided 2026-08-14:
  SEASON-LONG lines first - player season totals and futures for calibration and
  draft-season/dynasty-valuation use - before any week-by-week props; weekly is an
  in-season Track 4 consumer later): the long-held idea — market
  dynasty values move slower than real news; sportsbook lines (and possibly
  curated Twitter/X flow) lead them. Find the lag, surface the buy/sell window
  before the market reprices. Once an odds feed is pulling for our own
  projections anyway, **surfacing betting angles becomes a cheap side product**
  of the same data — same feed, second consumer. Anything with a paid feed
  obeys the CLAUDE.md rule: budget cap before it's wired to anything
  automated.
- **Community heuristics as hypotheses**: mine FF YouTuber/analyst transcripts
  for the folk strategies they preach ("stack cheap upside RBs in redraft",
  "deep benches are for upside plays, not floor") — then TEST them against our
  outcome data before any of it touches a recommendation. The creators are the
  hypothesis generator; the dataset is the referee. A heuristic that survives
  measurement graduates into LOGIC.md with its evidence; one that fails is
  worth a write-up too. (Transcript access via legitimate caption APIs, and
  LLM summarization of transcripts obeys the budget-cap rule.)
- **Redraft**: far future state. Most machinery transfers (lineups, waivers,
  luck, format conditioning); windows/runway/picks do not. Do not build toward
  it speculatively — note what transfers when designing, nothing more.

## Sequencing (plan of record)

1. Finish trade_eval to Caleb's taste; wire as MCP tool behind the doctrine.
2. (Optional, cheap) FC poller as a small cron for the market pulse — demoted,
   nothing depends on it; the crawl + DP archive is the primary trade dataset.
3. Manager scores v1 on our own leagues (lineups + schedule luck first — zero
   new data needed — then waivers, drafts, trades with the DP join).
4. League report card as a product surface for the friends test.
5. Sleeper crawler at pilot scope (one hop), then scale; re-run everything at n.
6. "What converts to wins" regression; only then talk about learned models.
