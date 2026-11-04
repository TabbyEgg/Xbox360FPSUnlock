"""Synthetic format fixtures; no game code or copyrighted executables."""
import hashlib
import struct

from rghfps.formats import StfsPackage

HEADER = 0x400
EXEC = 0x90
FORMAT = 0x80


def make_xex(title=0x55530807, media=0x12345678, version=0x100,
             source_version=None, payload=None, encrypted=0, compression=0,
             execution_info=True, library=False):
    payload = payload if payload is not None else b"\xCC" * 0x2000
    data = bytearray(HEADER + len(payload))
    data[:4] = b"XEX2"
    flags = 0x50 if source_version is not None else 0x8 if library else 1
    options = [(0x3FF, FORMAT), (0x10201, 0x82000000)]
    if execution_info:
        options.insert(1, (0x40006, EXEC))
    if source_version is not None:
        options.append((0x5FF, 0xD0))
    struct.pack_into(">IIIII", data, 4, flags, HEADER, 0, 0x100, len(options))
    for number, option in enumerate(options):
        struct.pack_into(">II", data, 24 + number * 8, *option)
    struct.pack_into(">IHH", data, FORMAT, 8, encrypted, compression)
    struct.pack_into(">IIII", data, EXEC, media, version, 0x100, title)
    if source_version is not None:
        struct.pack_into(">III", data, 0xD0, 0x4C, version, source_version)
    struct.pack_into(">II", data, 0x100, 0x184, len(payload))
    struct.pack_into(">I", data, 0x210, 0x82000000)
    data[HEADER:] = payload
    return bytes(data)


def stfs_fixture(files, *, copies=1, active=0, chains=None, total=None):
    """Lay out backing blocks by insertion events, independent of address formulas."""
    chains = chains or {}
    data_blocks = {0: bytearray(4096)}
    next_blocks = {0: 0xFFFFFF}
    next_free = 1
    for entry_number, (name, content) in enumerate(files):
        needed = (len(content) + 4095) // 4096
        chain = chains.get(name, list(range(next_free, next_free + needed)))
        next_free = max([next_free] + [n + 1 for n in chain])
        entry = bytearray(64)
        entry[:len(name.encode())] = name.encode()
        entry[40] = len(name.encode())
        entry[41:44] = needed.to_bytes(3, "little")
        entry[44:47] = needed.to_bytes(3, "little")
        entry[47:50] = (chain[0] if chain else 0xFFFFFF).to_bytes(3, "little")
        entry[50:52] = b"\xFF\xFF"
        struct.pack_into(">I", entry, 52, len(content))
        data_blocks[0][entry_number * 64:(entry_number + 1) * 64] = entry
        for index, block in enumerate(chain):
            chunk = content[index * 4096:(index + 1) * 4096]
            data_blocks[block] = bytearray(chunk.ljust(4096, b"\x00"))
            next_blocks[block] = chain[index + 1] if index + 1 < len(chain) else 0xFFFFFF
    total = total or max(data_blocks) + 1
    backing = []
    table_positions = {}
    block_positions = {}

    def insert_table(level, group):
        table_positions[(level, group)] = len(backing)
        backing.extend(bytearray(4096) for _ in range(copies))

    for block in range(total):
        if block and block % 28900 == 0:
            if block == 28900:
                insert_table(2, 0)
            insert_table(1, block // 28900)
        elif block == 170:
            insert_table(1, 0)
        if block % 170 == 0:
            insert_table(0, block // 170)
        block_positions[block] = len(backing)
        backing.append(data_blocks.get(block, bytearray(4096)))
    selected = active if copies == 2 else 0

    def table(level, group):
        return backing[table_positions[(level, group)] + selected]

    for block in range(total):
        digest = hashlib.sha1(backing[block_positions[block]]).digest()
        state = 0x80000000 if block in data_blocks else 0
        record = digest + struct.pack(">I", state | next_blocks.get(block, 0xFFFFFF))
        pos = (block % 170) * 24
        table(0, block // 170)[pos:pos + 24] = record
    if total > 170:
        for group in range((total + 169) // 170):
            record = hashlib.sha1(table(0, group)).digest() + struct.pack(">I", 0x40000000 if selected else 0)
            pos = (group % 170) * 24
            table(1, group // 170)[pos:pos + 24] = record
    if total > 28900:
        for group in range((total + 28899) // 28900):
            record = hashlib.sha1(table(1, group)).digest() + struct.pack(">I", 0x40000000 if selected else 0)
            pos = group * 24
            table(2, 0)[pos:pos + 24] = record
    highest = 2 if total > 28900 else 1 if total > 170 else 0
    header = bytearray(0xA000)
    header[:4] = b"LIVE"
    struct.pack_into(">I", header, 0x340, len(header))
    struct.pack_into(">II", header, 0x344, 0xB0000, 1)
    struct.pack_into(">IIII", header, 0x354, 0x12345678, 0x200, 0x100, 0x55530807)
    header[0x379] = 0x24
    header[0x37B] = (1 if copies == 1 else 0) | (2 if selected else 0)
    struct.pack_into("<H", header, 0x37C, 1)
    header[0x37E:0x381] = bytes(3)
    header[0x381:0x395] = hashlib.sha1(table(highest, 0)).digest()
    struct.pack_into(">I", header, 0x395, total)
    return bytes(header) + b"".join(backing)
