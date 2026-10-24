# Install and launch

## Command-file preview

Extract the complete `Auto-Outbound-Desktop-preview.zip`. Keep the command files beside `app/`.
Double-click `Open Auto Outbound.command` on macOS, or `Open Auto Outbound.cmd` on Windows.
PowerShell users can run `Open Auto Outbound.ps1`. Linux users can run `sh "Open Auto Outbound.sh"`.
The scripts select the appropriate bundled supervisor for x64 or ARM64.

The browser opens during setup. The supervisor downloads pinned Python and locked production dependencies into the OS application-data directory.
It uses the same runtime, session protection, instance reuse, and Quit contract as the native images.
First launch needs internet; customers do not install developer tools.
The app folder can be read-only after extraction.

macOS may ask before it opens a downloaded command file. Windows can apply local PowerShell policy.
Use your OS trust controls; do not disable system-wide security settings.
The `.cmd` entry invokes PowerShell for this process and keeps a failure message visible.

The Apple Silicon command launcher passed setup, actual browser opening, lead research, CSV/XLSX downloads, instance reuse, persistence, and Quit.
Other OS/architecture builds remain unverified. This requested script format is a preview; it does not establish full native M1a acceptance.

## Native ZIP edition

The release builder produces one ZIP with five launcher images and a shared `app/` directory. This repository's initial build is a **development build**. All five images compile. The Linux x86_64 AppImage has passed setup, workspace opening, instance reuse, restart persistence, and GUI Quit in a headless browser test. Native desktop acceptance on all six targets remains incomplete. See the verification status in the main README before relying on a platform.

1. Extract the complete ZIP into a permanent folder.
2. Open the launcher matching your computer. Keep it beside `app/`.
3. A setup page opens while a pinned Python runtime and locked dependencies download.
4. Setup opens the workspace when the local server is ready.

| Computer | Launcher |
|---|---|
| Mac, Apple Silicon or Intel | `Open Auto Outbound — macOS.app` |
| Windows, Intel/AMD | `Open Auto Outbound — Windows x64.exe` |
| Windows, ARM | `Open Auto Outbound — Windows ARM64.exe` |
| Linux, Intel/AMD | `Open Auto Outbound — Linux x86_64.AppImage` |
| Linux, ARM | `Open Auto Outbound — Linux ARM64.AppImage` |

The macOS launcher requires macOS 13 or later, matching the [minimum supported by its pinned Go toolchain](https://go.dev/doc/go1.27#linker).

First setup requires internet access. Customers do not need to install Python, pip, Go, Node.js, or a compiler to run a built ZIP. Real research does require an installed, authenticated Codex or Claude Code CLI; those subscriptions and provider services are separate from this application. Demo mode does not use an agent.

These images are unsigned. macOS Gatekeeper and Windows SmartScreen can show trust prompts. Linux file managers may require the launcher executable permission to be enabled. If the AppImage cannot use FUSE, start it with `--appimage-extract-and-run`; the adjacent `app/` path remains unchanged.

Use **Quit** in the app to stop its local server. Closing a browser tab leaves it running; opening the launcher again returns to the same instance.

## Source checkout

The source workflow is documented in the main README. It is intended for developers and CLI users who already have Python or `uv`. It is separate from the no-tooling requirement for a built native ZIP.

## Data and recovery

Data, documents, logs, managed Python, and cached dependencies live outside the extracted program folder:

| OS | Application data |
|---|---|
| macOS | `~/Library/Application Support/AutoOutbound/` |
| Windows | `%LOCALAPPDATA%\AutoOutbound\` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/AutoOutbound/` |

The launcher uses `data/`, `config/`, `logs/`, `python/`, `runtimes/`, `runtime/`, and `cache/` within this location. Keep the database and your offers private. Back up the data directory before deleting or moving it.

The setup page shows a short error code and log location. Network/TLS failures allow a bounded retry; missing or damaged release files require extracting a complete clean copy. The previous ready runtime is preserved when a dependency update fails. If the default browser cannot open, the launcher writes the exact local URL to `logs/OPEN_THIS_URL.txt`.

If an agent is not found, confirm its command works in a terminal. Finder and desktop launchers may inherit a different PATH. `AUTO_OUTBOUND_CODEX_BIN` and `AUTO_OUTBOUND_CLAUDE_BIN` can specify executable paths when launching from a terminal.

## Build the command-file ZIP

The build host needs the pinned Go version in `scripts/launcher/tool-versions.json` and Python. End users do not.

```sh
python3 scripts/build_release.py --script-launchers --package \
  --output dist/Auto-Outbound-Desktop-preview.zip
```

This builds six supervisors and bundles six pinned `uv` tools. The packager validates the explicit root whitelist and rejects secrets and runtime data.
For application-only updates, add `--reuse-images`; launcher hashes stay unchanged.
A fresh Git checkout needs this build before its command files can run.

## Build the native ZIP

Build tools are used only on the developer/CI host. Linux can cross-compile the five images; each target still needs a native GUI smoke test.

```sh
# Go version and all tool pins: scripts/launcher/tool-versions.json
python scripts/build_release.py --prepare-tools --target all --package
```

The output is `dist/Auto-Outbound-M1a-unverified.zip`, its SHA-256 file, and `build/images/launcher-hashes.json`. The script rejects incomplete release roots, mismatched runtime tools, leaked development files, and missing lockfiles. No release is published automatically.

For source-only changes, retain the previously built images:

```sh
python scripts/build_release.py --reuse-images --package
```

The manual GitHub Actions workflow **Build native ZIP** performs the same build and stores it as a workflow artifact. It does not promote the build to an accepted release.

Before accepting a native release, exercise first and second launch, a source-only update, path names with spaces/Unicode, restart persistence, failed/offline setup, concurrent launch, port collision, browser-open failure, and graceful Quit on each of the six OS/architecture combinations. Cross-compilation cannot substitute for these checks.
