"""OpenAI-compatible Q0 gateway that authorizes restricted work before contacting a model."""

from dataclasses import dataclass
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from q0_destination import Destination, DestinationDenied, Q0_AGENT, SourceSnapshot, send_source

APPROVED_ALIAS = "q0-approved"
UNAPPROVED_ALIAS = "q0-unapproved"
APPROVED_MODEL = "q0-approved-local-model"
UNAPPROVED_MODEL = "q0-unapproved-model"
POLICY_VERSION = "q0-model-gateway-v1"
API_PATH = "/v1/chat/completions"


@dataclass(frozen=True)
class ModelRoute:
    identifier: str
    upstream_url: str
    upstream_model: str

    def __post_init__(self):
        uri = urlsplit(self.upstream_url)
        if uri.scheme != "http" or uri.hostname != "127.0.0.1" or uri.path != API_PATH:
            raise ValueError("The Q0 gateway uses explicit loopback model routes")
        if uri.username or uri.password or uri.query or uri.fragment:
            raise ValueError("Unexpected credentials or URL components in Q0 model route")

    @property
    def destination(self):
        return Destination(self.identifier, "model.invoke", self.upstream_url)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post_model(route, body, request_id):
    payload = {**body, "model": route.upstream_model}
    request = Request(
        route.upstream_url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Q0-Request": request_id},
    )
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=120) as response:
        return response.status, response.headers.get("Content-Type", "application/json"), response.read()


def gateway(routes, key, snapshot: SourceSnapshot, audit: Path, *, admit_negative_fixture=False):
    """Bind the listener credential to one restricted work item, independent of request JSON."""
    admitted = {APPROVED_ALIAS}
    if admit_negative_fixture:
        admitted.add(UNAPPROVED_ALIAS)
    allowed = frozenset(
        ("model.invoke", routes[alias].identifier, routes[alias].upstream_url)
        for alias in admitted
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self, status, body, content_type="application/json"):
            if isinstance(body, dict):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def error(self, status, code):
            self.reply(status, {"error": {"message": code, "code": code}})

        def do_POST(self):
            if self.path != API_PATH:
                return self.error(HTTPStatus.NOT_FOUND, "route_not_found")
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + key):
                return self.error(HTTPStatus.UNAUTHORIZED, "invalid_work_identity")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    return self.error(HTTPStatus.BAD_REQUEST, "invalid_body_size")
                body = json.loads(self.rfile.read(size))
                route = routes[body["model"]]
                if not isinstance(body["messages"], list):
                    return self.error(HTTPStatus.BAD_REQUEST, "invalid_messages")
            except (KeyError, TypeError, ValueError):
                return self.error(HTTPStatus.BAD_REQUEST, "invalid_model_request")

            request_id = uuid.uuid4().hex
            try:
                status, content_type, response = send_source(
                    Q0_AGENT, snapshot, route.destination, request_id, audit,
                    lambda request_id, _: post_model(route, body, request_id),
                    allowed_destinations=allowed,
                    policy_version=POLICY_VERSION if not admit_negative_fixture else "q0-overbroad-fixture",
                )
            except DestinationDenied as error:
                return self.error(HTTPStatus.FORBIDDEN, str(error))
            except (HTTPError, URLError, TimeoutError):
                return self.error(HTTPStatus.BAD_GATEWAY, "model_receiver_unavailable")
            self.reply(status, response, content_type)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)
