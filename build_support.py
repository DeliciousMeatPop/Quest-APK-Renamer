"""Small helpers shared by the platform-specific PyInstaller specs."""

from __future__ import annotations

import importlib.util
import platform
import tkinter
from pathlib import Path


def tkdnd_data_files(
    *,
    system: str | None = None,
    machine: str | None = None,
    tcl_major: int | None = None,
    package_root: Path | None = None,
) -> list[tuple[str, str]]:
    """Return only the tkdnd backend needed by this native build."""
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    tcl_major = tcl_major or int(tkinter.TclVersion)

    if system == "Windows":
        architecture = {
            "amd64": "win-x64",
            "x86_64": "win-x64",
            "arm64": "win-arm64",
            "x86": "win-x86",
            "i386": "win-x86",
        }.get(machine)
    elif system == "Darwin":
        architecture = {
            "arm64": "osx-arm64",
            "aarch64": "osx-arm64",
            "x86_64": "osx-x64",
        }.get(machine)
    elif system == "Linux":
        architecture = {
            "aarch64": "linux-arm64",
            "arm64": "linux-arm64",
            "x86_64": "linux-x64",
            "amd64": "linux-x64",
        }.get(machine)
    else:
        architecture = None

    if architecture is None:
        raise RuntimeError(f"Unsupported tkdnd build target: {system} {machine}")

    variant = f"{architecture}-tcl9" if tcl_major >= 9 else architecture
    if package_root is None:
        spec = importlib.util.find_spec("tkinterdnd2")
        if spec is None or not spec.submodule_search_locations:
            raise RuntimeError("tkinterdnd2 is not installed in the build environment")
        package_root = Path(next(iter(spec.submodule_search_locations)))

    source = package_root / "tkdnd" / variant
    if not source.is_dir():
        raise RuntimeError(f"tkinterdnd2 does not contain the required backend: {variant}")
    return [(str(source), f"tkinterdnd2/tkdnd/{variant}")]
