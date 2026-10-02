#!/usr/bin/env python3
"""Build deterministic single-executable archives for MindAlert CLI tools."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import struct
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TOOLS_DIR = ROOT / "tools"
DIST_DIR = ROOT / "dist"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\Z")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_UNIX_METADATA = (
    struct.pack("<HHBI", 0x5455, 5, 1, 0)
    + struct.pack("<HHBBBBB", 0x7875, 5, 1, 1, 0, 1, 0)
)


def fail(detail: str) -> int:
    print(f"build error: {detail}", file=sys.stderr)
    return 2


def source_files(tool_dir: Path) -> list[Path]:
    files = [
        path
        for path in tool_dir.rglob("*")
        if path.is_file()
        and path.name != "VERSION"
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    ]
    return sorted(files, key=lambda path: path.relative_to(tool_dir).as_posix())


def build_zipapp(tool_dir: Path) -> bytes:
    files = source_files(tool_dir)
    if not (tool_dir / "__main__.py").is_file():
        raise ValueError("tool source must contain __main__.py")

    archive = io.BytesIO()
    archive.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for path in files:
            relative = path.relative_to(tool_dir).as_posix()
            info = zipfile.ZipInfo(relative, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (0o100644 & 0xFFFF) << 16
            info.extra = ZIP_UNIX_METADATA
            bundle.writestr(info, path.read_bytes())
    return archive.getvalue()


def build_tarball(executable_name: str, executable: bytes) -> bytes:
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as gzipped:
        with tarfile.open(
            fileobj=gzipped, mode="w", format=tarfile.USTAR_FORMAT
        ) as bundle:
            info = tarfile.TarInfo(executable_name)
            info.size = len(executable)
            info.mode = 0o755
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            bundle.addfile(info, io.BytesIO(executable))
    return compressed.getvalue()


def atomic_write(path: Path, content: bytes, mode: int = 0o644) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not NAME_RE.fullmatch(argv[0]):
        return fail("usage: build.py <tool-name>")

    name = argv[0]
    tool_dir = TOOLS_DIR / name
    if not tool_dir.is_dir():
        return fail(f"tool not found: {name}")

    version_path = tool_dir / "VERSION"
    if not version_path.is_file():
        return fail(f"VERSION not found for {name}")
    version = version_path.read_text(encoding="utf-8").strip()
    if not VERSION_RE.fullmatch(version):
        return fail(f"invalid VERSION for {name}")

    try:
        executable = build_zipapp(tool_dir)
        tarball = build_tarball(name, executable)
    except (OSError, ValueError) as error:
        return fail(str(error))

    DIST_DIR.mkdir(exist_ok=True)
    archive_path = DIST_DIR / f"{name}-{version}.tar.gz"
    checksum_path = Path(f"{archive_path}.sha256")
    digest = hashlib.sha256(tarball).hexdigest()
    atomic_write(archive_path, tarball)
    checksum = f"{digest}  {archive_path.name}\n".encode("ascii")
    atomic_write(checksum_path, checksum)
    print(archive_path.relative_to(ROOT))
    print(checksum_path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
