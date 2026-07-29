# Future patch/tweak tool — plan only

This is a design plan. The current app does not patch game behavior.

## Goal

Add an optional recipe-based patch step for known, user-selected Quest tweaks.
Possible first recipes include replacing a store auto-open action with QGO or
disabling that auto-open action.

## Safety requirements

- Patches are always opt-in and off by default.
- Each recipe names the supported game and APK versions.
- A preflight scan proves the expected original bytes or decoded values exist.
- The app shows exactly what the recipe will change before building.
- Every recipe has an independent on/off switch and plain-language description.
- A mismatch stops safely instead of guessing.
- The build log records recipe name, version, input hash, and result.
- Source APKs remain untouched; patches only affect the decoded working copy.
- Package renaming and patching remain separate steps internally.
- Recipes include verification checks and a way to compare patched output.

## Proposed workflow

1. Choose and inspect a game bundle.
2. Open an optional **Tweaks** card.
3. Show only recipes verified for the detected game/version.
4. Select a tweak and review its exact effect.
5. Run patch preflight.
6. Apply the recipe to the temporary decoded copy.
7. Verify the expected result, then continue rebuild/signing.

No patch recipe should be added until it can be tested against an authorized
APK sample and reversed by rebuilding from the unchanged source.
