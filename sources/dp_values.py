"""DynastyProcess's historical player values - the point-in-time value archive.

DP commits `files/values-players.csv` roughly weekly, and the git history of that one
file IS the archive: each commit is a dated snapshot of every player's 1QB/2QB value,
age, and draft year. GPL-3.0 data - fetched at analysis time into the gitignored
data/ directory, never vendored into this repo.

Mechanics: the commit list comes from the GitHub API (a handful of paginated calls);
the snapshots themselves come from raw.githubusercontent.com, which is CDN-served and
not API-rate-limited. The local archive is a JSONL of {scrape_date, sha, rows}, deduped
by scrape_date (DP sometimes commits twice in a week; the file's own scrape_date is the
honest key, not the commit timestamp).

Smoke test: python -m sources.dp_values   (fetches anything new, prints coverage)
"""

import csv
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = "dynastyprocess/data"
FILE = "files/values-players.csv"
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "dp_values.jsonl"
_HEADERS = {"User-Agent": "fantasy-fanatic (github.com/calebeurich)"}

# fp_id is the join key across years - names collide (Jr/Sr) and change teams.
KEEP = ("fp_id", "player", "pos", "team", "age", "draft_year", "value_1qb", "value_2qb")


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    return urllib.request.urlopen(req, timeout=60).read()


def list_snapshot_commits() -> list[dict]:
    """Every commit touching the values file, oldest first: [{sha, date}]."""
    commits, page = [], 1
    while True:
        url = (f"https://api.github.com/repos/{REPO}/commits"
               f"?path={FILE}&per_page=100&page={page}")
        batch = json.loads(_get(url))
        if not batch:
            break
        commits += [{"sha": c["sha"], "date": c["commit"]["committer"]["date"][:10]}
                    for c in batch]
        page += 1
    return list(reversed(commits))


def fetch_snapshot(sha: str) -> list[dict]:
    raw = _get(f"https://raw.githubusercontent.com/{REPO}/{sha}/{FILE}").decode("utf-8", "ignore")
    rows = []
    for r in csv.DictReader(io.StringIO(raw)):
        rows.append({k: r.get(k) for k in KEEP} | {"scrape_date": r.get("scrape_date")})
    return rows


def load(path: Path = DEFAULT_PATH) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def build_archive(path: Path = DEFAULT_PATH, pause: float = 0.3) -> dict:
    """Fetch every snapshot not already stored. Restart-safe: keyed by commit sha, and
    each snapshot is one JSONL line appended as it lands."""
    have = {s["sha"] for s in load(path)}
    commits = list_snapshot_commits()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = 0
    with open(path, "a", encoding="utf-8") as f:
        for c in commits:
            if c["sha"] in have:
                continue
            try:
                rows = fetch_snapshot(c["sha"])
            except Exception as e:
                print(f"  skip {c['sha'][:8]} ({c['date']}): {type(e).__name__}", file=sys.stderr)
                continue
            scrape = next((r["scrape_date"] for r in rows if r.get("scrape_date")), c["date"])
            f.write(json.dumps({"sha": c["sha"], "commit_date": c["date"],
                                "scrape_date": scrape, "rows": rows}) + "\n")
            new += 1
            time.sleep(pause)
    return {"new": new, "total": len(have) + new, "path": str(path)}


if __name__ == "__main__":
    summary = build_archive()
    snaps = load()
    dates = sorted(s["scrape_date"] for s in snaps)
    print(f"{summary['new']} new snapshots -> {len(snaps)} total, "
          f"{dates[0]} .. {dates[-1]}" if snaps else "no snapshots")
