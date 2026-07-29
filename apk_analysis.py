"""APK inspection and package-change reporting for Quest APK Renamer."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Callable


ANDROID_NS = "http://schemas.android.com/apk/res/android"
ANDROID = f"{{{ANDROID_NS}}}"
TECHNICAL_XML_ROOTS = {"res"}
KNOWN_RENAME_TAGS = {"apc", "mr", "dev", "test", "qa"}


def _attribute(node: ET.Element | None, name: str) -> str | None:
    if node is None:
        return None
    return node.get(f"{ANDROID}{name}") or node.get(name)


def _string_value(node: ET.Element) -> str:
    return "".join(node.itertext()).strip()


def load_default_strings(decoded: Path) -> dict[str, str]:
    strings: dict[str, str] = {}
    values = decoded / "res" / "values"
    if not values.is_dir():
        return strings
    for path in sorted(values.glob("*.xml")):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        for node in root.findall("string"):
            name = node.get("name")
            if name and name not in strings:
                strings[name] = _string_value(node)
    return strings


def resolve_label(value: str | None, strings: dict[str, str]) -> str | None:
    if not value:
        return None
    match = re.fullmatch(r"@(?:[^:]+:)?string/([A-Za-z0-9_.]+)", value)
    if match:
        return strings.get(match.group(1), value)
    return value


def decode_gl_es(value: str | None) -> str | None:
    if not value:
        return None
    try:
        encoded = int(value, 0)
    except ValueError:
        return value
    return f"{encoded >> 16}.{encoded & 0xFFFF}"


def parse_manifest(path: Path, strings: dict[str, str] | None = None) -> dict:
    root = ET.parse(path).getroot()
    strings = strings or {}
    uses_sdk = root.find("uses-sdk")
    application = root.find("application")

    permissions = sorted(
        {
            value
            for node in list(root.findall("uses-permission"))
            + list(root.findall("uses-permission-sdk-23"))
            if (value := _attribute(node, "name"))
        }
    )
    features = []
    gl_es_version = None
    for node in root.findall("uses-feature"):
        gl_value = _attribute(node, "glEsVersion")
        if gl_value and not gl_es_version:
            gl_es_version = decode_gl_es(gl_value)
        name = _attribute(node, "name")
        if name:
            features.append(
                {
                    "name": name,
                    "required": _attribute(node, "required") != "false",
                }
            )

    components = {}
    if application is not None:
        for kind in ("activity", "activity-alias", "service", "receiver", "provider"):
            components[kind] = len(application.findall(kind))

    return {
        "package_name": root.get("package"),
        "version_code": _attribute(root, "versionCode"),
        "version_name": _attribute(root, "versionName"),
        "compile_sdk": (
            _attribute(root, "compileSdkVersion")
            or root.get("platformBuildVersionCode")
        ),
        "min_sdk": _attribute(uses_sdk, "minSdkVersion"),
        "target_sdk": _attribute(uses_sdk, "targetSdkVersion"),
        "app_label": resolve_label(_attribute(application, "label"), strings),
        "permissions": permissions,
        "features": sorted(features, key=lambda item: item["name"].lower()),
        "gl_es_version": gl_es_version,
        "components": components,
        "debuggable": _attribute(application, "debuggable") == "true",
        "extract_native_libs": _attribute(application, "extractNativeLibs"),
    }


def parse_apktool_yml(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    def grab(pattern: str) -> str | None:
        match = re.search(pattern, text, re.MULTILINE)
        return match.group(1).strip() if match else None

    return {
        key: value
        for key, value in {
            "min_sdk": grab(r"^\s*minSdkVersion:\s*['\"]?([^'\"\n]+)"),
            "target_sdk": grab(r"^\s*targetSdkVersion:\s*['\"]?([^'\"\n]+)"),
            "version_code": grab(r"^\s*versionCode:\s*['\"]?([^'\"\n]+)"),
            "version_name": grab(r"^\s*versionName:\s*['\"]?([^'\"\n]+)"),
        }.items()
        if value
    }


def inspect_zip(apk: Path) -> dict:
    abis: set[str] = set()
    locales: set[str] = set()
    dex_files = 0
    native_libraries = 0
    entries = 0
    with zipfile.ZipFile(apk) as archive:
        for info in archive.infolist():
            name = info.filename
            entries += 1
            abi_match = re.match(r"^lib/([^/]+)/[^/]+\.so$", name)
            if abi_match:
                abis.add(abi_match.group(1))
                native_libraries += 1
            if re.fullmatch(r"classes(?:\d+)?\.dex", name):
                dex_files += 1
            locale_match = re.match(r"^res/values-([^/]+)/", name)
            if locale_match:
                qualifier = locale_match.group(1)
                if re.search(r"(^|-)r[A-Z]{2}($|-)", qualifier) or re.match(
                    r"^[a-z]{2,3}(?:-|$)", qualifier
                ):
                    locales.add(qualifier)
    return {
        "abis": sorted(abis),
        "locales": sorted(locales),
        "dex_files": dex_files,
        "native_libraries": native_libraries,
        "zip_entries": entries,
    }


def compute_hashes(path: Path) -> dict[str, str]:
    digests = {
        "md5": hashlib.md5(usedforsecurity=False),
        "sha1": hashlib.sha1(usedforsecurity=False),
        "sha256": hashlib.sha256(),
    }
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            for digest in digests.values():
                digest.update(chunk)
    return {name: digest.hexdigest() for name, digest in digests.items()}


def extract_rdn(dn: str | None, key: str) -> str | None:
    if not dn:
        return None
    match = re.search(
        rf"(?:^|,)\s*{re.escape(key)}\s*=\s*((?:\\.|[^,])*)",
        dn,
        re.IGNORECASE,
    )
    return match.group(1).replace("\\,", ",").strip() if match else None


def load_signer_registry(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    signers = data.get("signers", []) if isinstance(data, dict) else []
    return [entry for entry in signers if isinstance(entry, dict)]


def identify_signer(certificate: dict, registry: list[dict]) -> dict:
    searchable = []
    for dn in (certificate.get("subject"), certificate.get("issuer")):
        for key in ("CN", "O"):
            value = extract_rdn(dn, key)
            if value:
                searchable.extend((value, f"{key}={value}"))
    haystack = " ".join(searchable).lower()
    for entry in registry:
        needles = entry.get("dn_contains", [])
        if any(str(needle).lower() in haystack for needle in needles):
            return {
                "id": entry.get("id", "known"),
                "label": entry.get("label", "Known signer"),
                "full_name": entry.get("full_name", entry.get("label", "Known signer")),
                "color": entry.get("color", "#57e6b1"),
            }
    common_name = extract_rdn(certificate.get("subject"), "CN")
    return {
        "id": "unknown",
        "label": common_name or "Unknown signer",
        "full_name": common_name or "Unrecognized signing certificate",
        "color": "#f4b860",
    }


def _digest(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return re.sub(r"[^0-9a-f]", "", match.group(1)).lower()
    return None


def parse_signer_output(text: str, registry: list[dict] | None = None) -> dict:
    registry = registry or []
    schemes = {}
    for number in (1, 2, 3):
        positive = bool(
            re.search(
                rf"(?:verified\s+using\s+v{number}\s+scheme|"
                rf"(?:signature\s+)?scheme\s+v{number}|v{number}\s+scheme)"
                rf"[^\n]*(?:true|verified|success)",
                text,
                re.IGNORECASE,
            )
        )
        if not positive:
            signed_list = re.search(
                r"signed\s+with\s+(?:the\s+)?following\s+schemes?\s*:\s*([^\n]+)",
                text,
                re.IGNORECASE,
            )
            positive = bool(
                signed_list and re.search(rf"\bv{number}\b", signed_list.group(1), re.I)
            )
        if not positive:
            verified_list = re.search(
                r"signature\s+verified\s*\[([^\]]+)\]",
                text,
                re.IGNORECASE,
            )
            positive = bool(
                verified_list
                and re.search(rf"\bv{number}\b", verified_list.group(1), re.I)
            )
        schemes[f"v{number}"] = positive

    signer_numbers = sorted(
        {
            int(value)
            for value in re.findall(
                r"Signer\s*#(\d+)\s+certificate", text, re.IGNORECASE
            )
        }
    )
    certificates = []
    if signer_numbers:
        for number in signer_numbers:
            prefix = rf"Signer\s*#{number}\s+certificate\s+"
            subject_match = re.search(
                prefix + r"(?:DN|subject)\s*:\s*([^\n\r]+)", text, re.IGNORECASE
            )
            issuer_match = re.search(
                prefix + r"issuer\s*:\s*([^\n\r]+)", text, re.IGNORECASE
            )
            sha256 = _digest(
                [prefix + r"SHA-?256\s+(?:digest\s*)?:\s*([0-9a-f:\s]{64,})"],
                text,
            )
            sha1 = _digest(
                [prefix + r"SHA-?1\s+(?:digest\s*)?:\s*([0-9a-f:\s]{40,})"],
                text,
            )
            certificates.append(
                {
                    "subject": subject_match.group(1).strip() if subject_match else None,
                    "issuer": issuer_match.group(1).strip() if issuer_match else None,
                    "sha256": sha256,
                    "sha1": sha1,
                }
            )
    else:
        subjects = re.findall(
            r"^\s*(?:-\s*)?Subject\s*:\s*([^\n\r]+)",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        issuers = re.findall(
            r"^\s*(?:-\s*)?Issuer\s*:\s*([^\n\r]+)",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        sha256s = re.findall(
            r"^\s*(?:-\s*)?SHA-?256\s*:\s*([0-9a-f:]{64,})",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        sha1s = re.findall(
            r"^\s*(?:-\s*)?SHA-?1\s*:\s*([0-9a-f:]{40,})",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        for index, subject in enumerate(subjects):
            certificates.append(
                {
                    "subject": subject.strip(),
                    "issuer": issuers[index].strip() if index < len(issuers) else None,
                    "sha256": re.sub(r"[^0-9a-f]", "", sha256s[index]).lower()
                    if index < len(sha256s)
                    else None,
                    "sha1": re.sub(r"[^0-9a-f]", "", sha1s[index]).lower()
                    if index < len(sha1s)
                    else None,
                }
            )

    verified = bool(
        any(schemes.values())
        or re.search(
            r"(?:file-signature\s+verified|signature\s+(?:is\s+valid|verified)|verified\s*:\s*true)",
            text,
            re.IGNORECASE,
        )
    )
    signed = bool(certificates or any(schemes.values()))
    identity = identify_signer(certificates[0], registry) if certificates else None
    return {
        "signed": signed,
        "verified": verified,
        "schemes": schemes,
        "certificates": certificates,
        "identity": identity,
    }


def inspect_signature(
    apk: Path,
    java: Path | str,
    signer_jar: Path,
    registry: list[dict] | None = None,
    timeout: int = 90,
) -> dict:
    if not signer_jar.is_file():
        return {
            "signed": False,
            "verified": False,
            "schemes": {"v1": False, "v2": False, "v3": False},
            "certificates": [],
            "identity": None,
            "error": "APK signer tool is unavailable.",
        }
    try:
        result = subprocess.run(
            [
                str(java),
                "-jar",
                str(signer_jar),
                "--apks",
                str(apk),
                "--onlyVerify",
                "--verbose",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "signed": False,
            "verified": False,
            "schemes": {"v1": False, "v2": False, "v3": False},
            "certificates": [],
            "identity": None,
            "error": str(exc),
        }
    parsed = parse_signer_output(
        (result.stdout or "") + "\n" + (result.stderr or ""),
        registry,
    )
    if result.returncode != 0 and not parsed["signed"]:
        parsed["error"] = "The APK signature could not be verified."
    return parsed


def is_technical_file(relative: Path) -> bool:
    posix = relative.as_posix()
    return (
        posix in {"AndroidManifest.xml", "apktool.yml"}
        or relative.suffix.lower() == ".smali"
        or (
            len(relative.parts) > 1
            and relative.parts[0] in TECHNICAL_XML_ROOTS
            and relative.suffix.lower() == ".xml"
        )
    )


def scan_package_references(decoded: Path, old_package: str) -> dict:
    exact = old_package.encode("utf-8")
    slash = old_package.replace(".", "/").encode("utf-8")
    technical_files = []
    preserved_files = []
    native_warnings = []
    total_occurrences = 0

    for path in decoded.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(decoded)
        try:
            if path.stat().st_size > 64 * 1024 * 1024:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        exact_count = data.count(exact)
        slash_count = data.count(slash) if slash != exact else 0
        count = exact_count + slash_count
        if not count:
            continue
        entry = {
            "path": relative.as_posix(),
            "occurrences": count,
            "dotted": exact_count,
            "slashed": slash_count,
        }
        if is_technical_file(relative):
            technical_files.append(entry)
            total_occurrences += count
        else:
            preserved_files.append(entry)
            if relative.suffix.lower() in {".so", ".dex"}:
                native_warnings.append(entry)

    return {
        "old_package": old_package,
        "technical_occurrences": total_occurrences,
        "technical_files": technical_files,
        "preserved_files": preserved_files,
        "native_or_compiled_warnings": native_warnings,
    }


def inspect_decoded_apk(
    apk: Path,
    decoded: Path,
    java: Path | str,
    signer_jar: Path,
    signer_registry_path: Path,
) -> dict:
    manifest = parse_manifest(
        decoded / "AndroidManifest.xml",
        load_default_strings(decoded),
    )
    yml = parse_apktool_yml(decoded / "apktool.yml")
    zip_info = inspect_zip(apk)
    registry = load_signer_registry(signer_registry_path)
    signature = inspect_signature(apk, java, signer_jar, registry)
    package_name = manifest.get("package_name") or ""
    return {
        "file_name": apk.name,
        "file_path": str(apk),
        "file_size": apk.stat().st_size,
        **manifest,
        "version_code": manifest.get("version_code") or yml.get("version_code"),
        "version_name": manifest.get("version_name") or yml.get("version_name"),
        "min_sdk": manifest.get("min_sdk") or yml.get("min_sdk"),
        "target_sdk": manifest.get("target_sdk") or yml.get("target_sdk"),
        **zip_info,
        "hashes": compute_hashes(apk),
        "signature": signature,
        "package_references": scan_package_references(decoded, package_name)
        if package_name
        else {},
    }


def inspect_apk(
    apk: Path,
    java: Path | str,
    apktool_jar: Path,
    signer_jar: Path,
    signer_registry_path: Path,
    log: Callable[[str], None] | None = None,
) -> dict:
    with tempfile.TemporaryDirectory(prefix=".quest-apk-analysis-") as temporary:
        decoded = Path(temporary) / "decoded"
        if log:
            log("Reading APK metadata and technical references…")
        result = subprocess.run(
            [
                str(java),
                "-jar",
                str(apktool_jar),
                "d",
                "-f",
                str(apk),
                "-o",
                str(decoded),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(detail or "Apktool could not inspect this APK.")
        return inspect_decoded_apk(
            apk,
            decoded,
            java,
            signer_jar,
            signer_registry_path,
        )


def package_with_tag(package_name: str, tag: str) -> str:
    tag = tag.strip().lower().lstrip(".")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", tag):
        raise ValueError("A package tag must start with a letter and use 1–16 characters.")
    parts = package_name.split(".")
    if len(parts) < 2:
        raise ValueError("The package ID must contain at least one dot.")
    parts.insert(1, tag)
    return ".".join(parts)


def detect_existing_tag(
    package_name: str,
    *,
    signer_identity: dict | None = None,
    managed_marker: bool = False,
    extra_tags: set[str] | None = None,
) -> str | None:
    if managed_marker:
        return "This folder was already created by Quest APK Renamer."
    if signer_identity and signer_identity.get("id") == "quest_apk_renamer":
        return "This APK is already signed by Quest APK Renamer."
    tags = KNOWN_RENAME_TAGS | (extra_tags or set())
    match = next((part for part in package_name.lower().split(".") if part in tags), None)
    if match:
        return f'The current package ID already contains the rename tag “.{match}”.'
    return None


def write_json_report(path: Path, report: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
