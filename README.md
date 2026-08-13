# fantasy-fanatic

A dynasty fantasy football analyst: a deterministic Python analysis layer over real
league data, exposed to an LLM agent through MCP tools, with real evals, grounding
checks that recompute the model's claims after it makes them, and hard cost guardrails.

Live on Cloud Run behind a shared-link gate, currently being tested by the author's
actual league mates — every heuristic in it was validated against real rosters and
real trades, not synthetic fixtures, and the sharpest bugs in this repo were found by
a friend asking a question the author didn't think to.

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
| `analysis/` | Age-curve bucketing, team-window classification, replacement-level positional needs, trade-target matching, single-player outlooks, mutual-swap detection, waiver upgrades |
| `agent/` | MCP server (8 read-only tools), Claude Agent SDK agent, grounding checks, eval harness, session manager, budget + observability, HTTP API, static web UI |

## Engineering decisions worth reading about

Full reasoning for every heuristic and every non-obvious call lives in
[`LOGIC.md`](LOGIC.md) — written as the project was built, including the things that
didn't work.

**Generate-then-verify beats prompt engineering.** The agent occasionally asserted
things its own tools contradicted — suggesting a player be traded away who wasn't on
that team's real offer list, or telling a user to sell a QB to a team whose QB room
was already elite. Strengthening the system prompt failed, verified by eval. The fix
that holds: after the model answers, recompute the ground truth in Python — the same
Python the tools call — and force one corrective retry on a mismatch. There are now
two such checks (trade-away names, positional-need claims), built the same way, and
the pattern is a ratchet: each claim class the model fabricates in the wild gets its
own deterministic check. Generation is probabilistic; set membership isn't.

**Python-layer fixes propagate; prompt-layer fixes don't.** A misclassification bug
(the #1 roster in a league reading as a "Rebuilding" seller because it also held a lot
of young talent) was fixed once in `team_state.py` and instantly corrected every
downstream tool, the agent included, with zero prompt changes. This asymmetry drove
most of the architecture.

**The most dangerous failures are silent, so make them loud.** The worst bug in the
project's history: MCP tool results over a size limit were silently swapped for a tiny
preview and a file path the model couldn't open — so it answered confidently from a
fraction of the data, with no error anywhere. The fix keeps every payload under a
measured wire budget and *labels* any trimming inside the payload itself. Same
principle elsewhere: a conversation silently evicted server-side now comes back with a
`conversation_reset` flag, and the UI tells the user the model can't see their earlier
messages instead of letting it improvise.

**Guardrails enforced by the SDK, not requested in the prompt.** The agent is given an
explicit 8-tool allowlist — this is what actually excludes every built-in
file/shell/web tool, rather than gating them behind a permission prompt — plus a hard
turn cap and a per-call budget ceiling. Verified by evals that attempt explicit
instruction overrides.

**Cost was measured, then root-caused, then capped at two levels.** Found the SDK
silently loading the repo's own `CLAUDE.md` as agent context on every single call —
38% of input tokens, paid for, and irrelevant to answering a fantasy question. In
production, a per-call ceiling caps any one question and an in-process daily budget
(exact because the service is pinned to one instance) fails closed to a free static
message — the whole exposure if the shared link ever leaks is one day's budget.

**Format-aware values, with an honest gap.** League format (superflex, PPR, team
count, dynasty vs. redraft) is detected from real league settings and passed to
FantasyCalc so values reflect that specific league — QBs really are worth more in
superflex. TE-premium leagues are a documented, unfixable-at-source gap:
FantasyCalc's API has no parameter for it, and inventing a correction multiplier
without data to calibrate against would be a guess dressed as a feature.

## Tests and evals

Two layers, deliberately, because they catch opposite failures.

**Unit tests — free, offline, instant.** 124 tests over the analysis heuristics and
the agent infrastructure. Almost every rule in `analysis/` is a pure function taking
plain data, so these need no fixtures and no network. They assert the *boundaries*
the heuristics turn on — exact age cutoffs, relevance fractions, need-vs-surplus
symmetry — plus regression guards for real bugs (never offer a starter, never offer a
position you need), and the grounding checks are tested against recorded transcripts
of real agent answers rather than invented strings.

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Verified to actually bite: the suite was mutation-tested — deliberately breaking a
threshold fails tests rather than passing quietly.

**Evals — 12 cases, real API calls (~$0.35/run).** These test *agent behavior*:
correct tool selection, non-dynasty refusal, resistance to instruction-override
attempts, off-topic scope refusal, trade-suggestion grounding, lineup-format
awareness, sell-on-runway-not-age reasoning, answering a named player rather than
dismissing him for being absent from a ranked list, and graceful handling of a
nonexistent league ID. Ground truth is recomputed at eval time from the same Python
the tools call, so cases can't go stale as real rosters change.

```bash
python -m agent.evals
```

The split matters: the evals would happily pass while a threshold was silently wrong,
and the unit tests would happily pass while the agent ignored its instructions. Each
eval case is a real, paid API call, which is exactly why there are 12 and not 50 —
the suite covers failure modes actually observed, most of them reported by real
testers.

## Running it

```bash
pip install -r requirements.txt
```

Needs `ANTHROPIC_API_KEY` in a `.env` file at the repo root. Sleeper, FantasyCalc,
and nflverse are all free and keyless.

```bash
python -m agent.agent "For Sleeper league <league_id>, what should <owner> do and why?"
```

Any analysis module also runs standalone against a real league:

```bash
python -m analysis.team_state <league_id>
python -m analysis.trade_targets <league_id> <owner_name>
python -m analysis.trade_targets <league_id> "player=<name>=<asker>"
```

HTTP API + web UI:

```bash
python -m uvicorn agent.api:app
```

## Deployment

Cloud Run, deployed by GitHub Actions on every push to `main` that passes CI — the
deploy job triggers on `workflow_run` and checks `conclusion == 'success'`, since
that event fires on failure too. Auth is Workload Identity Federation rather than a
stored service account key: each run exchanges GitHub's OIDC token for a short-lived
GCP token, and the identity pool is pinned to this repository. The runtime identity
is a dedicated service account holding exactly one permission (read the API key
secret), not the default Editor-carrying compute account.

The Cloud Run settings that matter are declared in the workflow with their reasons,
not left as console click-state: one instance (the daily budget counter is in-process
and exact only there), concurrency 2 (two friends can ask at once without queueing
behind a 60–90s answer), and a session manager that keeps a bounded number of live
conversations, each holding a real subprocess pair.

The serving layer earned some scars from real use: answers survive the asker's tab
closing (measured: Cloud Run finishes an abandoned request, so the answer is stashed
server-side and reclaimed on return), the UI shows the agent's actual tool steps
while it works rather than a fake progress bar, and an operator page shows what
people asked and what they thought of the answers — thumbs-down comments from league
mates have been the single richest source of real bugs.

## Status

Live and in friends-testing. Known limitations and planned work are tracked at the
end of `LOGIC.md` — the largest being that everything is preseason roster math until
a projections source is added: the agent knows values, windows, and needs, but not
schedules or per-week outcomes. The deployment write-up there also documents the five
non-reproducible-locally failures it took to get the first container serving, and why
the most instructive of them reached the user as a confident, fabricated answer
instead of an error.
