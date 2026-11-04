"""Inspect/extract a small title-update package without requiring a game dump."""
from pathlib import Path

from .formats import StfsPackage, WorkbenchError, bounded_read, inspect_xex


def _payload_info(data: bytes) -> dict:
    info = inspect_xex(data)
    kind = "delta patch" if info.patch_source_version or info.module_flags & 0x40 else "full patch" if info.module_flags & 0x20 else "complete executable"
    return {"kind": kind, **info.to_dict()}


def inspect_title_update(path: Path) -> dict:
    data = bounded_read(path)
    if data[:4] == b"XEX2":
        return {"format": "raw XEX/XEXP", "payload": _payload_info(data),
                "repacking_supported": False}
    package = StfsPackage(data)
    files = []
    for entry in package.entries:
        if entry.is_directory:
            continue
        content = package.file_bytes(entry)  # Check every payload block too.
        record = {"path": entry.path, "size": entry.size}
        if entry.path.casefold().endswith((".xex", ".xexp", ".dllp")):
            record["executable"] = _payload_info(content)
        files.append(record)
    return {"format": "STFS title update", "package": package.metadata(), "files": files,
            "repacking_supported": False,
            "note": "Read-only inspection. A TU is not a ZIP; creating a modified TU needs a valid executable/delta payload and rebuilt package integrity data."}


def extract_title_update(path: Path, destination: Path) -> dict:
    package = StfsPackage(bounded_read(path))
    files = package.extract(Path(destination))
    return {"output": str(Path(destination).resolve()), "files": files,
            "metadata": package.metadata(), "repacking_supported": False}
