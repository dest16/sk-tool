import os
import re
import shutil
import unicodedata
from pathlib import Path


class MoveConflictError(RuntimeError):
    pass


class UnsafePathError(RuntimeError):
    pass


class NoMatchingFilesError(RuntimeError):
    """Raised when a sync filter excludes every file in a completed task."""


class InvalidFilterError(ValueError):
    """Raised when a configured filename or size filter is invalid."""


def safe_name(value: str, fallback: str = "未命名") -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    value = re.sub(r'[<>:"/\\|?*]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if value in {"", ".", ".."}:
        value = fallback
    if value.casefold().split(".")[0] in {"con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "lpt1", "lpt2", "lpt3"}:
        value = f"_{value}"
    return value[:180]


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_no_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise UnsafePathError("不允许处理符号链接")
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*dirs, *files]:
            item = current_path / name
            if item.is_symlink():
                raise UnsafePathError("下载内容包含符号链接，已拒绝整理")


def _count_and_size(root: Path) -> tuple[int, int]:
    if root.is_file():
        return 1, root.stat().st_size
    count = 0
    size = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise UnsafePathError("不允许校验符号链接")
        if path.is_file():
            count += 1
            size += path.stat().st_size
    return count, size


def _copy_checked(source: Path, destination: Path) -> None:
    ensure_no_symlinks(source)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    if _count_and_size(source) != _count_and_size(destination):
        raise IOError("复制校验失败")


def _move_download_unfiltered(staging: Path, library: Path, title: str, job_id: str, staging_root: Path | None = None) -> str:
    # Check the link itself before resolving: resolving first would make a symlinked
    # staging directory appear to be a legitimate directory outside the download root.
    if staging.is_symlink() or library.is_symlink():
        raise UnsafePathError("不允许处理符号链接目录")
    staging = staging.resolve()
    library = library.resolve()
    if staging_root is not None and not within(staging, staging_root):
        raise UnsafePathError("下载暂存目录越出允许范围")
    if staging_root is not None and staging == staging_root.resolve():
        raise UnsafePathError("暂存目录必须是下载根目录下的任务目录")
    if not staging.exists() or not staging.is_dir():
        raise UnsafePathError("下载暂存目录无效")
    library.mkdir(parents=True, exist_ok=True)
    if not within(library, library.parent):
        raise UnsafePathError("整理目录无效")
    ensure_no_symlinks(staging)
    entries = sorted(staging.iterdir(), key=lambda p: p.name.casefold())
    entries = [entry for entry in entries if not entry.name.startswith(".aria2")]
    if not entries:
        raise FileNotFoundError("下载目录为空")
    if len(entries) == 1:
        final_name = safe_name(entries[0].name)
        source = entries[0]
        wrapper = False
    else:
        final_name = safe_name(title)
        source = staging
        wrapper = True
    destination = library / final_name
    if destination.exists() or destination.is_symlink():
        raise MoveConflictError(f"目标已存在：{final_name}")
    if final_name.startswith(".sukebei-moving-"):
        final_name = f"_{final_name}"
        destination = library / final_name
    temp = library / f".sukebei-moving-{job_id}"
    if temp.exists():
        raise MoveConflictError("检测到未完成的整理临时目录，请先处理")
    target_names: dict[str, str] = {}
    if wrapper:
        for entry in entries:
            target_name = safe_name(entry.name)
            if target_name in target_names.values():
                raise MoveConflictError("文件名清理后发生重名冲突")
            target_names[entry.name] = target_name
    same_device = os.stat(source).st_dev == os.stat(library).st_dev
    try:
        if wrapper:
            temp.mkdir()
            for entry in entries:
                target = temp / target_names[entry.name]
                if same_device:
                    os.replace(entry, target)
                else:
                    _copy_checked(entry, target)
        else:
            if same_device:
                os.replace(source, temp)
            else:
                _copy_checked(source, temp)
        os.replace(temp, destination)
        if not same_device:
            if wrapper:
                for entry in entries:
                    if entry.exists():
                        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            else:
                shutil.rmtree(source) if source.is_dir() else source.unlink()
        if staging.exists() and not any(staging.iterdir()):
            staging.rmdir()
        return final_name
    except Exception:
        # Same-device renames may have moved the source before a later rename
        # failed. Restore it rather than deleting user data.
        if same_device and temp.exists():
            try:
                if wrapper:
                    for item in list(temp.iterdir()):
                        original_name = next((name for name, target in target_names.items() if target == item.name), item.name)
                        os.replace(item, staging / original_name)
                    temp.rmdir()
                elif not source.exists():
                    os.replace(temp, source)
            except OSError:
                pass
        if temp.exists():
            shutil.rmtree(temp) if temp.is_dir() else temp.unlink()
        raise


def _iter_regular_files(root: Path):
    if root.is_file():
        if not root.name.startswith(".aria2"):
            yield root
        return
    for path in root.rglob("*"):
        if path.is_symlink():
            raise UnsafePathError("不允许处理符号链接")
        if path.is_file() and not path.name.startswith(".aria2"):
            yield path


def _remove_empty_dirs(root: Path) -> None:
    if not root.exists() or not root.is_dir() or root.is_symlink():
        return
    directories = sorted((path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()), key=lambda path: len(path.parts), reverse=True)
    for path in directories:
        try:
            path.rmdir()
        except OSError:
            pass


def _move_download_filtered(
    staging: Path,
    library: Path,
    title: str,
    job_id: str,
    staging_root: Path | None,
    filename_regex: str | None,
    min_size_bytes: int | None,
    max_size_bytes: int | None,
    skipped: list[str] | None,
) -> str:
    if filename_regex and len(filename_regex) > 200:
        raise InvalidFilterError("文件名正则不能超过 200 个字符")
    try:
        pattern = re.compile(filename_regex) if filename_regex else None
    except re.error as exc:
        raise InvalidFilterError(f"文件名正则无效：{exc}") from exc
    if min_size_bytes is not None and min_size_bytes < 0:
        raise InvalidFilterError("最小文件大小不能为负数")
    if max_size_bytes is not None and max_size_bytes < 0:
        raise InvalidFilterError("最大文件大小不能为负数")
    if min_size_bytes is not None and max_size_bytes is not None and min_size_bytes > max_size_bytes:
        raise InvalidFilterError("最小文件大小不能大于最大文件大小")

    # Check the link itself before resolving; resolving first would make a symlinked
    # staging directory appear to be a legitimate directory outside the download root.
    if staging.is_symlink() or library.is_symlink():
        raise UnsafePathError("不允许处理符号链接目录")
    staging = staging.resolve()
    library = library.resolve()
    if staging_root is not None and not within(staging, staging_root):
        raise UnsafePathError("下载暂存目录越出允许范围")
    if staging_root is not None and staging == staging_root.resolve():
        raise UnsafePathError("暂存目录必须是下载根目录下的任务目录")
    if not staging.exists() or not staging.is_dir():
        raise UnsafePathError("下载暂存目录无效")
    ensure_no_symlinks(staging)
    entries = sorted((entry for entry in staging.iterdir() if not entry.name.startswith(".aria2")), key=lambda p: p.name.casefold())
    if not entries:
        raise FileNotFoundError("下载目录为空")

    all_files = sorted(_iter_regular_files(staging), key=lambda path: path.relative_to(staging).as_posix().casefold())
    selected: list[Path] = []
    skipped_paths: list[Path] = []
    for path in all_files:
        size = path.stat().st_size
        matches_name = pattern.search(path.name) is not None if pattern else True
        matches_min = min_size_bytes is None or size >= min_size_bytes
        matches_max = max_size_bytes is None or size <= max_size_bytes
        if matches_name and matches_min and matches_max:
            selected.append(path)
        else:
            skipped_paths.append(path)
    if skipped is not None:
        skipped.extend(path.relative_to(staging).as_posix() for path in skipped_paths)
    if not selected:
        raise NoMatchingFilesError("没有文件符合当前同步过滤条件，文件仍保留在暂存目录")

    library.mkdir(parents=True, exist_ok=True)
    if not within(library, library.parent):
        raise UnsafePathError("整理目录无效")

    single_entry = len(entries) == 1
    single_file = single_entry and entries[0].is_file()
    wrapper = not single_entry
    final_name = safe_name(entries[0].name if single_entry else title)
    if final_name.startswith(".sukebei-moving-"):
        final_name = f"_{final_name}"
    destination = library / final_name
    if destination.exists() or destination.is_symlink():
        raise MoveConflictError(f"目标已存在：{final_name}")
    temp = library / f".sukebei-moving-{job_id}"
    if temp.exists():
        raise MoveConflictError("检测到未完成的整理临时目录，请先处理")

    source_root = staging if wrapper else entries[0]
    target_paths: dict[Path, Path] = {}
    used_targets: set[str] = set()
    for source in selected:
        relative = source.relative_to(staging if wrapper else source_root)
        if wrapper:
            parts = list(relative.parts)
            parts[0] = safe_name(parts[0])
            target_relative = Path(*parts)
        elif single_file:
            target_relative = Path(safe_name(source.name))
        else:
            target_relative = relative
        target_key = target_relative.as_posix().casefold()
        if target_key in used_targets:
            raise MoveConflictError("文件名清理后发生重名冲突")
        used_targets.add(target_key)
        target_paths[source] = target_relative

    expected_count = len(selected)
    expected_size = sum(path.stat().st_size for path in selected)
    same_device = os.stat(staging).st_dev == os.stat(library).st_dev
    temp_is_file = single_file
    try:
        if temp_is_file:
            source = selected[0]
            if same_device:
                os.replace(source, temp)
            else:
                _copy_checked(source, temp)
        else:
            temp.mkdir()
            for source, relative in target_paths.items():
                target = temp / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if same_device:
                    os.replace(source, target)
                else:
                    _copy_checked(source, target)
        if _count_and_size(temp) != (expected_count, expected_size):
            raise IOError("复制校验失败")
        os.replace(temp, destination)
        if not same_device:
            for source in selected:
                if source.exists():
                    source.unlink()
        _remove_empty_dirs(staging)
        if staging.exists() and not any(staging.iterdir()):
            staging.rmdir()
        return final_name
    except Exception:
        # Same-device renames may have moved files before a later operation
        # failed. Restore them to the staging tree while leaving skipped files
        # untouched.
        if same_device and temp.exists():
            try:
                if temp_is_file:
                    if not selected[0].exists():
                        os.replace(temp, selected[0])
                else:
                    for source, relative in target_paths.items():
                        moved = temp / relative
                        if moved.exists():
                            source.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(moved, source)
                    _remove_empty_dirs(temp)
                    temp.rmdir()
            except OSError:
                pass
        if temp.exists():
            shutil.rmtree(temp) if temp.is_dir() else temp.unlink()
        raise


def move_download(
    staging: Path,
    library: Path,
    title: str,
    job_id: str,
    staging_root: Path | None = None,
    *,
    filename_regex: str | None = None,
    min_size_bytes: int | None = None,
    max_size_bytes: int | None = None,
    skipped: list[str] | None = None,
) -> str:
    """Move a completed task, optionally selecting files by name and size.

    The unfiltered path preserves the original atomic top-level move behavior.
    When a filter is configured, matching regular files are moved into an
    atomic temporary tree; non-matching files remain in the task staging
    directory for inspection or a later retry.
    """
    if not filename_regex and min_size_bytes is None and max_size_bytes is None:
        return _move_download_unfiltered(staging, library, title, job_id, staging_root)
    return _move_download_filtered(
        staging,
        library,
        title,
        job_id,
        staging_root,
        filename_regex,
        min_size_bytes,
        max_size_bytes,
        skipped,
    )

