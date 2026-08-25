"""Exact and perceptual hashing, plus the dedupe index."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 of the file bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def phash_hex(path: Path) -> str | None:
    """Perceptual hash for near-duplicate detection.

    Returns None rather than raising: this is a nice-to-have signal and is
    never worth failing a run over.
    """
    try:
        import imagehash

        from .imaging import loader

        with loader.open_image(path, srgb=False) as img:
            return str(imagehash.phash(img))
    except Exception as exc:
        log.debug("pHash failed for %s: %s", path, exc)
        return None


def hamming(a: str, b: str) -> int | None:
    """Hamming distance between two hex pHashes, or None if either is unusable."""
    try:
        import imagehash

        return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)
    except Exception:
        return None


class DedupeIndex:
    """Exact and near-duplicate lookup that persists across runs.

    v4 rebuilt ``seen_hashes``/``seen_phashes`` empty on every run, so a
    byte-identical file submitted in a later batch was never caught -- the
    SHA-256 was written into the registry but never read back. This index is
    seeded from the registry, so duplicate detection finally spans runs.
    """

    def __init__(self, threshold: int = 6):
        self.threshold = threshold
        self._by_sha: dict[str, str] = {}
        self._phashes: dict[str, str] = {}

    @classmethod
    def from_registry(cls, registry: "object", threshold: int = 6) -> "DedupeIndex":
        idx = cls(threshold)
        index = getattr(registry, "index", None) or {}
        idx._by_sha = dict(index.get("sha256", {}))
        idx._phashes = dict(index.get("phash", {}))
        return idx

    def exact_match(self, sha: str) -> str | None:
        """Filename of an earlier file with identical bytes, if any."""
        return self._by_sha.get(sha)

    def near_match(self, phash: str | None) -> tuple[str, int] | None:
        """(filename, distance) of a visually similar earlier file, if any.

        Purely informational -- near-duplicates are logged, never auto-moved.
        Two frames from a burst, or a slightly different crop, are often both
        independently worth submitting, and silently binning one would be a
        far worse error than mentioning both.
        """
        if not phash:
            return None
        best: tuple[str, int] | None = None
        for name, other in self._phashes.items():
            dist = hamming(phash, other)
            if dist is None:
                continue
            if dist <= self.threshold and (best is None or dist < best[1]):
                best = (name, dist)
        return best

    def add(self, name: str, sha: str | None, phash: str | None) -> None:
        if sha:
            self._by_sha.setdefault(sha, name)
        if phash:
            self._phashes[name] = phash

    def as_index(self) -> dict:
        return {"sha256": dict(self._by_sha), "phash": dict(self._phashes)}

    def __len__(self) -> int:
        return len(self._by_sha)
