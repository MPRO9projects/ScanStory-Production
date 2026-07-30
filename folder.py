from pathlib import Path
import argparse
import shutil
import zipfile
from datetime import datetime

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico"}
VIDEO_EXTS = {".mp4", ".webm", ".mov"}

CODE_EXTS = {
    ".html", ".htm", ".py", ".js", ".css",
    ".json", ".txt", ".jinja", ".j2", ".webp"
}

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__",
    "node_modules", "migrations", "instance", "backups"
}


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def add_to_backup(zipf, path: Path, root: Path):
    if path.exists() and path.is_file():
        zipf.write(path, path.relative_to(root))


def build_mapping(root: Path, move_videos: bool):
    static_dir = root / "static"
    uploads_dir = static_dir / "uploads"
    assets_dir = static_dir / "assets"

    mapping = {}

    # 1. Move logo folder files
    logo_dir = uploads_dir / "logos"
    if logo_dir.exists():
        for old_file in logo_dir.iterdir():
            if old_file.is_file():
                new_file = assets_dir / "logos" / old_file.name
                mapping[old_file] = new_file

    # 2. Move OG image folder files
    og_dir = uploads_dir / "og"
    if og_dir.exists():
        for old_file in og_dir.iterdir():
            if old_file.is_file():
                new_file = assets_dir / "og" / old_file.name
                mapping[old_file] = new_file

    # 3. Move direct landing page images from static/uploads/
    # This will NOT touch folders like @olympianaomy or admin.
    if uploads_dir.exists():
        for old_file in uploads_dir.iterdir():
            if old_file.is_file() and old_file.suffix.lower() in IMAGE_EXTS:
                new_file = assets_dir / "landing" / old_file.name
                mapping[old_file] = new_file

    # 4. Optional: move public demo videos
    # Use only when you also update your Flask /media routes.
    video_dir = uploads_dir / "videos"
    if move_videos and video_dir.exists():
        for old_file in video_dir.iterdir():
            if old_file.is_file() and old_file.suffix.lower() in VIDEO_EXTS:
                new_file = assets_dir / "videos" / old_file.name
                mapping[old_file] = new_file

    return mapping


def collect_code_files(root: Path):
    files = []
    for path in root.rglob("*"):
        if should_skip(path):
            continue
        if path.is_file() and path.suffix.lower() in CODE_EXTS:
            files.append(path)
    return files


def replace_references(root: Path, mapping: dict, apply_changes: bool, backup_zip):
    static_dir = root / "static"
    changed_files = []

    replacements = []

    for old_file, new_file in mapping.items():
        old_rel_static = old_file.relative_to(static_dir).as_posix()
        new_rel_static = new_file.relative_to(static_dir).as_posix()

        old_static_url = "/static/" + old_rel_static
        new_static_url = "/static/" + new_rel_static

        replacements.append((old_rel_static, new_rel_static))
        replacements.append((old_static_url, new_static_url))

    for code_file in collect_code_files(root):
        try:
            original = code_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        updated = original

        for old_text, new_text in replacements:
            updated = updated.replace(old_text, new_text)

        if updated != original:
            changed_files.append(code_file)

            if apply_changes:
                add_to_backup(backup_zip, code_file, root)
                code_file.write_text(updated, encoding="utf-8")

    return changed_files


def move_files(root: Path, mapping: dict, apply_changes: bool, backup_zip):
    moved_files = []

    for old_file, new_file in mapping.items():
        if not old_file.exists():
            continue

        moved_files.append((old_file, new_file))

        if apply_changes:
            add_to_backup(backup_zip, old_file, root)

            new_file.parent.mkdir(parents=True, exist_ok=True)

            if new_file.exists():
                print(f"[SKIP MOVE] Target already exists: {new_file}")
                continue

            shutil.move(str(old_file), str(new_file))

    return moved_files


def main():
    parser = argparse.ArgumentParser(
        description="Move public ScanStory landing assets from static/uploads to static/assets and update references."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move files and update code. Without this, only dry-run is shown."
    )
    parser.add_argument(
        "--move-videos",
        action="store_true",
        help="Also move static/uploads/videos to static/assets/videos. Use only after checking Flask media routes."
    )

    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    static_dir = root / "static"
    uploads_dir = static_dir / "uploads"

    if not uploads_dir.exists():
        print("ERROR: static/uploads folder not found.")
        print(f"Checked path: {uploads_dir}")
        return

    mapping = build_mapping(root, args.move_videos)

    print("\n==============================")
    print("SCANSTORY ASSET MIGRATION")
    print("==============================")
    print(f"Project root: {root}")
    print(f"Mode: {'APPLY CHANGES' if args.apply else 'DRY RUN ONLY'}")
    print(f"Move videos: {'YES' if args.move_videos else 'NO'}")
    print("==============================\n")

    if not mapping:
        print("No files found to move.")
        return

    print("Files planned for move:\n")
    for old_file, new_file in mapping.items():
        print(f"OLD: {old_file.relative_to(root)}")
        print(f"NEW: {new_file.relative_to(root)}")
        print("-" * 50)

    backup_zip = None

    if args.apply:
        backup_dir = root / "backups"
        backup_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"before_static_asset_migration_{timestamp}.zip"

        backup_zip = zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED)
        print(f"\nBackup will be created at: {backup_path.relative_to(root)}\n")

    changed_files = replace_references(root, mapping, args.apply, backup_zip)
    moved_files = move_files(root, mapping, args.apply, backup_zip)

    if backup_zip:
        backup_zip.close()

    print("\n==============================")
    print("CODE FILES UPDATED")
    print("==============================")

    if changed_files:
        for file in changed_files:
            print(file.relative_to(root))
    else:
        print("No code files needed updates.")

    print("\n==============================")
    print("FILES MOVED")
    print("==============================")

    if moved_files:
        for old_file, new_file in moved_files:
            print(f"{old_file.relative_to(root)}  ->  {new_file.relative_to(root)}")
    else:
        print("No files moved.")

    print("\n==============================")
    print("NEXT CHECK")
    print("==============================")
    print("Search your project for:")
    print("/static/uploads")
    print("uploads/logos")
    print("uploads/og")
    print("\nExpected after correction:")
    print("Landing page should use /static/assets/")
    print("User/customer uploads should NOT be inside public static/uploads/")


if __name__ == "__main__":
    main()