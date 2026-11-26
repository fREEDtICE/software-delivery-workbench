"""Cloud runtime control plane for AWS EC2 and Alibaba Cloud ECS.

The package keeps the workspace controller, the cloud provider adapters and the
Linux first-boot bootstrap in one reviewable delivery. Only standard library is
used so the same entry runs on a disposable Linux CI runner without Docker.
"""

__all__ = ["errors", "providers", "bootstrap", "fakes", "controller", "cli"]
