/* PM-Mike Desk. Every number comes from the server (scr/app.py -> scr/*.py).
   The page keeps unsaved Book and Constraints edits in memory and previews
   them through /preview; nothing reaches disk until Save. */
"use strict";

const DEF = {NO:0, UW:0.75, AV:1, OW:1.25};
const FREQ = {"2W":"Every 2 weeks","1M":"Monthly","1Q":"Quarterly"};
const EPS = 1e-12;
const STEPS = [
  {k:"statement", t:"Statement", s:"Mandate and range"},
  {k:"fork", t:"Fork", s:"Anchor and baseline"},
  {k:"screen", t:"Screen", s:"Invalidate names", opt:true},
  {k:"book", t:"Book", s:"Investable universe"},
  {k:"constraints", t:"Constraints", s:"Caps", opt:true},
  {k:"target", t:"Target", s:"Allocation"},
];

const S = {page:"data", port:null, step:0, state:null, P:null, preview:null,
           console:{}, confirmDel:null, busy:false, seq:0};

/* ---------- helpers ---------- */
const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct = (x, d=2) => (x*100).toFixed(d) + "%";
const bn = x => Math.round(x).toLocaleString("en-US");
const id = s => s.replace(/\W+/g,"_");
const short = n => n.replace(/^hsc_strat_/, "");

async function api(method, url, data){
  const opt = {method, headers:{}};
  if (data !== undefined){ opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(data); }
  const r = await fetch(url, opt);
  let j = null;
  try { j = await r.json(); } catch { j = {ok:false, log:`FAIL  ${r.status} ${r.statusText}\n`}; }
  return j;
}
function logHtml(text){
  return esc(text).split("\n").map(l =>
    l.startsWith("OK") ? `<span class="o">OK</span>${l.slice(2)}` :
    l.startsWith("WARN") ? `<span class="w">WARN</span>${l.slice(4)}` :
    l.startsWith("FAIL") ? `<span class="e">FAIL</span>${l.slice(4)}` :
    l.startsWith("INFO") ? `<span class="i">INFO</span>${l.slice(4)}` : l).join("\n").trimEnd();
}
const consoleBox = k => S.console[k] ? `<div class="console" aria-live="polite">${logHtml(S.console[k])}</div>` : "";
function rangePill(n, h){
  if (!h) return "";
  if (n < h.min) return `<span class="pill warn">${n} below ${h.min}</span>`;
  if (n > h.max) return `<span class="pill warn">${n} above ${h.max}</span>`;
  return `<span class="pill ok">${n} in range</span>`;
}
function mandateText(r){
  const a = [];
  if (r.frequency) a.push(FREQ[r.frequency]);
  if (r.drift_threshold !== null && r.drift_threshold !== undefined) a.push(`drift > ${pct(r.drift_threshold,0)}`);
  return a.join(" or ") || "none";
}
function go(page, port=null, step=0){
  S.page = page; S.step = step; S.confirmDel = null;
  if (port && port !== S.port){ S.port = port; S.P = null; S.preview = null; S.console.flow = null; }
  render(); window.scrollTo({top:0});
  if (page === "flow" && !S.P) loadPortfolio();
  if (page === "backtest"){
    const names = S.state.portfolios.map(p => p.name);
    const want = port || BT.name || S.port || names[0];
    if (want && (want !== BT.name || !BT.draft)) btLoad(want).then(() => { if (!BT.res && !BT.err) btRun(); });
  }
}
async function refreshState(){ S.state = await api("GET", "/api/state"); }

/* ---------- portfolio model ---------- */
function model(d, keep){
  const P = {name:d.name, d, statement:d.statement, errors:d.errors, anchor:d.anchor, forked:d.forked, refork:d.refork, versions:d.versions,
    invalid:new Set(d.invalid.map(r => r.ticker)), exclusions:d.exclusions,
    u:null, book:{}, tac:{on:false, groups:[]}, cons:null,
    dirty:{book:false, cons:false}, screenPicks:new Set(), lastFork:keep?.lastFork ?? null};
  if (d.universe){
    const u = {...d.universe, FC:{}, HOME:{}, TO:{}, byName:{}};
    u.groups.forEach(g => { u.byName[g.name] = g; g.members.forEach(m => { u.FC[m.t]=m.fcap; u.HOME[m.t]=g.name; u.TO[m.t]=m.to; }); });
    P.u = u;
  }
  const c = d.constraints;
  P.cons = {sector:{on:c.sector.on, max:c.sector.max, per:{...c.sector.per_group},
                    mode:Object.keys(c.sector.per_group).length ? "individual" : "universal"},
            stock:{on:c.stock.on, max:c.stock.max},
            large:{on:c.large.on, T:c.large.threshold, L:c.large.aggregate}};
  if (P.u && d.book){
    P.u.groups.forEach(g => { const b = d.book[g.name] || {rating:"AV", mult:null, investable:g.members.map(m=>m.t)};
      P.book[g.name] = {rating:b.rating, mult:b.mult, included:new Set(b.investable), autoNo:false}; });
    P.tac = {on:d.tactical.on, groups:d.tactical.groups.map(g => ({name:g.name, rating:g.rating, mult:g.mult, tickers:[...g.members], autoNo:g.rating === "NO" && !g.members.length}))};
  }
  /* keep unsaved edits across a reload that did not change them on disk */
  if (keep && keep.dirty.book && P.u && d.book && keep.anchor === P.anchor){
    P.book = keep.book; P.tac = keep.tac; P.dirty.book = true;
    Object.values(P.book).forEach(b => P.invalid.forEach(t => b.included.delete(t)));
  }
  if (keep && keep.dirty.cons){ P.cons = keep.cons; P.dirty.cons = true; }
  return P;
}
async function loadPortfolio(opts={}){
  const name = S.port;
  const d = await api("GET", `/api/p/${encodeURIComponent(name)}`);
  if (S.port !== name) return;
  if (!d || !d.name){ S.P = null; S.console.flow = `FAIL  could not load ${name}\n`; render(); return; }
  S.P = model(d, opts.discard ? {lastFork:S.P?.lastFork, dirty:{book:false, cons:false}} : S.P);
  normalize(S.P); render(); schedulePreview();
}
const ready = P => P && P.u && Object.keys(P.book).length;
function claims(P){ const c = {}; if (P.tac.on) P.tac.groups.forEach(tg => tg.tickers.forEach(t => { if (!P.invalid.has(t)) c[t] = tg.name; })); return c; }
function survivors(P, g){ const c = claims(P); const bk = P.book[g.name]; return g.members.filter(m => bk.included.has(m.t) && !P.invalid.has(m.t) && !c[m.t]); }
/* auto-NO: an empty investable box is NO; a name arriving in an auto-NO group makes it AV again */
function normalize(P){
  if (!ready(P)) return;
  P.u.groups.forEach(g => {
    const bk = P.book[g.name]; const n = survivors(P, g).length;
    if (n === 0 && bk.rating !== "NO"){ bk.rating = "NO"; bk.mult = null; bk.autoNo = true; }
    else if (n > 0 && bk.autoNo){ bk.rating = "AV"; bk.autoNo = false; }
  });
  P.tac.groups.forEach(tg => {
    tg.tickers = tg.tickers.filter(t => P.u.FC[t] !== undefined && !P.invalid.has(t));
    if (!tg.tickers.length && tg.rating !== "NO"){ tg.rating = "NO"; tg.mult = null; tg.autoNo = true; }
    else if (tg.tickers.length && tg.autoNo){ tg.rating = "AV"; tg.autoNo = false; }
  });
}
function bookSpec(P){
  const groups = {};
  P.u.groups.forEach(g => { const b = P.book[g.name]; groups[g.name] = {rating:b.rating, mult:b.rating==="NO" ? null : b.mult, investable:g.members.map(m=>m.t).filter(t => b.included.has(t))}; });
  return {groups, tactical:{on:P.tac.on, groups:P.tac.groups.map(g => ({name:g.name, rating:g.rating, mult:g.rating==="NO" ? null : g.mult, members:g.tickers}))}};
}
function consSpec(P){
  const c = P.cons;
  const known = new Set([...(P.u ? P.u.groups.map(g=>g.name) : []), ...(P.tac.on ? P.tac.groups.map(g=>g.name) : [])]);
  const per = {};
  if (c.sector.mode === "individual") Object.entries(c.sector.per).forEach(([g, v]) => { if (known.has(g)) per[g] = v; });
  return {sector:{on:c.sector.on, max:c.sector.max, per_group:per},
          stock:{on:c.stock.on, max:c.stock.max},
          large:{on:c.large.on, threshold:c.large.T, aggregate:c.large.L}};
}
let previewTimer = null;
function schedulePreview(){
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 120);
}
async function runPreview(){
  const P = S.P; if (!ready(P)) { S.preview = null; return; }
  const seq = ++S.seq;
  const res = await api("POST", `/api/p/${encodeURIComponent(P.name)}/preview`, {...bookSpec(P), constraints:consSpec(P)});
  if (seq !== S.seq || S.P !== P) return;
  S.preview = res; render();
}
const edited = (what) => { S.P.dirty[what] = true; normalize(S.P); render(); schedulePreview(); };

/* ---------- nav ---------- */
function renderNav(){
  const st = S.state; const names = st ? st.portfolios.map(p=>p.name) : [];
  $("#nav").innerHTML = `
    <div class="label">Workspace</div>
    <button data-go="data" aria-current="${S.page==="data"}">Market data ${st?.sticky ? `<span class="pill ok plain num" style="font-size:11px">${st.sticky.slice(5)}</span>` : ""}</button>
    <button data-go="list" aria-current="${S.page==="list"||S.page==="new"}">Portfolios <span class="num" style="font-size:12px;color:var(--ink-3)">${names.length}</span></button>
    ${names.map(n => `<button class="sub" data-port="${esc(n)}" aria-current="${S.page==="flow" && S.port===n}">${esc(short(n))}</button>`).join("")}
    <button data-go="backtest" aria-current="${S.page==="backtest"}">Backtest</button>`;
  $("#railAnchor").textContent = st?.sticky ? `sticky anchor ${st.sticky}` : "";
  $$("#nav [data-go]").forEach(b => b.onclick = () => go(b.dataset.go));
  $$("#nav [data-port]").forEach(b => b.onclick = () => go("flow", b.dataset.port, S.port===b.dataset.port ? S.step : 0));
}

/* ---------- page: data ---------- */
function renderData(){
  const st = S.state, m = st.market, base = st.baseline;
  const tos = base ? base.groups.flatMap(g => g.members.map(x => x.to)).filter(x => x !== null).sort((a,b)=>a-b) : [];
  const med = tos.length ? tos[Math.floor(tos.length/2)] : null;
  const maxW = base ? Math.max(...base.groups.map(g => g.weight)) : 1;
  const opts = [...st.eligible].reverse().map(d => `<option value="${d}" ${d===st.sticky?"selected":""}>${d}${!st.anchors.includes(d) ? "  (not built)" : st.stale[d] ? "  (stale)" : ""}</option>`).join("");
  const stale = Object.entries(st.stale);
  const status = st.unmapped.length
    ? `<span class="pill bad">${st.unmapped.length} unmapped</span>`
    : stale.length ? `<span class="pill warn">${stale.length} anchor${stale.length > 1 ? "s" : ""} stale</span>` : `<span class="pill ok">fresh</span>`;
  return `<div class="page">
    <div class="head"><div><div class="crumbs">data/market.db</div><h1>Market data</h1></div>
      ${m.db ? `<span class="pill ok">${st.sessions.length} sessions to ${st.sessions.at(-1)}</span>` : `<span class="pill bad">No database</span>`}</div>
    <div class="grid2">
      <section class="panel">
        <div class="panel-h"><h2>Ingest FiinPro drops</h2><button class="btn primary" id="runIngest">Rebuild database</button></div>
        <div class="panel-b" style="display:flex;flex-direction:column;gap:14px">
          ${m.db ? `<dl class="kv">
            <dt>Rows</dt><dd>${bn(m.rows)}</dd>
            <dt>Tickers</dt><dd>${m.tickers}</dd>
            <dt>Sessions</dt><dd>${st.sessions.length} · ${st.sessions[0]} → ${st.sessions.at(-1)}</dd>
            <dt>Rebuilt</dt><dd>${m.rebuilt}</dd>
          </dl>` : ""}
          <div class="scroll"><table>
            <thead><tr><th>Drop in data/fiinpro/</th><th class="n">MB</th><th>State</th></tr></thead>
            <tbody>${m.drops.length ? m.drops.map(dp => `<tr><td class="mono">${esc(dp.file)}</td><td class="n">${dp.mb}</td><td>${dp.newer_than_db ? '<span class="pill xs warn">newer than database</span>' : '<span class="pill xs ok">loaded</span>'}</td></tr>`).join("") : `<tr><td colspan="3" class="empty">No drops</td></tr>`}</tbody>
          </table></div>
          ${m.loads?.length ? `<div class="scroll"><table>
            <thead><tr><th>Loaded</th><th>Kind</th><th class="n">Rows</th><th class="n">Dropped</th><th class="n">Names</th><th>Range</th></tr></thead>
            <tbody>${m.loads.map(l => `<tr><td class="mono">${esc(l.file)}</td><td>${l.kind}</td><td class="n">${bn(l.rows)}</td><td class="n">${bn(l.dropped)}</td><td class="n">${l.tickers}</td><td class="mono">${l.from} → ${l.to}</td></tr>`).join("")}</tbody>
          </table></div>` : ""}
          ${m.db ? `<div class="scroll"><table>
            <thead><tr><th>Benchmark</th><th class="n">Sessions</th><th>Range</th><th>Stock sessions</th></tr></thead>
            <tbody>${m.benchmarks?.length ? m.benchmarks.map(b => `<tr><td class="mono">${esc(b.code)}</td><td class="n">${b.sessions}</td><td class="mono">${b.from} → ${b.to}</td><td>${b.missing ? `<span class="pill xs warn">${b.missing} missing</span>` : '<span class="pill xs ok">all covered</span>'}</td></tr>`).join("") : `<tr><td colspan="4" class="empty">No index drop loaded</td></tr>`}</tbody>
          </table></div>` : ""}
          <p class="note">Drop new exports into <span class="mono">data/fiinpro/</span>: stock exports (a Ticker column) and index exports (an Index/Sector column) side by side. The database is rebuilt from scratch into a temp file and swapped in only if every drop validates. Benchmarks are <b>price</b> indexes: dividends are not reinvested, so a total-return backtest leads them by roughly the dividend yield.</p>
        </div>
        ${consoleBox("ingest")}
      </section>
      <section class="panel">
        <div class="panel-h"><h2>Parameters and baselines</h2>${status}</div>
        <div class="panel-b" style="display:flex;flex-direction:column;gap:14px">
          <div class="inline"><label for="bdate" class="label">Anchor</label><select id="bdate" style="width:auto">${opts}</select><button class="btn primary" id="runParams">Build params and baseline</button></div>
          ${st.unmapped.length ? `<p class="note warn"><b>Not in the group map:</b> <span class="mono">${st.unmapped.map(esc).join(" ")}</span>. These trade on the latest session but <span class="mono">index/group_map_live.csv</span> has no group for them. Add the rows by hand; a build on that session fails until you do.</p>` : ""}
          ${stale.length ? `<p class="note warn"><b>Stale, rebuild:</b> ${stale.map(([a, why]) => `${a} (${why.map(esc).join(", ")} is newer)`).join("; ")}. A fork onto a stale anchor rebuilds it first.</p>` : ""}
          <dl class="kv">
            <dt>Sticky anchor</dt><dd>${st.sticky ?? "none"} · index/anchor_date.json</dd>
            <dt>Built anchors</dt><dd>${st.anchors.length ? st.anchors.map(a => a + (st.stale[a] ? " (stale)" : "")).join(" · ") : "none"}</dd>
            <dt>Group map</dt><dd>index/group_map_live.csv${base ? ` · ${base.groups.length} groups` : ""}${st.group_map.edited ? ` · edited ${st.group_map.edited}` : ""}</dd>
            ${med !== null ? `<dt>21d turnover</dt><dd>median ${med.toFixed(3)}% of float cap / day</dd>` : ""}
            <dt>FOL</dt><dd><span class="pill off">index/fol.csv deferred</span></dd>
          </dl>
          <p class="note">Runs <span class="mono">params.py</span> then <span class="mono">baseline.py</span> for the anchor; both re-read the group map, so this is the button after editing it. The sticky copy at the baseline root is refreshed only for the sticky anchor. Portfolios keep their own copy of the baseline until you re-fork them.</p>
        </div>
        ${consoleBox("params")}
      </section>
    </div>
    ${base ? `<section class="panel">
      <div class="panel-h"><h2>Baseline allocation, ${base.anchor}</h2><span class="label">free-float cap weight · ${base.groups.length} groups</span></div>
      <div class="scroll"><table>
        <thead><tr><th>Group</th><th class="n">Names</th><th class="n">Float cap, bn VND</th><th class="n">Weight</th><th style="width:34%"></th></tr></thead>
        <tbody>${base.groups.map(g => { const fc = g.members.reduce((a,x)=>a+x.fcap,0); return `<tr>
          <td>${esc(g.name)}</td><td class="n">${g.members.length}</td><td class="n">${bn(fc)}</td><td class="n">${pct(g.weight)}</td>
          <td><div class="bar"><div class="t" style="width:${g.weight/maxW*100}%;background:var(--accent)"></div></div></td></tr>`; }).join("")}</tbody>
      </table></div>
    </section>` : ""}
  </div>`;
}
function bindData(){
  $("#runIngest").onclick = () => action("ingest", "POST", "/api/run/ingest", {}, async () => { await refreshState(); });
  $("#runParams").onclick = () => action("params", "POST", "/api/run/baseline", {date:$("#bdate").value}, async () => { await refreshState(); });
}

/* one action at a time; its log lands in S.console[key] */
async function action(key, method, url, data, after){
  if (S.busy) return null;
  S.busy = true; S.console[key] = "running...\n"; render();
  let res;
  try { res = await api(method, url, data); }
  finally { S.busy = false; }
  S.console[key] = res.log ?? (res.ok ? "OK\n" : "FAIL\n");
  if (after) await after(res);
  render();
  return res;
}

/* what a re-fork onto the rebuilt baseline would change */
function reforkText(rf){
  const lost = Object.entries(rf.lost ?? {}).map(([g, r]) => `${g} ${r}`);
  const parts = [];
  if (lost.length) parts.push(`lost: ${lost.join(", ")}`);
  if (rf.appeared?.length) parts.push(`new, AV: ${rf.appeared.join(", ")}`);
  return parts.length ? parts.join(" · ") : "same groups, names moved or changed";
}

/* ---------- page: list ---------- */
function renderList(){
  const cards = S.state.portfolios.map(p => {
    const st = p.statement;
    const scr = st ? Object.entries(st.screens).filter(([,v])=>v.on).map(([k])=>k) : [];
    const del = S.confirmDel === p.name
      ? `<span class="confirm">Delete portfolio/${esc(p.name)}/ and its files? <button class="btn sm danger" data-del-yes="${esc(p.name)}">Delete</button><button class="btn sm" data-del-no>Keep</button></span>`
      : `<button class="btn ghost sm" data-del="${esc(p.name)}">Delete</button>`;
    const status = !p.anchor ? '<span class="pill off">not forked</span>' : p.error ? `<span class="pill bad" title="${esc(p.error)}">blocked</span>` : rangePill(p.n, st?.holdings);
    const rf = p.refork?.needed ? `<div class="row"><span>Baseline</span><span class="pill xs warn" title="${esc(reforkText(p.refork))}">regrouped since fork, re-fork</span></div>` : "";
    return `<div class="card">
      <button class="open" data-port="${esc(p.name)}">
      <div class="inline" style="justify-content:space-between;width:100%"><span class="nm">${esc(p.name)}</span>${status}</div>
      <p style="color:var(--ink-2);font-size:13px">${st?.approach ? esc(st.approach) : '<i style="color:var(--ink-3)">No approach written yet</i>'}</p>
      <div style="display:flex;flex-direction:column;gap:4px;width:100%">
        ${st ? `<div class="row"><span>Holdings range</span><span class="num">${st.holdings.min}–${st.holdings.max}</span></div>
        <div class="row"><span>Rebalance</span><span>${mandateText(st.rebalance)}</span></div>` : ""}
        <div class="row"><span>Screens · constraints</span><span>${scr.length ? scr.join(", ") : "off"} · ${p.constraints_on.length ? p.constraints_on.join(", ") : "off"}</span></div>
        ${p.top ? `<div class="row"><span>Top group</span><span>${esc(p.top.group)} <span class="num">${pct(p.top.w,1)}</span></span></div>` : ""}
        <div class="row"><span>Last built</span><span class="num">${p.built_at ?? "never"}</span></div>
        ${rf}
      </div></button>
      <div class="foot"><span class="mono" style="color:var(--ink-3)">${p.anchor ? "anchor " + p.anchor : "no anchor"}</span>${del}</div>
    </div>`;
  }).join("");
  return `<div class="page">
    <div class="head"><div><div class="crumbs">portfolio/</div><h1>Portfolios</h1></div></div>
    <div class="cards">${cards}
      <button class="card new" id="newCard"><b>+ New portfolio</b><span style="font-size:13px">Write the statement, then fork a baseline.</span></button>
    </div>
    ${consoleBox("list")}
  </div>`;
}
function bindList(){
  $$(".open[data-port]").forEach(c => c.onclick = () => go("flow", c.dataset.port, 0));
  $("#newCard").onclick = () => go("new");
  $$("[data-del]").forEach(b => b.onclick = () => { S.confirmDel = b.dataset.del; render(); });
  $$("[data-del-no]").forEach(b => b.onclick = () => { S.confirmDel = null; render(); });
  $$("[data-del-yes]").forEach(b => b.onclick = () => {
    const n = b.dataset.delYes; S.confirmDel = null;
    action("list", "DELETE", `/api/p/${encodeURIComponent(n)}`, {yes:true}, async () => { if (S.port === n){ S.port = null; S.P = null; } await refreshState(); });
  });
}

/* ---------- statement form ---------- */
function statementForm(st, isNew){
  const r = st.rebalance;
  return `<div class="form">
    ${isNew ? `<div class="field full"><label for="f_name">Folder name</label><input type="text" id="f_name" placeholder="hsc_strat_dividend"><span class="hint">Lowercase letters, digits, underscore. Becomes portfolio/&lt;name&gt;/.</span></div>` : ""}
    <div class="field full"><label for="f_app">Approach strategy</label><textarea id="f_app" placeholder="e.g. Growth at a reasonable price; overweight consumption and private banks on credit recovery.">${esc(st.approach)}</textarea><span class="hint">Reference text for later review. Not used in calculations.</span></div>
    <div class="field full"><label for="f_scope">Scope universe</label><textarea id="f_scope" placeholder="e.g. VN100 constituents, HOSE only, excluding state banks.">${esc(st.scope)}</textarea></div>
    <div class="field"><label for="f_min">Holdings, minimum</label><input type="number" id="f_min" min="1" step="1" value="${st.holdings.min}"></div>
    <div class="field"><label for="f_max">Holdings, maximum</label><input type="number" id="f_max" min="1" step="1" value="${st.holdings.max}"><span class="hint">The target step flags a holding count outside this range.</span></div>
    <div class="field"><label>Rebalance on schedule</label>
      <div class="seg" role="group" aria-label="Frequency">${["none","2W","1M","1Q"].map(f => `<button type="button" data-freq="${f}" aria-pressed="${(r.frequency||"none")===f}">${f==="none"?"Off":f}</button>`).join("")}</div>
      <span class="hint">2W every two weeks · 1M monthly · 1Q quarterly</span></div>
    <div class="field"><label for="f_drift">Rebalance on drift, % of book</label>
      <div class="inline"><label class="toggle"><input type="checkbox" id="f_drift_on" ${r.drift_threshold!==null?"checked":""}> On</label>
      <input type="number" id="f_drift" min="1" max="99" step="1" value="${r.drift_threshold!==null ? Math.round(r.drift_threshold*100) : 8}" style="width:80px" ${r.drift_threshold===null?"disabled":""}></div>
      <span class="hint">Drift = ½ Σ |actual − target| across groups. Whichever trigger fires first.</span></div>
    <div class="field full"><span class="err" id="f_err" role="alert"></span></div>
  </div>`;
}
function readStatementForm(base){
  const st = structuredClone(base);
  st.approach = $("#f_app").value.trim(); st.scope = $("#f_scope").value.trim();
  st.holdings = {min: parseInt($("#f_min").value,10), max: parseInt($("#f_max").value,10)};
  const f = $("[data-freq][aria-pressed=true]").dataset.freq;
  st.rebalance = {frequency: f==="none" ? null : f, drift_threshold: $("#f_drift_on").checked ? (+$("#f_drift").value)/100 : null};
  return st;
}
function bindStatementForm(){
  $$("[data-freq]").forEach(b => b.onclick = () => $$("[data-freq]").forEach(x => x.setAttribute("aria-pressed", x===b)));
  $("#f_drift_on").onchange = e => $("#f_drift").disabled = !e.target.checked;
}
const DEFAULT_STATEMENT = {approach:"", scope:"", holdings:{min:20,max:30}, rebalance:{frequency:"1Q", drift_threshold:null},
  screens:{turnover:{on:false,min_pct:0.10}, float_cap:{on:false,min_bn_vnd:1000}}};
function renderNew(){
  return `<div class="page" style="max-width:760px">
    <div class="head"><div><div class="crumbs">portfolio/ · new</div><h1>New portfolio</h1></div></div>
    <section class="panel"><div class="panel-h"><h2>Portfolio statement</h2><span class="label">writes statement.json</span></div>
      <div class="panel-b">${statementForm(DEFAULT_STATEMENT, true)}</div>
      <div class="panel-f"><button class="btn ghost" id="cancelNew">Cancel</button><button class="btn primary" id="createNew">Create portfolio</button></div>
      ${consoleBox("new")}
    </section>
  </div>`;
}
function bindNew(){
  bindStatementForm();
  $("#cancelNew").onclick = () => go("list");
  $("#createNew").onclick = async () => {
    const name = $("#f_name").value.trim(); const st = readStatementForm(DEFAULT_STATEMENT);
    const res = await action("new", "POST", "/api/p", {name, statement:st});
    if (res?.ok){ await refreshState(); go("flow", name, 1); }
  };
}

/* ---------- page: flow ---------- */
function stepStatus(P, k){
  const st = P.statement; const r = S.preview;
  if (!st && k !== "statement") return "";
  switch(k){
    case "statement": return st ? mandateText(st.rebalance) + ` · ${st.holdings.min}–${st.holdings.max}` : "missing";
    case "fork": return P.anchor ? `anchor ${P.anchor}` : "not forked";
    case "screen": { const on = Object.values(st.screens).filter(v=>v.on).length; return `${on ? on + " on" : "off"} · ${P.invalid.size} invalid`; }
    case "book": { if (!ready(P)) return "fork first"; const no = Object.values(P.book).filter(b=>b.rating==="NO").length; return `${r?.ok ? r.n + " names · " : ""}${no} NO${P.tac.on ? " · tactical" : ""}${P.dirty.book ? " · unsaved" : ""}`; }
    case "constraints": { const on = Object.entries(P.cons).filter(([,v])=>v.on).map(([k])=>k); return (on.length ? on.join(" · ") : "off") + (P.dirty.cons ? " · unsaved" : ""); }
    case "target": return !r ? "" : r.ok ? `${r.n} names` : "blocked";
  }
}
function renderFlow(){
  const P = S.P;
  if (!P) return `<div class="page"><div class="head"><div><div class="crumbs">portfolio/${esc(S.port)}/</div><h1>${esc(S.port)}</h1></div></div><p style="color:var(--ink-3)">Loading...</p>${consoleBox("flow")}</div>`;
  const k = STEPS[S.step].k;
  const body = {statement:stepStatement, fork:stepFork, screen:stepScreen, book:stepBook, constraints:stepConstraints, target:stepTarget}[k](P);
  const r = S.preview; const h = P.statement?.holdings;
  const n = r?.ok ? r.n : 0;
  const hi = h ? Math.max(h.max*1.4, n*1.1, 10) : 10;
  const top = r?.ok ? r.rows.filter(x=>x.w>0).slice(0,5) : [];
  const dirty = P.dirty.book || P.dirty.cons;
  return `<div class="page">
    <div class="head"><div><div class="crumbs">portfolio/${esc(P.name)}/</div><h1>${esc(P.name)}</h1></div>
      <div class="inline"><button class="btn ghost" id="reload" title="Re-read the files on disk; unsaved edits stay in the page">Reload</button><button class="btn ghost" id="toBt">Backtest</button><button class="btn ghost" id="toList">All portfolios</button></div></div>
    ${P.errors.length ? `<p class="note bad">${P.errors.map(esc).join("<br>")}</p>` : ""}
    <nav class="steps" aria-label="Portfolio flow">${STEPS.map((s,i) => `<button data-step="${i}" aria-current="${i===S.step?"step":"false"}">
      <span class="i">${i+1}${s.opt?'<span class="opt">optional</span>':""}</span><span class="t">${s.t}</span><span class="s">${esc(stepStatus(P, s.k))}</span></button>`).join("")}</nav>
    ${dirty ? `<div class="dirtybar"><span>Unsaved edits in ${[P.dirty.book && "Book", P.dirty.cons && "Constraints"].filter(Boolean).join(" and ")}. The preview shows them; the files on disk do not have them yet.</span>
      <span class="inline"><button class="btn sm" id="discardAll">Discard</button><button class="btn sm primary" id="saveAll">Save</button></span></div>` : ""}
    <div class="flow">
      <div style="display:flex;flex-direction:column;gap:16px;min-width:0">${body}
        ${consoleBox("flow")}
        <div class="inline" style="justify-content:space-between">
          <button class="btn" id="prev" ${S.step===0?"disabled":""}>Back</button>
          ${S.step<5 ? `<button class="btn primary" id="next">Next: ${STEPS[S.step+1].t}</button>` : ""}
        </div>
      </div>
      <aside class="aside">
        <section class="panel"><div class="panel-b" style="display:flex;flex-direction:column;gap:4px">
          <span class="label">Live target preview</span>
          ${!ready(P) ? `<p style="color:var(--ink-3);font-size:13px">Fork a baseline to see the target.</p>` : !r ? `<p style="color:var(--ink-3);font-size:13px">Computing...</p>` : `
          <div class="inline" style="justify-content:space-between"><span class="big">${n}</span>${r.ok ? rangePill(n, h) : '<span class="pill bad">blocked</span>'}</div>
          <span style="font-size:12.5px;color:var(--ink-3)">holdings vs statement range</span>
          ${h ? `<div class="meter" aria-hidden="true"><div class="band" style="left:${h.min/hi*100}%;width:${(h.max-h.min)/hi*100}%"></div><div class="dot" style="left:calc(${Math.min(n/hi,1)*100}% - 1px)"></div></div>
          <div class="meter-l"><span>0</span><span>${h.min}–${h.max}</span><span>${Math.round(hi)}</span></div>` : ""}
          ${r.ok ? "" : `<p class="err" style="margin-top:8px;white-space:pre-wrap">${esc(r.error)}</p>`}`}
        </div></section>
        ${top.length ? `<section class="panel"><div class="panel-b" style="display:flex;flex-direction:column;gap:8px">
          <span class="label">Largest groups</span>
          ${top.map(x => `<div style="display:grid;grid-template-columns:1fr auto;gap:2px 8px;font-size:12.5px">
            <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(x.group)}</span><span class="num">${pct(x.w,1)}</span>
            <div class="bar" style="grid-column:1/-1;height:6px;min-width:0"><div class="t" style="top:0;bottom:0;width:${x.w*100}%;background:var(--${x.rating.toLowerCase()})"></div></div></div>`).join("")}
        </div></section>` : ""}
        ${P.statement ? `<section class="panel"><div class="panel-b kv" style="font-size:12.5px">
          <dt>Rebalance</dt><dd style="font-family:var(--sans)">${mandateText(P.statement.rebalance)}</dd>
          <dt>Baseline</dt><dd>${P.anchor ?? "not forked"}</dd>
          <dt>Constraints</dt><dd style="font-family:var(--sans)">${esc(stepStatus(P,"constraints"))}</dd>
        </div></section>` : ""}
      </aside>
    </div>
  </div>`;
}

function stepStatement(P){
  const st = P.statement || DEFAULT_STATEMENT;
  return `<section class="panel"><div class="panel-h"><h2>Portfolio statement</h2><span class="label">statement.json</span></div>
    <div class="panel-b">${statementForm(st, false)}</div>
    <div class="panel-f"><button class="btn primary" id="saveStmt">Save statement</button></div>
  </section>`;
}
function stepFork(P){
  const st = S.state; const rep = P.lastFork;
  const opts = [...st.eligible].reverse().map(d => `<option value="${d}" ${d===(P.anchor ?? st.sticky)?"selected":""}>${d}${!st.anchors.includes(d) ? "  (builds on fork)" : st.stale[d] ? "  (stale, rebuilds on fork)" : ""}</option>`).join("");
  const lost = rep ? Object.entries(rep.lost) : [];
  const rf = P.refork?.needed ? `<div class="note warn"><b>The baseline for ${esc(P.anchor)} was rebuilt after this fork.</b> Re-fork to pick up the new grouping: ${esc(reforkText(P.refork))}.</div>` : "";
  return `<section class="panel"><div class="panel-h"><h2>Fork the baseline</h2></div>
    <div class="panel-b" style="display:flex;flex-direction:column;gap:14px">
      <div class="form">
        <div class="field"><label for="anchorSel">Baseline anchor</label><select id="anchorSel">${opts}</select>
          <span class="hint">Any session with 21 sessions of history behind it. One that is not built yet gets its params and baseline built first.</span></div>
        <div class="field"><label>&nbsp;</label><div class="inline"><button class="btn primary" id="refork" ${P.dirty.book ? "disabled" : ""}>${P.anchor ? "Re-fork onto selected" : "Fork"}</button></div>
          <span class="hint">${P.dirty.book ? "Save or discard the Book edits first: forking rebuilds the book." : "Carries ratings, multipliers and the investable universe by group name."}</span></div>
      </div>
      ${P.anchor ? `<dl class="kv">
        <dt>Forked from</dt><dd>${esc(P.forked.forked_from)}</dd>
        <dt>Forked at</dt><dd>${esc(P.forked.forked_at)}</dd>
        ${P.u ? `<dt>Groups</dt><dd>${P.u.groups.length}</dd>` : ""}
        <dt>Sticky anchor</dt><dd>${st.sticky}${P.anchor !== st.sticky ? ' <span class="pill warn">portfolio is not on the sticky anchor</span>' : ""}</dd>
      </dl>` : ""}
      ${rf}
      ${lost.length ? `<div class="note warn"><b>Lost on this re-fork:</b> ${lost.map(([g, r]) => `${esc(g)} ${esc(r)}`).join(", ")}. The group left the baseline, so its view is gone. Re-express it on another group in the Book step if it still holds.</div>` : ""}
      <p style="font-size:13px;color:var(--ink-2)">Forking copies the baseline grid into <span class="mono">input/</span> and rebuilds the book. You never edit weights directly: every change after this point is a group rating, a multiplier or a choice of names.</p>
    </div></section>`;
}
function stepScreen(P){
  if (!P.statement || !ready(P)) return `<section class="panel"><div class="panel-b"><p style="color:var(--ink-3)">Write the statement and fork a baseline first.</p></div></section>`;
  const sc = P.statement.screens; const u = P.u;
  const tos = Object.values(u.TO).filter(x => x !== null).sort((a,b)=>a-b);
  const med = tos.length ? tos[Math.floor(tos.length/2)] : null;
  const minFc = Object.entries(u.FC).sort((a,b)=>a[1]-b[1])[0];
  const hits = P.exclusions;
  const picks = [...new Set(hits.map(x => x.ticker))];
  return `<section class="panel"><div class="panel-h"><h2>Screens</h2><span class="label">data/params/${P.anchor}.csv · suggest only</span></div>
    <div class="panel-b" style="display:flex;flex-direction:column;gap:14px">
      <div class="scroll"><table>
        <thead><tr><th>Screen</th><th>On</th><th>Suggest invalidating when below</th><th>Universe</th></tr></thead>
        <tbody>
          <tr><td><b>21-day turnover</b><div style="font-size:12px;color:var(--ink-3)">avg daily value ÷ float cap</div></td>
            <td><label class="toggle"><input type="checkbox" id="sc_to" ${sc.turnover.on?"checked":""} aria-label="Turnover screen on"></label></td>
            <td><div class="inline"><input type="number" id="sc_to_v" step="0.05" min="0" value="${sc.turnover.min_pct}" style="width:90px"> <span class="mono">% / day</span></div></td>
            <td class="mono" style="font-size:12px">${med !== null ? `median ${med.toFixed(3)}%` : ""}</td></tr>
          <tr><td><b>Float cap</b><div style="font-size:12px;color:var(--ink-3)">free float × official close</div></td>
            <td><label class="toggle"><input type="checkbox" id="sc_fc" ${sc.float_cap.on?"checked":""} aria-label="Float cap screen on"></label></td>
            <td><div class="inline"><input type="number" id="sc_fc_v" step="100" min="0" value="${sc.float_cap.min_bn_vnd}" style="width:90px"> <span class="mono">bn VND</span></div></td>
            <td class="mono" style="font-size:12px">min ${bn(minFc[1])} (${minFc[0]})</td></tr>
          <tr><td><b>Foreign ownership limit</b></td><td colspan="3"><span class="pill off">Deferred until index/fol.csv is filled</span></td></tr>
        </tbody></table></div>
      <div class="inline"><button class="btn primary" id="scRun">Save screens and run</button><span style="font-size:12.5px;color:var(--ink-3)">Writes the screens into statement.json and rewrites screen/exclusions.csv. Nothing leaves the book until you invalidate.</span></div>
    </div>
    <div class="panel-h" style="border-top:1px solid var(--line)"><h3>Suggested <span class="num" style="color:var(--ink-3)">${picks.length}</span></h3>
      <div class="inline"><button class="btn" id="scPickAll" ${hits.length?"":"disabled"}>Select all</button><button class="btn primary" id="scApply" ${P.screenPicks.size?"":"disabled"}>Invalidate ${P.screenPicks.size||""}</button></div></div>
    ${hits.length ? `<div class="scroll"><table><thead><tr><th></th><th>Ticker</th><th>Group</th><th>Screen</th><th class="n">Value</th><th class="n">Threshold</th></tr></thead><tbody>
      ${hits.map((x, i) => `<tr><td><input type="checkbox" data-pick="${esc(x.ticker)}" id="pick_${i}" ${P.screenPicks.has(x.ticker)?"checked":""} aria-label="Invalidate ${esc(x.ticker)}"></td><td class="mono">${esc(x.ticker)}</td><td>${esc(x.group)}</td><td class="mono">${esc(x.screen)}</td><td class="n">${x.value === "" ? "no 21-session history" : (+x.value).toPrecision(4)}</td><td class="n">&lt; ${esc(x.threshold)}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="panel-b"><p style="color:var(--ink-3);font-size:13px">${Object.values(sc).some(v=>v.on) ? "No investable name fails the screens, or they have not been run." : "Turn a screen on and run it to see suggestions."}</p></div>`}
    <div class="panel-h" style="border-top:1px solid var(--line)"><h3>Invalidated <span class="num" style="color:var(--ink-3)">${P.invalid.size}</span></h3><span class="label">stays in the book universe, cannot be made investable</span></div>
    <div class="panel-b">${P.invalid.size ? `<div class="chips">${[...P.invalid].sort().map(t => `<span class="chip inv" style="cursor:default">${esc(t)}<button class="x" data-restore="${esc(t)}" title="Restore ${esc(t)}" aria-label="Restore ${esc(t)}">×</button></span>`).join("")}</div>
      <p class="hint" style="font-size:12px;color:var(--ink-3);margin-top:8px">Restoring does not put a name back in the book; add it in the Book step.</p>` : `<p style="color:var(--ink-3);font-size:13px">None.</p>`}</div>
  </section>`;
}
function chip(P, t, cls, extra=""){ return `<span class="chip ${cls}" draggable="${cls.includes("inv")||cls.includes("in ")||cls.includes("claimed")?"false":"true"}" data-t="${esc(t)}" title="${esc(P.u.co[t]||t)}">${esc(t)}${extra}</span>`; }
function stepBook(P){
  if (!ready(P)) return `<section class="panel"><div class="panel-b"><p style="color:var(--ink-3)">Fork a baseline first.</p></div></section>`;
  const u = P.u; const c = claims(P); const r = S.preview;
  const byG = r?.ok ? Object.fromEntries(r.rows.map(x=>[x.group,x])) : {};
  const groups = u.groups.map(g => { const bk = P.book[g.name]; const x = byG[g.name]; const surv = survivors(P, g);
    const inc = g.members.filter(m => bk.included.has(m.t));
    const d = x ? x.w - g.weight : 0;
    return `<div class="grp ${bk.rating==="NO"?"isno":""}" data-g="${esc(g.name)}">
      <div class="gside">
        <div class="gname" title="${esc(g.name)}">${esc(g.name)}</div>
        <div class="rate" role="group" aria-label="Rating for ${esc(g.name)}">${["NO","UW","AV","OW"].map(k => `<button data-g="${esc(g.name)}" data-r="${k}" aria-pressed="${bk.rating===k}" ${!surv.length && k!=="NO" ? "disabled title=\"No investable name\"" : ""}>${k}</button>`).join("")}</div>
        <div class="inline" style="gap:6px"><span class="gmeta">×</span><input class="mult" type="number" step="0.05" min="0" id="m_${id(g.name)}" data-mg="${esc(g.name)}" placeholder="${DEF[bk.rating].toFixed(2)}" value="${bk.mult ?? ""}" ${bk.rating==="NO"?"disabled":""} aria-label="Multiplier override for ${esc(g.name)}">${bk.autoNo ? '<span class="pill xs off plain">auto NO</span>' : ""}</div>
        <div class="gmeta">base ${pct(g.weight,1)} → <b style="color:var(--ink)">${x ? pct(x.w,1) : "–"}</b> ${x ? `<span class="delta ${d>0.0005?"up":d<-0.0005?"dn":""}">${d>=0?"+":""}${(d*100).toFixed(1)}</span>` : ""}${x?.capped ? ' <span class="pill xs warn plain">cap</span>' : ""}</div>
      </div>
      <div class="gboxes">
        <div class="zone zone-all" data-g="${esc(g.name)}">
          <div class="zone-h"><span class="label">All names · ${g.members.length}</span><button class="btn sm" data-addall="${esc(g.name)}" ${inc.length === g.members.filter(m=>!P.invalid.has(m.t)).length ? "disabled":""}>Add all</button></div>
          <div class="chips">${g.members.map(m => { const cls = P.invalid.has(m.t) ? "inv" : c[m.t] ? "claimed" : bk.included.has(m.t) ? "in " : ""; return chip(P, m.t, cls, c[m.t] ? `<small style="opacity:.8">→ ${esc(c[m.t])}</small>` : ""); }).join("")}</div>
        </div>
        <div class="zone zone-inc" data-g="${esc(g.name)}">
          <div class="zone-h"><span class="label">Investable · ${surv.length}</span><button class="btn sm" data-rmall="${esc(g.name)}" ${inc.length?"":"disabled"}>Remove all</button></div>
          <div class="chips">${surv.length ? surv.map(m => chip(P, m.t, "", `<button class="x" data-rm="${esc(m.t)}" aria-label="Remove ${esc(m.t)}">×</button>`)).join("") : `<span class="empty">Drop names here${g.members.some(m => c[m.t]) ? " · some claimed by a tactical group" : ""}</span>`}</div>
        </div>
      </div>
    </div>`; }).join("");
  const tac = P.tac.groups.map((tg, i) => { const x = byG[tg.name]; return `<div class="tacg" data-tg="${i}">
      <div class="gside">
        <div class="inline" style="gap:6px"><input type="text" value="${esc(tg.name)}" id="tgname_${i}" data-tgname="${i}" aria-label="Tactical group name" style="padding:3px 6px;font-weight:600"><button class="btn sm ghost" data-tgdel="${i}" aria-label="Delete tactical group">×</button></div>
        <div class="rate" role="group"><span class="chip tac" style="cursor:default;font-size:10.5px">tactical</span>${["UW","AV","OW"].map(k => `<button data-tg="${i}" data-r="${k}" aria-pressed="${tg.rating===k}" ${tg.tickers.length ? "" : "disabled"}>${k}</button>`).join("")}</div>
        <div class="inline" style="gap:6px"><span class="gmeta">×</span><input class="mult" type="number" step="0.05" min="0" id="tgm_${i}" data-tgm="${i}" placeholder="${DEF[tg.rating].toFixed(2)}" value="${tg.mult ?? ""}" ${tg.rating==="NO"?"disabled":""} aria-label="Multiplier">${tg.rating==="NO" ? '<span class="pill xs off plain">NO until it claims</span>' : ""}</div>
        <div class="gmeta">budget ${pct(tg.tickers.reduce((a,t)=>a+u.FC[t],0)/u.groups.reduce((a,g)=>a+g.members.reduce((b,m)=>b+m.fcap,0),0),2)} → <b style="color:var(--ink)">${P.tac.on && x ? pct(x.w,1) : "off"}</b></div>
      </div>
      <div class="zone zone-tac" data-tg="${i}">
        <div class="zone-h"><span class="label">Claims · ${tg.tickers.length}</span><span class="gmeta">from ${[...new Set(tg.tickers.map(t=>u.HOME[t]))].map(esc).join(", ") || "—"}</span></div>
        <div class="combo">
          <input type="text" id="tsearch_${i}" data-tsearch="${i}" placeholder="Type a ticker or company, Enter to claim" autocomplete="off" role="combobox" aria-expanded="false" aria-controls="tlist_${i}" aria-label="Search stocks to claim for ${esc(tg.name)}">
          <ul class="combo-list" id="tlist_${i}" role="listbox" hidden></ul>
        </div>
        <span class="err" id="terr_${i}" role="alert"></span>
        <div class="chips">${tg.tickers.length ? tg.tickers.map(t => `<span class="chip tac" style="cursor:default" title="${esc(u.co[t]||t)} · home ${esc(u.HOME[t])}">${esc(t)}<button class="x" data-unclaim="${esc(t)}" aria-label="Release ${esc(t)}">×</button></span>`).join("") : `<span class="empty">No claims. A claimed name leaves its home group and takes its float-cap budget along.</span>`}</div>
      </div>
    </div>`; }).join("");
  return `<section class="panel"><div class="panel-h"><h2>Book</h2>
      <div class="legend"><span><i style="background:var(--ow)"></i>OW 1.25</span><span><i style="background:var(--av)"></i>AV 1.00</span><span><i style="background:var(--uw)"></i>UW 0.75</span><span><i style="background:var(--no)"></i>NO 0</span><span>Drag a name into Investable to include it. An empty box is NO.</span></div>
      <div class="inline"><button class="btn sm" id="addAllG">Add all groups</button><button class="btn sm" id="rmAllG">Remove all groups</button></div></div>
    ${groups}
    <div class="panel-b"><p class="note">Removing a name keeps its group's budget; the survivors absorb it by float cap. Struck-through names are invalidated by a screen. A blank multiplier uses the rating default.</p></div>
  </section>
  <section class="panel"><div class="panel-h"><h2>Tactical overlay</h2><label class="toggle"><input type="checkbox" id="tac_on" ${P.tac.on?"checked":""}> ${P.tac.on?"On":"Off"}</label></div>
    <div class="panel-b" style="display:flex;flex-direction:column;gap:12px;${P.tac.on?"":"opacity:.6"}">
      ${tac || `<p style="color:var(--ink-3);font-size:13px">No tactical groups.</p>`}
      <div class="inline"><input type="text" id="tacNew" placeholder="New tactical group name" style="width:260px"><button class="btn" id="tacAdd">Add tactical group</button>
        <span style="font-size:12.5px;color:var(--ink-3)">${P.tac.on ? "Switching off releases every claim back to its home group." : "Claims are kept but inert while the overlay is off."}</span></div>
    </div>
    <div class="panel-f"><button class="btn" id="discardBook" ${P.dirty.book?"":"disabled"}>Discard</button><button class="btn primary" id="saveBook" ${P.dirty.book?"":"disabled"}>Save book and overlay</button></div>
  </section>`;
}
function capOf(P, g){ const s = P.cons.sector; return s.mode === "individual" ? (s.per[g] ?? s.max) : s.max; }
function reportRows(P){
  const r = S.preview; const rep = r?.ok ? r.report : {}; const c = P.cons;
  const out = [];
  out.push({k:"Sector cap", on:c.sector.on, txt: !rep.sector ? "–" : rep.sector.capped.length ? `binding on ${rep.sector.capped.length} of ${rep.sector.live} live groups` : "not binding"});
  out.push({k:"Max per stock", on:c.stock.on, txt: !rep.stock ? "–" : rep.stock.at_max ? `binding on ${rep.stock.at_max} name${rep.stock.at_max>1?"s":""}` : "not binding"});
  out.push({k:"Large holdings", on:c.large.on, txt: !rep.large ? "–" : `${rep.large.n} above ${pct(c.large.T,1)} sum to ${pct(rep.large.sum,1)}${rep.large.at_threshold ? `; ${rep.large.at_threshold} held at ${pct(c.large.T,1)}` : ""}`});
  return out;
}
const statusTable = P => `<div class="scroll"><table class="status"><tbody>${reportRows(P).map(x => `<tr><td style="width:40%"><b>${x.k}</b></td><td>${x.on ? esc(x.txt) : '<span style="color:var(--ink-3)">off</span>'}</td></tr>`).join("")}</tbody></table></div>`;
function stepConstraints(P){
  const cs = P.cons; const r = S.preview;
  const live = r?.ok ? r.rows.filter(x => x.unc > 0) : [];
  const n = r?.ok ? r.n : 0;
  const con = (key, title, desc, body) => `<div class="con ${cs[key].on?"on":""}">
      <label class="check"><input type="checkbox" id="con_${key}" data-con="${key}" ${cs[key].on?"checked":""}> ${title}</label><span class="desc" style="text-align:right">${desc}</span>
      ${cs[key].on ? `<div class="body">${body}</div>` : ""}</div>`;
  const sector = con("sector", "Sector cap", "group grain · applies to tactical groups too", `
      <div class="inline"><div class="seg" role="group"><button data-capmode="universal" aria-pressed="${cs.sector.mode==="universal"}">Universal</button><button data-capmode="individual" aria-pressed="${cs.sector.mode==="individual"}">Individual</button></div>
        <label for="cap_v" style="font-size:12.5px;color:var(--ink-2)">${cs.sector.mode==="universal" ? "Max weight per group" : "Default for groups without a value"}</label><input class="small-in" type="number" id="cap_v" min="1" max="100" step="1" value="${+(cs.sector.max*100).toFixed(2)}"><span class="mono">%</span></div>
      ${cs.sector.mode==="individual" ? `<div class="scroll"><table><thead><tr><th>Live group</th><th class="n">Tilted</th><th class="n">Cap %</th></tr></thead><tbody>
        ${live.map(x => `<tr><td>${esc(x.group)}${x.kind==="tactical"?' <span class="pill xs off plain">tactical</span>':""}</td><td class="n">${pct(x.unc,1)}</td><td class="n"><input class="small-in" type="number" min="1" max="100" step="1" id="capg_${id(x.group)}" data-capg="${esc(x.group)}" value="${+(capOf(P, x.group)*100).toFixed(2)}" aria-label="Cap for ${esc(x.group)}"></td></tr>`).join("")}
      </tbody></table></div>` : ""}
      <p class="desc">Feasible when caps sum to at least 100%: now ${pct(live.reduce((a,x)=>a+capOf(P, x.group),0),0)} across ${live.length} live groups. Excess is redistributed pro-rata to groups under their cap, iterating until none is above.</p>`);
  const stock = con("stock", "Max holding per stock", "stock grain", `
      <div class="inline"><label for="stk_v" style="font-size:12.5px;color:var(--ink-2)">Max weight per name</label><input class="small-in" type="number" id="stk_v" min="0.5" max="100" step="0.5" value="${+(cs.stock.max*100).toFixed(2)}"><span class="mono">%</span></div>
      <p class="desc">Feasible when ${pct(cs.stock.max,1)} × ${n} names ≥ 100%: now ${pct(cs.stock.max*n,0)}. Excess stays inside the name's group where it can; it spills to other groups only when every name there is already at the cap.</p>`);
  const large = con("large", "Large holdings cap", "UCITS 5/10/40 style · stock grain", `
      <div class="inline"><label for="uc_t" style="font-size:12.5px;color:var(--ink-2)">A name is large above</label><input class="small-in" type="number" id="uc_t" min="0.5" max="50" step="0.5" value="${+(cs.large.T*100).toFixed(2)}"><span class="mono">%</span>
        <label for="uc_l" style="font-size:12.5px;color:var(--ink-2);margin-left:12px">Large names together at most</label><input class="small-in" type="number" id="uc_l" min="1" max="100" step="1" value="${+(cs.large.L*100).toFixed(2)}"><span class="mono">%</span></div>
      <p class="desc">Large names are scaled down pro-rata to the aggregate cap; a name that would fall under the threshold stops there. The excess pours into names below the threshold, each stopping at the threshold, so no name ever crosses it and the large set only shrinks. Feasible when L + T × small names ≥ 100%.</p>`);
  return `<section class="panel"><div class="panel-h"><h2>Constraints</h2><span class="label">constraints.json · applied after the tilt</span></div>
    <div class="panel-b cons">${sector}${stock}${large}</div>
    <div class="panel-h" style="border-top:1px solid var(--line)"><h3>Status</h3></div>
    ${statusTable(P)}
    ${r && !r.ok ? `<div class="panel-b"><p class="note bad" style="white-space:pre-wrap">${esc(r.error)}</p></div>` : ""}
    <div class="panel-f"><button class="btn" id="discardCons" ${P.dirty.cons?"":"disabled"}>Discard</button><button class="btn primary" id="saveCons" ${P.dirty.cons?"":"disabled"}>Save constraints</button></div>
  </section>`;
}
function stepTarget(P){
  const r = S.preview;
  if (!ready(P)) return `<section class="panel"><div class="panel-b"><p style="color:var(--ink-3)">Fork a baseline first.</p></div></section>`;
  if (!r) return `<section class="panel"><div class="panel-b"><p style="color:var(--ink-3)">Computing...</p></div></section>`;
  const summ = S.state.portfolios.find(p => p.name === P.name);
  const dirty = P.dirty.book || P.dirty.cons;
  const buildBtn = `<button class="btn primary" id="build" ${!r.ok ? "disabled" : ""}>${dirty ? "Save and build" : "Build target"}</button>`;
  if (!r.ok) return `<section class="panel"><div class="panel-h"><h2>Target allocation</h2><span class="inline"><span class="pill bad">Blocked</span>${buildBtn}</span></div><div class="panel-b"><p class="err" style="white-space:pre-wrap">${esc(r.error)}</p></div></section>`;
  const st = P.statement; const n = r.n; const u = P.u; const cs = P.cons;
  const maxW = Math.max(...r.rows.map(x=>Math.max(x.w, x.base)));
  return `<section class="panel"><div class="panel-h"><h2>Target allocation</h2>
      <div class="inline">${dirty ? `<span class="pill warn">Preview includes unsaved edits</span>` : summ?.built_at ? `<span class="pill ok">Last built ${esc(summ.built_at)}</span>` : `<span class="pill off">Never built</span>`}${buildBtn}</div></div>
    ${dirty ? `<div class="panel-b" style="padding-bottom:0"><p class="note warn">The build reads the files on disk. Save and build writes the ${[P.dirty.book && "Book", P.dirty.cons && "Constraints"].filter(Boolean).join(" and ")} edits first, then builds.</p></div>` : ""}
    ${r.in_range === false && st ? `<div class="panel-b" style="padding-bottom:0"><p class="note warn"><b>${n} holdings is outside ${st.holdings.min}–${st.holdings.max}.</b> ${n < st.holdings.min ? "Add names to investable boxes or rate more groups above NO." : "Remove names or rate groups NO in the Book step."} The build still runs; the flag is a warning.</p></div>` : ""}
    ${r.messages.length ? `<div class="panel-b" style="padding-bottom:0"><div class="console" style="border-radius:6px;border:0">${logHtml(r.messages.join("\n"))}</div></div>` : ""}
    <div class="scroll"><table>
      <thead><tr><th>Group</th><th>Rating</th><th class="n">Names</th><th class="n">Base</th><th class="n">Tilted</th><th class="n">Target</th><th style="width:26%">Base <span style="font-weight:400">|</span> target</th></tr></thead>
      <tbody>${r.rows.filter(x=>x.w>0).map(x => `<tr><td>${esc(x.group)}${x.kind==="tactical"?' <span class="pill plain off xs">tactical</span>':""}${x.capped?' <span class="pill plain warn xs">capped</span>':""}${x.full?' <span class="pill plain warn xs">full</span>':""}</td>
        <td><span class="mono" style="color:var(--${x.rating.toLowerCase()});font-weight:500">${x.rating}</span> <span class="mono" style="color:var(--ink-3);font-size:12px">×${x.m.toFixed(2)}</span></td>
        <td class="n">${x.n}/${x.n_all}</td><td class="n">${pct(x.base)}</td><td class="n" style="color:var(--ink-3)">${pct(x.unc)}</td><td class="n"><b>${pct(x.w)}</b></td>
        <td><div class="bar"><div class="t" style="width:${x.w/maxW*100}%;background:var(--${x.rating.toLowerCase()})"></div><div class="b" style="width:${x.base/maxW*100}%"></div>${cs.sector.on ? `<div class="c" style="left:${Math.min(1, capOf(P, x.group)/maxW)*100}%"></div>` : ""}</div></td></tr>`).join("")}
      </tbody></table></div>
    <div class="panel-b" style="padding-block:6px 10px"><span style="font-size:12px;color:var(--ink-3)">${r.rows.filter(x=>x.w===0).length} groups at 0 (NO). Bar is the target, grey tick the baseline weight${cs.sector.on ? ", dashed line the cap" : ""}. ${esc(r.tac_note)}</span></div>
    </section>
    <section class="panel"><div class="panel-h"><h2>Constraints in force</h2></div>${statusTable(P)}</section>
    <section class="panel"><div class="panel-h"><h2>Holdings <span class="num" style="color:var(--ink-3)">${n}</span></h2><span class="label">target/holdings.csv</span></div>
      <div class="scroll" style="max-height:440px;overflow:auto"><table>
        <thead><tr><th>Ticker</th><th>Company</th><th>Group</th><th class="n">Float cap, bn</th><th class="n">Weight</th><th></th></tr></thead>
        <tbody>${r.holdings.map(hh => `<tr><td class="mono">${esc(hh.t)}</td><td>${esc(u.co[hh.t]||"")}</td><td>${esc(hh.group)}${hh.group!==hh.home?` <span style="color:var(--ink-3);font-size:12px">from ${esc(hh.home)}</span>`:""}</td><td class="n">${bn(hh.fcap)}</td><td class="n">${pct(hh.w)}</td><td>${hh.pin==="stock_max"?'<span class="pill xs warn plain">at max</span>':hh.pin==="at_threshold"?'<span class="pill xs warn plain">at threshold</span>':hh.pin==="large"?'<span class="pill xs off plain">large</span>':""}</td></tr>`).join("")}</tbody>
      </table></div></section>`;
}

/* ---------- tactical claim search ---------- */
/* Barrier rule: a stock belongs to at most one tactical group. Invalidated names cannot be claimed. */
function claimBlock(P, i, t){
  if (P.invalid.has(t)) return "invalidated by a screen";
  const other = P.tac.groups.find((g, j) => j !== i && g.tickers.includes(t));
  if (other) return `already in ${other.name}`;
  if (P.tac.groups[i].tickers.includes(t)) return "already claimed here";
  return null;
}
function bindTacSearch(P){
  const u = P.u;
  const all = Object.keys(u.FC).sort();
  $$("[data-tsearch]").forEach(inp => {
    const i = +inp.dataset.tsearch;
    const list = $("#tlist_" + i), err = $("#terr_" + i);
    let hits = [], cur = -1;
    const draw = () => {
      const q = inp.value.trim().toUpperCase();
      if (!q){ list.hidden = true; inp.setAttribute("aria-expanded","false"); return; }
      hits = all.filter(t => t.startsWith(q))
        .concat(all.filter(t => !t.startsWith(q) && ((u.co[t]||"").toUpperCase().includes(q))))
        .slice(0, 8).map(t => ({t, why: claimBlock(P, i, t)}));
      if (cur >= hits.length || (cur >= 0 && hits[cur].why)) cur = hits.findIndex(h => !h.why);
      if (cur < 0) cur = hits.findIndex(h => !h.why);
      list.innerHTML = hits.length ? hits.map((h, k) => `<li role="option" id="topt_${i}_${k}" data-k="${k}" aria-selected="${k===cur}" aria-disabled="${!!h.why}" class="${h.why?"dis":""}">
          <span class="mono"><b>${esc(h.t)}</b></span><span class="co">${esc(u.co[h.t]||"")}</span><span class="why">${h.why ? esc(h.why) : esc(u.HOME[h.t])}</span></li>`).join("")
        : `<li class="dis" aria-disabled="true"><span class="why">No stock in the ${P.anchor} universe matches "${esc(inp.value.trim())}"</span></li>`;
      list.hidden = false; inp.setAttribute("aria-expanded","true");
      inp.setAttribute("aria-activedescendant", cur >= 0 ? `topt_${i}_${cur}` : "");
    };
    const take = h => {
      if (!h) return;
      if (h.why){ err.textContent = `${h.t} cannot be claimed: ${h.why}.`; return; }
      P.tac.groups[i].tickers.push(h.t);
      inp.value = "";
      edited("book");
      const again = $("#tsearch_" + i); if (again) again.focus();
    };
    inp.addEventListener("input", () => { err.textContent = ""; cur = -1; draw(); });
    inp.addEventListener("keydown", e => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp"){
        e.preventDefault(); if (!hits.length) return;
        const step = e.key === "ArrowDown" ? 1 : -1;
        for (let n = 0, k = cur; n < hits.length; n++){ k = (k + step + hits.length) % hits.length; if (!hits[k].why){ cur = k; break; } }
        draw();
      } else if (e.key === "Enter"){
        e.preventDefault();
        const q = inp.value.trim().toUpperCase();
        const exact = hits.find(h => h.t === q);
        take(exact || hits[cur] || hits[0]);
      } else if (e.key === "Escape"){ inp.value = ""; draw(); }
    });
    inp.addEventListener("blur", () => setTimeout(() => { if (list) list.hidden = true; }, 150));
    list.addEventListener("mousedown", e => { const li = e.target.closest("li[data-k]"); if (!li) return; e.preventDefault(); take(hits[+li.dataset.k]); });
  });
}

/* ---------- drag and drop ---------- */
function bindDrag(P){
  const u = P.u;
  let drag = null;
  $$('.chip[draggable="true"]').forEach(ch => {
    ch.addEventListener("dragstart", e => { drag = {t:ch.dataset.t, from: ch.closest(".zone")?.classList.contains("zone-inc") ? "inc" : "all"}; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", ch.dataset.t); ch.classList.add("ghost"); });
    ch.addEventListener("dragend", () => { drag = null; ch.classList.remove("ghost"); });
  });
  const accepts = (z, d) => {
    if (!d || P.invalid.has(d.t)) return false;
    const home = u.HOME[d.t];
    if (z.classList.contains("zone-inc")) return z.dataset.g === home && !claims(P)[d.t] && !P.book[home].included.has(d.t);
    if (z.classList.contains("zone-all")) return z.dataset.g === home && d.from !== "all";
    return false;                                  // tactical claims come from search only
  };
  $$(".zone").forEach(z => {
    z.addEventListener("dragover", e => { if (accepts(z, drag)) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; z.classList.add("over"); } });
    z.addEventListener("dragleave", () => z.classList.remove("over"));
    z.addEventListener("drop", e => { e.preventDefault(); z.classList.remove("over"); if (!accepts(z, drag)) return;
      const t = drag.t, home = u.HOME[t];
      if (z.classList.contains("zone-inc")) P.book[home].included.add(t);
      else if (z.classList.contains("zone-all") && drag.from === "inc") P.book[home].included.delete(t);
      edited("book"); });
  });
}

/* ---------- saving ---------- */
async function saveBook(){
  const P = S.P;
  const res = await action("flow", "PUT", `/api/p/${encodeURIComponent(P.name)}/book`, {...bookSpec(P), version:P.versions.book});
  if (res?.ok){ P.dirty.book = false; await afterSave(); }
  return res?.ok;
}
async function saveCons(){
  const P = S.P;
  const res = await action("flow", "PUT", `/api/p/${encodeURIComponent(P.name)}/constraints`, {constraints:consSpec(P), version:P.versions.constraints});
  if (res?.ok){ P.dirty.cons = false; await afterSave(); }
  return res?.ok;
}
async function afterSave(){ await Promise.all([loadPortfolio(), refreshState()]); render(); }

function bindFlow(){
  const P = S.P; if (!P) return;
  $("#toList").onclick = () => go("list");
  $("#toBt").onclick = () => go("backtest", P.name);
  $("#reload").onclick = () => afterSave();
  const k = STEPS[S.step].k;
  $$("[data-step]").forEach(b => b.onclick = () => { S.step = +b.dataset.step; render(); });
  $("#prev").onclick = () => { S.step--; render(); };
  if ($("#next")) $("#next").onclick = () => { S.step++; render(); window.scrollTo({top:0}); };
  if ($("#saveAll")) $("#saveAll").onclick = async () => { if (P.dirty.book && !(await saveBook())) return; if (S.P.dirty.cons) await saveCons(); };
  if ($("#discardAll")) $("#discardAll").onclick = () => loadPortfolio({discard:true});
  const name = encodeURIComponent(P.name);
  if (k === "statement"){
    bindStatementForm();
    $("#saveStmt").onclick = () => action("flow", "PUT", `/api/p/${name}/statement`,
      {statement: readStatementForm(P.statement || DEFAULT_STATEMENT), version:P.versions.statement}, async res => { if (res.ok) await afterSave(); });
  }
  if (k === "fork") $("#refork").onclick = () => action("flow", "POST", `/api/p/${name}/fork`, {anchor:$("#anchorSel").value},
    async res => { if (res.ok){ S.P.lastFork = res.report; await refreshState(); await loadPortfolio({discard:true}); S.P.lastFork = res.report; } });
  if (k === "screen" && $("#scRun")){
    const screens = () => ({turnover:{on:$("#sc_to").checked, min_pct:+$("#sc_to_v").value},
                            float_cap:{on:$("#sc_fc").checked, min_bn_vnd:+$("#sc_fc_v").value}});
    const after = async () => { P.screenPicks.clear(); await Promise.all([loadPortfolio(), refreshState()]); };
    $("#scRun").onclick = () => action("flow", "POST", `/api/p/${name}/screen`, {screens:screens()}, after);
    $$("[data-pick]").forEach(c => c.onchange = () => { c.checked ? P.screenPicks.add(c.dataset.pick) : P.screenPicks.delete(c.dataset.pick); render(); });
    if ($("#scPickAll")) $("#scPickAll").onclick = () => { P.exclusions.forEach(x => P.screenPicks.add(x.ticker)); render(); };
    $("#scApply").onclick = () => action("flow", "POST", `/api/p/${name}/screen`, {invalidate:[...P.screenPicks]}, after);
    $$("[data-restore]").forEach(b => b.onclick = () => action("flow", "POST", `/api/p/${name}/screen`, {restore:[b.dataset.restore]}, after));
  }
  if (k === "book" && ready(P)){
    const u = P.u;
    $$(".grp .rate button").forEach(b => b.onclick = () => { const bk = P.book[b.dataset.g]; bk.rating = b.dataset.r; bk.autoNo = false; if (bk.rating==="NO") bk.mult = null; edited("book"); });
    $$("[data-mg]").forEach(i => i.onchange = () => { P.book[i.dataset.mg].mult = i.value === "" ? null : Math.max(0, +i.value); edited("book"); });
    $$("[data-addall]").forEach(b => b.onclick = () => { const g = u.byName[b.dataset.addall]; g.members.forEach(m => { if (!P.invalid.has(m.t)) P.book[g.name].included.add(m.t); }); edited("book"); });
    $$("[data-rmall]").forEach(b => b.onclick = () => { P.book[b.dataset.rmall].included.clear(); edited("book"); });
    $$("[data-rm]").forEach(b => b.onclick = () => { P.book[u.HOME[b.dataset.rm]].included.delete(b.dataset.rm); edited("book"); });
    $("#addAllG").onclick = () => { u.groups.forEach(g => g.members.forEach(m => { if (!P.invalid.has(m.t)) P.book[g.name].included.add(m.t); })); edited("book"); };
    $("#rmAllG").onclick = () => { u.groups.forEach(g => P.book[g.name].included.clear()); edited("book"); };
    $("#tac_on").onchange = e => { P.tac.on = e.target.checked; edited("book"); };
    $("#tacAdd").onclick = () => { const nm = $("#tacNew").value.trim(); if (!nm || P.tac.groups.some(g=>g.name===nm) || u.byName[nm]) return; P.tac.groups.push({name:nm, rating:"NO", mult:null, tickers:[], autoNo:true}); edited("book"); };
    $$("[data-tgname]").forEach(i => i.onchange = () => { const nm = i.value.trim(); const j = +i.dataset.tgname;
      if (nm && !u.byName[nm] && !P.tac.groups.some((g, x) => x !== j && g.name === nm)){ const old = P.tac.groups[j].name; P.tac.groups[j].name = nm; if (P.cons.sector.per[old] !== undefined){ P.cons.sector.per[nm] = P.cons.sector.per[old]; delete P.cons.sector.per[old]; } }
      edited("book"); });
    $$("[data-tgdel]").forEach(b => b.onclick = () => { P.tac.groups.splice(+b.dataset.tgdel, 1); edited("book"); });
    $$("[data-tg][data-r]").forEach(b => b.onclick = () => { const tg = P.tac.groups[+b.dataset.tg]; tg.rating = b.dataset.r; tg.autoNo = false; edited("book"); });
    $$("[data-tgm]").forEach(i => i.onchange = () => { P.tac.groups[+i.dataset.tgm].mult = i.value === "" ? null : Math.max(0, +i.value); edited("book"); });
    $$("[data-unclaim]").forEach(b => b.onclick = () => { P.tac.groups.forEach(g => g.tickers = g.tickers.filter(x => x !== b.dataset.unclaim)); edited("book"); });
    $("#saveBook").onclick = saveBook;
    $("#discardBook").onclick = () => loadPortfolio({discard:true});
    bindTacSearch(P);
    bindDrag(P);
  }
  if (k === "constraints"){
    const cs = P.cons;
    const frac = (v, lo) => Math.min(1, Math.max(lo, +v/100));
    $$("[data-con]").forEach(c => c.onchange = () => { cs[c.dataset.con].on = c.checked; edited("cons"); });
    $$("[data-capmode]").forEach(b => b.onclick = () => { cs.sector.mode = b.dataset.capmode; edited("cons"); });
    if ($("#cap_v")) $("#cap_v").onchange = e => { cs.sector.max = frac(e.target.value, 0.001); edited("cons"); };
    $$("[data-capg]").forEach(i => i.onchange = () => { cs.sector.per[i.dataset.capg] = frac(i.value, 0.001); edited("cons"); });
    if ($("#stk_v")) $("#stk_v").onchange = e => { cs.stock.max = frac(e.target.value, 0.001); edited("cons"); };
    if ($("#uc_t")) $("#uc_t").onchange = e => { cs.large.T = Math.min(0.999, frac(e.target.value, 0.001)); edited("cons"); };
    if ($("#uc_l")) $("#uc_l").onchange = e => { cs.large.L = frac(e.target.value, 0.001); edited("cons"); };
    $("#saveCons").onclick = saveCons;
    $("#discardCons").onclick = () => loadPortfolio({discard:true});
  }
  if (k === "target" && $("#build")) $("#build").onclick = async () => {
    if (S.P.dirty.book && !(await saveBook())) return;       // a failed save leaves the edits in the page
    if (S.P.dirty.cons && !(await saveCons())) return;
    await action("flow", "POST", `/api/p/${name}/build`, {}, async () => { await refreshState(); });
  };
}

/* ---------- page: backtest ----------
   Every number comes from /backtest (backtest_engine.run). Portfolio, start,
   costs and lag need a run; benchmark and risk-free rate apply to the last
   result (the engine returns every benchmark; Sharpe is rescaled by rf). */
const BT = {name:null, start:null, bench:"VNINDEX", draft:null, saved:null, version:null,
            defaults:null, cfgErr:null, res:null, req:null, err:null, running:false, cur:null};
const RUNKEYS = ["brokerage_bps", "sell_tax_bps", "lag_sessions"];
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const spct = (v, d=2) => (v >= 0 ? "+" : "−") + Math.abs(v*100).toFixed(d) + "%";
const spp = (v, d=2) => (v >= 0 ? "+" : "−") + Math.abs(v*100).toFixed(d) + " pp";
const n2 = v => v === null || v === undefined ? "n/a" : (v < 0 ? "−" : "") + Math.abs(v).toFixed(2);
const tone = v => v >= 0 ? "pos" : "negv";
const mpct = v => (v < 0 ? "−" : "") + Math.abs(v*100).toFixed(2) + "%";

async function btLoad(name){
  BT.name = name; BT.draft = null; render();     // Run disabled until the config is in
  const d = await api("GET", `/api/p/${encodeURIComponent(name)}/backtest`);
  if (BT.name !== name) return;
  BT.draft = {...d.config}; BT.saved = {...d.config}; BT.version = d.version;
  BT.defaults = d.defaults; BT.cfgErr = d.error;
  render();
}
function btDirty(){
  const q = BT.req;
  if (!q || !BT.draft) return true;
  return q.name !== BT.name || q.start !== BT.start || RUNKEYS.some(k => q.cfg[k] !== BT.draft[k]);
}
async function btRun(){
  if (BT.running || !BT.draft) return;
  const req = {name:BT.name, start:BT.start, cfg:{...BT.draft}};
  BT.running = true; render();
  let r;
  try { r = await api("POST", `/api/p/${encodeURIComponent(req.name)}/backtest`,
                      {start:req.start, benchmark:BT.bench, config:req.cfg}); }
  finally { BT.running = false; }
  BT.req = req;
  if (r?.ok){ BT.res = r; BT.err = null; BT.ranAt = new Date().toTimeString().slice(0, 8); } else { BT.res = null; BT.err = r?.error || r?.log || "FAIL"; }
  render();
}
function btSnap(iso){
  const ss = S.state.sessions;
  if (!iso) return ss[0];
  return ss.find(s => s >= iso) ?? null;
}

function niceTicks(lo, hi, count){
  if (hi - lo < 1e-9){ lo -= 1; hi += 1; }
  const raw = (hi - lo) / count, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m*mag).find(s => s >= raw);
  const out = [];
  for (let v = Math.floor(lo/step)*step; v <= Math.ceil(hi/step)*step + step/2; v += step) out.push(+v.toFixed(10));
  return out;
}
const CM = {l:52, r:14};
const xScale = n => i => CM.l + (n <= 1 ? 0 : i/(n-1)*(1000 - CM.l - CM.r));
const linePath = (vals, x, y) => vals.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join("");
function xAxis(dates, x, h){
  const t = dates.map((d, i) => i === 0 || d.slice(0,7) !== dates[i-1].slice(0,7) ? i : -1).filter(i => i >= 0);
  const every = Math.ceil(t.length / 9);
  return t.filter((_, k) => k % every === 0).map(i => { const m = +dates[i].slice(5,7) - 1;
    return `<line class="gridl" x1="${x(i)}" x2="${x(i)}" y1="8" y2="${h}" opacity=".55"/><text class="ax" x="${x(i)}" y="${h+16}" text-anchor="middle">${m === 0 || i === 0 ? `${MON[m]} ${dates[i].slice(2,4)}` : MON[m]}</text>`; }).join("");
}
function btCharts(r, code){
  const P = r.portfolio.map(v => v*100), B = r.benchmarks[code].equity.map(v => v*100);
  const E = P.map((v, i) => v / B[i] - 1), n = P.length, x = xScale(n), dates = r.dates;
  let H = 272, top = 12, tk = niceTicks(Math.min(...P, ...B), Math.max(...P, ...B), 5);
  let lo = tk[0], hi = tk.at(-1), y = v => top + (hi - v)/(hi - lo)*(H - top);
  const pos = Object.fromEntries(dates.map((d, i) => [d, i]));
  let main = tk.map(t => `<line class="${t === 100 ? "zero" : "gridl"}" x1="${CM.l}" x2="${1000-CM.r}" y1="${y(t)}" y2="${y(t)}"/><text class="ax" x="${CM.l-8}" y="${y(t)+4}" text-anchor="end">${t}</text>`).join("")
    + xAxis(dates, x, H) + `<path class="lb" d="${linePath(B, x, y)}"/><path class="lp" d="${linePath(P, x, y)}"/>`
    + r.rebalances.map(e => { const i = pos[e.fill]; return `<circle class="mk-${e.trigger}" cx="${x(i)}" cy="${y(P[i])}" r="4.5"><title>${e.trigger} fill ${e.fill}</title></circle>`; }).join("")
    + `<circle cx="${x(n-1)}" cy="${y(P[n-1])}" r="3.5" fill="var(--accent)"/><line class="cross" id="xh" x1="0" x2="0" y1="${top}" y2="${H}" visibility="hidden"/>`;
  H = 86; top = 6; tk = niceTicks(Math.min(0, ...E), Math.max(0, ...E), 2); lo = tk[0]; hi = tk.at(-1);
  y = v => top + (hi - v)/(hi - lo)*(H - top);
  const y0 = y(0), area = vals => `M${x(0)} ${y0}` + vals.map((v, i) => `L${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join("") + `L${x(n-1)} ${y0}Z`;
  const ex = tk.map(t => `<line class="${t === 0 ? "zero" : "gridl"}" x1="${CM.l}" x2="${1000-CM.r}" y1="${y(t)}" y2="${y(t)}"/><text class="ax" x="${CM.l-8}" y="${y(t)+4}" text-anchor="end">${t > 0 ? "+" : ""}${+(t*100).toFixed(1)}%</text>`).join("")
    + xAxis(dates, x, H) + `<path class="ex-up" d="${area(E.map(v => Math.max(v, 0)))}"/><path class="ex-dn" d="${area(E.map(v => Math.min(v, 0)))}"/><path class="lx" d="${linePath(E, x, y)}"/>`
    + `<line class="cross" id="xh2" x1="0" x2="0" y1="${top}" y2="${H}" visibility="hidden"/>`;
  BT.cur = {P, B, E, dates, x, code};
  return {main, ex};
}

function renderBacktest(){
  const st = S.state, names = st.portfolios.map(p => p.name);
  if (!names.length) return `<div class="page"><div class="head"><div><div class="crumbs">backtest</div><h1>Backtest</h1></div></div><p class="note">No portfolio yet. Create one on the Portfolios page.</p></div>`;
  const codes = (st.market.benchmarks || []).map(b => b.code);
  const ss = st.sessions, d = BT.draft, r = BT.res, dirty = btDirty();
  const snapped = btSnap(BT.start);
  const summ = st.portfolios.find(p => p.name === BT.name);
  const cfgDirty = d && BT.saved && Object.keys(d).some(k => d[k] !== BT.saved[k]);
  const field = (k, label, step, hint="") => `<div class="field"><label for="bt_${k}">${label}</label><input type="number" id="bt_${k}" data-cfg="${k}" min="0" step="${step}" value="${d ? (k === "risk_free_rate" ? +(d[k]*100).toFixed(4) : d[k]) : ""}">${hint ? `<span class="hint">${hint}</span>` : ""}</div>`;

  let results = "";
  if (r){
    const code = r.benchmarks[BT.bench] ? BT.bench : r.benchmark;
    const bs = r.benchmarks[code].stats, p = bs.portfolio, b = bs.benchmark, rf = d ? d.risk_free_rate : r.config.risk_free_rate;
    const shp = s => s.vol > 0 ? (s.annualised - rf) / s.vol : null;
    const {main, ex} = btCharts(r, code);
    const h = r.holdings_range, reb = r.rebalance, last = r.rebalances.at(-1);
    const row = (l, a, c, diff, cls="") => `<tr><td>${l}</td><td class="n num">${a}</td><td class="n num">${c}</td><td class="n num ${cls}">${diff}</td></tr>`;
    const kv = (l, v) => `<tr><td>${l}</td><td class="n num">${v}</td></tr>`;
    const c = r.config;
    results = `
    <div class="bt-stamp">Run ${esc(BT.ranAt || "")} · ${esc(r.name)} · ${r.start} → ${r.end} · lag ${c.lag_sessions} · brokerage ${c.brokerage_bps} bps a side, sell tax ${c.sell_tax_bps} bps · ${esc(mandateText(reb))}</div>
    ${r.messages.length ? `<div class="console">${logHtml(r.messages.join("\n"))}</div>` : ""}
    ${code !== BT.bench ? `<p class="note warn">${esc(BT.bench)} has no close on every session of this window; showing ${esc(code)}.</p>` : ""}
    <div class="bt-res${dirty ? " stale" : ""}">
    <section class="panel">
      <div class="bt-figs">
        <div class="fig"><span class="label">Portfolio</span><span class="big ${tone(p.total)}">${spct(p.total)}</span><span class="sub">total return, ${bs.sessions} sessions</span></div>
        <div class="fig"><span class="label">${esc(code)}</span><span class="big ${tone(b.total)}">${spct(b.total)}</span><span class="sub">price return</span></div>
        <div class="fig"><span class="label">Excess</span><span class="big ${tone(bs.excess)}">${spp(bs.excess)}</span><span class="sub">IR ${n2(bs.information_ratio)} · TE ${pct(bs.tracking_error, 1)}</span></div>
        <div class="fig"><span class="label">Max drawdown</span><span class="big negv">${mpct(p.max_drawdown)}</span><span class="sub">${esc(code)} ${mpct(b.max_drawdown)}</span></div>
      </div>
      <div class="panel-h" style="border-top:1px solid var(--line)"><h2>Growth of 100</h2>
        <div class="bt-key"><span><i></i>${esc(r.name)}</span><span><i class="b"></i>${esc(code)}</span><span><i class="d"></i>calendar fill</span>${reb.drift_threshold != null ? '<span><i class="d w"></i>drift fill</span>' : ""}</div></div>
      <div class="bt-chart" id="btCw">
        <svg id="btMain" viewBox="0 0 1000 300" role="img" aria-label="Portfolio and benchmark growth of 100">${main}</svg>
        <div class="label" style="padding:6px 8px 0">Excess vs benchmark</div>
        <svg id="btEx" viewBox="0 0 1000 110" role="img" aria-label="Cumulative excess return">${ex}</svg>
        <div class="bt-tip" id="btTip" hidden></div>
      </div>
    </section>
    <div class="grid2">
      <section class="panel"><div class="panel-h"><h2>Performance</h2><span class="label">${bs.years < 1 ? `annualised over ${bs.sessions - 1} sessions` : ""}</span></div>
        <div class="scroll"><table class="bt-cmp"><thead><tr><th></th><th class="n">Portfolio</th><th class="n">${esc(code)}</th><th class="n">Diff</th></tr></thead><tbody>
        ${row("Total return", spct(p.total), spct(b.total), spp(bs.excess), tone(bs.excess))}
        ${row("Annualised return", spct(p.annualised), spct(b.annualised), spp(p.annualised - b.annualised), tone(p.annualised - b.annualised))}
        ${row("Volatility, annualised", pct(p.vol), pct(b.vol), spp(p.vol - b.vol))}
        ${row(`Sharpe, rf ${pct(rf, 1)}`, n2(shp(p)), n2(shp(b)), shp(p) !== null && shp(b) !== null ? (shp(p) - shp(b) >= 0 ? "+" : "−") + Math.abs(shp(p) - shp(b)).toFixed(2) : "n/a")}
        ${row("Max drawdown", mpct(p.max_drawdown), mpct(b.max_drawdown), spp(p.max_drawdown - b.max_drawdown), tone(p.max_drawdown - b.max_drawdown))}
        </tbody></table></div></section>
      <section class="panel"><div class="panel-h"><h2>Relative and trading</h2></div>
        <div class="scroll"><table class="bt-cmp"><tbody>
        ${kv("Tracking error, annualised", pct(bs.tracking_error))}${kv("Information ratio", n2(bs.information_ratio))}${kv(`Beta to ${esc(code)}`, n2(bs.beta))}
        ${kv("Rebalances", `${bs.n_calendar} calendar · ${reb.drift_threshold != null ? bs.n_drift + " drift" : "drift off"}`)}
        ${kv("Turnover after inception, one-way", pct(bs.turnover, 1))}${kv("Trading costs, incl. inception", (bs.cost*1e4).toFixed(1) + " bps")}
        </tbody></table></div></section>
    </div>
    <section class="panel"><div class="panel-h"><h2>Rebalance log</h2><span class="label">decision at the close · fill ${c.lag_sessions} session${c.lag_sessions === 1 ? "" : "s"} later</span></div>
      <div class="scroll"><table><thead><tr><th>Decision</th><th>Fill</th><th>Trigger</th><th class="n">Group drift</th><th class="n">Turnover</th><th class="n">Cost</th><th class="n">Holdings</th><th>Note</th></tr></thead><tbody>
      ${r.rebalances.map(e => `<tr><td class="mono">${e.decision}</td><td class="mono">${e.fill}</td>
        <td>${e.trigger === "drift" ? '<span class="pill warn">drift</span>' : e.trigger === "calendar" ? '<span class="pill info">calendar</span>' : '<span class="pill off">inception</span>'}</td>
        <td class="n">${e.drift === null ? "—" : pct(e.drift)}</td><td class="n">${pct(e.turnover, 1)}</td><td class="n">${(e.cost*1e4).toFixed(1)} bps</td>
        <td class="n"><span class="pill xs ${e.in_range ? "ok" : "warn"}">${e.holdings} ${e.in_range ? "in" : "OUTSIDE"} ${h.min}–${h.max}</span></td>
        <td>${e.gone ? `<span class="err">no priced name: ${esc(e.gone)}</span>` : ""}</td></tr>`).join("")}
      </tbody></table></div></section>
    <section class="panel"><div class="panel-h"><h2>Holdings at the end</h2><span class="label">drifted weight on ${r.end} vs the target filled ${last ? last.fill : "—"}</span></div>
      <div class="scroll"><table><thead><tr><th>Ticker</th><th>Group</th><th class="n">Weight</th><th class="n">Target</th><th class="n">Gap</th></tr></thead><tbody>
      ${r.holdings_end.map(x => `<tr><td class="mono">${esc(x.t)}</td><td>${esc(x.group)}</td><td class="n">${pct(x.w)}</td><td class="n">${pct(x.target)}</td><td class="n ${tone(x.w - x.target)}">${spp(x.w - x.target)}</td></tr>`).join("")}
      </tbody></table></div></section>
    </div>`;
  }

  const mand = r && r.name === BT.name ? {reb:r.rebalance, h:r.holdings_range, cons:r.constraints, tac:r.tactical, anchor:r.anchor}
    : summ?.statement ? {reb:summ.statement.rebalance, h:summ.statement.holdings, cons:summ.constraints_on.length ? summ.constraints_on.join(", ") : "off", tac:"see the Book step", anchor:summ.anchor} : null;
  return `<div class="page">
    <div class="head"><div><div class="crumbs">portfolio/${esc(BT.name)}/backtest_config.json</div><h1>Backtest</h1></div>
      ${r ? `<span class="label">${r.start} → ${r.end} vs ${esc(r.benchmarks[BT.bench] ? BT.bench : r.benchmark)}</span>` : ""}</div>
    <section class="panel"><div class="panel-b bt-ctl">
      <div class="field"><label for="btName">Portfolio</label><select id="btName">${names.map(n => `<option value="${esc(n)}" ${n === BT.name ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
      <div class="field"><label for="btStart">Start date</label>
        <div class="inline"><input type="date" id="btStart" min="${ss[0]}" max="${ss.at(-1)}" value="${BT.start ?? ss[0]}" style="max-width:170px">
          <div class="seg" role="group" aria-label="Start presets">${[["first","Earliest"],["q2","Q2"],["q3","Q3"],["m1","1M"]].map(([k, l]) => `<button type="button" data-preset="${k}">${l}</button>`).join("")}</div></div>
        <span class="bt-snap">${snapped ? `first session ${snapped} · ${ss.length - ss.indexOf(snapped)} sessions to ${ss.at(-1)}` : "after the last session"}</span></div>
      <div class="field"><label>Benchmark</label><div class="seg" role="group" aria-label="Benchmark">${codes.map(c => `<button type="button" data-bench="${esc(c)}" aria-pressed="${c === BT.bench}">${esc(c)}</button>`).join("") || '<span class="err">no benchmark in market.db</span>'}</div></div>
      <div class="field"><button type="button" class="btn ${dirty ? "primary" : ""}" id="btRun" ${BT.running || !d ? "disabled" : ""}>${BT.running ? "Running..." : dirty ? "Run backtest" : "Up to date"}</button></div>
    </div></section>
    ${r && dirty && !BT.running ? `<div class="dirtybar"><span>Settings changed. Results below show the last run.</span><button class="btn sm primary" id="btRun2">Run backtest</button></div>` : ""}
    ${BT.err ? `<p class="note bad" style="white-space:pre-wrap">FAIL  ${esc(BT.err)}</p>` : ""}
    ${!r && !BT.err ? `<p class="note">${BT.running ? "Running the engine..." : "Pick a start date and benchmark, then Run backtest."}</p>` : ""}
    ${results}
    <div class="grid2">
      <section class="panel"><div class="panel-h"><h2>Mandate</h2><span class="pill plain off">statement.json · constraints.json</span></div>
        <div class="panel-b">${mand ? `<dl class="kv bt-mand">
          <dt>Rebalance</dt><dd>${mand.reb.frequency ? `${FREQ[mand.reb.frequency]} (${mand.reb.frequency}), first session of the period` : "No calendar; inception and drift only"}</dd>
          <dt>Drift threshold</dt><dd>${mand.reb.drift_threshold != null ? `${pct(mand.reb.drift_threshold, 1)} at group grain, ½ Σ |gap|` : "Off"}</dd>
          <dt>Holdings range</dt><dd>${mand.h.min}–${mand.h.max}, flagged per rebalance</dd>
          <dt>Constraints</dt><dd>${esc(mand.cons)}</dd>
          <dt>Tactical overlay</dt><dd>${esc(mand.tac)}</dd>
          <dt>Book</dt><dd>as of anchor ${esc(mand.anchor ?? "not forked")}</dd>
        </dl><p class="note" style="margin-top:12px">Edit the mandate in the portfolio's Statement step; the next run reads it.</p>` : '<p class="note warn">No statement.json; the backtest needs its rebalance mandate.</p>'}</div></section>
      <section class="panel"><div class="panel-h"><h2>Backtest settings</h2><span class="mono" style="font-size:11.5px;color:var(--ink-3)">backtest_config.json</span></div>
        ${BT.cfgErr ? `<p class="note bad" style="margin:12px 16px 0">${esc(BT.cfgErr)}. Showing defaults; Save replaces the file.</p>` : ""}
        <div class="panel-b bt-cfg">
          ${field("brokerage_bps", "Brokerage, bps per side", 1)}${field("sell_tax_bps", "Sell tax, bps", 1)}
          ${field("lag_sessions", "Fill lag, sessions", 1, "0–5")}${field("risk_free_rate", "Risk-free rate, % a year", 0.1, "Sharpe only; applies without a run")}
        </div>
        <div class="panel-f"><span class="label" style="margin-right:auto">${cfgDirty ? "unsaved" : "saved"}</span>
          <button class="btn" id="btReset" ${cfgDirty ? "" : "disabled"}>Revert</button>
          <button class="btn primary" id="btSave" ${cfgDirty ? "" : "disabled"}>Save settings</button></div>
        ${consoleBox("bt")}
      </section>
    </div>
    <section class="panel"><div class="panel-h"><h2>Read before trusting the numbers</h2></div><div class="panel-b">
      <ul class="bt-caveats">
        <li><b>Return basis differs.</b> The portfolio is total return on adjusted prices, cash dividends reinvested on the ex-date. The benchmarks are price indexes, so the portfolio leads them by roughly the dividend yield before any skill.</li>
        <li><b>Today's book, held backwards.</b> Group map, ratings, deleted and invalidated names are as of the anchor and apply to every past date: survivorship and look-ahead bias flatter every level. Read excess and spreads first.</li>
        <li><b>Rebalances follow the mandate.</b> A scheduled rebalance re-derives budgets from that session's free float × official close and re-solves the constraints; a drift rebalance restores the last target.</li>
        <li><b>Cash until the first fill.</b> The benchmark counts from the start close; the portfolio buys at the close after the fill lag.</li>
      </ul></div></section>
  </div>`;
}
function bindBacktest(){
  if (!$("#btName")) return;
  $("#btName").onchange = e => { btLoad(e.target.value); };
  $("#btStart").onchange = e => { BT.start = e.target.value || null; render(); };
  $$("[data-preset]").forEach(b => b.onclick = () => {
    const ss = S.state.sessions, y = ss.at(-1).slice(0, 4);
    const m1 = new Date(ss.at(-1) + "T00:00:00Z"); m1.setUTCMonth(m1.getUTCMonth() - 1);
    BT.start = {first:null, q2:`${y}-04-01`, q3:`${y}-07-01`, m1:m1.toISOString().slice(0, 10)}[b.dataset.preset];
    render();
  });
  $$("[data-bench]").forEach(b => b.onclick = () => { BT.bench = b.dataset.bench; render(); });
  $$("[data-cfg]").forEach(i => i.onchange = () => {
    const k = i.dataset.cfg; let v = parseFloat(i.value);
    if (!isFinite(v) || v < 0){ render(); return; }
    if (k === "lag_sessions") v = Math.min(5, Math.round(v));
    if (k === "risk_free_rate") v = Math.min(0.99, v / 100);
    BT.draft[k] = v; render();
  });
  $$("[data-cfg]").forEach(i => i.onkeydown = e => { if (e.key === "Enter"){ i.onchange(); if (i.dataset.cfg !== "risk_free_rate") btRun(); } });
  $("#btStart").onkeydown = e => { if (e.key === "Enter") btRun(); };
  ["#btRun", "#btRun2"].forEach(s => { if ($(s)) $(s).onclick = btRun; });
  if ($("#btReset")) $("#btReset").onclick = () => { BT.draft = {...BT.saved}; render(); };
  if ($("#btSave")) $("#btSave").onclick = () => action("bt", "PUT", `/api/p/${encodeURIComponent(BT.name)}/backtest_config`,
    {config:BT.draft, version:BT.version}, async res => { if (res.ok){ BT.saved = {...BT.draft}; BT.version = res.version; BT.cfgErr = null; } });
  const svg = $("#btMain");
  if (svg && BT.cur){
    const move = ev => {
      const c = BT.cur, rect = svg.getBoundingClientRect(), wrap = $("#btCw").getBoundingClientRect();
      const vx = (ev.clientX - rect.left) / rect.width * 1000, n = c.P.length;
      const i = Math.max(0, Math.min(n - 1, Math.round((vx - CM.l) / (1000 - CM.l - CM.r) * (n - 1))));
      ["#xh", "#xh2"].forEach(s => { const l = $(s); l.setAttribute("x1", c.x(i)); l.setAttribute("x2", c.x(i)); l.setAttribute("visibility", "visible"); });
      const tip = $("#btTip");
      tip.innerHTML = `<b>${c.dates[i]}</b><span>Portfolio</span><span>${c.P[i].toFixed(2)}</span><span>${esc(c.code)}</span><span>${c.B[i].toFixed(2)}</span><span>Excess</span><span>${spct(c.E[i])}</span>`;
      tip.hidden = false;
      tip.style.left = Math.min(rect.left - wrap.left + c.x(i) / 1000 * rect.width + 12, wrap.width - tip.offsetWidth - 8) + "px";
      tip.style.top = "14px";
    };
    const leave = () => { $("#btTip").hidden = true; ["#xh", "#xh2"].forEach(s => $(s).setAttribute("visibility", "hidden")); };
    [svg, $("#btEx")].forEach(el => { el.onmousemove = move; el.onmouseleave = leave; });
  }
}

/* ---------- render ---------- */
function render(){
  /* keep focus, caret and scroll in a text field across re-renders */
  const a = document.activeElement;
  const keep = a && a.id && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT")
    ? {id:a.id, value:a.value, start:a.selectionStart, end:a.selectionEnd, typed:a.type === "text" || a.tagName === "TEXTAREA"} : null;
  const y = window.scrollY;
  renderNav();
  const m = $("#main");
  if (!S.state){ m.innerHTML = `<p style="color:var(--ink-3)">Loading...</p>`; return; }
  if (S.page === "data"){ m.innerHTML = renderData(); bindData(); }
  else if (S.page === "list"){ m.innerHTML = renderList(); bindList(); }
  else if (S.page === "new"){ m.innerHTML = renderNew(); bindNew(); }
  else if (S.page === "backtest"){ m.innerHTML = renderBacktest(); bindBacktest(); }
  else { m.innerHTML = renderFlow(); bindFlow(); }
  m.classList.toggle("busy", S.busy);
  if (keep){
    const el = document.getElementById(keep.id);
    if (el){
      if (keep.typed){ el.value = keep.value; try { el.setSelectionRange(keep.start, keep.end); } catch {} }
      el.focus({preventScroll:true});
      if (keep.typed && el.dataset.tsearch !== undefined && el.value) el.dispatchEvent(new Event("input"));
    }
  }
  window.scrollTo({top:y});
}

(async () => { await refreshState(); render(); })();
