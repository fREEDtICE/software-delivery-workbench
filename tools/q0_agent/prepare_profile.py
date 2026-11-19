"""Assemble the macOS Q0 profile around two already built, pinned checkouts."""

import argparse
import json
from pathlib import Path
import shlex
import shutil

BRIDGE = "@multica-ai/dsh-runtime"
RUNTIME_DEPS = {
    "@deepseek-ai/dsh-agent",
    "@deepseek-ai/dsh-llm",
    "@deepseek-ai/dsh-mcp-client",
    "@deepseek-ai/dsh-session",
    "@deepseek-ai/dsh-subprocess",
}


def prepare(dsh, bridge, node, lab):
    if not (dsh / "apps/cli/lib/bin.js").is_file() or not (bridge / "dist/index.js").is_file():
        raise ValueError("Build both pinned checkouts first.")
    lab.mkdir()
    for name in ("home", "session-store", "workspace-store", "tmp", "scratch"):
        (lab / name).mkdir()
    profile = lab / "dsh-home" / "profiles" / "multica"
    bundle = profile / "node_modules" / BRIDGE
    bundle.mkdir(parents=True)
    for name in ("package.json", "cordis.patch.yml"):
        shutil.copyfile(bridge / name, bundle / name)
    shutil.copytree(bridge / "dist", bundle / "dist")

    found = set()
    for manifest in (dsh / "packages").glob("*/*/package.json"):
        package = json.loads(manifest.read_text()).get("name")
        if package in RUNTIME_DEPS:
            link = profile / "node_modules" / package
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(manifest.parent)
            found.add(package)
    if found != RUNTIME_DEPS:
        raise ValueError(f"Pinned DSH runtime dependencies missing: {RUNTIME_DEPS - found}")

    manifest = {
        "name": "q0-source-profile",
        "private": True,
        "dependencies": {BRIDGE: json.loads((bundle / "package.json").read_text())["version"]},
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", BRIDGE]}},
    }
    (profile / "package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(Path(__file__).with_name("profile.patch.yml"), profile / "cordis.patch.yml")

    policy = "(version 1)\n(allow default)\n(deny network-outbound)\n"
    policy += '(allow network-outbound (remote ip "localhost:11434"))\n(deny file-write*)\n'
    policy += '(allow file-write* (subpath ' + json.dumps(str(lab)) + ') (literal "/dev/null"))\n'
    (lab / "runtime.sb").write_text(policy)
    quote = shlex.quote
    command = [
        "/usr/bin/env", "-i", f"HOME={lab / 'home'}", f"TMPDIR={lab / 'tmp'}",
        f"PATH={node.parent}:/usr/bin:/bin:/opt/homebrew/bin",
        f"DSH_HOME={lab / 'dsh-home'}", "Q0_LOCAL_MODEL_TOKEN=local-test",
    ]
    tail = ["/usr/bin/sandbox-exec", "-f", str(lab / "runtime.sb"), str(node), str(dsh / "apps/cli/lib/bin.js")]
    launcher = lab / "dsh"
    launcher.write_text(
        "#!/bin/sh\nQ0_SESSION_DEFAULT=" + quote(str(lab / "session-store")) + "\nexec "
        + " ".join(map(quote, command))
        + ' MULTICA_DSH_SESSION_ROOT="${MULTICA_DSH_SESSION_ROOT:-$Q0_SESSION_DEFAULT}" '
        + " ".join(map(quote, tail))
        + ' "$@"\n'
    )
    launcher.chmod(0o700)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dsh", "bridge", "node", "lab"):
        parser.add_argument(name, type=lambda arg: Path(arg).absolute())
    args = parser.parse_args()
    prepare(args.dsh, args.bridge, args.node, args.lab)


if __name__ == "__main__":
    main()
