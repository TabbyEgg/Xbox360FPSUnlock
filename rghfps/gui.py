"""Tk desktop UI. Python's standard Windows installation includes Tk."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import webbrowser

from . import __version__
from .catalog import filter_catalog, find_installations, load_catalog
from .neighborhood_ui import ConsolePanel
from .tu import extract_title_update, inspect_title_update
from .formats import WorkbenchError, bounded_read, inspect_xex
from .patches import (bind_signatures, description_profile, read_recipe,
                      recipe_preview, write_json)
from .workflow import (export_candidate, extract_disc_input, inspect_game_input, load_project,
                       prepare_game, recipe_from_reference)


def bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"RGH FPS Workbench {__version__}")
        self.root.geometry("1100x850")
        self.root.minsize(900, 720)
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.recipe: dict | None = None
        self.buttons: list[ttk.Button] = []
        self.home = Path.home() / "Documents" / "RGH-FPS-Workbench"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.game = tk.StringVar()
        self.tu = tk.StringVar()
        self.tool = tk.StringVar(value=str(bundle_root() / "tools" / "xextool.exe"))
        self.project = tk.StringVar(value=str(self.home / f"project-{stamp}"))
        self.summary = tk.StringVar(value="Choose the extracted game folder, or its default.xex.")
        self.status = tk.StringVar(value="Ready — local workflow and optional console connection.")
        self.patch_status = tk.StringVar(value="Prepare or open a project to inspect its exact build.")
        self.profile_name = tk.StringVar()
        self.profiles: dict[str, Path] = {}
        self.imported_profile: dict | None = None
        self.catalog = load_catalog(bundle_root())
        self.catalog_query = tk.StringVar()
        self.include_references = tk.BooleanVar(value=False)
        self.catalog_rows = []
        self.local_games = []
        self._make_ui()
        self.project.trace_add("write", lambda *_: self._invalidate())
        self.root.after(100, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _button(self, parent, text, command, **kwargs):
        button = ttk.Button(parent, text=text, command=command, **kwargs)
        self.buttons.append(button)
        return button

    def _make_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#F3F5F8")
        style.configure("TLabel", background="#F3F5F8", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 7))
        style.configure("TLabelframe", background="#F3F5F8")
        style.configure("TLabelframe.Label", background="#F3F5F8", font=("Segoe UI", 10, "bold"))
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"), foreground="#182A42")
        style.configure("Small.TLabel", font=("Segoe UI", 9), foreground="#536277")
        style.configure("Treeview", rowheight=29, font=("Consolas", 9))
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="RGH FPS Workbench", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="XEX, ISO or GOD input. Matching title update. Exact edits. Optional Neighborhood connection.", style="Small.TLabel").pack(anchor="w", pady=(2, 15))
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        prepare_tab = ttk.Frame(notebook, padding=18)
        self.prepare_tab = prepare_tab
        self.patch_tab = ttk.Frame(notebook, padding=18)
        games_tab = ttk.Frame(notebook, padding=18)
        console_tab = ttk.Frame(notebook, padding=18)
        notes_tab = ttk.Frame(notebook, padding=18)
        notebook.add(prepare_tab, text="  1  Prepare  ")
        notebook.add(self.patch_tab, text="  2  Scan & patch  ")
        notebook.add(games_tab, text="  Games  ")
        notebook.add(console_tab, text="  Neighborhood / XBDM  ")
        notebook.add(notes_tab, text="  Support & sources  ")
        self.notebook = notebook

        paths = ttk.LabelFrame(prepare_tab, text="Your local files", padding=12)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)
        rows = [("Game", self.game, [("Folder…", self._game_folder), ("XEX…", self._game_file), ("ISO…", self._iso_file), ("GOD…", self._god_file)]),
                ("Title update", self.tu, [("Package / XEXP…", self._tu_file), ("Folder…", self._tu_folder)]),
                ("XexTool", self.tool, [("Browse…", self._tool_file)]),
                ("New project", self.project, [("Choose parent…", self._project_parent)])]
        for row, (label, variable, buttons) in enumerate(rows):
            ttk.Label(paths, text=label, width=12).grid(row=row, column=0, sticky="w", pady=6)
            ttk.Entry(paths, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=6)
            actions = ttk.Frame(paths)
            actions.grid(row=row, column=2, sticky="w")
            for caption, action in buttons:
                self._button(actions, caption, action).pack(side="left", padx=(0, 5))
        ttk.Label(paths, text="GOD: select the header beside its .data folder. TU: select a package/folder/XEXP, or leave blank for the original build.", style="Small.TLabel", wraplength=880).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        actions = ttk.Frame(prepare_tab)
        actions.pack(fill="x", pady=14)
        self._button(actions, "Inspect game", self._inspect).pack(side="left", padx=(0, 8))
        self._button(actions, "Prepare copy", self._prepare).pack(side="left", padx=(0, 8))
        self._button(actions, "Open existing project…", self._open_project).pack(side="left")
        self._button(actions, "Extract ISO / GOD only…", self._extract_only).pack(side="left", padx=(8, 0))
        utilities = ttk.Frame(prepare_tab)
        utilities.pack(fill="x", pady=(0, 5))
        self._button(utilities, "Inspect TU only…", self._inspect_tu).pack(side="left", padx=(0, 8))
        self._button(utilities, "Extract TU only…", self._extract_tu).pack(side="left")
        ttk.Label(utilities, text="These work with the small TU file alone; no full game copy needed.", style="Small.TLabel").pack(side="left", padx=10)
        ttk.Label(prepare_tab, textvariable=self.summary, wraplength=940, justify="left").pack(anchor="w", pady=10)
        ttk.Label(prepare_tab, text="A title update is merged before scanning for an FPS patch. The project keeps original copies, update contents, hashes, and the tool log.", wraplength=900, style="Small.TLabel").pack(anchor="w", pady=4)

        top = ttk.Frame(self.patch_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Recipe").pack(side="left", padx=(0, 10))
        self.combo = ttk.Combobox(top, textvariable=self.profile_name, state="readonly", width=55)
        self.combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.combo.bind("<<ComboboxSelected>>", lambda _: self._invalidate())
        self._button(top, "Import JSON…", self._import_recipe).pack(side="left")
        for path in sorted((bundle_root() / "recipes").glob("*.json")):
            try:
                profile = read_recipe(path)
                self.profiles[profile.get("name", path.stem)] = path
            except (OSError, WorkbenchError):
                continue
        self.combo["values"] = list(self.profiles)
        if self.profiles:
            # Start with the user's target: Rainbow Six Vegas 2.
            selection = next((n for n in self.profiles if "Rainbow" in n), next(iter(self.profiles)))
            self.profile_name.set(selection)
        actions = ttk.Frame(self.patch_tab)
        actions.pack(fill="x", pady=12)
        self._button(actions, "Scan recipe", self._scan).pack(side="left", padx=(0, 8))
        self._button(actions, "Paste instructions…", self._paste).pack(side="left", padx=(0, 8))
        self._button(actions, "Compare patched XEX…", self._compare).pack(side="left")
        ttk.Label(self.patch_tab, textvariable=self.patch_status, wraplength=940).pack(anchor="w", pady=(0, 10))
        table_frame = ttk.Frame(self.patch_tab)
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_frame, columns=("offset", "before", "after", "label"), show="headings", height=7)
        for column, title, width in [("offset", "File offset", 100), ("before", "Original bytes", 250), ("after", "Replacement bytes", 250), ("label", "Edit", 170)]:
            self.table.heading(column, text=title)
            self.table.column(column, width=width, minwidth=70)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        footer = ttk.Frame(self.patch_tab)
        footer.pack(fill="x", pady=(12, 0))
        self.save_button = self._button(footer, "Save bound recipe…", self._save_recipe)
        self.save_button.pack(side="left", padx=(0, 8))
        self.export_button = self._button(footer, "Export patched copy…", self._export)
        self.export_button.pack(side="left")
        self.save_button.state(["disabled"])
        self.export_button.state(["disabled"])

        self._games_ui(games_tab)
        self.console_panel = ConsolePanel(self, console_tab)

        support = """Included in this version

• Rainbow Six Vegas 2: the two exact signatures and replacements you supplied,
  credited to Xbox360forever. Matches are checked on the prepared build.
• GOD input: select the header and retain all Data0000… files in its .data
  folder. The app extracts modules for preparation, then the full game on export.
• ISO input: bare/XISO and full XGD1/XGD2/XGD3 layouts. Extract-only mode
  works without XexTool; the FPS workflow still needs it.
• Neighborhood/XBDM: browse, pull, upload a new copy, verify, and launch.
  Use your console IP and existing XBDM setup. No extra Python packages.
• Games tab: scan your local XEX/GOD library or search the offline reference
  catalog. A reference listing is not an included console patch recipe.
• Pasted instructions: reads explicit 'change … to …' lines and long hex
  signatures. It never executes commands or guesses a limiter.
• Compare mode: normalizes a supplied patched XEX and builds a small recipe
  only when its Title ID, Media ID and version match the prepared copy.

Title-update support

The selected TU's executable patches are embedded with XexTool before any
FPS edits. Additional TU assets are retained. A matching signature proves the
byte location; full TU loading and the resulting FPS need a console test.
The app exports normal extracted games for ISO/GOD input. Console uploads and
launch happen only through their buttons; cached TU settings are not changed.

Requirements

Windows with Python 3.10+ (including Tk) and your own XexTool executable.
No third-party Python packages, automatic downloads or game files included.
Use a local NTFS project folder. Exports are separate candidates for testing.
"""
        ttk.Label(notes_tab, text=support, justify="left", wraplength=920, font=("Segoe UI", 10)).pack(anchor="w")
        links = ttk.Frame(notes_tab)
        links.pack(fill="x", pady=10)
        for caption, url in [("Creator's channel", "https://www.youtube.com/@xbox360forever3"),
                             ("Creator's tutorial", "https://www.youtube.com/watch?v=W26KD-mntEY"),
                             ("XexTool releases", "https://github.com/Team-Resurgent/XexTool/releases")]:
            self._button(links, caption, lambda u=url: webbrowser.open(u)).pack(side="left", padx=(0, 8))

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=(14, 6))
        ttk.Label(outer, textvariable=self.status, style="Small.TLabel").pack(anchor="w")
        self.log = scrolledtext.ScrolledText(outer, height=6, font=("Consolas", 9), state="disabled", background="#172336", foreground="#DCE6F4", relief="flat")
        self.log.pack(fill="x", pady=(8, 0))

    def _invalidate(self):
        self.recipe = None
        if hasattr(self, "table"):
            self.table.delete(*self.table.get_children())
            self.patch_status.set("Scan the selected recipe against the prepared build.")
            self.save_button.state(["disabled"])
            self.export_button.state(["disabled"])

    def _file(self, variable, title, types):
        path = filedialog.askopenfilename(title=title, filetypes=types)
        if path:
            variable.set(path)

    def _game_folder(self):
        path = filedialog.askdirectory(title="Choose the extracted game folder")
        if path:
            self.game.set(path)

    def _game_file(self):
        self._file(self.game, "Choose original default.xex", [("XEX executable", "*.xex"), ("All files", "*")])

    def _god_file(self):
        self._file(self.game, "Choose GOD header (not a Data0000 chunk; keep the matching .data folder)", [("GOD package header (often extensionless)", "*")])

    def _iso_file(self):
        self._file(self.game, "Choose Xbox 360 ISO / XISO", [("Xbox disc image", "*.iso *.xiso"), ("All files", "*")])

    def _extract_only(self):
        image = Path(self.game.get())
        parent = filedialog.askdirectory(title="Choose a parent for a new extracted game folder")
        if not parent:
            return
        target = Path(parent) / ("Extracted-game-" + datetime.now().strftime("%Y%m%d-%H%M%S"))

        def done(result):
            self.status.set("Extracted game: " + result["output"])
            if os.name == "nt":
                os.startfile(result["output"])
        self._run(lambda log: extract_disc_input(image, target, log), done)

    def _tu_source(self):
        path = self.tu.get().strip()
        if path and Path(path).is_file():
            return Path(path)
        selected = filedialog.askopenfilename(title="Choose the TU package from your console Cache folder", filetypes=[("TU package / XEXP (often extensionless)", "*")])
        if selected:
            self.tu.set(selected)
            return Path(selected)
        return None

    def _inspect_tu(self):
        source = self._tu_source()
        if source is None:
            return

        def done(result):
            dialog = tk.Toplevel(self.root)
            dialog.title("Title update contents — read-only")
            dialog.geometry("850x600")
            text = scrolledtext.ScrolledText(dialog, font=("Consolas", 10), wrap="word")
            text.pack(fill="both", expand=True, padx=14, pady=14)
            text.insert("end", json.dumps(result, indent=2))
            text.configure(state="disabled")
        self._run(lambda log: inspect_title_update(source), done)

    def _extract_tu(self):
        source = self._tu_source()
        if source is None:
            return
        parent = filedialog.askdirectory(title="Choose a parent for a new TU-only extraction")
        if not parent:
            return
        target = Path(parent) / ("TU-extracted-" + datetime.now().strftime("%Y%m%d-%H%M%S"))

        def done(result):
            self.status.set("TU contents extracted: " + result["output"])
            if os.name == "nt":
                os.startfile(result["output"])
        self._run(lambda log: extract_title_update(source, target), done)

    def _tu_file(self):
        self._file(self.tu, "Choose title-update package or default.xexp", [("All files (TU packages often have no extension)", "*"), ("XEXP", "*.xexp")])

    def _tu_folder(self):
        path = filedialog.askdirectory(title="Choose extracted TU folder (default.xexp in root)")
        if path:
            self.tu.set(path)

    def _tool_file(self):
        self._file(self.tool, "Choose XexTool executable", [("Executable", "*.exe"), ("All files", "*")])

    def _project_parent(self):
        parent = filedialog.askdirectory(title="Choose a parent outside the original game folder")
        if parent:
            self.project.set(str(Path(parent) / ("project-" + datetime.now().strftime("%Y%m%d-%H%M%S"))))

    def _append(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", str(message) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _run(self, action, on_success):
        if self.busy:
            return
        self.busy = True
        for button in self.buttons:
            button.state(["disabled"])
        self.combo.configure(state="disabled")
        self.progress.start(12)
        self.status.set("Working on copies…")

        def worker():
            try:
                result = action(lambda m: self.events.put(("log", m)))
                self.events.put(("done", (on_success, result)))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._append(value)
                    continue
                self.busy = False
                self.progress.stop()
                for button in self.buttons:
                    button.state(["!disabled"])
                self.combo.configure(state="readonly")
                if self.recipe is None:
                    self.save_button.state(["disabled"])
                    self.export_button.state(["disabled"])
                if kind == "error":
                    self.status.set("Stopped — " + value)
                    self._append("Stopped: " + value)
                    messagebox.showerror("Could not complete this step", value)
                else:
                    self.status.set("Step completed.")
                    callback, result = value
                    try:
                        callback(result)
                    except Exception as exc:
                        self._append(str(exc))
                        messagebox.showerror("Could not display result", str(exc))
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _inspect(self):
        game = Path(self.game.get())
        self._run(lambda log: inspect_game_input(game), self._show_info)

    def _show_info(self, info):
        if info["format"] == "GOD":
            self.summary.set(f"GOD — {info['title']}\nTitle ID  {info['title_id']}     Media ID  {info['media_id']}\n{len(info['parts'])} fragments; {info['files']} files; {info['extracted_bytes'] // (1024 * 1024)} MiB to extract on export.")
        elif info["format"] == "ISO":
            self.summary.set(f"ISO — {info['title']} ({info['layout']})\nTitle ID  {info['title_id']}     Media ID  {info['media_id']}\n{info['files']} files; {info['extracted_bytes'] // (1024 * 1024)} MiB to extract on export.")
        else:
            self.summary.set(f"Title ID  {info['title_id']}     Media ID  {info['media_id']}\nVersion  0x{info['version']}     Encryption  {info['encryption']}     Compression  {info['compression']}\nSHA-256  {info['sha256']}")

    def _prepare(self):
        game, tu, tool, project = self.game.get(), self.tu.get().strip(), self.tool.get(), self.project.get()
        self._invalidate()
        self._run(lambda log: prepare_game(Path(game), Path(tu) if tu else None, Path(tool), Path(project), log), self._prepared)

    def _prepared(self, report):
        info = report["prepared"]
        self.summary.set(f"Prepared Title ID  {info['title_id']}     Media ID  {info['media_id']}     Version  0x{info['version']}\nTitle update: {'embedded' if report['title_update'] else 'none selected'}\nSHA-256  {info['sha256']}")
        self.notebook.select(self.patch_tab)
        for name, path in self.profiles.items():
            profile = read_recipe(path)
            if info["title_id"] in profile.get("title_ids", []):
                self.profile_name.set(name)
                break
        self.patch_status.set("Prepared copy ready. Select a recipe or paste the creator's instructions.")

    def _open_project(self):
        path = filedialog.askdirectory(title="Choose a project containing project.json")
        if path:
            self.project.set(path)
            self._run(lambda log: load_project(Path(path))[0], self._prepared)

    def _import_recipe(self):
        path = filedialog.askopenfilename(title="Import a signature profile or bound recipe", filetypes=[("JSON", "*.json")])
        if path:
            try:
                self.imported_profile = read_recipe(Path(path))
                self.combo["values"] = [*self.profiles, "Imported JSON"]
                self.profile_name.set("Imported JSON")
                self._invalidate()
                self.patch_status.set(f"Imported: {self.imported_profile.get('name', 'Unnamed recipe')}")
            except (OSError, WorkbenchError) as exc:
                messagebox.showerror("Cannot import recipe", str(exc))

    def _scan(self):
        project = Path(self.project.get())
        selected = self.profile_name.get()
        imported = self.imported_profile
        self._invalidate()

        def action(log):
            _, data = load_project(project)
            profile = imported if selected == "Imported JSON" else read_recipe(self.profiles[selected])
            if not profile or profile.get("kind") == "unavailable":
                raise WorkbenchError((profile or {}).get("reason", "No patch recipe is available."))
            recipe = bind_signatures(data, profile) if profile.get("kind") == "signature_patch" else profile
            preview = recipe_preview(data, recipe)
            log(f"All {len(preview['edits'])} edit locations match this exact build.")
            return recipe, preview

        self._run(action, self._show_recipe)

    def _show_recipe(self, result):
        recipe, preview = result
        self.recipe = recipe
        self.table.delete(*self.table.get_children())
        for edit in preview["edits"]:
            self.table.insert("", "end", values=(edit["offset"], edit["expected"], edit["replacement"], edit["label"]))
        self.patch_status.set(f"{preview['name']} — {len(preview['edits'])} exact edits match. Console / TU runtime behavior remains untested.")
        self.save_button.state(["!disabled"])
        self.export_button.state(["!disabled"])

    def _paste(self):
        if self.busy:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Paste the creator's instructions")
        dialog.geometry("850x650")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Paste the full description with 'change … to …' edits and long hex signatures.", wraplength=800).pack(anchor="w", pady=(0, 8))
        name, source = tk.StringVar(value="Imported FPS patch"), tk.StringVar(value="https://www.youtube.com/@xbox360forever3")
        for caption, variable in [("Recipe name", name), ("Source / credit", source)]:
            ttk.Label(frame, text=caption).pack(anchor="w")
            ttk.Entry(frame, textvariable=variable).pack(fill="x", pady=(2, 8))
        text = scrolledtext.ScrolledText(frame, font=("Consolas", 10), wrap="word")
        text.pack(fill="both", expand=True)
        error = tk.StringVar()
        ttk.Label(frame, textvariable=error, wraplength=800, foreground="#AE332D").pack(anchor="w", pady=6)

        def scan():
            try:
                _, data = load_project(Path(self.project.get()))
                info = inspect_xex(data)
                profile = description_profile(text.get("1.0", "end"), info.title_id, name.get(), source.get())
                recipe = bind_signatures(data, profile)
                preview = recipe_preview(data, recipe)
                dialog.destroy()
                self._show_recipe((recipe, preview))
            except (OSError, WorkbenchError) as exc:
                error.set(str(exc))

        ttk.Button(frame, text="Find edits in prepared copy", command=scan).pack(anchor="e")

    def _compare(self):
        path = filedialog.askopenfilename(title="Choose a known patched XEX from the SAME title / Media ID / TU", filetypes=[("XEX", "*.xex")])
        if not path:
            return
        project, tool = Path(self.project.get()), Path(self.tool.get())
        self._invalidate()

        def action(log):
            recipe = recipe_from_reference(project, Path(path), tool, "Imported XEX differences", "User-selected patched reference: " + Path(path).name, log)
            _, data = load_project(project)
            return recipe, recipe_preview(data, recipe)

        self._run(action, self._show_recipe)

    def _save_recipe(self):
        if not self.recipe:
            return
        path = filedialog.asksaveasfilename(title="Save a reusable recipe under a new name", defaultextension=".json", initialfile="bound-fps-recipe.json", filetypes=[("JSON", "*.json")])
        if path:
            try:
                write_json(Path(path), self.recipe)
                self.status.set("Saved recipe: " + path)
            except (OSError, WorkbenchError) as exc:
                messagebox.showerror("Could not save recipe", str(exc))

    def _export(self):
        if not self.recipe:
            return
        parent = filedialog.askdirectory(title="Choose an export parent outside the preparation project")
        if not parent:
            return
        target = Path(parent) / ("FPS-candidate-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        project, recipe = Path(self.project.get()), self.recipe

        def done(receipt):
            self.status.set("Exported: " + str(target))
            self.console_panel.suggest_export(target, receipt)
            self._append("Read READ-BEFORE-TESTING.txt. This is a candidate, not a confirmed console-tested release.")
            if os.name == "nt":
                os.startfile(str(target / "game" if receipt.get("output_layout") == "complete_extracted_game" else target))

        self._run(lambda log: export_candidate(project, recipe, target, log), done)

    def _games_ui(self, parent):
        search = ttk.Frame(parent)
        search.pack(fill="x", pady=(0, 8))
        ttk.Label(search, text="Find game / Title ID").pack(side="left", padx=(0, 8))
        ttk.Entry(search, textvariable=self.catalog_query).pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(search, text="Show community references", variable=self.include_references,
                        command=self._filter_games).pack(side="left", padx=(12, 0))
        ready = sum(g["kind"] == "ready_recipe" for g in self.catalog)
        references = len(self.catalog) - ready
        ttk.Label(parent, text=f"{ready} included recipe(s) • {references} community reference builds. References can contain non-FPS fixes and require their own console recipe.", style="Small.TLabel", wraplength=930).pack(anchor="w", pady=(0, 7))
        self.catalog_table = ttk.Treeview(parent, columns=("title", "id", "status"), show="headings", height=5)
        for key, caption, width in [("title", "Game / build", 430), ("id", "Title ID", 110), ("status", "Availability", 320)]:
            self.catalog_table.heading(key, text=caption)
            self.catalog_table.column(key, width=width)
        self.catalog_table.pack(fill="both", expand=True)
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=7)
        self._button(actions, "Use included recipe", self._use_catalog_game).pack(side="left", padx=(0, 8))
        self._button(actions, "Open source", self._catalog_source).pack(side="left")
        ttk.Separator(parent).pack(fill="x", pady=5)
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=7)
        self._button(actions, "Scan my game folder…", self._scan_library).pack(side="left", padx=(0, 8))
        self._button(actions, "Use selected installation", self._use_installation).pack(side="left")
        self.inventory_status = tk.StringVar(value="Find extracted games and GOD headers on your PC. GOD sources stay untouched.")
        ttk.Label(parent, textvariable=self.inventory_status, style="Small.TLabel").pack(anchor="w", pady=(0, 5))
        self.install_table = ttk.Treeview(parent, columns=("title", "format", "media", "status"), show="headings", height=4)
        for key, caption, width in [("title", "Local game", 400), ("format", "Format", 75), ("media", "Media ID", 110), ("status", "Patch", 270)]:
            self.install_table.heading(key, text=caption)
            self.install_table.column(key, width=width)
        self.install_table.pack(fill="both", expand=True)
        self.install_table.bind("<Double-1>", lambda _: self._use_installation())
        self.catalog_query.trace_add("write", lambda *_: self._filter_games())
        self._filter_games()

    def _filter_games(self):
        self.catalog_rows = filter_catalog(self.catalog, self.catalog_query.get(), self.include_references.get())
        self.catalog_table.delete(*self.catalog_table.get_children())
        for index, row in enumerate(self.catalog_rows):
            self.catalog_table.insert("", "end", iid=str(index), values=(row["title"], row["title_id"], row["status"]))
        if self.catalog_rows:
            self.catalog_table.selection_set("0")

    def _use_catalog_game(self):
        selected = self.catalog_table.selection()
        if not selected or self.busy:
            return
        game = self.catalog_rows[int(selected[0])]
        if game["kind"] != "ready_recipe":
            messagebox.showinfo("Community reference", "This is a source reference, not an included console recipe. Import a documented recipe before applying any changes.")
            return
        for name, path in self.profiles.items():
            if path.name == game["recipe"]:
                self.profile_name.set(name)
                self._invalidate()
                self.notebook.select(self.prepare_tab)
                self.summary.set("Selected " + game["title"] + ". Choose its XEX/folder or GOD header.")
                break

    def _catalog_source(self):
        selected = self.catalog_table.selection()
        if selected and not self.busy:
            source = self.catalog_rows[int(selected[0])].get("source", "")
            if source.startswith("https://"):
                webbrowser.open(source)

    def _scan_library(self):
        folder = filedialog.askdirectory(title="Choose a folder containing your XEX / GOD installations")
        if not folder:
            return

        def done(result):
            self.local_games = result["games"]
            self.install_table.delete(*self.install_table.get_children())
            for index, row in enumerate(self.local_games):
                self.install_table.insert("", "end", iid=str(index), values=(row["title"], row["format"], row["media_id"], row["patch_status"]))
            self.inventory_status.set(f"Found {len(self.local_games)} installation(s); {len(result['failures'])} item(s) need attention. Double-click a game to select it.")
            for failure in result["failures"][:10]:
                self._append(failure["path"] + ": " + failure["error"])

        self._run(lambda log: find_installations(Path(folder), self.catalog, log), done)

    def _use_installation(self):
        selected = self.install_table.selection()
        if selected and not self.busy:
            row = self.local_games[int(selected[0])]
            self.game.set(row["path"])
            self.summary.set(f"Selected {row['title']} ({row['format']}); Media ID {row['media_id']}. Choose its matching TU if needed.")
            self.notebook.select(self.prepare_tab)

    def _close(self):
        if self.busy:
            messagebox.showinfo("Step still running", "Let the current step finish before closing the workbench.")
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
