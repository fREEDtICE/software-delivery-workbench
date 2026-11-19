"""Compile a saved fixture version and record what the executor actually read."""

import hashlib
import json
import os
from pathlib import Path
import py_compile
import sys


def main():
    source, destination = map(Path, sys.argv[1:])
    destination.mkdir()
    artifact = destination / "quote.pyc"
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    py_compile.compile(
        str(source),
        cfile=str(artifact),
        dfile="quote.py",
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )
    receipt = {
        "source_sha256": source_digest,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "executor_pid": os.getpid(),
    }
    (destination / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")


if __name__ == "__main__":
    main()
