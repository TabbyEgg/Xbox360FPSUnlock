"""Sparse synthetic SVOD writer; no real game data.

Build groups of 204 4-KiB blocks as documented by the iso2god writer, without
using the reader's sector-location code. Sparse holes keep boundary tests small.
"""
import hashlib
from pathlib import Path
import struct


def make_god(folder: Path, files: dict[str, bytes], *, enhanced=False,
             cross_part=False, embedded=False, title=0x55530807) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    header_path = folder / "12345678"
    shift = 2 if enhanced else 0
    sectors = {}

    def put(sector, content):
        for offset in range(0, len(content), 2048):
            sectors[sector + offset // 2048] = content[offset:offset + 2048].ljust(2048, b"\0")

    tree = {}
    for path, content in files.items():
        parent = tree
        parts = path.split("/")
        for component in parts[:-1]:
            parent = parent.setdefault(component, {})
        parent[parts[-1]] = content
    directories = []
    allocation = [40]

    def allocate_directory(tree):
        size = max(2048, ((len(tree) * 256 + 2047) // 2048) * 2048)
        sector = allocation[0]
        allocation[0] += size // 2048 + 1
        descriptor = (tree, sector, size)
        directories.append(descriptor)
        for value in tree.values():
            if isinstance(value, dict):
                allocate_directory(value)
        return descriptor

    root_descriptor = allocate_directory(tree)
    starts = {}
    cursor = max(50, allocation[0] + 7)
    for index, (name, content) in enumerate(files.items()):
        if cross_part and index == len(files) - 1:
            cursor = 0x14388 - 1
        starts[name] = cursor
        put(cursor + shift, content)
        cursor += (len(content) + 2047) // 2048 + 8

    def write_directory(descriptor, prefix=""):
        children, sector, size = descriptor
        directory = bytearray(size)
        for index, (name, value) in enumerate(children.items()):
            path = prefix + name
            if isinstance(value, dict):
                subdir = next(d for d in directories if d[0] is value)
                block, length, attributes = subdir[1], subdir[2], 0x10
                write_directory(subdir, path + "/")
            else:
                block, length, attributes = starts[path], len(value), 0x20
            offset = index * 256
            right = ((index + 1) * 256 // 4) if index + 1 < len(children) else 0
            encoded = name.encode("utf-8")
            struct.pack_into("<HHIIBB", directory, offset, 0, right, block, length, attributes, len(encoded))
            directory[offset + 14:offset + 14 + len(encoded)] = encoded
        put(sector + shift, directory)

    write_directory(root_descriptor)
    volume = bytearray(2048)
    volume[:20] = volume[-20:] = b"MICROSOFT*XBOX*MEDIA"
    struct.pack_into("<II", volume, 20, root_descriptor[1], root_descriptor[2])
    put(0 if enhanced else 32, volume)
    block_count = (max(sectors) + 2) // 2
    parts = (block_count + 0xA1C4 - 1) // 0xA1C4
    data_folder = header_path.with_name(header_path.name + ".data")
    data_folder.mkdir()
    masters = []
    zero = bytes(4096)
    zero_digest = hashlib.sha1(zero).digest()
    for part in range(parts):
        local_blocks = min(0xA1C4, block_count - part * 0xA1C4)
        master = bytearray(4096)
        part_path = data_folder / f"Data{part:04d}"
        with part_path.open("w+b") as output:
            output.write(master)
            for group in range((local_blocks + 203) // 204):
                count = min(204, local_blocks - group * 204)
                table = bytearray(4096)
                data = []
                for block in range(count):
                    global_block = part * 0xA1C4 + group * 204 + block
                    first = sectors.get(global_block * 2)
                    second = sectors.get(global_block * 2 + 1)
                    content = (first or bytes(2048)) + (second or bytes(2048)) if first is not None or second is not None else None
                    data.append(content)
                    table[block * 20:(block + 1) * 20] = hashlib.sha1(content).digest() if content is not None else zero_digest
                master[group * 20:(group + 1) * 20] = hashlib.sha1(table).digest()
                output.write(table)
                for content in data:
                    if content is None:
                        output.seek(4096, 1)
                    else:
                        output.write(content)
            output.truncate(output.tell())
        masters.append(master)
    for part in range(parts - 2, -1, -1):
        masters[part][203 * 20:204 * 20] = hashlib.sha1(masters[part + 1]).digest()
    for part, master in enumerate(masters):
        with (data_folder / f"Data{part:04d}").open("r+b") as output:
            output.write(master)
    header = bytearray(0xB000)
    header[:4] = b"LIVE"
    struct.pack_into(">I", header, 0x340, 0xAD0E)
    struct.pack_into(">II", header, 0x344, 0x7000, 2)
    struct.pack_into(">IIII", header, 0x354, 0x12345678, 0x100, 0x100, title)
    header[0x379] = 0x24
    header[0x37D:0x391] = hashlib.sha1(masters[0]).digest()
    header[0x391] = 0x40 if enhanced else 0
    header[0x392:0x395] = block_count.to_bytes(3, "big")
    struct.pack_into(">I", header, 0x39D, parts)
    struct.pack_into(">I", header, 0x3A9, 1)
    name = "Tom Clancy's Rainbow Six Vegas 2".encode("utf-16-be")
    header[0x1691:0x1691 + len(name)] = name
    header[0x32C:0x340] = hashlib.sha1(header[0x344:]).digest()
    header_path.write_bytes(header)
    if embedded:
        assert parts == 1
        content = (data_folder / "Data0000").read_bytes()
        header_path.write_bytes(header + content)
        (data_folder / "Data0000").unlink()
        data_folder.rmdir()
    return header_path
