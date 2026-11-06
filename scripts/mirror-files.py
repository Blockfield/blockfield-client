#!/usr/bin/env python3
"""Content-addressed copies of the built pack for download mirrors.

python -I scripts/mirror-files.py stage dist/web dist/mirror
python -I scripts/mirror-files.py github dist/mirror Blockfield/blockfield-pack-mirror files

packwiz-installer resolves {hash} in mirrors.json to the hash the pack declares for a file, so R2 and
the GitHub mirror release keep every file under that name, shared by all releases.
"""

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from pathlib import Path

# GitHub allows about 500 content-creating requests an hour and 1000 assets per release.
UPLOADS_PER_RUN = 400
ASSETS_KEPT = 900
GITHUB_ASSETS_MAX = 1000

# R2 bucket blockfield-pack expires objects 30 days after upload (Cloudflare
# dashboard lifecycle, not codable in repo). Mirror objects are hash-named and
# immutable, and CI uploads with --ignore-existing, so without a refresh even the
# current release would rot away. Each tag release publishes MANIFEST_ASSET with
# its mirror keys; retain() refreshes every object the last RETAIN_RELEASES
# releases need, which exempts the rollback window from the 30-day expiry.
# Fail safe: when the release list or a manifest is unreadable, nothing is
# pruned on either mirror and the current release still publishes.
RELEASES_REPO = "Blockfield/blockfield-releases"
MANIFEST_ASSET = "mirror-manifest.txt"
RETAIN_RELEASES = 5
# One rclone process per key; bound the pool so a big rollback window cannot
# exhaust fds/processes or stall the build job (step has its own timeout).
TOUCH_WORKERS = 16


class RetentionError(Exception):
    """Retention inputs are unreadable: prune nothing until they read again."""


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


def plan(existing, staged, retain=()):
    """(names to upload, asset ids to delete) for one run; existing maps name to (id, created_at).

    retain holds mirror keys the last releases still need: they are never
    deleted, even when the mirror is over ASSETS_KEPT.
    """
    upload = sorted(set(staged) - set(existing))[:UPLOADS_PER_RUN]
    stale = sorted(
        (created, asset)
        for name, (asset, created) in existing.items()
        if name not in staged and name not in retain
    )
    excess = len(existing) + len(upload) - ASSETS_KEPT
    return upload, [asset for _, asset in stale[: max(0, excess)]]


def write_manifest(web, out):
    """Sorted mirror keys of a built web tree, published as a release asset."""
    keys = sorted(mirror_files(web))
    out.write_text("".join(f"{key}\n" for key in keys), encoding="utf-8")
    print(f"Wrote {len(keys)} mirror keys to {out}")
    return keys


def select_release_tags(names, limit, skip=()):
    """Newest-first version tags from a `gh release list` order, minus skip."""
    tags = [
        name
        for name in names
        if name not in skip and re.fullmatch(r"v\d+\.\d+\.\d+", name)
    ]
    return tags[:limit]


def recent_release_tags(limit, repo=RELEASES_REPO, skip=()):
    """Newest-first release tags of repo (raises RetentionError when gh fails)."""
    try:
        listing = subprocess.check_output(
            [
                "gh",
                "release",
                "list",
                "--repo",
                repo,
                "--limit",
                str(max(limit + len(skip) + 5, 10)),
                "--json",
                "tagName",
                "--jq",
                ".[].tagName",
            ],
            text=True,
        )
    except subprocess.SubprocessError as error:
        raise RetentionError(f"Could not list releases of {repo}: {error}")
    return select_release_tags(listing.splitlines(), limit, skip)


def manifest_keys_for(tag, repo=RELEASES_REPO):
    """Mirror keys a release needs (raises RetentionError when unreadable).

    A release with genuinely no manifest asset (pre-retention) needs
    nothing: that is set(), not an error.
    """
    try:
        assets = subprocess.check_output(
            [
                "gh",
                "release",
                "view",
                tag,
                "--repo",
                repo,
                "--json",
                "assets",
                "--jq",
                ".assets[].name",
            ],
            text=True,
        )
    except subprocess.SubprocessError as error:
        raise RetentionError(f"Could not read release {tag} of {repo}: {error}")
    if MANIFEST_ASSET not in assets.splitlines():
        print(f"No manifest for {tag} (pre-retention release?)")
        return set()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                [
                    "gh",
                    "release",
                    "download",
                    tag,
                    "--repo",
                    repo,
                    "--pattern",
                    MANIFEST_ASSET,
                    "--dir",
                    tmp,
                    "--clobber",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return {
                line.strip()
                for line in Path(tmp, MANIFEST_ASSET).read_text().splitlines()
                if line.strip()
            }
        except (subprocess.SubprocessError, OSError) as error:
            raise RetentionError(f"Could not download manifest of {tag}: {error}")


def retained_releases(staged, repo=RELEASES_REPO, skip=(), keep=RETAIN_RELEASES):
    """[(label, keys)] newest-first: the current release, then previous releases."""
    releases = [("current", set(staged))]
    if keep > 1:
        for tag in recent_release_tags(keep - 1, repo, skip):
            releases.append((tag, manifest_keys_for(tag, repo)))
    return releases


def cap_retained_for_github(releases, budget):
    """(kept keys, dropped labels): current whole, then newest whole releases in budget."""
    kept = set(releases[0][1]) if releases else set()
    dropped = []
    for label, keys in releases[1:]:
        if len(kept | keys) <= budget:
            kept |= keys
        else:
            dropped.append(label)
    return kept, dropped


def retain(
    staged,
    repo=RELEASES_REPO,
    skip=(),
    keep=RETAIN_RELEASES,
    dry_run=False,
    remote=None,
    verbose=True,
):
    """Refresh R2 objects the last keep releases need; dry_run only lists them.

    Returns the retained key set. Refresh is a server-side touch (new
    LastModified, no bytes moved), so the 30-day lifecycle restarts for exactly
    the rollback window. staged is the current release's key set (kept too).
    When old manifests are unreadable, only the current release is refreshed.
    """
    try:
        releases = retained_releases(staged, repo, skip, keep)
    except RetentionError as error:
        print(f"WARNING: {error}; refreshing current release only")
        releases = [("current", set(staged))]
    tags = [label for label, _ in releases[1:]]
    retained = set().union(*(keys for _, keys in releases))
    kept = sorted([*skip, *tags])
    print(f"Keeping {len(retained)} mirror objects for releases: {', '.join(kept)}")
    if dry_run and verbose:
        for key in sorted(retained):
            print(f"  keep {key}")
        return retained
    if dry_run:
        return retained
    rclone = shutil.which("rclone") or os.environ.get("RCLONE_BIN")
    if rclone is None:
        try:
            # -I drops the script dir from sys.path; restore it for the import.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from toolchain import tool

            rclone = str(tool("rclone"))
        except Exception as error:
            raise SystemExit(f"no rclone available: {error}")
    errors = 0

    def touch(key):
        return key, subprocess.run(
            [rclone, "touch", "--no-create", f"{remote}/{key}"],
            capture_output=True,
            text=True,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=TOUCH_WORKERS) as pool:
        futures = [pool.submit(touch, key) for key in sorted(retained)]
        for future in concurrent.futures.as_completed(futures):
            key, result = future.result()
            if result.returncode != 0:
                errors += 1
                print(f"touch failed for {key}: {result.stderr.strip()}")
    print(
        f"Refreshed {len(retained) - errors}/{len(retained)} mirror objects on {remote}"
    )
    if errors and errors == len(retained):
        raise SystemExit(
            f"All {len(retained)} touches failed; R2 retention not refreshed"
        )
    return retained


def github(out, repo, tag, current=""):
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
    # The last releases are never pruned (rollback window), but only whole
    # releases that fit under the 1000-asset cap minus this run's upload
    # headroom stay on the GitHub mirror; R2 keeps all of them regardless.
    # Retention never fails the publish: when manifests are unreadable,
    # nothing is pruned and the current release still uploads.
    staged = {path.name for path in out.iterdir() if path.stat().st_size > 0}
    try:
        releases = retained_releases(staged, skip={current} if current else set())
        retained, dropped = cap_retained_for_github(
            releases, GITHUB_ASSETS_MAX - UPLOADS_PER_RUN
        )
        if dropped:
            print(
                f"GitHub mirror over the {GITHUB_ASSETS_MAX}-asset cap:"
                f" not retaining releases: {', '.join(dropped)}"
            )
    except RetentionError as error:
        # Fail safe: the retained set is unknown, so every existing asset stays.
        print(f"WARNING: {error}; skipping prune this run")
        retained = set(existing)
    upload, delete = plan(existing, staged, retain=retained)
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
    elif command == "manifest":
        write_manifest(Path(args[0]), Path(args[1]))
    elif command == "github":
        github(Path(args[0]), args[1], args[2], args[3] if len(args) > 3 else "")
    elif command == "retain":
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--repo", default=RELEASES_REPO)
        parser.add_argument(
            "--tag", default="", help="current release tag (kept, not previous)"
        )
        parser.add_argument("--keep", type=int, default=RETAIN_RELEASES)
        parser.add_argument("--remote", default="pack:blockfield-pack")
        parser.add_argument(
            "--staged-dir", default="", help="dir of staged mirror files"
        )
        parser.add_argument("--dry-run", action="store_true")
        opts = parser.parse_args(args)
        if opts.staged_dir:
            staged = {
                path.name
                for path in Path(opts.staged_dir).iterdir()
                if path.stat().st_size > 0
            }
        elif not sys.stdin.isatty():
            staged = set(sys.stdin.read().split())
        elif opts.tag:
            try:
                staged = manifest_keys_for(opts.tag, opts.repo)
            except RetentionError as error:
                print(f"WARNING: {error}; keeping current release only")
                staged = set()
        else:
            staged = set()
        retain(
            staged,
            repo=opts.repo,
            skip={opts.tag} if opts.tag else set(),
            keep=opts.keep,
            dry_run=opts.dry_run,
            remote=opts.remote,
        )
    else:
        raise SystemExit(f"Unknown command {command}")
