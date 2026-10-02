import pathlib
import os
import subprocess
import tempfile
import unittest
from datetime import datetime

from zmk_support import (ZMK_VERSION_UNKNOWN, artifact_name, copy_uf2,
                         detect_zmk_version, discover_zmk_project,
                         select_destination, verify_copy)


class ZmkVersionDetectionTests(unittest.TestCase):
    def detect(self, manifest):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "config").mkdir()
            (root / "config" / "west.yml").write_text(manifest)
            sources = {"config/west.yml": (root / "config" / "west.yml").read_text()}
            return detect_zmk_version(sources)

    def test_detects_v03_from_zmk_project_revision(self):
        result = self.detect("""manifest:
  projects:
    - name: zmk
      revision: v0.3-branch+dya
    - name: unrelated-module
      revision: v0.4.2
""")
        self.assertEqual(result.version, "v0.3")
        self.assertEqual(result.revision, "v0.3-branch+dya")
        self.assertEqual(result.source, "config/west.yml")

    def test_detects_v04_from_zmk_project_revision(self):
        result = self.detect("""manifest:
  projects:
    - name: zmk
      revision: v0.4.0
""")
        self.assertEqual(result.version, "v0.4")

    def test_unqualified_branch_or_commit_is_explicitly_unknown(self):
        for revision in ("main", "641514a97db345f499dd50b0360e594270f008fe"):
            with self.subTest(revision=revision):
                result = self.detect(f"""manifest:
  projects:
    - name: zmk
      revision: {revision}
""")
                self.assertEqual(result.version, ZMK_VERSION_UNKNOWN)

    def test_unrelated_project_revision_does_not_set_zmk_version(self):
        result = self.detect("""manifest:
  projects:
    - name: unrelated-module
      revision: v0.4.0
""")
        self.assertEqual(result.version, ZMK_VERSION_UNKNOWN)

    def test_conflicting_supported_releases_are_unknown(self):
        result = detect_zmk_version({
            "config/west.yml": "manifest:\n  projects:\n    - name: zmk\n      revision: v0.3\n",
            "west.yml": "manifest:\n  projects:\n    - name: zmk\n      revision: v0.4.0\n",
        })
        self.assertEqual(result.version, ZMK_VERSION_UNKNOWN)
        self.assertIn("conflicting", result.reason)

    def test_patch_variants_within_same_supported_minor_select_same_environment(self):
        result = detect_zmk_version({
            "config/west.yml": "manifest:\n  projects:\n    - name: zmk\n      revision: v0.4.0\n",
            "west.yml": "manifest:\n  projects:\n    - name: zmk\n      revision: v0.4.1\n",
        })
        self.assertEqual(result.version, "v0.4")

    def test_installed_cli_includes_zmk_support_module(self):
        repository = pathlib.Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            environment = dict(os.environ, HOME=str(home))
            result = subprocess.run(
                ["/bin/zsh", str(repository / "install.sh")],
                cwd=repository,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            installed_cli = home / ".local" / "share" / "local-engineer" / "local_engineer.py"
            smoke = subprocess.run(
                ["python3", str(installed_cli), "projects"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            self.assertEqual(smoke.returncode, 0, smoke.stdout)

    def test_discovers_zmk_v03_targets_and_keyboard_name(self):
        info = discover_zmk_project({
            "config/west.yml": "manifest:\n  projects:\n    - name: zmk\n      revision: v0.3.0\n",
            "build.yaml": "include:\n  - board: nice_nano_v2\n    shield: koZakura48_L rgbled_adapter\n",
        }, "/work/zmk-config-koZakura")
        self.assertTrue(info["is_zmk"])
        self.assertEqual(info["version"], "v0.3")
        self.assertEqual(info["keyboard"], "koZakura48")
        self.assertEqual(info["targets"], [{"board": "nice_nano_v2", "shield": "koZakura48_L rgbled_adapter"}])

    def test_discovers_zmk_v04_and_multiple_targets(self):
        info = discover_zmk_project({
            "west.yml": "manifest:\n  projects:\n    - name: zmk\n      revision: v0.4.0\n",
            "build.yaml": "include:\n  - board: board_a\n    shield: alpha_left\n  - board: board_b\n    shield: beta_right\n    artifact-name: beta-r\n",
        }, "/work/keyboard")
        self.assertEqual(info["version"], "v0.4")
        self.assertEqual(len(info["targets"]), 2)
        self.assertEqual(info["targets"][1]["artifact-name"], "beta-r")

    def test_discovers_standard_top_level_board_and_shield_matrices(self):
        info = discover_zmk_project({
            "west.yml": "manifest:\n  projects:\n    - name: zmk\n      revision: v0.3.0\n",
            "build.yaml": "board: [ board_a, board_b ]\nshield: [ my_keyboard_left, my_keyboard_right ]\n",
        }, "/work/project")
        self.assertEqual(len(info["targets"]), 4)
        self.assertEqual(info["keyboard"], "my_keyboard")
        self.assertEqual(info["targets"][0], {"board": "board_a", "shield": "my_keyboard_left"})

    def test_artifact_name_and_version_destination(self):
        stamp = datetime(2026, 10, 2, 7, 8, 9)
        self.assertEqual(artifact_name("Kbd", "Peripheral", stamp, "v0.3"),
                         "Kbd-Peripheral-20261002070809-v0.3.uf2")
        self.assertEqual(select_destination("v0.4", "my-project"),
                         "/mnt/d/ZMK-Firmware/zmk-dev/v0.4/my-project")
        self.assertEqual(select_destination("v0.3", "my-project"),
                         "/mnt/d/ZMK-Firmware/zmk-dev/v0.3/my-project")
        with self.assertRaises(ValueError):
            select_destination("v0.4", "../escape")

    def test_copy_uf2_and_verify_destination_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "build" / "zephyr" / "zmk.uf2"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"firmware-image")
            destination = root / "release"
            result = copy_uf2(str(source), str(destination), "kbd-Central-20261002070809-v0.4.uf2")
            self.assertTrue(result["verified"])
            self.assertEqual(result["size"], len(b"firmware-image"))
            self.assertEqual(pathlib.Path(result["destination"]).read_bytes(), source.read_bytes())
            self.assertEqual(verify_copy(result["source"], result["destination"])["sha256"], result["sha256"])
            with self.assertRaisesRegex(ValueError, "already exists"):
                copy_uf2(str(source), str(destination), pathlib.Path(result["destination"]).name)

    def test_copy_rejects_non_uf2_and_unsafe_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "firmware.bin"
            source.write_bytes(b"binary")
            with self.assertRaises(ValueError):
                copy_uf2(str(source), str(root / "release"), "kbd-Central-v0.3.uf2")
            source.rename(root / "firmware.uf2")
            with self.assertRaises(ValueError):
                copy_uf2(str(root / "firmware.uf2"), str(root / "release"), "../escape.uf2")

    def test_copy_verification_detects_size_and_content_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source, destination = root / "source.uf2", root / "destination.uf2"
            source.write_bytes(b"abcdef")
            destination.write_bytes(b"abc")
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                verify_copy(str(source), str(destination))
            destination.write_bytes(b"abcdeg")
            with self.assertRaisesRegex(ValueError, "content mismatch"):
                verify_copy(str(source), str(destination))

    def test_copy_refuses_destination_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            source = root / "source.uf2"
            source.write_bytes(b"firmware")
            (allowed / "project").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "escapes"):
                copy_uf2(str(source), str(allowed / "project"), "firmware.uf2", str(allowed))
            self.assertFalse((outside / "firmware.uf2").exists())


if __name__ == "__main__":
    unittest.main()
