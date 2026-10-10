import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from quest_renamer.domain.library import GameProfile
from quest_renamer.infrastructure.process_runner import CommandResult
from quest_renamer.infrastructure.quest_identity_store import (
    KEY_NAME,
    METADATA_NAME,
    PROFILE_NAME,
    REMOTE_ROOT,
    QuestIdentityStore,
)


class MemoryAdbRunner:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.commands: list[tuple[str, ...]] = []
        self.fail_move_to = ""

    def run(self, arguments: object, **kwargs: object) -> CommandResult:
        del kwargs
        command = tuple(str(item) for item in arguments)  # type: ignore[union-attr]
        self.commands.append(command)
        adb = command[3:]
        output: tuple[str, ...] = ()
        returncode = 0
        if adb[:3] == ("shell", "ls", "-1"):
            root = adb[3].rstrip("/") + "/"
            output = tuple(
                sorted(
                    {
                        path[len(root) :].split("/", 1)[0]
                        for path in self.files
                        if path.startswith(root)
                    }
                )
            )
        elif adb[:3] == ("shell", "stat", "-c"):
            path = adb[4]
            if path in self.files:
                output = (str(len(self.files[path])),)
            else:
                returncode = 1
        elif adb[:2] == ("exec-out", "cat"):
            path = adb[2]
            if path in self.files:
                output = tuple(self.files[path].decode("utf-8").splitlines())
            else:
                returncode = 1
        elif adb and adb[0] == "push":
            self.files[adb[2]] = Path(adb[1]).read_bytes()
        elif adb and adb[0] == "pull":
            Path(adb[2]).write_bytes(self.files[adb[1]])
        elif adb[:2] == ("shell", "sha256sum"):
            path = adb[2]
            if path in self.files:
                output = (f"{hashlib.sha256(self.files[path]).hexdigest()}  {path}",)
            else:
                returncode = 1
        elif adb[:3] == ("shell", "toybox", "sha256sum"):
            path = adb[3]
            if path in self.files:
                output = (f"{hashlib.sha256(self.files[path]).hexdigest()}  {path}",)
            else:
                returncode = 1
        elif adb[:2] == ("shell", "mkdir"):
            pass
        elif adb[:2] == ("shell", "mv"):
            if self.fail_move_to and adb[3].endswith(self.fail_move_to):
                raise RuntimeError("simulated interrupted transfer")
            self.files[adb[3]] = self.files.pop(adb[2])
        elif adb[:3] == ("shell", "rm", "-f"):
            for path in adb[3:]:
                self.files.pop(path, None)
        elif adb[:3] == ("shell", "rm", "-rf"):
            prefix = adb[3].rstrip("/") + "/"
            self.files = {
                path: value
                for path, value in self.files.items()
                if not path.startswith(prefix)
            }
        else:
            raise AssertionError(f"Unhandled fake ADB command: {adb}")
        return CommandResult(command, returncode, output)


class QuestIdentityStoreTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[QuestIdentityStore, MemoryAdbRunner, GameProfile]:
        adb = root / "adb"
        adb.touch()
        key = root / "signing-key.p12"
        metadata = root / "identity.json"
        icon = root / "icon.png"
        key.write_bytes(b"private signing key")
        metadata.write_text('{"alias":"quest","password":"secret"}', encoding="utf-8")
        icon.write_bytes(b"\x89PNG\r\n\x1a\nicon")
        profile = GameProfile.create(
            game_name="Example Game",
            original_package="com.example.game",
            target_package="com.dev.example.game",
            signing_keystore=str(key),
            signing_metadata=str(metadata),
            app_icon=str(icon),
        )
        runner = MemoryAdbRunner()
        store = QuestIdentityStore(root / "cache", adb_path=adb, runner=runner)  # type: ignore[arg-type]
        return store, runner, profile

    def test_save_and_load_round_trip_verified_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, runner, profile = self._fixture(root)

            store.save("QUEST123", profile)
            records = store.load("QUEST123", {profile.target_package, "com.other.app"})

            self.assertEqual(len(records), 1)
            loaded = records[0].profile
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.target_package, profile.target_package)
            self.assertEqual(Path(loaded.signing_keystore).read_bytes(), b"private signing key")
            self.assertTrue(loaded.key_available)
            moves = [command for command in runner.commands if command[3:5] == ("shell", "mv")]
            self.assertTrue(moves[-1][-1].endswith(PROFILE_NAME))

    def test_corrupt_remote_key_is_reported_instead_of_used(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, runner, profile = self._fixture(root)
            store.save("QUEST123", profile)
            manifest_path = f"{REMOTE_ROOT}/{profile.target_package}/{PROFILE_NAME}"
            manifest = json.loads(runner.files[manifest_path])
            remote = f"{REMOTE_ROOT}/{profile.target_package}/{manifest['key']}"
            runner.files[remote] = b"tampered"

            records = store.load("QUEST123", {profile.target_package})

            self.assertEqual(len(records), 1)
            self.assertIsNone(records[0].profile)
            self.assertIn("checksum", records[0].error)

    def test_uninstalled_identities_are_ignored_and_delete_is_narrow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, runner, profile = self._fixture(root)
            store.save("QUEST123", profile)
            other_root = f"{REMOTE_ROOT}/com.other.app"
            runner.files[f"{other_root}/{KEY_NAME}"] = b"other"
            runner.files[f"{other_root}/{METADATA_NAME}"] = b"{}"

            self.assertEqual(store.load("QUEST123", set()), ())
            store.delete("QUEST123", profile.target_package)

            self.assertFalse(
                any(
                    path.startswith(f"{REMOTE_ROOT}/{profile.target_package}/")
                    for path in runner.files
                )
            )
            self.assertTrue(any(path.startswith(other_root + "/") for path in runner.files))

    def test_interrupted_update_preserves_previous_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, runner, profile = self._fixture(root)
            store.save("QUEST123", profile)
            manifest_path = f"{REMOTE_ROOT}/{profile.target_package}/{PROFILE_NAME}"
            original_manifest = runner.files[manifest_path]

            Path(profile.signing_keystore).write_bytes(b"replacement signing key")
            runner.fail_move_to = ".json"
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                store.save("QUEST123", profile)

            self.assertEqual(runner.files[manifest_path], original_manifest)
            runner.fail_move_to = ""
            records = store.load("QUEST123", {profile.target_package})
            self.assertEqual(len(records), 1)
            loaded = records[0].profile
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(
                Path(loaded.signing_keystore).read_bytes(), b"private signing key"
            )

    def test_legacy_fixed_filenames_remain_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, runner, profile = self._fixture(root)
            store.save("QUEST123", profile)
            remote_root = f"{REMOTE_ROOT}/{profile.target_package}"
            manifest_path = f"{remote_root}/{PROFILE_NAME}"
            manifest = json.loads(runner.files[manifest_path])
            key_name = manifest.pop("key")
            metadata_name = manifest.pop("metadata")
            runner.files[f"{remote_root}/{KEY_NAME}"] = runner.files.pop(
                f"{remote_root}/{key_name}"
            )
            runner.files[f"{remote_root}/{METADATA_NAME}"] = runner.files.pop(
                f"{remote_root}/{metadata_name}"
            )
            manifest["files"][KEY_NAME] = manifest["files"].pop(key_name)
            manifest["files"][METADATA_NAME] = manifest["files"].pop(metadata_name)
            icon_name = manifest.get("icon", "")
            if icon_name:
                runner.files.pop(f"{remote_root}/{icon_name}")
                manifest["files"].pop(icon_name)
                manifest["icon"] = ""
            runner.files[manifest_path] = json.dumps(manifest).encode()

            records = store.load("QUEST123", {profile.target_package})

            self.assertEqual(len(records), 1)
            self.assertIsNotNone(records[0].profile)
            self.assertEqual(records[0].error, "")

    def test_manifest_artifact_paths_cannot_escape_identity_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, runner, profile = self._fixture(root)
            store.save("QUEST123", profile)
            manifest_path = f"{REMOTE_ROOT}/{profile.target_package}/{PROFILE_NAME}"
            manifest = json.loads(runner.files[manifest_path])
            manifest["key"] = "../signing-key.p12"
            runner.files[manifest_path] = json.dumps(manifest).encode()

            records = store.load("QUEST123", {profile.target_package})

            self.assertEqual(len(records), 1)
            self.assertIsNone(records[0].profile)
            self.assertIn("invalid key filename", records[0].error)


if __name__ == "__main__":
    unittest.main()
