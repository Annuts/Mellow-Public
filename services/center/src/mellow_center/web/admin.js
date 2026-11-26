"use strict";
Object.assign(views, {
  connectors: "同步",
  automations: "提醒",
  models: "模型",
  backups: "备份",
  audit: "操作记录",
  family: "家庭",
});
Object.assign(errors, {
  SETTINGS_CONFLICT:
    "设置已被其他窗口或手机修改。输入仍保留，请关闭后刷新并重新编辑。",
  IMPORT_CONFLICT:
    "来源内容在中枢被修改或删除，已阻止覆盖。请拒绝此批次，使用新的来源编号重新导入。",
  OPERATION_REUSED: "请求编号已用于其他内容，请刷新后重试。",
  CONNECTOR_SCOPE_DENIED: "同步来源已暂停或没有此方向的授权。",
  WEAK_PASSPHRASE: "备份口令至少 12 个字符。",
  INVALID_BACKUP_OR_PASSPHRASE: "备份文件或口令不正确。",
  PREVIEW_STALE: "预览后数据发生变化，请重新预览。",
  PREVIEW_REQUIRED: "预览已过期、取消或被另一个窗口替换，请重新预览。",
  BACKUP_OWNER_MISMATCH:
    "这份备份属于另一资料库。空库灾难恢复请按部署手册操作。",
});
const ruleNames = {
  task_due: "到期任务",
  project_stalled: "项目停滞",
  weekly_review: "每周复盘",
};
const actionNames = {
  "connector.save": "保存同步来源",
  "connector.rotate": "重置同步口令",
  "device.revoke": "撤销设备授权",
  "device.remove": "删除设备并撤销授权",
  "import.stage": "提交导入预览",
  "import.approve": "批准导入",
  "import.reject": "拒绝导入",
  "rule.save": "保存规则",
  "rule.delete": "删除规则",
  "preferences.save": "更新主动设置",
  "preferences.learning.clear": "重置学到的提醒偏好",
  "model.save": "更新模型",
  "model.clear": "清除模型凭据",
  "backup.policy": "更新备份计划",
  "backup.restore": "恢复备份",
  "backup.download": "下载自动备份",
  "import.mute": "忽略项目",
  "import.unmute": "恢复接收项目",
  "family.rename": "重命名家庭空间",
  "family.member.rename": "重命名家庭成员",
  "family.member.remove": "移除家庭成员",
  "family.invite.cancel": "取消家庭邀请",
  "family.leave": "成员退出家庭空间",
};
const learnedState = { device: null, rules: [], clues: [], last_day: null, error: "" };
const learnedActions = { notify: "继续提醒", track: "保持跟进", silent: "暂不通知" };
async function loadLearned() {
  const epoch = state.epoch, device = state.session?.device_id;
  try {
    const value = await api("/preferences/learned-rules");
    if (epoch !== state.epoch) return;
    Object.assign(learnedState, { device, rules: value.rules || [], clues: value.clues || [], last_day: value.last_day ?? null, error: "" });
  } catch (e) {
    if (epoch !== state.epoch) return;
    if (e.status === 401) lockScreen(e.message);
    else Object.assign(learnedState, { device, rules: [], clues: [], error: e.message });
  }
}
async function refreshAdmin() {
  const epoch = state.epoch;
  try {
    const [result, devices, family] = await Promise.all([
      request("/console/admin"),
      state.view === "audit" ? api("/devices").catch(() => null) : null,
      state.view === "family" ? loadFamily() : null,
      state.view === "automations" ? loadLearned() : null,
    ]);
    if (epoch !== state.epoch) return;
    state.admin = result;
    if (devices) state.devices = devices.devices;
    if (family) state.family = family;
    if (extraViews[state.view]) render();
  } catch (e) {
    if (epoch === state.epoch) {
      if (e.status === 401) lockScreen(e.message);
      else notice(e.message);
    }
  }
}
const input = (value = "", attrs = {}) => node("input", { value, ...attrs });
const check = (label, value = false) => {
  const control = input("", { type: "checkbox", checked: value });
  return {
    control,
    element: node("label", { class: "checkline" }, control, label),
  };
};
const selectField = (label, options, value) => {
  const control = select(options, value, () => {});
  return { control, element: field(label, control) };
};
const rowActions = (...buttons) =>
  node("div", { class: "admin-actions" }, ...buttons);
function download(name, value) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const link = node("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function showSecret(token) {
  ++editorGeneration;
  $("editor-content").replaceChildren(
    node("h2", {}, "同步口令"),
    node(
      "p",
      {},
      "请保存到来源设备的环境变量 MELLOW_CONNECTOR_TOKEN。关闭后不再显示；30 天有效，可以重置或暂停同步来源。",
    ),
    node("textarea", {
      value: token,
      readOnly: true,
      "aria-label": "同步口令",
    }),
    button("已保存，关闭", () => $("editor").close()),
  );
  if (!$("editor").open) $("editor").showModal();
}
function adminForm(title, fields, read, onSuccess) {
  const generation = ++editorGeneration,
    base = state.admin.revision;
  const current = () => generation === editorGeneration && $("editor").open;
  const error = node("p", { class: "form-error", role: "alert" }),
    save = node("button", { type: "submit", class: "primary" }, "保存设置");
  const form = node(
    "form",
    {},
    ...fields,
    error,
    rowActions(
      button("取消", () => $("editor").close(), "quiet"),
      save,
    ),
  );
  let command;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    try {
      if (!command)
        command = {
          operation_id: crypto.randomUUID(),
          base_revision: base,
          ...(await read()),
        };
      save.disabled = true;
      form
        .querySelectorAll("input,select,textarea")
        .forEach((x) => (x.disabled = true));
      const result = await request("/console/actions", {
        method: "POST",
        body: command,
      });
      if (!current()) {
        await refreshAdmin();
        return;
      }
      $("editor").close();
      await refreshAdmin();
      await refresh();
      if (onSuccess && state.session && !$("editor").open) onSuccess(result);
      else toast("设置已保存");
    } catch (err) {
      if (!current()) return;
      error.textContent = err.message;
      if (err.status >= 400 && err.status < 500 && err.status !== 408) {
        command = null;
        form
          .querySelectorAll("input,select,textarea")
          .forEach((x) => (x.disabled = false));
      } else if (command) {
        save.textContent = "重试同一次保存";
        error.textContent += " 请求结果未知，将使用原编号重试。";
      }
    } finally {
      save.disabled = false;
    }
  });
  $("editor-content").replaceChildren(node("h2", {}, title), form);
  if (!$("editor").open) $("editor").showModal();
}
function adminConfirm(title, description, action, data, onSuccess, options = {}) {
  const command = {
    operation_id: crypto.randomUUID(),
    base_revision: state.admin.revision,
    action,
    data,
  };
  confirmAction(title, description, async () => {
    const result = await request("/console/actions", {
      method: "POST",
      body: command,
    });
    await refreshAdmin();
    await refresh();
    if (onSuccess && state.session) onSuccess(result);
  }, options);
}
const destructive = (label) => ({ label, style: "danger" });
function connectorEdit(c = null) {
  const name = input(c?.name || "通用背景资料", {
    required: true,
    maxLength: 80,
  });
  const adapter = selectField(
    "来源工具",
    {
      generic: "通用文件 / API",
      codex: "Codex",
      opencode: "OpenCode",
      dsh: "DSH",
    },
    c?.adapter || "generic",
  );
  const enabled = check("启用同步来源", c?.enabled || false),
    incoming = check("允许提交导入，审核后入库", c?.import ?? true),
    outgoing = check("允许读取已授权 AI 的背景资料", c?.export || false);
  const choices = Object.entries(layers).map(([id, label]) => ({
    id,
    ...check(label, c?.layers?.includes(id) ?? true),
  }));
  adminForm(
    c ? "编辑同步来源" : "新建同步来源",
    [
      field("同步来源名称", name),
      adapter.element,
      node(
        "p",
        { class: "muted" },
        "工具名称用于区分来源，接入方式统一为文件或 API；当前不会自动读取工具会话。",
      ),
      enabled.element,
      incoming.element,
      outgoing.element,
      node("p", {}, "允许导出的层级"),
      ...choices.map((c) => c.element),
    ],
    () => ({
      action: "connector.save",
      data: {
        id: c?.id || crypto.randomUUID(),
        name: name.value,
        adapter: adapter.control.value,
        space: OWNER_SPACE,
        enabled: enabled.control.checked,
        import: incoming.control.checked,
        export: outgoing.control.checked,
        layers: choices.filter((c) => c.control.checked).map((c) => c.id),
      },
    }),
    (r) => (r.token ? showSecret(r.token) : toast("同步来源已更新")),
  );
}
function importFile(c) {
  const file = input("", {
      type: "file",
      accept: ".json,.md,.txt",
      required: true,
    }),
    description = node(
      "p",
      { class: "muted" },
      "选择你允许同步的文件。JSON 格式为 {items:[{external_id,title,body}]}；Markdown/TXT 作为一条资料。导入后默认不允许 AI 使用。每批最多 100 条、文件最多 512 KB。",
    );
  adminForm(
    "上传并预览",
    [description, field("来源文件", file)],
    async () => {
      const f = file.files[0];
      if (!f || f.size > 512 * 1024)
        throw new Error("请选择不超过 512 KB 的文件");
      const text = await f.text();
      const items = f.name.toLowerCase().endsWith(".json")
        ? JSON.parse(text).items
        : [{ external_id: f.name, title: f.name, body: text }];
      return { action: "import.stage", data: { connector_id: c.id, items } };
    },
    () => toast("已生成待审核批次，请检查内容后批准"),
  );
}
function inspectBatch(b) {
  ++editorGeneration;
  $("editor-content").replaceChildren(
    node("h2", {}, "检查导入内容"),
    node(
      "p",
      {},
      `${b.items.length} 条 · ${stamp(b.expires_at)} 到期`,
    ),
    ...b.items.map((i) =>
      section(
        i.data.title,
        badge(
          i.conflict
            ? "冲突，禁止覆盖"
            : i.base_revision
              ? "更新来源内容"
              : "新增",
        ),
        node("pre", { class: "admin-detail" }, i.data.body),
      ),
    ),
    rowActions(
      button("关闭", () => $("editor").close()),
      button(
        "拒绝批次",
        () =>
          adminConfirm(
            "拒绝这批导入？",
            "不会写入任何背景资料。",
            "import.reject",
            { id: b.id },
            () => $("editor").close(),
            destructive("拒绝"),
          ),
        "danger",
      ),
      button("批准并允许 AI 使用", () => adminConfirm(
        "允许 AI 使用这批资料？",
        "批准后，手机模型和具有导出权限的同步来源可以读取这些摘要。",
        "import.approve", { id: b.id, allow_ai: true }, () => $("editor").close(),
      )),
      button(
        "批准导入",
        () =>
          adminConfirm(
            "批准导入？",
            "所列内容将同步给你的设备；AI 授权默认关闭。已有修改或删除的内容不会被覆盖。",
            "import.approve",
            { id: b.id },
            () => $("editor").close(),
          ),
        "primary",
      ),
    ),
  );
  if (!$("editor").open) $("editor").showModal();
}
const reviewFilters = {query: '', origin: '', tool: ''};

const reviewSelection = {device: null, ids: new Set()};

function reviewTable(admin) {
  const source = id => admin.connectors.find(c => c.id === id)?.name || '来源未知';
  const toolText = b => [...new Set(b.items.flatMap(i =>
    [...i.data.body.matchAll(/〔(Codex|dsh|OpenCode) /g)].map(m => m[1])))].join('、');
  const labels = b => b.items.map(i => i.data.title).join('、');
  const body = node('tbody');
  const count = node('small', {role:'status'});
  if (reviewSelection.device !== state.session.device_id) {
    reviewSelection.device = state.session.device_id;
    reviewSelection.ids.clear();
  }
  const selected = reviewSelection.ids;
  let visible = [];
  const all = node('input',{type:'checkbox','aria-label':'全选当前筛选结果',onchange:()=>{
    for (const b of visible) {
      if (all.checked) selected.add(b.id); else selected.delete(b.id);
    }
    refresh();
  }});
  const decide = (label, approve, allowAI=false) => button(label,()=>{
    const batches = visible.filter(b=>selected.has(b.id));
    if (!batches.length) return;
    const detail = batches.map(b=>labels(b)).join('、');
    adminConfirm(label+'（'+batches.length+' 批）？',
      (allowAI?'批准后，获准的 AI 可以读取这些资料。':'')+detail,
      approve?'import.approve':'import.reject',
      {ids:batches.map(b=>b.id), ...(allowAI?{allow_ai:true}:{})}, null, approve ? {} : destructive('拒绝'));
  }, approve ? 'secondary' : 'danger');
  const actions = [decide('批量批准',true),decide('批量批准并允许 AI 使用',true,true),decide('批量拒绝',false)];
  const updateSelection = () => {
    all.disabled = !visible.length;
    all.checked = visible.length > 0 && selected.size === visible.length;
    all.indeterminate = selected.size > 0 && !all.checked;
    for (const action of actions) action.disabled = !selected.size;
    count.textContent = visible.length+' / '+admin.batches.length+' 批 · 已选 '+selected.size+' 批';
  };
  const refresh = () => {
    const query = reviewFilters.query.trim().toLowerCase();
    const rows = admin.batches.filter(b =>
      (!reviewFilters.tool || toolText(b).split('、').includes(reviewFilters.tool)) &&
      (!reviewFilters.origin || b.connector_id === reviewFilters.origin) &&
      (!query || [labels(b),toolText(b),source(b.connector_id),...b.items.map(i=>i.data.body)]
          .join(' ').toLowerCase().includes(query))
    ).sort((a,b)=>b.created_at.localeCompare(a.created_at));
    visible = rows;
    const available = new Set(rows.map(b=>b.id));
    for (const id of selected) if (!available.has(id)) selected.delete(id);
    updateSelection();
    body.replaceChildren(...rows.map(b => {
      const id = b.items.length === 1 && b.items[0].external_id;
      const project = typeof id === 'string' && /^project-brief-([a-f0-9]{64})-(?:\d+|profile)$/.exec(id);
      const firstTitle = b.items[0]?.data.title || '资料批次';
      const title = firstTitle.length > 80 ? firstTitle.slice(0,80)+'…' : firstTitle;
      const summary = b.items.length > 1 ? '共 '+b.items.length+' 条' : '';
      return node('tr',{},
        node('td',{},node('input',{type:'checkbox',checked:selected.has(b.id),'aria-label':['选择 '+title,summary].filter(Boolean).join(' · '),
          onchange:e=>{if(e.target.checked)selected.add(b.id);else selected.delete(b.id);updateSelection();}})),
        node('td',{},title,summary ? node('small',{},summary) : null), node('td',{},toolText(b) || '其他',node('br'),source(b.connector_id)),
        node('td',{},stamp(b.created_at)),
        node('td',{},rowActions(
          button('查看并审核',()=>inspectBatch(b)),
          button('拒绝本批',()=>adminConfirm('拒绝这一批？',labels(b),'import.reject',{id:b.id},null,destructive('拒绝')),'danger'),
          project ? button('忽略此项目',()=>adminConfirm('以后忽略这个项目？',
            '该同步来源下此项目的待审摘要和后续摘要都将跳过。已入库资料保留，可在忽略列表中恢复接收。',
            'import.mute',{id:b.id},null,destructive('忽略项目'))) : null)));
    }));
    if (!rows.length) body.append(node('tr',{},node('td',{colSpan:5},'没有匹配的待审核内容')));
  };
  const query = node('input',{id:'review-search',type:'search',placeholder:'搜索项目、工具或摘要正文','aria-label':'搜索待审核导入',
    value:reviewFilters.query,oninput:e=>{reviewFilters.query=e.target.value;selected.clear();refresh();}});
  const picker = (key,options,label) => select(options,reviewFilters[key],v=>{reviewFilters[key]=v;selected.clear();refresh();},{'aria-label':label});
  const muted = admin.connectors.flatMap(c=>Object.entries(c.ignored_projects || {}).map(([project,title])=>
    node('p',{},title+' · '+c.name+' ',button('恢复接收',()=>adminConfirm('恢复接收此项目？',
      '该项目的新摘要将再次进入待审核列表。','import.unmute',{connector_id:c.id,project})))));
  refresh();
  return section('待审核导入',count,[
    rowActions(query,picker('tool',{'':'所有工具',Codex:'Codex',dsh:'dsh',OpenCode:'OpenCode'},'按工具筛选'),picker('origin',{'':'所有同步来源',...Object.fromEntries(admin.connectors.map(c=>[c.id,c.name]))},'按同步来源筛选')),
    rowActions(...actions),
    node('div',{class:'table-wrap'},node('table',{class:'stack'},node('thead',{},node('tr',{},node('th',{},all),['项目或标题','工具与来源','提交时间','操作'].map(t=>node('th',{},t)))),body)),
    muted.length ? node('details',{},node('summary',{},'已忽略的项目（'+muted.length+'）'),muted) : null]);
}

let activityHistoryLimit = 20;
function activityPanel() {
  const rows = planningRows("context").filter(r => r.data.tags.includes("agent-activity"))
    .map(record => ({record, event:readActivity(record)}))
    .sort((a,b) => (b.event?.observed_at || b.record.updated_at).localeCompare(a.event?.observed_at || a.record.updated_at));
  const recent = rows.filter(({event}) => event?.fresh && event.namedProject && event.describedTask && event.status !== "状态未知").slice(0, 12);
  const history = rows.filter(row => !recent.includes(row));
  const listRow = ({record, event}) => node("div", {class:"row"},
    node("div", {class:"row-main"}, node("strong", {}, event?.namedProject ? event.project : "未标注项目"),
      event?.describedTask ? node("p", {}, event.task) : null,
      node("small", {}, [...(event ? [event.device, event.toolName] : []), spaceLabel(record.space), stamp(event?.observed_at || record.updated_at)].filter(Boolean).join(" · "))),
    badge(event?.status || "无法读取", event?.fresh && event.describedTask ? "green" : "gray"));
  return section("开发工具会话活动", null, [
    node("p", {class:"muted"}, "插件上报项目、任务短标题和会话状态。一轮会话结束只代表该轮工具活动结束。"),
    recent.length ? recent.map(listRow) : node("p", {class:"muted"}, rows.length ? "近 10 分钟内没有带具体任务标题的会话上报。" : "连接的开发工具上报后，会在这里显示。"),
    history.length ? disclosure("历史与信息不完整的上报（" + history.length + "）", "session-history", [
      history.slice(0,activityHistoryLimit).map(listRow),
      history.length > activityHistoryLimit ? button("显示更多上报", () => {activityHistoryLimit += 20; render();}, "quiet") : null,
    ]) : null,
  ]);
}
function connectorsView() {
  const a = state.admin;
  if (!a) return skeleton();
  return [
    toolbar([button("新建同步来源", () => connectorEdit(), "primary")], {
      search: false,
    }),
    node(
      "p",
      { class: "muted" },
      "每个同步来源使用独立口令。它提交的资料由你审核后入库，能读取的也只限已授权的资料。",
    ),
    node(
      "div",
      { class: "admin-list" },
      ...a.connectors.map((c) =>
        section(c.name, badge(c.enabled ? "已启用" : "已暂停"), [
          node(
            "p",
            {},
            `${{generic:"通用文件 / API",codex:"Codex",opencode:"OpenCode",dsh:"DeepSeek Harness"}[c.adapter] || c.adapter} · ${c.import ? "可提交资料" : "不可提交资料"} · ${c.export ? "可读取已授权资料" : "不可读取资料"}`,
          ),
          node(
            "p",
            { class: "muted" },
            `最近连接：${stamp(c.last_seen)} · 口令到期：${stamp(c.expires_at)}`,
          ),
          rowActions(
            button("编辑", () => connectorEdit(c)),
            c.enabled && c.import
              ? button("上传文件", () => importFile(c))
              : null,
            button("重置口令", () =>
              adminConfirm(
                "重置口令？",
                "旧口令立即失效，需要更新来源设备配置。",
                "connector.rotate",
                { id: c.id },
                (r) => showSecret(r.token),
                destructive("重置"),
              ),
            ),
          ),
        ]),
      ),
    ),
    reviewTable(a),
    activityPanel(),
    disclosure(
      "高级：接入地址",
      "connector-endpoint",
      node(
        "div",
        {},
        node("code", {}, `${location.origin}/api/connectors/context`),
        node(
          "p",
          {},
          "来源设备使用仓库 scripts/connectors/bridge.py，可拉取背景资料或提交所选 JSON/Markdown。证书用 --ca 明确信任，口令通过环境变量传入。",
        ),
      ), "panel",
    ),
  ];
}
function preferenceEdit() {
  const p = state.admin.preferences,
    active = check("启用主动建议", p.proactive),
    model = check(
      "允许模型生成主动建议（消耗调用额度）",
      p.model_proactive || false,
    );
  const count = input(p.daily_limit ?? 3, {
      type: "number",
      min: 0,
      max: 10,
      required: true,
    }),
    modelCount = input(p.model_daily_limit ?? 3, {
      type: "number",
      min: 0,
      max: 20,
      required: true,
    }),
    tz = input(p.timezone || "Asia/Shanghai", { required: true }),
    quiet = input(p.conversation_quiet_seconds ?? 180, { type: "number", min: 0, max: 86400, step: 1, required: true }),
    dreamHour = input(p.dreaming_hour ?? 3, { type: "number", min: 0, max: 7, step: 1, required: true }),
    start = input(p.quiet_start ?? 22, {
      type: "number",
      min: 0,
      max: 23,
      required: true,
    }),
    end = input(p.quiet_end ?? 8, {
      type: "number",
      min: 0,
      max: 23,
      required: true,
    });
  adminForm(
    "主动建议设置",
    [
      active.element,
      model.element,
      field("每日建议上限", count),
      field("每日主动模型调用上限", modelCount),
      field("时区", tz),
      field("安静开始小时", start),
      field("安静结束小时", end),
      field("对话安静秒数（0–86400）", quiet),
      field("夜间整理开始小时（0–7，8 点前执行）", dreamHour),
      node(
        "p",
        {},
        "同一时刻表示不设安静时段；关闭主动建议会撤回未处理建议并取消主动模型作业。",
      ),
    ],
    () => ({
      action: "preferences.save",
      data: {
        proactive: active.control.checked,
        model_proactive: model.control.checked,
        daily_limit: +count.value,
        model_daily_limit: +modelCount.value,
        timezone: tz.value,
        quiet_start: +start.value,
        quiet_end: +end.value,
        conversation_quiet_seconds: boundedInteger(quiet.value, 0, 86400, "对话安静秒数"),
        dreaming_hour: boundedInteger(dreamHour.value, 0, 7, "夜间整理开始小时"),
      },
    }),
  );
}
function boundedInteger(raw, low, high, label) {
  const value = Number(raw);
  if (String(raw).trim() === "" || !Number.isInteger(value) || value < low || value > high)
    throw new Error(`${label}需要是 ${low} 到 ${high} 的整数`);
  return value;
}
function ruleEdit(r = null) {
  const name = input(r?.name || "到期任务提醒", {
      required: true,
      maxLength: 80,
    }),
    kind = selectField("规则类型", ruleNames, r?.kind || "task_due"),
    enabled = check("启用规则", r?.enabled || false),
    tz = input(r?.timezone || "Asia/Shanghai", { required: true }),
    hour = input(r?.hour ?? 9, {
      type: "number",
      min: 0,
      max: 23,
      required: true,
    }),
    day = selectField(
      "复盘星期",
      {
        0: "周一",
        1: "周二",
        2: "周三",
        3: "周四",
        4: "周五",
        5: "周六",
        6: "周日",
      },
      String(r?.weekday ?? 6),
    ),
    days = input(r?.days ?? 7, {
      type: "number",
      min: 1,
      max: 90,
      required: true,
    });
  adminForm(
    r ? "编辑规则" : "新建规则",
    [
      field("规则名称", name),
      kind.element,
      enabled.element,
      field("规则时区", tz),
      field("触发小时", hour),
      day.element,
      field("项目停滞天数", days),
      node(
        "p",
        { class: "muted" },
        "规则产生建议，不自动更改任务或调用外部工具。复盘需有进行中的项目；每个来源每天最多提醒一次。",
      ),
    ],
    () => ({
      action: "rule.save",
      data: {
        id: r?.id || crypto.randomUUID(),
        name: name.value,
        kind: kind.control.value,
        space: OWNER_SPACE,
        enabled: enabled.control.checked,
        timezone: tz.value,
        hour: +hour.value,
        weekday: +day.control.value,
        days: +days.value,
      },
    }),
  );
}
async function previewRule(r) {
  const generation = ++editorGeneration,
    epoch = state.epoch;
  try {
    const value = await request("/console/rules/preview", {
      method: "POST",
      body: { id: r.id },
    });
    if (generation !== editorGeneration || epoch !== state.epoch) return;
    $("editor-content").replaceChildren(
      node("h2", {}, "规则试运行"),
      node("p", {}, value.explanation),
      node(
        "p",
        {},
        value.can_deliver
          ? "当前允许投递新候选"
          : "当前未启用或被安静时段 / 配额限制",
      ),
      ...value.candidates.map((s) =>
        section(s.text, null, node("p", {}, s.reason)),
      ),
      value.candidates.length
        ? null
        : node("p", {}, "当前没有满足触发条件的内容"),
      button("关闭", () => $("editor").close()),
    );
    if (!$("editor").open) $("editor").showModal();
  } catch (e) {
    notice(e.message);
  }
}
const learnedWindow = (w) => (Array.isArray(w) && w.length === 2 ? `${w[0]}:00–${w[1]}:00` : "全天");
function learnedRow(item, isRule) {
  const expired = isRule && item.expires_at && Date.parse(item.expires_at) <= Date.now();
  const disabled = isRule && item.action === "silent" && !item.expires_at;
  const notes = [learnedWindow(item.window), `${(item.sources || []).length} 条依据`];
  if (isRule && item.expires_at) notes.push((expired ? "已过期 " : "到期 ") + stamp(item.expires_at));
  return node("div", { class: "row" }, node("div", { class: "row-main" },
    node("strong", {}, `${item.item_type} → ${learnedActions[item.action] || item.action}`),
    node("p", {}, notes.join(" · ")), !isRule ? node("small", {}, "依据还不足，暂不影响提醒。") : null),
    badge(isRule ? (disabled ? "旧规则已停用" : expired ? "已过期" : "生效中") : "线索", isRule && !expired && !disabled ? "green" : "gray"));
}
function resetLearned(space) {
  confirmAction("重置学到的提醒偏好？",
    "会删除已学到的规则和线索，并取消排队中的整理任务。操作会记入操作记录。",
    async () => {
      const result = await api(`/preferences/learned-rules?space=${encodeURIComponent(space)}`, { method: "DELETE" });
      await loadLearned();
      if (state.view === "automations") render();
      toast(`已重置，清除 ${result.removed} 条`);
    }, destructive("重置"));
}
function learnedPanel() {
  const space = state.space, ready = learnedState.device === state.session.device_id && !learnedState.error;
  const rules = ready ? learnedState.rules.filter((r) => r.space === space) : [],
    clues = ready ? learnedState.clues.filter((c) => c.space === space) : [];
  const p = state.admin.preferences;
    const reset = button("重置提醒学习", () => resetLearned(space), "danger", { disabled: !rules.length && !clues.length });
  let body;
  if (learnedState.error) body = [node("p", { class: "form-error", role: "alert" }, learnedState.error),
    button("重试", async () => { await loadLearned(); render(); }, "quiet")];
  else if (!ready) body = skeleton();
  else body = [rules.length ? rules.map((r) => learnedRow(r, true)) : empty("还没有学到的规则", "规则需要明确依据；关闭一次不会永久停用一类提醒。"),
    clues.length ? disclosure(`待确认的线索（${clues.length}）`, "learned-clues", clues.map((c) => learnedRow(c, false))) : null];
  return section("学到的提醒偏好", reset, [
    node("p", { class: "muted" }, `每天 ${p.dreaming_hour ?? 3}:00 到 8:00 之间整理一次反馈。静默规则最多保留 30 天。`), body]);
}
function suggestionRow(s, refreshView) {
  const feedback = (label, action) => button(label, async e => {
    const control = e.currentTarget;
    control.disabled = true;
    try {
      await api("/suggestions/" + encodeURIComponent(s.id) + "/feedback", {method:"POST", body:{action}});
      await refreshView();
    } catch (error) {
      notice(error.message);
    } finally {
      control.disabled = false;
    }
  }, "quiet");
  return node("div", {class:"row"},
    node("div", {class:"row-main"}, node("strong", {}, s.text), node("p", {}, s.reason),
      spaceLabel(s.space) ? node("small", {}, spaceLabel(s.space)) : null),
    rowActions(feedback("有帮助", "useful"), feedback("忽略", "dismiss")));
}
function automationsView() {
  const a = state.admin;
  if (!a) return skeleton();
  const active = a.suggestions.filter(s => s.state === "new" && Date.parse(s.expires_at) > Date.now())
    .sort((a,b) => b.created_at.localeCompare(a.created_at));
  return [
    toolbar([button("主动建议设置", preferenceEdit), button("新建规则", () => ruleEdit(), "primary")], {search:false}),
    section("我的主动建议", badge(a.preferences.proactive ? "已开启" : "已关闭", a.preferences.proactive ? "green" : "gray"), [
      node("p", {}, "建议给你本人。中枢会显示在「总览」和本页，手机同步后可在小满里查看。"),
      node("p", {class:"muted"}, "开启手机的小满通知并授予系统权限后，手机可在同步时发出本机通知。试运行只显示命中结果。"),
      node("p", {class:"muted"}, "每日最多 " + a.preferences.daily_limit + " 条，遵守全局安静时段。"),
      active.length ? active.map(s => suggestionRow(s, refreshAdmin)) : node("p", {class:"muted"}, "当前没有待处理的建议。"),
    ]),
    section("规则", badge(a.rules.length + " 条"), a.rules.length ? a.rules.map(r =>
      node("div", {class:"row"},
        node("div", {class:"row-main"}, node("strong", {}, r.name),
          node("p", {}, [ruleNames[r.kind], spaceLabel(r.space), r.timezone + " " + r.hour + ":00 后"].filter(Boolean).join(" · "))),
        badge(r.enabled ? "启用" : "暂停", r.enabled ? "green" : "gray"),
        rowActions(button("编辑", () => ruleEdit(r)), button("试运行", () => previewRule(r)),
          button("删除规则", () => adminConfirm("删除规则？", "此规则发出的待处理建议会撤回。", "rule.delete", {id:r.id}, null, destructive("删除")), "danger"))))
        : empty("还没有规则", "可以为到期任务、停滞项目或每周复盘添加规则。")),
    learnedPanel(),
  ];
}
function deepSeekEdit() {
  const current = state.admin.model;
  const official = ["https://api.deepseek.com", "https://api.deepseek.com/v1"];
  const previous = current?.base_url?.replace(/\/+$/, "");
  const sameProvider = official.includes(previous);
  const keepKey = sameProvider && state.admin.has_model_key;
  const models = {
    "deepseek-flash": "DeepSeek Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
  };
  const model = select(models, current?.model in models ? current.model : "deepseek-flash", () => {});
  const key = input("", { type: "password", autocomplete: "new-password", required: !keepKey, maxLength: 4096 });
  adminForm("DeepSeek 设置", [
    field(keepKey ? "API Key（已保存，留空继续使用）" : "DeepSeek API Key", key),
    field("模型", model),
  ], () => {
    const secret = key.value.trim();
    if (!secret && !keepKey) throw new Error("请输入 DeepSeek API Key。");
    return {
      action: "model.save",
      data: {
        config: {
          base_url: sameProvider ? current.base_url : official[0],
          model: model.value,
          spaces: [OWNER_SPACE],
          roles: current?.roles ?? ["chat", "extract", "proactive"],
          daily_limit: current?.daily_limit ?? 20,
          stream: current?.stream ?? false,
        },
        ...(secret ? { key: secret } : {}),
      },
    };
  });
}

function modelEdit() {
  const a = state.admin,
    m = a.model || {
      base_url: "https://api.openai.com/v1",
      model: "",
      spaces: [OWNER_SPACE],
      roles: ["chat"],
      daily_limit: 20,
    };
  const url = input(m.base_url, { required: true }),
    name = input(m.model, { required: true, maxLength: 128 }),
    key = input("", {
      type: "password",
      autocomplete: "new-password",
      maxLength: 4096,
    }),
    limit = input(m.daily_limit, {
      type: "number",
      min: 0,
      max: 1000,
      required: true,
    }),
    local = check("允许中枢本机 HTTP 服务", m.allow_local_http || false),
    stream = check("请求流式响应", m.stream || false),
    roles = Object.entries({
      chat: "聊天",
      extract: "提取记忆",
      proactive: "主动建议",
    }).map(([id, label]) => ({ id, ...check(label, m.roles.includes(id)) }));
  adminForm(
    "中枢模型设置",
    [
      field("API 基础地址", url),
      field("模型 ID", name),
      field("API Key（留空保留；地址变化会清除旧 Key）", key),
      field("每日调用上限（UTC 日）", limit),
      local.element,
      stream.element,
      node("p", {}, "允许的用途"),
      ...roles.map((x) => x.element),
    ],
    () => ({
      action: "model.save",
      data: {
        config: {
          base_url: url.value,
          model: name.value,
          daily_limit: +limit.value,
          allow_local_http: local.control.checked,
          stream: stream.control.checked,
          spaces: [OWNER_SPACE],
          roles: roles.filter((x) => x.control.checked).map((x) => x.id),
        },
        ...(key.value ? { key: key.value } : {}),
      },
    }),
  );
}
function modelsView() {
  const a = state.admin;
  if (!a) return skeleton();
  const m = a.model;
  const rows = [
    ["模型", m ? m.model : "尚未配置"],
    ["密钥", a.has_model_key ? "已保存（不会显示）" : "未保存"],
    ["今日调用", `${a.usage.calls} 次，上限 ${m?.daily_limit ?? 0} 次（按 UTC 日计）`],
  ];
  return [
    section("中枢模型", button(m ? "修改 DeepSeek 设置" : "设置 DeepSeek", deepSeekEdit, "primary"), [
      node("dl", { class: "kv" }, rows.flatMap(([k, v]) => [node("dt", {}, k), node("dd", {}, v)])),
      node("p", { class: "muted" },
        "这里只配置中枢的模型。手机直连模型的密钥由手机自己保管。修改配置会取消旧配置下排队中的任务。"),
      rowActions(
        button("测试连接", () =>
          confirmAction(
            "测试模型连接？",
            "会发送一句固定的测试文本，消耗一次调用额度，不会发送你的个人内容。",
            async () => {
              await api("/model/probe", { method: "POST" });
              await refreshAdmin();
              toast("连接成功。流式输出和结构化输出需要实际使用后才能确认。");
            },
            { label: "开始测试" },
          ),
        ),
        m ? button("清除模型和密钥", () =>
          adminConfirm("清除模型和密钥？", "中枢的模型调用会暂停。你的资料和手机上的模型配置不受影响。",
            "model.clear", {}, null, destructive("清除")), "danger") : null,
      ),
    ]),
    disclosure("高级", "model-advanced", [
      m ? node("dl", { class: "kv" }, node("dt", {}, "接口地址"), node("dd", {}, m.base_url)) : null,
      node("div", { class: "admin-actions" }, button("自定义模型服务…", modelEdit, "secondary")),
    ], "panel"),
  ];
}
function policyEdit() {
  const a = state.admin,
    p = a.backup_policy || {},
    enabled = check("启用每日自动备份", p.enabled || false),
    keep = input(p.keep ?? 7, {
      type: "number",
      min: 1,
      max: 90,
      required: true,
    }),
    hour = input(p.hour ?? 3, {
      type: "number",
      min: 0,
      max: 23,
      required: true,
    }),
    tz = input(p.timezone || "Asia/Shanghai", { required: true }),
    password = input("", {
      type: "password",
      minLength: 12,
      autocomplete: "new-password",
    });
  adminForm(
    "自动备份计划",
    [
      enabled.element,
      field("保留份数", keep),
      field("执行小时", hour),
      field("备份时区", tz),
      field("备份口令（至少 12 字符；留空保留）", password),
      node(
        "p",
        { class: "muted" },
        "备份保存在中枢数据目录的 backups 子目录。关闭计划会移除保存的备份口令，已有文件保留；更换口令后旧文件仍需旧口令解密。",
      ),
    ],
    () => ({
      action: "backup.policy",
      data: {
        enabled: enabled.control.checked,
        keep: +keep.value,
        hour: +hour.value,
        timezone: tz.value,
        ...(password.value ? { password: password.value } : {}),
      },
    }),
  );
}
function backupWizard(restore = false) {
  const generation = ++editorGeneration,
    current = () => generation === editorGeneration && $("editor").open;
  const password = input("", {
      type: "password",
      required: true,
      minLength: 12,
      autocomplete: "new-password",
    }),
    repeat = input("", {
      type: "password",
      required: !restore,
      minLength: 12,
      autocomplete: "new-password",
    }),
    file = input("", { type: "file", required: restore, accept: ".json,.enc" }),
    error = node("p", { class: "form-error", role: "alert" }),
    submit = node(
      "button",
      { type: "submit", class: "primary" },
      restore ? "检查并预览恢复" : "下载加密备份",
    );
  const form = node(
    "form",
    {},
    node(
      "p",
      {},
      restore
        ? "恢复当前资料库的备份。先查看新增、冲突和删除数量，再确认；现有冲突记录保留，备份中的删除记录会生效。"
        : "下载包含背景资料和人生记录的加密文件。不包含模型密钥、同步口令、自动化配置或设备身份；请单独保存备份口令。",
    ),
    restore ? field("加密备份文件", file) : null,
    field("备份口令", password),
    restore ? null : field("再次输入口令", repeat),
    error,
    rowActions(
      button("取消", () => $("editor").close()),
      submit,
    ),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    submit.disabled = true;
    try {
      if (!restore) {
        if (password.value !== repeat.value) throw new Error("两次口令不一致");
        const result = await api("/backup", {
          method: "POST",
          body: { password: password.value },
        });
        if (!current()) return;
        download(`mellow-${localDay()}.enc`, result);
        $("editor").close();
        toast("加密备份已下载");
      } else {
        const f = file.files[0];
        if (!f || f.size > 32 * 1024 * 1024)
          throw new Error("请选择不超过 32 MB 的备份文件");
        const result = await api("/restore/preview", {
          method: "POST",
          body: {
            password: password.value,
            bundle: JSON.parse(await f.text()),
          },
        });
        password.value = "";
        if (!current()) return;
        const commit = button(
          "确认恢复",
          async () => {
            commit.disabled = true;
            try {
              const saved = await api("/restore/commit", {
                method: "POST",
                body: { confirmation_token: result.confirmation_token },
              });
              if (!current()) return;
              $("editor").close();
              await refresh();
              await refreshAdmin();
              toast(
                `恢复完成：新增 ${saved.restored} 条，保留冲突 ${saved.skipped_conflicts} 条`,
              );
            } catch (err) {
              if (current()) error.textContent = err.message;
            } finally {
              commit.disabled = false;
            }
          },
          "primary",
        );
        $("editor-content").replaceChildren(
          node("h2", {}, "恢复预览"),
          node(
            "p",
            {},
            `新增 ${result.additions} 条 · 保留现有冲突 ${result.conflicts} 条 · 删除 ${result.deletions} 条`,
          ),
          node(
            "p",
            {},
            "确认后会同步给设备。预览 10 分钟有效；数据变化后需要重新预览。此操作不会恢复密钥、模型配置或同步来源。",
          ),
          error,
          rowActions(
            button("取消恢复", async () => {
              try {
                await api("/restore/preview", { method: "DELETE" });
                if (current()) $("editor").close();
              } catch (err) {
                error.textContent = err.message;
              }
            }),
            commit,
          ),
        );
      }
    } catch (err) {
      if (current()) error.textContent = err.message;
    } finally {
      submit.disabled = false;
    }
  });
  $("editor-content").replaceChildren(
    node("h2", {}, restore ? "恢复加密备份" : "创建备份"),
    form,
  );
  if (!$("editor").open) $("editor").showModal();
}
function backupsView() {
  const a = state.admin;
  if (!a) return skeleton();
  return [
    section(
      "已生成的自动备份",
      null,
      (a.backup_files || []).length
        ? a.backup_files.map((f) =>
            node(
              "div",
              {},
              node("p", {}, `${f.name} · ${Math.ceil(f.size / 1024)} KB`),
              button("下载这份备份", async () => {
                try {
                  const bundle = await request(
                    `/console/backup-files/${encodeURIComponent(f.name)}`,
                  );
                  download(f.name, bundle);
                  await refreshAdmin();
                } catch (e) {
                  notice(e.message);
                }
              }),
            ),
          )
        : empty("还没有自动备份", "在下面启用自动备份，中枢会每天生成加密备份。"),
    ),
    toolbar(
      [
        button("下载加密备份", () => backupWizard(), "primary"),
        button("从文件恢复", () => backupWizard(true)),
      ],
      { search: false },
    ),
    section("自动备份", button("编辑备份计划", policyEdit), [
      node(
        "p",
        {},
        a.backup_policy?.enabled
          ? `每天 ${a.backup_policy.hour}:00 后 · ${a.backup_policy.timezone} · 保留 ${a.backup_policy.keep} 份`
          : "计划未启用",
      ),
      disclosure("高级：备份位置", "backup-directory", node("p", { class: "admin-detail" },
        a.backup_policy?.directory || "启用后保存在中枢数据目录 / backups")),
      node(
        "p",
        {},
        a.last_backup
          ? `最近结果：${a.last_backup.state === "succeeded" ? "成功" : "失败"} · ${stamp(a.last_backup.finished_at)}`
          : "尚无自动备份结果",
      ),
      node(
        "p",
        { class: "muted" },
        "只有中枢服务运行时才执行。自动备份仍在同一台机器，建议定期把下载文件另存到其他设备。如果中枢数据丢失，请按部署手册在全新的中枢上恢复，并重新配对设备、重新配置模型密钥。",
      ),
    ]),
  ];
}
function auditView() {
  const names = Object.fromEntries(state.devices.map((d) => [d.id, d.name]));
  const who = (id) => names[id] || "已删除的设备";
  const target = (a) => names[a.target] || (/^[0-9a-f-]{36}$/.test(a.target) ? "" : a.target.slice(0, 40));
  const rows = (state.admin?.audit || []).map((a) =>
    node("div", { class: "row" },
      node("div", { class: "row-main" },
        node("strong", {}, actionNames[a.action] || a.action),
        node("p", {}, [who(a.actor), target(a)].filter(Boolean).join(" · "))),
      node("small", { class: "muted" }, stamp(a.at))));
  return section("最近的管理操作", badge(rows.length + " 条"),
    rows.length ? rows : empty("还没有管理操作", "修改设置、审核资料或管理设备后，会在这里留下记录。"));
}

async function loadFamily() {
  try {
    return { groups: (await api("/family")).groups };
  } catch (e) {
    if (e.status === 401) throw e;
    if (e.status !== 404) return { error: e.message };
    try {
      const legacy = await api("/groups");
      return { groups: legacy.groups.map((g) => ({ ...g, members: null, invites: [] })), limited: true };
    } catch (error) {
      return { error: error.message };
    }
  }
}
async function reloadFamily() {
  const family = await loadFamily();
  if (family) state.family = family;
  await refresh();
  if (state.view === "family") render();
}
function nameDialog({ title, label, value = "", submit, hint, run }) {
  const generation = ++editorGeneration;
  const current = () => generation === editorGeneration && $("editor").open;
  const name = input(value, { maxLength: 80, required: true });
  const error = node("p", { class: "form-error", role: "alert" });
  const save = node("button", { type: "submit", class: "primary" }, submit);
  const form = node("form", {},
    field(label, name),
    hint ? node("p", { class: "field-hint" }, hint) : null,
    error,
    node("div", { class: "form-actions" }, button("取消", () => $("editor").close(), "quiet"), save));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = name.value.trim();
    if (!text) {
      error.textContent = "请填写名称。";
      name.setAttribute("aria-invalid", "true");
      name.focus();
      return;
    }
    save.disabled = true;
    error.textContent = "";
    try {
      await run(text);
    } catch (err) {
      if (current()) error.textContent = err.message;
    } finally {
      save.disabled = false;
    }
  });
  name.addEventListener("input", () => name.removeAttribute("aria-invalid"));
  $("editor-content").replaceChildren(
    node("div", { class: "dialog-head" }, node("h2", {}, title), closeButton($("editor"))), form);
  if (!$("editor").open) $("editor").showModal();
  name.select();
}
function createFamily() {
  nameDialog({
    title: "新建家庭空间", label: "家庭空间名称", value: "我们家", submit: "新建",
    hint: "家庭空间只共享清单和日程。个人记忆、聊天和模型用量不会共享。",
    run: async (name) => {
      await api("/groups", { method: "POST", body: { name } });
      $("editor").close();
      await reloadFamily();
      toast("已新建家庭空间，接着邀请成员吧");
    },
  });
}
function renameFamily(group) {
  nameDialog({
    title: "重命名家庭空间", label: "家庭空间名称", value: group.name, submit: "保存",
    run: async (name) => {
      await api(`/groups/${encodeURIComponent(group.id)}/rename`, { method: "POST", body: { name } });
      $("editor").close();
      await reloadFamily();
      toast("已重命名");
    },
  });
}
function renameMember(group, member) {
  nameDialog({
    title: "重命名成员设备", label: "设备名称", value: member.name, submit: "保存",
    hint: "只改变这里的显示名称，不影响对方手机。",
    run: async (name) => {
      await api(`/groups/${encodeURIComponent(group.id)}/members/${encodeURIComponent(member.id)}/rename`,
        { method: "POST", body: { name } });
      $("editor").close();
      await reloadFamily();
      toast("已重命名");
    },
  });
}
function inviteFamily(group) {
  nameDialog({
    title: `邀请加入「${group.name}」`, label: "对方的设备名称", value: "家人的手机", submit: "生成邀请",
    hint: "对方只能看到这个家庭空间里的清单和日程，不会获得管理权限。",
    run: async (name) => {
      const path = `/groups/${encodeURIComponent(group.id)}`;
      let value;
      try {
        value = await api(`${path}/invitation`, { method: "POST", body: { name } });
      } catch (e) {
        if (e.status !== 404) throw e;
        value = { ...(await api(`${path}/invite`, { method: "POST", body: { name } })), lan: location.origin };
      }
      showInvitation(value, `邀请加入「${group.name}」`);
      await reloadFamily();
    },
  });
}
function removeMember(group, member) {
  confirmAction(
    `移除「${member.name}」？`,
    `它将无法再看到「${group.name}」里的内容，需要重新被邀请才能加入。已经下载到对方手机上的副本会保留。`,
    async () => {
      await api(`/groups/${encodeURIComponent(group.id)}/members/${encodeURIComponent(member.id)}`, { method: "DELETE" });
      await reloadFamily();
      toast(`已移除「${member.name}」`);
    },
    { label: "移除", style: "danger" },
  );
}
function cancelInvite(group, invite) {
  confirmAction(
    "取消这个邀请？",
    `「${invite.name}」的邀请会立即失效。`,
    async () => {
      await api(`/groups/${encodeURIComponent(group.id)}/invites/${encodeURIComponent(invite.id)}`, { method: "DELETE" });
      await reloadFamily();
      toast("邀请已取消");
    },
    { label: "取消邀请", style: "danger" },
  );
}
const memberStatus = {
  active: ["已加入", "green"], waiting: ["已加入 · 尚未同步", "gray"],
  expired: ["授权已到期", "gray"], revoked: ["已撤销", "gray"],
};
function familyGroup(group) {
  const members = group.members;
  const memberRow = (m) => node("div", { class: "row" },
    node("div", { class: "row-main" },
      node("strong", {}, m.name),
      node("p", {}, [platformNames[m.platform] || "尚未上报型号", m.last_seen ? "最近在线 " + stamp(m.last_seen) : "还没有在线过"].join(" · "))),
    badge(...memberStatus[m.status] || [m.status]),
    rowActions(button("重命名", () => renameMember(group, m), "quiet"), button("移除", () => removeMember(group, m), "danger")));
  const inviteRow = (i) => node("div", { class: "row" },
    node("div", { class: "row-main" }, node("strong", {}, i.name), node("p", {}, "等待加入 · " + stamp(i.expires_at) + " 前有效")),
    badge("邀请中", "gray"),
    rowActions(button("取消邀请", () => cancelInvite(group, i), "danger")));
  return section(group.name, rowActions(
    button("重命名", () => renameFamily(group), "quiet"),
    button("邀请成员", () => inviteFamily(group), "primary")), [
    members === null
      ? node("p", { class: "muted" }, "当前中枢版本暂不支持查看成员列表，更新中枢后即可管理成员。")
      : [members.map(memberRow), group.invites.map(inviteRow),
        members.length || group.invites.length ? null
          : empty("还没有成员", "点「邀请成员」，在家人的手机上粘贴邀请即可加入。")],
    node("p", { class: "muted" }, members === null ? "" : `你的 ${group.owner_devices} 台管理员设备自动属于每个家庭空间。`),
  ]);
}
function familyView() {
  const f = state.family;
  if (!f) return skeleton();
  if (f.error) return empty("暂时读不到家庭空间", f.error, button("重试", refresh, "primary"));
  return [
    f.groups.length ? toolbar([button("新建家庭空间", createFamily, "primary")], { search: false }) : null,
    f.groups.length ? f.groups.map(familyGroup)
      : empty("还没有家庭空间", "家庭空间让你和家人共用购物清单和日程。个人记忆和聊天不会共享。",
        button("新建家庭空间", createFamily, "primary")),
    node("p", { class: "muted" }, "家庭空间只共享「清单」和「日历」。成员的手机连接后，会在小满里看到这个空间。"),
  ];
}
Object.assign(extraViews, {
  connectors: connectorsView,
  automations: automationsView,
  models: modelsView,
  backups: backupsView,
  audit: auditView,
  family: familyView,
});
