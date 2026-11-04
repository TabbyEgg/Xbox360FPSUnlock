"""Tk panel for the same XBDM connection used by Xbox Neighborhood."""
from __future__ import annotations

from datetime import datetime
import ntpath
import os
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .formats import WorkbenchError, bounded_read, inspect_xex
from .god import is_god_header
from .workflow import game_executable
from .xbdm import XbdmClient, join_remote, parent_remote, remote_path


class ConsolePanel:
    def __init__(self, app, parent):
        self.app = app
        self.host = tk.StringVar()
        self.port = tk.StringVar(value="730")
        self.path = tk.StringVar(value="Hdd:\\")
        self.drive = tk.StringVar(value="Hdd:\\")
        self.local = tk.StringVar()
        self.target = tk.StringVar(value="Hdd:\\Games\\RSV2\\default_unlocked.xex")
        self.info = tk.StringVar(value="Enter the IP from your working Neighborhood setup. The console needs XBDM running.")
        self.cancel = threading.Event()
        self.active = False
        self.entries = []
        self.last_upload = None
        connect = ttk.Frame(parent)
        connect.pack(fill="x", pady=(0, 8))
        ttk.Label(connect, text="Console IP / hostname").pack(side="left", padx=(0, 7))
        ttk.Entry(connect, textvariable=self.host, width=22).pack(side="left", padx=(0, 9))
        ttk.Label(connect, text="Port").pack(side="left", padx=(0, 5))
        ttk.Entry(connect, textvariable=self.port, width=6).pack(side="left", padx=(0, 8))
        app._button(connect, "Connect", self._connect).pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(connect, text="Cancel transfer", command=self.cancel.set)
        self.cancel_button.pack(side="left")
        self.cancel_button.state(["disabled"])
        ttk.Label(parent, textvariable=self.info, style="Small.TLabel", wraplength=920).pack(anchor="w", pady=(0, 8))
        browse = ttk.Frame(parent)
        browse.pack(fill="x", pady=(0, 7))
        self.drives = ttk.Combobox(browse, textvariable=self.drive, values=["Hdd:\\"], state="readonly", width=11)
        self.drives.pack(side="left", padx=(0, 7))
        self.drives.bind("<<ComboboxSelected>>", self._drive_changed)
        ttk.Entry(browse, textvariable=self.path).pack(side="left", fill="x", expand=True, padx=(0, 7))
        app._button(browse, "Up", self._up).pack(side="left", padx=(0, 5))
        app._button(browse, "Browse", self._browse).pack(side="left")
        table_frame = ttk.Frame(parent)
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_frame, columns=("name", "type", "size"), show="headings", height=5)
        for column, label, width in [("name", "Console file / folder", 500), ("type", "Type", 90), ("size", "Bytes", 130)]:
            self.table.heading(column, text=label)
            self.table.column(column, width=width)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<Double-1>", self._open_selected)
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=7)
        app._button(actions, "Pull selection to PC…", self._pull).pack(side="left")
        ttk.Label(actions, text="GOD headers automatically include their .data folder.", style="Small.TLabel").pack(side="left", padx=12)
        ttk.Separator(parent).pack(fill="x", pady=6)
        upload = ttk.Frame(parent)
        upload.pack(fill="x")
        upload.columnconfigure(1, weight=1)
        ttk.Label(upload, text="Local patched copy").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(upload, textvariable=self.local).grid(row=0, column=1, sticky="ew", pady=5)
        picks = ttk.Frame(upload)
        picks.grid(row=0, column=2, sticky="w", padx=(7, 0))
        app._button(picks, "XEX…", self._pick_file).pack(side="left", padx=(0, 5))
        app._button(picks, "Game folder…", self._pick_folder).pack(side="left")
        ttk.Label(upload, text="New console path").grid(row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(upload, textvariable=self.target).grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=7)
        app._button(actions, "Upload new copy & verify", self._upload).pack(side="left", padx=(0, 8))
        app._button(actions, "Launch verified copy", self._launch).pack(side="left")
        ttk.Label(parent, text="Uploads use a new filename/folder and a full SHA-256 readback. Launch is a separate action. No game-memory or firmware writes are performed.", style="Small.TLabel", wraplength=920).pack(anchor="w")
        app.root.after(200, self._refresh_cancel)

    def _refresh_cancel(self):
        self.cancel_button.state(["!disabled"] if self.active and self.app.busy else ["disabled"])
        self.app.root.after(200, self._refresh_cancel)

    def _run(self, action, done):
        if self.app.busy:
            return
        host, port = self.host.get(), self.port.get()
        self.cancel.clear()
        self.active = True

        def work(log):
            try:
                return action(XbdmClient(host, port, cancel=self.cancel.is_set, log=log), log)
            finally:
                self.active = False

        self.app._run(work, done)

    def _connect(self):
        def done(result):
            self.drives["values"] = result["drives"]
            drive = next((d for d in result["drives"] if d.casefold() == "hdd:\\"), result["drives"][0])
            self.drive.set(drive)
            self.path.set(drive)
            self.info.set(f"Connected to {result['host']}:{result['port']} — {result['greeting']}")
            self._browse()
        self._run(lambda client, log: client.probe(), done)

    def _browse(self):
        path = self.path.get()

        def done(result):
            path, self.entries = result
            self.path.set(path)
            self.table.delete(*self.table.get_children())
            for index, entry in enumerate(self.entries):
                self.table.insert("", "end", iid=str(index), values=(entry.name, "Folder" if entry.is_directory else "File", "" if entry.is_directory else f"{entry.size:,}"))
        self._run(lambda client, log: (remote_path(path), client.listdir(path)), done)

    def _drive_changed(self, _):
        if not self.app.busy:
            self.path.set(self.drive.get())
            self._browse()

    def _up(self):
        try:
            self.path.set(parent_remote(self.path.get()))
            self._browse()
        except WorkbenchError as exc:
            messagebox.showerror("Console path", str(exc))

    def _selected(self):
        selected = self.table.selection()
        return self.entries[int(selected[0])] if selected else None

    def _open_selected(self, _):
        entry = self._selected()
        if entry and entry.is_directory and not self.app.busy:
            self.path.set(join_remote(self.path.get(), entry.name))
            self._browse()

    def _pull(self):
        entry = self._selected()
        if entry is None:
            messagebox.showinfo("Choose an item", "Select a game folder, GOD header, XEX, or TU package in the console list.")
            return
        source = join_remote(self.path.get(), entry.name)
        parent = filedialog.askdirectory(title="Choose a local parent for a new console download")
        if not parent:
            return
        destination = Path(parent) / ("Xbox-pull-" + datetime.now().strftime("%Y%m%d-%H%M%S"))

        def done(result):
            root = Path(result["local_root"])
            selected = Path(result["selected_file"]) if result.get("selected_file") else None
            is_tu = bool(selected and selected.suffix.casefold() == ".xexp")
            if selected:
                with selected.open("rb") as stream:
                    prefix = stream.read(0x3AD)
                is_tu = is_tu or (len(prefix) >= 0x348 and prefix[:4] in (b"LIVE", b"PIRS", b"CON ") and int.from_bytes(prefix[0x344:0x348], "big") == 0xB0000)
            if is_tu:
                self.app.tu.set(str(selected))
            elif any(p.name.casefold() == "default.xexp" for p in root.iterdir()):
                self.app.tu.set(str(root))
            elif selected and (selected.suffix.casefold() in (".xex", ".iso", ".xiso") or is_god_header(selected)):
                self.app.game.set(str(selected))
            elif not selected:
                self.app.game.set(str(root))
            if selected and selected.suffix.casefold() == ".xex":
                self.target.set(join_remote(parent_remote(source), ntpath.splitext(entry.name)[0] + "_unlocked.xex"))
            self.info.set("Downloaded to " + str(root))
            self.app.summary.set("Console download ready. Choose the matching TU, then Prepare copy.")
            self.app.notebook.select(self.app.prepare_tab)

        self._run(lambda client, log: client.pull(source, destination), done)

    def _pick_file(self):
        path = filedialog.askopenfilename(title="Choose the exported patched XEX", filetypes=[("XEX", "*.xex")])
        if path:
            self.local.set(path)

    def _pick_folder(self):
        path = filedialog.askdirectory(title="Choose the complete exported game/ folder")
        if path:
            self.local.set(path)

    def suggest_export(self, path: Path, receipt: dict):
        if receipt.get("output_layout") == "complete_extracted_game":
            self.local.set(str(path / "game"))
            self.target.set("Hdd:\\Games\\RSV2-FPS-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        else:
            self.local.set(str(path / "game_overlay" / "default.xex"))

    def _upload(self):
        source, target = Path(self.local.get()), self.target.get()

        def work(client, log):
            if source.is_dir():
                if source.name.casefold() == "game_overlay":
                    raise WorkbenchError("game_overlay is not a full game. Upload its XEX under a new name in the existing game folder, or select the complete game/ export.")
                game_executable(source)
                return client.upload_new_tree(source, target)
            inspect_xex(bounded_read(source))
            if not target.casefold().endswith(".xex"):
                raise WorkbenchError("Give the new executable a console filename ending in .xex.")
            return client.upload_new_file(source, target)

        def done(receipt):
            self.last_upload = receipt
            self.info.set("Uploaded and readback-verified: " + receipt["remote_path"] + ". Launch is available.")
        self._run(work, done)

    def _launch(self):
        if not self.last_upload:
            messagebox.showinfo("Upload first", "Upload and verify the patched copy before launching it.")
            return
        receipt = self.last_upload
        self._run(lambda client, log: client.launch_verified(receipt),
                  lambda result: self.info.set("Launch requested: " + result["remote_path"] + ". Check the console."))
