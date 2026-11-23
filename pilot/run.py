"""Deploy an isolated Linux Q0 pilot and emit evidence from each boundary."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = json.loads((ROOT / "upstreams.lock.json").read_text())
BRIDGE = "571d7c036d15f58ee88c89fd80afb0a6c7f0023c"
IMAGES = {
    "postgres": "postgres:17-bookworm", "ollama": "ollama/ollama:0.13.5",
    "backend": "q0-pilot-multica:local", "runtime": "q0-pilot-runtime:local",
    "service": "q0-pilot-services:local",
}
MODEL = "qwen3:1.7b"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def eventually(check, timeout, label):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result is not None:
                return result
        except OSError as error:
            last = str(error)
        time.sleep(1)
    raise RuntimeError(f"{label} took more than {timeout}s" + (f": {last}" if last else ""))


class Pilot:
    def __init__(self, context, evidence):
        self.cli = ["docker"] + (["--context", context] if context else [])
        self.evidence = evidence
        self.prefix = "q0-pilot-" + secrets.token_hex(4)
        self.containers = {}
        self.networks = []
        self.volumes = []
        self.mask = []
        self.record = {"run": self.prefix, "started_at": datetime.now(timezone.utc).isoformat(),
                       "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                       "upstreams": {**VERSIONS, "bridge": BRIDGE}, "checks": {}, "services": {}}

    def redact(self, value):
        for secret in self.mask:
            value = value.replace(secret, "[redacted]")
        return value

    def secret(self, value=None):
        value = value or secrets.token_hex(32)
        self.mask.append(value)
        return value

    def docker(self, *args, timeout=180, check=True, stdin=None):
        result = subprocess.run([*self.cli, *args], text=True, capture_output=True, timeout=timeout, input=stdin)
        if result.returncode and check:
            raise RuntimeError(f"docker {args[0]} failed: {self.redact(result.stderr[-1800:])}")
        return result

    def mark(self, label):
        self.record["stage"] = label
        print(label, flush=True)

    def build(self):
        args = []
        for name, commit in (("MULTICA", VERSIONS["multica"]["commit"]), ("DSH", VERSIONS["dsh"]["commit"]), ("BRIDGE", BRIDGE)):
            args += ["--build-arg", f"{name}_COMMIT={commit}"]
        with (self.evidence / "images-build.log").open("w") as log:
            for target, role in (("multica", "backend"), ("runtime", "runtime"), ("services", "service")):
                self.mark("build " + target)
                subprocess.run([*self.cli, "build", "--progress=plain", "-f", "pilot/Dockerfile",
                                "--target", target, "-t", IMAGES[role], *args, "."],
                               cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=1200, check=True)

    def network(self, role, internal=True):
        name = f"{self.prefix}-{role}"
        self.docker("network", "create", *(["--internal"] if internal else []), name)
        self.networks.append(name)
        return name

    def volume(self, role):
        name = f"{self.prefix}-{role}"
        self.docker("volume", "create", name)
        self.volumes.append(name)
        return name

    def container(self, role, image, networks, *, env=None, options=(), command=()):
        name = f"{self.prefix}-{role}"
        args = ["run", "-d", "--name", name, "--network", networks[0], "--network-alias", role, *options]
        # Supply values through the child environment so command lines carry names only.
        credentials = env or {}
        for key in credentials:
            args += ["--env", key]
        run = subprocess.run([*self.cli, *args, image, *command], env={**os.environ, **credentials},
                             text=True, capture_output=True, timeout=240)
        self.containers[role] = name
        require(run.returncode == 0, f"start {role}: {self.redact(run.stderr[-1800:])}")
        for network in networks[1:]:
            self.docker("network", "connect", "--alias", role, network, name)
        return name

    def inspect(self, role, form):
        return self.docker("inspect", "--format", form, self.containers[role]).stdout.strip()

    def request(self, role, path, body=None, token=None, workspace=None, *, health=False, raw=False):
        require(role in {"backend", "compiler", "policy"}, "Unknown operator API")
        parameters = {"url": f"http://{role}:8080{path}", "body": body, "token": token, "workspace": workspace}
        response = json.loads(self.docker("exec", "-i", self.containers["operator"], "python", "/app/operator_request.py",
                                          stdin=json.dumps(parameters)).stdout)
        if raw:
            return response
        status = response["status"]
        if health:
            return status if status == 200 else None
        require(200 <= status < 300, f"{path} returned HTTP {status}: {response.get('error', '')}")
        return response["body"]

    def io(self, role, *args, **kwargs):
        return self.docker("exec", self.containers[role], *args, **kwargs).stdout.strip()

    def events(self, role):
        log = self.docker("logs", self.containers[role], check=False)
        return [json.loads(line) for line in log.stdout.splitlines() if line.strip().startswith("{")]

    def api_setup(self, code):
        email = self.prefix + "@example.invalid"
        self.request("backend", "/auth/send-code", {"email": email})
        token = self.secret(self.request("backend", "/auth/verify-code", {"email": email, "code": code})["token"])
        workspace = self.request("backend", "/api/workspaces/", {"name": "Q0 Pilot", "slug": self.prefix}, token)["id"]

        def api(path, body=None):
            return self.request("backend", path, body, token, workspace)

        result = api("/api/daemon/register", {
            "workspace_id": workspace, "daemon_id": str(uuid.uuid4()), "device_name": "Q0 Linux",
            "cli_version": "0.0.0-q0", "runtimes": [
                {"name": "DSH", "type": "dsh", "runtime_mode": "local", "version": "0.2.2-rc.3", "status": "online"},
            ],
        })
        runtime_id = result["runtimes"][0]["id"]
        agent = api("/api/agents/", {"name": "Q0 Pilot Agent", "runtime_id": runtime_id})["id"]
        issue = api("/api/issues/", {
            "title": "Fix the synthetic line subtotal", "description": "Save and build quote.py.",
            "assignee_type": "agent", "assignee_id": agent,
        })["id"]
        self.record["backend"] = {"ready": True, "workspace": workspace, "runtime": runtime_id, "issue": issue}
        claim = eventually(lambda: api(f"/api/daemon/runtimes/{runtime_id}/tasks/claim", {}).get("task"),
                           35, "Multica issue task claim")
        task = claim["id"]
        require(claim.get("issue_id") == issue, "Claimed task belongs to a different issue")
        route = f"/api/daemon/tasks/{task}"
        api(route + "/start", {})
        require(api(route + "/status")["status"] == "running", "Multica task failed to start")
        self.record["backend"].update(task=task, running=True)
        return api, route, task

    def describe(self, role):
        mounts = json.loads(self.inspect(role, "{{json .Mounts}}"))
        return {
            "container_id": self.inspect(role, "{{.Id}}"), "image": self.inspect(role, "{{.Image}}"),
            "networks": sorted(json.loads(self.inspect(role, "{{json .NetworkSettings.Networks}}"))),
            "mounts": [{"type": m["Type"], "destination": m["Destination"]} for m in mounts],
            "published_ports": json.loads(self.inspect(role, "{{json .HostConfig.PortBindings}}")) or {},
        }

    def wait_runtime(self, role, ready_key, timeout):
        def check():
            output = self.docker("logs", self.containers[role])
            messages = [json.loads(line) for line in self.redact(output.stdout).splitlines()]
            result = {key: value for message in messages for key, value in message.items()}
            if result.get(ready_key):
                return result
            if self.inspect(role, "{{.State.Status}}") != "running":
                raise RuntimeError(f"{role} exited before {ready_key}: {self.redact(output.stderr[-1800:])}")
            return None

        return eventually(check, timeout, ready_key)

    def checkpoint(self, intent=None):
        path = "/state/build-request.json"
        if intent is not None:
            self.docker("exec", "-i", self.containers["operator"], "python", "-c", (
                "import os,sys; f=open(sys.argv[1], 'x'); f.write(sys.stdin.read()); "
                "f.flush(); os.fsync(f.fileno()); f.close(); "
                "d=os.open('/state', os.O_RDONLY | os.O_DIRECTORY); os.fsync(d); os.close(d)"
            ), path, stdin=json.dumps(intent))
        return json.loads(self.io("operator", "cat", path))

    def recover_build(self, work, version, build_key):
        intent = self.checkpoint({"request_id": secrets.token_hex(16), "work": work, "source_version": version})
        request_id = intent["request_id"]
        recovery = self.record["recovery"] = {"durable_intent": intent}
        self.mark("accept build and drop the HTTP receipt")
        lost = self.request("compiler", "/builds", {**intent, "drop_receipt": True}, build_key, raw=True)
        recovery["first_submit"] = lost
        require(lost == {"status": 0, "error": "connection_closed_without_response"}, "Original submit received a response")

        def build_waiting():
            return next((e for e in self.events("compiler") if e["event"] == "build_waiting_for_release"
                         and e["request_id"] == request_id), None)

        waiting = eventually(build_waiting, 30, "accepted build preparing source")
        recovery["build_running_before_reclaim"] = waiting
        require(not any(e["event"] == "compile_and_direct_store" for e in self.events("compiler")),
                "Build completed before Runtime reclamation")
        runtime = self.containers["runtime"]
        container_id = self.record["services"]["runtime"]["container_id"]
        self.mark("reclaim the running Runtime and restart the operator")
        require(self.inspect("runtime", "{{.State.Status}}") == "running", "Runtime exited before reclamation")
        self.docker("kill", runtime)
        exit_status = int(self.inspect("runtime", "{{.State.ExitCode}}"))
        self.docker("rm", "-v", runtime)
        require(self.docker("inspect", container_id, check=False).returncode != 0, "Original Runtime still exists")
        require(exit_status == 137, f"Unexpected reclaimed Runtime exit status: {exit_status}")
        self.record["runtime_exit_status"] = exit_status
        recovery["runtime_reclaimed"] = {"container_id": container_id, "exit_status": exit_status, "removed": True}
        operator_started = self.inspect("operator", "{{.State.StartedAt}}")
        self.docker("restart", "--timeout", "0", self.containers["operator"])
        operator_restarted = self.inspect("operator", "{{.State.StartedAt}}")
        require(operator_restarted != operator_started, "Operator process was not restarted")
        recovery["operator_restart"] = {"started_before": operator_started, "started_after": operator_restarted}

        recovered = self.checkpoint()
        require(recovered == intent and set(recovered) == {"request_id", "work", "source_version"},
                "Operator did not recover the original intent without a build receipt")
        recovery["intent_after_restart"] = recovered
        path = f"/builds/{recovered['request_id']}"
        original = self.request("compiler", path, token=build_key)
        recovery["lookup_after_restart"] = original
        require(original["build_id"] == waiting["build_id"] and original["work"] == work
                and original["source_version"] == version and original["status"] == "running" and not original["released"],
                "Lookup did not return the running original build")
        replay = self.request("compiler", "/builds", recovered, build_key)
        require(replay == original, "Replay while running changed the build")
        recovery["duplicate_while_running"] = replay
        return path

    def verify_revocation(self, work, version, policy_admin):
        self.mark("revoke the unexpired lease and test the first new requests")
        approved_before = self.events("model-receiver")
        objects_before = self.events("objects")
        ack = self.request("policy", "/revoke", {}, policy_admin)
        self.record["revocation"] = {"ack": ack}
        probe = json.loads(self.io("replacement", "python3", "/pilot/runtime.py", "--probe-revoked"))
        self.record["revocation"].update(probe)
        approved_after, objects_after = self.events("model-receiver"), self.events("objects")
        self.record["revocation"]["receiver_deltas"] = {
            "approved_model": len(approved_after) - len(approved_before),
            "objects": len(objects_after) - len(objects_before),
        }
        require(ack["work"] == work and probe["source_version"] == version, "Revocation tested a different work or source")
        require(set(probe["requests"]) == {"gateway", "workspace"}, "A lease entry point was not tested")
        for result in probe["requests"].values():
            require(result["status"] == 403 and result["reason"] == "lease_revoked"
                    and result["lease_remaining_ms"] > 0 and result["request_started_at_ns"] >= ack["revoked_at_ns"],
                    "An unexpired lease was not rejected on its first request after revocation")
            result["delay_since_revoke_ms"] = round((result["request_started_at_ns"] - ack["revoked_at_ns"]) / 1e6, 1)
        require(approved_before == approved_after and objects_before == objects_after,
                "A revoked request reached a downstream receiver")
        policy = self.events("policy")
        require(sum(e["event"] == "lease_revoked" for e in policy) == 1
                and sum(e.get("decision") == "lease_revoked" for e in policy) == 2,
                "Policy did not record both denied uses of the revoked lease")

    def complete_recovered_build(self, path, build_key):
        self.mark("finish the original build and verify duplicate requests return the same result")
        self.request("compiler", path + "/release", {}, build_key)

        def finished_build():
            state = self.request("compiler", path, token=build_key)
            require(state["status"] != "failed", "Original build failed")
            return state if state["status"] == "completed" else None

        job = eventually(finished_build, 35, "recovered build completion")
        recovery = self.record["recovery"]
        replay = self.request("compiler", "/builds", self.checkpoint(), build_key)
        require(replay == job and job["build_id"] == recovery["lookup_after_restart"]["build_id"],
                "Completed build differs from the recovered original")
        recovery["duplicate_after_completion"] = replay
        events = self.events("compiler")
        expected = {"build_accepted": 1, "acceptance_response_dropped": 1, "source_received": 1,
                    "compile_started": 1, "compile_and_direct_store": 1, "duplicate_submission": 2}
        counts = {event: sum(e["event"] == event for e in events) for event in expected}
        recovery["compiler_event_counts"] = counts
        require(counts == expected and all(e["build_id"] == job["build_id"] for e in events),
                "Receipts or actual compiler executions were duplicated")
        return job["receipt"]

    def run(self):
        self.mark("start private pilot services")
        edge, storage, model, control, policy_net = (self.network(role) for role in ("edge", "storage", "model", "control", "policy"))
        prep = self.network("model-prep", internal=False)
        for image in IMAGES.values():
            self.record.setdefault("images", {})[image] = json.loads(
                self.docker("image", "inspect", "--format", "{{json .Id}}", image).stdout,
            )
        self.record["engine"] = self.docker("version", "--format", "{{.Server.Os}}/{{.Server.Arch}} {{.Server.Version}}").stdout.strip()
        pg, workspace_key, compiler_key, build_key = (self.secret() for _ in range(4))
        self.container("db", IMAGES["postgres"], [control], env={"POSTGRES_USER": "pilot",
                       "POSTGRES_PASSWORD": pg, "POSTGRES_DB": "pilot"},
                       options=["--mount", f"type=volume,src={self.volume('db')},dst=/var/lib/postgresql/data"])
        self.container("ollama", IMAGES["ollama"], [prep, model],
                       options=["--mount", f"type=volume,src={self.volume('models')},dst=/root/.ollama"])
        readiness = lambda: self.io("db", "pg_isready", "-h", "127.0.0.1", "-U", "pilot", check=False) or None
        eventually(lambda: ready if (ready := readiness()) and "accepting connections" in ready else None, 120, "PostgreSQL")
        code = self.secret(str(100000 + secrets.randbelow(900000)))
        self.container("backend", IMAGES["backend"], [control], env={
            "DATABASE_URL": f"postgres://pilot:{pg}@db:5432/pilot?sslmode=disable",
            "JWT_SECRET": self.secret(), "APP_ENV": "development", "PORT": "8080",
            "MULTICA_DEV_VERIFICATION_CODE": code, "DO_NOT_TRACK": "1",
        }, command=["sh", "-ec", "/app/migrate up && exec /app/multica-server"])
        self.container("objects", IMAGES["service"], [storage], env={
            "WORKSPACE_OBJECT_KEY": workspace_key, "COMPILER_OBJECT_KEY": compiler_key,
        }, options=["--mount", f"type=volume,src={self.volume('objects')},dst=/data"], command=["objects"])
        self.container("compiler", IMAGES["service"], [storage], env={
            "OBJECT_KEY": compiler_key, "BUILD_KEY": build_key,
        }, options=["--mount", f"type=volume,src={self.volume('build-state')},dst=/build-state"], command=["compiler"])
        self.container("operator", IMAGES["service"], [control, storage, policy_net],
                       options=["--entrypoint", "python", "--mount", f"type=volume,src={self.volume('operator')},dst=/state"],
                       command=["-c", "import time; time.sleep(3600)"])
        self.mark("prepare and isolate real model")
        eventually(lambda: self.io("ollama", "ollama", "list", check=False) or None, 45, "Ollama")
        self.io("ollama", "ollama", "pull", MODEL, timeout=540)
        self.record["model_inventory"] = self.io("ollama", "ollama", "list")
        self.docker("network", "disconnect", prep, self.containers["ollama"])
        for role in ("model-receiver", "denied-receiver"):
            self.container(role, IMAGES["service"], [model], command=[role])
        self.mark("claim a real Multica task")
        eventually(lambda: self.request("backend", "/healthz", health=True), 180, "Multica health")
        api, route, work = self.api_setup(code)

        lease = self.secret()
        expires = str(int(time.time()) + 600)
        policy_admin, policy_check = self.secret(), self.secret()
        env = {"WORK_ID": work, "POLICY_CHECK_KEY": policy_check}
        self.container("policy", IMAGES["service"], [policy_net], env={
            **env, "POLICY_ADMIN_KEY": policy_admin, "WORK_LEASE_SHA256": hashlib.sha256(lease.encode()).hexdigest(),
            "LEASE_EXPIRY": expires,
        }, options=["--mount", f"type=volume,src={self.volume('policy')},dst=/policy-state"], command=["policy"])
        self.container("workspace", IMAGES["service"], [edge, storage, policy_net], env={**env, "OBJECT_KEY": workspace_key}, command=["workspace"])
        self.container("gateway", IMAGES["service"], [edge, model, policy_net], env=env, command=["gateway"])
        def address(role, network, port):
            networks = json.loads(self.inspect(role, "{{json .NetworkSettings.Networks}}"))
            return [networks[network]["IPAddress"], port]

        probes = {role: address(role, network, port) for role, network, port in (
            ("objects", storage, 8080), ("compiler", storage, 8080),
            ("model-receiver", model, 8080), ("denied-receiver", model, 8080), ("ollama", model, 11434),
            ("policy", policy_net, 8080),
        )}
        self.record["receiver_health"] = {}
        for role in ("objects", "compiler", "workspace", "gateway", "model-receiver", "denied-receiver", "policy"):
            self.record["receiver_health"][role] = int(eventually(
                lambda: self.io(role, "python", "-c",
                                "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status)",
                                check=False) or None, 20, role + " health",
            ))
        self.mark("execute DSH in an ephemeral Runtime")
        runtime_env = {"WORK_LEASE": lease, "LEASE_EXPIRY": expires, "PROBE_ADDRESSES": json.dumps(probes)}
        runtime_options = [
            "--user", "1000:1000", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--tmpfs", "/run/q0:rw,nosuid,nodev,noexec,uid=1000,gid=1000,mode=0700",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,mode=1777",
        ]
        self.container("runtime", IMAGES["runtime"], [edge], env=runtime_env, options=runtime_options, command=["--hold-for-reclaim"])
        self.record.update(self.wait_runtime("runtime", "ready_to_reclaim", 440))
        deploy = self.record["services"]
        deploy.update({role: self.describe(role) for role in self.containers})
        self.record["network_internal"] = {
            n: self.docker("network", "inspect", "--format", "{{.Internal}}", n).stdout.strip() == "true"
            for n in self.networks
        }
        require(deploy["runtime"]["networks"] == [edge] and not deploy["runtime"]["mounts"],
                "Runtime network or mount boundary failed")
        require(not any(service["published_ports"] for service in deploy.values()), "A private service published ports")

        saves = self.events("workspace")
        version, = [e["source_version"] for e in saves if e["event"] == "save_confirmed" and e["work"] == work]
        fixture = (ROOT / "tests/fixtures/quote_current.py").read_bytes()
        require(version == hashlib.sha256(fixture).hexdigest(), "Workspace saved the wrong fixture bytes")
        require(self.record["denied_model"]["source_version"] == version, "Denial probe used different source bytes")
        agent_run = self.record["agent"]
        require(agent_run["result"]["Status"] == "completed" and any(
            e["type"] == "tool-result" and version in e.get("output", "") for e in agent_run["events"]
        ), "Agent did not observe the saved source version")

        build_path = self.recover_build(work, version, build_key)
        self.container("replacement", IMAGES["runtime"], [edge], env=runtime_env, options=runtime_options, command=["--replacement"])
        replacement = self.wait_runtime("replacement", "fresh_scratch", 30)
        self.record["recovery"]["replacement"] = replacement
        deploy["replacement"] = self.describe("replacement")
        require(deploy["replacement"]["networks"] == [edge] and not deploy["replacement"]["mounts"],
                "Replacement Runtime network or mount boundary failed")
        self.verify_revocation(work, version, policy_admin)
        receipt = self.complete_recovered_build(build_path, build_key)
        artifact = receipt["artifact_sha256"]
        self.record["build_receipt"] = receipt
        require(receipt["source_version"] == version and receipt["case_results"] == [21, 0, 200],
                "Compiler processed the wrong source or produced wrong results")
        disk = {}
        for kind, digest in (("source", version), ("artifact", artifact)):
            disk[kind] = self.io("objects", "python", "-c",
                                 "import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())",
                                 f"/data/{kind}/{digest}")
            require(disk[kind] == digest, f"Stored {kind} bytes do not match the receipt")
        self.record["receive_storage_sha256"] = disk
        objects = self.events("objects")
        require([(e["event"], e["actor"], e["kind"], e["sha256"], e["work"]) for e in objects] == [
            ("object_written", "workspace", "source", version, work),
            ("object_read", "compiler", "source", version, work),
            ("object_written", "compiler", "artifact", artifact, work),
        ], "Unexpected object receiver operations")
        api(route + "/complete", {"output": f"Source {version}; artifact {artifact}."})
        status = api(route + "/status")["status"]
        require(status == "completed", f"Unexpected Multica final task status: {status}")
        self.record["backend"]["final_status"] = status

        decisions = self.events("gateway")
        received = self.events("model-receiver")
        denied = self.events("denied-receiver")
        allowed = [e for e in decisions if e["event"] == "model_destination" and e["decision"] == "allow" and e["work"] == work]
        rejected = [e for e in decisions if e["event"] == "model_destination" and e["decision"] == "deny" and e["work"] == work]
        require(len(rejected) == 1 and rejected[0]["destination"] == "pilot-unapproved", "Missing gateway denial")
        require(len(allowed) > 0 and len(allowed) == sum(e["event"] == "model_completed" and e["work"] == work for e in received),
                "Approved model requests did not finish")
        require(not denied, "Unapproved model receiver observed a request")
        self.record["checks"] = {"source_saved": True, "exact_source_compiled": True, "compiler_direct_store": True,
                                 "runtime_boundary": True, "denial_before_send": True,
                                 "runtime_reclaimed_during_build": True, "lost_receipt_recovered": True,
                                 "exactly_one_pilot_compile": True, "revoked_lease_denied_at_both_edges": True}
        self.record["approved_model_completed"] = len(allowed)
        self.record["unapproved_model_received"] = len(denied)
        self.record["status"] = "passed"
        self.mark("pilot verification passed")

    def finish(self):
        for role in ("objects", "workspace", "gateway", "compiler", "model-receiver", "denied-receiver", "policy"):
            if role in self.containers:
                self.record.setdefault("receiver_events", {})[role] = self.events(role)
        if self.record.get("status") == "failed" and "backend" in self.containers:
            logs = self.docker("logs", "--tail", "90", self.containers["backend"], check=False)
            lines = (logs.stdout + logs.stderr).splitlines()
            # Application logs can contain a generated verification code.
            lines = [line for line in lines if not re.search(r"code|token|authorization", line, re.I)]
            self.record["backend_diagnostics"] = lines[-20:]
        self.record["finished_at"] = datetime.now(timezone.utc).isoformat()
        (self.evidence / "deployment.json").write_text(self.redact(json.dumps(self.record, indent=2) + "\n"))
        for role in reversed(self.containers):
            self.docker("rm", "-fv", self.containers[role], timeout=30, check=False)
        for network in reversed(self.networks):
            self.docker("network", "rm", network, timeout=30, check=False)
        for volume in reversed(self.volumes):
            self.docker("volume", "rm", volume, timeout=30, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--evidence", default="evidence/q0-pilot")
    args = parser.parse_args()
    evidence = Path(args.evidence).absolute()
    evidence.mkdir(parents=True, exist_ok=True)
    pilot = Pilot(args.context, evidence)
    try:
        if args.build:
            pilot.build()
        for role in ("postgres", "ollama"):
            if pilot.docker("image", "inspect", IMAGES[role], check=False).returncode:
                pilot.mark("pull " + role)
                pilot.docker("pull", IMAGES[role], timeout=480)
        pilot.run()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        pilot.record.update(status="failed", error=pilot.redact(str(error)))
        print(pilot.record["error"], flush=True)
        raise SystemExit(1) from None
    finally:
        pilot.finish()


if __name__ == "__main__":
    main()
