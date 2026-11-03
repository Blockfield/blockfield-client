import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "rn", ROOT / "scripts/release-notes-tool.py"
)
rn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rn)


def pin(tag, sha, repo="Blockfield/x"):
    return {
        "name": "X",
        "filename": f"x-{tag}.jar",
        "repo": repo,
        "tag": tag,
        "hash": sha,
        "url": f"https://github.com/{repo}/releases/download/{tag}/x.jar",
    }


class PinDiff(unittest.TestCase):
    def changes(self, old, new):
        return [row[1] for row in rn.pin_diff(old, new)]

    def test_kinds(self):
        self.assertEqual(self.changes({}, {"x": pin("bf1", "a")}), ["added"])
        self.assertEqual(self.changes({"x": pin("bf1", "a")}, {}), ["removed"])
        self.assertEqual(
            self.changes({"x": pin("bf1", "a")}, {"x": pin("bf3", "b")}), ["update"]
        )
        self.assertEqual(
            self.changes({"x": pin("v1.10.0", "a")}, {"x": pin("v1.9.0", "b")}),
            ["rollback"],
        )
        self.assertEqual(
            self.changes({"x": pin("bf1", "a")}, {"x": pin("bf1", "b")}), ["re-pinned"]
        )
        self.assertEqual(
            self.changes({"x": pin("bf1", "a")}, {"x": pin("bf1", "a")}), []
        )
        self.assertEqual(
            self.changes({"x": pin("import-2.53.0", "a")}, {"x": pin("bf9", "b")}),
            ["update"],
        )
        self.assertEqual(
            self.changes({"x": pin("bf10", "a")}, {"x": pin("bf9", "b")}), ["rollback"]
        )

    def test_cross_family_moves_are_updates_not_rollbacks(self):
        # Настоящий регрессионный случай: старый код сравнивал кортежи
        # (1, 0, 1) < (2, 53, 0) и помечал прямое движение как откат.
        self.assertEqual(
            self.changes({"x": pin("import-2.53.0", "a")}, {"x": pin("v1.0.1", "b")}),
            ["update"],
        )
        # Обратное направление тоже вне порядка: семьи несравнимы.
        self.assertEqual(
            self.changes({"x": pin("bf9", "a")}, {"x": pin("import-2.53.0", "b")}),
            ["update"],
        )
        self.assertFalse(rn.comparable(rn.version_key("bf9"), rn.version_key("v1.0.1")))
        self.assertTrue(rn.comparable(rn.version_key("bf9"), rn.version_key("bf10")))
        self.assertTrue(
            rn.comparable(rn.version_key("v1.9.0"), rn.version_key("v1.10.0"))
        )

    def test_same_bytes_new_tag_is_url_change(self):
        moved = pin("import-2.53.0", "a", repo="Blockfield/y")
        self.assertEqual(
            self.changes({"x": pin("bf9", "a")}, {"x": moved}), ["url-change"]
        )

    def test_r2_move_preserves_component_release_provenance(self):
        old = pin("music-v3", "same", "Blockfield/blockfield-music")
        moved = rn.pin_from(
            "s3://blockfield-assets/music/music-v3/x.jar",
            "X",
            old["filename"],
            old["hash"],
            old["url"],
        )
        self.assertEqual((moved["repo"], moved["tag"]), (old["repo"], old["tag"]))
        self.assertEqual(self.changes({"x": old}, {"x": moved}), ["url-change"])


class Check(unittest.TestCase):
    def write(self, text):
        path = Path(tempfile.mkdtemp(), "release-notes.md")
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def test_published_notes_pass(self):
        for path in sorted(ROOT.glob("release-notes/*.md")):
            with self.subTest(path=path.name):
                self.assertEqual(
                    rn.check(
                        path, "Blockfield/blockfield-releases", path.stem, "client"
                    ),
                    [],
                )

    def test_skeleton_without_comparison_warns_instead_of_claiming_no_changes(self):
        text = rn.skeleton(
            "Blockfield/blockfield-releases",
            "v9.9.9",
            None,
            "client",
            "2026-09-27",
            (),
            False,
        )
        self.assertNotIn("Пины не изменились", text)
        self.assertIn("ВНИМАНИЕ", text)
        self.assertTrue(rn.check(self.write(text)))
        compared = rn.skeleton(
            "Blockfield/blockfield-releases",
            "v9.9.9",
            "v9.9.8",
            "client",
            "2026-09-27",
            (),
            True,
        )
        self.assertIn("Пины не изменились", compared)

    def test_rejects_skeleton_wrong_version_and_missing_file(self):
        text = rn.skeleton(
            "Blockfield/blockfield-releases",
            "v9.9.9",
            "v9.9.8",
            "client",
            "2026-09-27",
            compared=True,
        )
        path = self.write(text)
        self.assertTrue(any("скелет" in e for e in rn.check(path)))
        filled = text.replace("TODO", "Заполнено")
        self.assertEqual(
            rn.check(self.write(filled), version="v9.9.9", kind="client"), []
        )
        self.assertTrue(rn.check(self.write(filled), version="v9.9.10"))
        self.assertTrue(rn.check(Path(tempfile.mkdtemp(), "absent.md")))


if __name__ == "__main__":
    unittest.main()
