const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const localTime = (value) => value ? new Date(value).toLocaleString("zh-CN", {hour12:false}) : "—";
const statusLabels = {running:"运行中", succeeded:"成功", failed:"失败", timed_out:"超时", skipped:"跳过", cancelled:"已停止"};
const pageSize = 50;
let offset = 0;
let jobs = [];
let activeRun = null;
let activeRunId = null;

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.json();
}

function filters() {
  return {job_id:$("#filter-job").value, date:$("#filter-date").value, status:$("#filter-status").value};
}

function searchParams(includePaging = true) {
  const params = new URLSearchParams();
  Object.entries(filters()).forEach(([key, value]) => { if (value) params.set(key, value); });
  if (includePaging) { params.set("limit", String(pageSize)); params.set("offset", String(offset)); }
  return params;
}

function notice(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 3000);
}

async function loadJobs() {
  jobs = await request("/api/jobs");
  const selected = new URLSearchParams(location.search).get("job_id") || "";
  $("#filter-job").innerHTML = '<option value="">全部任务</option>' + jobs.map((job) => '<option value="' + esc(job.id) + '">' + esc(job.name) + ' · ' + esc(job.id) + '</option>').join("");
  if (selected && !jobs.some((job) => job.id === selected)) {
    $("#filter-job").insertAdjacentHTML("beforeend", '<option value="' + esc(selected) + '">' + esc(selected) + '（已移除）</option>');
  }
  $("#filter-job").value = selected;
  $("#filter-date").value = new URLSearchParams(location.search).get("date") || "";
  $("#filter-status").value = new URLSearchParams(location.search).get("status") || "";
}

async function loadLogs() {
  try {
    const params = searchParams();
    const result = await request("/api/runs/search?" + params);
    if (location.pathname === "/logs") history.replaceState(null, "", "/logs?" + searchParams(false));
    const names = new Map(jobs.map((job) => [job.id, job.name]));
    $("#result-count").textContent = "共 " + result.total + " 条，保留最近 " + result.retention_days + " 天";
    const pages = Math.max(1, Math.ceil(result.total / pageSize));
    $("#page-label").textContent = (Math.floor(offset / pageSize) + 1) + " / " + pages;
    $("#previous-page").disabled = offset === 0;
    $("#next-page").disabled = offset + pageSize >= result.total;
    $("#log-rows").innerHTML = result.items.length ? result.items.map((run) => {
      const detailUrl = "/logs/" + run.id + "?" + searchParams(false);
      return '<tr><td>' + esc(localTime(run.started_at)) + '</td><td><code>' + esc(run.job_id) + '</code></td><td>' + esc(names.get(run.job_id) || "已移除任务") + '</td><td><code title="' + esc(run.workflow_run_id || "") + '">' + esc(run.workflow_run_id ? run.workflow_run_id.slice(0, 8) : "—") + '</code></td><td><span class="status ' + esc(run.status) + '">' + esc(statusLabels[run.status] || run.status) + '</span></td><td>' + esc(run.exit_code ?? "—") + '</td><td><a class="log-link" href="' + detailUrl + '">查看明细</a></td></tr>';
    }).join("") : '<tr><td colspan="7" class="empty">所选条件下没有运行记录</td></tr>';
  } catch (error) {
    $("#result-count").textContent = "读取失败";
    $("#log-rows").innerHTML = '<tr><td colspan="7" class="empty">无法读取日志：' + esc(error.message) + '</td></tr>';
  }
}

async function showDetail(runId, open = true) {
  try {
    activeRun = await request("/api/runs/" + encodeURIComponent(runId));
    activeRunId = String(runId);
    const job = jobs.find((item) => item.id === activeRun.job_id);
    $("#detail-title").textContent = (job?.name || "已移除任务") + " · " + activeRun.job_id;
    $("#detail-meta").textContent = "运行 ID：" + activeRun.id + " · 开始：" + localTime(activeRun.started_at) + " · 结束：" + localTime(activeRun.finished_at) + " · 状态：" + (statusLabels[activeRun.status] || activeRun.status) + " · 退出码：" + (activeRun.exit_code ?? "—") + " · 定义版本：v" + (activeRun.definition_revision || 1) + " · 工作流：" + (activeRun.workflow_run_id || "—");
    const output = $("#detail-output");
    const shouldFollow = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
    output.textContent = activeRun.output || (activeRun.status === "running" ? "任务正在运行，等待输出…" : "此运行没有记录输出。");
    if (shouldFollow) output.scrollTop = output.scrollHeight;
    $("#rerun-detail").disabled = !job || activeRun.status === "running";
    $("#stop-run").hidden = activeRun.status !== "running";
    $("#rerun-note").textContent = job ? "使用当前任务定义；回刷历史数据前请将命令参数设为对应日期。" : "任务已移除，无法重新运行。";
    if (open && !$("#log-detail-dialog").open) $("#log-detail-dialog").showModal();
  } catch (error) { notice(error.message); }
}

function applyFilters() { offset = 0; loadLogs(); }
$("#filter-job").addEventListener("change", applyFilters);
$("#filter-date").addEventListener("change", applyFilters);
$("#filter-status").addEventListener("change", applyFilters);
$("#refresh-logs").addEventListener("click", loadLogs);
$("#clear-filters").addEventListener("click", () => {
  $("#filter-job").value = "";
  $("#filter-date").value = "";
  $("#filter-status").value = "";
  applyFilters();
});
$("#previous-page").addEventListener("click", () => { offset = Math.max(0, offset - pageSize); loadLogs(); });
$("#next-page").addEventListener("click", () => { offset += pageSize; loadLogs(); });
$("#close-detail").addEventListener("click", () => $("#log-detail-dialog").close());
$("#close-detail-button").addEventListener("click", () => $("#log-detail-dialog").close());
$("#rerun-detail").addEventListener("click", async () => {
  if (!activeRun || !confirm("按当前任务定义重新运行此节点？回刷历史日期前请先修改任务参数。")) return;
  try {
    await request("/api/jobs/" + encodeURIComponent(activeRun.job_id) + "/run?scope=self", {method:"POST"});
    $("#log-detail-dialog").close();
    notice("已提交此节点运行");
    await loadLogs();
  } catch (error) { notice(error.message); }
});
$("#stop-run").addEventListener("click", async () => {
  if (!activeRun || activeRun.status !== "running") return;
  if (!confirm(`确认停止运行 #${activeRun.id}？该运行启动的进程及其子进程会被终止。`)) return;
  try {
    await request("/api/runs/" + encodeURIComponent(activeRun.id) + "/stop", {method:"POST"});
    notice("已请求停止；正在等待进程退出");
    await showDetail(activeRun.id, false);
    await loadLogs();
  } catch (error) { notice(error.message); }
});

async function initialize() {
  try {
    const runId = location.pathname.match(/^\/logs\/(\d+)$/)?.[1];
    await loadJobs();
    await loadLogs();
    if (runId) await showDetail(runId);
  } catch (error) {
    $("#log-rows").innerHTML = '<tr><td colspan="7" class="empty">无法读取日志：' + esc(error.message) + '</td></tr>';
  }
}
initialize();
setInterval(() => {
  if (location.pathname === "/logs") loadLogs();
  if (activeRunId && activeRun?.status === "running" && $("#log-detail-dialog").open) showDetail(activeRunId, false);
}, 1500);
