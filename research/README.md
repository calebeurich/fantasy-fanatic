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
cost table (analysis/market_drift.py) is tier-conditioned, because a cohort median
without tier tells an elite player's owner a fate that mostly belongs to the
non-elite - the exact bleed into projections-arbitrage territory the owner flagged.

**What changed in the product so far:** one artifact - analysis/market_drift.py, the
tier-conditioned holding-cost table (measured constants with provenance; note-only
by doctrine, not yet wired into payloads). Everything else is recorded and waiting:
constants graduate when findings survive a re-run after more data accumulates.
Graduation queue, in order of evidence strength: the tier-conditioned drift note
into sell-window payloads; the contract-year signal as a second note; the decline
tails from eye-test to measured; the rushing-QB override review once the EPA split
is reconstructed.

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
