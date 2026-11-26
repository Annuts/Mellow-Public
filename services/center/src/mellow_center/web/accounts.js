"use strict";

views.accounts = "外部生活账户";
let accountsLoading = false;
let accountsLoadingEpoch = null;

async function loadAccounts() {
  if (accountsLoading && accountsLoadingEpoch === state.epoch) return;
  accountsLoading = true;
  const epoch = state.epoch;
  accountsLoadingEpoch = epoch;
  try {
    const value = await request("/console/accounts");
    if (epoch === state.epoch && state.session) {
      state.accounts = value;
      state.accountsEpoch = epoch;
      if (state.view === "accounts") render();
    }
  } catch (e) {
    if (epoch === state.epoch) {
      state.accounts = { error: e.message };
      state.accountsEpoch = epoch;
      if (state.view === "accounts") render();
    }
  } finally {
    if (accountsLoadingEpoch === epoch) accountsLoading = false;
  }
}

function editAccount(kind) {
  const data = state.accounts[kind];
  const baseRevision = state.accounts.revision;
  const enabled = check("启用此连接器（默认关闭）", data.enabled);
  const controls = {};
  const fields = [enabled.element];
  const add = (key, label, type = "text", fallback = "") => {
    const e = input(type === "password" ? "" : data[key] ?? fallback, {
      type, autocomplete: "off", maxLength: 2000,
    });
    controls[key] = e;
    fields.push(field(label, e));
  };
  if (kind === "ha") {
    fields.push(node("p", { class: "muted" }, "仅访问你选中的设备；每次控制都必须在手机上确认。令牌加密保存在家中中枢，不会回显。"));
    add("base_url", "Home Assistant 地址", "url", "http://host.docker.internal:8123");
    add("token", "长期访问令牌（留空保留原凭据）", "password");
    add("entities", "允许的实体 ID（逗号分隔，如 light.living_room）");
    controls.entities.value = (data.entities || []).join(", ");
  } else {
    fields.push(node("p", { class: "muted" }, "只读 INBOX 的主题、发件人、日期、邮件编号；不读取正文。仅导入命中发件人或主题关键词白名单的邮件，规则为空时不导入。账单与快递提示只是待确认草稿，不会自动创建付款或提醒。"));
    add("host", "IMAP SSL 服务器（QQ / 163 / Gmail / Outlook）");
    add("port", "SSL 端口", "number", 993);
    add("username", "邮箱账号");
    add("password", "应用密码 / 授权码（留空保留原凭据）", "password");
    add("days", "最近多少天（1—30）", "number", 7);
    add("senders", "发件人白名单（完整邮箱，逗号分隔）");
    add("keywords", "主题关键词白名单（逗号分隔）");
    controls.senders.value = (data.senders || []).join(", ");
    controls.keywords.value = (data.keywords || []).join(", ");
    fields.push(node("p", { class: "muted" }, "授权中枢定期连接邮箱服务器；导入资料留在家中，仅按你已有的同步与模型授权使用。Outlook / Gmail 若禁用密码登录，请先确认服务商允许 IMAP 应用密码；不支持 OAuth 登录。"));
  }
  const forget = check("同时清除中枢保存的凭据（请先关闭此连接器）");
  if (data.has_credentials) fields.push(forget.element);
  const error = node("p", { class: "form-error", role: "alert" });
  const save = node("button", { type: "submit", class: "primary" }, "保存设置");
  const form = node("form", {}, ...fields, error, rowActions(button("取消", () => $("editor").close(), "quiet"), save));
  const dialog = $("editor");
  const generation = ++editorGeneration;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    save.disabled = true;
    error.textContent = "";
    try {
      const next = { enabled: enabled.control.checked };
      for (const [key, e] of Object.entries(controls)) {
        if (["entities", "senders", "keywords"].includes(key)) next[key] = e.value.split(/[,，\n]/).map(s => s.trim()).filter(Boolean);
        else if (["port", "days"].includes(key)) next[key] = Number(e.value);
        else if (e.type !== "password" || e.value) next[key] = e.type === "password" ? e.value : e.value.trim();
      }
      if (forget.control.checked) next[kind === "ha" ? "token" : "password"] = "";
      form.querySelectorAll("input").forEach(e => { e.disabled = true; });
      await request("/console/accounts", { method: "PUT", body: { revision: baseRevision, [kind]: next } });
      if (generation === editorGeneration) {
        Object.values(controls).filter(e => e.type === "password").forEach(e => { e.value = ""; });
        dialog.close();
      }
      await loadAccounts();
      toast("设置已保存；关闭连接器可立即停止后续读取。已有导入可在收集箱删除。");
    } catch (e) {
      if (generation === editorGeneration) error.textContent = e.message;
    } finally {
      save.disabled = false;
      form.querySelectorAll("input").forEach(e => { e.disabled = false; });
    }
  });
  $("editor-content").replaceChildren(node("h2", {}, kind === "ha" ? "家里 · Home Assistant" : "邮箱 · IMAP 只读"), form);
  if (!dialog.open) dialog.showModal();
}

function accountsView() {
  if (!state.accounts || state.accountsEpoch !== state.epoch) {
    queueMicrotask(loadAccounts);
    return skeleton();
  }
  const a = state.accounts;
  if (a.error) return empty("暂时读不到账户设置", a.error, button("重试", loadAccounts, "primary"));
  const card = (kind, title, description) => section(title,
    rowActions(badge(a[kind].enabled ? "已启用" : "已关闭", a[kind].enabled ? "green" : "gray"), button("设置", () => editAccount(kind))),
    [node("p", {}, description), node("p", { class: "muted" }, a[kind].has_credentials ? "凭据已加密保存，不会在页面或接口中返回。" : "尚未保存凭据。"),
      kind === "mail" && a.mail.status?.message ? node("p", { role: "status" }, a.mail.status.message) : null,
      kind === "mail" && a.mail.status?.last_success_at ? node("p", { class: "muted" }, "最近读取：" + stamp(a.mail.status.last_success_at) + "；下次间隔至少 15 分钟。") : null]);
  return [
    toolbar([button("刷新状态", loadAccounts)], { search: false }),
    node("p", { class: "muted" }, "每个连接器都默认关闭。只读取你明确选择的内容；不会把整本通讯录、通知原文或邮件正文上传。"),
    card("ha", "家里 · Home Assistant", "读取所选灯、开关、空调、传感器和窗帘。控制操作先生成草稿，只能在手机确认后执行。"),
    card("mail", "邮箱 · IMAP 只读", "按白名单把邮件信头放入收集箱。无规则不导入、不标记已读、不移动或删除邮件。"),
    section("账单与人物", null, [node("p", {}, "在手机「我的 → 连接 → 外部生活账户」中开启账单解析或逐个选择联系人。账单默认仅本机；联系人号码默认不保存。不需要在此填写手机权限。")]),
  ];
}

extraViews.accounts = accountsView;
