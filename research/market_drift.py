"""What holding a player has historically cost, by position x age band x tier.

Measured, not modeled: median one-year dynasty value ratios 2020-2026 from
research/age_curve_study.py (8,328 player-year observations, exits counted at the
list floor). TIER-CONDITIONED because the elite-vs-average split is large and
survives the held-out era at every position: top-12 QBs hold ~0.9-1.0 at every age
while the rest hold ~0.5, elite RBs hold roughly double their cohort - a cohort
median without the tier would tell an elite receiver's owner a fate that mostly
belongs to non-elite players his age (the CeeDee Lamb objection, verbatim from the
owner). Cells under n=10 omitted rather than estimated; regeneration documented in
research/README.md.

This only ever picks a sentence - it never decides whether an entry exists, ranks a
list, or feeds any other computation. A median describes the cohort, not the player.
"""

# (band, tier) -> median 1yr value ratio. Bands: young <=24, mid 25-27, old 28+.
MEASURED_DRIFT = {
    "QB": {("young", "top12"): 0.96, ("young", "rest"): 0.51,
           ("mid", "top12"): 0.99, ("mid", "rest"): 0.50,
           ("old", "top12"): 0.89, ("old", "rest"): 0.48},
    "RB": {("young", "top12"): 0.82, ("young", "rest"): 0.53,
           ("mid", "top12"): 0.64, ("mid", "rest"): 0.35,
           ("old", "top12"): 0.49, ("old", "rest"): 0.26},
    "WR": {("young", "top12"): 0.98, ("young", "rest"): 0.71,
           ("mid", "top12"): 0.80, ("mid", "rest"): 0.67,
           ("old", "top12"): 0.68, ("old", "rest"): 0.35},
    "TE": {("young", "top12"): 0.79, ("young", "rest"): 0.76,
           ("mid", "top12"): 0.63, ("mid", "rest"): 0.31,
           ("old", "top12"): 0.42, ("old", "rest"): 0.48},
}


def _band(age: float) -> str:
    return "young" if age < 25 else "mid" if age < 28 else "old"


def holding_cost_note(position: str, age: float | None, positional_rank: int | None) -> str | None:
    """One measured sentence about what a year of holding has typically cost players
    of this age and standing, or None where the table has no confident cell.
    `positional_rank` = his dynasty-value rank within his position league-wide."""
    if age is None or positional_rank is None:
        return None
    tier = "top12" if positional_rank <= 12 else "rest"
    drift = MEASURED_DRIFT.get(position, {}).get((_band(age), tier))
    if drift is None:
        return None
    who = (f"top-12 {position}s" if tier == "top12" else f"{position}s outside the top 12")
    return (f"Holding cost, measured (2020-26 medians): {who} in his age band have "
            f"typically shed ~{round((1 - drift) * 100)}% of dynasty value over the "
            f"following year. A cohort median, not a forecast for him specifically - "
            f"and elite standing is itself the strongest holder we measured.")
