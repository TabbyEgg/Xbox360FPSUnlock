import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from rghfps.catalog import filter_catalog, find_installations, load_catalog
from rghfps.formats import WorkbenchError
from rghfps.god import GodContainer, resolve_god
from rghfps.patches import bind_signatures, patched_bytes
from rghfps.workflow import export_candidate, inspect_game_input, load_project, prepare_game
from tests.god_fixtures import make_god
from tests.test_workbench import ROOT, RSV2, fake_runner, game_data
from tests.fixtures import make_xex, stfs_fixture


class GodTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = game_data(encrypted=1)

    def test_standard_enhanced_and_embedded_layouts(self):
        for enhanced, embedded in [(False, False), (True, False), (True, True)]:
            with self.subTest(enhanced=enhanced, embedded=embedded):
                header = make_god(self.root / f"{enhanced}-{embedded}", {"default.xex": self.original, "media.bin": b"asset"}, enhanced=enhanced, embedded=embedded)
                with GodContainer(header) as container:
                    entry = next(e for e in container.entries if e.path == "default.xex")
                    self.assertEqual(container.read_file(entry), self.original)
                    output = self.root / f"extract-{enhanced}-{embedded}"
                    result = container.extract(output)
                    self.assertEqual((output / "media.bin").read_bytes(), b"asset")
                    self.assertEqual(result["files"]["default.xex"], hashlib.sha256(self.original).hexdigest())

    def test_fragment_boundary_and_partial_sector(self):
        asset = b"X" * 2048 + b"Y" * 73
        header = make_god(self.root / "cross", {"default.xex": self.original, "asset.bin": asset}, cross_part=True)
        with GodContainer(header) as container:
            self.assertEqual(container.part_count, 2)
            entry = next(e for e in container.entries if e.path == "asset.bin")
            self.assertEqual(container.read_file(entry), asset)
            self.assertEqual(container.sector_location(0x14388), (1, 0x2000))

    def test_missing_data_folder_or_part_is_actionable(self):
        header = make_god(self.root / "missing", {"default.xex": self.original})
        data = header.with_name(header.name + ".data")
        (data / "Data0000").unlink()
        with self.assertRaisesRegex(WorkbenchError, "missing or extra"):
            GodContainer(header)
        data.rmdir()
        with self.assertRaisesRegex(WorkbenchError, "small GOD header"):
            GodContainer(header)

    def test_header_master_and_data_corruption(self):
        for damage in ("header", "master", "data"):
            with self.subTest(damage=damage):
                header = make_god(self.root / damage, {"default.xex": self.original})
                file = header if damage == "header" else header.with_name(header.name + ".data") / "Data0000"
                offset = 0x360 if damage == "header" else 0 if damage == "master" else 0x2000 + 50 * 2048
                with file.open("r+b") as stream:
                    stream.seek(offset)
                    byte = stream.read(1)
                    stream.seek(offset)
                    stream.write(bytes([byte[0] ^ 1]))
                if damage == "data":
                    with GodContainer(header) as container:
                        with self.assertRaisesRegex(WorkbenchError, "data integrity"):
                            container.read_file(container.entries[0])
                else:
                    with self.assertRaises(WorkbenchError):
                        GodContainer(header)

    def test_prepare_god_and_export_complete_game(self):
        header = make_god(self.root / "original", {"Default.XEX": self.original, "media.bin": b"unchanged asset"})
        original_header = header.read_bytes()
        project = self.root / "project"
        report = prepare_game(header, None, Path("fake"), project, runner=fake_runner)
        self.assertEqual(report["input"]["format"], "GOD")
        self.assertFalse((project / "game_extracted").exists())
        _, data = load_project(project)
        recipe = bind_signatures(data, RSV2)
        output = self.root / "export"
        receipt = export_candidate(project, recipe, output)
        self.assertEqual(receipt["output_layout"], "complete_extracted_game")
        self.assertEqual((output / "game" / "Default.XEX").read_bytes(), patched_bytes(data, recipe))
        self.assertEqual((output / "game" / "media.bin").read_bytes(), b"unchanged asset")
        self.assertEqual(header.read_bytes(), original_header)
        self.assertEqual(len([p for p in (output / "game").iterdir() if p.name.casefold() == "default.xex"]), 1)
        with GodContainer(header) as container:
            self.assertEqual(container.read_file(container.entries[0]), self.original)

    def test_source_changes_block_full_export(self):
        header = make_god(self.root / "original", {"default.xex": self.original})
        project = self.root / "project"
        prepare_game(header, None, Path("fake"), project, runner=fake_runner)
        _, data = load_project(project)
        recipe = bind_signatures(data, RSV2)
        content = bytearray(header.read_bytes())
        content[0x1691] ^= 1
        content[0x32C:0x340] = hashlib.sha1(content[0x344:]).digest()
        header.write_bytes(content)
        output = self.root / "export"
        with self.assertRaisesRegex(WorkbenchError, "changed since"):
            export_candidate(project, recipe, output)
        self.assertFalse(output.exists())

    def test_god_path_resolution_and_inspection(self):
        header = make_god(self.root / "original", {"default.xex": self.original})
        self.assertEqual(resolve_god(header.parent), header)
        self.assertEqual(resolve_god(header.with_name(header.name + ".data")), header)
        self.assertEqual(inspect_game_input(header)["title_id"], "55530807")

    def test_nested_directories_empty_file_and_long_directory_table(self):
        files = {"default.xex": self.original, "maps/level/asset.bin": b"nested", "empty.bin": b""}
        files.update({f"file{i}.bin": bytes([i]) for i in range(12)})
        header = make_god(self.root / "nested", files)
        with GodContainer(header) as container:
            output = self.root / "nested-output"
            container.extract(output)
        self.assertEqual((output / "maps" / "level" / "asset.bin").read_bytes(), b"nested")
        self.assertEqual((output / "empty.bin").read_bytes(), b"")
        self.assertEqual((output / "file11.bin").read_bytes(), bytes([11]))

    def test_unsafe_directory_is_rejected(self):
        header = make_god(self.root / "unsafe", {"default.xex": self.original, "../escape.bin": b"no"})
        with self.assertRaisesRegex(WorkbenchError, "filename"):
            GodContainer(header)
        self.assertFalse((self.root / "escape.bin").exists())

    def test_god_with_tu_merges_code_and_overrides_asset_case_insensitively(self):
        header = make_god(self.root / "updated", {"default.xex": self.original, "Media.bin": b"old asset"})
        tu = self.root / "title-update"
        tu.write_bytes(stfs_fixture([("default.xexp", make_xex(version=0x200, source_version=0x100, compression=3)),
                                     ("media.bin", b"updated asset")]))
        project = self.root / "project"
        report = prepare_game(header, tu, Path("fake"), project, runner=fake_runner)
        self.assertEqual(report["prepared"]["version"], "00000200")
        _, data = load_project(project)
        output = self.root / "export"
        receipt = export_candidate(project, bind_signatures(data, RSV2), output)
        self.assertEqual((output / "game" / "Media.bin").read_bytes(), b"updated asset")
        self.assertFalse((output / "game" / "media.bin").exists())
        self.assertEqual(receipt["files"]["Media.bin"], hashlib.sha256(b"updated asset").hexdigest())


class CatalogTests(unittest.TestCase):
    def test_rsv2_is_the_ready_game_and_references_are_separate(self):
        entries = load_catalog(ROOT)
        ready = filter_catalog(entries, include_references=False)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["title_id"], "55530807")
        self.assertEqual(ready[0]["kind"], "ready_recipe")
        self.assertEqual(len(filter_catalog(entries, "rainbow six vegas 2")), 1)
        references = [r for r in entries if r["kind"] == "community_reference"]
        self.assertEqual(len(references), 501)
        self.assertTrue(all("Reference only" in r["status"] for r in references))

    def test_scan_local_god_and_extracted_games(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_god(root / "GOD", {"default.xex": game_data()})
            game = root / "extracted"
            game.mkdir()
            (game / "default.xex").write_bytes(game_data())
            result = find_installations(root, load_catalog(ROOT))
            self.assertEqual({r["format"] for r in result["games"]}, {"GOD", "XEX"})
            self.assertEqual(len(result["games"]), 2)
            self.assertTrue(all(r["title_id"] == "55530807" for r in result["games"]))


if __name__ == "__main__":
    unittest.main()
