import os
import subprocess
import pytest
from core.scanner import find_git_root, should_scan_file, compute_delta


class TestShouldScanFile:
    def test_md_files_allowed(self):
        assert should_scan_file("README.md") is True

    def test_py_files_excluded(self):
        assert should_scan_file("main.py") is False

    def test_env_excluded(self):
        assert should_scan_file(".env") is False

    def test_credentials_excluded(self):
        assert should_scan_file("credentials.json") is False

    def test_excluded_dir(self):
        assert should_scan_file("node_modules/README.md") is False
        assert should_scan_file(".git/config") is False


class TestFindGitRoot:
    def test_finds_root(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subdir = tmp_path / "sub" / "deep"
        subdir.mkdir(parents=True)
        root = find_git_root(str(subdir))
        assert root == str(tmp_path)

    def test_returns_none_for_non_git(self, tmp_path):
        root = find_git_root(str(tmp_path))
        assert root is None


class TestComputeDelta:
    def test_no_changes(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], capture_output=True)
        readme = tmp_path / "README.md"
        readme.write_text("# Test")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True)
        head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        delta = compute_delta("test-project", str(tmp_path), head)
        assert delta is None
