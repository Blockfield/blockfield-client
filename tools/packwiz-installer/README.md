# Blockfield installer patch

Upstream: [packwiz-installer v0.5.14](https://github.com/packwiz/packwiz-installer/tree/7420866dfc6ae1f68079a166e9804b7ec31a59ca), MIT (see LICENSE).
The shipped JAR is v0.5.14-bf6. `installer.patch` contains the complete source change and runnable network checks
(`resumeTest`, `mirrorTest`).

## Mirrors

`mirrors.json` beside `pack.toml` lists download mirrors (the build writes it from the repository's `mirrors.json`):
`{"schema":1,"probe":{"path","hash"},"mirrors":[{"name","url"}]}`. In a mirror URL `{path}` is the file's URL relative
to the pack host (a pull CDN such as Gcore) and `{hash}` the hash the pack declares for it (content-addressed storage
such as R2 or GitHub release assets). Before the first download of a run the installer fetches the same 512 KiB probe
slice from every mirror (timed from the first response bytes), drops mirrors that fail or deliver under about
1 Mbit/s, and orders the rest by speed. The pack host is not probed: it is only the last resort. Each file tries the mirrors once each through a client without the shared
10/15/60 s timeout retries, so a stalled source (the Russian 16 KB cut of Cloudflare) hands over in seconds; the partial
carries over and the pack host resumes it with its usual three attempts. A mirror that fails is skipped for the rest of
the run; when a resumed file fails the hash, that mirror first gets one clean try, since the bad bytes may come from an
earlier source. A missing or invalid `mirrors.json` means the pack host alone, as before. Every file is still verified against
the pack's hash, so a mirror cannot change what is installed.

The installer prints `[blockfield-mirror] <name>[ <speed> MB/s]` whenever the run's current source changes; the
launcher shows it as the active mirror.

A mirror that passes the probe but then trickles below the same ~1 Mbit/s floor over a 10 s window hands over
mid-file too: the partial carries over and the next source resumes it. The pack-host fallback has no such watchdog,
so a slow link still completes. Every receive also prints throttled
`[blockfield-file] {"name":…,"received":…,"total":…|"null","source":…}` lines (JSON, at most one per 2 s
per file); the launcher parses them into the live "current file" row, so a large jar no longer looks stuck.

## Resume

Downloads stream to `.packwiz-downloads/*.partial` inside the game directory, instead of buffering whole files in RAM.
Three attempts use HTTP Range to continue from the persisted offset. Partials survive cancellation and launcher restarts;
the cache key includes the destination and expected content hash. A changed hash starts a separate download and removes
the previous partial for that destination. Successful files leave no partial behind.

Every complete file is verified with the pack's declared hash before replacing the installed file.
A server that ignores Range restarts the file safely; malformed ranges cannot append data. Hash failures discard the partial
and retry from zero. Local source packs stream to disk too, but do not use HTTP resume. The existing ten-file concurrency is retained.

Build with Java 17 supplied by native JDK (no global installs):

```sh
just check
```

The build pins the upstream commit, applies the patch, runs actual interrupted-HTTP checks, builds the distribution JAR,
tests two separate bootstrap processes, and updates only its hash in `launcher.json`.
`--check` rebuilds and compares without changing the shipped files. To test the shipped artifact alone:

```sh
just check
```

The existing launcher downloads the new installer when its SHA-256 changes; release via the normal modpack tag workflow.
The pack build takes the installer bytes from `launcher.json` `installer.source` (a release asset of this repository,
tag `installer-<version>`) until the pack host serves the same JAR at `installer.url`.
Existing Prism instances run the bootstrap with `--bootstrap-no-update` and keep the installer they were imported with
(direct downloads only); re-importing `Blockfield-prism.zip` brings the mirror-aware installer.
