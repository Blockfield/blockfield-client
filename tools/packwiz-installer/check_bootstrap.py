#!/usr/bin/env python3
"""Exercise the real bootstrap + installer across two separate Java processes."""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--installer", type=Path, default=ROOT / "packwiz-installer.jar"
    )
    args = parser.parse_args()
    payload = bytes(range(256)) * 8192
    digest = lambda value: hashlib.sha256(value).hexdigest()
    resources = {}
    state = {"mode": "drop", "ranges": [], "bytes": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/fixture.jar":
                header = self.headers.get("Range")
                state["ranges"].append(header)
                if state["mode"] == "fail":
                    self.send_response(503)
                    self.end_headers()
                    return
                offset = (
                    int(header.removeprefix("bytes=").removesuffix("-"))
                    if header
                    else 0
                )
                self.send_response(206 if offset else 200)
                self.send_header("Content-Length", str(len(payload) - offset))
                if offset:
                    self.send_header(
                        "Content-Range",
                        f"bytes {offset}-{len(payload) - 1}/{len(payload)}",
                    )
                self.end_headers()
                if state["mode"] == "drop":
                    state["mode"] = "fail"
                    self.wfile.write(payload[: len(payload) // 2])
                    self.wfile.flush()
                    state["bytes"] += len(payload) // 2
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                else:
                    self.wfile.write(payload[offset:])
                    state["bytes"] += len(payload) - offset
            else:
                data = resources.get(self.path)
                self.send_response(200 if data is not None else 404)
                self.send_header("Content-Length", str(len(data or b"")))
                self.end_headers()
                if data is not None:
                    self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    metadata = (
        f'name = "Fixture"\nfilename = "fixture.jar"\nside = "client"\n'
        f'[download]\nurl = "{base}/fixture.jar"\nhash-format = "sha256"\nhash = "{digest(payload)}"\n'
    ).encode()
    config = b"pack default"
    root_config = b'{"updated": true}'
    index = (
        f'hash-format = "sha256"\n[[files]]\nfile = "mods/fixture.pw.toml"\n'
        f'hash = "{digest(metadata)}"\nmetafile = true\n'
        f'[[files]]\nfile = "launcher.json"\nhash = "{digest(root_config)}"\n'
        f'[[files]]\nfile = "config/user.txt"\nhash = "{digest(config)}"\npreserve = true\n'
    ).encode()
    resources.update(
        {
            "/launcher.json": root_config,
            "/mods/fixture.pw.toml": metadata,
            "/config/user.txt": config,
            "/index.toml": index,
            "/pack.toml": (
                f'name = "Resume fixture"\npack-format = "packwiz:1.1.0"\n[index]\n'
                f'file = "index.toml"\nhash-format = "sha256"\nhash = "{digest(index)}"\n'
            ).encode(),
        }
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    java = (
        str(Path(os.environ["JAVA_HOME"]) / "bin/java")
        if "JAVA_HOME" in os.environ
        else "java"
    )
    try:
        with tempfile.TemporaryDirectory(
            prefix="blockfield-bootstrap-check-"
        ) as temporary:
            game = Path(temporary)
            (game / "launcher.json").write_bytes(b"old root config")
            (game / "mods").mkdir()
            (game / "mods/fixture.jar").write_bytes(b"previous installed file")
            (game / "config").mkdir()
            (game / "config/user.txt").write_bytes(b"player settings")
            command = [
                java,
                "-Djava.awt.headless=true",
                "-jar",
                str(ROOT / "packwiz-installer-bootstrap.jar"),
                "--bootstrap-no-update",
                "--bootstrap-main-jar",
                str(args.installer.resolve()),
                "-g",
                "-s",
                "client",
                "--pack-folder",
                ".",
                f"{base}/pack.toml",
            ]
            first = subprocess.run(command, cwd=game, capture_output=True, text=True)
            assert first.returncode != 0, first.stdout + first.stderr
            partials = list((game / ".packwiz-downloads").glob("*.partial"))
            assert len(partials) == 1, first.stdout + first.stderr
            assert partials[0].stat().st_size == len(payload) // 2
            assert (
                game / "mods/fixture.jar"
            ).read_bytes() == b"previous installed file"
            state["mode"] = "healthy"
            state["ranges"].clear()
            second = subprocess.run(command, cwd=game, capture_output=True, text=True)
            assert second.returncode == 0, second.stdout + second.stderr
            assert (game / "launcher.json").read_bytes() == root_config
            assert state["ranges"] == [f"bytes={len(payload) // 2}-"]
            assert (game / "mods/fixture.jar").read_bytes() == payload
            assert (game / "config/user.txt").read_bytes() == b"player settings"
            assert not list((game / ".packwiz-downloads").glob("*.partial"))
            assert state["bytes"] == len(payload)
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "processes": 2,
                        "file_bytes": len(payload),
                        "network_bytes": state["bytes"],
                        "resumed_range": state["ranges"][0],
                        "preserved": "installed file on failure, player settings",
                    }
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
