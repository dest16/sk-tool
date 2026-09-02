import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select

from .files import InvalidFilterError, MoveConflictError, NoMatchingFilesError, UnsafePathError, move_download
from .models import DownloadTask, Setting, utc_now
from .transmission import TransmissionClient, TransmissionError, is_metadata_file

logger = logging.getLogger(__name__)

TERMINAL = {"moved", "failed", "cancelled", "conflict", "filtered"}


class DownloadManager:
    def __init__(self, settings, session_factory, downloader: TransmissionClient):
        self.settings = settings
        self.session_factory = session_factory
        self.downloader = downloader
        self._poll_task: asyncio.Task | None = None
        self._move_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        self.settings.download_dir.mkdir(parents=True, exist_ok=True)
        self.settings.library_dir.mkdir(parents=True, exist_ok=True)
        await self.downloader.start()
        self._poll_task = asyncio.create_task(self.poll_loop())

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        await self.downloader.stop()

    async def create(self, title: str, magnet_uri: str, source_url: str | None, auto_move: bool) -> DownloadTask:
        task_id = str(uuid.uuid4())
        staging = self.settings.download_dir / task_id
        staging.mkdir(parents=True, exist_ok=False)
        try:
            gid = await self.downloader.add_magnet(magnet_uri, staging)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        task = DownloadTask(
            id=task_id,
            gid=gid,
            title=title,
            magnet_uri=magnet_uri,
            source_url=source_url,
            status="waiting",
            staging_dir=str(staging),
            auto_move=auto_move,
        )
        async with self.session_factory() as session:
            session.add(task)
            await session.commit()
            await session.refresh(task)
        return task

    async def get(self, task_id: str) -> DownloadTask | None:
        async with self.session_factory() as session:
            return await session.get(DownloadTask, task_id)

    async def action(self, task_id: str, action: str, *, delete_files: bool = True) -> DownloadTask:
        task = await self.get(task_id)
        if not task:
            raise KeyError("任务不存在")
        cancel_delete_error: str | None = None
        if action == "pause" and task.gid:
            await self.downloader.pause(task.gid)
            task.status = "paused"
        elif action == "resume" and task.gid:
            await self.downloader.resume(task.gid)
            task.status = "waiting"
        elif action == "cancel":
            if task.status == "moving":
                raise ValueError("整理进行中，暂时不能删除任务")
            if task.gid and task.status not in {"completed_pending_move", "conflict", "filtered"}:
                await self.downloader.remove(task.gid)
            task.status = "cancelled"
            if delete_files:
                cancel_delete_error = await self._delete_staging(task.staging_dir)
                task.error = cancel_delete_error
            else:
                task.error = "任务已删除，暂存文件已保留"
        elif action == "retry":
            if task.status not in {"failed", "cancelled"}:
                raise ValueError("只有失败或已取消的任务可以重试")
            staging = Path(task.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            task.gid = await self.downloader.add_magnet(task.magnet_uri, staging)
            task.status = "waiting"
            task.error = None
        elif action == "cleanup":
            if task.status not in {"failed", "cancelled", "filtered"}:
                raise ValueError("只有失败、已取消或被过滤的任务可以清理暂存文件")
            cleanup_error = await self._delete_staging(task.staging_dir)
            if cleanup_error:
                raise OSError(cleanup_error)
            task.error = "暂存文件已清理"
        elif action == "move":
            await self.move(task)
        else:
            raise ValueError("不支持的任务操作")
        async with self.session_factory() as session:
            db_task = await session.get(DownloadTask, task_id)
            if db_task:
                if action == "cancel" and cancel_delete_error is None:
                    # Cancellation is a destructive delete operation: once
                    # The downloader is stopped and the optional staging cleanup has
                    # succeeded, remove the task row as well so it no longer
                    # appears in the task list.  The detached object is still
                    # returned to the API caller for a final acknowledgement.
                    await session.delete(db_task)
                    await session.commit()
                    return task
                for key, value in task.__dict__.items():
                    if key not in {"_sa_instance_state", "id"}:
                        setattr(db_task, key, value)
                db_task.updated_at = utc_now()
                await session.commit()
                await session.refresh(db_task)
                return db_task
        return task

    async def move(self, task: DownloadTask) -> None:
        lock = self._move_locks.setdefault(task.id, asyncio.Lock())
        async with lock:
            if task.status not in {"completed_pending_move", "conflict", "filtered"}:
                raise ValueError("任务尚未完成，不能整理")
            task.status = "moving"
            skipped: list[str] = []
            try:
                filename_regex, min_size_bytes, max_size_bytes = await self._sync_filters()
                await asyncio.to_thread(
                    move_download,
                    Path(task.staging_dir),
                    self.settings.library_dir,
                    task.title,
                    task.id,
                    self.settings.download_dir,
                    filename_regex=filename_regex,
                    min_size_bytes=min_size_bytes,
                    max_size_bytes=max_size_bytes,
                    skipped=skipped,
                )
            except NoMatchingFilesError as exc:
                task.status = "filtered"
                task.error = str(exc)
                return
            except InvalidFilterError as exc:
                task.status = "failed"
                task.error = f"整理过滤规则无效：{exc}"
                return
            except MoveConflictError as exc:
                task.status = "conflict"
                task.error = str(exc)
                return
            except (UnsafePathError, FileNotFoundError, OSError) as exc:
                task.status = "failed"
                task.error = f"整理失败：{exc}"
                return
            task.status = "moved"
            task.error = f"已同步，跳过 {len(skipped)} 个不符合过滤条件的文件" if skipped else None
            task.moved_at = utc_now()

    async def _sync_filters(self) -> tuple[str | None, int | None, int | None]:
        async with self.session_factory() as session:
            values: dict[str, str] = {}
            for key in ("sync_filename_regex", "sync_min_size_bytes", "sync_max_size_bytes"):
                setting = await session.get(Setting, key)
                values[key] = setting.value if setting else ""
        filename_regex = values["sync_filename_regex"] or None
        sizes: list[int | None] = []
        for key in ("sync_min_size_bytes", "sync_max_size_bytes"):
            raw = values[key].strip()
            if not raw:
                sizes.append(None)
                continue
            try:
                parsed = int(raw)
            except ValueError as exc:
                raise InvalidFilterError(f"{key} 不是有效的整数") from exc
            sizes.append(parsed)
        return filename_regex, sizes[0], sizes[1]

    async def _delete_staging(self, staging_dir: str) -> str | None:
        """Delete one task staging directory after validating its boundary."""
        staging = Path(staging_dir)
        root = self.settings.download_dir.resolve()
        try:
            if staging.is_symlink():
                return "暂存目录不允许是符号链接"
            resolved = staging.resolve()
            if resolved == root or not resolved.is_relative_to(root):
                return "暂存目录不在允许范围内"
            if resolved.exists() and not resolved.is_dir():
                return "暂存路径不是目录"
            if resolved.exists():
                await asyncio.to_thread(shutil.rmtree, resolved)
        except OSError as exc:
            return f"暂存文件删除失败：{exc}"
        return None

    async def poll_loop(self) -> None:
        while True:
            try:
                if not self.downloader.process or self.downloader.process.returncode is not None:
                    try:
                        await self.downloader.start()
                    except Exception:
                        logger.exception("Transmission 尚未就绪，稍后重试")
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("下载任务轮询失败")
            await asyncio.sleep(2)

    async def poll_once(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(select(DownloadTask).where(DownloadTask.status.not_in(TERMINAL)))
            tasks = list(result.scalars())
        for task in tasks:
            if not task.gid:
                continue
            if task.status in {"completed_pending_move", "moving"}:
                if task.status == "moving":
                    task.status = "completed_pending_move"
                    await self._update(task.id, status="completed_pending_move")
                if task.auto_move:
                    await self.move(task)
                    await self._save(task)
                continue
            try:
                status = await self.downloader.status(task.gid)
            except TransmissionError as exc:
                if task.status not in {"paused", "cancelled"}:
                    await self._update(task.id, error=str(exc))
                continue
            state = status.get("status", "")
            aria_files = status.get("files") or []
            metadata_only = bool(aria_files) and all(is_metadata_file(item) for item in aria_files)
            updates = {
                "total_bytes": int(status.get("totalLength") or 0),
                "completed_bytes": int(status.get("completedLength") or 0),
                "download_speed": float(status.get("downloadSpeed") or 0),
                "error": status.get("errorMessage") or None,
            }
            if metadata_only:
                # A downloader may expose temporary metadata as a regular file.
                # Its size is not the size of the requested payload.
                updates.update(total_bytes=0, completed_bytes=0, download_speed=0, eta_seconds=None)
            remaining = updates["total_bytes"] - updates["completed_bytes"]
            updates["eta_seconds"] = int(remaining / updates["download_speed"]) if remaining > 0 and updates["download_speed"] > 0 else None
            if state == "active":
                updates["status"] = "metadata" if metadata_only or not updates["total_bytes"] else "downloading"
            elif state == "waiting":
                updates["status"] = "metadata" if metadata_only else "waiting"
            elif state == "paused":
                updates["status"] = "paused"
            elif state == "complete":
                followed_by = status.get("followedBy") or []
                next_gid = next((str(gid) for gid in followed_by if gid and str(gid) != task.gid), None)
                if next_gid:
                    # A magnet URI is represented by a metadata download first;
                    # Keep compatibility with adapters that represent magnet
                    # metadata and payload as two linked tasks.
                    previous_gid = task.gid
                    await self._update(
                        task.id,
                        gid=next_gid,
                        status="waiting",
                        total_bytes=0,
                        completed_bytes=0,
                        download_speed=0,
                        eta_seconds=None,
                        error=None,
                    )
                    await self._remove_result_quietly(previous_gid)
                    continue
                updates.update({"status": "completed_pending_move", "completed_at": utc_now(), "download_speed": 0})
                updated = await self._update(task.id, **updates)
                await self._remove_result_quietly(task.gid)
                if updated and updated.status == "completed_pending_move" and updated.auto_move:
                    await self.move(updated)
                    await self._save(updated)
                continue
            elif state in {"error", "removed"}:
                updates["status"] = "failed" if state == "error" else "cancelled"
            updated = await self._update(task.id, **updates)
            if updated and updated.status == "completed_pending_move" and updated.auto_move:
                await self.move(updated)
                await self._save(updated)

    async def _remove_result_quietly(self, gid: str | None) -> None:
        if not gid:
            return
        try:
            await self.downloader.remove_download_result(gid)
        except TransmissionError as exc:
            # A completed metadata/result identifier can disappear while the
            # downloader is creating its child download. It is safe to continue because the
            # payload is already complete and no longer needs to seed.
            logger.warning("清理已完成下载任务 %s 失败，将继续处理：%s", gid, exc)

    async def _update(self, task_id: str, **updates) -> DownloadTask | None:
        async with self.session_factory() as session:
            task = await session.get(DownloadTask, task_id)
            if not task:
                return None
            for key, value in updates.items():
                setattr(task, key, value)
            task.updated_at = utc_now()
            await session.commit()
            await session.refresh(task)
            return task

    async def _save(self, task: DownloadTask) -> None:
        await self._update(task.id, **{k: v for k, v in task.__dict__.items() if k not in {"_sa_instance_state", "id"}})


