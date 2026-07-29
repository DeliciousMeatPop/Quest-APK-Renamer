# macOS build

The macOS port uses the same Python application and test suite as Linux and
Windows. Each packaged build contains:

- a native `Quest APK Renamer.app`;
- a trimmed Java 21 runtime with `keytool`, created from Eclipse Temurin;
- Android SDK Platform-Tools;
- Apktool and Uber APK Signer; and
- native Finder drag-and-drop through TkinterDnD2/tkdnd.

Separate DMGs are produced for Apple Silicon (`arm64`) and Intel (`x86_64`)
because the embedded Python and Java runtimes are architecture-specific.

## Build on macOS

Install a current Python from python.org so Tk is included, then run:

```bash
chmod +x macos/build.sh macos/bootstrap-dependencies.sh
./macos/build.sh
```

The script downloads and records the bundled runtime dependencies, uses
`jlink` to keep only the required Java modules, runs the test suite, creates
the `.app`, applies ad-hoc signing, verifies the bundle, and creates the
architecture-specific DMG under `dist/`.

To use a Developer ID certificate:

```bash
MACOS_CODESIGN_IDENTITY="Developer ID Application: Example (TEAMID)" \
  ./macos/build.sh
```

The GitHub workflow can also import a Developer ID certificate and notarize
the DMG when the Apple signing secrets described in
[`docs/RELEASING.md`](../docs/RELEASING.md) are configured.

Without Developer ID signing and notarization, Gatekeeper warns that Apple
cannot verify the developer. The app itself does not need administrator
access. Signing keys and preferences remain in:

```text
~/Library/Application Support/Quest APK Renamer/
```

Deleting the `.app` deliberately does not remove that directory, because it
contains the signing identity needed to update previously renamed apps.
