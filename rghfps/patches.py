"""Exact-byte recipes. Matching is not a claim of runtime compatibility."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
import os

from .formats import WorkbenchError, XexInfo, bounded_read, inspect_xex


def write_json(path: Path, value: dict, *, overwrite: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"), overwrite=overwrite)


def atomic_write(path: Path, data: bytes, *, overwrite: bool = False) -> None:
    path = Path(path)
    if path.is_symlink() or (path.exists() and not overwrite):
        raise WorkbenchError(f"Output already exists; choose a new name: {path}")
    if not path.parent.is_dir():
        raise WorkbenchError("The output parent folder does not exist.")
    fd, temporary = tempfile.mkstemp(prefix=".rghfps-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Use a no-overwrite hard link for publication. No existing file can be
        # replaced by a second process between the existence check and publish.
        if overwrite:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise WorkbenchError(f"Output already exists: {path}") from exc
            except OSError as exc:
                raise WorkbenchError("Output must be on a filesystem supporting hard links (for example NTFS).") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_recipe(path: Path) -> dict:
    try:
        value = json.loads(bounded_read(Path(path), 1024 * 1024).decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkbenchError(f"Recipe is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise WorkbenchError("Unsupported recipe format; expected schema 1.")
    return value


def hex_bytes(value: str, label: str = "hex bytes") -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f\s]+", value):
        raise WorkbenchError(f"Invalid {label}. Use byte pairs such as 60 00 00 00.")
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise WorkbenchError(f"Invalid {label}; each byte needs two hex digits.") from exc
    if not 1 <= len(result) <= 4096:
        raise WorkbenchError(f"{label.capitalize()} must contain 1 to 4096 bytes.")
    return result


def format_hex(value: bytes) -> str:
    return value.hex(" ").upper()


def _identity(info: XexInfo) -> dict:
    return {"title_id": info.title_id, "media_id": info.media_id,
            "version": info.version, "sha256": info.sha256}


def _normal_info(data: bytes) -> XexInfo:
    info = inspect_xex(data)
    if not info.normalized or info.module_flags & 0x70:
        raise WorkbenchError("Prepare a decrypted, uncompressed, complete XEX before making a recipe.")
    if any(value is None for value in (info.title_id, info.media_id, info.version)):
        raise WorkbenchError("FPS recipes require an executable with complete game identity metadata.")
    return info


def validate_recipe(data: bytes, recipe: dict) -> list[dict]:
    info = _normal_info(data)
    if recipe.get("schema") != 1 or recipe.get("kind") != "bound_patch":
        raise WorkbenchError("Bind a signature recipe to this prepared build first.")
    target = recipe.get("target")
    if not isinstance(target, dict):
        raise WorkbenchError("Recipe has no target build identity.")
    for field, actual in _identity(info).items():
        expected = target.get(field)
        if not isinstance(expected, str) or expected.casefold() != actual.casefold():
            raise WorkbenchError(f"Wrong build: recipe {field} does not match this XEX.")
    edits = recipe.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 512:
        raise WorkbenchError("Recipe needs 1 to 512 exact-byte edits.")
    checked = []
    total = 0
    for number, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            raise WorkbenchError(f"Edit {number} is not an object.")
        offset_value = edit.get("offset")
        if not isinstance(offset_value, (str, int)) or isinstance(offset_value, bool):
            raise WorkbenchError(f"Edit {number} has an invalid file offset.")
        try:
            offset = int(offset_value, 0) if isinstance(offset_value, str) else offset_value
        except ValueError as exc:
            raise WorkbenchError(f"Edit {number} offset must use decimal or 0x-prefixed hex.") from exc
        before, after = hex_bytes(edit.get("expected")), hex_bytes(edit.get("replacement"))
        if len(before) != len(after) or before == after:
            raise WorkbenchError(f"Edit {number} must replace bytes with different bytes of the same length.")
        if offset < info.header_size or offset + len(before) > len(data):
            raise WorkbenchError(f"Edit {number} is outside the XEX payload. Header edits are disabled.")
        actual = data[offset:offset + len(before)]
        if actual != before:
            raise WorkbenchError(f"Edit {number} byte mismatch at 0x{offset:X}: expected {format_hex(before)}, found {format_hex(actual)}.")
        checked.append({"offset": offset, "expected": before, "replacement": after,
                        "label": str(edit.get("label", f"Edit {number}"))[:200]})
        total += len(before)
    if total > 8192:
        raise WorkbenchError("Recipe exceeds the 8192-byte edit limit.")
    ordered = sorted(checked, key=lambda item: item["offset"])
    for left, right in zip(ordered, ordered[1:]):
        if left["offset"] + len(left["expected"]) > right["offset"]:
            raise WorkbenchError("Recipe contains overlapping edits.")
    return checked


def recipe_preview(data: bytes, recipe: dict) -> dict:
    checked = validate_recipe(data, recipe)
    return {"name": str(recipe.get("name", "Untitled patch"))[:200],
            "target": _identity(inspect_xex(data)),
            "hardware_tested": recipe.get("hardware_tested") is True,
            "compatibility": "Exact bytes match; console behavior needs testing",
            "edits": [{"offset": f"0x{e['offset']:X}", "expected": format_hex(e["expected"]),
                       "replacement": format_hex(e["replacement"]), "label": e["label"]} for e in checked]}


def patched_bytes(data: bytes, recipe: dict) -> bytes:
    checked = validate_recipe(data, recipe)  # All checks finish before any edit.
    result = bytearray(data)
    for edit in checked:
        start = edit["offset"]
        result[start:start + len(edit["replacement"])] = edit["replacement"]
    return bytes(result)


def new_recipe(data: bytes, name: str, edits: list[dict], source: str = "") -> dict:
    info = _normal_info(data)
    recipe = {"schema": 1, "kind": "bound_patch", "name": str(name)[:200],
              "target": _identity(info), "hardware_tested": False,
              "provenance": {"source": str(source)[:2048],
                             "note": "Bound to exact input bytes; no runtime validation implied."},
              "edits": edits}
    validate_recipe(data, recipe)
    return recipe


def bind_signatures(data: bytes, profile: dict) -> dict:
    info = _normal_info(data)
    if profile.get("schema") != 1 or profile.get("kind") != "signature_patch":
        raise WorkbenchError("Choose a schema-1 signature profile.")
    title_ids = profile.get("title_ids")
    if not isinstance(title_ids, list) or not title_ids or info.title_id not in title_ids:
        raise WorkbenchError("Signature profile is for a different Title ID.")
    signatures = profile.get("signatures")
    if not isinstance(signatures, list) or not 1 <= len(signatures) <= 32:
        raise WorkbenchError("Profile needs 1 to 32 signatures.")
    edits = []
    for index, spec in enumerate(signatures, 1):
        if not isinstance(spec, dict):
            raise WorkbenchError("Invalid signature entry.")
        needle = hex_bytes(spec.get("pattern"), "signature")
        if len(needle) < 16:
            raise WorkbenchError("Search signatures must contain at least 16 exact bytes.")
        found = data.find(needle, info.header_size)
        if found < 0:
            raise WorkbenchError(f"Signature {index} was not found. This TU/region needs a different recipe.")
        if data.find(needle, found + 1) != -1:
            raise WorkbenchError(f"Signature {index} occurs more than once. Refusing to guess a location.")
        relative = spec.get("write_offset", 0)
        if not isinstance(relative, int) or isinstance(relative, bool) or relative < 0:
            raise WorkbenchError("Invalid signature write offset.")
        expected = hex_bytes(spec.get("expected"))
        if relative + len(expected) > len(needle) or needle[relative:relative + len(expected)] != expected:
            raise WorkbenchError("Expected bytes must be contained in their full search signature.")
        edits.append({"offset": f"0x{found + relative:X}", "expected": format_hex(expected),
                      "replacement": spec.get("replacement"),
                      "label": str(spec.get("label", f"Signature {index}"))[:200]})
    recipe = new_recipe(data, profile.get("name", "Imported signature patch"), edits,
                        profile.get("source", ""))
    recipe["signature_profile"] = profile.get("id", "custom")
    recipe["provenance"]["note"] = "Exact signatures matched this build. This does not prove the patch works with its TU."
    return recipe


def description_profile(text: str, title_id: str, name: str, source: str = "") -> dict:
    """Parse plain text only: change AA BB CC DD to EE FF GG HH + long signatures.

    Never evaluate commands, infer bytes, or reuse numeric offsets from a video.
    """
    if len(text) > 100000 or not re.fullmatch(r"[0-9A-F]{8}", title_id):
        raise WorkbenchError("Description is too large or the prepared Title ID is invalid.")
    byte = r"[0-9A-Fa-f]{2}(?![0-9A-Za-z])"
    pair = byte + r"(?:[ \t]+" + byte + r")+"
    changes = re.findall(r"\bchange[ \t]+(" + pair + r")[ \t]+to[ \t]+(" + pair + r")", text, re.IGNORECASE)
    needles = []
    wrapped_lines = []

    def flush_wrapped():
        if wrapped_lines:
            value = b"".join(wrapped_lines)
            if len(value) >= 16:
                needles.append(value)
            wrapped_lines.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(pair, stripped):
            wrapped_lines.append(bytes.fromhex(stripped))
        else:
            flush_wrapped()
            for match in re.finditer(r"(?<![0-9A-Za-z])" + pair, line):
                value = bytes.fromhex(match.group())
                if len(value) >= 16:
                    needles.append(value)
    flush_wrapped()
    if not changes or len(changes) > 32:
        raise WorkbenchError("No supported edits found. Use lines like 'change 81 5F 35 0C to 39 40 00 01' and include a long signature for each edit.")
    parsed_changes = [(hex_bytes(before), hex_bytes(after)) for before, after in changes]
    specs = []
    for index, (before, after) in enumerate(parsed_changes, 1):
        if len(before) != len(after):
            raise WorkbenchError("Description contains a replacement with a different byte length.")
        matches = []
        for needle in needles:
            start = needle.find(before)
            while start >= 0:
                tail = needle[start:]
                # Wrapped descriptions may collapse two signature paragraphs
                # into a single run. Stop at the next edit's original bytes.
                for other, _ in parsed_changes:
                    boundary = tail.find(other, max(len(before), 1))
                    if boundary >= 16:
                        tail = tail[:boundary]
                if len(tail) >= 16:
                    matches.append(tail[:128])
                start = needle.find(before, start + 1)
        if len(matches) != 1:
            raise WorkbenchError(f"Edit {index} needs exactly one long signature starting with its original bytes.")
        specs.append({"label": f"Description edit {index}", "pattern": format_hex(matches[0]),
                      "expected": format_hex(before), "replacement": format_hex(after), "write_offset": 0})
    return {"schema": 1, "kind": "signature_patch", "id": "pasted-description",
            "name": str(name)[:200], "title_ids": [title_id], "source": str(source)[:2048],
            "hardware_tested": False, "signatures": specs}


def diff_recipe(before: bytes, after: bytes, name: str, source: str = "") -> dict:
    left, right = _normal_info(before), _normal_info(after)
    for field in ("title_id", "media_id", "version", "header_size", "image_base"):
        if getattr(left, field) != getattr(right, field):
            raise WorkbenchError(f"Original and patched copies have different {field}. Normalize the same build first.")
    if len(before) != len(after):
        raise WorkbenchError("Original and patched copies differ in size. They are not an equal-length hex patch pair.")
    if left.module_flags != right.module_flags:
        raise WorkbenchError("Original and patched copies have different module flags.")
    edits = []
    position = left.header_size
    while position < len(before):
        if before[position] == after[position]:
            position += 1
            continue
        start = position
        while position < len(before) and before[position] != after[position]:
            position += 1
        edits.append({"offset": f"0x{start:X}", "expected": format_hex(before[start:position]),
                      "replacement": format_hex(after[start:position]), "label": f"Difference {len(edits) + 1}"})
        if len(edits) > 512:
            raise WorkbenchError("Too many differences for a small exact-byte patch.")
    if not edits:
        raise WorkbenchError("These copies contain identical bytes.")
    recipe = new_recipe(before, name, edits, source)
    recipe["provenance"]["header_differences_excluded"] = before[:left.header_size] != after[:left.header_size]
    return recipe


def apply_to_file(source: Path, recipe: dict, output: Path) -> dict:
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve():
        raise WorkbenchError("Output must be a new file; in-place patching is disabled.")
    data = bounded_read(source)
    preview = recipe_preview(data, recipe)
    result = patched_bytes(data, recipe)
    atomic_write(output, result)
    preview["output_sha256"] = hashlib.sha256(result).hexdigest()
    return preview
