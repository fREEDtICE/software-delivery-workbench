"""Provider adapters for AWS EC2 and Alibaba Cloud ECS.

Both adapters speak the provider's real wire protocol (AWS Signature V4 query
API and Alibaba Cloud RPC HMAC-SHA1). In ``fake`` mode only the endpoint and
the credentials change; the signing, request encoding and response parsing stay
identical, so the local fake cloud validates the same code path used live.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.client import RemoteDisconnected
import xml.etree.ElementTree as ET

from .errors import AuthorizationError, CloudRuntimeError, ConfigurationError

# Fixed credentials exposed by the local fake cloud. They look like cloud keys
# but only authenticate against the in-process test servers, never a real cloud.
FAKE_AWS_ACCESS_KEY = "AKIAFAKEHELIOS00001"
FAKE_AWS_SECRET_KEY = "fake-aws-secret-helios-0001"
FAKE_ALIYUN_ACCESS_KEY = "LTAIfakeHelios00001"
FAKE_ALIYUN_SECRET_KEY = "fake-aliyun-secret-0001"

AWS_AUTH_CODES = {"AuthFailure", "UnauthorizedOperation", "InvalidClientTokenId",
                  "SignatureDoesNotMatch", "UnauthorizedAccess", "InsufficientInstanceCapacity"}
ALIYUN_AUTH_CODES = {"InvalidAccessKeyId.NotFound", "InvalidAccessKeyId.BlankCheckFailed",
                     "SignatureDoesNotMatch", "Forbidden.RAM", "Unauthorized",
                     "InvalidSecurityGroup.NotFound", "InvalidVSwitchId.NotFound"}


def percent_encode(value):
    """RFC 3986 unreserved set only (so '/', '+', ' ' are all encoded)."""
    return urllib.parse.quote(str(value), safe="~")


def _http(method, url, headers=None, body=None, timeout=15):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()
    except (urllib.error.URLError, RemoteDisconnected, TimeoutError, OSError) as error:
        raise CloudRuntimeError(f"cloud endpoint unreachable: {error}") from None


# --------------------------------------------------------------------------- #
# AWS Signature V4
# --------------------------------------------------------------------------- #

def _hmac(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def aws_signing_key(secret_key, date_stamp, region, service):
    key = ("AWS4" + secret_key).encode()
    for step in (date_stamp, region, service, "aws4_request"):
        key = _hmac(key, step)
    return key


def aws_canonical_request(method, url, query, headers, body):
    parsed = urllib.parse.urlsplit(url)
    signed = {k.lower().strip(): v.strip() for k, v in headers.items()}
    signed["host"] = parsed.netloc
    header_names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in header_names)
    canonical_query = "&".join(
        f"{percent_encode(k)}={percent_encode(v)}" for k, v in sorted(query.items())
    )
    payload_hash = hashlib.sha256(body or b"").hexdigest()
    canonical = "\n".join((
        method, parsed.path or "/", canonical_query, canonical_headers,
        ";".join(header_names), payload_hash,
    ))
    return canonical, payload_hash, header_names


def aws_signed_headers(method, url, query, headers, body, *, access_key, secret_key,
                       region, service, amz_date, session_token=None):
    """Return the full header set (including Authorization) for one request."""
    body = body or b""
    headers = dict(headers)
    headers["X-Amz-Date"] = amz_date
    if session_token:
        headers["X-Amz-Security-Token"] = session_token
    canonical, _, header_names = aws_canonical_request(method, url, query, headers, body)
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join((
        "AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest(),
    ))
    signature = hmac.new(
        aws_signing_key(secret_key, amz_date[:8], region, service),
        string_to_sign.encode(), hashlib.sha256,
    ).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={';'.join(header_names)}, Signature={signature}"
    )
    return headers


def aws_signature_for_test(method, url, query, headers, body, *, access_key, secret_key,
                           region, service, amz_date):
    """Signature only, used by the official AWS SigV4 documentation test vector."""
    signed = aws_signed_headers(
        method, url, query, headers, body, access_key=access_key, secret_key=secret_key,
        region=region, service=service, amz_date=amz_date,
    )
    return signed["Authorization"].rsplit("Signature=", 1)[1]


# --------------------------------------------------------------------------- #
# Alibaba Cloud RPC signature (HMAC-SHA1)
# --------------------------------------------------------------------------- #

def aliyun_signature(params, secret_key, method="GET"):
    canonical = "&".join(
        f"{percent_encode(k)}={percent_encode(v)}" for k, v in sorted(params.items())
    )
    string_to_sign = method + "&" + percent_encode("/") + "&" + percent_encode(canonical)
    digest = hmac.new((secret_key + "&").encode(), string_to_sign.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class LaunchSpec:
    name: str
    image_id: str
    instance_type: str
    subnet_id: str
    security_group_ids: list
    user_data: str
    profile: str                      # IAM instance profile / RAM role name
    client_token: str                 # stable idempotency token per workspace runtime
    workspace_id: str
    runtime_name: str

    def tags(self):
        return {
            "Name": self.name,
            "helios.workspace": self.workspace_id,
            "helios.runtime": self.runtime_name,
            "helios.managed": "true",
        }


@dataclass
class Instance:
    instance_id: str
    state: str                        # canonical: pending | running | terminated
    image_id: str
    region: str
    provider: str
    private_ip: str = ""
    public_ip: str = ""
    client_token: str = ""
    profile: str = ""
    tags: dict = field(default_factory=dict)

    def identity_doc(self):
        """Independent cloud identity an acceptor can cross-check."""
        return {
            "provider": self.provider, "region": self.region,
            "instance_id": self.instance_id, "image_id": self.image_id,
            "private_ip": self.private_ip, "public_ip": self.public_ip,
            "instance_profile": self.profile,
        }


def _form_encode(params):
    return urllib.parse.urlencode(params, doseq=True).encode()


def _xml_error(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None, text
    code = root.findtext(".//Code")
    message = root.findtext(".//Message") or text
    return code, message


# --------------------------------------------------------------------------- #
# AWS EC2 / STS adapter
# --------------------------------------------------------------------------- #

class AwsProvider:
    name = "aws"
    api_version = "2016-11-15"

    def __init__(self, cfg):
        self.cfg = cfg
        self.region = cfg["region"]
        self.ec2_endpoint = cfg.get("endpoint") or f"https://ec2.{self.region}.amazonaws.com"
        if cfg.get("mode", "live") == "fake":
            # The in-process fake serves every EC2 and STS action on one endpoint.
            self.sts_endpoint = cfg.get("sts_endpoint") or self.ec2_endpoint
        else:
            self.sts_endpoint = cfg.get("sts_endpoint") or f"https://sts.{self.region}.amazonaws.com"
        if cfg.get("mode", "live") == "fake":
            self.access_key, self.secret_key, self.session_token = (
                FAKE_AWS_ACCESS_KEY, FAKE_AWS_SECRET_KEY, None)
        else:
            self.access_key = os.environ.get(cfg["access_key_env"])
            self.secret_key = os.environ.get(cfg["secret_key_env"])
            self.session_token = os.environ.get(cfg.get("session_token_env", "")) or None
            if not self.access_key or not self.secret_key:
                raise ConfigurationError(
                    f"missing AWS credentials in {cfg['access_key_env']}/{cfg['secret_key_env']}",
                    actionable="提供具备 EC2 与 STS 权限的访问密钥，或切换到 fake 模式做本地验证。")

    def _call(self, endpoint, service, action, params, *, method="POST", version=None):
        url = endpoint + "/"
        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if method == "POST":
            params = {"Action": action, "Version": version or self.api_version, **params}
            body = _form_encode(params)
            headers = aws_signed_headers(
                method, url, {}, {"Content-Type": "application/x-www-form-urlencoded"}, body,
                access_key=self.access_key, secret_key=self.secret_key, region=self.region,
                service=service, amz_date=amz_date, session_token=self.session_token)
            status, text = _http(method, url, headers, body)
        else:
            params = {"Action": action, "Version": version or self.api_version, **params}
            headers = aws_signed_headers(
                method, url, params, {}, b"", access_key=self.access_key,
                secret_key=self.secret_key, region=self.region, service=service, amz_date=amz_date,
                session_token=self.session_token)
            status, text = _http(method, url + "?" + urllib.parse.urlencode(params), headers)
        if status >= 400:
            code, message = _xml_error(text)
            if code in AWS_AUTH_CODES:
                raise AuthorizationError(
                    f"AWS {action} rejected: {code} {message}",
                    actionable="检查 IAM 策略是否包含该动作，或更换已授权的访问密钥。")
            raise CloudRuntimeError(f"AWS {action} failed: {code or status} {message}")
        return ET.fromstring(text)

    def authorize(self):
        root = self._call(self.sts_endpoint, "sts", "GetCallerIdentity",
                          {}, method="GET", version="2011-06-15")
        return {"account": root.findtext(".//Account"),
                "arn": root.findtext(".//Arn"),
                "user_id": root.findtext(".//UserId")}

    def ensure_network(self, subnet_id, security_group_ids):
        self._call(self.ec2_endpoint, "ec2", "DescribeSubnets", {"SubnetId.1": subnet_id})
        for index, group_id in enumerate(security_group_ids, start=1):
            self._call(self.ec2_endpoint, "ec2", "DescribeSecurityGroups",
                       {f"GroupId.{index}": group_id})
        return {"subnet_id": subnet_id, "security_group_ids": list(security_group_ids)}

    def run_instances(self, spec):
        params = {
            "ImageId": spec.image_id, "InstanceType": spec.instance_type,
            "SubnetId": spec.subnet_id, "MinCount": 1, "MaxCount": 1,
            "UserData": base64.b64encode(spec.user_data.encode()).decode(),
            "IamInstanceProfile.Name": spec.profile, "ClientToken": spec.client_token,
        }
        for index, group_id in enumerate(spec.security_group_ids, start=1):
            params[f"SecurityGroupId.{index}"] = group_id
        for index, (key, value) in enumerate(spec.tags().items(), start=1):
            params[f"TagSpecification.1.ResourceType"] = "instance"
            params[f"Tag.1.{index}.Key"] = key
            params[f"Tag.1.{index}.Value"] = value
        root = self._call(self.ec2_endpoint, "ec2", "RunInstances", params)
        item = root.find(".//instancesSet/item")
        return self._parse_instance(item, spec.tags())

    def describe_instance(self, instance_id):
        root = self._call(self.ec2_endpoint, "ec2", "DescribeInstances",
                          {"InstanceId.1": instance_id})
        item = root.find(".//instancesSet/item")
        if item is None:
            raise CloudRuntimeError(f"instance {instance_id} not found")
        tags = {tag.findtext("key"): tag.findtext("value") for tag in item.findall(".//tagSet/item")}
        return self._parse_instance(item, tags)

    def get_user_data(self, instance_id):
        root = self._call(self.ec2_endpoint, "ec2", "DescribeInstanceAttribute",
                          {"InstanceId": instance_id, "Attribute": "userData"})
        encoded = root.findtext(".//userData/value") or ""
        return base64.b64decode(encoded).decode() if encoded else ""

    def console_output(self, instance_id):
        root = self._call(self.ec2_endpoint, "ec2", "GetConsoleOutput", {"InstanceId": instance_id})
        encoded = root.findtext(".//output") or ""
        return base64.b64decode(encoded).decode() if encoded else ""

    def terminate_instance(self, instance_id):
        root = self._call(self.ec2_endpoint, "ec2", "TerminateInstances",
                          {"InstanceId.1": instance_id})
        return (root.findtext(".//currentState/name") or "shutting-down")

    def list_managed(self, workspace_id, runtime_name=None):
        params = {"Filter.1.Name": "tag:helios.workspace", "Filter.1.Value.1": workspace_id}
        if runtime_name:
            params.update({"Filter.2.Name": "tag:helios.runtime",
                           "Filter.2.Value.1": runtime_name})
        root = self._call(self.ec2_endpoint, "ec2", "DescribeInstances", params)
        result = []
        for item in root.findall(".//instancesSet/item"):
            tags = {tag.findtext("key"): tag.findtext("value") for tag in item.findall(".//tagSet/item")}
            instance = self._parse_instance(item, tags)
            if instance.state != "terminated":
                result.append(instance)
        return result

    def _parse_instance(self, item, tags):
        state = (item.findtext(".//instanceState/name") or item.findtext(".//state/name") or "pending")
        profile = item.findtext(".//iamInstanceProfile/arn") or item.findtext(".//iamInstanceProfile/name") or ""
        return Instance(
            instance_id=item.findtext("instanceId"), state=state,
            image_id=item.findtext("imageId"), region=self.region, provider=self.name,
            private_ip=item.findtext("privateIpAddress") or "",
            public_ip=item.findtext("ipAddress") or "",
            client_token=item.findtext("clientToken") or "",
            profile=profile.split("/")[-1], tags=tags,
        )


# --------------------------------------------------------------------------- #
# Alibaba Cloud ECS adapter
# --------------------------------------------------------------------------- #

class AliyunProvider:
    name = "aliyun"
    version = "2014-05-26"

    def __init__(self, cfg):
        self.cfg = cfg
        self.region = cfg["region"]
        self.endpoint = cfg.get("endpoint") or f"https://ecs.{self.region}.aliyuncs.com"
        if cfg.get("mode", "live") == "fake":
            self.access_key, self.secret_key = FAKE_ALIYUN_ACCESS_KEY, FAKE_ALIYUN_SECRET_KEY
        else:
            self.access_key = os.environ.get(cfg["access_key_env"])
            self.secret_key = os.environ.get(cfg["secret_key_env"])
            if not self.access_key or not self.secret_key:
                raise ConfigurationError(
                    f"missing Alibaba Cloud credentials in {cfg['access_key_env']}/{cfg['secret_key_env']}",
                    actionable="提供具备 ECS 与 VPC 权限的 RAM 用户 AccessKey，或切换到 fake 模式做本地验证。")

    def _call(self, action, params):
        query = {
            "Action": action, "Format": "JSON", "Version": self.version,
            "AccessKeyId": self.access_key, "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0", "SignatureNonce": str(uuid.uuid4()),
            "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "RegionId": self.region, **params,
        }
        query["Signature"] = aliyun_signature(query, self.secret_key)
        url = self.endpoint + "/?" + urllib.parse.urlencode(query)
        status, text = _http("GET", url)
        if status >= 400:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                raise CloudRuntimeError(f"Aliyun ECS {action} failed: HTTP {status} {text}")
            code, message = payload.get("Code", str(status)), payload.get("Message", text)
            if code in ALIYUN_AUTH_CODES:
                raise AuthorizationError(
                    f"Aliyun {action} rejected: {code} {message}",
                    actionable="检查 RAM 策略是否包含该 ECS/VPC 动作，或更换已授权的 AccessKey。")
            raise CloudRuntimeError(f"Aliyun ECS {action} failed: {code} {message}")
        return json.loads(text)

    def authorize(self):
        self._call("DescribeRegions", {})
        return {"account": self.access_key, "region": self.region, "authorized": True}

    def ensure_network(self, subnet_id, security_group_ids):
        self._call("DescribeVSwitches", {"VSwitchId.1": subnet_id})
        for index, group_id in enumerate(security_group_ids, start=1):
            self._call("DescribeSecurityGroups", {f"SecurityGroupId.{index}": group_id})
        return {"subnet_id": subnet_id, "security_group_ids": list(security_group_ids)}

    def run_instances(self, spec):
        params = {
            "ImageId": spec.image_id, "InstanceType": spec.instance_type,
            "VSwitchId": spec.subnet_id, "Amount": 1, "InstanceName": spec.name,
            "UserData": base64.b64encode(spec.user_data.encode()).decode(),
            "RamRoleName": spec.profile, "ClientToken": spec.client_token,
        }
        for index, group_id in enumerate(spec.security_group_ids, start=1):
            params[f"SecurityGroupId.{index}"] = group_id
        for index, (key, value) in enumerate(spec.tags().items(), start=1):
            params[f"Tag.{index}.Key"] = key
            params[f"Tag.{index}.Value"] = value
        payload = self._call("RunInstances", params)
        instance_id = payload["InstanceIdSets"]["InstanceIdSet"][0]
        return self.describe_instance(instance_id)

    def describe_instance(self, instance_id):
        payload = self._call("DescribeInstances", {"InstanceIds": json.dumps([instance_id])})
        instances = payload.get("Instances", {}).get("Instance", [])
        if not instances:
            raise CloudRuntimeError(f"instance {instance_id} not found")
        item = instances[0]
        tags = {tag["TagKey"]: tag["TagValue"] for tag in item.get("Tags", {}).get("Tag", [])}
        private_ip = item.get("VpcAttributes", {}).get("PrivateIpAddress", {}).get("IpAddress", [""])[0]
        return Instance(
            instance_id=item["InstanceId"],
            state={"Pending": "pending", "Running": "running", "Stopping": "terminated",
                   "Stopped": "terminated"}.get(item.get("Status", ""), item.get("Status", "pending")),
            image_id=item.get("ImageId", ""), region=self.region, provider=self.name,
            private_ip=private_ip, public_ip=item.get("PublicIpAddress", {}).get("IpAddress", [""])[0],
            client_token=item.get("ClientToken", ""), profile=item.get("RamRoleName", ""), tags=tags,
        )

    def get_user_data(self, instance_id):
        payload = self._call("DescribeUserData", {"InstanceId": instance_id})
        return base64.b64decode(payload.get("UserData", "")).decode()

    def terminate_instance(self, instance_id):
        self._call("DeleteInstance", {"InstanceId": instance_id, "Force": "true"})
        return "terminated"

    def list_managed(self, workspace_id, runtime_name=None):
        params = {"Tag.1.Key": "helios.workspace", "Tag.1.Value": workspace_id}
        if runtime_name:
            params.update({"Tag.2.Key": "helios.runtime", "Tag.2.Value": runtime_name})
        payload = self._call("DescribeInstances", params)
        instances = []
        for item in payload.get("Instances", {}).get("Instance", []):
            if item.get("Status") not in {"Stopped", "Stopping"}:
                instances.append(self._parse_managed(item))
        return instances

    def _parse_managed(self, item):
        tags = {tag["TagKey"]: tag["TagValue"] for tag in item.get("Tags", {}).get("Tag", [])}
        private_ip = item.get("VpcAttributes", {}).get("PrivateIpAddress", {}).get("IpAddress", [""])[0]
        return Instance(
            instance_id=item["InstanceId"],
            state={"Pending": "pending", "Running": "running"}.get(item.get("Status"), "terminated"),
            image_id=item.get("ImageId", ""), region=self.region, provider=self.name,
            private_ip=private_ip, client_token=item.get("ClientToken", ""),
            profile=item.get("RamRoleName", ""), tags=tags,
        )


def build_provider(name, cfg):
    if name == "aws":
        return AwsProvider(cfg)
    if name == "aliyun":
        return AliyunProvider(cfg)
    raise ConfigurationError(f"unsupported cloud provider: {name}",
                             actionable="首批仅支持 aws 与 aliyun。")


def retry_budget(deadline_monotonic):
    """Helper for bounded readiness polling."""
    return time.monotonic() < deadline_monotonic
