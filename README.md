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

服务端口：

| 宿主机端口 | 用途 |
| --- | --- |
| `8080/tcp` | Web 界面和 API |
| `51413/tcp`、`51413/udp` | aria2 BitTorrent/DHT 入站端口 |

aria2 已启用 DHT、PEX 和 UPnP/NAT-PMP。若宿主机端口被占用，可在 `.env` 中修改 `ARIA2_P2P_PORT`，然后重新创建容器。Docker 映射端口和 aria2 监听端口会保持一致；RPC 端口仍只监听容器内回环地址，不对外暴露。

登录后在“完成后同步过滤”中可配置文件名正则和单文件大小范围（MiB）。整理时只同步同时满足条件的文件；未匹配文件会留在对应的 `/downloads/<任务 ID>` 暂存目录，任务标记为“已整理”并显示跳过数量。如果没有任何文件匹配，任务会标记为“无文件符合过滤条件”，修改规则后可再次点击“移动到整理目录”。清空全部过滤项即可恢复整任务整理。

下载任务的“删除任务”会停止 aria2 任务，并在确认框中默认勾选删除该任务的暂存文件；删除成功后任务记录也会从下载列表和数据库中移除。取消勾选则只删除任务记录、保留暂存文件；如果文件系统拒绝删除，任务会保留并显示错误，便于处理权限后重试。

Unraid 默认的 `nobody:users` 对应 UID/GID 通常为 `99:100`，镜像和 Compose 示例也采用这个默认值；其它 NAS 或普通 Linux 主机请在启动前设置匹配宿主机目录的 `PUID` 和 `PGID`。入口脚本会在 Docker 首次自动创建的空挂载目录上修正顶层属主，然后以该 UID/GID 降权运行；已有目录和文件不会被递归改权限。如果已有目录不可写，请在宿主机为对应 UID/GID 配置 ACL 或属主。

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

