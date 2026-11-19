"""Create a validated gzipped deployment archive for a project directory."""
import argparse
import fnmatch
import sys
import tarfile
from pathlib import Path


def _is_excluded(relative_path, patterns):
    parts = relative_path.parts
    name = relative_path.name
    return any(
        fnmatch.fnmatch(str(relative_path), pattern)
        or fnmatch.fnmatch(name, pattern)
        or any(fnmatch.fnmatch(part, pattern) for part in parts)
        for pattern in patterns
    )


def create_archive(root, output, patterns):
    root = Path(root).resolve()
    output = Path(output).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Archive root does not exist: {root}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if path.is_symlink() or _is_excluded(relative, patterns):
                continue
            # Skip anything that is not a regular file or directory. On
            # Windows, reserved device names ("nul", "con") stat as character
            # devices and would otherwise be tarred as device nodes, breaking
            # the remote "COPY . ." docker build.
            if not (path.is_file() or path.is_dir()):
                print(f"[WARN] Skipping non-regular file: {relative}")
                continue
            archive.add(path, arcname=str(relative), recursive=False)

    with tarfile.open(output, "r:gz") as archive:
        members = archive.getmembers()
    if not members:
        raise RuntimeError(f"Archive is empty: {output}")
    print(f"[OK] Created and validated {len(members)} archive members: {output}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("output")
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()
    try:
        create_archive(args.root, args.output, args.exclude)
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
