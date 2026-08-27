# Sukebei 下载管理器

一个单容器、自托管的简体中文 Web 应用：搜索 Sukebei Nyaa、将选中的磁力链接交给内置 aria2 下载，并在完成后整理到固定目录。

## 快速启动

```bash
mkdir -p data/config data/downloads data/library
docker compose up -d --build
docker compose logs -f sukebei-manager
```

第一次启动后，从日志提示的 `/config/setup-token`（宿主机为 `data/config/setup-token`）读取一次性令牌，在 `http://localhost:8080` 创建管理员账号。初始化后令牌会被删除。

宿主机目录映射：

| 容器路径 | 用途 |
| --- | --- |
| `/config` | SQLite、aria2 会话和初始化状态 |
| `/downloads` | 每个任务的下载暂存目录 |
| `/library` | 下载完成后的唯一整理目录 |

如果 NAS 使用其它 UID/GID，可在启动前设置 `PUID` 和 `PGID`。挂载目录必须已经允许该用户读写。

## 开发

后端：

```bash
python -m venv .venv
.venv\\Scripts\\activate       # Linux/macOS: source .venv/bin/activate
pip install -r backend/requirements-dev.txt
uvicorn app.main:app --app-dir backend --reload
```

前端：

```bash
cd frontend
npm install
npm run dev
```

运行测试：

```bash
pytest -q
```

目标站点页面结构由 `backend/app/indexer.py` 的独立适配器处理。发布前应使用当前站点响应生成脱敏 fixture，并运行解析器回归测试；页面结构变化会以明确错误呈现，而不会创建残缺任务。

## 安全边界

- 只有管理员会话可以搜索、创建和控制任务。
- aria2 RPC 只监听容器回环地址，且使用随机密钥。
- 只接受合法 BTIH magnet；整理操作拒绝路径穿越、符号链接和目标覆盖。
- 站点和 aria2 代理可在设置中分别配置，凭据不会出现在 API 响应或日志中。
- 建议通过 HTTPS 反向代理暴露服务，并遵守目标站点、内容来源和所在地区的适用法律及服务条款。


