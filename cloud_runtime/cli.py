"""Administrator entry point for workspace cloud runtimes.

Every command is a short-lived process: state lives in the state directory, so
restarting the controller (or running a later command) resumes durable work.
"""

import argparse
import json
import sys

from .controller import Controller
from .errors import CloudRuntimeError, ConfigurationError


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _bindings(values):
    bindings = []
    for value in values:
        agent_id, runtime_id = value.split("=", 1)
        bindings.append({"agent_id": agent_id, "runtime_id": runtime_id})
    return bindings


def build_parser():
    parser = argparse.ArgumentParser(prog="cloud-runtime", description=__doc__)
    parser.add_argument("--state-dir", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser("configure")
    configure.add_argument("--workspace-id", required=True)
    configure.add_argument("--workspace-url", required=True)
    configure.add_argument("--admin-token-env", required=True)
    configure.add_argument("--gateway-url")
    configure.add_argument("--provider", required=True, choices=["aws", "aliyun"])
    configure.add_argument("--mode", choices=["fake", "live"], default="live")
    configure.add_argument("--region", required=True)
    configure.add_argument("--endpoint", help="override for fake mode or private clouds")
    configure.add_argument("--image-id", required=True)
    configure.add_argument("--instance-type", required=True)
    configure.add_argument("--subnet-id", required=True)
    configure.add_argument("--sg", action="append", default=[], required=True)
    configure.add_argument("--profile", required=True, help="IAM instance profile or RAM role name")
    configure.add_argument("--access-key-env")
    configure.add_argument("--secret-key-env")
    configure.add_argument("--binding", action="append", default=[], help="existing agent binding agent_id=runtime_id")

    provision = sub.add_parser("provision")
    provision.add_argument("--name", required=True)
    provision.add_argument("--provider", required=True, choices=["aws", "aliyun"])
    provision.add_argument("--readiness-timeout", type=float, default=60)
    provision.add_argument("--poll-interval", type=float, default=0.1)

    retry = sub.add_parser("retry")
    retry.add_argument("--name", required=True)
    retry.add_argument("--readiness-timeout", type=float, default=60)
    retry.add_argument("--poll-interval", type=float, default=0.1)

    status = sub.add_parser("status")
    status.add_argument("--name")

    default = sub.add_parser("set-default")
    default.add_argument("--name", required=True)

    smoke = sub.add_parser("smoke-task")
    smoke.add_argument("--name", required=True)
    smoke.add_argument("--timeout", type=float, default=30)
    smoke.add_argument("--poll-interval", type=float, default=0.1)

    teardown = sub.add_parser("teardown")
    teardown.add_argument("--name", required=True)

    sub.add_parser("audit")

    dispatch = sub.add_parser("dispatch-check")
    dispatch.add_argument("--agent-id", required=True)
    group = dispatch.add_mutually_exclusive_group(required=True)
    group.add_argument("--new", action="store_true")
    group.add_argument("--existing", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    controller = Controller(args.state_dir)
    try:
        if args.command == "configure":
            provider_cfg = {
                "mode": args.mode, "region": args.region, "image_id": args.image_id,
                "instance_type": args.instance_type, "subnet_id": args.subnet_id,
                "security_group_ids": args.sg, "profile": args.profile,
                "access_key_env": args.access_key_env or f"HELIOS_{args.provider.upper()}_AK",
                "secret_key_env": args.secret_key_env or f"HELIOS_{args.provider.upper()}_SK",
            }
            if args.endpoint:
                provider_cfg["endpoint"] = args.endpoint
            try:
                config = controller.load_config()
            except ConfigurationError:
                config = {"workspace_id": args.workspace_id, "workspace_url": args.workspace_url,
                          "workspace_admin_token_env": args.admin_token_env, "providers": {}}
            config["workspace_id"] = args.workspace_id
            config["workspace_url"] = args.workspace_url
            config["workspace_admin_token_env"] = args.admin_token_env
            if args.gateway_url:
                config["model_gateway_url"] = args.gateway_url
            config.setdefault("providers", {})[args.provider] = provider_cfg
            config["bindings"] = _bindings(args.binding) or config.get("bindings", [])
            _print(controller.save_config(config))
            return 0

        if args.command == "provision":
            _print(controller.provision(
                args.name, args.provider,
                readiness_timeout=args.readiness_timeout, poll_interval=args.poll_interval))
            return 0
        if args.command == "retry":
            _print(controller.retry(
                args.name, readiness_timeout=args.readiness_timeout,
                poll_interval=args.poll_interval))
            return 0
        if args.command == "status":
            _print(controller.load_state(args.name) if args.name
                   else {"runtimes": controller.list_states()})
            return 0
        if args.command == "set-default":
            _print({"default_runtime": controller.set_default(args.name)})
            return 0
        if args.command == "smoke-task":
            _print({"task": controller.smoke_task(
                args.name, timeout=args.timeout, poll_interval=args.poll_interval)})
            return 0
        if args.command == "teardown":
            _print(controller.teardown(args.name))
            return 0
        if args.command == "audit":
            _print(controller.audit())
            return 0
        if args.command == "dispatch-check":
            _print(controller.dispatch_decision(args.agent_id, is_new=args.new))
            return 0
    except ConfigurationError as error:
        _print({"error": error.to_dict()})
        return 2
    except CloudRuntimeError as error:
        _print({"error": error.to_dict()})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
