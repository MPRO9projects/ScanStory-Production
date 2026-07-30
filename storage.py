import hashlib
import mimetypes
import os
import shutil
import tempfile
from pathlib import Path


ALLOWED_STORAGE_SEGMENTS = {"workspaces", "experiences", "triggers", "original", "derived", "recognition", "qr", "tmp"}


class StorageError(Exception):
    pass


class StorageSecurityError(StorageError):
    pass


def normalize_storage_key(key):
    if not key or "\x00" in key:
        raise StorageSecurityError("invalid storage key")
    key = key.replace("\\", "/").strip("/")
    parts = [part for part in key.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise StorageSecurityError("path traversal rejected")
    if Path(key).is_absolute() or ":" in parts[0]:
        raise StorageSecurityError("absolute storage key rejected")
    return "/".join(parts)


def build_storage_key(workspace_key, experience_key, trigger_key, asset_kind, filename):
    safe_name = Path(filename).name.replace("\\", "_").replace("/", "_")
    ext = Path(safe_name).suffix.lower()
    stem = Path(safe_name).stem or asset_kind
    if not ext:
        ext = ".bin"
    return normalize_storage_key(
        f"workspaces/{workspace_key}/experiences/{experience_key}/triggers/{trigger_key}/{asset_kind}/{stem}{ext}"
    )


def stream_hash(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LocalFilesystemStorage:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        normalized = normalize_storage_key(key)
        path = (self.root / normalized).resolve()
        if path != self.root and self.root not in path.parents:
            raise StorageSecurityError("storage key escapes root")
        return path

    def put_file(self, key, source_path):
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(target.parent))
        os.close(fd)
        try:
            shutil.copyfile(source_path, temp_name)
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return key

    def get_file(self, key, destination_path):
        source = self._path(key)
        if not source.exists():
            raise FileNotFoundError(key)
        Path(destination_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination_path)
        return destination_path

    def open_file(self, key, mode="rb"):
        path = self._path(key)
        return open(path, mode)

    def exists(self, key):
        return self._path(key).exists()

    def delete(self, key):
        path = self._path(key)
        if path.exists():
            path.unlink()
            return True
        return False

    def copy(self, source_key, target_key):
        source = self._path(source_key)
        if not source.exists():
            raise FileNotFoundError(source_key)
        target = self._path(target_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return target_key

    def move(self, source_key, target_key):
        self.copy(source_key, target_key)
        self.delete(source_key)
        return target_key

    def get_size(self, key):
        return self._path(key).stat().st_size

    def get_metadata(self, key):
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(key)
        return {
            "key": normalize_storage_key(key),
            "size": path.stat().st_size,
            "content_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
            "sha256": stream_hash(path),
        }

    def generate_access_url(self, key):
        return f"local://{normalize_storage_key(key)}"

    def list_prefix(self, prefix):
        base = self._path(prefix)
        if not base.exists():
            return []
        return [str(path.relative_to(self.root)).replace("\\", "/") for path in base.rglob("*") if path.is_file()]


class LegacyStorageCompatibility:
    def __init__(self, roots):
        self.roots = {name: Path(path).resolve() for name, path in roots.items()}

    def resolve(self, root_name, filename):
        if root_name not in self.roots:
            raise StorageSecurityError("unknown legacy root")
        if not filename:
            raise FileNotFoundError("missing legacy filename")
        root = self.roots[root_name]
        path = (root / Path(filename).name).resolve()
        if path != root and root not in path.parents:
            raise StorageSecurityError("legacy path escapes root")
        if not path.exists():
            raise FileNotFoundError(str(path))
        return path

    def exists(self, root_name, filename):
        try:
            self.resolve(root_name, filename)
            return True
        except FileNotFoundError:
            return False
