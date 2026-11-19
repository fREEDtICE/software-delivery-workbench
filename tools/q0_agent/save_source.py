"""Q0's sole write tool. Validate and save a synthetic code snapshot via MCP."""

import base64
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from q0_remote_compile import source_bytes


def respond(message, destination):
    method = message["method"]
    if "id" not in message:
        return None
    if method == "initialize":
        return {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "q0-workspace", "version": "1"},
        }
    if method == "tools/list":
        return {"tools": [{
            "name": "save_source",
            "description": "Persist the complete Python source for quote.py and return its saved version.",
            "inputSchema": {
                "type": "object",
                "properties": {"source": {"type": "string"}},
                "required": ["source"],
                "additionalProperties": False,
            },
        }]}
    if method == "tools/call":
        params = message["params"]
        try:
            if params["name"] != "save_source":
                raise ValueError("Unknown tool")
            data = params["arguments"]["source"].encode()
            source_bytes(base64.b64encode(data).decode())
            version = hashlib.sha256(data).hexdigest()
            if destination.exists() and destination.read_bytes() != data:
                raise ValueError("This Q0 snapshot was already saved")
            if not destination.exists():
                with destination.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            return {"content": [{"type": "text", "text": f"Saved quote.py at SHA-256 {version}"}]}
        except (OSError, AttributeError, KeyError, ValueError, SyntaxError) as error:
            return {"content": [{"type": "text", "text": str(error)}], "isError": True}
    if method == "ping":
        return {}
    raise ValueError(f"Unsupported method: {method}")


def main():
    destination = Path(sys.argv[1])
    for line in sys.stdin:
        message = json.loads(line)
        try:
            result = respond(message, destination)
        except (ValueError, KeyError) as error:
            reply = {"error": {"code": -32602, "message": str(error)}}
        else:
            if result is None:
                continue
            reply = {"result": result}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], **reply}), flush=True)


if __name__ == "__main__":
    main()
