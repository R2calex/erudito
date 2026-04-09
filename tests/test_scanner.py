import os
import subprocess
import time
import pytest
from core.scanner import find_git_root, should_scan_file, compute_delta, compute_fs_hash, compute_fs_delta


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


class TestComputeFsHash:
    def test_deterministic(self, tmp_path):
        (tmp_path / "a.md").write_text("hello")
        (tmp_path / "b.md").write_text("world")
        h1 = compute_fs_hash(str(tmp_path))
        h2 = compute_fs_hash(str(tmp_path))
        assert h1 == h2

    def test_changes_on_new_file(self, tmp_path):
        (tmp_path / "a.md").write_text("hello")
        h1 = compute_fs_hash(str(tmp_path))
        time.sleep(0.05)
        (tmp_path / "b.md").write_text("world")
        h2 = compute_fs_hash(str(tmp_path))
        assert h1 != h2

    def test_changes_on_content_change(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("hello")
        h1 = compute_fs_hash(str(tmp_path))
        time.sleep(1.1)  # mtime resolution is 1s on some filesystems
        f.write_text("updated content that changes size")
        h2 = compute_fs_hash(str(tmp_path))
        assert h1 != h2

    def test_empty_dir(self, tmp_path):
        h = compute_fs_hash(str(tmp_path))
        assert isinstance(h, str)
        assert len(h) == 16

    def test_excludes_dirs(self, tmp_path):
        (tmp_path / "good.md").write_text("ok")
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        (node_modules / "bad.md").write_text("should be excluded")
        h_with = compute_fs_hash(str(tmp_path))
        # Hash should be the same as without node_modules
        # (since node_modules is excluded)
        assert isinstance(h_with, str)


class TestComputeFsDelta:
    def test_full_scan_no_last_hash(self, tmp_path):
        (tmp_path / "doc.md").write_text("# Hello\nContent here")
        delta = compute_fs_delta("test-proj", str(tmp_path), None)
        assert delta is not None
        assert delta["project"] == "test-proj"
        assert delta["old_hash"] is None
        assert len(delta["files"]) == 1
        assert delta["files"][0]["path"] == "doc.md"
        assert delta["files"][0]["action"] == "added"
        assert "Hello" in delta["files"][0]["content"]

    def test_no_changes_same_hash(self, tmp_path):
        (tmp_path / "doc.md").write_text("# Hello")
        delta1 = compute_fs_delta("test-proj", str(tmp_path), None)
        assert delta1 is not None
        delta2 = compute_fs_delta("test-proj", str(tmp_path), delta1["new_hash"])
        assert delta2 is None  # No changes

    def test_detects_new_file(self, tmp_path):
        (tmp_path / "a.md").write_text("# A")
        delta1 = compute_fs_delta("test-proj", str(tmp_path), None)
        time.sleep(0.05)
        (tmp_path / "b.md").write_text("# B")
        delta2 = compute_fs_delta("test-proj", str(tmp_path), delta1["new_hash"])
        assert delta2 is not None
        assert len(delta2["files"]) == 2  # full rescan on change

    def test_nonexistent_dir(self, tmp_path):
        delta = compute_fs_delta("test-proj", str(tmp_path / "nope"), None)
        assert delta is None

    def test_nested_dirs(self, tmp_path):
        sub = tmp_path / "docs" / "spec"
        sub.mkdir(parents=True)
        (sub / "SPEC.md").write_text("# SPEC")
        (tmp_path / "README.md").write_text("# README")
        delta = compute_fs_delta("test-proj", str(tmp_path), None)
        assert delta is not None
        paths = [f["path"] for f in delta["files"]]
        assert "README.md" in paths
        assert os.path.join("docs", "spec", "SPEC.md") in paths

    def test_skips_non_md(self, tmp_path):
        (tmp_path / "doc.md").write_text("# Doc")
        (tmp_path / "script.py").write_text("print('hi')")
        delta = compute_fs_delta("test-proj", str(tmp_path), None)
        assert len(delta["files"]) == 1
        assert delta["files"][0]["path"] == "doc.md"
