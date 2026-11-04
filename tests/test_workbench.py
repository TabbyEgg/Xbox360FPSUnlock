from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from rghfps.formats import StfsPackage, WorkbenchError, inspect_xex
from rghfps.patches import (apply_to_file, bind_signatures, description_profile,
                            diff_recipe, format_hex, patched_bytes, read_recipe,
                            recipe_preview)
from rghfps.workflow import (export_candidate, load_project, prepare_game,
                             recipe_from_reference)
from tests.fixtures import EXEC, FORMAT, HEADER, make_xex, stfs_fixture

ROOT = Path(__file__).resolve().parent.parent
RSV2 = read_recipe(ROOT / "recipes" / "rainbow-six-vegas-2.json")
NEEDLES = [bytes.fromhex(s["pattern"]) for s in RSV2["signatures"]]


def game_data(title=0x55530807, version=0x100, duplicate=False, encrypted=0):
    payload = bytearray(b"\xCC" * 0x3000)
    payload[0x120:0x120 + len(NEEDLES[0])] = NEEDLES[0]
    payload[0x168:0x168 + len(NEEDLES[1])] = NEEDLES[1]
    if duplicate:
        payload[0x220:0x220 + len(NEEDLES[0])] = NEEDLES[0]
    return make_xex(title=title, version=version, payload=payload, encrypted=encrypted)


def fake_runner(tool, args, log):
    source = Path(args[-1])
    data = bytearray(source.read_bytes())
    if "-p" in args:
        patch = inspect_xex(Path(args[args.index("-p") + 1]).read_bytes())
        current = inspect_xex(data)
        if current.version is not None and current.version != patch.patch_source_version:
            raise WorkbenchError("Synthetic delta source mismatch")
        if current.version is not None:
            struct.pack_into(">I", data, EXEC + 4, int(patch.patch_target_version, 16))
    if "-e" in args:
        struct.pack_into(">HH", data, FORMAT + 4, 0, 0)
    output = Path(args[args.index("-o") + 1])
    output.write_bytes(data)
    log("Synthetic XexTool fixture completed (not a real crypto/delta test).")


class FormatTests(unittest.TestCase):
    def test_metadata_and_patch_versions(self):
        info = inspect_xex(game_data())
        self.assertEqual((info.title_id, info.media_id, info.version), ("55530807", "12345678", "00000100"))
        patch = inspect_xex(make_xex(version=0x200, source_version=0x100, compression=3))
        self.assertEqual((patch.patch_source_version, patch.patch_target_version), ("00000100", "00000200"))

    def test_truncated_header_and_duplicate_options_rejected(self):
        for data in [b"", b"XEX2", game_data()[:64]]:
            with self.subTest(size=len(data)), self.assertRaises(WorkbenchError):
                inspect_xex(data)
        data = bytearray(game_data())
        data[32:36] = data[24:28]
        with self.assertRaisesRegex(WorkbenchError, "Duplicate"):
            inspect_xex(data)

    def test_header_pointer_outside_headers_rejected(self):
        data = bytearray(game_data())
        struct.pack_into(">I", data, 36, len(data) - 8)
        with self.assertRaisesRegex(WorkbenchError, "execution"):
            inspect_xex(data)

    def test_encrypted_input_not_patchable(self):
        with self.assertRaisesRegex(WorkbenchError, "decrypted"):
            bind_signatures(game_data(encrypted=1), RSV2)


class SignatureTests(unittest.TestCase):
    def test_both_user_edits_found_before_any_replacement(self):
        data = game_data()
        recipe = bind_signatures(data, RSV2)
        result = patched_bytes(data, recipe)
        self.assertEqual(result[HEADER + 0x120:HEADER + 0x124], bytes.fromhex("39 40 00 01"))
        self.assertEqual(result[HEADER + 0x168:HEADER + 0x16C], bytes.fromhex("39 40 00 00"))
        expected_changed = {HEADER + 0x120 + i for i in range(4) if data[HEADER + 0x120 + i] != result[HEADER + 0x120 + i]}
        expected_changed |= {HEADER + 0x168 + i for i in range(4) if data[HEADER + 0x168 + i] != result[HEADER + 0x168 + i]}
        self.assertEqual({i for i, (a, b) in enumerate(zip(data, result)) if a != b}, expected_changed)
        self.assertEqual(data, game_data())

    def test_signatures_follow_shifted_locations(self):
        data = make_xex(payload=b"\xCC" * 0x900 + NEEDLES[0] + b"\xCC" * 0x800 + NEEDLES[1])
        recipe = bind_signatures(data, RSV2)
        self.assertEqual(recipe["edits"][0]["offset"], hex(HEADER + 0x900).upper().replace("0X", "0x"))

    def test_duplicate_missing_and_wrong_game_stop(self):
        for data, message in [(game_data(duplicate=True), "more than once"),
                              (make_xex(), "not found"), (game_data(title=0x5454081A), "Title ID")]:
            with self.subTest(message=message), self.assertRaisesRegex(WorkbenchError, message):
                bind_signatures(data, RSV2)

    def test_bound_hash_version_and_bytes_all_checked(self):
        original = game_data()
        recipe = bind_signatures(original, RSV2)
        altered = bytearray(original)
        altered[-1] ^= 1
        with self.assertRaisesRegex(WorkbenchError, "sha256"):
            patched_bytes(altered, recipe)
        altered_recipe = deepcopy(recipe)
        altered_recipe["target"]["version"] = "00000200"
        with self.assertRaisesRegex(WorkbenchError, "version"):
            patched_bytes(original, altered_recipe)
        altered_recipe = deepcopy(recipe)
        altered_recipe["edits"][1]["expected"] = "DE AD BE EF"
        with self.assertRaisesRegex(WorkbenchError, "byte mismatch"):
            patched_bytes(original, altered_recipe)

    def test_overlap_and_header_edits_stop(self):
        data = game_data()
        recipe = bind_signatures(data, RSV2)
        overlap = deepcopy(recipe)
        overlap["edits"].append(deepcopy(overlap["edits"][0]))
        with self.assertRaisesRegex(WorkbenchError, "overlapping"):
            patched_bytes(data, overlap)
        header_edit = deepcopy(recipe)
        header_edit["edits"] = [{"offset": "0x0", "expected": "58 45 58 32", "replacement": "00 00 00 00"}]
        with self.assertRaisesRegex(WorkbenchError, "Header edits"):
            patched_bytes(data, header_edit)

    def test_description_parser_uses_both_long_signatures(self):
        text = ("44D444 (82451444 in absolute values) and change 81 5f 35 0c to 39 40 00 01\n"
                "44D48C and change 39 40 00 01 to 39 40 00 00\n\n"
                + "\n\n".join(format_hex(n) for n in NEEDLES))
        profile = description_profile(text, "55530807", "Pasted instructions", "Creator")
        recipe = bind_signatures(game_data(), profile)
        self.assertEqual(len(recipe["edits"]), 2)
        self.assertEqual(recipe["edits"][1]["replacement"], "39 40 00 00")

    def test_description_without_signature_is_not_a_recipe(self):
        with self.assertRaisesRegex(WorkbenchError, "long signature"):
            description_profile("change 81 5F 35 0C to 39 40 00 01", "55530807", "No signature")

    def test_full_description_still_parses_when_newlines_collapse(self):
        text = ("Search for these locations (CTRL+G): 44D444 (82451444 in absolute values) and change 81 5f 35 0c to 39 40 00 01 "
                "44D48C (8245148c in absolute values) and change 39 40 00 01 to 39 40 00 00 "
                "NOTE: if you don't find it, search for these values. The strings to change are the first: "
                + "  ".join(format_hex(n) for n in NEEDLES) + " 5. Save the file.")
        profile = description_profile(text, "55530807", "Collapsed description")
        recipe = bind_signatures(game_data(), profile)
        self.assertEqual(len(recipe["edits"]), 2)

    def test_no_partial_output_when_second_edit_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "original.xex", Path(directory) / "output.xex"
            data = game_data()
            source.write_bytes(data)
            recipe = bind_signatures(data, RSV2)
            recipe["edits"][1]["expected"] = "DE AD BE EF"
            with self.assertRaises(WorkbenchError):
                apply_to_file(source, recipe, output)
            self.assertFalse(output.exists())
            self.assertEqual(source.read_bytes(), data)

    def test_output_cannot_overwrite_original_or_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "original.xex", Path(directory) / "output.xex"
            data = game_data()
            source.write_bytes(data)
            output.write_bytes(b"keep")
            recipe = bind_signatures(data, RSV2)
            for path in (source, output):
                with self.assertRaises(WorkbenchError):
                    apply_to_file(source, recipe, path)
            self.assertEqual(output.read_bytes(), b"keep")

    def test_diff_round_trip_excludes_header_digests(self):
        before = game_data()
        expected = patched_bytes(before, bind_signatures(before, RSV2))
        changed_header = bytearray(expected)
        changed_header[0x220] = 12
        recipe = diff_recipe(before, changed_header, "Imported")
        self.assertTrue(recipe["provenance"]["header_differences_excluded"])
        self.assertEqual(patched_bytes(before, recipe), expected)
        with self.assertRaisesRegex(WorkbenchError, "version"):
            diff_recipe(before, game_data(version=0x200), "Wrong TU")


class StfsTests(unittest.TestCase):
    def test_extract_tu_package_and_resource(self):
        patch = make_xex(version=0x200, source_version=0x100, compression=3)
        package = StfsPackage(stfs_fixture([("default.xexp", patch), ("media.bin", b"update asset")]))
        self.assertEqual(package.metadata()["title_id"], "55530807")
        self.assertEqual(package.file_bytes(package.entries[0]), patch)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "extracted"
            package.extract(output)
            self.assertEqual((output / "media.bin").read_bytes(), b"update asset")

    def test_noncontiguous_chain_crosses_hash_table_boundary(self):
        content = b"A" * 4096 + b"B" * 500
        for copies, active in [(1, 0), (2, 0), (2, 1)]:
            with self.subTest(copies=copies, active=active):
                package = StfsPackage(stfs_fixture([("default.xexp", content)], copies=copies, active=active,
                                                   chains={"default.xexp": [1, 172]}, total=174))
                self.assertEqual(package.file_bytes(package.entries[0]), content)
                self.assertEqual(package.block_offset(170), 0xA000 + (170 + 3 * copies) * 4096)

    def test_level_two_hash_tree(self):
        # A real-sized synthetic package traverses both parent hash levels.
        content = b"A" * 4096 + b"Z" * 12
        package = StfsPackage(stfs_fixture([("default.xexp", content)], chains={"default.xexp": [1, 28900]}, total=28901))
        self.assertEqual(package.file_bytes(package.entries[0]), content)

    def test_corrupt_file_block_and_root_table_are_detected(self):
        content = stfs_fixture([("default.xexp", b"abc")])
        package = StfsPackage(content)
        changed = bytearray(content)
        changed[package.block_offset(1)] ^= 1
        package = StfsPackage(changed)
        with self.assertRaisesRegex(WorkbenchError, "integrity"):
            package.file_bytes(package.entries[0])
        changed = bytearray(content)
        changed[0xA000] ^= 1
        with self.assertRaisesRegex(WorkbenchError, "integrity"):
            StfsPackage(changed)

    def test_non_tu_and_unsafe_paths_rejected(self):
        package = bytearray(stfs_fixture([("default.xexp", b"abc")]))
        struct.pack_into(">I", package, 0x344, 0x7000)
        with self.assertRaisesRegex(WorkbenchError, "not marked"):
            StfsPackage(package)
        for name in ("../evil", "CON", "bad."):
            with self.subTest(name=name), self.assertRaisesRegex(WorkbenchError, "filename"):
                StfsPackage(stfs_fixture([(name, b"abc")]))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / "Original Game ü"
        self.game.mkdir()
        self.original = game_data(encrypted=1)
        (self.game / "default.xex").write_bytes(self.original)
        self.patch = make_xex(version=0x200, source_version=0x100, compression=3)
        self.tu = self.root / "TU package"
        self.tu.write_bytes(stfs_fixture([("default.xexp", self.patch), ("media.bin", b"TU asset")]))
        self.project = self.root / "Project"

    def prepare(self, **kwargs):
        return prepare_game(self.game, self.tu, self.root / "fake tool", self.project, runner=fake_runner, **kwargs)

    def test_full_prepare_scan_export_restore_bytes_untouched(self):
        report = self.prepare()
        self.assertEqual(report["prepared"]["version"], "00000200")
        self.assertTrue(report["title_update"]["complete_package_supplied"])
        _, data = load_project(self.project)
        recipe = bind_signatures(data, RSV2)
        output = self.root / "Export"
        receipt = export_candidate(self.project, recipe, output)
        self.assertEqual(receipt["state"], "candidate_exported")
        self.assertEqual((output / "game_overlay" / "media.bin").read_bytes(), b"TU asset")
        self.assertEqual((output / "game_overlay" / "default.xex").read_bytes(), patched_bytes(data, recipe))
        self.assertEqual((self.game / "default.xex").read_bytes(), self.original)
        self.assertEqual((self.project / "originals" / "default.xex").read_bytes(), self.original)
        self.assertFalse(receipt["console_tested"])
        self.assertEqual(receipt["output_sha256"], hashlib.sha256((output / "game_overlay" / "default.xex").read_bytes()).hexdigest())

    def test_tu_merge_occurs_before_normalization(self):
        calls = []
        def runner(tool, args, log):
            calls.append(args)
            fake_runner(tool, args, log)
        prepare_game(self.game, self.tu, Path("unused"), self.project, runner=runner)
        self.assertIn("-p", calls[0])
        self.assertIn("-u", calls[0])
        self.assertIn("-e", calls[1])
        self.assertTrue(calls[1][-1].endswith("0-merged.xex"))

    def test_wrong_tu_source_version_and_ids_stop_before_tool(self):
        self.tu.write_bytes(stfs_fixture([("default.xexp", make_xex(version=0x300, source_version=0x200, compression=3))]))
        def forbidden(*args):
            self.fail("Tool must not run for an incompatible TU")
        with self.assertRaisesRegex(WorkbenchError, "different original"):
            prepare_game(self.game, self.tu, Path("unused"), self.project, runner=forbidden)
        self.assertTrue((self.project / "failure.json").is_file())
        self.assertFalse((self.project / "project.json").exists())
        self.assertEqual((self.game / "default.xex").read_bytes(), self.original)

    def test_missing_tu_assets_reported_for_xexp_only(self):
        loose = self.root / "default.xexp"
        loose.write_bytes(self.patch)
        report = prepare_game(self.game, loose, Path("unused"), self.project, runner=fake_runner)
        self.assertFalse(report["title_update"]["complete_package_supplied"])

    def test_project_rejects_later_manual_modification(self):
        self.prepare()
        path = self.project / "prepared" / "default.xex"
        altered = bytearray(path.read_bytes())
        altered[-1] ^= 1
        path.write_bytes(altered)
        with self.assertRaisesRegex(WorkbenchError, "changed"):
            load_project(self.project)

    def test_auxiliary_modules_are_merged_and_exported(self):
        (self.game / "helper.dll").write_bytes(game_data())
        folder = self.root / "TU folder"
        folder.mkdir()
        (folder / "default.xexp").write_bytes(self.patch)
        (folder / "helper.dllp").write_bytes(self.patch)
        report = prepare_game(self.game, folder, Path("unused"), self.project, runner=fake_runner)
        self.assertEqual(len(report["modules"]), 2)
        self.assertEqual(inspect_xex((self.project / "prepared" / "helper.dll").read_bytes()).version, "00000200")

    def test_export_rejects_invalid_second_edit_without_creating_folder(self):
        self.prepare()
        _, data = load_project(self.project)
        recipe = bind_signatures(data, RSV2)
        recipe["edits"][1]["expected"] = "DE AD BE EF"
        output = self.root / "No export"
        with self.assertRaisesRegex(WorkbenchError, "mismatch"):
            export_candidate(self.project, recipe, output)
        self.assertFalse(output.exists())

    def test_reference_must_match_tu(self):
        self.prepare()
        reference = self.root / "TU0 patched.xex"
        data = game_data()
        reference.write_bytes(patched_bytes(data, bind_signatures(data, RSV2)))
        with self.assertRaisesRegex(WorkbenchError, "different version"):
            recipe_from_reference(self.project, reference, Path("unused"), "Imported", runner=fake_runner)

    def test_no_project_inside_original_game_folder(self):
        with self.assertRaisesRegex(WorkbenchError, "outside"):
            prepare_game(self.game, self.tu, Path("unused"), self.game / "project", runner=fake_runner)

    def test_real_subprocess_wrapper_and_cli(self):
        if sys.platform == "win32":
            self.skipTest("The synthetic executable shim here uses a Unix shebang")
        shim = self.root / "synthetic xextool with spaces"
        shim.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, {str(ROOT)!r})\nfrom tests.test_workbench import fake_runner\nfake_runner(Path(sys.argv[0]), sys.argv[1:], print)\n", encoding="utf-8")
        shim.chmod(0o700)
        prepare_game(self.game, self.tu, shim, self.project)
        cli = subprocess.run([sys.executable, "-m", "rghfps", "scan", "--project", str(self.project), "--recipe", str(ROOT / "recipes" / "rainbow-six-vegas-2.json")], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(len(json.loads(cli.stdout)["edits"]), 2)


if __name__ == "__main__":
    unittest.main()
