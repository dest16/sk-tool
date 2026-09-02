import os
from pathlib import Path

import pytest

from app.files import MoveConflictError, NoMatchingFilesError, UnsafePathError, move_download, safe_name


def test_safe_name():
    assert safe_name("  hello/../world?.mkv  ") == "hello_.._world_.mkv"
    assert safe_name("...") == "未命名"


def test_move_single_file(tmp_path: Path):
    staging = tmp_path / "downloads" / "job"
    library = tmp_path / "library"
    staging.mkdir(parents=True)
    (staging / "clip.txt").write_text("ok")
    assert move_download(staging, library, "title", "job") == "clip.txt"
    assert (library / "clip.txt").read_text() == "ok"
    assert not staging.exists()


def test_move_direct_download_file(tmp_path: Path):
    staging = tmp_path / "downloads" / "clip.txt"
    library = tmp_path / "library"
    staging.parent.mkdir(parents=True)
    staging.write_text("ok")

    assert move_download(staging, library, "title", "job", tmp_path / "downloads") == "clip.txt"
    assert (library / "clip.txt").read_text() == "ok"
    assert not staging.exists()


def test_move_multiple_files_uses_title_folder(tmp_path: Path):
    staging = tmp_path / "downloads" / "job"
    library = tmp_path / "library"
    staging.mkdir(parents=True)
    (staging / "a.txt").write_text("a")
    (staging / "b.txt").write_text("b")
    assert move_download(staging, library, "a / b", "job") == "a _ b"
    assert (library / "a _ b" / "a.txt").exists()


def test_conflict_does_not_overwrite(tmp_path: Path):
    staging = tmp_path / "downloads" / "job"
    library = tmp_path / "library"
    staging.mkdir(parents=True)
    library.mkdir()
    (staging / "clip.txt").write_text("new")
    (library / "clip.txt").write_text("old")
    with pytest.raises(MoveConflictError):
        move_download(staging, library, "title", "job")
    assert (library / "clip.txt").read_text() == "old"


def test_symlink_is_rejected(tmp_path: Path):
    staging = tmp_path / "downloads" / "job"
    library = tmp_path / "library"
    staging.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    try:
        (staging / "link").symlink_to(outside)
    except OSError:
        if os.name == "nt":
            pytest.skip("当前 Windows 测试环境没有创建符号链接的权限")
        raise
    with pytest.raises(UnsafePathError):
        move_download(staging, library, "title", "job")


def test_filter_moves_matching_files_and_leaves_skipped_files(tmp_path: Path):
    staging = tmp_path / "downloads" / "job"
    library = tmp_path / "library"
    staging.mkdir(parents=True)
    (staging / "movie.mkv").write_bytes(b"12345")
    (staging / "sample.txt").write_bytes(b"x")
    skipped: list[str] = []

    assert move_download(staging, library, "title", "job", filename_regex=r"\.mkv$", skipped=skipped) == "title"
    assert (library / "title" / "movie.mkv").read_bytes() == b"12345"
    assert (staging / "sample.txt").read_bytes() == b"x"
    assert skipped == ["sample.txt"]


def test_filter_applies_size_bounds(tmp_path: Path):
    staging = tmp_path / "downloads" / "job"
    library = tmp_path / "library"
    staging.mkdir(parents=True)
    (staging / "small.bin").write_bytes(b"1")
    (staging / "large.bin").write_bytes(b"1234")

    move_download(staging, library, "title", "job", min_size_bytes=2, max_size_bytes=4)

    assert (library / "title" / "large.bin").exists()
    assert (staging / "small.bin").exists()


def test_filter_with_no_matches_keeps_staging(tmp_path: Path):
    staging = tmp_path / "downloads" / "job"
    library = tmp_path / "library"
    staging.mkdir(parents=True)
    (staging / "clip.txt").write_text("ok")

    with pytest.raises(NoMatchingFilesError):
        move_download(staging, library, "title", "job", filename_regex=r"\.mkv$")
    assert (staging / "clip.txt").exists()
    assert not library.exists()


def test_filter_rejects_invalid_regex_and_range(tmp_path: Path):
    staging = tmp_path / "downloads" / "job"
    library = tmp_path / "library"
    staging.mkdir(parents=True)
    (staging / "clip.txt").write_text("ok")

    with pytest.raises(ValueError):
        move_download(staging, library, "title", "job", filename_regex="[")
    with pytest.raises(ValueError):
        move_download(staging, library, "title", "job", min_size_bytes=10, max_size_bytes=1)

