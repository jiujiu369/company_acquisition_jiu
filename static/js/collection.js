(() => {
  const $ = id => document.getElementById(id);
  const page = document.body.dataset.page;
  const limit = 20;
  const labels = {PENDING: "待采集", RUNNING: "采集中", SUCCESS: "已完成", PARTIAL: "部分失败", NEEDS_MANUAL: "等待人工处理", FAILED: "失败", CANCELED: "已暂停"};
  const statusIcons = {PENDING: "clock", RUNNING: "refresh", SUCCESS: "check", PARTIAL: "alert", NEEDS_MANUAL: "alert", FAILED: "alert", CANCELED: "pause"};
  let jobOffset = 0, productOffset = 0, jobTotal = 0, productTotal = 0, jobs = [], jobLoading = false, jobPending = false, jobPendingForce = false;
  let productLoading = false, productPending = false, activeModalTrigger = null, skuPage = 0, imageShown = 24;
  const el = (tag, text = "", className = "") => {const n = document.createElement(tag); n.textContent = text; n.className = className; return n;};
  const icon = name => {const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.classList.add("icon"); svg.setAttribute("aria-hidden", "true"); const use = document.createElementNS("http://www.w3.org/2000/svg", "use"); use.setAttribute("href", `#i-${name}`); svg.append(use); return svg;};
  const feedback = (id, text, type = "") => {const n = $(id); if (n) {n.textContent = text; n.className = `feedback ${type}`;}};
  const productName = p => p.title || `商品 ${(p.source_url || "").match(/[?&]id=(\d+)/)?.[1] || p.source_product_id || ""}（名称待获取）`;
  const stageAge = value => {const d = Date.parse(value || ""); if (!Number.isFinite(d)) return ""; const sec = Math.max(0, Math.floor((Date.now() - d) / 1000)); return ` · 最近更新 ${sec < 60 ? `${sec} 秒前` : `${Math.floor(sec / 60)} 分钟前`}`;};
  const shopStage = result => {const products = result.products || []; if (!products.length) return `${result.stage || "等待发现店铺商品"}${stageAge(result.stage_at)}`; const active = products.findIndex(p => p.status === "RUNNING"); const i = active >= 0 ? active : products.findIndex(p => p.status === "PENDING"); if (i < 0) return `${result.stage || "正在汇总店铺结果"}${stageAge(result.stage_at)}`; const p = products[i]; return `第 ${i + 1}/${products.length} 件：${productName(p)} · ${p.status === "RUNNING" ? (p.stage || "正在获取商品页面") + stageAge(p.stage_at) : "等待轮到此商品"}`;};
  const qs = values => new URLSearchParams(values).toString();
  async function api(url, options) {const r = await fetch(url, options); let data; try {data = await r.json();} catch {throw new Error("服务响应异常，请稍后重试");} if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求失败，请稍后重试"); return data;}
  const post = (url, body) => api(url, {method: "POST", ...(body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})});
  const button = (label, action, iconName, className = "") => {const b = el("button", "", `button ${className}`); b.type = "button"; if (iconName) b.append(icon(iconName)); b.append(document.createTextNode(label)); b.addEventListener("click", async () => {b.disabled = true; try {await action(b);} catch (error) {feedback(page === "progress" ? "jobs-feedback" : "products-feedback", error.message, "error");} finally {b.disabled = false;}}); return b;};
  const link = (label, href, iconName, external = false) => {const a = el("a", "", "button"); a.href = href; if (iconName) a.append(icon(iconName)); a.append(document.createTextNode(label)); if (external) {a.target = "_blank"; a.rel = "noopener noreferrer";} return a;};
  function badge(status) {const b = el("span", "", `badge ${status}`); b.append(icon(statusIcons[status] || "clock"), document.createTextNode(labels[status] || status)); return b;}
  async function health() {try {const h = await api("/health"); $("system-status").textContent = h.worker === "running" ? "采集服务运行中" : "采集服务已连接";} catch {$("system-status").textContent = "服务暂不可用";}}

  function initCollection() {
    window.PreparationUI?.initDirectory();
    const input = $("source-urls"), submit = $("submit-collection");
    const updateCount = () => {const count = input.value.split(/\r?\n/).filter(v => v.trim()).length; $("url-count").textContent = `${count} / 50`; $("url-count").classList.toggle("error", count > 50);};
    const clearError = () => {$("url-error").hidden = true; input.removeAttribute("aria-invalid");};
    input.addEventListener("input", () => {updateCount(); clearError();});
    $("collection-form").addEventListener("submit", async event => {
      event.preventDefault(); clearError(); const urls = input.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
      let problem = "";
      if (!urls.length) problem = "请至少输入一个商品或店铺链接。";
      else if (urls.length > 50) problem = "每次最多提交 50 个链接，请分批采集。";
      else if (urls.some(s => {try {return !["http:", "https:"].includes(new URL(s).protocol);} catch {return true;}})) problem = "链接格式无效。请每行输入一个完整的 http 或 https 地址。";
      if (problem) {$("url-error").textContent = problem; $("url-error").hidden = false; input.setAttribute("aria-invalid", "true"); input.focus(); return;}
      submit.disabled = true; submit.querySelector("span").textContent = "正在提交…"; feedback("collection-message", "正在创建任务…");
      try {const preparation = await window.PreparationUI.submission(); const result = await post("/api/collection/jobs/batch", {urls, ...preparation}); feedback("collection-message", `已创建 ${result.count} 个任务。可在“采集进度”查看处理情况。`, "success"); input.value = ""; updateCount();}
      catch (error) {feedback("collection-message", `提交失败：${error.message}`, "error");}
      finally {submit.disabled = false; submit.querySelector("span").textContent = "开始采集";}
    });
    const setNetwork = (state, message) => {const n = $("network-state"); n.className = `connection-state ${state}`; n.replaceChildren(icon(({success: "check", failure: "alert", testing: "refresh"})[state] || "clock"), document.createTextNode(({success: "连接成功", failure: "连接失败", testing: "测试中…"})[state] || "未测试")); $("network-status").textContent = message;};
    api("/api/collection/network").then(s => {if (!s.enabled) {setNetwork("idle", "ScraperAPI 未启用；当前使用浏览器采集页面。"); $("test-network").disabled = true;} else if (!s.configured) {setNetwork("idle", "服务器尚未配置 ScraperAPI 密钥。"); $("test-network").disabled = true;} else setNetwork("idle", "已配置。点击测试连接以检查当前是否可用。");}).catch(() => setNetwork("failure", "无法读取服务配置，请刷新页面后重试。"));
    $("test-network").addEventListener("click", async () => {const b = $("test-network"); b.disabled = true; b.querySelector("span").textContent = "测试中…"; setNetwork("testing", "正在通过服务器检查连接，可能需要一些时间。"); try {const result = await post("/api/collection/network/test"); if (result.success) setNetwork("success", `连接正常 · 用时 ${(result.duration_ms / 1000).toFixed(1)} 秒。`); else setNetwork("failure", result.error || "连接未成功，请检查服务器配置或稍后重试。");} catch (error) {setNetwork("failure", `测试失败：${error.message}`);} finally {b.disabled = false; b.querySelector("span").textContent = "重新测试";}});
  }

  function jobControls(row, root = true) {
    const actions = el("div", "", "actions"), task = row.task || row, result = row.result || row, id = encodeURIComponent(task.task_id), status = task.status;
    const run = (label, op, glyph) => button(label, async () => {await post(`/api/collection/jobs/${id}/${op}`); feedback("jobs-feedback", `${label}操作已提交。`, "success"); await refreshJobs(true);}, glyph);
    if (["PENDING", "RUNNING", "NEEDS_MANUAL"].includes(status)) actions.append(run("暂停", "pause", "pause"));
    if (["CANCELED", "NEEDS_MANUAL"].includes(status)) actions.append(run(status === "NEEDS_MANUAL" ? "人工完成后继续" : "继续", "resume", "play"));
    if (["FAILED", "PARTIAL"].includes(status)) actions.append(run(root && row.kind === "shop" ? "重试失败商品" : "重试未完成部分", "retry", "refresh"));
    if (result.product_id) actions.append(
      link("导出 Excel", `/api/collection/jobs/${id}/export.xlsx`, "download"),
      button("查看结果", eventButton => showProduct(result.product_id, eventButton), "eye"),
      link("完整资料 ZIP", `/api/collection/jobs/${id}/export.zip`, "download"),
      link("资料 JSON（不含素材）", `/api/collection/jobs/${id}/product.json`),
      link("商品 CSV", `/api/collection/jobs/${id}/product.csv`)
    );
    else if (root && row.kind === "shop" && result.products?.some(p => p.product_id)) actions.append(link("店铺 Excel", `/api/collection/jobs/${id}/export.xlsx`, "download"), link("店铺结果 ZIP", `/api/collection/jobs/${id}/export.zip`, "download"));
    if (root) actions.append(button("删除任务", async () => {if (!confirm("删除该采集任务？未完成的采集会停止，已保存的商品资料和原图保留。")) return; await api(`/api/collection/jobs/${id}`, {method: "DELETE"}); feedback("jobs-feedback", "任务已删除，商品资料和原图保留。", "success"); await refreshJobs(true);}, "trash", "danger"));
    return actions;
  }
  function apiProgress(row, host) {
    const r = row.result || row, task = row.task || row;
    if (row.kind === "shop") {
      const count = value => Number.isInteger(value) ? `${value} 次` : "历史未记录";
      host.append(el("p", `ScraperAPI：商品请求 ${count(r.product_scraperapi_requests)} · 店铺列表请求 ${count(r.scraperapi_requests)}`, "muted"));
      if (r.discovery_complete === false) host.append(el("p", "链接发现尚未结束；进度按已发现商品计算。", "muted"));
    } else if (r.reused) host.append(el("p", "复用已有资料；未重新请求商品页面。", "muted"));
    else {
      const count = Number.isInteger(r.scraperapi_requests) ? `${r.scraperapi_requests} 次` : task.status === "PENDING" ? "等待请求" : "历史未记录";
      const mode = r.access_method === "scraperapi" ? " · 页面来源：ScraperAPI" : r.access_method === "direct" ? " · 页面来源：保留的浏览器" : "";
      host.append(el("p", `ScraperAPI：${count}${r.scraperapi_render ? "（渲染请求）" : ""}${mode}`, "muted"));
      if (r.browser_fallback_reason) host.append(el("p", `浏览器补充原因：${r.browser_fallback_reason}`, "muted"));
    }
  }
  function renderJobs() {
    const host = $("recent-collection"), opened = new Set([...host.querySelectorAll("details[open]")].map(n => n.dataset.key)), fragment = document.createDocumentFragment();
    if (!jobs.length) fragment.append(el("div", $("job-search").value.trim() || $("job-status").value !== "all" ? "没有符合条件的任务，请调整筛选条件。" : "暂无采集任务。前往“商品采集”添加链接。", "list-state"));
    for (const row of jobs) {
      const result = row.result || {}, task = row.task, card = el("article", "", "job"), head = el("div", "", "job-head"), identity = el("div", "", "job-identity");
      let title = row.kind === "shop" ? (result.shop_name || (() => {try {return new URL(row.source_url).hostname;} catch {return "店铺商品采集";}})()) : productName({...row, title: result.title});
      const displayStatus = row.kind === "shop" && task.status === "PENDING" && result.products?.some(p => p.status === "RUNNING") ? "RUNNING" : task.status;
      identity.append(el("span", row.kind === "shop" ? "店铺任务" : "商品任务", "kind-label"), el("h3", title)); head.append(identity, badge(displayStatus)); card.append(head);
      card.append(el("p", row.source_url, "source"));
      if (row.kind === "shop") {
        const total = result.total_products ?? result.count ?? 0, complete = result.completed_products || 0, failed = result.failed_products || 0;
        const metrics = el("div", "", "job-metrics"); for (const [label, value] of [["商品", total || "发现中"], ["完成", complete], ["失败", failed], ["待采集", result.pending_products ?? "—"], ["原图", result.total_asset_count ?? result.detail_count ?? 0]]) {const m = el("span", "", "metric"); m.append(el("strong", String(value)), document.createTextNode(label)); metrics.append(m);} card.append(metrics);
        const track = el("div", "", "progress-row"), p = el("progress"); p.max = total || 1; if (total) p.value = Math.min(total, complete + failed); p.setAttribute("aria-label", "店铺商品采集进度"); track.append(p, el("span", total ? `${Math.round((complete + failed) / total * 100)}%` : "发现中")); card.append(track);
      } else {const m = el("div", "", "job-metrics"); for (const [label, value] of [["商品", 1], ["完成", task.status === "SUCCESS" ? 1 : 0], ["失败", task.status === "FAILED" ? 1 : 0], ["待采集", ["PENDING", "RUNNING"].includes(task.status) ? 1 : 0], ["原图", result.total_asset_count ?? result.detail_count ?? 0]]) {const n = el("span", "", "metric"); n.append(el("strong", String(value)), document.createTextNode(label)); m.append(n);} card.append(m); const track = el("div", "", "progress-row"), progress = el("progress"); progress.max = 1; if (["SUCCESS", "FAILED", "PARTIAL"].includes(task.status)) progress.value = 1; else if (task.status === "PENDING") progress.value = 0; progress.setAttribute("aria-label", "商品采集进度"); track.append(progress, el("span", ["SUCCESS", "FAILED", "PARTIAL"].includes(task.status) ? "100%" : task.status === "PENDING" ? "0%" : labels[task.status] || "处理中")); card.append(track);}
      apiProgress(row, card);
      if (["PENDING", "RUNNING"].includes(task.status)) card.append(el("p", row.kind === "shop" ? shopStage(result) : `${result.stage || "等待执行"}${stageAge(result.stage_at)}`, "stage"));
      const issue = row.manual_reason || task.error_message; if (issue) card.append(el("p", issue, "issue"));
      if (result.asset_errors?.length) {const d = el("details"); d.dataset.key = task.task_id + ":errors"; d.open = opened.has(d.dataset.key); d.append(el("summary", `查看 ${result.asset_errors.length} 张未完成图片的原因`)); for (const e of result.asset_errors) d.append(el("p", `${e.url}：${e.error}`, "issue source")); card.append(d);}
      window.PreparationUI?.status(row, card);
      window.PreparationUI?.saveControls(row, card, () => refreshJobs(true));
      card.append(jobControls(row));
      if (row.kind === "shop" && result.products?.length) {const d = el("details", "", "children-details"), list = el("div", "", "children-list"); d.dataset.key = task.task_id + ":children"; d.open = opened.has(d.dataset.key); d.append(el("summary", `查看 ${result.products.length} 个商品进度`)); for (const p of result.products) {const item = el("div", "", "child"), childHead = el("div", "", "child-head"); childHead.append(el("strong", productName(p)), badge(p.status)); item.append(childHead, el("p", `原图 ${p.total_asset_count ?? p.detail_count ?? 0} 张`, "muted")); apiProgress(p, item); if (p.status === "RUNNING") item.append(el("p", `${p.stage || "正在获取商品页面"}${stageAge(p.stage_at)}`, "stage")); if (p.error) item.append(el("p", p.error, "issue")); window.PreparationUI?.status(p, item); item.append(jobControls(p, false)); list.append(item);} d.append(list); card.append(d);}
      fragment.append(card);
    }
    host.replaceChildren(fragment); $("jobs-count").textContent = jobTotal; $("job-page").textContent = `第 ${Math.floor(jobOffset / limit) + 1} 页 · 共 ${jobTotal} 项任务`;
    $("prev-jobs").disabled = !jobOffset; $("next-jobs").disabled = jobOffset + limit >= jobTotal;
    for (const [id, test] of [["pause-page", r => ["PENDING", "RUNNING", "NEEDS_MANUAL"].includes(r.task.status)], ["resume-page", r => ["CANCELED", "NEEDS_MANUAL"].includes(r.task.status)], ["retry-page", r => ["FAILED", "PARTIAL"].includes(r.task.status)], ["export-page", r => r.result?.product_id || r.result?.products?.some(p => p.product_id)], ["delete-page", () => true]]) $(id).disabled = !jobs.some(test);
  }
  async function refreshJobs(force = false) {
    if (jobLoading) {jobPending = true; jobPendingForce ||= !!force; return;} jobLoading = true;
    try {let data = await api(`/api/collection/jobs?${qs({limit, offset: jobOffset, q: $("job-search").value.trim(), status: $("job-status").value})}`); if (jobOffset && !data.items.length) {jobOffset = data.total ? Math.floor((data.total - 1) / limit) * limit : 0; data = await api(`/api/collection/jobs?${qs({limit, offset: jobOffset, q: $("job-search").value.trim(), status: $("job-status").value})}`);} jobs = data.items; jobTotal = data.total; if (force || !$("recent-collection").contains(document.activeElement)) renderJobs();}
    catch (error) {$("recent-collection").replaceChildren(el("div", `读取任务失败：${error.message}`, "list-state error")); feedback("jobs-feedback", "读取任务失败，可点击刷新重试。", "error");}
    finally {jobLoading = false; if (jobPending) {const pendingForce = jobPendingForce; jobPending = false; jobPendingForce = false; refreshJobs(pendingForce);}}
  }
  function initProgress() {
    $("export-excel-page").addEventListener("click", async () => {
      const task_ids = jobs.filter(r => r.result?.product_id || r.result?.products?.some(p => p.product_id)).map(r => r.task.task_id);
      if (!task_ids.length) {feedback("jobs-feedback", "本页尚无可导出的商品。", "error"); return;}
      const b = $("export-excel-page"); b.disabled = true;
      try {
        const r = await fetch("/api/collection/export.xlsx", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({task_ids})});
        if (!r.ok) throw new Error((await r.json()).detail || "导出失败");
        const disposition = r.headers.get("content-disposition") || "";
        const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i), plain = disposition.match(/filename="([^"]+)"/i);
        const multiple = r.headers.get("content-type")?.includes("application/zip");
        const url = URL.createObjectURL(await r.blob()), a = el("a");
        a.href = url; a.download = encoded ? decodeURIComponent(encoded[1]) : plain?.[1] || (multiple ? "products-excel.zip" : "product.xlsx");
        a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        feedback("jobs-feedback", multiple ? "已开始下载 ZIP，每件商品各有一个 Excel 表格。" : "已开始下载商品 Excel 表格。", "success");
      } catch (error) {feedback("jobs-feedback", error.message, "error");} finally {b.disabled = false;}
    });
    $("refresh-jobs").addEventListener("click", refreshJobs);
    let searchTimer; $("job-search").addEventListener("input", () => {clearTimeout(searchTimer); searchTimer = setTimeout(() => {jobOffset = 0; refreshJobs();}, 300);});
    $("job-status").addEventListener("change", () => {jobOffset = 0; refreshJobs();});
    for (const [id, step] of [["prev-jobs", -limit], ["next-jobs", limit]]) $(id).addEventListener("click", () => {jobOffset = Math.max(0, jobOffset + step); refreshJobs();});
    for (const [id, op, allowed] of [
      ["pause-page", "pause", ["PENDING", "RUNNING", "NEEDS_MANUAL"]],
      ["resume-page", "resume", ["CANCELED", "NEEDS_MANUAL"]],
      ["retry-page", "retry", ["FAILED", "PARTIAL"]]
    ]) $(id).addEventListener("click", async () => {
      const b = $(id); b.disabled = true;
      try {
        for (const row of jobs.filter(r => allowed.includes(r.task.status)))
          await post(`/api/collection/jobs/${encodeURIComponent(row.task.task_id)}/${op}`);
        feedback("jobs-feedback", "本页任务操作已提交。", "success");
      } catch (error) {feedback("jobs-feedback", error.message, "error");}
      finally {await refreshJobs(true);}
    });
    $("delete-page").addEventListener("click", async () => {const task_ids = jobs.map(r => r.task.task_id); if (!task_ids.length || !confirm(`删除本页 ${task_ids.length} 个任务？未完成的采集会停止，已保存的商品资料和原图保留。`)) return; try {const result = await post("/api/collection/jobs/delete", {task_ids}); feedback("jobs-feedback", `已删除 ${result.deleted_count} 个任务，商品资料和原图保留。`, "success");} catch (error) {feedback("jobs-feedback", error.message, "error");} finally {await refreshJobs();}});
    $("export-page").addEventListener("click", async () => {const task_ids = jobs.filter(r => r.result?.product_id || r.result?.products?.some(p => p.product_id)).map(r => r.task.task_id); const b = $("export-page"); b.disabled = true; try {const r = await fetch("/api/collection/export.zip", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({task_ids})}); if (!r.ok) throw new Error((await r.json()).detail || "导出失败"); const url = URL.createObjectURL(await r.blob()), a = el("a"); a.href = url; a.download = "collection-batch.zip"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); feedback("jobs-feedback", "已开始下载本页结果 ZIP。", "success");} catch (error) {feedback("jobs-feedback", error.message, "error");} finally {b.disabled = false;}});
    refreshJobs(); setInterval(() => {if (!document.hidden && !$("product-dialog").open) refreshJobs();}, 3000);
  }

  async function refreshProducts() {
    if (productLoading) {productPending = true; return;} productLoading = true;
    try {let data = await api(`/api/collection/products?${qs({limit, offset: productOffset, q: $("product-search").value.trim(), platform: $("product-platform").value})}`); if (productOffset && !data.items.length) {productOffset = data.total ? Math.floor((data.total - 1) / limit) * limit : 0; data = await api(`/api/collection/products?${qs({limit, offset: productOffset, q: $("product-search").value.trim(), platform: $("product-platform").value})}`);} productTotal = data.total;
      const fragment = document.createDocumentFragment(); if (!data.items.length) fragment.append(el("div", $("product-search").value.trim() || $("product-platform").value !== "all" ? "没有符合条件的商品，请更换搜索或来源筛选。" : "尚未保存商品。完成采集后，商品会出现在这里。", "list-state"));
      for (const p of data.items) {const row = el("article", "", "saved-row"), info = el("div", "", "saved-info"), actions = el("div", "", "actions"); info.append(el("h3", p.title || `商品 ${p.source_product_id || ""}`)); const meta = el("div", "", "saved-meta"); for (const label of [p.source_platform === "tmall" ? "天猫" : "淘宝", `商品 ID ${p.source_product_id || "—"}`, `${p.image_count} 张原图`, `保存于 ${new Date(p.collected_at).toLocaleString("zh-CN")}`]) meta.append(el("span", label)); info.append(meta); actions.append(button("查看结果", trigger => showProduct(p.product_id, trigger), "eye"), link("导出 Excel", `/api/collection/products/${encodeURIComponent(p.product_id)}/export.xlsx`, "download"), link("导出 ZIP", `/api/collection/products/${encodeURIComponent(p.product_id)}/export.zip`, "download")); row.append(info, actions); fragment.append(row);}
      $("saved-products").replaceChildren(fragment); $("products-count").textContent = productTotal; $("product-page").textContent = `第 ${Math.floor(productOffset / limit) + 1} 页 · 共 ${productTotal} 个商品`; $("prev-products").disabled = !productOffset; $("next-products").disabled = productOffset + limit >= productTotal;
    } catch (error) {$("saved-products").replaceChildren(el("div", `读取商品失败：${error.message}`, "list-state error")); feedback("products-feedback", "读取商品失败，可点击刷新重试。", "error");}
    finally {productLoading = false; if (productPending) {productPending = false; refreshProducts();}}
  }
  function initProducts() {$("refresh-products").addEventListener("click", refreshProducts); let timer; $("product-search").addEventListener("input", () => {clearTimeout(timer); timer = setTimeout(() => {productOffset = 0; refreshProducts();}, 300);}); $("product-platform").addEventListener("change", () => {productOffset = 0; refreshProducts();}); for (const [id, step] of [["prev-products", -limit], ["next-products", limit]]) $(id).addEventListener("click", () => {productOffset = Math.max(0, productOffset + step); refreshProducts();}); refreshProducts();}

  function imageKey(value) {try {const u = new URL(value, location.origin); return `${u.hostname}${u.pathname}${u.search}`;} catch {return value || "";}}
  function imagePath(value) {try {const u = new URL(value, location.origin); return `${u.hostname}${u.pathname}`;} catch {return value || "";}}
  function imageTile(asset, title, className = "") {const wrap = el("div", "", className), img = document.createElement("img"), missing = el("span", "图片不可用", "image-missing"); if (asset) {img.src = asset.url; img.alt = title; img.loading = "lazy"; img.addEventListener("error", () => {img.remove(); missing.hidden = false;}); missing.hidden = true; const zoom = el("button", "", "image-trigger"); zoom.type = "button"; zoom.setAttribute("aria-label", `查看大图：${title}`); zoom.append(img, missing); zoom.addEventListener("click", () => {if (!img.isConnected) return; $("large-image").src = asset.url; $("large-image").alt = title; $("large-image-label").textContent = title; $("image-dialog").showModal(); $("close-image").focus();}); wrap.append(zoom);} else wrap.append(missing); return wrap;}
  function renderProduct(p) {
    const content = $("product-content"), fragment = document.createDocumentFragment(), data = p.data || {}, actions = el("div", "", "dialog-actions"); actions.append(link("导出 Excel", `/api/collection/products/${encodeURIComponent(p.product_id)}/export.xlsx`, "download"), link("查看来源页", p.source_url, "external", true), link("资料包 ZIP", `/api/collection/products/${encodeURIComponent(p.product_id)}/export.zip`, "download")); fragment.append(actions); const preparationControls = window.PreparationUI?.render(p, fragment, () => showProduct(p.product_id, activeModalTrigger));
    const focused = p.preparation?.collection_profile === "sku_package";
    if (!focused) {const section = el("section", "", "detail-section"), info = el("dl", "", "detail-grid"); section.append(el("h3", "基本信息")); for (const [label, value] of [["商品名称", data.title || p.title], ["型号", data.model]]) {const box = el("div", "", "detail-pair"); box.append(el("dt", label), el("dd", value == null || value === "" ? "未提供" : String(value))); info.append(box);} section.append(info); fragment.append(section);}
    const value = f => f?.status === "obtained" ? f.value : null;
    const skus = focused ? p.preparation.effective.skus.map(s => ({name: value(s.name), properties: s.combination, package_quantity: value(s.package_quantity), package_unit: value(s.package_unit), package_price: value(s.package_price), stock: value(s.stock), stock_source: s.stock?.source_refs?.[0], image_url: s.image_source_url})) : (Array.isArray(data.skus) ? data.skus.map(s => ({...s, package_price: s.package_price ?? s.price, stock: 9999, stock_source: "default_configuration"})) : []);
    const images = p.images || [], bySource = new Map(images.map(a => [imageKey(a.source_url), a])), byPath = new Map();
    for (const asset of images) {const key = imagePath(asset.source_url); if (!byPath.has(key)) byPath.set(key, asset); else byPath.set(key, null);}
    if (skus.length) {
      const group = el("section", "", "detail-section"), head = el("div", "", "detail-section-head"), grid = el("div", "", "sku-grid"), nav = el("nav", "", "pagination");
      head.append(el("h3", `SKU（${skus.length}）`), el("span", "包装数量与每包价格对应；库存默认9999，供后续填写", "muted")); group.append(head, grid, nav);
      const renderSkus = () => {
        grid.replaceChildren();
        for (const [index, sku] of skus.slice(skuPage * 12, skuPage * 12 + 12).entries()) {
          const card = el("article", "", "sku-card"), title = sku.name || Object.values(sku.properties || {}).join(" · ") || `SKU ${skuPage * 12 + index + 1}`, asset = bySource.get(imageKey(sku.image_url)) || byPath.get(imagePath(sku.image_url));
          if (p.preparation?.include_images !== false) card.append(imageTile(asset, `${p.title} · ${title}`, "sku-image")); else card.classList.add("text-only");
          const body = el("div", "", "sku-body"), facts = el("div", "", "sku-facts"); body.append(el("h4", title));
          const quantity = sku.package_quantity == null ? null : `${sku.package_quantity}${sku.package_unit ? " " + sku.package_unit : ""}`;
          for (const [label, value] of [["包装数量", quantity], ["每包价格", sku.package_price], [sku.stock_source === "default_configuration" ? "库存（默认填写）" : "填写库存", sku.stock]]) {
            const item = el("div"); item.append(el("span", label), el("strong", value == null || value === "" ? "未提供" : String(value))); facts.append(item);
          }
          body.append(facts); card.append(body); grid.append(card);
        }
        nav.replaceChildren(); if (skus.length > 12) {const prev = button("上一页", () => {skuPage--; renderSkus();}, null), next = button("下一页", () => {skuPage++; renderSkus();}, null); prev.disabled = !skuPage; next.disabled = (skuPage + 1) * 12 >= skus.length; nav.append(prev, el("span", `第 ${skuPage + 1} / ${Math.ceil(skus.length / 12)} 页`), next);}
      }; renderSkus(); fragment.append(group);
    }
    if (preparationControls) fragment.append(preparationControls);
    const other = p.preparation?.include_images === false ? [] : images.filter(a => a.role !== "sku" || !skus.some(sku => bySource.get(imageKey(sku.image_url))?.id === a.id || byPath.get(imagePath(sku.image_url))?.id === a.id)); if (other.length) {const group = el("section", "", "detail-section"), grid = el("div", "", "image-grid"), more = button("加载更多图片", () => {imageShown += 24; renderImages();}, "plus"); group.append(el("h3", `其他原图（${other.length}）`), grid, more); const renderImages = () => {grid.replaceChildren(); for (const [i, a] of other.slice(0, imageShown).entries()) {const item = el("figure", "", "image-card"), role = ({main: "主图", detail: "详情图", sku: "SKU 原图"})[a.role] || "原图"; item.append(imageTile(a, `${p.title} · ${role} ${i + 1}`, "gallery-image"), el("figcaption", `${role} · ${a.width ?? "?"} × ${a.height ?? "?"}`), link("下载原图", a.url + "?download=true", "download")); grid.append(item);} more.hidden = imageShown >= other.length;}; renderImages(); fragment.append(group);}

    content.replaceChildren(fragment);
  }
  async function showProduct(id, trigger) {activeModalTrigger = trigger || document.activeElement; skuPage = 0; imageShown = 24; const dialog = $("product-dialog"); $("product-title").textContent = "商品详情"; $("product-content").replaceChildren(el("div", "正在读取商品详情…", "list-state")); if (!dialog.open) dialog.showModal(); $("close-product").focus(); try {const p = await api(`/api/collection/products/${encodeURIComponent(id)}`); if (!dialog.open) return; $("product-title").textContent = p.preparation?.effective?.fields?.title?.value || p.title || "商品详情"; renderProduct(p);} catch (error) {$("product-content").replaceChildren(el("div", `读取结果失败：${error.message}`, "list-state error"));}}
  function initDialogs() {if (!$("product-dialog")) return; $("close-product").addEventListener("click", () => $("product-dialog").close()); $("close-image").addEventListener("click", () => $("image-dialog").close()); $("product-dialog").addEventListener("close", () => {if ($( "image-dialog").open) $("image-dialog").close(); if (activeModalTrigger?.isConnected) activeModalTrigger.focus();}); $("image-dialog").addEventListener("close", () => {$("large-image").removeAttribute("src");}); for (const id of ["product-dialog", "image-dialog"]) $(id).addEventListener("click", e => {if (e.target === $(id)) $(id).close();});}
  health(); initDialogs(); if (page === "collection") initCollection(); else if (page === "progress") initProgress(); else if (page === "products") initProducts();
})();
