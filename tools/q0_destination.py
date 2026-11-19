"""Q0 egress authorization for a server-owned saved source snapshot."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

POLICY_VERSION = "q0-restricted-source-v1"
BUILD_DESTINATION = "q0-compile"
MODEL_DESTINATION = "q0-unapproved-model"
APPROVED_BUILD_REPOSITORY = "fREEDtICE/software-delivery-workbench"
APPROVED_BUILD_WORKFLOW = "q0-remote-compile.yml"
APPROVED_BUILD_REF = "main"
BUILD_BINDING = f"{APPROVED_BUILD_REPOSITORY}:{APPROVED_BUILD_WORKFLOW}@{APPROVED_BUILD_REF}"
ALLOWED_SOURCE_DESTINATIONS = frozenset({("build.submit", BUILD_DESTINATION, BUILD_BINDING)})


@dataclass(frozen=True)
class AgentIdentity:
    identifier: str
    grants: frozenset[str]


@dataclass(frozen=True)
class SourceSnapshot:
    version: str
    content: bytes


@dataclass(frozen=True)
class Destination:
    identifier: str
    action: str
    binding: str


Q0_AGENT = AgentIdentity("q0-fixture-agent", frozenset({"source.read", "model.invoke", "build.submit"}))


class DestinationDenied(PermissionError):
    pass


def append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def send_source(agent, snapshot, destination, request_id, audit_path, transport, *,
                allowed_destinations=ALLOWED_SOURCE_DESTINATIONS, policy_version=POLICY_VERSION):
    """Resolve authorization before calling a trusted adapter bound to the recipient."""
    required = {"source.read", destination.action}
    identity_authorized = required <= agent.grants
    version_matches = hashlib.sha256(snapshot.content).hexdigest() == snapshot.version
    key = (destination.action, destination.identifier, destination.binding)
    if not identity_authorized:
        reason = "required_grant_missing"
    elif not version_matches:
        reason = "source_version_mismatch"
    elif key not in allowed_destinations:
        reason = "source_destination_not_authorized"
    else:
        reason = "source_destination_authorized"
    allowed = reason == "source_destination_authorized"
    append_record(audit_path, {
        "record_type": "source_egress_decision",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "policy_version": policy_version,
        "request_id": request_id,
        "agent": agent.identifier,
        "agent_grants": sorted(agent.grants),
        "action": destination.action,
        "destination": destination.identifier,
        "binding": destination.binding,
        "source_class": "restricted-source",
        "source_version": snapshot.version,
        "required_grants_present": identity_authorized,
        "decision": "allow" if allowed else "deny",
        "reason": reason,
    })
    if not allowed:
        raise DestinationDenied(reason)
    return transport(request_id, snapshot)
