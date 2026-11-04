"""Work on copies; merge TU before looking for signatures; export candidates."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable

from . import __version__
from .formats import (MAX_FILE, StfsPackage, WorkbenchError, bounded_read,
                      inspect_xex, safe_component, safe_relative)
from .god import GodContainer, resolve_god
from .iso import IsoImage
from .patches import atomic_write, diff_recipe, patched_bytes, recipe_preview, write_json


Log = Callable[[str], None]
Runner = Callable[[Path, list[str], Log], None]


def tool_runner(tool: Path, arguments: list[str], log: Log) -> None:
    tool = tool.resolve()
    if not tool.is_file() or tool.is_symlink():
        raise WorkbenchError("Select your installed XexTool executable.")
    log("XexTool: " + " ".join(arguments))
    try:
        result = subprocess.run([str(tool), *arguments], cwd=str(tool.parent),
                                capture_output=True, text=True, errors="replace",
                                timeout=180, shell=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkbenchError(f"Could not complete XexTool: {exc}") from exc
    output = ((result.stdout or "") + (result.stderr or ""))[-64000:]
    if output.strip():
        log(output.strip())
    if result.returncode != 0:
        raise WorkbenchError(f"XexTool failed with exit code {result.returncode}. No candidate was exported.")


def _case_path(root: Path, relative: str) -> Path:
    current = root
    for part in safe_relative(relative).parts:
        if not current.is_dir():
            raise WorkbenchError(f"Missing original game module: {relative}")
        found = [p for p in current.iterdir() if p.name.casefold() == part.casefold()]
        if len(found) != 1 or found[0].is_symlink():
            raise WorkbenchError(f"Missing, linked, or ambiguous original game module: {relative}")
        current = found[0]
    return current


def game_executable(game: Path) -> tuple[Path, Path | None]:
    game = Path(game).expanduser()
    if game.is_symlink():
        raise WorkbenchError("Choose an original game file or folder rather than a link.")
    if game.is_dir():
        return _case_path(game.resolve(), "default.xex"), game.resolve()
    if not game.is_file() or game.suffix.casefold() != ".xex":
        raise WorkbenchError("Choose default.xex or an extracted game folder containing it.")
    return game.resolve(), None


def _snapshot_files(source: Path, destination: Path) -> None:
    """Copy a bounded, regular extracted TU tree with Windows-safe paths."""
    destination.mkdir(parents=True, exist_ok=False)
    seen = set()
    total = 0
    count = 0
    for root, directories, files in os.walk(source, followlinks=False):
        for name in directories + files:
            path = Path(root) / name
            if path.is_symlink():
                raise WorkbenchError("Extracted TU folders must not contain links.")
            relative = path.relative_to(source).as_posix()
            safe_relative(relative)
            folded = relative.casefold()
            if folded in seen:
                raise WorkbenchError("TU contains paths that collide on Windows.")
            seen.add(folded)
            if path.is_dir():
                (destination / relative).mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                content = bounded_read(path)
                total += len(content)
                count += 1
                if total > MAX_FILE or count > 8192:
                    raise WorkbenchError("Extracted TU exceeds the 512 MiB / 8192-file limit.")
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            else:
                raise WorkbenchError("TU contains a non-regular file.")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _output_path(root: Path, relative: str) -> Path:
    """Resolve existing output names as the console's case-insensitive FS does."""
    current = root
    for component in safe_relative(relative).parts:
        matches = [p for p in current.iterdir() if p.name.casefold() == component.casefold()] if current.is_dir() else []
        if len(matches) > 1:
            raise WorkbenchError("Output contains case-colliding paths.")
        current = matches[0] if matches else current / component
        if current.is_symlink():
            raise WorkbenchError("Output must not contain links.")
    return current


def _same_ids(left, right, label: str) -> None:
    for key in ("title_id", "media_id"):
        expected = getattr(left, key)
        actual = getattr(right, key)
        if expected is None or actual is None:
            continue
        # Some packages omit Media ID; XexTool still validates the delta digest.
        if key == "media_id" and actual == "00000000":
            continue
        if expected != actual:
            raise WorkbenchError(f"{label} {key.replace('_', ' ')} {actual} does not match game {expected}.")


def _copy_result(path: Path, expected_title: str | None, expected_media: str | None):
    info = inspect_xex(bounded_read(path))
    if ((expected_title is not None and info.title_id != expected_title)
            or (expected_media is not None and info.media_id != expected_media)):
        raise WorkbenchError("XexTool changed the game identity unexpectedly.")
    return info


def open_disc_input(game: Path):
    header = resolve_god(game)
    if header:
        return GodContainer(header)
    if Path(game).suffix.casefold() in (".iso", ".xiso"):
        return IsoImage(game)
    return None


def extract_disc_input(game: Path, destination: Path, log: Log = lambda _: None) -> dict:
    reader = open_disc_input(game)
    if reader is None:
        raise WorkbenchError("Select an Xbox ISO/XISO or GOD header for extraction.")
    with reader as container:
        destination = Path(destination).expanduser().resolve()
        if container.metadata()["format"] == "GOD" and _inside(destination, container.path.with_name(container.path.name + ".data")):
            raise WorkbenchError("Extract outside the source GOD .data folder.")
        metadata = container.metadata()
        result = container.extract(destination, log=log)
        return {"input": metadata, "output": str(destination), **result}


def inspect_game_input(game: Path) -> dict:
    reader = open_disc_input(game)
    if reader:
        with reader as container:
            return container.metadata()
    executable, _ = game_executable(game)
    return {"format": "XEX", **inspect_xex(bounded_read(executable)).to_dict()}


def prepare_game(game: Path, tu: Path | None, tool: Path, destination: Path,
                 log: Log = lambda _: None, runner: Runner = tool_runner) -> dict:
    reader = open_disc_input(Path(game))
    if reader is None:
        return _prepare_extracted(game, tu, tool, destination, log, runner)
    destination = Path(destination).expanduser().resolve()
    with reader as container:
        if destination.exists() or _inside(destination, container.path.parent):
            raise WorkbenchError("Choose a new project folder outside the original image installation.")
        if Path(tool).is_symlink() or (runner is tool_runner and not Path(tool).is_file()):
            raise WorkbenchError("Choose XexTool before preparing this image.")
        metadata = container.metadata()
        log(f"{metadata['format']} detected: {metadata['title']}.")
        with tempfile.TemporaryDirectory(prefix="rghfps-image-modules-") as temporary:
            extracted = Path(temporary) / "modules"
            container.extract(extracted, modules_only=True, log=log)
            original, _ = game_executable(extracted)
            _same_ids(inspect_xex(bounded_read(original)), container, "Image identity")
            report = _prepare_extracted(extracted, tu, tool, destination, log, runner)
    # Keep the source read-only; the complete game is streamed at export time.
    report["input"] = metadata
    report["note"] = "Image modules prepared. Export will extract the complete game; keep the original ISO or GOD installation available."
    write_json(destination / "project.json", report, overwrite=True)
    return report


def _prepare_extracted(game: Path, tu: Path | None, tool: Path, destination: Path,
                 log: Log = lambda _: None, runner: Runner = tool_runner) -> dict:
    """Create a project. On failure preserve a failure report, never an export."""
    executable, game_root = game_executable(Path(game))
    original = bounded_read(executable)
    original_info = inspect_xex(original)
    if original_info.module_flags & 0x70:
        raise WorkbenchError("Select the original complete executable, not an .xexp or patch module.")
    if any(value is None for value in (original_info.title_id, original_info.media_id, original_info.version)):
        raise WorkbenchError("The main game executable must include execution information.")
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise WorkbenchError("Project output already exists. Choose a new project folder.")
    if _inside(destination, executable.parent):
        raise WorkbenchError("Keep the project outside the original game's folder.")
    if tu and Path(tu).is_dir() and _inside(destination, Path(tu)):
        raise WorkbenchError("Keep the project outside the source TU folder.")
    tool = Path(tool).expanduser().resolve()
    if runner is tool_runner and not tool.is_file():
        raise WorkbenchError("Choose XexTool before preparing the executable.")
    destination.mkdir(parents=True, exist_ok=False)
    messages: list[str] = []

    def emit(message: str) -> None:
        messages.append(str(message))
        log(str(message))

    report = {"app_version": __version__, "state": "preparing",
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "source_name": executable.name, "original": original_info.to_dict(),
              "title_update": None, "modules": [], "resources": [],
              "console_tested": False}
    try:
        originals = destination / "originals"
        prepared = destination / "prepared"
        originals.mkdir()
        prepared.mkdir()
        (originals / "default.xex").write_bytes(original)
        emit(f"Original copied. Title {original_info.title_id}, Media {original_info.media_id}, version 0x{original_info.version}.")
        tu_files = None
        default_patch = None
        package_meta = None
        if tu:
            tu = Path(tu).expanduser()
            if tu.is_symlink():
                raise WorkbenchError("Choose a regular TU file or extracted folder, not a link.")
            tu_files = destination / "tu_extracted"
            if tu.is_dir():
                _snapshot_files(tu.resolve(), tu_files)
                emit("Copied the extracted title-update folder.")
            else:
                content = bounded_read(tu)
                if content[:4] in (b"LIVE", b"PIRS", b"CON "):
                    package = StfsPackage(content)
                    _same_ids(original_info, package, "Title update")
                    package_meta = package.metadata()
                    (originals / "title-update.stfs").write_bytes(content)
                    package.extract(tu_files)
                    emit(f"Extracted {package_meta['files']} TU files with STFS hash checks.")
                elif tu.suffix.casefold() == ".xexp":
                    tu_files.mkdir()
                    (tu_files / "default.xexp").write_bytes(content)
                    emit("Using the selected executable patch only; no additional TU assets were supplied.")
                else:
                    raise WorkbenchError("Choose a TU package, an extracted TU folder, or default.xexp.")
            all_files = sorted(p for p in tu_files.rglob("*") if p.is_file())
            defaults = [p for p in all_files if p.relative_to(tu_files).as_posix().casefold() == "default.xexp"]
            if len(defaults) != 1:
                raise WorkbenchError("TU must contain exactly one default.xexp in its root.")
            default_patch = defaults[0]
            patch_info = inspect_xex(bounded_read(default_patch))
            _same_ids(original_info, patch_info, "Executable patch")
            if patch_info.patch_source_version and patch_info.patch_source_version != original_info.version:
                raise WorkbenchError("TU patch expects a different original executable version. Start with a clean dump matching this TU.")
            report["title_update"] = {"kind": "embedded", "package": package_meta,
                                      "patch": patch_info.to_dict(),
                                      "complete_package_supplied": tu.is_dir() or package_meta is not None}
        modules = [("default.xex", originals / "default.xex", default_patch)]
        if tu_files:
            resources = destination / "tu_resources"
            resources.mkdir()
            for path in sorted(p for p in tu_files.rglob("*") if p.is_file()):
                relative = path.relative_to(tu_files).as_posix()
                lower = relative.casefold()
                if lower == "default.xexp":
                    continue
                if lower.endswith((".xexp", ".dllp")):
                    module_name = relative[:-1]
                    if not game_root:
                        raise WorkbenchError(f"TU also patches {module_name}. Select the complete extracted game folder.")
                    source = _case_path(game_root, module_name)
                    snapshot = originals / module_name
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    snapshot.write_bytes(bounded_read(source))
                    modules.append((module_name, snapshot, path))
                else:
                    if lower.endswith((".xex", ".dll")):
                        raise WorkbenchError("TU contains a full replacement executable. This release handles delta module updates only.")
                    target = resources / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bounded_read(path))
                    report["resources"].append(relative)
        intermediate = destination / "intermediate"
        intermediate.mkdir()
        for number, (module_name, source, patch) in enumerate(modules):
            base_info = inspect_xex(bounded_read(source))
            current = source
            target_version = None
            if patch:
                delta_info = inspect_xex(bounded_read(patch))
                _same_ids(base_info, delta_info, f"Patch for {module_name}")
                if (base_info.version is not None and delta_info.patch_source_version
                        and delta_info.patch_source_version != base_info.version):
                    raise WorkbenchError(f"TU source-version mismatch for {module_name}.")
                merged = intermediate / f"{number}-merged.xex"
                emit(f"Merging title update into {module_name}.")
                runner(tool, ["-p", str(patch), "-u", "-o", str(merged), str(source)], emit)
                merged_info = _copy_result(merged, base_info.title_id, base_info.media_id)
                target_version = delta_info.patch_target_version or delta_info.version
                if merged_info.version is not None and target_version is not None and merged_info.version != target_version:
                    raise WorkbenchError(f"Merged {module_name} did not reach the TU's target version.")
                current = merged
            output = prepared / module_name
            output.parent.mkdir(parents=True, exist_ok=True)
            emit(f"Decrypting and decompressing {module_name}.")
            runner(tool, ["-c", "u", "-e", "u", "-o", str(output), str(current)], emit)
            final_info = _copy_result(output, base_info.title_id, base_info.media_id)
            if not final_info.normalized or final_info.module_flags & 0x70:
                raise WorkbenchError("XexTool did not produce an uncompressed, unencrypted, complete XEX.")
            expected_version = (target_version or base_info.version) if base_info.version is not None else None
            if expected_version is not None and final_info.version != expected_version:
                raise WorkbenchError("Executable version changed unexpectedly during normalization.")
            report["modules"].append({"path": module_name, "sha256": final_info.sha256,
                                       "version": final_info.version})
        report["prepared"] = inspect_xex(bounded_read(prepared / "default.xex")).to_dict()
        report["state"] = "prepared"
        report["note"] = "Preparation completed. FPS behavior and full TU resource loading have not been tested on a console."
        write_json(destination / "project.json", report)
        emit("Prepared copy is ready for signature scanning. Originals were not changed.")
        return report
    except Exception as exc:
        report["state"] = "failed"
        report["error"] = str(exc)
        write_json(destination / "failure.json", report)
        raise
    finally:
        atomic_write(destination / "preparation.log", ("\n\n".join(messages) + "\n").encode("utf-8"))


def load_project(project: Path) -> tuple[dict, bytes]:
    project = Path(project).resolve()
    metadata = project / "project.json"
    try:
        report = json.loads(bounded_read(metadata, 2 * 1024 * 1024).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkbenchError("Choose a completed project folder containing project.json.") from exc
    if not isinstance(report, dict) or report.get("state") != "prepared":
        raise WorkbenchError("This project has not completed preparation.")
    data = bounded_read(project / "prepared" / "default.xex")
    if inspect_xex(data).sha256 != report.get("prepared", {}).get("sha256"):
        raise WorkbenchError("Prepared XEX changed after preparation. Create a fresh project.")
    for module in report.get("modules", []):
        relative = safe_relative(module["path"])
        content = bounded_read(project / "prepared" / Path(*relative.parts))
        if hashlib.sha256(content).hexdigest() != module["sha256"]:
            raise WorkbenchError(f"Prepared module changed: {module['path']}")
    return report, data


def export_candidate(project: Path, recipe: dict, destination: Path,
                     log: Log = lambda _: None) -> dict:
    project = Path(project).resolve()
    report, data = load_project(project)
    preview = recipe_preview(data, recipe)
    result = patched_bytes(data, recipe)
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or _inside(destination, project):
        raise WorkbenchError("Choose a new export folder outside the preparation project.")
    # All patch validations precede output creation. Export is never installed.
    destination.mkdir(parents=True, exist_ok=False)
    try:
        image_source = report.get("input", {})
        full_game = image_source.get("format") in ("GOD", "ISO")
        overlay = destination / ("game" if full_game else "game_overlay")
        file_hashes = {}
        if full_game:
            image_path = Path(image_source["header_path"] if image_source["format"] == "GOD" else image_source["image_path"])
            reader = open_disc_input(image_path)
            if reader is None:
                raise WorkbenchError("The original game image is no longer available.")
            with reader as container:
                if image_source["format"] == "GOD":
                    changed = container.header_sha256 != image_source.get("header_sha256") or container.metadata()["parts"] != image_source.get("parts")
                else:
                    changed = container.metadata()["source_fingerprint"] != image_source.get("source_fingerprint")
                if changed:
                    raise WorkbenchError("The original game image changed since preparation. Make a new project.")
                log(f"Extracting the complete {image_source['format']} game directly.")
                extraction = container.extract(overlay, log=log)
                file_hashes.update(extraction["files"])
            for module in report["modules"]:
                relative = Path(*safe_relative(module["path"]).parts)
                target = _output_path(overlay, module["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bounded_read(project / "prepared" / relative))
        else:
            shutil.copytree(project / "prepared", overlay)
        module_paths = {m["path"].casefold() for m in report["modules"]}
        resources = project / "tu_resources"
        if resources.exists():
            for path in resources.rglob("*"):
                if path.is_file():
                    target = _output_path(overlay, path.relative_to(resources).as_posix())
                    if path.relative_to(resources).as_posix().casefold() in module_paths:
                        raise WorkbenchError("A TU resource conflicts with a prepared module.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bounded_read(path))
        patched_target = _output_path(overlay, "default.xex")
        patched_target.write_bytes(result)
        final_hash = hashlib.sha256(bounded_read(patched_target)).hexdigest()
        if final_hash != hashlib.sha256(result).hexdigest():
            raise WorkbenchError("Export failed its output hash check.")
        write_json(destination / "applied-recipe.json", recipe)
        changed_paths = [m["path"] for m in report["modules"]] + report["resources"]
        for relative in changed_paths:
            target = _output_path(overlay, relative)
            canonical = target.relative_to(overlay).as_posix()
            for old in [key for key in file_hashes if key.casefold() == canonical.casefold()]:
                del file_hashes[old]
            file_hashes[canonical] = hashlib.sha256(bounded_read(target)).hexdigest()
        receipt = {"app_version": __version__, "state": "candidate_exported",
                   "console_tested": False, "patch": preview,
                   "original_sha256": report["original"]["sha256"],
                   "prepared_sha256": report["prepared"]["sha256"],
                   "output_sha256": final_hash, "title_update": report["title_update"],
                   "resources": report["resources"],
                   "output_layout": "complete_extracted_game" if full_game else "game_overlay",
                   "game_folder": overlay.name, "files": file_hashes}
        write_json(destination / "receipt.json", receipt)
        instructions = """RGH FPS Workbench — candidate for console testing

For ISO/GOD input, game/ is the complete extracted game with FPS edits applied.
For extracted/XEX input, game_overlay/ contains changed modules and TU assets;
copy that overlay onto a separate complete extracted game copy.
This tool does not modify or rebuild the original GOD package.

The original game and selected TU were never modified. The preparation project
contains the original module copies. To undo a test, restore those originals
and the normal matching TU setup, or return to the untouched game copy.

If this candidate embeds a TU, its executable already contains that update.
Do not manually apply the same .xexp again. This release does not configure
Aurora/DashLaunch, deploy files, or prove that a cached TU will be skipped.
Use a separate test copy and verify the launch/update setup on your console.

Some games resolve additional update assets through update: rather than the
game folder. Assets are retained, but their mount/loading behavior needs a
game-specific console test. Selecting only .xexp cannot supply those assets.

An exact signature/byte match proves the edit location, not the effect on
gameplay. Check game speed, missions, cutscenes, saves, and frame pacing. This
tool does not measure FPS and removing a cap cannot guarantee 60 FPS.

See receipt.json for version, hashes and the exact changes. Never treat an
untested candidate as a confirmed TU-compatible release.
"""
        atomic_write(destination / "READ-BEFORE-TESTING.txt", instructions.encode("utf-8"))
        log(f"Candidate exported: {destination}")
        return receipt
    except Exception:
        shutil.rmtree(destination)
        raise


def recipe_from_reference(project: Path, reference: Path, tool: Path, name: str,
                          source: str = "", log: Log = lambda _: None,
                          runner: Runner = tool_runner) -> dict:
    _, before = load_project(project)
    reference_bytes = bounded_read(reference)
    reference_info = inspect_xex(reference_bytes)
    before_info = inspect_xex(before)
    for field in ("title_id", "media_id", "version"):
        if getattr(reference_info, field) != getattr(before_info, field):
            raise WorkbenchError(f"Patched reference has a different {field}. A TU0 patch cannot be transferred to TU1 by comparing files.")
    with tempfile.TemporaryDirectory(prefix="rghfps-compare-") as temporary:
        temporary = Path(temporary)
        snapshot = temporary / "reference.xex"
        snapshot.write_bytes(reference_bytes)
        normalized = temporary / "normalized.xex"
        runner(Path(tool), ["-c", "u", "-e", "u", "-o", str(normalized), str(snapshot)], log)
        return diff_recipe(before, bounded_read(normalized), name, source)
