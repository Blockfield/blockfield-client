"""scripts/bump-fork.sh pins fork releases, inside the pinned client checkout."""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAR = b"fake jar bytes"

# gh stand-in: records its calls, answers the few queries bump-fork.sh makes.
GH = textwrap.dedent("""\
    #!/bin/sh
    echo "$*" >> "$GH_LOG"
    case "$1 $2" in
      "api repos/"*"/git/ref/tags/"*) exit 1 ;;
      "api repos/"*"/commits/"*) echo 0123456789abcdef ;;
      "api repos/"*"/git/refs") ;;
      "run list") echo 42 ;;
      "run watch") ;;
      "release view") printf '%s\\n' "$GH_ASSET" ;;
      "release download")
        while [ $# -gt 0 ]; do [ "$1" = -D ] && dir=$2; [ "$1" = -p ] && name=$2; shift; done
        printf 'fake jar bytes' > "$dir/$name" ;;
      *) echo "unexpected gh $*" >&2; exit 2 ;;
    esac
""")


class BumpForkTest(unittest.TestCase):
    def run_script(self, *args, asset, meta=None):
        with tempfile.TemporaryDirectory() as tmp:
            repo, bin_dir = Path(tmp) / "pack", Path(tmp) / "bin"
            (repo / "scripts").mkdir(parents=True)
            (repo / "mods").mkdir(parents=True)
            (repo / ".tools").mkdir()
            bin_dir.mkdir()
            shutil.copy(ROOT / "scripts/bump-fork.sh", repo / "scripts/bump-fork.sh")
            (repo / ".tools/packwiz").write_text(
                '#!/bin/sh\necho refreshed >> "$GH_LOG"\n',
                encoding="utf-8",
                newline="\n",
            )
            (bin_dir / "gh").write_text(GH, encoding="utf-8", newline="\n")
            for f in (repo / ".tools/packwiz", bin_dir / "gh"):
                f.chmod(0o755)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            if meta:
                (repo / "mods" / meta[0]).write_text(
                    meta[1], encoding="utf-8", newline="\n"
                )
            log = Path(tmp) / "gh.log"
            env = dict(
                os.environ,
                PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                GH_LOG=str(log),
                GH_ASSET=asset,
            )
            result = subprocess.run(
                ["sh", str(repo / "scripts/bump-fork.sh"), *args],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            pins = {p.name: p.read_text() for p in (repo / "mods").glob("*.pw.toml")}
            return pins, log.read_text()

    def test_existing_pin_keeps_its_title_and_skips_the_sources_jar(self):
        meta = (
            "parcool.pw.toml",
            'name = "Own title"\nfilename = "ParCool-1.0.0+mc1.21.1-bf1.jar"\n',
        )
        name = "ParCool-1.0.0+mc1.21.1-bf2.jar"
        # The query itself filters -sources.jar out; the stand-in returns what that query would.
        pins, calls = self.run_script("parcool", "bf2", asset=name, meta=meta)
        self.assertNotIn(
            "git/refs", calls, "an existing release is pinned without tagging"
        )
        self.assertIn(
            'select(endswith(".jar") and (endswith("-sources.jar") | not))',
            (ROOT / "scripts/bump-fork.sh").read_text(),
        )
        self.assertTrue(pins["parcool.pw.toml"].startswith('name = "Own title"\n'))
        self.assertIn("/bf2/", pins["parcool.pw.toml"])

    def test_other_forks_still_tag_main(self):
        meta = (
            "superbwarfare.pw.toml",
            'name = "SBW"\nfilename = "superbwarfare-0.8.9.1-mc1.21.1-bf18.jar"\n',
        )
        pins, calls = self.run_script(
            "superbwarfare", asset="superbwarfare-0.8.9.1-mc1.21.1-bf19.jar", meta=meta
        )
        self.assertIn("repos/Blockfield/superbwarfare-fabric/commits/main", calls)
        self.assertIn("refs/tags/bf19", calls)


if __name__ == "__main__":
    unittest.main()
