"""In-process fake AWS EC2, Alibaba ECS and Multica workspace for end-to-end runs.

The fakes speak the same wire protocols as the live clouds, including request
signature verification, so the real provider adapters run unchanged. Cloud
instances are simulated state machines; when one reaches ``running`` its
first-boot user-data is parsed and the instance agent registers against the
fake workspace exactly as a real Linux instance would.
"""

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import bootstrap
from .providers import (
    FAKE_ALIYUN_SECRET_KEY, FAKE_AWS_ACCESS_KEY, FAKE_AWS_SECRET_KEY,
    aliyun_signature, aws_signed_headers,
)

READY_AFTER_DESCRIBES = 2


class CloudState:
    def __init__(self):
        self.lock = threading.Lock()
        self.instances = {}
        self.counters = {"aws": 0, "aliyun": 0}
        self.faults = {
            "aws": {"auth_failure": False, "stall": False},
            "aliyun": {"auth_failure": False, "stall": False},
        }
        self.calls = []

    def log(self, provider, action, **fields):
        self.calls.append({"provider": provider, "action": action, "at_ns": time.time_ns(), **fields})

    def new_instance_id(self, provider):
        self.counters[provider] += 1
        if provider == "aws":
            return "i-" + f"{self.counters[provider]:017x}"
        return "i-bp" + f"{self.counters[provider]:014x}"

    def snapshot(self, provider=None):
        with self.lock:
            return [dict(record) for record in self.instances.values()
                    if provider is None or record["provider"] == provider]


class FakeWorkspace:
    """Minimal Multica contract: activations, daemon register, issues, tasks."""

    def __init__(self, admin_token):
        self.admin_token = admin_token
        self.lock = threading.Lock()
        # Keyed by lease hash: a runtime that retries keeps its original
        # first-boot lease, so historical activations must remain resolvable.
        self.activations = {}
        self.runtimes = {}
        self.tasks = {}

    def _admin(self, headers):
        return hmac.compare_digest(headers.get("Authorization", ""), "Bearer " + self.admin_token)

    def handle(self, method, path, headers, body):
        if method == "POST" and path == "/api/cloud-runtimes/activations":
            if not self._admin(headers):
                return 401, {"error": "admin_credential_required"}
            payload = json.loads(body)
            activation = {
                "id": "act-" + uuid.uuid4().hex[:12], "status": "pending",
                "registered_runtime_id": None, **payload,
            }
            with self.lock:
                self.activations[payload["lease_sha256"]] = activation
            return 201, activation
        if method == "POST" and path == "/api/daemon/register":
            lease = headers.get("Authorization", "").removeprefix("Bearer ")
            lease_sha = hashlib.sha256(lease.encode()).hexdigest()
            payload = json.loads(body)
            with self.lock:
                activation = next((item for item in self.activations.values()
                                   if item["lease_sha256"] == lease_sha), None)
                if activation is None:
                    return 403, {"error": {"code": "lease_required"}}
                if time.time() >= activation["expires_at"]:
                    return 403, {"error": {"code": "lease_expired"}}
                runtime_id = "rt-" + uuid.uuid4().hex[:12]
                runtime = payload["runtimes"][0]
                record = {
                    "id": runtime_id, "name": activation["runtime_name"],
                    "type": runtime["type"], "runtime_mode": runtime["runtime_mode"],
                    "status": "online", "identity": runtime.get("instance", {}),
                    "boot_log": runtime.get("boot_log", []), "daemon_id": payload.get("daemon_id"),
                }
                self.runtimes[runtime_id] = record
                activation["status"] = "registered"
                activation["registered_runtime_id"] = runtime_id
            return 200, {"runtimes": [{"id": runtime_id, "name": record["name"],
                                       "type": record["type"], "status": "online"}]}
        if method == "GET" and path == "/api/daemon/runtimes":
            if not self._admin(headers):
                return 401, {"error": "admin_credential_required"}
            with self.lock:
                return 200, {"runtimes": list(self.runtimes.values())}
        if method == "POST" and path.startswith("/api/daemon/runtimes/") and path.endswith("/offline"):
            if not self._admin(headers):
                return 401, {"error": "admin_credential_required"}
            runtime_id = path.split("/")[-2]
            with self.lock:
                runtime = self.runtimes.get(runtime_id)
                if not runtime:
                    return 404, {"error": "unknown_runtime"}
                runtime["status"] = "offline"
            return 200, {"id": runtime_id, "status": "offline"}
        if method == "POST" and path == "/api/issues/":
            if not self._admin(headers):
                return 401, {"error": "admin_credential_required"}
            payload = json.loads(body)
            task_id = uuid.uuid4().hex
            issue_id = uuid.uuid4().hex
            with self.lock:
                runtime = self.runtimes.get(payload["runtime_id"])
                task = {"id": task_id, "issue_id": issue_id, "runtime_id": payload["runtime_id"],
                        "title": payload["title"], "status": "queued", "receipt": None}
                self.tasks[task_id] = task
            if runtime:
                threading.Thread(target=self._execute_on_instance, args=(task_id, dict(runtime)),
                                 daemon=True).start()
            return 201, {"id": issue_id, "task_id": task_id}
        if method == "GET" and path.startswith("/api/daemon/tasks/"):
            if not self._admin(headers):
                return 401, {"error": "admin_credential_required"}
            task_id = path.rsplit("/", 1)[1]
            with self.lock:
                task = self.tasks.get(task_id)
            return (200, dict(task)) if task else (404, {"error": "unknown_task"})
        return 404, {"error": "unknown_route"}

    def _execute_on_instance(self, task_id, runtime):
        # Simulates the cloud instance agent claiming, starting and completing.
        time.sleep(0.2)
        with self.lock:
            self.tasks[task_id]["status"] = "running"
        time.sleep(0.2)
        receipt = {
            "task_id": task_id, "runtime_id": runtime["id"],
            "instance": runtime["identity"], "executed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "result": {"synthetic": "cloud-smoke", "answer": 42, "runtime": runtime["name"]},
        }
        with self.lock:
            self.tasks[task_id].update(status="completed", receipt=receipt)


class Handler(BaseHTTPRequestHandler):
    server_version = "HeliosFake/1.0"

    def log_message(self, *args):
        pass

    def _send(self, status, payload, content_type="application/json"):
        if isinstance(payload, dict) or isinstance(payload, list):
            data = json.dumps(payload).encode()
        else:
            data = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        body = self._read_body()
        try:
            if self.server.kind == "workspace":
                status, payload = self.server.workspace.handle(
                    method, urllib.parse.urlsplit(self.path).path, self.headers, body.decode())
                return self._send(status, payload)
            if self.server.kind == "aws":
                return self._aws(method, body)
            return self._aliyun(method)
        except Exception as error:  # keep the fake honest: surface unexpected faults
            self._send(500, {"Code": "InternalError", "Message": str(error)})

    # ------------------------------------------------------------------ #
    # AWS wire protocol
    # ------------------------------------------------------------------ #

    def _aws_error(self, code, message, status=403):
        xml = (f"<?xml version=\"1.0\"?><Response><Errors><Error><Code>{code}</Code>"
               f"<Message>{message}</Message></Error></Errors><RequestId>{uuid.uuid4()}</RequestId></Response>")
        self._send(status, xml, "text/xml")

    def _aws_verify(self, method, body):
        parsed = urllib.parse.urlsplit(self.path)
        match = re.search(r"Credential=[^/]+/([^/]+)/([^/]+)/([^/]+)/aws4_request",
                          self.headers.get("Authorization", ""))
        if not match:
            return False
        date, region, service = match.groups()
        url = "http://" + self.headers["Host"] + parsed.path
        if method == "POST":
            produced = aws_signed_headers(
                method, url, {}, {"Content-Type": self.headers.get("Content-Type", "")}, body,
                access_key=FAKE_AWS_ACCESS_KEY, secret_key=FAKE_AWS_SECRET_KEY,
                region=region, service=service, amz_date=self.headers["X-Amz-Date"])
        else:
            query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items()}
            produced = aws_signed_headers(
                method, url, query, {}, b"", access_key=FAKE_AWS_ACCESS_KEY,
                secret_key=FAKE_AWS_SECRET_KEY, region=region, service=service,
                amz_date=self.headers["X-Amz-Date"])
        return hmac.compare_digest(produced["Authorization"], self.headers["Authorization"])

    def _aws(self, method, body):
        state = self.server.cloud
        if not self._aws_verify(method, body):
            state.log("aws", "signature_rejected")
            return self._aws_error("SignatureDoesNotMatch", "fake cloud signature check failed")
        params = dict(urllib.parse.parse_qsl(body.decode() if method == "POST"
                                             else urllib.parse.urlsplit(self.path).query))
        action = params.get("Action")
        if state.faults["aws"]["auth_failure"] and action in {"GetCallerIdentity", "DescribeSubnets",
                                                               "DescribeSecurityGroups", "RunInstances"}:
            state.log("aws", "auth_failure", attempted_action=action)
            return self._aws_error("AuthFailure", "test account is not authorized for " + action)
        handler = getattr(self, "_aws_" + action, None)
        if handler is None:
            return self._aws_error("InvalidAction", action, status=400)
        state.log("aws", action)
        return handler(state, params)

    def _aws_GetCallerIdentity(self, state, params):
        xml = ("<GetCallerIdentityResponse><GetCallerIdentityResult>"
               "<Arn>arn:aws:iam::123456789012:user/helios-test</Arn>"
               "<UserId>AIDAFakeHeliosUser</UserId><Account>123456789012</Account>"
               "</GetCallerIdentityResult></GetCallerIdentityResponse>")
        self._send(200, xml, "text/xml")

    def _aws_DescribeSubnets(self, state, params):
        subnet = params.get("SubnetId.1", "subnet-helios")
        self._send(200, f"<DescribeSubnetsResponse><subnetSet><item><subnetId>{subnet}</subnetId>"
                        f"<vpcId>vpc-helios</vpcId></item></subnetSet></DescribeSubnetsResponse>", "text/xml")

    def _aws_DescribeSecurityGroups(self, state, params):
        groups = "".join(f"<item><groupId>{value}</groupId></item>"
                         for key, value in sorted(params.items()) if key.startswith("GroupId."))
        self._send(200, f"<DescribeSecurityGroupsResponse><securityGroupInfo>{groups}"
                        f"</securityGroupInfo></DescribeSecurityGroupsResponse>", "text/xml")

    def _aws_RunInstances(self, state, params):
        record = self._launch(state, "aws", params, token=params.get("ClientToken"),
                              user_data=base64.b64decode(params.get("UserData", "")).decode(),
                              image_id=params["ImageId"], subnet_id=params.get("SubnetId", ""),
                              profile=params.get("IamInstanceProfile.Name", ""),
                              extra={"instance_type": params.get("InstanceType", "")})
        self._send(200, "<RunInstancesResponse>" + self._aws_instance_xml(record)
                        + "</RunInstancesResponse>", "text/xml")

    def _aws_DescribeInstances(self, state, params):
        if "InstanceId.1" in params:
            records = [self._advance(state, params["InstanceId.1"])]
        else:
            with state.lock:
                records = [dict(item) for item in state.instances.values()
                           if item["provider"] == "aws" and item["state"] != "terminated"]
        items = "".join(self._aws_instance_xml(record) for record in records if record)
        self._send(200, f"<DescribeInstancesResponse><reservationSet><item>{items}</item>"
                        f"</reservationSet></DescribeInstancesResponse>", "text/xml")

    def _aws_DescribeInstanceAttribute(self, state, params):
        record = self._find(state, params["InstanceId"])
        if not record:
            return self._aws_error("InvalidInstanceID.NotFound", params["InstanceId"], status=400)
        encoded = base64.b64encode(record["user_data"].encode()).decode()
        self._send(200, f"<DescribeInstanceAttributeResponse><instanceAttribute>"
                        f"<userData><value>{encoded}</value></userData></instanceAttribute>"
                        f"</DescribeInstanceAttributeResponse>", "text/xml")

    def _aws_GetConsoleOutput(self, state, params):
        record = self._find(state, params["InstanceId"])
        if not record:
            return self._aws_error("InvalidInstanceID.NotFound", params["InstanceId"], status=400)
        output = base64.b64encode("\n".join(record["boot_log"]).encode()).decode()
        self._send(200, f"<GetConsoleOutputResponse><instanceId>{record['instance_id']}</instanceId>"
                        f"<output>{output}</output></GetConsoleOutputResponse>", "text/xml")

    def _aws_TerminateInstances(self, state, params):
        with state.lock:
            record = state.instances.get(params["InstanceId.1"])
            if record:
                record["state"] = "terminated"
        self._send(200, "<TerminateInstancesResponse><instancesSet><item>"
                        f"<instanceId>{params['InstanceId.1']}</instanceId>"
                        "<currentState><name>terminated</name></currentState>"
                        "</item></instancesSet></TerminateInstancesResponse>", "text/xml")

    def _aws_instance_xml(self, record):
        tags = "".join(f"<item><key>{k}</key><value>{v}</value></item>" for k, v in record["tags"].items())
        ip = record.get("private_ip", "")
        return (f"<instancesSet><item><instanceId>{record['instance_id']}</instanceId>"
                f"<imageId>{record['image_id']}</imageId><instanceType>{record['instance_type']}</instanceType>"
                f"<privateIpAddress>{ip}</privateIpAddress><subnetId>{record['subnet_id']}</subnetId>"
                f"<clientToken>{record['client_token']}</clientToken>"
                f"<iamInstanceProfile><name>{record['profile']}</name>"
                f"<arn>arn:aws:iam::123456789012:instance-profile/{record['profile']}</arn></iamInstanceProfile>"
                f"<instanceState><name>{record['state']}</name></instanceState>"
                f"<tagSet>{tags}</tagSet></item></instancesSet>")

    # ------------------------------------------------------------------ #
    # Alibaba Cloud ECS wire protocol
    # ------------------------------------------------------------------ #

    def _aliyun_error(self, code, message, status=403):
        self._send(status, {"Code": code, "Message": message, "RequestId": uuid.uuid4().hex})

    def _aliyun_verify(self):
        query = {key: values[0] for key, values in
                 urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
        signature = query.pop("Signature", None)
        if signature is None:
            return False, query
        return hmac.compare_digest(
            aliyun_signature(query, FAKE_ALIYUN_SECRET_KEY), signature), query

    def _aliyun(self, method):
        ok, params = self._aliyun_verify()
        state = self.server.cloud
        if not ok:
            state.log("aliyun", "signature_rejected")
            return self._aliyun_error("SignatureDoesNotMatch", "fake cloud signature check failed")
        action = params.get("Action")
        if state.faults["aliyun"]["auth_failure"] and action in {"DescribeRegions", "DescribeVSwitches",
                                                                 "DescribeSecurityGroups", "RunInstances"}:
            state.log("aliyun", "auth_failure", attempted_action=action)
            return self._aliyun_error("Forbidden.RAM", "test RAM user lacks " + action)
        handler = getattr(self, "_aliyun_" + action, None)
        if handler is None:
            return self._aliyun_error("InvalidAction", action, status=400)
        state.log("aliyun", action)
        return handler(state, params)

    def _aliyun_DescribeRegions(self, state, params):
        self._send(200, {"Regions": {"Region": [{"RegionId": "cn-hangzhou"}]}})

    def _aliyun_DescribeVSwitches(self, state, params):
        self._send(200, {"VSwitches": {"VSwitch": [{"VSwitchId": params.get("VSwitchId.1", "vsw-helios")}]}})

    def _aliyun_DescribeSecurityGroups(self, state, params):
        self._send(200, {"SecurityGroups": {"SecurityGroup": [
            {"SecurityGroupId": value} for key, value in sorted(params.items())
            if key.startswith("SecurityGroupId.")]}})

    def _aliyun_RunInstances(self, state, params):
        record = self._launch(
            state, "aliyun", params, token=params.get("ClientToken"),
            user_data=base64.b64decode(params.get("UserData", "")).decode(),
            image_id=params["ImageId"], subnet_id=params.get("VSwitchId", ""),
            profile=params.get("RamRoleName", ""), extra={"instance_type": params.get("InstanceType", "")})
        self._send(200, {"InstanceIdSets": {"InstanceIdSet": [record["instance_id"]]}})

    def _aliyun_DescribeInstances(self, state, params):
        if "InstanceIds" in params:
            target = json.loads(params["InstanceIds"])[0]
            records = [self._advance(state, target)]
        else:
            with state.lock:
                records = [dict(item) for item in state.instances.values()
                           if item["provider"] == "aliyun" and item["state"] != "terminated"]
        self._send(200, {"Instances": {"Instance": [
            self._aliyun_instance(record) for record in records if record]}})

    def _aliyun_DescribeUserData(self, state, params):
        record = self._find(state, params["InstanceId"])
        if not record:
            return self._aliyun_error("InvalidInstanceId.NotFound", params["InstanceId"], status=404)
        self._send(200, {"InstanceId": record["instance_id"],
                         "UserData": base64.b64encode(record["user_data"].encode()).decode()})

    def _aliyun_DeleteInstance(self, state, params):
        with state.lock:
            record = state.instances.get(params["InstanceId"])
            if record:
                record["state"] = "terminated"
        self._send(200, {"RequestId": uuid.uuid4().hex})

    def _aliyun_instance(self, record):
        status = {"pending": "Pending", "running": "Running", "terminated": "Stopped"}[record["state"]]
        return {
            "InstanceId": record["instance_id"], "Status": status, "ImageId": record["image_id"],
            "ClientToken": record["client_token"], "RamRoleName": record["profile"],
            "VpcAttributes": {"PrivateIpAddress": {"IpAddress": [record.get("private_ip", "")]}},
            "PublicIpAddress": {"IpAddress": [record.get("public_ip", "")]},
            "Tags": {"Tag": [{"TagKey": k, "TagValue": v} for k, v in record["tags"].items()]},
        }

    # ------------------------------------------------------------------ #
    # Shared instance simulation
    # ------------------------------------------------------------------ #

    def _tags_from_params(self, params):
        tags = {}
        index = 1
        while f"Tag.1.{index}.Key" in params:
            tags[params[f"Tag.1.{index}.Key"]] = params[f"Tag.1.{index}.Value"]
            index += 1
        return tags

    def _launch(self, state, provider, params, *, token, user_data, image_id, subnet_id, profile, extra):
        with state.lock:
            existing = next((record for record in state.instances.values()
                             if record["provider"] == provider and record["client_token"] == token), None)
            if existing:
                state.log(provider, "duplicate_launch_suppressed", client_token=token)
                return dict(existing)
            record = {
                "provider": provider, "instance_id": state.new_instance_id(provider),
                "state": "pending", "client_token": token, "user_data": user_data,
                "image_id": image_id, "subnet_id": subnet_id, "profile": profile,
                "security_group_ids": [], "tags": self._tags_from_params(params),
                "private_ip": "", "public_ip": "", "boot_log": ["cloud-init: executing user-data"],
                "describe_count": 0, "region": self.server.region, **extra,
            }
            state.instances[record["instance_id"]] = record
            state.log(provider, "instance_launched", instance_id=record["instance_id"], client_token=token)
            return dict(record)

    def _find(self, state, instance_id):
        with state.lock:
            record = state.instances.get(instance_id)
            return dict(record) if record else None

    def _advance(self, state, instance_id):
        with state.lock:
            record = state.instances.get(instance_id)
            if record is None:
                return None
            if record["state"] == "pending":
                record["describe_count"] += 1
                if not state.faults[record["provider"]]["stall"] and record["describe_count"] >= READY_AFTER_DESCRIBES:
                    record["state"] = "running"
                    record["private_ip"] = f"10.{len(record['instance_id']) % 200}.0.{record['describe_count'] + 10}"
                    record["public_ip"] = "203.0.113." + str(record["describe_count"] + 1)
                    # Only Helios runtime instances carry the first-boot config;
                    # ordinary cloud instances simply reach running without it.
                    if bootstrap.CONFIG_PATH in record["user_data"]:
                        snapshot = dict(record)
                        threading.Thread(target=self._simulate_boot, args=(snapshot,), daemon=True).start()
            return dict(record)

    def _simulate_boot(self, record):
        config = bootstrap.parse_runtime_config(record["user_data"])
        lease = bootstrap.parse_lease(record["user_data"])["lease"]
        time.sleep(0.1)
        boot_lines = record["boot_log"] + [
            "helios-agent: loaded short-lived task lease",
            "helios-agent: registered through " + config["register_path"],
            "helios-agent: runtime online",
        ]
        with self.server.cloud.lock:
            stored = self.server.cloud.instances.get(record["instance_id"])
            if stored is not None:
                stored["boot_log"] = list(boot_lines)
        register = {
            "daemon_id": config["daemon_id"], "device_name": f"cloud-{record['provider']}",
            "cli_version": "0.0.0-helios",
            "runtimes": [{
                "name": config["runtime_name"], "type": config["runtime_type"],
                "runtime_mode": "cloud", "version": "0.1.0", "status": "online",
                "instance": {
                    "provider": record["provider"], "region": record["region"],
                    "instance_id": record["instance_id"], "image_id": record["image_id"],
                    "private_ip": record["private_ip"], "instance_profile": record["profile"],
                },
                "boot_log": boot_lines,
            }],
        }
        request = urllib.request.Request(
            config["workspace_url"] + "/api/daemon/register",
            data=json.dumps(register).encode(),
            headers={"Authorization": "Bearer " + lease, "Content-Type": "application/json"},
            method="POST")
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("fake instance registration failed")


class FakeCloud:
    def __init__(self, workspace_admin_token):
        self.cloud = CloudState()
        self.workspace = FakeWorkspace(workspace_admin_token)
        self.servers = {}
        self._start("aws", "us-east-1")
        self._start("aliyun", "cn-hangzhou")
        self._start_workspace()

    def _start(self, kind, region):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.kind = kind
        server.cloud = self.cloud
        server.region = region
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers[kind] = server

    def _start_workspace(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.kind = "workspace"
        server.workspace = self.workspace
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers["workspace"] = server

    def url(self, kind):
        return f"http://127.0.0.1:{self.servers[kind].server_address[1]}"

    def endpoints(self):
        return {"aws": self.url("aws"), "aliyun": self.url("aliyun"), "workspace": self.url("workspace")}

    def set_auth_failure(self, provider, enabled):
        with self.cloud.lock:
            self.cloud.faults[provider]["auth_failure"] = enabled

    def set_stall(self, provider, enabled):
        with self.cloud.lock:
            self.cloud.faults[provider]["stall"] = enabled

    def active_instances(self, provider=None):
        return [record for record in self.cloud.snapshot(provider) if record["state"] != "terminated"]

    def stop(self):
        for server in self.servers.values():
            server.shutdown()
            server.server_close()
