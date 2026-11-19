"""Test the egress gate against two independent, loopback-only receivers."""

import argparse
import hashlib
import http.server
import json
from pathlib import Path
from threading import Thread
import urllib.request

from q0_destination import (
    BUILD_BINDING, BUILD_DESTINATION, MODEL_DESTINATION, Q0_AGENT,
    Destination, DestinationDenied, SourceSnapshot, append_record, send_source,
)

ROOT = Path(__file__).resolve().parents[1]


class Receiver:
    def __init__(self, name, log_path):
        self.name = name
        self.log_path = log_path

    def __enter__(self):
        name, log_path = self.name, self.log_path

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                append_record(log_path, {"destination": name, "method": "GET", "path": self.path})
                self.send_response(204)
                self.end_headers()

            def do_POST(self):
                size = int(self.headers["Content-Length"])
                source = self.rfile.read(size)
                append_record(log_path, {
                    "destination": name,
                    "method": "POST",
                    "request_id": self.headers["X-Q0-Request"],
                    "declared_source_version": self.headers["X-Q0-Source-Version"],
                    "observed_source_version": hashlib.sha256(source).hexdigest(),
                    "body_size": len(source),
                })
                self.send_response(204)
                self.end_headers()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *unused):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def ping(self):
        with urllib.request.urlopen(self.url + "/health", timeout=3) as response:
            return response.status == 204

    def receive(self, request_id, snapshot):
        request = urllib.request.Request(
            self.url + "/source", data=snapshot.content, method="POST",
            headers={"X-Q0-Request": request_id, "X-Q0-Source-Version": snapshot.version},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 204:
                raise RuntimeError("Receiver did not accept the snapshot")


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def exercise(directory, inject):
    directory.mkdir(parents=True, exist_ok=True)
    policy_path = directory / "policy.jsonl"
    receiver_path = directory / "receivers.jsonl"
    if policy_path.exists() or receiver_path.exists():
        raise FileExistsError("Use a new evidence directory.")
    source = (ROOT / "tests/fixtures/quote_current.py").read_bytes() + b"\n# Q0 synthetic source marker\n"
    expected_version = hashlib.sha256(source).hexdigest()
    snapshot = SourceSnapshot(expected_version, source)
    build_dest = Destination(BUILD_DESTINATION, "build.submit", BUILD_BINDING)
    model_dest = Destination(MODEL_DESTINATION, "model.invoke", "q0-isolated-model-receiver")
    with Receiver(BUILD_DESTINATION, receiver_path) as builder, Receiver(MODEL_DESTINATION, receiver_path) as model:
        assert builder.ping() and model.ping(), "Receivers must both be reachable"
        send_source(Q0_AGENT, snapshot, build_dest, "allowed-build", policy_path, builder.receive)
        settings = {}
        if inject:
            # Mutate the test policy only. Both destinations are loopback
            # receivers in this process and all the bytes are synthetic.
            settings = {
                "allowed_destinations": frozenset({(model_dest.action, model_dest.identifier, model_dest.binding)}),
                "policy_version": "q0-deliberately-overbroad-test-policy",
            }
        reason = None
        try:
            send_source(Q0_AGENT, snapshot, model_dest, "unapproved-model", policy_path, model.receive, **settings)
        except DestinationDenied as error:
            reason = str(error)
    received = records(receiver_path)
    allowed = [item for item in received if item["method"] == "POST" and item["destination"] == BUILD_DESTINATION]
    denied = [item for item in received if item["method"] == "POST" and item["destination"] == MODEL_DESTINATION]
    decisions = {item["request_id"]: item for item in records(policy_path)}
    checks = {
        "one_authorized_build_receive": len(allowed) == 1,
        "compiler_received_saved_version": len(allowed) == 1 and (
            allowed[0]["declared_source_version"] == allowed[0]["observed_source_version"] == expected_version
        ),
        "compiler_allowed_by_policy": decisions["allowed-build"]["decision"] == "allow",
        "agent_has_read_and_model_grants": decisions["unapproved-model"]["required_grants_present"],
        "unapproved_model_denied_before_send": reason == "source_destination_not_authorized",
        "unapproved_model_received_zero_posts": not denied,
    }
    report = {
        "scenario": "overbroad-test-policy" if inject else "destination-policy",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "source_version": expected_version,
        "checks": checks,
        "compiler_requests_received": len(allowed),
        "unapproved_model_requests_received": len(denied),
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return all(checks.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--overbroad-test-policy", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if exercise(args.evidence, args.overbroad_test_policy) else 1)


if __name__ == "__main__":
    main()
