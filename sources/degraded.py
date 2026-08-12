"""Which reference feeds failed this process, so the ANSWER can say so and not just the logs.

Both nflverse call sites already degrade gracefully instead of crashing, and both print a
warning to stderr. That is the right behaviour for the CLI, where the author sees the warning,
and useless for the thing this project is being built toward: a friend asking the agent a
question sees a confident answer and no stderr at all.

**It changes advice, which is why it cannot be a log line.** With usage roles unreachable every
age curve falls back to its position default, and on one live roster that moved Jared Goff from
6.2 years of runway to 2.1 - inverting which quarterback a rebuilding team should trade. An
answer built on defaults is not wrong, but it is answering a slightly different question than
the one it appears to answer, and the reader is entitled to know which.

Deliberately a module-level set rather than anything cleverer: the facts are per-process, the
key space is two entries, and nothing needs to un-record a failure. `record` is called from the
`except` branch that already exists, and `note` is joined onto MCP tool results.
"""

_MISSING: dict[str, str] = {}


def record(feed: str, effect: str) -> None:
    """Called from the degradation branch that already prints to stderr."""
    _MISSING[feed] = effect


def note() -> str | None:
    """One sentence for the reader, or None when everything loaded.

    Phrased as a limit on the answer rather than as an apology about a data source, because the
    only thing the reader can act on is knowing which parts to trust less."""
    if not _MISSING:
        return None
    return ("DATA GAP THIS RUN - say this in your answer, briefly, rather than dropping it: "
            + " ".join(f"{feed} could not be loaded, so {effect}."
                       for feed, effect in sorted(_MISSING.items()))
            + " Anything turning on a player's age or runway is less precise than usual; the "
              "rest of the analysis is unaffected.")
