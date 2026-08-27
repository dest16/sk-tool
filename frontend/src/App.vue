<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";

type Result = {
  result_id: string; title: string; category: string; size_text: string; published_at: string | null;
  seeders: number; leechers: number; completed: number; magnet_uri: string; details_url?: string | null;
};
type Download = {
  id: string; title: string; status: string; auto_move: boolean; total_bytes: number; completed_bytes: number;
  download_speed: number; eta_seconds: number | null; error: string | null; files: { path: string }[];
  created_at: string; completed_at: string | null; moved_at: string | null;
};

const busy = ref(true);
const setupRequired = ref(false);
const loggedIn = ref(false);
const username = ref("");
const csrf = ref("");
const notice = ref("");
const error = ref("");
const query = ref("");
const results = ref<Result[]>([]);
const downloads = ref<Download[]>([]);
const selected = ref<Result | null>(null);
const searchPage = ref(1);
const hasNext = ref(false);
const categories = ref<Record<string, string>>({ "0_0": "全部分类" });
const sorts = ref<Record<string, string>>({ "": "默认" });
const filters = reactive({ category: "0_0", sort: "", order: "desc" });
const setup = reactive({ token: "", username: "admin", password: "" });
const login = reactive({ username: "", password: "" });
const moveOnComplete = ref(true);
const proxy = reactive({ indexer_proxy: "", aria2_proxy: "" });
const proxyConfigured = reactive({ indexer_proxy: false, aria2_proxy: false });
const syncFilters = reactive({ filename_regex: "", min_size_mib: "", max_size_mib: "" });
const pendingCancelTask = ref<Download | null>(null);
const deleteFilesOnCancel = ref(true);
let pollTimer: number | undefined;

const statusLabels: Record<string, string> = {
  waiting: "排队中", metadata: "获取元数据", downloading: "下载中", paused: "已暂停", completed_pending_move: "完成待整理",
  moving: "整理中", moved: "已整理", conflict: "名称冲突", filtered: "无文件符合过滤条件", failed: "失败", cancelled: "已删除",
};
const activeDownloads = computed(() => downloads.value.filter((item) => !["moved", "failed", "cancelled", "filtered"].includes(item.status)));

async function api(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (csrf.value && ["POST", "PUT", "DELETE"].includes((init.method || "GET").toUpperCase())) headers.set("X-CSRF-Token", csrf.value);
  const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "请求失败");
  return data;
}

async function bootstrap() {
  busy.value = true; error.value = "";
  try {
    const status = await api("/api/setup/status");
    setupRequired.value = status.setup_required;
    if (!setupRequired.value) {
      try {
        const me = await api("/api/auth/me");
        loggedIn.value = true; username.value = me.username; csrf.value = me.csrf_token;
        await loadMeta(); await refreshDownloads(); await loadProxy(); await loadFilters();
      } catch { loggedIn.value = false; }
    }
  } catch (err) { error.value = (err as Error).message; }
  busy.value = false;
}

async function loadMeta() { const data = await api("/api/meta"); categories.value = data.categories; sorts.value = data.sorts; }
async function loadProxy() { const data = await api("/api/settings/proxy"); proxy.indexer_proxy = data.indexer_proxy || ""; proxy.aria2_proxy = data.aria2_proxy || ""; proxyConfigured.indexer_proxy = data.indexer_proxy_configured; proxyConfigured.aria2_proxy = data.aria2_proxy_configured; }
function formatMiB(value: number | null) { if (value === null || value === undefined) return ""; const mib = value / 1024 / 1024; return String(Math.round(mib * 100) / 100); }
async function loadFilters() { const data = await api("/api/settings/filters"); syncFilters.filename_regex = data.filename_regex || ""; syncFilters.min_size_mib = formatMiB(data.min_size_bytes); syncFilters.max_size_mib = formatMiB(data.max_size_bytes); }
async function doSetup() {
  error.value = "";
  try { const data = await api("/api/setup", { method: "POST", body: JSON.stringify({ setup_token: setup.token, username: setup.username, password: setup.password }) });
    loggedIn.value = true; setupRequired.value = false; username.value = data.username; csrf.value = data.csrf_token; await loadMeta(); await loadFilters(); notice.value = "初始化成功";
  } catch (err) { error.value = (err as Error).message; }
}
async function doLogin() {
  error.value = "";
  try { const data = await api("/api/auth/login", { method: "POST", body: JSON.stringify(login) }); loggedIn.value = true; username.value = data.username; csrf.value = data.csrf_token; await loadMeta(); await refreshDownloads(); await loadProxy(); await loadFilters(); }
  catch (err) { error.value = (err as Error).message; }
}
async function doLogout() { try { await api("/api/auth/logout", { method: "POST" }); } finally { loggedIn.value = false; csrf.value = ""; stopPolling(); } }

async function search(reset = true) {
  if (!query.value.trim()) { results.value = []; return; }
  if (reset) searchPage.value = 1;
  error.value = "";
  try { const params = new URLSearchParams({ q: query.value, category: filters.category, page: String(searchPage.value), sort: filters.sort, order: filters.order }); const data = await api(`/api/search?${params}`); results.value = reset ? data.items : [...results.value, ...data.items]; hasNext.value = data.has_next; }
  catch (err) { error.value = (err as Error).message; }
}
async function nextPage() { if (!hasNext.value) return; searchPage.value += 1; await search(false); }
async function refreshDownloads() { try { downloads.value = (await api("/api/downloads")).items; } catch (err) { error.value = (err as Error).message; } }
async function createDownload() {
  if (!selected.value) return;
  try { await api("/api/downloads", { method: "POST", body: JSON.stringify({ magnet_uri: selected.value.magnet_uri, title: selected.value.title, source_url: selected.value.details_url, auto_move: moveOnComplete.value }) }); selected.value = null; notice.value = "已加入下载队列"; await refreshDownloads(); startPolling(); }
  catch (err) { error.value = (err as Error).message; }
}
async function taskAction(task: Download, action: string) {
  if (action === "cancel") { pendingCancelTask.value = task; deleteFilesOnCancel.value = true; return; }
  if (["move", "cleanup"].includes(action) && !window.confirm(action === "cleanup" ? "确认删除该任务的暂存文件吗？此操作不可恢复。" : "确认将文件移动到整理目录吗？")) return;
  await sendTaskAction(task, action);
}
async function sendTaskAction(task: Download, action: string, body: Record<string, unknown> = {}) {
  try {
    const data = await api(`/api/downloads/${task.id}/${action}`, { method: "POST", body: JSON.stringify(body) });
    const deletionFailed = action === "cancel" && body.delete_files === true && Boolean(data.task?.error);
    if (action === "cancel" && !deletionFailed) {
      // The backend removes the row as part of a successful cancellation;
      // remove it locally immediately instead of briefly rendering the
      // acknowledgement object returned by the action endpoint.
      downloads.value = downloads.value.filter((item) => item.id !== task.id);
    } else {
      const index = downloads.value.findIndex((item) => item.id === task.id);
      if (index >= 0 && data.task) downloads.value[index] = data.task;
    }
    await refreshDownloads();
    return data.task || null;
  } catch (err) {
    error.value = (err as Error).message;
    return null;
  }
}
async function confirmCancel() {
  const task = pendingCancelTask.value;
  if (!task) return;
  const deleteFiles = deleteFilesOnCancel.value;
  pendingCancelTask.value = null;
  const result = await sendTaskAction(task, "cancel", { delete_files: deleteFiles });
  if (!result) return;
  if (deleteFiles && result.error) { error.value = result.error; return; }
  notice.value = deleteFiles ? "任务和暂存文件已删除" : "任务已删除，暂存文件已保留";
}
async function deleteHistory(task: Download) {
  if (!window.confirm("确认删除这条历史记录吗？已整理的文件不会被删除。")) return;
  try {
    await api(`/api/downloads/${task.id}/history`, { method: "DELETE" });
    downloads.value = downloads.value.filter((item) => item.id !== task.id);
  } catch (err) { error.value = (err as Error).message; }
}
async function saveProxy() { try { const update: Record<string, string> = {}; if (proxy.indexer_proxy) update.indexer_proxy = proxy.indexer_proxy; if (proxy.aria2_proxy) update.aria2_proxy = proxy.aria2_proxy; const data = await api("/api/settings/proxy", { method: "PUT", body: JSON.stringify(update) }); proxyConfigured.indexer_proxy = data.indexer_proxy_configured; proxyConfigured.aria2_proxy = data.aria2_proxy_configured; proxy.indexer_proxy = ""; proxy.aria2_proxy = ""; notice.value = "代理设置已保存（已配置的值不会回显）"; } catch (err) { error.value = (err as Error).message; } }
function sizeInputToBytes(value: string) { if (!value.trim()) return null; const mib = Number(value); if (!Number.isFinite(mib) || mib < 0) throw new Error("文件大小必须是非负数字（MiB）"); const bytes = Math.round(mib * 1024 * 1024); if (!Number.isSafeInteger(bytes)) throw new Error("文件大小超出允许范围"); return bytes; }
async function saveFilters() { try { const data = await api("/api/settings/filters", { method: "PUT", body: JSON.stringify({ filename_regex: syncFilters.filename_regex || null, min_size_bytes: sizeInputToBytes(syncFilters.min_size_mib), max_size_bytes: sizeInputToBytes(syncFilters.max_size_mib) }) }); syncFilters.filename_regex = data.filename_regex || ""; syncFilters.min_size_mib = formatMiB(data.min_size_bytes); syncFilters.max_size_mib = formatMiB(data.max_size_bytes); notice.value = "文件同步过滤已保存"; } catch (err) { error.value = (err as Error).message; } }
function formatBytes(value: number) { if (!value) return "—"; const units = ["B", "KiB", "MiB", "GiB", "TiB"]; let n = value; let i = 0; while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; } return `${n.toFixed(i ? 1 : 0)} ${units[i]}`; }
function formatSpeed(value: number) { return value ? `${formatBytes(value)}/s` : "—"; }
function formatEta(value: number | null) { if (!value || value < 0) return "—"; const h = Math.floor(value / 3600); const m = Math.floor(value % 3600 / 60); const s = value % 60; return h ? `${h}小时${m}分` : m ? `${m}分${s}秒` : `${s}秒`; }
function progress(item: Download) { return item.total_bytes ? Math.min(100, Math.round(item.completed_bytes / item.total_bytes * 100)) : 0; }
function startPolling() { if (!pollTimer) pollTimer = window.setInterval(refreshDownloads, 2000); }
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = undefined; } }
onMounted(async () => { await bootstrap(); if (loggedIn.value) startPolling(); });
onUnmounted(stopPolling);
</script>

<template>
  <main class="shell">
    <div v-if="busy" class="center-card">正在准备工作区…</div>
    <section v-else-if="setupRequired" class="center-card auth-card">
      <div class="eyebrow">FIRST RUN</div><h1>初始化下载管理器</h1><p>从容器日志或 <code>/config/setup-token</code> 读取一次性令牌。</p>
      <form @submit.prevent="doSetup"><label>初始化令牌<input v-model="setup.token" required /></label><label>管理员用户名<input v-model="setup.username" required /></label><label>密码<input v-model="setup.password" type="password" minlength="8" required /></label><button class="primary">创建管理员</button></form>
    </section>
    <section v-else-if="!loggedIn" class="center-card auth-card">
      <div class="eyebrow">SUK EBEI / MANAGER</div><h1>欢迎回来</h1><p>登录后搜索资源并管理下载任务。</p>
      <form @submit.prevent="doLogin"><label>用户名<input v-model="login.username" required /></label><label>密码<input v-model="login.password" type="password" required /></label><button class="primary">登录</button></form>
    </section>
    <template v-else>
      <header class="topbar"><div><div class="eyebrow">SUK EBEI / MANAGER</div><h1>下载工作台</h1></div><div class="account"><span>{{ username }}</span><button class="ghost" @click="doLogout">退出</button></div></header>
      <div v-if="error" class="alert error">{{ error }}<button @click="error = ''">×</button></div><div v-if="notice" class="alert success">{{ notice }}<button @click="notice = ''">×</button></div>
      <section class="panel search-panel"><div class="panel-heading"><div><div class="eyebrow">INDEXER</div><h2>搜索资源</h2></div><span class="muted">sukebei.nyaa.si</span></div>
        <form class="search-form" @submit.prevent="search(true)"><input v-model="query" class="search-input" placeholder="输入关键词…" /><select v-model="filters.category"><option v-for="(label, key) in categories" :key="key" :value="key">{{ label }}</option></select><select v-model="filters.sort"><option v-for="(label, key) in sorts" :key="key" :value="key">{{ label }}</option></select><select v-model="filters.order"><option value="desc">降序</option><option value="asc">升序</option></select><button class="primary">搜索</button></form>
        <div v-if="results.length" class="results"><div class="result-row result-head"><span>标题</span><span>大小</span><span>日期</span><span>做种 / 下载</span><span></span></div><div v-for="item in results" :key="item.result_id" class="result-row"><div><a v-if="item.details_url" :href="item.details_url" target="_blank" rel="noreferrer">{{ item.title }}</a><span v-else>{{ item.title }}</span><small>{{ item.category }}</small></div><span>{{ item.size_text }}</span><span>{{ item.published_at ? new Date(item.published_at).toLocaleDateString('zh-CN') : '—' }}</span><span><b class="seed">{{ item.seeders }}</b> / <b class="leech">{{ item.leechers }}</b></span><button class="small-action" @click="selected = item">下载</button></div><button v-if="hasNext" class="load-more" @click="nextPage">加载下一页</button></div><div v-else-if="query" class="empty">没有结果，换个关键词试试。</div>
      </section>
      <section class="panel"><div class="panel-heading"><div><div class="eyebrow">QUEUE</div><h2>下载任务 <span class="count">{{ activeDownloads.length }}</span></h2></div><button class="ghost" @click="refreshDownloads">刷新</button></div><div v-if="downloads.length" class="tasks"><article v-for="task in downloads" :key="task.id" class="task"><div class="task-main"><div><h3>{{ task.title }}</h3><div class="task-meta"><span :class="['status', task.status]">{{ statusLabels[task.status] || task.status }}</span><span v-if="task.status === 'metadata'">实际大小待解析</span><span v-else>{{ formatBytes(task.completed_bytes) }} / {{ formatBytes(task.total_bytes) }}</span><span v-if="task.download_speed">{{ formatSpeed(task.download_speed) }}</span><span v-if="task.eta_seconds">剩余 {{ formatEta(task.eta_seconds) }}</span></div></div><strong>{{ progress(task) }}%</strong></div><div class="progress"><i :style="{ width: `${progress(task)}%` }"></i></div><div v-if="task.files.length" class="file-list"><span v-for="file in task.files.slice(0, 3)" :key="file.path">{{ file.path }}</span><em v-if="task.files.length > 3">还有 {{ task.files.length - 3 }} 个文件</em></div><div v-if="task.error" class="task-error">{{ task.error }}</div><div class="task-actions"><button v-if="['waiting','metadata','downloading'].includes(task.status)" class="ghost" @click="taskAction(task, 'pause')">暂停</button><button v-if="task.status === 'paused'" class="ghost" @click="taskAction(task, 'resume')">继续</button><button v-if="['completed_pending_move','conflict','filtered'].includes(task.status)" class="ghost" @click="taskAction(task, 'move')">移动到整理目录</button><button v-if="task.status === 'failed' || task.status === 'cancelled'" class="ghost" @click="taskAction(task, 'retry')">重试</button><button v-if="['failed','cancelled','filtered'].includes(task.status)" class="ghost" @click="taskAction(task, 'cleanup')">清理暂存文件</button><button v-if="!['moved','failed','cancelled','filtered','moving'].includes(task.status)" class="danger" @click="taskAction(task, 'cancel')">删除任务</button><button v-if="['moved','failed','cancelled','conflict','filtered'].includes(task.status)" class="danger" @click="deleteHistory(task)">删除历史</button></div></article></div><div v-else class="empty">还没有下载任务。</div></section>
      <section class="panel settings"><div class="panel-heading"><div><div class="eyebrow">SETTINGS</div><h2>网络代理</h2></div></div><p class="muted">可选。{{ proxyConfigured.indexer_proxy ? '站点代理已配置。' : '站点代理未配置。' }}{{ proxyConfigured.aria2_proxy ? '下载代理已配置。' : '下载代理未配置。' }} 输入新地址后保存；已配置的值不会回显。</p><div class="proxy-grid"><label>站点代理<input v-model="proxy.indexer_proxy" placeholder="例如 http://127.0.0.1:7890" /></label><label>下载代理<input v-model="proxy.aria2_proxy" placeholder="例如 socks5://127.0.0.1:1080" /></label><button class="primary" @click="saveProxy">保存设置</button></div></section>
      <section class="panel settings"><div class="panel-heading"><div><div class="eyebrow">SYNC FILTER</div><h2>完成后同步过滤</h2></div></div><p class="muted">按每个文件的文件名和大小过滤；未匹配的文件会保留在任务暂存目录，可修改规则后再次移动。大小单位为 MiB，留空表示不限制。</p><div class="filter-grid"><label>文件名正则<input v-model="syncFilters.filename_regex" placeholder="例如 \\.mkv$" /></label><label>最小大小（MiB）<input v-model="syncFilters.min_size_mib" inputmode="decimal" placeholder="不限" /></label><label>最大大小（MiB）<input v-model="syncFilters.max_size_mib" inputmode="decimal" placeholder="不限" /></label><button class="primary" @click="saveFilters">保存过滤</button></div></section>
    </template>
    <div v-if="selected" class="modal-backdrop" @click.self="selected = null"><div class="modal"><div class="eyebrow">NEW DOWNLOAD</div><h2>添加下载任务</h2><p>{{ selected.title }}</p><label class="check"><input v-model="moveOnComplete" type="checkbox" /> 下载完成后自动移动到整理目录</label><div class="modal-actions"><button class="ghost" @click="selected = null">取消</button><button class="primary" @click="createDownload">开始下载</button></div></div></div>
    <div v-if="pendingCancelTask" class="modal-backdrop" @click.self="pendingCancelTask = null"><div class="modal"><div class="eyebrow">REMOVE DOWNLOAD</div><h2>删除下载任务</h2><p>{{ pendingCancelTask.title }}</p><label class="check"><input v-model="deleteFilesOnCancel" type="checkbox" /> 同时删除下载文件（默认）</label><p class="muted cancel-help">取消勾选只删除任务，暂存文件会保留。</p><div class="modal-actions"><button class="ghost" @click="pendingCancelTask = null">返回</button><button class="danger" @click="confirmCancel">删除任务</button></div></div></div>
  </main>
</template>

