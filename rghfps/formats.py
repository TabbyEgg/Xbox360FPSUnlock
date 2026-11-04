"""Bounded XEX2 metadata and read-only STFS title-update support.

Format layout and STFS address calculations follow Xenia's published formats.
See THIRD_PARTY_NOTICES.txt. No executable decryption keys are included.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path, PurePosixPath
import struct


class WorkbenchError(ValueError):
    """An input cannot be handled without guessing or changing the originals."""


MAX_FILE = 512 * 1024 * 1024


def bounded_read(path: Path, limit: int = MAX_FILE) -> bytes:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise WorkbenchError(f"Choose a regular file, not a link or folder: {path}")
    if path.stat().st_size > limit:
        raise WorkbenchError(f"File exceeds the {limit // (1024 * 1024)} MiB limit: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise WorkbenchError("File grew beyond the size limit while reading it.")
    return data


def _need(data: bytes, offset: int, length: int, label: str = "field") -> None:
    if offset < 0 or length < 0 or offset + length > len(data):
        raise WorkbenchError(f"Truncated or invalid {label} at 0x{offset:X}.")


def u32(data: bytes, offset: int) -> int:
    _need(data, offset, 4)
    return struct.unpack_from(">I", data, offset)[0]


@dataclass(frozen=True)
class XexInfo:
    title_id: str | None
    media_id: str | None
    version: str | None
    base_version: str | None
    header_size: int
    image_base: int | None
    image_size: int | None
    encryption: int
    compression: int
    module_flags: int
    sha256: str
    size: int
    patch_source_version: str | None = None
    patch_target_version: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def normalized(self) -> bool:
        return self.encryption == 0 and self.compression == 0


def inspect_xex(data: bytes) -> XexInfo:
    _need(data, 0, 24, "XEX2 header")
    if data[:4] != b"XEX2":
        raise WorkbenchError("This is not a supported XEX2 executable or XEXP patch.")
    header_size, security, count = u32(data, 8), u32(data, 16), u32(data, 20)
    table_end = 24 + count * 8
    if count > 4096 or header_size < table_end or header_size > len(data):
        raise WorkbenchError("Invalid XEX2 header size or optional-header table.")
    opts: dict[int, int] = {}
    for offset in range(24, table_end, 8):
        key, value = struct.unpack_from(">II", data, offset)
        if key in opts:
            raise WorkbenchError(f"Duplicate XEX2 optional header 0x{key:X}.")
        opts[key] = value

    def header_field(key: int, size: int, label: str) -> int:
        if key not in opts:
            raise WorkbenchError(f"XEX2 is missing {label}.")
        offset = opts[key]
        if offset < table_end or offset + size > header_size:
            raise WorkbenchError(f"Invalid XEX2 {label} location.")
        return offset

    flags = u32(data, 4)
    # Delta modules and libraries can legitimately omit execution information.
    # Keep absent fields unknown; the enclosing TU identifies the game, and
    # XexTool checks the delta's binding to its original module.
    execution = None
    if 0x40006 in opts or not flags & (0x8 | 0x10):
        execution = header_field(0x40006, 24, "execution information")
    file_format = header_field(0x3FF, 8, "file-format information")
    format_size = u32(data, file_format)
    if format_size < 8 or file_format + format_size > header_size:
        raise WorkbenchError("Invalid XEX2 file-format information size.")
    encryption, compression = struct.unpack_from(">HH", data, file_format + 4)
    if encryption not in (0, 1) or compression not in (0, 1, 2, 3):
        raise WorkbenchError("Unsupported XEX2 encryption or compression type.")
    image_size = None
    image_base = opts.get(0x10201)
    if security:
        if security < table_end or security + 0x114 > header_size:
            raise WorkbenchError("Invalid XEX2 security information location.")
        image_size = u32(data, security + 4)
        security_base = u32(data, security + 0x110)
        if image_base is None:
            image_base = security_base
        elif image_base != security_base:
            raise WorkbenchError("XEX2 image base disagrees with its security information.")
    source = target = None
    if 0x5FF in opts:
        delta = header_field(0x5FF, 12, "delta-patch descriptor")
        target = f"{u32(data, delta + 4):08X}"
        source = f"{u32(data, delta + 8):08X}"
    return XexInfo(
        title_id=f"{u32(data, execution + 12):08X}" if execution is not None else None,
        media_id=f"{u32(data, execution):08X}" if execution is not None else None,
        version=f"{u32(data, execution + 4):08X}" if execution is not None else None,
        base_version=f"{u32(data, execution + 8):08X}" if execution is not None else None,
        header_size=header_size, image_base=image_base, image_size=image_size,
        encryption=encryption, compression=compression,
        module_flags=flags, sha256=hashlib.sha256(data).hexdigest(),
        size=len(data), patch_source_version=source, patch_target_version=target,
    )


def safe_component(name: str) -> str:
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10))
    if (not name or len(name) > 240 or name in (".", "..")
            or any(ord(c) < 32 or c in '/\\:<>"|?*' for c in name)
            or name.endswith((".", " ")) or name.split(".")[0].upper() in reserved):
        raise WorkbenchError(f"Unsafe or unsupported package filename: {name!r}")
    return name


def safe_relative(path: str) -> PurePosixPath:
    if not isinstance(path, str) or "\\" in path:
        raise WorkbenchError("Resource paths must use relative forward-slash paths.")
    parts = path.split("/")
    for part in parts:
        safe_component(part)
    return PurePosixPath(*parts)


@dataclass(frozen=True)
class StfsEntry:
    path: str
    is_directory: bool
    size: int
    start_block: int
    allocated_blocks: int


class StfsPackage:
    """Extract STFS TU packages; validate hashes, chains, bounds and paths.

    Hash verification detects damaged content, not Microsoft signature validity.
    SVOD and split/GOD containers are intentionally not supported here.
    """

    BLOCK = 0x1000
    PER_TABLE = 170
    END = 0xFFFFFF

    def __init__(self, data: bytes):
        data = bytes(data)
        _need(data, 0, 0x3AD, "STFS header")
        if data[:4] not in (b"CON ", b"LIVE", b"PIRS"):
            raise WorkbenchError("Choose an STFS title-update package, extracted TU folder, or .xexp.")
        self.data = data
        self.header_size = u32(data, 0x340)
        self.data_start = (self.header_size + 0xFFF) & ~0xFFF
        if self.header_size < 0x3AD or self.data_start + 0x2000 > len(data):
            raise WorkbenchError("Invalid or truncated STFS package header.")
        self.content_type = u32(data, 0x344)
        if self.content_type != 0xB0000:
            raise WorkbenchError("This package is not marked as a title update (000B0000).")
        if u32(data, 0x3A9) != 0:
            raise WorkbenchError("SVOD packages are not supported. Select the actual TU package.")
        if data[0x379] != 0x24:
            raise WorkbenchError("Unsupported STFS volume descriptor.")
        self.title_id = f"{u32(data, 0x360):08X}"
        self.media_id = f"{u32(data, 0x354):08X}"
        self.version = f"{u32(data, 0x358):08X}"
        self.flags = data[0x37B]
        self.tables_per_hash = 1 if self.flags & 1 else 2
        self.root_active = 1 if self.flags & 2 else 0
        self.table_count = struct.unpack_from("<H", data, 0x37C)[0]
        self.table_start = int.from_bytes(data[0x37E:0x381], "little")
        self.root_hash = data[0x381:0x395]
        self.total_blocks = u32(data, 0x395)
        if not 1 <= self.table_count <= 1024 or not 1 <= self.total_blocks <= 170**3:
            raise WorkbenchError("STFS block counts are invalid or outside this tool's limits.")
        if self.total_blocks * self.BLOCK > len(data):
            raise WorkbenchError("STFS declares more blocks than its file can contain.")
        self._tables: dict[tuple[int, bytes], bytes] = {}
        self._verified_blocks: dict[int, bytes] = {}
        self.entries = self._read_directory()

    def metadata(self) -> dict:
        return {"title_id": self.title_id, "media_id": self.media_id,
                "version": self.version, "content_type": "000B0000",
                "files": sum(not e.is_directory for e in self.entries),
                "sha256": hashlib.sha256(self.data).hexdigest(),
                "integrity": "STFS SHA-1 block hashes; signatures not verified"}

    def block_offset(self, index: int) -> int:
        if not 0 <= index < self.total_blocks:
            raise WorkbenchError(f"Invalid STFS data block {index}.")
        physical = index
        span = 170
        for _ in range(3):
            physical += ((index + span) // span) * self.tables_per_hash
            if index < span:
                break
            span *= 170
        offset = self.data_start + physical * self.BLOCK
        _need(self.data, offset, self.BLOCK, "STFS data block")
        return offset

    def _hash_offset(self, index: int, level: int) -> int:
        copies = self.tables_per_hash
        step0, step1 = 170 + copies, 28900 + 171 * copies
        if level == 0:
            if index < 170:
                number = 0
            else:
                number = (index // 170) * step0 + (index // 28900 + 1) * copies
                if index >= 28900:
                    number += copies
        elif level == 1:
            number = step0 if index < 28900 else (index // 28900) * step1 + copies
        else:
            number = step1
        return self.data_start + number * self.BLOCK

    def _table(self, offset: int, expected_hash: bytes) -> bytes:
        key = (offset, expected_hash)
        if key not in self._tables:
            _need(self.data, offset, self.BLOCK, "STFS hash table")
            table = self.data[offset:offset + self.BLOCK]
            if hashlib.sha1(table).digest() != expected_hash:
                raise WorkbenchError(f"STFS hash-table integrity failure at 0x{offset:X}.")
            self._tables[key] = table
        return self._tables[key]

    def hash_record(self, index: int) -> bytes:
        if not 0 <= index < self.total_blocks:
            raise WorkbenchError("STFS chain points outside the declared blocks.")
        active = self.root_active if self.tables_per_hash == 2 else 0
        expected = self.root_hash
        highest = 2 if self.total_blocks > 28900 else 1 if self.total_blocks > 170 else 0
        for level in range(highest, -1, -1):
            table = self._table(self._hash_offset(index, level) + active * self.BLOCK, expected)
            record_index = (index // (170**level)) % 170
            record = table[record_index * 24:(record_index + 1) * 24]
            if level == 0:
                return record
            expected = record[:20]
            active = 1 if self.tables_per_hash == 2 and u32(record, 20) & 0x40000000 else 0
        raise AssertionError("Unreachable STFS hash level")

    def read_block(self, index: int) -> bytes:
        if index not in self._verified_blocks:
            record = self.hash_record(index)
            if not u32(record, 20) & 0x80000000:
                raise WorkbenchError("STFS chain refers to an unallocated block.")
            offset = self.block_offset(index)
            block = self.data[offset:offset + self.BLOCK]
            if hashlib.sha1(block).digest() != record[:20]:
                raise WorkbenchError(f"STFS data integrity failure in block {index}.")
            self._verified_blocks[index] = block
        return self._verified_blocks[index]

    def next_block(self, index: int) -> int:
        return u32(self.hash_record(index), 20) & 0xFFFFFF

    def _read_directory(self) -> list[StfsEntry]:
        entries: list[StfsEntry] = []
        seen_paths: set[str] = set()
        seen_blocks: set[int] = set()
        block_index = self.table_start
        for table_number in range(self.table_count):
            if block_index in seen_blocks or block_index == self.END:
                raise WorkbenchError("STFS file-table chain is cyclic or truncated.")
            seen_blocks.add(block_index)
            data = self.read_block(block_index)
            for offset in range(0, self.BLOCK, 64):
                if data[offset] == 0:
                    break
                entry = data[offset:offset + 64]
                length = entry[40] & 0x3F
                if not 1 <= length <= 40:
                    raise WorkbenchError("Invalid STFS filename length.")
                try:
                    name = safe_component(entry[:length].decode("utf-8", "strict"))
                except UnicodeDecodeError as exc:
                    raise WorkbenchError("Unsupported non-UTF-8 STFS filename.") from exc
                parent = int.from_bytes(entry[50:52], "big")
                if parent == 0xFFFF:
                    path = name
                elif parent < len(entries) and entries[parent].is_directory:
                    path = entries[parent].path + "/" + name
                else:
                    raise WorkbenchError("Invalid STFS parent-directory index.")
                folded = path.casefold()
                if folded in seen_paths:
                    raise WorkbenchError("STFS contains duplicate or case-colliding paths.")
                seen_paths.add(folded)
                size = u32(entry, 52)
                if size > MAX_FILE or len(entries) >= 8192:
                    raise WorkbenchError("STFS extraction exceeds this tool's limits.")
                entries.append(StfsEntry(path, bool(entry[40] & 0x80), size,
                                         int.from_bytes(entry[47:50], "little"),
                                         int.from_bytes(entry[44:47], "little")))
            if table_number + 1 < self.table_count:
                block_index = self.next_block(block_index)
        if sum(e.size for e in entries if not e.is_directory) > MAX_FILE:
            raise WorkbenchError("TU contents exceed the 512 MiB extraction limit.")
        return entries

    def file_bytes(self, entry: StfsEntry) -> bytes:
        if entry.is_directory:
            raise WorkbenchError("A directory has no file bytes.")
        needed = (entry.size + self.BLOCK - 1) // self.BLOCK
        if needed > entry.allocated_blocks:
            raise WorkbenchError(f"STFS allocation is too short for {entry.path}.")
        result = bytearray()
        seen: set[int] = set()
        index = entry.start_block
        for _ in range(needed):
            if index == self.END or index in seen:
                raise WorkbenchError(f"STFS file chain is cyclic or truncated: {entry.path}")
            seen.add(index)
            result.extend(self.read_block(index))
            index = self.next_block(index)
        return bytes(result[:entry.size])

    def extract(self, destination: Path) -> list[str]:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        files = []
        for entry in self.entries:
            relative = safe_relative(entry.path)
            target = destination.joinpath(*relative.parts)
            if entry.is_directory:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(self.file_bytes(entry))
                files.append(entry.path)
        return files
