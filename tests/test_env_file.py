import os

from flighttracker.web import load_env_file


def test_env_file_reads_values_and_keeps_existing(tmp_path, monkeypatch):
    f = tmp_path / "flight-tracker.env"
    # Windows PowerShell writes a byte-order mark; passwords may contain '='.
    f.write_text("﻿FT_PASSWORD=a=b c\n# comment\n\nFT_PORT=9000\nFT_PROVIDER=fake\n", encoding="utf-8")
    # setenv first so monkeypatch restores (removes) them after the test.
    monkeypatch.setenv("FT_PASSWORD", "x")
    monkeypatch.setenv("FT_PORT", "x")
    monkeypatch.delenv("FT_PASSWORD")
    monkeypatch.delenv("FT_PORT")
    monkeypatch.setenv("FT_PROVIDER", "google-flights")
    load_env_file(f)
    assert os.environ["FT_PASSWORD"] == "a=b c"
    assert os.environ["FT_PORT"] == "9000"
    assert os.environ["FT_PROVIDER"] == "google-flights"  # already set, not overwritten


def test_missing_env_file_is_fine(tmp_path):
    load_env_file(tmp_path / "nope.env")
