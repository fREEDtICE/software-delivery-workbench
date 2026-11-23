"""Lease state for one protected work item in the isolated pilot."""

import hmac
import json
import os
from pathlib import Path
import threading
import time

STATE = Path("/policy-state")
REVOKED = STATE / "revoked-at"
REPLACEMENT = STATE / "replacement-lease.json"
LOCK = threading.Lock()


def persist(path, data):
    marker = path.with_suffix(".pending")
    with marker.open("w") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    marker.replace(path)
    # Acknowledge only after the updated directory entry is durable.
    parent = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def decide(digest, capability):
    with LOCK:
        if hmac.compare_digest(digest, os.environ["WORK_LEASE_SHA256"]):
            if REVOKED.exists():
                return "lease_revoked"
            expires = int(os.environ["LEASE_EXPIRY"])
            permitted = capability in {"source.save", "model.invoke"}
        else:
            new = json.loads(REPLACEMENT.read_text()) if REPLACEMENT.exists() else {}
            if not hmac.compare_digest(digest, new.get("lease_sha256", "")):
                return "lease_required"
            expires = new["expires_at"]
            permitted = capability == "build.read"
        if time.time() >= expires:
            return "lease_expired"
        return "allow" if permitted else "lease_scope_denied"


def revoke():
    with LOCK:
        if REVOKED.exists():
            return int(REVOKED.read_text())
        now = time.time_ns()
        persist(REVOKED, str(now))
        return now


def reissue(digest):
    with LOCK:
        if not REVOKED.exists() or hmac.compare_digest(digest, os.environ["WORK_LEASE_SHA256"]):
            raise ValueError("Initial lease must be revoked and replaced with a different credential")
        if REPLACEMENT.exists():
            existing = json.loads(REPLACEMENT.read_text())
            if not hmac.compare_digest(digest, existing["lease_sha256"]):
                raise ValueError("Replacement lease already issued for this pilot work")
            return existing["expires_at"]
        expires = int(time.time()) + 300
        persist(REPLACEMENT, json.dumps({"lease_sha256": digest, "expires_at": expires}))
        return expires
