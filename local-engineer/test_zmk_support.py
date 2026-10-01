import pathlib
import os
import subprocess
import tempfile
import unittest

from zmk_support import ZMK_VERSION_UNKNOWN, detect_zmk_version


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


if __name__ == "__main__":
    unittest.main()
