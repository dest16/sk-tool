import os
import re
import shutil
import unicodedata
from pathlib import Path


class MoveConflictError(RuntimeError):
    pass


class UnsafePathError(RuntimeError):
    pass


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


def move_download(staging: Path, library: Path, title: str, job_id: str, staging_root: Path | None = None) -> str:
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

