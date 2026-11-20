"""Private Q0 services with separate runtime, model and object networks."""

import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import py_compile
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from q0_remote_compile import CASES, source_bytes
from q0_destination import POLICY_VERSION
from verify_q0 import EXECUTE_ARTIFACT

ROLE = sys.argv[1]
PORT = 8080
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
CHATS = "/v1/chat/completions"
ALLOWED_MODEL = "pilot-approved"
DENIED_MODEL = "pilot-unapproved"


def log(event, **fields):
    print(json.dumps({"service": ROLE, "event": event, **fields}, sort_keys=True), flush=True)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(url, method="GET", body=None, *, key=None, work=None, timeout=310):
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key
    if work:
        headers["X-Q0-Work"] = work
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=body, headers=headers, method=method)
    with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=timeout) as response:
        return response.headers["Content-Type"], response.read()


def object_data(kind, digest, data=None, work=None):
    return request(
        f"http://objects:{PORT}/objects/{kind}/{digest}", "PUT" if data is not None else "GET",
        data, key=os.environ["OBJECT_KEY"], work=work,
    )[1]


def compiler(version, work, build_dir):
    received = object_data("source", version, work=work)
    assert sha(received) == version
    source_bytes(base64.b64encode(received).decode())
    log("source_received", work=work, version=version, bytes=len(received))
    src = build_dir / "quote.py"
    artifact = build_dir / "quote.pyc"
    src.write_bytes(received)
    py_compile.compile(str(src), cfile=str(artifact), dfile="quote.py", doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
    cases = json.loads(subprocess.check_output(
        [sys.executable, "-I", "-c", EXECUTE_ARTIFACT, str(artifact), json.dumps(CASES)], text=True,
    ))
    data = artifact.read_bytes()
    receipt = {"source_version": version, "artifact_sha256": sha(data), "case_results": cases, "work": work}
    object_data("artifact", receipt["artifact_sha256"], data, work)
    log("compile_and_direct_store", **receipt)
    return receipt


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data, content_type="application/json"):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authenticated(self, key):
        return hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + key)

    def lease_ok(self):
        return time.time() < int(os.environ["LEASE_EXPIRY"]) and self.authenticated(os.environ["WORK_LEASE"])

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 65536:
            raise ValueError("invalid length")
        return self.rfile.read(length)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"service": ROLE})
        if ROLE == "objects":
            return self.object_request("GET")
        self.reply(404, {"error": "unknown_route"})

    def do_PUT(self):
        if ROLE == "objects":
            return self.object_request("PUT")
        self.reply(404, {"error": "unknown_route"})

    def object_request(self, method):
        fields = self.path.split("/")
        if len(fields) != 4 or fields[1] != "objects" or not DIGEST.fullmatch(fields[3]):
            return self.reply(404, {"error": "unknown_object"})
        kind, digest = fields[2:]
        work = self.headers.get("X-Q0-Work")
        if self.authenticated(os.environ["WORKSPACE_OBJECT_KEY"]):
            actor = "workspace"
        elif self.authenticated(os.environ["COMPILER_OBJECT_KEY"]):
            actor = "compiler"
        else:
            return self.reply(401, {"error": "object_service_credentials_required"})
        if (actor, method, kind) not in {("workspace", "PUT", "source"), ("compiler", "GET", "source"), ("compiler", "PUT", "artifact")}:
            return self.reply(403, {"error": "object_service_operation_denied"})
        path = Path("/data") / kind / digest
        if method == "GET":
            if not path.is_file():
                return self.reply(404, {"error": "missing_object"})
            data = path.read_bytes()
            log("object_read", actor=actor, kind=kind, sha256=sha(data), work=work)
            return self.reply(200, data, "application/octet-stream")
        data = self.body()
        if sha(data) != digest:
            return self.reply(409, {"error": "digest_mismatch"})
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(output.name, path)
        log("object_written", actor=actor, kind=kind, sha256=digest, bytes=len(data), work=work)
        self.reply(201, {"sha256": digest})

    def do_POST(self):
        try:
            if ROLE == "workspace" and self.path == "/save":
                return self.save_source()
            if ROLE == "compiler" and self.path == "/build":
                return self.compile_source()
            if ROLE == "gateway" and self.path == CHATS:
                return self.model_gateway()
            if ROLE in {"model-receiver", "denied-receiver"} and self.path == CHATS:
                return self.model_receive()
            self.reply(404, {"error": "unknown_route"})
        except (KeyError, ValueError, SyntaxError, TypeError):
            self.reply(400, {"error": "invalid_payload"})
        except (OSError, subprocess.SubprocessError):
            log("downstream_failed")
            self.reply(502, {"error": "downstream_failed"})

    def save_source(self):
        if not self.lease_ok():
            return self.reply(401, {"error": "lease_required"})
        source = source_bytes(base64.b64encode(self.body()).decode())
        version = sha(source)
        work = os.environ["WORK_ID"]
        object_data("source", version, source, work)
        log("save_confirmed", work=work, source_version=version)
        self.reply(201, {"saved_version": version, "work": work})

    def compile_source(self):
        if not self.authenticated(os.environ["BUILD_KEY"]):
            return self.reply(401, {"error": "build_credential_required"})
        body = json.loads(self.body())
        version, work = body["source_version"], body["work"]
        if not DIGEST.fullmatch(version):
            raise ValueError("invalid version")
        with tempfile.TemporaryDirectory() as tmp:
            result = compiler(version, work, Path(tmp))
        self.reply(201, result)

    def model_gateway(self):
        if not self.lease_ok():
            return self.reply(401, {"error": {"message": "lease_required", "code": "lease_required"}})
        body = json.loads(self.body())
        model = body["model"]
        allowed = model == ALLOWED_MODEL
        known = model in {ALLOWED_MODEL, DENIED_MODEL}
        log("model_destination", work=os.environ["WORK_ID"], destination=model if known else "unrecognized",
            classification="restricted-source", policy_version=POLICY_VERSION,
            decision="allow" if allowed else "deny",
            reason="approved_work_model" if allowed else "source_destination_not_authorized")
        if not allowed:
            return self.reply(403, {"error": {"message": "source_destination_not_authorized", "code": "source_destination_not_authorized"}})
        # This arithmetic fixture is a tool-use check, with a small inference budget.
        payload = {**body, "model": "qwen3:1.7b", "max_tokens": 512, "temperature": 0,
                   "reasoning": {"effort": "none"}, "reasoning_effort": "none"}
        content_type, data = request(
            "http://model-receiver:8080" + CHATS, "POST", json.dumps(payload).encode(), work=os.environ["WORK_ID"],
        )
        self.reply(200, data, content_type)

    def model_receive(self):
        body = self.body()
        log("model_received", work=self.headers.get("X-Q0-Work"), body_bytes=len(body))
        if ROLE == "denied-receiver":
            return self.reply(403, {"error": "unexpected_model_route"})
        start = time.monotonic()
        content_type, response = request("http://ollama:11434" + CHATS, "POST", body)
        log("model_completed", work=self.headers.get("X-Q0-Work"), duration_ms=round(1000 * (time.monotonic() - start)))
        self.reply(200, response, content_type)


ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
