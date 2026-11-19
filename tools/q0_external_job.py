"""Submit one saved Q0 fixture to GitHub-hosted compute and recover by request ID."""

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from q0_destination import (
    APPROVED_BUILD_REF, APPROVED_BUILD_REPOSITORY, APPROVED_BUILD_WORKFLOW,
    BUILD_DESTINATION, Q0_AGENT, Destination, SourceSnapshot, send_source,
)
from q0_remote_compile import source_bytes

WORKFLOW = APPROVED_BUILD_WORKFLOW
ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.check_output(args, text=True, cwd=ROOT)


def write(path, payload):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


def compile_runs(state):
    repo = state["repository"]
    request_title = f"q0-compile {state['request_id']}"
    pages = json.loads(
        run(
            "gh", "api", "--paginate", "--slurp",
            f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100",
        )
    )
    matches = [
        item for page in pages for item in page["workflow_runs"]
        if item["display_title"] == request_title
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Duplicate build requests accepted: {len(matches)} for {request_title}")
    return matches


def wait_for_job(state, *, completed):
    stop = time.monotonic() + 150
    while time.monotonic() < stop:
        matches = compile_runs(state)
        if matches and (not completed or matches[0]["status"] == "completed"):
            return matches[0]
        time.sleep(2)
    raise RuntimeError("Result needs reconciliation. Retain the request ID and query again.")


def save(args, journal):
    data = args.source.read_bytes()
    encoded = base64.b64encode(data).decode()
    source_bytes(encoded)
    digest = hashlib.sha256(data).hexdigest()
    (journal.parent / "saved.py").write_bytes(data)
    state = {
        "request_id": uuid.uuid4().hex,
        "source_sha256": digest,
        "repository": args.repository,
        "ref": args.ref,
        "state": "saved",
    }
    write(journal, state)
    print(f"Saved {digest} for {state['request_id']}", flush=True)


def submit(args, journal, state):
    if state["state"] != "saved":
        raise RuntimeError("Request already sent or pending. Reconcile it before attempting any further action.")
    source = (journal.parent / "saved.py").read_bytes()

    def dispatch(request_id, snapshot):
        # Persist the intent before the external action. A dead sender or lost
        # response leaves a request ID that a fresh process can query.
        state["state"] = "awaiting_receipt"
        write(journal, state)
        subprocess.run(
            [
                "gh", "workflow", "run", WORKFLOW, "-R", state["repository"], "--ref", state["ref"],
                "-f", f"request_id={request_id}",
                "-f", f"source_sha={snapshot.version}",
                "-f", f"source_base64={base64.b64encode(snapshot.content).decode()}",
            ],
            cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
        )

    send_source(
        Q0_AGENT,
        SourceSnapshot(state["source_sha256"], source),
        Destination(BUILD_DESTINATION, "build.submit", f"{state['repository']}:{WORKFLOW}@{state['ref']}"),
        state["request_id"], journal.parent / "destination-policy.jsonl", dispatch,
    )
    accepted = wait_for_job(state, completed=False)
    print(f"External runner accepted {state['request_id']} as run {accepted['id']}", flush=True)
    if args.lose_receipt:
        print("Fault injected: sender exited before recording the external run ID", flush=True)
        os._exit(75)
    write(journal, {**state, "state": "accepted", "run_id": accepted["id"]})


def reconcile(journal, state):
    if state["state"] == "saved":
        raise RuntimeError("The request has not been dispatched.")
    result = wait_for_job(state, completed=True)
    run_id = result["id"]
    if result["conclusion"] != "success" or result["run_attempt"] != 1:
        raise RuntimeError(f"External build {run_id} did not succeed on its only attempt")
    if "run_id" in state and state["run_id"] != run_id:
        raise RuntimeError("Recovered run differs from the recorded receipt.")
    output = journal.parent / "compiled"
    if not output.exists():
        subprocess.run(
            [
                "gh", "run", "download", str(run_id), "-R", state["repository"],
                "-n", f"q0-build-{state['request_id']}", "-D", str(output),
            ], check=True, cwd=ROOT,
        )
    receipt = json.loads((output / "receipt.json").read_text())
    if (
        receipt["request_id"] != state["request_id"]
        or int(receipt["run_id"]) != run_id
        or receipt["source_sha256"] != state["source_sha256"]
        or receipt["runner"] != "github-hosted"
        or receipt["runner_os"] != "Linux"
        or receipt["case_results"] != [21, 0, 200]
        or hashlib.sha256((output / "quote.pyc").read_bytes()).hexdigest() != receipt["artifact_sha256"]
    ):
        raise RuntimeError("External artifact provenance failed verification.")
    jobs = json.loads(run("gh", "api", f"repos/{state['repository']}/actions/runs/{run_id}/jobs"))
    build_steps = [
        step for job in jobs["jobs"] for step in job["steps"]
        if step["name"] == "Compile once in the hosted runner" and step["conclusion"] == "success"
    ]
    if len(build_steps) != 1 or len(compile_runs(state)) != 1:
        raise RuntimeError("Expected one external build step in one accepted run.")
    report = {
        **state,
        "state": "verified",
        "run_id": run_id,
        "run_url": result["html_url"],
        "compile_step_count": len(build_steps),
        "external_run_count": 1,
        "artifact_receipt": receipt,
    }
    write(journal, report)
    print(json.dumps(report, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    save_args = actions.add_parser("save")
    save_args.add_argument("source", type=Path)
    save_args.add_argument("--repository", default=APPROVED_BUILD_REPOSITORY)
    save_args.add_argument("--ref", default=APPROVED_BUILD_REF)
    send_args = actions.add_parser("submit")
    send_args.add_argument("--lose-receipt", action="store_true")
    actions.add_parser("reconcile")
    args = parser.parse_args()
    area = args.state_dir.resolve()
    area.mkdir(parents=True, exist_ok=True)
    journal = area / "request.json"

    with (area / "request.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "save":
            if journal.exists():
                raise RuntimeError("Use a new state directory for another request.")
            save(args, journal)
        else:
            state = json.loads(journal.read_text())
            if args.action == "submit":
                submit(args, journal, state)
            else:
                reconcile(journal, state)


if __name__ == "__main__":
    main()
