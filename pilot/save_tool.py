"""Expose the pilot workspace's source write as one MCP tool."""

import json
from pathlib import Path
import sys
from urllib.request import ProxyHandler, Request, build_opener


def respond(message, lease, source):
    method = message["method"]
    if method == "initialize":
        return {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "q0-workspace", "version": "1"},
        }
    if method == "tools/list":
        return {"tools": [{
            "name": "save_source",
            "description": "Save the controlled quote.py snapshot for this pilot task and return its version.",
            "inputSchema": {
                "type": "object", "properties": {}, "additionalProperties": False,
            },
        }]}
    if method == "ping":
        return {}
    if method != "tools/call" or message["params"]["name"] != "save_source":
        raise ValueError("Unsupported workspace tool")
    if message["params"].get("arguments", {}):
        raise ValueError("The controlled snapshot takes no arguments")
    request = Request("http://workspace:8080/save", data=source, headers={"Authorization": "Bearer " + lease})
    with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
        if response.status != 201:
            raise ValueError("Workspace did not confirm the save")
        version = json.load(response)["saved_version"]
    Path("/run/q0/saved-source").write_bytes(source)
    return {"content": [{"type": "text", "text": f"Saved quote.py at SHA-256 {version}"}]}


if __name__ == "__main__":
    lease = Path(sys.argv[1]).read_text()
    source = Path("/pilot/quote.py").read_bytes()
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        try:
            result = respond(message, lease, source)
        except (KeyError, OSError, ValueError):
            result = {"isError": True, "content": [{"type": "text", "text": "Workspace request rejected"}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
