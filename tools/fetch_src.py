"""Fetch sqlglot's sources at the pinned commit from GitHub, with only the standard library.

    python fetch_src.py <sha> <dest>

Runs in `docker build`. It avoids apt and git: the README allows downloads only from public
registries and sqlglot's repository at the pinned commit.
"""

import io
import sys
import tarfile
import time
import urllib.request
from pathlib import Path


def main() -> None:
    sha, dest = sys.argv[1], Path(sys.argv[2])
    url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
    for attempt in range(4):
        try:
            data = urllib.request.urlopen(url, timeout=120).read()
            break
        except Exception as e:
            print(f"fetch attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    else:
        raise SystemExit("could not download sqlglot sources")
    root = f"sqlglot-{sha}/"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = [m for m in tf.getmembers() if m.name.startswith((root + "sqlglot/", root + "tests/"))]
        for m in members:
            m.name = m.name[len(root):]
        dest.mkdir(parents=True, exist_ok=True)
        tf.extractall(dest, members=members, filter="data")
    for must in ("sqlglot/__init__.py", "sqlglot/parser.py", "tests/helpers.py"):
        if not (dest / must).is_file():
            raise SystemExit(f"missing {must} after extraction")
    print(f"fetched {len(members)} files into {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
