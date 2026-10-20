#!/usr/bin/env python3
"""Refresh RUNNER_VERSION and RUNNER_SHA256 in release.py from an actions/runner release.

Usage: scripts/update_runner_version.py [vX.Y.Z]   (default: latest). Review the diff, then commit.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "src/octopus/agent/runner/release.py"
KEYS = ("win-x64", "win-arm64", "osx-x64", "osx-arm64", "linux-x64", "linux-arm64")


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "latest"
    path = "releases/latest" if tag == "latest" else f"releases/tags/{tag}"
    out = subprocess.run(
        ["gh", "api", f"repos/actions/runner/{path}"], capture_output=True, text=True, check=True
    )
    release = json.loads(out.stdout)
    version = release["tag_name"].lstrip("v")
    sums = {}
    for key in KEYS:
        m = re.search(rf"BEGIN SHA {key} -->([0-9a-f]{{64}})<!--", release["body"])
        if not m:
            print(f"could not find checksum in release notes: {key}", file=sys.stderr)
            return 1
        sums[key] = m.group(1)
    text = TARGET.read_text()
    table = "RUNNER_SHA256 = {\n" + "".join(f'    "{k}": "{v}",\n' for k, v in sums.items()) + "}"
    text = re.sub(r'RUNNER_VERSION = "[^"]*"', f'RUNNER_VERSION = "{version}"', text)
    text = re.sub(r"RUNNER_SHA256 = \{.*?\n\}", table, text, flags=re.DOTALL)
    TARGET.write_text(text)
    print(f"pinned actions/runner {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
