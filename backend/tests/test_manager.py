from pathlib import Path

from app.aria2 import Aria2Error
from app.manager import DownloadManager
from app.models import DownloadTask


class _Result:
    def __init__(self, items):
        self.items = items

    def scalars(self):
        return iter(self.items)


class _Session:
    def __init__(self, task):
        self.task = task

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        return _Result([self.task])


class _SessionFactory:
    def __init__(self, task):
        self.task = task

    def __call__(self):
        return _Session(self.task)


class _Aria2:
    process = object()

    def __init__(self, response, remove_error: Exception | None = None):
        self.response = response
        self.remove_error = remove_error
        self.status_calls = []
        self.removed = []

    async def status(self, gid):
        self.status_calls.append(gid)
        return self.response

    async def remove_download_result(self, gid):
        self.removed.append(gid)
        if self.remove_error:
            raise self.remove_error


class _ActionSession:
    def __init__(self, task, factory):
        self.task = task
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, model, task_id):
        return self.task if task_id == self.task.id else None

    async def commit(self):
        return None

    async def refresh(self, task):
        return None

    async def delete(self, task):
        self.factory.deleted.append(task.id)


class _ActionSessionFactory:
    def __init__(self, task):
        self.task = task
        self.deleted = []

    def __call__(self):
        return _ActionSession(self.task, self)


class _ActionAria2:
    def __init__(self):
        self.removed = []

    async def status(self, gid):
        return {}

    async def remove(self, gid):
        self.removed.append(gid)


def _task(tmp_path: Path, gid: str = "metadata-gid") -> DownloadTask:
    return DownloadTask(
        id="task-id",
        gid=gid,
        title="测试任务",
        magnet_uri="magnet:?xt=urn:btih:" + "a" * 40,
        status="metadata",
        staging_dir=str(tmp_path / "staging"),
        auto_move=False,
    )


async def _poll(manager: DownloadManager, task: DownloadTask):
    async def update(task_id: str, **updates):
        assert task_id == task.id
        for key, value in updates.items():
            setattr(task, key, value)
        return task

    manager._update = update  # type: ignore[method-assign]
    await manager.poll_once()


async def test_metadata_completion_follows_child_gid(tmp_path: Path):
    task = _task(tmp_path)
    aria2 = _Aria2(
        {
            "status": "complete",
            "totalLength": "546",
            "completedLength": "546",
            "downloadSpeed": "0",
            "followedBy": ["torrent-gid"],
        }
    )
    manager = DownloadManager(object(), _SessionFactory(task), aria2)

    await _poll(manager, task)

    assert task.gid == "torrent-gid"
    assert task.status == "waiting"
    assert task.total_bytes == 0
    assert aria2.removed == ["metadata-gid"]


async def test_metadata_file_size_is_not_reported_as_payload_size(tmp_path: Path):
    task = _task(tmp_path)
    aria2 = _Aria2(
        {
            "status": "active",
            "totalLength": "546500",
            "completedLength": "498500",
            "downloadSpeed": "12000",
            "files": [{"path": str(tmp_path / "staging" / "[METADATA]resource.torrent")}],
        }
    )
    manager = DownloadManager(object(), _SessionFactory(task), aria2)

    await _poll(manager, task)

    assert task.status == "metadata"
    assert task.total_bytes == 0
    assert task.completed_bytes == 0
    assert task.download_speed == 0
    assert task.eta_seconds is None


async def test_completed_download_is_saved_even_if_result_cleanup_returns_400(tmp_path: Path):
    task = _task(tmp_path, gid="torrent-gid")
    aria2 = _Aria2(
        {
            "status": "complete",
            "totalLength": "100",
            "completedLength": "100",
            "downloadSpeed": "0",
        },
        remove_error=Aria2Error("aria2 RPC HTTP 400：invalid request", status_code=400),
    )
    manager = DownloadManager(object(), _SessionFactory(task), aria2)

    await _poll(manager, task)

    assert task.status == "completed_pending_move"
    assert task.completed_bytes == 100
    assert aria2.removed == ["torrent-gid"]


async def test_cancel_deletes_staging_files_by_default(tmp_path: Path):
    task = _task(tmp_path, gid="active-gid")
    staging = Path(task.staging_dir)
    staging.mkdir(parents=True)
    (staging / "partial.bin").write_bytes(b"partial")
    settings = type("Settings", (), {"download_dir": tmp_path, "library_dir": tmp_path / "library"})()
    aria2 = _ActionAria2()
    session_factory = _ActionSessionFactory(task)
    manager = DownloadManager(settings, session_factory, aria2)

    result = await manager.action(task.id, "cancel")

    assert result.status == "cancelled"
    assert aria2.removed == ["active-gid"]
    assert not staging.exists()
    assert session_factory.deleted == [task.id]


async def test_cancel_can_keep_staging_files_when_explicitly_requested(tmp_path: Path):
    task = _task(tmp_path, gid="active-gid")
    staging = Path(task.staging_dir)
    staging.mkdir(parents=True)
    (staging / "partial.bin").write_bytes(b"partial")
    settings = type("Settings", (), {"download_dir": tmp_path, "library_dir": tmp_path / "library"})()
    session_factory = _ActionSessionFactory(task)
    manager = DownloadManager(settings, session_factory, _ActionAria2())

    result = await manager.action(task.id, "cancel", delete_files=False)

    assert result.status == "cancelled"
    assert staging.exists()
    assert result.error == "任务已删除，暂存文件已保留"
    assert session_factory.deleted == [task.id]




async def test_delete_staging_accepts_a_single_file(tmp_path: Path):
    file_path = tmp_path / "video.mkv"
    file_path.write_bytes(b"partial")
    settings = type("Settings", (), {"download_dir": tmp_path, "library_dir": tmp_path / "library"})()
    manager = DownloadManager(settings, _SessionFactory(_task(tmp_path)), _ActionAria2())

    assert await manager._delete_staging(str(file_path)) is None
    assert not file_path.exists()
