"""Compile only the bounded arithmetic fixture supplied by the Q0 save service."""

import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from compile_fixture import main as compile_file
import sys

from verify_q0 import EXECUTE_ARTIFACT

CASES = [[3, 7], [0, 12], [2, 100]]


def source_bytes(encoded):
    source = base64.b64decode(encoded, validate=True)
    if not 0 < len(source) <= 512:
        raise ValueError("Q0 fixture source must be from 1 to 512 bytes")
    tree = ast.parse(source, mode="exec")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("Expected only the Q0 total function")
    function = tree.body[0]
    if (
        function.name != "total"
        or [arg.arg for arg in function.args.args] != ["quantity", "unit_price"]
        or function.decorator_list
        or len(function.body) != 1
        or not isinstance(function.body[0], ast.Return)
    ):
        raise ValueError("Expected total(quantity, unit_price) with one return")
    safe = (
        ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.BinOp,
        ast.Add, ast.Sub, ast.Mult, ast.Name, ast.Load, ast.Constant,
    )
    for node in ast.walk(tree):
        if not isinstance(node, safe):
            raise ValueError(f"Unexpected syntax in Q0 fixture: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in {"quantity", "unit_price"}:
            raise ValueError("Unexpected name in Q0 fixture")
        if isinstance(node, ast.Constant) and (type(node.value) is not int or abs(node.value) > 1000):
            raise ValueError("Expected a small integer")
    if function.args.defaults or function.args.kw_defaults or function.returns:
        raise ValueError("Unexpected argument defaults or annotation")
    return source


def main():
    request_id = os.environ["Q0_REQUEST_ID"]
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("Expected a Q0 request ID")
    source = source_bytes(os.environ["Q0_SOURCE"])
    digest = hashlib.sha256(source).hexdigest()
    if digest != os.environ["Q0_SOURCE_SHA"]:
        raise ValueError("Source differs from the saved version")
    saved = Path("quote.py")
    saved.write_bytes(source)
    print(f"Compiling request {request_id} from saved source {digest}", flush=True)
    sys.argv = [str(__file__), str(saved), "output"]
    compile_file()
    receipt = Path("output/receipt.json")
    data = json.loads(receipt.read_text())
    results = json.loads(subprocess.check_output(
        [sys.executable, "-I", "-c", EXECUTE_ARTIFACT, "output/quote.pyc", json.dumps(CASES)],
        text=True,
    ))
    data.update(
        case_inputs=CASES,
        case_results=results,
        request_id=request_id,
        runner="github-hosted",
        runner_os=os.environ["RUNNER_OS"],
        run_id=os.environ["GITHUB_RUN_ID"],
        run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
    )
    receipt.write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
