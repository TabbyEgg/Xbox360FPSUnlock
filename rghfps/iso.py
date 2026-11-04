"""Streaming Xbox XDVDFS ISO reader: bare images and XGD1/2/3 partitions."""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct

from .disc import DiscFileSystem
from .formats import WorkbenchError, inspect_xex


class IsoImage(DiscFileSystem):
    OFFSETS = (("bare/XISO", 0), ("XGD2", 0xFD90000),
               ("XGD1", 0x18300000), ("XGD3", 0x2080000))
    MAGIC = b"MICROSOFT*XBOX*MEDIA"

    def __init__(self, path: Path):
        path = Path(path).expanduser()
        self.stream = None
        if path.is_symlink() or not path.is_file():
            raise WorkbenchError("Choose a regular Xbox 360 ISO/XISO file.")
        self.path = path.resolve()
        self.stat = self.path.stat()
        self.size = self.stat.st_size
        try:
            self.stream = self.path.open("rb")
            self.partition = None
            for layout, offset in self.OFFSETS:
                if offset + 0x10800 > self.size:
                    continue
                self.stream.seek(offset + 0x10000)
                volume = self.stream.read(0x800)
                if volume[:20] == self.MAGIC and volume[-20:] == self.MAGIC:
                    self.layout, self.partition, self.volume = layout, offset, volume
                    break
            if self.partition is None:
                raise WorkbenchError("No supported Xbox XDVDFS volume found. Use a complete bare/XGD1/XGD2/XGD3 ISO; split or compressed images are not supported.")
            sector, length = struct.unpack_from("<II", self.volume, 20)
            if not length:
                raise WorkbenchError("ISO root directory is empty.")
            self.entries = self._directory_tree(sector, length)
            self.title_id = self.media_id = self.version = "00000000"
            self.xex_sha256 = None
            default = next((e for e in self.entries if e.path.casefold() == "default.xex" and not e.is_directory), None)
            if default:
                info = inspect_xex(self.read_file(default))
                self.title_id, self.media_id, self.version = info.title_id, info.media_id, info.version
                self.xex_sha256 = info.sha256
        except Exception:
            self.close()
            raise

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def sector_location(self, sector: int) -> tuple[int, int]:
        offset = self.partition + sector * 0x800
        if sector < 0 or offset >= self.size:
            raise WorkbenchError("ISO file references an out-of-range sector.")
        return 0, offset

    def read_range(self, sector: int, offset: int, length: int) -> bytes:
        _, start = self.sector_location(sector)
        start += offset
        if offset < 0 or not 0 <= length <= 16 * 1024 * 1024 or start + length > self.size:
            raise WorkbenchError("ISO data reference is truncated or outside the image.")
        self.stream.seek(start)
        content = self.stream.read(length)
        if len(content) != length:
            raise WorkbenchError("ISO was truncated or changed while reading.")
        return content

    def metadata(self) -> dict:
        return {"format": "ISO", "title": self.path.stem, "title_id": self.title_id,
                "media_id": self.media_id, "version": self.version,
                "image_path": str(self.path), "layout": self.layout,
                "partition_offset": self.partition, "files": sum(not e.is_directory for e in self.entries),
                "extracted_bytes": sum(e.size for e in self.entries if not e.is_directory),
                "default_xex_sha256": self.xex_sha256,
                "source_fingerprint": {"size": self.size, "mtime_ns": self.stat.st_mtime_ns,
                                       "volume_sha256": hashlib.sha256(self.volume).hexdigest(),
                                       "default_xex_sha256": self.xex_sha256},
                "integrity": "XDVDFS bounds/path checks and extracted-file hashes; no ISO authenticity check"}
