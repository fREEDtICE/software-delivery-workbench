"""Render the Linux first-boot user-data for a cloud Agent runtime.

The script is a normal cloud-init compatible shell user-data: AWS EC2 and
Alibaba Cloud ECS both execute it on first boot with no interactive login. It
writes a runtime configuration and a short-lived task lease, then starts the
runtime agent which self-registers through the formal Multica daemon entry
(the same ``/api/daemon/register`` contract exercised by the Q0 pilot).

Long-lived cloud management credentials never appear here: the instance only
references its IAM instance profile / RAM role and receives a task lease that
the policy plane can revoke and that expires quickly.
"""

import json
import re

CONFIG_PATH = "/etc/helios/runtime.json"
LEASE_PATH = "/etc/helios/lease.json"
AGENT_BIN = "/opt/helios/bin/helios-runtime-agent"
MAX_LEASE_SECONDS = 900

_HEREDOC = re.compile(
    r"cat > (?P<path>/etc/helios/[a-z.]+) <<'(?P<marker>[A-Z_]+)'\n(?P<body>.*?)\n(?P=marker)\n",
    re.DOTALL,
)


def render_user_data(*, workspace_url, provider, region, runtime_name, daemon_id,
                     instance_profile, gateway_url, lease, lease_expires_at,
                     register_path="/api/daemon/register"):
    """Return the user-data shell script.

    ``lease`` is the per-task short-lived secret; ``lease_expires_at`` is an
    absolute epoch second. Callers enforce the maximum lifetime before rendering.
    """
    runtime_config = {
        "workspace_url": workspace_url,
        "register_path": register_path,
        "daemon_id": daemon_id,
        "runtime_name": runtime_name,
        "runtime_type": f"cloud-{provider}",
        "runtime_mode": "cloud",
        "provider": provider,
        "region": region,
        "instance_profile": instance_profile,
        "model_gateway_url": gateway_url,
        "lease_path": LEASE_PATH,
    }
    lease_config = {
        "lease": lease,
        "expires_at": lease_expires_at,
        "capabilities": ["source.save", "model.invoke", "build.read"],
    }
    return f"""#!/bin/bash
set -euo pipefail
# Rendered by the Helios cloud runtime control plane. Executed once by the
# cloud first-boot mechanism; no SSH login or manual package registration.
mkdir -p /etc/helios /opt/helios/bin
cat > {CONFIG_PATH} <<'HELIOS_RUNTIME_JSON'
{json.dumps(runtime_config, indent=2, sort_keys=True)}
HELIOS_RUNTIME_JSON
cat > {LEASE_PATH} <<'HELIOS_LEASE_JSON'
{json.dumps(lease_config, indent=2, sort_keys=True)}
HELIOS_LEASE_JSON
chmod 600 {LEASE_PATH}
exec {AGENT_BIN} --config {CONFIG_PATH}
"""


def embedded_files(user_data):
    """Parse the heredoc payloads back out (used by the fake cloud and checks)."""
    return {match.group("path"): match.group("body") for match in _HEREDOC.finditer(user_data)}


def parse_runtime_config(user_data):
    files = embedded_files(user_data)
    return json.loads(files[CONFIG_PATH])


def parse_lease(user_data):
    files = embedded_files(user_data)
    return json.loads(files[LEASE_PATH])
