AUTO OUTBOUND
Goosnav Labs

DEVELOPMENT BUILD
-----------------
This preview is unsigned. Full clean-machine acceptance on every operating
system and architecture is not yet complete.

OPEN
----
Extract the complete ZIP. Keep the launcher beside the app folder.

COMMAND-FILE PREVIEW (this ZIP)
Mac:        Double-click Open Auto Outbound.command
Windows:    Double-click Open Auto Outbound.cmd
PowerShell: Run Open Auto Outbound.ps1
Linux:      Run sh "Open Auto Outbound.sh"

NATIVE IMAGE EDITION (when supplied)
Mac:        Open Auto Outbound — macOS.app
Windows:    Open Auto Outbound — Windows x64.exe or Windows ARM64.exe
Linux:      Open Auto Outbound — Linux x86_64.AppImage or Linux ARM64.AppImage

The setup page downloads Python and locked dependencies, then opens the app.
First setup needs internet. No Python, Node.js or compiler installation is
required to run this ZIP. For live research, install and sign into Codex or
Claude Code separately. Use Open sample to test without an agent account.

OS PROMPTS
----------
Unsigned applications can trigger macOS Gatekeeper or Windows SmartScreen.
Follow your operating system's trust controls for the downloaded application.
On Linux, you may need Properties > Permissions > Allow executing as program.
If FUSE is unavailable, the AppImage supports --appimage-extract-and-run.
These are development images; a warning-free install has not been verified.

DATA AND RECOVERY
-----------------
Mac:     ~/Library/Application Support/AutoOutbound/
Windows: %LOCALAPPDATA%\AutoOutbound\
Linux:   ~/.local/share/AutoOutbound/ (or XDG_DATA_HOME/AutoOutbound/)

The logs folder is inside that location. A browser-open failure also writes
logs/OPEN_THIS_URL.txt. Setup shows its error code and a Retry action for
recoverable errors. Keep a complete app folder beside the launcher.
Back up your data before deleting any application-data folder.

Use Quit app in Help to stop the local server. Closing the browser tab leaves
it running. Open the launcher again to return to the existing instance.

Report an issue: https://github.com/goosnav/auto-outbound/issues
