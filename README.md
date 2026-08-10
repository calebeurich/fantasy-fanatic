# fantasy-fanatic

A dynasty fantasy football analyst: a deterministic Python analysis layer over real
league data, exposed to an LLM agent through MCP tools, with real evals and hard cost
guardrails.

Built against two live Sleeper dynasty leagues — every heuristic in it was validated
against real rosters and real trades, not synthetic fixtures.

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
sources/     Raw data clients      Sleeper API, FantasyCalc, nflverse
    ↓
analysis/    Deterministic logic   team windows, positional needs, trade matching
    ↓
agent/       LLM layer             MCP server → Claude Agent SDK → FastAPI
```

**The load-bearing idea:** every fact the agent can state comes from `analysis/`, which
is plain Python with no LLM involved. The model chooses *which* questions to ask and
explains results in context — it never computes a value, ranks a team, or decides
what's tradeable.

| Layer | What's in it |
|---|---|
| `sources/` | Sleeper league/roster/transaction data, FantasyCalc dynasty values, nflverse contracts + usage stats |
| `analysis/` | Age-curve bucketing, team-window classification, replacement-level positional needs, trade-target matching, mutual-swap detection, waiver upgrades |
| `agent/` | MCP server (7 read-only tools), Claude Agent SDK agent, eval harness, observability logging, HTTP API |

## Engineering decisions worth reading about

Full reasoning for every heuristic and every non-obvious call lives in
[`LOGIC.md`](LOGIC.md) — written as the project was built, including the things that
didn't work.

**Generate-then-verify beats prompt engineering.** The agent kept occasionally
suggesting a player be traded away who wasn't actually on that team's real offer list.
Strengthening the system prompt failed — twice, verified by eval. The fix was to stop
asking the model to follow a rule and instead recompute the real offerable set in
Python after the answer, then force one corrective retry on a mismatch. Generation is
probabilistic; set membership isn't. That check later needed narrowing when it turned
out to fire on harmless descriptive mentions ("your cornerstones: X") rather than
actual recommendations.

**Python-layer fixes propagate; prompt-layer fixes don't.** A misclassification bug
(the #1 roster in a league reading as a "Rebuilding" seller because it also held a lot
of young talent) was fixed once in `team_state.py` and instantly corrected every
downstream tool, the agent included, with zero prompt changes. This asymmetry drove
most of the architecture.

**Guardrails enforced by the SDK, not requested in the prompt.** The agent is given an
explicit 7-tool allowlist — this is what actually excludes every built-in file/shell/web
tool, rather than gating them behind a permission prompt — plus a hard turn cap and a
per-call budget ceiling. Verified by an eval that attempts an explicit instruction
override.

**Cost was measured, then root-caused.** Found the SDK silently loading the repo's own
`CLAUDE.md` as agent context on every single call — 38% of input tokens, paid for, and
irrelevant to answering a fantasy question. Separately investigated why prompt caching
never activated and concluded it structurally can't at this tool-surface size, rather
than cargo-culting a fix.

**Format-aware values, with an honest gap.** League format (superflex, PPR, team count,
dynasty vs. redraft) is detected from real league settings and passed to FantasyCalc so
values reflect that specific league — QBs really are worth more in superflex. TE-premium
leagues are a documented, unfixable-at-source gap: FantasyCalc's API has no parameter
for it, and inventing a correction multiplier without data to calibrate against would be
a guess dressed as a feature.

## Tests and evals

Two layers, deliberately, because they catch opposite failures.

**Unit tests — free, offline, instant.** 26 tests over the analysis heuristics and the
agent infrastructure. Almost every rule in `analysis/` is a pure function taking plain
data, so these need no fixtures and no network. They assert the *boundaries* the
heuristics turn on — exact age cutoffs, the 50%/25% relevance fractions, need-vs-surplus
symmetry, and regression guards for real bugs (never offer a starter, never offer a
position you need).

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Verified to actually bite: deliberately changing the RB decline age from 27 to 99 fails
2 tests rather than passing quietly.

**Evals — 7 cases, real API calls (~$0.11/run).** These test *agent behavior*: correct
tool selection, non-dynasty refusal, resistance to instruction-override attempts,
off-topic scope refusal, trade-suggestion grounding, and graceful handling of a
nonexistent league ID. Ground truth is recomputed at eval time from the same Python the
tools call, so cases can't go stale as real rosters change.

```bash
python -m agent.evals
```

The split matters: the evals would happily pass while a threshold was silently wrong,
and the unit tests would happily pass while the agent ignored its instructions.

Covers: correct tool selection, non-dynasty league refusal, trade-target grounding,
resistance to instruction-override attempts, off-topic scope refusal, and graceful
handling of a nonexistent league ID.

Each case is a real, paid API call, which is exactly why there are 7 and not 50 — the
suite covers failure modes actually observed in development rather than hypothetical
ones. Two known low-frequency flakes are documented in `LOGIC.md` rather than papered
over.

## Running it

```bash
pip install -r requirements.txt
```

Needs `ANTHROPIC_API_KEY` in a `.env` file at the repo root. Sleeper, FantasyCalc, and
nflverse are all free and keyless.

```bash
python -m agent.agent "For Sleeper league <league_id>, what should <owner> do and why?"
```

Any analysis module also runs standalone against a real league:

```bash
python -m analysis.team_state <league_id>
python -m analysis.trade_targets <league_id> <owner_name>
```

HTTP API:

```bash
python -m uvicorn agent.api:app
```

## Status

Deployed and working on Google Cloud Run — the full chain (FastAPI → Agent SDK →
`claude` CLI → MCP subprocess → analysis modules → live league data) runs in a
container, answering real questions with real data. Currently authenticated-only rather
than public; the daily budget ceiling is in place and verified, but a public URL is a
separate decision from a working deploy.

Cloud Run was chosen after an actual cost/fit comparison (documented in `LOGIC.md`),
including a finding that the originally-planned AWS Lambda + API Gateway setup carried a
non-obvious recurring cost, since API Gateway has no permanent free tier.

Getting there took five real failures, none reproducible locally — a corrupted wheel
from a Windows-only dependency, an IAM role that looks sufficient but isn't, a CLI that
refuses to run as root, one fix that turned out to be solving a non-problem, and finally
an unpinned dependency resolving differently in a clean environment. The write-up in
`LOGIC.md` includes the wrong turn, because the interesting part is *why* it was hard to
see: the underlying error was a `ModuleNotFoundError` that reached the user as a
confident, entirely fabricated answer. That trail is the most instructive thing in this
repo.

Known limitations and planned work are tracked at the end of `LOGIC.md` — including no
caching on the data sources (every tool call refetches), no conversation memory (the
agent is single-turn and doesn't look it), and several analytics ideas that are blocked
on in-season data rather than on effort.
