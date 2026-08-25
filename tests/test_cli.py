"""CLI argument handling and exit codes."""

from __future__ import annotations

import pytest

from stockflow.cli import build_parser, main


class TestParser:
    def test_folder_is_positional(self):
        assert build_parser().parse_args(["D:/photos"]).folder == "D:/photos"

    def test_flags_map_to_settings_fields(self):
        args = build_parser().parse_args(
            ["f", "--limit", "10", "--workers", "2", "--min-score", "80",
             "--min-megapixels", "6", "--model", "gemini-x"]
        )
        assert args.batch_limit == 10
        assert args.workers == 2
        assert args.min_score == 80
        assert args.min_megapixels == 6.0
        assert args.model == "gemini-x"

    def test_unset_flags_are_none_so_they_do_not_override(self):
        """Anything not passed must stay None, otherwise argparse defaults
        would silently outrank the env var and config file."""
        args = build_parser().parse_args(["f"])
        assert args.min_score is None
        assert args.dry_run is None
        assert args.workers is None

    def test_boolean_flags(self):
        args = build_parser().parse_args(["f", "--dry-run", "--no-move", "--retry-failed"])
        assert args.dry_run and args.no_move and args.retry_failed

    def test_quality_gate_flags(self):
        args = build_parser().parse_args(
            ["f", "--min-blur", "120", "--max-noise", "6", "--max-clipping", "0.05"]
        )
        assert args.min_blur == 120.0
        assert args.max_noise == 6.0
        assert args.max_clipping == 0.05

    def test_verbose_and_quiet_shorthands(self):
        assert build_parser().parse_args(["f", "-v"]).verbose is True
        assert build_parser().parse_args(["f", "-q"]).quiet is True


class TestExitCodes:
    def test_no_folder_prints_help(self, capsys):
        assert main([]) == 2
        assert "usage" in capsys.readouterr().out.lower()

    def test_missing_folder_is_a_config_error(self, tmp_path, capsys):
        assert main([str(tmp_path / "nope")]) == 2
        assert "Folder not found" in capsys.readouterr().err

    def test_missing_api_key_is_reported_clearly(self, photo_folder, monkeypatch, capsys):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        code = main([str(photo_folder)])
        assert code == 4
        assert "GEMINI_API_KEY" in capsys.readouterr().err

    def test_dry_run_needs_no_api_key(self, photo_folder, monkeypatch, capsys):
        """The whole point of --dry-run is inspecting a folder before
        committing to anything, including before setting up credentials."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert main([str(photo_folder), "--dry-run"]) == 0

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "StockFlow" in capsys.readouterr().out


class TestDryRunEndToEnd:
    def test_reports_without_touching_anything(self, photo_folder, make_image,
                                               monkeypatch, capsys):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        make_image("photo.jpg", folder=photo_folder)
        before = sorted(p.name for p in photo_folder.iterdir())

        assert main([str(photo_folder), "--dry-run", "--min-megapixels", "1"]) == 0

        assert sorted(p.name for p in photo_folder.iterdir()) == before
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "would analyse" in out

    def test_banner_shows_format_support(self, photo_folder, monkeypatch, capsys):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        main([str(photo_folder), "--dry-run"])
        assert "Formats" in capsys.readouterr().out


class TestConsoleSafety:
    def test_console_reconfiguration_is_harmless(self):
        """On a cp1252 Windows console an unguarded print of a CJK filename
        raises UnicodeEncodeError and kills the whole run."""
        from stockflow.cli import make_console_safe

        make_console_safe()
        make_console_safe()  # idempotent
