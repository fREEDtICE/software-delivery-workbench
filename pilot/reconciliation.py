"""Read-only build reconciliation console for one isolated pilot task."""

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

STATE = Path("/state")
CASE = STATE / "build-reconciliation.json"
LOCK = threading.Lock()
ROOT = "/reconciliation"
STATES = {
    "awaiting_lookup": ("等待查询", "正在确认编译端是否已经接单。"),
    "awaiting_review": ("待核对", "编译端暂时查不到任务。请在查询恢复后重新查询，平台会继续核对原请求。"),
    "linked": ("已关联原任务", "已查回原构建，可查看任务详情。"),
    "completed": ("已完成", "已取得原构建的完成回执。"),
    "build_failed": ("构建失败", "已查回原构建，编译端报告失败。"),
}


def read_case():
    return json.loads(CASE.read_text()) if CASE.exists() else None


def persist(case):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE, delete=False) as stream:
        json.dump(case, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, CASE)
    fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def compiler_lookup(intent):
    request_id = intent["request_id"]
    # The console has a read-only credential and uses GET for all reconciliation.
    req = Request(f"http://compiler:8080/builds/{request_id}", headers={
        "Authorization": "Bearer " + os.environ["BUILD_READ_KEY"],
    })
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=5) as response:
            job = json.load(response)
            status = response.status
    except HTTPError as error:
        return error.code, None, "lookup_unavailable"
    except (OSError, ValueError):
        return 0, None, "lookup_unavailable"
    statuses = ("accepted", "running", "completed", "failed")
    keys = ("request_id", "work", "source_version")
    if (not isinstance(job, dict) or any(job.get(key) != intent[key] for key in keys)
            or job.get("status") not in statuses or not job.get("build_id")):
        return status, None, "build_identity_mismatch"
    return status, job, "original_build_found"


def reconcile(case, trigger):
    previous = case["state"]
    status, job, reason = compiler_lookup(case["intent"])
    original = case["build"]
    if job and original and original["build_id"] != job["build_id"]:
        job, reason = None, "build_identity_mismatch"
    if job:
        case["build"] = job
        case["state"] = {"completed": "completed", "failed": "build_failed"}.get(job["status"], "linked")
    elif original is None:
        case["state"] = "awaiting_review"
    case["history"].append({
        "at_ns": time.time_ns(), "trigger": trigger, "from": previous, "to": case["state"],
        "compiler_http": status, "reason": reason, "build_id": job["build_id"] if job else None,
    })
    persist(case)


def view(case, *, detail=False):
    label, hint = STATES[case["state"]]
    owner = escape(case["owner"]["label"])
    request_id = escape(case["intent"]["request_id"])
    work = escape(case["intent"]["work"])
    job = case["build"]
    build = escape(job["build_id"]) if job else "待确认"
    view_build = "" if not job or detail else '<a id="original-build" href="/reconciliation/build">查看原构建</a>'
    detail_rows = ""
    if detail and job:
        detail_rows += f"<dt>编译端状态</dt><dd>{escape(job['status'])}</dd>"
        if job.get("receipt"):
            detail_rows += f"<dt>产物摘要</dt><dd><code>{escape(job['receipt']['artifact_sha256'])}</code></dd>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>构建任务核对</title>
<style>
body {{ margin: 0; padding: 40px 20px; background: #f3f5f8; color: #162138; font: 16px/1.65 system-ui, sans-serif; }}
main {{ max-width: 720px; margin: auto; padding: 36px; background: white; border: 1px solid #e0e6ef; border-radius: 18px; }}
h1 {{ font-size: 27px; margin: 0 0 14px; }} .status {{ display: inline-block; background: #edf2ff; color: #203e77; padding: 4px 12px; border-radius: 8px; font-weight: 650; }}
p {{ color: #40516b; }} dl {{ display: grid; grid-template-columns: minmax(88px, 116px) minmax(0, 1fr); gap: 10px 14px; padding: 18px 0; border-block: 1px solid #e0e6ef; }}
dt {{ color: #40516b; }} dd {{ margin: 0; overflow-wrap: anywhere; }} form {{ display: inline-block; margin-right: 16px; }}
button {{ font: inherit; color: white; background: #254a86; padding: 9px 17px; border: none; border-radius: 8px; cursor: pointer; }} a {{ color: #254a86; }}
@media(max-width: 520px) {{ main {{ padding: 24px; }} dl {{ grid-template-columns: 1fr; gap: 4px; }} dd {{ margin-bottom: 8px; }} }}
</style></head>
<body><main>
<h1>构建任务核对</h1>
<div class="status" id="state" data-state="{case['state']}" role="status">{label}</div>
<p>{hint}</p>
<dl><dt>负责人</dt><dd id="owner">{owner}</dd>
<dt>任务</dt><dd><code>{work}</code></dd>
<dt>原请求</dt><dd><code>{request_id}</code></dd>
<dt>原构建</dt><dd id="build">{build}</dd>{detail_rows}</dl>
<form id="recheck" method="post" action="{ROOT}/recheck"><button type="submit">重新查询</button></form>
{view_build}
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, body):
        content_type = "text/html" if isinstance(body, str) else "application/json"
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.respond(200, {"service": "reconciliation"})
        case = read_case()
        if case and self.path == ROOT + "/state":
            return self.respond(200, case)
        if case and (self.path == ROOT or self.path == ROOT + "/build" and case["build"]):
            return self.respond(200, view(case, detail=self.path.endswith("/build")))
        self.respond(404, {"error": "not_found"})

    def do_POST(self):
        with LOCK:
            case = read_case()
            if self.path == ROOT + "/start" and case is None:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    return self.respond(400, {"error": "invalid_owner"})
                try:
                    owner = json.loads(self.rfile.read(length))["owner"]
                    intent = json.loads((STATE / "build-request.json").read_text())
                    if not owner["id"] or not owner["label"] or not re.fullmatch(r"[a-f0-9]{32}", intent["request_id"]):
                        raise ValueError("missing owner or invalid original request")
                except (OSError, ValueError, TypeError, KeyError):
                    return self.respond(400, {"error": "invalid_case"})
                case = {"state": "awaiting_lookup", "intent": intent, "owner": owner, "build": None, "history": []}
                persist(case)
                reconcile(case, "receipt_missing")
                return self.respond(201, case)
            if self.path == ROOT + "/recheck" and case:
                reconcile(case, "operator_recheck")
                self.send_response(303)
                self.send_header("Location", ROOT)
                self.end_headers()
                return
        self.respond(409, {"error": "case_action_unavailable"})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
