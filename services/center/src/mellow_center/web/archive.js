"use strict";
// Kept separate from collection/inbox: archive quotes are live, permission-checked originals.
views.lifeArchive = "人生档案";
const lifeArchiveState = {entries:null, letters:[], grants:[], contacts:[], audit:[], received:[], loading:false,
  period:"", person:"", place:"", tag:"", featured:false};
const archiveVisibility = {private:"仅自己", future_self:"留给未来的我", designated:"可交给指定的人"};
const archiveRun = action => async () => { try { await action(); } catch (e) { toast(e.message || "操作失败"); } };
function archiveField(label, input) { input.setAttribute("aria-label", label); return node("label", {}, label, input); }
function archiveInput(value="", type="text") { return node("input", {type, value}); }
function archiveDialog(title, children) {
  const dialog = $("editor"); $("editor-content").replaceChildren(closeButton(dialog), node("h2", {}, title), ...children); dialog.showModal();
}
async function archiveLoad() {
  if(lifeArchiveState.loading) return;
  lifeArchiveState.loading = true;
  try {
    const query = new URLSearchParams();
    for (const key of ["period","person","place","tag"]) if(lifeArchiveState[key]) query.set(key,lifeArchiveState[key]);
    if(lifeArchiveState.featured) query.set("featured","true");
    const [a,l,g,r] = await Promise.all([api("/archive?"+query),api("/archive/letters"),api("/archive/legacy"),api("/archive/legacy/received")]);
    Object.assign(lifeArchiveState, {entries:a.entries,letters:l.letters,grants:g.grants,contacts:g.contacts,audit:g.audit,received:r.grants});
  } finally { lifeArchiveState.loading=false; if(state.view==="lifeArchive") render(); }
}
function archiveAnnotate(e) {
  const m=e.metadata, visibility=node("select",{},...Object.entries(archiveVisibility).map(([v,t])=>node("option",{value:v,selected:m.visibility===v},t)));
  const reflection=node("textarea",{rows:5,value:m.reflection});
  const people=archiveInput(m.people.join("，")),places=archiveInput(m.places.join("，")),tags=archiveInput(m.tags.join("，"));
  const featured=node("input",{type:"checkbox",checked:m.featured});
  const parts = input => input.value.split(/[,，]/).map(s=>s.trim()).filter(Boolean);
  archiveDialog("整理档案 · 不改原话", [archiveField("档案精选",featured),archiveField("可见范围",visibility),
    node("p",{class:"muted"},"选择可交给指定的人不会立即共享；还需要单独启用托付并满足交付条件。"),
    archiveField("后来反思（与当时原话分开）",reflection),archiveField("人物（逗号分隔，可填写人物记录 ID）",people),
    archiveField("地点",places),archiveField("主题",tags),button("保存",archiveRun(async()=>{
      await api("/archive/entries/"+encodeURIComponent(e.id)+"/note",{method:"PUT",body:{featured:featured.checked,visibility:visibility.value,
        reflection:reflection.value,people:parts(people),places:parts(places),tags:parts(tags)}});
      $("editor").close(); await archiveLoad();
    }),"primary")]);
}
function archiveCard(e,readonly=false) {
  return node("details",{class:"card","data-key":e.id},node("summary",{},node("strong",{},e.title),
    node("p",{},e.at+" · "+archiveVisibility[e.metadata.visibility]+(e.metadata.featured?" · 档案精选":""))),
    ...e.quotes.map(q=>node("div",{},node("p",{class:"muted"},`${q.at} · ${q.id} · 修订 ${q.revision}`),node("blockquote",{class:"body-copy"},q.text))),
    ...(e.revisions||[]).map(r=>node("details",{},node("summary",{},`先前修订 ${r.revision} · ${r.revised_at}`),node("blockquote",{class:"body-copy"},r.text))),
    e.metadata.reflection?node("p",{class:"body-copy"},"后来的反思："+e.metadata.reflection):null,
    readonly?null:button("精选、反思与可见范围",()=>archiveAnnotate(e)));
}
function archiveNewLetter() {
  const title=archiveInput(),text=node("textarea",{rows:8}),date=archiveInput("","datetime-local"),condition=archiveInput();
  const recipient=node("select",{},node("option",{value:"self"},"未来的我"),...lifeArchiveState.contacts.map(c=>node("option",{value:c.id},c.name)));
  archiveDialog("写给未来",[archiveField("标题",title),archiveField("信件原文",text),archiveField("开启日期",date),archiveField("收信人",recipient),
    archiveField("附加约定（可选；到期后仍需本人确认）",condition),node("p",{class:"muted"},"封存是界面约束，不是对资料库所有者的密码锁。正文保存在加密库；不会发送邮件。"),
    button("封存",archiveRun(async()=>{
      if(!date.value || !Number.isFinite(new Date(date.value).getTime()) || new Date(date.value)<=new Date()) throw new Error("请选择未来的开启日期");
      await api("/operations",{method:"POST",body:{operation_id:crypto.randomUUID(),id:crypto.randomUUID(),action:"put",base_revision:0,
        record:{kind:"letter",space:OWNER_SPACE,mode:"source_only",data:{title:title.value,text:text.value,
          open_at:new Date(date.value).toISOString(),recipient:recipient.value,condition:condition.value}}}});
      $("editor").close();await archiveLoad();
    }),"primary")]);
}
function archiveNewGrant() {
  if(!lifeArchiveState.contacts.length) { toast("请先在家庭空间配对指定亲友设备"); return; }
  const recipient=node("select",{},...lifeArchiveState.contacts.map(c=>node("option",{value:c.id},c.name+" · "+c.id)));
  const type=node("select",{},node("option",{value:"inactivity"},"长期未活跃"),node("option",{value:"date"},"固定日期"));
  const date=archiveInput("","datetime-local"),days=archiveInput("180","number"),waiting=archiveInput("14","number"),tags=archiveInput(),ids=archiveInput();
  const first=node("input",{type:"checkbox"}),second=node("input",{type:"checkbox"});
  archiveDialog("托付与继承 · 默认关闭",[node("p",{},"只读交付：只有逐条标为「可交给指定的人」且符合范围的原件可被查看。未活跃方式还须亲友最终确认和至少 14 天等待；本人活动会取消。不能把它视为死亡证明或法律遗嘱。"),
    archiveField("指定亲友设备（先在家庭空间配对）",recipient),archiveField("主题（可选，逗号分隔）",tags),archiveField("条目 ID（可选，逗号分隔）",ids),
    archiveField("交付条件",type),archiveField("固定日期",date),archiveField("未活跃天数",days),archiveField("确认后等待天数",waiting),
    archiveField("我了解只读交付与离线副本不能远程收回",first),archiveField("我再次确认这个人、范围和条件",second),
    button("启用这一项托付",archiveRun(async()=>{
      const list=e=>e.value.split(/[,，]/).map(s=>s.trim()).filter(Boolean);
      await api("/archive/legacy",{method:"POST",body:{recipient_id:recipient.value,scope:{spaces:[OWNER_SPACE],tags:list(tags),entry_ids:list(ids)},
        condition:type.value==="date"?{type:"date",at:new Date(date.value).toISOString()}:{type:"inactivity",days:Number(days.value),waiting_days:Number(waiting.value)},
        confirm:first.checked,confirm_again:second.checked}});$("editor").close();await archiveLoad();
    }),"primary")]);
}
function archiveChat(grantId=null, spaces=null) {
  const question=archiveInput(),output=node("div",{role:"status"});
  // A received grant is queried in the space its entries came from; own archive is always personal.
  const space=spaces?.length?ownerSpace(spaces[0]):OWNER_SPACE;
  archiveDialog(grantId?"与 TA 的回忆对话":"与过去的我对话",[node("p",{},"这是模拟，不是本人。仅选取有日期的本人原句；没有依据就说「我当时没有说过这件事」。所选原文会发送给你配置的模型。"),
    archiveField("想问什么",question),button("从原话中回答",archiveRun(async()=>{
      const result=await api("/archive/digital-self",{method:"POST",body:{question:question.value,space,...(grantId?{grant_id:grantId}:{})}});
      output.replaceChildren(node("p",{class:"body-copy"},result.text));
    }),"primary"),output]);
}
function archiveExport() {
  const pass=archiveInput("","password");pass.autocomplete="new-password";
  const visibility=node("select",{},node("option",{value:""},"全部可见范围"),...Object.entries(archiveVisibility).map(([v,t])=>node("option",{value:v},t)));
  archiveDialog("导出人生档案",[node("p",{},"ZIP 含 Markdown 原话、修订、原附件、稳定 ID 与校验清单；留空口令为明文 ZIP。离开小满仍能阅读。离线副本无法远程撤回。"),
    archiveField("导出可见范围",visibility),archiveField("可选独立口令（至少 12 字符）",pass),button("下载",archiveRun(async()=>{
      const encrypted=Boolean(pass.value);const response=await fetch("/api/preview/archive/export",{method:"POST",credentials:"same-origin",
        headers:{"X-Mellow-Console":"1","Content-Type":"application/json"},body:JSON.stringify({period:lifeArchiveState.period,...(visibility.value?{visibility:visibility.value}:{}),...(encrypted?{passphrase:pass.value}:{})})});
      if(!response.ok)throw new ApiError((await response.json()).code,response.status);
      const url=URL.createObjectURL(await response.blob()),link=node("a",{href:url,download:encrypted?"mellow-archive.encrypted.json":"mellow-archive.zip"});
      document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);$("editor").close();
    }),"primary")]);
}
extraViews.lifeArchive = function() {
  const admin=Boolean(state.session.is_admin), owner=admin || Boolean(state.session.owner_enrolled);
  if(lifeArchiveState.entries===null&&!lifeArchiveState.loading) archiveLoad().catch(e=>toast(e.message));
  const filters=[...[['period','时期（年 / 年-月）'],['person','人物'],['place','地点'],['tag','主题']].map(([key,label])=>{
    const input=archiveInput(lifeArchiveState[key]);input.addEventListener("change",()=>lifeArchiveState[key]=input.value.trim());return archiveField(label,input);
  }),button("当时我是怎么想的 · 查询",archiveRun(archiveLoad)),button("仅看档案精选",archiveRun(async()=>{lifeArchiveState.featured=!lifeArchiveState.featured;await archiveLoad();}))];
  return [node("section",{class:"card"},node("h2",{},"人生档案"),node("p",{},"原话与修订分开保留。默认仅自己；留给未来和交给亲友是两个独立决定。"),...filters,
    button("写给未来",archiveNewLetter),button("与过去的我对话",()=>archiveChat()),button("导出档案",archiveExport)),
    ...(lifeArchiveState.entries||[]).map(e=>archiveCard(e)),
    node("section",{class:"card"},node("h2",{},"写给未来"),...lifeArchiveState.letters.map(r=>node("details",{},node("summary",{},r.data.title+" · "+r.data.open_at),
      node("p",{class:"body-copy"},r.sealed?"尚未到约定日期或附加条件未确认，信件仍在封存。":r.data.text),r.data.condition?node("p",{},"约定："+r.data.condition):null,
      r.data.condition&&!r.data.condition_confirmed?button("本人确认附加条件已满足",archiveRun(async()=>{
        await api("/operations",{method:"POST",body:{operation_id:crypto.randomUUID(),action:"put",id:r.id,base_revision:r.revision,
          record:{kind:"letter",space:ownerSpace(r.space),mode:r.mode,data:{...state.records.find(x=>x.id===r.id).data,condition_confirmed:true}}}});await archiveLoad();
      })):null))),
     node("section",{class:"card"},node("h2",{},"托付与继承"),admin?button("新增托付（双重确认）",archiveNewGrant):null,owner?button("我在 · 取消未活跃交付",archiveRun(async()=>{await api("/archive/legacy/check-in",{method:"POST",body:{}});await archiveLoad();})):null,
       ...lifeArchiveState.grants.map(g=>node("div",{},node("p",{},`${g.data.recipient_id} · ${g.data.status} · ${JSON.stringify(g.data.condition)}`),
         g.data.recipient_id===state.session.device_id && g.data.status==="pending" && !g.data.confirmed_at?button("亲友最终确认 · 开始至少 14 天可取消等待",archiveRun(async()=>{await api(`/archive/legacy/${g.id}/confirm`,{method:"POST",body:{confirm:true}});await archiveLoad();})):null,
         admin?button("立即撤销",archiveRun(async()=>{await api(`/archive/legacy/${g.id}/revoke`,{method:"POST",body:{}});await archiveLoad();})):null)),
      node("details",{},node("summary",{},"托付审计日志"),...lifeArchiveState.audit.map(e=>node("p",{},`${e.at} · ${e.action} · ${e.actor}`)))),
     ...lifeArchiveState.received.map(g=>node("section",{},node("h2",{},"获托付的只读档案"),button("与 TA 的回忆对话",()=>archiveChat(g.id,[...new Set(g.entries.map(e=>e.space))])),...g.entries.map(e=>archiveCard(e,true))))];
};
