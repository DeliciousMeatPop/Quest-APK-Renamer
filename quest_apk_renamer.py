#!/usr/bin/env python3
"""Quest APK Renamer - GUI workflow for repackaging APK + OBB bundles.

The application never modifies the source bundle. It decodes a selected APK,
updates package references, rebuilds it, signs it with a persistent local key,
and creates a matching OBB/release.manifest output bundle.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    X,
    BooleanVar,
    Canvas,
    PhotoImage,
    StringVar,
    Text,
    Tk,
    Toplevel,
    filedialog,
    font as tkfont,
    messagebox,
    ttk,
)


APP_NAME = "Quest APK Renamer"
APP_VERSION = "1.8.0"
PACKAGE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)+$")
OBB_RE = re.compile(r"^(main|patch)\.(\d+)\.(.+)\.obb$", re.IGNORECASE)

SCRIPT_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SCRIPT_DIR))
VENDOR_DIR = SCRIPT_DIR / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_AVAILABLE = True
except ImportError:
    DND_FILES = None
    TkinterDnD = None
    DND_AVAILABLE = False

TOOLS_DIR = RESOURCE_DIR / "tools"
APKTOOL_JAR = TOOLS_DIR / "apktool.jar"
SIGNER_JAR = TOOLS_DIR / "uber-apk-signer.jar"
TOOL_INFO_FILE = TOOLS_DIR / "versions.json"
RUNTIME_DIR = RESOURCE_DIR / "runtime"


def platform_app_data() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        return (
            Path(base) / APP_NAME
            if base
            else Path.home() / "AppData" / "Local" / APP_NAME
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg_data = os.environ.get("XDG_DATA_HOME")
    return (
        Path(xdg_data) / "quest-apk-renamer"
        if xdg_data
        else Path.home() / ".local" / "share" / "quest-apk-renamer"
    )


APP_DATA = platform_app_data()
MANAGED_KEYSTORE = APP_DATA / "quest-renamer-signing-key.jks"
MANAGED_KEY_INFO = APP_DATA / "signing-key.json"
KEY_BACKUP_MARKER = APP_DATA / "signing-key-backup.json"
SETTINGS_FILE = APP_DATA / "settings.json"
LINUX_LAUNCHER = (
    Path.home() / ".local" / "share" / "applications" / "quest-apk-renamer.desktop"
)
WINDOWS_START_MENU = (
    Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
    / f"{APP_NAME}.lnk"
)
MACOS_USER_APPLICATION = Path.home() / "Applications" / f"{APP_NAME}.app"
MACOS_SYSTEM_APPLICATION = Path("/Applications") / f"{APP_NAME}.app"

# Pinned, verified releases. The installer also records their provenance.
TOOL_DOWNLOADS = {
    "apktool": {
        "version": "3.0.3",
        "url": "https://github.com/iBotPeaches/Apktool/releases/download/v3.0.3/apktool_3.0.3.jar",
        "sha256": "dbf930b076c6b9be08d57c449cacefc3bdd6b71ebd59b3066fc0e1f5b14f9423",
        "path": APKTOOL_JAR,
    },
    "uber-apk-signer": {
        "version": "1.3.0",
        "url": "https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar",
        "sha256": "e1299fd6fcf4da527dd53735b56127e8ea922a321128123b9c32d619bba1d835",
        "path": SIGNER_JAR,
    },
}


DEFAULT_SETTINGS = {
    "ask_signing_key_backup": True,
}


def load_user_settings(path: Path = SETTINGS_FILE) -> dict[str, object]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if isinstance(stored, dict) and isinstance(
        stored.get("ask_signing_key_backup"), bool
    ):
        settings["ask_signing_key_backup"] = stored["ask_signing_key_backup"]
    return settings


def save_user_settings(
    settings: dict[str, object],
    path: Path = SETTINGS_FILE,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(settings, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


@dataclass
class BundleInfo:
    apk: Path
    root: Path
    obbs: list[Path]
    release_manifest: Path | None
    package_name: str
    version_code: str
    game_name: str


class UserError(Exception):
    """An error that should be shown without a traceback."""


class OperationCancelled(Exception):
    """Raised at a safe boundary after the user requests cancellation."""

    def __init__(self, details: dict[str, object] | None = None) -> None:
        super().__init__("Operation cancelled")
        self.details = details or {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid_package(value: str) -> bool:
    return bool(PACKAGE_RE.fullmatch(value.strip()))


def parse_release_manifest(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        header_index = next(
            i for i, line in enumerate(lines) if line.startswith("Game Name;")
        )
        reader = csv.DictReader(lines[header_index : header_index + 2], delimiter=";")
        return next(reader, {}) or {}
    except (OSError, StopIteration, csv.Error):
        return {}


def detect_bundle(apk: Path, bundle_root: Path | None = None) -> BundleInfo:
    apk = apk.expanduser().resolve()
    if not apk.is_file() or apk.suffix.lower() != ".apk":
        raise UserError("Choose a valid .apk file.")

    root = bundle_root.expanduser().resolve() if bundle_root else apk.parent
    if not root.is_dir():
        raise UserError("Choose a valid game folder.")
    if root != apk.parent and root not in apk.parents:
        raise UserError("The APK must be inside the selected game folder.")
    manifest = root / "release.manifest"
    if not manifest.is_file():
        manifest = None

    metadata = parse_release_manifest(manifest) if manifest else {}
    obbs = sorted(
        (path for path in root.rglob("*.obb") if path.is_file()),
        key=lambda path: str(path).lower(),
    )

    package_name = (metadata.get("Package Name") or "").strip()
    version_code = (metadata.get("Version Code") or "").strip()
    game_name = (metadata.get("Game Name") or "").strip()

    if not package_name and is_valid_package(apk.stem):
        package_name = apk.stem

    for obb in obbs:
        match = OBB_RE.match(obb.name)
        if not match:
            continue
        if not version_code:
            version_code = match.group(2)
        if not package_name and is_valid_package(match.group(3)):
            package_name = match.group(3)

    return BundleInfo(
        apk=apk,
        root=root,
        obbs=obbs,
        release_manifest=manifest,
        package_name=package_name,
        version_code=version_code,
        game_name=game_name,
    )


def detect_bundle_from_folder(folder: Path) -> BundleInfo:
    """Find one main APK and its companion files inside a selected folder."""
    root = folder.expanduser().resolve()
    if not root.is_dir():
        raise UserError("Choose the folder that contains the APK and OBB files.")

    top_level_apks = sorted(
        (path for path in root.glob("*.apk") if path.is_file()),
        key=lambda path: path.name.lower(),
    )
    apks = top_level_apks
    if not apks:
        apks = sorted(
            (path for path in root.rglob("*.apk") if path.is_file()),
            key=lambda path: str(path).lower(),
        )
    if not apks:
        raise UserError(
            "No APK was found in that folder.\n\n"
            "Choose the main game folder—the one showing the .apk, "
            "release.manifest, and the OBB subfolder."
        )

    if len(apks) > 1:
        manifest = root / "release.manifest"
        package_name = ""
        if manifest.is_file():
            package_name = parse_release_manifest(manifest).get("Package Name", "")
        matching = [path for path in apks if path.stem == package_name]
        if len(matching) == 1:
            apks = matching
        else:
            names = "\n".join(f"• {path.relative_to(root)}" for path in apks[:6])
            raise UserError(
                "More than one APK was found. Put one game bundle in each folder, "
                "then select that folder.\n\nFound:\n" + names
            )

    return detect_bundle(apks[0], bundle_root=root)


def unique_output_dir(requested: Path) -> Path:
    if not requested.exists():
        return requested
    if requested.is_dir() and not any(requested.iterdir()):
        return requested
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = requested.with_name(f"{requested.name}-{stamp}")
    counter = 2
    while candidate.exists():
        candidate = requested.with_name(
            f"{requested.name}-{stamp}-{counter}"
        )
        counter += 1
    return candidate


def package_with_suffix(package_name: str, suffix: str) -> str:
    suffix = suffix.strip()
    if not suffix or not re.fullmatch(r"[a-zA-Z0-9_]+", suffix):
        raise UserError(
            "The bulk suffix must contain only letters, numbers, or underscores."
        )
    result = package_name + suffix
    if not is_valid_package(result):
        raise UserError(f"Adding “{suffix}” would create an invalid package ID.")
    return result


def discover_bundle_folders(parent: Path) -> tuple[list[Path], list[str]]:
    """Find bundles in a chosen folder or its direct child folders."""
    parent = parent.expanduser().resolve()
    if not parent.is_dir():
        raise UserError("Choose a valid folder.")
    if any(path.is_file() for path in parent.glob("*.apk")):
        candidates = [parent]
    else:
        candidates = sorted(
            (path for path in parent.iterdir() if path.is_dir()),
            key=lambda path: path.name.lower(),
        )
    found = []
    skipped = []
    for candidate in candidates:
        try:
            detect_bundle_from_folder(candidate)
        except UserError as exc:
            if candidate != parent:
                skipped.append(f"{candidate.name}: {exc}")
            continue
        found.append(candidate)
    return found, skipped


def commit_staged_replacement(
    source: Path,
    staged: Path,
    move_to_trash,
) -> tuple[Path, Path | None, str | None]:
    """Atomically activate a completed staged bundle with rollback protection."""
    source = source.expanduser().resolve()
    staged = staged.expanduser().resolve()
    if not source.is_dir():
        raise UserError("The source folder disappeared before replacement.")
    if not is_managed_output(staged):
        raise UserError("The staged replacement failed its completion check.")
    if source.parent != staged.parent or source == staged:
        raise UserError("The staged replacement must be beside the source folder.")

    backup = unique_output_dir(
        source.parent / f".{source.name} - Source Backup"
    )
    source.rename(backup)
    try:
        staged.rename(source)
    except Exception as exc:
        try:
            backup.rename(source)
        except Exception as rollback_exc:
            raise UserError(
                "Replacement failed and automatic rollback also failed. "
                f"Keep both folders and restore this backup manually:\n{backup}\n\n"
                f"Rollback error: {rollback_exc}"
            ) from exc
        raise UserError(
            f"Replacement could not be activated, so the original folder was restored: {exc}"
        ) from exc

    trash_error = move_to_trash(backup)
    if not trash_error:
        return source, None, None

    visible_backup = unique_output_dir(
        source.parent / f"{source.name} - Original Backup"
    )
    try:
        backup.rename(visible_backup)
    except OSError:
        visible_backup = backup
    return source, visible_backup, trash_error


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1000
    return f"{value:.1f} TB"


def required_working_space(bundle: BundleInfo, copy_obbs: bool = True) -> int:
    """Conservative workspace estimate for decode, rebuild, signing, and OBB copy."""
    apk_size = bundle.apk.stat().st_size
    obb_size = sum(path.stat().st_size for path in bundle.obbs) if copy_obbs else 0
    return int(apk_size * 5 + obb_size * 1.1 + 500_000_000)


def required_quest_space(bundle: BundleInfo) -> int:
    """Conservative estimate for APK installation plus OBB storage."""
    apk_size = bundle.apk.stat().st_size
    obb_size = sum(path.stat().st_size for path in bundle.obbs)
    return int(apk_size * 2 + obb_size * 1.05 + 250_000_000)


def existing_parent(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def package_is_installed(returncode: int, output: str) -> bool:
    return returncode == 0 and any(
        line.strip().startswith("package:") for line in output.splitlines()
    )


def is_managed_output(path: Path) -> bool:
    return path.is_dir() and (path / "RENAMED-BUNDLE.txt").is_file()


def find_java() -> Path | None:
    executable = "java.exe" if sys.platform.startswith("win") else "java"
    candidates = [
        RUNTIME_DIR / "java" / "bin" / executable,
        SCRIPT_DIR / "windows" / "runtime" / "java" / "bin" / executable,
        SCRIPT_DIR / "macos" / "runtime" / "java" / "bin" / executable,
    ]
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(Path(java_home) / "bin" / executable)
    path_java = shutil.which(executable)
    if path_java:
        candidates.append(Path(path_java))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def find_keytool() -> Path | None:
    executable = "keytool.exe" if sys.platform.startswith("win") else "keytool"
    candidates = [
        RUNTIME_DIR / "java" / "bin" / executable,
        SCRIPT_DIR / "windows" / "runtime" / "java" / "bin" / executable,
        SCRIPT_DIR / "macos" / "runtime" / "java" / "bin" / executable,
    ]
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(Path(java_home) / "bin" / executable)
    path_keytool = shutil.which(executable)
    if path_keytool:
        candidates.append(Path(path_keytool))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def tool_status() -> tuple[bool, str]:
    java = find_java()
    keytool = find_keytool()
    missing = []
    if not java:
        missing.append("Java")
    if not keytool:
        missing.append("keytool")
    if not APKTOOL_JAR.is_file():
        missing.append("Apktool")
    if not SIGNER_JAR.is_file():
        missing.append("APK Signer")
    if missing:
        return False, "Missing: " + ", ".join(missing)
    return True, "Ready"


def find_adb() -> Path | None:
    """Find Android Debug Bridge in PATH or common local SDK locations."""
    executable = "adb.exe" if sys.platform.startswith("win") else "adb"
    candidates = []
    candidates.extend(
        [
            RUNTIME_DIR / "platform-tools" / executable,
            SCRIPT_DIR / "windows" / "runtime" / "platform-tools" / executable,
            SCRIPT_DIR / "macos" / "runtime" / "platform-tools" / executable,
        ]
    )
    path_adb = shutil.which("adb")
    if path_adb:
        candidates.append(Path(path_adb))
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk_root = os.environ.get(variable)
        if sdk_root:
            candidates.append(Path(sdk_root) / "platform-tools" / executable)
    local_app_data = os.environ.get("LOCALAPPDATA")
    roaming_app_data = os.environ.get("APPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data)
            / "Android"
            / "Sdk"
            / "platform-tools"
            / executable
        )
    if roaming_app_data:
        candidates.append(
            Path(roaming_app_data)
            / "SideQuest"
            / "platform-tools"
            / executable
        )
    candidates.extend(
        [
            Path.home() / "Android" / "Sdk" / "platform-tools" / executable,
            Path.home()
            / "Library"
            / "Android"
            / "sdk"
            / "platform-tools"
            / executable,
            Path.home()
            / "Library"
            / "Application Support"
            / "SideQuest"
            / "platform-tools"
            / executable,
            Path.home() / "Downloads" / "platform-tools" / executable,
            Path.home() / "Downloads" / "RSL" / "platform-tools" / executable,
            TOOLS_DIR / "platform-tools" / executable,
        ]
    )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def parse_adb_devices(output: str) -> tuple[list[str], list[str]]:
    connected = []
    unavailable = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*") or line.startswith("List of devices"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        if fields[1] == "device":
            connected.append(fields[0])
        elif fields[1] in {"unauthorized", "offline"}:
            unavailable.append(f"{fields[0]} ({fields[1]})")
    return connected, unavailable


def parse_adb_model(output: str, serial: str) -> str:
    for raw_line in output.splitlines():
        fields = raw_line.strip().split()
        if not fields or fields[0] != serial:
            continue
        for field in fields[2:]:
            if field.startswith("model:"):
                model = field.removeprefix("model:").replace("_", " ").strip()
                return model or "Quest"
    return "Quest"


def parse_df_storage(output: str) -> tuple[int, int] | None:
    """Return available and total bytes from Android's `df -k` output."""
    for raw_line in reversed(output.splitlines()):
        fields = raw_line.strip().split()
        percent_index = next(
            (index for index, field in enumerate(fields) if field.endswith("%")),
            None,
        )
        if percent_index is None or percent_index < 3:
            continue
        try:
            total_kib = int(fields[percent_index - 3])
            available_kib = int(fields[percent_index - 1])
        except ValueError:
            continue
        if total_kib > 0 and available_kib >= 0:
            return available_kib * 1024, total_kib * 1024
    return None


def native_choose_directory(title: str, initial: Path | None = None) -> str:
    """Open the desktop's native folder picker, with Tk as a final fallback."""
    start = str((initial or Path.home()).expanduser())
    if sys.platform == "darwin":
        return filedialog.askdirectory(
            title=title,
            initialdir=start,
            mustexist=True,
        )
    kdialog = shutil.which("kdialog")
    if kdialog:
        result = subprocess.run(
            [kdialog, "--title", title, "--getexistingdirectory", start],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    zenity = shutil.which("zenity")
    if zenity:
        result = subprocess.run(
            [
                zenity,
                "--file-selection",
                "--directory",
                f"--title={title}",
                f"--filename={start}/",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    return filedialog.askdirectory(title=title, initialdir=start, mustexist=True)


def native_choose_apk(title: str, initial: Path | None = None) -> str:
    """Open the desktop's native APK picker, with Tk as a final fallback."""
    start = str((initial or Path.home()).expanduser())
    if sys.platform == "darwin":
        return filedialog.askopenfilename(
            title=title,
            initialdir=start,
            filetypes=[("Android packages", "*.apk"), ("All files", "*")],
        )
    kdialog = shutil.which("kdialog")
    if kdialog:
        result = subprocess.run(
            [
                kdialog,
                "--title",
                title,
                "--getopenfilename",
                start,
                "*.apk|Android packages (*.apk)",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    zenity = shutil.which("zenity")
    if zenity:
        result = subprocess.run(
            [
                zenity,
                "--file-selection",
                f"--title={title}",
                f"--filename={start}/",
                "--file-filter=Android packages | *.apk",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    return filedialog.askopenfilename(
        title=title,
        initialdir=start,
        filetypes=[("Android packages", "*.apk"), ("All files", "*")],
    )


def native_choose_apks(title: str, initial: Path | None = None) -> list[str]:
    """Open a native picker that can select several APKs at once."""
    start = str((initial or Path.home()).expanduser())
    if sys.platform == "darwin":
        return list(
            filedialog.askopenfilenames(
                title=title,
                initialdir=start,
                filetypes=[
                    ("Android packages", "*.apk"),
                    ("All files", "*"),
                ],
            )
        )
    kdialog = shutil.which("kdialog")
    if kdialog:
        result = subprocess.run(
            [
                kdialog,
                "--title",
                title,
                "--multiple",
                "--separate-output",
                "--getopenfilename",
                start,
                "*.apk|Android packages (*.apk)",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return (
            [line for line in result.stdout.splitlines() if line]
            if result.returncode == 0
            else []
        )
    zenity = shutil.which("zenity")
    if zenity:
        result = subprocess.run(
            [
                zenity,
                "--file-selection",
                "--multiple",
                "--separator=\n",
                f"--title={title}",
                f"--filename={start}/",
                "--file-filter=Android packages | *.apk",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return (
            [line for line in result.stdout.splitlines() if line]
            if result.returncode == 0
            else []
        )
    return list(
        filedialog.askopenfilenames(
            title=title,
            initialdir=start,
            filetypes=[("Android packages", "*.apk"), ("All files", "*")],
        )
    )


def load_managed_key() -> tuple[Path, str, str]:
    if not MANAGED_KEYSTORE.is_file() or not MANAGED_KEY_INFO.is_file():
        raise UserError("The managed signing key has not been created.")
    try:
        info = json.loads(MANAGED_KEY_INFO.read_text(encoding="utf-8"))
        return MANAGED_KEYSTORE, info["alias"], info["password"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise UserError(f"Could not read the managed signing key: {exc}") from exc


def ensure_managed_key(run_command, log) -> tuple[Path, str, str]:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    if MANAGED_KEYSTORE.is_file() and MANAGED_KEY_INFO.is_file():
        return load_managed_key()

    alias = "quest-renamer"
    password = secrets.token_urlsafe(24)
    log("Creating a persistent local signing identity…")
    run_command(
        [
            str(find_keytool() or "keytool"),
            "-genkeypair",
            "-noprompt",
            "-keystore",
            str(MANAGED_KEYSTORE),
            "-storepass",
            password,
            "-keypass",
            password,
            "-alias",
            alias,
            "-keyalg",
            "RSA",
            "-keysize",
            "2048",
            "-validity",
            "10000",
            "-dname",
            "CN=Quest APK Renamer, OU=Local Build, O=Local, L=Local, ST=Local, C=US",
        ],
        log=log,
        secret_values={password},
    )
    MANAGED_KEY_INFO.write_text(
        json.dumps({"alias": alias, "password": password}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(MANAGED_KEY_INFO, stat.S_IRUSR | stat.S_IWUSR)
    os.chmod(MANAGED_KEYSTORE, stat.S_IRUSR | stat.S_IWUSR)
    return MANAGED_KEYSTORE, alias, password


def replace_package_references(decoded: Path, old: str, new: str, log) -> int:
    replacements = 0
    preserved_matches = []
    old_bytes = old.encode("utf-8")
    old_slash = old.replace(".", "/").encode("utf-8")
    new_bytes = new.encode("utf-8")
    new_slash = new.replace(".", "/").encode("utf-8")

    for path in decoded.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(decoded)
        relative_posix = relative.as_posix()
        technical_file = (
            relative_posix in {"AndroidManifest.xml", "apktool.yml"}
            or path.suffix.lower() == ".smali"
            or (
                len(relative.parts) > 1
                and relative.parts[0] == "res"
                and path.suffix.lower() == ".xml"
            )
        )
        try:
            if not technical_file and path.stat().st_size > 64 * 1024 * 1024:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if not technical_file:
            if old_bytes in data or old_slash in data:
                preserved_matches.append(relative)
            continue

        changed = data.replace(old_bytes, new_bytes)
        if old_slash != old_bytes:
            changed = changed.replace(old_slash, new_slash)
        if changed != data:
            path.write_bytes(changed)
            replacements += data.count(old_bytes) + data.count(old_slash)

    log(
        f"Updated {replacements:,} package reference(s) in Android technical files."
    )
    if preserved_matches:
        examples = ", ".join(str(path) for path in preserved_matches[:3])
        more = (
            f" and {len(preserved_matches) - 3} more"
            if len(preserved_matches) > 3
            else ""
        )
        log(
            "Notice: the original package also appears in game assets or compiled "
            f"data ({examples}{more}). These were deliberately preserved unchanged; "
            "test the game for runtime or entitlement dependencies."
        )
    return replacements


def copy_with_progress(source: Path, destination: Path, log, progress=None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = source.stat().st_size
    copied = 0
    next_report = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while True:
            chunk = src.read(8 * 1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            copied += len(chunk)
            if progress:
                progress(copied, total)
            percent = int(copied * 100 / total) if total else 100
            if percent >= next_report:
                log(f"Copying {source.name}: {percent}%")
                next_report = percent + 10
    shutil.copystat(source, destination)


def build_release_manifest(
    source_manifest: Path | None,
    output_root: Path,
    new_package: str,
    game_name: str,
    version_code: str,
    apk_output: Path,
    obb_outputs: list[Path],
) -> Path:
    release_name = output_root.name
    if source_manifest:
        metadata = parse_release_manifest(source_manifest)
        release_name = metadata.get("Release Name") or release_name
    total_size = apk_output.stat().st_size + sum(path.stat().st_size for path in obb_outputs)
    size_mb = str(round(total_size / 1_000_000))
    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "#VRPRELEASEMANIFEST 1.0",
        "Game Name;Release Name;Package Name;Version Code;Last Updated;Size (MB);Downloads;Rating;Rating Count",
        f"{game_name};{release_name};{new_package};{version_code};{updated};{size_mb};0;0;0",
        "",
        "#filelist",
        "type;name;size",
        f"f;./{apk_output.name};{apk_output.stat().st_size}",
    ]
    if obb_outputs:
        lines.append(f"d;./{new_package};0")
        for obb in obb_outputs:
            relative = obb.relative_to(output_root).as_posix()
            lines.append(f"f;./{relative};{obb.stat().st_size}")

    path = output_root / "release.manifest"
    path.write_text("\ufeff" + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def desktop_entry_text() -> str:
    """Build a launcher entry that remains tied to this app folder."""
    launcher = SCRIPT_DIR / "launch.sh"
    icon = RESOURCE_DIR / "assets" / "quest-apk-renamer.svg"
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.0",
            f"Name={APP_NAME}",
            "Comment=Rename, rebuild, sign, and install Quest APK/OBB bundles",
            f'Exec="{launcher}" %f',
            f"Icon={icon}",
            "Terminal=false",
            "Categories=Utility;",
            "Keywords=Quest;APK;OBB;Android;Sideload;",
            "StartupNotify=true",
            "StartupWMClass=Tk",
            "MimeType=application/vnd.android.package-archive;",
            "",
        ]
    )


def find_macos_app_bundle(executable: Path | None = None) -> Path | None:
    """Return the containing .app bundle for a frozen macOS executable."""
    if executable is None and not bool(getattr(sys, "frozen", False)):
        return None
    candidate = (executable or Path(sys.executable)).expanduser().resolve()
    for path in (candidate, *candidate.parents):
        if path.suffix.lower() == ".app":
            return path
    return None


def macos_app_is_installed(
    current_bundle: Path | None = None,
    *,
    user_application: Path | None = None,
    system_application: Path | None = None,
) -> bool:
    """Report whether the macOS app is already in an Applications folder."""
    user_target = user_application or MACOS_USER_APPLICATION
    system_target = system_application or MACOS_SYSTEM_APPLICATION
    if user_target.is_dir() or system_target.is_dir():
        return True
    if current_bundle is None:
        current_bundle = find_macos_app_bundle()
    if current_bundle is None:
        return False
    resolved = current_bundle.expanduser().resolve()
    return resolved in {
        user_target.expanduser().resolve(),
        system_target.expanduser().resolve(),
    }


class StatusBadge(Canvas):
    """Small rounded status chip that fits the app's dark visual language."""

    PALETTES = {
        "success": ("#153a30", "#6ee7a8"),
        "neutral": ("#303849", "#c9d3e4"),
        "warning": ("#493b22", "#f4c96b"),
    }

    def __init__(
        self,
        parent,
        text: str,
        *,
        tone: str = "success",
        background: str = "#202633",
    ) -> None:
        self.badge_font = tkfont.Font(family="Sans", size=9, weight="bold")
        self.badge_text = text
        self.tone = tone
        width = self.badge_font.measure(text) + 28
        super().__init__(
            parent,
            width=width,
            height=30,
            bg=background,
            bd=0,
            highlightthickness=0,
            takefocus=0,
        )
        self._draw()

    def set(self, text: str, tone: str) -> None:
        self.badge_text = text
        self.tone = tone
        self.configure(width=self.badge_font.measure(text) + 28)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        fill, foreground = self.PALETTES[self.tone]
        width = int(self.cget("width"))
        height = int(self.cget("height"))
        radius = height // 2
        self.create_oval(1, 1, height - 1, height - 1, fill=fill, outline=fill)
        self.create_oval(
            width - height + 1,
            1,
            width - 1,
            height - 1,
            fill=fill,
            outline=fill,
        )
        self.create_rectangle(
            radius,
            1,
            width - radius,
            height - 1,
            fill=fill,
            outline=fill,
        )
        self.create_text(
            width / 2,
            height / 2,
            text=self.badge_text,
            fill=foreground,
            font=self.badge_font,
        )


class DeviceStatusCard(Canvas):
    """Rounded, clickable Quest connection card for the app header."""

    PALETTES = {
        "success": ("#153a30", "#6ee7a8", "#b6ddcf"),
        "error": ("#42212a", "#ff8a9a", "#dfb6bf"),
        "warning": ("#493b22", "#f4c96b", "#e4d0a0"),
        "neutral": ("#293143", "#d7dfed", "#9ca9bd"),
    }

    def __init__(self, parent, command) -> None:
        super().__init__(
            parent,
            width=246,
            height=58,
            bg="#151923",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            takefocus=1,
        )
        self.command = command
        self.title_font = tkfont.Font(family="Sans", size=9, weight="bold")
        self.detail_font = tkfont.Font(family="Sans", size=8)
        self.title = "Checking Quest…"
        self.detail = "Checking headset storage"
        self.tone = "neutral"
        self.bind("<Button-1>", lambda _event: self.command())
        self.bind("<Return>", lambda _event: self.command())
        self.bind("<space>", lambda _event: self.command())
        self._draw()

    def set(self, title: str, detail: str, tone: str) -> None:
        self.title = title
        self.detail = detail
        self.tone = tone
        self._draw()

    def _rounded_box(self, fill: str) -> None:
        width = int(self.cget("width"))
        height = int(self.cget("height"))
        radius = 18
        self.create_oval(1, 1, radius * 2, height - 1, fill=fill, outline=fill)
        self.create_oval(
            width - radius * 2,
            1,
            width - 1,
            height - 1,
            fill=fill,
            outline=fill,
        )
        self.create_rectangle(
            radius,
            1,
            width - radius,
            height - 1,
            fill=fill,
            outline=fill,
        )

    def _draw(self) -> None:
        self.delete("all")
        fill, title_color, detail_color = self.PALETTES[self.tone]
        self._rounded_box(fill)
        self.create_oval(
            15,
            15,
            25,
            25,
            fill=title_color,
            outline=title_color,
        )
        self.create_text(
            33,
            20,
            text=self.title,
            anchor="w",
            fill=title_color,
            font=self.title_font,
        )
        self.create_text(
            16,
            40,
            text=self.detail,
            anchor="w",
            fill=detail_color,
            font=self.detail_font,
        )
        self.create_text(
            226,
            29,
            text="↻",
            fill=detail_color,
            font=("Sans", 13, "bold"),
        )


class RenamerApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        initial_width = min(900, max(680, self.root.winfo_screenwidth() - 120))
        initial_height = min(900, max(620, self.root.winfo_screenheight() - 140))
        self.root.geometry(f"{initial_width}x{initial_height}")
        self.root.minsize(640, 580)
        icon_path = RESOURCE_DIR / "assets" / "quest-apk-renamer.png"
        self.window_icon = PhotoImage(file=icon_path) if icon_path.is_file() else None
        if self.window_icon:
            self.root.iconphoto(True, self.window_icon)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.bundle: BundleInfo | None = None
        self.last_output: Path | None = None
        self.busy = False
        self.last_progress = 0
        self.device_check_running = False
        self.device_refresh_job = None
        self.readiness_job = None
        self.ready_to_build = False
        self.device_snapshot: dict[str, object] = {}
        self.cancel_event = threading.Event()
        self.operation_kind: str | None = None
        self.failed_obb_retry: dict[str, object] | None = None
        self.bulk_window: Toplevel | None = None
        self.bulk_paths: list[Path] = []
        self.bulk_tree = None
        self.user_settings = load_user_settings()

        self.source_folder_var = StringVar()
        self.apk_var = StringVar()
        self.detected_var = StringVar(
            value="Start by choosing a game folder."
        )
        self.current_info_var = StringVar(value="Current ID and version will appear here.")
        self.package_status_var = StringVar(
            value="A separate app ID will be suggested for you."
        )
        self.output_summary_var = StringVar(
            value="The save location will be chosen automatically."
        )
        self.old_package_var = StringVar()
        self.new_package_var = StringVar()
        self.version_var = StringVar()
        self.output_var = StringVar()
        self.copy_obb_var = BooleanVar(value=True)
        self.sign_var = BooleanVar(value=True)
        self.ask_key_backup_var = BooleanVar(
            value=bool(self.user_settings["ask_signing_key_backup"])
        )
        self.replace_source_var = BooleanVar(value=False)
        self.delete_source_after_install_var = BooleanVar(value=False)
        self.bulk_suffix_var = StringVar(value="a")
        self.bulk_replace_var = BooleanVar(value=False)
        self.bulk_delete_var = BooleanVar(value=False)
        self.tool_var = StringVar()
        self.preflight_var = StringVar(value="Choose a game and the app will check it.")
        self.progress_percent_var = StringVar(value="0%")
        self.status_var = StringVar(value="Choose a game folder to begin")
        self.details_visible = False
        self.advanced_visible = False

        self._configure_style()
        self._build_ui()
        self.new_package_var.trace_add("write", self._on_package_changed)
        self.bulk_suffix_var.trace_add("write", self._refresh_bulk_tree)
        self._refresh_tool_status()
        self.root.bind("<Control-q>", lambda _event: self.root.destroy())
        self.root.after(100, self._drain_events)
        self._schedule_device_refresh(250)

    def _configure_style(self) -> None:
        self.root.configure(bg="#151923")
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=("Sans", 10))
        style.configure("TFrame", background="#151923")
        style.configure("Card.TFrame", background="#202633")
        style.configure("TLabel", background="#151923", foreground="#dfe7f5")
        style.configure("Card.TLabel", background="#202633", foreground="#dfe7f5")
        style.configure(
            "Section.TLabel",
            background="#202633",
            foreground="#f7f9fc",
            font=("Sans", 10, "bold"),
        )
        style.configure(
            "Title.TLabel",
            background="#151923",
            foreground="#ffffff",
            font=("Sans", 22, "bold"),
        )
        style.configure(
            "Subtitle.TLabel", background="#151923", foreground="#96a3b8"
        )
        style.configure("Hint.TLabel", background="#202633", foreground="#9ca9bd")
        style.configure(
            "InlineSuccess.TLabel",
            background="#202633",
            foreground="#6ee7a8",
            font=("Sans", 9, "bold"),
        )
        style.configure(
            "InlineError.TLabel",
            background="#202633",
            foreground="#ff8a9a",
            font=("Sans", 9, "bold"),
        )
        style.configure(
            "InlineHint.TLabel",
            background="#202633",
            foreground="#9ca9bd",
            font=("Sans", 9),
        )
        style.configure(
            "Success.TLabel",
            background="#202633",
            foreground="#6ee7a8",
            font=("Sans", 10, "bold"),
        )
        style.configure(
            "Accent.TButton",
            background="#7c5cff",
            foreground="#ffffff",
            padding=(16, 10),
            font=("Sans", 10, "bold"),
        )
        style.map("Accent.TButton", background=[("active", "#9278ff")])
        style.configure(
            "Big.TButton",
            background="#7c5cff",
            foreground="#ffffff",
            padding=(18, 14),
            font=("Sans", 12, "bold"),
        )
        style.map("Big.TButton", background=[("active", "#9278ff")])
        style.configure(
            "TButton", background="#303849", foreground="#f5f7fb", padding=(10, 7)
        )
        style.map("TButton", background=[("active", "#3b465b")])
        style.configure(
            "TEntry",
            fieldbackground="#11151e",
            foreground="#ffffff",
            insertcolor="#ffffff",
            padding=7,
        )
        style.configure(
            "TCheckbutton", background="#202633", foreground="#dfe7f5"
        )
        style.map(
            "TCheckbutton",
            background=[("active", "#202633")],
            foreground=[("active", "#ffffff")],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background="#7c5cff",
            troughcolor="#11151e",
        )
        style.configure(
            "Dark.Vertical.TScrollbar",
            background="#303849",
            troughcolor="#151923",
            bordercolor="#151923",
            arrowcolor="#96a3b8",
            lightcolor="#303849",
            darkcolor="#303849",
            width=10,
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", "#48536a")],
        )
        style.configure(
            "Treeview",
            background="#11151e",
            fieldbackground="#11151e",
            foreground="#dfe7f5",
            bordercolor="#303849",
            rowheight=28,
        )
        style.map(
            "Treeview",
            background=[("selected", "#4e3e91")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Treeview.Heading",
            background="#303849",
            foreground="#f7f9fc",
            padding=(8, 7),
            font=("Sans", 9, "bold"),
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "#3b465b")],
        )

    def _build_ui(self) -> None:
        viewport = ttk.Frame(self.root)
        viewport.pack(fill=BOTH, expand=True)
        self.scroll_canvas = Canvas(
            viewport,
            bg="#151923",
            bd=0,
            highlightthickness=0,
        )
        scrollbar = ttk.Scrollbar(
            viewport,
            orient="vertical",
            command=self.scroll_canvas.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.scroll_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=RIGHT, fill="y")
        self.scroll_canvas.pack(side=LEFT, fill=BOTH, expand=True)

        outer = ttk.Frame(self.scroll_canvas, padding=(24, 22, 24, 24))
        self.canvas_window = self.scroll_canvas.create_window(
            (0, 0), window=outer, anchor="nw"
        )
        self.outer = outer

        header = ttk.Frame(outer)
        header.pack(fill=X)
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        device_panel = ttk.Frame(header)
        device_panel.grid(row=0, column=1, rowspan=2, sticky="ne")
        self.device_card = DeviceStatusCard(
            device_panel,
            command=self.refresh_device_status,
        )
        self.device_card.grid(row=0, column=0, rowspan=2, sticky="e", padx=(0, 8))
        StatusBadge(
            device_panel,
            f"v{APP_VERSION}",
            tone="neutral",
            background="#151923",
        ).grid(row=0, column=1, sticky="e")
        self.subtitle_label = ttk.Label(
            header,
            text="Choose a game, change its ID, and the app handles the rest.",
            style="Subtitle.TLabel",
        )
        self.subtitle_label.grid(
            row=1, column=0, sticky="w", pady=(2, 18), padx=(0, 16)
        )

        source = ttk.Frame(outer, style="Card.TFrame", padding=16)
        source.pack(fill=X, pady=(0, 12))
        self.source_card = source
        self.source_title_label = ttk.Label(
            source,
            text="1  PICK A GAME",
            style="Section.TLabel",
        )
        self.source_title_label.pack(
            anchor="w"
        )
        self.source_help_label = ttk.Label(
            source,
            text="Choose the main folder. APK and OBB files are found automatically.",
            style="Hint.TLabel",
        )
        self.source_help_label.pack(anchor="w", pady=(4, 10))
        self.choose_folder_button = ttk.Button(
            source,
            text="Choose game folder…",
            style="Big.TButton",
            command=self.choose_folder,
        )
        self.choose_folder_button.pack(fill=X)
        self.drop_hint_var = StringVar(
            value=(
                "Drop a game folder here"
                "  •  drop several folders for Bulk"
                if DND_AVAILABLE
                else "Drag-and-drop support is unavailable; use the button above."
            )
        )
        self.drop_hint_label = ttk.Label(
            source,
            textvariable=self.drop_hint_var,
            style="Hint.TLabel",
        )
        self.drop_hint_label.pack(anchor="center", pady=(8, 0))
        self.source_path_label = ttk.Label(
            source,
            textvariable=self.source_folder_var,
            style="Hint.TLabel",
            wraplength=730,
        )
        self.source_path_label.pack(anchor="w", pady=(10, 0))
        self.detected_label = ttk.Label(
            source,
            textvariable=self.detected_var,
            style="Success.TLabel",
            wraplength=730,
        )
        self.detected_label.pack(anchor="w", pady=(5, 0))

        settings = ttk.Frame(outer, style="Card.TFrame", padding=16)
        settings.pack(fill=X, pady=(0, 12))
        settings.columnconfigure(0, weight=1)
        ttk.Label(settings, text="2  MAKE IT A NEW APP", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.package_help_label = ttk.Label(
            settings,
            text="We add “a” for you. Edit it only if you want a different ID.",
            style="Hint.TLabel",
        )
        self.package_help_label.grid(row=1, column=0, sticky="w", pady=(4, 9))
        self.new_package_entry = ttk.Entry(
            settings, textvariable=self.new_package_var, font=("Sans", 12)
        )
        self.new_package_entry.grid(row=2, column=0, sticky="ew")
        ttk.Label(
            settings,
            textvariable=self.current_info_var,
            style="Hint.TLabel",
        ).grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.package_status_label = ttk.Label(
            settings,
            textvariable=self.package_status_var,
            style="InlineHint.TLabel",
        )
        self.package_status_label.grid(row=4, column=0, sticky="w", pady=(4, 0))

        self.output_card = ttk.Frame(outer, style="Card.TFrame", padding=16)
        self.output_card.pack(fill=X, pady=(0, 12))
        ttk.Label(
            self.output_card, text="3  BUILD OR INSTALL", style="Section.TLabel"
        ).pack(anchor="w")
        self.output_summary_label = ttk.Label(
            self.output_card,
            textvariable=self.output_summary_var,
            style="Hint.TLabel",
            wraplength=730,
        )
        self.output_summary_label.pack(anchor="w", pady=(5, 10))
        readiness_row = ttk.Frame(self.output_card, style="Card.TFrame")
        readiness_row.pack(fill=X, pady=(0, 7))
        self.preflight_badge = StatusBadge(
            readiness_row,
            "Waiting for a game",
            tone="neutral",
        )
        self.preflight_badge.pack(side=LEFT)
        ttk.Label(
            readiness_row,
            text="APK, OBB, manifest, signing, and space are checked automatically.",
            style="Hint.TLabel",
        ).pack(side=LEFT, padx=(9, 0))
        self.preflight_label = ttk.Label(
            self.output_card,
            textvariable=self.preflight_var,
            style="Hint.TLabel",
        )
        self.preflight_label.pack(anchor="w", pady=(0, 11))
        install_actions = ttk.Frame(self.output_card, style="Card.TFrame")
        install_actions.pack(fill=X)
        install_actions.columnconfigure(0, weight=1)
        install_actions.columnconfigure(1, weight=1)
        self.install_button = ttk.Button(
            install_actions,
            text="Install current folder",
            command=self.install_on_quest,
            state="disabled",
        )
        self.install_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.select_install_button = ttk.Button(
            install_actions,
            text="Select folder to install…",
            command=self.choose_install_folder,
        )
        self.select_install_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.retry_obb_button = ttk.Button(
            self.output_card,
            text="Retry failed OBB transfer only",
            command=self.retry_failed_obbs,
        )

        self.advanced_frame = ttk.Frame(outer, style="Card.TFrame", padding=16)
        self.advanced_frame.columnconfigure(1, weight=1)
        ttk.Label(
            self.advanced_frame, text="MORE OPTIONS", style="Card.TLabel"
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(
            self.advanced_frame, text="Output folder", style="Card.TLabel"
        ).grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.output_entry = ttk.Entry(
            self.advanced_frame, textvariable=self.output_var
        )
        self.output_entry.grid(
            row=1, column=1, sticky="ew"
        )
        self.output_button = ttk.Button(
            self.advanced_frame, text="Change…", command=self.choose_output
        )
        self.output_button.grid(row=1, column=2, padx=(8, 0))
        choices = ttk.Frame(self.advanced_frame, style="Card.TFrame")
        choices.grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(9, 0)
        )
        choices.columnconfigure(0, weight=1)
        choices.columnconfigure(1, weight=1)
        self.copy_obb_check = ttk.Checkbutton(
            choices,
            text="Include OBB files",
            variable=self.copy_obb_var,
            command=self._invalidate_preflight,
        )
        self.copy_obb_check.grid(row=0, column=0, sticky="w")
        self.sign_check = ttk.Checkbutton(
            choices,
            text="Sign APK",
            variable=self.sign_var,
            command=self._invalidate_preflight,
        )
        self.sign_check.grid(row=0, column=1, sticky="w")
        self.ask_key_backup_check = ttk.Checkbutton(
            choices,
            text="Backup reminders",
            variable=self.ask_key_backup_var,
            command=self._save_backup_reminder_setting,
        )
        self.ask_key_backup_check.grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        self.replace_source_check = ttk.Checkbutton(
            choices,
            text="Replace source after build",
            variable=self.replace_source_var,
            command=self._on_replace_source_toggle,
        )
        self.replace_source_check.grid(
            row=1, column=1, sticky="w", pady=(4, 0)
        )
        self.delete_source_check = ttk.Checkbutton(
            choices,
            text="Trash local copy after verified install",
            variable=self.delete_source_after_install_var,
            command=self._invalidate_preflight,
        )
        self.delete_source_check.grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        self.tools_button = ttk.Button(
            self.advanced_frame,
            text="Repair / verify Android tools",
            command=self.install_tools,
        )
        self.tools_button.grid(row=3, column=1, sticky="w", pady=(8, 0))
        ttk.Label(
            self.advanced_frame, textvariable=self.tool_var, style="Card.TLabel"
        ).grid(row=3, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
        utility_actions = ttk.Frame(self.advanced_frame, style="Card.TFrame")
        utility_actions.grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(12, 0)
        )
        for column in range(4):
            utility_actions.columnconfigure(column, weight=1)
        ttk.Button(
            utility_actions,
            text="Choose single APK…",
            command=self.choose_apk,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(
            utility_actions,
            text="Open output",
            command=self.open_output,
        ).grid(row=0, column=1, sticky="ew", padx=4)
        self.backup_key_button = ttk.Button(
            utility_actions,
            text="Back up signing key…",
            command=self.backup_signing_key,
            state="normal" if MANAGED_KEYSTORE.is_file() else "disabled",
        )
        self.backup_key_button.grid(row=0, column=2, sticky="ew", padx=4)
        self.cleanup_button = ttk.Button(
            utility_actions,
            text="Clean old output…",
            command=self.cleanup_output,
        )
        self.cleanup_button.grid(row=0, column=3, sticky="ew", padx=(4, 0))
        self.start_button = ttk.Button(
            outer,
            text="Create renamed game",
            style="Big.TButton",
            command=self.start,
            state="disabled",
        )
        self.start_button.pack(fill=X, pady=(0, 10))

        self.progress_row = ttk.Frame(outer)
        self.progress = ttk.Progressbar(
            self.progress_row,
            mode="determinate",
            maximum=100,
        )
        self.progress.pack(side=LEFT, fill=X, expand=True)
        self.cancel_button = ttk.Button(
            self.progress_row,
            text="Cancel safely",
            command=self.request_cancel,
            state="disabled",
        )
        self.cancel_button.pack(side=RIGHT, padx=(10, 0))
        ttk.Label(
            self.progress_row,
            textvariable=self.progress_percent_var,
            font=("Sans", 10, "bold"),
        ).pack(side=RIGHT, padx=(10, 0))
        self.status_label = ttk.Label(outer, textvariable=self.status_var)
        self.status_label.pack(anchor="w")

        self.footer = ttk.Frame(outer)
        self.footer.pack(fill=X, pady=(10, 0))
        self.advanced_button = ttk.Button(
            self.footer, text="More options", command=self.toggle_advanced
        )
        self.advanced_button.pack(side=LEFT)
        self.bulk_button = ttk.Button(
            self.footer,
            text="Bulk tools…",
            command=self.open_bulk_tools,
        )
        self.bulk_button.pack(side=LEFT, padx=(8, 0))
        if sys.platform.startswith("win"):
            launcher_text = (
                "In Start Menu ✓"
                if WINDOWS_START_MENU.is_file()
                else "Add to Start Menu"
            )
        elif sys.platform == "darwin":
            launcher_text = (
                "In Applications ✓"
                if macos_app_is_installed()
                else "Add to Applications"
            )
        else:
            launcher_text = (
                "In Applications ✓"
                if LINUX_LAUNCHER.is_file()
                else "Add to Applications"
            )
        self.launcher_button = ttk.Button(
            self.footer,
            text=launcher_text,
            command=self.install_app_launcher,
        )
        self.launcher_button.pack(side=LEFT, padx=(8, 0))
        if not (
            sys.platform.startswith("linux")
            or sys.platform.startswith("win")
            or (
                sys.platform == "darwin"
                and find_macos_app_bundle() is not None
            )
        ):
            self.launcher_button.configure(state="disabled")
        self.details_button = ttk.Button(
            self.footer, text="Show details", command=self.toggle_details
        )
        self.details_button.pack(side=RIGHT)
        ttk.Button(
            self.footer,
            text="About",
            command=self.show_about,
        ).pack(side=RIGHT, padx=(0, 8))

        self.log = Text(
            outer,
            height=9,
            bg="#0e121a",
            fg="#bdc9dc",
            insertbackground="#ffffff",
            relief="flat",
            padx=10,
            pady=10,
            font=("Monospace", 9),
            state="disabled",
        )
        self._append_log(
            "Source folders stay unchanged unless Replace source is explicitly enabled. "
            "Use only APKs you own or are authorized to change."
        )
        self._enable_drag_and_drop()
        self.scroll_canvas.bind("<Configure>", self._on_canvas_configure)
        outer.bind("<Configure>", self._on_content_configure)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_mousewheel, add="+")

    def _on_canvas_configure(self, event) -> None:
        self.scroll_canvas.itemconfigure(self.canvas_window, width=event.width)
        wraplength = max(300, event.width - 90)
        self.subtitle_label.configure(wraplength=max(240, event.width - 390))
        for label in (
            self.source_help_label,
            self.source_path_label,
            self.detected_label,
            self.package_help_label,
            self.output_summary_label,
            self.preflight_label,
        ):
            label.configure(wraplength=wraplength)

    def _on_content_configure(self, _event=None) -> None:
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def _on_mousewheel(self, event) -> None:
        if event.num == 4:
            direction = -1
        elif event.num == 5:
            direction = 1
        else:
            direction = -1 if event.delta > 0 else 1
        self.scroll_canvas.yview_scroll(direction * 3, "units")

    def _enable_drag_and_drop(self) -> None:
        if not DND_AVAILABLE or DND_FILES is None:
            return
        widgets = (
            self.source_card,
            self.source_title_label,
            self.source_help_label,
            self.choose_folder_button,
            self.drop_hint_label,
            self.source_path_label,
            self.detected_label,
        )
        for widget in widgets:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            widget.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop_enter(self, _event):
        if not self.busy:
            self.drop_hint_var.set("Release to load this game folder")
            self.drop_hint_label.configure(style="InlineSuccess.TLabel")
        return "copy"

    def _on_drop_leave(self, _event):
        self.drop_hint_var.set(
            "Drop a game folder here  •  drop several folders for Bulk"
        )
        self.drop_hint_label.configure(style="Hint.TLabel")
        return "copy"

    def _on_drop(self, event):
        self._on_drop_leave(event)
        if self.busy:
            return "copy"
        raw_paths = self.root.tk.splitlist(event.data)
        paths = []
        for raw_path in raw_paths:
            if raw_path.startswith("file://"):
                raw_path = urllib.request.url2pathname(raw_path[7:])
            paths.append(Path(raw_path).expanduser())
        if len(paths) > 1:
            if not all(path.is_dir() for path in paths):
                messagebox.showerror(
                    "Drop game folders",
                    "For a bulk job, every dropped item must be a game folder.",
                )
                return "copy"
            self.open_bulk_tools(paths)
            return "copy"
        if not paths:
            return "copy"
        path = paths[0]
        try:
            if path.is_dir():
                bundle = detect_bundle_from_folder(path)
            elif path.is_file() and path.suffix.lower() == ".apk":
                bundle = detect_bundle(path)
            else:
                raise UserError(
                    "Drop the game folder containing the APK and OBB files."
                )
        except UserError as exc:
            messagebox.showerror("Cannot load dropped item", str(exc))
            return "copy"
        self._load_bundle(bundle)
        self.drop_hint_var.set("✓ Folder loaded — drop another folder to replace it")
        self.drop_hint_label.configure(style="InlineSuccess.TLabel")
        return "copy"

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(END, message.rstrip() + "\n")
        self.log.see(END)
        self.log.configure(state="disabled")

    def _refresh_tool_status(self) -> None:
        ready, detail = tool_status()
        self.tool_var.set(("✓ " if ready else "⚠ ") + detail)
        if self.bundle is not None:
            self._invalidate_preflight()

    def _schedule_device_refresh(self, delay_ms: int = 15000) -> None:
        if self.device_refresh_job is not None:
            self.root.after_cancel(self.device_refresh_job)
        self.device_refresh_job = self.root.after(
            delay_ms, self.refresh_device_status
        )

    def refresh_device_status(self) -> None:
        self.device_refresh_job = None
        if self.device_check_running:
            return
        if self.busy:
            self._schedule_device_refresh(3000)
            return
        self.device_check_running = True
        self.device_card.set(
            "Checking Quest…",
            "Checking headset storage",
            "neutral",
        )
        threading.Thread(
            target=self._device_status_worker,
            daemon=True,
        ).start()

    def _device_status_worker(self) -> None:
        adb = find_adb()
        if not adb:
            self.events.put(
                (
                    "device_status",
                    {
                        "label": "ADB unavailable",
                        "detail": "Android Platform Tools not found",
                        "tone": "error",
                        "state": "unavailable",
                    },
                )
            )
            return
        try:
            devices_result = subprocess.run(
                [str(adb), "devices", "-l"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
            if devices_result.returncode != 0:
                raise OSError(
                    devices_result.stderr.strip()
                    or devices_result.stdout.strip()
                    or "ADB device check failed"
                )
            connected, unavailable = parse_adb_devices(devices_result.stdout)
            if not connected:
                if unavailable:
                    label = "USB approval needed"
                    detail = "Put on the Quest and approve debugging"
                    tone = "warning"
                else:
                    label = "Quest not connected"
                    detail = "Connect the headset by USB"
                    tone = "error"
                self.events.put(
                    (
                        "device_status",
                        {
                            "label": label,
                            "detail": detail,
                            "tone": tone,
                            "state": "unauthorized"
                            if unavailable
                            else "disconnected",
                        },
                    )
                )
                return
            if len(connected) > 1:
                self.events.put(
                    (
                        "device_status",
                        {
                            "label": f"{len(connected)} devices connected",
                            "detail": "Leave only the Quest connected",
                            "tone": "warning",
                            "state": "multiple",
                        },
                    )
                )
                return
            serial = connected[0]
            model = parse_adb_model(devices_result.stdout, serial)
            storage_result = subprocess.run(
                [str(adb), "-s", serial, "shell", "df", "-k", "/sdcard"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
            storage = (
                parse_df_storage(storage_result.stdout)
                if storage_result.returncode == 0
                else None
            )
            detail = "Connected • storage unavailable"
            if storage:
                available, total = storage
                detail = f"{format_size(available)} free of {format_size(total)}"
            self.events.put(
                (
                    "device_status",
                    {
                        "label": f"{model} connected",
                        "detail": detail,
                        "tone": "success",
                        "state": "connected",
                        "serial": serial,
                        "available": storage[0] if storage else 0,
                        "total": storage[1] if storage else 0,
                    },
                )
            )
        except (OSError, subprocess.TimeoutExpired):
            self.events.put(
                (
                    "device_status",
                    {
                        "label": "Quest check failed",
                        "detail": "Use Refresh to try again",
                        "tone": "error",
                        "state": "error",
                    },
                )
            )

    def _apply_device_status(self, payload: dict[str, object]) -> None:
        self.device_check_running = False
        self.device_snapshot = dict(payload)
        self.device_card.set(
            str(payload["label"]),
            str(payload["detail"]),
            str(payload["tone"]),
        )
        self._schedule_device_refresh(3000 if self.busy else 15000)

    def _on_package_changed(self, *_args) -> None:
        new_package = self.new_package_var.get().strip()
        old_package = self.old_package_var.get().strip()
        if self.bundle is None:
            message = "A separate app ID will be suggested for you."
            style = "InlineHint.TLabel"
        elif new_package == old_package:
            message = "Add a letter so Quest sees this as a separate app."
            style = "InlineHint.TLabel"
        elif not is_valid_package(new_package):
            message = "Use letters, numbers, underscores, and at least one dot."
            style = "InlineError.TLabel"
        else:
            message = "✓ Ready to install as a separate app."
            style = "InlineSuccess.TLabel"
        self.package_status_var.set(message)
        self.package_status_label.configure(style=style)
        self._invalidate_preflight()
        self._update_action_state()

    def _invalidate_preflight(self) -> None:
        self.ready_to_build = False
        if self.bundle is None:
            self.preflight_var.set("Choose a game and the app will check it.")
            self.preflight_badge.set("Waiting for a game", "neutral")
        else:
            self.preflight_var.set("Checking files, tools, and free space…")
            self.preflight_badge.set("Checking…", "neutral")
        self._update_action_state()
        self._schedule_readiness_check()

    def _schedule_readiness_check(self, delay_ms: int = 250) -> None:
        if self.readiness_job is not None:
            self.root.after_cancel(self.readiness_job)
        self.readiness_job = self.root.after(
            delay_ms, self._run_automatic_readiness_check
        )

    def _run_automatic_readiness_check(self) -> None:
        self.readiness_job = None
        if self.busy or self.bundle is None:
            return
        try:
            self._perform_preflight()
        except UserError as exc:
            self.ready_to_build = False
            self.preflight_var.set(str(exc))
            self.preflight_badge.set("Needs attention", "warning")
        else:
            self.ready_to_build = True
        self._update_action_state()

    def _save_backup_reminder_setting(self) -> None:
        enabled = self.ask_key_backup_var.get()
        self.user_settings["ask_signing_key_backup"] = enabled
        try:
            save_user_settings(self.user_settings)
        except OSError as exc:
            messagebox.showerror(
                "Could not save setting",
                "The backup-reminder preference will apply for this session, "
                f"but could not be saved:\n\n{exc}",
            )
            return
        self._append_log(
            "Signing-key backup reminders "
            + ("enabled." if enabled else "disabled.")
        )

    def choose_folder(self) -> None:
        initial = (
            Path(self.source_folder_var.get()).parent
            if self.source_folder_var.get()
            else Path.home() / "Downloads"
        )
        selected = native_choose_directory(
            "Choose the folder containing your APK and OBB",
            initial,
        )
        if not selected:
            return
        try:
            bundle = detect_bundle_from_folder(Path(selected))
        except UserError as exc:
            messagebox.showerror("That folder is not ready", str(exc))
            return
        self._load_bundle(bundle)

    def choose_apk(self) -> None:
        initial = (
            Path(self.source_folder_var.get())
            if self.source_folder_var.get()
            else Path.home() / "Downloads"
        )
        selected = native_choose_apk(
            "Choose a single Quest APK",
            initial,
        )
        if selected:
            self.apk_var.set(selected)
            self.inspect_apk()

    def inspect_apk(self) -> None:
        try:
            bundle = detect_bundle(Path(self.apk_var.get()))
        except UserError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._load_bundle(bundle)

    def _load_bundle(self, bundle: BundleInfo) -> None:
        self.bundle = bundle
        self.source_folder_var.set(str(bundle.root))
        self.apk_var.set(str(bundle.apk))
        self.old_package_var.set(bundle.package_name)
        try:
            suggestion = package_with_suffix(bundle.package_name, "a")
        except UserError:
            suggestion = bundle.package_name or "com.example.myquestgamea"
        self.new_package_var.set(suggestion)
        self.version_var.set(bundle.version_code)
        output = self._apply_output_mode()
        if (bundle.root / "RENAMED-BUNDLE.txt").is_file():
            self.last_output = bundle.root
            self.install_button.configure(
                text="Install this folder on Quest",
                state="normal",
            )
        elif output.is_dir() and any(output.glob("*.apk")):
            self.last_output = output
            self.install_button.configure(
                text="Install existing copy",
                state="normal",
            )
        else:
            self.last_output = None
            self.install_button.configure(
                text="Install current folder",
                state="disabled",
            )

        found = [f"APK ({bundle.apk.stat().st_size / 1_000_000:.1f} MB)"]
        if bundle.obbs:
            found.append(f"{len(bundle.obbs)} OBB file" + ("s" if len(bundle.obbs) != 1 else ""))
        if bundle.release_manifest:
            found.append("release.manifest")
        name = bundle.game_name or bundle.root.name
        self.detected_var.set(f"✓ Found {name}: " + " + ".join(found))
        self.current_info_var.set(
            f"Current ID: {bundle.package_name or 'not detected'}"
            f"    •    Version: {bundle.version_code or 'not detected'}"
        )
        self._append_log(f"Loaded source bundle: {bundle.root}")
        self.status_var.set("Game found — we suggested a new ID and checked the rest")
        self._update_action_state()
        self.new_package_entry.focus_set()
        self.new_package_entry.selection_clear()
        self.new_package_entry.icursor(END)

    def _apply_output_mode(self) -> Path:
        assert self.bundle is not None
        if self.replace_source_var.get():
            output = self.bundle.root
            self.output_var.set(str(output))
            self.output_summary_var.set(
                "Safely replaces the original only after the new game is verified:\n"
                f"{output}"
            )
            self.output_entry.configure(state="disabled")
            self.output_button.configure(state="disabled")
            self.delete_source_after_install_var.set(False)
            self.delete_source_check.configure(state="disabled")
        else:
            output = self.bundle.root.parent / f"{self.bundle.root.name} - Renamed"
            self.output_var.set(str(output))
            self.output_summary_var.set(
                f"Saved beside the original as:\n{output}"
            )
            self.output_entry.configure(state="normal")
            self.output_button.configure(state="normal")
            self.delete_source_check.configure(state="normal")
        self._invalidate_preflight()
        return output

    def _on_replace_source_toggle(self) -> None:
        if self.replace_source_var.get():
            if not messagebox.askyesno(
                "Replace the source folder?",
                "The app will build and verify the complete renamed bundle in a "
                "separate staging folder first.\n\nOnly after a successful build "
                "will it replace the selected source folder. The original folder "
                "will be moved to Trash so it can still be restored.\n\n"
                "Enable source replacement?",
            ):
                self.replace_source_var.set(False)
        if self.bundle is not None:
            self._apply_output_mode()
        self._update_action_state()

    def choose_output(self) -> None:
        if self.replace_source_var.get():
            return
        initial = (
            Path(self.output_var.get()).parent
            if self.output_var.get()
            else Path.home()
        )
        selected = native_choose_directory("Choose the output folder", initial)
        if selected:
            self.output_var.set(selected)
            self.output_summary_var.set(
                f"The finished copy will be saved to:\n{Path(selected).resolve()}"
            )
            self._invalidate_preflight()

    def choose_install_folder(self) -> None:
        initial = (
            self.last_output.parent
            if self.last_output
            else (
                Path(self.source_folder_var.get()).parent
                if self.source_folder_var.get()
                else Path.home() / "Downloads"
            )
        )
        selected = native_choose_directory(
            "Choose the finished renamed folder",
            initial,
        )
        if selected:
            self._prepare_quest_install(Path(selected))

    def open_bulk_tools(self, initial_paths: list[Path] | None = None) -> None:
        if self.busy:
            return
        if self.bulk_window is not None and self.bulk_window.winfo_exists():
            self.bulk_window.lift()
            self.bulk_window.focus_force()
            for path in initial_paths or []:
                self._add_bulk_path(path)
            return

        window = Toplevel(self.root)
        self.bulk_window = window
        window.title("Bulk rename and install")
        window.geometry("760x640")
        window.minsize(660, 560)
        window.configure(bg="#151923")
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self._close_bulk_tools)

        outer = ttk.Frame(window, padding=18)
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(
            outer,
            text="Bulk rename and install",
            style="Title.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "Pick several APKs at once, add folders, or scan a parent folder. "
                "The app handles each game one at a time."
            ),
            style="Subtitle.TLabel",
            wraplength=700,
        ).pack(anchor="w", pady=(3, 12))

        columns = ("folder", "current", "new")
        tree = ttk.Treeview(
            outer,
            columns=columns,
            show="headings",
            selectmode="extended",
            height=7,
        )
        self.bulk_tree = tree
        tree.heading("folder", text="Game folder")
        tree.heading("current", text="Current package")
        tree.heading("new", text="Bulk rename result")
        tree.column("folder", width=250, minwidth=160)
        tree.column("current", width=205, minwidth=150)
        tree.column("new", width=220, minwidth=160)
        tree.pack(fill=BOTH, expand=True)

        list_actions = ttk.Frame(outer)
        list_actions.pack(fill=X, pady=(8, 12))
        ttk.Button(
            list_actions,
            text="Add APKs…",
            command=self._bulk_add_apks,
        ).pack(side=LEFT)
        ttk.Button(
            list_actions,
            text="Add folder…",
            command=self._bulk_add_one,
        ).pack(side=LEFT, padx=(8, 0))
        ttk.Button(
            list_actions,
            text="Scan parent…",
            command=self._bulk_add_parent,
        ).pack(side=LEFT, padx=(8, 0))
        ttk.Button(
            list_actions,
            text="Remove selected",
            command=self._bulk_remove_selected,
        ).pack(side=RIGHT)
        ttk.Button(
            list_actions,
            text="Clear",
            command=self._bulk_clear,
        ).pack(side=RIGHT, padx=(0, 8))

        options = ttk.Frame(outer, style="Card.TFrame", padding=12)
        options.pack(fill=X)
        options.columnconfigure(1, weight=1)
        ttk.Label(
            options,
            text="Package suffix",
            style="Card.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(options, textvariable=self.bulk_suffix_var, width=12).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(
            options,
            text="Example: com.example.game + a → com.example.gamea",
            style="Hint.TLabel",
        ).grid(row=1, column=1, sticky="w", pady=(3, 7))
        ttk.Checkbutton(
            options,
            text="Replace each source folder only after its build succeeds",
            variable=self.bulk_replace_var,
        ).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(
            options,
            text="Move each local folder to Trash only after its install verifies",
            variable=self.bulk_delete_var,
        ).grid(row=3, column=1, sticky="w", pady=(3, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill=X, pady=(12, 0))
        self.bulk_rename_button = ttk.Button(
            actions,
            text="Rename all",
            style="Accent.TButton",
            command=self.start_bulk_rename,
        )
        self.bulk_rename_button.pack(side=LEFT, fill=X, expand=True, padx=(0, 4))
        self.bulk_install_button = ttk.Button(
            actions,
            text="Install all",
            style="Accent.TButton",
            command=self.start_bulk_install,
        )
        self.bulk_install_button.pack(side=LEFT, fill=X, expand=True, padx=(4, 0))

        for path in initial_paths or []:
            self._add_bulk_path(path)
        self._refresh_bulk_tree()

    def _close_bulk_tools(self) -> None:
        if self.bulk_window is not None:
            self.bulk_window.destroy()
        self.bulk_window = None
        self.bulk_tree = None

    def _add_bulk_path(self, path: Path) -> None:
        path = path.expanduser().resolve()
        try:
            detect_bundle_from_folder(path)
        except UserError as exc:
            messagebox.showerror(
                "Cannot add folder",
                f"{path}\n\n{exc}",
                parent=self.bulk_window,
            )
            return
        for existing in self.bulk_paths:
            if path == existing:
                return
            if path in existing.parents or existing in path.parents:
                messagebox.showerror(
                    "Folders cannot be nested",
                    "Do not add a parent folder and one of its child folders to "
                    "the same batch.",
                    parent=self.bulk_window,
                )
                return
        self.bulk_paths.append(path)
        self._refresh_bulk_tree()

    def _bulk_add_one(self) -> None:
        selected = native_choose_directory(
            "Add a game folder to the batch",
            self.bulk_paths[-1].parent
            if self.bulk_paths
            else Path.home() / "Downloads",
        )
        if selected:
            self._add_bulk_path(Path(selected))

    def _bulk_add_apks(self) -> None:
        selected = native_choose_apks(
            "Choose one or more Quest APKs",
            self.bulk_paths[-1].parent
            if self.bulk_paths
            else Path.home() / "Downloads",
        )
        if not selected:
            return
        skipped = []
        added_before = len(self.bulk_paths)
        for selected_apk in selected:
            apk = Path(selected_apk).expanduser().resolve()
            try:
                bundle = detect_bundle(apk)
                if not is_valid_package(bundle.package_name):
                    raise UserError("The package ID could not be detected.")
            except UserError as exc:
                skipped.append(f"{apk.name}: {exc}")
                continue
            self._add_bulk_path(bundle.root)
        if skipped:
            messagebox.showwarning(
                "Some APKs need attention",
                f"Added {len(self.bulk_paths) - added_before} game(s).\n\n"
                + "\n".join(skipped[:8]),
                parent=self.bulk_window,
            )

    def _bulk_add_parent(self) -> None:
        selected = native_choose_directory(
            "Choose a parent containing game folders",
            self.bulk_paths[-1].parent
            if self.bulk_paths
            else Path.home() / "Downloads",
        )
        if not selected:
            return
        try:
            found, skipped = discover_bundle_folders(Path(selected))
        except UserError as exc:
            messagebox.showerror(
                "Cannot scan parent folder",
                str(exc),
                parent=self.bulk_window,
            )
            return
        for path in found:
            self._add_bulk_path(path)
        if not found:
            detail = "\n".join(skipped[:8])
            messagebox.showinfo(
                "No game folders found",
                "No ready game bundles were found in that folder or its direct "
                f"subfolders.{chr(10) + chr(10) + detail if detail else ''}",
                parent=self.bulk_window,
            )

    def _bulk_remove_selected(self) -> None:
        if self.bulk_tree is None:
            return
        indexes = sorted(
            (int(item) for item in self.bulk_tree.selection()),
            reverse=True,
        )
        for index in indexes:
            if 0 <= index < len(self.bulk_paths):
                self.bulk_paths.pop(index)
        self._refresh_bulk_tree()

    def _bulk_clear(self) -> None:
        self.bulk_paths.clear()
        self._refresh_bulk_tree()

    def _refresh_bulk_tree(self, *_args) -> None:
        if self.bulk_tree is None:
            return
        for item in self.bulk_tree.get_children():
            self.bulk_tree.delete(item)
        suffix = self.bulk_suffix_var.get().strip()
        for index, path in enumerate(self.bulk_paths):
            try:
                bundle = detect_bundle_from_folder(path)
                current = bundle.package_name or "not detected"
                renamed = package_with_suffix(current, suffix)
            except UserError as exc:
                current = "needs attention"
                renamed = str(exc)
            self.bulk_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(path.name, current, renamed),
            )
        state = "normal" if self.bulk_paths and not self.busy else "disabled"
        if hasattr(self, "bulk_rename_button"):
            self.bulk_rename_button.configure(state=state)
            self.bulk_install_button.configure(state=state)

    def _collect_bulk_bundles(self) -> list[BundleInfo]:
        if not self.bulk_paths:
            raise UserError("Add at least one game folder.")
        bundles = []
        seen_packages = set()
        for path in self.bulk_paths:
            bundle = detect_bundle_from_folder(path)
            if not is_valid_package(bundle.package_name):
                raise UserError(
                    f"{path.name}: the package ID could not be detected."
                )
            if bundle.package_name in seen_packages:
                raise UserError(
                    f"The package ID {bundle.package_name} appears more than once."
                )
            seen_packages.add(bundle.package_name)
            bundles.append(bundle)
        return bundles

    def start_bulk_rename(self) -> None:
        if self.busy:
            return
        copy_obbs = self.copy_obb_var.get()
        should_sign = self.sign_var.get()
        replace_sources = self.bulk_replace_var.get()
        try:
            bundles = self._collect_bulk_bundles()
            suffix = self.bulk_suffix_var.get().strip()
            jobs = []
            new_packages = set()
            space_by_device: dict[int, dict[str, object]] = {}
            for bundle in bundles:
                new_package = package_with_suffix(bundle.package_name, suffix)
                if new_package in new_packages:
                    raise UserError(
                        f"More than one folder would become {new_package}."
                    )
                if bundle.obbs and not bundle.version_code:
                    raise UserError(
                        f"{bundle.root.name}: a version code is required for its OBB."
                    )
                if replace_sources and bundle.obbs and not copy_obbs:
                    raise UserError(
                        f"{bundle.root.name}: source replacement requires OBB copying "
                        "so the replacement remains complete."
                    )
                unreadable = [
                    path.name
                    for path in [bundle.apk, *bundle.obbs]
                    if not os.access(path, os.R_OK)
                ]
                if unreadable:
                    raise UserError(
                        f"{bundle.root.name}: unreadable files: "
                        + ", ".join(unreadable)
                    )
                new_packages.add(new_package)
                output = (
                    bundle.root
                    if replace_sources
                    else bundle.root.parent / f"{bundle.root.name} - Renamed"
                )
                jobs.append((bundle, new_package, output))
                disk_parent = existing_parent(output.parent)
                device = os.stat(disk_parent).st_dev
                entry = space_by_device.setdefault(
                    device,
                    {"path": disk_parent, "required": 0},
                )
                entry["required"] = int(entry["required"]) + required_working_space(
                    bundle, copy_obbs
                )
            for entry in space_by_device.values():
                free = shutil.disk_usage(entry["path"]).free
                required = int(entry["required"])
                if free < required:
                    raise UserError(
                        f"The batch needs about {format_size(required)}, but only "
                        f"{format_size(free)} is free on {entry['path']}."
                    )
            ready, detail = tool_status()
            if not ready:
                raise UserError(f"{detail}. Repair the Android tools first.")
        except UserError as exc:
            messagebox.showerror(
                "Bulk rename is not ready",
                str(exc),
                parent=self.bulk_window,
            )
            return

        warning = (
            "\n\nEach original folder will be moved to Trash only after its "
            "complete replacement is built and activated."
            if replace_sources
            else "\n\nOriginal folders will remain unchanged."
        )
        if not messagebox.askyesno(
            "Rename all selected folders?",
            f"Folders: {len(jobs)}\nPackage suffix: {suffix}\n"
            "Jobs will run one at a time." + warning,
            parent=self.bulk_window,
        ):
            return
        self._close_bulk_tools()
        self.cancel_event.clear()
        self.operation_kind = "bulk_rename"
        self._set_busy(
            True,
            f"Preparing bulk rename of {len(jobs)} folders…",
            cancellable=True,
        )
        threading.Thread(
            target=self._bulk_rename_worker,
            args=(jobs, replace_sources, copy_obbs, should_sign),
            daemon=True,
        ).start()

    def start_bulk_install(self) -> None:
        if self.busy:
            return
        try:
            bundles = self._collect_bulk_bundles()
            delete_sources = self.bulk_delete_var.get()
            if self.device_snapshot.get("state") != "connected":
                raise UserError(
                    "Connect and authorize one Quest, then refresh the device "
                    "status card before starting a bulk install."
                )
            if delete_sources:
                unmanaged = [
                    bundle.root.name
                    for bundle in bundles
                    if not is_managed_output(bundle.root)
                ]
                if unmanaged:
                    raise UserError(
                        "Automatic cleanup only accepts app-created renamed "
                        "folders. Missing RENAMED-BUNDLE.txt:\n"
                        + "\n".join(unmanaged)
                    )
            conflicts = [
                bundle.package_name
                for bundle in bundles
                if self._package_conflict_on_connected_quest(bundle.package_name)
            ]
        except UserError as exc:
            messagebox.showerror(
                "Bulk install is not ready",
                str(exc),
                parent=self.bulk_window,
            )
            return

        total_required = sum(required_quest_space(bundle) for bundle in bundles)
        available = int(self.device_snapshot.get("available", 0) or 0)
        space_warning = ""
        if available and total_required > available:
            space_warning = (
                f"\n\n⚠ The queue may need {format_size(total_required)}, while "
                f"the Quest reports {format_size(available)} free."
            )
        cleanup_note = (
            "\n\nAfter each verified install, that local folder will be moved "
            "to Trash. Failed folders will be kept."
            if delete_sources
            else "\n\nLocal folders will be kept."
        )
        conflict_note = (
            f"\n\n{len(conflicts)} package"
            f"{'s are' if len(conflicts) != 1 else ' is'} already installed and "
            "will be updated/replaced."
            if conflicts
            else ""
        )
        if not messagebox.askyesno(
            "Install all selected folders?",
            f"Folders: {len(bundles)}\nInstalls will run one at a time."
            + space_warning
            + conflict_note
            + cleanup_note,
            parent=self.bulk_window,
        ):
            return
        self._close_bulk_tools()
        self.cancel_event.clear()
        self.operation_kind = "bulk_install"
        self._set_busy(
            True,
            f"Preparing bulk install of {len(bundles)} folders…",
            cancellable=True,
        )
        threading.Thread(
            target=self._bulk_install_worker,
            args=(bundles, delete_sources),
            daemon=True,
        ).start()

    def install_on_quest(self) -> None:
        if self.busy:
            return
        if self.last_output and self.last_output.is_dir():
            self._prepare_quest_install(self.last_output)
        else:
            self.choose_install_folder()

    def _prepare_quest_install(self, folder: Path) -> None:
        folder = folder.expanduser().resolve()
        try:
            bundle = detect_bundle_from_folder(folder)
        except UserError as exc:
            messagebox.showerror("Cannot install that folder", str(exc))
            return
        if not is_valid_package(bundle.package_name):
            messagebox.showerror(
                "Cannot install that folder",
                "The package ID could not be detected. Choose the finished renamed folder.",
            )
            return
        delete_source_after = self.delete_source_after_install_var.get()
        if delete_source_after and not is_managed_output(folder):
            messagebox.showerror(
                "Source cleanup is not available",
                "For safety, automatic cleanup only accepts a finished folder "
                "created by Quest APK Renamer. Turn off the cleanup option or "
                "choose a folder containing RENAMED-BUNDLE.txt.",
            )
            return
        quest_required = required_quest_space(bundle)
        quest_available = int(self.device_snapshot.get("available", 0) or 0)
        if not quest_available:
            quest_available = self._connected_quest_available_space()
        if (
            quest_available
            and quest_required > quest_available
            and not messagebox.askyesno(
                "Quest may not have enough space",
                f"This install needs about {format_size(quest_required)}, but the "
                f"connected Quest reports only {format_size(quest_available)} free.\n\n"
                "The transfer is likely to fail. Continue anyway?",
            )
        ):
            return
        conflict = self._package_conflict_on_connected_quest(bundle.package_name)
        obb_size = sum(path.stat().st_size for path in bundle.obbs)
        summary = (
            f"Package: {bundle.package_name}\n"
            f"APK: {bundle.apk.name}\n"
            f"OBB files: {len(bundle.obbs)} ({obb_size / 1_000_000_000:.2f} GB)\n\n"
            "This installs the APK and copies its OBB data to the connected Quest. "
            "Keep the headset awake and connected by USB."
        )
        if delete_source_after:
            summary += (
                "\n\nAfter the APK and every OBB pass verification, this local "
                "finished folder will be moved to Trash. It will not be removed "
                "if any install or verification step fails."
            )
        title = "Install on Quest?"
        if conflict:
            title = "Package already installed"
            summary += (
                "\n\n⚠ This package ID is already installed on the connected Quest. "
                "Continuing will update/replace that copy. Its app data is normally "
                "kept, but the install will fail if it was signed with a different key."
            )
        if not messagebox.askyesno(title, summary):
            return
        self.failed_obb_retry = None
        self.retry_obb_button.pack_forget()
        self.cancel_event.clear()
        self.operation_kind = "install"
        self._set_busy(
            True,
            "Checking for a connected Quest…",
            cancellable=True,
        )
        self._set_progress(2, "Checking for a connected Quest…")
        threading.Thread(
            target=self._quest_install_worker,
            args=(bundle, conflict, delete_source_after),
            daemon=True,
        ).start()

    @staticmethod
    def _connected_quest_available_space() -> int:
        adb = find_adb()
        if not adb:
            return 0
        try:
            devices = subprocess.run(
                [str(adb), "devices"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
            connected, _unavailable = parse_adb_devices(devices.stdout)
            if devices.returncode != 0 or len(connected) != 1:
                return 0
            storage_result = subprocess.run(
                [str(adb), "-s", connected[0], "shell", "df", "-k", "/sdcard"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
            storage = parse_df_storage(storage_result.stdout)
            return storage[0] if storage_result.returncode == 0 and storage else 0
        except (OSError, subprocess.TimeoutExpired):
            return 0

    def _package_conflict_on_connected_quest(self, package_name: str) -> bool:
        adb = find_adb()
        if not adb:
            return False
        try:
            devices = subprocess.run(
                [str(adb), "devices"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
            connected, _unavailable = parse_adb_devices(devices.stdout)
            if devices.returncode != 0 or len(connected) != 1:
                return False
            result = subprocess.run(
                [
                    str(adb),
                    "-s",
                    connected[0],
                    "shell",
                    "pm",
                    "path",
                    package_name,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
            return package_is_installed(result.returncode, result.stdout)
        except (OSError, subprocess.TimeoutExpired):
            return False

    def toggle_advanced(self) -> None:
        self.advanced_visible = not self.advanced_visible
        if self.advanced_visible:
            self.advanced_frame.pack(
                after=self.output_card, fill=X, pady=(0, 12)
            )
            self.advanced_button.configure(text="Hide options")
            self.root.after_idle(lambda: self.scroll_canvas.yview_moveto(1.0))
        else:
            self.advanced_frame.pack_forget()
            self.advanced_button.configure(text="More options")
        self.root.after_idle(self._on_content_configure)

    def toggle_details(self) -> None:
        self.details_visible = not self.details_visible
        if self.details_visible:
            self.log.pack(
                before=self.footer, fill=X, pady=(10, 0)
            )
            self.details_button.configure(text="Hide details")
            self.root.after_idle(lambda: self.scroll_canvas.yview_moveto(1.0))
        else:
            self.log.pack_forget()
            self.details_button.configure(text="Show details")
        self.root.after_idle(self._on_content_configure)

    def install_tools(self) -> None:
        if self.busy:
            return
        self._set_busy(True, "Installing and verifying tools…")
        threading.Thread(target=self._install_tools_worker, daemon=True).start()

    def _copy_obbs_to_quest(
        self,
        adb_target: list[str],
        bundle: BundleInfo,
        obbs: list[Path],
        log,
        progress,
        completed_remote: list[str],
        progress_start: float = 27,
        progress_span: float = 61,
    ) -> list[Path]:
        if self.cancel_event.is_set():
            raise OperationCancelled()
        failed = []
        total_bytes = sum(path.stat().st_size for path in obbs)
        processed_bytes = 0
        remote_dir = f"/sdcard/Android/obb/{bundle.package_name}"
        self._run_command(
            adb_target + ["shell", "mkdir", "-p", remote_dir],
            log=log,
        )
        for index, obb in enumerate(obbs, start=1):
            if self.cancel_event.is_set():
                raise OperationCancelled()
            size_gb = obb.stat().st_size / 1_000_000_000
            log(
                f"Copying OBB {index}/{len(obbs)} ({size_gb:.2f} GB). "
                "Keep the Quest awake; this can take several minutes…"
            )
            remote_path = f"{remote_dir}/{obb.name}"
            if remote_path not in completed_remote:
                completed_remote.append(remote_path)
            base_bytes = processed_bytes

            def push_progress(file_percent: int) -> None:
                transferred = obb.stat().st_size * file_percent / 100
                fraction = (
                    (base_bytes + transferred) / total_bytes if total_bytes else 1
                )
                progress(
                    progress_start + fraction * progress_span,
                    f"Copying OBB {index}/{len(obbs)}: {file_percent}%",
                )

            try:
                self._run_command(
                    adb_target + ["push", "-p", str(obb), remote_path],
                    log=log,
                    progress=push_progress,
                )
                size_result = subprocess.run(
                    adb_target + ["shell", "stat", "-c", "%s", remote_path],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                remote_size = size_result.stdout.strip().replace("\r", "")
                if (
                    size_result.returncode != 0
                    or not remote_size.isdigit()
                    or int(remote_size) != obb.stat().st_size
                ):
                    raise UserError(
                        f"{obb.name} was copied, but its size could not be verified."
                    )
                log(f"Verified {obb.name} ({int(remote_size):,} bytes).")
            except (UserError, subprocess.TimeoutExpired) as exc:
                failed.append(obb)
                log(f"OBB transfer failed for {obb.name}: {exc}")
            processed_bytes += obb.stat().st_size
            progress(
                progress_start
                + (
                    processed_bytes / total_bytes * progress_span
                    if total_bytes
                    else progress_span
                ),
                f"Checked OBB {index}/{len(obbs)}.",
            )
            if self.cancel_event.is_set():
                raise OperationCancelled()
        return failed

    def _quest_install_worker(
        self,
        bundle: BundleInfo,
        package_existed: bool,
        delete_source_after: bool = False,
        *,
        emit_events: bool = True,
        log_callback=None,
        progress_callback=None,
    ) -> dict[str, object] | None:
        def log(message: str) -> None:
            if log_callback:
                log_callback(message)
            elif emit_events:
                self.events.put(("log", message))
                self.events.put(("status", message))

        def progress(percent: float, message: str) -> None:
            if progress_callback:
                progress_callback(percent, message)
            elif emit_events:
                self.events.put(("progress", (percent, message)))

        serial = ""
        apk_installed = False
        completed_remote: list[str] = []
        try:
            adb = find_adb()
            if not adb:
                raise UserError(
                    "ADB was not found. Install Android Platform Tools or SideQuest, "
                    "then open the app again."
                )
            log(f"Using ADB: {adb}")
            progress(3, "Checking the Quest connection…")
            devices_result = subprocess.run(
                [str(adb), "devices", "-l"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
            if devices_result.returncode != 0:
                raise UserError(
                    "ADB could not check the Quest connection:\n"
                    + (devices_result.stderr.strip() or devices_result.stdout.strip())
                )
            connected, unavailable = parse_adb_devices(devices_result.stdout)
            if not connected:
                extra = ""
                if unavailable:
                    extra = (
                        "\n\nDetected but not ready: "
                        + ", ".join(unavailable)
                        + ". Put on the headset and approve USB debugging."
                    )
                raise UserError(
                    "No ready Quest was found. Connect it by USB, keep it awake, "
                    "and approve the USB debugging prompt inside the headset." + extra
                )
            if len(connected) > 1:
                raise UserError(
                    "More than one Android device is connected. Disconnect the others "
                    "and leave only the Quest connected."
                )
            serial = connected[0]
            adb_target = [str(adb), "-s", serial]
            log(f"Connected Quest: {serial}")
            progress(8, f"Connected to Quest {serial}.")
            existing_result = subprocess.run(
                adb_target + ["shell", "pm", "path", bundle.package_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
            package_existed = package_existed or package_is_installed(
                existing_result.returncode,
                existing_result.stdout,
            )
            if self.cancel_event.is_set():
                raise OperationCancelled()

            log(f"Installing APK ({bundle.apk.stat().st_size / 1_000_000:.1f} MB)…")
            progress(12, "Installing APK…")
            self._run_command(
                adb_target + ["install", "-r", str(bundle.apk)],
                log=log,
            )
            apk_installed = True
            progress(25, "APK installed.")
            if self.cancel_event.is_set():
                raise OperationCancelled()
            failed = self._copy_obbs_to_quest(
                adb_target,
                bundle,
                bundle.obbs,
                log,
                progress,
                completed_remote,
            )
            if failed:
                if emit_events:
                    self.events.put(
                        (
                            "obb_failed",
                            {
                                "bundle": bundle,
                                "serial": serial,
                                "failed": failed,
                                "package_existed": package_existed,
                            },
                        )
                    )
                    return None
                names = ", ".join(path.name for path in failed)
                raise UserError(f"OBB transfer or verification failed: {names}")

            progress(92, "Verifying the installed package…")
            package_result = subprocess.run(
                adb_target + ["shell", "pm", "path", bundle.package_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if not package_is_installed(
                package_result.returncode, package_result.stdout
            ):
                raise UserError(
                    "The transfer finished, but Quest did not report the package as "
                    "installed. Reconnect the headset and try again."
                )
            source_removed = False
            cleanup_error = None
            if delete_source_after:
                if not is_managed_output(bundle.root):
                    cleanup_error = (
                        "Cleanup refused the folder because its managed-output "
                        "marker was missing."
                    )
                else:
                    cleanup_error = self._move_path_to_trash(bundle.root)
                    source_removed = cleanup_error is None
                if source_removed:
                    log(f"Moved verified local install folder to Trash: {bundle.root}")
                else:
                    log(f"Local folder cleanup was skipped: {cleanup_error}")
            progress(100, "APK and OBB files installed and verified.")
            payload = {
                "package": bundle.package_name,
                "folder": str(bundle.root),
                "source_removed": source_removed,
                "cleanup_error": cleanup_error,
            }
            if emit_events:
                self.events.put(("quest_complete", payload))
            return payload
        except OperationCancelled:
            cancelled_payload = {
                "package": bundle.package_name,
                "serial": serial,
                "apk_installed": apk_installed,
                "package_existed": package_existed,
                "completed_remote": completed_remote,
                "retry": False,
            }
            if emit_events:
                self.events.put(
                    (
                        "cancelled_install",
                        cancelled_payload,
                    )
                )
                return None
            raise OperationCancelled(cancelled_payload)
        except subprocess.TimeoutExpired:
            message = (
                "The Quest connection timed out. Keep the headset awake, "
                "reconnect the USB cable, and try again."
            )
            if emit_events:
                self.events.put(("error", message))
                return None
            raise UserError(message)
        except UserError as exc:
            if emit_events:
                self.events.put(("error", str(exc)))
                return None
            raise
        except Exception as exc:
            if emit_events:
                self.events.put(("log", traceback.format_exc()))
                self.events.put(("error", f"Unexpected install error: {exc}"))
                return None
            raise

    def _bulk_rename_worker(
        self,
        jobs: list[tuple[BundleInfo, str, Path]],
        replace_sources: bool,
        copy_obbs: bool,
        should_sign: bool,
    ) -> None:
        results = []
        cancel_output = None
        total = len(jobs)
        for index, (bundle, new_package, output) in enumerate(jobs):
            if self.cancel_event.is_set():
                break
            label = bundle.game_name or bundle.root.name
            self.events.put(
                ("log", f"\n=== Bulk rename {index + 1}/{total}: {label} ===")
            )

            def log(message: str, item_label=label) -> None:
                self.events.put(("log", f"[{item_label}] {message}"))

            def progress(
                percent: float,
                message: str,
                item_index=index,
                item_label=label,
            ) -> None:
                overall = (item_index + percent / 100) / total * 100
                self.events.put(
                    (
                        "progress",
                        (
                            overall,
                            f"{item_index + 1}/{total} {item_label}: {message}",
                        ),
                    )
                )

            try:
                final_output = self._rename_worker(
                    bundle.package_name,
                    new_package,
                    bundle.version_code,
                    output,
                    copy_obbs,
                    should_sign,
                    replace_sources,
                    bundle=bundle,
                    emit_events=False,
                    log_callback=log,
                    progress_callback=progress,
                )
                results.append(
                    {
                        "name": label,
                        "success": True,
                        "detail": str(final_output),
                        "output": str(final_output),
                    }
                )
            except OperationCancelled as exc:
                cancel_output = exc.details.get("output")
                results.append(
                    {"name": label, "success": False, "detail": "Cancelled"}
                )
                break
            except Exception as exc:
                log(f"Failed: {exc}")
                results.append(
                    {"name": label, "success": False, "detail": str(exc)}
                )
        cancelled = self.cancel_event.is_set() or cancel_output is not None
        self.events.put(
            (
                "bulk_complete",
                {
                    "mode": "rename",
                    "results": results,
                    "total": total,
                    "cancelled": cancelled,
                    "cancel_output": cancel_output,
                },
            )
        )

    def _bulk_install_worker(
        self,
        bundles: list[BundleInfo],
        delete_sources: bool,
    ) -> None:
        results = []
        cancel_details = None
        total = len(bundles)
        for index, bundle in enumerate(bundles):
            if self.cancel_event.is_set():
                break
            label = bundle.game_name or bundle.root.name
            self.events.put(
                ("log", f"\n=== Bulk install {index + 1}/{total}: {label} ===")
            )

            def log(message: str, item_label=label) -> None:
                self.events.put(("log", f"[{item_label}] {message}"))

            def progress(
                percent: float,
                message: str,
                item_index=index,
                item_label=label,
            ) -> None:
                overall = (item_index + percent / 100) / total * 100
                self.events.put(
                    (
                        "progress",
                        (
                            overall,
                            f"{item_index + 1}/{total} {item_label}: {message}",
                        ),
                    )
                )

            try:
                conflict = self._package_conflict_on_connected_quest(
                    bundle.package_name
                )
                outcome = self._quest_install_worker(
                    bundle,
                    conflict,
                    delete_sources,
                    emit_events=False,
                    log_callback=log,
                    progress_callback=progress,
                )
                cleanup_note = (
                    " • local folder moved to Trash"
                    if outcome and outcome.get("source_removed")
                    else ""
                )
                if outcome and outcome.get("cleanup_error"):
                    cleanup_note = (
                        " • installed, but local cleanup failed: "
                        + str(outcome["cleanup_error"])
                    )
                results.append(
                    {
                        "name": label,
                        "success": True,
                        "detail": f"{bundle.package_name}{cleanup_note}",
                        "folder": str(bundle.root),
                        "source_removed": bool(
                            outcome and outcome.get("source_removed")
                        ),
                    }
                )
            except OperationCancelled as exc:
                cancel_details = exc.details
                results.append(
                    {"name": label, "success": False, "detail": "Cancelled"}
                )
                break
            except Exception as exc:
                log(f"Failed: {exc}")
                results.append(
                    {"name": label, "success": False, "detail": str(exc)}
                )
        cancelled = self.cancel_event.is_set() or cancel_details is not None
        self.events.put(
            (
                "bulk_complete",
                {
                    "mode": "install",
                    "results": results,
                    "total": total,
                    "cancelled": cancelled,
                    "cancel_details": cancel_details,
                },
            )
        )

    def retry_failed_obbs(self) -> None:
        state = self.failed_obb_retry
        if self.busy or not state:
            return
        failed = list(state["failed"])
        if not messagebox.askyesno(
            "Retry failed OBB transfer?",
            f"Retry {len(failed)} failed OBB file"
            f"{'s' if len(failed) != 1 else ''}?\n\n"
            "The APK will not be reinstalled.",
        ):
            return
        self.cancel_event.clear()
        self.operation_kind = "obb_retry"
        self._set_busy(True, "Preparing to retry failed OBB files…", cancellable=True)
        self.retry_obb_button.configure(state="disabled")
        threading.Thread(
            target=self._obb_retry_worker,
            args=(state,),
            daemon=True,
        ).start()

    def _obb_retry_worker(self, state: dict[str, object]) -> None:
        bundle = state["bundle"]
        serial = str(state["serial"])
        failed_obbs = list(state["failed"])
        completed_remote: list[str] = []

        def log(message: str) -> None:
            self.events.put(("log", message))
            self.events.put(("status", message))

        def progress(percent: float, message: str) -> None:
            self.events.put(("progress", (percent, message)))

        try:
            adb = find_adb()
            if not adb:
                raise UserError("ADB was not found.")
            devices_result = subprocess.run(
                [str(adb), "devices"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            connected, _unavailable = parse_adb_devices(devices_result.stdout)
            if serial not in connected:
                raise UserError(
                    "The same Quest is not connected. Reconnect it and retry again."
                )
            adb_target = [str(adb), "-s", serial]
            remaining = self._copy_obbs_to_quest(
                adb_target,
                bundle,
                failed_obbs,
                log,
                progress,
                completed_remote,
                progress_start=5,
                progress_span=90,
            )
            if remaining:
                self.events.put(
                    (
                        "obb_failed",
                        {
                            **state,
                            "failed": remaining,
                        },
                    )
                )
                return
            progress(100, "Failed OBB files transferred and verified.")
            self.events.put(("quest_complete", bundle.package_name))
        except OperationCancelled:
            self.events.put(
                (
                    "cancelled_install",
                    {
                        "package": bundle.package_name,
                        "serial": serial,
                        "apk_installed": True,
                        "package_existed": True,
                        "completed_remote": completed_remote,
                        "retry": True,
                    },
                )
            )
        except (UserError, subprocess.TimeoutExpired) as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:
            self.events.put(("log", traceback.format_exc()))
            self.events.put(("error", f"Unexpected OBB retry error: {exc}"))

    def _install_tools_worker(self) -> None:
        try:
            TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            versions = {}
            tool_items = list(TOOL_DOWNLOADS.items())
            for index, (name, item) in enumerate(tool_items):
                self.events.put(
                    (
                        "progress",
                        (
                            index / len(tool_items) * 100,
                            f"Checking {name}…",
                        ),
                    )
                )
                path: Path = item["path"]
                expected = item["sha256"]
                if path.is_file() and sha256_file(path) == expected:
                    self.events.put(("log", f"{name} {item['version']} is verified."))
                else:
                    self.events.put(("log", f"Downloading {name} {item['version']}…"))
                    temporary = path.with_suffix(path.suffix + ".download")
                    request = urllib.request.Request(
                        item["url"], headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"}
                    )
                    with urllib.request.urlopen(request, timeout=60) as response:
                        with temporary.open("wb") as handle:
                            shutil.copyfileobj(response, handle)
                    actual = sha256_file(temporary)
                    if actual != expected:
                        temporary.unlink(missing_ok=True)
                        raise UserError(
                            f"{name} checksum did not match. Download was discarded."
                        )
                    temporary.replace(path)
                    self.events.put(("log", f"{name} download verified."))
                versions[name] = {
                    "version": item["version"],
                    "source": item["url"],
                    "sha256": item["sha256"],
                }
                self.events.put(
                    (
                        "progress",
                        (
                            (index + 1) / len(tool_items) * 100,
                            f"{name} is ready.",
                        ),
                    )
                )
            TOOL_INFO_FILE.write_text(
                json.dumps(versions, indent=2) + "\n", encoding="utf-8"
            )
            self.events.put(("done_tools", None))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _validate(self):
        if self.bundle is None or self.bundle.apk != Path(
            self.apk_var.get()
        ).expanduser().resolve():
            self.bundle = detect_bundle(Path(self.apk_var.get()))
        old = self.old_package_var.get().strip()
        new = self.new_package_var.get().strip()
        version = self.version_var.get().strip()
        output = Path(self.output_var.get()).expanduser().resolve()
        replace_source = self.replace_source_var.get()

        if not is_valid_package(old):
            raise UserError(
                "The current package is missing or invalid. It should look like com.company.game."
            )
        if not is_valid_package(new):
            raise UserError(
                "The new package is invalid. Use letters, numbers, underscores, and at least one dot."
            )
        if old == new:
            raise UserError("The new package must be different from the current package.")
        if version and not version.isdigit():
            raise UserError("Version code must contain digits only.")
        if self.copy_obb_var.get() and self.bundle.obbs and not version:
            raise UserError("A version code is required to name the OBB.")
        if replace_source and self.bundle.obbs and not self.copy_obb_var.get():
            raise UserError(
                "Source replacement requires OBB copying so the replacement "
                "folder remains complete."
            )
        if replace_source and output != self.bundle.root:
            raise UserError(
                "Replace source mode must target the selected source folder."
            )
        if not replace_source and (
            output == self.bundle.root or self.bundle.root in output.parents
        ):
            raise UserError("Choose an output folder outside the source bundle.")
        ready, detail = tool_status()
        if not ready:
            raise UserError(
                f"{detail}. Open “More options” and click "
                "“Repair / verify Android tools” first."
            )
        return (
            old,
            new,
            version,
            output,
            self.copy_obb_var.get(),
            self.sign_var.get(),
            replace_source,
        )

    def _perform_preflight(self):
        values = self._validate()
        assert self.bundle is not None
        source_files = [self.bundle.apk, *self.bundle.obbs]
        unreadable = [path.name for path in source_files if not os.access(path, os.R_OK)]
        if unreadable:
            raise UserError(
                "These source files cannot be read: " + ", ".join(unreadable)
            )
        output: Path = values[3]
        disk_root = existing_parent(output.parent)
        required = required_working_space(self.bundle, values[4])
        free = shutil.disk_usage(disk_root).free
        if free < required:
            raise UserError(
                f"Not enough free space. This build needs about "
                f"{format_size(required)}, but {format_size(free)} is available "
                f"on {disk_root}."
            )
        note = (
            f"Ready • about {format_size(required)} needed • "
            f"{format_size(free)} free"
        )
        if (
            values[5]
            and self.ask_key_backup_var.get()
            and MANAGED_KEYSTORE.is_file()
            and not KEY_BACKUP_MARKER.is_file()
        ):
            note += " • signing-key backup recommended"
        self.preflight_var.set(note)
        self.preflight_badge.set("✓  Ready to build", "success")
        return values

    def start(self) -> None:
        if self.busy:
            return
        try:
            values = self._perform_preflight()
        except UserError as exc:
            self.preflight_var.set(str(exc))
            self.preflight_badge.set("Needs attention", "warning")
            messagebox.showerror("One thing needs attention", str(exc))
            return
        self.cancel_event.clear()
        self.operation_kind = "build"
        self._set_busy(
            True,
            "Everything looks good — starting the build…",
            cancellable=True,
        )
        self._set_progress(3, "Everything looks good — starting the build…")
        threading.Thread(
            target=self._rename_worker, args=values, daemon=True
        ).start()

    def _rename_worker(
        self,
        old_package: str,
        new_package: str,
        version_code: str,
        output: Path,
        copy_obbs: bool,
        should_sign: bool,
        replace_source: bool,
        *,
        bundle: BundleInfo | None = None,
        emit_events: bool = True,
        log_callback=None,
        progress_callback=None,
    ) -> Path | None:
        bundle = bundle or self.bundle
        assert bundle is not None

        def log(message: str) -> None:
            if log_callback:
                log_callback(message)
            elif emit_events:
                self.events.put(("log", message))
                self.events.put(("status", message))

        def progress(percent: float, message: str) -> None:
            if progress_callback:
                progress_callback(percent, message)
            elif emit_events:
                self.events.put(("progress", (percent, message)))

        staging_output: Path | None = None
        try:
            source = bundle.root
            if replace_source:
                staging_output = Path(
                    tempfile.mkdtemp(
                        prefix=f".{source.name}-renamed-staging-",
                        dir=source.parent,
                    )
                )
                output = staging_output
                log(f"Staging replacement beside source: {output}")
            else:
                output = unique_output_dir(output)
            if self.cancel_event.is_set():
                raise OperationCancelled()
            output.parent.mkdir(parents=True, exist_ok=True)
            log(f"Output bundle: {output}")
            progress(5, "Preparing a safe working folder…")
            with tempfile.TemporaryDirectory(
                prefix=".quest-renamer-", dir=output.parent
            ) as temporary:
                work = Path(temporary)
                decoded = work / "decoded"
                unsigned = work / f"{new_package}-unsigned.apk"
                signed_dir = work / "signed"

                log("Decoding APK with Apktool…")
                progress(8, "Decoding APK…")
                self._run_command(
                    [
                        str(find_java() or "java"),
                        "-jar",
                        str(APKTOOL_JAR),
                        "d",
                        "-f",
                        str(bundle.apk),
                        "-o",
                        str(decoded),
                    ],
                    log=log,
                )
                if self.cancel_event.is_set():
                    raise OperationCancelled()
                progress(26, "APK decoded.")

                replace_package_references(
                    decoded, old_package, new_package, log=log
                )
                if self.cancel_event.is_set():
                    raise OperationCancelled()
                progress(32, "Package references updated.")
                log("Game name, launcher label, assets, and in-game text were untouched.")

                log("Rebuilding renamed APK…")
                progress(36, "Rebuilding renamed APK…")
                self._run_command(
                    [
                        str(find_java() or "java"),
                        "-jar",
                        str(APKTOOL_JAR),
                        "b",
                        str(decoded),
                        "-o",
                        str(unsigned),
                    ],
                    log=log,
                )
                if self.cancel_event.is_set():
                    raise OperationCancelled()
                progress(56, "APK rebuilt.")

                output.mkdir(parents=True, exist_ok=True)
                if should_sign:
                    keystore, alias, password = ensure_managed_key(
                        self._run_command, log
                    )
                    signed_dir.mkdir(parents=True, exist_ok=True)
                    log("Zip-aligning, signing, and verifying APK…")
                    progress(60, "Signing and verifying APK…")
                    self._run_command(
                        [
                            str(find_java() or "java"),
                            "-jar",
                            str(SIGNER_JAR),
                            "--apks",
                            str(unsigned),
                            "--out",
                            str(signed_dir),
                            "--ks",
                            str(keystore),
                            "--ksAlias",
                            alias,
                            "--ksPass",
                            password,
                            "--ksKeyPass",
                            password,
                            "--allowResign",
                        ],
                        log=log,
                        secret_values={password},
                    )
                    if self.cancel_event.is_set():
                        raise OperationCancelled()
                    signed_candidates = sorted(signed_dir.glob("*-aligned-signed.apk"))
                    if not signed_candidates:
                        signed_candidates = sorted(signed_dir.glob("*.apk"))
                    if not signed_candidates:
                        raise UserError("Signer completed but produced no APK.")
                    built_apk = signed_candidates[0]
                    progress(70, "APK signed and verified.")
                else:
                    built_apk = unsigned
                    log("Warning: signing was disabled; Android will not install the APK.")
                    progress(70, "Unsigned APK prepared.")

                apk_output = output / f"{new_package}.apk"
                shutil.copy2(built_apk, apk_output)
                log(f"Created {apk_output.name}")
                progress(73, "APK saved to the output folder.")

                obb_outputs = []
                total_obb_bytes = (
                    sum(path.stat().st_size for path in bundle.obbs)
                    if copy_obbs
                    else 0
                )
                completed_obb_bytes = 0
                if copy_obbs:
                    for obb_index, source_obb in enumerate(bundle.obbs, start=1):
                        if self.cancel_event.is_set():
                            raise OperationCancelled()
                        match = OBB_RE.match(source_obb.name)
                        kind = match.group(1).lower() if match else "main"
                        obb_version = version_code or (
                            match.group(2) if match else "1"
                        )
                        destination = (
                            output
                            / new_package
                            / f"{kind}.{obb_version}.{new_package}.obb"
                        )
                        base_bytes = completed_obb_bytes

                        def obb_progress(copied: int, _total: int) -> None:
                            fraction = (
                                (base_bytes + copied) / total_obb_bytes
                                if total_obb_bytes
                                else 1
                            )
                            progress(
                                73 + fraction * 21,
                                f"Copying OBB {obb_index}/{len(bundle.obbs)}…",
                            )

                        copy_with_progress(
                            source_obb,
                            destination,
                            log=log,
                            progress=obb_progress,
                        )
                        completed_obb_bytes += source_obb.stat().st_size
                        obb_outputs.append(destination)
                        if self.cancel_event.is_set():
                            raise OperationCancelled()
                progress(94, "Bundle files copied.")
                if self.cancel_event.is_set():
                    raise OperationCancelled()

                manifest = build_release_manifest(
                    source_manifest=bundle.release_manifest,
                    output_root=output,
                    new_package=new_package,
                    game_name=bundle.game_name or new_package,
                    version_code=version_code or bundle.version_code or "1",
                    apk_output=apk_output,
                    obb_outputs=obb_outputs,
                )
                log(f"Created {manifest.name}")
                progress(97, "Manifest updated.")

                summary = output / "RENAMED-BUNDLE.txt"
                summary.write_text(
                    "\n".join(
                        [
                            f"{APP_NAME} {APP_VERSION}",
                            f"Source package: {old_package}",
                            f"New package: {new_package}",
                            f"Version code: {version_code or bundle.version_code}",
                            f"APK: {apk_output.name}",
                            f"OBB files: {len(obb_outputs)}",
                            "Game name and in-game text: unchanged",
                            "Signing: persistent local key"
                            if should_sign
                            else "Signing: disabled",
                            "",
                            "Keep the signing key on this computer. Future APK updates",
                            "for this renamed package must use the same key.",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
            backup = None
            trash_error = None
            if replace_source:
                progress(98, "Activating the completed replacement…")
                output, backup, trash_error = commit_staged_replacement(
                    source,
                    output,
                    self._move_path_to_trash,
                )
                log(f"Replaced source folder with completed bundle: {output}")
                if backup:
                    log(
                        "Warning: the original could not be moved to Trash and "
                        f"was kept at: {backup}"
                    )
            progress(100, "Renamed bundle completed.")
            payload = {
                "output": output,
                "source_replaced": replace_source,
                "backup": backup,
                "trash_error": trash_error,
            }
            if emit_events:
                self.events.put(("complete", payload))
            return output
        except OperationCancelled:
            if emit_events:
                self.events.put(("cancelled_build", output))
                return None
            raise OperationCancelled({"output": str(output)})
        except UserError as exc:
            if staging_output is not None and staging_output.is_dir():
                cleanup_error = self._move_path_to_trash(staging_output)
                if cleanup_error:
                    log(
                        "The failed replacement staging folder was kept because "
                        f"cleanup failed: {cleanup_error}"
                    )
            if emit_events:
                self.events.put(("error", str(exc)))
                return None
            raise
        except Exception as exc:
            if staging_output is not None and staging_output.is_dir():
                cleanup_error = self._move_path_to_trash(staging_output)
                if cleanup_error:
                    log(
                        "The failed replacement staging folder was kept because "
                        f"cleanup failed: {cleanup_error}"
                    )
            if emit_events:
                self.events.put(("log", traceback.format_exc()))
                self.events.put(("error", f"Unexpected error: {exc}"))
                return None
            raise

    @staticmethod
    def _run_command(args, log, secret_values=None, progress=None) -> None:
        secrets_to_hide = secret_values or set()
        display = " ".join(
            "<hidden>" if value in secrets_to_hide else str(value) for value in args
        )
        log(f"$ {display}")
        process = subprocess.Popen(
            [str(value) for value in args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            for value in secrets_to_hide:
                line = line.replace(value, "<hidden>")
            if line:
                log(line)
                if progress:
                    match = re.search(r"(?<!\d)(\d{1,3})%", line)
                    if match:
                        progress(min(100, int(match.group(1))))
        process.stdout.close()
        code = process.wait()
        if code != 0:
            raise UserError(f"Command failed with exit code {code}. See the log above.")

    def _set_progress(self, percent: float, status: str | None = None) -> None:
        value = max(0, min(100, int(round(percent))))
        self.last_progress = value
        self.progress.configure(value=value)
        self.progress_percent_var.set(f"{value}%")
        if status:
            self.status_var.set(status)

    def request_cancel(self) -> None:
        if not self.busy or self.operation_kind not in {
            "build",
            "install",
            "obb_retry",
            "bulk_rename",
            "bulk_install",
        }:
            return
        if self.cancel_event.is_set():
            return
        if not messagebox.askyesno(
            "Cancel safely?",
            "The current stage or OBB transfer will finish first, then the app "
            "will stop at a safe boundary.\n\nContinue with cancellation?",
        ):
            return
        self.cancel_event.set()
        self.cancel_button.configure(
            text="Cancel requested…",
            state="disabled",
        )
        self.status_var.set(
            "Cancellation requested — waiting for the current safe step to finish…"
        )

    def _set_busy(
        self,
        busy: bool,
        status: str,
        *,
        cancellable: bool = False,
    ) -> None:
        self.busy = busy
        self.status_var.set(status)
        if busy:
            self._set_progress(0)
            self.progress_row.pack(
                before=self.status_label, fill=X, pady=(0, 8)
            )
            self.start_button.configure(state="disabled", text="Working…")
            self.install_button.configure(state="disabled")
            self.select_install_button.configure(state="disabled")
            self.tools_button.configure(state="disabled")
            self.backup_key_button.configure(state="disabled")
            self.cleanup_button.configure(state="disabled")
            self.retry_obb_button.configure(state="disabled")
            self.replace_source_check.configure(state="disabled")
            self.delete_source_check.configure(state="disabled")
            self.ask_key_backup_check.configure(state="disabled")
            self.copy_obb_check.configure(state="disabled")
            self.sign_check.configure(state="disabled")
            self.bulk_button.configure(state="disabled")
            self.cancel_button.configure(
                text="Cancel safely",
                state="normal" if cancellable else "disabled",
            )
        else:
            self.progress_row.pack_forget()
            self.cancel_button.configure(text="Cancel safely", state="disabled")
            self.cancel_event.clear()
            self.operation_kind = None
            self.start_button.configure(text="Create renamed game")
            self.select_install_button.configure(state="normal")
            self.tools_button.configure(state="normal")
            self.cleanup_button.configure(state="normal")
            self.replace_source_check.configure(state="normal")
            self.ask_key_backup_check.configure(state="normal")
            self.copy_obb_check.configure(state="normal")
            self.sign_check.configure(state="normal")
            self.delete_source_check.configure(
                state="disabled" if self.replace_source_var.get() else "normal"
            )
            self.bulk_button.configure(state="normal")
            self.backup_key_button.configure(
                state="normal" if MANAGED_KEYSTORE.is_file() else "disabled"
            )
            self.retry_obb_button.configure(
                state="normal" if self.failed_obb_retry else "disabled"
            )
            self._schedule_device_refresh(500)
            self._update_action_state()

    def _update_action_state(self) -> None:
        new_package = self.new_package_var.get().strip()
        old_package = self.old_package_var.get().strip()
        state = (
            "normal"
            if self.bundle is not None
            and is_valid_package(new_package)
            and new_package != old_package
            and self.ready_to_build
            and not self.busy
            else "disabled"
        )
        self.start_button.configure(state=state)
        install_state = (
            "normal"
            if self.last_output is not None
            and self.last_output.is_dir()
            and not self.busy
            else "disabled"
        )
        self.install_button.configure(state=install_state)

    def _forget_removed_local_bundle(self, folder: Path) -> None:
        if self.bundle is None or self.bundle.root != folder:
            return
        self.bundle = None
        self.source_folder_var.set("")
        self.apk_var.set("")
        self.old_package_var.set("")
        self.new_package_var.set("")
        self.version_var.set("")
        self.output_var.set("")
        self.detected_var.set(
            "The local folder was moved to Trash after verified installation."
        )
        self.current_info_var.set("Choose another game folder to continue.")
        self.output_summary_var.set(
            "An output folder will be chosen automatically."
        )
        self._update_action_state()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    percent, message = payload
                    self._set_progress(float(percent), str(message))
                elif kind == "device_status":
                    self._apply_device_status(payload)
                elif kind == "done_tools":
                    self._set_busy(False, "Tools are installed and verified.")
                    self._refresh_tool_status()
                    messagebox.showinfo(APP_NAME, "Android tools are ready.")
                elif kind == "error":
                    self._set_busy(False, "Stopped with an error.")
                    self._refresh_tool_status()
                    messagebox.showerror(APP_NAME, str(payload))
                elif kind == "obb_failed":
                    self.failed_obb_retry = payload
                    failed_count = len(payload["failed"])
                    self._set_busy(
                        False,
                        f"{failed_count} OBB transfer"
                        f"{'s' if failed_count != 1 else ''} failed.",
                    )
                    self.retry_obb_button.configure(
                        text=f"Retry {failed_count} failed OBB "
                        f"file{'s' if failed_count != 1 else ''} only",
                        state="normal",
                    )
                    self.retry_obb_button.pack(fill=X, pady=(8, 0))
                    messagebox.showwarning(
                        "OBB transfer needs retry",
                        f"The APK is installed, but {failed_count} OBB file"
                        f"{'s' if failed_count != 1 else ''} failed verification.\n\n"
                        "Use the new retry button. The APK will not be reinstalled.",
                    )
                elif kind == "cancelled_build":
                    self._handle_cancelled_build(Path(payload))
                elif kind == "cancelled_install":
                    self._handle_cancelled_install(payload)
                elif kind == "partial_cleanup_done":
                    self._set_busy(False, "Partial files removed.")
                    messagebox.showinfo(
                        "Partial files removed",
                        str(payload),
                    )
                elif kind == "bulk_complete":
                    mode = str(payload["mode"])
                    results = list(payload["results"])
                    total = int(payload["total"])
                    succeeded = sum(
                        1 for result in results if result["success"]
                    )
                    failed = sum(
                        1 for result in results if not result["success"]
                    )
                    unprocessed = max(0, total - len(results))
                    cancelled = bool(payload.get("cancelled"))
                    if not cancelled and failed == 0:
                        self._set_progress(100, "Bulk operation completed.")
                    self._set_busy(
                        False,
                        f"Bulk {mode}: {succeeded} succeeded, {failed} failed.",
                    )
                    if mode == "rename":
                        completed_outputs = [
                            Path(result["output"])
                            for result in results
                            if result["success"] and result.get("output")
                        ]
                        if completed_outputs:
                            self.last_output = completed_outputs[-1]
                    else:
                        removed_folders = {
                            Path(result["folder"])
                            for result in results
                            if result.get("source_removed") and result.get("folder")
                        }
                        if self.last_output in removed_folders:
                            self.last_output = None
                        if self.bundle and self.bundle.root in removed_folders:
                            self._forget_removed_local_bundle(self.bundle.root)
                    self._update_action_state()
                    lines = []
                    for result in results[:20]:
                        marker = "✓" if result["success"] else "✗"
                        lines.append(
                            f"{marker} {result['name']}: {result['detail']}"
                        )
                    if len(results) > 20:
                        lines.append(f"…and {len(results) - 20} more")
                    summary = (
                        f"Succeeded: {succeeded}\nFailed: {failed}\n"
                        f"Not started: {unprocessed}\n\n"
                        + "\n".join(lines)
                    )
                    if cancelled:
                        summary = "The queue was cancelled safely.\n\n" + summary
                    dialog = (
                        messagebox.showinfo
                        if failed == 0 and not cancelled
                        else messagebox.showwarning
                    )
                    dialog(
                        f"Bulk {mode} overview",
                        summary,
                    )
                    if mode == "rename" and succeeded:
                        self._offer_signing_key_backup()
                    cancel_output = payload.get("cancel_output")
                    cancel_details = payload.get("cancel_details")
                    if cancel_output:
                        self._handle_cancelled_build(Path(str(cancel_output)))
                    elif cancel_details:
                        self._handle_cancelled_install(cancel_details)
                elif kind == "complete":
                    if isinstance(payload, dict):
                        output = Path(payload["output"])
                        source_replaced = bool(payload["source_replaced"])
                        backup = payload.get("backup")
                        trash_error = payload.get("trash_error")
                    else:
                        output = Path(payload)
                        source_replaced = False
                        backup = None
                        trash_error = None
                    self.last_output = output
                    self.install_button.configure(
                        text="Install this copy on Quest"
                    )
                    self._set_busy(False, f"Complete: {output}")
                    if source_replaced:
                        self.bundle = detect_bundle_from_folder(output)
                        self.source_folder_var.set(str(output))
                        self.apk_var.set(str(self.bundle.apk))
                        self.old_package_var.set(self.bundle.package_name)
                        self.new_package_var.set(self.bundle.package_name)
                        self._append_log(
                            "Done. The staged bundle replaced the source only after "
                            "the complete build succeeded."
                        )
                    else:
                        self._append_log("Done. Your source bundle was not changed.")
                    if backup:
                        messagebox.showwarning(
                            "Original backup kept",
                            "The renamed replacement is ready, but the original "
                            "folder could not be moved to Trash.\n\n"
                            f"It was kept safely at:\n{backup}\n\n"
                            f"Trash error: {trash_error}",
                        )
                    self._offer_signing_key_backup()
                    if messagebox.askyesno(
                        APP_NAME,
                        f"Renamed bundle created successfully:\n\n{output}\n\nOpen it now?",
                    ):
                        self._open_path(output)
                elif kind == "quest_complete":
                    if isinstance(payload, dict):
                        package_name = str(payload["package"])
                        source_removed = bool(payload.get("source_removed"))
                        cleanup_error = payload.get("cleanup_error")
                        installed_folder = Path(str(payload.get("folder", "")))
                    else:
                        package_name = str(payload)
                        source_removed = False
                        cleanup_error = None
                        installed_folder = None
                    self.failed_obb_retry = None
                    self.retry_obb_button.pack_forget()
                    self._set_busy(False, "Installed successfully on the Quest.")
                    self._append_log(
                        f"Quest install complete: {package_name}"
                    )
                    if source_removed:
                        if self.last_output == installed_folder:
                            self.last_output = None
                        if installed_folder is not None:
                            self._forget_removed_local_bundle(installed_folder)
                        self.install_button.configure(
                            text="Install current folder",
                            state="disabled",
                        )
                        cleanup_note = (
                            "\n\nThe verified local install folder was moved to Trash."
                        )
                    elif cleanup_error:
                        cleanup_note = (
                            "\n\nThe Quest install succeeded, but the local folder "
                            f"could not be moved to Trash:\n{cleanup_error}"
                        )
                    else:
                        cleanup_note = ""
                    messagebox.showinfo(
                        "Installed on Quest",
                        f"{package_name} was installed successfully.\n\n"
                        "The APK and all OBB files were copied and verified. "
                        "You can disconnect USB and launch it from the headset."
                        + cleanup_note,
                    )
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._drain_events)

    def _handle_cancelled_build(self, output: Path) -> None:
        self._set_busy(False, "Build cancelled safely.")
        self._append_log("Build cancelled at a safe stage boundary.")
        if output.is_dir() and any(output.iterdir()):
            if messagebox.askyesno(
                "Remove partial build?",
                f"The build stopped safely. Move this partial output to Trash?\n\n"
                f"{output}\n\nChoose No to keep it.",
            ):
                error = self._move_path_to_trash(output)
                if error:
                    messagebox.showerror("Could not use Trash", error)
                else:
                    messagebox.showinfo(
                        "Partial build removed",
                        "The partial output was moved to Trash.",
                    )
                    return
        messagebox.showinfo(
            "Build cancelled",
            "The source bundle was not changed.",
        )

    def _handle_cancelled_install(self, payload: dict[str, object]) -> None:
        self._set_busy(False, "Quest operation cancelled safely.")
        completed_remote = [str(path) for path in payload["completed_remote"]]
        retry = bool(payload["retry"])
        package_existed = bool(payload["package_existed"])
        apk_installed = bool(payload["apk_installed"])
        if retry and completed_remote:
            prompt = (
                "The retry stopped between OBB files.\n\nRemove the OBB files that "
                "were started during this retry?"
            )
            cleanup_mode = "files"
        elif apk_installed and not package_existed:
            prompt = (
                "The install stopped after creating a new package on the Quest.\n\n"
                "Remove the partial APK and its OBB folder?"
            )
            cleanup_mode = "package"
        else:
            prompt = ""
            cleanup_mode = ""
        if prompt and messagebox.askyesno("Remove partial Quest files?", prompt):
            self._set_busy(True, "Removing partial Quest files…")
            threading.Thread(
                target=self._quest_partial_cleanup_worker,
                args=(payload, cleanup_mode),
                daemon=True,
            ).start()
            return
        extra = (
            "\n\nAn older installation was detected, so the app did not offer to "
            "remove it automatically."
            if package_existed and not retry
            else ""
        )
        messagebox.showinfo(
            "Quest operation cancelled",
            "Cancellation completed at a safe boundary. You can retry later." + extra,
        )

    def _quest_partial_cleanup_worker(
        self,
        payload: dict[str, object],
        mode: str,
    ) -> None:
        try:
            package_name = str(payload["package"])
            serial = str(payload["serial"])
            if not is_valid_package(package_name) or not serial:
                raise UserError("The partial install target could not be verified.")
            adb = find_adb()
            if not adb:
                raise UserError("ADB was not found.")
            adb_target = [str(adb), "-s", serial]
            if mode == "package":
                uninstall_result = subprocess.run(
                    adb_target + ["uninstall", package_name],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                remote_dir = f"/sdcard/Android/obb/{package_name}"
                remove_result = subprocess.run(
                    adb_target + ["shell", "rm", "-rf", remote_dir],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                uninstall_output = (
                    uninstall_result.stdout + uninstall_result.stderr
                ).strip()
                if uninstall_result.returncode != 0 or (
                    "Success" not in uninstall_output
                    and "Unknown package" not in uninstall_output
                ):
                    raise UserError(
                        "Quest did not confirm removal of the partial APK."
                    )
                if remove_result.returncode != 0:
                    raise UserError(
                        "Quest did not confirm removal of the partial OBB folder."
                    )
                message = (
                    f"The partial package and OBB folder for {package_name} were removed."
                )
            else:
                allowed_prefix = f"/sdcard/Android/obb/{package_name}/"
                removed = 0
                for remote_path in payload["completed_remote"]:
                    remote_path = str(remote_path)
                    if not remote_path.startswith(allowed_prefix):
                        raise UserError("An OBB cleanup path failed its safety check.")
                    remove_result = subprocess.run(
                        adb_target + ["shell", "rm", "-f", remote_path],
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    if remove_result.returncode != 0:
                        raise UserError(
                            f"Quest did not confirm removal of {Path(remote_path).name}."
                        )
                    removed += 1
                message = f"Removed {removed} OBB file{'s' if removed != 1 else ''}."
            self.events.put(("partial_cleanup_done", message))
        except (UserError, OSError, subprocess.TimeoutExpired) as exc:
            self.events.put(("error", f"Partial-file cleanup failed: {exc}"))

    @staticmethod
    def _move_path_to_trash(path: Path) -> str | None:
        if sys.platform.startswith("win"):
            try:
                import ctypes
                from ctypes import wintypes

                class SHFILEOPSTRUCTW(ctypes.Structure):
                    _fields_ = [
                        ("hwnd", wintypes.HWND),
                        ("wFunc", wintypes.UINT),
                        ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR),
                        ("fFlags", wintypes.WORD),
                        ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", wintypes.LPCWSTR),
                    ]

                operation = SHFILEOPSTRUCTW()
                operation.wFunc = 3  # FO_DELETE
                operation.pFrom = str(path.resolve()) + "\0\0"
                operation.fFlags = 0x40 | 0x10 | 0x400 | 0x4
                result = ctypes.windll.shell32.SHFileOperationW(  # type: ignore[attr-defined]
                    ctypes.byref(operation)
                )
                if result != 0:
                    return f"Windows Recycle Bin returned error code {result}."
                if operation.fAnyOperationsAborted:
                    return "The Recycle Bin operation was cancelled."
                return None
            except (OSError, AttributeError) as exc:
                return f"Windows Recycle Bin error: {exc}"
        if sys.platform == "darwin":
            environment = os.environ.copy()
            environment["QAR_TRASH_PATH"] = str(path.expanduser().resolve())
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/osascript",
                        "-e",
                        'tell application "Finder" to delete POSIX file '
                        '(system attribute "QAR_TRASH_PATH")',
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    env=environment,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"macOS Trash error: {exc}"
            if result.returncode != 0:
                return (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "Finder could not move the item to Trash."
                )
            return None
        trash_tool = shutil.which("gio")
        command = [trash_tool, "trash", str(path)] if trash_tool else None
        if not command and shutil.which("trash-put"):
            command = [shutil.which("trash-put"), str(path)]
        if not command:
            return "A safe desktop Trash command was not found."
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return (
                result.stderr.strip()
                or result.stdout.strip()
                or "Unknown Trash error."
            )
        return None

    def open_output(self) -> None:
        value = self.output_var.get().strip()
        if value:
            self._open_path(Path(value).expanduser())

    def backup_signing_key(self) -> None:
        if not MANAGED_KEYSTORE.is_file() or not MANAGED_KEY_INFO.is_file():
            messagebox.showinfo(
                "Signing key not created yet",
                "The signing key will be created during your first signed build. "
                "The app will remind you to back it up afterward.",
            )
            return
        selected = native_choose_directory(
            "Choose where to save the signing-key backup",
            Path.home() / "Documents",
        )
        if not selected:
            return
        destination = (
            Path(selected).expanduser().resolve()
            / f"Quest APK Renamer Key Backup {dt.date.today().isoformat()}"
        )
        destination = unique_output_dir(destination)
        try:
            destination.mkdir(parents=False)
            shutil.copy2(MANAGED_KEYSTORE, destination / MANAGED_KEYSTORE.name)
            shutil.copy2(MANAGED_KEY_INFO, destination / MANAGED_KEY_INFO.name)
            (destination / "KEEP-PRIVATE.txt").write_text(
                "Quest APK Renamer signing-key backup\n\n"
                "Keep both the .jks and signing-key.json files private and together.\n"
                "They are required to update apps signed with this identity.\n",
                encoding="utf-8",
            )
            APP_DATA.mkdir(parents=True, exist_ok=True)
            KEY_BACKUP_MARKER.write_text(
                json.dumps(
                    {
                        "backed_up_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "destination": str(destination),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            messagebox.showerror(
                "Backup failed",
                f"The signing-key backup could not be created:\n\n{exc}",
            )
            return
        self.preflight_var.set("Signing key backed up successfully.")
        self._append_log(f"Signing key backed up to: {destination}")
        messagebox.showinfo(
            "Signing key backed up",
            f"Backup created at:\n\n{destination}\n\n"
            "Keep this folder private and store another copy on a safe drive.",
        )

    def _offer_signing_key_backup(self) -> None:
        if (
            not self.ask_key_backup_var.get()
            or not MANAGED_KEYSTORE.is_file()
            or not MANAGED_KEY_INFO.is_file()
            or KEY_BACKUP_MARKER.is_file()
        ):
            return
        if messagebox.askyesno(
            "Back up your signing key",
            "Your persistent signing key has not been backed up yet.\n\n"
            "You need this key to update renamed apps later. Back it up now?",
        ):
            self.backup_signing_key()

    def cleanup_output(self) -> None:
        initial = (
            self.last_output.parent
            if self.last_output
            else existing_parent(Path(self.output_var.get() or Path.home()))
        )
        selected = native_choose_directory(
            "Choose an old Quest APK Renamer output to move to Trash",
            initial,
        )
        if not selected:
            return
        target = Path(selected).expanduser().resolve()
        if not is_managed_output(target):
            messagebox.showerror(
                "Not a managed output",
                "For safety, cleanup only accepts folders containing "
                "RENAMED-BUNDLE.txt created by this app.",
            )
            return
        total_size = sum(
            path.stat().st_size
            for path in target.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        if not messagebox.askyesno(
            "Move output to Trash?",
            f"This will move the following renamed output to Trash:\n\n"
            f"{target}\n\nSize: {format_size(total_size)}\n\n"
            "It can be restored from your desktop Trash.",
        ):
            return
        trash_error = self._move_path_to_trash(target)
        if trash_error:
            messagebox.showerror(
                "Could not move output to Trash",
                trash_error,
            )
            return
        if self.last_output == target:
            self.last_output = None
            self.install_button.configure(
                text="Install current folder",
                state="disabled",
            )
        self.status_var.set("Old output moved to Trash.")
        self._append_log(f"Moved output to Trash: {target}")
        messagebox.showinfo(
            "Output cleaned up",
            "The renamed output was moved to Trash and can still be restored.",
        )

    def install_app_launcher(self) -> None:
        if sys.platform.startswith("win"):
            self.install_windows_launcher()
        elif sys.platform == "darwin":
            self.install_macos_application()
        elif sys.platform.startswith("linux"):
            self.install_linux_launcher()

    def install_windows_launcher(self) -> None:
        if not sys.platform.startswith("win"):
            return
        frozen = bool(getattr(sys, "frozen", False))
        if frozen:
            target = Path(sys.executable).resolve()
            arguments = ""
            working_directory = target.parent
            icon = target
        else:
            python_executable = Path(sys.executable)
            pythonw = python_executable.with_name("pythonw.exe")
            target = pythonw if pythonw.is_file() else python_executable
            arguments = f'"{SCRIPT_DIR / "quest_apk_renamer.py"}"'
            working_directory = SCRIPT_DIR
            icon_candidate = RESOURCE_DIR / "assets" / "quest-apk-renamer.ico"
            icon = icon_candidate if icon_candidate.is_file() else target
        try:
            WINDOWS_START_MENU.parent.mkdir(parents=True, exist_ok=True)
            environment = os.environ.copy()
            environment.update(
                {
                    "QAR_SHORTCUT": str(WINDOWS_START_MENU),
                    "QAR_TARGET": str(target),
                    "QAR_ARGUMENTS": arguments,
                    "QAR_WORKDIR": str(working_directory),
                    "QAR_ICON": str(icon),
                }
            )
            script = (
                "$shell = New-Object -ComObject WScript.Shell; "
                "$shortcut = $shell.CreateShortcut($env:QAR_SHORTCUT); "
                "$shortcut.TargetPath = $env:QAR_TARGET; "
                "$shortcut.Arguments = $env:QAR_ARGUMENTS; "
                "$shortcut.WorkingDirectory = $env:QAR_WORKDIR; "
                "$shortcut.IconLocation = $env:QAR_ICON; "
                "$shortcut.Save()"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=environment,
            )
            if result.returncode != 0 or not WINDOWS_START_MENU.is_file():
                raise OSError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "Windows did not create the shortcut."
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror(
                "Could not add the Start Menu shortcut",
                str(exc),
            )
            return
        self.launcher_button.configure(text="In Start Menu ✓")
        self.status_var.set("Added to the Windows Start Menu.")
        self._append_log(f"Installed Start Menu shortcut: {WINDOWS_START_MENU}")
        messagebox.showinfo(
            "Added to Start Menu",
            "Quest APK Renamer is now available from the Windows Start Menu.",
        )

    def install_linux_launcher(self) -> None:
        if not sys.platform.startswith("linux"):
            messagebox.showinfo(
                APP_NAME,
                "The app-launcher shortcut is available on Linux desktops.",
            )
            return
        try:
            LINUX_LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
            LINUX_LAUNCHER.write_text(desktop_entry_text(), encoding="utf-8")
            LINUX_LAUNCHER.chmod(0o755)
            database_updater = shutil.which("update-desktop-database")
            if database_updater:
                subprocess.run(
                    [database_updater, str(LINUX_LAUNCHER.parent)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        except OSError as exc:
            messagebox.showerror(
                "Could not add the launcher",
                f"The desktop shortcut could not be created:\n\n{exc}",
            )
            return
        self.launcher_button.configure(text="In Applications ✓")
        self.status_var.set("Added to your Linux app launcher.")
        self._append_log(f"Installed app launcher: {LINUX_LAUNCHER}")
        messagebox.showinfo(
            "Added to Applications",
            "Quest APK Renamer is now in your app launcher.\n\n"
            "Open the launcher and search for “Quest APK Renamer”. "
            "You can pin it to your taskbar from there.",
        )

    def install_macos_application(self) -> None:
        if sys.platform != "darwin":
            return
        source = find_macos_app_bundle()
        if source is None:
            messagebox.showinfo(
                "Use the macOS app",
                "Adding to Applications is available in the packaged macOS "
                "version. Open the DMG, then launch Quest APK Renamer.",
            )
            return
        source = source.resolve()
        user_target = MACOS_USER_APPLICATION.expanduser()
        system_target = MACOS_SYSTEM_APPLICATION
        if source in {user_target.resolve(), system_target.resolve()}:
            self.launcher_button.configure(text="In Applications ✓")
            messagebox.showinfo(
                "Already in Applications",
                "Quest APK Renamer is already installed in Applications.",
            )
            return
        if system_target.is_dir():
            self.launcher_button.configure(text="In Applications ✓")
            messagebox.showinfo(
                "Already in Applications",
                f"Quest APK Renamer is already installed at:\n\n{system_target}",
            )
            return
        replacing = user_target.is_dir()
        if replacing and not messagebox.askyesno(
            "Replace the installed app?",
            "Quest APK Renamer is already in your Applications folder.\n\n"
            "Replace it with this copy? Your signing key and settings are "
            "stored separately and will be preserved.",
        ):
            return

        staging = None
        backup = None
        try:
            user_target.parent.mkdir(parents=True, exist_ok=True)
            staging = unique_output_dir(
                user_target.with_name(f".{APP_NAME}.app.installing")
            )
            copy_result = subprocess.run(
                ["/usr/bin/ditto", str(source), str(staging)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            if copy_result.returncode != 0:
                raise OSError(
                    copy_result.stderr.strip()
                    or copy_result.stdout.strip()
                    or "macOS could not copy the app."
                )
            if not (staging / "Contents" / "Info.plist").is_file():
                raise OSError("The copied app bundle is incomplete.")
            if replacing:
                backup = unique_output_dir(
                    user_target.with_name(f".{APP_NAME}.app.previous")
                )
                user_target.rename(backup)
            staging.rename(user_target)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if backup is not None and backup.exists() and not user_target.exists():
                try:
                    backup.rename(user_target)
                except OSError:
                    pass
            messagebox.showerror(
                "Could not add to Applications",
                f"The app could not be installed:\n\n{exc}",
            )
            return

        backup_warning = None
        if backup is not None and backup.exists():
            backup_warning = self._move_path_to_trash(backup)
        self.launcher_button.configure(text="In Applications ✓")
        self.status_var.set("Added to your macOS Applications folder.")
        self._append_log(f"Installed macOS app bundle: {user_target}")
        message = (
            f"Quest APK Renamer is now installed at:\n\n{user_target}\n\n"
            "Open it from Applications, Launchpad, or Spotlight."
        )
        if backup_warning:
            message += (
                "\n\nThe previous copy could not be moved to Trash and remains at:\n"
                f"{backup}\n\n{backup_warning}"
            )
        messagebox.showinfo("Added to Applications", message)

    @staticmethod
    def _open_path(path: Path) -> None:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def show_key_info(self) -> None:
        exists = MANAGED_KEYSTORE.is_file()
        messagebox.showinfo(
            "Signing key info",
            (
                f"Managed key: {MANAGED_KEYSTORE}\n\n"
                + (
                    "The key exists and will be reused for every build."
                    if exists
                    else "The key will be created the first time you sign a build."
                )
                + "\n\nBack up the .jks file and signing-key.json together. "
                "Losing them means you cannot install an update over an older renamed build."
            ),
        )

    @staticmethod
    def show_about() -> None:
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} {APP_VERSION}\n\n"
            "A local, non-destructive helper for authorized Quest APK/OBB builds.\n\n"
            "Uses Apktool and Uber APK Signer. No files are uploaded.",
        )


def main() -> int:
    root = TkinterDnD.Tk() if DND_AVAILABLE and TkinterDnD else Tk()
    app = RenamerApp(root)
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1]).expanduser()
        if candidate.is_dir():
            def load_folder() -> None:
                try:
                    app._load_bundle(detect_bundle_from_folder(candidate))
                except UserError as exc:
                    messagebox.showerror(APP_NAME, str(exc))

            root.after(50, load_folder)
        elif candidate.suffix.lower() == ".apk":
            app.apk_var.set(str(candidate))
            root.after(50, app.inspect_apk)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
