"""Embedding metadata with exiftool.

Everything here is driven by a UTF-8 argfile rather than command-line
arguments. That single decision fixes four separate problems, each verified
against the bundled exiftool 13.52:

1. **Encoding.** Passing ``-Title=Café`` as a process argument on Windows
   round-trips through the ANSI code page and arrives mangled: ``café`` became
   ``caf?`` and ``日本の桜`` became ``????`` -- in XMP as well as IPTC, and with
   exiftool exiting 0, so nothing detected it. Via a UTF-8 argfile all of it
   round-trips intact.
2. **Duplication.** ``-Keywords+=`` appends. Running it twice over one file
   produced ``alpha, beta, alpha, gamma``. Repeated plain ``-Keywords=``
   assignments replace the whole list instead, and are idempotent.
3. **Length.** 50 keywords across two tag families approaches the Windows
   32 KB command-line ceiling. An argfile has no such limit.
4. **Injection.** A model-authored title beginning with ``-`` would otherwise
   be read as an exiftool option.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, Sequence

from .errors import MetadataWriteError

log = logging.getLogger(__name__)

#: exiftool truncates IPTC:Keywords entries beyond this many bytes.
IPTC_KEYWORD_BYTES = 64


class MetadataWriter(Protocol):
    def write(self, path: Path, *, title: str, description: str,
              keywords: Sequence[str], **extra: str) -> None: ...


def _build_args(
    path: Path, title: str, description: str, keywords: Sequence[str], extra: dict
) -> list[str]:
    """Build the argfile lines. One argument per line, no quoting needed."""
    args = [
        "-overwrite_original",
        "-codedcharacterset=utf8",
        # Clear both keyword families first so a re-run can't stack duplicates.
        "-IPTC:Keywords=",
        "-XMP-dc:Subject=",
    ]

    if title:
        args += [
            f"-IPTC:ObjectName={title}",
            f"-XMP-dc:Title={title}",
            f"-EXIF:ImageDescription={title}",
        ]
    if description:
        args += [
            f"-IPTC:Caption-Abstract={description}",
            f"-XMP-dc:Description={description}",
        ]

    for kw in keywords:
        # Plain `=` on a list tag builds a fresh replacement list.
        args.append(f"-IPTC:Keywords={kw}")
        args.append(f"-XMP-dc:Subject={kw}")

    for tag, value in extra.items():
        if value:
            args.append(f"-{tag}={value}")

    args.append(str(path))
    return args


class ExifToolWriter:
    """Writes metadata by invoking the exiftool binary."""

    def __init__(self, exiftool: str, timeout: float = 120.0):
        self.exiftool = exiftool
        self.timeout = timeout

    def available(self) -> bool:
        try:
            result = subprocess.run(
                [self.exiftool, "-ver"], capture_output=True, text=True, timeout=20
            )
            return result.returncode == 0
        except Exception:
            return False

    def version(self) -> str:
        try:
            result = subprocess.run(
                [self.exiftool, "-ver"], capture_output=True, text=True, timeout=20
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        """Run exiftool with every argument passed through a UTF-8 argfile.

        The file *path* has to go through the argfile as well as the tag
        values. Verified: passing a path such as ``日本の桜-café.jpg`` as a
        command-line argument makes exiftool report "Invalid filename
        encoding / No matching files", because the path is mangled at the
        process boundary before exiftool ever sees it.
        """
        fd, argfile = tempfile.mkstemp(prefix="stockflow-exif-", suffix=".args", text=False)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(str(a) for a in args) + "\n")
            cmd = [
                self.exiftool,
                "-charset", "exiftool=UTF8",
                "-charset", "iptc=UTF8",
                "-charset", "filename=UTF8",
                "-@", argfile,
            ]
            try:
                return subprocess.run(
                    cmd, capture_output=True, encoding="utf-8", errors="replace",
                    timeout=self.timeout,
                )
            except FileNotFoundError as exc:
                raise MetadataWriteError(
                    f"exiftool not found at {self.exiftool!r}. Put exiftool.exe beside "
                    f"stockflow.py, or set STOCKFLOW_EXIFTOOL to its full path."
                ) from exc
        finally:
            try:
                os.unlink(argfile)
            except OSError:
                pass

    def write(
        self,
        path: Path,
        *,
        title: str,
        description: str,
        keywords: Sequence[str],
        **extra: str,
    ) -> None:
        args = _build_args(path, title, description, keywords, extra)
        try:
            result = self._run(args)
        except subprocess.TimeoutExpired as exc:
            raise MetadataWriteError(
                f"exiftool timed out after {self.timeout:.0f}s on {path.name}"
            ) from exc

        if result.returncode != 0:
            raise MetadataWriteError(
                f"exiftool failed on {path.name}: {(result.stderr or '').strip()}"
            )

        stderr = (result.stderr or "").strip()
        if stderr:
            # Warnings don't fail the write, but a truncation warning means the
            # file on disk no longer matches what we asked for.
            log.debug("exiftool warnings for %s: %s", path.name, stderr)
            if "exceeds length limit" in stderr:
                log.warning("%s: exiftool truncated an over-long metadata value", path.name)

    def copy_capture_metadata(self, source: Path, target: Path) -> None:
        """Carry camera EXIF from an original onto a derived JPEG.

        Marketplaces display capture date, camera body and lens, and buyers
        filter on them. A JPEG rendered from a RAW or HEIC starts with none of
        that unless it is copied across. ICC profiles are deliberately excluded:
        the derived file has already been converted to sRGB, so copying the
        source profile would mislabel it.
        """
        args = [
            "-overwrite_original",
            "-TagsFromFile", str(source),
            "-EXIF:All",
            "-XMP:All",
            "--XMP-dc:All",          # keep the metadata we are about to write
            "--ICC_Profile:All",     # derived file is already sRGB
            # Rotation is baked into the pixels, so the tag must say "normal".
            # The '#' forces a numeric write: plain `-Orientation=1` goes
            # through exiftool's human-readable conversion and silently lands
            # on 3 ("Rotate 180"), which makes viewers re-rotate a correct
            # image. Verified against exiftool 13.52.
            "-Orientation#=1",
            str(target),
        ]
        try:
            result = self._run(args)
            if result.returncode != 0:
                log.debug(
                    "Could not copy capture metadata %s -> %s: %s",
                    source.name, target.name, (result.stderr or "").strip(),
                )
        except Exception as exc:
            log.debug("Capture-metadata copy failed: %s", exc)

    def read_tags(self, path: Path, tags: Sequence[str]) -> dict:
        """Read tags back as UTF-8 JSON. Used by tests and verification."""
        import json

        args = ["-json", *[f"-{t}" for t in tags], str(path)]
        result = self._run(args)
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        try:
            data = json.loads(result.stdout)[0]
        except Exception:
            return {}

        # exiftool's JSON collapses a single-valued list tag to a bare string
        # and only emits a real list for two or more. Normalising here spares
        # every caller a str-or-list branch.
        for tag in ("Keywords", "Subject"):
            if isinstance(data.get(tag), str):
                data[tag] = [data[tag]]
        return data


class FakeMetadataWriter:
    """Records calls instead of touching files."""

    def __init__(self, fail_on: set[str] | None = None):
        self.written: list[dict] = []
        self.fail_on = fail_on or set()

    def available(self) -> bool:
        return True

    def version(self) -> str:
        return "fake"

    def write(self, path: Path, *, title: str, description: str,
              keywords: Sequence[str], **extra: str) -> None:
        if path.name in self.fail_on:
            raise MetadataWriteError(f"simulated exiftool failure on {path.name}")
        self.written.append(
            {
                "path": Path(path),
                "title": title,
                "description": description,
                "keywords": list(keywords),
                **extra,
            }
        )

    def copy_capture_metadata(self, source: Path, target: Path) -> None:
        return None
