"""Single-work lease state for the pilot's private policy service."""

from pathlib import Path
import os
import threading
import time

REVOKED = Path("/policy-state/revoked-at")
LOCK = threading.Lock()


def decide(lease_matches):
    with LOCK:
        if not lease_matches:
            return "lease_required"
        if REVOKED.exists():
            return "lease_revoked"
        if time.time() >= int(os.environ["LEASE_EXPIRY"]):
            return "lease_expired"
        return "allow"


def revoke():
    with LOCK:
        if REVOKED.exists():
            return int(REVOKED.read_text())
        now = time.time_ns()
        marker = REVOKED.with_suffix(".pending")
        with marker.open("w") as stream:
            stream.write(str(now))
            stream.flush()
            os.fsync(stream.fileno())
        marker.replace(REVOKED)
        # The acknowledgement is issued after the marker and its directory are durable.
        parent = os.open(REVOKED.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return now
