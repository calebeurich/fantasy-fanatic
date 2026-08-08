# fantasy-fanatic

Dynasty fantasy football assistant, built incrementally as a portfolio project
demonstrating agentic AI engineering (Claude Agent SDK, MCP, RAG, evals) alongside
real fantasy football utility for the author's own Sleeper league.

## Prime directive: simplicity over sophistication

Every design and code decision optimizes for "can a stranger read this once and hold
the whole thing in their head." That beats clever, beats fast, beats complete.

- No premature abstraction. Three similar lines beats one wrong abstraction.
- No speculative generality. Build for the case that exists today, not the one that
  might exist later.
- Comments are a warning sign, not a virtue. A comment that's more than one line
  means the code underneath is too complicated — simplify the code instead of
  explaining it. Only comment the non-obvious "why" (a workaround, a hidden
  constraint), never the "what."
- Flat over nested. Small files over big ones. Plain functions over class
  hierarchies unless state genuinely demands a class.
- No framework/dependency added "because we'll need it eventually." Add it in the
  step where it's actually used.
- If a piece of code needs a paragraph to explain, it's wrong — restructure it
  instead of documenting around it.

## How this project is built

- One capability at a time, validated end-to-end before moving to the next. No
  scaffolding for future phases ahead of when they're needed.
- Every external integration (Sleeper, FantasyCalc, sportsbook odds, Twitter/X,
  etc.) gets a small standalone module with a manual smoke test before anything
  else depends on it.
- Never scrape or reproduce KeepTradeCut values — their ToS explicitly forbids it.
  Use FantasyCalc's public API for dynasty value data instead.
- This is a public repo. Never commit API keys, tokens, or league data that isn't
  the author's own public Sleeper data. Anything that costs money to call (LLM
  APIs, paid odds feeds) needs a rate limit / budget cap before it's wired to
  anything automated.
- Every heuristic, threshold, or "why behind a recommendation" goes in LOGIC.md in
  the same change that introduces it - not as a follow-up. The end goal is a chatbot
  that explains its recommendations, so the reasoning has to exist in writing
  somewhere other than this conversation and this code.

## Stack (as introduced, not upfront)

Python. Dependencies added only when a step needs them — no requirements.txt
entries "just in case."
