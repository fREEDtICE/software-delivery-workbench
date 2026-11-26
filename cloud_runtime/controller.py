"""Cloud runtime lifecycle controller.

One controller owns the workspace-scoped configuration and one durable state
file per cloud runtime. All transitions are persisted atomically before the
corresponding cloud call is considered done, so a controller restart resumes
the same instance instead of creating a second billable one.
"""

import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import urllib.error
import urllib.request

from . import bootstrap
from .errors import (
    BootstrapTimeout, CloudRuntimeError, ConfigurationError, OrphanResourceError,
    ProvisionTimeout, RuntimeUnavailable,
)
from .providers import LaunchSpec, build_provider

LEASE_SECONDS = 600


def persist(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = path.with_suffix(path.suffix + ".pending")
    with marker.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(marker, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Controller:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.config_path = self.state_dir / "config.json"
        self.runtime_dir = self.state_dir / "runtimes"

    # --------------------------------------------------------------- #
    # Configuration and durable state
    # --------------------------------------------------------------- #

    def save_config(self, config):
        required = {"workspace_id", "workspace_url", "workspace_admin_token_env", "providers"}
        missing = required - set(config)
        if missing:
            raise ConfigurationError(f"config missing fields: {sorted(missing)}")
        config.setdefault("default_runtime", None)
        config.setdefault("bindings", [])
        persist(self.config_path, config)
        return config

    def load_config(self):
        if not self.config_path.is_file():
            raise ConfigurationError("workspace is not configured",
                                     actionable="先运行 configure 完成云账号与网络配置。")
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _state_path(self, name):
        return self.runtime_dir / f"{name}.json"

    def load_state(self, name):
        path = self._state_path(name)
        if not path.is_file():
            raise ConfigurationError(f"unknown cloud runtime: {name}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_state(self, state):
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        persist(self._state_path(state["name"]), state)

    def list_states(self):
        if not self.runtime_dir.is_dir():
            return []
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(self.runtime_dir.glob("*.json"))]

    def _event(self, state, event, **fields):
        state.setdefault("events", []).append(
            {"event": event, "at_ns": time.time_ns(), **fields})
        self._save_state(state)

    # --------------------------------------------------------------- #
    # Workspace API (formal Multica entry, admin token from environment)
    # --------------------------------------------------------------- #

    def _workspace(self, config, method, path, body=None):
        token = os.environ.get(config["workspace_admin_token_env"])
        if not token:
            raise ConfigurationError(
                f"missing workspace admin token in {config['workspace_admin_token_env']}")
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        request = urllib.request.Request(
            config["workspace_url"] + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise CloudRuntimeError(f"workspace {path} failed: HTTP {error.code} {error.read()[:300]!r}")

    # --------------------------------------------------------------- #
    # Provisioning
    # --------------------------------------------------------------- #

    def provision(self, name, provider_name, *, readiness_timeout=60, poll_interval=0.1):
        config = self.load_config()
        if provider_name not in config["providers"]:
            raise ConfigurationError(f"provider {provider_name} is not configured")
        provider_cfg = config["providers"][provider_name]
        state = self._initial_state(name, provider_name, provider_cfg, config["workspace_id"])
        provider = build_provider(provider_name, provider_cfg)
        client_token = hashlib.sha256(
            f"{config['workspace_id']}|{provider_name}|{name}".encode()).hexdigest()[:32]
        try:
            state["phase"] = "authorizing"
            self._event(state, "authorize_start")
            state["cloud_account"] = provider.authorize()
            self._event(state, "authorize_passed", account=state["cloud_account"])

            state["phase"] = "network_ready"
            state["network"] = provider.ensure_network(
                provider_cfg["subnet_id"], provider_cfg["security_group_ids"])
            self._event(state, "network_ready", network=state["network"])

            state["phase"] = "launching"
            if not state.get("instance_id"):
                # The short-lived lease and activation are bound to the instance
                # at first launch. Only the lease hash is persisted, never the
                # secret; a retry reuses the same instance and its first-boot lease.
                lease = secrets.token_hex(32)
                lease_expires = int(time.time()) + LEASE_SECONDS
                activation = self._workspace(config, "POST", "/api/cloud-runtimes/activations", {
                    "runtime_name": name, "provider": provider_name, "region": provider_cfg["region"],
                    "lease_sha256": hashlib.sha256(lease.encode()).hexdigest(),
                    "expires_at": lease_expires,
                    "capabilities": ["source.save", "model.invoke", "build.read"],
                })
                state["activation_id"] = activation["id"]
                state["lease_sha256"] = hashlib.sha256(lease.encode()).hexdigest()
                state["lease_expires_at"] = lease_expires

                user_data = bootstrap.render_user_data(
                    workspace_url=config["workspace_url"], provider=provider_name,
                    region=provider_cfg["region"], runtime_name=name,
                    daemon_id=f"helios-{provider_name}-{name}",
                    instance_profile=provider_cfg["profile"],
                    gateway_url=config.get("model_gateway_url", config["workspace_url"]),
                    lease=lease, lease_expires_at=lease_expires)
                state["user_data_sha256"] = hashlib.sha256(user_data.encode()).hexdigest()

                spec = LaunchSpec(
                    name=f"helios-{name}", image_id=provider_cfg["image_id"],
                    instance_type=provider_cfg["instance_type"],
                    subnet_id=provider_cfg["subnet_id"],
                    security_group_ids=provider_cfg["security_group_ids"],
                    user_data=user_data, profile=provider_cfg["profile"],
                    client_token=client_token, workspace_id=config["workspace_id"],
                    runtime_name=name)
                instance = provider.run_instances(spec)
                state["instance_id"] = instance.instance_id
                self._event(state, "instance_launched", instance_id=instance.instance_id,
                            client_token=client_token)
            else:
                # Resume: keep the original activation, lease hash and user-data
                # digest so registration and metadata verification stay consistent.
                self._event(state, "resume_existing_instance", instance_id=state["instance_id"])

            self._wait_running(provider, state, readiness_timeout, poll_interval)
            state["phase"] = "bootstrapping"
            self._event(state, "instance_running")
            runtime = self._wait_registered(config, state, readiness_timeout, poll_interval)

            instance = provider.describe_instance(state["instance_id"])
            if runtime["identity"]["instance_id"] != instance.instance_id:
                raise CloudRuntimeError(
                    f"registered identity {runtime['identity']['instance_id']} != cloud instance "
                    f"{instance.instance_id}")
            rendered = provider.get_user_data(state["instance_id"])
            if hashlib.sha256(rendered.encode()).hexdigest() != state["user_data_sha256"]:
                raise CloudRuntimeError("cloud instance metadata returned different user-data")
            state["identity"] = instance.identity_doc()
            state["workspace_runtime_id"] = runtime["id"]
            state["execution_log"] = runtime["boot_log"]
            if provider_name == "aws":
                console = provider.console_output(state["instance_id"])
                state["console_output"] = console
                if "registered" not in console:
                    raise BootstrapTimeout("EC2 console output lacks the registration marker")
            state["phase"] = "ready"
            self._event(state, "runtime_ready", identity=state["identity"])
            return state
        except CloudRuntimeError as error:
            state["phase"] = "failed"
            state["error"] = error.to_dict()
            self._event(state, "provision_failed", **error.to_dict())
            raise

    def _initial_state(self, name, provider_name, provider_cfg, workspace_id):
        path = self._state_path(name)
        if path.is_file():
            state = json.loads(path.read_text(encoding="utf-8"))
            if state["phase"] == "ready":
                return state
            state["error"] = None
            return state
        state = {
            "name": name, "provider": provider_name, "workspace_id": workspace_id,
            "region": provider_cfg["region"], "phase": "configured", "attempts": 0,
            "instance_id": None, "identity": None, "task": None, "events": [],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        state["attempts"] = 1
        self._save_state(state)
        return state

    def _wait_running(self, provider, state, timeout, interval):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            instance = provider.describe_instance(state["instance_id"])
            if instance.state == "running":
                return instance
            if instance.state == "terminated":
                raise ProvisionTimeout(
                    f"instance {state['instance_id']} terminated during provisioning",
                    actionable="检查云账号配额与实例规格后重试，幂等令牌不会重复开机。")
            time.sleep(interval)
        raise ProvisionTimeout(
            f"instance {state['instance_id']} did not become running within {timeout}s",
            actionable="运行 retry 续接同一实例；若地域/配额问题持续，先在云控制台排查后再重试。")

    def _wait_registered(self, config, state, timeout, interval):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            runtimes = self._workspace(config, "GET", "/api/daemon/runtimes")["runtimes"]
            runtime = next((item for item in runtimes if item["name"] == state["name"]
                            and item["status"] == "online"), None)
            if runtime:
                return runtime
            time.sleep(interval)
        raise BootstrapTimeout(
            f"runtime {state['name']} did not register within {timeout}s",
            actionable="查看实例系统日志与网络出口；重试会复用同一实例与启动配置。")

    def retry(self, name, **kwargs):
        state = self.load_state(name)
        if state["phase"] != "failed":
            raise ConfigurationError(f"runtime {name} is in phase {state['phase']}, only failed runs retry")
        state["attempts"] += 1
        self._save_state(state)
        return self.provision(name, state["provider"], **kwargs)

    # --------------------------------------------------------------- #
    # Reclamation
    # --------------------------------------------------------------- #

    def teardown(self, name, *, timeout=30, poll_interval=0.1):
        config = self.load_config()
        state = self.load_state(name)
        if state["phase"] == "terminated":
            return state
        provider = build_provider(state["provider"], config["providers"][state["provider"]])
        state["phase"] = "terminating"
        self._event(state, "teardown_start")
        if state.get("instance_id"):
            provider.terminate_instance(state["instance_id"])
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                instance = provider.describe_instance(state["instance_id"])
                if instance.state == "terminated":
                    break
                time.sleep(poll_interval)
            else:
                raise CloudRuntimeError(f"instance {state['instance_id']} did not terminate in time")
        if state.get("workspace_runtime_id"):
            self._workspace(config, "POST",
                            f"/api/daemon/runtimes/{state['workspace_runtime_id']}/offline", {})
        state["phase"] = "terminated"
        state["identity"] = state.get("identity")
        self._event(state, "runtime_terminated", instance_id=state.get("instance_id"))
        return state

    # --------------------------------------------------------------- #
    # Default runtime and dispatch
    # --------------------------------------------------------------- #

    def set_default(self, name):
        config = self.load_config()
        state = self.load_state(name)
        if state["phase"] != "ready":
            raise ConfigurationError(f"runtime {name} is {state['phase']}, only ready runtimes can be default")
        config["default_runtime"] = {"provider": state["provider"], "name": name}
        persist(self.config_path, config)
        return config["default_runtime"]

    def dispatch_decision(self, agent_id, *, is_new):
        config = self.load_config()
        binding = next((item for item in config.get("bindings", [])
                        if item["agent_id"] == agent_id), None)
        if binding:
            # Existing bindings keep their original runtime; the cloud default never rewrites them.
            return {"agent_id": agent_id, "runtime_id": binding["runtime_id"],
                    "location": "existing-binding", "fallback": False}
        if not is_new:
            raise ConfigurationError(f"agent {agent_id} has no binding")
        default = config.get("default_runtime")
        if not default:
            raise ConfigurationError("workspace has no default runtime",
                                     actionable="先 set-default 一个就绪的云运行时。")
        state = self.load_state(default["name"])
        if state["phase"] != "ready":
            raise RuntimeUnavailable(
                f"cloud runtime {default['name']} is {state['phase']}",
                actionable="修复云运行时后再创建 Agent；平台不会静默回退到本地执行。")
        return {"agent_id": agent_id, "runtime_id": state["workspace_runtime_id"],
                "location": "cloud", "provider": state["provider"],
                "instance_id": state["instance_id"], "fallback": False}

    # --------------------------------------------------------------- #
    # Synthetic task with durable continuation
    # --------------------------------------------------------------- #

    def smoke_task(self, name, *, timeout=30, poll_interval=0.1):
        config = self.load_config()
        state = self.load_state(name)
        if state["phase"] != "ready":
            raise RuntimeUnavailable(f"runtime {name} is {state['phase']}")
        if not state.get("task"):
            created = self._workspace(config, "POST", "/api/issues/", {
                "title": f"cloud smoke task {secrets.token_hex(4)}",
                "description": "Synthetic task proving cloud execution and result return.",
                "runtime_id": state["workspace_runtime_id"],
            })
            state["task"] = {"issue_id": created["id"], "task_id": created["task_id"],
                             "status": "queued", "receipt": None}
            self._save_state(state)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            task = self._workspace(config, "GET", "/api/daemon/tasks/" + state["task"]["task_id"])
            state["task"]["status"] = task["status"]
            if task["status"] == "completed":
                state["task"]["receipt"] = task["receipt"]
                self._save_state(state)
                if task["receipt"]["instance"]["instance_id"] != state["instance_id"]:
                    raise CloudRuntimeError("task receipt came from a different cloud instance")
                return state["task"]
            if task["status"] == "failed":
                raise CloudRuntimeError("cloud task failed")
            time.sleep(poll_interval)
        self._save_state(state)
        raise ProvisionTimeout(f"task {state['task']['task_id']} did not complete in {timeout}s")

    # --------------------------------------------------------------- #
    # Orphan / billable resource audit
    # --------------------------------------------------------------- #

    def audit(self):
        config = self.load_config()
        report = {"workspace_id": config["workspace_id"], "runtimes": [], "orphans": []}
        local = {state["name"]: state for state in self.list_states()}
        for provider_name, provider_cfg in config["providers"].items():
            provider = build_provider(provider_name, provider_cfg)
            cloud_instances = provider.list_managed(config["workspace_id"])
            for instance in cloud_instances:
                runtime_name = instance.tags.get("helios.runtime")
                state = local.get(runtime_name)
                record = {"provider": provider_name, "instance_id": instance.instance_id,
                          "state": instance.state, "runtime": runtime_name}
                if state is None or state["phase"] == "terminated":
                    report["orphans"].append(record)
                else:
                    report["runtimes"].append({**record, "local_phase": state["phase"]})
        report["orphan_count"] = len(report["orphans"])
        if report["orphan_count"]:
            raise OrphanResourceError(json.dumps(report, ensure_ascii=False))
        return report
