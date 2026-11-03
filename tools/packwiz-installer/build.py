#!/usr/bin/env python3
"""Build and test the pinned installer fork; --check verifies the shipped JAR."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REVISION = "7420866dfc6ae1f68079a166e9804b7ec31a59ca"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="blockfield-installer-") as temporary:
        checkout = Path(temporary) / "source"
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--branch",
                "v0.5.14",
                "https://github.com/packwiz/packwiz-installer.git",
                str(checkout),
            ],
            check=True,
        )
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
        ).strip()
        if revision != REVISION:
            raise SystemExit(f"Unexpected upstream revision: {revision}")
        subprocess.run(
            ["git", "apply", str(HERE / "installer.patch")], cwd=checkout, check=True
        )
        subprocess.run(
            [
                "./gradlew",
                "resumeTest",
                "mirrorTest",
                "copyJar",
                "--no-daemon",
                "--max-workers=2",
                "--console=plain",
            ],
            cwd=checkout,
            check=True,
        )
        built = checkout / "build/dist/packwiz-installer.jar"
        subprocess.run(
            [
                sys.executable,
                str(HERE / "check_bootstrap.py"),
                "--installer",
                str(built),
            ],
            check=True,
        )
        digest = hashlib.sha256(built.read_bytes()).hexdigest()
        target = ROOT / "packwiz-installer.jar"
        if args.check:
            if digest != hashlib.sha256(target.read_bytes()).hexdigest():
                raise SystemExit(
                    "Shipped installer differs from the tested source build"
                )
        else:
            shutil.copyfile(built, target)
            launcher_path = ROOT / "launcher.json"
            launcher = json.loads(launcher_path.read_text())
            launcher["installer"]["sha256"] = digest
            launcher_path.write_text(json.dumps(launcher, indent=2) + "\n")
        print(f"packwiz-installer v0.5.14-bf6: {digest}")


if __name__ == "__main__":
    main()
