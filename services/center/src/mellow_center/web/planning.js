"use strict";

function planningRows(kind) {
  return kind ? state.records.filter(r => r.kind === kind) : state.records;
}

const byDueDate = (a, b) => (a.data.due_on || "9999").localeCompare(b.data.due_on || "9999")
  || b.updated_at.localeCompare(a.updated_at);

const activityLabels = {running:"运行中", waiting:"等待输入", completed:"本轮完成",
  stopped:"已停止", failed:"本轮失败", unknown:"状态未知"};
const toolLabels = {codex:"Codex", opencode:"OpenCode", dsh:"DeepSeek Harness", "claude-code":"Claude Code"};
const activityWindow = 10 * 60000;

function readActivity(record) {
  if (record.kind !== "context" || !record.data.tags.includes("agent-activity")) return null;
  let event;
  try { event = JSON.parse(record.data.body); } catch { return null; }
  if (!event || !["project", "task", "tool", "device", "observed_at"].every(k => typeof event[k] === "string")
    || !Number.isFinite(Date.parse(event.observed_at))) return null;
  const project = event.project.trim();
  const task = event.task.trim().replace(/^已归档 · /, "");
  const namedProject = project && !["未命名项目", "未知项目"].includes(project);
  const describedTask = task && !["会话进行中", "未知", "未命名会话"].includes(task);
  const fresh = Date.now() - Date.parse(event.observed_at) <= activityWindow;
  const label = activityLabels[event.status] || activityLabels.unknown;
  const status = !fresh && ["running", "waiting", "unknown"].includes(event.status) ? "上次上报 · " + label : label;
  return {...event, project, task, namedProject, describedTask, fresh, status, toolName:toolLabels[event.tool] || event.tool};
}

function projectEvidence(record) {
  if (record.kind !== "context" || record.mode !== "normal") return null;
  const d = record.data;
  if (d.tags.includes("agent-activity")) {
    const event = readActivity(record);
    if (!event?.namedProject || !event.describedTask) return null;
    return {record, project:event.project, type:"设备活动", at:event.observed_at, text:event.task,
      source:[event.device, event.toolName, event.status].join(" · ")};
  }
  // Existing mobile references and connector briefs both encode the project in the title.
  const match = /^(.+) · (收藏资料|项目简介|项目交接)(?: \d+)?$/u.exec(d.title);
  if (!match && !d.parent_id) return null;
  const lines = d.body.split("\n").filter(line => line.trim() && !line.startsWith("项目："));
  const summary = lines.filter(line => line.startsWith("会话报告的进展")).at(-1) || lines[0] || d.body;
  return {record, project:d.parent_id ? nameOf(d.parent_id) : match[1].trim(), type:match?.[2] || "关联资料", at:record.updated_at, text:summary,
    source:d.tags.includes("项目资料") ? "手机收藏" : "中枢背景资料"};
}

function evidenceCard(e) {
  return node("details", {class:"evidence-card", "data-key":e.record.id},
    node("summary", {}, node("strong", {}, e.project+" · "+e.type),
      node("small", {}, [spaceLabel(e.record.space), e.source, stamp(e.at)].filter(Boolean).join(" · ")),
      node("span", {}, e.text.slice(0, 180))),
    node("p", {class:"body-copy"}, e.type === "设备活动" ? e.text : e.record.data.body),
    e.type === "设备活动" ? null : button("查看与编辑资料", () => edit("context", e.record), "quiet"));
}

function projectPortfolio(records) {
  const items = [], byName = new Map(), byId = new Map(), clues = new Map();
  const keyOf = title => title.trim().toLowerCase();
  for (const r of records.filter(r => r.kind === "project")) {
    const item = {id:r.id, title:r.data.title, space:r.space, record:r, evidence:[], tasks:[], previews:[], at:r.updated_at};
    const key = keyOf(item.title);
    if (!byName.has(key)) byName.set(key, []);
    byName.get(key).push(item);
    items.push(item);
    byId.set(r.id, item);
  }
  function findProject(title, space, at) {
    const key = keyOf(title), named = byName.get(key) || [];
    const inSpace = named.filter(p => sameSpace(p.space, space));
    if (inSpace.length === 1) return inSpace[0];
    if (named.length === 1) return named[0];
    const clueKey = JSON.stringify([named.length ? ownerSpace(space) : "", key]);
    if (!clues.has(clueKey)) {
      const item = {id:"evidence:"+clueKey, title, space, evidence:[], tasks:[], previews:[], at, ambiguous:named.length > 0};
      items.push(item);
      clues.set(clueKey, item);
    }
    return clues.get(clueKey);
  }
  for (const r of records) {
    if (r.kind === "task") {
      const item = byId.get(r.data.parent_id);
      if (item && sameSpace(item.space, r.space)) item.tasks.push(r);
      continue;
    }
    const e = projectEvidence(r);
    if (!e) continue;
    const item = byId.get(r.data.parent_id) || findProject(e.project, r.space, e.at);
    item.evidence.push(e);
  }
  for (const preview of state.projectPreviews || []) {
    if (!hasSpace(state.session.spaces, preview.space)) continue;
    findProject(preview.project, preview.space, preview.at).previews.push(preview);
  }
  for (const item of items) {
    item.tasks.sort(byDueDate);
    item.evidence.sort((a,b) => Date.parse(b.at) - Date.parse(a.at));
    for (const at of [item.evidence[0]?.at, ...item.tasks.map(t => t.updated_at)]) {
      if (Date.parse(at) > Date.parse(item.at)) item.at = at;
    }
  }
  return items.sort((a,b) => Date.parse(b.at) - Date.parse(a.at) || a.title.localeCompare(b.title));
}

function taskProgressLabel(project) {
  const tasks = project.tasks.filter(t => t.data.status !== "archived" || t.data.completed_at);
  const done = tasks.filter(t => t.data.status === "done" || t.data.completed_at).length;
  return tasks.length ? `${done} / ${tasks.length} 项任务完成` : project.record ? "尚未拆分任务" : "尚无任务记录";
}

const projectTags = ["工作", "生活", "个人项目"];
const recentWindow = 14 * 86400000;

function projectFacts(p) {
  const data = p.record?.data || {};
  const preview = {};
  for (const row of p.previews) {
    for (const key of ["description","purpose","progress","next"]) {
      if (row[key] && !preview[key]) preview[key] = row[key];
    }
  }
  const signals = {};
  const briefs = [...p.evidence.filter(e => e.type === "项目简介"), ...p.evidence.filter(e => e.type === "项目交接")];
  for (const e of briefs) {
    for (const line of e.record.data.body.split("\n")) {
      const m = /^(项目简介|会话报告的进展|待确认) · (.+?)(?:〔.*)?$/.exec(line);
      if (!m || signals[m[1]]) continue;
      if (/https?:\/\/|\]\(|(?:现在|请|可以先|接下来你|建议你)(?:打开|点击?|进入|查看|看)|如果你|你愿意|要不要我/.test(m[2])) continue;
      signals[m[1]] = m[2];
    }
  }
  const declared = p.evidence.find(e => e.type === "项目简介");
  const declaredText = declared?.record.data.body.match(/^项目简介 · ([^〔\n]+)/m)?.[1]?.trim();
  const activity = p.evidence.find(e => e.type === "设备活动" && Date.now() - Date.parse(e.at) < recentWindow);
  const task = p.tasks.find(t => t.data.status === "active");
  const completed = p.tasks.filter(t => t.data.status === "done" || t.data.completed_at)
    .sort((a,b) => b.updated_at.localeCompare(a.updated_at))[0];
  return {
    focus: activity?.text || "",
    objective: preview.purpose || "",
    purpose: data.body || declaredText || signals["项目简介"] || preview.description || "",
    purposeSource: data.body ? "" : declaredText || signals["项目简介"] ? "已入库的项目资料" : preview.description ? "设备摘要 · 待核对" : "",
    progress: data.progress_note || (completed && "已完成「"+completed.data.title+"」") || signals["会话报告的进展"] || preview.progress || "",
    progressSource: data.progress_note ? "项目记录" : completed ? "任务记录" : signals["会话报告的进展"] ? "会话报告" : preview.progress ? "设备摘要 · 待核对" : "",
    next: data.next_step || task?.data.title || signals["待确认"] || preview.next || "",
    nextSource: data.next_step || task ? "" : signals["待确认"] ? "待确认事项" : "设备摘要 · 待核对"
  };
}

function classifyProject(p, facts) {
  if (p.record?.data.area_tags?.length) return p.record.data.area_tags;
  const description = [p.title, facts.purpose, facts.objective, facts.progress, facts.next, ...p.evidence.slice(0,12).map(e => e.text)].join(" ");
  const tags = [];
  if (/客户|公司业务|同事|团队交付|工作项目|商务|部门|企业客户|client delivery/i.test(description)) tags.push("工作");
  if (/家庭|家人|家中|家居|健身|饮食|衣橱|家务|旅行|烹饪|智能家居|全屋|窗帘|Home Assistant/i.test(description)
    || ["home","health","food","travel","family","clothing"].includes(p.record?.data.domain)) tags.push("生活");
  if (/个人项目|自己的|自用|个人助理|私人管家|兴趣|爱好|独立开发|业余|个人中枢|人生管理|个人上下文|side project/i.test(description)) tags.push("个人项目");
  if (!tags.length && p.record?.data.domain === "work") tags.push("工作");
  return tags;
}

function openProject(project) {
  state.project = project.id;
  state.projectTag = "全部";
  state.archived = project.record?.data.status === "archived";
  navigate("projects");
}

function projectSummary(p) {
  const facts = projectFacts(p), tags = classifyProject(p, facts);
  return node("div", {class:"row-main"}, node("strong", {}, p.title),
    node("small", {}, [...tags, p.record ? statuses[p.record.data.status] : p.previews.length ? "设备摘要 · 待核对" : "设备活动"].join(" · ")),
    facts.purpose ? node("p", {}, "项目简介　"+facts.purpose.slice(0,110)+(facts.purposeSource.includes("待核对") ? "（待核对）" : "")) : null,
    facts.focus && facts.focus !== facts.purpose ? node("p", {}, "最近在做　"+facts.focus.slice(0,110)) : null,
    facts.progress && facts.progress !== facts.focus ? node("p", {}, (facts.progressSource.includes("待核对") ? "设备报告　" : "进展　")+facts.progress.slice(0,110)) : null,
    facts.next && facts.next !== facts.progress ? node("p", {}, (facts.nextSource ? "待确认　" : "下一步　")+facts.next.slice(0,100)) : null,
    p.tasks.length ? node("p", {}, taskProgressLabel(p)) : null);
}

function reviewLink() {
  const count = Object.values(state.pendingImports || {}).reduce((total, n) => total + n, 0);
  return button(count ? `审核设备摘要（${count} 批）` : "查看待审核导入", () => {
    reviewFilters.query = reviewFilters.tool = reviewFilters.origin = "";
    navigate("connectors");
  }, "quiet");
}

function portfolioOverview(portfolio) {
  const recent = portfolio.filter(p => p.record ? p.record.data.status === "active" :
    p.evidence.some(e => e.type === "设备活动" && Date.now() - Date.parse(e.at) < recentWindow));
  return section("最近在做什么", button("全部跟进", () => navigate("projects"), "quiet"), [
    recent.length ? recent.slice(0,4).map(p => node("div", {class:"row"}, projectSummary(p), button("看项目", () => openProject(p), "quiet")))
      : node("p", {class:"muted"}, "还没有进行中的项目或近两周的设备活动。"),
    recent.length ? null : button("新建项目", () => edit("project"))]);
}

function directionHistory(records) {
  const notes = records.filter(r => ["journal","review","activity","habit_log"].includes(r.kind)
    && (r.kind !== "habit_log" || r.data.done)).sort((a,b) => b.updated_at.localeCompare(a.updated_at));
  const recent = projectPortfolio(records).filter(p => p.evidence.some(e => e.type === "设备活动"
    && Date.now() - Date.parse(e.at) < recentWindow)).slice(0,3);
  if (!notes.length && !recent.length) return null;
  return section("最近把时间花在哪里", null, [
    recent.map(p => node("div", {class:"row"}, projectSummary(p), button("看项目", () => openProject(p), "quiet"))),
    notes.slice(0,5).map(r => node("details", {class:"evidence-card","data-key":r.id},
      node("summary", {}, node("strong", {}, r.data.title || r.data.text?.slice(0,80) || "生活记录"), node("small", {}, r.data.date || stamp(r.updated_at))),
      node("p", {class:"body-copy"}, r.data.body || r.data.text)))]);
}

function lifeHistory(records) {
  const entries = records.filter(r => r.mode === "normal" && (["journal","review","vision","goal"].includes(r.kind)
    || (r.kind === "project" && (r.data.status === "done" || r.data.completed_at))
    || directionMaterial(r))).map(r => ({
      id:r.id, day:r.data.date || r.data.completed_at || r.updated_at.slice(0,10),
      kind:({journal:"日记",review:"复盘",vision:"方向",goal:"目标",project:"完成的项目",context:"长期背景",memory:"有效记忆"})[r.kind],
      title:r.data.title || "", text:r.data.body || r.data.text || "",
    })).sort((a,b) => b.day.localeCompare(a.day) || a.id.localeCompare(b.id));
  const query = state.search.trim().toLowerCase();
  const visible = entries.filter(e => [e.day,e.kind,e.title,e.text].join(" ").toLowerCase().includes(query));
  function exportEntries() {
    const lines = visible.map(e => [e.day+" · "+e.kind, e.title, e.text].filter(Boolean).join("\n"));
    const blob = new Blob(["我的人生记录\n\n"+lines.join("\n\n")], {type:"text/plain;charset=utf-8"});
    const url = URL.createObjectURL(blob), link = node("a", {href:url, download:"小满-人生记录-"+localDay()+".txt"});
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const entry = e => node("details", {class:"evidence-card","data-key":"history-"+e.id},
    node("summary", {}, node("strong", {}, e.title || e.text.slice(0,60)), node("small", {}, e.day+" · "+e.kind)),
    node("p", {class:"body-copy"}, e.text));
  return section("我的人生记录", button("记一件事", () => livingForm("journal")), [
    visible.slice(0,3).map(entry),
    visible.length > 3 ? node("details", {"data-key":"older-life"}, node("summary", {}, "更早的记录（"+(visible.length-3)+"）"), visible.slice(3).map(entry)) : null,
    visible.length ? button("下载这 "+visible.length+" 条记录", exportEntries, "quiet")
      : node("p", {class:"muted"}, query ? "没有匹配的记录。" : "日记、复盘、方向、目标和完成的项目会按时间保存在这里。")]);
}

function directionMaterial(r) {
  if (r.mode !== "normal") return false;
  if (r.kind === "context") return r.data.layer === "profile";
  if (r.kind === "journal") return r.data.tags.includes("小满问答");
  return r.kind === "memory" && r.effective_status === "active" && ["goal","preference"].includes(r.data.category)
    && (r.data.goal_state || "open") === "open";
}

function directionOverview(records) {
  const visions = records.filter(r => r.kind === "vision" && r.data.status === "active");
  const goals = records.filter(r => r.kind === "goal" && r.data.status === "active");
  const rows = [...visions, ...goals];
  const materials = records.filter(directionMaterial);
  return section("人生方向与目标", button("整理人生方向", () => navigate("life"), "quiet"),
    rows.length ? rows.slice(0,5).map(r => node("div", {class:"row"}, node("div", {class:"row-main"},
      node("strong", {}, r.data.title), node("p", {}, [spaceLabel(r.space), kinds[r.kind], r.data.body].filter(Boolean).join(" · "))),
      button("编辑", () => edit(r.kind,r), "quiet")))
    : [node("p", {}, materials.length ? "可以从已记录的目标或偏好开始整理。" : "你想长期为哪件事留出时间？"),
      materials[0] ? node("p", {class:"body-copy"}, materials[0].data.title || materials[0].data.text) : null,
      button("写下第一个方向", () => edit("vision"))]);
}

function directionSources(records, portfolio) {
  const materials = records.filter(directionMaterial).filter(r => [r.data.title, r.data.body, r.data.text]
    .some(s => s?.toLowerCase().includes(state.search.toLowerCase()))).sort((a,b) => b.updated_at.localeCompare(a.updated_at));
  const unlinked = portfolio.filter(p => p.record?.data.status === "active" && !p.record.data.parent_id && matches(p.record));
  if (!materials.length && !unlinked.length) return null;
  return section("待整理的方向线索", null, [
    materials.slice(0,12).map(r => {
      const text = r.data.body || r.data.text || r.data.title;
      return node("details", {class:"evidence-card", "data-key":"direction-"+r.id}, node("summary", {}, node("strong", {}, r.data.title || text.slice(0,100)),
        node("small", {}, [spaceLabel(r.space), r.kind === "context" ? "长期背景" : r.kind === "journal" ? "我的问答" : "已记录的"+(r.data.category === "goal" ? "目标" : "偏好"), stamp(r.updated_at)].filter(Boolean).join(" · "))),
        node("p", {class:"body-copy"}, text),
        button("据此写方向", () => edit("vision", null, {body:text, domain:r.data.domain || "general"}, r.space)));
    }),
    unlinked.map(p => node("div", {class:"row"}, projectSummary(p),
      button("关联目标", () => edit("project", p.record), "quiet"))),
  ]);
}

function projects() {
  const records = planningRows();
  const query = state.search.trim().toLowerCase();
  const portfolio = projectPortfolio(records).filter(p => (state.archived || p.record?.data.status !== "archived")
    && (state.projectTag === "全部" || classifyProject(p, projectFacts(p)).includes(state.projectTag))
    && [p.title, ...Object.values(projectFacts(p)), ...p.tasks.map(t => t.data.title), ...p.evidence.map(e => e.record.data.body)]
      .some(s => s?.toLowerCase().includes(query)));
  const currentProjects = portfolio.filter(p => p.record?.data.status === "active" ||
    p.evidence.some(e => e.type === "设备活动" && Date.now() - Date.parse(e.at) < recentWindow));
  const primary = query || state.projectTag !== "全部" ? portfolio : currentProjects;
  const other = portfolio.filter(p => !primary.includes(p));
  const chosen = portfolio.find(p => p.id === state.project) || primary[0] || other[0];
  const projectIds = new Set(records.filter(r => r.kind === "project").map(r => r.id));
  const looseTasks = records.filter(r => r.kind === "task" && !projectIds.has(r.data.parent_id)
    && (state.archived || r.data.status !== "archived") && matches(r)).sort(byDueDate);
  const projectButton = p => button(node("div", {class:"row-main"}, node("strong", {}, p.title),
    node("small", {}, classifyProject(p,projectFacts(p)).join(" · ") || (p.record ? statuses[p.record.data.status] : "设备线索"))), () => {
    state.project = p.id;
    render();
  }, "project-tile"+(chosen === p ? " selected" : ""));
  const picker = node("div", {class:"project-picker", "aria-label":"项目列表"},
    primary.length ? node("div", {class:"project-group"}, primary.map(projectButton)) : null,
    other.length ? node("div", {class:"project-group"}, node("p", {class:"group-label"}, "其他项目与线索"), other.map(projectButton)) : null);
  let detail;
  if (chosen) {
    const p = chosen, r = p.record;
    const tasks = p.tasks.filter(t => state.archived || t.data.status !== "archived");
    const facts = projectFacts(p);
    const activities = p.evidence.filter(e => e.type === "设备活动");
    const factRow = (label, text, source) => node("div", {class:"project-fact"}, node("strong", {}, label),
      node("p", {}, text || "尚未记录"), text && source ? node("small", {}, source) : null);
    detail = section(p.title, r ? button("编辑项目", () => edit("project",r), "quiet")
      : p.ambiguous ? null : button("加入我的项目", () => edit("project", null, {title:p.title, body:facts.purpose, progress_note:facts.progress, next_step:facts.next}, p.previews[0]?.space || p.space), "primary"), [
      node("div", {class:"card-meta"}, classifyProject(p,facts).map(tag => badge(tag)), p.tasks.length ? badge(taskProgressLabel(p)) : null),
      factRow("项目简介", facts.purpose || "还没有项目简介", facts.purposeSource),
      !facts.purpose && facts.objective ? factRow("摘要中提到的目标", facts.objective, "设备摘要 · 待核对") : null,
      facts.focus ? factRow("最近在做", facts.focus, "") : null,
      factRow("做到哪了", facts.progress, facts.progressSource),
      factRow(facts.nextSource ? "接下来要确认" : "下一步", facts.next, facts.nextSource),
      p.previews.some(v => v.profile_batch) ? button("核对项目简介和进展", async () => {
        const epoch = state.epoch, id = p.previews.find(v => v.profile_batch).profile_batch;
        try {
          const response = await request("/console/admin");
          if (epoch !== state.epoch || state.view !== "projects") return;
          state.admin = response;
          const batch = response.batches.find(b => b.id === id);
          if (batch) inspectBatch(batch);
          else await refresh();
        } catch (error) { toast(error.message); }
      }, "quiet") : null,
      p.ambiguous ? node("p", {}, "有多个同名项目，这些设备线索尚待关联。") : null,
      r?.data.parent_id ? node("p", {}, "关联目标　"+nameOf(r.data.parent_id)) : null,
      r ? node("div", {class:"section-header"}, node("h3", {}, "任务"),
        button("添加任务", () => edit("task", null, {parent_id:r.id, domain:r.data.domain}), "primary")) : null,
      tasks.map(taskRow),
      node("details", {class:"project-sources", "data-key":p.id}, node("summary", {}, "相关资料与来源"),
        p.evidence.filter(e => e.type !== "设备活动").map(evidenceCard),
        activities.length ? node("details", {"data-key":"activity-"+p.id}, node("summary", {}, "查看设备活动"),
          activities.map(evidenceCard)) : null,
        p.previews.length ? button("核对这个项目的设备摘要", () => {
          reviewFilters.query = p.title+" · ";
          reviewFilters.tool = reviewFilters.origin = "";
          navigate("connectors");
        }) : null,
        r ? button("关联一条资料", () => edit("context", null, {layer:"active", parent_id:r.id, domain:r.data.domain}, r.space)) : null)]);
  }
  return [toolbar([select(Object.fromEntries(["全部",...projectTags].map(t => [t,t])), state.projectTag, value => {
    state.projectTag = value; render();
  }, {"aria-label":"按项目标签筛选"}), node("label", {class:"checkline"}, node("input", {type:"checkbox", checked:state.archived,
    onchange:e => {state.archived = e.target.checked; render();}}), "显示归档"),
    button("独立任务", () => edit("task")), button("新建项目", () => edit("project"), "primary")]),
    detail ? node("div", {class:"project-layout"}, picker, detail) : looseTasks.length ? null : empty(query ? "没有匹配的项目或任务" : "中枢还没有项目或设备摘要",
      query ? "试试搜索项目名、任务名或交接内容。" : "可以新建项目、审核电脑提交的摘要，或在手机同步已有项目。"),
    looseTasks.length ? section("独立任务", badge(looseTasks.length + " 项"), looseTasks.map(taskRow)) : null,
    Object.values(state.pendingImports || {}).some(Boolean) ? section("设备导入", reviewLink(), null) : null];
}
