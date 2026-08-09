"""Quick-glance summary of agent/observability.py's JSONL log - the "legible,
presentable artifact" from the Phase 3 plan, without standing up a real
dashboard/vendor for a personal project at this scale.

Run: python -m agent.log_summary (from the repo root)
"""

from collections import Counter

from . import observability


def main() -> None:
    runs = observability.read_runs()
    if not runs:
        print("no runs logged yet")
        return

    costs = [r["cost_usd"] for r in runs if r.get("cost_usd") is not None]
    latencies = [r["latency_seconds"] for r in runs if r.get("latency_seconds") is not None]
    errors = [r for r in runs if r["outcome"] == "error"]
    retried = [r for r in runs if r.get("grounding_retries")]
    tool_errors = sum(len(r.get("tool_errors", [])) for r in runs)
    tiers = Counter(r["format_tier"] for r in runs if r.get("format_tier"))

    print(f"{len(runs)} run(s) logged")
    print(f"total cost: ${sum(costs):.4f}")
    if costs:
        print(f"avg cost/run: ${sum(costs) / len(costs):.4f}")
    if latencies:
        print(f"avg latency: {sum(latencies) / len(latencies):.1f}s")
    print(f"errored runs (exception reached run_query): {len(errors)}/{len(runs)}")
    print(f"runs with a grounding retry: {len(retried)}/{len(runs)}")
    print(f"tool-level errors seen (e.g. bad league_id): {tool_errors}")
    if tiers:
        print("format tiers seen:", dict(tiers))


if __name__ == "__main__":
    main()
