"""Exercise real HTTP model egress and compare independent receiver evidence."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from threading import Thread
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from q0_destination import SourceSnapshot, append_record
from q0_model_gateway import (
    API_PATH, APPROVED_ALIAS, APPROVED_MODEL, UNAPPROVED_ALIAS, UNAPPROVED_MODEL,
    ModelRoute, gateway, post_model,
)

ROOT = Path(__file__).resolve().parents[1]
MARKER_OPEN = "<q0-source>\n"
MARKER_CLOSE = "</q0-source>"
OLLAMA_URL = "http://127.0.0.1:11434" + API_PATH


def source_sample():
    return (ROOT / "tests/fixtures/quote_current.py").read_bytes() + b"\n# Q0 synthetic source marker\n"


def source_prompt(source):
    return (
        "Read the synthetic Python function below. Answer with the value of total(3, 7) only.\n"
        + MARKER_OPEN + source.decode() + MARKER_CLOSE
    )


@contextmanager
def listening(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def receiver(name, audit, upstream_model=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path != "/health":
                return self.send_error(404)
            append_record(audit, {"destination": name, "method": "GET", "path": self.path})
            self.send_response(204)
            self.end_headers()

        def do_POST(self):
            if self.path != API_PATH:
                return self.send_error(404)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            digests = []
            for message in body["messages"]:
                text = message.get("content")
                if isinstance(text, str) and MARKER_OPEN in text and MARKER_CLOSE in text:
                    source = text.split(MARKER_OPEN, 1)[1].split(MARKER_CLOSE, 1)[0]
                    digests.append(hashlib.sha256(source.encode()).hexdigest())
            request_id = self.headers["X-Q0-Request"]
            append_record(audit, {
                "destination": name, "method": "POST", "request_id": request_id,
                "observed_source_versions": digests, "requested_model": body["model"],
            })
            if upstream_model:
                status, content_type, data = post_model(
                    ModelRoute(name, OLLAMA_URL, upstream_model), body, request_id,
                )
            else:
                status, content_type = 200, "application/json"
                data = json.dumps({
                    "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "21"}, "finish_reason": "stop"}],
                }).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


@dataclass(frozen=True)
class Rig:
    gateway_url: str
    key: str
    snapshot: SourceSnapshot


@contextmanager
def rig(directory, *, real=False, overbroad=False):
    if real and overbroad:
        raise ValueError("Policy fault injection is limited to isolated fixture receivers")
    directory.mkdir()
    source = source_sample()
    snapshot = SourceSnapshot(hashlib.sha256(source).hexdigest(), source)
    log = directory / "receivers.jsonl"
    key = secrets.token_hex(24)
    client = build_opener(ProxyHandler({}))
    with (
        listening(receiver(APPROVED_MODEL, log, "qwen3:1.7b" if real else None)) as allowed,
        listening(receiver(UNAPPROVED_MODEL, log, "qwen3.5:0.8b" if real else None)) as forbidden,
    ):
        for url in (allowed, forbidden):
            with client.open(url + "/health", timeout=3) as response:
                assert response.status == 204
        routes = {
            APPROVED_ALIAS: ModelRoute(APPROVED_MODEL, allowed + API_PATH, "qwen3:1.7b"),
            UNAPPROVED_ALIAS: ModelRoute(UNAPPROVED_MODEL, forbidden + API_PATH, "qwen3.5:0.8b"),
        }
        with listening(gateway(
            routes, key, snapshot, directory / "gateway.jsonl", admit_negative_fixture=overbroad,
        )) as address:
            yield Rig(address, key, snapshot)


def records(directory, file):
    return [json.loads(line) for line in (directory / file).read_text().splitlines()]


def model_request(running, alias, key, *, fake_public=False):
    body = {
        "model": alias, "messages": [{"role": "user", "content": source_prompt(running.snapshot.content)}],
        "stream": False, "max_tokens": 256,
    }
    if fake_public:
        body["source_class"] = "public"
    request = Request(
        running.gateway_url + API_PATH, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    client = build_opener(ProxyHandler({}))
    try:
        with client.open(request, timeout=135) as response:
            return response.status, json.load(response)
    except HTTPError as response:
        with response:
            return response.code, json.load(response)


def verify(directory, overbroad):
    with rig(directory, overbroad=overbroad) as running:
        denied_bad_key, _ = model_request(running, APPROVED_ALIAS, "unauthorized-identity")
        allowed, response = model_request(running, APPROVED_ALIAS, running.key)
        denied, error = model_request(running, UNAPPROVED_ALIAS, running.key, fake_public=True)
    audit = records(directory, "gateway.jsonl")
    observed = records(directory, "receivers.jsonl")
    posts = [r for r in observed if r["method"] == "POST"]
    approved = [r for r in posts if r["destination"] == APPROVED_MODEL]
    unapproved = [r for r in posts if r["destination"] == UNAPPROVED_MODEL]
    checks = {
        "unauthenticated_request_rejected": denied_bad_key == 401,
        "approved_inference_completed": allowed == 200 and response["choices"][0]["message"]["content"] == "21",
        "approved_receiver_observed_saved_version": len(approved) == 1
        and approved[0]["observed_source_versions"] == [running.snapshot.version]
        and approved[0]["request_id"] == audit[0]["request_id"],
        "source_and_model_grants_present": audit[1]["required_grants_present"],
        "gateway_rejected_unapproved_destination": denied == 403
        and error["error"]["code"] == "source_destination_not_authorized"
        and audit[1]["decision"] == "deny",
        "user_supplied_public_label_did_not_override_scope": audit[1]["source_class"] == "restricted-source"
        and audit[1]["decision"] == "deny",
        "unapproved_receiver_observed_zero_posts": len(unapproved) == 0,
    }
    result = {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "source_version": running.snapshot.version,
        "approved_status": allowed,
        "unapproved_status": denied,
        "approved_receiver_posts": len(approved),
        "unapproved_receiver_posts": len(unapproved),
        "checks": checks,
    }
    (directory / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return all(checks.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--overbroad-test-policy", action="store_true")
    args = parser.parse_args()
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(0 if verify(args.evidence, args.overbroad_test_policy) else 1)


if __name__ == "__main__":
    main()
