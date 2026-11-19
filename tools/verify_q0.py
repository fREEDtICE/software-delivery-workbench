"""Run the controlled build and independently verify provenance and behavior."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
EXECUTE_ARTIFACT = """\
import importlib.machinery
import importlib.util
import json
import sys

loader = importlib.machinery.SourcelessFileLoader("quote", sys.argv[1])
module = importlib.util.module_from_spec(importlib.util.spec_from_loader("quote", loader))
loader.exec_module(module)
print(json.dumps([module.total(*case) for case in json.loads(sys.argv[2])]))
"""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage_source(name, workspace):
    source = FIXTURES / name
    version = digest(source)
    saved = workspace / version / "quote.py"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(source.read_bytes())
    return version, saved


def verify(scenario, workdir, report):
    expected_version, current = stage_source("quote_current.py", workdir / "workspace")
    source = current
    if scenario == "wrong-source":
        _, source = stage_source("quote_previous.py", workdir / "workspace")

    output = workdir / "artifacts"
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "compile_fixture.py"), str(source), str(output)],
        cwd=workdir,
        check=True,
    )
    receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
    artifact_digest = digest(output / "quote.pyc")

    cases = [(3, 7), (0, 12), (2, 100)]
    expected = [21, 0, 200]
    observed = json.loads(
        subprocess.check_output(
            [sys.executable, "-I", "-c", EXECUTE_ARTIFACT, str(output / "quote.pyc"), json.dumps(cases)],
            text=True,
            cwd=workdir,
        )
    )
    report.update(
        requested_source_sha256=expected_version,
        executor_receipt=receipt,
        observed_artifact_sha256=artifact_digest,
        checks=[
            {
                "name": "saved-source-version",
                "passed": receipt["source_sha256"] == expected_version,
                "expected": expected_version,
                "observed": receipt["source_sha256"],
            },
            {
                "name": "artifact-integrity",
                "passed": receipt["artifact_sha256"] == artifact_digest,
                "expected": receipt["artifact_sha256"],
                "observed": artifact_digest,
            },
            {
                "name": "compiled-behavior",
                "passed": observed == expected,
                "inputs": cases,
                "expected": expected,
                "observed": observed,
            },
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["normal", "wrong-source"], default="normal")
    parser.add_argument("--report", type=Path, default=Path("evidence/q0-baseline.json"))
    args = parser.parse_args()

    report = {
        "scenario": args.scenario,
        "status": "BLOCKED",
        "python": platform.python_version(),
        "commit": os.getenv("GITHUB_SHA"),
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "checks": [],
    }
    exit_code = 2
    try:
        with TemporaryDirectory(prefix="q0-baseline-") as temporary:
            verify(args.scenario, Path(temporary), report)
        passed = all(check["passed"] for check in report["checks"])
        report["status"] = "PASS" if passed else "FAIL"
        exit_code = 0 if passed else 1
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        report["error"] = str(error)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    lines = [f"Q0 baseline / {args.scenario} / {report['status']}"]
    for check in report["checks"]:
        result = "PASS" if check["passed"] else "FAIL"
        lines.append(f"- {result} {check['name']}")
        if not check["passed"]:
            lines.append(f"  expected {check['expected']}; observed {check['observed']}")
    if "error" in report:
        lines.append(report["error"])
    print("\n".join(lines), flush=True)
    if summary := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("### " + "\n".join(lines) + "\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
