"""Refuse unsafe releases and wait for the independently running Game/Workshop pair.

Workshop-only mode (preflight-workshop / wait-workshop, workshop_only=True)
stages and verifies the approved Workshop target without requiring a running
Game: sleeping with a valid target is staged-offline, a busy or failed editor
defers activation until the deadline. It never claims public pair compatibility.

Game-only mode (wait-game / wait-rollback-game, game_only=True) verifies the
running Game container, image digest and READY record without reading the
Workshop controller or Workshop data at all: a down, asleep or mid-transition
Workshop never blocks a Game deploy or its rollback.

Host paths and service names come from the environment (set on the deploy host):
BLOCKFIELD_WORKSHOP_STATUS, BLOCKFIELD_BACKEND_LOCK, BLOCKFIELD_GAME_SERVICE,
BLOCKFIELD_GAME_DATA, BLOCKFIELD_WORKSHOP_SERVICE, BLOCKFIELD_WORKSHOP_DATA.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID


def _required_env(name):
    # Host-specific deployment config lives on the deploy host, not in this repo.
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


STATUS = Path(_required_env("BLOCKFIELD_WORKSHOP_STATUS"))
LOCK = Path(_required_env("BLOCKFIELD_BACKEND_LOCK"))
APPS = {
    "game": (
        _required_env("BLOCKFIELD_GAME_SERVICE"),
        Path(_required_env("BLOCKFIELD_GAME_DATA")),
    ),
    "workshop": (
        _required_env("BLOCKFIELD_WORKSHOP_SERVICE"),
        Path(_required_env("BLOCKFIELD_WORKSHOP_DATA")),
    ),
}
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
HEX = re.compile(r"[0-9a-f]{64}")
# Same as the Workshop controller's releases.CLIENT_VERSION: modpack builds omit the "v".
CLIENT_VERSION = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+")
SERVER_REPOSITORY = "ghcr.io/blockfield/blockfield-server"
IMAGE_DIGEST = re.compile(re.escape(SERVER_REPOSITORY) + r"@sha256:[0-9a-f]{64}")
DEADLINE = 1200
POLL = 10


class Pending(ValueError):
    """A valid rollout is still waiting for actual readiness."""


def _release(value):
    """A controller release identity: a tag (deprecated) or an image digest."""
    return isinstance(value, str) and (
        TAG.fullmatch(value) is not None or DIGEST.fullmatch(value) is not None
    )


def _record_match(release, record):
    """Whether a goal release names its approved record: the tag or the image digest."""
    return (
        release == record["release"]
        or release == record["imageDigest"].split("@", 1)[1]
    )


def _observed(release, actual, digest):
    """A controller-observed release naming the running build: its tag or the deployed digest."""
    return release == actual or (digest is not None and release == digest)


def read_json(path, missing=False, limit=16384):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError(f"Symlink refused: {path.name}")
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError:
        if missing:
            return None
        raise Pending(f"Missing {path.name}") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"Invalid JSON file: {path.name}")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"JSON too large: {path.name}")
    document = json.loads(data, object_pairs_hook=_unique)
    if not isinstance(document, dict):
        raise ValueError(f"Expected object: {path.name}")
    return document


def docker(*args, input=None):
    return subprocess.run(
        ["docker", *args],
        input=input,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def controller(workshop_only=False):
    state = read_json(STATUS, missing=True)
    if state is None:
        return None
    if (
        type(state.get("schema")) is not int
        or state["schema"] != 1
        or type(state.get("revision")) is not int
        or state["revision"] < 0
    ):
        raise ValueError("Invalid Workshop status")
    phase = state.get("phase")
    if phase in {"REQUESTED", "DRAINING", "SAVING", "STARTING"}:
        raise Pending(f"Workshop is {phase}")
    if phase == "game" and "game" not in state and state.get("gate") == "public":
        if workshop_only:
            raise ValueError("Workshop-only deploy requires a Workshop target")
        return state  # Migration from the former exclusive controller.
    if phase == "sleeping":
        # Cold Workshop: 0 replicas; admin/status.json reports gate "sleeping" and desired
        # keeps the current goal.
        if state.get("gate") != "sleeping":
            raise ValueError("Sleeping Workshop must report the sleeping gate")
    elif phase != "workshop" or state.get("gate") != "builders":
        if workshop_only and phase in {"test", "ERROR"}:
            # Deferred activation: wait() converges on it up to its deadline.
            raise Pending(f"Workshop is {phase}; activation deferred")
        raise ValueError(
            "Deploy requires a stable Workshop editor; test/error/unknown state refused"
        )
    goal = state.get("desired")
    game = state.get("game")
    if (
        not isinstance(goal, dict)
        or goal.get("profile") != "workshop"
        or not canonical_uuid(goal.get("session"))
    ):
        raise ValueError("Invalid selected Workshop session")
    if workshop_only:
        if not _release(goal.get("release")):
            raise ValueError("Invalid Workshop target release")
        return state
    if (
        not isinstance(game, dict)
        or game.get("phase") != "game"
        or game.get("gate") != "public"
    ):
        if state.get("phase") == "workshop":
            # The container controller has no docker.sock (policy) and never observes
            # Game since the W07 cut-over; Game readiness is verified directly from
            # its container and READY record in snapshot() instead.
            state.pop("game", None)
        else:
            raise Pending("Game readiness has not been observed by the controller")
    if any(
        not _release(document.get("release"))
        for document in ((goal,) if "game" not in state else (goal, state["game"]))
    ):
        raise ValueError("Invalid controller release")
    return state


# Publication evidence contract from blockfield-workshop#32 (d506e909).
# Keep this standalone verifier usable without installing the controller.
def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate release metadata key")
        result[key] = value
    return result


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _image_evidence(source, digest, release):
    manifest_bytes = source["imageManifest"].encode()
    if (
        "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        != digest.split("@", 1)[1]
    ):
        raise ValueError("published image manifest digest mismatch")
    manifest = json.loads(manifest_bytes, object_pairs_hook=_unique)
    if "manifests" in manifest:
        platform_bytes = source["platformManifest"].encode()
        platform_digest = "sha256:" + hashlib.sha256(platform_bytes).hexdigest()
        platforms = [
            entry
            for entry in manifest["manifests"]
            if entry.get("platform") == {"architecture": "amd64", "os": "linux"}
        ]
        if len(platforms) != 1 or platforms[0]["digest"] != platform_digest:
            raise ValueError("published linux/amd64 manifest binding mismatch")
        manifest = json.loads(platform_bytes, object_pairs_hook=_unique)
    if manifest.get("schemaVersion") != 2 or not manifest.get("layers"):
        raise ValueError("published image manifest required")
    config_bytes = source["imageConfig"].encode()
    if (
        manifest["config"]["digest"]
        != "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    ):
        raise ValueError("published image config checksum mismatch")
    config = json.loads(config_bytes, object_pairs_hook=_unique)
    labels = config["config"]["Labels"]
    if (
        labels.get("org.opencontainers.image.source")
        != "https://github.com/Blockfield/blockfield-server"
        or labels.get("io.blockfield.server-digest") != release
    ):
        raise ValueError("published image source/release mismatch")


def validate_release_record(document: dict) -> None:
    """Check publication, checksum, build and image provenance consistency."""
    if not isinstance(document, dict):
        raise ValueError("release record must be an object")
    if (
        type(document.get("schema")) is not int
        or document["schema"] != 1
        or type(document.get("revision")) is not int
        or document["revision"] < 1
    ):
        raise ValueError("unsupported release record schema/revision")
    release, digest = document.get("release"), document.get("imageDigest")
    if not _matches(TAG, release):
        raise ValueError("release record needs a published vX.Y.Z tag")
    if not _matches(IMAGE_DIGEST, digest):
        raise ValueError("release record needs an immutable server imageDigest")
    for key in ("serverRuntime", "clientContract", "source"):
        if not isinstance(document.get(key), dict) or not document[key]:
            raise ValueError(f"release record needs {key} metadata")
    source = document["source"]
    if (
        source.get("repository") != "Blockfield/blockfield-server"
        or source.get("tag") != release
        or source.get("draft") is not False
        or source.get("prerelease") is not False
        or not _matches(re.compile(r"[0-9a-f]{40}"), source.get("commit"))
        or source["commit"] == "0" * 40
    ):
        raise ValueError("invalid published release provenance")
    try:
        published = datetime.fromisoformat(source["publishedAt"].replace("Z", "+00:00"))
        if published.tzinfo is None:
            raise ValueError("publication timestamp must include timezone")
        payload, sums = source["buildJson"], source["checksums"]
        if not isinstance(payload, str) or not isinstance(sums, str):
            raise ValueError("original build.json and SHA256SUMS required")
        build = json.loads(payload, object_pairs_hook=_unique)
        checksums = {}
        for line in sums.splitlines():
            checksum, name = line.split("  ", 1)
            if not _matches(HEX, checksum) or name in checksums:
                raise ValueError("invalid or duplicate publication checksum")
            checksums[name] = checksum
        build_sha = hashlib.sha256(payload.encode()).hexdigest()
        if checksums.get("build.json") != build_sha:
            raise ValueError("published build.json checksum mismatch")
        _image_evidence(source, digest, release)
        provenance = source["provenance"]
        if provenance != {
            "imageDigest": digest,
            "sourceCommit": source["commit"],
            "buildSha256": build_sha,
        }:
            raise ValueError("image/build provenance mismatch")
        if build["build"] != release or build["sources"]["server"] != source["commit"]:
            raise ValueError("published build release/source mismatch")
        for key in ("server_runtime", "server_digest", "client", "compatibility"):
            if not _matches(HEX, build[key]) or build[key] == "0" * 64:
                raise ValueError("invalid published build fingerprint/contract")
        if not _matches(CLIENT_VERSION, build["client_version"]):
            raise ValueError("invalid published client version")
        for key in ("minecraft", "fabric"):
            if not _matches(re.compile(r"[0-9]+(?:\.[0-9]+){1,3}"), build[key]):
                raise ValueError("invalid published runtime version")
        if document["serverRuntime"] != {
            "fingerprint": build["server_runtime"],
            "serverDigest": build["server_digest"],
            "minecraft": build["minecraft"],
            "fabric": build["fabric"],
        } or document["clientContract"] != {
            "digest": build["client"],
            "version": build["client_version"],
            "compatibility": build["compatibility"],
        }:
            raise ValueError(
                "release runtime/client metadata differs from published build"
            )
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(f"invalid publication evidence: {error}") from error


def approved_target():
    """Read W02's private, revision-bound controller projection."""
    try:
        record = read_json(
            STATUS.parent.parent / "controller/target.json", limit=262144
        )
    except Pending:
        raise ValueError("Missing approved Workshop target") from None
    validate_release_record(record)
    if (
        not canonical_uuid(record.get("operation"))
        or not canonical_uuid(record.get("runtimeOperation"))
        or type(record.get("stateRevision")) is not int
        or type(record.get("selectionRevision")) is not int
        or not 1 <= record["selectionRevision"] <= record["stateRevision"]
        or "previousTarget" not in record
    ):
        raise ValueError("Invalid revision-bound Workshop target")
    if record["previousTarget"] is not None:
        validate_release_record(record["previousTarget"])
    return record


def ready_record(profile, tag):
    ready = read_json(APPS[profile][1] / ".blockfield-ready.json")
    if set(ready) != {"profile", "generation", "session", "release"}:
        raise ValueError(f"Invalid {profile} READY record")
    if ready["profile"] != profile or ready["release"] != tag:
        raise Pending(f"{profile} READY does not report {tag}")
    if profile == "game":
        if ready["session"] is not None:
            raise ValueError("Game READY carries an editor session")
        if ready["generation"] is not None and not canonical_uuid(ready["generation"]):
            raise ValueError("Invalid Game generation")
    elif not canonical_uuid(ready["generation"]) or not canonical_uuid(
        ready["session"]
    ):
        raise ValueError("Invalid Workshop generation/session")
    selected = read_json(LOCK / profile / "profile.json", missing=profile == "game")
    if selected is None:
        if ready["generation"] is not None:
            raise Pending("Game READY has no selected generation")
    elif (
        type(selected.get("schema")) is not int
        or selected["schema"] != 1
        or any(
            selected.get(key) != ready[key]
            for key in ("profile", "generation", "session")
        )
    ):
        raise Pending(f"{profile} READY differs from selected generation/session")
    elif profile == "workshop" and selected.get("release") != tag:
        raise Pending("Workshop selected release differs from READY")
    return ready


def snapshot(tag=None, digest=None, workshop_only=False, game_only=False):
    if tag is not None and DIGEST.fullmatch(tag):
        tag = None  # digest-addressed check: the image digest binds the release
    if game_only and workshop_only:
        raise ValueError("game_only and workshop_only are mutually exclusive")
    # Game-only checks never read the Workshop controller: there is nothing to
    # consult, so a missing/down/mid-transition Workshop cannot block Game.
    state = None if game_only else controller(workshop_only=workshop_only)
    if workshop_only:
        if state is None or state.get("phase") == "game":
            raise ValueError("Workshop-only deploy requires a Workshop target")
        target = approved_target()
        selection = state.get("selection")
        if (
            not isinstance(selection, dict)
            or type(selection.get("revision")) is not int
            or type(selection.get("deferred")) is not bool
            or target["stateRevision"] != state["revision"]
            or target["runtimeOperation"] != state.get("operation")
            or target["operation"] != selection.get("operation")
            or target["selectionRevision"] != selection["revision"]
        ):
            raise Pending("Workshop approval differs from controller selection")
        if tag is not None and target["release"] != tag:
            raise Pending("Workshop target has not converged")
        if digest is not None:
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("Expected immutable image digest")
            if target["imageDigest"] != SERVER_REPOSITORY + "@" + digest:
                raise Pending("Workshop target digest has not converged")
        if state.get("phase") == "sleeping":
            # Staged-offline: the approved target is valid; nothing runs yet.
            # Wake later uses it. Never reported as READY. Positively verify
            # that no Workshop container is running and that the selection
            # did not change while verifying.
            if docker(
                "ps",
                "--no-trunc",
                "--filter",
                "label=com.docker.swarm.service.name=" + APPS["workshop"][0],
                "--format",
                "{{.ID}}",
            ).strip():
                raise Pending("Workshop is running; sleeping observation is stale")
            if controller(workshop_only=True) != state or approved_target() != target:
                raise Pending(
                    "Controller selection changed during readiness verification"
                )
            return {
                "workshop": {
                    "staged": True,
                    "release": target["release"],
                    "session": state["desired"].get("session"),
                    "digest": target["imageDigest"],
                }
            }
        if (
            not _record_match(state["desired"]["release"], target)
            or selection["deferred"]
        ):
            raise Pending("Workshop runtime has not activated the approved selection")
        # Preflight has no caller hints: approval still binds the observed image/build.
        tag = target["release"]
        digest = target["imageDigest"].split("@", 1)[1]
        profiles = ["workshop"]
    elif game_only:
        profiles = ["game"]
    elif state is not None and state.get("phase") == "sleeping":
        profiles = ["game"]
    elif state is not None and "game" in state:
        profiles = ["game", "workshop"]
    else:
        profiles = ["game"]
    # Since W07 the container controller never observes Game (no docker.sock) and the
    # stable editor runs its own :approved image/release, so a Game deploy verifies
    # Game alone; controller() has already refused a Workshop mid-transition.
    independent = (
        state is not None and state.get("phase") == "workshop" and "game" not in state
    )
    if (
        "workshop" not in profiles
        and not independent
        and not game_only
        and docker(
            "ps",
            "--no-trunc",
            "--filter",
            "label=com.docker.swarm.service.name=" + APPS["workshop"][0],
            "--format",
            "{{.ID}}",
        ).strip()
    ):
        raise ValueError(
            "Workshop is running without an independent controller contract"
        )
    apps = {}
    for profile in profiles:
        ids = docker(
            "ps",
            "--no-trunc",
            "--filter",
            "label=com.docker.swarm.service.name=" + APPS[profile][0],
            "--format",
            "{{.ID}}",
        ).splitlines()
        if len(ids) != 1 or not HEX.fullmatch(ids[0]):
            raise Pending(f"Expected exactly one running {profile} container")
        container = json.loads(docker("inspect", ids[0]))[0]
        if (
            not container["State"].get("Running")
            or container["State"].get("Health", {}).get("Status") != "healthy"
        ):
            raise Pending(f"{profile} container is not healthy")
        environment = dict(
            value.split("=", 1)
            for value in container["Config"].get("Env", [])
            if value.startswith(("BLOCKFIELD_PROFILE=", "SERVER_PORT="))
        )
        if (
            container["Config"].get("WorkingDir") != "/data"
            or environment.get("BLOCKFIELD_PROFILE", "game") != profile
        ):
            raise ValueError(f"Unexpected {profile} container profile/data directory")
        data_mounts = [
            mount
            for mount in container.get("Mounts", [])
            if mount.get("Destination") == "/data"
        ]
        if (
            len(data_mounts) != 1
            or data_mounts[0].get("Type") != "bind"
            or data_mounts[0].get("Source") != APPS[profile][1].as_posix()
            or data_mounts[0].get("RW") is not True
        ):
            raise ValueError(f"Unexpected {profile} data mount")
        port = environment.get("SERVER_PORT", "25565")
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError(f"Invalid {profile} server port")
        apps[profile] = {
            "id": ids[0],
            "container": container,
            "port": environment.get("SERVER_PORT", "25565"),
        }
    primary = apps["game"] if "game" in apps else apps["workshop"]
    build = json.loads(
        docker("exec", primary["id"], "cat", "/opt/blockfield-build.json")
    )
    actual = build.get("build")
    if (
        not isinstance(actual, str)
        or not TAG.fullmatch(actual)
        or not isinstance(build.get("server_runtime"), str)
        or not HEX.fullmatch(build["server_runtime"])
    ):
        raise ValueError(
            f"Invalid running {'Game' if 'game' in apps else 'Workshop'} build/fingerprint"
        )
    if tag is not None and actual != tag:
        raise Pending(
            f"{'Game' if 'game' in apps else 'Workshop'} runs {actual}, waiting for {tag}"
        )
    if digest is not None:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Expected immutable image digest")
        image = json.loads(docker("image", "inspect", primary["container"]["Image"]))[0]
        if SERVER_REPOSITORY + "@" + digest not in (image.get("RepoDigests") or []):
            raise Pending(
                f"{'Game' if 'game' in apps else 'Workshop'} image does not carry the requested digest"
            )
    if workshop_only and (
        target["serverRuntime"]
        != {
            "fingerprint": build.get("server_runtime"),
            "serverDigest": build.get("server_digest"),
            "minecraft": build.get("minecraft"),
            "fabric": build.get("fabric"),
        }
        or target["clientContract"]
        != {
            "digest": build.get("client"),
            "version": build.get("client_version"),
            "compatibility": build.get("compatibility"),
        }
    ):
        raise Pending("Workshop build differs from approved runtime/client metadata")
    for profile, app in apps.items():
        if (
            app["container"]["Image"] != primary["container"]["Image"]
            or app["port"] != primary["port"]
        ):
            raise Pending("Workshop image/port differs from Game")
        app["build"] = build
        ready = ready_record(profile, actual)
        if profile == "workshop":
            goal = state["desired"]
            if workshop_only:
                # Staged digest goal: the approved record binds the running image.
                bound = (
                    _record_match(goal.get("release"), target)
                    and actual == target["release"]
                )
            else:
                bound = _observed(goal.get("release"), actual, digest)
            if (
                ready["session"] != goal["session"]
                or not bound
                or (
                    not workshop_only
                    and "game" in state
                    and not _observed(state["game"].get("release"), actual, digest)
                )
            ):
                raise Pending(
                    "Controller/Workshop READY do not bind the same release/session"
                )
    if state is not None and state.get("phase") == "sleeping":
        # The sleeping goal is not rewritten to follow a new Game; the next wake uses
        # the then-current Game digest. Only require the controller to observe this Game.
        if not _observed(state["game"].get("release"), actual, digest):
            raise Pending("Sleeping Workshop has not observed the running Game")
    if not game_only and (
        controller(workshop_only=workshop_only) != state
        or (workshop_only and approved_target() != target)
    ):
        raise Pending("Controller selection changed during readiness verification")
    return apps


def asleep():
    """Logging-only probe for a sleeping Workshop. Reads files, never wakes anything."""
    try:
        state = controller()
    except (ValueError, OSError):
        return False
    return state is not None and state.get("phase") == "sleeping"


def wait(tag, digest, rollback=False, workshop_only=False, game_only=False):
    if tag is not None and DIGEST.fullmatch(tag):
        tag = None  # digest-addressed wait: the image digest binds the release
    if (tag is not None and TAG.fullmatch(tag) is None) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", digest
    ):
        raise ValueError("Expected a release tag or digest and immutable image digest")
    label = tag if tag is not None else digest
    deadline = time.monotonic() + DEADLINE
    while True:
        try:
            apps = snapshot(
                tag, digest, workshop_only=workshop_only, game_only=game_only
            )
            if game_only:
                print("Ready:", ", ".join(apps), label)
                return
            if "workshop" not in apps and asleep():
                print(
                    f"Workshop is sleeping; Game ready for {label},"
                    " wake uses the current Game digest",
                    flush=True,
                )
            elif workshop_only and apps.get("workshop", {}).get("staged"):
                print(
                    f"Workshop target staged-offline for {label};"
                    " wake uses this target without a running Game",
                    flush=True,
                )
                return
            print("Ready:", ", ".join(apps), label)
            return
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            if (
                isinstance(error, ValueError)
                and not isinstance(error, Pending)
                and not rollback
            ):
                raise
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"{'Workshop target' if workshop_only else 'Game' if game_only else 'Game/Workshop pair'}"
                    f" did not become ready for {label}"
                ) from error
            print(
                f"Waiting for {'Workshop' if workshop_only else 'Game' if game_only else 'Game/Workshop'} {label}: {type(error).__name__}",
                flush=True,
            )
            time.sleep(min(POLL, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    if sys.argv[1:] == ["preflight"]:
        print("Stable deploy pair:", ", ".join(snapshot()))
    elif sys.argv[1:] == ["preflight-workshop"]:
        print("Workshop target:", ", ".join(snapshot(workshop_only=True)))
    elif len(sys.argv) == 4 and sys.argv[1] in {"wait", "wait-rollback"}:
        wait(sys.argv[2], sys.argv[3], rollback=sys.argv[1] == "wait-rollback")
    elif len(sys.argv) == 4 and sys.argv[1] in {"wait-game", "wait-rollback-game"}:
        wait(
            sys.argv[2],
            sys.argv[3],
            rollback=sys.argv[1] == "wait-rollback-game",
            game_only=True,
        )
    elif len(sys.argv) == 4 and sys.argv[1] == "wait-workshop":
        wait(sys.argv[2], sys.argv[3], workshop_only=True)
    else:
        raise SystemExit(
            "workshop-deploy.py preflight | preflight-workshop"
            " | wait[-rollback] <vX.Y.Z|sha256:digest> <sha256:digest>"
            " | wait-game[-rollback] <vX.Y.Z|sha256:digest> <sha256:digest>"
            " | wait-workshop <vX.Y.Z|sha256:digest> <sha256:digest>"
        )
