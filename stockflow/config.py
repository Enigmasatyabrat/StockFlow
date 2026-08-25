"""Settings resolution.

Precedence, highest first: CLI flag > environment variable > config file > default.

`load_settings` takes a plain dict rather than an argparse.Namespace so it can
be unit-tested without building a parser.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

VERSION = "5.0.0"

#: Repository root -- the directory containing the `stockflow/` package.
#: NOT `Path(__file__).parent`, which is the package dir; exiftool.exe lives
#: one level up, beside the shim.
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent

DEFAULT_MODEL = "gemini-2.5-flash-lite"

# Formats PIL handles natively.
BASE_IMAGE_TYPES = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"})
HEIF_IMAGE_TYPES = frozenset({".heic", ".heif"})
RAW_IMAGE_TYPES = frozenset(
    {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng",
     ".orf", ".raf", ".rw2", ".pef", ".raw"}
)


@dataclass(frozen=True)
class Settings:
    """Every knob, as a field rather than a module global."""

    folder: Path

    # Model / API
    model: str = DEFAULT_MODEL
    api_key: str = ""
    rpm: int | None = None          # None -> take from limits.for_model
    rpd: int | None = None
    max_retries: int = 5

    # Batch shape
    batch_limit: int = 50
    workers: int = 3

    # Thresholds
    min_score: int = 60
    min_megapixels: float = 4.0
    max_file_size_mb: float = 50.0
    max_attempts: int = 3
    api_max_edge: int = 1024
    phash_threshold: int = 6
    #: Bounds the whole output filename, extension included. Defaults to the
    #: stricter of the two marketplaces: Adobe Stock requires 30 characters or
    #: fewer and Shutterstock documents no limit, so one set of files uploads
    #: to both. Raise it if you only submit to Shutterstock.
    max_filename_chars: int = 30

    # Local quality gates. Each None means "measure and report, never gate".
    # Gating locally saves quota on hopeless images but risks rejecting
    # legitimately soft-focus work, so it is opt-in by default.
    min_blur: float | None = None
    max_noise: float | None = None
    max_clipping: float | None = None

    # Behaviour
    dry_run: bool = False
    no_move: bool = False
    retry_failed: bool = False
    force_fresh_registry: bool = False
    verbose: bool = False
    quiet: bool = False

    # Tools
    exiftool_path: str = ""

    # Derived / injected
    image_types: frozenset[str] = BASE_IMAGE_TYPES

    def with_(self, **kw: Any) -> "Settings":
        return replace(self, **kw)

    @property
    def work_dir(self) -> Path:
        from .models import WORK_DIRNAME

        return self.folder / WORK_DIRNAME

    @property
    def reports_dir(self) -> Path:
        from .models import FOLDER_REPORTS

        return self.folder / FOLDER_REPORTS


def find_exiftool(explicit: str = "") -> str:
    """Locate the exiftool binary.

    Order: explicit path > STOCKFLOW_EXIFTOOL env > repo root > PATH.

    The repo-root lookup matters: v4 used ``Path(__file__).parent``, which
    now resolves to ``stockflow/`` and would miss the ``exiftool.exe`` sitting
    beside the shim.
    """
    if explicit:
        return explicit
    env = os.environ.get("STOCKFLOW_EXIFTOOL", "").strip()
    if env:
        return env
    exe = "exiftool.exe" if os.name == "nt" else "exiftool"
    local = REPO_ROOT / exe
    if local.exists():
        return str(local)
    return exe


def _coerce(value: Any, target: Any) -> Any:
    """Convert a config-file/env string into the type the default implies."""
    if value is None:
        return None
    if isinstance(target, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(target, int) and not isinstance(target, bool):
        return int(value)
    if isinstance(target, float):
        return float(value)
    return value


# Environment variable name -> Settings field.
_ENV_MAP = {
    "GEMINI_MODEL": "model",
    "GEMINI_API_KEY": "api_key",
    "STOCKFLOW_WORKERS": "workers",
    "STOCKFLOW_MIN_SCORE": "min_score",
    "STOCKFLOW_MIN_MEGAPIXELS": "min_megapixels",
    "STOCKFLOW_BATCH_LIMIT": "batch_limit",
    "STOCKFLOW_RPM": "rpm",
    "STOCKFLOW_RPD": "rpd",
}


def load_settings(
    cli: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> Settings:
    """Resolve settings from all sources. ``cli['folder']`` is required."""
    cli = {k: v for k, v in (cli or {}).items() if v is not None}
    env = os.environ if env is None else env

    folder = cli.get("folder")
    if folder is None:
        raise ConfigError("No photo folder given.")
    folder = Path(folder).expanduser().resolve()

    values: dict[str, Any] = {"folder": folder}
    defaults = Settings(folder=folder)

    # 3. config file (lowest of the three real sources)
    if config_path is None:
        candidate = folder / "stockflow.json"
        config_path = candidate if candidate.exists() else None
    if config_path is not None:
        try:
            raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigError(f"Could not read config file {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"Config file {config_path} must contain a JSON object.")
        for key, val in raw.items():
            if hasattr(defaults, key) and key != "folder":
                values[key] = _coerce(val, getattr(defaults, key))

    # 2. environment
    for env_name, field_name in _ENV_MAP.items():
        raw_env = env.get(env_name)
        if raw_env not in (None, ""):
            try:
                values[field_name] = _coerce(raw_env, getattr(defaults, field_name))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{env_name}={raw_env!r} is not valid: {exc}") from exc

    # 1. CLI wins
    for key, val in cli.items():
        if key == "folder":
            continue
        if hasattr(defaults, key):
            values[key] = val

    values["exiftool_path"] = find_exiftool(values.get("exiftool_path", ""))
    values["image_types"] = _resolve_image_types()

    settings = Settings(**values)
    _validate(settings)
    return settings


def _resolve_image_types() -> frozenset[str]:
    """Extensions we can actually decode in this environment."""
    types = set(BASE_IMAGE_TYPES)
    from .imaging import loader

    if loader.has_heif_support():
        types |= HEIF_IMAGE_TYPES
    if loader.has_raw_support():
        types |= RAW_IMAGE_TYPES
    return frozenset(types)


def _validate(s: Settings) -> None:
    if not s.folder.is_dir():
        raise ConfigError(f"Folder not found: {s.folder}")
    if s.workers < 1:
        raise ConfigError("--workers must be at least 1.")
    if not 0 <= s.min_score <= 100:
        raise ConfigError("--min-score must be between 0 and 100.")
    if s.min_megapixels < 0:
        raise ConfigError("--min-megapixels cannot be negative.")
    if s.batch_limit < 1:
        raise ConfigError("--limit must be at least 1.")
    if s.quiet and s.verbose:
        raise ConfigError("--quiet and --verbose are mutually exclusive.")
    if not 0 <= s.phash_threshold <= 64:
        raise ConfigError("--phash-threshold must be between 0 and 64.")
    if s.max_filename_chars < 8:
        raise ConfigError("--max-filename must be at least 8.")
