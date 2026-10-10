# Connected-headset Library and update identities

The Library is a live view of third-party apps on the authorized Quest. It does not maintain a
second desktop vault or pretend that old computer records are the headset's current state.

## Normal behavior

1. Connect and authorize a Quest. The Library reads its currently installed third-party package
   IDs and versions.
2. Select an app and choose its newer APK or complete game folder.
3. Publisher-signed apps are installed unchanged. A renamed app is rebuilt with the exact QAPKR
   identity associated with that installed package.
4. After a renamed install and all OBB verification succeed, QAPKR copies that app's signing
   identity to the connected Quest and verifies every transferred file.
5. Future updates materialize that identity into private desktop cache only while it is needed.

Selecting the wrong game is rejected before a build can replace the chosen app. Installing an
already-present package offers **Update** and **Cancel**; creating another copy requires a different
app ID first.

## On-headset layout

Each managed renamed app owns one identity folder:

```text
/sdcard/QAPKR/identities/<renamed-package-id>/
  profile.json
  signing-key.<version>.p12
  identity.<version>.json
  icon.<version>.png       # optional; extension may differ
```

`profile.json` records the original and renamed package IDs, display information, key hashes,
patches, and update bookkeeping. The JSON key metadata intentionally includes the password needed
to reuse its PKCS#12 file without prompting.

Writes are transactional: new files use unique immutable names, every upload is SHA-256 verified,
and `profile.json` is moved into place last. The prior manifest and its files remain usable until
that final commit; stale versions are removed afterwards. Readers reject missing, oversized, malformed,
mismatched, or checksum-invalid identities instead of silently generating a replacement key.

The signing identity belongs to that installed app on that headset. A headset wipe removes both
the app and its QAPKR identity. Installing the same renamed build onto several connected headsets
in one session stores a verified copy on each one.

## Recovery

The selected managed app can export a `.qarlib` recovery archive. The archive contains its full
profile, PKCS#12 key, key metadata, and cached icon with size and SHA-256 records. Treat it as a
private credential.

**Restore key…** validates an archive and accepts it only when it contains exactly one identity
whose renamed package ID matches the selected installed app. The restored files are then verified
on the Quest using the same transactional write path. **Remove key** deletes only that app's QAPKR
identity; it does not uninstall the app or touch its OBB directory.

Old desktop Library entries are migration inputs only. If an older release has a complete key for
an app that is installed on the connected Quest, QAPKR can use it for the next verified update and
then moves ownership to the headset. New identities remain in recoverable desktop cache only until
their install succeeds.

## Signing invariants

- An existing managed app is never silently assigned a new key.
- A missing, incomplete, changed, or corrupt identity blocks a managed update.
- Build requests pin the exact key and metadata paths selected for that app.
- A managed renamed identity cannot be reused with APK signing disabled.
- A lower numeric version code is blocked only when live headset inventory confirms a newer
  installed version.

## OBB synchronization

OBBs are synchronized as a verified set rather than pushed blindly:

1. Inventory the target package's Quest OBB directory.
2. Hash same-sized candidates when the Quest provides `sha256sum` or Toybox.
3. Skip a same-name, identical OBB.
4. Rename an identical package-matching OBB already on the Quest instead of uploading it again.
5. Upload changed data under a temporary `.qar-new-*` name and verify its size.
6. Stop the package, preserve replaced files as `.qar-old-*`, and activate the prepared set.
7. Install and verify the APK, then verify every expected OBB.
8. Only after success, remove transaction backups and obsolete package-matching OBB files.

Unrecognized files are preserved. If installation or post-install verification fails, the prior
OBB set is restored or retained under unique recovery names.

## Interface

The Dashboard remains the build workspace. The Library provides:

- a live inventory of current third-party apps and versions;
- Quest-visible names, IDs, launcher icons, and clear update-key health;
- automatic managed/direct update selection;
- APK and full-folder update actions; and
- per-app export, restore, and removal for QAPKR identities.

Known display metadata and icons are shared by normalized app name, so original and renamed IDs can
reuse one image without pulling every installed APK from the headset.
