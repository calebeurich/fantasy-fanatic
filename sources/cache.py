"""Tiny TTL cache for the data-source calls.

Every one of these functions did a fresh HTTP request or nflverse download on every
call, and a single agent question makes 2-4 tool calls that each re-derive the same
league state - so the same FantasyCalc values and nflverse datasets were being pulled
several times to answer one question. That was most of the ~30s per-question latency.

Deliberately ~20 lines rather than a caching library: this needs a TTL and nothing
else, and the project's rule is to not add a dependency for something this small.

**The TTL choice is a correctness decision, not just a performance one.** Roster data
changes whenever someone makes a trade or a waiver claim, and serving a stale roster
means giving confidently wrong advice - strictly worse than being slow. So live league
state gets a short TTL that mainly de-duplicates *within a single question*, while
reference data that genuinely changes weekly gets a long one. See the constants below.

Not bounded in size: the key space here is tiny (a handful of leagues, one format tuple
each) and entries are replaced rather than accumulated. If this ever fronted many
leagues at once, it would need an eviction policy.
"""

import functools
import time

# Live league state - changes the moment anyone trades or claims a player. Short
# enough that a stale roster can't survive between questions, long enough to collapse
# the 2-4 tool calls within one question into a single fetch, which is where nearly
# all the win is.
LIVE_TTL = 60

# League configuration - scoring settings, roster slots, member list. Can change
# mid-season but effectively never does.
LEAGUE_CONFIG_TTL = 600

# Dynasty market values. FantasyCalc recomputes these periodically, not continuously;
# an hour old is still an accurate read of the market.
MARKET_TTL = 3600

# nflverse reference data - contracts, the gsis->sleeper id crosswalk, season usage
# stats. Updates weekly at most, and these are the slowest pulls in the project.
REFERENCE_TTL = 6 * 3600


def ttl_cache(seconds: float):
    """Memoize on arguments, expiring entries after `seconds`."""
    def decorator(fn):
        store: dict = {}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            entry = store.get(key)
            if entry is not None and now - entry[0] < seconds:
                return entry[1]
            value = fn(*args, **kwargs)
            store[key] = (now, value)
            return value

        wrapper.cache_clear = store.clear
        wrapper.cache_info = lambda: {"entries": len(store), "ttl_seconds": seconds}
        return wrapper

    return decorator
