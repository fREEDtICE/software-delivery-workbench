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
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import build_jobs
import lease_policy
from q0_remote_compile import CASES, source_bytes
from q0_destination import POLICY_VERSION
from verify_q0 import EXECUTE_ARTIFACT

ROLE = sys.argv[1]
PORT = 8080
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
BUILD_ROUTE = re.compile(r"/builds/([0-9a-f]{32})(/release)?\Z")
CHATS = "/v1/chat/completions"
ALLOWED_MODEL = "pilot-approved"
DENIED_MODEL = "pilot-unapproved"


def log(event, **fields):
    print(json.dumps({"service": ROLE, "event": event, "at_ns": time.time_ns(), **fields}, sort_keys=True), flush=True)


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


def compile_job(job, build_dir):
    version, work, request_id = (job[k] for k in ("source_version", "work", "request_id"))
    identity = {"work": work, "request_id": request_id, "build_id": job["build_id"]}
    received = object_data("source", version, work=work)
    if sha(received) != version:
        raise ValueError("saved source digest mismatch")
    source_bytes(base64.b64encode(received).decode())
    log("source_received", **identity, version=version, bytes=len(received))
    src = build_dir / "quote.py"
    artifact = build_dir / "quote.pyc"
    src.write_bytes(received)
    build_jobs.start(request_id)
    log("build_waiting_for_release", **identity)

    # The pilot holds the build after source preparation for deterministic fault injection.
    deadline = time.monotonic() + 120
    while not build_jobs.lookup(request_id)["released"]:
        if time.monotonic() >= deadline:
            raise TimeoutError("operator did not release the build")
        time.sleep(0.1)

    log("compile_started", **identity)
    py_compile.compile(str(src), cfile=str(artifact), dfile="quote.py", doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
    cases = json.loads(subprocess.check_output(
        [sys.executable, "-I", "-c", EXECUTE_ARTIFACT, str(artifact), json.dumps(CASES)], text=True,
    ))
    data = artifact.read_bytes()
    receipt = {"source_version": version, "artifact_sha256": sha(data), "case_results": cases, **identity}
    object_data("artifact", receipt["artifact_sha256"], data, work)
    return receipt


def execute_job(job):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            receipt = compile_job(job, Path(tmp))
        log("compile_and_direct_store", **receipt)
        build_jobs.finish(job["request_id"], receipt)
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError, py_compile.PyCompileError) as error:
        build_jobs.finish(job["request_id"])
        log("build_failed", request_id=job["request_id"], error_type=type(error).__name__)


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

    def check_lease(self):
        authorization = self.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        _, data = request(
            f"http://policy:{PORT}/check", "POST", json.dumps({"lease_sha256": sha(token.encode())}).encode(),
            key=os.environ["POLICY_CHECK_KEY"], timeout=5,
        )
        result = json.loads(data)
        code = result["decision"]
        if code == "allow":
            return True
        log("lease_denied", work=os.environ["WORK_ID"], reason=code)
        error = {"message": code, "code": code} if ROLE == "gateway" else code
        self.reply(403 if code == "lease_revoked" else 401, {"error": error})
        return False

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
        if ROLE == "compiler" and (route := BUILD_ROUTE.fullmatch(self.path)) and not route[2]:
            if not self.authenticated(os.environ["BUILD_KEY"]):
                return self.reply(401, {"error": "build_credential_required"})
            job = build_jobs.lookup(route[1])
            return self.reply(200 if job else 404, job or {"error": "unknown_build"})
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
            if ROLE == "compiler" and self.path == "/builds":
                return self.submit_build()
            if ROLE == "compiler" and (route := BUILD_ROUTE.fullmatch(self.path)) and route[2]:
                if not self.authenticated(os.environ["BUILD_KEY"]):
                    return self.reply(401, {"error": "build_credential_required"})
                if not build_jobs.release(route[1]):
                    return self.reply(409, {"error": "build_is_not_waiting"})
                return self.reply(200, {"released": True})
            if ROLE == "policy" and self.path in {"/check", "/revoke"}:
                return self.policy_request()
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
        if not self.check_lease():
            return
        source = source_bytes(base64.b64encode(self.body()).decode())
        version = sha(source)
        work = os.environ["WORK_ID"]
        object_data("source", version, source, work)
        log("save_confirmed", work=work, source_version=version)
        self.reply(201, {"saved_version": version, "work": work})

    def submit_build(self):
        if not self.authenticated(os.environ["BUILD_KEY"]):
            return self.reply(401, {"error": "build_credential_required"})
        body = json.loads(self.body())
        version, work, request_id = body["source_version"], body["work"], body["request_id"]
        if not DIGEST.fullmatch(version) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("invalid build identity")
        job, created = build_jobs.submit(request_id, work, version)
        if job["work"] != work or job["source_version"] != version:
            return self.reply(409, {"error": "request_identity_conflict"})
        identity = {"work": work, "request_id": request_id, "build_id": job["build_id"]}
        if not created:
            log("duplicate_submission", **identity, status=job["status"])
            return self.reply(200, job)
        log("build_accepted", **identity, version=version)
        threading.Thread(target=execute_job, args=(job,), daemon=True).start()
        if body.get("drop_receipt") is True:
            # Simulate a transport fault after the durable accept, before any HTTP response.
            log("acceptance_response_dropped", **identity)
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_WR)
            return
        self.reply(202, job)

    def policy_request(self):
        work = os.environ["WORK_ID"]
        if self.path == "/revoke":
            if not self.authenticated(os.environ["POLICY_ADMIN_KEY"]):
                return self.reply(401, {"error": "policy_admin_required"})
            revoked_at = lease_policy.revoke()
            log("lease_revoked", work=work, revoked_at_ns=revoked_at)
            return self.reply(200, {"work": work, "revoked_at_ns": revoked_at})
        if not self.authenticated(os.environ["POLICY_CHECK_KEY"]):
            return self.reply(401, {"error": "policy_check_credential_required"})
        digest = json.loads(self.body())["lease_sha256"]
        decision = lease_policy.decide(hmac.compare_digest(digest, os.environ["WORK_LEASE_SHA256"]))
        log("lease_check", work=work, decision=decision)
        self.reply(200, {"decision": decision})

    def model_gateway(self):
        if not self.check_lease():
            return
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


if ROLE == "compiler":
    build_jobs.init()
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
