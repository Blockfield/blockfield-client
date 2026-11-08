"""The pack ships the tested immutable Raknetify build.

mods/raknetify.pw.toml names a fixed fork release and file and carries its
sha512, so a moved tag or a replaced asset fails the pack build instead of
silently changing the login transport. Change TESTED only after a stand run of
RakNet (both ports) and TCP logins, together with the proxy pin of the same
release (blockfield-proxy distribution/Dockerfile.proxy).
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTED = (
    "0.1.0+alpha.5.1-bf3",
    "0448dc7581463587f5d51fbfcaf7a9f970b55b7d234b85116c278278533dbe8d"
    "81e99ba687f3bee20001e4f533377c81f79bc78710f0e0ab4ee5c4e79cc87875",
)
URL = re.compile(
    r"https://github\.com/Blockfield/raknetify/releases/download/(?P<tag>bf\d+)/"
    r"(?P<file>raknetify-fabric-(?P<version>[^/]+)-all\.jar)"
)


class RaknetifyPinTest(unittest.TestCase):
    def test_pack_pins_the_tested_raknetify_build(self):
        meta = tomllib.loads(
            (ROOT / "mods/raknetify.pw.toml").read_text(encoding="utf-8")
        )
        pin = URL.fullmatch(meta["download"]["url"])
        self.assertIsNotNone(pin, "Raknetify must come from a fixed fork release")
        self.assertEqual(meta["download"]["hash-format"], "sha512")
        self.assertEqual((pin["version"], meta["download"]["hash"]), TESTED)
        self.assertTrue(pin["version"].endswith("-" + pin["tag"]), "tag matches jar")
        self.assertEqual(meta["filename"], pin["file"])
        self.assertIn(
            "Raknetify `" + TESTED[0] + "`",
            (ROOT / "NOTICE.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
