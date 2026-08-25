"""Settings resolution and precedence."""

from __future__ import annotations

import json

import pytest

from stockflow.config import Settings, find_exiftool, load_settings
from stockflow.errors import ConfigError


class TestPrecedence:
    def test_defaults(self, photo_folder):
        s = load_settings({"folder": str(photo_folder)}, env={})
        assert s.min_score == 60
        assert s.workers == 3
        assert s.model == "gemini-2.5-flash-lite"

    def test_env_beats_default(self, photo_folder):
        s = load_settings({"folder": str(photo_folder)}, env={"STOCKFLOW_MIN_SCORE": "75"})
        assert s.min_score == 75

    def test_cli_beats_env(self, photo_folder):
        s = load_settings(
            {"folder": str(photo_folder), "min_score": 90},
            env={"STOCKFLOW_MIN_SCORE": "75"},
        )
        assert s.min_score == 90

    def test_config_file_read(self, photo_folder):
        (photo_folder / "stockflow.json").write_text(
            json.dumps({"min_score": 42, "workers": 7}), encoding="utf-8"
        )
        s = load_settings({"folder": str(photo_folder)}, env={})
        assert s.min_score == 42 and s.workers == 7

    def test_env_beats_config_file(self, photo_folder):
        (photo_folder / "stockflow.json").write_text(
            json.dumps({"min_score": 42}), encoding="utf-8"
        )
        s = load_settings({"folder": str(photo_folder)}, env={"STOCKFLOW_MIN_SCORE": "75"})
        assert s.min_score == 75

    def test_api_key_from_env(self, photo_folder):
        s = load_settings({"folder": str(photo_folder)}, env={"GEMINI_API_KEY": "secret"})
        assert s.api_key == "secret"

    def test_empty_env_var_ignored(self, photo_folder):
        s = load_settings({"folder": str(photo_folder)}, env={"STOCKFLOW_MIN_SCORE": ""})
        assert s.min_score == 60


class TestValidation:
    def test_missing_folder_argument(self):
        with pytest.raises(ConfigError, match="No photo folder"):
            load_settings({}, env={})

    def test_nonexistent_folder(self, tmp_path):
        with pytest.raises(ConfigError, match="Folder not found"):
            load_settings({"folder": str(tmp_path / "nope")}, env={})

    @pytest.mark.parametrize(
        "override,message",
        [
            ({"workers": 0}, "workers"),
            ({"min_score": 200}, "min-score"),
            ({"batch_limit": 0}, "limit"),
            ({"min_megapixels": -1}, "megapixels"),
            ({"phash_threshold": 99}, "phash"),
            ({"quiet": True, "verbose": True}, "mutually exclusive"),
        ],
    )
    def test_rejects_bad_values(self, photo_folder, override, message):
        with pytest.raises(ConfigError, match=message):
            load_settings({"folder": str(photo_folder), **override}, env={})

    def test_bad_env_value_is_reported_clearly(self, photo_folder):
        with pytest.raises(ConfigError, match="STOCKFLOW_MIN_SCORE"):
            load_settings({"folder": str(photo_folder)}, env={"STOCKFLOW_MIN_SCORE": "abc"})

    def test_broken_config_file_is_reported(self, photo_folder):
        (photo_folder / "stockflow.json").write_text("{nope", encoding="utf-8")
        with pytest.raises(ConfigError, match="config file"):
            load_settings({"folder": str(photo_folder)}, env={})


class TestExiftoolDiscovery:
    def test_explicit_path_wins(self):
        assert find_exiftool("/custom/exiftool") == "/custom/exiftool"

    def test_env_var_used(self, monkeypatch):
        monkeypatch.setenv("STOCKFLOW_EXIFTOOL", "/from/env")
        assert find_exiftool() == "/from/env"

    def test_finds_binary_in_repo_root(self):
        """The binary sits beside the shim, not inside the package. v4 anchored
        on the module's own directory, which now resolves to stockflow/."""
        found = find_exiftool()
        assert "exiftool" in found.lower()


class TestSettingsHelpers:
    def test_with_returns_a_copy(self, settings):
        modified = settings.with_(min_score=99)
        assert modified.min_score == 99
        assert settings.min_score != 99

    def test_derived_paths(self, settings):
        assert settings.work_dir.name == ".stockflow_work"
        assert settings.reports_dir.name == "Reports"

    def test_image_types_reflect_installed_codecs(self, settings):
        assert ".jpg" in settings.image_types
        assert ".tif" in settings.image_types
