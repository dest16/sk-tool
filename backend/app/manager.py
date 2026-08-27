import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select

from .aria2 import Aria2Client, Aria2Error
from .files import MoveConflictError, UnsafePathError, move_download
from .models import DownloadTask, utc_now

logger = logging.getLogger(__name__)

TERMINAL = {"moved", "failed", "cancelled", "conflict"}


class DownloadManager:
    def __init__(self, settings, session_factory, aria2: Aria2Client):
        self.settings = settings
        self.session_factory = session_factory
        self.aria2 = aria2
        self._poll_task: asyncio.Task | None = None
        self._move_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        self.settings.download_dir.mkdir(parents=True, exist_ok=True)
        self.settings.library_dir.mkdir(parents=True, exist_ok=True)
        await self.aria2.start()
        self._poll_task = asyncio.create_task(self.poll_loop())

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        await self.aria2.stop()

    async def create(self, title: str, magnet_uri: str, source_url: str | None, auto_move: bool) -> DownloadTask:
        task_id = str(uuid.uuid4())
        staging = self.settings.download_dir / task_id
        staging.mkdir(parents=True, exist_ok=False)
        try:
            gid = await self.aria2.add_magnet(magnet_uri, staging)
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

    async def action(self, task_id: str, action: str) -> DownloadTask:
        task = await self.get(task_id)
        if not task:
            raise KeyError("任务不存在")
        if action == "pause" and task.gid:
            await self.aria2.pause(task.gid)
            task.status = "paused"
        elif action == "resume" and task.gid:
            await self.aria2.resume(task.gid)
            task.status = "waiting"
        elif action == "cancel":
            if task.gid:
                await self.aria2.remove(task.gid)
            task.status = "cancelled"
        elif action == "retry":
            if task.status not in {"failed", "cancelled"}:
                raise ValueError("只有失败或已取消的任务可以重试")
            staging = Path(task.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            task.gid = await self.aria2.add_magnet(task.magnet_uri, staging)
            task.status = "waiting"
            task.error = None
        elif action == "cleanup":
            if task.status not in {"failed", "cancelled"}:
                raise ValueError("只有失败或已取消的任务可以清理暂存文件")
            staging = Path(task.staging_dir)
            root = self.settings.download_dir.resolve()
            if staging.is_symlink() or staging.resolve() == root or not staging.resolve().is_relative_to(root):
                raise ValueError("暂存目录不在允许范围内")
            if staging.exists():
                await asyncio.to_thread(shutil.rmtree, staging)
            task.error = "暂存文件已清理"
        elif action == "move":
            await self.move(task)
        else:
            raise ValueError("不支持的任务操作")
        async with self.session_factory() as session:
            db_task = await session.get(DownloadTask, task_id)
            if db_task:
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
            if task.status not in {"completed_pending_move", "conflict"}:
                raise ValueError("任务尚未完成，不能整理")
            task.status = "moving"
            try:
                await asyncio.to_thread(
                    move_download,
                    Path(task.staging_dir),
                    self.settings.library_dir,
                    task.title,
                    task.id,
                    self.settings.download_dir,
                )
            except MoveConflictError as exc:
                task.status = "conflict"
                task.error = str(exc)
                return
            except (UnsafePathError, FileNotFoundError, OSError) as exc:
                task.status = "failed"
                task.error = f"整理失败：{exc}"
                return
            task.status = "moved"
            task.error = None
            task.moved_at = utc_now()

    async def poll_loop(self) -> None:
        while True:
            try:
                if not self.aria2.process or self.aria2.process.returncode is not None:
                    try:
                        await self.aria2.start()
                    except Exception:
                        logger.exception("aria2 尚未就绪，稍后重试")
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
                status = await self.aria2.status(task.gid)
            except Aria2Error as exc:
                if task.status not in {"paused", "cancelled"}:
                    await self._update(task.id, error=str(exc))
                continue
            state = status.get("status", "")
            updates = {
                "total_bytes": int(status.get("totalLength") or 0),
                "completed_bytes": int(status.get("completedLength") or 0),
                "download_speed": float(status.get("downloadSpeed") or 0),
                "error": status.get("errorMessage") or None,
            }
            remaining = updates["total_bytes"] - updates["completed_bytes"]
            updates["eta_seconds"] = int(remaining / updates["download_speed"]) if remaining > 0 and updates["download_speed"] > 0 else None
            if state == "active":
                updates["status"] = "metadata" if not updates["total_bytes"] else "downloading"
            elif state == "waiting":
                updates["status"] = "waiting"
            elif state == "paused":
                updates["status"] = "paused"
            elif state == "complete":
                updates.update({"status": "completed_pending_move", "completed_at": utc_now(), "download_speed": 0})
                await self.aria2.remove(task.gid)
            elif state in {"error", "removed"}:
                updates["status"] = "failed" if state == "error" else "cancelled"
            updated = await self._update(task.id, **updates)
            if updated and updated.status == "completed_pending_move" and updated.auto_move:
                await self.move(updated)
                await self._save(updated)

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

