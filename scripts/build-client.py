#!/usr/bin/env python3
"""Build the client from pinned dependencies without server sources."""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import sys
import tomllib
import urllib.parse
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from packlib import (  # noqa: E402 - standalone trusted sibling import under Python -I.
    digest,
    distribution_index,
    get_bytes,
    git_head,
    inventory,
    resolve_mod,
    require_zip_compressor,
    toml_string,
    tree_digest,
    write_tar,
    write_tree,
    write_zip,
)

PUBLIC_FILES = (
    "Blockfield-client.zip",
    "Blockfield-prism.zip",
    "Blockfield.mrpack",
    "build.json",
)
WORKSHOP_FILES = (
    "Blockfield-workshop.zip",
    "Blockfield-workshop-prism.zip",
    "Blockfield-workshop.mrpack",
    "build.json",
)
WEB_TAR = "Blockfield-web.tar"
FILES_MTIME = 315532800


def workshop_selection(root):
    """Lean Workshop set: kept mod basenames + dropped config prefixes, or (None, None)."""
    keep, drops, section = set(), [], None
    for line in (root / "pack-workshop.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line in ("[mods]", "[drop-config]"):
            section = line
            continue
        if section == "[mods]":
            keep.add(line)
        elif section == "[drop-config]":
            drops.append(line)
        else:
            raise ValueError(f"pack-workshop.txt entry outside a section: {line}")
    if not keep:
        raise ValueError("pack-workshop.txt lists no mods")
    return keep, drops


def apply_workshop_selection(root, mods, defaults):
    keep, drops = workshop_selection(root)
    basenames = {name.rsplit("/", 1)[-1] for name, _ in mods}
    if unknown := sorted(keep - basenames):
        raise ValueError(f"Workshop set lists missing mods: {', '.join(unknown)}")
    # shortcut: dropping a config the game pack already removed is a no-op, not an error;
    # silently keeping a combat mod in the Workshop pack would be.
    kept = [(name, mod) for name, mod in mods if name.rsplit("/", 1)[-1] in keep]
    pruned = {
        name: data
        for name, data in defaults.items()
        if not any(name == p or name.startswith(p) for p in drops)
    }
    return kept, pruned


def workshop_inputs_hash(root, mods, protocol, versions):
    """Owner rule 2026-10-31 (1): the Workshop pack release gate.

    Hashes ONLY the Workshop pack's own inputs — the lean set pins (kept mods
    with their download hashes), the mod contract protocol and the platform
    versions. The pack version and every game-only pin are not inputs, so game
    releases never move it and never trigger a Workshop pack release.
    """
    selected = sorted(
        (
            name,
            mod.get("filename", ""),
            json.dumps(mod.get("download", {}), sort_keys=True),
        )
        for name, mod in mods
    )
    return digest(
        json.dumps(
            {
                "set": (root / "pack-workshop.txt").read_bytes().decode(),
                "pins": selected,
                "protocol": protocol,
                "versions": versions,
            },
            sort_keys=True,
        ).encode()
    )


def verify(out, resolved, defaults, names=PUBLIC_FILES):
    expected = {target: data for _, _, target, data in resolved}
    with zipfile.ZipFile(out / names[0]) as archive:
        actual = {
            n
            for n in archive.namelist()
            if n.startswith("mods/") and n.endswith(".jar")
        }
        if actual != set(expected) or "options.txt" in archive.namelist():
            raise ValueError("Client archive inventory differs")
        for name, data in expected.items():
            if archive.read(name) != data:
                raise ValueError(f"Client archive checksum mismatch: {name}")
    with zipfile.ZipFile(out / names[2]) as archive:
        manifest = json.loads(archive.read("modrinth.index.json"))
        if {f["path"] for f in manifest["files"]} != set(expected):
            raise ValueError("mrpack inventory differs")
        if "overrides/options.txt" in archive.namelist():
            raise ValueError("mrpack contains installer-only options seed")
        for name, data in defaults.items():
            if name != "options.txt" and archive.read(f"overrides/{name}") != data:
                raise ValueError(f"mrpack default differs: {name}")


def build(args):
    require_zip_compressor()
    root, out = args.root.resolve(), args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("Output directory must be empty")
    pack = tomllib.loads((root / "pack.toml").read_text())
    source_index = (root / "index.toml").read_bytes()
    if digest(source_index, pack["index"]["hash-format"]) != pack["index"]["hash"]:
        raise ValueError("Stale pack index checksum")
    mods, defaults = inventory(root)
    workshop = getattr(args, "variant", "game") == "workshop"
    if workshop:
        mods, defaults = apply_workshop_selection(root, mods, defaults)
    for name, mod in mods:
        if mod.get("side", "both") not in ("client", "both"):
            raise ValueError(f"Non-client mod in public inventory: {name}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        resolved = list(pool.map(lambda item: resolve_mod(root, item, {}), mods))
    if not workshop and not any(
        target.startswith("mods/blockfield-client-") for _, _, target, _ in resolved
    ):
        raise ValueError("Missing blockfield-client mod")
    protocol_file = root / "compatibility.json"
    protocol = (
        json.loads(protocol_file.read_text())["protocol"]
        if protocol_file.exists()
        else 1
    )
    contract = tree_digest(
        {
            target: data
            for _, mod, target, data in resolved
            if mod.get("side", "both") == "both"
        }
        | {name: data for name, data in defaults.items() if name.startswith("tacz/")}
        | {
            "versions": json.dumps(pack["versions"], sort_keys=True).encode(),
            "protocol": str(protocol).encode(),
        }
    )
    client = tree_digest(
        defaults
        | {"pack.toml[versions]": json.dumps(pack["versions"], sort_keys=True).encode()}
        | {target: data for _, _, target, data in resolved}
    )
    # Owner rule 2026-10-31 (1): the Workshop pack release gate, recorded only
    # for the workshop variant; the game build.json is untouched.
    pack_inputs = (
        workshop_inputs_hash(root, mods, protocol, pack["versions"])
        if workshop
        else None
    )
    defaults["config/blockfield-pack-version.txt"] = (client + "\n").encode()
    defaults["config/blockfield-pack-contract.txt"] = (contract + "\n").encode()
    archive_defaults = {
        name: data for name, data in defaults.items() if name != "options.txt"
    }
    web = out / "web"
    web.mkdir(parents=True)
    version, mc, loader = (
        pack["version"],
        pack["versions"]["minecraft"],
        pack["versions"]["fabric"],
    )
    base = args.public_base.rstrip("/")
    client_files, metadata, manifest = dict(archive_defaults), {}, []
    checks = {
        "version": version,
        "variant": "workshop" if workshop else "game",
        "client_version": version,
        "client": client,
        "compatibility": contract,
        "build": f"v{version}",
        "minecraft": mc,
        "fabric": loader,
        "mods": [],
        "sources": {"modpack": git_head(root)},
    }
    if workshop:
        checks["workshop_inputs"] = pack_inputs
    for name, mod, target, data in resolved:
        side = mod.get("side", "both")
        client_files[target] = data
        manifest.append(
            {
                "path": target,
                "hashes": {
                    "sha1": digest(data, "sha1"),
                    "sha512": digest(data, "sha512"),
                },
                "downloads": [f"{base}/files/{target}"],
                "fileSize": len(data),
                "env": {
                    "client": "required",
                    "server": "unsupported" if side == "client" else "required",
                },
            }
        )
        write_tree(web, {f"files/{target}": data})
        metadata[name] = (
            f"name = {toml_string(mod['name'])}\nfilename = {toml_string(mod['filename'])}\n"
            f'side = "{side}"\n\n[download]\nurl = "{base}/files/{target}"\n'
            f'hash-format = "sha512"\nhash = "{digest(data, "sha512")}"\n'
        ).encode()
        checks["mods"].append({"path": target, "side": side, "sha256": digest(data)})
    names = WORKSHOP_FILES if workshop else PUBLIC_FILES
    client_name, prism_name, mrpack_name = names[0], names[1], names[2]
    instance_name = "Blockfield-Workshop" if workshop else "Blockfield"
    client_files["ЧИТАЙ.txt"] = (
        f"{instance_name} {version}\nMinecraft {mc}, Fabric {loader}.\n"
        "Распакуйте в папку игры. Сервер: raknet;play.blockfield.pro:25566\n"
        f"Для автоматического обновления используйте Blockfield Launcher или {prism_name}.\n"
    ).encode()
    write_zip(out / client_name, client_files)
    mrpack = {f"overrides/{name}": data for name, data in archive_defaults.items()}
    mrpack["modrinth.index.json"] = json.dumps(
        {
            "formatVersion": 1,
            "game": "minecraft",
            "versionId": version,
            "name": instance_name,
            "dependencies": {"minecraft": mc, "fabric-loader": loader},
            "files": manifest,
        },
        indent=2,
    ).encode()
    write_zip(out / mrpack_name, mrpack)
    launcher = json.loads((root / "launcher.json").read_text())
    installers = {}
    for key, name in [
        ("bootstrap", "packwiz-installer-bootstrap.jar"),
        ("installer", "packwiz-installer.jar"),
    ]:
        spec = launcher[key]
        # "source" pins a new installer build before the pack host serves it at "url".
        data = get_bytes(spec.get("source", spec["url"]))
        if digest(data) != spec["sha256"]:
            raise ValueError(f"Installer checksum mismatch: {name}")
        installers[name] = data
    cfg = (
        f"[General]\nConfigVersion=1.3\nInstanceType=OneSix\nname={instance_name}\nOverrideCommands=true\n"
        f"PreLaunchCommand=$INST_JAVA -jar packwiz-installer-bootstrap.jar --bootstrap-no-update -g -s client {base}/pack.toml\n"
        "JoinServerOnLaunch=true\nJoinServerOnLaunchAddress=raknet;play.blockfield.pro:25566\n"
    )
    write_zip(
        out / prism_name,
        {
            "instance.cfg": cfg.encode(),
            **{f".minecraft/{name}": data for name, data in installers.items()},
            "mmc-pack.json": json.dumps(
                {
                    "formatVersion": 1,
                    "components": [
                        {"uid": "net.minecraft", "version": mc, "important": True},
                        {
                            "uid": "net.fabricmc.intermediary",
                            "version": mc,
                            "dependencyOnly": True,
                        },
                        {"uid": "net.fabricmc.fabric-loader", "version": loader},
                    ],
                }
            ).encode(),
        },
    )
    # Mirrors for packwiz-installer (tools/packwiz-installer); the largest file is the probe every
    # mirror and the pack host are timed on.
    probe = max(resolved, key=lambda item: len(item[3]))
    mirrors = {
        "schema": 1,
        "probe": {
            "path": "files/" + urllib.parse.quote(probe[2]),
            "hash": digest(probe[3], "sha512"),
        },
        "mirrors": json.loads((root / "mirrors.json").read_text()),
    }
    index = distribution_index(defaults, metadata, tomllib.loads(source_index.decode()))
    pack_text = re.sub(
        r'(?m)^hash = ".*"$',
        f'hash = "{digest(index.encode())}"',
        (root / "pack.toml").read_text(),
    )
    write_tree(
        web,
        defaults
        | metadata
        | installers
        | {
            "pack.toml": pack_text.encode(),
            "index.toml": index.encode(),
            "build.json": json.dumps(checks, indent=2).encode(),
            "mirrors.json": json.dumps(mirrors, indent=2).encode(),
        },
    )
    for name in ("launcher.json", "content.json"):
        shutil.copy2(root / name, web / name)
    verify(out, resolved, defaults, names)
    for name in (prism_name, mrpack_name):
        shutil.copy2(out / name, web / name)
    for path in [web / "files", *(web / "files").rglob("*")]:
        os.utime(path, (FILES_MTIME, FILES_MTIME))
    (web / "SHA256SUMS").write_text(
        "".join(
            f"{digest(((web if name == 'build.json' else out) / name).read_bytes())}  {name}\n"
            for name in names
        ),
        encoding="utf-8",
        newline="\n",
    )
    shutil.copy2(web / "SHA256SUMS", out / "SHA256SUMS")
    # The tar holds web/SHA256SUMS, so only the release copy of the sums can list the tar itself.
    write_tar(out / WEB_TAR, web, FILES_MTIME)
    with (out / "SHA256SUMS").open("a", encoding="utf-8", newline="\n") as sums:
        sums.write(f"{digest((out / WEB_TAR).read_bytes())}  {WEB_TAR}\n")
    print(
        f"Verified {len(resolved)} client mods; client {version}; digest {client}; compatibility {contract}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--public-base", default="https://blockfield.pro")
    parser.add_argument("--variant", choices=("game", "workshop"), default="game")
    build(parser.parse_args())
