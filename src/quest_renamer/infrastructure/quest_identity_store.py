"""Verified signing identities stored alongside managed apps on a Quest.

The headset is the durable owner of an installed renamed app's identity. Private
files are pulled only into a cache while that headset is connected. Remote writes
stage every file and publish ``profile.json`` last, so an interrupted USB transfer
cannot replace a previously usable identity with a partial one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from quest_renamer.domain.library import GameProfile, QuestIdentityRecord, profile_id
from quest_renamer.domain.package_ids import is_valid_package_id
from quest_renamer.infrastructure.adb_device import find_adb
from quest_renamer.infrastructure.process_runner import ProcessRunner

REMOTE_ROOT = "/sdcard/QAPKR/identities"
PROFILE_NAME = "profile.json"
KEY_NAME = "signing-key.p12"
METADATA_NAME = "identity.json"
FORMAT = 1
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_PROFILE_SIZE = 256 * 1024
_MAX_METADATA_SIZE = 64 * 1024
_MAX_KEY_SIZE = 16 * 1024 * 1024
_MAX_ICON_SIZE = 16 * 1024 * 1024
_VERSIONED_KEY = re.compile(r"signing-key\.[0-9a-f]{12}\.p12")
_VERSIONED_METADATA = re.compile(r"identity\.[0-9a-f]{12}\.json")
_ICON_NAME = re.compile(r"icon(?:\.[0-9a-f]{12})?\.(?:png|webp|jpe?g)")
_STAGED_NAME = re.compile(
    r"\.(?:signing-key\.[0-9a-f]{12}\.p12|identity\.[0-9a-f]{12}\.json|"
    r"icon\.[0-9a-f]{12}\.(?:png|webp|jpe?g)|profile\.json)\.tmp"
)


class QuestIdentityError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_serial_key(serial: str) -> str:
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()[:20]


def _private(*paths: Path) -> None:
    if os.name == "nt":
        return
    mode = stat.S_IRUSR | stat.S_IWUSR
    for path in paths:
        os.chmod(path, mode)


def _identity_metadata_is_valid(path: Path) -> bool:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and isinstance(payload.get("alias"), str)
        and payload["alias"]
        and isinstance(payload.get("password"), str)
        and payload["password"]
    )


class QuestIdentityStore:
    def __init__(
        self,
        cache_root: Path,
        *,
        resource_root: Path | None = None,
        executable: Path | None = None,
        adb_path: Path | None = None,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.cache_root = cache_root
        self.resource_root = resource_root
        self.executable = executable
        self.adb_path = adb_path
        self.runner = runner or ProcessRunner()
        # Save, refresh, import, and delete can be triggered by different worker
        # threads. Serializing them prevents one successful manifest commit from
        # cleaning another operation's newly versioned files.
        self._operation_lock = threading.RLock()

    def _target(self, serial: str) -> tuple[Path, str, str]:
        if not serial:
            raise QuestIdentityError("No authorized Quest is selected.")
        adb = self.adb_path or find_adb(
            resource_root=self.resource_root,
            executable=self.executable,
        )
        if adb is None:
            raise QuestIdentityError("Android Platform Tools are not available.")
        return adb, "-s", serial

    @staticmethod
    def _remote_dir(package_name: str) -> str:
        if not is_valid_package_id(package_name):
            raise QuestIdentityError("The stored app ID is invalid.")
        return f"{REMOTE_ROOT}/{package_name}"

    def _remote_size(self, target: tuple[Path, str, str], path: str) -> int | None:
        result = self.runner.run(
            (*target, "shell", "stat", "-c", "%s", path),
            check=False,
        )
        for line in result.output:
            value = line.strip()
            if value.isdigit():
                return int(value)
        return None

    def _remote_sha256(self, target: tuple[Path, str, str], path: str) -> str:
        for command in (
            (*target, "shell", "sha256sum", path),
            (*target, "shell", "toybox", "sha256sum", path),
        ):
            result = self.runner.run(command, check=False)
            for line in result.output:
                value = line.strip().split(maxsplit=1)[0].lower()
                if _DIGEST.fullmatch(value):
                    return value
        return ""

    def _read_profile(self, target: tuple[Path, str, str], package_name: str) -> dict[str, Any]:
        remote = f"{self._remote_dir(package_name)}/{PROFILE_NAME}"
        size = self._remote_size(target, remote)
        if size is None:
            raise QuestIdentityError("QAPKR identity metadata is missing.")
        if size > _MAX_PROFILE_SIZE:
            raise QuestIdentityError("QAPKR identity metadata is unexpectedly large.")
        result = self.runner.run((*target, "exec-out", "cat", remote), check=False)
        if result.returncode != 0:
            raise QuestIdentityError("QAPKR identity metadata could not be read.")
        try:
            payload: Any = json.loads("\n".join(result.output))
        except json.JSONDecodeError as exc:
            raise QuestIdentityError("QAPKR identity metadata is invalid JSON.") from exc
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            raise QuestIdentityError("QAPKR identity metadata uses an unsupported format.")
        return payload

    def load(
        self,
        serial: str,
        installed_packages: set[str],
    ) -> tuple[QuestIdentityRecord, ...]:
        with self._operation_lock:
            return self._load(serial, installed_packages)

    def _load(
        self,
        serial: str,
        installed_packages: set[str],
    ) -> tuple[QuestIdentityRecord, ...]:
        target = self._target(serial)
        listed = self.runner.run(
            (*target, "shell", "ls", "-1", REMOTE_ROOT),
            check=False,
        )
        packages = tuple(
            sorted(
                {
                    line.strip()
                    for line in listed.output
                    if is_valid_package_id(line.strip())
                    and line.strip() in installed_packages
                },
                key=str.casefold,
            )
        )
        records: list[QuestIdentityRecord] = []
        for package_name in packages:
            try:
                profile = self._materialize(target, serial, package_name)
                records.append(QuestIdentityRecord(package_name, profile))
            except (OSError, QuestIdentityError) as exc:
                records.append(QuestIdentityRecord(package_name, error=str(exc)))
        return tuple(records)

    def _materialize(
        self,
        target: tuple[Path, str, str],
        serial: str,
        package_name: str,
    ) -> GameProfile:
        payload = self._read_profile(target, package_name)
        raw_profile = payload.get("profile")
        profile = GameProfile.from_mapping(raw_profile)
        if profile is None or profile.target_package != package_name:
            raise QuestIdentityError("QAPKR identity does not match the installed app ID.")
        if not is_valid_package_id(profile.original_package):
            raise QuestIdentityError("QAPKR identity contains an invalid original app ID.")
        if profile.id != profile_id(profile.original_package, profile.target_package):
            raise QuestIdentityError("QAPKR identity has an invalid identity ID.")
        files = payload.get("files")
        if not isinstance(files, dict):
            raise QuestIdentityError("QAPKR identity file records are missing.")
        key_name = self._manifest_name(payload, "key", KEY_NAME, _VERSIONED_KEY)
        metadata_name = self._manifest_name(
            payload, "metadata", METADATA_NAME, _VERSIONED_METADATA
        )
        key_hash = self._file_digest(files, key_name)
        metadata_hash = self._file_digest(files, metadata_name)
        remote_dir = self._remote_dir(package_name)
        self._check_remote_file(target, f"{remote_dir}/{key_name}", _MAX_KEY_SIZE)
        self._check_remote_file(
            target, f"{remote_dir}/{metadata_name}", _MAX_METADATA_SIZE
        )

        destination = self.cache_root / _safe_serial_key(serial) / package_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{package_name}.identity-", dir=destination.parent)
        )
        try:
            key = staging / KEY_NAME
            metadata = staging / METADATA_NAME
            self.runner.run((*target, "pull", f"{remote_dir}/{key_name}", key))
            self.runner.run(
                (*target, "pull", f"{remote_dir}/{metadata_name}", metadata)
            )
            if _sha256(key) != key_hash or _sha256(metadata) != metadata_hash:
                raise QuestIdentityError("QAPKR signing identity failed its checksum check.")
            if not _identity_metadata_is_valid(metadata):
                raise QuestIdentityError("QAPKR signing identity has invalid key metadata.")
            _private(key, metadata)
            icon_path = ""
            icon_name = payload.get("icon")
            if isinstance(icon_name, str) and _ICON_NAME.fullmatch(icon_name):
                icon_hash = self._file_digest(files, icon_name)
                self._check_remote_file(
                    target, f"{remote_dir}/{icon_name}", _MAX_ICON_SIZE
                )
                local_icon_name = f"icon{Path(icon_name).suffix.lower()}"
                icon = staging / local_icon_name
                self.runner.run((*target, "pull", f"{remote_dir}/{icon_name}", icon))
                if _sha256(icon) != icon_hash:
                    raise QuestIdentityError("QAPKR app icon failed its checksum check.")
                icon_path = str(destination / local_icon_name)
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(staging, destination)
            return profile.updated(
                signing_keystore=str(destination / KEY_NAME),
                signing_metadata=str(destination / METADATA_NAME),
                signing_key_sha256=key_hash,
                app_icon=icon_path,
                last_device_serial=serial,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _manifest_name(
        payload: dict[str, Any],
        field: str,
        legacy_name: str,
        versioned_pattern: re.Pattern[str],
    ) -> str:
        value = payload.get(field, legacy_name)
        if value == legacy_name:
            return legacy_name
        if not isinstance(value, str) or not versioned_pattern.fullmatch(value):
            raise QuestIdentityError(f"QAPKR identity has an invalid {field} filename.")
        return value

    @staticmethod
    def _file_digest(files: dict[str, Any], name: str) -> str:
        value = files.get(name)
        if not isinstance(value, dict):
            raise QuestIdentityError(f"QAPKR file record is missing: {name}.")
        digest = value.get("sha256")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise QuestIdentityError(f"QAPKR file checksum is invalid: {name}.")
        return digest

    def _check_remote_file(
        self,
        target: tuple[Path, str, str],
        path: str,
        maximum: int,
    ) -> None:
        size = self._remote_size(target, path)
        if size is None:
            name = path.rsplit("/", 1)[-1]
            raise QuestIdentityError(f"QAPKR identity file is missing: {name}.")
        if size > maximum:
            raise QuestIdentityError(
                f"QAPKR identity file is unexpectedly large: {path.rsplit('/', 1)[-1]}."
            )

    def save(self, serial: str, profile: GameProfile) -> None:
        with self._operation_lock:
            self._save(serial, profile)

    def _save(self, serial: str, profile: GameProfile) -> None:
        if not profile.key_available:
            raise QuestIdentityError("The signing identity is incomplete or changed.")
        target = self._target(serial)
        remote_dir = self._remote_dir(profile.target_package)
        key = Path(profile.signing_keystore)
        metadata = Path(profile.signing_metadata)
        if key.stat().st_size > _MAX_KEY_SIZE:
            raise QuestIdentityError("The signing key is unexpectedly large.")
        if metadata.stat().st_size > _MAX_METADATA_SIZE:
            raise QuestIdentityError("The signing metadata is unexpectedly large.")
        if not _identity_metadata_is_valid(metadata):
            raise QuestIdentityError("The signing identity metadata is invalid.")
        key_hash = _sha256(key)
        metadata_hash = _sha256(metadata)
        icon = Path(profile.app_icon) if profile.app_icon else None
        if icon is not None and (
            not icon.is_file()
            or icon.stat().st_size > _MAX_ICON_SIZE
            or icon.suffix.lower() not in {".png", ".webp", ".jpg", ".jpeg"}
        ):
            icon = None
        nonce = uuid.uuid4().hex[:12]
        key_name = f"signing-key.{nonce}.p12"
        metadata_name = f"identity.{nonce}.json"
        icon_name = (
            f"icon.{nonce}{icon.suffix.lower()}" if icon is not None else ""
        )
        clean_profile = profile.updated(
            source_path="",
            output_path="",
            signing_keystore="",
            signing_metadata="",
            app_icon="",
            last_device_serial="",
            signing_key_sha256=key_hash,
        )
        files: dict[str, dict[str, str | int]] = {
            key_name: {"sha256": key_hash, "size": key.stat().st_size},
            metadata_name: {
                "sha256": metadata_hash,
                "size": metadata.stat().st_size,
            },
        }
        if icon is not None:
            files[icon_name] = {"sha256": _sha256(icon), "size": icon.stat().st_size}
        manifest = {
            "format": FORMAT,
            "profile": clean_profile.to_mapping(),
            "files": files,
            "key": key_name,
            "metadata": metadata_name,
            "icon": icon_name,
        }
        self.cache_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="qapkr-identity-", dir=self.cache_root) as raw:
            temporary = Path(raw)
            manifest_path = temporary / PROFILE_NAME
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            staged: list[tuple[Path, str, str]] = [
                (key, f".{key_name}.tmp", key_name),
                (metadata, f".{metadata_name}.tmp", metadata_name),
            ]
            if icon is not None:
                staged.append((icon, f".{icon_name}.tmp", icon_name))
            staged.append((manifest_path, f".{PROFILE_NAME}.tmp", PROFILE_NAME))
            self.runner.run((*target, "shell", "mkdir", "-p", remote_dir))
            remote_temps: list[str] = []
            moved_artifacts: list[str] = []
            committed = False
            try:
                for source, staged_name, _final_name in staged:
                    remote = f"{remote_dir}/{staged_name}"
                    remote_temps.append(remote)
                    self.runner.run((*target, "push", source, remote))
                    digest = self._remote_sha256(target, remote)
                    if not digest or digest != _sha256(source):
                        raise QuestIdentityError(
                            f"The Quest did not verify {source.name} after transfer."
                        )
                # The manifest is committed last; readers either see the complete
                # previous identity or the complete new one.
                for _source, staged_name, final_name in staged:
                    final_path = f"{remote_dir}/{final_name}"
                    self.runner.run(
                        (
                            *target,
                            "shell",
                            "mv",
                            f"{remote_dir}/{staged_name}",
                            final_path,
                        )
                    )
                    if final_name == PROFILE_NAME:
                        committed = True
                    else:
                        moved_artifacts.append(final_path)
                remote_temps.clear()
            finally:
                if remote_temps:
                    self.runner.run(
                        (*target, "shell", "rm", "-f", *remote_temps),
                        check=False,
                    )
                if not committed and moved_artifacts:
                    self.runner.run(
                        (*target, "shell", "rm", "-f", *moved_artifacts),
                        check=False,
                    )
            self._remove_stale_files(
                target,
                remote_dir,
                {PROFILE_NAME, key_name, metadata_name, icon_name} - {""},
            )

    def _remove_stale_files(
        self,
        target: tuple[Path, str, str],
        remote_dir: str,
        keep: set[str],
    ) -> None:
        listed = self.runner.run(
            (*target, "shell", "ls", "-1", remote_dir),
            check=False,
        )
        stale = [
            f"{remote_dir}/{name}"
            for raw in listed.output
            if (name := raw.strip()) not in keep
            and (
                name in {KEY_NAME, METADATA_NAME}
                or _VERSIONED_KEY.fullmatch(name)
                or _VERSIONED_METADATA.fullmatch(name)
                or _ICON_NAME.fullmatch(name)
                or _STAGED_NAME.fullmatch(name)
            )
        ]
        if stale:
            self.runner.run((*target, "shell", "rm", "-f", *stale), check=False)

    def delete(self, serial: str, package_name: str) -> None:
        with self._operation_lock:
            self._delete(serial, package_name)

    def _delete(self, serial: str, package_name: str) -> None:
        target = self._target(serial)
        remote_dir = self._remote_dir(package_name)
        # Package validation above makes this an exact, narrow QAPKR directory.
        self.runner.run((*target, "shell", "rm", "-rf", remote_dir))
