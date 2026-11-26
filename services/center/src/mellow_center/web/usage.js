"use strict";
views.usage = "使用量";
actionNames['usage.prices'] = '更新用量单价';
const usageState = {period:'month', report:null, key:'', loading:false, fetched:0, error:''};
const costText = costs => Object.entries(costs || {}).map(([c,n])=>`${c === 'CNY' ? '¥' : '$'}${Number(n).toFixed(6)}`).join(' · ') || '暂无已计价用量';
function usageRange() {
  const today = new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  return {start:usageState.period==='today'?today:usageState.period==='month'?today.slice(0,7)+'-01':'',end:today};
}
async function loadUsage() {
  if(usageState.loading)return;
  const epoch=state.epoch, range=usageRange(), key=JSON.stringify([epoch,range]);
  usageState.loading=true;usageState.error='';
  try {
    const result=await api(`/usage?start=${range.start}&end=${range.end}`);
    if(epoch!==state.epoch)return;
    usageState.report=result;usageState.key=key;usageState.fetched=Date.now();
  } catch(e) {if(epoch===state.epoch)usageState.error=e.message;}
  finally {usageState.loading=false;if(epoch===state.epoch&&state.view==='usage')render();}
}
function usagePricesEdit() {
  if(!state.admin || !usageState.report){toast("正在读取用量，请稍后再试。");return;}
  const prices=structuredClone(usageState.report?.prices || {});
  const names=[...new Set([...Object.keys(prices),...Object.keys(usageState.report?.models||{}),state.admin?.model?.model].filter(Boolean))];
  const model=input(state.admin?.model?.model || names[0] || '',{maxLength:128,list:'usage-models'});
  const currency=selectField('币种',{CNY:'人民币 CNY',USD:'美元 USD'},'CNY');
  const units=[input('',{type:'number',min:0,max:1000000,step:'any',required:true}),input('',{type:'number',min:0,max:1000000,step:'any',required:true}),input('',{type:'number',min:0,max:1000000,step:'any',required:true})];
  const keys=['input_per_million','cached_input_per_million','output_per_million'];
  const load=()=>{const p=prices[model.value]||{};currency.control.value=p.currency||'CNY';units.forEach((n,i)=>n.value=p[keys[i]]??'');};load();model.addEventListener('change',load);
  adminForm('模型单价（每百万 Token）',[
    field('模型 ID',model),node('datalist',{id:'usage-models'},names.map(n=>node('option',{value:n}))),currency.element,
    ...units.map((n,i)=>field(['输入（未命中缓存）','输入（缓存命中）','输出'][i],n)),
    node('p',{class:'muted'},'单价仅影响后续调用，历史保留调用时价格。默认 DeepSeek 参考 2026-11-17 官方高峰价；空闲、节假日和套餐折扣可能使实扣更低。'),
    node('a',{href:'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',target:'_blank',rel:'noopener noreferrer'},'查看 DeepSeek 官方价格'),
  ],()=>{
    if(!model.value.trim()||units.some(n=>!n.value||!Number.isFinite(Number(n.value))||Number(n.value)<0||Number(n.value)>1000000))throw new Error('请填写模型和有效单价。');
    prices[model.value.trim()]={currency:currency.control.value,...Object.fromEntries(keys.map((k,i)=>[k,units[i].value]))};
    return {action:'usage.prices',data:{prices}};
  },()=>{usageState.fetched=0;loadUsage();});
}
function usageTable(title, rows) {
  if(!rows.length)return section(title,null,empty('这段时间没有记录','调用模型后会计入这里。'));
  return section(title,null,node('div',{class:'table-wrap'},node('table',{},
    node('thead',{},node('tr',{},['名称','调用次数','输入','输出','缓存命中','合计用量'].map(t=>node('th',{},t)))),
    node('tbody',{},rows.map(([name,r])=>node('tr',{},[name,r.calls,r.prompt_tokens,r.completion_tokens,r.cached_tokens,r.total_tokens].map(v=>node('td',{},v))))))));
}
function usageView() {
  const key=JSON.stringify([state.epoch,usageRange()]);
  if((key!==usageState.key||Date.now()-usageState.fetched>15000)&&!usageState.loading&&!usageState.error)queueMicrotask(loadUsage);
  const r=key===usageState.key?usageState.report:null;
  return [toolbar([
    select({today:'今天',month:'本月',all:'全部'},usageState.period,v=>{usageState.period=v;usageState.error='';usageState.fetched=0;render();}),
    button(usageState.loading?'正在读取…':'刷新用量',()=>loadUsage()),Object.assign(button('设置模型单价',usagePricesEdit),{disabled:!state.admin||!r}),
    button('导出统计',()=>{if(r)download('mellow-usage.json',r);}),
  ],{search:false}),
    ...(r?[
      node('div',{class:'metrics'},metric('总用量',r.totals.total_tokens.toLocaleString('zh-CN'),'单位 Token（模型计费用量），按服务商返回的输入和输出统计'),metric('中枢 DeepSeek 估算费用',r.center_deepseek.calls ? costText(r.center_deepseek.costs) : '¥0', '仅统计 Mac mini 调用 DeepSeek · '+r.center_deepseek.calls+' 次'),metric('调用次数',r.totals.calls,`未返回用量 ${r.totals.missing_usage} 次`)),
      node('p',{class:'muted'},'按北京时间统计；包含中枢、已同步手机及获准项目的插件用量。同一回执去重。费用仅统计中枢 DeepSeek。手机直连、开发工具和本地摘要只展示用量，不估算费用。'),
      usageTable('按来源',Object.entries(r.sources).map(([s,v])=>[{generic:'通用同步来源',local_summary:'本地摘要',center:'中枢',phone:'手机',codex:'Codex',opencode:'OpenCode',dsh:'DeepSeek Harness'}[s]||s,v])),
      usageTable('按模型',Object.entries(r.models)),usageTable('每日用量',Object.entries(r.daily).sort(([a],[b])=>b.localeCompare(a))),
    ]:[usageState.loading||!usageState.error?skeleton():empty('还没有用量统计','使用中枢模型或开发工具插件后，调用会自动计入这里。')]),
    usageState.error?node('p',{role:'alert',class:'form-error'},usageState.error):null,
  ];
}
extraViews.usage=usageView;
