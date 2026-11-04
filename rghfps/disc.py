"""Shared bounded XDVDFS directory traversal and extraction for ISO and GOD."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import struct
from .formats import MAX_FILE, WorkbenchError, safe_component, safe_relative
SECTOR = 0x800

@dataclass(frozen=True)
class DiscEntry:
    path: str
    sector: int
    size: int
    is_directory: bool

class DiscFileSystem:
    """Implement read_range and sector_location in the image transport."""
    def _directory_tree(self, root_sector: int, root_size: int) -> list[DiscEntry]:
        tasks = [(root_sector, root_size, "", 0)]
        seen_dirs, seen_paths = set(), set()
        entries = []
        while tasks:
            sector, size, prefix, depth = tasks.pop()
            if depth > 64 or (sector, size) in seen_dirs or not 0 < size <= 16 * 1024 * 1024:
                raise WorkbenchError("disc directory tree is cyclic or outside the supported limits.")
            seen_dirs.add((sector, size))
            data = self.read_range(sector, 0, size)
            nodes, visited = [0], set()
            while nodes:
                offset = nodes.pop()
                if offset in visited or offset + 14 > len(data):
                    raise WorkbenchError("disc directory node is cyclic or outside its table.")
                visited.add(offset)
                left, right, block, length, attributes, name_length = struct.unpack_from("<HHIIBB", data, offset)
                if left == right == 0xFFFF or data[offset:offset + 14] == bytes(14):
                    continue
                if not name_length or offset + 14 + name_length > len(data):
                    raise WorkbenchError("disc directory has an invalid filename.")
                try:
                    name = safe_component(data[offset + 14:offset + 14 + name_length].decode("utf-8", "strict"))
                except UnicodeError as exc:
                    raise WorkbenchError("disc filename is not supported UTF-8.") from exc
                path = f"{prefix}/{name}" if prefix else name
                folded = path.casefold()
                if folded in seen_paths or len(entries) >= 100000:
                    raise WorkbenchError("disc has duplicate paths or too many files.")
                seen_paths.add(folded)
                is_directory = bool(attributes & 0x10)
                entries.append(DiscEntry(path, block, length, is_directory))
                if is_directory and length:
                    tasks.append((block, length, path, depth + 1))
                elif length:
                    self.sector_location(block)
                    self.sector_location(block + (length - 1) // SECTOR)
                for child in (left, right):
                    if child:
                        if child == 0xFFFF:
                            raise WorkbenchError("Invalid disc directory child index.")
                        nodes.append(child * 4)
        if sum(e.size for e in entries if not e.is_directory) > 16 * 1024**3:
            raise WorkbenchError("disc extracted files exceed this release's 16 GiB limit.")
        return entries

    def read_file(self, entry: DiscEntry, limit=MAX_FILE) -> bytes:
        if entry.is_directory or entry.size > limit:
            raise WorkbenchError("Selected disc file is a directory or exceeds the executable read limit.")
        result = bytearray()
        for start in range(0, entry.size, 1024 * 1024):
            result.extend(self.read_range(entry.sector, start, min(1024 * 1024, entry.size - start)))
        return bytes(result)

    def extract(self, destination: Path, *, modules_only=False, log=lambda _: None) -> dict:
        destination = Path(destination)
        selected = [e for e in self.entries if not e.is_directory and
                    (not modules_only or e.path.casefold().endswith((".xex", ".dll")))]
        needed = sum(e.size for e in selected)
        parent = destination.parent
        while not parent.exists():
            parent = parent.parent
        if shutil.disk_usage(parent).free < needed + 16 * 1024 * 1024:
            raise WorkbenchError("Not enough free space to extract this disc installation.")
        if modules_only and any(e.size > MAX_FILE for e in selected):
            raise WorkbenchError("A disc executable exceeds the supported 512 MiB limit.")
        destination.mkdir(parents=True, exist_ok=False)
        hashes = {}
        done, last_progress = 0, 0
        try:
            if not modules_only:
                for entry in self.entries:
                    if entry.is_directory:
                        destination.joinpath(*safe_relative(entry.path).parts).mkdir(parents=True, exist_ok=True)
            for entry in selected:
                target = destination.joinpath(*safe_relative(entry.path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with target.open("xb") as stream:
                    for offset in range(0, entry.size, 1024 * 1024):
                        chunk = self.read_range(entry.sector, offset, min(1024 * 1024, entry.size - offset))
                        stream.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if done - last_progress >= 64 * 1024 * 1024:
                            log(f"disc extraction: {done // (1024 * 1024)} / {needed // (1024 * 1024)} MiB")
                            last_progress = done
                hashes[entry.path] = digest.hexdigest()
            log(f"Extracted {len(selected)} disc files ({done // (1024 * 1024)} MiB); SHA-256 file hashes recorded.")
            return {"files": hashes, "bytes": done, "modules_only": modules_only}
        except Exception:
            shutil.rmtree(destination)
            raise

