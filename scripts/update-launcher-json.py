#!/usr/bin/env python3
"""Regenerate launcher.json: pack URL, game server, packwiz-installer hash, Temurin 21 JRE per platform (Adoptium API)."""

import hashlib
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://blockfield.pro"
PLATFORMS = {
    "windows-x86_64": ("windows", "x64"),
    "linux-x86_64": ("linux", "x64"),
    "linux-aarch64": ("linux", "aarch64"),
    "macos-x86_64": ("mac", "x64"),
    "macos-aarch64": ("mac", "aarch64"),
}


def adoptium(os_, arch):
    url = f"https://api.adoptium.net/v3/assets/latest/21/hotspot?os={os_}&architecture={arch}&image_type=jre&vendor=eclipse"
    a = json.load(
        urllib.request.urlopen(
            urllib.request.Request(
                url, headers={"User-Agent": "blockfield-modpack/1.0"}
            ),
            timeout=60,
        )
    )[0]
    p = a["binary"]["package"]
    return {
        "version": a["version"]["semver"],
        "url": p["link"],
        "sha256": p["checksum"],
        "size": p["size"],
    }


def main():
    # CI builds installers from "source" (immutable release asset), never from
    # the pack host: keep existing entries so regenerating cannot revert that.
    old = (
        json.loads((ROOT / "launcher.json").read_text())
        if (ROOT / "launcher.json").exists()
        else {}
    )

    def entry(key, filename):
        data = {
            "url": f"{BASE}/{filename}",
            "sha256": hashlib.sha256((ROOT / filename).read_bytes()).hexdigest(),
        }
        if "source" in old.get(key, {}):
            data["source"] = old[key]["source"]
        return data

    data = {
        "pack": f"{BASE}/pack.toml",
        "server": "raknet;play.blockfield.pro:25566",
        "installer": entry("installer", "packwiz-installer.jar"),
        "bootstrap": entry("bootstrap", "packwiz-installer-bootstrap.jar"),
        "java": {key: adoptium(*v) for key, v in PLATFORMS.items()},
    }
    (ROOT / "launcher.json").write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps({k: v["version"] for k, v in data["java"].items()}, indent=1))


if __name__ == "__main__":
    main()
