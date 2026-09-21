/* PM-Mike Desk. Every number comes from the server (scr/app.py -> scr/*.py).
   The page keeps unsaved Allocation and Constraints edits in memory and
   previews them through /preview, priced as of the effective date; nothing
   reaches disk until Save. Setup (1-3) is the mandate; the loop (4-8) is
   allocate, build and record, test, monitor, allocate again. */
"use strict";

/* Rating tiers mirror target.TIERS. The bounds here only keep inputs in range;
   target.py checks range, floor, net and budget on every preview and build. */
const TIER = {"1":3, "2":6, "3":9};
const NET_TOL = 1e-3;
const side = r => r.startsWith("OW") ? "ow" : r.startsWith("UW") ? "uw" : r.toLowerCase();
const tierRange = r => r === "NO" || r === "AV" ? [0, 0] : r.startsWith("OW") ? [0, TIER[r[2]]] : [-TIER[r[2]], 0];
/* The rating spectrum, UW3 to OW3: [rating, fill, ink]. AV sits in the middle and
   belongs to every range. NO is the only red on the page. */
const SPEC = [["UW3","#003366","#FFFFFF"], ["UW2","#0055CC","#FFFFFF"], ["UW1","#66B2FF","#0B2545"],
              ["AV","#FFF176","#4A3F00"],
              ["OW1","#C8E6C9","#14421F"], ["OW2","#66BB6A","#08260F"], ["OW3","#2E7D32","#FFFFFF"]];
const AV_AT = 3;
const COVER_THIN = 1 / 3;     // below this the group's weight rests on a sliver of it
/* Cell r is lit when it lies between AV and the selected rating, inclusive. */
function inBand(sel, r){
  if (sel === "NO") return false;
  if (r === "AV") return true;
  const i = SPEC.findIndex(c => c[0] === r), s = SPEC.findIndex(c => c[0] === sel);
  return s < AV_AT ? i >= s && i < AV_AT : s > AV_AT && i <= s && i > AV_AT;
}
const fpp = (v, d=2) => (v > 0.0000001 ? "+" : v < -0.0000001 ? "−" : "") + Math.abs(v).toFixed(d);
const FREQ = {"2W":"Every 2 weeks","1M":"Monthly","1Q":"Quarterly"};
const EPS = 1e-12;
const STEPS = [
  {k:"statement", t:"Statement", s:"Mandate and range", blk:"Setup"},
  {k:"rebalancing", t:"Rebalancing", s:"When to trade", blk:"Setup"},
  {k:"constraints", t:"Constraints", s:"Budget and caps", blk:"Setup"},
  {k:"allocation", t:"Allocation", s:"Ratings and names", blk:"Loop"},
  {k:"target", t:"Target", s:"Build and record", blk:"Loop"},
  {k:"backtest", t:"Backtest", s:"Replay decisions", blk:"Loop"},
  {k:"monitor", t:"Monitor", s:"Drift, breach, flags", blk:"Loop"},
  {k:"decisions", t:"Decisions", s:"Recorded profiles", blk:"Loop", opt:true},
];
const STEP = k => STEPS.findIndex(s => s.k === k);
const PAIR = new Set(["allocation", "target"]);          // one page, two steps
const WIDE = new Set(["backtest", "monitor", "decisions"]); // full width, no preview aside

const S = {page:"data", port:null, step:0, state:null, P:null, preview:null,
           console:{}, confirmDel:null, confirmReset:false, busy:false, seq:0, scrollTo:null};

/* ---------- helpers ---------- */
const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct = (x, d=2) => (x*100).toFixed(d) + "%";
const bn = x => Math.round(x).toLocaleString("en-US");
const id = s => s.replace(/\W+/g,"_");
const short = n => n.replace(/^hsc_strat_/, "");
const today = () => new Date().toLocaleDateString("sv");

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
const FLAGNAME = {turnover:"21-day turnover", float_cap:"float cap", fol:"foreign ownership limit"};
const UNIT = {turnover:"% / day", float_cap:" bn VND", fol:"%"};
const flagText = f => f.why === "no data" ? `${FLAGNAME[f.screen]}: no data`
  : `${FLAGNAME[f.screen]} ${(+f.value).toPrecision(3)}${UNIT[f.screen]} below ${f.threshold}${UNIT[f.screen]}`;
function go(page, port=null, step=0){
  S.page = page; S.step = step; S.confirmDel = null; S.confirmReset = false;
  if (port && port !== S.port){ S.port = port; S.P = null; S.preview = null; S.console.flow = null; }
  render(); window.scrollTo({top:0});
  if (page === "flow" && !S.P) loadPortfolio();
  enterStep();
}
/* steps that fetch their own data when entered */
function enterStep(){
  if (S.page !== "flow" || !S.P) return;
  const k = STEPS[S.step].k;
  if (k === "backtest") btEnsure(S.P.name);
  if (k === "monitor" && !S.P.mon && !S.P.monLoading) loadMonitor();
}
async function refreshState(){ S.state = await api("GET", "/api/state"); }

/* ---------- portfolio model ---------- */
function buildU(uni){
  const u = {...uni, FC:{}, HOME:{}, TO:{}, FOL:{}, NEW:{}, byName:{}};
  u.groups.forEach(g => { u.byName[g.name] = g; g.members.forEach(m => {
    u.FC[m.t] = m.fcap; u.HOME[m.t] = g.name; u.TO[m.t] = m.to; u.FOL[m.t] = m.fol; if (m.new) u.NEW[m.t] = true; }); });
  return u;
}
function model(d, keep){
  const P = {name:d.name, d, statement:d.statement, screens:d.screens, errors:d.errors, versions:d.versions,
    u:null, book:{}, tac:{on:false, groups:[]}, cons:null, defaultBook:!d.book,
    dirty:{book:false, cons:false}, mon:null, monLoading:false, decSel:keep?.decSel ?? null,
    decDraft:keep?.decDraft ?? {eff:today(), note:"", kind:"period"}};
  if (d.universe) P.u = buildU(d.universe);
  const c = d.constraints;
  P.cons = {active:{budget:c.active.budget_pp},
            sector:{on:c.sector.on, max:c.sector.max, per:{...c.sector.per_group},
                    mode:Object.keys(c.sector.per_group).length ? "individual" : "universal"},
            stock:{on:c.stock.on, max:c.stock.max},
            large:{on:c.large.on, T:c.large.threshold, L:c.large.aggregate}};
  if (d.book){
    Object.entries(d.book.groups).forEach(([g, b]) => { P.book[g] = {rating:b.rating, pp:b.pp, included:new Set(b.investable), autoNo:false}; });
    P.tac = {on:d.book.tactical.on, groups:d.book.tactical.groups.map(g => ({name:g.name, rating:g.rating, pp:g.pp, tickers:[...g.members], autoNo:g.rating === "NO" && !g.members.length}))};
  }
  ensureGroups(P);
  /* keep unsaved edits across a reload */
  if (keep && keep.dirty.book){ P.book = keep.book; P.tac = keep.tac; P.dirty.book = true; if (keep.u) P.u = keep.u; }
  if (keep && keep.dirty.cons){ P.cons = keep.cons; P.dirty.cons = true; }
  return P;
}
/* A group of the universe the book does not have: the default book holds it AV
   with every name; a saved book rates it NO (D59, a gained group). */
function ensureGroups(P){
  if (!P.u) return;
  P.u.groups.forEach(g => { if (!P.book[g.name]) P.book[g.name] = P.defaultBook
    ? {rating:"AV", pp:0, included:new Set(g.members.map(m => m.t)), autoNo:false}
    : {rating:"NO", pp:0, included:new Set(), autoNo:false, gained:true}; });
}
async function loadPortfolio(opts={}){
  const name = S.port;
  const eff = S.P?.decDraft?.eff;
  const d = await api("GET", `/api/p/${encodeURIComponent(name)}${eff ? `?as_of=${eff}` : ""}`);
  if (S.port !== name) return;
  if (!d || !d.name){ S.P = null; S.console.flow = `FAIL  could not load ${name}\n`; render(); return; }
  S.P = model(d, opts.discard ? {decDraft:S.P?.decDraft, decSel:S.P?.decSel, dirty:{book:false, cons:false}} : S.P);
  normalize(S.P); render(); schedulePreview(); enterStep();
}
const ready = P => P && P.u && Object.keys(P.book).length;
function claims(P){ const c = {}; if (P.tac.on) P.tac.groups.forEach(tg => tg.tickers.forEach(t => { c[t] = tg.name; })); return c; }
function survivors(P, g){ const c = claims(P); const bk = P.book[g.name]; return g.members.filter(m => bk.included.has(m.t) && !c[m.t]); }
/* investable names of a group that do not trade on the session: kept in the book, dropped at evaluation */
const notTrading = (P, g) => [...P.book[g].included].filter(t => P.u.HOME[t] === undefined).sort();
/* groups in the book the session's universe lacks: dropped with their pp at evaluation */
const lostGroups = P => Object.keys(P.book).filter(g => !P.u.byName[g]).sort();
/* How much of a group its kept names actually are. The group's neutral is its
   float-cap weight in the universe, so names left out still support it and the
   survivors carry their share: cover says how thin that is. A name claimed by
   a tactical group leaves with its budget, so it leaves both sides. Display
   only -- no weight depends on this. */
function coverage(P, g, x){
  const c = claims(P);
  const base = g.members.filter(m => !c[m.t]);
  const den = base.reduce((a, m) => a + m.fcap, 0);
  const surv = survivors(P, g);
  const num = surv.reduce((a, m) => a + m.fcap, 0);
  if (!den || !surv.length) return null;
  const top = surv.reduce((a, m) => m.fcap > a.fcap ? m : a, surv[0]);
  return {cover:num / den, kept:surv.length, of:base.length, top:top.t,
          topw: x ? x.w * top.fcap / num : null};
}
/* auto-NO: an empty investable box is NO; a name arriving in an auto-NO group makes it AV again */
function normalize(P){
  if (!ready(P)) return;
  P.u.groups.forEach(g => {
    const bk = P.book[g.name]; const n = survivors(P, g).length;
    if (n === 0 && bk.rating !== "NO"){ bk.rating = "NO"; bk.pp = 0; bk.autoNo = true; }
    else if (n > 0 && bk.autoNo){ bk.rating = "AV"; bk.autoNo = false; }
  });
  P.tac.groups.forEach(tg => {
    const n = tg.tickers.filter(t => P.u.FC[t] !== undefined).length;
    if (!n && tg.rating !== "NO"){ tg.rating = "NO"; tg.pp = 0; tg.autoNo = true; }
    else if (n && tg.autoNo){ tg.rating = "AV"; tg.autoNo = false; }
  });
}
/* The spec as book.json holds it. A name the session puts in another group
   (regrouped in the map) leaves this group; a name not trading stays. */
function bookSpec(P){
  const groups = {};
  Object.entries(P.book).forEach(([g, b]) => { groups[g] = {rating:b.rating, pp:b.rating==="NO" ? 0 : b.pp,
    investable:[...b.included].filter(t => P.u.HOME[t] === undefined || P.u.HOME[t] === g).sort()}; });
  return {groups, tactical:{on:P.tac.on, groups:P.tac.groups.map(g => ({name:g.name, rating:g.rating, pp:g.rating==="NO" ? 0 : g.pp, members:g.tickers}))}};
}
/* the rows the target reads: every universe group, tactical groups while the overlay is on */
const activeRows = P => [...P.u.groups.map(g => P.book[g.name]), ...(P.tac.on ? P.tac.groups : [])];
function activeSums(P){
  const rows = activeRows(P);
  const net = rows.reduce((a, b) => a + b.pp, 0), used = rows.reduce((a, b) => a + Math.abs(b.pp), 0) / 2;
  const budget = P.cons.active.budget;
  return {net, used, budget, netOk:Math.abs(net) <= NET_TOL, budgetOk:used <= budget + NET_TOL};
}
function setRating(bk, r){
  const [lo, hi] = tierRange(r);
  bk.rating = r; bk.autoNo = false; bk.pp = Math.min(hi, Math.max(lo, bk.pp));
}
/* Balance to zero: scale the larger side (OW or UW) down to the smaller; a
   scale-down never breaks a range, the floor or the budget. Rounded to 0.01 pp. */
function balance(P){
  const rows = activeRows(P);
  const pos = rows.filter(b => b.pp > 0), neg = rows.filter(b => b.pp < 0);
  const sp = pos.reduce((a, b) => a + b.pp, 0), sn = -neg.reduce((a, b) => a + b.pp, 0);
  if (Math.abs(sp - sn) <= NET_TOL) return;
  P.undoPP = rows.map(b => [b, b.pp]);
  const [big, k] = sp > sn ? [pos, sn / sp] : [neg, sp / sn];
  big.forEach(b => { b.pp = Math.round(b.pp * k * 100) / 100; });
  const net = Math.round(rows.reduce((a, b) => a + b.pp, 0) * 100) / 100;
  if (net){ const m = big.reduce((a, b) => Math.abs(b.pp) >= Math.abs(a.pp) ? b : a); m.pp = Math.round((m.pp - net) * 100) / 100; }
}
function consSpec(P){
  const c = P.cons;
  const known = new Set([...(P.u ? P.u.groups.map(g=>g.name) : []), ...(P.tac.on ? P.tac.groups.map(g=>g.name) : [])]);
  const per = {};
  if (c.sector.mode === "individual") Object.entries(c.sector.per).forEach(([g, v]) => { if (known.has(g)) per[g] = v; });
  return {active:{budget_pp:c.active.budget},
          sector:{on:c.sector.on, max:c.sector.max, per_group:per},
          stock:{on:c.stock.on, max:c.stock.max},
          large:{on:c.large.on, threshold:c.large.T, aggregate:c.large.L}};
}
let previewTimer = null;
function schedulePreview(){
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 120);
}
/* The preview is priced as of the effective date: its universe replaces the
   page's when the session moves, keeping the book's edits. */
async function runPreview(){
  const P = S.P; if (!ready(P)) { S.preview = null; return; }
  const seq = ++S.seq;
  const res = await api("POST", `/api/p/${encodeURIComponent(P.name)}/preview`,
                        {...bookSpec(P), constraints:consSpec(P), as_of:P.decDraft.eff || null});
  if (seq !== S.seq || S.P !== P) return;
  if (res?.ok && res.universe && res.universe.as_of !== P.u.as_of){
    P.u = buildU(res.universe); ensureGroups(P); normalize(P);
  }
  S.preview = res; render();
}
const edited = (what, keepUndo=false) => { S.P.dirty[what] = true; if (!keepUndo) S.P.undoPP = null; normalize(S.P); render(); schedulePreview(); };

/* ---------- nav ---------- */
function renderNav(){
  const st = S.state; const names = st ? st.portfolios.map(p=>p.name) : [];
  const last = st?.sessions?.at(-1);
  $("#nav").innerHTML = `
    <div class="label">Workspace</div>
    <button data-go="data" aria-current="${S.page==="data"}">Market data ${last ? `<span class="pill ok plain num" style="font-size:11px">${last.slice(5)}</span>` : ""}</button>
    <button data-go="list" aria-current="${S.page==="list"||S.page==="new"}">Portfolios <span class="num" style="font-size:12px;color:var(--ink-3)">${names.length}</span></button>
    ${names.map(n => `<button class="sub" data-port="${esc(n)}" aria-current="${S.page==="flow" && S.port===n}">${esc(short(n))}</button>`).join("")}`;
  $("#railLatest").textContent = last ? `latest session ${last}` : "";
  $$("#nav [data-go]").forEach(b => b.onclick = () => go(b.dataset.go));
  $$("#nav [data-port]").forEach(b => b.onclick = () => go("flow", b.dataset.port, S.port===b.dataset.port ? S.step : STEP("allocation")));
}

/* ---------- page: data ---------- */
function renderData(){
  const st = S.state, m = st.market, u = st.universe;
  const checks = dataChecks(st);
  const nWarn = checks.filter(c => c.lvl !== "ok").length;
  const maxW = u?.groups?.length ? Math.max(...u.groups.map(g => g.weight)) : 1;
  return `<div class="page wide">
    <div class="head"><div><div class="crumbs">data/market.db</div><h1>Market data</h1></div>
      ${m.db ? `<span class="pill ok">${st.sessions.length} sessions to ${st.sessions.at(-1)}</span>` : `<span class="pill bad">No database</span>`}</div>
    <div class="flow"><div class="page">
    <div class="grid2">
      <section class="panel">
        <div class="panel-h"><h2>Database</h2><button class="btn primary" id="runIngest">Rebuild database</button></div>
        <div class="panel-b stack">
          ${m.db ? `<dl class="kv"><dt>Last built</dt><dd>${m.rebuilt}</dd></dl>
          <div class="dbsum">
            <div><div class="label">Stock</div><dl class="kv">
              <dt>Tickers</dt><dd>${m.tickers}</dd>
              <dt>Sessions</dt><dd>${st.sessions.length}</dd>
              <dt>From</dt><dd>${st.sessions[0]}</dd>
              <dt>To</dt><dd>${st.sessions.at(-1)}</dd>
            </dl></div>
            <div><div class="label">Benchmarks</div>${benchSummary(m.benchmarks ?? [])}</div>
          </div>` : `<p class="note bad">No database. Rebuild it from the drops in <span class="mono">data/fiinpro/</span>.</p>`}
          <div class="scroll"><table>
            <thead><tr><th>Inputs</th><th class="n">MB</th><th>State</th></tr></thead>
            <tbody>${m.drops.length ? m.drops.map(dp => `<tr><td class="mono">${esc(dp.file)}</td><td class="n">${dp.mb}</td><td>${dp.newer_than_db ? '<span class="pill xs warn">newer than database</span>' : '<span class="pill xs ok">loaded</span>'}</td></tr>`).join("") : `<tr><td colspan="3" class="empty">No drops</td></tr>`}</tbody>
          </table></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-h"><h2>Universe</h2><span class="label">${u?.as_of ? `latest session ${u.as_of}` : ""}</span></div>
        <div class="panel-b stack">
          ${u?.error ? `<p class="note bad">${esc(u.error)}</p>` : ""}
          <dl class="kv">
            <dt>Group map</dt><dd>index/group_map_live.csv${u?.groups ? ` · ${u.groups.length} groups, ${u.tickers} names` : ""}${st.group_map.edited ? ` · edited ${st.group_map.edited}` : ""}</dd>
            ${u?.median_to != null ? `<dt>21d turnover</dt><dd>median ${u.median_to.toFixed(3)}% of float cap / day</dd>` : ""}
            ${u?.groups ? `<dt>FOL</dt><dd>index/fol.csv · ${u.fol} of ${u.tickers} names have a limit</dd>` : ""}
            ${u?.no_float_cap?.length ? `<dt>No float cap</dt><dd>${u.no_float_cap.map(esc).join(" ")} (left out)</dd>` : ""}
          </dl>
          <p class="note">Every calculation prices its own session from market.db, the group map and fol.csv. An edit to either file applies on the next preview; there is nothing to rebuild and no portfolio to re-fork.</p>
        </div>
      </section>
    </div>
    ${u?.groups ? `<section class="panel">
      <div class="panel-h"><h2>Float-cap weights, ${u.as_of}</h2><span class="label">free float × official close · ${u.groups.length} groups</span></div>
      <div class="scroll"><table>
        <thead><tr><th>Group</th><th class="n">Names</th><th class="n">Float cap, bn VND</th><th class="n">Weight</th><th style="width:34%"></th></tr></thead>
        <tbody>${u.groups.map(g => `<tr>
          <td>${esc(g.name)}</td><td class="n">${g.n}</td><td class="n">${bn(g.fcap)}</td><td class="n">${pct(g.weight)}</td>
          <td><div class="bar"><div class="t" style="width:${g.weight/maxW*100}%;background:var(--accent)"></div></div></td></tr>`).join("")}</tbody>
      </table></div>
    </section>` : ""}
    </div>
    <aside class="aside">
      <section class="panel">
        <div class="panel-h"><h2>Checks</h2>${nWarn ? `<span class="pill warn">${nWarn} to review</span>` : '<span class="pill ok">all clear</span>'}</div>
        <ul class="checks">${checks.map(c => `<li class="${c.lvl}"><i aria-hidden="true"></i><div><b>${c.title}</b>${c.body ? `<div class="cb">${c.body}</div>` : ""}</div></li>`).join("")}</ul>
      </section>
      ${S.console.ingest ? `<section class="panel">
        <div class="panel-h"><h3>Last run · Rebuild database</h3></div>
        ${consoleBox("ingest")}
      </section>` : ""}
    </aside>
    </div>
  </div>`;
}

/* benchmark names, then sessions/from/to once when every index shares them */
function benchSummary(bs){
  if (!bs.length) return '<p class="empty">No index drop loaded</p>';
  const same = bs.every(b => b.sessions === bs[0].sessions && b.from === bs[0].from && b.to === bs[0].to);
  const span = b => `<dt>Sessions</dt><dd>${b.sessions}</dd><dt>From</dt><dd>${b.from}</dd><dt>To</dt><dd>${b.to}</dd>`;
  return same
    ? `<ul class="bnames">${bs.map(b => `<li>${esc(b.code)}</li>`).join("")}</ul><dl class="kv">${span(bs[0])}</dl>`
    : bs.map(b => `<div class="bnames">${esc(b.code)}</div><dl class="kv">${span(b)}</dl>`).join("");
}

/* Data-tab health checks, worst first: each {lvl: ok|warn|bad, title, body} */
function dataChecks(st){
  const m = st.market, out = [];
  if (!m.db) return [{lvl:"bad", title:"No database", body:"Rebuild database from the drops in <span class=\"mono\">data/fiinpro/</span>."}];
  const last = st.sessions.at(-1);

  const newer = m.drops.filter(d => d.newer_than_db);
  out.push(newer.length
    ? {lvl:"warn", title:`${newer.length} drop${newer.length > 1 ? "s" : ""} newer than the database`, body:`<span class="mono">${newer.map(d => esc(d.file)).join(", ")}</span>. Rebuild database to load ${newer.length > 1 ? "them" : "it"}.`}
    : {lvl:"ok", title:`All ${m.drops.length} drops loaded`});

  const bs = m.benchmarks ?? [];
  const byKey = (list, key) => Object.values(list.reduce((a, b) => ((a[key(b)] ??= []).push(b), a), {}));
  const late = byKey(bs.filter(b => b.late), b => `${b.late}|${b.to}`);
  late.forEach(g => out.push({lvl:"warn", title:"Benchmarks run past the stock data",
    body:`<span class="mono">${g.map(b => esc(b.code)).join(", ")}</span> ${g.length > 1 ? "have" : "has"} ${g[0].late} session${g[0].late > 1 ? "s" : ""} after the last stock session <span class="mono">${last}</span> (index to <span class="mono">${g[0].to}</span>). Add a stock drop through ${g[0].to}; until then everything stops at ${last}.`}));
  const gaps = bs.filter(b => b.missing);
  if (gaps.length) out.push({lvl:"warn", title:"Benchmarks miss stock sessions",
    body: gaps.map(b => `<span class="mono">${esc(b.code)}</span> misses ${b.missing}`).join(" · ") + ". A backtest over those sessions falls back to a covered benchmark."});
  if (!bs.length) out.push({lvl:"warn", title:"No benchmark index loaded", body:"Add an index export (Index/Sector column) to <span class=\"mono\">data/fiinpro/</span>."});
  else if (!late.length && !gaps.length) out.push({lvl:"ok", title:"Benchmarks match the stock calendar", body:`${bs.map(b => esc(b.code)).join(", ")} to ${last}.`});

  const mc = m.mcap;
  if (mc?.rows) out.push({lvl:"warn", title:`Vendor market cap off on ${bn(mc.rows)} of ${bn(m.rows)} rows`,
    body:`<table class="mini"><thead><tr><th>Ticker</th><th class="n">Rows</th><th class="n">Worst</th></tr></thead><tbody>${mc.tickers.map(t => `<tr><td class="mono">${esc(t.ticker)}</td><td class="n">${t.rows}</td><td class="n">${pct(t.worst)}</td></tr>`).join("")}</tbody></table>${mc.names > mc.tickers.length ? `<div>+${mc.names - mc.tickers.length} more names.</div>` : ""}<div>QA only: weights use free float × official close, so targets are unaffected.</div>`});
  else if (mc) out.push({lvl:"ok", title:"Vendor market cap reconciles", body:"close × shares on every row."});

  out.push(st.unmapped.length
    ? {lvl:"bad", title:`${st.unmapped.length} ticker${st.unmapped.length > 1 ? "s" : ""} not in the group map`, body:`<span class="mono">${st.unmapped.map(esc).join(" ")}</span> trade on ${last}. Add them to <span class="mono">index/group_map_live.csv</span>; every calculation on that session fails until you do.`}
    : {lvl:"ok", title:"Group map covers the latest session"});

  const rank = {bad:0, warn:1, ok:2};
  return out.sort((a, b) => rank[a.lvl] - rank[b.lvl]);
}
function bindData(){
  $("#runIngest").onclick = () => action("ingest", "POST", "/api/run/ingest", {}, async () => { await refreshState(); });
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

/* ---------- page: list ---------- */
function renderList(){
  const cards = S.state.portfolios.map(p => {
    const st = p.statement;
    const del = S.confirmDel === p.name
      ? `<span class="confirm">Delete portfolio/${esc(p.name)}/ and its files? <button class="btn sm danger" data-del-yes="${esc(p.name)}">Delete</button><button class="btn sm" data-del-no>Keep</button></span>`
      : `<button class="btn ghost sm" data-del="${esc(p.name)}">Delete</button>`;
    const status = p.error ? `<span class="pill bad" title="${esc(p.error)}">blocked</span>` : p.n !== undefined ? rangePill(p.n, st?.holdings) : "";
    return `<div class="card">
      <button class="open" data-port="${esc(p.name)}">
      <div class="inline" style="justify-content:space-between;width:100%"><span class="nm">${esc(p.name)}</span>${status}</div>
      <p style="color:var(--ink-2);font-size:13px">${st?.approach ? esc(st.approach) : '<i style="color:var(--ink-3)">No approach written yet</i>'}</p>
      <div style="display:flex;flex-direction:column;gap:4px;width:100%">
        ${st ? `<div class="row"><span>Holdings range</span><span class="num">${st.holdings.min}–${st.holdings.max}</span></div>
        <div class="row"><span>Rebalance</span><span>${mandateText(st.rebalance)}</span></div>` : ""}
        <div class="row"><span>Constraints</span><span>${p.constraints_on.length ? p.constraints_on.join(", ") : "caps off"}</span></div>
        ${p.top ? `<div class="row"><span>Top group</span><span>${esc(p.top.group)} <span class="num">${pct(p.top.w,1)}</span></span></div>` : ""}
        <div class="row"><span>Last decision</span><span class="num">${p.last ? `${esc(p.last.id)} · ${esc(p.last.kind)}` : "none"}</span></div>
        <div class="row"><span>Last built</span><span class="num">${p.built_at ?? "never"}</span></div>
      </div></button>
      <div class="foot"><span class="mono" style="color:var(--ink-3)">${p.decisions} decision${p.decisions === 1 ? "" : "s"}</span>${del}</div>
    </div>`;
  }).join("");
  return `<div class="page">
    <div class="head"><div><div class="crumbs">portfolio/</div><h1>Portfolios</h1></div></div>
    <div class="cards">${cards}
      <button class="card new" id="newCard"><b>+ New portfolio</b><span style="font-size:13px">Write the statement, then set the allocation and record its inception.</span></button>
    </div>
    ${consoleBox("list")}
  </div>`;
}
function bindList(){
  $$(".open[data-port]").forEach(c => c.onclick = () => go("flow", c.dataset.port, STEP("allocation")));
  $("#newCard").onclick = () => go("new");
  $$("[data-del]").forEach(b => b.onclick = () => { S.confirmDel = b.dataset.del; render(); });
  $$("[data-del-no]").forEach(b => b.onclick = () => { S.confirmDel = null; render(); });
  $$("[data-del-yes]").forEach(b => b.onclick = () => {
    const n = b.dataset.delYes; S.confirmDel = null;
    action("list", "DELETE", `/api/p/${encodeURIComponent(n)}`, {yes:true}, async () => { if (S.port === n){ S.port = null; S.P = null; } await refreshState(); });
  });
}

/* ---------- statement form ---------- */
/* part: "new" (every field), "mandate" (Statement step), "rebalance" (Rebalancing step) */
function statementForm(st, part){
  const r = st.rebalance;
  const tol = r.breach_tolerance === undefined ? 0.10 : r.breach_tolerance;
  const mandate = `
    ${part === "new" ? `<div class="field full"><label for="f_name">Folder name</label><input type="text" id="f_name" placeholder="hsc_strat_dividend"><span class="hint">Lowercase letters, digits, underscore. Becomes portfolio/&lt;name&gt;/.</span></div>` : ""}
    <div class="field full"><label for="f_app">Approach strategy</label><textarea id="f_app" placeholder="e.g. Growth at a reasonable price; overweight consumption and private banks on credit recovery.">${esc(st.approach)}</textarea><span class="hint">Reference text for later review. Not used in calculations.</span></div>
    <div class="field full"><label for="f_scope">Scope universe</label><textarea id="f_scope" placeholder="e.g. VN100 constituents, HOSE only, excluding state banks.">${esc(st.scope)}</textarea></div>
    <div class="field"><label for="f_min">Holdings, minimum</label><input type="number" id="f_min" min="1" step="1" value="${st.holdings.min}"></div>
    <div class="field"><label for="f_max">Holdings, maximum</label><input type="number" id="f_max" min="1" step="1" value="${st.holdings.max}"><span class="hint">The Target step flags a holding count outside this range.</span></div>`;
  const reb = `
    <div class="field"><label>Rebalance on schedule</label>
      <div class="seg" role="group" aria-label="Frequency">${["none","2W","1M","1Q"].map(f => `<button type="button" data-freq="${f}" aria-pressed="${(r.frequency||"none")===f}">${f==="none"?"Off":f}</button>`).join("")}</div>
      <span class="hint">2W every two weeks · 1M monthly · 1Q quarterly. The first session of each period trades the book back to the standing target.</span></div>
    <div class="field"><label for="f_drift">Rebalance on drift, % of book</label>
      <div class="inline"><label class="toggle"><input type="checkbox" id="f_drift_on" ${r.drift_threshold!==null?"checked":""}> On</label>
      <input type="number" id="f_drift" min="1" max="99" step="1" value="${r.drift_threshold!==null ? Math.round(r.drift_threshold*100) : 8}" style="width:80px" ${r.drift_threshold===null?"disabled":""}></div>
      <span class="hint">Drift = ½ Σ |actual − target| across groups. Whichever trigger fires first.</span></div>
    <div class="field"><label for="f_breach">Rebalance on breach, % of the cap</label>
      <div class="inline"><label class="toggle"><input type="checkbox" id="f_breach_on" ${tol!==null?"checked":""}> On</label>
      <input type="number" id="f_breach" min="1" max="99" step="1" value="${tol!==null ? Math.round(tol*100) : 10}" style="width:80px" ${tol===null?"disabled":""}></div>
      <span class="hint">How far a Constraints-step cap may be broken before a trade is forced: 10% of a 20% stock cap trips at 22%. Off means caps never force one.</span></div>`;
  return `<div class="form">${part !== "rebalance" ? mandate : ""}${part !== "mandate" ? reb : ""}
    <div class="field full"><span class="err" id="f_err" role="alert"></span></div>
  </div>`;
}
function readStatementForm(base){
  const st = structuredClone(base);
  delete st.screens;
  if ($("#f_app")){
    st.approach = $("#f_app").value.trim(); st.scope = $("#f_scope").value.trim();
    st.holdings = {min: parseInt($("#f_min").value,10), max: parseInt($("#f_max").value,10)};
  }
  if ($("#f_drift_on")){
    const f = $("[data-freq][aria-pressed=true]").dataset.freq;
    st.rebalance = {frequency: f==="none" ? null : f, drift_threshold: $("#f_drift_on").checked ? (+$("#f_drift").value)/100 : null,
                    breach_tolerance: $("#f_breach_on").checked ? (+$("#f_breach").value)/100 : null};
  }
  return st;
}
function bindStatementForm(){
  if (!$("#f_drift_on")) return;
  $$("[data-freq]").forEach(b => b.onclick = () => $$("[data-freq]").forEach(x => x.setAttribute("aria-pressed", x===b)));
  $("#f_drift_on").onchange = e => $("#f_drift").disabled = !e.target.checked;
  $("#f_breach_on").onchange = e => $("#f_breach").disabled = !e.target.checked;
}
const DEFAULT_STATEMENT = {approach:"", scope:"", holdings:{min:20,max:30}, rebalance:{frequency:"1Q", drift_threshold:null, breach_tolerance:0.10}};
function renderNew(){
  return `<div class="page" style="max-width:760px">
    <div class="head"><div><div class="crumbs">portfolio/ · new</div><h1>New portfolio</h1></div></div>
    <section class="panel"><div class="panel-h"><h2>Portfolio statement</h2><span class="label">writes statement.json</span></div>
      <div class="panel-b">${statementForm(DEFAULT_STATEMENT, "new")}</div>
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
    if (res?.ok){ await refreshState(); go("flow", name, STEP("allocation")); }
  };
}

/* ---------- page: flow ---------- */
function stepStatus(P, k){
  const st = P.statement; const r = S.preview;
  const dec = P.d.decisions || [];
  if (!st && k !== "statement") return "";
  switch(k){
    case "statement": return st ? `${st.holdings.min}–${st.holdings.max} holdings` : "missing";
    case "rebalancing": return mandateText(st.rebalance);
    case "constraints": { const on = ["sector","stock","large"].filter(k => P.cons[k].on); return (on.length ? on.join(" · ") : "caps off") + (P.dirty.cons ? " · unsaved" : ""); }
    case "allocation": { if (!ready(P)) return ""; const a = activeSums(P); return `${a.used.toFixed(1)}/${a.budget} pp${a.netOk ? "" : " · net " + fpp(a.net, 1)}${P.tac.on ? " · tactical" : ""}${P.dirty.book ? " · unsaved" : ""}`; }
    case "target": return !r ? "" : r.ok ? `${r.n} names · ${r.as_of}` : "blocked";
    case "backtest": return BT.name === P.name && BT.res ? `total ${spct(BT.res.benchmarks[BT.res.benchmark].stats.portfolio.total)}` : dec.length ? `replays ${dec.length}` : "book.json";
    case "monitor": { const m = P.mon; if (!m) return dec.length ? "" : "no decision"; if (!m.ok) return "error"; if (!m.decision) return "no decision";
      return m.now ? `drift ${pct(m.now.drift, 1)}${m.now.breach ? " · breach" : ""}` : "too recent"; }
    case "decisions": return dec.length ? `${dec.length} · last ${dec[dec.length-1].effective}` : "none";
  }
}
function renderFlow(){
  const P = S.P;
  if (!P) return `<div class="page"><div class="head"><div><div class="crumbs">portfolio/${esc(S.port)}/</div><h1>${esc(S.port)}</h1></div></div><p style="color:var(--ink-3)">Loading...</p>${consoleBox("flow")}</div>`;
  const k = STEPS[S.step].k;
  const body = PAIR.has(k) ? stepAllocation(P) + stepTarget(P)
    : {statement:stepStatement, rebalancing:stepRebalancing, constraints:stepConstraints, backtest:renderBacktest,
       monitor:stepMonitor, decisions:stepDecisions}[k](P);
  const r = S.preview; const h = P.statement?.holdings;
  const n = r?.ok ? r.n : 0;
  const hi = h ? Math.max(h.max*1.4, n*1.1, 10) : 10;
  const top = r?.ok ? r.rows.filter(x=>x.w>0).slice(0,5) : [];
  const dirty = P.dirty.book || P.dirty.cons;
  const dec = P.d.decisions || [];
  const wide = WIDE.has(k);
  const aside = wide ? "" : `<aside class="aside">
        <section class="panel"><div class="panel-b" style="display:flex;flex-direction:column;gap:4px">
          <span class="label">Live target preview${r?.ok ? ` · ${r.as_of}` : ""}</span>
          ${!ready(P) ? `<p style="color:var(--ink-3);font-size:13px">No universe: rebuild the database.</p>` : !r ? `<p style="color:var(--ink-3);font-size:13px">Computing...</p>` : `
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
            <div class="bar" style="grid-column:1/-1;height:6px;min-width:0"><div class="t" style="top:0;bottom:0;width:${x.w*100}%;background:var(--${side(x.rating)})"></div></div></div>`).join("")}
        </div></section>` : ""}
        ${P.statement ? `<section class="panel"><div class="panel-b kv" style="font-size:12.5px">
          <dt>Rebalance</dt><dd style="font-family:var(--sans)">${mandateText(P.statement.rebalance)}</dd>
          <dt>Last decision</dt><dd>${dec.length ? `${esc(dec.at(-1).effective)} ${esc(dec.at(-1).kind)}` : "none"}</dd>
          <dt>Constraints</dt><dd style="font-family:var(--sans)">${esc(stepStatus(P,"constraints"))}</dd>
        </div></section>` : ""}
      </aside>`;
  return `<div class="page${wide ? " wide" : ""}">
    <div class="head"><div><div class="crumbs">portfolio/${esc(P.name)}/</div><h1>${esc(P.name)}</h1></div>
      <div class="inline"><button class="btn ghost" id="reload" title="Re-read the files on disk; unsaved edits stay in the page">Reload</button><button class="btn ghost" id="toList">All portfolios</button></div></div>
    ${P.errors.length ? `<p class="note bad">${P.errors.map(esc).join("<br>")}</p>` : ""}
    <nav class="steps" aria-label="Portfolio flow">${STEPS.map((s,i) => `<button data-step="${i}" class="blk-${s.blk.toLowerCase()}${i === STEP("allocation") ? " loop-start" : ""}${PAIR.has(s.k) && PAIR.has(k) && i !== S.step ? " pair" : ""}" aria-current="${i===S.step?"step":"false"}">
      <span class="i">${s.blk} ${i+1}${s.opt?'<span class="opt">optional</span>':""}</span><span class="t">${s.t}</span><span class="s">${esc(stepStatus(P, s.k))}</span></button>`).join("")}</nav>
    ${dirty ? `<div class="dirtybar"><span>Unsaved edits in ${[P.dirty.book && "Allocation", P.dirty.cons && "Constraints"].filter(Boolean).join(" and ")}. The preview shows them; the files on disk do not have them yet.</span>
      <span class="inline"><button class="btn sm" id="discardAll">Discard</button><button class="btn sm primary" id="saveAll">Save</button></span></div>` : ""}
    <div class="${wide ? "flow-wide" : "flow"}">
      <div style="display:flex;flex-direction:column;gap:16px;min-width:0">${body}
        ${consoleBox("flow")}
        <div class="inline" style="justify-content:space-between">
          <button class="btn" id="prev" ${S.step===0?"disabled":""}>Back</button>
          ${S.step < STEP("target") ? `<button class="btn primary" id="next">Next: ${STEPS[S.step+1].t}</button>` : ""}
        </div>
      </div>
      ${aside}
    </div>
  </div>`;
}

function stepStatement(P){
  const st = P.statement || DEFAULT_STATEMENT;
  return `<section class="panel"><div class="panel-h"><h2>Portfolio statement</h2><span class="label">statement.json</span></div>
    <div class="panel-b">${statementForm(st, "mandate")}</div>
    <div class="panel-f"><button class="btn primary" id="saveStmt">Save statement</button></div>
  </section>`;
}
function stepRebalancing(P){
  const st = P.statement || DEFAULT_STATEMENT;
  return `<section class="panel"><div class="panel-h"><h2>Rebalancing rule</h2><span class="label">statement.json · rebalance</span></div>
    <div class="panel-b">${statementForm(st, "rebalance")}</div>
    <div class="panel-b" style="padding-top:0"><p class="note">The backtest and the Monitor hold the last recorded decision as the standing target. A calendar date re-derives it from that session's float caps; drift and breach trade back to it between calendar dates.</p></div>
    <div class="panel-f"><button class="btn primary" id="saveStmt">Save rebalancing rule</button></div>
  </section>`;
}
/* a universe name as a chip: flags from screens.json, new since the last decision */
function chip(P, t, cls, extra=""){
  const f = P.u.flags?.[t] || [];
  const deco = (f.length ? `<i class="flag" title="${esc(f.map(flagText).join("; "))}">!</i>` : "")
    + (P.u.NEW[t] ? '<i class="newdot" title="not in the universe of the last decision">new</i>' : "");
  return `<span class="chip ${cls}" draggable="${cls.includes("in ")||cls.includes("claimed")||cls.includes("gone")?"false":"true"}" data-t="${esc(t)}" title="${esc(P.u.co[t]||t)}">${esc(t)}${deco}${extra}</span>`;
}
/* Rating control: a NO on/off button (the only red) and, while NO is off, the
   UW3-OW3 spectrum. Clicking a cell sets the rating and lights every cell
   between AV and it; the rest stay grey. The active pp input follows, bounded
   to the tier and, from the last preview, the floor (-neutral). */
function ppBounds(r, x){
  const [lo, hi] = tierRange(r);
  return [x && lo < 0 ? Math.max(lo, -Math.floor(x.neutral * 10000) / 100) : lo, hi];
}
function rateControl(key, attr, bk, x, opts, disabled){
  const isNo = bk.rating === "NO";
  const locked = (opts.no && isNo) || disabled;
  const [lo, hi] = ppBounds(bk.rating, x);
  const tiered = /\d$/.test(bk.rating);
  const cells = SPEC.map(([r, bg, ink]) => {
    const sel = bk.rating === r;
    const lit = !locked && (sel || inBand(bk.rating, r));
    const tip = r === "AV" ? "AV · 0 pp"
      : `${r} · ${r[0] === "U" ? "down to −" : "up to +"}${TIER[r[2]]} pp`;
    return `<button class="cell${sel ? " sel" : ""}" ${attr}="${esc(key)}" data-rate="${r}"${lit ? ` style="background:${bg};color:${ink}"` : ""} aria-pressed="${sel}" ${locked ? "disabled" : ""} title="${tip}" aria-label="${r}">${r === "AV" ? "AV" : r[2]}</button>`;
  }).join("");
  return `${opts.no ? `<button class="nobtn${isNo ? " on" : ""}" ${attr}="${esc(key)}" data-no="1" aria-pressed="${isNo}" title="Out of scope: no weight, no active pp">NO</button>` : ""}
    <div class="spec" role="group" aria-label="Rating for ${esc(key)}">${cells}</div>
    ${locked && !isNo ? `<div class="gmeta">no investable name</div>` : ""}
    ${!locked && tiered ? `<div class="inline" style="gap:6px"><input class="mult" type="number" step="0.25" min="${lo}" max="${hi}" ${attr}="${esc(key)}" data-pp="1" value="${bk.pp}" aria-label="Active pp for ${esc(key)}"><span class="gmeta">${bk.rating} · range ${fpp(lo)} to ${fpp(hi)} pp</span></div>` : ""}`;
}
function actBar(P){
  const a = activeSums(P); const r = S.preview;
  const faults = r?.ok ? r.active.faults.filter(f => !f.startsWith("active pp")) : [];
  const fill = Math.min(1, a.used / a.budget);
  return `<div class="panel-b actbar">
      <span class="pill ${a.netOk ? "ok" : "bad"}" title="Overweights are funded by underweights: the active pp must sum to 0">Net ${fpp(a.net)} pp</span>
      <span class="inline" style="gap:8px"><span class="label">Budget</span><div class="bar" style="min-width:140px" aria-hidden="true"><div class="t" style="width:${fill*100}%;background:var(--${a.budgetOk ? "accent" : "uw"})"></div></div>
        <span class="num ${a.budgetOk ? "" : "err"}">${a.used.toFixed(2)} / ${a.budget} pp</span></span>
      <span class="inline" style="gap:6px"><button class="btn sm" id="balance" ${a.netOk ? "disabled" : ""} title="Scale the larger side (OW or UW) down until the net is 0">Balance to zero</button>${P.undoPP ? '<button class="btn sm ghost" id="undoBalance">Undo</button>' : ""}</span>
      ${faults.length ? `<p class="note bad" style="flex-basis:100%;white-space:pre-wrap">${faults.map(esc).join("\n")}</p>` : ""}
    </div>`;
}
const bookOk = P => { const a = activeSums(P); return a.netOk && a.budgetOk; };
function stepAllocation(P){
  if (!ready(P)) return `<section class="panel" id="sec-allocation"><div class="panel-b"><p style="color:var(--ink-3)">No universe to allocate: rebuild the database on the Market data page.</p></div></section>`;
  const u = P.u; const c = claims(P); const r = S.preview;
  const byG = r?.ok ? Object.fromEntries(r.rows.map(x=>[x.group,x])) : {};
  const isNo = g => P.book[g.name].rating === "NO";
  const order = [...u.groups].sort((a, b) => isNo(a) - isNo(b) || a.name.localeCompare(b.name));
  const groups = order.map(g => { const bk = P.book[g.name]; const x = byG[g.name]; const surv = survivors(P, g);
    const gone = notTrading(P, g.name);
    const inc = g.members.filter(m => bk.included.has(m.t));
    return `<div class="grp ${bk.rating==="NO"?"isno":""}" data-g="${esc(g.name)}">
      <div class="gside">
        <div class="gname" title="${esc(g.name)}">${esc(g.name)}${bk.autoNo ? ' <span class="pill xs off plain">auto NO</span>' : ""}${bk.gained ? ' <span class="pill xs warn plain" title="in the universe but not in the saved book: rated NO until you rate it">not in book</span>' : ""}</div>
        ${rateControl(g.name, "data-g", bk, x, {no:true}, !surv.length)}
        <div class="gmeta">${bk.rating === "NO" ? `base ${pct(g.weight,1)} · out of scope` : x ? `neutral ${pct(x.neutral,1)} → <b style="color:var(--ink)">${pct(x.w,1)}</b> <span class="delta ${x.vs_neutral>0.05?"up":x.vs_neutral<-0.05?"dn":""}">${fpp(x.vs_neutral,1)}</span>` : "–"}${x?.capped ? ' <span class="pill xs warn plain">cap</span>' : ""}</div>
        ${bk.rating === "NO" ? "" : (cov => cov ? `<div class="gmeta cover${cov.cover < COVER_THIN ? " thin" : ""}" title="The group's neutral is its float-cap weight in the universe. Names you left out still support it, so the ones you kept carry their share.">cover ${pct(cov.cover,0)} · ${cov.kept} of ${cov.of} names${cov.topw !== null ? ` · ${esc(cov.top)} ${pct(cov.topw,1)} of the book` : ""}</div>` : "")(coverage(P, g, x))}
      </div>
      <div class="gboxes">
        <div class="zone zone-all" data-g="${esc(g.name)}">
          <div class="zone-h"><span class="label">All names · ${g.members.length}</span><button class="btn sm" data-addall="${esc(g.name)}" ${inc.length === g.members.length ? "disabled":""}>Add all</button></div>
          <div class="chips">${g.members.map(m => { const cls = c[m.t] ? "claimed" : bk.included.has(m.t) ? "in " : ""; return chip(P, m.t, cls, c[m.t] ? `<small style="opacity:.8">→ ${esc(c[m.t])}</small>` : ""); }).join("")}</div>
        </div>
        <div class="zone zone-inc" data-g="${esc(g.name)}">
          <div class="zone-h"><span class="label">Investable · ${surv.length}</span><button class="btn sm" data-rmall="${esc(g.name)}" ${inc.length || gone.length ?"":"disabled"}>Remove all</button></div>
          <div class="chips">${surv.length || gone.length ? surv.map(m => chip(P, m.t, "", `<button class="x" data-rm="${esc(m.t)}" data-rmg="${esc(g.name)}" aria-label="Remove ${esc(m.t)}">×</button>`)).join("")
            + gone.map(t => `<span class="chip gone" title="not trading on ${esc(u.as_of)}: kept in the book, left out of this target">${esc(t)}<button class="x" data-rm="${esc(t)}" data-rmg="${esc(g.name)}" aria-label="Remove ${esc(t)}">×</button></span>`).join("")
            : `<span class="empty">Drop names here${g.members.some(m => c[m.t]) ? " · some claimed by a tactical group" : ""}</span>`}</div>
        </div>
      </div>
    </div>`; }).join("");
  const lost = lostGroups(P);
  const tac = P.tac.groups.map((tg, i) => { const x = byG[tg.name]; const live = tg.tickers.filter(t => u.FC[t] !== undefined); return `<div class="tacg" data-tg="${i}">
      <div class="gside">
        <div class="inline" style="gap:6px"><input type="text" value="${esc(tg.name)}" id="tgname_${i}" data-tgname="${i}" aria-label="Tactical group name" style="padding:3px 6px;font-weight:600"><button class="btn sm ghost" data-tgdel="${i}" aria-label="Delete tactical group">×</button></div>
        <div class="inline" style="gap:6px"><span class="chip tac" style="cursor:default;font-size:10.5px">tactical</span>${tg.rating==="NO" ? '<span class="pill xs off plain">NO until it claims</span>' : ""}</div>
        ${rateControl(String(i), "data-tg", tg, P.tac.on ? x : null, {no:false}, !live.length)}
        <div class="gmeta">budget ${pct(live.reduce((a,t)=>a+u.FC[t],0)/u.groups.reduce((a,g)=>a+g.members.reduce((b,m)=>b+m.fcap,0),0),2)} → <b style="color:var(--ink)">${P.tac.on && x ? pct(x.w,1) : "off"}</b></div>
      </div>
      <div class="zone zone-tac" data-tg="${i}">
        <div class="zone-h"><span class="label">Claims · ${tg.tickers.length}</span><span class="gmeta">from ${[...new Set(live.map(t=>u.HOME[t]))].map(esc).join(", ") || "—"}</span></div>
        <div class="combo">
          <input type="text" id="tsearch_${i}" data-tsearch="${i}" placeholder="Type a ticker or company, Enter to claim" autocomplete="off" role="combobox" aria-expanded="false" aria-controls="tlist_${i}" aria-label="Search stocks to claim for ${esc(tg.name)}">
          <ul class="combo-list" id="tlist_${i}" role="listbox" hidden></ul>
        </div>
        <span class="err" id="terr_${i}" role="alert"></span>
        <div class="chips">${tg.tickers.length ? tg.tickers.map(t => `<span class="chip tac${u.FC[t] === undefined ? " gone" : ""}" style="cursor:default" title="${u.FC[t] === undefined ? `not trading on ${esc(u.as_of)}` : `${esc(u.co[t]||t)} · home ${esc(u.HOME[t])}`}">${esc(t)}<button class="x" data-unclaim="${esc(t)}" aria-label="Release ${esc(t)}">×</button></span>`).join("") : `<span class="empty">No claims. A claimed name leaves its home group and takes its float-cap budget along.</span>`}</div>
      </div>
    </div>`; }).join("");
  return `<section class="panel" id="sec-allocation"><div class="panel-h"><h2>Allocation</h2>
      <div class="legend"><span class="specleg" aria-hidden="true">${SPEC.map(([, bg]) => `<i style="background:${bg}"></i>`).join("")}</span><span>UW3 → OW3, tiers up to ∓3 / ∓6 / ∓9 pp; AV is 0</span><span><i style="background:#E53935"></i>NO out of scope</span><span><b class="flag-l">!</b> screen flag</span><span><b class="new-l">new</b> since the last decision</span></div>
      <div class="inline"><button class="btn sm" id="addAllG">Add all groups</button><button class="btn sm" id="rmAllG">Remove all groups</button></div></div>
    <div class="panel-b" style="padding-bottom:0"><span class="label">universe of ${esc(u.as_of)} · the Target's effective date sets it</span></div>
    ${actBar(P)}
    ${lost.length ? `<div class="panel-b" style="padding-top:0"><p class="note warn"><b>Not in the universe on ${esc(u.as_of)}:</b> ${lost.map(g => `${esc(g)} ${esc(P.book[g].rating)} ${fpp(P.book[g].pp)} pp <button class="btn sm" data-droplost="${esc(g)}">Remove from book</button>`).join(" · ")}. A group missing from the universe is dropped with its pp at evaluation.</p></div>` : ""}
    ${groups}
    <div class="panel-b"><p class="note">Each group starts at its neutral weight: its float-cap share of the groups not rated NO. Active pp move it from there, no further than the tier allows and never below 0%. Overweights are funded by underweights, so the pp must net to 0 within the budget (Constraints step). Leaving a name out keeps its group's budget; the kept names absorb it by float cap. Screen flags never remove a name; untick it to leave it out. A new listing stays out until you add it.</p></div>
  </section>
  <section class="panel"><div class="panel-h"><h2>Tactical overlay</h2><label class="toggle"><input type="checkbox" id="tac_on" ${P.tac.on?"checked":""}> ${P.tac.on?"On":"Off"}</label></div>
    <div class="panel-b" style="display:flex;flex-direction:column;gap:12px;${P.tac.on?"":"opacity:.6"}">
      ${tac || `<p style="color:var(--ink-3);font-size:13px">No tactical groups.</p>`}
      <div class="inline"><input type="text" id="tacNew" placeholder="New tactical group name" style="width:260px"><button class="btn" id="tacAdd">Add tactical group</button>
        <span style="font-size:12.5px;color:var(--ink-3)">${P.tac.on ? "Switching off releases every claim back to its home group." : "Claims are kept but inert while the overlay is off."}</span></div>
    </div>
    <div class="panel-f">${P.dirty.book && !bookOk(P) ? '<span class="err">Balance the net to 0 within the budget to save.</span>' : ""}<button class="btn" id="discardBook" ${P.dirty.book?"":"disabled"}>Discard</button><button class="btn primary" id="saveBook" ${P.dirty.book && bookOk(P)?"":"disabled"}>Save allocation</button></div>
  </section>`;
}
/* ---------- target: build and record (D60) ---------- */
function recordState(P){
  const dec = P.d.decisions || [], eff = P.decDraft.eff;
  const inc = dec[0]?.effective;
  const isInc = !dec.length || eff === inc;
  const kind = isInc ? "inception" : P.decDraft.kind;
  const exists = dec.some(d => d.effective === eff);
  const before = !isInc && eff && eff < inc;
  const label = exists ? `Replace decision for ${eff}` : kind === "inception" ? "Record inception" : `Record ${kind} rebalance`;
  return {dec, eff, inc, isInc, kind, exists, before, label};
}
function recordBox(P){
  const r = S.preview, s = recordState(P);
  const faults = r?.ok ? r.active.faults : [];
  const dirty = P.dirty.book || P.dirty.cons;
  const blocked = !s.eff || s.before || !r?.ok || faults.length;
  const head = P.d.head_matches_book;
  return `<div class="panel-b dec-log">
      <div class="dec-rec">
        <div class="field"><label for="decEff">Effective date</label><input type="date" id="decEff" value="${esc(s.eff)}" min="${esc(S.state.sessions[0] || "")}"></div>
        <div class="field"><label>Kind</label>${s.isInc ? `<span class="pill dec kindpill" title="${s.dec.length ? "re-recording the inception date replaces it" : "the first decision of a portfolio"}">inception</span>`
          : `<div class="seg" role="group" aria-label="Kind of rebalance">${["period","active"].map(k => `<button type="button" data-kind="${k}" aria-pressed="${s.kind===k}" title="${k === "period" ? "the scheduled review of the period" : "a discretionary change inside the period"}">${k === "period" ? "Period" : "Active"}</button>`).join("")}</div>`}</div>
        <div class="field"><label for="decNote">Note</label><input type="text" id="decNote" value="${esc(P.decDraft.note)}" placeholder="why the allocation changed"></div>
        <div><button class="btn primary" id="decRec" ${blocked ? "disabled" : ""}>${esc(dirty ? "Save and " + s.label[0].toLowerCase() + s.label.slice(1) : s.label)}</button></div>
      </div>
      ${s.before ? `<p class="note bad">${esc(s.eff)} is before inception ${esc(s.inc)}. A later decision must be dated after it; Reset on the Decisions step to start a new inception.</p>` : ""}
      ${r?.ok && r.requested && r.as_of !== r.requested ? `<span class="hint">Effective ${esc(r.requested)}, priced as of ${esc(r.as_of)}: the last session on or before it.</span>` : ""}
      <p class="note">Record builds target/ on the date's session and stores the allocation as saved, its holdings, the screen flags on them and a fingerprint of the setup in decisions/. One decision per date: recording a date again replaces it. ${head === false ? '<span class="pill xs warn">the saved allocation differs from the last decision</span>' : head ? '<span class="pill xs ok">the saved allocation matches the last decision</span>' : ""}</p>
    </div>`;
}
function stepTarget(P){
  const r = S.preview;
  if (!ready(P)) return "";
  if (!r) return `<section class="panel" id="sec-target"><div class="panel-h"><h2>Target</h2></div>${recordBox(P)}<div class="panel-b"><p style="color:var(--ink-3)">Computing...</p></div></section>`;
  const summ = S.state.portfolios.find(p => p.name === P.name);
  const dirty = P.dirty.book || P.dirty.cons;
  if (!r.ok) return `<section class="panel" id="sec-target"><div class="panel-h"><h2>Target</h2><span class="pill bad">Blocked</span></div>${recordBox(P)}<div class="panel-b"><p class="err" style="white-space:pre-wrap">${esc(r.error)}</p></div></section>`;
  const faults = r.active.faults;
  const st = P.statement; const n = r.n; const u = P.u; const cs = P.cons;
  const maxW = Math.max(...r.rows.map(x=>Math.max(x.w, x.neutral)));
  const flags = u.flags || {};
  const nFlag = r.holdings.filter(hh => flags[hh.t]).length;
  const drops = r.drops, dn = Object.values(drops.dropped_names).flat();
  return `<section class="panel" id="sec-target"><div class="panel-h"><h2>Target <span class="label" style="margin-left:6px">priced as of ${esc(r.as_of)}</span></h2>
      <div class="inline">${dirty ? `<span class="pill warn">Preview includes unsaved edits</span>` : summ?.built_at ? `<span class="pill ok">Last built ${esc(summ.built_at)}</span>` : `<span class="pill off">Never built</span>`}</div></div>
    ${recordBox(P)}
    ${faults.length ? `<div class="panel-b" style="padding-bottom:0"><p class="note bad" style="white-space:pre-wrap"><b>Blocked: the active weights break the book rules.</b> The weights below are a preview with each group floored at 0%.\n${faults.map(esc).join("\n")}</p></div>` : ""}
    ${r.in_range === false && st ? `<div class="panel-b" style="padding-bottom:0"><p class="note warn"><b>${n} holdings is outside ${st.holdings.min}–${st.holdings.max}.</b> ${n < st.holdings.min ? "Add names to investable boxes or rate more groups above NO." : "Remove names or rate groups NO in the Allocation step."} Record still runs; the flag is a warning.</p></div>` : ""}
    ${dn.length || Object.keys(drops.lost_groups).length ? `<div class="panel-b" style="padding-bottom:0"><p class="note warn">${dn.length ? `<b>Not trading on ${esc(r.as_of)}, left out:</b> ${dn.map(esc).join(", ")}. ` : ""}${Object.keys(drops.lost_groups).length ? `<b>Groups not in the universe, dropped with their pp:</b> ${Object.entries(drops.lost_groups).map(([g, v]) => `${esc(g)} ${esc(v)}`).join(", ")}.` : ""}</p></div>` : ""}
    <div class="scroll"><table>
      <thead><tr><th>Group</th><th>Rating</th><th class="n">Names</th><th class="n">Base</th><th class="n">Neutral</th>${r.rows.some(x => Math.abs(x.unc - x.w) > 1e-9) ? '<th class="n">Tilted</th>' : ""}<th class="n">Target</th><th class="n">vs neutral</th><th class="n">vs base</th><th style="width:22%">Neutral <span style="font-weight:400">|</span> target</th></tr></thead>
      <tbody>${r.rows.filter(x=>x.w>0).map(x => `<tr><td>${esc(x.group)}${x.kind==="tactical"?' <span class="pill plain off xs">tactical</span>':""}${x.capped?' <span class="pill plain warn xs">capped</span>':""}${x.full?' <span class="pill plain warn xs">full</span>':""}</td>
        <td><span class="mono" style="color:var(--${side(x.rating)});font-weight:500">${x.rating}</span> <span class="mono" style="color:var(--ink-3);font-size:12px">${fpp(x.pp)}</span></td>
        <td class="n">${x.n}/${x.n_all}</td><td class="n">${pct(x.base)}</td><td class="n">${pct(x.neutral)}</td>${r.rows.some(y => Math.abs(y.unc - y.w) > 1e-9) ? `<td class="n" style="color:var(--ink-3)">${pct(x.unc)}</td>` : ""}<td class="n"><b>${pct(x.w)}</b></td>
        <td class="n ${x.vs_neutral > 0.005 ? "pos" : x.vs_neutral < -0.005 ? "negv" : ""}">${fpp(x.vs_neutral)}</td><td class="n" style="color:var(--ink-3)">${fpp((x.w - x.base) * 100)}</td>
        <td><div class="bar"><div class="t" style="width:${x.w/maxW*100}%;background:var(--${side(x.rating)})"></div><div class="b" style="width:${x.neutral/maxW*100}%"></div>${cs.sector.on ? `<div class="c" style="left:${Math.min(1, capOf(P, x.group)/maxW)*100}%"></div>` : ""}</div></td></tr>`).join("")}
      </tbody></table></div>
    <div class="panel-b" style="padding-block:6px 10px"><span style="font-size:12px;color:var(--ink-3)">${r.rows.filter(x=>x.w===0).length} groups at 0. Active ${fpp(r.active.net_pp)} pp net, ${r.active.used_pp.toFixed(2)} of ${r.active.budget_pp} pp budget. vs neutral and vs base are pp after the caps; base is the universe float cap, the nearest proxy for VNINDEX. Bar is the target, grey tick the neutral${cs.sector.on ? ", dashed line the cap" : ""}. ${esc(r.tac_note)}</span></div>
    </section>
    <section class="panel"><div class="panel-h"><h2>Constraints in force</h2></div>${statusTable(P)}</section>
    <section class="panel"><div class="panel-h"><h2>Holdings <span class="num" style="color:var(--ink-3)">${n}</span></h2><span class="label">${nFlag ? `${nFlag} flagged · ` : ""}target/holdings.csv on Record</span></div>
      <div class="scroll" style="max-height:440px;overflow:auto"><table>
        <thead><tr><th>Ticker</th><th>Company</th><th>Group</th><th class="n">Float cap, bn</th><th class="n">Weight</th><th></th><th>Flags</th></tr></thead>
        <tbody>${r.holdings.map(hh => `<tr><td class="mono">${esc(hh.t)}</td><td>${esc(u.co[hh.t]||"")}</td><td>${esc(hh.group)}${hh.group!==hh.home?` <span style="color:var(--ink-3);font-size:12px">from ${esc(hh.home)}</span>`:""}</td><td class="n">${bn(hh.fcap)}</td><td class="n">${pct(hh.w)}</td><td>${hh.pin==="stock_max"?'<span class="pill xs warn plain">at max</span>':hh.pin==="at_threshold"?'<span class="pill xs warn plain">at threshold</span>':hh.pin==="large"?'<span class="pill xs off plain">large</span>':""}</td><td>${(flags[hh.t] || []).map(f => `<span class="pill xs warn plain" title="${esc(flagText(f))}">${esc(f.screen)}</span>`).join(" ")}</td></tr>`).join("")}</tbody>
      </table></div></section>`;
}

/* ---------- monitor (D63) ---------- */
async function loadMonitor(){
  const P = S.P; P.monLoading = true; render();
  const m = await api("GET", `/api/p/${encodeURIComponent(P.name)}/monitor`);
  if (S.P !== P) return;
  P.mon = m; P.monLoading = false; render();
}
function screensForm(P){
  const sc = P.screens, u = P.u;
  const tos = u ? Object.values(u.TO).filter(x => x !== null).sort((a,b)=>a-b) : [];
  const med = tos.length ? tos[Math.floor(tos.length/2)] : null;
  const nfol = u ? Object.values(u.FOL).filter(x => x !== null).length : 0;
  const row = (key, title, sub, field, unit, step, note) => `<tr><td><b>${title}</b><div style="font-size:12px;color:var(--ink-3)">${sub}</div></td>
    <td><label class="toggle"><input type="checkbox" id="sc_${key}" ${sc[key].on?"checked":""} aria-label="${title} flag on"></label></td>
    <td><div class="inline"><input type="number" id="sc_${key}_v" step="${step}" min="0" value="${sc[key][field]}" style="width:90px"> <span class="mono">${unit}</span></div></td>
    <td class="mono" style="font-size:12px">${note}</td></tr>`;
  return `<section class="panel"><div class="panel-h"><h2>Screen rules</h2><span class="label">screens.json · flags only, never exclusions</span></div>
    <div class="scroll"><table>
      <thead><tr><th>Screen</th><th>On</th><th>Flag when below</th><th>Universe</th></tr></thead>
      <tbody>
        ${row("turnover", "21-day turnover", "avg daily value ÷ float cap", "min_pct", "% / day", 0.05, med !== null ? `median ${med.toFixed(3)}%` : "")}
        ${row("float_cap", "Float cap", "free float × official close", "min_bn_vnd", "bn VND", 100, "")}
        ${row("fol", "Foreign ownership limit", "index/fol.csv; a name without a row flags no data", "min_limit_pct", "%", 1, `${nfol} names with a limit`)}
      </tbody></table></div>
    <div class="panel-f"><span style="font-size:12.5px;color:var(--ink-3);margin-right:auto">A flag marks a risk on a name in the Allocation and Target steps and here; it never removes the name. Flags are measured at each decision's session and stored with it.</span><button class="btn primary" id="scSave">Save screen rules</button></div>
  </section>`;
}
function stepMonitor(P){
  const m = P.mon;
  let body;
  if (!m || P.monLoading) body = `<section class="panel"><div class="panel-b"><p style="color:var(--ink-3)">Running the engine from the last decision...</p></div></section>`;
  else if (!m.ok) body = `<section class="panel"><div class="panel-b"><p class="note bad" style="white-space:pre-wrap">${esc(m.error)}</p></div></section>`;
  else if (!m.decision) body = `<section class="panel"><div class="panel-h"><h2>Monitor</h2></div><div class="panel-b"><p class="note">${esc(m.status)}</p></div></section>`;
  else {
    const d = m.decision, now = m.now, fl = m.flags || [];
    const byT = fl.reduce((a, f) => ((a[f.t] ??= []).push(f), a), {});
    const figs = now ? `<div class="bt-figs">
        <div class="fig"><span class="label">Group drift</span><span class="big ${now.threshold != null && now.drift > now.threshold ? "negv" : ""}">${pct(now.drift, 2)}</span><span class="sub">${now.threshold != null ? `trigger at ${pct(now.threshold, 0)}` : "drift trigger off"}</span></div>
        <div class="fig"><span class="label">Breach</span><span class="big ${now.breach ? "negv" : ""}" style="font-size:18px">${now.breach ? "yes" : "none"}</span><span class="sub">${now.breach ? esc(now.breach) : now.tolerance != null ? `tolerance ${pct(now.tolerance, 0)} of a cap` : "breach off"}</span></div>
        <div class="fig"><span class="label">Next calendar date</span><span class="big" style="font-size:18px">${m.next_calendar ?? "none"}</span><span class="sub">${m.frequency ? `${FREQ[m.frequency]}; the first session on or after it` : "no schedule"}</span></div>
        <div class="fig"><span class="label">Since the decision</span><span class="big ${tone(m.total)}">${spct(m.total)}</span><span class="sub">paper portfolio, total return</span></div>
      </div>` : "";
    body = `<section class="panel"><div class="panel-h"><h2>Monitor <span class="label" style="margin-left:6px">${esc(m.status)}</span></h2><button class="btn sm" id="monRefresh">Refresh</button></div>
      <div class="panel-b"><dl class="kv"><dt>Standing target</dt><dd>decision ${esc(d.id)} · ${esc(d.kind)}${d.priced_as_of ? ` · priced as of ${esc(d.priced_as_of)}` : ""}${d.note ? ` · ${esc(d.note)}` : ""}</dd><dt>Latest session</dt><dd>${esc(m.latest)}</dd></dl></div>
      ${figs}
      ${m.messages?.length ? `<div class="console">${logHtml(m.messages.join("\n"))}</div>` : ""}
    </section>
    ${m.rebalances.length ? `<section class="panel"><div class="panel-h"><h2>Fills since the decision</h2><span class="label">paper portfolio of the standing target, not broker fills</span></div>
      <div class="scroll"><table><thead><tr><th>Decision</th><th>Fill</th><th>Trigger</th><th>Policy</th><th class="n">Turnover</th></tr></thead><tbody>
      ${m.rebalances.map(e => `<tr><td class="mono">${e.decision}</td><td class="mono">${e.fill}</td><td>${e.trigger === "inception" ? '<span class="pill dec">decision</span>' : e.trigger === "breach" ? '<span class="pill bad">breach</span>' : e.trigger === "drift" ? '<span class="pill warn">drift</span>' : '<span class="pill info">calendar</span>'}</td><td>${e.policy}</td><td class="n">${pct(e.turnover, 1)}</td></tr>`).join("")}
      </tbody></table></div></section>` : ""}
    ${m.holdings.length ? `<section class="panel"><div class="panel-h"><h2>Holdings now</h2><span class="label">drifted weight on ${esc(m.latest)} vs the standing target</span></div>
      <div class="scroll"><table><thead><tr><th>Ticker</th><th>Group</th><th class="n">Weight</th><th class="n">Target</th><th class="n">Gap</th><th>Flags</th></tr></thead><tbody>
      ${m.holdings.map(x => `<tr><td class="mono">${esc(x.t)}</td><td>${esc(x.group)}</td><td class="n">${pct(x.w)}</td><td class="n">${pct(x.target)}</td><td class="n ${tone(x.w - x.target)}">${spp(x.w - x.target)}</td><td>${(byT[x.t] || []).map(f => `<span class="pill xs warn plain" title="${esc(flagText(f))}">${esc(f.screen)}</span>`).join(" ")}</td></tr>`).join("")}
      </tbody></table></div></section>` : ""}
    <section class="panel"><div class="panel-h"><h2>Screen flags <span class="num" style="color:var(--ink-3)">${Object.keys(byT).length}</span></h2><span class="label">${m.screens_on.length ? `on the latest session · ${m.screens_on.join(", ")} on` : "every screen off"}</span></div>
      ${fl.length ? `<div class="scroll"><table><thead><tr><th>Ticker</th><th>Screen</th><th class="n">Value</th><th class="n">Threshold</th><th></th></tr></thead><tbody>
        ${fl.map(f => `<tr><td class="mono">${esc(f.t)}</td><td>${esc(FLAGNAME[f.screen])}</td><td class="n">${f.value === null ? "no data" : (+f.value).toPrecision(4)}</td><td class="n">${f.threshold}${UNIT[f.screen]}</td><td>${f.why === "no data" ? '<span class="pill xs off">no data</span>' : '<span class="pill xs warn">below</span>'}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="panel-b"><p style="color:var(--ink-3);font-size:13px">${m.screens_on.length ? "No holding is flagged." : "Turn a screen rule on below to flag holdings."}</p></div>`}
    </section>`;
  }
  return body + screensForm(P);
}

/* ---------- decisions (D53, D56, D60, D61) ---------- */
const decDrops = r => !r ? "" : [Object.keys(r.dropped_names).length && `not trading today: ${Object.values(r.dropped_names).flat().join(", ")}`,
  Object.keys(r.lost_groups).length && `groups lost: ${Object.keys(r.lost_groups).join(", ")}`,
  r.appeared.length && `new groups, NO: ${r.appeared.join(", ")}`].filter(Boolean).join(" · ");
function stepDecisions(P){
  const log = (P.d.decisions || []).slice().reverse();
  if (!log.length) return `<section class="panel"><div class="panel-h"><h2>Decisions</h2></div><div class="panel-b"><p style="color:var(--ink-3);font-size:13px">No decision recorded. Record the inception on the Target step; until then the backtest holds book.json throughout.</p></div></section>`;
  const d = log.find(x => x.id === P.decSel) || log[0];
  const rated = Object.entries(d.groups).filter(([, s]) => (s.rating || "AV") !== "NO").sort(([a], [b]) => a.localeCompare(b));
  const no = Object.keys(d.groups).filter(g => d.groups[g].rating === "NO").sort();
  const tac = d.tactical?.groups || [];
  const drops = decDrops(d.report);
  const w = Object.fromEntries((d.holdings || []).map(h => [h.t, h.w]));
  const byT = (d.flags || []).reduce((a, f) => ((a[f.t] ??= []).push(f), a), {});
  const gw = (d.holdings || []).reduce((a, h) => (a[h.group] = (a[h.group] || 0) + h.w, a), {});
  const row = (name, s, names, tag) => `<tr><td>${esc(name)}${tag}</td><td><span class="mono" style="color:var(--${side(s.rating || "AV")});font-weight:500">${esc(s.rating || "AV")}</span></td><td class="n">${fpp(s.pp || 0)}</td><td class="n">${gw[name] !== undefined ? pct(gw[name], 1) : "—"}</td><td class="n">${names.length}</td><td class="mono" style="font-size:12px;color:var(--ink-2)">${names.map(t => esc(t) + (w[t] !== undefined ? `<span style="color:var(--ink-3)"> ${pct(w[t], 1)}</span>` : "") + (byT[t] ? '<b class="flag-l" title="' + esc(byT[t].map(flagText).join("; ")) + '">!</b>' : "")).join("  ")}</td></tr>`;
  const reset = S.confirmReset
    ? `<span class="confirm">Move the ${log.length} decision${log.length > 1 ? "s" : ""} to decisions/archive/ and start a new inception? <button class="btn sm danger" id="resetYes">Reset</button><button class="btn sm" id="resetNo">Keep</button></span>`
    : `<button class="btn sm" id="resetAsk" title="Archive every decision; the next Record is a new inception">Reset decisions</button>`;
  return `<section class="panel"><div class="panel-h"><h2>Decisions <span class="num" style="color:var(--ink-3)">${log.length}</span></h2><span class="inline"><span class="label">decisions/log.csv · read-only</span>${reset}</span></div>
    <div class="scroll"><table class="dec-list"><thead><tr><th>Effective</th><th>Kind</th><th>Priced as of</th><th>Recorded</th><th>Note</th><th>Setup</th><th>Against today's universe</th></tr></thead><tbody>
      ${log.map(x => `<tr data-dec="${esc(x.id)}" aria-selected="${x.id === d.id}"><td class="mono">${esc(x.effective)}</td><td><span class="pill xs ${x.kind === "inception" ? "dec" : x.kind === "active" ? "warn" : "info"}">${esc(x.kind || "")}</span></td><td class="mono">${esc(x.priced_as_of || "")}</td><td class="mono">${esc(x.recorded_at || "")}</td><td>${esc(x.note || "")}</td><td>${x.setup_differs ? '<span class="pill xs warn" title="statement.json or constraints.json changed since this decision; replay uses today\'s">setup differs</span>' : x.setup_hash ? '<span class="pill xs ok">same</span>' : '<span class="pill xs off">not recorded</span>'}</td><td>${decDrops(x.report) ? '<span class="err">names or groups dropped</span>' : "as recorded"}</td></tr>`).join("")}
    </tbody></table></div></section>
  <section class="panel"><div class="panel-h"><h2>Profile ${esc(d.effective)} <span class="label" style="margin-left:6px">${esc(d.kind || "")}</span></h2><span class="label">recorded ${esc(d.recorded_at || "")}${d.priced_as_of ? ` · priced as of ${esc(d.priced_as_of)}` : ""}${d.holdings ? ` · ${d.holdings.length} holdings` : ""}</span></div>
    ${d.note || drops ? `<div class="panel-b" style="padding-bottom:0">${d.note ? `<p>${esc(d.note)}</p>` : ""}${drops ? `<p class="note warn">Replayed on today's universe: ${esc(drops)}.</p>` : ""}</div>` : ""}
    <div class="scroll"><table><thead><tr><th>Group</th><th>Rating</th><th class="n">Active pp</th><th class="n">Weight</th><th class="n">Names</th><th>Investable${d.holdings ? " · weight as recorded" : ""}</th></tr></thead><tbody>
      ${rated.map(([g, s]) => row(g, s, s.investable || [], "")).join("")}
      ${tac.map(t => row(t.name, t, t.members || [], ` <span class="pill xs off plain">tactical${d.tactical.on ? "" : ", overlay off"}</span>`)).join("")}
    </tbody></table></div>
    ${no.length ? `<div class="panel-b" style="padding-block:6px 10px"><span style="font-size:12px;color:var(--ink-3)">${no.length} groups rated NO: ${esc(no.join(", "))}.</span></div>` : ""}
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
  const a = ready(P) ? activeSums(P) : {used:0, budgetOk:true};
  const budget = `<div class="con on">
      <label for="act_b" class="check" style="cursor:default">Active budget</label><span class="desc" style="text-align:right">always on · group grain</span>
      <div class="body"><div class="inline"><label for="act_b" style="font-size:12.5px;color:var(--ink-2)">Half the sum of |active pp| at most</label><input class="small-in" type="number" id="act_b" min="0.5" max="100" step="0.5" value="${cs.active.budget}"><span class="mono">pp</span></div>
      <p class="desc">The allocation uses ${a.used.toFixed(2)} pp now${a.budgetOk ? "" : ", over the budget: lower the tilts in the Allocation step or raise this"}. Default 20.</p></div></div>`;
  return `<section class="panel"><div class="panel-h"><h2>Constraints</h2><span class="label">constraints.json · caps apply after the tilt</span></div>
    <div class="panel-b cons">${budget}${sector}${stock}${large}</div>
    <div class="panel-h" style="border-top:1px solid var(--line)"><h3>Status</h3></div>
    ${statusTable(P)}
    ${r && !r.ok ? `<div class="panel-b"><p class="note bad" style="white-space:pre-wrap">${esc(r.error)}</p></div>` : ""}
    <div class="panel-f"><button class="btn" id="discardCons" ${P.dirty.cons?"":"disabled"}>Discard</button><button class="btn primary" id="saveCons" ${P.dirty.cons?"":"disabled"}>Save constraints</button></div>
  </section>`;
}

/* ---------- tactical claim search ---------- */
/* Barrier rule: a stock belongs to at most one tactical group. */
function claimBlock(P, i, t){
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
        : `<li class="dis" aria-disabled="true"><span class="why">No stock in the ${esc(u.as_of)} universe matches "${esc(inp.value.trim())}"</span></li>`;
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
    if (!d) return false;
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
  if (!bookOk(P)){ const a = activeSums(P); S.console.flow = `FAIL  allocation not saved: active pp net ${fpp(a.net)}, ${a.used.toFixed(2)} of ${a.budget} pp budget; balance the net to 0 within the budget\n`; render(); return false; }
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
/* Record: save what is unsaved, then record the saved allocation for the date and build target/ */
async function recordDecision(){
  if (S.P.dirty.book && !(await saveBook())) return;       // a failed save leaves the edits in the page
  if (S.P.dirty.cons && !(await saveCons())) return;
  const P = S.P, s = recordState(P);
  await action("flow", "POST", `/api/p/${encodeURIComponent(P.name)}/decisions`,
    {effective:s.eff, kind:s.kind, note:P.decDraft.note, version:P.versions.book, dirty:false},
    async res => { if (res.ok){ await afterSave(); S.P.decSel = res.id; S.P.mon = null; S.P.decDraft.note = ""; if (BT.name === P.name) BT.req = null; } });
}

function bindAllocation(P){
  const u = P.u;
  /* NO toggle, spectrum and pp: the same markup for book groups (data-g) and tactical groups (data-tg) */
  const rowOf = el => el.dataset.g !== undefined ? P.book[el.dataset.g] : P.tac.groups[+el.dataset.tg];
  $$("[data-no]").forEach(b => b.onclick = () => { const bk = rowOf(b); setRating(bk, bk.rating === "NO" ? "AV" : "NO"); bk.gained = false; edited("book"); });
  $$("[data-rate]").forEach(b => b.onclick = () => { const bk = rowOf(b); setRating(bk, b.dataset.rate); bk.gained = false; edited("book"); });
  $$("[data-pp]").forEach(i => i.onchange = () => { const bk = rowOf(i); const v = +i.value;
    bk.pp = Number.isFinite(v) && i.value !== "" ? Math.round(Math.min(+i.max, Math.max(+i.min, v)) * 100) / 100 : 0; edited("book"); });
  if ($("#balance")) $("#balance").onclick = () => { balance(P); edited("book", true); };
  if ($("#undoBalance")) $("#undoBalance").onclick = () => { P.undoPP.forEach(([b, v]) => { b.pp = v; }); edited("book"); };
  $$("[data-addall]").forEach(b => b.onclick = () => { const g = u.byName[b.dataset.addall]; g.members.forEach(m => P.book[g.name].included.add(m.t)); edited("book"); });
  $$("[data-rmall]").forEach(b => b.onclick = () => { P.book[b.dataset.rmall].included.clear(); edited("book"); });
  $$("[data-rm]").forEach(b => b.onclick = () => { P.book[b.dataset.rmg].included.delete(b.dataset.rm); edited("book"); });
  $$("[data-droplost]").forEach(b => b.onclick = () => { delete P.book[b.dataset.droplost]; edited("book"); });
  $("#addAllG").onclick = () => { u.groups.forEach(g => g.members.forEach(m => P.book[g.name].included.add(m.t))); edited("book"); };
  $("#rmAllG").onclick = () => { u.groups.forEach(g => P.book[g.name].included.clear()); edited("book"); };
  $("#tac_on").onchange = e => { P.tac.on = e.target.checked; edited("book"); };
  $("#tacAdd").onclick = () => { const nm = $("#tacNew").value.trim(); if (!nm || P.tac.groups.some(g=>g.name===nm) || u.byName[nm]) return; P.tac.groups.push({name:nm, rating:"NO", pp:0, tickers:[], autoNo:true}); edited("book"); };
  $$("[data-tgname]").forEach(i => i.onchange = () => { const nm = i.value.trim(); const j = +i.dataset.tgname;
    if (nm && !u.byName[nm] && !P.tac.groups.some((g, x) => x !== j && g.name === nm)){ const old = P.tac.groups[j].name; P.tac.groups[j].name = nm; if (P.cons.sector.per[old] !== undefined){ P.cons.sector.per[nm] = P.cons.sector.per[old]; delete P.cons.sector.per[old]; } }
    edited("book"); });
  $$("[data-tgdel]").forEach(b => b.onclick = () => { P.tac.groups.splice(+b.dataset.tgdel, 1); edited("book"); });
  $$("[data-unclaim]").forEach(b => b.onclick = () => { P.tac.groups.forEach(g => g.tickers = g.tickers.filter(x => x !== b.dataset.unclaim)); edited("book"); });
  $("#saveBook").onclick = saveBook;
  $("#discardBook").onclick = () => loadPortfolio({discard:true});
  bindTacSearch(P);
  bindDrag(P);
}
function bindTarget(P){
  if (!$("#decEff")) return;
  $("#decEff").onchange = e => { P.decDraft.eff = e.target.value; render(); schedulePreview(); };
  $("#decNote").oninput = e => { P.decDraft.note = e.target.value; };
  $$("[data-kind]").forEach(b => b.onclick = () => { P.decDraft.kind = b.dataset.kind; render(); });
  $("#decRec").onclick = recordDecision;
}

function bindFlow(){
  const P = S.P; if (!P) return;
  $("#toList").onclick = () => go("list");
  $("#reload").onclick = () => { P.mon = null; afterSave().then(enterStep); };
  const k = STEPS[S.step].k;
  const toStep = i => { S.step = i; S.confirmReset = false; if (PAIR.has(STEPS[i].k)) S.scrollTo = STEPS[i].k; render(); if (!PAIR.has(STEPS[i].k)) window.scrollTo({top:0}); enterStep(); };
  $$("[data-step]").forEach(b => b.onclick = () => toStep(+b.dataset.step));
  $("#prev").onclick = () => toStep(S.step - 1);
  if ($("#next")) $("#next").onclick = () => toStep(S.step + 1);
  if ($("#saveAll")) $("#saveAll").onclick = async () => { if (P.dirty.book && !(await saveBook())) return; if (S.P.dirty.cons) await saveCons(); };
  if ($("#discardAll")) $("#discardAll").onclick = () => loadPortfolio({discard:true});
  const name = encodeURIComponent(P.name);
  if (k === "statement" || k === "rebalancing"){
    bindStatementForm();
    $("#saveStmt").onclick = () => action("flow", "PUT", `/api/p/${name}/statement`,
      {statement: readStatementForm(P.statement || DEFAULT_STATEMENT), version:P.versions.statement}, async res => { if (res.ok){ P.mon = null; await afterSave(); } });
  }
  if (PAIR.has(k) && ready(P)){ bindAllocation(P); bindTarget(P); }
  if (k === "constraints"){
    const cs = P.cons;
    const frac = (v, lo) => Math.min(1, Math.max(lo, +v/100));
    $$("[data-con]").forEach(c => c.onchange = () => { cs[c.dataset.con].on = c.checked; edited("cons"); });
    $("#act_b").onchange = e => { const v = +e.target.value; cs.active.budget = Number.isFinite(v) && v > 0 ? Math.min(100, v) : cs.active.budget; edited("cons"); };
    $$("[data-capmode]").forEach(b => b.onclick = () => { cs.sector.mode = b.dataset.capmode; edited("cons"); });
    if ($("#cap_v")) $("#cap_v").onchange = e => { cs.sector.max = frac(e.target.value, 0.001); edited("cons"); };
    $$("[data-capg]").forEach(i => i.onchange = () => { cs.sector.per[i.dataset.capg] = frac(i.value, 0.001); edited("cons"); });
    if ($("#stk_v")) $("#stk_v").onchange = e => { cs.stock.max = frac(e.target.value, 0.001); edited("cons"); };
    if ($("#uc_t")) $("#uc_t").onchange = e => { cs.large.T = Math.min(0.999, frac(e.target.value, 0.001)); edited("cons"); };
    if ($("#uc_l")) $("#uc_l").onchange = e => { cs.large.L = frac(e.target.value, 0.001); edited("cons"); };
    $("#saveCons").onclick = saveCons;
    $("#discardCons").onclick = () => loadPortfolio({discard:true});
  }
  if (k === "monitor"){
    if ($("#monRefresh")) $("#monRefresh").onclick = () => { P.mon = null; loadMonitor(); };
    $("#scSave").onclick = () => {
      const v = (key, field) => ({on:$(`#sc_${key}`).checked, [field]:+$(`#sc_${key}_v`).value});
      action("flow", "PUT", `/api/p/${name}/screens`,
        {screens:{turnover:v("turnover", "min_pct"), float_cap:v("float_cap", "min_bn_vnd"), fol:v("fol", "min_limit_pct")}, version:P.versions.screens},
        async res => { if (res.ok){ await afterSave(); S.P.mon = null; loadMonitor(); } });
    };
  }
  if (k === "decisions"){
    $$("[data-dec]").forEach(tr => tr.onclick = () => { P.decSel = tr.dataset.dec; render(); });
    if ($("#resetAsk")) $("#resetAsk").onclick = () => { S.confirmReset = true; render(); };
    if ($("#resetNo")) $("#resetNo").onclick = () => { S.confirmReset = false; render(); };
    if ($("#resetYes")) $("#resetYes").onclick = () => { S.confirmReset = false;
      action("flow", "POST", `/api/p/${name}/reset`, {}, async res => { if (res.ok){ P.mon = null; if (BT.name === P.name) BT.req = null; await afterSave(); } }); };
  }
  if (k === "backtest") bindBacktest();
}

/* ---------- step: backtest ----------
   Every number comes from /backtest (backtest_engine.run). Start, costs, lag
   and replay need a run; benchmark and risk-free rate apply to the last result
   (the engine returns every benchmark; Sharpe is rescaled by rf). */
const BT = {name:null, start:null, bench:"VNINDEX", replay:true, draft:null, saved:null, version:null,
            defaults:null, cfgErr:null, res:null, req:null, err:null, running:false, cur:null};
const RUNKEYS = ["brokerage_bps", "sell_tax_bps", "lag_sessions"];
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const spct = (v, d=2) => (v >= 0 ? "+" : "−") + Math.abs(v*100).toFixed(d) + "%";
const spp = (v, d=2) => (v >= 0 ? "+" : "−") + Math.abs(v*100).toFixed(d) + " pp";
const n2 = v => v === null || v === undefined ? "n/a" : (v < 0 ? "−" : "") + Math.abs(v).toFixed(2);
const tone = v => v >= 0 ? "pos" : "negv";
const mpct = v => (v < 0 ? "−" : "") + Math.abs(v*100).toFixed(2) + "%";

function btEnsure(name){
  if (BT.name !== name){ BT.res = null; BT.req = null; BT.err = null; }
  if (BT.name !== name || !BT.draft) btLoad(name).then(() => { if (!BT.res && !BT.err) btRun(); });
}
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
  return q.name !== BT.name || q.start !== BT.start || q.replay !== BT.replay
    || RUNKEYS.some(k => q.cfg[k] !== BT.draft[k]);
}
async function btRun(){
  if (BT.running || !BT.draft) return;
  const req = {name:BT.name, start:BT.start, replay:BT.replay, cfg:{...BT.draft}};
  BT.running = true; render();
  let r;
  try { r = await api("POST", `/api/p/${encodeURIComponent(req.name)}/backtest`,
                      {start:req.start, benchmark:BT.bench, config:req.cfg, mechanical:!req.replay}); }
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

function renderBacktest(P){
  const st = S.state;
  const codes = (st.market.benchmarks || []).map(b => b.code);
  const ss = st.sessions, d = BT.name === P.name ? BT.draft : null, r = BT.name === P.name ? BT.res : null, dirty = btDirty();
  const snapped = btSnap(BT.start);
  const summ = st.portfolios.find(p => p.name === P.name);
  const cfgDirty = d && BT.saved && Object.keys(d).some(k => d[k] !== BT.saved[k]);
  const field = (k, label, step, hint="") => `<div class="field"><label for="bt_${k}">${label}</label><input type="number" id="bt_${k}" data-cfg="${k}" min="0" step="${step}" value="${d ? (k === "risk_free_rate" ? +(d[k]*100).toFixed(4) : d[k]) : ""}">${hint ? `<span class="hint">${hint}</span>` : ""}</div>`;

  let results = "";
  if (r){
    const code = r.benchmarks[BT.bench] ? BT.bench : r.benchmark;
    const bs = r.benchmarks[code].stats, p = bs.portfolio, b = bs.benchmark, rf = d ? d.risk_free_rate : r.config.risk_free_rate;
    const shp = s => s.vol > 0 ? (s.annualised - rf) / s.vol : null;
    const {main, ex} = btCharts(r, code);
    const h = r.holdings_range, reb = r.rebalance, last = r.rebalances.at(-1);
    const breachTol = reb.breach_tolerance === undefined ? 0.10 : reb.breach_tolerance;
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
        <div class="bt-key"><span><i></i>${esc(r.name)}</span><span><i class="b"></i>${esc(code)}</span><span><i class="d"></i>calendar fill</span>${r.timeline ? '<span><i class="d dec"></i>decision fill</span>' : ""}${bs.n_breach ? '<span><i class="d x"></i>breach fill</span>' : ""}${reb.drift_threshold != null ? '<span><i class="d w"></i>drift fill</span>' : ""}</div></div>
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
        ${kv("Rebalances", `${r.timeline ? bs.n_decision + " decision · " : ""}${bs.n_calendar} calendar · ${bs.n_breach} breach · ${reb.drift_threshold != null ? bs.n_drift + " drift" : "drift off"}`)}
        ${kv("Turnover after inception, one-way", pct(bs.turnover, 1))}${kv("Trading costs, incl. inception", (bs.cost*1e4).toFixed(1) + " bps")}
        </tbody></table></div></section>
    </div>
    <section class="panel"><div class="panel-h"><h2>Rebalance log</h2><span class="label">decision at the close · ${c.lag_sessions ? `fill at the open ${c.lag_sessions} session${c.lag_sessions === 1 ? "" : "s"} later` : "fill at the same close"}</span></div>
      <div class="scroll"><table><thead><tr><th>Decision</th><th>Fill</th><th>Trigger</th>${r.timeline ? "<th>Profile</th>" : ""}<th>Policy</th><th>Target as of</th><th class="n">Group drift</th><th class="n">Turnover</th><th class="n">Cost</th><th class="n">Holdings</th><th>Note</th></tr></thead><tbody>
      ${r.rebalances.map(e => `<tr><td class="mono">${e.decision}</td><td class="mono">${e.fill}</td>
        <td>${e.trigger === "drift" ? '<span class="pill warn">drift</span>' : e.trigger === "breach" ? `<span class="pill bad" title="a cap broken by more than ${pct(breachTol, 0)} of its limit; the fill clips it to the cap">breach</span>` : e.trigger === "calendar" ? '<span class="pill info">calendar</span>' : e.trigger === "decision" ? `<span class="pill dec" title="a recorded decision became the target${e.also ? "; also the calendar date" : ""}${e.deferred_from ? `; effective ${e.deferred_from}, deferred while a fill was pending` : ""}">decision${e.also ? " + calendar" : ""}</span>` : '<span class="pill off">inception</span>'}</td>
        ${r.timeline ? `<td class="mono">${esc(e.profile)}</td>` : ""}
        <td>${e.policy === "edge" ? '<span class="pill xs warn" title="broken cap clipped to its limit, the rest untouched">edge</span>' : '<span class="pill xs off" title="the whole book to the standing target">full</span>'}</td>
        <td class="mono">${e.target_as_of}</td>
        <td class="n">${e.drift === null ? "—" : pct(e.drift)}</td><td class="n">${pct(e.turnover, 1)}</td><td class="n">${(e.cost*1e4).toFixed(1)} bps</td>
        <td class="n"><span class="pill xs ${e.in_range ? "ok" : "warn"}">${e.holdings} ${e.in_range ? "in" : "OUTSIDE"} ${h.min}–${h.max}</span></td>
        <td>${e.dropped ? `<span class="err">no float cap, dropped: ${esc(e.dropped)}</span>` : ""}</td></tr>`).join("")}
      </tbody></table></div></section>
    ${r.timeline ? `<section class="panel"><div class="panel-h"><h2>Decision timeline</h2><span class="label">decisions/ replayed on the universe of ${esc(r.as_of)}</span></div>
      <div class="scroll"><table><thead><tr><th>Id</th><th>Kind</th><th>Effective</th><th>Applied</th><th>Status</th><th>Setup</th><th>Note</th><th>Dropped at replay</th></tr></thead><tbody>
      ${r.timeline.map(t => { const p = t.report; const drops = p ? [...Object.values(p.dropped_names).flat(), ...Object.keys(p.lost_groups).map(g => g + " (group)")].join(", ") : "";
        return `<tr><td class="mono">${esc(t.id)}</td><td>${esc(t.kind || "")}</td><td class="mono">${esc(t.effective)}</td><td class="mono">${t.applied ?? "—"}</td><td>${esc(t.status)}</td><td>${t.setup_differs ? '<span class="pill xs warn">setup differs</span>' : ""}</td><td>${esc(t.note || "")}</td><td>${drops ? `<span class="err">${esc(drops)}</span>` : ""}</td></tr>`; }).join("")}
      </tbody></table></div></section>` : ""}
    <section class="panel"><div class="panel-h"><h2>Holdings at the end</h2><span class="label">drifted weight on ${r.end} vs the standing target as of ${last ? last.target_as_of : "—"}</span></div>
      <div class="scroll"><table><thead><tr><th>Ticker</th><th>Group</th><th class="n">Weight</th><th class="n">Target</th><th class="n">Gap</th></tr></thead><tbody>
      ${r.holdings_end.map(x => `<tr><td class="mono">${esc(x.t)}</td><td>${esc(x.group)}</td><td class="n">${pct(x.w)}</td><td class="n">${pct(x.target)}</td><td class="n ${tone(x.w - x.target)}">${spp(x.w - x.target)}</td></tr>`).join("")}
      </tbody></table></div></section>
    </div>`;
  }

  const mand = r ? {reb:r.rebalance, h:r.holdings_range, cons:r.constraints, tac:r.tactical, asof:r.as_of}
    : summ?.statement ? {reb:summ.statement.rebalance, h:summ.statement.holdings, cons:summ.constraints_on.length ? summ.constraints_on.join(", ") : "off", tac:"see the Allocation step", asof:ss.at(-1)} : null;
  return `
    <section class="panel"><div class="panel-b bt-ctl">
      <div class="field"><label for="btStart">Start date</label>
        <div class="inline"><input type="date" id="btStart" min="${ss[0]}" max="${ss.at(-1)}" value="${BT.start ?? ss[0]}" style="max-width:170px">
          <div class="seg" role="group" aria-label="Start presets">${[["first","Earliest"],["q2","Q2"],["q3","Q3"],["m1","1M"]].map(([k, l]) => `<button type="button" data-preset="${k}">${l}</button>`).join("")}</div></div>
        <span class="bt-snap">${snapped ? `first session ${snapped} · ${ss.length - ss.indexOf(snapped)} sessions to ${ss.at(-1)}` : "after the last session"}</span></div>
      <div class="field"><label>Benchmark</label><div class="seg" role="group" aria-label="Benchmark">${codes.map(c => `<button type="button" data-bench="${esc(c)}" aria-pressed="${c === BT.bench}">${esc(c)}</button>`).join("") || '<span class="err">no benchmark in market.db</span>'}</div></div>
      <div class="field"><label>Decisions</label>${summ?.decisions ? `<label class="toggle" title="replay decisions/: the inception holds before its date, each decision is the standing target from its effective date; off holds book.json throughout"><input type="checkbox" id="btReplay" ${BT.replay ? "checked" : ""}> Replay ${summ.decisions}</label>` : '<span class="hint">none recorded; book.json</span>'}</div>
      <div class="field"><button type="button" class="btn ${dirty ? "primary" : ""}" id="btRun" ${BT.running || !d ? "disabled" : ""}>${BT.running ? "Running..." : dirty ? "Run backtest" : "Up to date"}</button></div>
    </div></section>
    ${r && dirty && !BT.running ? `<div class="dirtybar"><span>Settings changed. Results below show the last run.</span><button class="btn sm primary" id="btRun2">Run backtest</button></div>` : ""}
    ${BT.err && BT.name === P.name ? `<p class="note bad" style="white-space:pre-wrap">FAIL  ${esc(BT.err)}</p>` : ""}
    ${!r && !BT.err ? `<p class="note">${BT.running ? "Running the engine..." : "Pick a start date and benchmark, then Run backtest."}</p>` : ""}
    ${results}
    <div class="grid2">
      <section class="panel"><div class="panel-h"><h2>Mandate</h2><span class="pill plain off">statement.json · constraints.json</span></div>
        <div class="panel-b">${mand ? `<dl class="kv bt-mand">
          <dt>Rebalance</dt><dd>${mand.reb.frequency ? `${FREQ[mand.reb.frequency]} (${mand.reb.frequency}), first session of the period` : "No calendar; inception and drift only"}</dd>
          <dt>Drift threshold</dt><dd>${mand.reb.drift_threshold != null ? `${pct(mand.reb.drift_threshold, 1)} at group grain, ½ Σ |gap| vs the standing target` : "Off"}</dd>
          <dt>Breach</dt><dd>${(t => t != null ? `Any cap that is on, broken by more than ${pct(t, 0)} of its limit; the fill clips it to the cap` : "Off; a broken cap never forces a trade")(mand.reb.breach_tolerance === undefined ? 0.10 : mand.reb.breach_tolerance)}</dd>
          <dt>Holdings range</dt><dd>${mand.h.min}–${mand.h.max}, flagged per rebalance</dd>
          <dt>Constraints</dt><dd>${esc(mand.cons)}</dd>
          <dt>Tactical overlay</dt><dd>${esc(mand.tac)}</dd>
          <dt>Universe</dt><dd>every book evaluated on ${esc(mand.asof ?? "the latest session")}</dd>
        </dl><p class="note" style="margin-top:12px">Edit the mandate on the Rebalancing and Constraints steps; the next run reads it.</p>` : '<p class="note warn">No statement.json; the backtest needs its rebalance mandate.</p>'}</div></section>
      <section class="panel"><div class="panel-h"><h2>Backtest settings</h2><span class="mono" style="font-size:11.5px;color:var(--ink-3)">backtest_config.json</span></div>
        ${BT.cfgErr ? `<p class="note bad" style="margin:12px 16px 0">${esc(BT.cfgErr)}. Showing defaults; Save replaces the file.</p>` : ""}
        <div class="panel-b bt-cfg">
          ${field("brokerage_bps", "Brokerage, bps per side", 1)}${field("sell_tax_bps", "Sell tax, bps", 1)}
          ${field("lag_sessions", "Fill lag, sessions", 1, "0–5; fills at that session's open, 0 = same close")}${field("risk_free_rate", "Risk-free rate, % a year", 0.1, "Sharpe only; applies without a run")}
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
        <li><b>Today's universe, held backwards.</b> Every book is evaluated on the group map and listed names of the last session, applied to every past date: survivorship bias flatters every level. A name in a recorded book that no longer trades is dropped (listed in the Decision timeline). Read excess and spreads first.</li>
        <li><b>Recorded decisions are the standing targets.</b> With Replay on, the inception decision holds before its date; each later decision becomes the target from its effective date until the next one, and calendar, drift and breach rebalances trade to it. Statement and constraints are today's: a decision recorded under other settings shows "setup differs".</li>
        <li><b>Rebalances follow the mandate.</b> The standing target is derived at inception, on each decision and on each calendar date: the neutral from that session's free float × official close, plus the book's active pp, with the caps solved. It is held until the next one. Drift is measured against it and a drift fill trades back to it in full. A breach fill only clips the broken cap to its limit and spreads the excess inside the cap's scope by current weight; the rest of the book is untouched. An underweight larger than a past neutral holds the group at 0% (listed in the messages).</li>
        <li><b>Fills at the open.</b> A rebalance is decided at a close and trades at the open of the session the fill lag points to: the held book earns the overnight gap, the new book earns the day. At lag 1 every close is checked; at lag 2 or more the sessions in between are not, while the trade is in flight. The benchmark counts from the start close and the portfolio is cash until the first fill.</li>
      </ul></div></section>`;
}
function bindBacktest(){
  if (!$("#btRun")) return;
  $("#btStart").onchange = e => { BT.start = e.target.value || null; render(); };
  $$("[data-preset]").forEach(b => b.onclick = () => {
    const ss = S.state.sessions, y = ss.at(-1).slice(0, 4);
    const m1 = new Date(ss.at(-1) + "T00:00:00Z"); m1.setUTCMonth(m1.getUTCMonth() - 1);
    BT.start = {first:null, q2:`${y}-04-01`, q3:`${y}-07-01`, m1:m1.toISOString().slice(0, 10)}[b.dataset.preset];
    render();
  });
  $$("[data-bench]").forEach(b => b.onclick = () => { BT.bench = b.dataset.bench; render(); });
  if ($("#btReplay")) $("#btReplay").onchange = e => { BT.replay = e.target.checked; render(); };
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
  const sec = S.scrollTo && $("#sec-" + S.scrollTo);
  if (S.scrollTo && sec){ S.scrollTo = null; sec.scrollIntoView({block:"start"}); }
  else window.scrollTo({top:y});
}

(async () => { await refreshState(); render(); })();
