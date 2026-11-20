"""Run the pinned Multica/DSH agent and measure its container boundaries."""

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path("/run/q0")
BRIDGE = "@multica-ai/dsh-runtime"
RUNTIME_DEPS = {"@deepseek-ai/dsh-" + name for name in ("agent", "llm", "mcp-client", "session", "subprocess")}
ALLOWED_ENV = {"PATH", "HOSTNAME", "NODE_VERSION", "YARN_VERSION", "TERM", "HOME", "LC_CTYPE",
               "WORK_LEASE", "LEASE_EXPIRY", "PROBE_ADDRESSES"}
HTTP = build_opener(ProxyHandler({}))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def prepare_profile():
    for name in ("home", "sessions", "scratch"):
        (ROOT / name).mkdir()
    profile = ROOT / "dsh-home/profiles/multica"
    bundle = profile / "node_modules" / BRIDGE
    bundle.mkdir(parents=True)
    for name in ("package.json", "cordis.patch.yml"):
        shutil.copyfile("/bridge/" + name, bundle / name)
    shutil.copytree("/bridge/dist", bundle / "dist")
    found = set()
    for manifest in Path("/dsh/packages").glob("*/*/package.json"):
        name = json.loads(manifest.read_text()).get("name")
        if name in RUNTIME_DEPS:
            link = profile / "node_modules" / name
            link.parent.mkdir(exist_ok=True)
            link.symlink_to(manifest.parent)
            found.add(name)
    require(found == RUNTIME_DEPS, "Pinned DSH runtime dependencies missing")
    (profile / "package.json").write_text(json.dumps({
        "name": "q0-pilot-profile", "private": True,
        "dependencies": {BRIDGE: json.loads((bundle / "package.json").read_text())["version"]},
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", BRIDGE]}},
    }))
    disabled = Path("/pilot/profile.patch.yml").read_text().split("- id: tool-bash", 1)[1]
    (profile / "cordis.patch.yml").write_text("""- id: llm-pi-ai
  config:
    providers:
      pilot-gateway:
        apiKeyEnv: Q0_LOCAL_MODEL_TOKEN
        api: openai-completions
        baseURL: http://gateway:8080/v1
        models:
          - id: pilot-approved
            contextWindow: 32768
            maxTokens: 512
            input: [text]
- id: tool-bash""" + disabled)


def boundary():
    mounts = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        spec, fs = line.split(" - ")
        mounts[spec.split()[4]] = fs.split()[0]
    require(all(mounts.get(str(path)) == "tmpfs" for path in (ROOT, Path("/tmp"))),
            "Runtime scratch is not tmpfs")
    require(bool(os.statvfs("/").f_flag & os.ST_RDONLY), "Runtime root filesystem is writable")
    require(os.getuid() == 1000, "Unexpected runtime identity")
    require(not Path("/var/run/docker.sock").exists(), "Runtime can see the container engine socket")
    unexpected = sorted(set(os.environ) - ALLOWED_ENV)
    require(not unexpected, f"Unexpected runtime environment names: {unexpected}")
    lease_seconds = int(os.environ["LEASE_EXPIRY"]) - int(time.time())
    require(0 < lease_seconds <= 900, "Runtime lease must be short-lived")

    reachable = {}
    for name in ("workspace", "gateway"):
        with HTTP.open(f"http://{name}:8080/health", timeout=10) as response:
            reachable[name] = response.status
    direct = {}
    for name, (address, port) in json.loads(os.environ["PROBE_ADDRESSES"]).items():
        try:
            with socket.create_connection((address, port), timeout=1):
                direct[name] = "reachable"
        except OSError as error:
            direct[name] = "timed_out" if isinstance(error, TimeoutError) else errno.errorcode.get(error.errno, "blocked")
    require("reachable" not in direct.values(), "Runtime reached a private service directly")
    status = dict(line.split(":\t", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":\t" in line)
    require(int(status["CapEff"], 16) == 0 and status["NoNewPrivs"] == "1", "Unexpected runtime privileges")
    return {"root_read_only": True, "scratch_filesystems": {name: mounts[name] for name in ("/run/q0", "/tmp")},
            "uid": os.getuid(), "docker_socket": False, "capabilities": status["CapEff"], "no_new_privileges": True,
            "runtime_env_names": sorted(os.environ), "lease_remaining_seconds": lease_seconds,
            "allowed_service_health": reachable, "direct_tcp": direct}


def assert_destination_denied(lease, source):
    request = Request("http://gateway:8080/v1/chat/completions", headers={
        "Authorization": "Bearer " + lease, "Content-Type": "application/json",
    }, data=json.dumps({
        "model": "pilot-unapproved", "source_class": "public",
        "messages": [{"role": "user", "content": source.decode()}],
    }).encode())
    try:
        with HTTP.open(request, timeout=10) as response:
            raise RuntimeError(f"Unapproved destination returned {response.status}")
    except HTTPError as error:
        require(error.code == 403 and json.load(error)["error"]["code"] == "source_destination_not_authorized",
                "Expected model destination policy to block before sending")
    print(json.dumps({"denied_model": {"http_status": 403, "source_version": hashlib.sha256(source).hexdigest()}}), flush=True)


def main():
    lease = os.environ["WORK_LEASE"]
    print(json.dumps({"boundary": boundary()}), flush=True)
    prepare_profile()
    lease_file = ROOT / "lease"
    fd = os.open(lease_file, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(lease)
    prompt = ROOT / "prompt"
    prompt.write_text(
        "Call the tool mcp__workspace__save_source with an empty JSON object {} now. "
        "It saves the controlled quote.py for this task. After the tool confirms the saved version, reply SAVED."
    )
    run = subprocess.run([
        "/usr/local/bin/pilot-probe", "-dsh", "/pilot/dsh", "-model", "pilot-gateway/pilot-approved",
        "-cwd", str(ROOT / "scratch"), "-python", "/usr/bin/python3", "-tool", "/pilot/save_tool.py",
        "-saved", str(lease_file), "-session-root", str(ROOT / "sessions"), "-timeout", "6m",
        "-prompt-file", str(prompt),
    ], env={
        "PATH": os.environ["PATH"], "HOME": str(ROOT / "home"), "TMPDIR": "/tmp",
        "DSH_HOME": str(ROOT / "dsh-home"), "Q0_LOCAL_MODEL_TOKEN": lease,
        # Load the pinned native extension from the image so scratch remains noexec.
        "NARB_DISABLE_NATIVE_CACHE": "1",
    }, text=True, capture_output=True, timeout=395)
    if not run.stdout.strip():
        raise RuntimeError(f"Multica/DSH produced no result (status {run.returncode}): " + run.stderr[-3000:].replace(lease, "[redacted]"))
    result = json.loads(run.stdout.replace(lease, "[redacted]"))
    print(json.dumps({"agent": result}), flush=True)
    require(run.returncode == 0 and result["result"]["Status"] == "completed", "Multica/DSH agent did not complete")
    assert_destination_denied(lease, (ROOT / "saved-source").read_bytes())


if __name__ == "__main__":
    main()
