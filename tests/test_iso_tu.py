from pathlib import Path
import tempfile
import unittest

from rghfps.catalog import find_installations, load_catalog
from rghfps.formats import WorkbenchError
from rghfps.iso import IsoImage
from rghfps.patches import bind_signatures
from rghfps.tu import extract_title_update, inspect_title_update
from rghfps.workflow import export_candidate, extract_disc_input, load_project, prepare_game
from tests.fixtures import make_xex, stfs_fixture
from tests.iso_fixtures import make_iso
from tests.test_workbench import ROOT, RSV2, fake_runner, game_data


class IsoTuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_all_supported_iso_offsets_and_standalone_extraction(self):
        data = game_data(encrypted=1)
        for layout, offset in IsoImage.OFFSETS:
            with self.subTest(layout=layout):
                source = make_iso(self.root / (layout.replace("/", "-") + ".iso"), data, prefix=offset)
                with IsoImage(source) as image:
                    self.assertEqual(image.title_id, "55530807")
                    self.assertEqual(image.partition, offset)
                output = self.root / (layout.replace("/", "-") + "-out")
                result = extract_disc_input(source, output)
                self.assertEqual((output / "default.xex").read_bytes(), data)
                self.assertEqual((output / "asset.bin").read_bytes(), b"disc asset")
                self.assertEqual(result["input"]["format"], "ISO")

    def test_iso_prepare_tu_export_without_altering_iso(self):
        source = make_iso(self.root / "input" / "game.iso", game_data(encrypted=1))
        before = source.read_bytes()
        tu = self.root / "TU"
        tu.write_bytes(stfs_fixture([("default.xexp", make_xex(version=0x200, source_version=0x100, compression=3))]))
        project = self.root / "project"
        report = prepare_game(source, tu, Path("fake"), project, runner=fake_runner)
        self.assertEqual(report["prepared"]["version"], "00000200")
        _, data = load_project(project)
        receipt = export_candidate(project, bind_signatures(data, RSV2), self.root / "export")
        self.assertEqual(receipt["output_layout"], "complete_extracted_game")
        self.assertEqual((self.root / "export" / "game" / "asset.bin").read_bytes(), b"disc asset")
        self.assertEqual(source.read_bytes(), before)

    def test_iso_source_change_detected_and_bad_image_rejected(self):
        source = make_iso(self.root / "input" / "game.iso", game_data())
        project = self.root / "project"
        prepare_game(source, None, Path("fake"), project, runner=fake_runner)
        _, data = load_project(project)
        with source.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(WorkbenchError, "changed since"):
            export_candidate(project, bind_signatures(data, RSV2), self.root / "export")
        self.assertFalse((self.root / "export").exists())
        bad = self.root / "bad.iso"
        bad.write_bytes(b"not an ISO")
        with self.assertRaisesRegex(WorkbenchError, "No supported"):
            IsoImage(bad)

    def test_iso_unsafe_path_rejected_and_inventory_detects_iso(self):
        bad = make_iso(self.root / "bad.iso", game_data(), bad_name=True)
        with self.assertRaisesRegex(WorkbenchError, "filename"):
            IsoImage(bad)
        good = make_iso(self.root / "good.iso", game_data())
        result = find_installations(self.root, load_catalog(ROOT))
        self.assertEqual(len(result["games"]), 1)
        self.assertEqual(result["games"][0]["format"], "ISO")
        self.assertEqual(len(result["failures"]), 1)

    def test_inspect_and_extract_tu_without_base_game(self):
        tu = self.root / "TU"
        delta = make_xex(version=0x200, source_version=0x100, compression=3)
        tu.write_bytes(stfs_fixture([("default.xexp", delta), ("asset.bin", b"TU asset")]))
        result = inspect_title_update(tu)
        self.assertEqual(result["format"], "STFS title update")
        exe = next(f for f in result["files"] if f["path"] == "default.xexp")["executable"]
        self.assertEqual(exe["kind"], "delta patch")
        self.assertEqual(exe["patch_target_version"], "00000200")
        self.assertFalse(result["repacking_supported"])
        extract_title_update(tu, self.root / "contents")
        self.assertEqual((self.root / "contents" / "default.xexp").read_bytes(), delta)

    def test_delta_without_execution_metadata_can_be_inspected_and_merged(self):
        delta = make_xex(version=0x200, source_version=0x100,
                         compression=3, execution_info=False)
        tu = self.root / "TU"
        tu.write_bytes(stfs_fixture([("default.xexp", delta)]))
        result = inspect_title_update(tu)
        patch = result["files"][0]["executable"]
        self.assertIsNone(patch["title_id"])
        self.assertIsNone(patch["version"])
        self.assertEqual(patch["patch_source_version"], "00000100")
        game = self.root / "original" / "default.xex"
        game.parent.mkdir()
        game.write_bytes(game_data(encrypted=1))
        report = prepare_game(game, tu, Path("fake"), self.root / "project", runner=fake_runner)
        self.assertEqual(report["prepared"]["title_id"], "55530807")
        self.assertEqual(report["prepared"]["version"], "00000200")

    def test_missing_main_execution_metadata_still_rejected(self):
        from rghfps.formats import inspect_xex
        with self.assertRaisesRegex(WorkbenchError, "missing execution"):
            inspect_xex(make_xex(execution_info=False))
        info = inspect_xex(make_xex(execution_info=False, library=True))
        self.assertIsNone(info.title_id)
        with self.assertRaisesRegex(WorkbenchError, "identity metadata"):
            bind_signatures(make_xex(execution_info=False, library=True), RSV2)

    def test_auxiliary_library_without_execution_metadata_still_merges(self):
        game = self.root / "original"
        game.mkdir()
        (game / "default.xex").write_bytes(game_data(encrypted=1))
        (game / "support.dll").write_bytes(make_xex(execution_info=False, library=True, encrypted=1))
        tu = self.root / "TU"
        delta = make_xex(version=0x200, source_version=0x100, compression=3, execution_info=False)
        tu.write_bytes(stfs_fixture([("default.xexp", delta), ("support.dllp", delta)]))
        report = prepare_game(game, tu, Path("fake"), self.root / "project", runner=fake_runner)
        self.assertEqual(report["state"], "prepared")
        library = next(m for m in report["modules"] if m["path"] == "support.dll")
        self.assertIsNone(library["version"])


if __name__ == "__main__":
    unittest.main()
