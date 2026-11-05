"""Tests of the files of the server in the cache directory of the user."""

import pytest

from kicad_workbench.utils import user_files


def test_write_atomic(tmp_path):
    file = tmp_path / "a" / "state.json"
    user_files.write_atomic(file, "one")
    user_files.write_atomic(file, "two")
    assert file.read_text(encoding="utf-8") == "two"
    assert [p.name for p in file.parent.iterdir()] == ["state.json"]


def test_write_error_keeps_the_old_file(tmp_path):
    """An error at the write (here a text that UTF-8 cannot encode) removes the
    temporary file, and the old file stays."""
    file = tmp_path / "state.json"
    user_files.write_atomic(file, "old")
    with pytest.raises(UnicodeEncodeError):
        user_files.write_atomic(file, "\ud800")
    assert file.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_replace_error_removes_the_temporary_file(tmp_path, monkeypatch):
    file = tmp_path / "state.json"

    def replace(source, target):
        raise PermissionError("locked")

    monkeypatch.setattr(user_files.os, "replace", replace)
    with pytest.raises(PermissionError):
        user_files.write_atomic(file, "new")
    assert list(tmp_path.iterdir()) == []
