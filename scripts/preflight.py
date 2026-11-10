#!/usr/bin/env python3
"""Offline-safe prerequisite check for the public Phase-1 package."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def _binary(name: str) -> dict[str, object]:
    path = shutil.which(name)
    result: dict[str, object] = {"found": bool(path), "path": path}
    if not path:
        return result
    probe = subprocess.run([path, "--version"], capture_output=True, text=True)
    if probe.returncode != 0 and name in {"ffmpeg", "ffprobe"}:
        probe = subprocess.run([path, "-version"], capture_output=True, text=True)
    result["version_exit_code"] = probe.returncode
    first_line = (probe.stdout or probe.stderr).splitlines()
    result["version"] = first_line[0] if first_line else ""
    return result


def _module(name: str) -> dict[str, object]:
    try:
        loaded = importlib.import_module(name)
    except Exception as exc:  # report an actionable local prerequisite failure
        return {"importable": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"importable": True, "version": getattr(loaded, "__version__", None)}


def main() -> int:
    binaries = {name: _binary(name) for name in ("ffmpeg", "ffprobe", "fc-match")}
    modules = {name: _module(name) for name in ("numpy", "cv2", "rapidocr_onnxruntime")}
    passed = all(value["found"] and value.get("version_exit_code") == 0 for value in binaries.values())
    passed = passed and all(value["importable"] for value in modules.values())
    payload = {
        "status": "PASS" if passed else "BLOCKED",
        "python": sys.version.split()[0],
        "repository_root": str(Path(__file__).resolve().parents[1]),
        "binaries": binaries,
        "python_modules": modules,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
