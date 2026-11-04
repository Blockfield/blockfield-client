import hashlib
import json
import sys
import time
import tomllib
import urllib.request

BASE = "https://blockfield.pro"


def fetch(path):
    request = urllib.request.Request(
        f"{BASE}/{path}", headers={"Cache-Control": "no-cache"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def verify(tag):
    build_bytes = fetch("build.json")
    data = json.loads(build_bytes)
    if data["build"] != tag:
        raise ValueError("Distribution still reports another build")
    if not any(m["path"].startswith("mods/blockfield-client-") for m in data["mods"]):
        raise ValueError("Client mod missing")
    pack = tomllib.loads(fetch("pack.toml").decode())
    # Опубликованный pack.toml несёт версию клиента (у старых сборок её нет -- там это версия пака).
    if pack["version"] != data.get("client_version", data["version"]):
        raise ValueError("Pack version differs from build")
    index_bytes = fetch("index.toml")
    if (
        hashlib.new(pack["index"]["hash-format"], index_bytes).hexdigest()
        != pack["index"]["hash"]
    ):
        raise ValueError("Pack index checksum mismatch")
    index = tomllib.loads(index_bytes.decode())
    marker = next(
        f for f in index["files"] if f["file"] == "config/blockfield-pack-version.txt"
    )
    marker_bytes = fetch(marker["file"])
    if marker_bytes.decode().strip() != data["client"] or marker.get("preserve", False):
        raise ValueError("Invalid installed version marker")
    if hashlib.new(index["hash-format"], marker_bytes).hexdigest() != marker["hash"]:
        raise ValueError("Version marker checksum mismatch")
    sums = dict(
        line.split("  ", 1)[::-1] for line in fetch("SHA256SUMS").decode().splitlines()
    )
    # Blockfield-client.zip лежит только в GitHub-релизе; сайт на него редиректит.
    for name in ("build.json", "Blockfield-prism.zip", "Blockfield.mrpack"):
        payload = build_bytes if name == "build.json" else fetch(name)
        if hashlib.sha256(payload).hexdigest() != sums[name]:
            raise ValueError(f"{name}: download checksum mismatch")
    if json.loads(fetch("build.json"))["build"] != tag:
        raise ValueError("Distribution changed during verification")


def main(tag):
    # Ждём сайт и для серверного релиза: build.json с новым тегом значит, что образ сайта выкатился вместе с сервером.
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            verify(tag)
            print("Published distribution and downloads verified:", tag)
            return
        except Exception as error:
            print(f"Waiting for public distribution: {error}", flush=True)
        time.sleep(3)
    raise SystemExit("Public distribution not ready; server deployment blocked")


if __name__ == "__main__":
    main(sys.argv[1])
