"""Make a JSON request from the pilot operator network."""

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def main():
    parameters = json.load(sys.stdin)
    headers = {"Content-Type": "application/json"}
    if parameters["token"]:
        headers["Authorization"] = "Bearer " + parameters["token"]
    if parameters["workspace"]:
        headers["X-Workspace-ID"] = parameters["workspace"]
    data = parameters["body"]
    request = Request(parameters["url"], data=json.dumps(data).encode() if data is not None else None, headers=headers)
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
            print(json.dumps({"status": response.status, "body": json.load(response)}))
    except HTTPError as error:
        print(json.dumps({"status": error.code, "error": error.read(500).decode(errors="replace")}))
    except URLError:
        print(json.dumps({"status": 0, "error": "service not reachable"}))


if __name__ == "__main__":
    main()
