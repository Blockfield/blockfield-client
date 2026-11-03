#!/usr/bin/env python3
"""Content-addressed copies of the built pack for download mirrors.

python -I scripts/mirror-files.py stage dist/web dist/mirror
python -I scripts/mirror-files.py github dist/mirror Blockfield/blockfield-pack-mirror files

packwiz-installer resolves {hash} in mirrors.json to the hash the pack declares for a file, so R2 and
the GitHub mirror release keep every file under that name, shared by all releases.
"""

import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
from pathlib import Path

# GitHub allows about 500 content-creating requests an hour and 1000 assets per release.
UPLOADS_PER_RUN = 400
ASSETS_KEPT = 900


def mirror_files(web):
    """{declared hash: file in web} for everything packwiz-installer downloads."""
    files = {}
    for entry in tomllib.loads((web / "index.toml").read_text())["files"]:
        if entry.get("metafile"):
            download = tomllib.loads((web / entry["file"]).read_text())["download"]
            target = urllib.parse.unquote(download["url"].split("/files/", 1)[1])
            files[download["hash"]] = web / "files" / target
        else:
            files[entry["hash"]] = web / entry["file"]
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise SystemExit(f"Missing pack files: {missing}")
    return files


def stage(web, out):
    out.mkdir(parents=True)
    for name, path in mirror_files(web).items():
        try:
            os.link(path, out / name)
        except OSError:
            shutil.copyfile(path, out / name)
    print(f"Staged {len(list(out.iterdir()))} files in {out}")


def plan(existing, staged):
    """(names to upload, asset ids to delete) for one run; existing maps name to (id, created_at)."""
    upload = sorted(set(staged) - set(existing))[:UPLOADS_PER_RUN]
    stale = sorted(
        (created, asset)
        for name, (asset, created) in existing.items()
        if name not in staged
    )
    excess = len(existing) + len(upload) - ASSETS_KEPT
    return upload, [asset for _, asset in stale[: max(0, excess)]]


def github(out, repo, tag):
    release = subprocess.check_output(
        ["gh", "api", f"repos/{repo}/releases/tags/{tag}", "--jq", ".id"], text=True
    ).strip()
    listing = subprocess.check_output(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/releases/{release}/assets?per_page=100",
            "--jq",
            ".[] | [.name, .id, .created_at] | @json",
        ],
        text=True,
    )
    existing = {}
    for line in listing.splitlines():
        name, asset, created = json.loads(line)
        existing[name] = (asset, created)
    # GitHub rejects empty assets; those files stay on R2 and the pack host.
    staged = {path.name for path in out.iterdir() if path.stat().st_size > 0}
    upload, delete = plan(existing, staged)
    for asset in delete:
        subprocess.run(
            ["gh", "api", "-X", "DELETE", f"repos/{repo}/releases/assets/{asset}"],
            check=True,
        )
    for start in range(0, len(upload), 50):
        names = upload[start : start + 50]
        subprocess.run(
            ["gh", "release", "upload", tag, "--repo", repo, *(out / n for n in names)],
            check=True,
        )
    left = len(set(staged) - set(existing)) - len(upload)
    print(
        f"Uploaded {len(upload)}, deleted {len(delete)}, {left} left for the next release"
    )


if __name__ == "__main__":
    command, *args = sys.argv[1:]
    if command == "stage":
        stage(Path(args[0]), Path(args[1]))
    elif command == "github":
        github(Path(args[0]), args[1], args[2])
    else:
        raise SystemExit(f"Unknown command {command}")
