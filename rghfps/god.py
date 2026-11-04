"""Read-only GOD/SVOD game extraction, without an ISO conversion dependency.

Sector mapping follows Xenia; hash-chain structure is cross-checked against
iso2god-rs's published writer. See THIRD_PARTY_NOTICES.txt.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import re
import shutil
import struct

from .formats import WorkbenchError, u32
from .disc import DiscEntry as GodEntry, DiscFileSystem


SECTOR = 0x800
GROUP_BYTES = 0xCD000
PART_BYTES = 0xA290000
SECTORS_PER_PART = 0x14388
MAGIC = b"MICROSOFT*XBOX*MEDIA"


def _prefix(path: Path, length=0x3AD) -> bytes:
    if path.is_symlink() or not path.is_file():
        return b""
    with path.open("rb") as stream:
        return stream.read(length)


def is_god_header(path: Path) -> bool:
    data = _prefix(Path(path))
    return (len(data) >= 0x3AD and data[:4] in (b"LIVE", b"PIRS", b"CON ")
            and u32(data, 0x344) == 0x7000 and u32(data, 0x3A9) == 1)


def resolve_god(path: Path) -> Path | None:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise WorkbenchError("Choose a regular GOD header or game folder, not a link.")
    if path.is_file():
        return path.resolve() if is_god_header(path) else None
    if path.is_dir():
        if path.name.casefold().endswith(".data"):
            header = path.with_name(path.name[:-5])
            if is_god_header(header):
                return header.resolve()
            raise WorkbenchError("The .data folder is missing its matching GOD header file.")
        children = list(path.iterdir())
        if any(p.name.casefold() == "default.xex" for p in children):
            return None
        headers = [p for p in children if p.is_file() and is_god_header(p)]
        if len(headers) > 1:
            raise WorkbenchError("This folder contains several GOD games. Choose one package header or use the game list.")
        if headers:
            return headers[0].resolve()
    return None




class GodContainer(DiscFileSystem):
    """Standard .data split GODs and self-contained SVOD layouts.

    SHA-1 tables are checked back to the root digest in the selected header.
    This detects content damage; Microsoft signatures are not verified.
    """

    def __init__(self, header: Path):
        self.path = Path(header).resolve()
        self.handles = []
        self._tables = OrderedDict()
        self._blocks = OrderedDict()
        try:
            if not is_god_header(self.path):
                raise WorkbenchError("Select a Games-on-Demand header (00007000), not a Data0000 chunk or a TU.")
            first = _prefix(self.path, 0x344)
            header_size = u32(first, 0x340)
            header_end = (header_size + 0xFFF) & ~0xFFF
            if not 0x4000 <= header_end <= 1024 * 1024:
                raise WorkbenchError("Unsupported GOD header size.")
            with self.path.open("rb") as stream:
                self.header = stream.read(header_end)
            if len(self.header) != header_end:
                raise WorkbenchError("Truncated GOD header.")
            if hashlib.sha1(self.header[0x344:header_end]).digest() != self.header[0x32C:0x340]:
                raise WorkbenchError("GOD header integrity check failed.")
            self.title_id = f"{u32(self.header, 0x360):08X}"
            self.media_id = f"{u32(self.header, 0x354):08X}"
            self.version = f"{u32(self.header, 0x358):08X}"
            self.header_sha256 = hashlib.sha256(self.header).hexdigest()
            self.part_count = u32(self.header, 0x39D)
            if not 1 <= self.part_count <= 128 or self.header[0x379] != 0x24:
                raise WorkbenchError("Unsupported GOD fragment count or volume descriptor.")
            self.start_block = int.from_bytes(self.header[0x395:0x398], "little") * 2
            self.enhanced = bool(self.header[0x391] & 0x40)
            data_dir = self.path.with_name(self.path.name + ".data")
            self.part_base = 0
            self.parts = []
            if data_dir.is_dir() and not data_dir.is_symlink():
                candidates = list(data_dir.iterdir())
                expected_names = [f"Data{i:04d}" for i in range(self.part_count)]
                lookup = {p.name.casefold(): p for p in candidates}
                if len(lookup) != len(candidates):
                    raise WorkbenchError("GOD data fragments have case-colliding names.")
                actual = {p.name.casefold() for p in candidates if re.fullmatch(r"Data\d{4}", p.name, re.IGNORECASE)}
                if actual != {n.casefold() for n in expected_names}:
                    raise WorkbenchError("GOD .data folder has missing or extra Data0000… fragments. Copy the complete installation.")
                self.parts = [lookup[name.casefold()] for name in expected_names]
            elif self.part_count == 1 and self.path.stat().st_size > header_end + 0x2000:
                self.part_base = header_end
                self.parts = [self.path]
            else:
                raise WorkbenchError("Missing matching .data folder. The small GOD header alone does not contain the game.")
            self.sizes = []
            for number, path in enumerate(self.parts):
                if path.is_symlink() or not path.is_file():
                    raise WorkbenchError("GOD data fragments must be regular files.")
                size = path.stat().st_size - self.part_base
                if size < 0x3000 or size > PART_BYTES or (number + 1 < len(self.parts) and size != PART_BYTES):
                    raise WorkbenchError(f"GOD fragment has an invalid size: {path.name}")
                self.sizes.append(size)
                self.handles.append(path.open("rb"))
            self.masters = []
            expected = self.header[0x37D:0x391]
            for number in range(self.part_count):
                master = self._raw(number, 0, 0x1000)
                if hashlib.sha1(master).digest() != expected:
                    raise WorkbenchError(f"GOD master-hash chain failed at fragment {number}.")
                self.masters.append(master)
                expected = master[203 * 20:204 * 20]
            # The volume descriptor is stored in the first fragment. Its root
            # references use the same logical sector mapping as game files.
            candidates = [0x2000] if self.enhanced else [0x12000, 0x2000]
            self.volume_offset = next((off for off in candidates
                                       if off + SECTOR <= self.sizes[0]
                                       and self._physical(0, off, len(MAGIC)) == MAGIC), None)
            if self.volume_offset is None:
                raise WorkbenchError("GOD volume descriptor was not found in a supported layout.")
            volume = self._physical(0, self.volume_offset, SECTOR)
            if volume[-len(MAGIC):] != MAGIC:
                raise WorkbenchError("GOD volume descriptor has an invalid closing signature.")
            root_sector, root_size = struct.unpack_from("<II", volume, 20)
            if not root_size:
                raise WorkbenchError("GOD contains an empty root directory.")
            self.entries = self._directory_tree(root_sector, root_size)
            self.title = self._title_text() or self.title_id
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        for stream in self.handles:
            stream.close()
        self.handles.clear()

    def _raw(self, part: int, offset: int, length: int) -> bytes:
        if not 0 <= part < len(self.handles) or offset < 0 or length < 0 or offset + length > self.sizes[part]:
            raise WorkbenchError("GOD data reference extends beyond an available fragment.")
        stream = self.handles[part]
        stream.seek(self.part_base + offset)
        result = stream.read(length)
        if len(result) != length:
            raise WorkbenchError("A GOD fragment was truncated or changed during reading.")
        return result

    @staticmethod
    def _cache(cache, key, value, limit=128):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
        return value

    def _verified_block(self, part: int, offset: int) -> bytes:
        key = (part, offset)
        if key in self._blocks:
            self._blocks.move_to_end(key)
            return self._blocks[key]
        group = (offset - 0x1000) // GROUP_BYTES
        table_offset = 0x1000 + group * GROUP_BYTES
        block = (offset - table_offset - 0x1000) // 0x1000
        if not 0 <= group < 203 or not 0 <= block < 204:
            raise WorkbenchError("GOD data reference points into a hash table.")
        table_key = (part, group)
        table = self._tables.get(table_key)
        if table is None:
            table = self._raw(part, table_offset, 0x1000)
            expected = self.masters[part][group * 20:(group + 1) * 20]
            if hashlib.sha1(table).digest() != expected:
                raise WorkbenchError(f"GOD subtable integrity failed in fragment {part}, group {group}.")
            self._cache(self._tables, table_key, table)
        size = min(0x1000, self.sizes[part] - offset)
        content = self._raw(part, offset, size)
        if hashlib.sha1(content).digest() != table[block * 20:(block + 1) * 20]:
            raise WorkbenchError(f"GOD data integrity failed in fragment {part} at 0x{offset:X}.")
        return self._cache(self._blocks, key, content)

    def _physical(self, part: int, offset: int, length: int) -> bytes:
        result = bytearray()
        while length:
            aligned = offset & ~0xFFF
            block = self._verified_block(part, aligned)
            within = offset - aligned
            take = min(length, len(block) - within)
            if take <= 0:
                raise WorkbenchError("Truncated GOD data block.")
            result.extend(block[within:within + take])
            offset += take
            length -= take
        return bytes(result)

    def sector_location(self, sector: int) -> tuple[int, int]:
        true = sector - self.start_block + (2 if self.enhanced else 0)
        if true < 0:
            raise WorkbenchError("GOD sector precedes its declared data range.")
        part, local = divmod(true, SECTORS_PER_PART)
        group = local // 0x198
        offset = local * SECTOR + (group + 2) * 0x1000
        if offset >= PART_BYTES:
            part += 1
            offset = offset % PART_BYTES + 0x2000
        if part >= len(self.parts) or offset + 1 > self.sizes[part]:
            raise WorkbenchError("GOD file refers to a missing fragment or out-of-range sector.")
        return part, offset

    def read_range(self, sector: int, offset: int, length: int) -> bytes:
        if offset < 0 or not 0 <= length <= 16 * 1024 * 1024:
            raise WorkbenchError("Invalid GOD read size.")
        result = bytearray()
        while length:
            extra, within = divmod(offset, SECTOR)
            part, physical = self.sector_location(sector + extra)
            take = min(length, SECTOR - within)
            result.extend(self._physical(part, physical + within, take))
            offset += take
            length -= take
        return bytes(result)


    def _title_text(self) -> str:
        for offset, length in [(0x1691, 128), (0x411, 256)]:
            try:
                title = self.header[offset:offset + length].decode("utf-16-be").split("\0", 1)[0].strip()
                if title:
                    return title
            except UnicodeError:
                pass
        return ""

    def metadata(self) -> dict:
        return {"format": "GOD", "title": self.title, "title_id": self.title_id,
                "media_id": self.media_id, "version": self.version,
                "header_path": str(self.path), "header_sha256": self.header_sha256,
                "parts": [{"name": p.name, "size": n} for p, n in zip(self.parts, self.sizes)],
                "files": sum(not e.is_directory for e in self.entries),
                "extracted_bytes": sum(e.size for e in self.entries if not e.is_directory),
                "integrity": "SHA-1 header, master chain, subtables and all data read; signatures not verified"}


