"""Small independently laid-out XDVDFS images, with sparse XGD prefixes."""
import struct
from pathlib import Path


def make_iso(path: Path, xex: bytes, *, prefix=0, bad_name=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    volume = bytearray(2048)
    volume[:20] = volume[-20:] = b"MICROSOFT*XBOX*MEDIA"
    struct.pack_into("<II", volume, 20, 40, 2048)
    directory = bytearray(2048)
    for offset, right, name, sector, content in [(0, 32, "default.xex", 50, xex),
                                                 (128, 0, "../escape" if bad_name else "asset.bin", 90, b"disc asset")]:
        encoded = name.encode()
        struct.pack_into("<HHIIBB", directory, offset, 0, right, sector, len(content), 0x20, len(encoded))
        directory[offset + 14:offset + 14 + len(encoded)] = encoded
    with path.open("wb") as stream:
        for sector, data in [(32, volume), (40, directory), (50, xex), (90, b"disc asset")]:
            stream.seek(prefix + sector * 2048)
            stream.write(data)
    return path
