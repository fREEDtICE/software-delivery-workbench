"""Resolve the locked Multica and DSH commits via the GitHub API."""

import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"multica": "multica-ai/multica", "dsh": "deepseek-ai/deepseek-harness"}


def main():
    lock = json.loads((ROOT / "upstreams.lock.json").read_text(encoding="utf-8"))
    if set(lock) != set(EXPECTED):
        raise SystemExit("The lock must have exactly the Multica and DSH entries.")

    evidence = {}
    for name, repository in EXPECTED.items():
        entry = lock[name]
        commit = entry["commit"]
        if entry["repository"] != repository or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise SystemExit(f"Invalid source pin for {name}.")
        resolved = subprocess.check_output(
            ["gh", "api", f"repos/{repository}/git/commits/{commit}", "--jq", ".sha"],
            text=True,
        ).strip()
        if resolved != commit:
            raise SystemExit(f"{repository} resolved to {resolved}, expected {commit}.")
        print(f"Resolved {repository}@{commit}")
        evidence[name] = {**entry, "resolved_commit": resolved}

    path = ROOT / "evidence" / "upstreams.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
