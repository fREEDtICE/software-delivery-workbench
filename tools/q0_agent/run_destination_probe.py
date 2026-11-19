"""Exercise the DSH Agent's source-read and model-invoke tools using a controlled receiver."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
from q0_destination import MODEL_DESTINATION
from verify_q0_destination import Receiver, records


def verify(probe, lab, source, output):
    output.mkdir()
    log = output / "receivers.jsonl"
    version = hashlib.sha256(source.read_bytes()).hexdigest()
    with Receiver(MODEL_DESTINATION, log) as receiver, TemporaryDirectory(dir=lab) as temp:
        trial = Path(temp)
        (trial / "scratch").mkdir()
        (trial / "sessions").mkdir()
        audit = trial / "policy.jsonl"
        settings = trial / "settings.json"
        settings.write_text(json.dumps({
            "snapshot": str(source), "source_version": version, "audit": str(audit),
            "receiver": receiver.url,
        }))
        # Permit this precise loopback test receiver in the disposable profile
        # so health from the Agent tool proves transport can reach it.
        policy = trial / "destination.sb"
        policy.write_text(
            (lab / "runtime.sb").read_text()
            + f'(allow network-outbound (remote ip "localhost:{receiver.server.server_port}"))\n'
        )
        launcher = trial / "dsh"
        launcher.write_text((lab / "dsh").read_text().replace(str(lab / "runtime.sb"), str(policy)))
        launcher.chmod(0o700)
        command = [
            str(probe), "-dsh", str(launcher), "-model", "q0-ollama/qwen3:1.7b",
            "-cwd", str(trial / "scratch"), "-session-root", str(trial / "sessions"),
            "-python", sys.executable, "-tool", str(TOOLS / "q0_agent" / "destination_tools.py"),
            "-destination-settings", str(settings),
        ]
        with (output / "agent.json").open("wb") as stream, (output / "agent.stderr").open("wb") as stderr:
            result = subprocess.run(command, stdout=stream, stderr=stderr, timeout=190)
        if audit.exists():
            (output / "policy.jsonl").write_bytes(audit.read_bytes())
    assert result.returncode == 0, f"Agent failed, see {output}"
    report = json.loads((output / "agent.json").read_text())
    evidence = records(output / "policy.jsonl")
    received = records(log)
    reads = [entry for entry in evidence if entry["record_type"] == "source_read"]
    decisions = [entry for entry in evidence if entry["record_type"] == "source_egress_decision"]
    assert reads and reads[0]["decision"] == "allow" and reads[0]["source_version"] == version
    assert decisions and all(
        entry["agent"] == reads[0]["agent"]
        and entry["required_grants_present"]
        and entry["source_version"] == version
        and entry["decision"] == "deny"
        and entry["reason"] == "source_destination_not_authorized"
        for entry in decisions
    )
    assert evidence.index(reads[0]) < evidence.index(decisions[0])
    assert report["result"]["Usage"]
    assert any(
        entry.get("tool") == "mcp__egress__invoke_unapproved_model"
        and "DENIED: source_destination_not_authorized" in entry.get("output", "")
        for entry in report["events"]
    )
    assert received and all(entry["method"] == "GET" and entry["path"] == "/health" for entry in received)
    print(f"PASS: Agent read {version} and model egress was denied. Receiver recorded health and zero POSTs.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("probe", "lab", "source", "output"):
        parser.add_argument(name, type=lambda arg: Path(arg).resolve())
    args = parser.parse_args()
    verify(args.probe, args.lab, args.source, args.output)


if __name__ == "__main__":
    main()
