"""Neighborhood-compatible XBDM file transport, using only the standard library.

Wire commands are documented by the public clients linked in README.md. This
module does not redistribute the Microsoft SDK or a console plugin.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
import uuid

from .formats import WorkbenchError, safe_component


class XbdmError(WorkbenchError):
    pass


def remote_path(value: str, *, writing=False) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value) or '"' in value:
        raise XbdmError("Console paths must not contain quotes or control characters.")
    value = value.strip().replace("/", "\\")
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{0,15}):\\(.*)", value)
    if not match:
        raise XbdmError("Use a full console path, for example Hdd:\\Games\\RSV2.")
    drive, rest = match.groups()
    parts = rest.rstrip("\\").split("\\") if rest.rstrip("\\") else []
    for part in parts:
        safe_component(part)
        if writing and len(part.encode("utf-8")) > 42:
            raise XbdmError("New console path components must fit FATX's 42-byte filename limit.")
    if writing and drive.casefold() in {"flash", "systemflash", "sysext", "sysaux", "system"}:
        raise XbdmError("Game uploads must target a game storage drive, such as Hdd: or Usb0:.")
    return drive + ":\\" + "\\".join(parts)


def join_remote(parent: str, name: str) -> str:
    safe_component(name)
    return remote_path(remote_path(parent).rstrip("\\") + "\\" + name)


def parent_remote(path: str) -> str:
    path = remote_path(path)
    return remote_path(ntpath.dirname(path))


def _fields(line: str) -> dict[str, str]:
    return {m.group(1).casefold(): m.group(2) if m.group(2) is not None else m.group(3)
            for m in re.finditer(r'([A-Za-z][A-Za-z0-9_]*)=(?:"([^"]*)"|([^\s]+))', line)}


def _number(value: str) -> int:
    try:
        return int(value, 16)  # XBDM sizes are hex, with or without 0x.
    except (ValueError, TypeError) as exc:
        raise XbdmError("Console returned an invalid file size.") from exc


@dataclass(frozen=True)
class RemoteEntry:
    name: str
    size: int
    is_directory: bool


class _Session:
    def __init__(self, host, port, timeout):
        self.socket = socket.create_connection((host, port), timeout=timeout)
        self.socket.settimeout(timeout)
        self.stream = self.socket.makefile("rb")
        try:
            code, self.greeting = self.status()
            if code not in (200, 201):
                raise XbdmError(f"XBDM refused the connection ({code}): {self.greeting}")
        except Exception:
            self.close()
            raise

    def close(self):
        self.stream.close()
        self.socket.close()

    def line(self) -> str:
        data = self.stream.readline(8193)
        if not data or len(data) > 8192 or not data.endswith(b"\n"):
            raise XbdmError("XBDM closed the connection or sent an invalid response line.")
        return data.rstrip(b"\r\n").decode("utf-8", "replace")

    def status(self) -> tuple[int, str]:
        line = self.line()
        match = re.fullmatch(r"(\d{3})[- ](.*)", line)
        if not match:
            raise XbdmError("XBDM returned a malformed status response.")
        return int(match.group(1)), match.group(2)

    def command(self, command: str, expected=(200,)) -> tuple[int, str]:
        if "\r" in command or "\n" in command or len(command.encode("utf-8")) > 2048:
            raise XbdmError("Invalid XBDM command length or line break.")
        self.socket.sendall(command.encode("utf-8") + b"\r\n")
        code, text = self.status()
        if code not in expected:
            raise XbdmError(f"XBDM {code}: {text}")
        return code, text

    def exact(self, length: int) -> bytes:
        result = bytearray()
        while len(result) < length:
            chunk = self.stream.read(min(length - len(result), 256 * 1024))
            if not chunk:
                raise XbdmError("XBDM transfer ended before all bytes arrived.")
            result.extend(chunk)
        return bytes(result)

    def multiline(self, command: str) -> list[str]:
        self.command(command, (202,))
        lines = []
        for _ in range(100001):
            line = self.line()
            if line == ".":
                return lines
            lines.append(line)
        raise XbdmError("XBDM listing exceeds 100,000 entries.")


class XbdmClient:
    """Each operation owns its connection, including all binary/status framing."""

    def __init__(self, host: str, port=730, timeout=15, *, cancel=lambda: False,
                 log=lambda _: None):
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", host.strip()):
            raise XbdmError("Enter your console's IP address or resolvable hostname.")
        if not 1 <= int(port) <= 65535:
            raise XbdmError("Console port must be between 1 and 65535.")
        self.host, self.port, self.timeout = host.strip(), int(port), timeout
        self.cancel, self.log = cancel, log

    def _cancel(self):
        if self.cancel():
            raise XbdmError("Transfer cancelled.")

    @contextmanager
    def session(self):
        self._cancel()
        session = None
        try:
            session = _Session(self.host, self.port, self.timeout)
            yield session
        except (OSError, TimeoutError) as exc:
            raise XbdmError(f"XBDM connection/transfer failed: {exc}. Check the IP, XBDM plugin and local network.") from exc
        finally:
            if session:
                session.close()

    def probe(self) -> dict:
        with self.session() as session:
            greeting = session.greeting
        drives = self.drives()
        return {"host": self.host, "port": self.port, "greeting": greeting, "drives": drives}

    def drives(self) -> list[str]:
        with self.session() as session:
            lines = session.multiline("drivelist")
        drives = []
        for line in lines:
            value = _fields(line).get("drivename")
            if value:
                value = value.rstrip(":\\")
                path = remote_path(value + ":\\")
                if path.casefold() not in {p.casefold() for p in drives}:
                    drives.append(path)
        if not drives:
            raise XbdmError("XBDM responded but returned no usable storage drives.")
        return drives

    def listdir(self, path: str) -> list[RemoteEntry]:
        path = remote_path(path)
        with self.session() as session:
            lines = session.multiline(f'dirlist name="{path}"')
        entries, seen = [], set()
        for line in lines:
            fields = _fields(line)
            name = fields.get("name")
            if not name:
                raise XbdmError("Console directory entry is missing its name.")
            safe_component(name)
            if name.casefold() in seen:
                raise XbdmError("Console listing contains duplicate or case-colliding names.")
            seen.add(name.casefold())
            flags = re.sub(r'"[^"]*"', '""', line)
            directory = bool(re.search(r"(?:^|\s)directory(?:\s|$)", flags, re.IGNORECASE))
            directory = directory or bool(_number(fields.get("attributes", "0")) & 0x10)
            if directory:
                size = 0
            elif "sizehi" in fields and "sizelo" in fields:
                size = (_number(fields["sizehi"]) << 32) | _number(fields["sizelo"])
            else:
                raise XbdmError("Console listing is missing a file's size fields.")
            entries.append(RemoteEntry(name, size, directory))
        return sorted(entries, key=lambda e: (not e.is_directory, e.name.casefold()))

    def stat(self, path: str) -> RemoteEntry | None:
        path = remote_path(path)
        if path.endswith(":\\"):
            return RemoteEntry(path[:-2], 0, True)
        name = ntpath.basename(path)
        return next((e for e in self.listdir(parent_remote(path)) if e.name.casefold() == name.casefold()), None)

    def _receive(self, path: str, sink=None, *, expected_size=None) -> dict:
        path = remote_path(path)
        entry = self.stat(path)
        if entry is None or entry.is_directory:
            raise XbdmError("The selected console file is missing or is a directory.")
        if entry.size > 0xFFFFFFFF:
            raise XbdmError("This XBDM getfile transport supports files up to 4 GiB minus one byte.")
        if expected_size is not None and entry.size != expected_size:
            raise XbdmError("Console file size changed since the transfer was planned.")
        digest = hashlib.sha256()
        with self.session() as session:
            session.command(f'getfile name="{path}"', (203,))
            prefix = session.exact(4)
            # Older client implementations disagree about framing byte order.
            # Resolve against the independently listed file size, never guess.
            if entry.size not in (int.from_bytes(prefix, "little"), int.from_bytes(prefix, "big")):
                raise XbdmError("XBDM binary length does not match the console directory listing.")
            done, last = 0, 0
            while done < entry.size:
                self._cancel()
                chunk = session.exact(min(256 * 1024, entry.size - done))
                if sink:
                    sink.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if done - last >= 8 * 1024 * 1024:
                    self.log(f"Receiving {entry.name}: {done // (1024 * 1024)} / {entry.size // (1024 * 1024)} MiB")
                    last = done
        return {"size": entry.size, "sha256": digest.hexdigest(), "remote_path": path}

    def hash_file(self, path: str) -> dict:
        return self._receive(path)

    def download_file(self, path: str, destination: Path, *, expected_size=None) -> dict:
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise XbdmError("Choose a new local filename; downloads never replace existing files.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".rghfps-download-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                result = self._receive(path, stream, expected_size=expected_size)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
            return result
        except FileExistsError as exc:
            raise XbdmError("Local output appeared during transfer; it was not overwritten.") from exc
        finally:
            Path(temporary).unlink(missing_ok=True)

    def walk(self, path: str) -> list[tuple[str, RemoteEntry]]:
        path = remote_path(path)
        result, pending, seen, total = [], [(path, "", 0)], set(), 0
        while pending:
            self._cancel()
            current, relative, depth = pending.pop()
            if depth > 64 or current.casefold() in seen:
                raise XbdmError("Remote directory tree is too deep or cyclic.")
            seen.add(current.casefold())
            for entry in self.listdir(current):
                child = relative + "/" + entry.name if relative else entry.name
                result.append((child, entry))
                if len(result) > 100000:
                    raise XbdmError("Remote folder exceeds the 100,000-entry limit.")
                if entry.is_directory:
                    pending.append((join_remote(current, entry.name), child, depth + 1))
                else:
                    total += entry.size
                    if total > 16 * 1024**3:
                        raise XbdmError("Choose a smaller game folder; this transfer exceeds 16 GiB.")
        return result

    def download_tree(self, path: str, destination: Path) -> dict:
        path = remote_path(path)
        destination = Path(destination)
        if path.endswith(":\\"):
            raise XbdmError("Select a game/TU folder, rather than downloading an entire console drive.")
        plan = self.walk(path)
        needed = sum(entry.size for _, entry in plan if not entry.is_directory)
        parent = destination.parent
        while not parent.exists():
            parent = parent.parent
        if shutil.disk_usage(parent).free < needed + 16 * 1024 * 1024:
            raise XbdmError("Not enough free PC storage for this console folder.")
        destination.mkdir(parents=True, exist_ok=False)
        receipts = {}
        try:
            for relative, entry in plan:
                self._cancel()
                target = destination.joinpath(*relative.split("/"))
                if entry.is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    remote = remote_path(path + "\\" + relative.replace("/", "\\"))
                    receipts[relative] = self.download_file(remote, target, expected_size=entry.size)
            return {"local_root": str(destination), "files": receipts, "bytes": needed}
        except Exception:
            shutil.rmtree(destination)
            raise

    def pull(self, path: str, destination: Path) -> dict:
        """Download a selection, automatically pairing GOD headers/data folders."""
        path = remote_path(path)
        destination = Path(destination)
        entry = self.stat(path)
        if entry is None:
            raise XbdmError("Selected console item no longer exists.")
        if entry.is_directory and entry.name.casefold().endswith(".data"):
            header = path[:-5]
            if self.stat(header) is not None:
                path, entry = header, self.stat(header)
        if entry.is_directory:
            return self.download_tree(path, destination)
        destination.mkdir(parents=True, exist_ok=False)
        try:
            target = destination / entry.name
            receipt = self.download_file(path, target, expected_size=entry.size)
            from .god import is_god_header
            if is_god_header(target):
                data_path = path + ".data"
                companion = self.stat(data_path)
                if companion and companion.is_directory:
                    self.log("GOD header detected; fetching its matching .data folder.")
                    self.download_tree(data_path, destination / (entry.name + ".data"))
                else:
                    from .god import GodContainer
                    with GodContainer(target):
                        pass  # A supported self-contained GOD needs no companion.
            return {"local_root": str(destination), "selected_file": str(target), "files": {entry.name: receipt}}
        except Exception:
            shutil.rmtree(destination)
            raise

    def _simple(self, command):
        with self.session() as session:
            session.command(command)

    def _rename_new(self, source, target):
        source, target = remote_path(source, writing=True), remote_path(target, writing=True)
        if self.stat(target) is not None:
            raise XbdmError("Console destination already exists; choose a new name.")
        self._simple(f'rename name="{source}" newname="{target}"')

    def upload_new_file(self, source: Path, target: str) -> dict:
        source, target = Path(source), remote_path(target, writing=True)
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 0xFFFFFFFF:
            raise XbdmError("Choose a regular local file smaller than 4 GiB.")
        if self.stat(target) is not None:
            raise XbdmError("Console destination exists. Existing files are never overwritten.")
        stage = join_remote(parent_remote(target), "rghfps_" + uuid.uuid4().hex[:16] + ".tmp")
        if self.stat(stage) is not None:
            raise XbdmError("A staging-name collision occurred; retry the upload.")
        size = source.stat().st_size
        digest = hashlib.sha256()
        committed = False
        try:
            with self.session() as session, source.open("rb") as stream:
                session.command(f'sendfile name="{stage}" length=0x{size:X}', (204,))
                sent, last = 0, 0
                while sent < size:
                    self._cancel()
                    chunk = stream.read(min(256 * 1024, size - sent))
                    if not chunk:
                        raise XbdmError("Local source changed or was truncated during upload.")
                    session.socket.sendall(chunk)
                    digest.update(chunk)
                    sent += len(chunk)
                    if sent - last >= 8 * 1024 * 1024:
                        self.log(f"Sending {source.name}: {sent // (1024 * 1024)} / {size // (1024 * 1024)} MiB")
                        last = sent
                if stream.read(1):
                    raise XbdmError("Local source grew during upload.")
                code, message = session.status()
                if code != 200:
                    raise XbdmError(f"Console rejected the completed upload ({code}): {message}")
            self.log(f"Verifying uploaded bytes: {source.name}")
            verification = self.hash_file(stage)
            if verification["size"] != size or verification["sha256"] != digest.hexdigest():
                raise XbdmError("Uploaded file failed its SHA-256 readback check.")
            self._cancel()
            self._rename_new(stage, target)
            committed = True
            return {"host": self.host, "port": self.port, "remote_path": target,
                    "size": size, "sha256": digest.hexdigest(), "verified": True, "kind": "file"}
        finally:
            if not committed:
                # Cleanup is limited to this operation's random temp name.
                saved_cancel = self.cancel
                try:
                    self.cancel = lambda: False
                    self._simple(f'delete name="{stage}"')
                except Exception as exc:
                    self.log(f"An incomplete staging file may remain at {stage}: {exc}")
                finally:
                    self.cancel = saved_cancel

    def upload_new_tree(self, source: Path, target: str) -> dict:
        source, target = Path(source), remote_path(target, writing=True)
        if source.is_symlink() or not source.is_dir() or target.endswith(":\\"):
            raise XbdmError("Choose a game folder and a new console folder name.")
        if self.stat(target) is not None:
            raise XbdmError("Console folder exists. Choose a new destination for this game copy.")
        plan, total = [], 0
        for current, directories, files in os.walk(source, followlinks=False):
            for name in directories + files:
                path = Path(current) / name
                if path.is_symlink():
                    raise XbdmError("Upload folders must not contain links.")
                relative = path.relative_to(source).as_posix()
                remote_path(target + "\\" + relative.replace("/", "\\"), writing=True)
                if path.is_file():
                    total += path.stat().st_size
                plan.append((relative, path.is_dir()))
                if len(plan) > 100000 or total > 16 * 1024**3:
                    raise XbdmError("Upload exceeds the game-copy transfer limits.")
        stage = join_remote(parent_remote(target), "RGHFPS-stage-" + uuid.uuid4().hex[:12])
        if self.stat(stage) is not None:
            raise XbdmError("Staging-name collision; retry the upload.")
        self._simple(f'mkdir name="{stage}"')
        receipts = {}
        try:
            for relative, directory in sorted(plan, key=lambda item: (item[0].count("/"), not item[1], item[0])):
                self._cancel()
                remote = remote_path(stage + "\\" + relative.replace("/", "\\"), writing=True)
                if directory:
                    self._simple(f'mkdir name="{remote}"')
                else:
                    receipts[relative] = self.upload_new_file(source.joinpath(*relative.split("/")), remote)
            self._rename_new(stage, target)
        except Exception as exc:
            raise XbdmError(f"Upload stopped: {exc} Partial new staging folder: {stage}. Existing games were not replaced.") from exc
        return {"host": self.host, "port": self.port, "remote_path": target,
                "files": {name: {"sha256": item["sha256"], "size": item["size"]} for name, item in receipts.items()},
                "verified": True, "kind": "folder", "bytes": total}

    def launch_verified(self, receipt: dict, relative="default.xex") -> dict:
        if not receipt.get("verified") or receipt.get("host") != self.host or receipt.get("port") != self.port:
            raise XbdmError("Upload and verify this copy on the selected console before launching it.")
        if receipt["kind"] == "file":
            path, expected = receipt["remote_path"], receipt["sha256"]
        else:
            safe_component(relative)
            path = join_remote(receipt["remote_path"], relative)
            record = next((record for key, record in receipt["files"].items() if key.casefold() == relative.casefold()), None)
            if not record:
                raise XbdmError("Uploaded folder has no root default.xex to launch.")
            expected = record["sha256"]
        if not path.casefold().endswith(".xex"):
            raise XbdmError("Only an uploaded XEX executable can be launched.")
        if self.hash_file(path)["sha256"] != expected:
            raise XbdmError("Console executable changed since verification; launch was cancelled.")
        self._simple(f'magicboot title="{path}" directory="{parent_remote(path)}"')
        return {"requested": True, "remote_path": path}
