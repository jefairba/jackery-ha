"""Regression tests for repository metadata used by HACS."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "custom_components" / "jackery" / "manifest.json"
README_PATH = REPO_ROOT / "README.md"
HACS_PATH = REPO_ROOT / "hacs.json"


class MetadataTests(unittest.TestCase):
    """Validate versioning and canonical repository links."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text())
        cls.hacs = json.loads(HACS_PATH.read_text())
        cls.readme = README_PATH.read_text()

    def test_manifest_points_to_current_repository(self) -> None:
        """Manifest links should resolve to this repository."""
        self.assertEqual(
            self.manifest["documentation"],
            "https://github.com/jefairba/jackery-ha/blob/main/README.md",
        )
        # Issues are disabled on this fork, so no issue_tracker is advertised.
        self.assertNotIn("issue_tracker", self.manifest)
        self.assertEqual(self.manifest["codeowners"], ["@jefairba"])

    def test_readme_version_badge_matches_manifest(self) -> None:
        """README badge should advertise the same release as the manifest."""
        # shields.io static badges use "-" as a separator; a literal dash is "--".
        version = self.manifest["version"].replace("-", "--")
        self.assertIn(
            f"https://img.shields.io/badge/version-{version}-blue.svg",
            self.readme,
        )

    def test_fork_version_marks_upstream_base(self) -> None:
        """Fork releases are <upstream version>-jf.<n>."""
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+-jf\.\d+$")

    def test_hacs_offers_only_tagged_releases(self) -> None:
        """This fork pins installs to releases; HACS must hide the default branch."""
        self.assertTrue(self.hacs["hide_default_branch"])
        self.assertIn("default branch is hidden", self.readme)

    def test_repository_files_do_not_reference_fork_links(self) -> None:
        """The published metadata should not point at the temporary contributor fork."""
        legacy_repo = "usersaynoso/jackery-homeassistant"
        checked_files = [
            MANIFEST_PATH,
            README_PATH,
            HACS_PATH,
        ]
        for file_path in checked_files:
            self.assertNotIn(legacy_repo, file_path.read_text(), str(file_path))


if __name__ == "__main__":
    unittest.main()
