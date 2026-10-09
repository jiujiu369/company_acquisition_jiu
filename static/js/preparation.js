(() => {
  const n = (tag, text = '', cls = '') => {const e = document.createElement(tag); e.textContent = text; e.className = cls; return e;};
  const states = {obtained:'已取得',not_set:'明确未设置',not_applicable:'不适用',not_obtained:'未获取',failed:'获取失败'};
  const saved = {SAVED:'完整保存并校验',FAILED:'保存失败',INCOMPLETE:'未完整保存',PENDING:'待保存',SAVING:'正在保存'};
  async function req(url, body) {
    const r = await fetch(url, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    let d; try {d = await r.json();} catch {throw new Error('服务响应异常');}
    if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : '读取或保存失败'); return d;
  }
  function button(text, run) {const b = n('button',text,'button'); b.type='button'; b.addEventListener('click',async()=>{b.disabled=true;try{await run();}finally{b.disabled=false;}});return b;}
  const display = f => f?.status === 'obtained' ? (typeof f.value==='object'?JSON.stringify(f.value):String(f.value??''))+(f.unit?' '+f.unit:'') : states[f?.status]||'未获取';
  function status(row, host) {
    const r=row.result||row, box=n('div','','preparation-status');
    box.append(n('p','采集任务：'+((row.task?.status||row.status)==='SUCCESS'?'已完成':'请查看当前采集状态')),
      n('p','采集范围内必填资料：'+(r.completeness?.complete?'齐全':r.completeness?'缺失或待确认':'尚未检查')),
      n('p','商品资料包：'+(saved[r.package_status]||'历史任务尚无完整包记录')));
    if(r.collected_at)box.append(n('p',`${r.reused?'复用已有资料':'资料时间'}：${new Date(r.collected_at).toLocaleString('zh-CN')}`));
    if(r.package_path||r.save_target)box.append(n('p','实际保存路径：'+(r.package_path||r.save_target),'source'));
    if(r.package_error)box.append(n('p',r.package_error,'issue'));host.append(box);
  }
  function saveControls(row,host,refresh) {
    const r=row.result||row,tid=row.task?.task_id||row.task_id;if(!r.product_id&&!r.products?.some(p=>p.product_id))return;
    const msg=n('p','','feedback');msg.setAttribute('role','status');
    async function save(path) {msg.textContent='正在补存并校验资料包…';try{const d=await req(`/api/collection/jobs/${encodeURIComponent(tid)}/save-package`,path?{save_directory:path}:{});msg.textContent=d.status==='SAVED'?'资料包已完整保存。':`未完整保存：${(d.errors||[]).join('；')}`;if(refresh)await refresh();}catch(e){msg.textContent=e.message;}}
    const bar=n('div','','actions');bar.append(button('补存资料包',()=>save()),button('选择文件夹后补存',async()=>{msg.textContent='正在打开 Windows 文件夹选择窗口…';try{const d=await req('/api/collection/preparation/directory/select',{path:r.save_target||''});if(d.canceled){msg.textContent='已取消选择。';return;}if(d.writable===false){msg.textContent=`已选择：${d.path}。${d.error}；请重新选择文件夹。`;return;}await save(d.path);}catch(e){msg.textContent=e.message;}}));
    if(r.package_status==='SAVED'&&row.kind!=='shop'){const a=n('a','查看离线资料','button');a.href=`/api/collection/jobs/${encodeURIComponent(tid)}/package-file/README.html`;a.target='_blank';a.rel='noopener';bar.append(a);}host.append(bar,msg);
  }
  function initDirectory() {
    const input=document.getElementById('save-directory'),msg=document.getElementById('directory-status');if(!input)return;
    req('/api/collection/preparation/settings').then(d=>{if(!input.value)input.value=d.last_directory||'';}).catch(()=>{});
    document.getElementById('choose-directory').addEventListener('click',async e=>{const b=e.currentTarget;b.disabled=true;msg.textContent='正在打开 Windows 文件夹选择窗口，可选择已有文件夹或点击“新建文件夹”。';try{const d=await req('/api/collection/preparation/directory/select',{path:input.value});if(!d.canceled){input.value=d.path;msg.textContent=d.writable===false?`已选择：${d.path}。${d.error}；请重新选择文件夹。`:'已选择：'+d.path;}else msg.textContent='已取消选择，原路径保留。';}catch(err){msg.textContent=err.message;}finally{b.disabled=false;}});
    document.getElementById('check-directory').addEventListener('click',async()=>{try{const d=await req('/api/collection/preparation/directory/check',{path:input.value.trim()});input.value=d.path;msg.textContent='目录有效且可写：'+d.path;}catch(e){msg.textContent=e.message;input.focus();}});
  }
  async function submission() {const input=document.getElementById('save-directory');if(!input?.value.trim()){document.getElementById('choose-directory')?.focus();throw new Error('请点击“选择文件夹”选定保存位置。');}const d=await req('/api/collection/preparation/directory/check',{path:input.value.trim()});return {save_directory:d.path,collection_mode:document.getElementById('collection-mode').value,collect_images:document.getElementById('collection-content')?.value==='with_images'};}
  function render(p,host,refresh) {
    const d=p.preparation;if(!d)return;const focused=d.collection_profile==='sku_package',section=n('section','','detail-section preparation-panel');
    section.append(n('h3','整理后的商品资料'),n('p',focused?'Excel 按每个 SKU 一行导出，未取得的变量留空。包装数量和整包价格分别保存；总库存不从包装数量或SKU默认库存推算。':'这里展示后续读取的最终值。人工补充仅保存本地资料；原商品上架设置是历史来源。','muted'));
    const grid=n('dl','','detail-grid');
    for(const [key,f] of Object.entries(d.effective.fields)){if(focused&&(!['title','price','stock'].includes(key)||(key!=='title'&&f.status!=='obtained')))continue;const pair=n('div','','detail-pair');pair.append(n('dt',focused&&key==='title'?'商品名称':p.field_labels[key]||key),n('dd',display(f)+(f.needs_confirmation?' · 待确认':'')+(d.manual_overrides['fields.'+key]?' · 人工修改':'')));grid.append(pair);}
    const attributes=d.effective.attributes.filter(a=>!focused||['安装方式','导线材质','导体材质','型号','货号','导线截面积','导线截面','标称截面积','适用线缆类型','适用电缆类型'].includes(a.source_name));
    for(const a of attributes){const pair=n('div','','detail-pair');pair.append(n('dt',a.source_name),n('dd',display(a.field)+(a.field.needs_confirmation?' · 待确认':'')));grid.append(pair);}if(focused&&!attributes.length){const pair=n('div','','detail-pair');pair.append(n('dt','型号'),n('dd','未获取'));grid.append(pair);}section.append(grid);
    const missing=n('details');missing.open=!d.completeness.complete;missing.append(n('summary',d.completeness.complete?'采集范围内必填资料齐全':`缺失 ${d.completeness.missing.length} 项 · 待确认 ${d.completeness.pending.length} 项`));
    for(const i of [...d.completeness.missing,...d.completeness.pending])missing.append(n('p',`${i.label}：${states[i.status]||'待确认'}${i.reason?' · '+i.reason:''}`,'issue'));
    section.append(missing);if(!focused)section.append(n('p',d.reading_capabilities?.reason||'后台实际字段读取方式尚未核实。','muted'));
    if(d.effective.skus.length&&!focused){section.append(n('h3','SKU 最终值与原图关系'));for(const s of d.effective.skus){const card=n('article','','prepared-sku');card.append(n('h4',Object.values(s.combination).join(' / ')||`SKU ${s.order}`),n('p',`来源 SKU ID：${s.source_sku_id||'未获取'} · 实际价：${display(s.price)} ${display(s.currency)} · 实际库存：${display(s.stock)} ${display(s.stock_unit)}`));const a=p.images.find(a=>a.id===s.image_asset_id);if(a){const img=n('img');img.src=a.url;img.alt='此 SKU 对应的原图';img.loading='lazy';card.append(img);}else card.append(n('p','SKU 原图未取得','issue'));section.append(card);}}
    const editor=n('details','','manual-editor');editor.append(n('summary','人工补充或修正资料'));const form=n('form'),select=n('select');select.id='manual-field';
    const opt=(value,text)=>{const o=n('option',text);o.value=value;select.append(o);};
    for(const [key,label]of Object.entries(p.field_labels)){if(!focused||['title','price','stock'].includes(key))opt('fields.'+key,focused&&key==='title'?'商品名称':label);}
    for(const name of new Set(focused?['安装方式','导线材质','型号','导线截面积','适用线缆类型','货号']:[...(p.attribute_names||[]),...d.effective.attributes.map(a=>a.source_name)]))opt('attributes.'+name,'属性 · '+name);
    for(const s of d.effective.skus)for(const [key,label]of Object.entries(focused?{name:'SKU名称',package_quantity:'包装数量',package_unit:'包装单位',package_price:'每包价格',stock:'填写库存（默认9999）'}:{price:'实际价格',currency:'币种',stock:'实际库存',stock_unit:'库存单位',merchant_code:'商家编码',barcode:'条形码',sale_status:'可售状态'}))opt(`skus.${s.local_sku_id}.${key}`,`SKU ${s.name?.value||Object.values(s.combination).join(' / ')||s.order} · ${label}`);
    if(!focused)opt('custom','增加自定义商品属性');
    const value=n('textarea');value.rows=2;value.id='manual-value';const state=n('select');state.id='manual-state';for(const [k,v]of Object.entries(states)){const o=n('option',v);o.value=k;state.append(o);}
    const custom=n('input');custom.id='manual-custom-name';custom.type='text';const reason=n('input');reason.id='manual-reason';reason.type='text';const unit=n('input');unit.id='manual-unit';unit.type='text';const confirm=n('input');confirm.id='manual-confirm';confirm.type='checkbox';const msg=n('p','','feedback');msg.setAttribute('role','status');
    const label=(text,e)=>{const l=n('label',text);l.htmlFor=e.id;form.append(l,e);};label('选择字段',select);label('自定义属性名称（仅增加属性时填写）',custom);label('最终值（类目路径用 > 分隔）',value);label('字段状态',state);label('单位（如适用）',unit);label('依据或原图位置；不适用/未设置请说明原因',reason);label('识别结果仍需确认',confirm);
    const load=()=>{const a=select.value.split('.');let f=null;if(a[0]==='fields')f=d.effective.fields[a[1]];else if(a[0]==='attributes')f=d.effective.attributes.find(x=>x.source_name===a[1])?.field;else if(a[0]==='skus')f=d.effective.skus.find(s=>s.local_sku_id===a[1])?.[a[2]];value.value=f?.value==null?'':a[1]==='category_path'?f.value.join(' > '):typeof f.value==='object'?JSON.stringify(f.value):String(f.value);state.value=f?.status||'obtained';reason.value=f?.reason||'';unit.value=f?.unit||'';confirm.checked=!!f?.needs_confirmation;};select.addEventListener('change',load);load();
    const submit=n('button','保存人工补充并生成新资料版本','button primary');submit.type='submit';
    form.append(submit,button('取消此字段的人工覆盖',async()=>{try{await req(`/api/collection/products/${p.product_id}/manual`,{overrides:{[select.value]:null}});await refresh();}catch(e){msg.textContent=e.message;}}),msg);
    form.addEventListener('submit',async event=>{event.preventDefault();submit.disabled=true;try{const path=select.value==='custom'?'attributes.'+custom.value.trim():select.value,key=path.split('.').at(-1);let v=state.value==='obtained'?value.value.trim():null;if(v!==null&&['stock','package_quantity'].includes(key)){if(!/^\d+$/.test(v)||(key==='package_quantity'&&Number(v)<1))throw new Error('包装数量须为正整数，库存须为非负整数；未知请选“未获取”。');v=Number(v);}if(v!==null&&key==='category_path')v=v.split(/\s*[>＞]\s*/).filter(Boolean);if(v==='')throw new Error('已取得字段须填写实际值。');msg.textContent='正在保存人工修订和新资料包…';await req(`/api/collection/products/${p.product_id}/manual`,{overrides:{[path]:{value:v,status:state.value,unit:unit.value.trim()||null,reason:reason.value.trim()||null,needs_confirmation:confirm.checked}}});await refresh();}catch(e){msg.textContent=e.message;}finally{submit.disabled=false;}});
    editor.append(form);section.append(editor);
    const importer=n('details'),file=n('input'),note=n('p','','feedback');file.type='file';file.accept='.json,application/json';file.setAttribute('aria-label','选择本地已保存商品补充记录');
    importer.append(n('summary','补充本地已保存商品记录'),n('p','按来源商品和 SKU ID 关联的补充 JSON。此格式尚未核实为平台官方导出格式，导入值会标记待确认。'),file,note);
    importer.append(button('读取补充记录',async()=>{try{if(!file.files[0])throw new Error('请先选择补充记录文件。');if(file.files[0].size>2000000)throw new Error('记录文件过大，请仅保留当前商品资料。');const source=JSON.parse(await file.files[0].text());const result=await req(`/api/collection/products/${p.product_id}/saved-record`,{source});note.textContent=result.note;await refresh();}catch(e){note.textContent=e.message;}}));section.append(importer);
    const history=n('details');history.append(n('summary',`保存记录与实际路径（${p.package_history.length}）`));const tids=new Set();for(const h of p.package_history){history.append(n('p',`${saved[h.status]||h.status} · ${h.created_at} · ${h.path}`,'source'));if(h.error)history.append(n('p',h.error,'issue'));if(!tids.has(h.task_id)){tids.add(h.task_id);saveControls({task_id:h.task_id,product_id:p.product_id,package_path:h.path,package_status:h.status},history,refresh);}}section.append(history);
    const sources=n('details');sources.append(n('summary','来源、冲突与资料时间'));for(const s of d.sources)sources.append(n('p',`${s.method} · 商品 ID ${s.source_product_id} · ${s.observed_at} · ${s.url}`,'source'));for(const i of d.issues)sources.append(n('p',`${i.field||''} · ${i.reason||''}`,'issue'));section.append(sources);host.append(section);
    if(focused){const controls=n('section','','detail-section preparation-controls');controls.append(editor,importer,history,sources);return controls;}
  }
  window.PreparationUI={initDirectory,submission,render,status,saveControls};
})();
