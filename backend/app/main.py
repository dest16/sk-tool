import asyncio
import logging
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .aria2 import Aria2Client, Aria2Error, is_metadata_file
from .config import get_settings
from .db import Base, create_database
from .indexer import CATEGORY_OPTIONS, SORT_OPTIONS, IndexerError, SearchService, SukebeiAdapter
from .manager import DownloadManager
from .models import DownloadTask, Session, Setting, User
from .schemas import (
    ActionResponse,
    DownloadCreateRequest,
    DownloadListResponse,
    DownloadResponse,
    LoginRequest,
    ProxySettings,
    ProxySettingsResponse,
    SearchResponse,
    SetupRequest,
)
from .security import hash_password, new_token, session_for, token_hash, verify_password

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _serialize_files(task: DownloadTask, aria_files: list[dict] | None = None) -> list[dict]:
    if aria_files is not None:
        return [
            {
                "path": item.get("path", "").replace(str(task.staging_dir), "<暂存目录>"),
                "length": int(item.get("length") or 0),
                "completed_length": int(item.get("completedLength") or 0),
                "selected": item.get("selected") == "true",
            }
            for item in aria_files
            if not is_metadata_file(item)
        ]
    root = Path(task.staging_dir)
    if not root.exists():
        return []
    files = []
    for path in root.rglob("*"):
        if path.is_file() and not path.name.startswith(".aria2"):
            files.append({"path": str(path.relative_to(root)), "length": path.stat().st_size, "completed_length": path.stat().st_size, "selected": True})
    return files[:500]


def task_response(task: DownloadTask, aria_files: list[dict] | None = None) -> DownloadResponse:
    return DownloadResponse(
        id=task.id,
        gid=task.gid,
        title=task.title,
        status=task.status,
        auto_move=task.auto_move,
        total_bytes=task.total_bytes,
        completed_bytes=task.completed_bytes,
        download_speed=task.download_speed,
        eta_seconds=task.eta_seconds,
        error=task.error,
        files=_serialize_files(task, aria_files),
        created_at=task.created_at,
        completed_at=task.completed_at,
        moved_at=task.moved_at,
    )


settings = get_settings()
engine, session_factory = create_database(settings)
aria2 = Aria2Client(settings)
manager = DownloadManager(settings, session_factory, aria2)
search_service = SearchService(lambda proxy: SukebeiAdapter(settings.indexer_base_url, settings.request_timeout_seconds, proxy), settings.search_cache_seconds)
login_attempts: dict[str, list[float]] = defaultdict(list)


async def ensure_setup_token() -> None:
    async with session_factory() as session:
        exists = await session.scalar(select(func.count(User.id)))
    if not exists and not settings.setup_token_file.exists():
        settings.setup_token_file.write_text(new_token(24), encoding="utf-8")
        logger.warning("首次初始化令牌已生成，请从 %s 读取", settings.setup_token_file)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await ensure_setup_token()
    async with session_factory() as session:
        aria_proxy = await session.get(Setting, "aria2_proxy")
        aria2.proxy = aria_proxy.value if aria_proxy and aria_proxy.value else None
    try:
        await manager.start()
    except Exception:
        logger.exception("aria2 启动失败，Web 服务仍可用于配置检查")
        # Keep the web process alive and let the manager retry aria2 in the
        # background (for example while a mounted binary or port becomes ready).
        manager._poll_task = asyncio.create_task(manager.poll_loop())
    yield
    await manager.stop()
    await engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
frontend_dir = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dir.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")


async def db_session() -> AsyncSession:
    async with session_factory() as session:
        yield session


async def current_context(session_cookie: str | None, session: AsyncSession) -> tuple[User, Session] | None:
    if not session_cookie:
        return None
    db_session_obj = await session.scalar(select(Session).where(Session.token_hash == token_hash(session_cookie)))
    if not db_session_obj or db_session_obj.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        return None
    user = await session.get(User, db_session_obj.user_id)
    return (user, db_session_obj) if user else None


async def require_user(request: Request, session: AsyncSession = Depends(db_session), session_cookie: str | None = Cookie(default=None, alias="session")) -> tuple[User, Session]:
    context = await current_context(session_cookie, session)
    if not context:
        raise HTTPException(status_code=401, detail="请先登录")
    return context


async def require_write(
    request: Request,
    context=Depends(require_user),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
):
    if not csrf_token or not secrets.compare_digest(csrf_token, context[1].csrf_token):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    return context


@app.get("/api/health")
async def health():
    database_ok = True
    try:
        async with session_factory() as session:
            await session.scalar(select(func.count(User.id)))
    except Exception:
        database_ok = False
    aria_ok = False
    try:
        await aria2.call("aria2.getVersion", [])
        aria_ok = True
    except Exception:
        pass
    return {"ok": database_ok, "database": database_ok, "aria2": aria_ok, "downloads": settings.download_dir.exists(), "library": settings.library_dir.exists()}


@app.get("/api/setup/status")
async def setup_status(session: AsyncSession = Depends(db_session)):
    configured = bool(await session.scalar(select(func.count(User.id))))
    return {"configured": configured, "setup_required": not configured}


@app.post("/api/setup")
async def setup(payload: SetupRequest, response: Response, session: AsyncSession = Depends(db_session)):
    if await session.scalar(select(func.count(User.id))):
        raise HTTPException(status_code=409, detail="管理员已经初始化")
    try:
        expected = settings.setup_token_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="初始化令牌不存在")
    if not secrets.compare_digest(expected, payload.setup_token):
        raise HTTPException(status_code=403, detail="初始化令牌不正确")
    user = User(username=payload.username, password_hash=hash_password(payload.password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    session_token = new_token()
    db_session_obj, csrf = session_for(session_token, user.id, settings.session_days)
    session.add(db_session_obj)
    await session.commit()
    settings.setup_token_file.unlink(missing_ok=True)
    response.set_cookie("session", session_token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.session_days * 86400)
    return {"ok": True, "csrf_token": csrf, "username": user.username}


@app.post("/api/auth/login")
async def login(payload: LoginRequest, request: Request, response: Response, session: AsyncSession = Depends(db_session)):
    now = datetime.now(timezone.utc).timestamp()
    client = request.client.host if request.client else "unknown"
    login_attempts[client] = [value for value in login_attempts[client] if now - value < 300]
    if len(login_attempts[client]) >= 10:
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
    user = await session.scalar(select(User).where(User.username == payload.username))
    if not user or not verify_password(payload.password, user.password_hash):
        login_attempts[client].append(now)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    login_attempts[client].clear()
    session_token = new_token()
    db_session_obj, csrf = session_for(session_token, user.id, settings.session_days)
    session.add(db_session_obj)
    await session.commit()
    response.set_cookie("session", session_token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.session_days * 86400)
    return {"ok": True, "csrf_token": csrf, "username": user.username}


@app.post("/api/auth/logout")
async def logout(response: Response, context=Depends(require_write), session: AsyncSession = Depends(db_session), session_cookie: str | None = Cookie(default=None, alias="session")):
    if session_cookie:
        await session.execute(delete(Session).where(Session.token_hash == token_hash(session_cookie)))
        await session.commit()
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(context=Depends(require_user)):
    return {"username": context[0].username, "csrf_token": context[1].csrf_token}


@app.get("/api/search", response_model=SearchResponse)
async def search(
    q: str = Query(default="", max_length=200),
    category: str = Query(default="0_0"),
    page: int = Query(default=1, ge=1, le=10000),
    sort: str = Query(default=""),
    order: str = Query(default="desc"),
    context=Depends(require_user),
    session: AsyncSession = Depends(db_session),
):
    proxy_setting = await session.get(Setting, "indexer_proxy")
    try:
        return await search_service.search(q, category, page, sort, order, proxy_setting.value if proxy_setting else None)
    except IndexerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/meta")
async def meta(context=Depends(require_user)):
    return {"categories": CATEGORY_OPTIONS, "sorts": SORT_OPTIONS, "statuses": {"waiting": "排队中", "metadata": "获取元数据", "downloading": "下载中", "paused": "已暂停", "completed_pending_move": "完成待整理", "moving": "整理中", "moved": "已整理", "conflict": "名称冲突", "failed": "失败", "cancelled": "已取消"}}


@app.get("/api/downloads", response_model=DownloadListResponse)
async def downloads(context=Depends(require_user), aria: Aria2Client = Depends(lambda: aria2)):
    async with session_factory() as session:
        rows = await session.execute(select(DownloadTask).order_by(DownloadTask.created_at.desc()))
        tasks = list(rows.scalars())
    items = []
    for task in tasks:
        files = None
        if task.gid and task.status in {"waiting", "metadata", "downloading", "paused"}:
            try:
                files = (await aria.status(task.gid)).get("files")
            except Exception:
                pass
        items.append(task_response(task, files))
    return DownloadListResponse(items=items)


@app.post("/api/downloads", response_model=DownloadResponse)
async def create_download(payload: DownloadCreateRequest, context=Depends(require_write)):
    from .indexer import btih_from_magnet

    if not btih_from_magnet(payload.magnet_uri):
        raise HTTPException(status_code=422, detail="只接受合法的 BTIH magnet 链接")
    try:
        task = await manager.create(payload.title, payload.magnet_uri, payload.source_url, payload.auto_move)
    except (Aria2Error, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return task_response(task)


@app.post("/api/downloads/{task_id}/{action}", response_model=ActionResponse)
async def download_action(task_id: str, action: str, context=Depends(require_write)):
    if action not in {"pause", "resume", "cancel", "retry", "move", "cleanup"}:
        raise HTTPException(status_code=404, detail="不支持的操作")
    try:
        task = await manager.action(task_id, action)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, Aria2Error, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ActionResponse(task=task_response(task))


@app.delete("/api/downloads/{task_id}/history")
async def delete_history(task_id: str, context=Depends(require_write), session: AsyncSession = Depends(db_session)):
    task = await session.get(DownloadTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in {"moved", "failed", "cancelled", "conflict", "completed_pending_move"}:
        raise HTTPException(status_code=409, detail="活动任务不能删除历史")
    await session.delete(task)
    await session.commit()
    return {"ok": True}


@app.get("/api/settings/proxy", response_model=ProxySettingsResponse)
async def get_proxy(context=Depends(require_user), session: AsyncSession = Depends(db_session)):
    values = {}
    for key in ("indexer_proxy", "aria2_proxy"):
        setting = await session.get(Setting, key)
        # Never return proxy credentials to the browser. The UI can replace a
        # configured proxy by entering a complete new URL.
        values[key] = None
        values[f"{key}_configured"] = bool(setting and setting.value)
    return ProxySettingsResponse(**values)


@app.put("/api/settings/proxy", response_model=ProxySettingsResponse)
async def put_proxy(payload: ProxySettings, context=Depends(require_write), session: AsyncSession = Depends(db_session)):
    previous_aria_proxy = (await session.get(Setting, "aria2_proxy"))
    previous_aria_proxy = previous_aria_proxy.value if previous_aria_proxy else None
    for key in payload.model_fields_set:
        value = getattr(payload, key)
        setting = await session.get(Setting, key)
        if setting:
            setting.value = value or ""
        else:
            session.add(Setting(key=key, value=value or ""))
    await session.commit()
    # aria2 proxy is applied on restart; clear the cached search adapter immediately.
    search_service.cache.clear()
    aria_changed = "aria2_proxy" in payload.model_fields_set
    if aria_changed and previous_aria_proxy != payload.aria2_proxy:
        aria2.proxy = payload.aria2_proxy
        try:
            await aria2.stop()
            await aria2.start()
        except Exception:
            logger.exception("应用 aria2 代理设置失败")
    values = {}
    for key in ("indexer_proxy", "aria2_proxy"):
        setting = await session.get(Setting, key)
        values[f"{key}_configured"] = bool(setting and setting.value)
    return ProxySettingsResponse(
        **values,
    )


@app.get("/{path:path}")
async def spa(path: str):
    if path.startswith("api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    index = frontend_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"detail": "前端尚未构建，请运行 npm run build"}, status_code=404)

