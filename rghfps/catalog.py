"""Local game inventory and an explicitly separate community reference list."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .formats import WorkbenchError, bounded_read, inspect_xex
from .god import GodContainer, is_god_header
from .iso import IsoImage
from .patches import read_recipe


def load_catalog(bundle: Path) -> list[dict]:
    records = []
    path = bundle / "catalog" / "community-games.json"
    if path.is_file():
        value = json.loads(bounded_read(path, 2 * 1024 * 1024).decode("utf-8"))
        records.extend(value.get("games", []))
    for path in sorted((bundle / "recipes").glob("*.json")):
        recipe = read_recipe(path)
        if recipe.get("kind") not in ("signature_patch", "bound_patch"):
            continue
        ids = recipe.get("title_ids") or [recipe.get("target", {}).get("title_id")]
        for title in ids:
            if title:
                records.insert(0, {"title": recipe.get("game_title", recipe.get("name", path.stem)),
                                   "title_id": title, "kind": "ready_recipe", "recipe": path.name,
                                   "status": "Recipe included; console test pending",
                                   "source": recipe.get("source", "")})
    return records


def filter_catalog(records: list[dict], query: str = "", include_references=True) -> list[dict]:
    query = query.casefold().strip()
    return [r for r in records if (include_references or r["kind"] == "ready_recipe")
            and (not query or query in (r["title"] + " " + r["title_id"]).casefold())]


def find_installations(root: Path, records: list[dict], log=lambda _: None) -> dict:
    root = Path(root).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise WorkbenchError("Choose a regular folder containing your games.")
    names, ready = {}, set()
    for entry in records:
        names.setdefault(entry["title_id"], entry["title"])
        if entry["kind"] == "ready_recipe":
            ready.add(entry["title_id"])
            names[entry["title_id"]] = entry["title"]
    results, failures = [], []
    visited = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [n for n in directories if not n.casefold().endswith(".data")
                           and not (Path(current) / n).is_symlink()]
        for filename in files:
            visited += 1
            if visited > 200000:
                raise WorkbenchError("Scan exceeds 200,000 files. Choose a smaller game library folder.")
            path = Path(current) / filename
            if path.is_symlink():
                continue
            try:
                if filename.casefold() == "default.xex":
                    info = inspect_xex(bounded_read(path))
                    row = {"title_id": info.title_id, "media_id": info.media_id,
                           "version": info.version, "format": "XEX", "path": str(path.parent.resolve())}
                elif path.suffix.casefold() in (".iso", ".xiso"):
                    with IsoImage(path) as image:
                        meta = image.metadata()
                    row = {"title_id": meta["title_id"], "media_id": meta["media_id"],
                           "version": meta["version"], "format": "ISO", "path": str(path.resolve()),
                           "package_title": meta["title"]}
                elif path.stat().st_size <= 1024 * 1024 and is_god_header(path):
                    with GodContainer(path) as container:
                        meta = container.metadata()
                    row = {"title_id": meta["title_id"], "media_id": meta["media_id"],
                           "version": meta["version"], "format": "GOD", "path": str(path.resolve()),
                           "package_title": meta["title"]}
                else:
                    continue
                row["title"] = names.get(row["title_id"], row.get("package_title", filename))
                row["patch_status"] = "Recipe included; scan required" if row["title_id"] in ready else "Import a recipe"
                results.append(row)
                log(f"Found {row['title']} ({row['format']}, {row['title_id']}).")
            except (OSError, WorkbenchError) as exc:
                if filename.casefold() == "default.xex" or path.suffix.casefold() in (".iso", ".xiso") or not path.suffix:
                    failures.append({"path": str(path), "error": str(exc)})
    return {"games": results, "failures": failures, "scanned_files": visited}
