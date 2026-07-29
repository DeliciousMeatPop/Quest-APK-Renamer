#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
runtime_dir="$script_dir/runtime"
java_dir="$runtime_dir/java"
platform_tools_dir="$runtime_dir/platform-tools"
force=0

if [[ "${1:-}" == "--force" ]]; then
    force=1
elif [[ -n "${1:-}" ]]; then
    echo "Usage: $0 [--force]" >&2
    exit 2
fi

machine_arch="$(uname -m)"
case "$machine_arch" in
    arm64)
        adoptium_arch="aarch64"
        ;;
    x86_64)
        adoptium_arch="x64"
        ;;
    *)
        echo "Unsupported macOS architecture: $machine_arch" >&2
        exit 1
        ;;
esac

temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/quest-apk-renamer-macos.XXXXXX")"
cleanup() {
    rm -rf "$temp_dir"
}
trap cleanup EXIT

mkdir -p "$runtime_dir"
hash_file="$runtime_dir/DEPENDENCY-HASHES.txt"
: > "$hash_file"

if [[ "$force" -eq 1 || ! -x "$java_dir/bin/java" || ! -x "$java_dir/bin/keytool" ]]; then
    java_archive="$temp_dir/temurin-jre.tar.gz"
    java_extract="$temp_dir/temurin"
    java_url="https://api.adoptium.net/v3/binary/latest/21/ga/mac/${adoptium_arch}/jre/hotspot/normal/eclipse"
    mkdir -p "$java_extract"
    echo "Downloading Eclipse Temurin JRE 21 for $machine_arch..."
    curl --fail --location --retry 3 "$java_url" --output "$java_archive"
    tar -xzf "$java_archive" -C "$java_extract"
    java_home="$(find "$java_extract" -type f -path '*/Contents/Home/bin/java' -print -quit)"
    if [[ -z "$java_home" ]]; then
        echo "The Temurin archive did not contain Contents/Home/bin/java." >&2
        exit 1
    fi
    java_home="$(dirname "$(dirname "$java_home")")"
    if [[ ! -x "$java_home/bin/keytool" ]]; then
        echo "The Temurin runtime does not include keytool." >&2
        exit 1
    fi
    rm -rf "$java_dir"
    /usr/bin/ditto "$java_home" "$java_dir"
    {
        echo "Temurin JRE archive SHA256: $(shasum -a 256 "$java_archive" | awk '{print $1}')"
        echo "Temurin source: $java_url"
    } >> "$hash_file"
else
    echo "Using existing bundled Java runtime."
fi

if [[ "$force" -eq 1 || ! -x "$platform_tools_dir/adb" ]]; then
    platform_archive="$temp_dir/platform-tools.zip"
    platform_extract="$temp_dir/android"
    platform_url="https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
    mkdir -p "$platform_extract"
    echo "Downloading Android SDK Platform-Tools..."
    curl --fail --location --retry 3 "$platform_url" --output "$platform_archive"
    /usr/bin/ditto -x -k "$platform_archive" "$platform_extract"
    extracted_tools="$platform_extract/platform-tools"
    if [[ ! -x "$extracted_tools/adb" ]]; then
        echo "The Platform-Tools archive did not contain adb." >&2
        exit 1
    fi
    rm -rf "$platform_tools_dir"
    /usr/bin/ditto "$extracted_tools" "$platform_tools_dir"
    {
        echo "Android Platform-Tools archive SHA256: $(shasum -a 256 "$platform_archive" | awk '{print $1}')"
        echo "Platform-Tools source: $platform_url"
    } >> "$hash_file"
else
    echo "Using existing bundled Android Platform-Tools."
fi

{
    echo
    echo "Resolved Java:"
    "$java_dir/bin/java" -version 2>&1
    echo
    echo "Resolved ADB:"
    "$platform_tools_dir/adb" version 2>&1
} >> "$hash_file"

echo "macOS runtime dependencies are ready in $runtime_dir"
