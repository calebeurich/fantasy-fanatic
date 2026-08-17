# fantasy-fanatic

A dynasty fantasy football analyst: a deterministic Python analysis layer over real
league data, exposed to an LLM agent through MCP tools, with an eval suite, grounding
checks that recompute the model's claims after it makes them, and hard cost guardrails.

**Live:** https://fantasy-fanatic-487070873858.us-central1.run.app — the public demo
opens on the author's own 12-team superflex dynasty league — the one it was built for
and is used in. The league table, rosters and the
trade composer are free to use; the assistant answers a few questions a day per
visitor from a small shared budget. Or look up your own Sleeper username.

It is used, every week, by the author's actual league mates. Every heuristic in it was
validated against real rosters and real trades rather than synthetic fixtures, and the
sharpest bugs in this repo were found by a friend asking a question the author didn't
think to.

---

## Why this exists

Dynasty fantasy football is a genuinely hard analysis problem: you're valuing players
on a 3-year horizon, against a specific league's scoring format, while accounting for
roster construction, age curves, and what other teams actually want. Most tools give
you a number. This one explains its reasoning — which is also what makes it a
reasonable substrate for an LLM agent.

The AI-engineering goal was to build something where the model is *constrained by*
deterministic logic rather than trusted to reason correctly on its own.

## Architecture

```
sources/     Raw data clients        Sleeper (rosters, transactions, projections), FantasyCalc, nflverse
    ↓
analysis/    Deterministic logic     team reads, positional needs, trade targets, the trade framer
    ↓
agent/       LLM layer               MCP server → Claude Agent SDK → FastAPI → static UI
research/    Offline studies         intuitions tested against real trade data before they become constants
```

**The load-bearing idea:** every fact the agent can state comes from `analysis/`, which
is plain Python with no LLM involved. The model chooses *which* questions to ask and
explains results in context — it never computes a value, ranks a team, or decides
what's tradeable. The web UI follows the same rule: everything deterministic renders
as a table (team reads, rosters, the trade composer's facts); every "should" comes
from the agent.

| Layer | What's in it |
|---|---|
| `sources/` | Sleeper league/roster/transaction/projection data, FantasyCalc dynasty + redraft values, nflverse contracts, usage roles, availability |
| `analysis/` | Age-curve runway, the three-tier team read (contention distance → composition → path), replacement-level positional needs, trade-target matching with a direction gate, the trade framer (per-side judgment, no package pricing), two-leg sequences, waivers |
| `agent/` | MCP server (10 read-only tools), Claude Agent SDK agent, grounding checks, eval harness, tiered sessions + budgets, streaming HTTP API, static UI with a trade composer |
| `research/` | Studies that turned gut feel into measured constants: consolidation premiums, what a stud actually fetches by value tier, boundary-noise calibration |

Three currencies, kept apart on purpose: **dynasty value** (what a piece fetches),
**redraft value** (what the market pays for his season), and **projected points a game**
(what a lineup actually scores — Sleeper's season projections under the league's own
scoring). Sums and shares of a lineup are always in points; prices are always market;
who starts is projected points with the market breaking near-ties.

## Engineering decisions worth reading about

Full reasoning for every heuristic and every non-obvious call lives in
[`LOGIC.md`](LOGIC.md) (~1,700 lines, written as the project was built, including the
things that didn't work). The short versions:

**Generate-then-verify beats prompt engineering.** The agent occasionally asserted
things its own tools contradicted — suggesting a player be traded away who wasn't on
that team's real offer list, or asserting a positional need the data didn't flag.
Strengthening the system prompt failed, verified by eval. The fix that holds: after the
model answers, recompute the ground truth in Python — the same Python the tools call —
and force one corrective retry on a mismatch. Each claim class the model fabricates in
the wild gets its own deterministic check. Generation is probabilistic; set membership
isn't.

**Python-layer fixes propagate; prompt-layer fixes don't.** A misclassification bug
was fixed once in `team_state.py` and instantly corrected every downstream tool, the
agent included, with zero prompt changes. This asymmetry drove most of the architecture.

**Measure the intuition before it becomes a constant.** "Trading for a stud takes a
good player and two firsts" was the owner's gut; `research/stud_returns.py` checked it
against ~3,000 crawled real trades with point-in-time values and found the actual
shape by tier (a top-2% piece's centerpiece return runs 0.50–0.71 of his value, 56%
of returns carry a 1st). The framer's ballpark quotes those bands. Same treatment for
the noise band that decides when a label is hedged (±2%, from 300 jittered refreshes)
and for the production measure (summing market prices made one star read as a whole
lineup — replaced with projected points after a 48-team before/after).

**The most dangerous failures are silent, so make them loud.** The worst bug in the
project's history: MCP tool results over a size limit were silently swapped for a
preview and a file path the model couldn't open — so it answered confidently from a
fraction of the data. Every payload now stays under a measured wire budget and
*labels* any trimming inside itself. A conversation silently evicted server-side comes
back with a `conversation_reset` flag and the UI says so; a data feed that falls back
attaches a `data_gap` note to every tool result computed while it was down.

**Guardrails enforced by the SDK, not requested in the prompt.** An explicit tool
allowlist (what actually excludes every built-in file/shell/web tool), a hard turn cap,
a per-call budget ceiling. Verified by evals that attempt explicit instruction
overrides.

**Cost and latency were measured, then root-caused.** The SDK was silently loading the
repo's own `CLAUDE.md` as agent context on every call — 38% of input tokens. A
"tell me about my team" answer was profiled event by event: ~8s of subprocess start-up
per session, cold data fetches inside the MCP subprocess that the API's warm-up never
reached, and ~27s of the model writing — so sessions pre-open on page load, both
processes warm the friends' leagues at boot, and answers stream to the page as they're
written. Two daily budgets (friends, public demo) fail closed to a free static message.

## Tests and evals

Two layers, deliberately, because they catch opposite failures.

**Unit tests — free, offline, instant.** 141 tests over the analysis heuristics and
the agent infrastructure. Almost every rule in `analysis/` is a pure function over
plain data, so they need no fixtures and no network. They assert the *boundaries* the
heuristics turn on — exact runway cutoffs, tier lines, the noise band — plus
regression guards for real bugs (never offer a starter you'd miss, never offer a
position you need, the market breaks a projection near-tie), and the grounding checks
are tested against recorded transcripts of real agent answers.

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Mutation-tested: deliberately breaking a threshold fails tests rather than passing
quietly.

**Evals — 12 cases, real API calls (~$0.40/run).** These test *agent behavior*: tool
selection, non-dynasty refusal, resistance to instruction overrides, off-topic scope
refusal, trade-suggestion grounding, lineup-format awareness, sell-on-runway-not-age
reasoning, path-vocabulary coherence (the read the tool returns is the read the answer
gives), and graceful handling of a nonexistent league. Ground truth is recomputed at
eval time from the same Python the tools call, so cases can't go stale as rosters
change.

```bash
python -m agent.evals
```

The split matters: the evals would happily pass while a threshold was silently wrong,
and the unit tests would happily pass while the agent ignored its instructions. Each
eval is a real, paid call — which is why there are 12 and not 50; the suite covers
failure modes actually observed, most reported by real testers.

## Running it

```bash
pip install -r requirements.txt
```

Needs `ANTHROPIC_API_KEY` in a `.env` file at the repo root. Sleeper, FantasyCalc and
nflverse are free and keyless.

```bash
python -m agent.agent "For Sleeper league <league_id>, what should <owner> do and why?"
```

Any analysis module also runs standalone against a real league:

```bash
python -m analysis.team_state <league_id>
python -m analysis.trade_targets <league_id> <owner_name>
python -m analysis.trade_eval <league_id> "<owner_a>: <piece>, <piece>" "<owner_b>: <piece>"
```

HTTP API + web UI:

```bash
python -m uvicorn agent.api:app
```

## Deployment

Cloud Run, two services from one repo: `main` deploys production, `dev` deploys a
staging service, both by GitHub Actions on pushes that pass CI (`workflow_run`, gated on
`conclusion == 'success'`, with concurrency groups so close-together pushes deploy
once). Auth is Workload Identity Federation — no stored service-account key; the
runtime identity holds exactly one permission (read the API-key secret).

The settings that matter are declared in the workflows with their reasons, not left
as console click-state: one instance (the daily budget counters are in-process and
exact only there), concurrency 2, a bounded session pool where each live conversation
holds a real subprocess pair, and two tiers on one deploy — a friends link with a
shared key, and a key-less public demo with its own small budget, a per-visitor cap,
and sessions that can never evict a friend's live conversation.

The serving layer earned its scars from real use: answers survive the asker's tab
closing (Cloud Run finishes the abandoned request; the answer is stashed and reclaimed
on return), the page shows the agent's real tool steps and streams the answer as it's
written, and an operator page shows what people asked and what they thought of it —
thumbs-down comments from league mates have been the richest source of real bugs.

## Not in this repo, on purpose

- **No retrieval / vector store.** The agent's context is small, structured and
  computed per question; there is no corpus to retrieve from. RAG here would be RAG
  for the résumé.
- **No fine-tuning.** The reasoning lives in deterministic Python plus a prompt; the
  thesis is that the model reasons over verified facts. The evals are the better story.
- **No multi-agent choreography** — with one deliberate exception. A trade has two
  sides, so the composer's "Ask" runs two agents in parallel: your advisor, and the
  *other manager* judging the same proposal from their own path with the same tools
  (accept / counter / no, and what it would take). Two opposing stances over one
  deterministic referee (the framer); the coordination is a Python function, not a
  third agent. Everything else people call multi-agent here (planner, critic) is done
  deterministically, and the critic is exact instead of another opinion.
- **One orchestrator, one model** — the Claude Agent SDK on Haiku, chosen after
  measuring cost and quality. A small LangGraph client driving the same MCP tools with
  an open-weight model exists as a portability proof (`agent/langgraph_client.py`;
  verified with Qwen2.5-72B-Instruct via HuggingFace's router driving the identical
  MCP tools), to show the tool layer is the asset and the orchestrator is swappable.

## License and data

Source is available under the **PolyForm Noncommercial License 1.0.0** (see
[`LICENSE.md`](LICENSE.md)): read it, run it, modify it, use it for anything noncommercial;
commercial use needs permission. The live demo is free to use by anyone at the link
above — using the hosted service needs no license at all.

The data is not the author's to license and stays under its sources' own terms:
Sleeper's public API, FantasyCalc's public API (used instead of KeepTradeCut, whose ToS
forbids reproducing values), nflverse (fetched, not vendored), and the DynastyProcess
values archive (GPL-3.0 — fetched at analysis time, never committed).

## Status

Live for friends and as a public demo. Planned work is in [`ROADMAP.md`](ROADMAP.md);
the largest open item is in-season: weekly projections (same endpoint as the season
ones already used) for start/sit and rest-of-season questions once games are played.
