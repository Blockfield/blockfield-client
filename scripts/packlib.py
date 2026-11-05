"""Packwiz inventory and reproducible client distribution primitives."""

import hashlib
import json
import io
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
import urllib.parse
import zipfile
import zlib
from pathlib import PurePosixPath


PUBLIC_R2_BASE_URL = "https://assets.blockfield.pro"

# A dead host must fail in seconds, not minutes: CI once hung on a pack fetch
# while the pack host was down. urllib applies this to the connect and to every
# read; retries stay for transient GitHub/R2 failures.
FETCH_TIMEOUT = 20


def require_zip_compressor():
    if (zlib.ZLIB_VERSION, zlib.ZLIB_RUNTIME_VERSION) != ("1.3.2", "1.3.2"):
        raise RuntimeError(
            "ZIP builds require zlib 1.3.2; run through just build (Python 3.12.14)"
        )


def toml_string(value):
    # json.dumps ломает TOML: эмодзи уезжает суррогатной парой \udXXX, которую парсер
    # packwiz-installer отвергает ("Kiwi 🥝" валил всю установку пака).
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(
        c if c >= " " and c != "\x7f" else f"\\u{ord(c):04X}" for c in escaped
    )
    return f'"{escaped}"'


def git_head(directory):
    return subprocess.check_output(
        ["git", "-C", str(directory), "rev-parse", "HEAD"], text=True
    ).strip()


def digest(data, algorithm="sha256"):
    return hashlib.new(algorithm.replace("-", ""), data).hexdigest()


def tree_digest(files):
    """Отпечаток содержимого дерева: одинаковые файлы — одинаковый отпечаток, независимо от порядка."""
    lines = sorted(f"{name}\t{digest(data)}" for name, data in files.items())
    return digest("\n".join(lines).encode())


def safe_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"Unsafe pack path: {value}")
    return str(path)


def retried(fetch, attempts=3):
    """Runners hit transient GitHub/R2 failures (HTTP 5xx, read timeouts); checksums still verify the bytes."""
    for attempt in range(1, attempts + 1):
        try:
            return fetch()
        except (OSError, subprocess.SubprocessError) as error:
            permanent = (
                isinstance(error, urllib.error.HTTPError)
                and error.code < 500
                and error.code != 429
            )
            if permanent or attempt == attempts:
                raise
            print(
                f"download failed ({error}), retry {attempt}/{attempts - 1}",
                file=sys.stderr,
            )
            time.sleep(10 * attempt)


def get_bytes(url):
    return retried(lambda: fetch_bytes(url))


def fetch_bytes(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "s3":
        if (
            parsed.netloc != "blockfield-assets"
            or parsed.query
            or parsed.fragment
            or not parsed.path.strip("/")
        ):
            raise ValueError("Unsupported R2 asset URL")
        key = safe_path(urllib.parse.unquote(parsed.path.lstrip("/")))
        url = f"{PUBLIC_R2_BASE_URL}/{urllib.parse.quote(key, safe='/')}"
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.hostname == "github.com"
        and len(parts) == 6
        and parts[2:4] == ["releases", "download"]
    ):
        repo = "/".join(parts[:2])
        tag, filename = map(urllib.parse.unquote, parts[4:])
        return subprocess.check_output(
            [
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                repo,
                "--pattern",
                filename,
                "--output",
                "-",
            ],
            timeout=600,
        )
    request = urllib.request.Request(url, headers={"User-Agent": "Blockfield-CI/1.0"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return response.read()


def inventory(root):
    index = tomllib.loads((root / "index.toml").read_text())
    mods, defaults = [], {}
    for entry in index["files"]:
        name = safe_path(entry["file"])
        if name.startswith(
            (
                "server/",
                "scripts/",
                "tests/",
                "tools/",
                ".github/",
                "proxy/",
                "distribution/",
            )
        ) or name in ("AGENTS.md", "CLAUDE.md"):
            raise ValueError(
                f"Development or server source leaked into client index: {name}"
            )
        data = (root / name).read_bytes()
        if digest(data, index["hash-format"]) != entry["hash"]:
            raise ValueError(f"Stale index checksum: {name}")
        if entry.get("metafile"):
            mod = tomllib.loads(data.decode())
            if mod.get("option", {}).get("optional"):
                raise ValueError(
                    f"Optional mod needs an explicit distribution policy: {name}"
                )
            if name.startswith("shaderpacks/"):
                if mod.get("side") != "client" or not mod["filename"].endswith(".zip"):
                    raise ValueError(f"Invalid client shader: {name}")
                _, _, target, archive = resolve_mod(root, (name, mod), {})
                if target in defaults:
                    raise ValueError(f"Duplicate shader: {target}")
                defaults[target] = archive
            else:
                mods.append((name, mod))
        else:
            defaults[name] = data
    load_resources(root, defaults)
    return mods, defaults


def load_resources(root, defaults):
    lock = root / "resources.lock.json"
    if lock.exists():
        for resource in json.loads(lock.read_text()):
            data = get_bytes(resource.get("r2-url", resource["url"]))
            if digest(data) != resource["sha256"]:
                raise ValueError("Resource archive checksum mismatch")
            prefix = resource["prefix"]
            # Контракт совместимости хеширует только tacz/; схемы WorldEdit в него не входят.
            if prefix not in (
                "tacz/gucci_vuitton_attachment/",
                "config/worldedit/schematics/",
            ):
                raise ValueError(f"Unsupported resource prefix: {prefix}")
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for entry in archive.infolist():
                    name = safe_path(entry.filename)
                    if entry.is_dir():
                        continue
                    if not name.startswith(prefix) or name in defaults:
                        raise ValueError(f"Unsafe or duplicate resource entry: {name}")
                    defaults[name] = archive.read(entry)


def resolve_mod(root, item, built):
    name, mod = item
    filename = safe_path(mod["filename"])
    target = str(PurePosixPath(name).parent / filename)
    download = mod["download"]
    # Версия в имени blockfield-*.jar меняется; собственные сборки сопоставляются по префиксу.
    prefix = next((p for p in built if filename.startswith(p)), None)
    if prefix is not None:
        data = built[prefix].read_bytes()
    else:
        local = root / "files" / target
        data = (
            local.read_bytes()
            if local.is_file()
            else get_bytes(download.get("r2-url", download["url"]))
        )
        if digest(data, download["hash-format"]) != download["hash"]:
            raise ValueError(f"Mod checksum mismatch: {target}")
    if not data.startswith(b"PK"):
        raise ValueError(f"Not a ZIP archive: {target}")
    return name, mod, target, data


def write_zip(path, files):
    require_zip_compressor()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(safe_path(name), (2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if name.endswith(".sh") else 0o644) << 16
            archive.writestr(info, data)


def write_tar(path, root, mtime):
    """Uncompressed tar of a directory tree: sorted, fixed owner, mode and mtime, so equal trees give equal bytes."""
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for item in sorted(
            root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()
        ):
            info = tarfile.TarInfo(item.relative_to(root).as_posix())
            info.mtime = mtime
            if item.is_dir():
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(info)
            else:
                info.size, info.mode = item.stat().st_size, 0o644
                with item.open("rb") as handle:
                    archive.addfile(info, handle)


def write_tree(root, files):
    for name, data in files.items():
        dest = root / safe_path(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def distribution_index(defaults, metadata, source_index):
    preserved = {
        entry["file"]
        for entry in source_index["files"]
        if entry.get("preserve") is True
        and not entry.get("metafile")
        and not entry["file"].startswith("mods/")
        and entry["file"] != "config/blockfield-pack-version.txt"
    }
    index = 'hash-format = "sha256"\n'
    for name, data in sorted((defaults | metadata).items()):
        index += f'\n[[files]]\nfile = {toml_string(name)}\nhash = "{digest(data)}"\n'
        if name in metadata:
            index += "metafile = true\n"
        elif name in preserved:
            index += "preserve = true\n"
    return index
