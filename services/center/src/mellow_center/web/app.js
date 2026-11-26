"use strict";
const $ = (id) => document.getElementById(id);
// The owner has a single personal space. life/work only survive as legacy
// aliases on old records; every request the console makes uses OWNER_SPACE.
// Family spaces are the only spaces that are ever labelled or selected.
const OWNER_SPACE = "personal";
const LEGACY_OWNER_SPACES = ["personal", "life", "work"];
const kinds = {
  vision: "方向",
  goal: "目标",
  project: "项目",
  task: "任务",
  review: "复盘",
  context: "背景资料",
};
const domains = {
  general: "综合",
  clothing: "衣",
  food: "食",
  home: "住",
  mobility: "行",
  health: "健康",
  finance: "财务",
  work: "工作",
  social: "社交",
  schedule: "日程",
  entertainment: "娱乐",
  learning: "学习",
  family: "家庭",
  shopping: "购物",
  travel: "出行",
};
const layers = {
  profile: "长期画像",
  active: "当前事项",
  event: "经历与事件",
  archive: "知识与档案",
};
const statuses = {
  active: "进行中",
  paused: "暂停",
  done: "完成",
  archived: "归档",
};
const parentKinds = { context: "project", goal: "vision", project: "goal", task: "project" };
const extraViews = {};
const views = {
  overview: "总览", headlines: "热点",
  archive: "收集箱",
  context: "背景资料",
  life: "人生方向",
  memories: "记忆",
  projects: "跟进",
  reviews: "复盘",
  devices: "设备",
};
const state = {
  admin: null,
  session: null,
  records: [],
  jobs: [],
  suggestions: [],
  space: OWNER_SPACE,
  view: "overview",
  search: "",
  projectTag: "全部",
  archiveKind: "all",
  archiveLimit: 50,
  layer: "all",
  domain: "all",
  archived: false,
  project: null,
  devices: [],
  hub: null,
  maintenance: null,
  spaceLabels: {},
  hasSnapshot: false,
  preferences: {},
  snapshotError: false,
  refreshing: false,
  epoch: 0,
};
const errors = {
  AUTH_FAILED: "登录已失效，请重新生成管理员邀请。",
  ADMIN_INVITATION_REQUIRED:
    "请使用 --admin 生成的管理员邀请。手机邀请不能登录后台。",
  PAIRING_FAILED: "邀请无效、已使用或过期，请重新生成。",
  CENTER_ID_MISMATCH: "邀请属于另一个中枢。请打开对应中枢地址。",
  REVISION_CONFLICT:
    "手机或其他窗口已修改此记录。你的输入还在，请加载中枢版本核对后再保存。",
  HAS_LINKED_ITEMS: "请先解除关联的目标、项目、任务或背景资料，再删除。",
  INVALID_PARENT: "关联项已删除或不在同一空间，请重新选择。",
  ORIGIN_DENIED: "请求来源与中枢不一致，请从中枢自己的地址打开页面。",
  PERMISSION_REVOKED: "设备或空间授权已变化，请刷新。",
  SNAPSHOT_CHANGED: "同步期间数据变化，本轮未替换页面；请再刷新一次。",
  INVALID_TIME: "请检查日期格式。",
  CURRENT_DEVICE: "当前浏览器请使用「退出登录」。",
  ADMIN_REQUIRED: "这个操作需要管理员设备。",
  NOT_FOUND: "找不到对应内容，它可能已被删除，请刷新后再试。",
  INVALID_ARGUMENT: "内容格式不正确，请检查后再试。",
  PAYLOAD_TOO_LARGE: "内容太大，请精简后再试。",
  BROWSER_REQUEST_REQUIRED: "请求不是从后台页面发出的，请刷新页面。",
  WORKER_UNAVAILABLE: "中枢的后台任务暂时不可用，手动管理不受影响。",
  OWNER_DEVICE: "管理员设备不能作为家庭成员管理。",
  OWNER_CANNOT_LEAVE: "管理员不能退出家庭空间。",
  GROUP_LIMIT: "家庭空间数量已达上限。",
};
let editorGeneration = 0;
let confirmationGeneration = 0;
$("confirmation").addEventListener("close", () => {
  confirmationGeneration++;
});
$("editor").addEventListener("close", () => {
  if ($("editor").open) return;
  editorGeneration++;
  $("editor-content").replaceChildren();
});
class ApiError extends Error {
  constructor(code, status = 0) {
    super(errors[code] || (/[一-鿿]/.test(code) ? code
      : status >= 500 ? "中枢暂时出错，请稍后重试。" : `操作没有完成（${code}）。`));
    this.code = code;
    this.status = status;
  }
}
function node(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    if (key.startsWith("on")) e.addEventListener(key.slice(2), value);
    else if (key === "class") e.className = value;
    else if (key in e && !key.startsWith("aria-") && key !== "form" && key !== "list")
      e[key] = value;
    else e.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    e.append(
      child instanceof Node ? child : document.createTextNode(String(child)),
    );
  }
  return e;
}
function button(label, action, style = "secondary", attrs = {}) {
  return node(
    "button",
    { type: "button", class: style, onclick: action, ...attrs },
    label,
  );
}
function closeButton(dialog) {
  return button("×", () => dialog.close(), "quiet", { "aria-label": "关闭" });
}
function badge(label, tone = "") {
  return node("span", { class: `badge ${tone}` }, label);
}
function localDay(date = new Date()) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function stamp(value) {
  if (!value) return "尚未上报";
  const d = new Date(value);
  return Number.isNaN(d.valueOf())
    ? "未知"
    : d.toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}
function isOwnerSpace(space) {
  return LEGACY_OWNER_SPACES.includes(space);
}
function ownerSpace(space) {
  return isOwnerSpace(space) ? OWNER_SPACE : space;
}
function sameSpace(a, b) {
  return ownerSpace(a) === ownerSpace(b);
}
function hasSpace(spaces, space) {
  return spaces.some(s => sameSpace(s, space));
}
// Owner spaces have no label; only family spaces are named.
function spaceLabel(space) {
  return isOwnerSpace(space) ? "" : state.spaceLabels[space] || "家庭共享";
}
function spaceBadge(space) {
  return isOwnerSpace(space) ? null : badge(spaceLabel(space));
}
function writableSpaces(kind) {
  const family = ["event", "list"].includes(kind) ? state.session.spaces.filter(s => !isOwnerSpace(s)) : [];
  return { [OWNER_SPACE]: "仅自己", ...Object.fromEntries(family.map(s => [s, spaceLabel(s)])) };
}
function nameOf(id) {
  return state.records.find((r) => r.id === id)?.data.title || "关联已失效";
}
function toast(message, undo = null) {
  const box = $("toast");
  box.replaceChildren(node("span", {}, message));
  if (undo) {
    box.append(button("撤销", async () => {
      box.hidden = true;
      clearTimeout(toast.timer);
      try { await undo(); } catch (e) { notice(e.message); }
    }, "quiet"));
  }
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (box.hidden = true), undo ? 8000 : 4000);
}
function notice(message = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}
async function request(path, { method = "GET", body, timeout = 60000 } = {}) {
  const control = new AbortController();
  const timer = setTimeout(() => control.abort(), timeout);
  try {
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: {
        "X-Mellow-Console": "1",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: control.signal,
    });
    const value = await response.json();
    if (!response.ok)
      throw new ApiError(
        value.code || `HTTP ${response.status}`,
        response.status,
      );
    return value;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError(
      "连接中枢失败，操作结果尚未确认。请保留此窗口，恢复连接后重试或刷新核对。",
    );
  } finally {
    clearTimeout(timer);
  }
}
const api = (path, options) => request(`/api/preview${path}`, options);
async function pullSnapshot() {
  const first = await api("/snapshot?paged=true");
  const records = [...first.records];
  let cursor = first.next_cursor;
  let pages = 0;
  while (cursor !== null && cursor !== undefined) {
    if (++pages > 1000) throw new ApiError("快照过大，请分批整理资料");
    const next = await api(
      `/snapshot?paged=true&cursor=${cursor}&at=${first.store_revision}`,
    );
    if (
      next.store_revision !== first.store_revision ||
      next.next_cursor === cursor
    )
      throw new ApiError("SNAPSHOT_CHANGED", 409);
    records.push(...next.records);
    cursor = next.next_cursor;
  }
  return { ...first, records };
}
async function pull() {
  try {
    return await pullSnapshot();
  } catch (e) {
    if (e.code !== "SNAPSHOT_CHANGED") throw e;
    return pullSnapshot();
  }
}
function lockScreen(message = "") {
  state.epoch++;
  state.session = null;
  boardItems = [];
  boardSettings = null;
  selectedBoard = "全部";
  boardStatus = "";
  state.admin = null;
  state.family = null;
  state.records = [];
  state.jobs = [];
  state.suggestions = [];
  state.preferences = {};
  state.projectPreviews = [];
  state.pendingImports = {};
  state.devices = [];
  state.hub = null;
  state.maintenance = null;
  state.hasSnapshot = false;
  state.snapshotError = false;
  state.spaceLabels = {};
  $("content").replaceChildren();
  $("editor").close();
  $("editor-content").replaceChildren();
  $("confirmation").close();
  $("confirmation-content").replaceChildren();
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-error").textContent = message;
}
async function refresh() {
  if (!state.session) return;
  if (state.refreshing) {
    const epoch = state.epoch;
    await state.refreshing;
    if (epoch !== state.epoch) return;
    return refresh();
  }
  state.refreshing = Promise.all([refreshSnapshot(), state.view === "headlines" ? loadBoards() : undefined])
    .finally(() => { state.refreshing = false; });
  return state.refreshing;
}
async function refreshSnapshot() {
  const epoch = state.epoch;
  $("refresh").disabled = true;
  try {
    const [result, preview] = await Promise.allSettled([pull(), request("/console/project-previews")]);
    if (epoch !== state.epoch) return;
    if (result.status === "rejected") throw result.reason;
    const snapshot = result.value;
    state.records = snapshot.records;
    state.jobs = snapshot.jobs;
    state.suggestions = snapshot.suggestions || [];
    state.preferences = snapshot.preferences || {};
    state.spaceLabels = snapshot.space_names || {};
    state.session.spaces = snapshot.spaces;
    state.hasSnapshot = true;
    state.snapshotError = false;
    state.space = OWNER_SPACE;
    if (preview.status === "fulfilled") {
      state.pendingImports = preview.value.pending;
      state.projectPreviews = preview.value.projects;
    } else {
      state.projectPreviews = (state.projectPreviews || []).filter(p => hasSpace(snapshot.spaces, p.space));
      state.pendingImports = Object.fromEntries(Object.entries(state.pendingImports || {}).filter(([s]) => hasSpace(snapshot.spaces, s)));
    }
    $("sync-state").textContent =
      `已同步 ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
    delete $("sync-state").dataset.status;
    notice(preview.status === "rejected" && ["projects", "overview", "context"].includes(state.view) ? "设备导入摘要暂时无法读取，可刷新重试。" : "");
    render();
    if (state.view === "devices") await refreshDevices();
    if (extraViews[state.view] && state.view !== "living") await refreshAdmin();
  } catch (e) {
    if (epoch !== state.epoch) return;
    if (e.status === 401) {
      lockScreen(e.message);
    } else {
      notice(e.message);
      $("sync-state").textContent = "同步未完成";
      $("sync-state").dataset.status = "error";
      state.snapshotError = true;
      if (!state.hasSnapshot) render();
    }
  } finally {
    $("refresh").disabled = false;
  }
}
async function refreshDevices() {
  const epoch = state.epoch;
  try {
    const [devices, hub, maintenance] = await Promise.all([
      api("/devices"),
      api("/hub"),
      api("/maintenance"),
    ]);
    if (epoch !== state.epoch) return;
    state.devices = devices.devices;
    state.hub = hub;
    state.maintenance = maintenance;
    if (state.view === "devices") render();
  } catch (e) {
    if (epoch === state.epoch) notice(e.message);
  }
}
async function enter(session) {
  state.epoch++;
  state.session = session;
  if(session.is_admin || session.owner_enrolled) api("/archive/legacy/check-in", {method:"POST",body:{}}).catch(()=>{});
  state.project = null;
  state.projectTag = "全部";
  state.view = views[location.hash.slice(1)] ? location.hash.slice(1) : "overview";
  state.space = OWNER_SPACE;
  livingKind = "all";
  $("invitation").value = "";
  $("login").hidden = true;
  $("app").hidden = false;
  $("sync-state").textContent = "正在连接…";
  delete $("sync-state").dataset.status;
  render();
  await refresh();
  if (state.session)
    api("/devices/heartbeat", {
      method: "POST",
      body: { platform: "browser", version: "console-0.5.0" },
    }).catch(() => {});
}
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const submit = e.target.querySelector("button");
  submit.disabled = true;
  $("login-error").textContent = "";
  try {
    const raw = $("invitation").value.trim();
    const invite = raw.startsWith("{") ? JSON.parse(raw) : { token: raw };
    await enter(
      await request("/console/session", {
        method: "POST",
        body: !invite.pairing_id || invite.pairing_id === "owner-access-v1" ? { token: invite.token } : {
          pairing_id: invite.pairing_id,
          token: invite.token,
          center_id: invite.center_id,
        },
      }),
    );
  } catch (error) {
    $("login-error").textContent =
      error instanceof SyntaxError ? "请粘贴完整的邀请 JSON。" : error.message;
  } finally {
    submit.disabled = false;
  }
});
$("refresh").addEventListener("click", refresh);
$("navigation").addEventListener("click", (e) => {
  const target = e.target.closest("[data-view]");
  if (target) navigate(target.dataset.view, { focus: true });
});
window.addEventListener("popstate", () => {
  if (state.session) navigate(location.hash.slice(1), { history: false });
});
let boardLoad = 0;
let boardItems = [], boardStatus = "", selectedBoard = "全部", boardSettings = null;
function headlineSettings() {
  if (!boardSettings) return [];
  const choices = Object.entries(boardSettings.sources).map(([id, label]) => {
    const control = node("input", {type:"checkbox", checked:boardSettings.boards.includes(id), value:id});
    return {id, control, element:node("label", {class:"checkline"}, control, label)};
  });
  const minutes = node("input", {type:"number", min:1, max:1440, required:true, value:boardSettings.poll_minutes});
  const key = node("input", {type:"password", autocomplete:"new-password", maxLength:4096,
    placeholder:boardSettings.has_key ? "已保存；留空保持不变" : "可选；不填也能获取热点"});
  const clearKey = node("input", {type:"checkbox"});
  const save = node("button", {type:"submit", class:"secondary"}, "保存热点设置");
  const form = node("form", {onsubmit:async e => {
    e.preventDefault();
    save.disabled = true;
    const epoch = state.epoch;
    try {
      const body = {boards:choices.filter(choice => choice.control.checked).map(choice => choice.id),
        poll_minutes:Number(minutes.value)};
      if (clearKey.checked || key.value.trim()) body.key = clearKey.checked ? "" : key.value.trim();
      const result = await api("/headlines/settings", {method:"PUT", body});
      key.value = "";
      if (epoch !== state.epoch) return;
      boardSettings = result;
      toast("热点设置已保存，下轮采集生效。");
      render();
    } catch(e) { if (epoch === state.epoch) notice(e.message); }
    finally {save.disabled = false;}
  }}, node("p", {class:"muted"}, "来源：什么火了（whatshot.top）。默认无需密钥；失败时尝试官方 RSS。取消全部榜单可暂停采集。"),
    choices.map(choice => choice.element), field("每轮间隔（分钟）", minutes),
    field("官方 API 密钥（仅公开接口不可用时使用，加密保存）", key),
    node("p", {class:"muted"}, "官方密钥每账号每天仅 50 次调用，与其他客户端共用；优先使用免密钥接口。热度是各平台原始指标，不跨平台比较。"),
    boardSettings.has_key ? node("label", {class:"checkline"}, clearKey, "清除已保存的官方密钥") : null, save);
  return node("details", {}, node("summary", {}, "热点来源与采集设置"), form);
}
function headlineView() {
  const boards = ["全部", ...new Set([...Object.entries(boardSettings?.sources || {})
    .filter(([id]) => boardSettings.boards.includes(id)).map(([, label]) => label), ...boardItems.map(i => i.source)])];
  const items = boardItems.filter(i => selectedBoard === "全部" || i.source === selectedBoard).slice(0,100);
  return section("热点", select(Object.fromEntries(boards.map(b => [b,b])), selectedBoard, value => {
    selectedBoard = value;
    render();
   }, {"aria-label":"热点来源"}), [node("p", {class:"muted"}, boardStatus), headlineSettings(),
    items.map(i => node("div", {class:"row"}, node("div", {class:"row-main"},
      node("a", {href:i.url,target:"_blank",rel:"noopener noreferrer"}, i.title),
      node("p", {}, [i.source, i.rank ? `榜内第 ${i.rank} 位` : "", i.hot_label].filter(Boolean).join(" · ")))))]);
}
async function loadBoards() {
  const epoch = state.epoch;
  const load = ++boardLoad;
  boardStatus = "读取中…";
  const [feed, config] = await Promise.allSettled([api("/headlines"), api("/headlines/settings")]);
  if (epoch !== state.epoch || load !== boardLoad) return;
  if (config.status === "fulfilled") boardSettings = config.value;
  if (feed.status === "fulfilled") {
    boardItems = feed.value.items;
    if (!boardItems.some(item => item.source === selectedBoard)) selectedBoard = "全部";
    if (boardItems.length) {
      boardStatus = feed.value.updated_at ? "中枢更新于 " + new Date(feed.value.updated_at).toLocaleString("zh-CN") : "";
    } else {
      boardStatus = boardSettings?.boards.length === 0 ? "采集已暂停，可在热点来源中选择榜单。" : "暂无近期热点，等待中枢下次采集。";
    }
  } else {
    boardStatus = "热点读取失败，可点右上角刷新重试。" + feed.reason.message;
  }
  if (state.view==="headlines" && state.session) render();
}
function navigate(view, { history: push = true, focus = false } = {}) {
  if (!views[view]) view = "overview";
  if(view==="headlines") {boardStatus="读取中…"; loadBoards();}
  if (push && state.view !== view) history.pushState(null, "", "#" + view);
  state.view = view;
  state.search = "";
  state.archiveLimit = 50;
  notice();
  render();
  window.scrollTo(0, 0);
  document.querySelector('#navigation [aria-current="page"]')?.scrollIntoView({ block: "nearest", inline: "center" });
  if (focus) $("page-title").focus({ preventScroll: true });
  if (view === "devices") refreshDevices();
  if (extraViews[view] && view !== "living") refreshAdmin();
}
$("logout").addEventListener("click", () =>
  confirmAction(
    "退出登录？",
    "会撤销这台浏览器的登录凭据，手机不受影响。",
    async () => {
      await request("/console/session", { method: "DELETE" });
      lockScreen();
    },
    { label: "退出登录", style: "danger" },
  ),
);
async function confirmAction(title, description, action, {label = "确认", style = "primary"} = {}) {
  const generation = ++confirmationGeneration;
  const modal = $("confirmation"),
    error = node("p", { class: "form-error", role: "alert" });
  const submit = button(
    label,
    async () => {
      submit.disabled = true;
      try {
        await action();
        if (generation === confirmationGeneration) modal.close();
      } catch (e) {
        error.textContent = e.message;
      } finally {
        submit.disabled = false;
      }
    },
    style,
  );
  $("confirmation-content").replaceChildren(
    node("h2", {}, title),
    node("p", {}, description),
    error,
    node(
      "div",
      { class: "form-actions" },
      button("取消", () => modal.close(), "quiet"),
      submit,
    ),
  );
  modal.showModal();
}
function empty(title, description, action = null) {
  return node(
    "div",
    { class: "empty" },
    node("h3", {}, title),
    description ? node("p", {}, description) : null,
    action,
  );
}
function skeleton() {
  return node("div", { class: "skeleton", role: "status", "aria-label": "正在读取" },
    node("i"), node("i"), node("i"));
}
function section(title, action, content) {
  return node(
    "section",
    { class: "panel" },
    node("div", { class: "section-header" }, node("h3", {}, title), action),
    content,
  );
}
function disclosure(title, key, content, className = "") {
  return node("details", {class:className, "data-key":key}, node("summary", {}, title), content);
}
function metric(label, value, detail) {
  return node(
    "div",
    { class: "metric" },
    node("span", { class: "metric-title" }, label),
    node("strong", {}, value),
    node("small", {}, detail),
  );
}
function toolbar(actions, { search = true } = {}) {
  return node(
    "div",
    { class: "toolbar" },
    search
      ? node("input", {
          id: "search",
          type: "search",
          "aria-label": "搜索标题与内容",
          value: state.search,
          placeholder: "搜索标题与内容",
          oninput: (e) => {
            state.search = e.target.value;
            render();
          },
        })
      : null,
    ...actions,
  );
}
function matches(r) {
  return [r.data.title, r.data.body, r.data.text].filter(Boolean).join(" ")
    .toLowerCase()
    .includes(state.search.trim().toLowerCase());
}
function select(options, value, onchange, attrs = {}) {
  return node(
    "select",
    { ...attrs, onchange: (e) => onchange(e.target.value) },
    ...Object.entries(options).map(([key, label]) =>
      node("option", { value: key, selected: key === value }, label),
    ),
  );
}
function render() {
  if (!state.session) return;
  const focus = document.activeElement?.id;
  const start = document.activeElement?.selectionStart;
  const end = document.activeElement?.selectionEnd;
  $("page-title").textContent = views[state.view];
  const descriptions = {
    overview: "今天的安排、习惯和正在推进的事。",
    projects: "项目的目的、进展、下一步和待办。",
    archive: "同步到中枢的全部记录，按更新时间排列。",
    context: "你保存的项目背景和个人笔记，可以按需授权给 AI。",
    life: "把长期方向、具体目标和推进它们的项目串起来。",
    memories: "你保存的长期背景，以及小满从对话中整理的记忆。",
    reviews: "回看这一周实际发生的事，写下下一步。",
    headlines: "中枢定时抓取的公开热点。",
    living: "日历、习惯、清单、日记、厨房与衣橱。",
    devices: "已连接的手机和浏览器，以及中枢的运行状态。",
    family: "管理家庭共享空间里的成员和邀请。",
    connectors: "连接开发工具，审核它们提交的资料。",
    automations: "小满给你的建议，以及生成建议的规则。",
    usage: "各设备和工具调用模型的用量与估算费用。",
    models: "中枢使用的模型与密钥。",
    backups: "加密备份、自动备份计划和从备份恢复。",
    audit: "最近的管理操作，不含正文和密钥。",
  };
  const description = descriptions[state.view];
  $("page-description").textContent = description || "";
  $("page-description").hidden = !description;
  document.title = `${views[state.view]} · 小满中枢`;
  $("navigation").querySelectorAll("[data-view]").forEach((b) => {
    if (b.dataset.view === state.view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  const previousView = $("content").dataset.view;
  const expanded = new Map(Array.from($("content").querySelectorAll("details[data-key]"), d => [d.dataset.key,d.open]));
  const content = {
    overview: overview, headlines: headlineView,
    archive: personalArchive,
    context: contexts,
    memories: memories,
    life: life,
    living: livingView,
    projects: projects,
    reviews: reviews,
    devices: devices,
    ...extraViews,
  };
  const viewContent = !state.hasSnapshot && !["devices", "headlines"].includes(state.view) && !extraViews[state.view]
    ? state.snapshotError ? empty("暂时读不到中枢资料", "检查网络后点右上角「刷新」重试。", button("重试", refresh, "primary")) : skeleton()
    : content[state.view]();
  $("content").replaceChildren(
    ...(Array.isArray(viewContent) ? viewContent.flat(Infinity) : [viewContent]).filter(x => x !== null && x !== undefined && x !== false),
  );
  $("content").dataset.view = state.view;
  if (previousView === state.view) for (const details of $("content").querySelectorAll("details[data-key]")) {
    if (expanded.has(details.dataset.key)) details.open = expanded.get(details.dataset.key);
  }
  if (["search", "review-search"].includes(focus) && $(focus)) {
    $(focus).focus({preventScroll:true});
    if (start !== null) $(focus).setSelectionRange(start, end);
  }
}
function taskRow(r) {
  const done = r.data.status === "done" || !!r.data.completed_at;
  const toggle = node("input", {
    type: "checkbox",
    checked: done,
    disabled: r.data.status === "archived",
    "aria-label": `完成任务 ${r.data.title}`,
    onchange: async () => {
      toggle.disabled = true;
      try {
        await write({
          ...r,
          data: {
            ...r.data,
            status: done ? "active" : "done",
            completed_at: done ? null : localDay(),
          },
        });
        const revert = async () => {
          const latest = state.records.find(x => x.id === r.id) || r;
          await write({...latest, data: {...latest.data, status: done ? "done" : "active",
            completed_at: done ? latest.data.completed_at || localDay() : null}});
          await refresh();
        };
        await refresh();
        toast(done ? "任务已重新打开" : "完成了一小步。", revert);
      } catch (e) {
        toggle.checked = done;
        notice(e.message);
        await refresh();
      } finally {
        toggle.disabled = false;
      }
    },
  });
  return node(
    "div",
    { class: "row" },
    toggle,
    node(
      "div",
      { class: "row-main" },
      node("strong", { class: done ? "task-done" : "" }, r.data.title),
      node("p", {}, [r.data.parent_id ? nameOf(r.data.parent_id) : null, r.data.due_on].filter(Boolean).join(" · ")),
    ),
    button("编辑", () => edit("task", r), "quiet"),
  );
}
function overviewZone() {
  const zone = state.preferences?.timezone || "Asia/Shanghai";
  try { new Intl.DateTimeFormat("en-CA", { timeZone: zone }); return zone; }
  catch { return "Asia/Shanghai"; }
}
function zoneOffsetMs(zone, at) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: zone, hourCycle: "h23", year: "numeric",
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    .formatToParts(new Date(at)).map(p => [p.type, p.value]));
  return Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second) - Math.floor(at / 1000) * 1000;
}
function zonedMidnight(day, zone) {
  const base = Date.parse(day + "T00:00:00Z");
  return base - zoneOffsetMs(zone, base - zoneOffsetMs(zone, base));
}
function overview() {
  if (!state.records.length) {
    return [node("div", { class: "hero" }, node("div", {}, node("h2", {}, "欢迎使用小满中枢"),
      node("p", {}, "这里会汇总手机同步来的记录。先连接一台手机，或直接新建第一条内容。")),
      button("连接一台手机", invitePhone, "primary")),
      empty("还没有任何记录", "在手机上选择同步，或者先在这里记下一个目标。", button("记录一个目标", () => edit("goal"), "secondary"))];
  }
  const records = planningRows();
  const portfolio = projectPortfolio(records);
  const tasks = records.filter(r => r.kind === "task" && r.data.status === "active").sort(byDueDate);
  const zone = overviewZone();
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const due = tasks.filter(r => r.data.due_on && r.data.due_on <= today);
  const weekday = new Date(today + "T12:00:00Z").getUTCDay() || 7;
  const habits = records.filter(r => r.kind === "habit").filter(r => r.data.status === "active" && r.data.weekdays.includes(weekday));
  const checked = new Set(records.filter(r => r.kind === "habit_log").filter(r => r.data.date === today && r.data.done).map(r => r.data.habit_id));
  const start = zonedMidnight(today, zone);
  const end = zonedMidnight(new Date(Date.parse(today + "T12:00:00Z") + 86400000).toISOString().slice(0, 10), zone);
  const events = records.filter(r => r.kind === "event").filter(r => r.data.status !== "cancelled" &&
    Date.parse(r.data.start) < end && Date.parse(r.data.end) > start)
    .sort((a,b) => Date.parse(a.data.start) - Date.parse(b.data.start));
  const suggestions = state.suggestions.filter(s => s.state === "new" && Date.parse(s.expires_at) > Date.now())
    .sort((a,b) => b.created_at.localeCompare(a.created_at));
  return [
    node("div", {class:"hero"},
      node("div", {}, node("p", {class:"eyebrow"}, today + " · " + zone),
        node("h2", {}, "今天"),
        node("p", {}, `今天有 ${events.length} 个日程，${due.length} 个任务到期或待补，${habits.filter(r => !checked.has(r.id)).length} 个习惯待打卡。`)),
      button("添加日程", () => livingForm("event"), "primary")),
    suggestions.length ? section("小满的建议", button("查看全部建议", () => navigate("automations"), "quiet"),
      suggestions.slice(0,3).map(s => suggestionRow(s, refresh))) : null,
    portfolioOverview(portfolio),
    node("div", {class:"two-columns"},
      section("今天的安排", button("查看日历", () => { livingKind = "event"; navigate("living"); }, "quiet"),
        [events.map(r => node("div", {class:"row"}, node("div", {class:"row-main"},
          node("strong", {}, r.data.text), node("p", {}, livingDetails(r))), button("编辑", () => livingForm("event", r), "quiet"))),
        due.map(taskRow),
        events.length || due.length ? null : empty("今天没有到期事项", "可以添加一个日程，或去「跟进」看看项目。")]),
      section("今日习惯", button("添加习惯", () => livingForm("habit"), "quiet"),
        habits.length ? habits.map(r => node("div", {class:"row"}, node("strong", {class:"row-main"}, r.data.text),
          button(checked.has(r.id) ? "已打卡 · 撤销" : "打卡", () => habitCheckin(r), "secondary"))) : empty("今天没有安排习惯", "已同步且安排在今天的习惯会出现在这里。"))),
    directionOverview(records),
    section("接下来可以做", button("查看跟进", () => navigate("projects"), "quiet"),
      tasks.length > due.length ? tasks.filter(r => !due.includes(r)).slice(0, 5).map(taskRow)
        : node("p", {class:"muted"}, "暂无其他待办任务。")),
    node(
      "div",
      { class: "toolbar" },
      button("记录一个目标", () => edit("goal"), "secondary"),
      button("添加背景资料", () => edit("context"), "secondary"),
      button(
        "写本周复盘",
        () => edit("review", null, { title: `本周复盘 · ${localDay()}`, body: reviewDraft() }, state.space),
        "quiet",
      ),
    ),
  ];
}
function personalArchive() {
  const labels = {...kinds, ...livingKinds, message:"聊天", memory:"记忆", document:"收藏资料", collection:"合集",
    habit_log:"习惯打卡", consumption:"食材消耗"};
  const rows = planningRows().filter(r => !(r.kind === "context" && r.data.tags.includes("agent-activity")));
  const groups = Object.fromEntries([...new Set(rows.map(r => r.kind))].map(k => [k, labels[k] || k]));
  const query = state.search.trim().toLowerCase();
  const results = [];
  for (const r of rows) {
    if (state.archiveKind !== "all" && r.kind !== state.archiveKind) continue;
    const d = r.data;
    const title = d.title || d.text || labels[r.kind] || r.kind;
    const parts = [d.body, d.summary, d.location, ...(d.tags || []),
      ...(d.pages || []).map(p => p.text), ...(d.items || []).map(i => typeof i === "string" ? i : i.text)];
    const body = parts.filter(Boolean).join("\n");
    if (query && ![title,body,labels[r.kind],spaceLabel(r.space)].some(v => v?.toLowerCase().includes(query))) continue;
    results.push({r,title,body});
  }
  results.sort((a,b) => b.r.updated_at.localeCompare(a.r.updated_at));
  const switchFilter = (key,value) => { state[key] = value; state.archiveLimit = 50; render(); };
  return [toolbar([
    select({all:"全部类型",...groups},state.archiveKind,v=>switchFilter("archiveKind",v),{"aria-label":"资料类型"})]),
    rows.length ? node("p",{class:"muted"},`显示 ${Math.min(state.archiveLimit,results.length)} 条，共 ${results.length} 条符合条件`) : null,
    ...results.slice(0,state.archiveLimit).map(({r,title,body}) => node("details",{class:"panel", "data-key":r.id},
      node("summary",{},node("strong",{},title.slice(0,100)),node("p",{class:"muted"},
        [spaceLabel(r.space),labels[r.kind] || r.kind,stamp(r.updated_at)].filter(Boolean).join(" · "))),
      body || title.length > 100 ? node("p",{class:"body-copy"}, [title.length > 100 && title,body].filter(Boolean).join("\n")) : null,
      r.data.parent_id ? node("p",{class:"muted"},`上级：${nameOf(r.data.parent_id)}`):null)),
    results.length ? null : empty(rows.length ? "没有匹配的资料" : "中枢还没有资料", rows.length ? "试试其他关键词或资料类型。" : "手机中仅本机保存的内容，需要选择同步后才会显示。"),
    results.length > state.archiveLimit ? button("显示更多",()=>{state.archiveLimit +=50;render();}) : null];
}
function contexts() {
  const records = planningRows();
  const inFilter = r => (state.layer === "all" || r.data.layer === state.layer)
    && (state.domain === "all" || r.data.domain === state.domain);
  const query = state.search.trim().toLowerCase();
  const personal = records.filter(r => r.kind === "context" && inFilter(r) && matches(r)
    && !r.data.tags.includes("agent-activity") && !projectEvidence(r))
    .sort((a,b) => b.updated_at.localeCompare(a.updated_at));
  const projects = projectPortfolio(records).filter(p => {
    const source = p.evidence.filter(e => inFilter(e.record) && e.type !== "设备活动");
    const projectMatch = p.record && (state.layer === "all" || state.layer === "active")
      && (state.domain === "all" || p.record.data.domain === state.domain);
    const pending = state.domain === "all" && state.layer === "all" && p.previews.some(v => v.description);
    if (!source.length && !projectMatch && !pending) return false;
    const facts = projectFacts(p);
    if (!facts.purpose && !facts.progress && !facts.next &&
        !source.some(e => ["收藏资料","关联资料"].includes(e.type)) && !query) return false;
    return [p.title, projectMatch && p.record.data.body, pending && facts.purpose, projectMatch && facts.progress, projectMatch && facts.next,
      ...source.map(e => e.record.data.body)].some(value => value && value.toLowerCase().includes(query));
  });
  const projectRow = p => {
    const facts = projectFacts(p);
    const material = p.evidence.find(e => inFilter(e.record) && ["收藏资料","关联资料"].includes(e.type));
    return node("div", {class:"row"}, node("div", {class:"row-main"}, node("strong", {}, p.title),
      facts.purpose ? node("p", {}, facts.purpose) : null,
      facts.purposeSource && facts.purpose ? node("small", {}, facts.purposeSource) : null,
      material ? node("p", {}, material.text.slice(0,140)) :
        !facts.purpose && (facts.progress || facts.next) ? node("p", {}, facts.progress || facts.next) : null), button("看项目", () => openProject(p), "quiet"));
  };
  return [
    toolbar([
      select(
        { all: "所有领域", ...domains },
        state.domain,
        (v) => {
          state.domain = v;
          render();
        },
        { "aria-label": "按领域筛选" },
      ),
      button("添加背景资料", () => edit("context"), "primary"),
    ]),
    node(
      "div",
      { class: "filters" },
      ...Object.entries({ all: "全部层级", ...layers }).map(([key, label]) =>
        button(
          label,
          () => {
            state.layer = key;
            render();
          },
          state.layer === key ? "active" : "",
        ),
      ),
    ),
    projects.length ? section("项目背景与资料", null, [
      projects.slice(0,5).map(projectRow),
      projects.length > 5 ? node("details", {"data-key":"more-context-projects"},
        node("summary", {}, "更多项目（"+(projects.length-5)+"）"), projects.slice(5).map(projectRow)) : null]) : null,
    personal.length ? section("我的背景与笔记", null, [
      node("div", {class:"cards"}, personal.slice(0,8).map(recordCard)),
      personal.length > 8 ? node("details", {"data-key":"more-context-notes"},
        node("summary", {}, "更早的笔记（"+(personal.length-8)+"）"), node("div", {class:"cards"}, personal.slice(8).map(recordCard))) : null]) : null,
    projects.length || personal.length ? null : empty(query ? "没有匹配的背景或资料" : "还没有项目背景或个人笔记",
      query ? "可以换个关键词，或查看全部资料中的原始记录。" : "收藏的项目资料、项目简介和你写的背景会出现在这里。"),
  ];
}
function recordCard(r) {
  return node(
    "article",
    { class: "record-card" },
    node(
      "div",
      { class: "card-meta" },
      spaceBadge(r.space),
      badge(domains[r.data.domain]),
      r.kind === "context"
        ? badge(layers[r.data.layer])
        : badge(
            statuses[r.data.status],
            r.data.status === "done" ? "green" : "",
          ),
    ),
    node("h3", {}, r.data.title),
    node("p", { class: "excerpt" }, r.data.body || "还没有补充内容。"),
    node(
      "div",
      { class: "card-footer" },
      node(
        "span",
        {},
        r.kind === "context"
          ? r.data.ai
            ? "AI 可用 · 中枢同步"
            : "AI 不可用 · 中枢同步"
          : `更新于 ${stamp(r.updated_at)}`,
      ),
      button("查看与编辑", () => edit(r.kind, r)),
    ),
  );
}
function memories() {
  const sorted = records => records.filter(matches).sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const profile = sorted(planningRows("context").filter(r => r.data.layer === "profile"));
  const rows = sorted(planningRows("memory"));
  const categories = {fact:"事实", preference:"偏好", goal:"目标", style:"沟通风格"};
  const labels = {active:"可使用", candidate:"待确认", suppressed:"已暂停", rejected:"已拒绝",
    invalidated:"来源有变化", conflicted:"有冲突", expired:"已过期", scheduled:"尚未生效",
    superseded:"已被合并或取代", needs_review:"需要复核"};
  const statusOf = r => r.effective_status || r.data.status;
  const slotLabels = {"identity.home_city":"居住城市", "identity.home_address":"居住地址", "identity.birthday":"生日",
    "work.address":"工作地址", "work.hours":"工作时间", "work.hours.start":"上班时间", "work.hours.end":"下班时间",
    "diet.restriction":"饮食限制", "diet.preference":"饮食偏好", "health.allergy":"过敏", "habit.sleep":"睡眠习惯",
    "family.partner":"伴侣", "family.children":"孩子", pet:"宠物", "transport.commute":"通勤方式",
    "preference.drink":"饮品偏好", "preference.food":"食物偏好", "preference.delivery":"快递偏好"};
  const slotLabel = slot => slotLabels[slot] || (slot.startsWith("interest.") ? "兴趣" : slot.startsWith("misc.") ? "其他" : slot);
  const review = (r, status) => confirmAction(
    status === "active" ? "确认这条记忆？" : "拒绝这条记忆？",
    status === "active" ? "确认后，小满可以在合适的对话中使用它。" : "拒绝后，小满不会再使用这条记忆。",
    async () => {
      await write({...r, data: {...r.data, status, ...(status === "active" ? {confirmed_at: new Date().toISOString(), needs_confirmation: false} : {})}});
      await refresh();
      toast(status === "active" ? "已确认这条记忆" : "已拒绝这条记忆");
    }, {label: status === "active" ? "确认" : "拒绝", style: status === "active" ? "primary" : "danger"});
  const row = r => {
    const d = r.data;
    const status = statusOf(r);
    const evidence = d.sources.map(ref => state.records.find(source => source.id === ref.id && source.revision === ref.revision))
      .filter(Boolean).map(source => source.data.text);
    return node("details", {class:"evidence-card", "data-key":r.id},
      node("summary", {}, node("strong", {}, d.text.slice(0, 110)),
        node("small", {}, [d.slot ? slotLabel(d.slot) : categories[d.category] || "记忆", labels[status] || status,
          spaceLabel(r.space), d.observed_at ? "观察于 " + stamp(d.observed_at) : stamp(r.updated_at)].filter(Boolean).join(" · "))),
      node("p", {class:"body-copy"}, d.text),
      d.slot ? node("p", {class:"muted"}, "槽位：" + slotLabel(d.slot) + (d.value !== undefined && d.value !== null && typeof d.value !== "object" ? " · " + d.value : "")) : null,
      d.observed_at ? node("p", {class:"muted"}, "观察时间 " + stamp(d.observed_at)) : null,
      d.valid_until ? node("p", {class:"muted"}, "有效至 " + stamp(d.valid_until)) : null,
      d.quote || evidence.length ? node("blockquote", {class:"memory-evidence"}, d.quote || evidence.join("\n")) : null,
      ["candidate", "needs_review"].includes(status) ? rowActions(button("确认", () => review(r, "active"), "primary"),
        button("拒绝", () => review(r, "rejected"), "danger")) : null);
  };
  const groupOrder = ["candidate", "active", "needs_review"];
  const grouped = status => rows.filter(r => statusOf(r) === status);
  const others = rows.filter(r => !groupOrder.includes(statusOf(r)));
  const groups = groupOrder.map(status => ({status, items: grouped(status)})).filter(g => g.items.length);
  return [
    toolbar([button("添加画像", () => edit("context", null, {layer:"profile"}), "primary")]),
    section("长期画像", badge(profile.length + " 条"), profile.length ? node("div", {class:"cards"}, profile.map(recordCard))
      : empty(state.search ? "没有匹配的画像" : "还没有长期画像", "添加一条长期背景，或在手机同步已有画像。")),
    section("对话记忆", badge(rows.length + " 条"), [
      groups.map(g => node("div", {class:"memory-group", "data-status":g.status},
        node("h3", {}, labels[g.status] + "（" + g.items.length + "）"), g.items.map(row))),
      others.length ? disclosure("其他状态（" + others.length + "）", "other-memories", others.map(row)) : null,
      rows.length ? null : empty(state.search ? "没有匹配的记忆" : "中枢还没有记忆", "手机里的记忆及其来源同步后会在这里显示。")]),
  ];
}
function life() {
  const records = planningRows();
  const goals = records.filter(r => r.kind === "goal" && r.data.status !== "archived" && matches(r));
  const visions = records.filter(r => r.kind === "vision" && r.data.status !== "archived"
    && (matches(r) || goals.some(g => g.data.parent_id === r.id)));
  const portfolio = projectPortfolio(records);
  const independent = goals.filter(g => !visions.some(v => v.id === g.data.parent_id));
  const archived = records.filter(r => ["vision", "goal"].includes(r.kind) && r.data.status === "archived" && matches(r));
  const clues = directionSources(records, portfolio);
  const goalRow = r => node("div", {class:"row"},
    node("div", {class:"row-main"}, node("strong", {}, r.data.title),
      r.data.body ? node("p", {}, r.data.body) : null,
      node("p", {}, [spaceLabel(r.space), statuses[r.data.status], r.data.due_on].filter(Boolean).join(" · ")),
      portfolio.filter(p => p.record?.data.parent_id === r.id).map(p =>
        button(p.title + " · " + taskProgressLabel(p), () => openProject(p), "quiet"))),
    rowActions(button("编辑", () => edit("goal", r), "quiet"),
      button("拆成项目", () => edit("project", null, {parent_id:r.id, domain:r.data.domain}))));
  return [
    toolbar([button("新建方向", () => edit("vision")),
      button("新建目标", () => edit("goal"), "primary")]),
    visions.length || independent.length || clues ? null : empty(state.search ? "没有匹配的方向或目标" : "还没有保存方向或目标",
      state.search ? "可以试试其他关键词。" : "在手机同步已有方向或目标，也可以在这里新建。"),
    visions.map(v => section(v.data.title, rowActions(spaceBadge(v.space),
      button("编辑方向", () => edit("vision", v), "quiet")), [
      v.data.body ? node("p", {class:"body-copy"}, v.data.body) : null,
      goals.filter(g => g.data.parent_id === v.id).map(goalRow),
      button("在这个方向下定目标", () => edit("goal", null, {parent_id:v.id}), "quiet"),
    ])),
    independent.length ? section("独立目标", null, independent.map(goalRow)) : null,
    clues,
    lifeHistory(records),
    directionHistory(records),
    archived.length ? disclosure("已归档的方向与目标（" + archived.length + "）", "archived-directions", archived.map(r =>
      node("div", {class:"row"}, node("div", {class:"row-main"}, r.data.title),
        button("编辑", () => edit(r.kind, r), "quiet"))), "panel") : null,
  ];
}
function reviewDraft() {
  const scoped = kind => planningRows(kind).filter(r => sameSpace(r.space, OWNER_SPACE));
  const end = localDay(),
    startDate = new Date();
  startDate.setDate(startDate.getDate() - 6);
  const start = localDay(startDate);
  const completed = scoped("task").filter(
    (r) =>
      ["done", "archived"].includes(r.data.status) &&
      r.data.completed_at >= start &&
      r.data.completed_at <= end,
  );
  const habits = scoped("habit_log").filter(
    (r) => r.data.done && r.data.date >= start && r.data.date <= end,
  );
  return `${start} 至 ${end}\n完成 ${completed.length} 项任务，记录 ${habits.length} 次习惯打卡。\n${completed.map((r) => "✓ " + r.data.title).join("\n")}\n\n项目进展\n${scoped(
    "project",
  )
    .filter((r) => r.data.status === "active")
    .map((r) => {
      const tasks = scoped("task").filter(t => t.data.parent_id === r.id);
      return r.data.title + "：" + taskProgressLabel({tasks});
    })
    .join("\n")}\n\n哪些有效：\n\n遇到的阻碍：\n\n下周想推进的三件事：\n`;
}
function reviews() {
  const rows = planningRows("review")
    .filter(matches)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  return [
    toolbar([
      button("空白复盘", () => edit("review")),
      button(
        "生成本周底稿",
        () =>
          edit("review", null, {
            title: `本周复盘 · ${localDay()}`,
            body: reviewDraft(),
          }, state.space),
        "primary",
      ),
    ]),
    node(
      "p",
      { class: "muted" },
      "本周底稿汇总已完成的任务和习惯打卡。",
    ),
    rows.length
      ? node("div", { class: "cards" }, ...rows.map(recordCard))
      : empty(
          "给这一周留一点回声",
          "从实际发生的事开始，找出值得继续和需要调整的地方。",
        ),
  ];
}
const platformNames = { browser: "浏览器", android: "Android 手机", ios: "iPhone", macos: "Mac", windows: "Windows", linux: "Linux" };
function devices() {
  const hub = state.hub;
  const currentId = state.session.device_id;
  const inactive = d => d.id !== currentId && (d.revoked || Date.parse(d.expires_at) <= Date.now());
  const oldDevices = state.devices.filter(inactive);
  const cleanable = oldDevices.slice(0, 1000);
  const handleDevice = (d, remove) => confirmAction(
    remove ? "删除设备？" : "撤销设备授权？",
    "「" + d.name + "」将失去中枢访问权；它此前下载的本地副本会保留。",
    async () => {
      await api("/devices/" + encodeURIComponent(d.id) + (remove ? "?remove=true" : ""), {method:"DELETE"});
      await refreshDevices();
      toast(remove ? "已删除设备并撤销授权" : "已撤销设备授权");
    }, {label:remove ? "删除设备" : "撤销授权", style:"danger"},
  );
  const deviceRow = d => {
    const status = d.revoked ? "已撤销" : Date.parse(d.expires_at) <= Date.now() ? "已到期" :
      d.last_seen || d.id === currentId ? "有效" : "尚未上报";
    return node("div", {class:"row device-row"},
      node("div", {class:"row-main"}, node("strong", {}, d.id === currentId ? d.name + " · 当前浏览器" : d.name),
        node("p", {}, [platformNames[d.platform] || "尚未上报型号", d.spaces.map(spaceLabel).filter(Boolean).join("、"), d.last_seen ? "最近在线 " + stamp(d.last_seen) : "还没有在线过"].filter(Boolean).join(" · ")),
        d.version && d.platform !== "browser" ? node("small", { title: "应用版本" }, "版本 " + d.version) : null),
      badge(status, status === "有效" ? "green" : "gray"),
      d.id !== currentId ? rowActions(
        status === "有效" ? button("撤销授权", () => handleDevice(d, false), "quiet") : null,
        button(status === "有效" || status === "尚未上报" ? "撤销并删除" : "删除", () => handleDevice(d, true), "danger"),
      ) : null);
  };
  const clean = cleanable.length ? button("清理旧设备（" + cleanable.length + "）", () => confirmAction(
    "清理旧设备？",
    "将移除列表中已撤销或已到期的 " + cleanable.length + " 台设备。这些设备需要重新连接，本地副本会保留。",
    async () => {
      const result = await api("/devices/cleanup", {method:"POST", body:{ids:cleanable.map(d => d.id)}});
      await refreshDevices();
      toast("已清理 " + result.removed + " 台设备");
    }, {label:"清理设备", style:"danger"},
  ), "quiet") : null;
  const jobKinds = {chat:"对话", extract:"提取记忆", proactive:"主动分析", recall:"回忆核对", draft:"起草"};
  const jobStates = {queued:"排队中", running:"运行中", completed:"已完成", succeeded:"已完成", failed:"失败", cancelled:"已取消", interrupted:"已中断"};
  const jobs = state.jobs.length ? [...state.jobs].sort((a,b) => (b.created_at || "").localeCompare(a.created_at || ""))
    .slice(0,8).map(j => node("div", {class:"row"},
      node("div", {class:"row-main"}, node("strong", {}, jobKinds[j.kind] || j.kind),
        j.error || j.created_at ? node("p", {}, errors[j.error] || j.error || stamp(j.created_at)) : null),
      badge(jobStates[j.state] || j.state))) : node("p", {class:"muted"}, "现在没有排队中的模型任务。");
  const lastBackup = state.maintenance?.last_backup;
  const backup = node(
    "div",
    {},
    node(
      "p",
      {},
      state.maintenance?.automatic_backup?.enabled
        ? "自动备份已开启"
        : "自动备份未开启",
    ),
    node(
      "p",
      { class: "muted" },
      "在「备份」管理计划、下载文件或从文件恢复。密钥与私钥不会显示。",
    ),
    node(
      "p",
      { class: "muted" },
      lastBackup?.state === "failed"
        ? `最近备份失败 · ${stamp(lastBackup.finished_at)}。请在 Mini 检查备份目录；${lastBackup.retry_after ? "计划重试：" + stamp(lastBackup.retry_after) : "尚未安排重试"}`
        : lastBackup?.state === "succeeded"
          ? `最近成功备份：${stamp(lastBackup.finished_at)}`
          : "暂无备份记录",
    ),
    button("打开备份", () => navigate("backups"), "quiet"),
  );
  return [
    node(
      "div",
      { class: "hero" },
      node(
        "div",
        {},
        node(
          "h2",
          {},
          hub
            ? hub.status === "ready"
              ? "中枢运行中"
              : "中枢需要检查"
            : "正在读取中枢状态…",
        ),
        node(
          "p",
          {},
          hub
            ? `中枢版本 ${hub.version} · ${hub.model_configured ? "中枢模型已配置" : "还没有配置中枢模型，手动整理不受影响"}`
            : "正在读取…",
        ),
      ),
      button("连接一台手机", invitePhone, "primary"),
    ),
    section("已连接的设备", clean, [
      state.devices.filter(d => !inactive(d)).sort((a,b) => Number(b.id === currentId) - Number(a.id === currentId)).map(deviceRow),
      oldDevices.length ? disclosure("旧设备（" + oldDevices.length + "）", "old-devices", oldDevices.map(deviceRow)) : null,
      state.devices.length ? null : hub ? empty("还没有连接设备", "点右上角「连接一台手机」，在手机上粘贴邀请。") : skeleton(),
    ]),
    node(
      "div",
      { class: "two-columns" },
      section("模型任务", null, jobs),
      section("备份", null, backup),
    ),
  ];
}

async function write(record, action = "put", command = null) {
  const op = command || {
    operation_id: crypto.randomUUID(),
    action,
    id: record.id || crypto.randomUUID(),
    base_revision: record.revision || 0,
    ...(action === "put"
      ? {
          record: {
            kind: record.kind,
            space: ownerSpace(record.space),
            mode: record.mode || "normal",
            data: record.data,
          },
        }
      : {}),
  };
  await api("/operations", { method: "POST", body: op });
  return op;
}
function field(label, element) {
  const id = element.id || `field-${crypto.randomUUID()}`;
  element.id = id;
  return node(
    "div",
    { class: "field" },
    node("label", { htmlFor: id }, label),
    element,
  );
}
function edit(kind, record = null, initial = {}, sourceSpace = null) {
  const generation = ++editorGeneration;
  const current = () => generation === editorGeneration && $("editor").open;
  const parentRecord = state.records.find(r => r.id === initial.parent_id);
  let target = ownerSpace(record?.space || parentRecord?.space || sourceSpace || state.space);
  const original = record ? structuredClone(record) : null;
  const data = {
    title: "",
    body: "",
    domain: "general",
    ...(kind === "context"
      ? {
          layer: "profile",
          sensitivity: "private",
          sync: true,
          ai: false,
          tags: [],
        }
      : { status: "active", parent_id: null, due_on: null }),
    ...(record?.data || {}),
    ...initial,
  };
  const dialog = $("editor");
  const title = node("input", {
    name: "title",
    value: data.title,
    maxLength: 200,
    required: true,
  });
  const body = node("textarea", {
    name: "body",
    value: data.body,
    rows: kind === "review" ? 12 : 5,
    maxLength: 16000,
  });
  const domain = select(domains, data.domain, () => {}, { name: "domain" });
  const content = [
    field("标题", title),
    field(
      kind === "goal"
        ? "完成的标准"
        : kind === "vision"
          ? "我重视什么，想成为什么样的人"
          : kind === "project" ? "项目简介" : "内容与记录",
      body,
    ),
    field("领域", domain),
  ];
  title.addEventListener("input", () => title.removeAttribute("aria-invalid"));
  let status, parent, due, ai, layer, tags, progressNote, nextStep, shareSummary, areaChecks;
  if (kind === "project") {
    progressNote = node("textarea", {name:"progress_note", rows:3, maxLength:2000, value:data.progress_note || ""});
    nextStep = node("input", {name:"next_step", maxLength:500, value:data.next_step || ""});
    shareSummary = node("input", {type:"checkbox", checked:data.share_summary === true});
    areaChecks = projectTags.map(tag => [tag, node("input", {type:"checkbox", checked:data.area_tags?.includes(tag)})]);
    content.push(field("目前做到哪了", progressNote), field("下一步做什么", nextStep),
      node("details", {}, node("summary", {}, "调整分类标签和工具权限"),
      node("p", {class:"muted"}, "默认按内容分类，也可以手动选标签。"),
      ...areaChecks.map(([tag,c]) => node("label", {class:"checkline"}, c, tag)),
      node("label", {class:"checkline"}, shareSummary, "允许已授权的开发工具按项目名读取这张项目卡")));
  }
  if (kind === "context") {
    layer = select(layers, data.layer, () => {}, { name: "layer" });
    tags = node("input", {
      value: data.tags.join("、"),
      maxLength: 480,
      name: "tags",
      placeholder: "用顿号分隔，最多 12 个标签",
    });
    ai = node("input", { type: "checkbox", checked: data.ai, name: "ai" });
    content.push(
      field("层级", layer),
      field("标签", tags),
      node(
        "label",
        { class: "checkline" },
        ai,
        "允许 AI 使用这条资料（使用时会发送给你配置的模型服务）",
      ),
      node(
        "p",
        { class: "fine" },
        "这里保存的内容会存在中枢，并同步到已授权的设备。「仅本机」的内容请在手机上创建。",
      ),
    );
  } else {
    status = select(statuses, data.status, () => {}, { name: "status" });
    due = node("input", {
      type: "date",
      value: data.due_on || "",
      name: "due_on",
      min: "2000-01-01",
      max: "2100-12-31",
    });
    content.push(
      node(
        "div",
        { class: "form-grid" },
        field("状态", status),
        field("目标日期", due),
      ),
    );
  }
  if (parentKinds[kind]) {
    const parents = state.records.filter(
      (r) => sameSpace(r.space, target) && r.kind === parentKinds[kind],
    );
    const options = {
      "": "独立事项",
      ...Object.fromEntries(parents.map((r) => [r.id, r.data.title])),
    };
    if (data.parent_id && !options[data.parent_id])
      options[data.parent_id] = "原关联已失效，请重新选择";
    parent = select(options, data.parent_id || "", () => {}, {
      name: "parent_id",
    });
    content.push(field(`关联${kinds[parentKinds[kind]]}`, parent));
  }
  const spaces = writableSpaces(kind);
  if (!record && !parentRecord && !sourceSpace && Object.keys(spaces).length > 1) {
    const destination = select(spaces, target, value => {
      target = value;
      if (parent) parent.replaceChildren(node("option", {value:""}, "独立事项"),
        ...state.records.filter(r => sameSpace(r.space, target) && r.kind === parentKinds[kind])
          .map(r => node("option", {value:r.id}, r.data.title)));
    });
    content.push(field("保存空间", destination));
  }
  const error = node("p", { class: "form-error", role: "alert" });
  const conflict = node("div", {});
  let command = null;
  const save = node(
    "button",
    { type: "submit", class: "primary" },
    "保存",
  );
  const close = button("取消", () => dialog.close(), "quiet");
  const remove = original
    ? button(
        "删除",
        () =>
          confirmAction(
            "删除这条记录？",
            `「${original.data.title}」会从中枢删除，并同步到已授权设备。有子项时需先解除关联。`,
            async () => {
              await write(original, "delete");
              if (current()) dialog.close();
              await refresh();
              toast("已删除");
            },
            { label: "删除", style: "danger" },
          ),
        "danger",
      )
    : null;
  const form = node(
    "form",
    {},
    ...content,
    error,
    conflict,
    node("div", { class: "form-actions" }, remove, close, save),
  );
  const controls = () => form.querySelectorAll("input,textarea,select");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    conflict.replaceChildren();
    if (!command) {
      const next = {
        ...data,
        title: title.value.trim(),
        body: body.value.trim(),
        domain: domain.value,
      };
      if (!next.title) {
        error.textContent = "请填写标题。";
        title.setAttribute("aria-invalid", "true");
        title.focus();
        return;
      }
      if (kind === "context") {
        if (parent.value) next.parent_id = parent.value;
        else delete next.parent_id;
        next.layer = layer.value;
        next.ai = ai.checked;
        next.tags = tags.value
          .split(/[、,，]/)
          .map((x) => x.trim())
          .filter(Boolean);
        if (next.tags.length > 12 || next.tags.some((t) => t.length > 40)) {
          error.textContent = "最多 12 个标签，每个不超过 40 字。";
          return;
        }
      } else {
        if (kind === "project") {
          next.area_tags = areaChecks.filter(([,c]) => c.checked).map(([tag]) => tag);
          next.progress_note = progressNote.value.trim();
          next.next_step = nextStep.value.trim();
          next.share_summary = shareSummary.checked;
        }
        next.status = status.value;
        next.parent_id = parent?.value || null;
        next.due_on = due.value || null;
        next.completed_at =
          next.status === "done"
            ? data.completed_at || localDay()
            : next.status === "archived"
              ? data.completed_at || null
              : null;
      }
      command = {
        operation_id: crypto.randomUUID(),
        action: "put",
        id: original?.id || crypto.randomUUID(),
        base_revision: original?.revision || 0,
        record: {
          kind,
          space: target,
          mode: original?.mode || "normal",
          data: next,
        },
      };
    }
    save.disabled = true;
    close.disabled = true;
    controls().forEach((c) => (c.disabled = true));
    try {
      await write(null, "put", command);
      if (!current()) {
        await refresh();
        return;
      }
      dialog.close();
      await refresh();
      toast("已保存，已授权的设备下次同步时可见。");
    } catch (err) {
      if (!current()) return;
      error.textContent = err.message;
      if (err.status >= 400 && err.status < 500 && err.status !== 408) {
        command = null;
        controls().forEach((c) => (c.disabled = false));
        if (err.code === "REVISION_CONFLICT")
          conflict.append(
            button("加载中枢版本，放弃当前表单修改", async () => {
              await refresh();
              if (!current()) return;
              const latest = state.records.find((r) => r.id === original?.id);
              if (latest) edit(kind, latest);
              else
                error.textContent = "记录已在中枢删除。你的输入仍保留在此处。";
            }),
          );
      } else {
        save.textContent = "重试同一次保存";
        error.textContent += " 重试不会重复创建；表单暂时锁定以便核对原请求。";
      }
    } finally {
      save.disabled = false;
      close.disabled = false;
    }
  });
  $("editor-content").replaceChildren(
    node(
      "div",
      { class: "dialog-head" },
      node(
        "div",
        {},
        isOwnerSpace(target) ? null : node("p", { class: "eyebrow" }, spaceLabel(target)),
        node("h2", {}, `${record ? "编辑" : "新建"}${kinds[kind]}`),
      ),
      closeButton(dialog),
    ),
    form,
  );
  if (!dialog.open) dialog.showModal();
  title.focus();
}
function showInvitation(value, title) {
  const text = JSON.stringify(value, null, 2);
  const local = /\/\/(localhost|127\.|\[::1\])/.test(value.lan);
  $("editor-content").replaceChildren(
    node("div", { class: "dialog-head" }, node("h2", {}, title), closeButton($("editor"))),
    node("ol", { class: "invite-steps" },
      node("li", {}, "在手机上打开小满，进入「我的」里的配对。"),
      node("li", {}, "粘贴下面复制的邀请。"),
      node("li", {}, "邀请 10 分钟内有效，只能使用一次。")),
    local ? node("p", { class: "notice" }, `现在用的是 ${value.lan}，手机访问不到。请改用中枢的局域网地址打开后台，再重新生成邀请。`) : null,
    node("div", { class: "form-actions" },
      button("关闭", () => $("editor").close(), "quiet"),
      button("复制邀请", async () => {
        try {
          await navigator.clipboard.writeText(text);
          toast("已复制邀请");
        } catch (_) {
          toast("无法自动复制，请展开原文后手动复制");
        }
      }, "primary")),
    node("details", {}, node("summary", {}, "高级：查看邀请原文"),
      node("textarea", { class: "invite-code", value: text, readOnly: true, "aria-label": "配对邀请原文" })),
  );
}
function invitePhone() {
  const generation = ++editorGeneration;
  const current = () => generation === editorGeneration && $("editor").open;
  const dialog = $("editor"),
    name = node("input", { value: "我的手机", maxLength: 80, required: true });
  const error = node("p", { class: "form-error", role: "alert" });
  const submit = node(
    "button",
    { type: "submit", class: "primary" },
    "生成邀请",
  );
  const form = node(
    "form",
    {},
    field("设备名称", name),
    node(
      "p",
      { class: "muted" },
      "手机会连接你的个人资料，不会获得后台管理权限。",
    ),
    error,
    node(
      "div",
      { class: "form-actions" },
      button("取消", () => dialog.close(), "quiet"),
      submit,
    ),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    submit.disabled = true;
    try {
      const value = await request("/console/invitations", {
        method: "POST",
        body: { name: name.value.trim(), spaces: [OWNER_SPACE] },
      });
      if (!current()) return;
      showInvitation(value, "在手机上连接中枢");
    } catch (err) {
      error.textContent = err.message;
      submit.disabled = false;
    }
  });
  $("editor-content").replaceChildren(
    node("div", { class: "dialog-head" }, node("h2", {}, "连接一台手机"), closeButton(dialog)), form);
  dialog.showModal();
  name.select();
}
setInterval(() => {
  if (!document.hidden && state.session) refresh();
}, 15000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.session) refresh();
});
document.addEventListener("DOMContentLoaded", () => {
  request("/console/session").then(enter).catch(() => lockScreen());
}, {once:true});
