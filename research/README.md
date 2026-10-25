# Research

Where the project's intuitions go to be tested. The product's rule (CLAUDE.md) is
that every heuristic ships with its reasoning in LOGIC.md; this directory is the
step before that - the studies that decide whether an intuition becomes a constant
at all. Everything here is deterministic, offline, and free to re-run: no LLM
anywhere in the loop.

## The method, which is the point

The product's aging logic was built from inspecting real league distributions and a
week of the owner's eye-tests ("a 23-year-old RB should read green", "any QB is done
by 40", "old rookies are still ascending"). Each of those is a claim about how value
behaves - so each one is testable, and the discipline for testing them without
fooling ourselves:

1. **Point-in-time everything.** Values come from the DynastyProcess archive
   (`sources/dp_values.py` - the git history of their weekly values file, ~330
   snapshots since 2020). Player archetypes are computed from data available *at the
   time*, never career hindsight.
2. **Survivorship counted, not dropped.** Players who crater vanish from the priced
   list; excluding them makes the surviving freaks look like the curve. Exits count
   at the destination snapshot's floor value, and exit rates ship beside the medians.
3. **Dilution normalized.** DP values are ECR-derived and roughly pool-conserved, so
   every rookie class dilutes every incumbent and raw year-over-year ratios sit
   below 1.0 almost everywhere. The honest age effect is each age cell relative to
   its own position's baseline.
4. **Hypotheses pre-registered, one at a time, confirmed on held-out years.** An
   effect that only exists in the years it was found in is noise wearing a pattern's
   clothes.

## Study: age curves vs. realized market value (2026-08-14)

`age_curve_study.py` - 8,328 player-year observations, 22 quarterly cohorts, each
player's value compared one year later, 2020-2026.

**Finding 1 - the market runs an earlier clock.** Relative to position baseline,
market value peaks 2-4 years *before* the production breakpoint everywhere: WR value
turns at ~26-27 (the production curve says 29), TE ~26 (vs 30), QB ~31 (vs 34-37),
and RB value never plateaus at all - it descends steadily from age 23, steepening
near the production breakpoint of 27. The product's `runway` is a production clock;
the market runs a value clock that leads it. The gap between the two clocks IS the
sell window, which validates the sell-early doctrine while sharpening its wording:
on average the market is already discounting from the mid-20s, so "the market has
not discounted him yet" is a claim about specific outliers - exactly the players the
`cornerstone-priced` note exists to flag.

**Finding 2 - QBs peak late, in value too.** QB value growth is strongest at ages
25-30 (1.14-1.26x position baseline) and 21-23-year-old QBs gain no faster than
baseline. Supports the prime-entry-at-27 tuning, and cautions that a very young QB's
"ascending" tag is a production claim, not a market-safety claim.

**Finding 3 - the pre-registered experience hypothesis is REJECTED.** The idea:
same-age rookies behave more "ascending" than experienced players (the old-rookie
intuition). In 2020-22 it looked spectacular - rookie RBs/WRs aged 22-23 at 1.07-1.25
vs 0.60-0.79 for 3+year veterans of the same age. In the held-out 2023-25 era the
effect vanished or inverted (rookie RBs 0.53 vs vets 0.64). Either rookie-mania was
an era artifact or the market learned to price entry hype at the draft - both
readings mean it does not graduate into the product. The overcook guard did its job
on the first swing. The specifically-older-rookie case (24+, the RJ Harvey shape)
never reached sample size and remains open.

## Study: the two clocks, production vs value (2026-08-14)

`redraft_curves.py` - the dynasty cohorts joined to historical POSITIONAL redraft
ECR (FantasyPros via db_fpecr, weekly since 2020) by FantasyPros id. 3,301
dynasty-relevant observations, 2,951 with a production rank.

**Finding 4 - the clocks cross, and that IS the market's age discount.** Among
players still dynasty-relevant, positional production rank IMPROVES with age while
dynasty value falls: an RB at 23 ranks ~RB30 as a producer at 1.27x baseline value;
at 27-29 he ranks ~RB17 while priced at 0.37-0.66x. WRs: rank ~45 at 22 (1.26x) vs
~20 at 29 (0.72x). Selection is doing honest work here - the aged players still on
the list are precisely the survivors - but that is the point: the market's discount
on them is NOT about current production, which is the best on the board; it prices
the missing future seasons. This puts numbers behind the shipped persuasion-tier
sentence "the market discounts age the buyer isn't paying for", and behind green's
mirror: young players carry the highest values while ranking WORST as producers -
green is a price about the future, not the present.

**Finding 5 - "how chopped is red, really": the decline tails roughly measure out.**
Among players PAST the production breakpoint who still rank startable: RBs stay
57-66% startable with 62-83% one-year survival through +2 years past the breakpoint,
then 0% survival at 3+ - the display tail of 2 is about right. WRs hold ~70%
survival through +1, drop to 20% at +2 and 0% at 3+ - the tail of 3 is, if
anything, generous. QB numbers are small and scattered (the survivors keep
surviving); elite TEs that remain relevant at 3+ past the break are the Kelce
pattern - rank ~TE2, 89% survival, pure survivor selection at n=13. First-pass
support for graduating the RB/WR tails from eye-test to measured constants after
one more year of data.

## Study: archetypes and tiers (2026-08-14)

`archetype_tests.py` - role tags reconstructed from the season COMPLETED before each
cohort (no hindsight), contract state from the deal actually covering year t, joined
via the DP id crosswalk. Plus a within-position value-tier split (top-12 at time t).

**Finding 6 - pass-catching RB: confirmed, strongly, in both eras.** RBs at >=4
targets/game the prior season hold roughly DOUBLE the value of other RBs at every
age (25+: 0.52 vs 0.33 in 2020-22, 0.66 vs 0.38 in 2023-25). The +2 curve shift is
directionally right and possibly understated.

**Finding 7 - the rushing-QB discount is questioned, not yet overturned.** High-rush
QBs as a pool hold BETTER than other QBs in both eras (about 1.0 vs 0.5-0.8) - but
this test pools true rushing_qbs with dual threats (the Lamar shape our override
already exempts). Overturning the discount needs the elite-passer EPA split
reconstructed per season; until then the override stands with a flag on it.

**Finding 8 - contract year is a real value signal, era-stable.** Final-contract-year
players shed far more value than mid-contract players of the same position and age
band (RB 23-28: ~0.35 vs ~0.61 in BOTH eras; TE similar; WR strong in 2020-22,
weak in 2023-25; QB present at 29+). Mechanism is partly selection - teams extend
the players they believe in, so final-year status carries the team's own quality
verdict - but it is knowable at decision time either way. This REOPENS the parked
contract question with the polarity clarified: contract state earns its way in as a
VALUE-drift signal, not as an age-curve modifier.

**Finding 9 - tier conditions everything (the CeeDee Lamb objection).** Top-12
players at time t hold dramatically better than the rest of their position in every
age band, era-stable: elite QBs ~0.9-1.0 vs ~0.5, elite RBs ~2x their cohort, elite
WRs 28+ 0.68 vs 0.35. The all-timer-TE pattern (finding: top-3 TEs 29+ hold 0.72 vs
0.34) is one instance of this. Consequence already applied: the measured holding-
cost table (research/market_drift.py) is tier-conditioned, because a cohort median
without tier tells an elite player's owner a fate that mostly belongs to the
non-elite - the exact bleed into projections-arbitrage territory the owner flagged.

**What changed in the product so far:** nothing wired yet - research/market_drift.py (moved
out of analysis/ 2026-10-11, since nothing in the product imports it) holds the
tier-conditioned holding-cost table (measured constants with provenance; note-only
by doctrine, not yet wired into payloads). Everything else is recorded and waiting:
constants graduate when findings survive a re-run after more data accumulates.
Graduation queue, in order of evidence strength: the tier-conditioned drift note
into sell-window payloads; the contract-year signal as a second note; the decline
tails from eye-test to measured; the rushing-QB override review once the EPA split
is reconstructed.

## Study: the pick premium - what a lone 1st actually buys (2026-08-18)

`research/pick_premium.py`. Owner: "we may just be undervaluing 1sts - in reality they
come off as golden trade pieces. I'd probably never send one for some pseudo bums."
Scale-free test on the crawl ledger: every trade where ONE side is exactly one pick, what
PERCENTILE of player it fetched (DP snapshot at the time), then where FantasyCalc prices
that pick TODAY in its own percentile ranking, and the FC value at the fetched percentile.

| pick | FC today (12-team SF) | a lone one fetches | premium | n |
|---|---|---|---|---|
| this-year 1st (flat) | 2,875 | top-9% player, ~4,372 | x1.5 | 24 |
| next-year 1st | 2,063 | top-12%, ~3,885 | x1.9 | 38 |
| two-years-out 1st | 1,840 | top-11%, ~4,136 | x2.2 | 11 |
| this-year 2nd | 1,854 | top-19%, ~3,095 | x1.7 | 23 |
| next-year 2nd | 1,339 | top-26%, ~2,045 | x1.5 | 59 |

- **The market barely discounts future years; FantasyCalc discounts them hard.** A lone
  1st fetches a top-11% player (quartiles 8-16%) whether it is this year's or two years
  out; FC halves the value per year out. That gap - not the current-year number - is
  where "1sts are golden" lives.
- **A 1st buys a starter, not a rental**: 76% one player back, median age 26.3, only 27%
  aged 28+. Owner's "never for pseudo bums" is what the corpus does.
- **2nds carry the same shape at a lower tier** (top-22%), premium x1.5-1.7.

Caveats: selection - picks measured as a WHOLE side; a 1st stapled on as a sweetener may
carry a different premium and that is how the generator mostly uses them; cross-scale
(DP percentile then -> FC value at that percentile now) is the right comparison but not
precise to the hundred; n=11 / 5 at two and three years out. Not yet applied anywhere:
if adopted, one named multiplier per years-out goes into the framer's ballpark AND the
ideas band together, and the visible consequence is that ideas need FEWER picks (Price + a
2nd lands for Taylor again).

## Study: what a stud actually fetches - return shape across the value spectrum (2026-08-16)

`research/stud_returns.py`. Owner's prior, from the framer's first edge cases: "the
packages really never work like that [3 RBs for Jeanty, 4 bodies + a 1st for JSN] -
trading for studs would require a good player like Drake London and 2 first round
picks." Tested on the crawl ledger: every trade whose best piece was ALONE on his side
(a true "what does he fetch"), priced point-in-time from the DP archive (2QB in
superflex leagues), picks at FantasyCalc's current flat round averages (an
approximation - picks aren't in the player archive; fine for shape, not precision).
461 such trades.

**Findings, by the best piece's value percentile at the time:**

| tier | n | pieces back | centerpiece / stud | summed | has a 1st | no picks | 4+ pieces |
|---|---|---|---|---|---|---|---|
| top 2% | 57 | 3 | 0.50 | 0.98x | 54% | 35% | 24% |
| 2-5% | 88 | 2 | 0.54 | 0.89x | 53% | 29% | 12% |
| 5-10% | 120 | 2 | 0.65 | 0.88x | 41% | 27% | 5% |
| 10-20% | 123 | 2 | 0.66 | 0.80x | 3% | 49% | 0% |
| 20-35% | 57 | 1 | 0.46 | 0.53x | 0% | 89% | 0% |
| 35-60% | 16 | 1 | 0.33 | 0.33x | 0% | 100% | 0% |

- **A stud's return is a centerpiece plus picks, not a pile.** Top-5% studs come back
  as 2-3 pieces whose best is ~half the stud (quartiles 0.43-0.66) - Drake London for
  JSN is 0.57, dead centre - with a 1st in ~54% of returns and two+ firsts in 20%.
- **Studs move at roughly additive parity, not a premium.** Summed 0.9-1.0x at the
  top. This DISAGREES with the fc_trades finding (n~93, all tiers: 2-for-1 clears at
  1.36x, 3-for-1 1.50x). Reconciled: the consolidation premium is a MID-TIER
  phenomenon; at the top the binding constraint is centerpiece quality, and nobody
  pays 1.5-1.9x. The framer's ballpark now speaks shape for stud deals and keeps the
  premium marks only below the top ~10%.
- **Body-heavy packages barely exist at the top**: 4-for-1s are 12-24% only in the
  top 5%, and pick-inclusive; of 18 four-piece stud returns exactly 1 was all
  players. 3 RBs for Jeanty (0.51 centerpiece, zero picks, three roster spots) is
  the rare shape; 4 bodies + a 1st for JSN at 1.88x is a shape that does not occur.
- **Firsts are a stud currency.** Below the top ~10% the share of returns with a 1st
  collapses (41% -> 3%); the mid-tier pays in players and 2nds. Below the top fifth,
  trades are 1-for-1 player swaps (89-100% no picks).
- **Position matters a little, not enough to key on**: among top-10% studs, RB
  returns are the most pick-inclusive (16% without picks) and QB the least (44% -
  QB-for-QB swaps), TE studs fetch 3 pieces. One clause in the ballpark, not a
  dimension.
- **The summed multiple falls with tier** (0.98 -> 0.53) - partly real (mid-tier
  swaps are lopsided player-for-player), partly construction (a 1-for-1 return is
  <=1x by definition when the best piece is alone). So summed multiples are only a
  meaningful benchmark in the top ~10% where packages exist; below that, the shape
  IS the finding.

**Robustness and the adopted basis (same day, after the owner's plain-text review):**
the first table (all leagues, 1st = 3,200 flat) read "inside the band" on returns the
owner called "close but not enough" (JT for Skattebo + 2nd, Jefferson for Fannin +
2nd). Two known biases push that table low: 28% of survivors are VEGAS best-ball
leagues, which trade lighter (top-5% summed 0.77 vs 0.94 non-VEGAS; 44% no picks vs
27%), and a flat 3,200 understates every early 1st while the framer prices OUR picks at
FantasyCalc's slotted values (a 2027 1st Early is 4,532). Re-cut on non-best-ball
leagues with 1sts at 4,500 / 2nds at 1,800: top-2% centerpiece 0.50-0.71 (median
0.59), top-5% 0.64-0.84 (0.71), 5-10% 0.44-0.90 (0.63), 10-20% 0.36-0.82 (0.68);
summed 1.11 / 1.06 / 0.96 / 0.81; a 1st in 56% / 55% / 25% / 0%. Samples 36-70 per
tier - wide bands, stated. That table is what `RETURN_SHAPES` carries, and the
framer says WHERE in the band a return sits (below / low half / high half / above),
which is what turned "inside" into the owner's "close but not enough". Where the
adopted table is stricter than the owner (Chase for Egbuka + Odunze + a 1st reads
below the band), the seven real Chase trades in the corpus back the table: their
centerpieces were JT / CMC / Nabers / Hampton / G. Wilson / Dart - 0.54-0.85 - with
one pile at 0.37.

Caveats: DP values are the retro source (coarser format conditioning than FC); the
pick constant is 2026 FC and applied to all seasons; the VEGAS-LIFE ecosystem caveat
applies (re-check outside any single ecosystem before promoting a threshold).
Anchors for the eye: Josh Allen <- Jayden Daniels; Allen <- three 1sts + a 2nd;
Jefferson <- St. Brown + Hockenson; Bijan <- Breece + a 1st; Lamb <- three 1sts + change.

## Pace backtest (`pace_backtest.py`, 2026-08-21; simulator added 2026-10-18)

Replays 131 completed crawled seasons week by week (weeks 3+, 16,344 team-week
predictions), feeding only what a live system would have known, through the Monte Carlo
`season_sim.simulate` (live as `playoff_pace` since 2026-10-18), against the actual
winners brackets. The closed-form row is from the run before it was replaced. Brier,
lower is better:

| | made playoffs | bye | title |
|---|---|---|---|
| season_sim | **0.102** | **0.072** | 0.075 |
| closed form (retired) | 0.107 | - | - |
| top-k today | 0.158 | - | - |
| base rate | 0.246 | 0.144 | 0.080 |

Both playoff numbers are calibrated within a few points per decile. Title odds barely
beat the base rate and run hot at 30%+ (131 champions is too few to pin them) - computed,
never shown. Validates the math (record + schedule + rules + noise model), not projection
quality - ppg-to-date stood in for ePPG, which history does not keep. (Corrected
2026-10-18: the first run also counted 60 unplayed 2026 seasons - every score 0, a
placeholder bracket - and printed 0.153 vs 0.253 across 191 seasons; it also ignored
median-league games.)

## Projection snapshots (`save_projections.py`, from 2026-10-18)

Run weekly in season: saves Sleeper's rest-of-season weekly lines as they stand that day
to `data/projections/<season>/` (~0.5 MB, not committed). Why: Sleeper keeps each past
week's final projection (2025 week 5 is still served), but never what it projected for
week 12 as of week 6 - and that view is exactly what ePPG and the playoff odds run on.
With a season of snapshots, the pace replay can feed real ePPG instead of ppg-to-date.

## Schedule luck (`schedule_luck.py`, 2026-10-24) - manager scores, channel 1

Track 3's first channel (ROADMAP; lineups dropped by the owner - "start sit decisions are
overrated"). Luck = actual head-to-head wins minus all-play expected wins (each week, the
share of the league you outscored). Median-league games are left out of both sides: they
are an all-play game already.

**Pre-registered before the first run (2026-10-24):**
- H1: luck spreads about +/-1 to 1.5 wins (SD) over a 14-week season.
- H2: luck does not persist - year-over-year correlation for the same manager in the same
  league is near zero (|r| < 0.1).
- H3: all-play win% DOES persist (r > 0.3) - it is the skill-carrying half.
- H4: next season's win% is predicted better by this season's all-play win% than by its
  actual win% - the record regresses toward all-play.

**Result (2026-10-24, 131 seasons, 1,468 manager-seasons, 709 back-to-back pairs) - all four
held:** H1 luck SD 1.36 wins a season (10th-90th: -1.8 to +1.8) - often the playoff line.
H2 year-over-year r = -0.07 (SE ~0.04): it does not follow the manager. H3 all-play win%
r = +0.41: it does. H4 next season's win% tracks this season's all-play win% (r 0.39) better
than its record (0.31). So all-play is the skill-carrying half of a record and the rest is
schedule - the first feature for the "what converts to wins" model.

Gut check on the owner's leagues (live Sleeper data through `luck_by_owner`): XFL 2's 2025
luckiest was dezdroppedit27 (11-3 on 8.9 earned, +2.1) and its unluckiest kierankieran
(5 W on 7.5 earned, -2.5); this season bergenjay is 5-2 on a 42% all-play (+2.1) and rjl22
3-4 on 65% (-1.5).

## Rookie drafting (`draft_skill.py`, 2026-10-24) - manager scores, channel 2

Each rookie pick is scored twice against its slot, inside the same rookie class and QB
format (expected value = the mean at pick number +/-2 across every crawled draft that
year): **outcome** = DynastyProcess value 12 months after the draft minus that slot
expectation; **draft-day edge** = the player's value on draft day minus the draft-day slot
expectation (did the manager take someone the market already rated above the slot?).
Rookie drafts 2021-2025 (a full 12 months has to have passed); startups are a later, separate
channel. Players DP stops carrying are worth 0 - busting off the file is an outcome.

**Pre-registered before the first run (2026-10-24):**
- D1: rookie-draft OUTCOME barely persists for a manager - year-over-year r between 0 and
  0.15. Rookie hits are mostly luck at this sample size.
- D2: draft-day EDGE persists more (r > 0.2) - it is a process (following or fading
  consensus), and a process repeats.
- D3: draft-day edge predicts outcome positively - taking the player the market rates
  higher at a slot pays off on average, i.e. reaching costs.

**Result (2026-10-24; 90 rookie drafts 2021-2025, 3,534 scored picks, 311 back-to-back
manager pairs - mostly 2024-25, so read the r's as +/-0.06):** D1 HELD weakly - outcome per
pick, year-over-year r +0.13. D2 REJECTED - draft-day edge r +0.06: taking the player the
market rates above the slot is not a repeatable trait; it mostly records who fell to you.
D3 HELD - edge predicts outcome (r +0.34; each point of draft-day edge is still ~0.85 a year
later, so draft-day consensus is well calibrated). Exploratory, not pre-registered: the
outcome BEYOND what consensus predicted persists at r +0.06 and flips sign by year (-0.18,
-0.07, +0.14) - no detectable scouting skill. **Rookie drafting, at this n, is consensus plus
luck.** For the report card it is a "returns" line like schedule luck, never called skill;
for the what-wins model it is still a feature (value gained helps win, skill or not).
Crosswalk: nflverse ff_playerids sleeper_id -> fantasypros_id (16 of 3,748 picks valued 0 on
draft day). Startups are a separate, later channel.

## Waivers (`waiver_skill.py`, 2026-10-24) - manager scores, channel 3

Waiver share = the starter points a manager got from players he added in-season (waiver or
free agent, regular-season weeks), counted only in weeks they were in his starting lineup,
as a share of all his starter points that season.

**Pre-registered before the first run (2026-10-24):**
- W1: waiver share persists for a manager year over year (r > 0.2) - working the wire is a
  habit.
- W2: waiver share is NEGATIVELY correlated with the same season's all-play win% - teams
  with holes start more pickups, so raw share is need as much as skill.
- W3: waiver share does not predict NEXT season's all-play win% (|r| < 0.1) - in-season
  pickups patch a season, they don't build lasting strength.

**Result (2026-10-24; 131 seasons, 1,468 manager-seasons, 709 pairs) - all three held:**
median waiver share 4.7% (10th-90th 0-17.6%). W1 r +0.41: working the wire is as stable a
trait as all-play skill. W2 r -0.18: it tracks need. W3 r -0.06: it patches a season and
does not carry into the next one's strength. So raw waiver share is a habit, not a skill
score; "share given need" (the residual after all-play) is the candidate feature.

## Roster strength -> next season (`roster_strength.py`, 2026-10-24) - the future half of championship equity

Owner: a trade's currency is titles, not linear value - "you could be losing value on paper
but making your lineup way better," and a rebuild "taking shots on players who turn into
stars is good." Step 1 of grading trades in expected titles: learn how a roster at the end
of season t turns into all-play win% in t+1. Each roster is rebuilt from the last regular-
season week's lineup record and valued with DynastyProcess on Dec 10 of that season (1QB or
2QB value by format); features are league-relative (1.0 = the league's average team).
Known gap: future picks are not valued (the crawl's pick ownership is today's, not
historical) - they enter once drafted.

Features: all-play win% in t; total value; star value (top 3 players); starter value (top
N by value, N = starting slots) vs bench; share of value in players 28+.
Fit on target seasons through 2024, tested on 2025 (never seen in fitting).

**Pre-registered before the first fit (2026-10-24):**
- C1: end-of-season roster value predicts next season's all-play win% better than this
  season's all-play does (out-of-sample r higher).
- C2: shape adds to total value out of sample - holding total fixed, star value carries a
  positive weight and bench value the smallest (stars matter more than linear value).
- C3: holding total value fixed, a bigger share in players 28+ predicts a WORSE next season.

**Result (2026-10-24; 709 team pairs, target seasons 2021-25 - 413 of them 2025).** Held-out
2025: all-play alone r 0.36, roster value alone 0.45, value + all-play 0.44, + shape 0.44.
Re-checked leave-one-season-out (more robust with this skew): all-play 0.40, total value
0.47, starter value 0.45, all-play + starter + bench 0.48 (best). **C1 HELD** - roster value
predicts next season better than this season's results. **C2 REJECTED** - no star premium
for regular-season wins: holding starter value fixed, value in the top 3 counts LESS per
unit (0.29) than the rest of the lineup (0.39), a squared term doesn't help, and bench
value carries little. **C3 REJECTED at one season** - old share is ~0 (-0.03 pooled); age
should bite at t+2+, not t+1. Reading: roster -> strength is close to linear in starter
value; the non-linearity the owner expects belongs to strength -> TITLES (a 1 seed wins far
more than a 5 seed), which the season simulator already models. Next: map all-play to
title odds through the simulator, then grade trades as change in expected titles.

## Manager habits (`manager_habits.py`, 2026-10-24) - claims from the literature, tested

The owner asked for a sweep of what people think makes a good manager, tested against our
data. Sources: the Fantasy Premier League skill study (O'Brien et al., PLOS One 2021 - ranks
persist across seasons; "long-term planning and consistently good decision-making"), the MIT
fantasy skill study (Hosoi et al.), Harstad's Footballguys "Dynasty, in Theory" columns (half
skill / half luck; the best team wins ~30-36% of titles WITH a bye and 16-20% without;
win-now buying adds ~4% title odds), and an FFToday reader survey (top seeds are more active
on the wire, maybe just engagement). No published study links trade volume to winning.

**Pre-registered before the first run (2026-10-24):**
- H5 (byes): in our 131 brackets, the two bye seeds win at least half of all titles, and a
  bye seed's title rate is at least 1.5x a non-bye playoff seed's.
- H6 (how long to know a manager): from the year-over-year all-play r, Spearman-Brown says
  ~3-4 seasons of all-play are needed for 0.7 reliability; checked directly, two seasons'
  average all-play predicts a third better than one season does.
- H7 (trading): trade volume is a stable habit (year-over-year r > 0.3), and holding this
  season's all-play and starter value fixed, more trades predict a slightly BETTER next
  season (positive partial weight) - action beats inaction.
- H8 (engagement): total moves (adds + trades) predict next season's all-play beyond roster
  and results less well than trades alone do - engagement isn't the same as dealing.

**Result (2026-10-24).** H5 HELD - 128 brackets with byes: bye seeds won 53% of titles, 26.6%
per bye team vs 11.7% per other playoff team (2.3x), in line with Harstad's ~30%; seeding is
where regular-season strength turns into titles. H6 HALF - Spearman-Brown says 3.3 seasons,
but directly (268 managers, three straight seasons) the latest season predicts the third
BETTER (r 0.45) than a two-season average (0.37): in dynasty a team's level moves too fast to
be a fixed manager trait - we are scoring team state plus behavior, not a permanent talent
number. H7 HELD - trades are a habit (median 3 a season, year-over-year r 0.39) with a small
positive partial weight on next season. H8 REJECTED, the interesting way: TOTAL moves (adds +
trades) predict next season better than trades alone (leave-one-season-out r 0.503 vs 0.488
vs 0.477 base) - positive in every held-out season (+0.011 to +0.034 all-play per SD), robust
to log(moves), adds weighing more than trades. One SD (13 moves; median 15) ~ +0.4 wins the
next season beyond roster value and results: the first behavior that predicts improvement.
Engagement, not cleverness, is the measurable edge so far.

## Trade grades (`trade_grades.py`, `trade_analysis.py`, 2026-10-24) - manager scores, channel 4

Every crawled two-team trade (2,513; 735 in-season) graded per side as the change in
expected titles, ex-ante: this season by re-running the season simulator before/after
(lineups at points per game to date, shrunk toward the position's typical starter by 3
games so a week-2 trade isn't priced on one Sunday; rostered-and-scoreless two weeks =
out), next season through the roster-strength curve (all-play + league-relative value ->
next all-play) and a title curve fitted on the crawl's brackets (P(title) = logistic(-7.57 +
8.80 x all-play): 4.0% at .500, 19.7% at .700 - the non-linearity lives here). Picks: their
round's draft-day mean in that class and format. Out of scope: 3+ team trades. Known
limit: the next-season half is linear in value margin by construction.

Owner gut check (live, `python -m research.trade_grades <league_id>`): Goff + Kittle after
Hurts' injury +10.1 lineup PPG, +9.8 title points this season; Cook for an injured Achane +
a 2nd +5.7 PPG, +3.8 titles, +11.5 playoff points; the Achane buy itself ~0 - not punished
for an ACL that came later. All three as the owner reads them.

**Pre-registered before the analysis (2026-10-24):**
- T-A (the lineup half is real): a side's predicted lineup gain shows up in its actual
  weekly scoring after the trade - slope between 0.3 and 1.2, r > 0.1.
- T-B (the value half is real): holding all-play and roster value fixed, value margin
  predicts next season's all-play with a positive weight.
- T-C (the owner's thesis): contenders (all-play to date >= .600) who LOSE on value in-season
  still gain this-season title odds on average.
- T-D (consolidation): in-season, the side receiving fewer pieces (the 2-for-1 buyer) gains
  more this-season title odds than the side receiving more.
- T-E (is trading a skill): a manager's average trade grade in titles persists year over
  year, r > 0.1.

**Result (2026-10-24).** T-A HELD - over 1,372 in-season sides, every predicted lineup point
shows up as ~1.05 real points a week after the trade (r 0.26): the this-season half is real.
T-B REJECTED as specified, and the miss rebuilt the grader: value won predicted a WORSE next
season (league bootstrap 95% -0.25 to -0.02). Split apart, PLAYER value won helps exactly as
the curve says (offseason +0.29 [0.17, 0.39], in-season +0.43 [0.19, 0.68]) while PICK value
won never helps the next season (in-season ~0, offseason -0.40 [-0.51, -0.31] - mostly who
buys picks: rebuilds making themselves worse). So the next-season half now prices player
value only, and picks ride along as `pick_margin` in value, unpriced in titles until a 2+
season horizon can be validated. T-C HELD - contenders who LOST on value in-season gained
+1.50 title points on average, more than contenders who won on value (+1.30): the owner's
thesis, measured. T-D HELD - the side receiving fewer pieces gains +1.17 title points vs
+0.23. T-E REJECTED - a manager's trade grade does not persist (r 0.02 over 455 pairs):
trade quality is not a stable trait. By situation, in-season: contenders +1.41 this-season
title points a trade, middle +0.42, rebuilds ~0.

## What converts to wins (`what_wins.py`, 2026-10-24) - the model over every channel

One linear model per target, every channel as a feature of season t, predicting season t+1;
every target season held out in turn (leave-one-season-out), weights read per SD. Features:
results (all-play, luck), roster (starter, bench, 28+ share), behavior (moves, trades, waiver
share), trade grades (this-season titles gained, next-season titles gained, pick value
bought). Draft outcomes are excluded - they are measured 12 months on, a leak.

**Pre-registered before the first fit (2026-10-24):**
- M1: behavior + trade grades improve held-out prediction of next season's all-play over
  the results + roster baseline (r up by at least 0.01).
- M2: luck carries ~zero weight (|weight per SD| < 0.005) - it is already out of all-play
  and nothing about it repeats.
- M3: of the behaviors, total moves carries the largest weight per SD.
- M4: the same model predicts next season's TITLE better than all-play alone does (held-out
  AUC up by at least 0.02).

**Result (2026-10-24; 709 manager pairs, 62 next-season champions).** Held-out next-season
all-play: all-play alone r 0.397, results + roster 0.478, + behavior + trade grades 0.494.
Title AUC: 0.691 / 0.723 / 0.720. **M1 HELD** (+0.016 r from behavior). **M2 REJECTED** - luck
carries -0.011 per SD: teams the schedule flattered do worse next season than their all-play
and roster predict. Checked and rejected: lucky teams standing pat (luck vs next season's
moves r 0.02). Open hypothesis: the draft - a flattering record buys a worse slot. **M3
HELD** - per SD: starter +0.052, all-play +0.036, MOVES +0.029, bench +0.016, next-season
trade titles +0.011, luck -0.011, picks bought +0.009, this-season trade titles +0.006;
waiver share, trades and age ~0 once moves is in. **M4 HELD as written** (+0.03 AUC over
all-play) but behavior adds nothing to TITLE prediction beyond roster + results - 62
champions is too few to see effects this size.

**The manager-score story, all channels together:** a dynasty team's next season is starter
value first, real (all-play) results second, and the manager's ACTIVITY third - the one
behavior that keeps paying. Luck, rookie drafting and trade quality do not persist; the
right trade depends on the team (contenders converting value to lineup gain title odds,
consolidators gain, pick value pays off - if at all - beyond the next season).

