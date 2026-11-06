import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import packlib  # noqa: E402 - requires the standalone entrypoint path setup.

spec = importlib.util.spec_from_file_location(
    "client", ROOT / "scripts/build-client.py"
)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
spec = importlib.util.spec_from_file_location(
    "mirror_files", ROOT / "scripts/mirror-files.py"
)
mirror_files = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror_files)


class ClientBuildTests(unittest.TestCase):
    def test_github_mirror_uploads_in_batches_and_prunes_oldest_stale_assets(self):
        staged = {f"h{i}" for i in range(1000)}
        upload, delete = mirror_files.plan({}, staged)
        self.assertEqual(len(upload), mirror_files.UPLOADS_PER_RUN)
        self.assertEqual(delete, [])
        existing = {f"old{i}": (i, f"2026-10-{10 + i:02}") for i in range(5)}
        existing |= {f"h{i}": (100 + i, "2026-10-01") for i in range(898)}
        upload, delete = mirror_files.plan(existing, staged)
        self.assertEqual(upload, sorted(staged - set(existing))[:400])
        # 903 kept + 102 new would pass 900: the oldest stale assets go first, live ones never.
        self.assertEqual(delete, [0, 1, 2, 3, 4])

    def test_github_mirror_never_prunes_retained_release_files(self):
        staged = {f"h{i}" for i in range(1000)}
        existing = {f"old{i}": (i, f"2026-10-{10 + i:02}") for i in range(5)}
        existing |= {f"h{i}": (100 + i, "2026-10-01") for i in range(898)}
        upload, delete = mirror_files.plan(
            existing, staged, retain={"old0", "old1", "old2", "old3", "old4"}
        )
        self.assertEqual(delete, [])
        existing |= {f"extra{i}": (200 + i, "2026-09-01") for i in range(10)}
        upload, delete = mirror_files.plan(
            existing, staged, retain={"old0", "old1", "old2", "old3", "old4"}
        )
        self.assertEqual(
            sorted(delete), [200, 201, 202, 203, 204, 205, 206, 207, 208, 209]
        )

    def test_recent_release_tags_picks_last_five_version_tags(self):
        names = [
            "v2.79.2",
            "files",
            "v2.79.1",
            "latest",
            "v2.79.0",
            "v2.78.9",
            "v2.78.8",
            "v2.78.7",
            "v2.78.6",
        ]
        self.assertEqual(
            mirror_files.select_release_tags(names, 5, skip={"v2.79.2"}),
            ["v2.79.1", "v2.79.0", "v2.78.9", "v2.78.8", "v2.78.7"],
        )
        self.assertEqual(
            mirror_files.select_release_tags(names, 5),
            ["v2.79.2", "v2.79.1", "v2.79.0", "v2.78.9", "v2.78.8"],
        )

    def test_manifest_lists_sorted_mirror_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = Path(tmp) / "web"
            web.mkdir()
            out = Path(tmp) / "manifest.txt"
            with patch.dict(
                mirror_files.__dict__,
                {"mirror_files": lambda w: {"c1": w / "a", "deadbeef": w / "b"}},
            ):
                keys = mirror_files.write_manifest(web, out)
            self.assertEqual(keys, ["c1", "deadbeef"])
            self.assertEqual(out.read_text().splitlines(), ["c1", "deadbeef"])

    def test_retain_dry_run_lists_kept_releases(self):
        out = io.StringIO()
        with (
            patch.object(
                mirror_files, "recent_release_tags", return_value=["v2.79.1", "v2.79.0"]
            ),
            patch.object(
                mirror_files,
                "manifest_keys_for",
                side_effect=lambda tag, repo: {f"hash-of-{tag}"},
            ),
            patch("sys.stdout", new=out),
        ):
            retained = mirror_files.retain(
                {"current-hash"}, dry_run=True, skip={"v2.79.2"}
            )
        text = out.getvalue()
        self.assertIn("v2.79.1", text)
        self.assertIn("v2.79.0", text)
        self.assertEqual(
            retained, {"current-hash", "hash-of-v2.79.1", "hash-of-v2.79.0"}
        )

    def test_retain_touch_targets_bucket_path_with_no_create(self):
        calls = []
        with (
            patch.object(mirror_files.shutil, "which", return_value="rclone"),
            patch.object(
                mirror_files.subprocess,
                "run",
                side_effect=lambda cmd, **kw: (
                    calls.append(cmd) or SimpleNamespace(returncode=0, stderr="")
                ),
            ),
            patch("sys.stdout", new=io.StringIO()),
        ):
            retained = mirror_files.retain(
                {"abc123"},
                keep=1,
                dry_run=False,
                remote="pack:blockfield-pack",
            )
        self.assertEqual(retained, {"abc123"})
        self.assertEqual(
            calls,
            [["rclone", "touch", "--no-create", "pack:blockfield-pack/abc123"]],
        )

    def test_retain_touches_concurrently_with_bounded_pool(self):
        import concurrent.futures

        keys = {f"key{i}" for i in range(6)}
        calls = []
        pools = []
        real_pool = concurrent.futures.ThreadPoolExecutor

        class SpyPool(real_pool):
            def __init__(self, *args, **kwargs):
                pools.append(kwargs.get("max_workers"))
                super().__init__(*args, **kwargs)

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[-1].endswith("key0"):
                return SimpleNamespace(returncode=1, stderr="boom")
            return SimpleNamespace(returncode=0, stderr="")

        logged = io.StringIO()
        with (
            patch.object(mirror_files.shutil, "which", return_value="rclone"),
            patch.object(mirror_files.subprocess, "run", side_effect=fake_run),
            patch.object(
                mirror_files.concurrent.futures,
                "ThreadPoolExecutor",
                SpyPool,
            ),
            patch("sys.stdout", new=logged),
        ):
            retained = mirror_files.retain(
                keys, keep=1, dry_run=False, remote="pack:blockfield-pack"
            )
        self.assertEqual(retained, keys)
        # Every key touched exactly once; one failure warns, never fails publish.
        self.assertEqual(
            sorted(cmd[-1] for cmd in calls),
            sorted(f"pack:blockfield-pack/{key}" for key in keys),
        )
        self.assertIn("touch failed for key0", logged.getvalue())
        self.assertIn("Refreshed 5/6 mirror objects", logged.getvalue())
        self.assertEqual(pools, [mirror_files.TOUCH_WORKERS])
        self.assertEqual(mirror_files.TOUCH_WORKERS, 16)

    def test_github_mirror_caps_retained_releases_at_asset_limit(self):
        current = {f"c{i}" for i in range(400)}
        v1_new = {f"n{i}" for i in range(150)}
        v0_new = {f"m{i}" for i in range(100)}
        releases = [
            ("current", current),
            ("v2.79.1", {f"c{i}" for i in range(250)} | v1_new),
            ("v2.79.0", set(current) | v0_new),
            ("v2.78.9", {f"c{i}" for i in range(50)}),
        ]
        budget = mirror_files.GITHUB_ASSETS_MAX - mirror_files.UPLOADS_PER_RUN
        self.assertEqual(budget, 600)
        kept, dropped = mirror_files.cap_retained_for_github(releases, budget)
        self.assertTrue(current <= kept)
        self.assertTrue(v1_new <= kept)
        self.assertTrue(v0_new.isdisjoint(kept))
        self.assertEqual(dropped, ["v2.79.0"])

    def test_github_mirror_keeps_all_releases_under_cap(self):
        current = {f"c{i}" for i in range(100)}
        releases = [("current", set(current))]
        for tag in ("v2.79.1", "v2.79.0", "v2.78.9", "v2.78.8"):
            releases.append((tag, set(current) | {f"{tag}-{i}" for i in range(50)}))
        budget = mirror_files.GITHUB_ASSETS_MAX - mirror_files.UPLOADS_PER_RUN
        kept, dropped = mirror_files.cap_retained_for_github(releases, budget)
        self.assertEqual(dropped, [])
        self.assertEqual(len(kept), 300)

    def test_github_mirror_release_uses_releases_repo_for_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "newhash").write_text("x")
            with (
                patch.object(
                    mirror_files.subprocess,
                    "check_output",
                    return_value="",
                ) as api,
                patch.object(mirror_files.subprocess, "run") as run,
                patch.object(
                    mirror_files,
                    "retained_releases",
                    return_value=[("current", {"newhash"})],
                ) as retain_call,
            ):
                mirror_files.github(
                    out, "Blockfield/blockfield-pack-mirror", "files", "v2.79.2"
                )
        retain_call.assert_called_once_with({"newhash"}, skip={"v2.79.2"})
        self.assertEqual(api.call_count, 2)
        self.assertEqual(run.call_count, 1)

    def test_recent_release_tags_raises_when_gh_fails(self):
        with patch.object(
            mirror_files.subprocess,
            "check_output",
            side_effect=subprocess.CalledProcessError(1, ["gh", "release", "list"]),
        ):
            with self.assertRaises(mirror_files.RetentionError):
                mirror_files.recent_release_tags(4)

    def test_manifest_keys_for_raises_when_release_unreadable(self):
        with patch.object(
            mirror_files.subprocess,
            "check_output",
            side_effect=subprocess.CalledProcessError(1, ["gh", "release", "view"]),
        ):
            with self.assertRaises(mirror_files.RetentionError):
                mirror_files.manifest_keys_for("v2.79.1")

    def test_manifest_keys_for_empty_when_release_has_no_manifest(self):
        with (
            patch.object(
                mirror_files.subprocess,
                "check_output",
                return_value="other-asset.txt\n",
            ),
            patch.object(mirror_files.subprocess, "run") as run,
        ):
            self.assertEqual(mirror_files.manifest_keys_for("v2.78.0"), set())
        run.assert_not_called()

    def test_github_mirror_skips_prune_when_retention_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "newhash").write_text("x")
            existing = "\n".join(
                json.dumps([f"old{i}", i, f"2026-09-{(i % 28) + 1:02d}"])
                for i in range(905)
            )
            failure = subprocess.CalledProcessError(1, ["gh", "release", "list"])
            logged = io.StringIO()
            with (
                patch.object(
                    mirror_files.subprocess,
                    "check_output",
                    side_effect=["123\n", existing, failure],
                ),
                patch.object(mirror_files.subprocess, "run") as run,
                patch("sys.stdout", new=logged),
            ):
                mirror_files.github(
                    out, "Blockfield/blockfield-pack-mirror", "files", "v2.79.2"
                )
        deletes = [
            call
            for call in run.call_args_list
            if list(call.args[0][:3]) == ["gh", "api", "-X"]
        ]
        uploads = [
            call
            for call in run.call_args_list
            if list(call.args[0][:3]) == ["gh", "release", "upload"]
        ]
        self.assertEqual(deletes, [])
        self.assertEqual(len(uploads), 1)
        self.assertIn("WARNING", logged.getvalue())
        self.assertIn("Could not list releases", logged.getvalue())

    def test_github_mirror_prunes_normally_when_old_release_has_no_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "newhash").write_text("x")
            existing = "\n".join(
                json.dumps([f"old{i}", i, f"2026-09-{(i % 28) + 1:02d}"])
                for i in range(905)
            )
            with (
                patch.object(
                    mirror_files.subprocess,
                    "check_output",
                    side_effect=["123\n", existing],
                ),
                patch.object(
                    mirror_files,
                    "recent_release_tags",
                    return_value=["v2.79.0"],
                ),
                patch.object(mirror_files, "manifest_keys_for", return_value=set()),
                patch.object(mirror_files.subprocess, "run") as run,
                patch("sys.stdout", new=io.StringIO()),
            ):
                mirror_files.github(
                    out, "Blockfield/blockfield-pack-mirror", "files", "v2.79.2"
                )
        deletes = [
            call
            for call in run.call_args_list
            if list(call.args[0][:3]) == ["gh", "api", "-X"]
        ]
        self.assertTrue(deletes)

    def test_retain_refreshes_current_when_old_manifests_unreadable(self):
        calls = []
        logged = io.StringIO()
        with (
            patch.object(
                mirror_files,
                "recent_release_tags",
                side_effect=mirror_files.RetentionError(
                    "Could not list releases of Blockfield/blockfield-releases"
                ),
            ),
            patch.object(mirror_files.shutil, "which", return_value="rclone"),
            patch.object(
                mirror_files.subprocess,
                "run",
                side_effect=lambda cmd, **kw: (
                    calls.append(cmd) or SimpleNamespace(returncode=0, stderr="")
                ),
            ),
            patch("sys.stdout", new=logged),
        ):
            retained = mirror_files.retain(
                {"abc123"},
                dry_run=False,
                remote="pack:blockfield-pack",
            )
        self.assertEqual(retained, {"abc123"})
        self.assertEqual(
            calls,
            [["rclone", "touch", "--no-create", "pack:blockfield-pack/abc123"]],
        )
        self.assertIn("WARNING", logged.getvalue())

    def test_r2_download_uses_public_https_and_rejects_unsafe_urls(self):
        with (
            patch.object(packlib.urllib.request, "urlopen") as fetch,
            patch.object(packlib.subprocess, "check_output") as external,
        ):
            fetch.return_value.__enter__.return_value.read.return_value = b"PK-resource"
            for key, quoted in (
                (
                    "music/music-v3/blockfield-music-3.jar",
                    "music/music-v3/blockfield-music-3.jar",
                ),
                (
                    "resources/киви %25%23%3F+&.zip",
                    "resources/%D0%BA%D0%B8%D0%B2%D0%B8%20%25%23%3F%2B%26.zip",
                ),
            ):
                with self.subTest(key=key):
                    self.assertEqual(
                        packlib.get_bytes(f"s3://blockfield-assets/{key}"),
                        b"PK-resource",
                    )
                    request = fetch.call_args.args[0]
                    self.assertEqual(
                        request.full_url, f"{packlib.PUBLIC_R2_BASE_URL}/{quoted}"
                    )
                    self.assertEqual(
                        request.headers, {"User-agent": "Blockfield-CI/1.0"}
                    )
                    self.assertEqual(
                        fetch.call_args.kwargs, {"timeout": packlib.FETCH_TIMEOUT}
                    )
            fetch.reset_mock()
            for url in (
                "s3://example-invalid-bucket/file",
                "s3://blockfield-assets/../secret",
                "s3://blockfield-assets/%2e%2e/secret",
                "s3://blockfield-assets/%2Fsecret",
                "s3://blockfield-assets/path%5Csecret",
                "s3://blockfield-assets/",
                "s3://blockfield-assets/file?token=secret",
                "s3://blockfield-assets/file#fragment",
            ):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    packlib.get_bytes(url)
            fetch.assert_not_called()
            external.assert_not_called()

    def test_fetch_timeout_fails_dead_hosts_in_seconds(self):
        self.assertLessEqual(packlib.FETCH_TIMEOUT, 30)

    def test_ci_installers_come_from_immutable_sources(self):
        launcher = json.loads((ROOT / "launcher.json").read_text())
        for key in ("installer", "bootstrap"):
            with self.subTest(key=key):
                self.assertTrue(
                    launcher[key]["source"].startswith("https://github.com/")
                )

    def test_update_launcher_json_keeps_immutable_installer_sources(self):
        launcher_spec = importlib.util.spec_from_file_location(
            "update_launcher_json", ROOT / "scripts/update-launcher-json.py"
        )
        launcher = importlib.util.module_from_spec(launcher_spec)
        launcher_spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "packwiz-installer.jar").write_bytes(b"new-installer")
            (root / "packwiz-installer-bootstrap.jar").write_bytes(b"new-bootstrap")
            (root / "launcher.json").write_text(
                json.dumps(
                    {
                        "installer": {
                            "url": "https://example.test/packwiz-installer.jar",
                            "source": "https://example.test/immutable-installer.jar",
                            "sha256": "old",
                        },
                        "bootstrap": {
                            "url": "https://example.test/packwiz-installer-bootstrap.jar",
                            "sha256": "old",
                        },
                    }
                )
            )
            with (
                patch.object(launcher, "ROOT", root),
                patch.object(
                    launcher,
                    "adoptium",
                    return_value={
                        "version": "21",
                        "url": "https://example.test/jre",
                        "sha256": "jre",
                        "size": 1,
                    },
                ),
            ):
                launcher.main()
            data = json.loads((root / "launcher.json").read_text())
            self.assertEqual(
                data["installer"]["source"],
                "https://example.test/immutable-installer.jar",
            )
            self.assertNotIn("source", data["bootstrap"])
            self.assertEqual(
                data["bootstrap"]["sha256"], packlib.digest(b"new-bootstrap")
            )

    def test_build_rejects_compressor_drift_before_loading_inputs(self):
        with patch.object(packlib.zlib, "ZLIB_RUNTIME_VERSION", "1.3.1"):
            with self.assertRaisesRegex(RuntimeError, "zlib 1.3.2"):
                client.build(None)

    def test_archive_rejects_compressor_drift_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "archive.zip"
            target.write_bytes(b"previous artifact")
            with patch.object(packlib.zlib, "ZLIB_RUNTIME_VERSION", "1.3.1"):
                with self.assertRaisesRegex(RuntimeError, "zlib 1.3.2"):
                    packlib.write_zip(target, {"a.txt": b"a"})
            self.assertEqual(target.read_bytes(), b"previous artifact")

    def test_web_tar_is_deterministic_with_fixed_owner_mode_and_mtime(self):
        def make(root):
            tree = Path(root) / "web"
            (tree / "files/mods").mkdir(parents=True)
            (tree / "files/mods/a.jar").write_bytes(b"PK")
            (tree / "build.json").write_text("{}")
            packlib.write_tar(Path(root) / "web.tar", tree, 315532800)
            return (Path(root) / "web.tar").read_bytes()

        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            data = make(one)
            self.assertEqual(data, make(two))
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = tar.getmembers()
        self.assertEqual(
            [m.name for m in members],
            ["build.json", "files", "files/mods", "files/mods/a.jar"],
        )
        self.assertEqual({m.mtime for m in members}, {315532800})
        self.assertEqual({(m.uid, m.gid) for m in members}, {(0, 0)})
        self.assertEqual([m.mode for m in members if m.isdir()], [0o755, 0o755])

    def test_archive_metadata_and_bytes_do_not_depend_on_host_os(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs = []
            for platform in ("linux", "win32"):
                target = Path(tmp) / f"{platform}.zip"
                with patch.object(zipfile.sys, "platform", platform):
                    packlib.write_zip(target, {"a.txt": b"a", "run.sh": b"echo test"})
                outputs.append(target.read_bytes())
                with zipfile.ZipFile(target) as archive:
                    for info in archive.infolist():
                        self.assertEqual(info.create_system, 3)
                        self.assertEqual(info.date_time, (2026, 1, 1, 0, 0, 0))
                        expected = 0o755 if info.filename.endswith(".sh") else 0o644
                        self.assertEqual(info.external_attr >> 16, expected)
            self.assertEqual(*outputs)

    def test_client_inventory_is_current_and_has_no_server_mods(self):
        pack = tomllib.loads((ROOT / "pack.toml").read_text())
        self.assertEqual(
            pack["index"]["hash"], packlib.digest((ROOT / "index.toml").read_bytes())
        )
        with (
            patch.object(packlib, "load_resources"),
            patch.object(
                packlib,
                "resolve_mod",
                side_effect=lambda root, item, built: (
                    *item,
                    "shaderpacks/" + item[1]["filename"],
                    b"PK-test",
                ),
            ),
        ):
            mods, defaults = packlib.inventory(ROOT)
        self.assertTrue(mods)
        self.assertTrue(
            all(m.get("side", "both") in ("client", "both") for _, m in mods)
        )
        self.assertEqual(defaults["options.txt"], b"")
        entries = tomllib.loads((ROOT / "index.toml").read_text())["files"]
        self.assertTrue(
            next(e for e in entries if e["file"] == "options.txt")["preserve"]
        )

    def test_build_preserves_player_options_and_rejects_server_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = b"PK-test-mod"
            installer = b"PK-test-installer"
            mod = {
                "name": "Test",
                "filename": "blockfield-client-1.jar",
                "side": "client",
                "download": {
                    "url": "https://example.test/mod.jar",
                    "hash-format": "sha256",
                    "hash": packlib.digest(bundle),
                },
            }
            (root / "pack.toml").write_text(
                'name = "Blockfield"\nversion = "1.0.0"\n[versions]\nminecraft = "1.21.1"\nfabric = "0.19.3"\n[index]\nhash-format = "sha256"\nhash = "'
                + packlib.digest(b"files = []\n")
                + '"\n',
                encoding="utf-8",
                newline="\n",
            )
            (root / "index.toml").write_text(
                "files = []\n", encoding="utf-8", newline="\n"
            )
            (root / "launcher.json").write_text(
                json.dumps(
                    {
                        k: {
                            "url": "https://example.test/installer.jar",
                            "sha256": packlib.digest(installer),
                        }
                        for k in ("bootstrap", "installer")
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )
            (root / "content.json").write_text("{}", encoding="utf-8", newline="\n")
            (root / "mirrors.json").write_text(
                '[{"name": "Gcore CDN", "url": "https://cdn.example/{path}"},'
                ' {"name": "GitHub", "url": "https://gh.example/files/{hash}"}]',
                encoding="utf-8",
            )
            args = SimpleNamespace(
                root=root, out=root / "out", public_base="https://example.test"
            )
            with (
                patch.object(
                    client,
                    "inventory",
                    return_value=([("mods/test.pw.toml", mod)], {"options.txt": b""}),
                ),
                patch.object(
                    client,
                    "resolve_mod",
                    return_value=(
                        "mods/test.pw.toml",
                        mod,
                        "mods/blockfield-client-1.jar",
                        bundle,
                    ),
                ),
                patch.object(client, "get_bytes", return_value=installer),
                patch.object(client, "git_head", return_value="public-commit"),
            ):
                client.build(args)
                build = json.loads((args.out / "web/build.json").read_text())
                self.assertEqual(build["sources"], {"modpack": "public-commit"})
                self.assertNotIn("server_digest", build)
                self.assertFalse((args.out / "server").exists())
                marker = "config/blockfield-pack-contract.txt"
                contract = (build["compatibility"] + "\n").encode()
                self.assertEqual((args.out / "web" / marker).read_bytes(), contract)
                with zipfile.ZipFile(args.out / "Blockfield-client.zip") as archive:
                    self.assertEqual(archive.read(marker), contract)
                with zipfile.ZipFile(args.out / "Blockfield.mrpack") as archive:
                    self.assertEqual(archive.read("overrides/" + marker), contract)
                self.assertIn(
                    f'file = "{marker}"', (args.out / "web/index.toml").read_text()
                )
                mirrors = json.loads((args.out / "web/mirrors.json").read_text())
                self.assertEqual(
                    mirrors["probe"],
                    {
                        "path": "files/mods/blockfield-client-1.jar",
                        "hash": packlib.digest(bundle, "sha512"),
                    },
                )
                self.assertEqual(
                    mirrors["mirrors"][1]["url"], "https://gh.example/files/{hash}"
                )
                staged = mirror_files.mirror_files(args.out / "web")
                self.assertEqual(
                    staged[packlib.digest(bundle, "sha512")],
                    args.out / "web/files/mods/blockfield-client-1.jar",
                )
                self.assertEqual(
                    staged[packlib.digest(contract)], args.out / "web" / marker
                )

                tar_bytes = (args.out / "Blockfield-web.tar").read_bytes()
                self.assertIn(
                    f"{packlib.digest(tar_bytes)}  Blockfield-web.tar\n",
                    (args.out / "SHA256SUMS").read_text(),
                )
                self.assertNotIn(
                    "Blockfield-web.tar", (args.out / "web/SHA256SUMS").read_text()
                )
                with tarfile.open(args.out / "Blockfield-web.tar") as tar:
                    self.assertLessEqual(
                        {
                            "build.json",
                            "SHA256SUMS",
                            "files/mods/blockfield-client-1.jar",
                        },
                        set(tar.getnames()),
                    )
                self.assertEqual(
                    list((args.out / "web/files/mods").iterdir()),
                    [args.out / "web/files/mods/blockfield-client-1.jar"],
                )
                mod["side"] = "server"
                args.out = root / "rejected"
                with self.assertRaisesRegex(ValueError, "Non-client mod"):
                    client.build(args)

    def test_workshop_set_is_a_classified_subset_of_the_game_inventory(self):
        keep, drops = client.workshop_selection(ROOT)
        names = {
            entry["file"].rsplit("/", 1)[-1]
            for entry in tomllib.loads((ROOT / "index.toml").read_text())["files"]
            if entry["file"].startswith("mods/")
        }
        self.assertTrue(keep <= names)
        for combat in (
            "tacz-refabricated.pw.toml",
            "tacz-tweaks.pw.toml",
            "elite-x-quality-guns.pw.toml",
            "superbwarfare.pw.toml",
            "lrtactical-fabric.pw.toml",
            "parcool.pw.toml",
            "blockfield-client.pw.toml",
        ):
            self.assertNotIn(combat, keep)
        self.assertIn("worldedit.pw.toml", keep)

    def test_workshop_build_keeps_only_the_lean_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = b"PK-test-mod"
            installer = b"PK-test-installer"
            game_mod = {
                "name": "Game",
                "filename": "game-1.jar",
                "side": "both",
                "download": {
                    "url": "https://example.test/game.jar",
                    "hash-format": "sha256",
                    "hash": packlib.digest(bundle),
                },
            }
            kept_mod = {
                "name": "Kept",
                "filename": "kept-1.jar",
                "side": "both",
                "download": {
                    "url": "https://example.test/kept.jar",
                    "hash-format": "sha256",
                    "hash": packlib.digest(bundle),
                },
            }
            (root / "pack.toml").write_text(
                'name = "Blockfield"\nversion = "1.0.0"\n[versions]\nminecraft = "1.21.1"\nfabric = "0.19.3"\n[index]\nhash-format = "sha256"\nhash = "'
                + packlib.digest(b"files = []\n")
                + '"\n',
                encoding="utf-8",
                newline="\n",
            )
            (root / "index.toml").write_text(
                "files = []\n", encoding="utf-8", newline="\n"
            )
            (root / "pack-workshop.txt").write_text(
                "[mods]\nkept.pw.toml\n[drop-config]\nconfig/game.toml\n",
                encoding="utf-8",
                newline="\n",
            )
            (root / "launcher.json").write_text(
                json.dumps(
                    {
                        k: {
                            "url": "https://example.test/installer.jar",
                            "sha256": packlib.digest(installer),
                        }
                        for k in ("bootstrap", "installer")
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )
            (root / "content.json").write_text("{}", encoding="utf-8", newline="\n")
            (root / "mirrors.json").write_text(
                '[{"name": "Gcore CDN", "url": "https://cdn.example/{path}"},'
                ' {"name": "GitHub", "url": "https://gh.example/files/{hash}"}]',
                encoding="utf-8",
                newline="\n",
            )
            args = SimpleNamespace(
                root=root,
                out=root / "out",
                public_base="https://example.test",
                variant="workshop",
            )
            inventory = [
                ("mods/game.pw.toml", game_mod),
                ("mods/kept.pw.toml", kept_mod),
            ]
            resolved = [
                ("mods/game.pw.toml", game_mod, "mods/game-1.jar", bundle),
                ("mods/kept.pw.toml", kept_mod, "mods/kept-1.jar", bundle),
            ]
            with (
                patch.object(
                    client,
                    "inventory",
                    return_value=(
                        inventory,
                        {"options.txt": b"", "config/game.toml": b"x"},
                    ),
                ),
                patch.object(
                    client,
                    "resolve_mod",
                    side_effect=lambda root, item, built: next(
                        r for r in resolved if r[0] == item[0]
                    ),
                ),
                patch.object(client, "get_bytes", return_value=installer),
                patch.object(client, "git_head", return_value="public-commit"),
            ):
                client.build(args)
                build = json.loads((args.out / "web/build.json").read_text())
                self.assertEqual(build["variant"], "workshop")
                self.assertEqual(
                    [m["path"] for m in build["mods"]], ["mods/kept-1.jar"]
                )
                with zipfile.ZipFile(args.out / "Blockfield-workshop.zip") as archive:
                    self.assertIn("mods/kept-1.jar", archive.namelist())
                    self.assertNotIn("mods/game-1.jar", archive.namelist())
                    self.assertNotIn("config/game.toml", archive.namelist())

    def test_resource_archive_is_pinned_and_cannot_escape_its_prefix(self):
        import io
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = [
                ("tacz/gucci_vuitton_attachment/", name)
                for name in (
                    "tacz/gucci_vuitton_attachment/data.json",
                    "../secret",
                    "config/secret.json",
                )
            ]
            cases += [
                ("config/worldedit/schematics/", name)
                for name in (
                    "config/worldedit/schematics/Map.schem",
                    "config/worldedit/secret.json",
                )
            ]
            cases.append(("config/", "config/secret.json"))
            for prefix, name in cases:
                out = io.BytesIO()
                with zipfile.ZipFile(out, "w") as archive:
                    archive.writestr(name, "{}")
                data = out.getvalue()
                lock = [
                    {
                        "url": "https://example.test/pack.zip",
                        "sha256": packlib.digest(data),
                        "prefix": prefix,
                    }
                ]
                (root / "resources.lock.json").write_text(
                    json.dumps(lock), encoding="utf-8", newline="\n"
                )
                with patch.object(packlib, "get_bytes", return_value=data):
                    defaults = {}
                    if name.startswith(prefix) and prefix != "config/":
                        packlib.load_resources(root, defaults)
                        self.assertEqual(defaults, {name: b"{}"})
                    else:
                        with self.assertRaises(ValueError):
                            packlib.load_resources(root, defaults)
                    lock[0]["sha256"] = "invalid"
                    (root / "resources.lock.json").write_text(
                        json.dumps(lock), encoding="utf-8", newline="\n"
                    )
                    with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                        packlib.load_resources(root, {})

    def test_shader_download_is_pinned_and_is_not_a_mod(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "shaderpacks").mkdir()
            data = b"PK-shader"
            metadata = (
                'name = "Shader"\nfilename = "shader.zip"\nside = "client"\n'
                '[download]\nurl = "https://example.test/shader.zip"\n'
                'hash-format = "sha256"\nhash = "' + packlib.digest(data) + '"\n'
            )
            (root / "shaderpacks/shader.pw.toml").write_text(
                metadata, encoding="utf-8", newline="\n"
            )
            (root / "index.toml").write_text(
                'hash-format = "sha256"\n[[files]]\n'
                'file = "shaderpacks/shader.pw.toml"\nmetafile = true\nhash = "'
                + packlib.digest(metadata.encode())
                + '"\n',
                encoding="utf-8",
                newline="\n",
            )
            with patch.object(packlib, "get_bytes", return_value=data):
                self.assertEqual(
                    packlib.inventory(root), ([], {"shaderpacks/shader.zip": data})
                )
            with patch.object(packlib, "get_bytes", return_value=b"PK-wrong"):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    packlib.inventory(root)

    def test_paths_cannot_escape_the_pack(self):
        for name in ("../secret", "/secret", "mods/../../secret", "mods\\secret"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                packlib.safe_path(name)

    def test_workshop_inputs_ignores_game_pins(self):
        mods, _ = client.inventory(ROOT)
        pack = tomllib.loads((ROOT / "pack.toml").read_text())
        protocol_file = ROOT / "compatibility.json"
        protocol = (
            json.loads(protocol_file.read_text())["protocol"]
            if protocol_file.exists()
            else 1
        )
        kept, _ = client.apply_workshop_selection(
            ROOT, mods, {"config/tacz-client.toml": b"x"}
        )
        base = client.workshop_inputs_hash(ROOT, kept, protocol, pack["versions"])
        self.assertRegex(base, r"[0-9a-f]{64}")
        # A game-only pin changing must not move the hash.
        moved_game = [
            (name, {**mod, "filename": "changed.jar"})
            if name == "mods/tacz-refabricated.pw.toml"
            else (name, mod)
            for name, mod in kept
        ]
        self.assertEqual(
            client.workshop_inputs_hash(ROOT, moved_game, protocol, pack["versions"]),
            base,
        )
        # The Workshop's own inputs move it: kept pin, protocol, platform.
        kept_name = kept[0][0]
        moved_pin = [
            (name, {**mod, "filename": "changed.jar"})
            if name == kept_name
            else (name, mod)
            for name, mod in kept
        ]
        self.assertNotEqual(
            client.workshop_inputs_hash(ROOT, moved_pin, protocol, pack["versions"]),
            base,
        )
        self.assertNotEqual(
            client.workshop_inputs_hash(ROOT, kept, protocol + 1, pack["versions"]),
            base,
        )
        bumped = dict(pack["versions"], minecraft="9.9.9")
        self.assertNotEqual(
            client.workshop_inputs_hash(ROOT, kept, protocol, bumped), base
        )


if __name__ == "__main__":
    unittest.main()
