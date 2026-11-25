import { Viewer } from './viewer.js';
import { elevation, sections, marks, ROLE } from './beamdraw.js';

const $ = (s, el = document) => el.querySelector(s);
const main = $('#main');
const fx = (v, n = 0) => v == null || isNaN(v) ? '—' : Number(v).toLocaleString('en-IN', { maximumFractionDigits: n, minimumFractionDigits: n });
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const PID = 'worklounge';
const S = { project: null, calc: null, base: null, overrides: {}, viewers: [] };
const store = { get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { } } };

async function api(path, body) {
  const r = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => t.hidden = true, 2600); }

async function boot() {
  S.overrides = store.get(`ovr:${PID}`, {});
  const [project, base] = await Promise.all([api(`/api/projects/${PID}`), api(`/api/projects/${PID}/calculate`, { overrides: {} })]);
  S.project = project; S.base = base;
  S.calc = Object.keys(S.overrides).length ? await api(`/api/projects/${PID}/calculate`, { overrides: S.overrides }) : base;
  S.sheets = Object.fromEntries(project.meta.sheets.map(s => [s.id, s]));
  index();
  const m = project.meta;
  $('#projName').innerHTML = `<b>${esc(m.name)}</b>${esc(m.scope)}`;
  $('#sideFoot').innerHTML = `${esc(m.consultant)} · ${esc(m.revision)}<br>${esc(m.status)}`;
  window.addEventListener('hashchange', route);
  route();
}

function index() {
  const c = S.calc;
  S.members = c.members.map(m => {
    const parts = c.beams.filter(b => b.base === m.name).sort((x, y) => (x.kind === 'S' ? 0 : 1) - (y.kind === 'S' ? 0 : 1));
    const span = parts.filter(p => p.kind === 'S').reduce((a, p) => a + p.clear_in, 0) || parts[0].clear_in;
    return { ...m, partsData: parts, size: `${parts[0].b_in}×${parts[0].D_in}`, span_in: span, kg_m: m.kg / (span * 0.0254) };
  });
  S.byName = Object.fromEntries(S.members.map(m => [m.name, m]));
  S.issues = [...S.project.issues, ...c.auto_issues];
  S.issueBeams = {};
  for (const i of S.issues) for (const b of i.beams || []) (S.issueBeams[b] ||= []).push(i);
  S.site = {};
  if (c.notebook) for (const r of c.notebook.rows) for (const n of r.beams) S.site[n.split('-')[0]] = r;
  $('#issueCount').textContent = S.issues.length;
  const n = Object.keys(S.overrides).length; const oc = $('#ovrCount'); oc.hidden = !n; oc.textContent = n;
}

function route() {
  S.viewers.forEach(v => v.destroy()); S.viewers = []; if (S.m3d) { S.m3d.destroy(); S.m3d = null; }
  const [v, ...rest] = (location.hash.slice(1) || 'overview').split('/').map(decodeURIComponent);
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.v === (v === 'beam' ? 'beams' : v)));
  const views = { overview, model, drawings, beams, beam, slab, issues, notebook, assumptions, upload };
  (views[v] || overview)(...rest);
  if (v !== 'model') { main.focus({ preventScroll: true }); window.scrollTo(0, 0); }
}

/* ---------- shared bits ---------- */
function kpis() {
  const s = S.calc.summary, b = S.base.summary;
  const d = (k, n = 0, unit = '') => { const x = s[k] - b[k]; return Math.abs(x) < 1e-6 ? '' : `<span class="d ${x > 0 ? 'up' : 'dn'}">${x > 0 ? '+' : ''}${fx(x, n)}${unit} vs drawings</span>`; };
  return `<div class="kpis">
    <div class="kpi"><span class="k">Steel incl. ${fx(S.calc.assumptions.steel_wastage * 100)}% wastage</span><span class="v">${fx(s.steel_with_wastage_kg / 1000, 2)}<small>t</small></span>${d('steel_with_wastage_kg', 0, ' kg')}</div>
    <div class="kpi"><span class="k">Concrete incl. wastage</span><span class="v">${fx(s.concrete_m3, 2)}<small>m³</small></span>${d('concrete_m3', 2, ' m³')}</div>
    <div class="kpi"><span class="k">Cement</span><span class="v">${fx(s.cement_bags)}<small>bags</small></span>${d('cement_bags')}</div>
    <div class="kpi"><span class="k">Steel + RMC cost</span><span class="v">₹${fx((s.steel_cost + s.rmc_cost) / 1e5, 2)}<small>lakh</small></span>${d('total_per_sqft', 1, ' ₹/sq ft')}</div>
    <div class="kpi"><span class="k">Per sq ft of floor</span><span class="v">₹${fx(s.total_per_sqft)}<small>· ${fx(s.kg_per_sqft, 2)} kg</small></span><span class="d muted">${fx(s.floor_sqft)} sq ft</span></div>
  </div>`;
}
const header = (title, sub, right = '') => `<div class="ph"><div><h1>${title}</h1>${sub ? `<div class="sub">${sub}</div>` : ''}</div><div class="row">${right}</div></div>`;
const sevChip = s => `<span class="chip ${s}">${{ high: 'High', medium: 'Medium', low: 'Low' }[s]}</span>`;
function steps(list) {
  return `<div class="steps">${list.map(s => `<span class="l">${esc(s.label)}</span><span class="fm">${esc(s.formula)}</span><span class="wk">= ${esc(s.working)}</span><span class="v">${fx(s.value, s.unit === 'm³' || s.unit === 'm' ? 2 : s.unit === 'kg' ? 2 : 0)} ${esc(s.unit)}</span>`).join('')}</div>`;
}
function mount(el, sheetId, opts = {}) {
  const sh = S.sheets[sheetId];
  const v = new Viewer(el, sh, { src: `/drawings/${PID}/${sh.svg}`, ...opts });
  S.viewers.push(v); return v;
}

/* ---------- overview ---------- */
function overview() {
  const s = S.calc.summary;
  const top = [...S.members].sort((a, b) => b.kg - a.kg).slice(0, 10), mx = top[0].kg;
  const nb = S.calc.notebook?.totals;
  const open = S.issues.filter(i => i.severity !== 'low');
  main.innerHTML = header(esc(S.project.meta.name), `${esc(S.project.meta.scope)} · ${S.members.length} beams · ${esc(S.project.meta.status)}`,
    `<a class="btn" href="#drawings">Open drawings</a><a class="btn pri" href="#beams">Beam by beam</a>`) + kpis() + `
  <div class="grid g-main">
    <div class="stack">
      <section class="panel"><div class="hd"><h2>Steel by bar size</h2><span class="muted small">kg · rates ${esc('₹/kg')} incl. Fe550 premium</span></div>
        <div class="tw"><table><thead><tr><th>Bar</th><th class="r">Beams</th><th class="r">Slab</th><th class="r">Total</th><th class="r">+ wastage</th><th class="r">Cost ₹</th></tr></thead><tbody>
        ${s.by_dia.map(r => `<tr><td class="mono">${r.dia} mm</td><td class="num">${fx(r.beams)}</td><td class="num">${r.slab ? fx(r.slab) : '—'}</td><td class="num">${fx(r.total)}</td><td class="num">${fx(r.with_wastage)}</td><td class="num">${fx(r.cost)}</td></tr>`).join('')}
        <tr class="tot"><td>Total</td><td class="num">${fx(s.beam_steel_kg)}</td><td class="num">${fx(s.slab_steel_kg)}</td><td class="num">${fx(s.steel_kg)}</td><td class="num">${fx(s.steel_with_wastage_kg)}</td><td class="num">${fx(s.steel_cost)}</td></tr>
        </tbody></table></div></section>
      <section class="panel"><div class="hd"><h2>Concrete and cement</h2><a class="small" href="#slab">Slab working</a></div>
        <div class="pad stack">${steps([
          { label: 'Beams below slab', formula: 'Σ b × (D − slab t) × L', working: `${S.members.length} beams`, value: s.beam_concrete_m3, unit: 'm³' },
          { label: 'Slab', formula: 'net area × thickness', working: `${fx(S.calc.slab.area_m2, 1)} m² × ${fx(S.calc.assumptions.slab_t_in * 25.4)} mm`, value: s.slab_concrete_m3, unit: 'm³' },
          ...s.concrete_steps])}</div></section>
    </div>
    <div class="stack">
      <section class="panel"><div class="hd"><h2>Needs an answer</h2><a class="small" href="#issues">All ${S.issues.length}</a></div>
        ${open.map(i => `<a href="#issues" style="display:flex;gap:10px;padding:10px 16px;border-bottom:1px solid var(--line);text-decoration:none;color:inherit">${sevChip(i.severity)}<span>${esc(i.title)}</span></a>`).join('')}</section>
      ${nb ? `<section class="panel"><div class="hd"><h2>Site QS notebook</h2><a class="small" href="#notebook">Compare</a></div><div class="pad stack small">
        <div class="row"><span class="sp">Notebook, beams</span><b class="mono">${fx(nb.beams.theirs)} kg</b></div>
        <div class="row"><span class="sp">Takeoff, beams</span><b class="mono">${fx(nb.beams.ours)} kg</b></div>
        <div class="row"><span class="sp">Difference</span><b class="mono up">+${fx(nb.beams.diff)} kg (${fx(nb.beams.diff / nb.beams.ours * 100)}%)</b></div>
        <p class="muted">Mostly extra bars counted full length and overall beam length used for every bar.</p></div></section>` : ''}
      <section class="panel"><div class="hd"><h2>Heaviest beams</h2><span class="muted small">kg</span></div><div class="pad stack" style="gap:7px">
        ${top.map(m => `<a href="#beam/${m.name}" class="row" style="text-decoration:none;color:inherit;flex-wrap:nowrap"><span class="mono" style="width:52px">${m.name}</span><span class="hbar sp"><i style="width:${m.kg / mx * 100}%"></i></span><span class="mono" style="width:44px;text-align:right">${fx(m.kg)}</span></a>`).join('')}
      </div></section>
    </div>
  </div>`;
}

/* ---------- drawings ---------- */
const MODES = { steel: 'Steel per metre', site: 'Site check', issues: 'Issues' };
function colorFor(mode, name) {
  const m = S.byName[name];
  if (!m) return '#9CA3AF';
  if (mode === 'issues') return S.issueBeams[name] ? '#B42318' : '#9CA3AF';
  if (mode === 'site') {
    const r = S.site[name]; if (!r || r.diff_pct == null) return '#9CA3AF';
    const p = r.diff_pct; return p > 25 ? '#B42318' : p > 10 ? '#EA580C' : p < -10 ? '#15803D' : '#2563EB';
  }
  const ks = S.members.map(x => x.kg_m).sort((a, b) => a - b), q = ks.findIndex(v => v >= m.kg_m) / ks.length;
  return ['#FDBA74', '#F97316', '#C2410C', '#7C2D12'][Math.min(3, Math.floor(q * 4))];
}
const LEG = {
  steel: `<span><i style="background:#FDBA74"></i>light</span><span><i style="background:#F97316"></i></span><span><i style="background:#C2410C"></i></span><span><i style="background:#7C2D12"></i>heavy kg/m</span>`,
  site: `<span><i style="background:#B42318"></i>QS &gt;25% higher</span><span><i style="background:#EA580C"></i>10–25%</span><span><i style="background:#2563EB"></i>within 10%</span><span><i style="background:#15803D"></i>QS lower</span><span><i style="background:#9CA3AF"></i>no data</span>`,
  issues: `<span><i style="background:#B42318"></i>has an issue</span><span><i style="background:#9CA3AF"></i>none</span>`,
};
function drawings(sheetId = 'layout', target) {
  const mode = store.get('mode', 'steel');
  const sh = S.sheets[sheetId] || S.sheets.layout;
  main.innerHTML = header('Drawings', `${esc(sh.drg)} · ${esc(sh.title)} · scroll or pinch to zoom, drag to move`,
    `<div class="seg" id="modes">${Object.entries(MODES).map(([k, v]) => `<button data-m="${k}" class="${k === mode ? 'on' : ''}">${v}</button>`).join('')}</div>`) + `
  <div class="row">${S.project.meta.sheets.map(s => `<a class="btn sm ${s.id === sh.id ? 'pri' : ''}" href="#drawings/${s.id}">${esc(s.drg)} ${esc(s.title.split(' (')[0])}</a>`).join('')}</div>
  <div class="dside"><div id="vw"></div><aside class="panel" id="pick"><div class="pad muted">Tap a beam marker to see its steel, the working and any issues.</div></aside></div>`;
  const hs = () => Object.entries(sh.hotspots).filter(([n]) => S.byName[n]).map(([n, h]) => ({ id: n, x: h.x, y: h.y, label: n, color: colorFor(store.get('mode', 'steel'), n) }));
  const v = mount($('#vw'), sh.id, { hotspots: hs(), legend: Object.keys(sh.hotspots).length ? LEG[mode] : '', onPick: h => pick(h.id) });
  const pick = name => {
    v.select(name);
    const m = S.byName[name], r = S.site[name], iss = S.issueBeams[name] || [];
    $('#pick').innerHTML = `<div class="hd"><h2>${name}</h2><a class="btn sm pri" href="#beam/${name}">Open</a></div><div class="pad stack small">
      <div class="row"><span class="sp muted">Size · span</span><span class="mono">${m.size}″ · ${fx(m.span_in * 25.4)} mm</span></div>
      <div class="row"><span class="sp muted">Steel</span><b class="mono">${fx(m.kg, 1)} kg</b></div>
      <div class="row"><span class="sp muted">Per metre</span><span class="mono">${fx(m.kg_m, 1)} kg/m</span></div>
      <div>${Object.entries(m.kg_by_dia).map(([d, k]) => `<span class="chip" style="margin:0 4px 4px 0">${d}φ ${fx(k, 1)}</span>`).join('')}</div>
      ${r ? `<div class="row"><span class="sp muted">Site notebook (${esc(r.label)})</span><span class="mono ${r.diff_kg > 0 ? 'up' : 'dn'}">${fx(r.theirs_kg)} vs ${fx(r.ours_kg)}</span></div>` : ''}
      ${iss.map(i => `<div class="row" style="flex-wrap:nowrap;align-items:flex-start">${sevChip(i.severity)}<span>${esc(i.title)}</span></div>`).join('')}
      ${m.parts.length > 1 ? `<div class="muted">Includes ${m.parts.join(', ')}</div>` : ''}</div>`;
    history.replaceState(null, '', `#drawings/${sh.id}/${name}`);
  };
  $('#modes').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    store.set('mode', b.dataset.m);
    document.querySelectorAll('#modes button').forEach(x => x.classList.toggle('on', x === b));
    v.setHotspots(hs()); const lg = $('#vw .legend'); if (lg) lg.innerHTML = LEG[b.dataset.m];
  });
  requestAnimationFrame(() => {
    if (target && target.startsWith('@')) { const [x, y, z] = target.slice(1).split(',').map(Number); v.focus(x, y, z || 3); }
    else if (target && sh.hotspots[target]) { const h = sh.hotspots[target]; v.focus(h.x, h.y, 4); pick(target); }
  });
}

/* ---------- 3D model ---------- */
const M3MODES = { type: 'Element', steel: 'Steel per metre', site: 'Site check', issues: 'Issues' };
const M3LEG = {
  type: `<span><i style="background:var(--b-top)"></i>beam</span><span><i style="background:var(--slab)"></i>slab (measured)</span><span><i style="background:var(--col)"></i>column</span><span><i style="background:var(--muted);opacity:.35"></i>other floors, not measured</span>`,
  steel: LEG.steel, site: LEG.site, issues: LEG.issues,
};
async function model(target) {
  const md = S.project.model;
  if (!md) { main.innerHTML = header('3D model', 'No framing model for this project yet.'); return; }
  const mode = store.get('m3mode', 'type');
  const ghosts = store.get('m3ghost', true), showSlab = store.get('m3slab', true);
  main.innerHTML = header('3D model', `Second floor slab and beams in 3D, from ${esc(md.source)}. Drag to turn, scroll or pinch to zoom, right-drag or two fingers to move. Double-click to zoom to a member.`,
    `<input class="f" id="m3q" placeholder="Go to beam, e.g. B47" aria-label="Go to beam" style="width:170px">`) + `
  <div class="row">
    <div class="seg" id="m3modes">${Object.entries(M3MODES).map(([k, v]) => `<button data-m="${k}" class="${k === mode ? 'on' : ''}">${v}</button>`).join('')}</div>
    <div class="seg"><button data-v="home">3D</button><button data-v="top">Top</button><button data-v="below">From below</button></div>
    <label class="row small" style="gap:6px"><input type="checkbox" id="m3slab" ${showSlab ? 'checked' : ''}>Slab</label>
    <label class="row small" style="gap:6px"><input type="checkbox" id="m3ghost" ${ghosts ? 'checked' : ''}>Other floors</label>
  </div>
  <div class="dside m3">
    <div class="m3wrap"><div id="m3" class="m3canvas"></div>
      <div class="viewer-tools"><button data-z="in" aria-label="Zoom in">+</button><button data-z="out" aria-label="Zoom out">−</button><button data-z="fit" aria-label="Reset view">⤢</button></div>
      <div class="legend m3leg" id="m3leg">${M3LEG[mode]}</div>
      <div class="m3tip" id="m3tip" hidden></div>
    </div>
    <aside class="panel m3side" id="m3pick"><div class="pad stack small">
      <p class="muted">Click a beam, the slab or a column to see how its quantities were worked out.</p>
      <div class="row"><span class="sp">Beams placed in 3D</span><b class="mono">${new Set(md.beams.map(b => b.name.split('-')[0])).size} of ${S.members.length}</b></div>
      ${md.unplaced.length ? `<p class="muted">Not placed (label not matched on the layout): ${md.unplaced.map(n => `<a href="#beam/${n.split('-')[0]}">${n}</a>`).join(', ')}. Their steel is still counted.</p>` : ''}
      <p class="muted">Floor-to-floor height is taken as 3.0 m for the picture. Only the second floor is measured; the other floors repeat its outline faintly for context.</p>
    </div></aside>
  </div>`;
  const { Model3D } = await import('./model3d.js');
  if (location.hash.split('/')[0] !== '#model') return;
  const m3 = S.m3d = new Model3D($('#m3'), {
    model: md, calc: S.calc, onPick: sel => m3panel(sel),
    onHover: (h, e) => {
      const tip = $('#m3tip'); if (!tip) return;
      if (!h || h.kind === 'ghost') { tip.hidden = true; return; }
      const r = $('#m3').getBoundingClientRect();
      tip.textContent = h.kind === 'beam' ? `${h.part} · ${fx(S.byName[h.id]?.kg, 1)} kg` : h.kind === 'slab' ? `Slab · ${fx(S.calc.slab.kg)} kg steel` : `Column ${h.id}`;
      tip.style.left = (e.clientX - r.left + 14) + 'px'; tip.style.top = (e.clientY - r.top + 10) + 'px'; tip.hidden = false;
    },
  });
  const recolor = mm => m3.colorBeams((n, mesh) => mm === 'type' ? (mesh.userData.cant ? '#9A3412' : getComputedStyle(document.documentElement).getPropertyValue('--b-top').trim()) : colorFor(mm, n));
  recolor(mode); m3.show('ghost', ghosts); m3.show('slab', showSlab);
  $('#m3modes').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; store.set('m3mode', b.dataset.m); document.querySelectorAll('#m3modes button').forEach(x => x.classList.toggle('on', x === b)); $('#m3leg').innerHTML = M3LEG[b.dataset.m]; recolor(b.dataset.m); });
  main.querySelectorAll('[data-v]').forEach(b => b.addEventListener('click', () => m3.view(b.dataset.v)));
  main.querySelectorAll('.viewer-tools button').forEach(b => b.addEventListener('click', () => b.dataset.z === 'fit' ? m3.home() : m3.zoom(b.dataset.z === 'in' ? 0.7 : 1.4)));
  $('#m3slab').addEventListener('change', e => { store.set('m3slab', e.target.checked); m3.show('slab', e.target.checked); });
  $('#m3ghost').addEventListener('change', e => { store.set('m3ghost', e.target.checked); m3.show('ghost', e.target.checked); });
  const go = n => { n = n.trim().toUpperCase(); if (!m3.beamMeshes[n]) { toast(S.byName[n] ? `${n} is not placed in 3D yet` : `No beam ${n}`); return; } const sel = { kind: 'beam', id: n }; m3.select(sel); m3.focus(sel); m3panel(sel); };
  $('#m3q').addEventListener('keydown', e => { if (e.key === 'Enter') go(e.target.value); });
  if (target) go(target);
}
function m3panel(sel) {
  const el = $('#m3pick'); if (!el) return;
  if (!sel) return;
  if (sel.kind === 'beam') {
    const m = S.byName[sel.id]; const r = S.site[sel.id], iss = S.issueBeams[sel.id] || [];
    history.replaceState(null, '', `#model/${sel.id}`);
    el.innerHTML = `<div class="hd"><h2>Beam ${m.name}</h2><a class="btn sm pri" href="#beam/${m.name}">Full detail</a></div><div class="pad stack small">
      <div class="row"><span class="sp muted">Size · clear span</span><span class="mono">${m.size}″ · ${fx(m.span_in * 25.4)} mm</span></div>
      <div class="row"><span class="sp">Steel</span><b class="mono">${fx(m.kg, 2)} kg</b></div>
      ${m.partsData.map(p => `<div class="m3part"><div class="row"><b>${esc(p.name)}</b><span class="muted">${p.kind === 'C' ? 'cantilever' : 'span'}</span><span class="sp"></span><span class="mono">${fx(p.kg, 2)} kg</span></div>
        ${marks(p).map(x => `<details class="m3bar"><summary><span class="swatch" style="background:var(--b-${{ 'r-top': 'top', 'r-bot': 'bot', 'r-ext': 'ext', 'r-xtop': 'xtop', 'r-stir': 'stir' }[ROLE[x.role].c]})"></span><span class="mono">${x.mark}</span> ${x.nos} × ${x.dia}φ ${esc(ROLE[x.role].short)}<span class="sp"></span><span class="mono">${fx(x.kg, 2)} kg</span></summary>
          <p class="why">${esc(x.why)}</p>${steps(x.steps)}</details>`).join('')}
        <details class="m3bar"><summary>Concrete below slab<span class="sp"></span><span class="mono">${fx(p.concrete.value, 3)} m³</span></summary>${steps([p.concrete])}</details></div>`).join('')}
      ${r ? `<div class="row"><span class="sp muted">Site notebook (${esc(r.label)})</span><span class="mono ${r.diff_kg > 0 ? 'up' : 'dn'}">${fx(r.theirs_kg)} vs ${fx(r.ours_kg)} kg</span></div>` : ''}
      ${iss.map(i => `<div class="row" style="flex-wrap:nowrap;align-items:flex-start">${sevChip(i.severity)}<span>${esc(i.title)}</span></div>`).join('')}
      <a class="small" href="#drawings/layout/${m.name}">Show on drawing</a></div>`;
  } else if (sel.kind === 'slab') {
    const sl = S.calc.slab, a = S.calc.assumptions;
    el.innerHTML = `<div class="hd"><h2>Second floor slab</h2><a class="btn sm pri" href="#slab">Full detail</a></div><div class="pad stack small">
      <div class="row"><span class="sp muted">Thickness</span><span class="mono">${a.slab_t_in}″ (${fx(a.slab_t_in * 25.4)} mm)</span></div>
      <div class="row"><span class="sp">Steel</span><b class="mono">${fx(sl.kg, 1)} kg</b></div>
      <div class="row"><span class="sp">Concrete</span><b class="mono">${fx(sl.concrete_m3, 2)} m³</b></div>
      ${sl.steps.map(s => `<details class="m3bar"><summary>${esc(s.label)}<span class="sp"></span><span class="mono">${fx(s.value, s.unit === 'kg' ? 1 : 2)} ${esc(s.unit)}</span></summary>${steps([s])}</details>`).join('')}
      <p class="muted">The 3D footprint is traced from the beam layout (${fx(S.project.model.slab.footprint_sqft)} sq ft inside the beam edges); the estimate uses the measured ${fx(a.floor_sqft)} sq ft outline less openings.</p></div>`;
  } else if (sel.kind === 'column') {
    const c = S.project.model.columns;
    el.innerHTML = `<div class="hd"><h2>Column ${esc(sel.id)}</h2></div><div class="pad stack small">
      <p>Columns are outside this estimate, which covers the second floor slab and beams only.</p>
      <div class="row"><span class="sp muted">Columns on the layout</span><span class="mono">${c.length}</span></div>
      <div class="row"><span class="sp muted">Size as drawn</span><span class="mono">9″ × 24″ (229 × 610)</span></div>
      <p class="muted">Add the column schedule to take off column steel and concrete.</p></div>`;
  } else if (sel.kind === 'ghost') {
    el.innerHTML = `<div class="hd"><h2>Floor ${sel.level ?? ''}</h2></div><div class="pad small stack"><p>Not measured. Only the second floor structural drawings have been received; this outline repeats the second floor for context.</p><a href="#upload">Add drawings for other floors</a></div>`;
  }
}

/* ---------- beams table ---------- */
function beams() {
  const sort = store.get('bsort', { k: 'name', d: 1 }); let q = store.get('bq', '');
  const nat = s => s.replace(/\d+/g, n => n.padStart(4, '0'));
  const cols = [['name', 'Beam'], ['size', 'b × D (in)'], ['span_in', 'Span (mm)'], ['kg', 'Steel kg'], ['kg_m', 'kg/m'], ['site', 'Site QS'], ['iss', 'Issues']];
  const render = () => {
    const rows = S.members.filter(m => !q || m.name.toLowerCase().includes(q.toLowerCase())).map(m => ({ ...m, site: S.site[m.name]?.diff_pct ?? null, iss: (S.issueBeams[m.name] || []).length }));
    rows.sort((a, b) => { const k = sort.k; const x = k === 'name' ? nat(a.name) : a[k] ?? -1e9, y = k === 'name' ? nat(b.name) : b[k] ?? -1e9; return (x > y ? 1 : x < y ? -1 : 0) * sort.d; });
    const mx = Math.max(...S.members.map(m => m.kg));
    $('#bt').innerHTML = `<thead><tr>${cols.map(([k, l]) => `<th class="sort ${['name', 'size'].includes(k) ? '' : 'r'} ${sort.k === k ? (sort.d > 0 ? 'asc' : 'desc') : ''}" data-k="${k}">${l}</th>`).join('')}<th>Bars</th></tr></thead><tbody>
      ${rows.map(m => { const r = S.site[m.name]; return `<tr class="click" data-n="${m.name}"><td class="mono"><b>${m.name}</b>${m.parts.length > 1 ? ' <span class="muted small">+cant.</span>' : ''}</td><td class="mono">${m.size}</td><td class="num">${fx(m.span_in * 25.4)}</td>
      <td class="num"><span class="row" style="justify-content:flex-end;flex-wrap:nowrap"><span class="hbar" style="width:70px"><i style="width:${m.kg / mx * 100}%"></i></span>${fx(m.kg, 1)}</span></td><td class="num">${fx(m.kg_m, 1)}</td>
      <td class="num ${r ? (r.diff_kg > 0 ? 'up' : 'dn') : ''}">${r && r.diff_pct != null ? `${r.diff_pct > 0 ? '+' : ''}${fx(r.diff_pct)}%` : '—'}</td><td class="r">${m.iss ? `<span class="chip medium">${m.iss}</span>` : ''}</td>
      <td class="small muted">${Object.entries(m.kg_by_dia).map(([d]) => d + 'φ').join(' ')}</td></tr>`; }).join('')}</tbody>
      <tfoot><tr class="tot"><td colspan="3">${rows.length} beams</td><td class="num">${fx(rows.reduce((a, m) => a + m.kg, 0))}</td><td colspan="4"></td></tr></tfoot>`;
  };
  main.innerHTML = header('Beams', 'Steel per beam as drawn on the schedule. Cantilever extensions are included with their beam.',
    `<input class="f" id="bq" placeholder="Find a beam, e.g. B47" value="${esc(q)}" aria-label="Find a beam">`) +
    `<section class="panel"><div class="tw"><table id="bt"></table></div></section>
     ${S.calc.chajja ? `<section class="panel pad small">Plus chajja beams CB1: ${S.calc.chajja.count} nos × ${S.calc.chajja.length_in}″, <b class="mono">${fx(S.calc.chajja.kg, 1)} kg</b>. ${S.calc.chajja.rows.map(r => `${r.nos} × ${r.dia}φ ${r.role.toLowerCase()} @ ${fx(r.cut_mm)}`).join(' · ')}.</section>` : ''}`;
  render();
  $('#bq').addEventListener('input', e => { q = e.target.value; store.set('bq', q); render(); });
  $('#bt').addEventListener('click', e => {
    const th = e.target.closest('th.sort'); if (th) { const k = th.dataset.k; store.set('bsort', { k, d: sort.k === k ? -sort.d : (k === 'name' ? 1 : -1) }); return beams(); }
    const tr = e.target.closest('tr[data-n]'); if (tr) location.hash = '#beam/' + tr.dataset.n;
  });
}

/* ---------- beam detail ---------- */
function bbs(part) {
  const rows = marks(part);
  return `<table class="bbs"><thead><tr><th></th><th>Mark</th><th>Bar</th><th class="r">Nos</th><th class="r">Dia</th><th class="r">Cut length mm</th><th class="r">Total m</th><th class="r">kg</th></tr></thead><tbody>
    ${rows.map((r, i) => `<tr class="click" data-i="${i}"><td><span class="caret">▸</span></td><td class="mono"><span class="swatch" style="background:var(--b-${{ 'r-top': 'top', 'r-bot': 'bot', 'r-ext': 'ext', 'r-xtop': 'xtop', 'r-stir': 'stir' }[ROLE[r.role].c]})"></span>${r.mark}</td><td>${esc(r.role)}${r.role === 'Stirrups' ? ` <span class="muted small">@${r.spacing.end_in}″ ends / ${r.spacing.mid_in}″ middle</span>` : ''}</td>
      <td class="num">${r.nos}</td><td class="num">${r.dia}</td><td class="num">${fx(r.cut_mm)}</td><td class="num">${fx(r.total_m, 2)}</td><td class="num">${fx(r.kg, 2)}</td></tr>
      <tr class="exp" data-x="${i}" hidden><td></td><td colspan="7"><p class="why">${esc(r.why)}</p>${steps(r.steps)}</td></tr>`).join('')}
    <tr class="tot"><td></td><td colspan="6">Total ${part.name}</td><td class="num">${fx(part.kg, 2)}</td></tr></tbody></table>`;
}
function beam(name) {
  const m = S.byName[name];
  if (!m) { main.innerHTML = header('Beam not found', `<a href="#beams">Back to beams</a>`); return; }
  const a = S.calc.assumptions, r = S.site[name], iss = S.issueBeams[name] || [];
  const order = S.members.map(x => x.name), i = order.indexOf(name);
  const loc = S.project.meta.sheets.filter(s => s.hotspots[name]);
  main.innerHTML = `<div class="crumb"><a href="#beams">Beams</a> / ${name}</div>` + header(`Beam ${name}`,
    `${m.size}″ · clear span ${fx(m.span_in * 25.4)} mm · ${fx(m.kg, 1)} kg steel · ${fx(m.kg_m, 1)} kg/m`,
    `${i > 0 ? `<a class="btn sm" href="#beam/${order[i - 1]}">← ${order[i - 1]}</a>` : ''}${i < order.length - 1 ? `<a class="btn sm" href="#beam/${order[i + 1]}">${order[i + 1]} →</a>` : ''}`) + `
  <div class="grid g-main">
    <div class="stack">
      ${m.partsData.map((p, pi) => `<section class="panel bd"><div class="hd"><h2>${p.kind === 'C' ? `${esc(p.name)} · cantilever` : 'Elevation as drawn'}</h2>
        <div class="lg"><span><i style="background:var(--b-top)"></i>top</span><span><i style="background:var(--b-xtop)"></i>extra top</span><span><i style="background:var(--b-bot)"></i>bottom</span><span><i style="background:var(--b-ext)"></i>extra bottom</span><span><i style="background:var(--b-stir)"></i>stirrups</span></div></div>
        <div class="pad stack"><div class="scroll">${elevation(p, a)}</div><div class="row" style="justify-content:center">${sections(p, a)}</div>
        <p class="muted small">Length to scale; depth exaggerated. Dimensions in mm.</p></div>
        <div class="tw" data-bbs="${pi}">${bbs(p)}</div></section>`).join('')}
    </div>
    <div class="stack">
      <section class="panel"><div class="hd"><h2>Summary</h2></div><div class="pad stack small">
        ${Object.entries(m.kg_by_dia).map(([d, k]) => `<div class="row"><span class="mono" style="width:44px">${d}φ</span><span class="hbar sp"><i style="width:${k / m.kg * 100}%"></i></span><span class="mono" style="width:60px;text-align:right">${fx(k, 1)}</span></div>`).join('')}
        <div class="row" style="border-top:1px solid var(--line);padding-top:8px"><span class="sp">Steel</span><b class="mono">${fx(m.kg, 2)} kg</b></div>
        ${m.partsData.map(p => `<div class="row"><span class="sp muted">Concrete below slab${m.partsData.length > 1 ? ' · ' + p.name : ''}</span><span class="mono">${fx(p.concrete.value, 3)} m³</span></div>`).join('')}
        <p class="muted">${esc(m.partsData[0].concrete.formula)} = ${esc(m.partsData[0].concrete.working)}</p></div></section>
      ${r ? siteCard(r, name) : ''}
      ${iss.length ? `<section class="panel"><div class="hd"><h2>Issues</h2></div>${iss.map(i => `<div class="pad" style="border-bottom:1px solid var(--line)"><div class="row">${sevChip(i.severity)}<b>${esc(i.title)}</b></div><p class="small muted" style="margin-top:4px">${esc(i.ask)}</p></div>`).join('')}</section>` : ''}
      ${loc.map(s => `<section class="panel"><div class="hd"><h2>${esc(s.drg)}</h2><a class="small" href="#drawings/${s.id}/${name}">Open sheet</a></div><div class="mini" id="mini-${s.id}"></div></section>`).join('')}
      <section class="panel pad small muted">Assumptions used: Ld ${a.ld_factor}d · cover ${a.cover_mm} mm · bottom anchorage ${a.bot_anch_mm} mm · extra bottom ${a.extra_bot_frac}L · slab ${a.slab_t_in}″. <a href="#assumptions">Change</a></section>
    </div>
  </div>`;
  main.querySelectorAll('[data-bbs]').forEach(t => t.addEventListener('click', e => {
    const tr = e.target.closest('tr[data-i]'); if (!tr) return;
    const x = t.querySelector(`tr[data-x="${tr.dataset.i}"]`); x.hidden = !x.hidden; tr.classList.toggle('open', !x.hidden);
  }));
  for (const s of loc) {
    const h = s.hotspots[name];
    const v = mount($(`#mini-${s.id}`), s.id, { hotspots: [{ id: name, x: h.x, y: h.y, label: name, color: '#C2410C' }], tools: false, onPick: () => location.hash = `#drawings/${s.id}/${name}` });
    requestAnimationFrame(() => v.focus(h.x, h.y, 5));
  }
}
function siteCard(r, name) {
  const dias = [...new Set([...Object.keys(r.ours_by_dia || {}), ...Object.keys(r.theirs_by_dia || {})])].sort((a, b) => a - b);
  return `<section class="panel"><div class="hd"><h2>Site QS notebook</h2><a class="small" href="#notebook">All beams</a></div><div class="pad stack small">
    <div class="muted">Notebook entry “${esc(r.label)}”${r.beams.length > 1 ? ` covers ${r.beams.map(b => b.split('-')[0]).filter((v, i, x) => x.indexOf(v) === i).join(', ')}` : ''}.</div>
    ${r.theirs_by_dia ? `<div class="tw"><table><thead><tr><th>Bar</th><th class="r">Notebook ft</th><th class="r">Notebook kg</th><th class="r">Takeoff kg</th></tr></thead><tbody>
      ${dias.map(d => `<tr><td class="mono">${d}φ</td><td class="num">${r.feet[d] ? r.feet[d].join(' + ') : '—'}</td><td class="num">${fx(r.theirs_by_dia[d], 1)}</td><td class="num">${fx(r.ours_by_dia[d], 1)}</td></tr>`).join('')}
      <tr class="tot"><td>Total</td><td></td><td class="num">${fx(r.theirs_kg, 1)}</td><td class="num">${fx(r.ours_kg, 1)}</td></tr></tbody></table></div>` :
      `<div class="row"><span class="sp">Notebook</span><b class="mono">${fx(r.theirs_kg)} kg</b></div><div class="row"><span class="sp">Takeoff</span><b class="mono">${fx(r.ours_kg)} kg</b></div>`}
    <div class="row"><span class="sp">Difference</span><b class="mono ${r.diff_kg > 0 ? 'up' : 'dn'}">${r.diff_kg > 0 ? '+' : ''}${fx(r.diff_kg, 1)} kg (${r.diff_pct > 0 ? '+' : ''}${fx(r.diff_pct)}%)</b></div>
    ${r.note ? `<p class="muted">${esc(r.note)}</p>` : ''}</div></section>`;
}

/* ---------- slab ---------- */
function slab() {
  const sl = S.calc.slab, a = S.calc.assumptions;
  main.innerHTML = header('Slab', `${a.slab_t_in}″ slab · ${fx(sl.net_sqft)} sq ft net · ${fx(sl.kg)} kg steel · ${fx(sl.concrete_m3, 2)} m³ concrete`) + `
  <div class="grid g-main"><div class="stack">
    <section class="panel"><div class="hd"><h2>How the slab was measured</h2></div><div class="pad stack">${steps(sl.steps)}
      <div class="row" style="border-top:1px solid var(--line);padding-top:10px"><span class="sp">Total slab steel</span><b class="mono">${fx(sl.kg, 1)} kg</b></div></div></section>
    <section class="panel pad small muted">The slab is measured with average spacings read off the bottom and top slab layouts, not panel by panel. Panel-by-panel reading is planned for the upload pipeline. Spacings and extension are editable in <a href="#assumptions">Assumptions</a>.</section>
  </div><div class="stack">
    <section class="panel"><div class="hd"><h2>${esc(S.sheets.slabbot.drg)} bottom</h2><a class="small" href="#drawings/slabbot">Open</a></div><div class="mini" id="m1" style="height:300px"></div></section>
    <section class="panel"><div class="hd"><h2>${esc(S.sheets.slabtop.drg)} top</h2><a class="small" href="#drawings/slabtop">Open</a></div><div class="mini" id="m2" style="height:300px"></div></section>
  </div></div>`;
  mount($('#m1'), 'slabbot', { tools: false }); mount($('#m2'), 'slabtop', { tools: false });
}

/* ---------- issues ---------- */
function whereLink(w) {
  if (!w) return '';
  if (w.beam) return `#drawings/${w.sheet}/${w.beam}`;
  if (w.x) return `#drawings/${w.sheet}/@${w.x},${w.y},${w.zoom || 3}`;
  return `#drawings/${w.sheet}`;
}
function issues() {
  const order = { high: 0, medium: 1, low: 2 };
  const list = [...S.issues].sort((a, b) => order[a.severity] - order[b.severity]);
  main.innerHTML = header('Issues', 'Things the drawings do not say, or say twice. Each one names the value we used until the consultant answers.',
    `<button class="btn" id="copy">Copy questions for consultant</button>`) + `
  <section class="panel">${list.map(i => `<div class="iss"><div><div class="row">${sevChip(i.severity)}<span class="chip">${esc(i.kind)}</span>${i.auto ? '<span class="chip acc">found by Takeoff</span>' : ''}</div>
    <div class="t" style="margin-top:6px">${esc(i.title)}</div><p class="small" style="margin-top:4px;color:var(--fg2)">${esc(i.detail)}</p>
    <dl><dt>Using for now</dt><dd>${esc(i.assumption)}</dd><dt>Affects</dt><dd>${esc(i.affects)}</dd><dt>Ask</dt><dd>${esc(i.ask)}</dd>
    ${i.beams ? `<dt>Beams</dt><dd>${i.beams.map(b => S.byName[b] ? `<a href="#beam/${b}">${b}</a>` : b).join(', ')}</dd>` : ''}</dl></div>
    <div class="acts">${i.where ? `<a class="btn sm" href="${whereLink(i.where)}">Show on drawing</a>` : ''}</div></div>`).join('')}</section>`;
  $('#copy').addEventListener('click', async () => {
    const txt = `${S.project.meta.name}: questions on ${S.project.meta.scope} (${S.project.meta.revision})\n\n` + list.map((i, n) => `${n + 1}. ${i.ask} (${i.title}; using ${i.assumption} for now)`).join('\n');
    try { await navigator.clipboard.writeText(txt); toast('Questions copied'); } catch { toast('Copy failed: select the text manually'); }
  });
}

/* ---------- site notebook ---------- */
function notebook() {
  const nb = S.calc.notebook; if (!nb) { main.innerHTML = header('Site check', 'No site takeoff loaded for this project.'); return; }
  const t = nb.totals, mx = Math.max(...nb.rows.map(r => Math.abs(r.diff_kg)));
  main.innerHTML = header('Site check', `Takeoff against the site QS's handwritten notebook. ${esc(nb.source)}.`) + `
  <div class="kpis" style="grid-template-columns:repeat(4,minmax(0,1fr))">
    <div class="kpi"><span class="k">Notebook, all beams</span><span class="v">${fx(t.beams.theirs)}<small>kg</small></span></div>
    <div class="kpi"><span class="k">Takeoff, all beams</span><span class="v">${fx(t.beams.ours)}<small>kg</small></span><span class="d muted">incl. CB1 ${fx(t.chajja.ours)} kg</span></div>
    <div class="kpi"><span class="k">Difference</span><span class="v up">+${fx(t.beams.diff)}<small>kg</small></span><span class="d up">+${fx(t.beams.diff / t.beams.ours * 100, 1)}%</span></div>
    <div class="kpi"><span class="k">Like for like (no CB1)</span><span class="v">+${fx(t.beams.diff + t.chajja.ours)}<small>kg</small></span><span class="d muted">CB1 not in notebook</span></div>
  </div>
  <section class="panel pad small"><b>Why the notebook is higher.</b> The QS runs extra top and bottom bars the full beam length and uses the overall beam length (over the columns) for every bar. Takeoff cuts bars to the drawn lengths. Stirrup counts also differ beam by beam. ${esc(nb.page1_note)}</section>
  <section class="panel"><div class="tw"><table><thead><tr><th>Notebook entry</th><th>Page</th><th class="r">Notebook kg</th><th class="r">Takeoff kg</th><th class="r">Diff kg</th><th>Takeoff lower ← → Notebook higher</th><th class="r">%</th></tr></thead><tbody>
    ${nb.rows.map(r => { const w = Math.abs(r.diff_kg) / mx * 50; const first = r.beams[0]?.split('-')[0];
      return `<tr class="${first && S.byName[first] ? 'click' : ''}" data-n="${first || ''}"><td><b class="mono">${esc(r.label)}</b>${r.note ? ` <span class="muted small" title="${esc(r.note)}">ⓘ</span>` : ''}</td><td class="num">${r.page}</td><td class="num">${fx(r.theirs_kg)}</td><td class="num">${fx(r.ours_kg)}</td>
      <td class="num ${r.diff_kg > 0 ? 'up' : 'dn'}">${r.diff_kg > 0 ? '+' : ''}${fx(r.diff_kg)}</td><td><div class="diffbar"><i class="${r.diff_kg > 0 ? 'p' : 'n'}" style="width:${w}%"></i></div></td><td class="num">${r.diff_pct == null ? '—' : fx(r.diff_pct) + '%'}</td></tr>`; }).join('')}
    <tr class="tot"><td>Page 1 (B1–B35)</td><td></td><td class="num">${fx(t.page1.theirs)}</td><td class="num">${fx(t.page1.ours)}</td><td class="num up">+${fx(t.page1.theirs - t.page1.ours)}</td><td colspan="2"></td></tr>
    <tr class="tot"><td>Pages 2+ (B36–B93)</td><td></td><td class="num">${fx(t.page2.theirs)}</td><td class="num">${fx(t.page2.ours)}</td><td class="num up">+${fx(t.page2.theirs - t.page2.ours)}</td><td colspan="2"></td></tr>
  </tbody></table></div></section>`;
  main.querySelector('tbody').addEventListener('click', e => { const tr = e.target.closest('tr.click'); if (tr) location.hash = '#beam/' + tr.dataset.n; });
}

/* ---------- assumptions ---------- */
function assumptions() {
  const defs = S.project.assumptions, a = S.calc.assumptions;
  const groups = [...new Set(defs.map(d => d.group))];
  main.innerHTML = header('Assumptions', 'Values the drawings do not give, plus rates. Change any value and every quantity, drawing and explanation updates. Changes are kept in this browser only.',
    `<button class="btn" id="reset" ${Object.keys(S.overrides).length ? '' : 'disabled'}>Back to drawing values</button>`) + kpis() + `
  <section class="panel"><div class="tw"><table class="asm"><thead><tr><th>Value</th><th class="r">Used</th><th>Unit</th><th>Where it comes from</th></tr></thead><tbody>
    ${groups.map(g => `<tr><td colspan="4" style="background:var(--sunk)"><h3>${g}</h3></td></tr>` + defs.filter(d => d.group === g).map(d => `
      <tr class="${d.key in S.overrides ? 'changed' : ''}"><td>${esc(d.label)} ${d.confirm ? '<span class="chip medium">confirm</span>' : ''}</td>
      <td class="r"><input class="f" type="number" step="any" id="as-${d.key}" data-k="${d.key}" value="${a[d.key]}" aria-label="${esc(d.label)}"></td>
      <td class="muted small">${esc(d.unit)}</td><td class="src">${esc(d.source)}${d.key in S.overrides ? `<br><b>Drawing value ${d.value}</b>` : ''}</td></tr>`).join('')).join('')}
  </tbody></table></div></section>`;
  let h;
  main.querySelectorAll('input[data-k]').forEach(inp => inp.addEventListener('input', () => {
    clearTimeout(h); h = setTimeout(async () => {
      const k = inp.dataset.k, v = parseFloat(inp.value), def = defs.find(d => d.key === k);
      if (isNaN(v)) return;
      if (v === def.value) delete S.overrides[k]; else S.overrides[k] = v;
      await recalc(); const pos = inp.id; assumptions(); const el = document.getElementById(pos); el && (el.focus(), el.select());
    }, 450);
  }));
  $('#reset').addEventListener('click', async () => { S.overrides = {}; await recalc(); assumptions(); toast('Back to drawing values'); });
}
async function recalc() {
  store.set(`ovr:${PID}`, S.overrides);
  S.calc = Object.keys(S.overrides).length ? await api(`/api/projects/${PID}/calculate`, { overrides: S.overrides }) : S.base;
  index();
}

/* ---------- upload (next) ---------- */
function upload() {
  main.innerHTML = header('Add drawings', 'Coming next: upload a new project\'s PDFs and get the same views.') + `
  <div class="grid g2"><section class="panel pad stack">
    <div class="drop"><b style="color:var(--fg)">Drop structural PDFs here</b><span>Beam layout, beam schedules, slab layouts</span><button class="btn" disabled>Choose files</button></div>
    <p class="small muted">Upload is switched off in this version. The Work Lounge pilot above was read with the same steps the uploader will run.</p></section>
  <section class="panel pad stack"><h2>What happens to an upload</h2><ol class="flow small">
    <li>Each sheet is stored and shown here at full resolution, so you can zoom into any detail.</li>
    <li>Beam names, sizes, spans and bar callouts are read from the layout and the schedules. Anything unreadable is listed for you to fill in, never guessed.</li>
    <li>Drawings are cross-checked: sizes that disagree between sheets, missing beam numbers, repeated rows, missing grade or cover. These become Issues.</li>
    <li>You confirm the values the drawings don't give. Nothing is calculated until every value is filled.</li>
    <li>Quantities, bar schedules and costs appear with the working for every number, as in this pilot.</li>
  </ol></section></div>`;
}

boot().catch(e => { main.innerHTML = `<div class="pad">Could not load the project. ${esc(e.message)}</div>`; console.error(e); });
