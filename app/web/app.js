/* 本地查看页逻辑。
 *
 * 安全约束：raw_output、模型输出与审计内容都是不可信文本，一律通过 textContent
 * 写入 DOM，不做任何 innerHTML 拼接。
 */

const MAX_DIFF_LINES = 400;
const SVG_NS = "http://www.w3.org/2000/svg";
const DIAL_RADIUS = 52;
const DIAL_CIRCUMFERENCE = 2 * Math.PI * DIAL_RADIUS;
const THEME_KEY = "output-better-theme";

const OPTION_FIELDS = [
  // key 是请求体 options 的字段名（服务端 Options.score），step 是管线步骤名（rules）：
  // 两者混为一谈过一次，勾选"评分"因此完全无效且零告警。
  { key: "score", label: "评分 rules", step: "rules" },
  { key: "repair", label: "修复 repair", step: "repair" },
  { key: "format", label: "格式化 format", step: "format" },
  { key: "simplify", label: "去防御化 simplify", step: "simplify" },
  { key: "strict", label: "严格 strict", step: "schema", from: "strict" },
];

const DIMENSION_LABELS = {
  completeness: "字段完整度",
  format: "格式合规",
  grounding: "与输入一致性",
  preservation: "与原文一致性",
  plainness: "通俗度",
  confidence: "自信度",
  concision: "简洁度",
};

// 留痕尾部分组：分值只说明"不好读"，标签才指出哪句是废话、哪句是真边界。
const REASON_TAGS = { 命中: "hit", 删除无据: "cut", 保留边界: "keep" };

const PIPE_ORDER = ["schema", "rules", "repair", "format", "simplify"];
const STEP_ICON = {
  schema: "i-schema",
  rules: "i-gauge",
  repair: "i-build",
  format: "i-format",
  simplify: "i-sparkle",
};
const STEP_HUE = {
  schema: "--blue-bright",
  rules: "--green-bright",
  repair: "--yellow",
  format: "--red-bright",
  simplify: "--orange",
};
const STEP_OPTION = { schema: "strict", rules: "score", repair: "repair", format: "format", simplify: "simplify" };
const SORTS = [
  { key: "new", label: "最新优先" },
  { key: "old", label: "最早优先" },
  { key: "score", label: "分数优先" },
];

const SAMPLE = {
  schema: "paper",
  instruction: "抽取论文元数据，参考文献按 GB/T 7714 规范",
  input_text:
    "《基于深度学习的图像分类方法研究》，张三、李四，发表于 CVPR 2023。关键词：深度学习；图像分类；卷积神经网络。" +
    "参考文献：Zhang San, Li Si. Research on image classification based on deep learning. CVPR 2023.",
  raw_output:
    '{"title":"基于深度学习的图像分类方法研究","authors":"张三、李四","year":"2023",' +
    '"keywords":"深度学习;图像分类;卷积神经网络","reference":"Zhang San, Li Si. Research on image classification ' +
    'based on deep learning. CVPR 2023.","note":"如需更多论文信息请随时告诉我"}',
};

const state = {
  config: null,
  items: [],
  selectedLine: null,
  harnessNotes: {},
  auditTotal: 0,
  auditError: "",
  query: "",
  sort: "new",
  currentRecord: null,
  diffRecord: null,
  diffRows: [],
  diffDegraded: false,
  diffNote: "",
  diffView: "unified",
  undoSnapshot: null,
};

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function icon(name, className) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", className ? `icon ${className}` : "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#${name}`);
  svg.appendChild(use);
  return svg;
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

/* ---------- 主题 ---------- */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const button = $("theme-toggle");
  button.title = theme === "dark" ? "切换到浅色主题" : "切换到深色主题";
  button.setAttribute("aria-pressed", String(theme === "dark"));
}

function initTheme() {
  let saved = null;
  try {
    saved = window.localStorage.getItem(THEME_KEY);
  } catch (error) {
    saved = null;
  }
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved === "dark" || saved === "light" ? saved : prefersDark ? "dark" : "light");
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  try {
    window.localStorage.setItem(THEME_KEY, next);
  } catch (error) {
    /* 隐私模式下写不进去也不影响本次切换 */
  }
}

/* ---------- Material 反馈：涟漪与 snackbar ---------- */

const RIPPLE_SELECTOR = ".btn, .icon-btn, .compose, .seg, .sort-btn, .history-item, .opt, .pipe-node";

function initRipple() {
  document.addEventListener("pointerdown", (event) => {
    const host = event.target instanceof Element ? event.target.closest(RIPPLE_SELECTOR) : null;
    if (!host || host.disabled) return;
    const rect = host.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    if (!size) return;
    const wave = el("span", "ripple");
    wave.style.width = `${size}px`;
    wave.style.height = `${size}px`;
    wave.style.left = `${event.clientX - rect.left - size / 2}px`;
    wave.style.top = `${event.clientY - rect.top - size / 2}px`;
    host.appendChild(wave);
    wave.addEventListener("animationend", () => wave.remove());
  });
}

let snackTimer = null;

function snack(message, action) {
  const bar = $("snackbar");
  const button = $("snackbar-action");
  setText("snackbar-text", message);
  clear(button);
  if (action) {
    button.hidden = false;
    button.textContent = action.label;
    button.onclick = () => {
      hideSnack();
      action.run();
    };
  } else {
    button.hidden = true;
    button.onclick = null;
  }
  bar.hidden = false;
  void bar.offsetWidth;
  bar.classList.add("is-open");
  if (snackTimer) window.clearTimeout(snackTimer);
  snackTimer = window.setTimeout(hideSnack, action ? 6000 : 3200);
}

function hideSnack() {
  const bar = $("snackbar");
  bar.classList.remove("is-open");
  if (snackTimer) window.clearTimeout(snackTimer);
  snackTimer = window.setTimeout(() => {
    bar.hidden = true;
  }, 260);
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
    input.setAttribute("aria-label", field.label);
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
  const refStyle = el("label", "opt-timeout");
  refStyle.appendChild(el("span", null, "引用风格"));
  const styleSelect = el("select");
  styleSelect.id = "field-reference-style";
  styleSelect.setAttribute("aria-label", "引用风格（每请求覆盖配置）");
  [["", "跟随配置"], ["gb-t-7714", "GB/T 7714"], ["apa", "APA"]].forEach(([value, label]) => {
    const option = el("option", null, label);
    option.value = value;
    styleSelect.appendChild(option);
  });
  refStyle.appendChild(styleSelect);
  box.appendChild(refStyle);
  syncOptionState();
}

function optionInput(key) {
  return document.querySelector(`#options input[data-key="${key}"]`);
}

function syncOptionState() {
  document.querySelectorAll("#options .opt").forEach((label) => {
    const input = label.querySelector("input");
    label.classList.toggle("is-on", Boolean(input && input.checked));
  });
  updatePipeStates();
}

function renderEnv(auditTotal, totalExact = true) {
  const list = $("env");
  clear(list);
  const config = state.config || {};
  const auditLabel =
    auditTotal === undefined ? "-" : totalExact ? `${auditTotal} 条` : `≥ ${auditTotal} 条（尾部窗口）`;
  const rows = [
    ["provider", config.provider || "-"],
    ["score 阈值", config.score_threshold === undefined ? "-" : config.score_threshold],
    ["timeout", `${config.timeout_ms ?? "-"} ms`],
    ["修复上限", config.max_repair_retries ?? "-"],
    ["fail_open", String(config.fail_open)],
    ["引用风格", config.reference_style || "-"],
    ["审计条数", auditLabel],
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
  // "写了 .env 但不生效"必须在这里看得见：这些组合不会报错，只会在半年后变成一个谜。
  const warnings = config.warnings || [];
  if (warnings.length) {
    list.appendChild(el("dt", null, "配置告警"));
    list.appendChild(el("dd", "env-warn", warnings.join("；")));
  }
}

async function refreshHealth() {
  const dot = $("health-dot");
  dot.className = "dot";
  try {
    const health = await getJSON("/healthz");
    const ok = health.status === "ok";
    dot.classList.add(ok ? "ok" : "warn");
    dot.title = `provider=${health.provider}`;
    setText("health-text", `${health.provider} · ${ok ? "正常" : "降级"}`);
    $("health-pill").title = `provider=${health.provider} · status=${health.status}`;
  } catch (error) {
    dot.classList.add("bad");
    dot.title = String(error.message || error);
    setText("health-text", "服务不可达");
  }
}

/* ---------- hero：流水线图与统计 ---------- */

function renderPipe() {
  const pipe = $("pipe");
  clear(pipe);
  PIPE_ORDER.forEach((name, index) => {
    if (index > 0) {
      const link = el("li", "pipe-link");
      link.setAttribute("aria-hidden", "true");
      link.style.setProperty("--from", `var(${STEP_HUE[PIPE_ORDER[index - 1]]})`);
      link.style.setProperty("--to", `var(${STEP_HUE[name]})`);
      pipe.appendChild(link);
    }
    const item = el("li", "pipe-step");
    item.dataset.step = name;
    const node = el("button", "pipe-node");
    node.type = "button";
    node.dataset.step = name;
    const dot = el("span", "pipe-dot");
    dot.appendChild(icon(STEP_ICON[name] || "i-layers"));
    node.appendChild(dot);
    node.appendChild(el("span", "pipe-name", name));
    node.appendChild(el("span", "pipe-state", ""));
    node.addEventListener("click", () => {
      const key = STEP_OPTION[name];
      const input = key ? optionInput(key) : null;
      if (!input) return;
      input.checked = !input.checked;
      syncOptionState();
    });
    item.appendChild(node);
    pipe.appendChild(item);
  });
  updatePipeStates();
}

function updatePipeStates() {
  document.querySelectorAll(".pipe-node").forEach((node) => {
    const name = node.dataset.step;
    const input = optionInput(STEP_OPTION[name]);
    if (name === "schema") {
      // schema 步骤常开，节点永远点亮；strict 只是它的一个开关，用双环与文案表达。
      const strict = Boolean(input && input.checked);
      node.classList.add("is-on");
      node.classList.toggle("is-strict", strict);
      node.querySelector(".pipe-state").textContent = strict ? "strict 严格" : "常开";
      node.title = strict ? "schema 常开，当前 strict 严格模式；点击关闭" : "schema 常开；点击开启 strict 严格模式";
      node.setAttribute("aria-pressed", String(strict));
      return;
    }
    const on = Boolean(input && input.checked);
    node.classList.toggle("is-on", on);
    node.querySelector(".pipe-state").textContent = on ? "启用" : "关闭";
    node.title = `${name} 当前${on ? "启用" : "关闭"}，点击切换（与下方开关联动）`;
    node.setAttribute("aria-pressed", String(on));
  });
}

const tweens = new Map();

function tweenValue(id, target, format) {
  const node = $(id);
  const previous = tweens.get(id);
  if (previous) window.cancelAnimationFrame(previous.frame);
  const from = Number(node.dataset.raw || 0);
  if (!Number.isFinite(target)) {
    node.dataset.raw = "0";
    node.textContent = "–";
    tweens.delete(id);
    return;
  }
  const start = performance.now();
  const duration = 520;
  const tick = (now) => {
    const progress = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    const value = from + (target - from) * eased;
    node.textContent = format(value);
    if (progress < 1) {
      tweens.set(id, { frame: window.requestAnimationFrame(tick) });
    } else {
      node.dataset.raw = String(target);
      node.textContent = format(target);
      tweens.delete(id);
    }
  };
  node.dataset.raw = String(target);
  node.textContent = format(target);
  tweens.set(id, { frame: window.requestAnimationFrame(tick) });
}

function renderStats() {
  const items = state.items;
  const scored = items.map((item) => Number(item.score)).filter((value) => Number.isFinite(value));
  const average = scored.length ? scored.reduce((sum, value) => sum + value, 0) / scored.length : NaN;
  const judged = items.filter((item) => typeof item.valid === "boolean");
  const passed = judged.filter((item) => item.valid && !item.degraded).length;
  const passRate = judged.length ? (passed / judged.length) * 100 : NaN;
  const times = items
    .map((item) => Number(item.ms))
    .filter((value) => Number.isFinite(value) && value > 0)
    .sort((a, b) => a - b);
  const median = times.length ? times[Math.floor(times.length / 2)] : NaN;

  tweenValue("stat-total", state.auditTotal || items.length, (value) => String(Math.round(value)));
  tweenValue("stat-avg", average, (value) => value.toFixed(3));
  tweenValue("stat-pass", passRate, (value) => `${Math.round(value)}%`);
  tweenValue("stat-ms", median, (value) => `${Math.round(value)}ms`);

  const scope = `统计口径：最近载入的 ${items.length} 条审计记录`;
  $("stat-avg").title = scope;
  $("stat-pass").title = `${scope}；通过 = valid 且未降级`;
  $("stat-ms").title = scope;
  $("stat-total").title = "审计文件中的记录总数";
}

/* ---------- 历史列表 ---------- */

function statusOf(record) {
  if (record.degraded) return { cls: "warn", label: "降级" };
  if (!record.valid) return { cls: "bad", label: "未通过" };
  return { cls: "ok", label: "通过" };
}

/**
 * 修复/重写各自的采纳结果。它们与 valid 是两件事：一次运行可能同时
 * “修复被闸门拒绝”与“去防御化改写被采纳”，标签只表达通过与否，细节进 flags。
 */
function flagsOf(record) {
  const markers = record.markers || [];
  const flags = [];
  if (markers.includes("repair_rejected")) flags.push("修复被拒");
  else if (record.repaired) flags.push("已修复");
  if (markers.includes("simplify_rejected")) flags.push("重写被拒");
  else if ((record.changes || []).some((text) => text.includes("通俗化重写"))) flags.push("已重写");
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

function haystack(record) {
  const final = record.final_output;
  return [
    record.raw_output,
    record.instruction,
    record.request_id,
    record.schema,
    record.provider,
    typeof final === "string" ? final : JSON.stringify(final ?? ""),
  ]
    .map((part) => String(part ?? "").toLowerCase())
    .join("\n");
}

function visibleItems() {
  let items = state.items.slice();
  const query = state.query.trim().toLowerCase();
  if (query) items = items.filter((item) => haystack(item).includes(query));
  if (state.sort === "old") items.reverse();
  else if (state.sort === "score") {
    items.sort((a, b) => (Number(b.score) || -1) - (Number(a.score) || -1));
  }
  return items;
}

function renderHistory() {
  const list = $("history-list");
  const fragment = document.createDocumentFragment();
  const template = $("tpl-history-item");
  const items = visibleItems();
  clear(list);
  setText("rail-count", String(items.length));
  if (!items.length) {
    const note = el(
      "div",
      "muted-note",
      state.query.trim()
        ? `没有匹配「${state.query.trim()}」的记录。`
        : state.auditError
          ? "审计读取失败，无法列出记录（原因见抽屉底部）。"
          : "暂无审计记录。运行一次优化即可在此留痕。"
    );
    note.style.padding = "10px 16px";
    list.appendChild(note);
    if (state.query.trim()) {
      const reset = el("button", "btn btn-text btn-sm", "清除搜索");
      reset.type = "button";
      reset.style.margin = "0 12px";
      reset.addEventListener("click", () => {
        state.query = "";
        $("history-search").value = "";
        renderHistory();
      });
      list.appendChild(reset);
    }
  }
  items.forEach((record) => {
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
    node.title = record.request_id || "";
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
    state.auditTotal = data.total_lines || 0;
    // 后端读不动审计时返回 200＋error（只坏历史面板，不 500 整页）：
    // 不展示就会伪装成"暂无记录"，把配置/权限问题藏成空列表。
    state.auditError = String(data.error || "");
    const exact = data.total_exact !== false;
    const totalText = exact ? `${data.total_lines} 条` : `尾部 ${data.total_lines} 条（窗口下界）`;
    foot.textContent = state.auditError
      ? state.auditError
      : `${state.items.length} / ${totalText}${data.skipped ? ` · 跳过损坏 ${data.skipped}` : ""}`;
    foot.classList.toggle("error", Boolean(state.auditError));
    renderEnv(data.total_lines, exact);
  } catch (error) {
    state.items = [];
    state.auditError = error.message;
    foot.textContent = `历史读取失败：${error.message}`;
    foot.classList.add("error");
  }
  renderHistory();
  renderStats();
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
  if (isNarrow()) setDrawer(false);
  $("result-panel").scrollIntoView({ block: "nearest", behavior: "smooth" });
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

function chip(label, value) {
  const node = el("span", "chip");
  node.appendChild(el("span", null, `${label}：`));
  node.appendChild(el("b", null, value));
  return node;
}

function scoreTone(ratio) {
  const threshold = Number(state.config && state.config.score_threshold);
  const limit = Number.isFinite(threshold) ? threshold : 0.8;
  if (ratio >= limit) return { hue: "var(--green-bright)", cls: "ok" };
  if (ratio >= limit * 0.6) return { hue: "var(--yellow)", cls: "warn" };
  return { hue: "var(--red-bright)", cls: "bad" };
}

function renderDial(record) {
  const arc = $("dial-arc");
  const raw = record.score;
  const hasScore = raw !== null && raw !== undefined && Number.isFinite(Number(raw));
  const ratio = hasScore ? Math.max(0, Math.min(1, Number(raw))) : 0;
  const tone = scoreTone(ratio);
  arc.style.strokeDasharray = String(DIAL_CIRCUMFERENCE);
  arc.style.strokeDashoffset = String(DIAL_CIRCUMFERENCE * (1 - ratio));
  arc.style.stroke = hasScore ? tone.hue : "var(--text-3)";
  setText("dial-value", hasScore ? ratio.toFixed(3) : "–");
  $("dial-value").style.color = hasScore ? tone.hue : "var(--text-3)";
  const threshold = Number(state.config && state.config.score_threshold);
  const limit = Number.isFinite(threshold) ? threshold : 0.8;
  $("dial-threshold").setAttribute("transform", `rotate(${limit * 360} 60 60)`);
  $("dial").title = hasScore ? `score ${ratio.toFixed(3)} · 采纳阈值 ${limit}` : "该记录没有 score";
  setText("threshold-note", `采纳阈值 ${limit.toFixed(2)} · 环上刻度即阈值位置`);
}

function renderResult(record) {
  const panel = $("result-panel");
  panel.hidden = false;
  state.currentRecord = record;
  setText("result-id", record.request_id);
  const status = statusOf(record);

  const verdict = $("verdict");
  clear(verdict);
  const statusChip = chip("状态", status.label);
  statusChip.classList.add(status.cls);
  verdict.appendChild(statusChip);
  verdict.appendChild(chip("score", record.score === null || record.score === undefined ? "-" : Number(record.score).toFixed(3)));
  verdict.appendChild(chip("repaired", String(Boolean(record.repaired))));
  verdict.appendChild(chip("degraded", String(Boolean(record.degraded))));
  const usage = record.usage || {};
  verdict.appendChild(chip("tokens", `${usage.prompt_tokens ?? 0} / ${usage.completion_tokens ?? 0}`));
  if (record.ms !== undefined && record.ms !== null) verdict.appendChild(chip("耗时", `${record.ms} ms`));
  verdict.appendChild(chip("Schema", (record.schema || "text").toLowerCase()));
  if (record.harness) {
    const info = record.harness;
    verdict.appendChild(
      chip("harness", `${info.name} · ${info.ms}ms · ${info.source}${info.truncated ? " · 已截断" : ""}`)
    );
  }

  renderDial(record);
  renderSteps(record.steps || []);
  renderDimensions(record.dimensions);
  renderTags(record);
}

function renderReason(reason) {
  const box = el("div", "step-reason");
  const parts = String(reason).split("｜");
  box.textContent = parts.shift();
  parts.forEach((part) => {
    const at = part.indexOf("：");
    if (at < 0) {
      box.appendChild(el("span", "reason-note", part));
      return;
    }
    const label = part.slice(0, at);
    if (label === "主张先行") {
      box.appendChild(el("span", `tag claim ${part.endsWith("是") ? "on" : "off"}`, part));
      return;
    }
    const style = REASON_TAGS[label];
    if (!style) return;
    part
      .slice(at + 1)
      .split("、")
      .forEach((item) => box.appendChild(el("span", `tag ${style}`, item)));
  });
  return box;
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
    const key = String(step.name || "").toLowerCase();
    const row = el("div", "step");
    row.dataset.step = key;
    if (!step.passed) row.classList.add("is-fail");
    row.title = step.passed ? "通过" : "未通过";
    const name = el("span", "step-name");
    const badge = el("span", "step-ico");
    badge.appendChild(icon(STEP_ICON[key] || "i-layers"));
    name.appendChild(badge);
    name.appendChild(el("span", null, step.name));
    const bar = el("span", "bar");
    const fill = el("span");
    // mock 下各步耗时为 0，条长退化为得分；有真实耗时时仍按耗时占比。
    const hasScore = step.score !== undefined && step.score !== null && Number.isFinite(Number(step.score));
    const ratio = hasScore
      ? Math.max(0, Math.min(1, Number(step.score)))
      : Math.max(0.02, (Number(step.ms) || 0) / maxMs);
    fill.style.width = `${Math.max(2, ratio * 100)}%`;
    bar.title = hasScore
      ? `步骤得分 ${Number(step.score).toFixed(2)} · 耗时 ${step.ms ?? 0}ms`
      : `耗时占比 ${step.ms ?? 0}ms / ${maxMs}ms`;
    if (!step.passed) fill.style.background = "var(--warn)";
    bar.appendChild(fill);
    const scoreText = step.score === undefined || step.score === null ? "" : ` · ${Number(step.score).toFixed(2)}`;
    row.appendChild(name);
    row.appendChild(bar);
    row.appendChild(el("span", "step-ms", `${step.ms ?? 0}ms${scoreText}`));
    if (step.reason) {
      row.appendChild(renderReason(step.reason));
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
    row.dataset.dim = key;
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

function splitPairs(rows) {
  const pairs = [];
  let index = 0;
  while (index < rows.length) {
    if (rows[index].t === "eq") {
      pairs.push({ left: rows[index], right: rows[index] });
      index += 1;
      continue;
    }
    const dels = [];
    const adds = [];
    while (index < rows.length && rows[index].t === "del") dels.push(rows[index++]);
    while (index < rows.length && rows[index].t === "add") adds.push(rows[index++]);
    const count = Math.max(dels.length, adds.length);
    for (let k = 0; k < count; k += 1) pairs.push({ left: dels[k] || null, right: adds[k] || null });
  }
  return pairs;
}

function splitCell(row, side) {
  if (!row) {
    const empty = el("span", "cell empty", " ");
    empty.title = side === "left" ? "优化前无对应行" : "优化后无对应行";
    return empty;
  }
  return el("span", `cell ${row.t}`, row.v.length ? row.v : " ");
}

function splitRow(pair) {
  const node = el("div", "dl split");
  node.appendChild(el("span", "no", pair.left ? pair.left.no : ""));
  node.appendChild(splitCell(pair.left, "left"));
  node.appendChild(el("span", "no", pair.right ? pair.right.no : ""));
  node.appendChild(splitCell(pair.right, "right"));
  return node;
}

function effectiveDiffView() {
  return state.diffView === "split" && !state.diffDegraded && state.diffRows.length ? "split" : "unified";
}

/* 选项的高亮/禁用必须与真正渲染的视图同源：降级或空对照时并排不可用，
   但保留用户偏好，回到正常记录时自动恢复并排。 */
function syncDiffControls() {
  const canSplit = Boolean(state.diffRows.length) && !state.diffDegraded;
  const view = effectiveDiffView();
  document.querySelectorAll("#diff-view .seg").forEach((button) => {
    const isSplit = button.dataset.view === "split";
    button.disabled = isSplit && !canSplit;
    button.classList.toggle("is-on", button.dataset.view === view);
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
    if (isSplit) {
      button.title = canSplit
        ? ""
        : state.diffDegraded
          ? `行数超过 ${MAX_DIFF_LINES}，已跳过逐行 diff`
          : "运行优化后可切换并排";
    }
  });
}

function paintDiff() {
  const box = $("diff");
  clear(box);
  const rows = state.diffRows;
  const split = effectiveDiffView() === "split";
  syncDiffControls();
  box.classList.toggle("is-split", split);
  if (state.diffNote) box.appendChild(el("div", "diff-note", state.diffNote));
  if (!rows.length) {
    if (!state.diffNote) box.appendChild(el("div", "diff-note", "运行优化后在此对照。"));
    setText("diff-mode", "");
    return;
  }
  if (split) {
    const head = el("div", "dl split-head");
    head.appendChild(el("span", null, "优化前"));
    head.appendChild(el("span", null, "优化后"));
    box.appendChild(head);
    splitPairs(rows).forEach((pair) => box.appendChild(splitRow(pair)));
  } else {
    rows.forEach((row) => box.appendChild(diffRow(row)));
  }
  const changed = rows.filter((row) => row.t !== "eq").length;
  setText("diff-mode", state.diffDegraded ? "已降级" : changed ? `${changed} 行变更` : "无变更");
}

function renderDiff(record) {
  state.diffRecord = record;
  const before = prettyText(record.raw_output).split("\n");
  const after = prettyText(record.final_output).split("\n");
  state.diffNote = "";
  if (before.length === 1 && !before[0] && after.length === 1 && !after[0]) {
    state.diffRows = [];
    state.diffDegraded = false;
    paintDiff();
    return;
  }
  if (before.length > MAX_DIFF_LINES || after.length > MAX_DIFF_LINES) {
    state.diffRows = after.map((line, index) => ({ t: "eq", v: line, no: index + 1 }));
    state.diffDegraded = true;
    state.diffNote = `行数超过 ${MAX_DIFF_LINES}，跳过逐行 diff，仅展示优化后结果。`;
    paintDiff();
    return;
  }
  state.diffRows = diffLines(before, after);
  state.diffDegraded = false;
  paintDiff();
}

function setDiffView(view) {
  state.diffView = view;
  paintDiff();
}

/* ---------- 表单 ---------- */

function setStatus(message, isError) {
  const node = $("form-status");
  node.textContent = message || "";
  node.classList.toggle("error", Boolean(isError));
}

/* ---------- 运行进度 ---------- */

let progressTimer = null;
let progressGen = 0;

function stopProgress() {
  progressGen += 1;
  if (progressTimer !== null) {
    clearInterval(progressTimer);
    progressTimer = null;
  }
  document.querySelectorAll(".pipe-node.is-live").forEach((node) => node.classList.remove("is-live"));
}

async function pollProgress(kind, statusNode, gen) {
  try {
    const data = await getJSON("/v1/progress");
    // 收尾与轮询存在竞态：已在飞的这次请求回来时运行可能已结束，
    // 迟到的阶段文本不得覆盖"完成/失败"结论，也不得重新点亮脉冲。
    if (gen !== progressGen) return;
    const active = (data.active || []).filter((run) => run.kind === kind);
    const run = active[active.length - 1];
    if (!run) return;
    const label = run.phase === "harness" ? "harness 运行中" : run.phase === "pipeline" ? "管线启动" : `管线 ${run.phase}`;
    statusNode.textContent = `${label}… ${(run.elapsed_ms / 1000).toFixed(1)}s`;
    document.querySelectorAll(".pipe-node").forEach((node) => {
      node.classList.toggle("is-live", node.dataset.step === run.phase);
    });
  } catch (error) {
    // 进度只是解释层：轮询失败不该把主流程染成错误，也不该打断提交
  }
}

function startProgress(kind, statusNode) {
  stopProgress();
  const gen = progressGen;
  pollProgress(kind, statusNode, gen);
  progressTimer = setInterval(() => pollProgress(kind, statusNode, gen), 400);
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
  const payload = {
    raw_output: $("field-raw").value,
    schema: $("field-schema").value || null,
    instruction: $("field-instruction").value,
    input_text: $("field-input-text").value,
    options: collectOptions(),
  };
  const style = $("field-reference-style").value;
  if (style) payload.reference_style = style;
  return payload;
}

function applyOptimizeResponse(response, fallbackRawOutput) {
  const record = recordById(response.request_id) || synthesizeRecord(response, fallbackRawOutput);
  state.selectedLine = record.line_no ?? null;
  renderResult(record);
  renderDiff(record);
  renderHistory();
  return record;
}

function requestRun() {
  const form = $("optimize-form");
  if ($("run-btn").disabled) return;
  if (typeof form.requestSubmit === "function") form.requestSubmit();
  else $("run-btn").click();
}

async function submitRun(event) {
  event.preventDefault();
  const button = $("run-btn");
  button.disabled = true;
  setStatus("运行中…");
  startProgress("optimize", $("form-status"));
  const payload = collectPayload();
  try {
    const response = await postJSON("/v1/optimize", payload);
    await loadHistory();
    const record = applyOptimizeResponse(response, payload.raw_output);
    setStatus(`完成 · ${record.request_id}`);
    snack(`优化完成 · score ${record.score === null || record.score === undefined ? "–" : Number(record.score).toFixed(3)}`);
    $("result-panel").scrollIntoView({ block: "nearest", behavior: "smooth" });
  } catch (error) {
    setStatus(`失败：${error.message}`, true);
    snack(`运行失败：${error.message}`, { label: "重试", run: requestRun });
  } finally {
    stopProgress();
    button.disabled = false;
  }
}

function fillSample() {
  $("field-schema").value = SAMPLE.schema;
  $("field-instruction").value = SAMPLE.instruction;
  $("field-input-text").value = SAMPLE.input_text;
  $("field-raw").value = SAMPLE.raw_output;
  setStatus("已填入示例，可直接运行");
  $("section-input").scrollIntoView({ block: "start", behavior: "smooth" });
  snack("已填入示例：paper schema + 一条待规范化的 CVPR 引用");
}

async function copyOutput() {
  const record = state.currentRecord;
  if (!record) {
    snack("还没有可复制的结果");
    return;
  }
  const value = record.final_output;
  const text = typeof value === "string" ? value : JSON.stringify(value ?? null, null, 2);
  try {
    await navigator.clipboard.writeText(text);
    snack("已复制优化后输出到剪贴板");
  } catch (error) {
    const helper = el("textarea");
    helper.value = text;
    helper.setAttribute("readonly", "readonly");
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (inner) {
      ok = false;
    }
    helper.remove();
    snack(ok ? "已复制优化后输出到剪贴板" : "复制失败：请手动选择文本");
  }
}

function rerunCurrent() {
  const record = state.currentRecord;
  if (!record) {
    snack("还没有可重跑的记录");
    return;
  }
  $("field-raw").value = String(record.raw_output ?? "");
  $("field-instruction").value = String(record.instruction ?? "");
  $("field-input-text").value = String(record.input_text ?? "");
  const schemaName = String(record.schema || "").toLowerCase();
  $("field-schema").value = Object.keys(state.config.schemas).includes(schemaName) ? schemaName : "";
  requestRun();
}

/* ---------- harness ---------- */

function harnessInfo() {
  return (state.config && state.config.harness) || { enabled: false, options: [], name: null, timeout_ms: 0 };
}

function renderHarnessEntry() {
  const info = harnessInfo();
  const button = $("harness-run-btn");
  const task = $("field-task");
  const options = info.options || [];
  button.disabled = !info.enabled || !options.length;
  task.disabled = button.disabled;
  if (!info.enabled) {
    $("harness-hint").textContent =
      "未启用：在 .env 设 HARNESS_ENABLED=true 后重启服务（可选 HARNESS_PROVIDERS 限定 profile）";
  } else if (!options.length) {
    $("harness-hint").textContent = "已启用但没有可驱动的 harness：检查下方检测结果或 API 密钥";
  } else {
    $("harness-hint").textContent =
      `已装配 ${options.length} 个 profile（默认 ${info.name}）· 超时 ${info.timeout_ms}ms · 并发上限 ${info.max_concurrency}`;
  }
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
  node.textContent = notes.length ? notes.join("；") : "";
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
    if (probe) {
      const cached = Number(data.probe_cached || 0);
      statusNode.textContent = data.probed
        ? `探测完成${cached ? ` · 缓存 ${cached} 项（有效期内直接复用，不重复起进程）` : ""}`
        : "驱动未启用，已跳过探测";
      statusNode.classList.remove("error");
    }
  } catch (error) {
    clear($("harness-detected"));
    $("harness-detected").appendChild(el("div", "muted-note", `检测失败：${error.message}`));
  } finally {
    button.disabled = false;
  }
}

async function submitHarness() {
  const button = $("harness-run-btn");
  const statusNode = $("harness-status");
  const task = $("field-task").value;
  if (!task.trim()) {
    statusNode.textContent = "请先填写 task";
    statusNode.classList.add("error");
    return;
  }
  button.disabled = true;
  statusNode.classList.remove("error");
  startProgress("run", statusNode);
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
    statusNode.textContent = `完成 · ${record.request_id} · harness ${info.ms ?? "-"}ms`;
    $("form-status").textContent = "";
    snack(`harness 完成 · ${info.name || "profile"} · ${info.ms ?? "-"}ms`);
  } catch (error) {
    statusNode.textContent = `失败：${error.message}`;
    statusNode.classList.add("error");
    snack(`harness 失败：${error.message}`);
  } finally {
    stopProgress();
    renderHarnessEntry();
  }
}

function resetRun(announce = false) {
  const snapshot = {
    raw: $("field-raw").value,
    instruction: $("field-instruction").value,
    inputText: $("field-input-text").value,
    schema: $("field-schema").value,
    refStyle: $("field-reference-style").value,
    selectedLine: state.selectedLine,
  };
  const hadContent = Boolean(snapshot.raw || snapshot.instruction || snapshot.inputText);
  state.selectedLine = null;
  state.currentRecord = null;
  state.undoSnapshot = snapshot;
  $("field-raw").value = "";
  $("field-instruction").value = "";
  $("field-input-text").value = "";
  $("field-schema").value = "";
  $("field-reference-style").value = "";
  $("result-panel").hidden = true;
  state.diffRows = [];
  state.diffDegraded = false;
  state.diffNote = "";
  state.diffRecord = null;
  paintDiff();
  setStatus("");
  renderHistory();
  $("field-raw").focus({ preventScroll: true });
  if (announce) {
    if (hadContent) {
      snack("已新建空白会话", {
        label: "撤销",
        run: () => {
          $("field-raw").value = snapshot.raw;
          $("field-instruction").value = snapshot.instruction;
          $("field-input-text").value = snapshot.inputText;
          $("field-schema").value = snapshot.schema;
          $("field-reference-style").value = snapshot.refStyle;
          state.selectedLine = snapshot.selectedLine;
          const record = state.items.find((item) => item.line_no === snapshot.selectedLine);
          if (record) {
            renderResult(record);
            renderDiff(record);
          }
          renderHistory();
          snack("已恢复上一份输入");
        },
      });
    } else {
      snack("已新建空白会话");
    }
  }
}

/* ---------- 导航与抽屉 ---------- */

const NAV_IDS = ["section-hero", "harness-panel", "section-input", "result-panel"];

function isNarrow() {
  return window.matchMedia("(max-width: 1040px)").matches;
}

let scrimTimer = null;

function setDrawer(open) {
  const app = $("app");
  const scrim = $("scrim");
  app.classList.toggle("drawer-open", open);
  $("menu-btn").setAttribute("aria-expanded", String(open));
  if (scrimTimer) window.clearTimeout(scrimTimer);
  if (open) {
    scrim.hidden = false;
    void scrim.offsetWidth;
    scrim.classList.add("is-open");
  } else {
    scrim.classList.remove("is-open");
    scrimTimer = window.setTimeout(() => {
      scrim.hidden = true;
    }, 260);
  }
}

function setActiveNav(id) {
  document.querySelectorAll(".appnav-link").forEach((link) => {
    link.classList.toggle("is-active", link.dataset.nav === id);
  });
}

function updateActiveNav() {
  let best = null;
  let bestScore = -Infinity;
  NAV_IDS.forEach((id) => {
    const node = $(id);
    if (!node || node.hidden) return;
    const rect = node.getBoundingClientRect();
    const top = Math.max(rect.top, 70);
    const bottom = Math.min(rect.bottom, window.innerHeight);
    const visible = Math.max(0, bottom - top);
    const score = visible - Math.abs(rect.top - 90) * 0.2;
    if (score > bestScore) {
      bestScore = score;
      best = id;
    }
  });
  if (best) setActiveNav(best);
}

function updateAppbarShadow() {
  const scrolled = $("workspace").scrollTop > 4 || window.scrollY > 4;
  $("appbar").classList.toggle("is-scrolled", scrolled);
}

function initNavigation() {
  document.querySelectorAll(".appnav-link").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const target = $(link.dataset.nav);
      if (!target) return;
      if (target.hidden) {
        snack("结果面板还没有内容：先运行一次优化");
        return;
      }
      target.scrollIntoView({ block: "start", behavior: "smooth" });
      setActiveNav(link.dataset.nav);
    });
  });
  $("wordmark").addEventListener("click", (event) => {
    event.preventDefault();
    $("section-hero").scrollIntoView({ block: "start", behavior: "smooth" });
    setActiveNav("section-hero");
  });

  let queued = false;
  const onScroll = () => {
    if (queued) return;
    queued = true;
    window.requestAnimationFrame(() => {
      queued = false;
      updateAppbarShadow();
      updateActiveNav();
    });
  };
  let railQueued = false;
  const onRailScroll = () => {
    if (railQueued) return;
    railQueued = true;
    window.requestAnimationFrame(() => {
      railQueued = false;
      updateAppbarShadow();
      setActiveNav("section-diff");
    });
  };
  $("workspace").addEventListener("scroll", onScroll, { passive: true });
  document.querySelector(".rail-right").addEventListener("scroll", onRailScroll, { passive: true });
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", () => {
    if (!isNarrow()) setDrawer(false);
    onScroll();
  });
  onScroll();
}

/* ---------- 启动 ---------- */

async function reloadAll() {
  try {
    state.config = await getJSON("/v1/config");
  } catch (error) {
    snack(`读取 /v1/config 失败：${error.message}`);
  }
  renderSchemaOptions();
  renderOptionToggles();
  renderPipe();
  renderEnv();
  renderHarnessEntry();
  renderHarnessOptions();
  await loadHistory();
  await refreshHealth();
}

async function init() {
  initTheme();
  initRipple();
  initNavigation();

  $("theme-toggle").addEventListener("click", toggleTheme);
  $("menu-btn").addEventListener("click", () => setDrawer(!$("app").classList.contains("drawer-open")));
  $("scrim").addEventListener("click", () => setDrawer(false));
  $("toggle-history").addEventListener("click", (event) => {
    if (isNarrow()) {
      setDrawer(false);
      return;
    }
    const app = $("app");
    app.classList.toggle("collapsed");
    event.currentTarget.setAttribute("aria-expanded", String(!app.classList.contains("collapsed")));
  });
  $("new-run").addEventListener("click", () => {
    resetRun(true);
    if (isNarrow()) setDrawer(false);
    $("section-input").scrollIntoView({ block: "start", behavior: "smooth" });
  });
  $("hero-new").addEventListener("click", () => {
    resetRun(true);
    $("section-input").scrollIntoView({ block: "start", behavior: "smooth" });
  });
  $("sample-btn").addEventListener("click", fillSample);
  $("appbar-run").addEventListener("click", () => {
    $("section-input").scrollIntoView({ block: "start", behavior: "smooth" });
    requestRun();
  });
  $("optimize-form").addEventListener("submit", submitRun);
  $("copy-output").addEventListener("click", copyOutput);
  $("rerun-btn").addEventListener("click", rerunCurrent);
  $("refresh-env").addEventListener("click", async () => {
    await reloadAll();
    snack("已重新读取配置、历史与健康状态");
  });
  $("harness-run-btn").addEventListener("click", submitHarness);
  $("harness-detect-btn").addEventListener("click", () => loadHarnesses(true));
  $("field-harness").addEventListener("change", renderProfileNotes);
  $("options").addEventListener("change", syncOptionState);
  $("history-search").addEventListener("input", (event) => {
    state.query = event.target.value;
    renderHistory();
  });
  $("history-sort").addEventListener("click", () => {
    const index = SORTS.findIndex((item) => item.key === state.sort);
    const next = SORTS[(index + 1) % SORTS.length];
    state.sort = next.key;
    setText("history-sort-label", next.label);
    renderHistory();
  });
  document.querySelectorAll("#diff-view .seg").forEach((button) => {
    button.addEventListener("click", () => setDiffView(button.dataset.view));
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setDrawer(false);
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      requestRun();
      return;
    }
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (event.key === "/" && !typing) {
      event.preventDefault();
      if (isNarrow()) setDrawer(true);
      $("history-search").focus();
    }
  });

  try {
    state.config = await getJSON("/v1/config");
  } catch (error) {
    setStatus(`读取 /v1/config 失败：${error.message}`, true);
    state.config = { schemas: {}, steps: {}, timeout_ms: 8000 };
  }
  renderSchemaOptions();
  renderOptionToggles();
  renderPipe();
  renderEnv();
  renderHarnessEntry();
  renderHarnessOptions();
  resetRun();
  await loadHistory();
  await refreshHealth();
  await loadHarnesses(false);
  updateActiveNav();
}

document.addEventListener("DOMContentLoaded", init);