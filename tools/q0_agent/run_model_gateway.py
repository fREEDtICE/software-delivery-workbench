"""Connect the pinned DSH model adapter to the isolated Q0 model gateway."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
from q0_model_gateway import APPROVED_ALIAS, APPROVED_MODEL, UNAPPROVED_ALIAS, UNAPPROVED_MODEL
from verify_q0_model_gateway import model_request, records, rig, source_prompt


def run_dsh(probe, lab, running, directory):
    patch = lab / "dsh-home/profiles/multica/cordis.patch.yml"
    original = patch.read_text()
    providers_end = original.index("\n- id:", 1)
    models = [
        "- id: llm-pi-ai",
        "  config:",
        "    providers:",
        "      q0-gateway:",
        "        apiKeyEnv: Q0_LOCAL_MODEL_TOKEN",
        "        api: openai-completions",
        f"        baseURL: {running.gateway_url}/v1",
        "        models:",
    ]
    for alias in (APPROVED_ALIAS, UNAPPROVED_ALIAS):
        models.extend([f"          - id: {alias}", "            contextWindow: 32768", "            maxTokens: 512", "            input: [text]"])
    patch.write_text("\n".join(models) + original[providers_end:])
    outcomes = {}
    try:
        with TemporaryDirectory(dir=lab) as temp:
            trial = Path(temp)
            runtime_policy = trial / "gateway.sb"
            source_policy = (lab / "runtime.sb").read_text()
            assert source_policy.count("localhost:11434") == 1
            runtime_policy.write_text(source_policy.replace("localhost:11434", f"localhost:{urlsplit(running.gateway_url).port}"))
            template = (lab / "dsh").read_text()
            assert template.count("Q0_LOCAL_MODEL_TOKEN=local-test") == 1
            launcher = trial / "dsh"
            launcher.write_text(template.replace(str(lab / "runtime.sb"), str(runtime_policy)).replace(
                "Q0_LOCAL_MODEL_TOKEN=local-test", 'Q0_LOCAL_MODEL_TOKEN="$Q0_GATEWAY_KEY"',
            ))
            launcher.chmod(0o700)
            prompt = trial / "prompt.txt"
            prompt.write_text("/no_think\n" + source_prompt(running.snapshot.content))
            for alias in (APPROVED_ALIAS, UNAPPROVED_ALIAS):
                work = trial / alias
                (work / "cwd").mkdir(parents=True)
                (work / "sessions").mkdir()
                command = [
                    str(probe), "-dsh", str(launcher), "-model", f"q0-gateway/{alias}",
                    "-cwd", str(work / "cwd"), "-session-root", str(work / "sessions"),
                    "-prompt-file", str(prompt),
                ]
                result_path = directory / f"{alias}-agent.json"
                stderr_path = directory / f"{alias}-agent.stderr"
                with result_path.open("wb") as out, stderr_path.open("wb") as err:
                    completed = subprocess.run(
                        command, stdout=out, stderr=err, timeout=180,
                        env={**os.environ, "Q0_GATEWAY_KEY": running.key},
                    )
                outcomes[alias] = {
                    "exit_code": completed.returncode,
                    "trace": json.loads(result_path.read_text()),
                }
    finally:
        patch.write_text(original)
    return outcomes


def verify(probe, lab, evidence):
    with rig(evidence, real=True) as running:
        outcomes = run_dsh(probe, lab, running, evidence)
        denied, response = model_request(running, UNAPPROVED_ALIAS, running.key, fake_public=True)
    audit = records(evidence, "gateway.jsonl")
    received = records(evidence, "receivers.jsonl")
    approve = [entry for entry in audit if entry["destination"] == APPROVED_MODEL]
    deny = [entry for entry in audit if entry["destination"] == UNAPPROVED_MODEL]
    approved_posts = [entry for entry in received if entry["destination"] == APPROVED_MODEL and entry["method"] == "POST"]
    unapproved_posts = [entry for entry in received if entry["destination"] == UNAPPROVED_MODEL and entry["method"] == "POST"]
    approved_turn = outcomes[APPROVED_ALIAS]["trace"]["result"]
    unapproved_turn = outcomes[UNAPPROVED_ALIAS]["trace"]["result"]
    assert approved_turn["Status"] == "completed" and approved_turn["Usage"], approved_turn
    assert "source_destination_not_authorized" in unapproved_turn["Error"], unapproved_turn
    assert denied == 403 and response["error"]["code"] == "source_destination_not_authorized"
    assert approve and all(d["decision"] == "allow" for d in approve)
    assert len(deny) >= 2 and all(
        d["decision"] == "deny" and d["required_grants_present"] and d["source_version"] == running.snapshot.version for d in deny
    )
    assert approved_posts and all(r["observed_source_versions"] == [running.snapshot.version] for r in approved_posts)
    assert {d["request_id"] for d in approve} == {r["request_id"] for r in approved_posts}
    assert not unapproved_posts
    result = {
        "source_version": running.snapshot.version,
        "approved_inference": approved_turn,
        "denied_inference": unapproved_turn,
        "unapproved_http_status": denied,
        "gateway_approved_count": len(approve),
        "gateway_denied_count": len(deny),
        "approved_receiver_posts": len(approved_posts),
        "unapproved_receiver_posts": len(unapproved_posts),
    }
    (evidence / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("probe", "lab", "evidence"):
        parser.add_argument(name, type=lambda arg: Path(arg).resolve())
    args = parser.parse_args()
    verify(args.probe, args.lab, args.evidence)


if __name__ == "__main__":
    main()
