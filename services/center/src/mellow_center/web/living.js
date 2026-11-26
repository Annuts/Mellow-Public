"use strict";
views.living='生活';
const livingKinds={event:'日历',reminder:'提醒',habit:'习惯',list:'清单',journal:'日记',activity:'运动与饮水',pantry:'食材库存',recipe:'菜谱',food_profile:'饮食与厨具',meal:'餐次',wardrobe:'衣橱',outfit:'搭配'};
let livingKind='all';
const shanghaiDay=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const beijingInput=iso=>new Date(new Date(iso).getTime()+8*3600000).toISOString().slice(0,16);
const beijingISO=value=>{if(!value)throw new Error('请填写完整时间。');return new Date(value+':00+08:00').toISOString();};
function livingDefaults(kind) {
  const today=shanghaiDay(),now=new Date().toISOString();
  return {
    event:{text:'',start:now,end:new Date(Date.now()+3600000).toISOString(),all_day:false,location:'',status:'confirmed'},
    reminder:{text:'',due_at:new Date(Date.now()+3600000).toISOString(),status:'pending'},
    habit:{text:'',weekdays:[1,2,3,4,5,6,7],status:'active'},
    list:{text:'',purpose:'todo',items:[]},journal:{text:'',date:today,mood:'unset',tags:[]},
    activity:{text:'',date:today,category:'exercise',amount:30},
    pantry:{text:'',quantity:1,unit:'个',location:'厨房',certainty:'approximate'},
    wardrobe:{text:'',category:'top',color:'',seasons:['spring','summer','autumn','winter'],status:'ready'},
    food_profile:{text:'饮食偏好',excluded:[],equipment:[],constraints_confirmed:false},
    recipe:{text:'',servings:1,minutes:20,ingredients:[],steps:[],equipment:[],verified:false},
    meal:{text:'',recipe_id:'',recipe_revision:1,servings:1,date:today,status:'planned'},
    outfit:{text:'',date:today,items:[],sources:[]},
  }[kind];
}
function pantryBatchData(batches,unit,today=shanghaiDay()) {
  const units={g:['mass',1],kg:['mass',1000],ml:['volume',1],l:['volume',1000],'个':['count',1],'份':['serving',1],'包':['pack',1]};
  if(!Array.isArray(batches)||batches.length>50||!units[unit])throw new Error('库存最多保存 50 个批次。');
  let quantity=0;const ids=new Set(),dates=[];
  for(const b of batches){
    if(typeof b.id!=='string'||!b.id||b.id.length>128||ids.has(b.id))throw new Error('批次编号无效或重复。');ids.add(b.id);
    if(typeof b.quantity!=='number'||!Number.isFinite(b.quantity)||b.quantity<0||b.quantity>1000000)throw new Error('请填写有效的批次数量。');
    if(!units[b.unit]||units[b.unit][0]!==units[unit][0])throw new Error('批次单位需要与库存总量兼容。');
    quantity+=b.quantity*units[b.unit][1]/units[unit][1];
    for(const key of ['purchased_on','expires_on'])if(key in b){
      if(typeof b[key]!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(b[key])||!Number.isFinite(Date.parse(b[key]))||new Date(b[key]).toISOString().slice(0,10)!==b[key])throw new Error('请填写有效的购买或到期日期。');
    }
    for(const [key,max] of [['source',200],['note',2000],['photo_ref',256]])if(key in b&&(typeof b[key]!=='string'||!b[key]||b[key].length>max))throw new Error('批次来源、备注或照片引用过长。');
    if('price' in b&&(typeof b.price!=='number'||!Number.isFinite(b.price)||b.price<0||b.price>1000000))throw new Error('请填写有效的批次价格。');
    if(b.quantity>0&&b.expires_on>=today)dates.push(b.expires_on);
  }
  if(quantity>1000000)throw new Error('库存总量超出上限。');
  dates.sort();return {batches,quantity,...(dates.length?{expires_on:dates[0]}:{})};
}
function pantryBatchEditor(data,quantityControl,unitControl={value:data.unit},expiryControl={value:data.expires_on||''}) {
  const rows=[],box=node('div',{}),list=node('div',{});
  let enabled=Array.isArray(data.batches);
  const add=(batch)=>{
    if(rows.length>=50){notice('最多 50 个批次。');return;}
    const controls={},panel=node('fieldset',{class:'choice-group'},node('legend',{},'采购批次'));
    for(const [key,label,type] of [['quantity','剩余数量','number'],['unit','单位','select'],['purchased_on','购买日期（选填）','date'],['expires_on','包装到期日（选填）','date'],['source','商店或渠道（选填）','text'],['price','价格（选填）','number'],['note','备注（选填）','text'],['photo_ref','包装照片引用（选填）','text']]){
      const e=type==='select'?select(Object.fromEntries(['g','kg','ml','l','个','份','包'].map(x=>[x,x])),batch[key]||unitControl.value,()=>{}):input(batch[key]??'',{type,required:key==='quantity',...(type==='number'?{min:0,max:1000000,step:'any'}:{}),...(type==='text'?{maxLength:key==='note'?2000:key==='photo_ref'?256:200}:{})});
      controls[key]=e;panel.append(field(label,e));
    }
    const row={batch,controls,panel};rows.push(row);
    panel.append(button('移除此批次',()=>{rows.splice(rows.indexOf(row),1);panel.remove();},'danger'));list.append(panel);
  };
  (data.batches||[]).forEach(b=>add(structuredClone(b)));
  const lockSummary=()=>{quantityControl.disabled=enabled;expiryControl.disabled=enabled;};lockSummary();
  box.append(node('h3',{},'采购批次'),node('p',{class:'muted'},'分别记录每次购买。总量按批次汇总，做饭优先扣减最早到期批次；已过期批次不会自动使用。'),list,button('添加采购批次',()=>{
    if(!enabled){enabled=true;lockSummary();add({id:crypto.randomUUID(),quantity:Number(quantityControl.value),unit:unitControl.value,...(expiryControl.value?{expires_on:expiryControl.value}:{})});}
    else add({id:crypto.randomUUID(),quantity:0,unit:unitControl.value});
  }));
  return {box,lockSummary,read:()=>enabled?rows.map(({batch,controls})=>{
    const next={...batch};for(const [key,e] of Object.entries(controls)){
      if(e.value==='')delete next[key];else next[key]=['quantity','price'].includes(key)?Number(e.value):e.value.trim();
    }return next;
  }):null};
}
function livingForm(kind, original=null) {
  let space=ownerSpace(original?.space||state.space);
  const data=structuredClone(original?.data||livingDefaults(kind));
  const controls={},content=[];
  const control=(key,label,type='text',options=null)=>{
    let e;
    if(options)e=select(options,data[key]??Object.keys(options)[0],()=>{});
    else if(type==='textarea')e=node('textarea',{value:data[key]||'',rows:key==='text'?3:5,maxLength:16000});
    else if(type==='checkbox')e=input('',{type:'checkbox',checked:data[key]===true});
    else e=input(type==='datetime-local'?beijingInput(data[key]):data[key]??'',{type,required:true,...(type==='number'?{min:0,max:1000000,step:'any'}:{})});
    controls[key]={e,type,options};
    content.push(type==='checkbox'?node('label',{class:'checkline'},e,label):field(label,e));return e;
  };
  const lines=(key,label)=>{const e=node('textarea',{value:(data[key]||[]).join('\n'),rows:3,maxLength:16000});controls[key]={e,type:'lines'};content.push(field(label+'（每行一项）',e));return e;};
  const choices=(key,label,options)=>{
    const box=node('fieldset',{class:'choice-group'},node('legend',{},label));
    const list=node('div',{class:'admin-actions'});
    const update=(options,selected=[])=>{
      list.replaceChildren();
      const entries=Object.entries(options).map(([v,n])=>{
        const e=input('',{type:'checkbox',checked:selected.includes(v)||selected.includes(Number(v))});
        list.append(node('label',{class:'checkline'},e,n));
        return [v,e];
      });
      controls[key]={type:'choices',entries};
    };
    box.append(list);content.push(box);update(options,data[key]);return update;
  };
  control('text',kind==='journal'?'今天的记录':'名称与内容',kind==='journal'?'textarea':'text');
  if(['journal','activity','meal','outfit'].includes(kind))control('date','日期','date');
  if(kind==='event'){control('start','开始（北京时间）','datetime-local');control('end','结束（北京时间）','datetime-local');control('departure_at','出发（北京时间，选填）','datetime-local');controls.departure_at.e.required=false;control('all_day','全天','checkbox');control('location','地点');controls.location.e.required=false;control('status','状态','text',{confirmed:'已确认',tentative:'待确认',cancelled:'已取消'});}
  if(kind==='reminder'){control('due_at','提醒时间（北京时间）','datetime-local');control('status','状态','text',{pending:'待提醒',done:'已完成',dismissed:'已取消'});}
  if(kind==='habit'){choices('weekdays','安排星期',{'1':'一','2':'二','3':'三','4':'四','5':'五','6':'六','7':'日'});control('status','状态','text',{active:'进行中',paused:'暂停',done:'完成'});}
  if(kind==='journal'){control('mood','心情','text',{unset:'未记录',great:'很好',good:'不错',okay:'一般',tired:'有点累',low:'不太好'});lines('tags','标签');}
  if(kind==='activity'){control('category','类型','text',{exercise:'运动（分钟）',water:'饮水（毫升）'});control('amount','数量','number');}
  if(kind==='list'){control('purpose','用途','text',{todo:'待办',shopping:'购物',packing:'打包',meal:'备餐'});const e=node('textarea',{value:data.items.map(i=>(i.done?'[x] ':'')+i.text).join('\n'),rows:8,maxLength:16000});controls.items={e,type:'items'};content.push(field('清单项目（每行一项，[x] 表示完成）',e));}
  if(kind==='pantry'){control('quantity','数量','number');control('unit','单位','text',Object.fromEntries(['g','kg','ml','l','个','份','包'].map(x=>[x,x])));control('location','存放位置');control('certainty','数量核对','text',{approximate:'大致数量',unknown:'尚不确定',confirmed:'我已核对当前数量'});const expiry=input(data.expires_on||'',{type:'date'});controls.expires_on={e:expiry,type:'optional'};content.push(field('到期日（包装上的日期，可不填）',expiry));const e=input(data.check_on||'',{type:'date'});controls.check_on={e,type:'optional'};content.push(field('下次检查日期',e));content.push(node('p',{class:'muted'},'到期日由你填写，系统不会自行推断保质期。选择“我已核对当前数量”并保存，会更新今天的核对时间。'));}
  const batchEditor=kind==='pantry'?pantryBatchEditor(data,controls.quantity.e,controls.unit.e,controls.expires_on.e):null;
  if(batchEditor)content.push(node('p',{class:'muted'},'启用批次后，总量和到期日由批次自动汇总，请在批次内修改。'),batchEditor.box);
  if(kind==='wardrobe'){control('category','类别','text',{top:'上装',bottom:'下装',outer:'外套',shoes:'鞋',accessory:'配件'});control('color','颜色');control('status','状态','text',{ready:'可穿',washing:'洗护中',stored:'收纳中'});choices('seasons','季节',{spring:'春',summer:'夏',autumn:'秋',winter:'冬'});}
  if(kind==='food_profile'){lines('excluded','不吃或过敏食材');lines('equipment','已有厨具');control('constraints_confirmed','我已核对上述饮食限制与厨具','checkbox');}
  if(kind==='recipe'){control('servings','人数','number');control('minutes','分钟','number');const e=node('textarea',{value:data.ingredients.map(i=>`${i.name} | ${i.quantity} | ${i.unit}`).join('\n'),rows:5});controls.ingredients={e,type:'ingredients'};content.push(field('配料（每行：名称 | 数量 | 单位，如 鸡蛋 | 2 | 个）',e));lines('steps','步骤');lines('equipment','所需厨具');control('verified','我已核对配料、单位和步骤','checkbox');control('feedback','偏好','text',{none:'未反馈',liked:'喜欢',too_slow:'太费时',avoid:'不再推荐'});}
  const dependencies=kind=>Object.fromEntries(planningRows(kind).filter(r=>sameSpace(r.space,space)).map(r=>[r.id,r.data.text]));
  let changeDependencies=()=>{};
  if(kind==='meal'){
    const recipe=control('recipe_id','菜谱','text',{'':'请选择菜谱',...dependencies('recipe')});
    changeDependencies=()=>recipe.replaceChildren(node('option',{value:''},'请选择菜谱'),
      ...Object.entries(dependencies('recipe')).map(([id,label])=>node('option',{value:id},label)));
    control('servings','人数','number');control('status','状态','text',{planned:'计划中',cancelled:'取消'});
  }
  if(kind==='outfit'){
    const update=choices('items','选择衣物',dependencies('wardrobe'));
    changeDependencies=()=>update(dependencies('wardrobe'));
  }
  const spaces=writableSpaces(kind);
  if(!original && Object.keys(spaces).length>1)content.push(field('保存空间',select(spaces,space,value=>{
    space=value;
    changeDependencies();
  })));
  const dialog=$('editor'),generation=++editorGeneration,current=()=>generation===editorGeneration&&dialog.open;
  const error=node('p',{class:'form-error',role:'alert'}),save=node('button',{type:'submit',class:'primary'},'保存');
  const close=button('取消',()=>dialog.close(),'quiet');let command;
  const form=node('form',{},...content,error,rowActions(close,save));
  form.addEventListener('submit',async event=>{
    event.preventDefault();error.textContent='';
    try {
      if(!command){
        const next={...data};
        for(const [key,c] of Object.entries(controls)){
          if(c.type==='checkbox')next[key]=c.e.checked;
          else if(c.type==='choices')next[key]=c.entries.filter(([,e])=>e.checked).map(([v])=>key==='weekdays'?Number(v):v);
          else if(key==='departure_at' && !c.e.value)delete next[key];
          else if(c.type==='datetime-local')next[key]=beijingISO(c.e.value);
          else if(c.type==='number')next[key]=Number(c.e.value);
          else if(c.type==='optional'){const value=c.e.value.trim();if(value)next[key]=value;else delete next[key];}
          else if(c.type==='lines')next[key]=c.e.value.split('\n').map(s=>s.trim()).filter(Boolean);
          else if(c.type==='items')next[key]=c.e.value.split('\n').map(s=>s.trim()).filter(Boolean).map((s,i)=>({id:data.items[i]?.id||crypto.randomUUID(),text:s.replace(/^\[x\]\s*/i,''),done:/^\[x\]/i.test(s)}));
          else if(c.type==='ingredients')next[key]=c.e.value.split('\n').filter(s=>s.trim()).map(s=>{const [name,quantity,unit,...extra]=s.split('|').map(x=>x.trim());if(!name||!unit||extra.length||!Number.isFinite(Number(quantity)))throw new Error('请按“名称 | 数量 | 单位”填写配料。');return {name,quantity:Number(quantity),unit};});
          else next[key]=c.e.value.trim();
        }
        if(kind==='pantry'&&next.certainty==='confirmed')next.confirmed_at=new Date().toISOString();
        if(batchEditor){const batches=batchEditor.read();if(batches!==null){delete next.expires_on;Object.assign(next,pantryBatchData(batches,next.unit));}}
        if(kind==='meal'){const recipe=state.records.find(r=>r.id===next.recipe_id);if(!recipe)throw new Error('请选择有效菜谱。');next.recipe_revision=recipe.revision;delete next.recipe_signature;delete next.sources;}
        command={operation_id:crypto.randomUUID(),id:original?.id||crypto.randomUUID(),base_revision:original?.revision||0,action:'put',record:{kind,space,mode:'normal',data:next}};
      }
      save.disabled=true;close.disabled=true;form.querySelectorAll('input,select,textarea').forEach(e=>e.disabled=true);
      if(batchEditor)batchEditor.box.querySelectorAll('button').forEach(e=>e.disabled=true);
      await write(null,'put',command);if(current())dialog.close();await refresh();toast('已保存，获准设备下次同步时可见。');
    }catch(e){if(!current())return;error.textContent=e.message;if(e.status&&e.status<500){command=null;form.querySelectorAll('input,select,textarea').forEach(e=>e.disabled=false);}else if(!command){form.querySelectorAll('input,select,textarea').forEach(e=>e.disabled=false);}else{error.textContent+=' 将按原编号重试。';save.textContent='重试同一次保存';}}
    finally{save.disabled=false;close.disabled=false;if(batchEditor&&!command){batchEditor.box.querySelectorAll('button').forEach(e=>e.disabled=false);batchEditor.lockSummary();save.textContent='保存';}}
  });
  $('editor-content').replaceChildren(node('h2',{},`${original?'编辑':'新增'}${livingKinds[kind]}`),form);if(!dialog.open)dialog.showModal();
}
async function habitCheckin(record) {
  const day=shanghaiDay();
  // Use noon UTC to obtain the civil date's weekday without crossing midnight.
  const civilWeekday=new Date(day+'T12:00:00Z').getUTCDay()||7;
  if(!record.data.weekdays.includes(civilWeekday)||record.data.status!=='active'){notice('这个习惯今天未安排或已暂停。');return;}
  const raw=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(`${record.id}:${day}`));
  const id='habit-log-'+Array.from(new Uint8Array(raw),n=>n.toString(16).padStart(2,'0')).join('');
  const old=state.records.find(r=>r.id===id);
  try{await write({id,kind:'habit_log',space:ownerSpace(record.space),revision:old?.revision||0,data:{text:record.data.text,habit_id:record.id,date:day,done:!old?.data.done}});await refresh();}catch(e){notice(e.message);}
}
async function cookMeal(record) {
  try {
    const preview=await api('/kitchen/preview',{method:'POST',body:{meal_id:record.id}});
    const command={operation_id:crypto.randomUUID(),action:'cook',meal_id:record.id,confirmation_hash:preview.confirmation_hash};
    confirmAction('确认已经做完这餐？','会按已核对库存原子扣减；数量不足或资料变化会阻止保存。',async()=>{
      await api('/kitchen/actions',{method:'POST',body:command});await refresh();
    });
  }catch(e){notice(e.message);}
}
async function kitchenExtra(record, action) {
  try {
    let command;
    if(action==='undo') {
      const log=state.records.find(r=>r.kind==='consumption'&&r.data.meal_id===record.id&&r.data.status==='applied');
      if(!log)throw new Error('没有可撤销的扣减记录。');
      command={operation_id:crypto.randomUUID(),action:'undo',consumption_id:log.id};
    } else {
      const preview=await api('/kitchen/preview',{method:'POST',body:{meal_id:record.id}});
      command={operation_id:crypto.randomUUID(),action:'shopping',meal_id:record.id,confirmation_hash:preview.confirmation_hash};
    }
    confirmAction(action==='undo'?'撤销这次库存扣减？':'建立缺少食材的采购清单？',
      action==='undo'?'只有库存没有后续变化时才能撤销；不会覆盖后来盘点的数量。':'只在中枢创建清单，并同步到获准设备。',async()=>{
        await api('/kitchen/actions',{method:'POST',body:command});await refresh();
      });
  }catch(e){notice(e.message);}
}
function livingDetails(r) {
  const d=r.data;
  if(r.kind==='event')return `${stamp(d.start)} — ${stamp(d.end)} · ${d.location||'未填地点'}`;
  if(r.kind==='habit')return `${d.weekdays.length===7?'每天':'每周'+d.weekdays.map(n=>'一二三四五六日'[n-1]).join('、')} · ${{active:'进行中',paused:'暂停',done:'完成'}[d.status]}`;
  if(r.kind==='pantry')return `${d.quantity} ${d.unit} · ${d.location} · ${{confirmed:'已核对',approximate:'大致数量',unknown:'待核对'}[d.certainty]} · 到期日：${d.expires_on||'未提供'}`;
  if(r.kind==='recipe')return `${d.servings} 人份 · ${d.minutes} 分钟 · ${d.ingredients.map(i=>`${i.name} ${i.quantity}${i.unit}`).join('、')}`;
  if(r.kind==='meal')return `${state.records.find(r=>r.id===d.recipe_id)?.data.text||'原菜谱已不可用'} · ${d.servings} 人份 · ${{planned:'计划中',cooked:'已做完',cancelled:'已取消'}[d.status]}`;
  if(r.kind==='food_profile')return `不吃或过敏：${d.excluded.join('、')||'未填写'}；厨具：${d.equipment.join('、')||'未填写'}`;
  if(r.kind==='wardrobe')return `${d.color} · ${{ready:'可穿',washing:'洗护中',stored:'收纳中'}[d.status]}`;
  if(r.kind==='outfit')return d.items.map(id=>state.records.find(r=>r.id===id)?.data.text||'衣物已删除').join('、');
  return '';
}
function pantryExpiry(data, today=shanghaiDay()) {
  if(!data.expires_on)return null;
  // Date-only values use Shanghai civil days, not the browser's timezone or DST.
  const days=(Date.parse(data.expires_on+'T00:00:00Z')-Date.parse(today+'T00:00:00Z'))/86400000;
  if(days<0)return badge('已过期','red');
  if(days<=7)return badge(days===0?'今天到期':`${days} 天内到期`,'red');
  return null;
}
function chooseLivingRecord() {
  const dialog=$('editor');
  editorGeneration++;
  $('editor-content').replaceChildren(
    node('div',{class:'dialog-head'},node('h2',{},'新增生活记录'),button('取消',()=>dialog.close(),'quiet')),
    node('div',{class:'record-types'},Object.entries(livingKinds).map(([kind,label])=>button(label,()=>livingForm(kind)))),
  );
  dialog.showModal();
}
function livingRecord(r) {
  const d=r.data;
  const meta=[spaceLabel(r.space),d.date,d.due_at?stamp(d.due_at):null,
    d.amount!==undefined?d.amount+(d.category==='water'?' 毫升':' 分钟'):null].filter(Boolean);
  return node('details',{class:'living-record','data-key':livingKind+'-'+r.id,open:livingKind!=='all' && ['habit','meal'].includes(r.kind)},
    node('summary',{},node('div',{class:'row-main'},
      node('strong',{},r.kind==='journal'?d.text.slice(0,90):d.text),
      r.kind==='pantry'?pantryExpiry(d):null,
      node('small',{},[...meta,livingDetails(r)].filter(Boolean).join(' · ')))),
    r.kind==='journal'?node('p',{class:'body-copy'},d.text):null,
    r.kind==='pantry'&&Array.isArray(d.batches)?node('div',{},node('h3',{},'采购批次'),d.batches.length?d.batches.map(b=>node('p',{},
      `${b.quantity} ${b.unit} · 购买：${b.purchased_on||'未记录'} · 到期：${b.expires_on||'未提供'} · 来源：${b.source||'未记录'}${b.price!==undefined?' · 价格：'+b.price:''}${b.note?' · '+b.note:''}${b.photo_ref?' · 包装照片：'+b.photo_ref:''}`)):node('p',{class:'muted'},'暂无批次')):null,
    r.kind==='list'?d.items.map(item=>node('label',{class:'checkline'},input('',{type:'checkbox',checked:item.done,onchange:async e=>{
      e.target.disabled=true;
      try{
        await write({...r,data:{...d,items:d.items.map(i=>i.id===item.id?{...i,done:e.target.checked}:i)}});
        await refresh();
      }catch(err){e.target.checked=item.done;notice(err.message);}
      finally{e.target.disabled=false;}
    }}),item.text)):null,
    rowActions(d.status==='cooked'?null:button('编辑',()=>livingForm(r.kind,r)),
      r.kind==='habit'?button('今日打卡 / 撤销',()=>habitCheckin(r)):null,
      r.kind==='meal'&&d.status==='planned'?button('做完并扣减库存',()=>cookMeal(r)):null,
      r.kind==='meal'&&d.status==='planned'?button('生成采购清单',()=>kitchenExtra(r,'shopping')):null,
      r.kind==='meal'&&d.status==='cooked'?button('撤销这次扣减',()=>kitchenExtra(r,'undo')):null,
      button('删除',()=>r.kind==='pantry'?deletePantryRecord(r):confirmAction('删除这条生活记录？',d.text,async()=>{await write(r,'delete');await refresh();}),'danger')));
}
function deletePantryRecord(r) {
  const command={operation_id:crypto.randomUUID(),action:'delete',id:r.id,base_revision:r.revision};
  return confirmAction('删除这条库存？',r.data.text,async()=>{await write(null,'delete',command);await refresh();});
}
function livingView() {
  const options={all:'全部类别',...livingKinds};
  const query=state.search.trim().toLowerCase();
  const records=planningRows().filter(r=>livingKinds[r.kind] && (!query || JSON.stringify(r.data).toLowerCase().includes(query)));
  const groups=Object.entries(livingKinds).filter(([kind])=>livingKind==='all'||livingKind===kind)
    .map(([kind,label])=>[label,records.filter(r=>r.kind===kind).sort((a,b)=>b.updated_at.localeCompare(a.updated_at))])
    .filter(([,rows])=>rows.length);
  const sections=groups.map(([label,rows])=>section(label,badge(rows.length+' 条'),livingKind==='all'&&rows.length>5
    ? [rows.slice(0,5).map(livingRecord),disclosure('更多'+label+'（'+(rows.length-5)+'）','more-living-'+rows[0].kind,rows.slice(5).map(livingRecord))]
    : rows.map(livingRecord)));
  return [
    toolbar([select(options,livingKind,value=>{livingKind=value;render();},{'aria-label':'生活记录类别'}),
      button(livingKind==='all'?'新增记录':'新增'+livingKinds[livingKind],
        ()=>livingKind==='all'?chooseLivingRecord():livingForm(livingKind),'primary')]),
    livingKind==='all'&&sections.length>1?node('div',{class:'living-categories'},sections):sections,
    groups.length?null:empty(state.search?'没有匹配的生活记录':livingKind==='all'?'中枢还没有生活记录':'还没有'+options[livingKind],
      state.search?'可以试试其他关键词或类别。':'可以在这里新增，也可以在手机选择同步已有记录。'),
  ];
}
