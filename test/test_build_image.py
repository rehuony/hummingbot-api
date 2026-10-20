"""Verify build provenance and Make entry points without a Docker daemon."""

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


@unittest.skipUnless(shutil.which("git") and shutil.which("make"), "image build checks require Git and Make")
class BuildImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.docker_calls = self.root / "docker-calls"
        commands = self.root / "bin"
        commands.mkdir()
        docker = commands / "docker"
        docker.write_text('#!/bin/sh\nprintf "%s\\0" "$@" >> "$BUILD_TEST_DOCKER_CALLS"\n')
        docker.chmod(0o700)
        self.env = {
            **os.environ,
            "PATH": str(commands) + os.pathsep + os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "BUILD_TEST_DOCKER_CALLS": str(self.docker_calls),
        }
        repository = Path(__file__).resolve().parents[1]
        (self.checkout / "scripts/shell").mkdir(parents=True)
        shutil.copytree(repository / "scripts/shell/lib", self.checkout / "scripts/shell/lib")
        for path in ("Makefile", "scripts/shell/build-image.sh", "scripts/shell/image-info.sh", "version.py"):
            shutil.copy2(repository / path, self.checkout / path)
        (self.checkout / ".gitignore").write_text(".env\n.setup-complete\n")
        self.git("init", "--initial-branch=main")
        self.git("add", ".")
        self.git("commit", "-m", "Build source")
        self.revision = self.git("rev-parse", "HEAD")

    def git(self, *arguments):
        result = subprocess.run(
            ["git", "-c", "user.name=Build Test", "-c", "user.email=build@example.invalid", *arguments],
            cwd=self.checkout,
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def run_command(self, *arguments):
        return subprocess.run(arguments, cwd=self.checkout, env=self.env, text=True, capture_output=True)

    def metadata(self):
        result = self.run_command("bash", "scripts/shell/build-image.sh", "--metadata")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.docker_calls.exists(), "Reading metadata must not invoke Docker")
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_metadata_identifies_exact_clean_checkout_and_current_utc_build_time(self):
        before = datetime.now(timezone.utc).replace(microsecond=0)
        metadata = self.metadata()
        after = datetime.now(timezone.utc)

        self.assertEqual(metadata["revision"], self.revision)
        self.assertEqual(metadata["branch"], "main")
        self.assertEqual(metadata["dirty"], "false")
        self.assertRegex(metadata["created"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        created = datetime.fromisoformat(metadata["created"].replace("Z", "+00:00"))
        self.assertLessEqual(before, created)
        self.assertLessEqual(created, after)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_metadata_uses_current_version_literal(self):
        (self.checkout / "version.py").write_text('"""Build fixture."""\n\nVERSION = "2.3.4"\n')

        self.assertEqual(self.metadata()["version"], "2.3.4")

    def test_metadata_marks_tracked_and_untracked_modifications(self):
        for tracked in (True, False):
            with self.subTest(tracked=tracked):
                path = self.checkout / ("version.py" if tracked else "new-source.py")
                original = path.read_text() if path.exists() else None
                path.write_text((original or "") + "\n# Local work\n")
                metadata = self.metadata()

                self.assertEqual(metadata["revision"], self.revision)
                self.assertEqual(metadata["dirty"], "true")
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original)

    def test_metadata_marks_staged_modifications(self):
        path = self.checkout / "new-source.py"
        path.write_text("# Staged source\n")
        self.git("add", "new-source.py")

        self.assertEqual(self.metadata()["dirty"], "true")

    def test_metadata_ignores_runtime_secrets(self):
        (self.checkout / ".env").write_text("PASSWORD=private-test-value\n")
        metadata = self.metadata()

        self.assertEqual(metadata["dirty"], "false")
        self.assertNotIn("private-test-value", str(metadata))

    def test_metadata_detects_untracked_source_when_git_hides_it_by_default(self):
        self.git("config", "status.showUntrackedFiles", "no")
        (self.checkout / "new-source.py").write_text("# Local work\n")

        self.assertEqual(self.metadata()["dirty"], "true")

    def test_missing_version_refuses_build_without_invoking_docker(self):
        (self.checkout / "version.py").write_text('"""Version assignment is missing."""\n')
        result = self.run_command("make", "build")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version.py", result.stderr)
        self.assertFalse(self.docker_calls.exists())

    def test_metadata_uses_actual_branch_and_reports_detached_checkout(self):
        self.git("switch", "-c", "feature/build-metadata")
        self.assertEqual(self.metadata()["branch"], "feature/build-metadata")
        self.git("checkout", "--detach", self.revision)
        self.assertEqual(self.metadata()["branch"], "detached")

    def test_build_supplies_metadata_to_docker_and_supports_image_override(self):
        for image in ("rehuony/hummingbot-api:main", "example/api:verify"):
            with self.subTest(image=image):
                result = self.run_command("make", "build", f"API_IMAGE={image}")
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = self.docker_calls.read_bytes().decode().rstrip("\0").split("\0")
                self.assertEqual(arguments[0], "build")
                self.assertEqual(arguments.count("build"), 1)
                self.assertEqual(arguments[-1], ".")
                self.assertEqual(arguments[arguments.index("-t") + 1], image)
                build_args = {
                    arguments[index + 1].split("=", 1)[0]: arguments[index + 1].split("=", 1)[1]
                    for index, value in enumerate(arguments)
                    if value == "--build-arg"
                }
                self.assertEqual(build_args["VCS_REF"], self.revision)
                self.assertEqual(build_args["VCS_BRANCH"], "main")
                self.assertEqual(build_args["VCS_DIRTY"], "false")
                self.assertRegex(build_args["BUILD_DATE"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
                version_source = (self.checkout / "version.py").read_text()
                self.assertIn(f'VERSION = "{build_args["API_VERSION"]}"', version_source)
                self.assertEqual(self.git("status", "--porcelain"), "")
                self.docker_calls.unlink()

    def test_make_build_defaults_to_personal_local_image(self):
        result = self.run_command("make", "build")

        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.docker_calls.read_bytes().decode().rstrip("\0").split("\0")
        self.assertEqual(arguments[arguments.index("-t") + 1], "rehuony/hummingbot-api:main")

    def test_image_info_only_inspects_requested_local_image(self):
        result = self.run_command("make", "image-info", "API_IMAGE=example/api:verify")

        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.docker_calls.read_bytes().decode().rstrip("\0").split("\0")
        self.assertEqual(arguments[:3], ["image", "inspect", "example/api:verify"])
        self.assertNotIn("build", arguments)
        self.assertNotIn("pull", arguments)
        self.assertNotIn("compose", arguments)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_help_has_no_setup_build_or_deployment_side_effects(self):
        result = self.run_command("make", "help")

        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("check-update", "image-info", "build", "deploy", "update"):
            self.assertIn(command, result.stdout)
        self.assertFalse(self.docker_calls.exists())
        self.assertFalse((self.checkout / ".env").exists())
        self.assertFalse((self.checkout / ".setup-complete").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_make_default_target_shows_help(self):
        result = self.run_command("make")
        help_result = self.run_command("make", "help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertEqual(result.stdout, help_result.stdout)
        self.assertFalse(self.docker_calls.exists())
        self.assertFalse((self.checkout / ".env").exists())
        self.assertFalse((self.checkout / ".setup-complete").exists())


if __name__ == "__main__":
    unittest.main()
