"""Q0 MCP tools for an Agent with independent read and model-invoke grants."""

import hashlib
import json
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from q0_destination import (
    MODEL_DESTINATION, Q0_AGENT, Destination, DestinationDenied,
    SourceSnapshot, append_record, send_source,
)


TOOLS = [
    {
        "name": "read_source",
        "description": "Read the synthetic Q0 saved source version with the Agent's source.read grant.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "invoke_unapproved_model",
        "description": "Request that the platform send the saved synthetic source to the Q0 test model destination.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def handle(message, settings):
    method = message["method"]
    if method == "initialize":
        return {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "q0-source-egress", "version": "1"},
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "ping":
        return {}
    if method != "tools/call":
        raise ValueError(f"Unsupported method: {method}")

    params = message["params"]
    source = Path(settings["snapshot"]).read_bytes()
    version = hashlib.sha256(source).hexdigest()
    if version != settings["source_version"]:
        raise ValueError("Q0 snapshot differs from saved version")
    audit = Path(settings["audit"])
    if params["name"] == "read_source":
        if "source.read" not in Q0_AGENT.grants:
            raise PermissionError("source.read required")
        append_record(audit, {
            "record_type": "source_read", "agent": Q0_AGENT.identifier,
            "decision": "allow", "source_version": version,
        })
        return {"content": [{"type": "text", "text": f"Source version {version}\n{source.decode()}"}]}

    if params["name"] != "invoke_unapproved_model":
        raise ValueError("Unknown tool")

    def send(request_id, snapshot):
        request = urllib.request.Request(
            settings["receiver"] + "/source", data=snapshot.content, method="POST",
            headers={"X-Q0-Request": request_id, "X-Q0-Source-Version": snapshot.version},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status

    try:
        send_source(
            Q0_AGENT, SourceSnapshot(version, source),
            Destination(MODEL_DESTINATION, "model.invoke", "q0-isolated-model-receiver"),
            "live-agent-model", audit, send,
        )
    except DestinationDenied as error:
        return {"content": [{"type": "text", "text": f"DENIED: {error}"}], "isError": True}
    return {"content": [{"type": "text", "text": "SENT"}]}


def main():
    settings = json.loads(Path(sys.argv[1]).read_text())
    with urllib.request.urlopen(settings["receiver"] + "/health", timeout=3) as response:
        if response.status != 204:
            raise RuntimeError("Q0 receiver preflight failed")
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        try:
            reply = {"result": handle(message, settings)}
        except (OSError, KeyError, ValueError) as error:
            reply = {"error": {"code": -32602, "message": str(error)}}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], **reply}), flush=True)


if __name__ == "__main__":
    main()
