"""Exercise source updates using disposable repositories and a local SSH transport."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("git") and shutil.which("make"), "source update checks require Git and Make")
class SourceUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.checkout = self.root / "checkout"
        self.env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_VARIANT": "ssh",
            "UPDATE_TEST_REMOTE": str(self.remote),
        }
        transport = self.root / "local-ssh"
        transport.write_text('#!/bin/sh\nexec git-upload-pack "$UPDATE_TEST_REMOTE"\n')
        transport.chmod(0o700)
        self.env["GIT_SSH_COMMAND"] = str(transport)
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "init", "--initial-branch=main", str(self.seed))
        shutil.copy2(Path(__file__).resolve().parents[1] / "Makefile", self.seed / "Makefile")
        self.git(self.seed, "add", "Makefile")
        self.git(self.seed, "commit", "-m", "Initial source")
        self.git(self.seed, "remote", "add", "origin", str(self.remote))
        self.git(self.seed, "push", "origin", "main")
        self.git(self.root, "clone", str(self.remote), str(self.checkout))
        self.git(self.checkout, "remote", "set-url", "origin", "git@github.com:rehuony/hummingbot-api.git")
        self.initial = self.git(self.checkout, "rev-parse", "HEAD")

    def git(self, directory, *arguments):
        result = subprocess.run(
            ["git", "-c", "user.name=Update Test", "-c", "user.email=update@example.invalid", *arguments],
            cwd=directory,
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def update(self):
        return subprocess.run(["make", "update"], cwd=self.checkout, env=self.env, text=True, capture_output=True)

    def advance_remote(self):
        self.git(self.seed, "commit", "--allow-empty", "-m", "New source")
        self.git(self.seed, "push", "origin", "main")
        return self.git(self.seed, "rev-parse", "HEAD")

    def test_clean_main_fast_forwards_without_building_or_deploying(self):
        expected = self.advance_remote()
        result = self.update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), expected)
        self.assertFalse((self.checkout / ".env").exists())
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_dirty_checkout_is_preserved(self):
        self.advance_remote()
        (self.checkout / "personal-notes").write_text("Uncommitted work\n")
        result = self.update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("commit or stash", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.initial)
        self.assertEqual((self.checkout / "personal-notes").read_text(), "Uncommitted work\n")

    def test_another_branch_is_preserved(self):
        self.git(self.checkout, "switch", "-c", "feature/work")
        result = self.update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires the main branch", result.stdout)
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "feature/work")

    def test_another_origin_is_refused(self):
        self.git(self.checkout, "remote", "set-url", "origin", "https://github.com/example/other-api.git")
        result = self.update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("origin must point", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.initial)

    def test_divergence_preserves_both_histories(self):
        self.advance_remote()
        self.git(self.checkout, "commit", "--allow-empty", "-m", "Local work")
        local = self.git(self.checkout, "rev-parse", "HEAD")
        result = self.update()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), local)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
