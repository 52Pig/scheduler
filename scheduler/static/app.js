const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const localTime = (value) => value ? new Date(value).toLocaleString("zh-CN", {hour12:false}) : "Next run not scheduled";
let toastTimer;
let jobsCache = [];
let currentRuns = {};
let positions = new Map();
const graphPositionsKey = "scheduler.graph.positions.v2";
try {
  positions = new Map(Object.entries(JSON.parse(localStorage.getItem(graphPositionsKey) || "{}")));
} catch {}
let connectingFrom = null;
let connectPointer = null;
let potentialTarget = null;
let dragState = null;
let editingJob = null;
let manualRunJob = null;
let connectClickMode = false;
let graphScale = 1;
let graphSize = {width:760,height:400};
let graphOffset = {x:0,y:0};
let graphFitPending = true;
let graphJobIds = "";

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 2800);
}

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.status === 204 ? null : response.json();
}

function parentOptions(selected = [], omitId = null) {
  $("#depends-on").innerHTML = jobsCache.filter((job) => job.id !== omitId).map((job) => `<option value="${esc(job.id)}" ${selected.includes(job.id) ? "selected" : ""}>${esc(job.name)} (${esc(job.id)})</option>`).join("");
}

function setScheduleType(value) {
  const radio = document.querySelector(`input[name="schedule"][value="${value || "interval"}"]`);
  if (radio) radio.checked = true;
}

function localDateInput(value) {
  if (!value) return "";
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function openJobEditor(job = null) {
  editingJob = job;
  const form = $("#job-form");
  form.reset();
  $("#form-error").textContent = "";
  parentOptions(job?.depends_on || [], job?.id || null);
  $("#dialog-title").textContent = job ? "修改任务" : "新建任务";
  $("#submit-job").textContent = job ? "保存修改" : "创建任务";
  form.elements.id.readOnly = Boolean(job);
  if (job) {
    form.elements.id.value = job.id;
    form.elements.name.value = job.name;
    form.elements.executable.value = job.command?.[0] || "";
    form.elements.arguments.value = job.command?.slice(1).join("\n") || "";
    form.elements.cwd.value = job.working_directory || "";
    form.elements.timeout.value = job.timeout_seconds;
    form.elements.timezone.value = job.timezone || "";
    form.elements.interval.value = job.interval_seconds || 3600;
    form.elements.cron.value = job.cron || "";
    form.elements.run_at.value = localDateInput(job.run_at);
    setScheduleType(job.schedule_type || "interval");
    $("#has-own-schedule").checked = Boolean(job.schedule_type);
    for (const parentId of job.depends_on || []) {
      const option = Array.from(form.elements.depends_on.options).find((item) => item.value === parentId);
      if (option) option.selected = true;
    }
  } else {
    setScheduleType("interval");
    $("#has-own-schedule").checked = false;
  }
  $("#job-dialog").showModal();
  syncScheduleVisibility();
}

function syncScheduleVisibility() {
  const hasParents = $("#depends-on").selectedOptions.length > 0;
  $("#own-schedule-row").hidden = !hasParents;
  const scheduleEnabled = !hasParents || $("#has-own-schedule").checked;
  $("#schedule-config").hidden = !scheduleEnabled;
  document.querySelectorAll(".schedule-field").forEach((field) => {
    field.hidden = !scheduleEnabled || field.dataset.for !== document.querySelector('input[name="schedule"]:checked').value;
    field.querySelector("input").required = !field.hidden;
  });
}

function openRunOptions(jobId) {
  manualRunJob = jobId;
  const job = jobsCache.find((item) => item.id === jobId);
  $("#manual-run-job").textContent = job ? `${job.name} (${job.id})` : jobId;
  $("#manual-run-dialog").showModal();
}

function renderRunHistory(jobId, recentRuns) {
  const runs = recentRuns[jobId] || [];
  if (!runs.length) return '<span class="run-history-empty">—</span>';
  const statusLabels = {
    succeeded:"成功", failed:"失败", timed_out:"超时", running:"运行中", skipped:"跳过", cancelled:"已停止",
  };
  const groups = new Map();
  for (const run of [...runs].reverse()) {
    const day = new Date(run.started_at).toLocaleDateString("zh-CN", {year:"numeric",month:"2-digit",day:"2-digit"});
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day).push(run);
  }
  return `<div class="run-history" aria-label="${runs.length} 次执行">${Array.from(groups, ([day, dayRuns]) => `<span class="run-history-day" title="${esc(day)}">${dayRuns.map((run) => {
    const status = statusLabels[run.status] || run.status;
    const time = localTime(run.started_at);
    const exitCode = run.exit_code === null ? "" : ` · 退出码 ${run.exit_code}`;
    const label = `${time} · ${status}${exitCode}`;
    return `<a class="run-dot ${esc(run.status)}" href="/logs/${run.id}" target="_blank" rel="noopener noreferrer" title="${esc(label)}" aria-label="${esc(label)}"></a>`;
  }).join("")}</span>`).join("")}</div>`;
}

function renderStopActions(jobId, recentRuns) {
  return (recentRuns[jobId] || []).filter((run) => run.status === "running").map((run) =>
    `<button class="small-button danger" data-stop-run="${run.id}" title="停止运行 #${run.id}" aria-label="停止运行 ${esc(jobId)}，运行编号 ${run.id}">停止</button>`
  ).join("");
}

async function queueManualRun(scope) {
  if (!manualRunJob) return;
  try {
    await request(`/api/jobs/${encodeURIComponent(manualRunJob)}/run?scope=${scope}`, {method:"POST"});
    $("#manual-run-dialog").close();
    toast(scope === "self" ? "已提交，仅运行此任务" : "已提交运行此任务及下游");
    manualRunJob = null;
    await refresh();
  } catch (error) { toast(error.message); }
}

function layoutJobs() {
  const byId = new Map(jobsCache.map((job) => [job.id, job]));
  const depth = new Map();
  const getDepth = (id, seen = new Set()) => {
    if (depth.has(id)) return depth.get(id);
    if (seen.has(id)) return 0;
    seen.add(id);
    const parents = (byId.get(id)?.depends_on || []).filter((parent) => byId.has(parent));
    const value = parents.length ? 1 + Math.max(...parents.map((parent) => getDepth(parent, seen))) : 0;
    depth.set(id, value);
    return value;
  };
  jobsCache.forEach((job) => getDepth(job.id));
  const levels = new Map();
  jobsCache.forEach((job) => {
    const level = depth.get(job.id);
    if (!levels.has(level)) levels.set(level, []);
    levels.get(level).push(job);
  });
  const viewportWidth = Math.max($("#graph-viewport").clientWidth, 760);
  const levelGap = 300;
  const nodeGap = 28;
  const nodeHeight = 126;
  const maxLevelHeight = Math.max(...[...levels.values()].map((items) => items.length * nodeHeight + Math.max(0, items.length - 1) * nodeGap), 400);
  for (const [level, jobs] of levels) {
    const levelHeight = jobs.length * nodeHeight + Math.max(0, jobs.length - 1) * nodeGap;
    const startY = Math.max(34, (maxLevelHeight - levelHeight) / 2 + 34);
    jobs.forEach((job, index) => {
      if (!positions.has(job.id)) positions.set(job.id, {x:34 + level * levelGap, y:startY + index * (nodeHeight + nodeGap)});
    });
  }
  const livePositions = jobsCache.map((job) => positions.get(job.id));
  const width = Math.max(viewportWidth, ...livePositions.map((point) => point.x + 260), 760);
  const height = Math.max(400, maxLevelHeight + 68, ...livePositions.map((point) => point.y + 150));
  graphSize = {width,height};
  $("#graph-canvas").style.width = `${width}px`;
  $("#graph-canvas").style.height = `${height}px`;
  updateGraphStage();
}

function updateGraphStage() {
  const viewport = $("#graph-viewport");
  const stage = $("#graph-stage");
  const scaledWidth = graphSize.width * graphScale;
  const scaledHeight = graphSize.height * graphScale;
  const stageWidth = Math.max(viewport.clientWidth, scaledWidth);
  const stageHeight = Math.max(viewport.clientHeight, scaledHeight);
  graphOffset = {x:(stageWidth - scaledWidth) / 2,y:(stageHeight - scaledHeight) / 2};
  stage.style.width = `${stageWidth}px`;
  stage.style.height = `${stageHeight}px`;
  const canvas = $("#graph-canvas");
  canvas.style.left = `${graphOffset.x}px`;
  canvas.style.top = `${graphOffset.y}px`;
  canvas.style.transform = `scale(${graphScale})`;
  $("#zoom-level").textContent = `${Math.round(graphScale * 100)}%`;
  $("#zoom-out").disabled = graphScale <= 0.1;
  $("#zoom-in").disabled = graphScale >= 2;
}

function fitGraph() {
  const viewport = $("#graph-viewport");
  graphScale = Math.min(1, (viewport.clientWidth - 32) / graphSize.width, (viewport.clientHeight - 32) / graphSize.height);
  updateGraphStage();
  viewport.scrollTo(0, 0);
  graphFitPending = false;
}

function zoomGraph(factor) {
  const viewport = $("#graph-viewport");
  const centerX = (viewport.scrollLeft + viewport.clientWidth / 2 - graphOffset.x) / graphScale;
  const centerY = (viewport.scrollTop + viewport.clientHeight / 2 - graphOffset.y) / graphScale;
  graphScale = Math.max(0.1, Math.min(2, graphScale * factor));
  updateGraphStage();
  viewport.scrollLeft = graphOffset.x + centerX * graphScale - viewport.clientWidth / 2;
  viewport.scrollTop = graphOffset.y + centerY * graphScale - viewport.clientHeight / 2;
}

function graphPoint(event) {
  const rect = $("#graph-canvas").getBoundingClientRect();
  return {x:(event.clientX - rect.left) / graphScale,y:(event.clientY - rect.top) / graphScale};
}

function pointFor(id, side) {
  const point = positions.get(id);
  return {x:point.x + 112, y:point.y + (side === "bottom" ? 102 : -14)};
}

function edgePath(from, to) {
  const bend = Math.max(40, Math.abs(to.y - from.y) * 0.45);
  return `M ${from.x} ${from.y} C ${from.x} ${from.y + bend}, ${to.x} ${to.y - bend}, ${to.x} ${to.y}`;
}

function renderGraph() {
  layoutJobs();
  if (!jobsCache.length) {
    $("#graph-nodes").innerHTML = '<div class="graph-empty">Create a scheduled root to start a workflow.</div>';
    $("#graph-edges").innerHTML = "";
    return;
  }
  const byId = new Map(jobsCache.map((job) => [job.id, job]));
  const svg = $("#graph-edges");
  svg.setAttribute("width", $("#graph-canvas").style.width);
  svg.setAttribute("height", $("#graph-canvas").style.height);
  const runningJobs = new Set(Object.entries(currentRuns).filter(([, runs]) => runs.some((run) => run.status === "running")).map(([jobId]) => jobId));
  const edges = [];
  for (const job of jobsCache) for (const parentId of job.depends_on || []) {
    if (!byId.has(parentId)) continue;
    const start = pointFor(parentId, "bottom");
    const end = pointFor(job.id, "top");
    const tipY = positions.get(job.id).y - 2;
    const path = edgePath(start, end);
    const removeX = (start.x + end.x) / 2;
    const removeY = (start.y + end.y) / 2;
    const active = runningJobs.has(parentId) || runningJobs.has(job.id);
    edges.push(`<g class="edge-group ${active ? "is-running" : ""}"><path class="edge-hit" d="${path}" data-parent="${esc(parentId)}" data-child="${esc(job.id)}"/><path class="edge" d="${path}" data-parent="${esc(parentId)}" data-child="${esc(job.id)}"/><polygon class="edge-arrow" points="${end.x},${tipY} ${end.x - 7},${end.y} ${end.x + 7},${end.y}"/><g class="edge-remove" data-parent="${esc(parentId)}" data-child="${esc(job.id)}" role="button" tabindex="0" aria-label="删除 ${esc(parentId)} 到 ${esc(job.id)} 的依赖" transform="translate(${removeX} ${removeY})"><circle class="remove-hit" r="9"/><circle class="remove-mark" r="6"/><text y="3.5">×</text></g></g>`);
  }
  if (connectingFrom && connectPointer) {
    const start = pointFor(connectingFrom, "bottom");
    const end = connectPointer;
    edges.push(`<path class="edge-temp" d="${edgePath(start, end)}"/>`);
  }
  svg.innerHTML = edges.join("");
  $("#graph-nodes").innerHTML = jobsCache.map((job) => {
    const point = positions.get(job.id);
    const command = job.command?.join(" ") || "Logical node";
    const meta = job.is_root ? `${job.schedule} · ${job.timezone}` : `${(job.depends_on || []).join(", ")} · ${job.is_scheduled ? job.schedule : "依赖触发"}`;
    const running = runningJobs.has(job.id);
    return `<article class="graph-node ${job.is_root ? "is-root" : "is-child"} ${running ? "is-running" : ""} ${potentialTarget === job.id ? "is-target" : ""} ${connectingFrom === job.id ? "is-connecting" : ""}" data-node="${esc(job.id)}" style="left:${point.x}px;top:${point.y}px"><div class="node-actions"><button type="button" class="node-edit" data-edit="${esc(job.id)}" title="修改任务">编辑</button><button type="button" class="node-connect" data-connect="${esc(job.id)}" title="选择下游节点添加依赖">连线</button></div><strong>${esc(job.name)}</strong><div class="node-id">${esc(job.id)}</div><div class="node-meta" title="${esc(command)}">${running ? "正在运行 · " : ""}${esc(meta)}</div><button class="node-port" type="button" aria-label="从 ${esc(job.name)} 添加依赖" title="拖到目标节点以添加依赖"></button></article>`;
  }).join("");
  if (graphFitPending) fitGraph();
}

async function setDependencies(jobId, parents) {
  try {
    await request(`/api/jobs/${encodeURIComponent(jobId)}/dependencies`, {
      method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify({depends_on:parents}),
    });
    toast("依赖关系已更新");
    await refresh();
  } catch (error) { toast(error.message); }
}

async function refresh() {
  try {
    const [jobs, runs, recentRuns, health] = await Promise.all([request("/api/jobs"), request("/api/runs?limit=50"), request("/api/runs/recent"), request("/api/health")]);
    jobsCache = jobs;
    const jobIds = jobs.map((job) => job.id).sort().join("\0");
    if (jobIds !== graphJobIds) {
      graphJobIds = jobIds;
      graphFitPending = true;
    }
    currentRuns = recentRuns;
    $("#health").textContent = health.scheduler_running ? "调度服务正常" : "调度服务未启动";
    $("#timezone").textContent = jobs.find((job) => job.is_root)?.timezone || "服务默认时区";
    $("#job-count").textContent = jobs.length;
    $("#success-count").textContent = runs.filter((run) => run.status === "succeeded").length;
    $("#failure-count").textContent = runs.filter((run) => ["failed", "timed_out"].includes(run.status)).length;
    $("#jobs").innerHTML = jobs.length ? jobs.map((job) => `<tr><td><div class="job-name">${esc(job.name)}</div><div class="job-id">${esc(job.id)} · v${job.revision || 1}</div></td><td><div class="command" title="${esc(job.command?.join(" ") || "Logical node")}">${esc(job.command?.join(" ") || "Logical node")}</div></td><td><div class="schedule">${esc(job.schedule)}</div><div class="job-id">${esc(job.depends_on.length ? "依赖触发" : job.timezone)}</div></td><td class="dependency-ids">${job.depends_on.length ? job.depends_on.map((id) => `<code>${esc(id)}</code>`).join(" ") : "—"}</td><td class="next-run">${esc(localTime(job.next_run_time))}</td><td>${renderRunHistory(job.id, recentRuns)}</td><td><div class="row-actions"><button class="small-button" data-edit="${esc(job.id)}" title="修改任务">编辑</button><a class="small-button job-log-link" href="/logs?job_id=${encodeURIComponent(job.id)}" target="_blank" rel="noopener noreferrer" title="在新标签页查看执行日志">日志</a>${renderStopActions(job.id, recentRuns)}<button class="small-button" data-run="${esc(job.id)}" title="运行任务">▷ 运行</button><button class="small-button danger" data-remove="${esc(job.id)}" title="移除任务">移除</button></div></td></tr>`).join("") : '<tr><td colspan="7" class="empty">还没有任务，创建一个调度入口开始。</td></tr>';
    renderGraph();
  } catch (error) {
    $("#health").textContent = "连接失败";
    $("#jobs").innerHTML = `<tr><td colspan="7" class="empty">无法读取任务：${esc(error.message)}</td></tr>`;
  }
}

$("#new-job").addEventListener("click", () => openJobEditor());
$("#cancel").addEventListener("click", () => $("#job-dialog").close());
$("#close-dialog").addEventListener("click", () => $("#job-dialog").close());
$("#refresh").addEventListener("click", refresh);
setInterval(refresh, 2000);
$("#fit-graph").addEventListener("click", fitGraph);
$("#zoom-in").addEventListener("click", () => zoomGraph(1.25));
$("#zoom-out").addEventListener("click", () => zoomGraph(0.8));
$("#reset-layout").addEventListener("click", () => { positions = new Map(); localStorage.removeItem(graphPositionsKey); graphFitPending = true; renderGraph(); });
window.addEventListener("resize", () => { if (jobsCache.length) { renderGraph(); fitGraph(); } });
$("#depends-on").addEventListener("change", syncScheduleVisibility);
$("#has-own-schedule").addEventListener("change", syncScheduleVisibility);
$("#close-manual-run").addEventListener("click", () => $("#manual-run-dialog").close());
$("#run-self").addEventListener("click", () => queueManualRun("self"));
$("#run-downstream").addEventListener("click", () => queueManualRun("downstream"));
document.querySelectorAll('input[name="schedule"]').forEach((input) => input.addEventListener("change", syncScheduleVisibility));

$("#graph-nodes").addEventListener("pointerdown", (event) => {
  if (connectClickMode || event.target.closest(".node-edit,.node-connect")) return;
  const port = event.target.closest(".node-port");
  const node = event.target.closest(".graph-node");
  if (!node) return;
  if (port) {
    event.preventDefault();
    connectingFrom = node.dataset.node;
    connectPointer = graphPoint(event);
    renderGraph();
    return;
  }
  const point = positions.get(node.dataset.node);
  const cursor = graphPoint(event);
  dragState = {id:node.dataset.node,dx:cursor.x - point.x,dy:cursor.y - point.y};
});
document.addEventListener("pointermove", (event) => {
  if (dragState) {
    const cursor = graphPoint(event);
    positions.set(dragState.id, {x:Math.max(0,cursor.x - dragState.dx),y:Math.max(0,cursor.y - dragState.dy)});
    renderGraph();
    return;
  }
  if (connectingFrom) {
    connectPointer = graphPoint(event);
    potentialTarget = event.target.closest?.(".graph-node")?.dataset.node || null;
    if (potentialTarget === connectingFrom) potentialTarget = null;
    renderGraph();
  }
});
document.addEventListener("pointerup", async (event) => {
  if (dragState) {
    dragState = null;
    localStorage.setItem(graphPositionsKey, JSON.stringify(Object.fromEntries(positions)));
    return;
  }
  if (connectClickMode) return;
  if (connectingFrom) {
    const source = connectingFrom;
    const target = event.target.closest?.(".graph-node")?.dataset.node;
    connectingFrom = null;
    connectPointer = null;
    potentialTarget = null;
    if (target && target !== source) {
      const job = jobsCache.find((item) => item.id === target);
      if (job && !job.depends_on.includes(source)) await setDependencies(target, [...job.depends_on, source]);
    }
    renderGraph();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !connectClickMode) return;
  connectingFrom = null;
  connectPointer = null;
  potentialTarget = null;
  connectClickMode = false;
  renderGraph();
});
$("#graph-edges").addEventListener("click", (event) => {
  const edge = event.target.closest(".edge-hit,.edge,.edge-remove");
  if (!edge) return;
  const job = jobsCache.find((item) => item.id === edge.dataset.child);
  if (job) setDependencies(job.id, job.depends_on.filter((parent) => parent !== edge.dataset.parent));
});
$("#graph-edges").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const edge = event.target.closest(".edge-remove");
  if (!edge) return;
  event.preventDefault();
  const job = jobsCache.find((item) => item.id === edge.dataset.child);
  if (job) setDependencies(job.id, job.depends_on.filter((parent) => parent !== edge.dataset.parent));
});
$("#graph-nodes").addEventListener("click", (event) => {
  if (connectClickMode && event.target.closest(".node-edit")) return;
  const connectId = event.target.closest("[data-connect]")?.dataset.connect;
  if (connectId) {
    connectingFrom = connectId;
    connectPointer = pointFor(connectId, "bottom");
    connectClickMode = true;
    toast("请选择下游任务以添加依赖");
    renderGraph();
    return;
  }
  if (connectClickMode) {
    const targetId = event.target.closest(".graph-node")?.dataset.node;
    const sourceId = connectingFrom;
    if (targetId && sourceId && targetId !== sourceId) {
      const job = jobsCache.find((item) => item.id === targetId);
      if (job && !job.depends_on.includes(sourceId)) setDependencies(targetId, [...job.depends_on, sourceId]);
    }
    connectingFrom = null;
    connectPointer = null;
    potentialTarget = null;
    connectClickMode = false;
    renderGraph();
    return;
  }
  const jobId = event.target.closest("[data-edit]")?.dataset.edit;
  if (jobId) openJobEditor(jobsCache.find((job) => job.id === jobId));
});
$("#jobs").addEventListener("click", async (event) => {
  const runId = event.target.closest("[data-run]")?.dataset.run;
  const stopRunId = event.target.closest("[data-stop-run]")?.dataset.stopRun;
  const removeId = event.target.closest("[data-remove]")?.dataset.remove;
  const editId = event.target.closest("[data-edit]")?.dataset.edit;
  try {
    if (editId) {
      openJobEditor(jobsCache.find((job) => job.id === editId));
      return;
    }
    if (stopRunId) {
      if (!confirm(`确认停止运行 #${stopRunId}？该运行启动的进程及其子进程会被终止。`)) return;
      await request(`/api/runs/${stopRunId}/stop`, {method:"POST"});
      toast(`已请求停止运行 #${stopRunId}`);
      await refresh();
      return;
    }
    if (runId) { openRunOptions(runId); return; }
    if (removeId && confirm(`确定移除任务“${removeId}”？`)) { await request(`/api/jobs/${encodeURIComponent(removeId)}`, {method:"DELETE"}); toast("任务已移除"); }
    await refresh();
  } catch (error) { toast(error.message); }
});

$("#job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const parents = Array.from($("#depends-on").selectedOptions, (option) => option.value);
  const executable = String(form.get("executable")).trim();
  const command = executable ? [executable, ...String(form.get("arguments")).split("\n").map((part) => part.trim()).filter(Boolean)] : null;
  const scheduleEnabled = !parents.length || $("#has-own-schedule").checked;
  const scheduleType = scheduleEnabled ? formElement.elements.schedule.value : null;
  const payload = {id:form.get("id"),name:form.get("name"),command,depends_on:parents,working_directory:form.get("cwd") || null,timeout_seconds:Number(form.get("timeout")),timezone:form.get("timezone") || null,schedule_type:scheduleType};
  if (scheduleType === "interval") payload.interval_seconds = Number(form.get("interval"));
  else if (scheduleType === "cron") payload.cron = form.get("cron");
  else if (scheduleType === "date") payload.run_at = new Date(form.get("run_at")).toISOString();
  try {
    const url = editingJob ? `/api/jobs/${encodeURIComponent(editingJob.id)}` : "/api/jobs";
    await request(url, {method:editingJob ? "PUT" : "POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    $("#job-dialog").close();
    formElement.reset();
    editingJob = null;
    document.querySelector('input[name="schedule"][value="interval"]').checked = true;
    toast("任务已保存");
    await refresh();
  } catch (error) { $("#form-error").textContent = error.message; }
});

refresh();
