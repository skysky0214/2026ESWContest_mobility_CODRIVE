"""Read AI Hub's TAR-wrapped, offset-named split ZIP files without extracting them.

The AI Hub shell downloader stores a logical ZIP as members named
``*.zip.part0``, ``*.zip.part1073741824``, and so on inside one POSIX TAR.
The numeric suffix is the byte offset of that part in the logical ZIP.  This
module presents those disjoint TAR regions as one seekable stream so Python's
``zipfile`` can access individual JSON/WAV members in place.
"""

from __future__ import annotations

import bisect
import io
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Iterator


_PART_PATTERN = re.compile(r"\.zip\.part(?P<offset>\d+)$")


@dataclass(frozen=True, slots=True)
class _Part:
    logical_start: int
    logical_end: int
    physical_start: int
    name: str


def repair_zip_name(name: str) -> str:
    """Recover CP949 names that old ZIP tools decoded as CP437.

    AI Hub's JSON is UTF-8, but this dataset's ZIP filename flag is absent.  A
    failed conversion simply means the name was already valid Unicode.
    """

    try:
        return name.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


class SplitPartTarReader(io.RawIOBase):
    """Seekable logical ZIP assembled virtually from members of an outer TAR."""

    def __init__(self, outer_tar: str | Path) -> None:
        super().__init__()
        self.path = Path(outer_tar)
        if not self.path.is_file():
            raise FileNotFoundError(self.path)

        with tarfile.open(self.path, mode="r:") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.isfile() and _PART_PATTERN.search(member.name)
            ]
        if not members:
            raise ValueError(f"no .zip.part<offset> members in {self.path}")
        members.sort(key=self._part_offset)

        logical_offset = 0
        parts: list[_Part] = []
        for member in members:
            declared_offset = self._part_offset(member)
            if declared_offset != logical_offset:
                raise ValueError(
                    f"split ZIP gap/overlap in {self.path}: "
                    f"{member.name} starts at {declared_offset}, expected {logical_offset}"
                )
            parts.append(
                _Part(
                    logical_start=logical_offset,
                    logical_end=logical_offset + member.size,
                    physical_start=member.offset_data,
                    name=member.name,
                )
            )
            logical_offset += member.size

        self._parts = tuple(parts)
        self._part_ends = tuple(part.logical_end for part in parts)
        self._size = logical_offset
        self._position = 0
        self._file: BinaryIO = self.path.open("rb")

    @staticmethod
    def _part_offset(member: tarfile.TarInfo) -> int:
        match = _PART_PATTERN.search(member.name)
        if match is None:
            raise ValueError(f"invalid split ZIP member: {member.name}")
        return int(match.group("offset"))

    @property
    def logical_size(self) -> int:
        return self._size

    @property
    def part_count(self) -> int:
        return len(self._parts)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        if whence == io.SEEK_CUR:
            offset += self._position
        elif whence == io.SEEK_END:
            offset += self._size
        elif whence != io.SEEK_SET:
            raise ValueError(f"unsupported whence: {whence}")
        if offset < 0:
            raise ValueError("negative seek position")
        self._position = offset
        return self._position

    def readinto(self, buffer: bytearray | memoryview) -> int:
        self._checkClosed()
        if self._position >= self._size:
            return 0
        target = memoryview(buffer).cast("B")
        written = 0
        while written < len(target) and self._position < self._size:
            part_index = bisect.bisect_right(self._part_ends, self._position)
            part = self._parts[part_index]
            available = part.logical_end - self._position
            requested = min(len(target) - written, available)
            physical_offset = part.physical_start + self._position - part.logical_start
            self._file.seek(physical_offset)
            chunk = self._file.read(requested)
            if not chunk:
                break
            target[written : written + len(chunk)] = chunk
            written += len(chunk)
            self._position += len(chunk)
        return written

    def close(self) -> None:
        if not self.closed:
            self._file.close()
        super().close()


class NestedZipArchive:
    """Random-access view of the logical inner ZIP in an AI Hub TAR download."""

    def __init__(self, outer_tar: str | Path) -> None:
        self.path = Path(outer_tar)
        self._reader: SplitPartTarReader | None = None
        self._zip: zipfile.ZipFile | None = None

    def open(self) -> "NestedZipArchive":
        if self._zip is None:
            self._reader = SplitPartTarReader(self.path)
            self._zip = zipfile.ZipFile(self._reader, mode="r")
        return self

    @property
    def zip_file(self) -> zipfile.ZipFile:
        if self._zip is None:
            raise RuntimeError("archive is not open")
        return self._zip

    def names(self, *, repaired: bool = False) -> tuple[str, ...]:
        names = tuple(self.zip_file.namelist())
        return tuple(repair_zip_name(name) for name in names) if repaired else names

    def file_names(self, suffix: str | None = None) -> Iterator[str]:
        normalized_suffix = None if suffix is None else suffix.lower()
        for info in self.zip_file.infolist():
            if info.is_dir():
                continue
            if normalized_suffix is None or info.filename.lower().endswith(normalized_suffix):
                yield info.filename

    def read(self, member: str) -> bytes:
        return self.zip_file.read(member)

    def open_member(self, member: str) -> zipfile.ZipExtFile:
        return self.zip_file.open(member, mode="r")

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def __enter__(self) -> "NestedZipArchive":
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.close()
        return False


__all__ = ["NestedZipArchive", "SplitPartTarReader", "repair_zip_name"]
