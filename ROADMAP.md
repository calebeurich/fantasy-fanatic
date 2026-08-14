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

---

## Track 1: Trade evaluation (branch `trade-eval`, active)

Judge a user-proposed trade per side without pricing packages. Built: best
single piece (players and picks), needs recomputed league-wide with rosters
swapped, lineup-cascade production delta, window-matched runway bars. Next:
Caleb's redline of the reads → MCP tool + agent doctrine reconciliation (the UI
promise "not a trade calculator" stays true: it judges, it never prices).

## Track 2: The trade-market dataset

Real trades, to ground trade_eval's sentences in measured behavior and
eventually to learn from.

- **FantasyCalc stream** (`sources/fc_trades.py`, built): rolling ~49-trade
  window per stream (superflex + 1QB), no history, ~500-700/day/stream. Pieces
  carry point-in-time values resolved at poll time — FC has no public value
  history (probed thoroughly), so poll-time resolution is the only point-in-time
  there will ever be. **OPEN: where the poller runs** (GitHub Actions cron or a
  tiny Cloud Run job beat local — every unpolled ~2h window is data lost
  forever).
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
  the CSVs into this repo.** Coarser format-conditioning than FC (no
  teams/ppr axis) — acceptable, FC's own ppr effect measured at ~0.6% on RBs.

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
- **Sportsbook lines / news-flow value arb**: the long-held idea — market
  dynasty values move slower than real news; sportsbook lines (and possibly
  curated Twitter/X flow) lead them. Find the lag, surface the buy/sell window
  before the market reprices. Anything with a paid feed obeys the CLAUDE.md
  rule: budget cap before it's wired to anything automated.
- **Redraft**: far future state. Most machinery transfers (lineups, waivers,
  luck, format conditioning); windows/runway/picks do not. Do not build toward
  it speculatively — note what transfers when designing, nothing more.

## Sequencing (plan of record)

1. Finish trade_eval to Caleb's taste; wire as MCP tool behind the doctrine.
2. Decide the FC poller home; start it (data loss is permanent until then).
3. Manager scores v1 on our own leagues (lineups + schedule luck first — zero
   new data needed — then waivers, drafts, trades with the DP join).
4. League report card as a product surface for the friends test.
5. Sleeper crawler at pilot scope (one hop), then scale; re-run everything at n.
6. "What converts to wins" regression; only then talk about learned models.
