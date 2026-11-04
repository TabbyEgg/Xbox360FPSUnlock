"""Protocol tests against an in-process TCP console, not real hardware."""
import hashlib
import ntpath
from pathlib import Path
import re
import socketserver
import tempfile
import threading
import unittest

from rghfps.formats import WorkbenchError
from rghfps.xbdm import XbdmClient, XbdmError, remote_path


class ConsoleServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ConsoleHandler)
        self.files = {"hdd:\\games\\original.xex": bytes(range(256)) * 5 + b"\r\n200-OK\r\n"}
        self.directories = {"hdd:\\", "hdd:\\games"}
        self.commands = []
        self.byteorder = "little"
        self.bad_length = False
        self.truncate = False
        self.corrupt_upload = False
        self.bad_greeting = False
        self.errors = []


class ConsoleHandler(socketserver.StreamRequestHandler):
    def send(self, value):
        data = value.encode("utf-8") if isinstance(value, str) else value
        for start in range(0, len(data), 7):
            self.wfile.write(data[start:start + 7])
        self.wfile.flush()

    def handle(self):
        try:
            self.process()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when a rejected transfer closes early.
        except Exception as exc:
            self.server.errors.append(repr(exc))

    def process(self):
        server = self.server
        self.send("bad greeting\r\n" if server.bad_greeting else "201- connected\r\n")
        line = self.rfile.readline().decode("utf-8").strip()
        if not line:
            return
        server.commands.append(line)
        verb = line.split(" ", 1)[0]
        args = dict(re.findall(r'(\w+)="([^"]*)"', line))
        name = args.get("name", "").casefold()
        if verb == "drivelist":
            self.send('202- drives\r\ndrivename="Hdd"\r\n.\r\n')
        elif verb == "dirlist":
            if name not in server.directories:
                self.send("402- no such directory\r\n")
                return
            records = []
            for path in sorted(server.directories | set(server.files)):
                if path == name or ntpath.dirname(path) != name:
                    continue
                filename = ntpath.basename(path)
                if path in server.directories:
                    records.append(f'name="{filename}" directory')
                else:
                    size = len(server.files[path])
                    records.append(f'name="{filename}" sizehi=0x0 sizelo=0x{size:X}')
            self.send("202- entries\r\n" + "".join(r + "\r\n" for r in records) + ".\r\n")
        elif verb == "getfile":
            if name not in server.files:
                self.send("402- no such file\r\n")
                return
            data = server.files[name]
            prefix = (len(data) + int(server.bad_length)).to_bytes(4, server.byteorder)
            self.send(b"203- binary\r\n" + prefix + (data[:3] if server.truncate else data))
        elif verb == "sendfile":
            if name in server.files or ntpath.dirname(name) not in server.directories:
                self.send("410- invalid destination\r\n")
                return
            length = int(re.search(r"length=(\S+)", line).group(1), 16)
            self.send("204- send binary\r\n")
            data = self.rfile.read(length)
            if len(data) != length:
                return
            server.files[name] = bytes([data[0] ^ 1]) + data[1:] if server.corrupt_upload and data else data
            self.send("200- stored\r\n")
        elif verb == "rename":
            target = args["newname"].casefold()
            if target in server.files or target in server.directories:
                self.send("410- exists\r\n")
                return
            if name in server.files:
                server.files[target] = server.files.pop(name)
            elif name in server.directories:
                for old in list(server.files):
                    if old.startswith(name + "\\"):
                        server.files[target + old[len(name):]] = server.files.pop(old)
                for old in list(server.directories):
                    if old == name or old.startswith(name + "\\"):
                        server.directories.remove(old)
                        server.directories.add(target + old[len(name):])
            else:
                self.send("402- no such item\r\n")
                return
            self.send("200- renamed\r\n")
        elif verb == "delete":
            server.files.pop(name, None)
            self.send("200- removed\r\n")
        elif verb == "mkdir":
            if name in server.directories or ntpath.dirname(name) not in server.directories:
                self.send("410- invalid directory\r\n")
            else:
                server.directories.add(name)
                self.send("200- created\r\n")
        elif verb == "magicboot":
            self.send("200- launch requested\r\n")
        else:
            self.send("400- unknown command\r\n")


class XbdmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = ConsoleServer()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.client = XbdmClient("127.0.0.1", self.server.server_address[1], timeout=2)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertEqual(self.server.errors, [])

    def test_probe_and_fragmented_binary_download_in_both_byte_orders(self):
        self.assertEqual(self.client.probe()["drives"], ["Hdd:\\"])
        original = self.server.files["hdd:\\games\\original.xex"]
        for order in ("little", "big"):
            with self.subTest(order=order):
                self.server.byteorder = order
                output = self.root / (order + ".xex")
                result = self.client.download_file("Hdd:\\Games\\Original.xex", output)
                self.assertEqual(output.read_bytes(), original)
                self.assertEqual(result["sha256"], hashlib.sha256(original).hexdigest())

    def test_bad_binary_length_and_truncation_leave_no_local_file(self):
        for mode in ("bad_length", "truncate"):
            with self.subTest(mode=mode):
                setattr(self.server, mode, True)
                output = self.root / (mode + ".xex")
                with self.assertRaises(XbdmError):
                    self.client.download_file("Hdd:\\Games\\Original.xex", output)
                self.assertEqual(list(self.root.iterdir()), [])
                setattr(self.server, mode, False)

    def test_upload_verifies_bytes_and_launch_is_separate(self):
        source = self.root / "patched.xex"
        source.write_bytes(b"XEX2" + bytes(range(256)) * 3)
        receipt = self.client.upload_new_file(source, "Hdd:\\Games\\Unlocked.xex")
        self.assertTrue(receipt["verified"])
        self.assertEqual(self.server.files["hdd:\\games\\unlocked.xex"], source.read_bytes())
        self.assertFalse(any(c.startswith("magicboot") for c in self.server.commands))
        self.client.launch_verified(receipt)
        self.assertEqual(sum(c.startswith("magicboot") for c in self.server.commands), 1)
        self.server.files["hdd:\\games\\unlocked.xex"] = b"changed"
        with self.assertRaisesRegex(XbdmError, "changed"):
            self.client.launch_verified(receipt)
        self.assertEqual(sum(c.startswith("magicboot") for c in self.server.commands), 1)

    def test_corrupt_upload_is_never_published_and_temp_file_is_removed(self):
        self.server.corrupt_upload = True
        source = self.root / "patched.xex"
        source.write_bytes(b"XEX2 candidate")
        before = dict(self.server.files)
        with self.assertRaisesRegex(XbdmError, "SHA-256"):
            self.client.upload_new_file(source, "Hdd:\\Games\\Unlocked.xex")
        self.assertEqual(self.server.files, before)
        self.assertFalse(any(c.startswith("rename") for c in self.server.commands))

    def test_existing_outputs_and_cancel_preserve_originals(self):
        source = self.root / "existing.xex"
        source.write_bytes(b"existing")
        before = dict(self.server.files)
        with self.assertRaisesRegex(XbdmError, "exists"):
            self.client.upload_new_file(source, "Hdd:\\Games\\Original.xex")
        with self.assertRaisesRegex(XbdmError, "never replace"):
            self.client.download_file("Hdd:\\Games\\Original.xex", source)
        self.client.cancel = lambda: True
        with self.assertRaisesRegex(XbdmError, "cancelled"):
            self.client.download_file("Hdd:\\Games\\Original.xex", self.root / "cancelled.xex")
        self.assertEqual(self.server.files, before)
        self.assertEqual(source.read_bytes(), b"existing")
        self.assertFalse((self.root / "cancelled.xex").exists())

    def test_folder_upload_and_download_preserve_nested_assets(self):
        source = self.root / "game"
        (source / "assets").mkdir(parents=True)
        (source / "assets" / "data.bin").write_bytes(b"asset")
        (source / "default.xex").write_bytes(b"XEX2 synthetic")
        receipt = self.client.upload_new_tree(source, "Hdd:\\Games\\NewCopy")
        self.assertTrue(receipt["verified"])
        self.assertEqual(len(receipt["files"]), 2)
        output = self.root / "download"
        self.client.download_tree("Hdd:\\Games\\NewCopy", output)
        self.assertEqual((output / "assets" / "data.bin").read_bytes(), b"asset")
        self.assertEqual((output / "default.xex").read_bytes(), b"XEX2 synthetic")

    def test_invalid_paths_and_malformed_greeting_are_rejected(self):
        for path in ('Hdd:\\Games\\..\\x.xex', 'Hdd:\\Games\\x".xex', 'Hdd:\\Games\\x\r\nreboot',
                     'relative.xex', 'Flash:\\default.xex', 'Hdd:\\Games\\' + "x" * 43):
            with self.subTest(path=path), self.assertRaises(WorkbenchError):
                remote_path(path, writing=True)
        self.server.bad_greeting = True
        with self.assertRaisesRegex(XbdmError, "malformed"):
            self.client.probe()


if __name__ == "__main__":
    unittest.main()
