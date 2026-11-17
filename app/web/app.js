/* 本地查看页逻辑。
 *
 * 安全约束：raw_output、模型输出与审计内容都是不可信文本，一律通过 textContent
 * 写入 DOM，不做任何 innerHTML 拼接。
 */

const MAX_DIFF_LINES = 400;

const MODE_HINTS = {
  paste: "粘贴 harness 或模型已经产出的原文",
  harness: "按内置 profile 白名单驱动，任务经 stdin 传入，参数不可由请求体指定",
};

const PROFILE_FALLBACK_NOTES = {
  command: "显式命令模式：命令与参数来自服务端 .env，不经自动发现",
};

const OPTION_FIELDS = [
  { key: "rules", label: "评分 rules", step: "rules" },
  { key: "repair", label: "修复 repair", step: "repair" },
  { key: "format", label: "格式化 format", step: "format" },
  { key: "humanize", label: "拟人化 humanize", step: "humanize" },
  { key: "strict", label: "严格 strict", step: "schema", from: "strict" },
];

const DIMENSION_LABELS = {
  completeness: "字段完整度",
  format: "格式合规",
  grounding: "与输入一致性",
};

const state = { config: null, items: [], selectedLine: null, harnessNotes: {} };

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function setText(id, value) {
  $(id).textContent = value === undefined || value === null ? "" : String(value);
}

async function getJSON(url) {
  const response = await fetch(url, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error(`${url} 返回 ${response.status}`);
  return response.json();
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload && payload.detail;
    if (typeof detail === "string") throw new Error(detail);
    if (Array.isArray(detail) && detail.length) {
      const first = detail[0];
      throw new Error(`${(first.loc || []).join(".")}: ${first.msg || "请求体校验失败"}`);
    }
    throw new Error(`请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

/* ---------- 配置与环境 ---------- */

function stepFlag(stepName, key) {
  const step = state.config && state.config.steps ? state.config.steps[stepName] : null;
  if (!step) return key === "strict" ? false : true;
  return key === "strict" ? Boolean(step.strict) : Boolean(step.enabled);
}

function renderSchemaOptions() {
  const select = $("field-schema");
  clear(select);
  select.appendChild(el("option", null, "无 Schema（纯文本）")).value = "";
  const schemas = state.config ? state.config.schemas : {};
  Object.keys(schemas).forEach((name) => {
    const required = schemas[name].required || [];
    const option = el("option", null, required.length ? `${name} · 必填 ${required.join("、")}` : name);
    option.value = name;
    select.appendChild(option);
  });
}

function renderOptionToggles() {
  const box = $("options");
  clear(box);
  OPTION_FIELDS.forEach((field) => {
    const label = el("label", "opt");
    const input = el("input");
    input.type = "checkbox";
    input.dataset.key = field.key;
    input.checked = stepFlag(field.step, field.from || "enabled");
    label.appendChild(input);
    label.appendChild(el("span", null, field.label));
    box.appendChild(label);
  });
  const timeout = el("label", "opt-timeout");
  timeout.appendChild(el("span", null, "timeout_ms"));
  const input = el("input");
  input.type = "number";
  input.id = "field-timeout";
  input.min = "50";
  input.max = "120000";
  input.step = "50";
  input.value = state.config ? String(state.config.timeout_ms) : "8000";
  timeout.appendChild(input);
  box.appendChild(timeout);
}

function renderEnv(auditTotal) {
  const list = $("env");
  clear(list);
  const config = state.config || {};
  const rows = [
    ["provider", config.provider || "-"],
    ["score 阈值", config.score_threshold === undefined ? "-" : config.score_threshold],
    ["timeout", `${config.timeout_ms ?? "-"} ms`],
    ["修复上限", config.max_repair_retries ?? "-"],
    ["fail_open", String(config.fail_open)],
    ["引用风格", config.reference_style || "-"],
    ["审计条数", auditTotal === undefined ? "-" : `${auditTotal} 条`],
  ];
  rows.forEach(([key, value]) => {
    list.appendChild(el("dt", null, key));
    list.appendChild(el("dd", null, value));
  });
  const pathRow = el("dt", null, "audit_path");
  const pathValue = el("dd", null, config.audit_path || "-");
  pathValue.title = String(config.audit_path || "");
  list.appendChild(pathRow);
  list.appendChild(pathValue);
}

async function refreshHealth() {
  const dot = $("health-dot");
  dot.className = "dot";
  try {
    const health = await getJSON("/healthz");
    dot.classList.add(health.status === "ok" ? "ok" : "warn");
    dot.title = `provider=${health.provider}`;
  } catch (error) {
    dot.classList.add("bad");
    dot.title = String(error.message || error);
  }
}

/* ---------- 历史列表 ---------- */

function statusOf(record) {
  if (record.degraded) return { cls: "warn", label: "降级" };
  if (!record.valid) return { cls: "bad", label: "未通过" };
  return { cls: "ok", label: "通过" };
}

/**
 * 修复/重写各自的采纳结果。它们与 valid 是两件事：一次运行可能同时
 * “修复被闸门拒绝”与“拟人化被采纳”，标签只表达通过与否，细节进 flags。
 */
function flagsOf(record) {
  const markers = record.markers || [];
  const flags = [];
  if (markers.includes("repair_rejected")) flags.push("修复被拒");
  else if (record.repaired) flags.push("已修复");
  if (markers.includes("humanize_rejected")) flags.push("重写被拒");
  else if ((record.changes || []).some((text) => text.includes("拟人化"))) flags.push("已重写");
  if (markers.includes("timeout")) flags.push("超时");
  return flags;
}

function timeAgo(value) {
  if (!value) return "未知时间";
  const stamp = Date.parse(value);
  if (Number.isNaN(stamp)) return String(value);
  const seconds = Math.max(0, (Date.now() - stamp) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function previewOf(record) {
  const text = String(record.raw_output ?? "").replace(/\s+/g, " ").trim();
  return text.length ? text.slice(0, 110) : "（空输出）";
}

function renderHistory() {
  const list = $("history-list");
  const fragment = document.createDocumentFragment();
  const template = $("tpl-history-item");
  clear(list);
  if (!state.items.length) {
    list.appendChild(el("div", "muted-note", "暂无审计记录。运行一次优化即可在此留痕。"));
  }
  state.items.forEach((record) => {
    const node = template.content.firstElementChild.cloneNode(true);
    const status = statusOf(record);
    node.querySelector('[data-role="dot"]').classList.add(status.cls);
    node.querySelector('[data-role="dot"]').title = status.label;
    node.querySelector('[data-role="schema"]').textContent = `${(record.schema || "text").toLowerCase()} · ${status.label}`;
    node.querySelector('[data-role="score"]').textContent =
      record.score === null || record.score === undefined ? "-" : Number(record.score).toFixed(3);
    node.querySelector('[data-role="preview"]').textContent = previewOf(record);
    const flags = flagsOf(record);
    if (record.harness) flags.unshift(`驱动:${record.harness.name}`);
    node.querySelector('[data-role="meta"]').textContent = [
      `${timeAgo(record.ts)} · ${record.ms ?? "-"}ms · ${record.provider || "-"}`,
      ...flags,
    ].join(" · ");
    node.classList.toggle("selected", record.line_no === state.selectedLine);
    node.addEventListener("click", () => selectRecord(record.line_no));
    fragment.appendChild(node);
  });
  list.appendChild(fragment);
}

async function loadHistory() {
  const foot = $("history-foot");
  try {
    const data = await getJSON("/v1/audits?limit=60");
    state.items = data.items || [];
    foot.textContent = `${state.items.length} / ${data.total_lines} 条${data.skipped ? ` · 跳过损坏 ${data.skipped}` : ""}`;
    renderEnv(data.total_lines);
  } catch (error) {
    state.items = [];
    foot.textContent = `历史读取失败：${error.message}`;
  }
  renderHistory();
}

function recordById(requestId) {
  // items 为倒序（最新在前），因此首个匹配就是本次运行写入的那条。
  return state.items.find((item) => item.request_id === requestId) || null;
}

function selectRecord(lineNo) {
  const record = state.items.find((item) => item.line_no === lineNo) || null;
  if (!record) return;
  state.selectedLine = lineNo;
  $("field-raw").value = String(record.raw_output ?? "");
  $("field-instruction").value = String(record.instruction ?? "");
  $("field-input-text").value = String(record.input_text ?? "");
  const schemaName = String(record.schema || "").toLowerCase();
  const select = $("field-schema");
  select.value = Object.keys(state.config.schemas).includes(schemaName) ? schemaName : "";
  renderResult(record);
  renderDiff(record);
  renderHistory();
  $("result-panel").scrollIntoView({ block: "nearest" });
}

/* ---------- 结果渲染 ---------- */

function synthesizeRecord(response, rawOutput) {
  return {
    request_id: response.request_id,
    schema: null,
    valid: response.valid,
    score: response.score,
    repaired: response.repaired,
    degraded: response.degraded,
    steps: response.steps || [],
    usage: response.usage || {},
    dimensions: {},
    markers: [],
    changes: [],
    errors: [],
    ms: undefined,
    raw_output: rawOutput,
    final_output: response.final_output,
  };
}

function pill(label, value) {
  return el("span", "pill", `${label}：${value}`);
}

function echoOf(record) {
  const text = String(record.raw_output ?? record.instruction ?? "").replace(/\s+/g, " ").trim();
  return text.length ? text.slice(0, 240) : "（空）";
}

function renderResult(record) {
  const panel = $("result-panel");
  panel.hidden = false;
  setText("result-id", record.request_id);
  setText("echo-input", echoOf(record));
  const status = statusOf(record);

  // 结论行只放"状态 + 分数"，其余指标降到详情区，主区因此保持安静。
  const verdict = $("verdict");
  clear(verdict);
  const state = el("span", `state ${status.cls}`, status.label);
  verdict.appendChild(state);
  verdict.appendChild(
    el("span", "bigscore", record.score === null || record.score === undefined ? "-" : Number(record.score).toFixed(3))
  );

  const meta = $("meta-chips");
  clear(meta);
  meta.appendChild(pill("repaired", String(Boolean(record.repaired))));
  meta.appendChild(pill("degraded", String(Boolean(record.degraded))));
  const usage = record.usage || {};
  meta.appendChild(pill("tokens", `${usage.prompt_tokens ?? 0} / ${usage.completion_tokens ?? 0}`));
  if (record.ms !== undefined && record.ms !== null) meta.appendChild(pill("耗时", `${record.ms} ms`));
  meta.appendChild(pill("Schema", (record.schema || "text").toLowerCase()));
  if (record.harness) {
    const info = record.harness;
    meta.appendChild(
      pill("harness", `${info.name} · ${info.ms}ms · ${info.source}${info.truncated ? " · 已截断" : ""}`)
    );
  }

  renderSteps(record.steps || []);
  renderDimensions(record.dimensions);
  renderTags(record);
}

function renderSteps(steps) {
  const box = $("steps");
  clear(box);
  if (!steps.length) {
    box.appendChild(el("div", "muted-note", "该记录没有步骤明细。"));
    return;
  }
  const maxMs = Math.max(1, ...steps.map((step) => Number(step.ms) || 0));
  steps.forEach((step) => {
    const row = el("div", "step");
    const name = el("span", "step-name");
    const dot = el("span", `dot ${step.passed ? "ok" : "bad"}`);
    name.appendChild(dot);
    name.appendChild(el("span", null, step.name));
    const bar = el("span", "bar");
    const fill = el("span");
    fill.style.width = `${Math.max(2, ((Number(step.ms) || 0) / maxMs) * 100)}%`;
    if (!step.passed) fill.style.background = "var(--warn)";
    bar.appendChild(fill);
    const scoreText = step.score === undefined || step.score === null ? "" : ` · ${Number(step.score).toFixed(2)}`;
    row.appendChild(name);
    row.appendChild(bar);
    row.appendChild(el("span", "step-ms", `${step.ms ?? 0}ms${scoreText}`));
    if (step.reason) {
      row.appendChild(el("div", "step-reason", step.reason));
    }
    box.appendChild(row);
  });
}

function renderDimensions(dimensions) {
  const box = $("dimensions");
  clear(box);
  const keys = dimensions ? Object.keys(dimensions) : [];
  if (!keys.length) {
    box.appendChild(el("div", "muted-note", "该记录未包含评分维度（较早的审计记录），或未启用评分步骤。"));
    return;
  }
  keys.forEach((key) => {
    const value = dimensions[key];
    const row = el("div", "dim");
    row.appendChild(el("span", "dim-name", DIMENSION_LABELS[key] || key));
    const bar = el("span", "bar");
    const fill = el("span");
    const ratio = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, Number(value)));
    fill.style.width = `${Math.round(ratio * 100)}%`;
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el("span", "dim-val", value === null || value === undefined ? "跳过" : ratio.toFixed(2)));
    if (value !== null && value !== undefined) {
      if (ratio === 0) row.classList.add("zero");
      else if (ratio < 0.8) row.classList.add("low");
    }
    box.appendChild(row);
  });
}

function renderTags(record) {
  const box = $("tags");
  clear(box);
  const changes = record.changes || [];
  const markers = record.markers || [];
  if (!changes.length && !markers.length) {
    box.appendChild(el("div", "muted-note", "无变更被采纳，也没有闸门拒绝标记。"));
  }
  changes.forEach((text) => box.appendChild(el("span", "tag", text)));
  markers.forEach((text) => box.appendChild(el("span", "tag marker", text)));

  const errors = $("errors");
  clear(errors);
  const list = (record.errors || []).filter(Boolean);
  if (list.length) errors.appendChild(el("div", null, `errors：${list.join("；")}`));
  if (record.degraded && record.degraded_reason) {
    errors.appendChild(el("div", null, `degraded：${record.degraded_reason}`));
  }
}

/* ---------- diff ---------- */

function prettyText(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      try {
        return JSON.stringify(JSON.parse(trimmed), null, 2);
      } catch (error) {
        return value;
      }
    }
    return value;
  }
  return JSON.stringify(value, null, 2);
}

function diffLines(before, after) {
  const n = before.length;
  const m = after.length;
  const table = [];
  for (let index = 0; index <= n; index += 1) table.push(new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i][j] = before[i] === after[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (before[i] === after[j]) {
      rows.push({ t: "eq", v: before[i], no: i + 1 });
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      rows.push({ t: "del", v: before[i], no: i + 1 });
      i += 1;
    } else {
      rows.push({ t: "add", v: after[j], no: j + 1 });
      j += 1;
    }
  }
  while (i < n) {
    rows.push({ t: "del", v: before[i], no: i + 1 });
    i += 1;
  }
  while (j < m) {
    rows.push({ t: "add", v: after[j], no: j + 1 });
    j += 1;
  }
  return rows;
}

function diffRow(row) {
  const node = el("div", `dl ${row.t}`);
  node.appendChild(el("span", "no", row.no));
  node.appendChild(el("span", "sign", row.t === "add" ? "+" : row.t === "del" ? "-" : ""));
  node.appendChild(el("span", null, row.v.length ? row.v : " "));
  return node;
}

function renderDiff(record) {
  const box = $("diff");
  clear(box);
  const before = prettyText(record.raw_output).split("\n");
  const after = prettyText(record.final_output).split("\n");
  if (before.length === 1 && !before[0] && after.length === 1 && !after[0]) {
    box.appendChild(el("div", "diff-note", "运行优化后在此对照。"));
    setText("diff-mode", "");
    return;
  }
  if (before.length > MAX_DIFF_LINES || after.length > MAX_DIFF_LINES) {
    box.appendChild(el("div", "diff-note", `行数超过 ${MAX_DIFF_LINES}，跳过逐行 diff，仅展示优化后结果。`));
    after.forEach((line, index) => box.appendChild(diffRow({ t: "eq", v: line, no: index + 1 })));
    setText("diff-mode", "已降级");
    return;
  }
  const rows = diffLines(before, after);
  const changed = rows.filter((row) => row.t !== "eq").length;
  rows.forEach((row) => box.appendChild(diffRow(row)));
  setText("diff-mode", changed ? `${changed} 行变更` : "无变更");
}

/* ---------- 表单 ---------- */

function setStatus(message, isError) {
  const node = $("form-status");
  node.textContent = message || "";
  node.classList.toggle("error", Boolean(isError));
}

function collectOptions() {
  const options = {};
  document.querySelectorAll('#options input[type="checkbox"]').forEach((input) => {
    options[input.dataset.key] = input.checked;
  });
  const timeoutRaw = $("field-timeout") ? $("field-timeout").value : "";
  const timeout = Number(timeoutRaw);
  if (timeoutRaw !== "" && Number.isFinite(timeout)) options.timeout_ms = Math.round(timeout);
  return options;
}

function collectPayload() {
  return {
    raw_output: $("field-raw").value,
    schema: $("field-schema").value || null,
    instruction: $("field-instruction").value,
    input_text: $("field-input-text").value,
    options: collectOptions(),
  };
}

function applyOptimizeResponse(response, fallbackRawOutput) {
  const record = recordById(response.request_id) || synthesizeRecord(response, fallbackRawOutput);
  state.selectedLine = record.line_no ?? null;
  renderResult(record);
  renderDiff(record);
  renderHistory();
  return record;
}

async function submitRun(event) {
  event.preventDefault();
  const button = $("run-btn");
  button.disabled = true;
  setStatus("运行中…");
  const payload = collectPayload();
  try {
    const response = await postJSON("/v1/optimize", payload);
    await loadHistory();
    const record = applyOptimizeResponse(response, payload.raw_output);
    setStatus(`完成 · ${record.request_id}`);
  } catch (error) {
    setStatus(`失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

function harnessInfo() {
  return (state.config && state.config.harness) || { enabled: false, options: [], name: null, timeout_ms: 0 };
}

function currentMode() {
  return $("optimize-form").dataset.mode || "paste";
}

function harnessAvailable() {
  const info = harnessInfo();
  return Boolean(info.enabled && (info.options || []).length);
}

function renderHarnessEntry() {
  const info = harnessInfo();
  const available = harnessAvailable();
  $("mode-harness").disabled = !available;
  $("field-task").disabled = !available;

  // 驱动不可用时把原因写清楚，而不是留个禁用按钮让人猜。
  if (!info.enabled) {
    $("harness-status").textContent =
      "驱动未启用：在 .env 设 HARNESS_ENABLED=true 后重启服务（可用 HARNESS_PROVIDERS 限定 profile）";
  } else if (!available) {
    $("harness-status").textContent = "已启用但没有可驱动的 profile：查看下方检测结果，或配置 OPENAI_API_KEY";
  } else {
    $("harness-status").textContent = "";
  }

  const harness = currentMode() === "harness";
  $("mode-hint").textContent = harness && !available ? "harness 驱动当前不可用" : MODE_HINTS[currentMode()];
  $("run-btn").textContent = harness ? "驱动 harness 并优化" : "运行优化";
  $("run-btn").disabled = harness && !available;
}

function focusInput() {
  const target = currentMode() === "harness" ? $("field-task") : $("field-raw");
  if (!target.disabled) target.focus();
}

function setMode(mode) {
  $("optimize-form").dataset.mode = mode;
  $("mode-paste").classList.toggle("on", mode === "paste");
  $("mode-harness").classList.toggle("on", mode === "harness");
  renderHarnessEntry();
}

function renderThemeButton() {
  const dark = document.documentElement.dataset.theme === "dark";
  const button = $("theme-toggle");
  button.textContent = dark ? "浅色" : "深色";
  button.setAttribute("aria-pressed", String(dark));
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    window.localStorage.setItem("optimize-theme", next);
  } catch (error) {
    // 禁用存储的浏览器下仍要让切换可用，只是不记忆选择。
  }
  renderThemeButton();
}

function renderHarnessOptions(options) {
  const select = $("field-harness");
  clear(select);
  const names = (options || harnessInfo().options) || [];
  if (!names.length) {
    const none = el("option", null, "（无可驱动 profile）");
    none.value = "";
    select.appendChild(none);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const preferred = harnessInfo().name;
  names.forEach((name) => {
    const option = el("option", null, name);
    option.value = name;
    if (name === preferred) option.selected = true;
    select.appendChild(option);
  });
}

function statusClass(item) {
  if (!item.drivable) return "unknown";
  if (item.status === "可执行") return item.version ? "ok" : "info";
  return "warn";
}

function renderDetected(data) {
  const box = $("harness-detected");
  clear(box);
  const items = data.detected || [];
  if (!items.length) {
    box.appendChild(el("div", "muted-note", "未检测到内置 profile 支持的 CLI。可在 .env 用 HARNESS_COMMAND 显式指定。"));
    return;
  }
  items.forEach((item) => {
    const row = el("div", "detected-row");
    row.appendChild(el("span", `dot ${statusClass(item)}`));
    row.appendChild(el("span", "det-name", item.profile));
    const bits = [item.status];
    if (item.version) bits.push(item.version);
    if (item.login) bits.push(item.login);
    if (item.detail) bits.push(item.detail);
    const detail = el("span", "det-detail", bits.join(" · "));
    detail.title = item.entry || "";
    row.appendChild(detail);
    box.appendChild(row);
  });
}

function renderProfileNotes() {
  const node = $("harness-notes");
  const selected = $("field-harness").value;
  const notes = (state.harnessNotes && state.harnessNotes[selected]) || [];
  node.textContent = notes.length ? notes.join("；") : PROFILE_FALLBACK_NOTES[selected] || "";
}

async function loadHarnesses(probe) {
  const button = $("harness-detect-btn");
  const statusNode = $("harness-status");
  button.disabled = true;
  if (probe) statusNode.textContent = "探测中…";
  try {
    const query = new URLSearchParams({ probe: probe ? "true" : "false" });
    const data = await getJSON(`/v1/harnesses?${query.toString()}`);
    state.harnessNotes = data.notes || {};
    renderDetected(data);
    renderProfileNotes();
    if (probe) statusNode.textContent = data.probed ? "探测完成" : "驱动未启用，已跳过探测";
  } catch (error) {
    clear($("harness-detected"));
    $("harness-detected").appendChild(el("div", "muted-note", `检测失败：${error.message}`));
  } finally {
    button.disabled = false;
  }
}

async function submitHarness() {
  const button = $("run-btn");
  const task = $("field-task").value;
  if (!task.trim()) {
    setStatus("请先填写 task", true);
    return;
  }
  button.disabled = true;
  $("harness-status").textContent = "";
  setStatus("harness 运行中…");
  const payload = {
    task,
    harness: $("field-harness").value || null,
    schema: $("field-schema").value || null,
    instruction: $("field-instruction").value,
    input_text: $("field-input-text").value,
    options: collectOptions(),
  };
  try {
    const response = await postJSON("/v1/run", payload);
    await loadHistory();
    const record = applyOptimizeResponse(response, "");
    const info = response.harness || {};
    setStatus(`完成 · ${record.request_id} · harness ${info.ms ?? "-"}ms`);
  } catch (error) {
    setStatus(`失败：${error.message}`, true);
  } finally {
    renderHarnessEntry();
  }
}

function dispatchRun(event) {
  event.preventDefault();
  if (currentMode() === "harness") {
    return submitHarness();
  }
  return submitRun(event);
}

function resetRun() {
  state.selectedLine = null;
  $("field-raw").value = "";
  $("field-task").value = "";
  $("field-instruction").value = "";
  $("field-input-text").value = "";
  $("field-schema").value = "";
  setText("echo-input", "");
  $("result-panel").hidden = true;
  clear($("diff"));
  $("diff").appendChild(el("div", "diff-note", "运行优化后在此对照。"));
  setText("diff-mode", "");
  setStatus("");
  renderHistory();
  focusInput();
}

/* ---------- 启动 ---------- */

async function init() {
  $("toggle-history").addEventListener("click", (event) => {
    const app = $("app");
    app.classList.toggle("collapsed");
    event.currentTarget.setAttribute("aria-expanded", String(!app.classList.contains("collapsed")));
  });
  $("new-run").addEventListener("click", resetRun);
  $("optimize-form").addEventListener("submit", dispatchRun);
  $("mode-paste").addEventListener("click", () => setMode("paste"));
  $("mode-harness").addEventListener("click", () => setMode("harness"));
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("harness-detect-btn").addEventListener("click", () => loadHarnesses(true));
  $("field-harness").addEventListener("change", renderProfileNotes);

  try {
    state.config = await getJSON("/v1/config");
  } catch (error) {
    setStatus(`读取 /v1/config 失败：${error.message}`, true);
    state.config = { schemas: {}, steps: {}, timeout_ms: 8000 };
  }
  renderSchemaOptions();
  renderOptionToggles();
  renderEnv();
  renderThemeButton();
  renderHarnessOptions();
  // setMode 会顺带刷新模式提示、按钮文案与 harness 可用性判断。
  setMode("paste");
  resetRun();
  await loadHistory();
  await refreshHealth();
  await loadHarnesses(false);
}

document.addEventListener("DOMContentLoaded", init);
