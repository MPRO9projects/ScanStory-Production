from pathlib import Path

import pytest

from storage import LegacyStorageCompatibility, LocalFilesystemStorage, StorageSecurityError, build_storage_key


def test_local_storage_put_get_exists_copy_delete_metadata(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "store")
    source = tmp_path / "source.txt"
    source.write_text("hello", encoding="utf-8")
    storage.put_file("workspaces/w/experiences/e/triggers/t/original/file.txt", source)

    assert storage.exists("workspaces/w/experiences/e/triggers/t/original/file.txt")
    assert storage.get_size("workspaces/w/experiences/e/triggers/t/original/file.txt") == 5
    assert storage.get_metadata("workspaces/w/experiences/e/triggers/t/original/file.txt")["sha256"]
    storage.copy("workspaces/w/experiences/e/triggers/t/original/file.txt", "workspaces/w/copy.txt")
    assert storage.exists("workspaces/w/copy.txt")
    storage.delete("workspaces/w/copy.txt")
    assert not storage.exists("workspaces/w/copy.txt")


def test_storage_rejects_path_traversal_and_root_escape(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "store")
    with pytest.raises(StorageSecurityError):
        storage.exists("../escape.txt")
    with pytest.raises(StorageSecurityError):
        storage.exists("C:/escape.txt")


def test_storage_atomic_replacement(tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "store")
    one = tmp_path / "one.txt"
    two = tmp_path / "two.txt"
    one.write_text("one", encoding="utf-8")
    two.write_text("two", encoding="utf-8")
    key = "workspaces/w/file.txt"
    storage.put_file(key, one)
    storage.put_file(key, two)
    with storage.open_file(key, "r") as handle:
        assert handle.read() == "two"


def test_build_storage_key_sanitizes_filename():
    key = build_storage_key("wsp", "exp", "trg", "original/reference-image", "..\\evil.JPG")
    assert key == "workspaces/wsp/experiences/exp/triggers/trg/original/reference-image/evil.jpg"
    assert ".." not in key


def test_legacy_storage_read_and_root_isolation(tmp_path):
    user_root = tmp_path / "user"
    admin_root = tmp_path / "admin"
    user_root.mkdir()
    admin_root.mkdir()
    (user_root / "image.jpg").write_bytes(b"user")
    (admin_root / "image.jpg").write_bytes(b"admin")
    legacy = LegacyStorageCompatibility({"user_images": user_root, "admin_images": admin_root})

    assert legacy.resolve("user_images", "image.jpg").read_bytes() == b"user"
    assert legacy.resolve("admin_images", "image.jpg").read_bytes() == b"admin"
    with pytest.raises(FileNotFoundError):
        legacy.resolve("user_images", "missing.jpg")
    with pytest.raises(StorageSecurityError):
        legacy.resolve("missing_root", "image.jpg")
