"""Versioned pre-push hook wiring."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "client_quality", ROOT / "scripts/quality.py"
)
client_quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_quality)


class PrePushHook(unittest.TestCase):
    def test_hook_runs_lint_and_format_check_without_gradle(self):
        hook = ROOT / ".githooks" / "pre-push"
        self.assertTrue(hook.is_file())
        self.assertTrue(hook.stat().st_mode & 0o111)
        text = hook.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("just lint", text)
        self.assertIn("just format-check", text)
        commands = [
            line for line in text.splitlines() if line and not line.startswith("#")
        ]
        self.assertFalse(any("gradle" in line.lower() for line in commands))

    def test_setup_wires_versioned_hooks(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            client_quality.wire_hooks(root)
            probe = subprocess.run(
                ["git", "config", "core.hooksPath"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.stdout.strip(), ".githooks")

    def test_wire_hooks_leaves_explicit_choice_alone(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            subprocess.run(
                ["git", "config", "core.hooksPath", ".custom"],
                cwd=root,
                check=True,
            )
            self.assertFalse(client_quality.wire_hooks(root))
            probe = subprocess.run(
                ["git", "config", "core.hooksPath"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.stdout.strip(), ".custom")

    def test_wire_hooks_reports_already_wired_as_success(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            subprocess.run(
                ["git", "config", "core.hooksPath", ".githooks"],
                cwd=root,
                check=True,
            )
            self.assertTrue(client_quality.wire_hooks(root))

    def test_wire_hooks_keeps_global_hooks_path(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp) / "repo"
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (root / ".githooks").mkdir()
            gitconfig = Path(tmp) / "gitconfig"
            gitconfig.write_text("[core]\n\thooksPath = /global/hooks\n")
            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(gitconfig)}):
                self.assertFalse(client_quality.wire_hooks(root))
            local = subprocess.run(
                ["git", "config", "--local", "--get", "core.hooksPath"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(local.stdout, "")

    def test_wire_hooks_keeps_active_hooks(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            (root / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
            self.assertFalse(client_quality.wire_hooks(root))
            local = subprocess.run(
                ["git", "config", "--local", "--get", "core.hooksPath"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(local.stdout, "")

    def test_wire_hooks_leaves_outer_repo_alone_on_invalid_gitdir(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            outer = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            subprocess.run(
                ["git", "config", "core.hooksPath", "/outer/hooks"],
                cwd=outer,
                check=True,
            )
            sub = outer / "sub"
            sub.mkdir()
            (sub / ".git").mkdir()  # invalid git dir: git would resolve upward
            (sub / ".githooks").mkdir()
            self.assertFalse(client_quality.wire_hooks(sub))
            probe = subprocess.run(
                ["git", "config", "--local", "--get", "core.hooksPath"],
                cwd=outer,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.stdout.strip(), "/outer/hooks")


class PreCommitHook(unittest.TestCase):
    def test_hook_formats_staged_files_without_gradle(self):
        hook = ROOT / ".githooks" / "pre-commit"
        self.assertTrue(hook.is_file())
        mode = subprocess.run(
            ["git", "ls-files", "-s", ".githooks/pre-commit"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertTrue(mode.startswith("100755"), mode)
        text = hook.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("just format-staged", text)
        commands = [
            line for line in text.splitlines() if line and not line.startswith("#")
        ]
        self.assertFalse(any("gradle" in line.lower() for line in commands))

    def test_commit_formats_staged_files(self):
        # Fail-before: without the hook + format-staged, the raw bytes commit.
        with tempfile.TemporaryDirectory(prefix="bf precommit ") as tmp:
            root = Path(tmp)
            for name in (
                ".gitignore",
                "justfile",
                "scripts/quality.py",
                "scripts/toolchain.py",
            ):
                src = ROOT / name
                dst = root / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
            (root / ".githooks").mkdir()
            (root / ".githooks" / "pre-commit").write_bytes(
                (ROOT / ".githooks" / "pre-commit").read_bytes()
            )
            (root / ".githooks" / "pre-commit").chmod(0o755)

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                ).stdout

            git("init", "--quiet")
            git("config", "user.email", "hook-test@example.com")
            git("config", "user.name", "hook-test")
            git("config", "core.hooksPath", str(root / ".githooks"))
            git("add", ".")
            git("commit", "--quiet", "-m", "fixture")
            (root / "probe.py").write_text("x=1\n", encoding="utf-8")
            (root / "probe.sh").write_text(
                "if [ 1 = 1 ] ; then echo hi ; fi\n", encoding="utf-8"
            )
            git("add", "probe.py", "probe.sh")
            git("commit", "--quiet", "-m", "probe")
            formatted_py = git("show", "HEAD:probe.py")
            formatted_sh = git("show", "HEAD:probe.sh")
            self.assertNotEqual(formatted_py, "x=1\n")
            self.assertNotEqual(formatted_sh, "if [ 1 = 1 ] ; then echo hi ; fi\n")
            self.assertEqual(
                subprocess.run(
                    ["just", "format-check"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=300,
                ).returncode,
                0,
            )

    def test_commit_leaves_partially_staged_files_alone(self):
        # Fail-before: the old hook formatted the worktree copy and staged it
        # whole, committing hunks the user never staged.
        with tempfile.TemporaryDirectory(prefix="bf precommit ") as tmp:
            root = Path(tmp)
            for name in (
                ".gitignore",
                "justfile",
                "scripts/quality.py",
                "scripts/toolchain.py",
            ):
                src = ROOT / name
                dst = root / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
            (root / ".githooks").mkdir()
            (root / ".githooks" / "pre-commit").write_bytes(
                (ROOT / ".githooks" / "pre-commit").read_bytes()
            )
            (root / ".githooks" / "pre-commit").chmod(0o755)

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                ).stdout

            git("init", "--quiet")
            git("config", "user.email", "hook-test@example.com")
            git("config", "user.name", "hook-test")
            git("config", "core.hooksPath", str(root / ".githooks"))
            git("add", ".")
            git("commit", "--quiet", "-m", "fixture")
            (root / "probe.py").write_text("x=1\n", encoding="utf-8")
            git("add", "probe.py")
            (root / "probe.py").write_text(
                "x=1\n# scratch, never staged\n", encoding="utf-8"
            )
            git("commit", "--quiet", "-m", "probe")
            self.assertEqual(git("show", "HEAD:probe.py"), "x=1\n")
            self.assertEqual(
                (root / "probe.py").read_text(encoding="utf-8"),
                "x=1\n# scratch, never staged\n",
            )


PUBLISHED_ID = "endpoint blockfield-proxy-abc123 !"
NEW_ID = "endpoint blockfield-proxy-def456 !"


def _git(root, *args, **env):
    full_env = dict(os.environ)
    full_env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
    )
    full_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=full_env,
    )


def _commit(root, name, files):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", name)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


class PrePushMergeScan(unittest.TestCase):
    """pre-push must not re-flag identifiers already published via a merge's
    second parent, but must flag new identifiers from resolutions/side commits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bf prepush ")
        self.root = Path(self.tmp.name)
        _git(self.root, "init", "-qb", "main", ".")
        dest = self.root / ".githooks"
        dest.mkdir()
        (dest / "pre-push").write_bytes((ROOT / ".githooks" / "pre-push").read_bytes())
        (dest / "pre-push").chmod(0o755)
        # Stub the two external gates: `just` and the pinned gitleaks binary.
        bindir = self.root / "bin"
        bindir.mkdir()
        just = bindir / "just"
        just.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        just.chmod(0o755)
        cache = self.root / ".cache" / "quality" / "gitleaks-8.24.3"
        cache.mkdir(parents=True)
        fake = cache / "gitleaks"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
            # Hermetic identity: `git merge` needs a committer even for
            # --no-commit, and hostname-based fallback fails on some hosts.
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@t",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@t",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, local_sha, remote_sha):
        line = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
        return subprocess.run(
            ["sh", ".githooks/pre-push"],
            cwd=self.root,
            input=line,
            capture_output=True,
            text=True,
            env=self.env,
        )

    ZERO = "0000000000000000000000000000000000000000"

    def run_hook_new_branch(self, local_sha, remote="origin"):
        line = f"refs/heads/feature {local_sha} refs/heads/feature {self.ZERO}\n"
        return subprocess.run(
            ["sh", ".githooks/pre-push", remote, f"/tmp/fake-{remote}"],
            cwd=self.root,
            input=line,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def _set_origin_main(self, sha):
        _git(self.root, "update-ref", "refs/remotes/origin/main", sha)
        _git(
            self.root,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        )

    def test_new_branch_merge_resolution_with_new_id_is_blocked(self):
        _commit(self.root, "base", {"h.txt": "line1\nline2\nline3\n"})
        base = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        self._set_origin_main(base)
        _git(self.root, "checkout", "-qb", "side")
        _commit(self.root, "side", {"h.txt": "line1\nSIDE\nline3\n"})
        _git(self.root, "checkout", "-q", "main")
        _commit(self.root, "mainwork", {"h.txt": "line1\nMAIN\nline3\n"})
        subprocess.run(
            ["git", "merge", "side", "--no-commit", "-q"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self.env,
        )
        (self.root / "h.txt").write_text(
            f"line1\nMAIN\nline3\n{NEW_ID}\n", encoding="utf-8"
        )
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-qm", "merge-resolve-newid")
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        probe = self.run_hook_new_branch(local)
        self.assertEqual(probe.returncode, 1)
        self.assertIn("infrastructure identifier", probe.stderr)

    def test_new_branch_merge_of_published_main_is_clean(self):
        _commit(
            self.root,
            "base",
            {"id.txt": PUBLISHED_ID + "\n", "h.txt": "line1\nBASE\nline3\n"},
        )
        base = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        self._set_origin_main(base)
        _git(self.root, "checkout", "-qb", "feature")
        _commit(self.root, "sidework", {"g.txt": "feat\n"})
        _git(self.root, "checkout", "-q", "main")
        _commit(self.root, "mainwork", {"m.txt": "work\n"})
        main_tip = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _git(self.root, "update-ref", "refs/remotes/origin/main", main_tip)
        _git(self.root, "checkout", "-q", "feature")
        _git(self.root, "merge", "-q", "main", "-m", "merge-main")
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        probe = self.run_hook_new_branch(local)
        self.assertEqual(probe.returncode, 0, f"hook blocked push:\n{probe.stderr}")

    def test_new_branch_uses_pushed_remote_default_branch(self):
        _commit(self.root, "base", {"h.txt": "line1\n"})
        base = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        self._set_origin_main(base)
        _git(self.root, "checkout", "-qb", "upstream-main")
        upstream_tip = _commit(self.root, "publish", {"id.txt": PUBLISHED_ID + "\n"})
        _git(self.root, "update-ref", "refs/remotes/upstream/main", upstream_tip)
        _git(
            self.root,
            "symbolic-ref",
            "refs/remotes/upstream/HEAD",
            "refs/remotes/upstream/main",
        )
        _git(self.root, "checkout", "-qb", "feature", base)
        _git(self.root, "merge", "-q", "upstream-main", "-m", "merge-upstream")
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        probe = self.run_hook_new_branch(local, remote="upstream")
        self.assertEqual(probe.returncode, 0, f"hook blocked push:\n{probe.stderr}")

    def test_new_branch_tip_published_after_fork_is_clean(self):
        _commit(self.root, "base", {"h.txt": "line1\n"})
        base = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        self._set_origin_main(base)
        _git(self.root, "checkout", "-qb", "feature", base)
        _commit(self.root, "side", {"id.txt": PUBLISHED_ID + "\n"})
        _git(self.root, "checkout", "-q", "main")
        main_tip = _commit(self.root, "publish", {"id.txt": PUBLISHED_ID + "\n"})
        _git(self.root, "update-ref", "refs/remotes/origin/main", main_tip)
        _git(
            self.root,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        )
        _git(self.root, "checkout", "-q", "feature")
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        probe = self.run_hook_new_branch(local)
        self.assertEqual(probe.returncode, 0, f"hook blocked push:\n{probe.stderr}")

    def test_new_branch_without_remote_default_still_blocks_new_id(self):
        _commit(self.root, "base", {"h.txt": "line1\n"})
        local = _commit(self.root, "side", {"s.txt": NEW_ID + "\n"})
        probe = self.run_hook_new_branch(local, remote="fresh")
        self.assertEqual(probe.returncode, 1)
        self.assertIn("infrastructure identifier", probe.stderr)

    def test_merge_of_main_with_published_id_is_clean(self):
        _commit(self.root, "base", {"f.txt": "base\n"})
        _git(self.root, "checkout", "-qb", "feature")
        plain = _commit(self.root, "side", {"g.txt": "feat\n"})
        _git(self.root, "checkout", "-q", "main")
        _commit(self.root, "publish", {"id.txt": PUBLISHED_ID + "\n"})
        remote = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _git(self.root, "checkout", "-q", "feature")
        _git(self.root, "merge", "-q", "main", "-m", "merge-main")
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(plain, remote)
        probe = self.run_hook(local, remote)
        self.assertEqual(probe.returncode, 0, f"hook blocked push:\n{probe.stderr}")

    def test_resolution_with_new_id_is_blocked(self):
        _commit(self.root, "base", {"h.txt": "line1\nline2\nline3\n"})
        _git(self.root, "checkout", "-qb", "side")
        _commit(self.root, "side", {"h.txt": "line1\nSIDE\nline3\n"})
        _git(self.root, "checkout", "-q", "main")
        remote = _commit(self.root, "main", {"h.txt": "line1\nMAIN\nline3\n"})
        subprocess.run(
            ["git", "merge", "side", "--no-commit", "-q"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self.env,
        )
        (self.root / "h.txt").write_text(
            f"line1\nMAIN\nline3\n{NEW_ID}\n", encoding="utf-8"
        )
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-qm", "merge-resolve-newid")
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        probe = self.run_hook(local, remote)
        self.assertEqual(probe.returncode, 1)
        self.assertIn("infrastructure identifier", probe.stderr)

    def test_resolution_keeping_remote_id_is_clean(self):
        _commit(
            self.root,
            "base",
            {"id.txt": PUBLISHED_ID + "\n", "h.txt": "line1\nBASE\nline3\n"},
        )
        _git(self.root, "checkout", "-qb", "side")
        _commit(
            self.root,
            "sidework",
            {"id.txt": "endpoint local-dev !\n", "h.txt": "line1\nSIDE\nline3\n"},
        )
        _git(self.root, "checkout", "-q", "main")
        remote = _commit(self.root, "mainwork", {"h.txt": "line1\nMAIN\nline3\n"})
        subprocess.run(
            ["git", "merge", "side", "--no-commit", "-q"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self.env,
        )
        (self.root / "id.txt").write_text(PUBLISHED_ID + "\n", encoding="utf-8")
        (self.root / "h.txt").write_text("line1\nMAIN\nline3\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-qm", "merge-keep-remote")
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        probe = self.run_hook(local, remote)
        self.assertEqual(probe.returncode, 0, f"hook blocked push:\n{probe.stderr}")

    def test_octopus_merge_resolution_with_new_id_is_blocked(self):
        _commit(self.root, "base", {"f.txt": "base\n"})
        _git(self.root, "checkout", "-qb", "b1")
        _commit(self.root, "one", {"1.txt": "one\n"})
        _git(self.root, "checkout", "-qb", "b2", "main")
        _commit(self.root, "two", {"2.txt": "two\n"})
        _git(self.root, "checkout", "-q", "main")
        remote = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        b1 = _git(self.root, "rev-parse", "b1").stdout.strip()
        b2 = _git(self.root, "rev-parse", "b2").stdout.strip()
        # Octopus merges cannot hold conflict resolutions, so craft the
        # result tree: main tree plus a file with a new identifier.
        (self.root / "new.txt").write_text(NEW_ID + "\n", encoding="utf-8")
        _git(self.root, "add", ".")
        tree = _git(self.root, "write-tree").stdout.strip()
        _git(self.root, "checkout", "-q", "--", ".")
        (self.root / "new.txt").unlink()
        local = _git(
            self.root,
            "commit-tree",
            tree,
            "-p",
            remote,
            "-p",
            b1,
            "-p",
            b2,
            "-m",
            "octopus-resolve-newid",
        ).stdout.strip()
        probe = self.run_hook(local, remote)
        self.assertEqual(probe.returncode, 1)
        self.assertIn("infrastructure identifier", probe.stderr)

    def test_octopus_merge_without_new_id_is_clean(self):
        _commit(self.root, "base", {"f.txt": "base\n"})
        _git(self.root, "checkout", "-qb", "b1")
        _commit(self.root, "one", {"1.txt": "one\n"})
        _git(self.root, "checkout", "-qb", "b2", "main")
        _commit(self.root, "two", {"2.txt": "two\n"})
        _git(self.root, "checkout", "-q", "main")
        _commit(self.root, "main-work", {"m.txt": "work\n"})
        remote = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _git(self.root, "merge", "b1", "b2", "-m", "octopus", "-q")
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        parents = _git(self.root, "rev-list", "--parents", "-n", "1", local)
        self.assertGreaterEqual(len(parents.stdout.split()), 4)
        probe = self.run_hook(local, remote)
        self.assertEqual(probe.returncode, 0, f"hook blocked push:\n{probe.stderr}")

    def test_unreadable_diff_fails_closed(self):
        import shutil

        real_git = shutil.which("git")
        wrapper = self.root / "bin" / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "log" ] || [ "$1" = "diff" ]; then exit 129; fi\n'
            f'exec "{real_git}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        _commit(self.root, "base", {"f.txt": "base\n"})
        remote = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _commit(self.root, "plain", {"g.txt": "feat\n"})
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        probe = self.run_hook(local, remote)
        self.assertEqual(probe.returncode, 1)
        self.assertIn("cannot inspect", probe.stderr)

    def test_octopus_scan_skips_tree_search_without_identifiers(self):
        import shutil

        real_git = shutil.which("git")
        count_file = self.root / "grep-calls"
        count_file.write_text("", encoding="utf-8")
        wrapper = self.root / "bin" / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            '[ "$1" = "grep" ] && echo x >> "$COUNT_FILE"\n'
            f'exec "{real_git}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        self.env = dict(self.env, COUNT_FILE=str(count_file))
        _commit(self.root, "base", {"f.txt": "base\n"})
        _git(self.root, "checkout", "-qb", "b1")
        big = "".join(f"data line {i}\n" for i in range(2000))
        _commit(self.root, "one", {"1.txt": big})
        _git(self.root, "checkout", "-qb", "b2", "main")
        _commit(self.root, "two", {"2.txt": big})
        _git(self.root, "checkout", "-q", "main")
        _commit(self.root, "main-work", {"m.txt": "work\n"})
        remote = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _git(self.root, "merge", "b1", "b2", "-m", "octopus", "-q")
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        probe = self.run_hook(local, remote)
        self.assertEqual(probe.returncode, 0)
        calls = len(count_file.read_text(encoding="utf-8"))
        self.assertEqual(calls, 0, f"hook ran {calls} parent-tree searches")

    def test_octopus_reviving_removed_id_is_blocked(self):
        _commit(self.root, "base", {"id.txt": PUBLISHED_ID + "\n"})
        _git(self.root, "checkout", "-qb", "old")
        old = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _git(self.root, "checkout", "-q", "main")
        lines = (self.root / "id.txt").read_text(encoding="utf-8").splitlines()
        kept = (
            "\n".join(line for line in lines if PUBLISHED_ID not in line) or "clean\n"
        )
        (self.root / "id.txt").write_text(kept + "\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-qm", "remove-id")
        remote = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _git(self.root, "checkout", "-qb", "b2", "main")
        _commit(self.root, "two", {"2.txt": "two\n"})
        b2 = _git(self.root, "rev-parse", "b2").stdout.strip()
        # Octopus result tree revives the removed identifier verbatim from
        # the excluded old history: invisible to the per-commit walk, but
        # the tip diff against the remote shows it as added.
        (self.root / "id.txt").write_text(PUBLISHED_ID + "\n", encoding="utf-8")
        _git(self.root, "add", ".")
        tree = _git(self.root, "write-tree").stdout.strip()
        _git(self.root, "checkout", "-q", "--", ".")
        local = _git(
            self.root,
            "commit-tree",
            tree,
            "-p",
            remote,
            "-p",
            old,
            "-p",
            b2,
            "-m",
            "octopus-revive-removed",
        ).stdout.strip()
        probe = self.run_hook(local, remote)
        self.assertEqual(probe.returncode, 1)
        self.assertIn("infrastructure identifier", probe.stderr)

    def test_side_commit_with_new_id_is_blocked(self):
        _commit(self.root, "base", {"f.txt": "base\n"})
        _git(self.root, "checkout", "-qb", "feature")
        remote = _commit(self.root, "side-plain", {"g.txt": "feat\n"})
        _commit(self.root, "side-id", {"s.txt": NEW_ID + "\n"})
        _git(self.root, "checkout", "-q", "main")
        _commit(self.root, "main-work", {"m.txt": "work\n"})
        main_tip = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        _git(self.root, "checkout", "-q", "feature")
        _git(self.root, "merge", "-q", main_tip, "-m", "merge-main")
        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        probe = self.run_hook(local, remote)
        self.assertEqual(probe.returncode, 1)
        self.assertIn("infrastructure identifier", probe.stderr)

    def test_added_line_starting_with_plus_is_scanned(self):
        # Review: a filter that drops "++..." lines also dropped added lines that start with "+".
        remote = _commit(self.root, "base", {"f.txt": "base\n"})
        local = _commit(self.root, "plus", {"p.txt": "+" + NEW_ID + "\n"})
        probe = self.run_hook(local, remote)
        self.assertEqual(probe.returncode, 1)
        self.assertIn("infrastructure identifier", probe.stderr)


class WireHooksStdout(unittest.TestCase):
    def test_wiring_notice_stays_off_stdout(self):
        # CI captures `quality.py run ... ` stdout into $GITHUB_OUTPUT; a notice there broke it.
        import contextlib
        import io

        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (Path(tmp) / ".githooks").mkdir()
            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertTrue(client_quality.wire_hooks(Path(tmp)))
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()


class WireHooksStrict(unittest.TestCase):
    def test_setup_fails_when_the_hooks_path_cannot_be_written(self):
        # pre-push runs the secret scan: `just setup` must not pass without it.
        import contextlib
        import io

        for module in (client_quality,):
            with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
                root = Path(tmp)
                subprocess.run(["git", "init", "--quiet", tmp], check=True)
                (root / ".githooks").mkdir()
                # Left behind by an interrupted git: config writes fail.
                (root / ".git" / "config.lock").write_text("")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertFalse(module.wire_hooks(root))
                    with self.assertRaises(SystemExit):
                        module.wire_hooks(root, strict=True)

    def test_non_utf8_hooks_path_only_warns(self):
        import contextlib
        import io

        for module in (client_quality,):
            with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
                root = Path(tmp)
                subprocess.run(["git", "init", "--quiet", tmp], check=True)
                (root / ".githooks").mkdir()
                with (root / ".git" / "config").open("ab") as config:
                    config.write(b"[core]\n\thooksPath = /hooks\xff\n")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertFalse(module.wire_hooks(root))
