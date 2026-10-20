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
        commands = self.root / "bin"
        commands.mkdir()
        self.docker_calls = self.root / "docker-calls"
        docker = commands / "docker"
        docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$UPDATE_TEST_DOCKER_CALLS"\nexit 99\n')
        docker.chmod(0o700)
        self.env["PATH"] = str(commands) + os.pathsep + self.env["PATH"]
        self.env["UPDATE_TEST_DOCKER_CALLS"] = str(self.docker_calls)
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "init", "--initial-branch=main", str(self.seed))
        repository = Path(__file__).resolve().parents[1]
        (self.seed / "scripts/shell").mkdir(parents=True)
        shutil.copytree(repository / "scripts/shell/lib", self.seed / "scripts/shell/lib")
        for path in ("Makefile", "scripts/shell/source-update.sh"):
            shutil.copy2(repository / path, self.seed / path)
        (self.seed / ".gitignore").write_text(".env\n.setup-complete\n")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Initial source")
        self.git(self.seed, "remote", "add", "origin", str(self.remote))
        self.git(self.seed, "push", "origin", "main")
        self.git(self.root, "clone", str(self.remote), str(self.checkout))
        self.git(self.checkout, "remote", "set-url", "origin", "git@github.com:rehuony/hummingbot-api.git")
        self.initial = self.git(self.checkout, "rev-parse", "HEAD")

    def tearDown(self):
        self.assertFalse(self.docker_calls.exists(), "Source checks and updates must never invoke Docker")

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

    def check_update(self):
        return subprocess.run(["make", "check-update"], cwd=self.checkout, env=self.env, text=True, capture_output=True)

    def checkout_state(self):
        return {
            "head": self.git(self.checkout, "rev-parse", "HEAD"),
            "branch": self.git(self.checkout, "branch", "--show-current"),
            "status": self.git(self.checkout, "status", "--porcelain"),
            "files": {
                str(path.relative_to(self.checkout)): path.read_bytes()
                for path in self.checkout.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(self.checkout).parts
            },
        }

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
        self.assertIn("commit or stash", result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.initial)
        self.assertEqual((self.checkout / "personal-notes").read_text(), "Uncommitted work\n")

    def test_another_branch_is_preserved(self):
        self.git(self.checkout, "switch", "-c", "feature/work")
        result = self.update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires the main branch", result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "feature/work")

    def test_another_origin_is_refused(self):
        self.git(self.checkout, "remote", "set-url", "origin", "https://github.com/example/other-api.git")
        result = self.update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("origin must point", result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.initial)

    def test_divergence_preserves_both_histories(self):
        self.advance_remote()
        self.git(self.checkout, "commit", "--allow-empty", "-m", "Local work")
        local = self.git(self.checkout, "rev-parse", "HEAD")
        result = self.update()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), local)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_check_reports_same_revision_without_changing_checkout(self):
        before = self.checkout_state()
        result = self.check_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("up_to_date", result.stdout)
        self.assertIn(self.initial, result.stdout)
        self.assertIn("Working tree: clean", result.stdout)
        self.assertEqual(self.checkout_state(), before)

    def test_check_fetches_new_main_without_updating_local_source(self):
        expected = self.advance_remote()
        before = self.checkout_state()
        result = self.check_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("update_available", result.stdout)
        self.assertIn(self.initial, result.stdout)
        self.assertIn(expected, result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "origin/main"), expected)
        self.assertEqual(self.checkout_state(), before)

    def test_check_reports_ahead_on_feature_branch_without_switching(self):
        self.git(self.checkout, "switch", "-c", "feature/local-work")
        self.git(self.checkout, "commit", "--allow-empty", "-m", "Local source")
        before = self.checkout_state()
        result = self.check_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ahead", result.stdout)
        self.assertIn(before["head"], result.stdout)
        self.assertEqual(self.checkout_state(), before)

    def test_check_reports_diverged_histories_without_modifying_them(self):
        expected = self.advance_remote()
        self.git(self.checkout, "commit", "--allow-empty", "-m", "Local source")
        before = self.checkout_state()
        result = self.check_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("diverged", result.stdout)
        self.assertIn(expected, result.stdout)
        self.assertIn(before["head"], result.stdout)
        self.assertEqual(self.checkout_state(), before)

    def test_check_reports_tracked_and_untracked_work_without_touching_it(self):
        for tracked in (True, False):
            with self.subTest(tracked=tracked):
                path = self.checkout / ("Makefile" if tracked else "personal-notes")
                original = path.read_text() if path.exists() else None
                path.write_text((original or "") + "\n# Uncommitted local work\n")
                before = self.checkout_state()
                result = self.check_update()

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("up_to_date", result.stdout)
                self.assertIn("Working tree: modified", result.stdout)
                self.assertEqual(self.checkout_state(), before)
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original)

    def test_check_ignores_runtime_configuration_and_keeps_it_private(self):
        (self.checkout / ".env").write_text("PASSWORD=private-test-value\n")
        before = self.checkout_state()
        result = self.check_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Working tree: clean", result.stdout)
        self.assertNotIn("private-test-value", result.stdout + result.stderr)
        self.assertEqual(self.checkout_state(), before)

    def test_untracked_work_is_detected_when_git_hides_it_by_default(self):
        self.git(self.checkout, "config", "status.showUntrackedFiles", "no")
        (self.checkout / "personal-notes").write_text("Uncommitted work\n")
        before = self.checkout_state()
        check = self.check_update()
        update = self.update()

        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("Working tree: modified", check.stdout)
        self.assertNotEqual(update.returncode, 0)
        self.assertIn("commit or stash", update.stdout + update.stderr)
        self.assertEqual(self.checkout_state(), before)

    def test_check_supports_detached_head(self):
        self.advance_remote()
        self.git(self.checkout, "checkout", "--detach", self.initial)
        before = self.checkout_state()
        result = self.check_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("update_available", result.stdout)
        self.assertEqual(self.checkout_state(), before)

    def test_check_refuses_unrelated_origin_without_fetching(self):
        self.advance_remote()
        self.git(self.checkout, "remote", "set-url", "origin", "https://github.com/example/other-api.git")
        before = self.checkout_state()
        remote_before = self.git(self.checkout, "rev-parse", "origin/main")
        result = self.check_update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("origin must point", result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "origin/main"), remote_before)
        self.assertEqual(self.checkout_state(), before)

    def test_check_refuses_shallow_history_instead_of_guessing_relationship(self):
        self.advance_remote()
        shallow = self.root / "shallow"
        self.git(self.root, "clone", "--depth=1", self.remote.as_uri(), str(shallow))
        self.checkout = shallow
        self.git(self.checkout, "remote", "set-url", "origin", "git@github.com:rehuony/hummingbot-api.git")
        before = self.checkout_state()
        result = self.check_update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full Git history", result.stdout + result.stderr)
        self.assertEqual(self.checkout_state(), before)

    def test_check_reports_fetch_failure_without_changing_checkout(self):
        self.env["UPDATE_TEST_REMOTE"] = str(self.root / "missing.git")
        before = self.checkout_state()
        result = self.check_update()

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("up_to_date", result.stdout)
        self.assertNotIn("update_available", result.stdout)
        self.assertEqual(self.checkout_state(), before)


if __name__ == "__main__":
    unittest.main()
