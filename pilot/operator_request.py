"""Make a JSON request from the pilot operator network."""

from http.client import RemoteDisconnected
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


def main():
    parameters = json.load(sys.stdin)
    headers = {"Content-Type": "application/json"}
    if parameters["token"]:
        headers["Authorization"] = "Bearer " + parameters["token"]
    if parameters["workspace"]:
        headers["X-Workspace-ID"] = parameters["workspace"]
    data = parameters["body"]
    if parameters.get("form"):
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        payload = urlencode(data).encode()
    else:
        payload = json.dumps(data).encode() if data is not None else None
    request = Request(parameters["url"], data=payload, headers=headers)
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
            if response.headers.get_content_type() == "text/html":
                body = response.read().decode("utf-8")
            else:
                body = json.load(response)
            print(json.dumps({"status": response.status, "body": body}))
    except HTTPError as error:
        print(json.dumps({"status": error.code, "error": error.read(500).decode(errors="replace")}))
    except RemoteDisconnected:
        print(json.dumps({"status": 0, "error": "connection_closed_without_response"}))
    except URLError:
        print(json.dumps({"status": 0, "error": "service not reachable"}))


if __name__ == "__main__":
    main()
