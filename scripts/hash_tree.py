"""Hash a source tree (excluding volatile dirs) so deploy.bat can skip
rebuilding a stack whose code has not changed since the last deploy."""
import fnmatch
import hashlib
import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1])
    excludes = sys.argv[2:]
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        top = rel.split("/", 1)[0]
        if any(
            fnmatch.fnmatch(rel, ex) or fnmatch.fnmatch(top, ex)
            for ex in excludes
        ):
            continue
        h.update(rel.encode())
        h.update(path.read_bytes())
    print(h.hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
