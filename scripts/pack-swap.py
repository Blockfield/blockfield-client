"""Switch the pack served by blockfield-distribution by repointing a symlink: the pack is data on the host.

The distribution container mounts BLOCKFIELD_PACK_ROOT read-only and serves <root>/current, a relative symlink
to releases/<tag>. An identical copy lives in blockfield-client and blockfield-server (sync with diff).

    pack-swap.py <vX.Y.Z>   install the release if needed, repoint current, check the public site, prune old
                            releases; any failure points current back to what it was
    pack-swap.py stage <vX.Y.Z>
                        install the release without touching current, so no launcher sees it
                        (the Deploy server pair switch serves it later); the job stays green
    pack-swap.py current    tag current points to (exit 1 when there is none)
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
check = __import__("check-deployed")


def _required_env(name):
    # The pack root lives on the distribution host, not in this repo.
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


ROOT = Path(_required_env("BLOCKFIELD_PACK_ROOT"))
BASE = "https://github.com/Blockfield/blockfield-releases/releases/download"
ASSET = "Blockfield-web.tar"
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
KEEP = 5


def open_asset(tag, name):
    if not TAG.fullmatch(tag):
        raise ValueError("Expected a release tag")
    request = urllib.request.Request(
        f"{BASE}/{tag}/{name}", headers={"User-Agent": "Blockfield-CI/1.0"}
    )
    return urllib.request.urlopen(request, timeout=120)


def release_sums(tag):
    with open_asset(tag, "SHA256SUMS") as response:
        lines = response.read().decode().splitlines()
    return dict(line.split("  ", 1)[::-1] for line in lines)


def web_sum(tag):
    try:
        return release_sums(tag)[ASSET]
    except KeyError:
        raise ValueError(
            f"{tag} has no {ASSET}: it predates the pack as data"
        ) from None


def release_build(tag):
    """The published build.json, checked against the release's SHA256SUMS."""
    with open_asset(tag, "build.json") as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != release_sums(tag)["build.json"]:
        raise ValueError(f"{tag}: build.json differs from SHA256SUMS")
    return json.loads(data)


def served_tag():
    try:
        name = Path(os.readlink(ROOT / "current")).name
    except OSError:
        return None
    return name if TAG.fullmatch(name) else None


def preflight(tag):
    """Fail before anything restarts when the swap to tag cannot work."""
    if not (ROOT / "releases" / tag).is_dir():
        web_sum(tag)
    if served_tag() is None or not os.access(ROOT / "releases", os.W_OK):
        raise ValueError(
            f"{ROOT}: needs releases/ writable by this user and a current link"
        )


def install(tag):
    final = ROOT / "releases" / tag
    if final.is_dir():
        return
    expected = web_sum(tag)
    tmp = final.with_name(tag + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with tempfile.TemporaryDirectory(dir=ROOT) as scratch:
            archive = Path(scratch) / ASSET
            digest = hashlib.sha256()
            with open_asset(tag, ASSET) as response, archive.open("wb") as out:
                while chunk := response.read(1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"{ASSET} checksum mismatch for {tag}")
            tmp.mkdir(parents=True)
            tmp.chmod(0o755)
            with tarfile.open(archive) as tar:
                tar.extractall(tmp, filter="data")
            # nginx runs as another user: do not depend on the runner's umask or the Python version's directory modes.
            for path in tmp.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
        if json.loads((tmp / "build.json").read_text())["build"] != tag:
            raise ValueError(f"{ASSET} of {tag} carries another build")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    tmp.rename(final)


def switch(tag):
    # ponytail: a packwiz client that read the old index.toml before this swap gets a 404 for a file the new release
    # dropped and retries on its next start; a content-addressed files/ pool shared by all releases would remove it.
    link, staging = ROOT / "current", ROOT / f".current.{os.getpid()}"
    staging.unlink(missing_ok=True)
    os.symlink(f"releases/{tag}", staging)
    os.replace(staging, link)


def stage(tag):
    # Invisible until the pair switch serves it: install + verify, but never
    # touch current, never check the public site, never prune.
    if not TAG.fullmatch(tag):
        raise ValueError("Expected a release tag")
    install(tag)
    if json.loads((ROOT / "releases" / tag / "build.json").read_text())["build"] != tag:
        raise ValueError(f"staged {tag} carries another build")


def prune(*keep):
    releases = ROOT / "releases"
    tags = sorted(
        (p.name for p in releases.iterdir() if TAG.fullmatch(p.name)),
        key=lambda t: tuple(map(int, t[1:].split("."))),
        reverse=True,
    )
    for name in tags[KEEP:]:
        if name not in keep:
            shutil.rmtree(releases / name)


def swap(tag):
    previous = served_tag()
    install(tag)
    try:
        switch(tag)
        check.main(tag)
    except (Exception, SystemExit):
        if previous:
            switch(previous)
        raise
    prune(tag, previous)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "current":
        print(served_tag() or "")
        sys.exit(served_tag() is None)
    if len(sys.argv) == 3 and sys.argv[1] == "stage":
        stage(sys.argv[2])
        print("Pack staged", sys.argv[2])
    elif len(sys.argv) == 2:
        swap(sys.argv[1])
        print("Pack switched to", sys.argv[1])
    else:
        raise SystemExit(__doc__)
