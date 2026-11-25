import { Viewer } from './viewer.js';
import { elevation, sections, marks, ROLE } from './beamdraw.js';

const $ = (s, el = document) => el.querySelector(s);
const main = $('#main');
const fx = (v, n = 0) => v == null || isNaN(v) ? '—' : Number(v).toLocaleString('en-IN', { maximumFractionDigits: n, minimumFractionDigits: n });
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
let PID = null, P = '#';          // current project slug, and the hash prefix for its views: #p/<slug>/
const S = { me: undefined, project: null, calc: null, base: null, overrides: {}, viewers: [] };
const store = { get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { } } };
const PROJECT_VIEWS = ['overview', 'model', 'drawings', 'beams', 'beam', 'slab', 'issues', 'notebook', 'assumptions', 'upload', 'review'];
const NEEDS_CALC = new Set(PROJECT_VIEWS.filter(v => !['overview', 'upload', 'review'].includes(v)));

class ApiError extends Error { constructor(status, detail) { super(detail || `Request failed (${status})`); this.status = status; } }
async function api(path, body, method = 'POST') {
  const r = await fetch(path, body ? { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
  if (r.status === 401 && !path.startsWith('/api/auth/')) { S.me = null; go('#signin'); throw new ApiError(401, 'Sign in required'); }
  if (!r.ok) {
    let d; try { d = (await r.json()).detail; } catch { }
    throw new ApiError(r.status, typeof d === 'string' ? d : Array.isArray(d) ? d.map(x => x.msg).join('; ') : null);
  }
  return r.status === 204 ? null : r.json();
}
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => t.hidden = true, 2600); }
const go = (h, replace = false) => { if (replace) { history.replaceState(null, '', h); route(); } else if (location.hash !== h) location.hash = h; else route(); };

async function boot() {
  window.addEventListener('hashchange', route);
  try { S.me = await api('/api/me'); } catch (e) { if (e.status !== 401) throw e; S.me = null; }
  route();
}

async function openProject(pid) {
  if (PID === pid && S.project) return;
  PID = pid; P = `#p/${encodeURIComponent(pid)}/`;
  S.project = S.calc = S.base = null;
  S.overrides = store.get(`ovr:${PID}`, {});
  const project = await api(`/api/projects/${PID}`);
  if (PID !== pid) return;
  S.project = project;
  S.sheets = Object.fromEntries((project.meta.sheets || []).map(s => [s.id, s]));
  if (project.ready) {
    S.base = await api(`/api/projects/${PID}/calculate`, { overrides: {} });
    S.calc = Object.keys(S.overrides).length ? await api(`/api/projects/${PID}/calculate`, { overrides: S.overrides }) : S.base;
    index();
  }
}

/* The side bar: inside a project it shows the project's views; elsewhere only the brand and the account. */
function shell(mode) {
  document.body.dataset.mode = mode;         // 'bare' (sign-in), 'home' (projects), 'project'
  $('#nav').querySelectorAll('a').forEach(a => {
    a.href = P + a.dataset.v;
    a.hidden = mode !== 'project' || (NEEDS_CALC.has(a.dataset.v) && !S.project?.ready && !(a.dataset.v === 'model' && S.project?.model));
  });
  const m = S.project?.meta;
  $('#projName').hidden = mode !== 'project';
  if (m) $('#projName').innerHTML = `<a href="#projects" class="small">← All projects</a><b>${esc(m.name)}</b>${esc(m.scope || [m.client, m.city].filter(Boolean).join(' · '))}`;
  const who = S.me ? `<div class="acct"><div>${esc(S.me.email)}</div><button class="link" id="signout">Sign out</button></div>` : '';
  $('#sideFoot').innerHTML = (mode === 'project' && m ? `<div class="pmeta">${[m.consultant, m.revision].filter(Boolean).map(esc).join(' · ')}<br>${esc(m.status)}<hr></div>` : '') + who;
  $('#signout')?.addEventListener('click', signOut);
}

let routing = 0;
async function route() {
  const mine = ++routing;
  S.viewers.forEach(v => v.destroy()); S.viewers = []; if (S.m3d) { S.m3d.destroy(); S.m3d = null; }
  const parts = location.hash.slice(1).split('/').map(decodeURIComponent);
  if (PROJECT_VIEWS.includes(parts[0])) return go('#p/worklounge/' + location.hash.slice(1), true);   // links from before projects
  if (parts[0] === 'signin') { shell('bare'); return signin(parts[1]); }
  if (S.me === null) return go('#signin', true);
  if (!parts[0] || parts[0] === 'projects') { shell('home'); return projectList(); }
  if (parts[0] === 'new') { shell('home'); return newProject(); }
  if (parts[0] !== 'p' || !parts[1]) return go('#projects', true);
  let [, pid, v = 'overview', ...rest] = parts;
  if (PID !== pid || !S.project) {
    shell('home'); main.innerHTML = '<div class="loading">Loading project…</div>';
    try { await openProject(pid); } catch (e) {
      if (mine !== routing || e.status === 401) return;
      PID = null; S.project = null;
      main.innerHTML = header('Project not found', 'It does not exist, or you have not been added to it.', '<a class="btn" href="#projects">All projects</a>');
      return;
    }
    if (mine !== routing) return;
  }
  if (!PROJECT_VIEWS.includes(v) || (NEEDS_CALC.has(v) && !S.project.ready && !(v === 'model' && S.project.model))) return go(P + 'overview', true);
  S.view = v;
  shell('project');
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.v === (v === 'beam' ? 'beams' : v)));
  const views = { overview: S.project.ready ? overview : setup, model, drawings, beams, beam, slab, issues, notebook, assumptions, upload, review };
  views[v](...rest);
  if (v !== 'model') { main.focus({ preventScroll: true }); window.scrollTo(0, 0); }
}

/* ---------- sign in ---------- */
function signin(state) {
  const expired = state === 'expired';
  main.innerHTML = `<div class="auth panel pad stack">
    <a class="brand" href="#projects"><span class="logo" aria-hidden="true"></span>Takeoff</a>
    <h1>Sign in</h1>
    ${expired ? '<p class="note warn">That sign-in link has expired or was already used. Ask for a new one below.</p>' : ''}
    <form id="signinForm" class="stack" novalidate>
      <label class="stack small">Work email<input class="f" type="email" name="email" autocomplete="email" required autofocus></label>
      <button class="btn pri" type="submit">Email me a sign-in link</button>
    </form>
    <p class="small muted">Takeoff is in a private beta. If your email has been invited, the link arrives within a minute and works for 15 minutes.</p>
  </div>`;
  $('#signinForm').addEventListener('submit', async e => {
    e.preventDefault();
    const f = e.target, email = f.email.value.trim(), btn = f.querySelector('button');
    if (!f.email.checkValidity() || !email) { f.email.focus(); toast('Enter a valid email address'); return; }
    btn.disabled = true;
    try {
      await api('/api/auth/link', { email });
      f.outerHTML = `<div class="note ok"><b>Check your email.</b> If <b>${esc(email)}</b> has been invited, a sign-in link is on its way. Open it on this device.</div>`;
    } catch (err) {
      btn.disabled = false;
      toast(err.status === 429 ? 'Too many links asked for. Try again in 15 minutes.' : err.message);
    }
  });
}

async function signOut() {
  try { await api('/api/auth/logout', {}); } catch { }
  S.me = null; PID = null; S.project = null;
  go('#signin');
}

/* ---------- projects ---------- */
async function projectList() {
  main.innerHTML = header('Projects', '') + '<div class="loading">Loading…</div>';
  const list = await api('/api/projects');
  if (location.hash.slice(1).split('/')[0] !== 'projects' && location.hash.slice(1)) return;
  const roles = { owner: 'Owner', editor: 'Can edit', viewer: 'Can view' };
  main.innerHTML = header('Projects', `${list.length} project${list.length === 1 ? '' : 's'}`, `<a class="btn pri" href="#new">New project</a>`) + (list.length ? `
  <div class="cards">${list.map(p => `<a class="panel card" href="#p/${encodeURIComponent(p.id)}/overview">
    <div class="row between"><b>${esc(p.name)}</b><span class="chip">${roles[p.role] || ''}</span></div>
    <div class="small muted">${esc([p.client, p.city].filter(Boolean).join(' · ') || p.scope || '')}</div>
    ${p.steel_t != null ? `<div class="row nums"><span><b>${fx(p.steel_t, 2)}</b> t steel</span><span><b>${fx(p.concrete_m3, 1)}</b> m³ concrete</span><span><b>₹${fx(p.per_sqft)}</b> /sq ft</span></div>`
      : `<div class="small">${p.floors} floor${p.floors === 1 ? '' : 's'} · no drawings yet</div>`}
    <div class="small muted">${esc(p.status || '')}${p.issues ? ` · ${p.issues} open issue${p.issues === 1 ? '' : 's'}` : ''}</div>
  </a>`).join('')}</div>` : `<section class="panel pad stack"><p>No projects yet.</p><p class="small muted">Create one to set up its buildings and floors, then add drawings.</p><div><a class="btn pri" href="#new">New project</a></div></section>`);
}

const ORD = ['Ground', 'First', 'Second', 'Third', 'Fourth', 'Fifth', 'Sixth', 'Seventh', 'Eighth', 'Ninth', 'Tenth'];
const floorName = i => (ORD[i] ? `${ORD[i]} floor` : `Floor ${i}`);
const floorRow = (name = '', level = '', typ = 1) => `<tr class="fl">
  <td><input class="f" name="fname" value="${esc(name)}" required maxlength="200" aria-label="Floor name"></td>
  <td><input class="f num" name="level" type="number" step="0.01" value="${esc(level)}" placeholder="—" aria-label="Level in metres"></td>
  <td><input class="f num" name="typ" type="number" min="1" max="100" value="${typ}" aria-label="Same floor repeated"></td>
  <td><button type="button" class="btn sm" data-act="delfloor" aria-label="Remove floor">✕</button></td></tr>`;
const buildingBlock = (n = 1) => `<fieldset class="bld panel pad stack">
  <div class="row between"><h2>Building</h2><button type="button" class="btn sm" data-act="delbld">Remove building</button></div>
  <div class="row">
    <label class="stack small grow">Name<input class="f" name="bname" value="${n === 1 ? 'Main building' : `Building ${n}`}" required maxlength="200"></label>
    <label class="stack small">Identical copies on site<input class="f num" name="copies" type="number" min="1" max="50" value="1"></label>
  </div>
  <div class="tw"><table class="floors"><thead><tr><th>Floor</th><th>Level (m, top of slab)</th><th>Typical × n</th><th></th></tr></thead>
    <tbody>${floorRow(floorName(0))}</tbody></table></div>
  <div><button type="button" class="btn sm" data-act="addfloor">Add floor</button></div>
</fieldset>`;

function newProject() {
  main.innerHTML = `<div class="crumb"><a href="#projects">Projects</a> / New</div>` + header('New project', 'Name the project and list its buildings and floors. Drawings are added next.') + `
  <form id="np" class="stack" novalidate>
    <section class="panel pad stack">
      <label class="stack small">Project name<input class="f" name="name" required maxlength="200" autofocus></label>
      <div class="row">
        <label class="stack small grow">Client<input class="f" name="client" maxlength="200"></label>
        <label class="stack small grow">City<input class="f" name="city" maxlength="100"></label>
      </div>
      <label class="stack small">What is being measured (optional)<input class="f" name="scope" maxlength="500" placeholder="e.g. Slabs and beams, all floors"></label>
    </section>
    <div id="blds" class="stack">${buildingBlock()}</div>
    <div class="row"><button type="button" class="btn" data-act="addbld">Add another building</button><span class="sp"></span>
      <a class="btn" href="#projects">Cancel</a><button class="btn pri" type="submit">Create project</button></div>
    <p class="small muted">Leave a level blank if you don't know it yet; it will be asked for before anything is calculated.</p>
  </form>`;
  const form = $('#np');
  form.addEventListener('click', e => {
    const b = e.target.closest('[data-act]'); if (!b) return;
    const bld = b.closest('.bld');
    if (b.dataset.act === 'addfloor') { const tb = bld.querySelector('tbody'); tb.insertAdjacentHTML('beforeend', floorRow(floorName(tb.children.length))); tb.lastElementChild.querySelector('input').focus(); }
    if (b.dataset.act === 'delfloor') { const tb = bld.querySelector('tbody'); if (tb.children.length > 1) b.closest('tr').remove(); else toast('A building needs at least one floor'); }
    if (b.dataset.act === 'addbld') $('#blds').insertAdjacentHTML('beforeend', buildingBlock($('#blds').children.length + 1));
    if (b.dataset.act === 'delbld') { if ($('#blds').children.length > 1) bld.remove(); else toast('A project needs at least one building'); }
  });
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const bad = [...form.querySelectorAll('input')].find(i => !i.checkValidity() || (i.required && !i.value.trim()));
    if (bad) { bad.focus(); toast(bad.name === 'name' ? 'Give the project a name' : 'Fill in the highlighted field'); return; }
    const body = {
      name: form.name.value, client: form.client.value, city: form.city.value, scope: form.scope.value,
      buildings: [...form.querySelectorAll('.bld')].map(b => ({
        name: b.querySelector('[name=bname]').value, copies: +b.querySelector('[name=copies]').value || 1,
        floors: [...b.querySelectorAll('tr.fl')].map(r => ({
          name: r.querySelector('[name=fname]').value, typical_count: +r.querySelector('[name=typ]').value || 1,
          level_m: r.querySelector('[name=level]').value === '' ? null : +r.querySelector('[name=level]').value,
        })),
      })),
    };
    const btn = form.querySelector('[type=submit]'); btn.disabled = true;
    try {
      const { id } = await api('/api/projects', body);
      S.me = await api('/api/me');
      go(`#p/${encodeURIComponent(id)}/overview`);
    } catch (err) { btn.disabled = false; toast(err.message); }
  });
}

/* A project with no measured floor yet: show its buildings and floors, and what comes next. */
function setup() {
  const p = S.project, m = p.meta;
  main.innerHTML = header(esc(m.name), esc([m.client, m.city, m.status].filter(Boolean).join(' · ')),
    `${p.model ? `<a class="btn" href="${P}model">3D model</a><a class="btn" href="${P}review">Review</a>` : ''}<a class="btn pri" href="${P}upload">Add drawings</a>`) + `
  <div class="grid g-main">
    <section class="panel"><div class="hd"><h2>Buildings and floors</h2></div><div class="tw"><table>
      <thead><tr><th>Building</th><th>Floor</th><th class="r">Level</th><th class="r">Typical × n</th><th>Drawings</th></tr></thead><tbody>
      ${p.buildings.map(b => b.floors.map((f, i) => `<tr>${i === 0 ? `<td rowspan="${b.floors.length}"><b>${esc(b.name)}</b>${b.copies > 1 ? `<div class="small muted">× ${b.copies} on site</div>` : ''}</td>` : ''}
        <td>${esc(f.name)}</td><td class="r mono nowrap">${f.level_m == null ? '<span class="muted">not given</span>' : `${fx(f.level_m, 2)} m`}</td><td class="r mono">${f.typical_count}</td>
        <td>${f.measured ? 'Read' : '<span class="muted">Not yet</span>'}</td></tr>`).join('')).join('')}
      </tbody></table></div></section>
    <section class="panel pad stack"><h2>Next</h2><ol class="flow small">
      <li>Add the structural drawings for each floor: beam layout, beam schedules, slab layouts.</li>
      <li>Check what was read, and answer the values the drawings don't give.</li>
      <li>Quantities, bar schedules and costs appear here with the working for every number.</li>
    </ol><div><a class="btn pri" href="${P}upload">Add drawings</a></div></section>
  </div>`;
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
    `<a class="btn" href="${P}drawings">Open drawings</a><a class="btn pri" href="${P}beams">Beam by beam</a>`) + kpis() + `
  <div class="grid g-main">
    <div class="stack">
      <section class="panel"><div class="hd"><h2>Steel by bar size</h2><span class="muted small">kg · rates ${esc('₹/kg')} incl. Fe550 premium</span></div>
        <div class="tw"><table><thead><tr><th>Bar</th><th class="r">Beams</th><th class="r">Slab</th><th class="r">Total</th><th class="r">+ wastage</th><th class="r">Cost ₹</th></tr></thead><tbody>
        ${s.by_dia.map(r => `<tr><td class="mono">${r.dia} mm</td><td class="num">${fx(r.beams)}</td><td class="num">${r.slab ? fx(r.slab) : '—'}</td><td class="num">${fx(r.total)}</td><td class="num">${fx(r.with_wastage)}</td><td class="num">${fx(r.cost)}</td></tr>`).join('')}
        <tr class="tot"><td>Total</td><td class="num">${fx(s.beam_steel_kg)}</td><td class="num">${fx(s.slab_steel_kg)}</td><td class="num">${fx(s.steel_kg)}</td><td class="num">${fx(s.steel_with_wastage_kg)}</td><td class="num">${fx(s.steel_cost)}</td></tr>
        </tbody></table></div></section>
      <section class="panel"><div class="hd"><h2>Concrete and cement</h2><a class="small" href="${P}slab">Slab working</a></div>
        <div class="pad stack">${steps([
          { label: 'Beams below slab', formula: 'Σ b × (D − slab t) × L', working: `${S.members.length} beams`, value: s.beam_concrete_m3, unit: 'm³' },
          { label: 'Slab', formula: 'net area × thickness', working: `${fx(S.calc.slab.area_m2, 1)} m² × ${fx(S.calc.assumptions.slab_t_in * 25.4)} mm`, value: s.slab_concrete_m3, unit: 'm³' },
          ...s.concrete_steps])}</div></section>
    </div>
    <div class="stack">
      <section class="panel"><div class="hd"><h2>Needs an answer</h2><a class="small" href="${P}issues">All ${S.issues.length}</a></div>
        ${open.map(i => `<a href="${P}issues" style="display:flex;gap:10px;padding:10px 16px;border-bottom:1px solid var(--line);text-decoration:none;color:inherit">${sevChip(i.severity)}<span>${esc(i.title)}</span></a>`).join('')}</section>
      ${nb ? `<section class="panel"><div class="hd"><h2>Site QS notebook</h2><a class="small" href="${P}notebook">Compare</a></div><div class="pad stack small">
        <div class="row"><span class="sp">Notebook, beams</span><b class="mono">${fx(nb.beams.theirs)} kg</b></div>
        <div class="row"><span class="sp">Takeoff, beams</span><b class="mono">${fx(nb.beams.ours)} kg</b></div>
        <div class="row"><span class="sp">Difference</span><b class="mono up">+${fx(nb.beams.diff)} kg (${fx(nb.beams.diff / nb.beams.ours * 100)}%)</b></div>
        <p class="muted">Mostly extra bars counted full length and overall beam length used for every bar.</p></div></section>` : ''}
      <section class="panel"><div class="hd"><h2>Heaviest beams</h2><span class="muted small">kg</span></div><div class="pad stack" style="gap:7px">
        ${top.map(m => `<a href="${P}beam/${m.name}" class="row" style="text-decoration:none;color:inherit;flex-wrap:nowrap"><span class="mono" style="width:52px">${m.name}</span><span class="hbar sp"><i style="width:${m.kg / mx * 100}%"></i></span><span class="mono" style="width:44px;text-align:right">${fx(m.kg)}</span></a>`).join('')}
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
  <div class="row">${S.project.meta.sheets.map(s => `<a class="btn sm ${s.id === sh.id ? 'pri' : ''}" href="${P}drawings/${s.id}">${esc(s.drg)} ${esc(s.title.split(' (')[0])}</a>`).join('')}</div>
  <div class="dside"><div id="vw"></div><aside class="panel" id="pick"><div class="pad muted">Tap a beam marker to see its steel, the working and any issues.</div></aside></div>`;
  const hs = () => Object.entries(sh.hotspots).filter(([n]) => S.byName[n]).map(([n, h]) => ({ id: n, x: h.x, y: h.y, label: n, color: colorFor(store.get('mode', 'steel'), n) }));
  const v = mount($('#vw'), sh.id, { hotspots: hs(), legend: Object.keys(sh.hotspots).length ? LEG[mode] : '', onPick: h => pick(h.id) });
  const pick = name => {
    v.select(name);
    const m = S.byName[name], r = S.site[name], iss = S.issueBeams[name] || [];
    $('#pick').innerHTML = `<div class="hd"><h2>${name}</h2><a class="btn sm pri" href="${P}beam/${name}">Open</a></div><div class="pad stack small">
      <div class="row"><span class="sp muted">Size · span</span><span class="mono">${m.size}″ · ${fx(m.span_in * 25.4)} mm</span></div>
      <div class="row"><span class="sp muted">Steel</span><b class="mono">${fx(m.kg, 1)} kg</b></div>
      <div class="row"><span class="sp muted">Per metre</span><span class="mono">${fx(m.kg_m, 1)} kg/m</span></div>
      <div>${Object.entries(m.kg_by_dia).map(([d, k]) => `<span class="chip" style="margin:0 4px 4px 0">${d}φ ${fx(k, 1)}</span>`).join('')}</div>
      ${r ? `<div class="row"><span class="sp muted">Site notebook (${esc(r.label)})</span><span class="mono ${r.diff_kg > 0 ? 'up' : 'dn'}">${fx(r.theirs_kg)} vs ${fx(r.ours_kg)}</span></div>` : ''}
      ${iss.map(i => `<div class="row" style="flex-wrap:nowrap;align-items:flex-start">${sevChip(i.severity)}<span>${esc(i.title)}</span></div>`).join('')}
      ${m.parts.length > 1 ? `<div class="muted">Includes ${m.parts.join(', ')}</div>` : ''}</div>`;
    history.replaceState(null, '', `${P}drawings/${sh.id}/${name}`);
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
  const noCalc = !S.calc;                       // read from uploads, not calculated yet: element colours only
  const modes = noCalc ? { type: M3MODES.type } : M3MODES;
  const mode = noCalc ? 'type' : store.get('m3mode', 'type');
  const measured = md.floors?.find(f => f.measured)?.name;
  const nBeams = new Set(md.beams.map(b => b.name.split('-')[0])).size;
  const ghosts = store.get('m3ghost', true), showSlab = store.get('m3slab', true);
  main.innerHTML = header('3D model', `${esc(measured || 'Second floor')} slab and beams in 3D, from ${esc(md.source)}. Drag to turn, scroll or pinch to zoom, right-drag or two fingers to move. Double-click to zoom to a member.`,
    `<input class="f" id="m3q" placeholder="Go to beam, e.g. B47" aria-label="Go to beam" style="width:170px">`) + `
  <div class="row">
    <div class="seg" id="m3modes">${Object.entries(modes).map(([k, v]) => `<button data-m="${k}" class="${k === mode ? 'on' : ''}">${v}</button>`).join('')}</div>
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
      ${noCalc ? `<div class="row"><span class="sp">Beams read from the layout</span><b class="mono">${nBeams}</b></div>
      <div class="row"><span class="sp">Columns</span><b class="mono">${md.columns.length}</b></div>
      ${md.slab.footprint_sqft ? `<div class="row"><span class="sp">Floor plate</span><b class="mono">${fx(md.slab.footprint_sqft)} sq ft</b></div>` : ''}
      ${md.unplaced.length ? `<p class="muted">Labels read with no beam found: ${md.unplaced.map(esc).join(', ')}. Fix them in <a href="${P}review">Review</a>.</p>` : ''}
      <p class="muted">Sizes are from the drawing labels. Nothing is calculated yet; the slab is shown ${5}″ thick for the picture only.</p>`
      : `<div class="row"><span class="sp">Beams placed in 3D</span><b class="mono">${nBeams} of ${S.members.length}</b></div>
      ${md.unplaced.length ? `<p class="muted">Not placed (label not matched on the layout): ${md.unplaced.map(n => `<a href="${P}beam/${n.split('-')[0]}">${n}</a>`).join(', ')}. Their steel is still counted.</p>` : ''}`}
      <p class="muted">${md.floors ? `Floor-to-floor height ${fx(md.floor_h, 2)} m from the project's floor levels.` : 'Floor-to-floor height is taken as 3.0 m for the picture.'} Only ${esc(measured || 'the second floor')} is measured; the other floors repeat its outline faintly for context.</p>
    </div></aside>
  </div>`;
  const { Model3D } = await import('./model3d.js');
  if (S.view !== 'model') return;
  const m3 = S.m3d = new Model3D($('#m3'), {
    model: md, calc: S.calc, onPick: sel => m3panel(sel),
    ...(md.floors ? { floorH: md.floor_h, level: md.level + 1, levels: Math.max(md.floors.length, md.level + 1) } : {}),
    onHover: (h, e) => {
      const tip = $('#m3tip'); if (!tip) return;
      if (!h || h.kind === 'ghost') { tip.hidden = true; return; }
      const r = $('#m3').getBoundingClientRect();
      tip.textContent = h.kind === 'beam' ? (noCalc ? h.part : `${h.part} · ${fx(S.byName[h.id]?.kg, 1)} kg`) : h.kind === 'slab' ? (noCalc ? 'Slab' : `Slab · ${fx(S.calc.slab.kg)} kg steel`) : `Column ${h.id}`;
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
  const go = n => { n = n.trim().toUpperCase(); if (!m3.beamMeshes[n]) { toast(S.byName?.[n] || md.unplaced.includes(n) ? `${n} is not placed in 3D yet` : `No beam ${n}`); return; } const sel = { kind: 'beam', id: n }; m3.select(sel); m3.focus(sel); m3panel(sel); };
  $('#m3q').addEventListener('keydown', e => { if (e.key === 'Enter') go(e.target.value); });
  if (target) go(target);
}
function m3panel(sel) {
  const el = $('#m3pick'); if (!el) return;
  if (!sel) return;
  if (!S.calc) return m3panelRead(sel, el);
  if (sel.kind === 'beam') {
    const m = S.byName[sel.id]; const r = S.site[sel.id], iss = S.issueBeams[sel.id] || [];
    history.replaceState(null, '', `${P}model/${sel.id}`);
    el.innerHTML = `<div class="hd"><h2>Beam ${m.name}</h2><a class="btn sm pri" href="${P}beam/${m.name}">Full detail</a></div><div class="pad stack small">
      <div class="row"><span class="sp muted">Size · clear span</span><span class="mono">${m.size}″ · ${fx(m.span_in * 25.4)} mm</span></div>
      <div class="row"><span class="sp">Steel</span><b class="mono">${fx(m.kg, 2)} kg</b></div>
      ${m.partsData.map(p => `<div class="m3part"><div class="row"><b>${esc(p.name)}</b><span class="muted">${p.kind === 'C' ? 'cantilever' : 'span'}</span><span class="sp"></span><span class="mono">${fx(p.kg, 2)} kg</span></div>
        ${marks(p).map(x => `<details class="m3bar"><summary><span class="swatch" style="background:var(--b-${{ 'r-top': 'top', 'r-bot': 'bot', 'r-ext': 'ext', 'r-xtop': 'xtop', 'r-stir': 'stir' }[ROLE[x.role].c]})"></span><span class="mono">${x.mark}</span> ${x.nos} × ${x.dia}φ ${esc(ROLE[x.role].short)}<span class="sp"></span><span class="mono">${fx(x.kg, 2)} kg</span></summary>
          <p class="why">${esc(x.why)}</p>${steps(x.steps)}</details>`).join('')}
        <details class="m3bar"><summary>Concrete below slab<span class="sp"></span><span class="mono">${fx(p.concrete.value, 3)} m³</span></summary>${steps([p.concrete])}</details></div>`).join('')}
      ${r ? `<div class="row"><span class="sp muted">Site notebook (${esc(r.label)})</span><span class="mono ${r.diff_kg > 0 ? 'up' : 'dn'}">${fx(r.theirs_kg)} vs ${fx(r.ours_kg)} kg</span></div>` : ''}
      ${iss.map(i => `<div class="row" style="flex-wrap:nowrap;align-items:flex-start">${sevChip(i.severity)}<span>${esc(i.title)}</span></div>`).join('')}
      <a class="small" href="${P}drawings/layout/${m.name}">Show on drawing</a></div>`;
  } else if (sel.kind === 'slab') {
    const sl = S.calc.slab, a = S.calc.assumptions;
    el.innerHTML = `<div class="hd"><h2>Second floor slab</h2><a class="btn sm pri" href="${P}slab">Full detail</a></div><div class="pad stack small">
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
    el.innerHTML = `<div class="hd"><h2>Floor ${sel.level ?? ''}</h2></div><div class="pad small stack"><p>Not measured. Only the second floor structural drawings have been received; this outline repeats the second floor for context.</p><a href="${P}upload">Add drawings for other floors</a></div>`;
  }
}

/* 3D side panel for a project read from uploads (no calculation yet): what was read, and where to fix it. */
function m3panelRead(sel, el) {
  const md = S.project.model;
  if (sel.kind === 'beam') {
    const parts = md.beams.filter(b => b.name.split('-')[0] === sel.id);
    history.replaceState(null, '', `${P}model/${sel.id}`);
    const len = b => Math.hypot(b.p2[0] - b.p1[0], b.p2[1] - b.p1[1]) / 0.0254;
    el.innerHTML = `<div class="hd"><h2>Beam ${esc(sel.id)}</h2></div><div class="pad stack small">
      ${parts.map(b => `<div class="m3part"><div class="row"><b>${esc(b.name)}</b><span class="muted">${b.cant ? 'cantilever' : 'span'}</span><span class="sp"></span>
        <span class="mono">${fx(b.b_in, 0)}″ × ${b.depth_known ? `${fx(b.D_in, 0)}″` : '? (depth not on label)'}</span></div>
        <div class="row"><span class="sp muted">Length centre to centre</span><span class="mono">${ftIn(len(b))}</span></div>
        <div class="row"><span class="sp muted">Checked</span><span>${{ auto: 'Not yet', accepted: 'Accepted', edited: 'Corrected', added: 'Added by hand' }[b.status] || b.status}</span></div>
        <a href="${P}review/${b.member_id}">Open in Review</a></div>`).join('')}
      <p class="muted">Quantities come once the schedules are read and the values the drawings don't give are entered.</p></div>`;
  } else if (sel.kind === 'slab') {
    el.innerHTML = `<div class="hd"><h2>Floor plate</h2></div><div class="pad stack small">
      <div class="row"><span class="sp muted">Area inside the outline, beams included</span><span class="mono">${fx(md.slab.footprint_sqft)} sq ft</span></div>
      <p class="muted">Traced from the beam edges on ${esc(md.source)}, including curved edges. Openings and the slab thickness come from the slab layouts.</p></div>`;
  } else if (sel.kind === 'column') {
    el.innerHTML = `<div class="hd"><h2>Column ${esc(sel.id)}</h2></div><div class="pad stack small"><div class="row"><span class="sp muted">Columns on the layout</span><span class="mono">${md.columns.length}</span></div>
      <p class="muted">Add the column schedule to take off column steel and concrete.</p></div>`;
  } else if (sel.kind === 'ghost') {
    const f = md.floors?.[(sel.level ?? 1) - 1];
    el.innerHTML = `<div class="hd"><h2>${esc(f?.name || `Floor ${sel.level ?? ''}`)}</h2></div><div class="pad small stack"><p>Not measured yet; this outline repeats the measured floor for context.</p><a href="${P}upload">Add drawings for this floor</a></div>`;
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
    const tr = e.target.closest('tr[data-n]'); if (tr) location.hash = P + 'beam/' + tr.dataset.n;
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
  if (!m) { main.innerHTML = header('Beam not found', `<a href="${P}beams">Back to beams</a>`); return; }
  const a = S.calc.assumptions, r = S.site[name], iss = S.issueBeams[name] || [];
  const order = S.members.map(x => x.name), i = order.indexOf(name);
  const loc = S.project.meta.sheets.filter(s => s.hotspots[name]);
  main.innerHTML = `<div class="crumb"><a href="${P}beams">Beams</a> / ${name}</div>` + header(`Beam ${name}`,
    `${m.size}″ · clear span ${fx(m.span_in * 25.4)} mm · ${fx(m.kg, 1)} kg steel · ${fx(m.kg_m, 1)} kg/m`,
    `${i > 0 ? `<a class="btn sm" href="${P}beam/${order[i - 1]}">← ${order[i - 1]}</a>` : ''}${i < order.length - 1 ? `<a class="btn sm" href="${P}beam/${order[i + 1]}">${order[i + 1]} →</a>` : ''}`) + `
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
      ${loc.map(s => `<section class="panel"><div class="hd"><h2>${esc(s.drg)}</h2><a class="small" href="${P}drawings/${s.id}/${name}">Open sheet</a></div><div class="mini" id="mini-${s.id}"></div></section>`).join('')}
      <section class="panel pad small muted">Assumptions used: Ld ${a.ld_factor}d · cover ${a.cover_mm} mm · bottom anchorage ${a.bot_anch_mm} mm · extra bottom ${a.extra_bot_frac}L · slab ${a.slab_t_in}″. <a href="${P}assumptions">Change</a></section>
    </div>
  </div>`;
  main.querySelectorAll('[data-bbs]').forEach(t => t.addEventListener('click', e => {
    const tr = e.target.closest('tr[data-i]'); if (!tr) return;
    const x = t.querySelector(`tr[data-x="${tr.dataset.i}"]`); x.hidden = !x.hidden; tr.classList.toggle('open', !x.hidden);
  }));
  for (const s of loc) {
    const h = s.hotspots[name];
    const v = mount($(`#mini-${s.id}`), s.id, { hotspots: [{ id: name, x: h.x, y: h.y, label: name, color: '#C2410C' }], tools: false, onPick: () => location.hash = `${P}drawings/${s.id}/${name}` });
    requestAnimationFrame(() => v.focus(h.x, h.y, 5));
  }
}
function siteCard(r, name) {
  const dias = [...new Set([...Object.keys(r.ours_by_dia || {}), ...Object.keys(r.theirs_by_dia || {})])].sort((a, b) => a - b);
  return `<section class="panel"><div class="hd"><h2>Site QS notebook</h2><a class="small" href="${P}notebook">All beams</a></div><div class="pad stack small">
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
    <section class="panel pad small muted">The slab is measured with average spacings read off the bottom and top slab layouts, not panel by panel. Panel-by-panel reading is planned for the upload pipeline. Spacings and extension are editable in <a href="${P}assumptions">Assumptions</a>.</section>
  </div><div class="stack">
    <section class="panel"><div class="hd"><h2>${esc(S.sheets.slabbot.drg)} bottom</h2><a class="small" href="${P}drawings/slabbot">Open</a></div><div class="mini" id="m1" style="height:300px"></div></section>
    <section class="panel"><div class="hd"><h2>${esc(S.sheets.slabtop.drg)} top</h2><a class="small" href="${P}drawings/slabtop">Open</a></div><div class="mini" id="m2" style="height:300px"></div></section>
  </div></div>`;
  mount($('#m1'), 'slabbot', { tools: false }); mount($('#m2'), 'slabtop', { tools: false });
}

/* ---------- issues ---------- */
function whereLink(w) {
  if (!w) return '';
  if (w.beam) return `${P}drawings/${w.sheet}/${w.beam}`;
  if (w.x) return `${P}drawings/${w.sheet}/@${w.x},${w.y},${w.zoom || 3}`;
  return `${P}drawings/${w.sheet}`;
}
function issues() {
  const order = { high: 0, medium: 1, low: 2 };
  const list = [...S.issues].sort((a, b) => order[a.severity] - order[b.severity]);
  main.innerHTML = header('Issues', 'Things the drawings do not say, or say twice. Each one names the value we used until the consultant answers.',
    `<button class="btn" id="copy">Copy questions for consultant</button>`) + `
  <section class="panel">${list.map(i => `<div class="iss"><div><div class="row">${sevChip(i.severity)}<span class="chip">${esc(i.kind)}</span>${i.auto ? '<span class="chip acc">found by Takeoff</span>' : ''}</div>
    <div class="t" style="margin-top:6px">${esc(i.title)}</div><p class="small" style="margin-top:4px;color:var(--fg2)">${esc(i.detail)}</p>
    <dl><dt>Using for now</dt><dd>${esc(i.assumption)}</dd><dt>Affects</dt><dd>${esc(i.affects)}</dd><dt>Ask</dt><dd>${esc(i.ask)}</dd>
    ${i.beams ? `<dt>Beams</dt><dd>${i.beams.map(b => S.byName[b] ? `<a href="${P}beam/${b}">${b}</a>` : b).join(', ')}</dd>` : ''}</dl></div>
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
  main.querySelector('tbody').addEventListener('click', e => { const tr = e.target.closest('tr.click'); if (tr) location.hash = P + 'beam/' + tr.dataset.n; });
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

/* ---------- add drawings ---------- */
const UP_STATUS = { queued: ['Waiting', ''], processing: ['Reading', 'acc'], done: ['Read', 'ok'], failed: ['Could not read', 'high'] };
const SHEET_STATUS = { queued: ['Waiting', ''], rendering: ['Drawing', 'acc'], ready: ['Ready', 'ok'], needs_attention: ['Needs attention', 'high'] };
const chip = ([label, cls]) => `<span class="chip ${cls}">${label}</span>`;
const mb = n => `${fx(n / 1048576, 1)} MB`;

function upload(sheetId) {
  const canEdit = ['owner', 'editor'].includes(S.project.role);
  main.innerHTML = header('Add drawings', canEdit ? 'Upload the structural PDFs for this project: beam layouts, beam schedules, slab layouts.' : 'Drawings uploaded to this project.') + `
  <div class="grid g-main">
    <div class="stack">
      ${canEdit ? `<section class="panel pad stack">
        <label class="drop" id="drop"><b style="color:var(--fg)">Drop PDFs here</b><span>or</span><span class="btn">Choose files</span>
          <input type="file" id="pick" accept="application/pdf,.pdf" multiple hidden></label>
        <div id="sending" class="stack small"></div>
        <p class="small muted">PDF only, up to 50 MB each. A file or page that is already in the project is not added again.</p>
      </section>` : ''}
      <section class="panel" id="coverage" hidden></section>
      <section class="panel" id="sheetView" hidden></section>
      <section class="panel"><div class="hd"><h2>Uploads</h2><span class="small muted" id="upState"></span></div><div id="ups" class="stack pad"><div class="muted small">Loading…</div></div></section>
    </div>
    <section class="panel pad stack"><h2>What happens to an upload</h2><ol class="flow small">
      <li>Each page becomes a sheet, drawn at full resolution so you can zoom into any detail. <b>This part works now.</b></li>
      <li>Next: each sheet's title block is read to tell layouts from schedules and to place it on a floor.</li>
      <li>Beam names, sizes, spans and bar callouts are read from the layout and the schedules. Anything unreadable is listed for you to fill in, never guessed.</li>
      <li>Drawings are cross-checked: sizes that disagree between sheets, missing beam numbers, repeated rows, missing grade or cover. These become Issues.</li>
      <li>You confirm the values the drawings don't give. Nothing is calculated until every value is filled.</li>
    </ol></section>
  </div>`;
  if (canEdit) {
    const drop = $('#drop'), pick = $('#pick');
    pick.addEventListener('change', () => { sendFiles([...pick.files]); pick.value = ''; });
    ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', e => sendFiles([...e.dataTransfer.files]));
  }
  const open = e => {
    const t = e.target.closest('[data-sheet]'); if (!t) return;
    e.preventDefault(); history.replaceState(null, '', `${P}upload/${t.dataset.sheet}`); showSheet(t.dataset.sheet);
  };
  $('#ups').addEventListener('click', open);
  $('#coverage').addEventListener('click', open);
  (S.sheetTypes ? Promise.resolve() : api('/api/sheet-types').then(t => { S.sheetTypes = t; }))
    .catch(() => { S.sheetTypes = []; }).then(() => refreshUploads(sheetId));
}

function sendFiles(files) {
  for (const f of files) {
    const row = document.createElement('div'); row.className = 'sendrow';
    row.innerHTML = `<span class="grow">${esc(f.name)} <span class="muted">${mb(f.size)}</span></span><progress max="100" value="0"></progress><span class="st muted">Sending</span>`;
    $('#sending').prepend(row);
    const done = (msg, cls) => { row.querySelector('progress').remove(); const st = row.querySelector('.st'); st.textContent = msg; st.className = `st ${cls}`; };
    if (!/\.pdf$/i.test(f.name) && f.type !== 'application/pdf') { done('Not a PDF', 'bad'); continue; }
    if (f.size > 50 * 1048576) { done('Larger than 50 MB', 'bad'); continue; }
    const fd = new FormData(); fd.append('files', f);
    const x = new XMLHttpRequest();
    x.open('POST', `/api/projects/${encodeURIComponent(PID)}/uploads`);
    x.upload.onprogress = e => { if (e.lengthComputable) row.querySelector('progress').value = e.loaded / e.total * 100; };
    x.onload = () => {
      let body = {}; try { body = JSON.parse(x.responseText); } catch { }
      if (x.status === 401) { S.me = null; go('#signin'); return; }
      if (x.status >= 300) { done(typeof body.detail === 'string' ? body.detail : `Failed (${x.status})`, 'bad'); return; }
      done(body.uploads?.[0]?.duplicate ? 'Already uploaded' : 'Sent', 'ok');
      refreshUploads();
    };
    x.onerror = () => done('Network error, try again', 'bad');
    x.send(fd);
  }
}

async function refreshUploads(openSheet) {
  const pid = PID;
  clearTimeout(refreshUploads.t);
  let ups;
  try { ups = await api(`/api/projects/${encodeURIComponent(pid)}/uploads`); } catch (e) { if ($('#ups')) $('#ups').innerHTML = `<div class="small">${esc(e.message)}</div>`; return; }
  if (PID !== pid || S.view !== 'upload' || !$('#ups')) return;
  S.uploads = ups;
  S.upSheets = Object.fromEntries(ups.flatMap(u => u.sheets.map(s => [s.id, { ...s, filename: u.filename }])));
  const busy = ups.some(u => ['queued', 'processing'].includes(u.status) || u.sheets.some(s => ['queued', 'rendering'].includes(s.status) || !s.classify_status || (s.sheet_type && !s.scale_status) || (s.sheet_type === 'beam_layout' && s.pt_per_in && !s.layout_note)));
  $('#upState').textContent = busy ? 'Working… this page updates by itself' : '';
  $('#ups').innerHTML = ups.length ? ups.map(u => `<div class="upl stack">
    <div class="row between"><span><b>${esc(u.filename)}</b> <span class="small muted">${mb(u.size_bytes)}${u.pages != null ? ` · ${u.pages} page${u.pages === 1 ? '' : 's'}` : ''}${u.pages != null && u.new_sheets < u.pages ? ` · ${u.pages - u.new_sheets} already in the project` : ''}</span></span>${chip(UP_STATUS[u.status])}</div>
    ${u.error ? `<div class="note warn">${esc(u.error)}</div>` : ''}
    ${u.sheets.length ? `<div class="thumbs">${u.sheets.map(s => `<a href="${P}upload/${s.id}" data-sheet="${s.id}" class="thumb ${s.status === 'ready' ? '' : 'off'}" aria-label="Page ${s.page_no}">
      ${s.thumb_url ? `<img src="${s.thumb_url}" alt="" loading="lazy">` : '<span class="ph2"></span>'}
      <span class="row between small"><span>${s.drg_no ? esc(s.drg_no) : `Page ${s.page_no}`}</span>${s.status === 'ready' ? (needsYou(s) ? chip(['Check', 'medium']) : '') : chip(SHEET_STATUS[s.status])}</span>
      <span class="small muted">${esc(sheetPlace(s))}</span></a>`).join('')}</div>` : ''}
  </div>`).join('') : '<div class="muted small">Nothing uploaded yet.</div>';
  renderCoverage();
  if (openSheet) showSheet(openSheet);
  if (busy) refreshUploads.t = setTimeout(() => refreshUploads(), 3000);
}

const SCALE_CHECK = ['differs', 'borrowed', 'unchecked'];
const needsYou = s => s.classify_status === 'needs_review' || SCALE_CHECK.includes(s.scale_status);
const typeLabel = k => (S.sheetTypes || []).find(t => t.key === k)?.label || (k ? k.replace(/_/g, ' ') : '');
const floorsList = () => (S.project.buildings || []).flatMap(b => b.floors.map(f => ({ ...f, building: b.name, label: S.project.buildings.length > 1 ? `${b.name} · ${f.name}` : f.name })));
const floorLabel = id => floorsList().find(f => f.id === id)?.label || '';
function sheetPlace(s) {
  if (s.status !== 'ready' && !s.classify_status) return '';
  if (!s.classify_status) return 'Reading title block…';
  return [typeLabel(s.sheet_type) || 'Type not known', floorLabel(s.floor_id) || 'Floor not known'].join(' · ');
}

/* Coverage: for each building, floors × the drawings each floor needs. Missing cells say what to upload. */
function renderCoverage() {
  const el = $('#coverage'); if (!el) return;
  const sheets = Object.values(S.upSheets || {});
  if (!sheets.length || !S.sheetTypes?.length) { el.hidden = true; return; }
  const cols = S.sheetTypes.filter(t => t.coverage);
  const at = (fid, type) => sheets.filter(s => s.floor_id === fid && s.sheet_type === type);
  const loose = sheets.filter(s => s.classify_status && (!s.floor_id || !s.sheet_type));
  const check = sheets.filter(needsYou);
  const other = sheets.filter(s => s.floor_id && s.sheet_type && !cols.some(c => c.key === s.sheet_type));
  el.hidden = false;
  el.innerHTML = `<div class="hd"><h2>Coverage</h2><span class="small muted">${check.length ? `${check.length} sheet${check.length === 1 ? '' : 's'} to check` : 'Sheets are placed by their title block and their scale is checked'}</span></div>
  ${S.project.buildings.map(b => `<div class="tw"><table class="cov">
    <thead><tr><th>${esc(b.name)}${b.copies > 1 ? ` <span class="muted">× ${b.copies}</span>` : ''}</th>${cols.map(c => `<th>${esc(c.label)}</th>`).join('')}<th>Other</th></tr></thead>
    <tbody>${b.floors.map(f => `<tr><td>${esc(f.name)}</td>${cols.map(c => {
      const got = at(f.id, c.key);
      return `<td>${got.length ? got.map(s => `<a href="${P}upload/${s.id}" data-sheet="${s.id}" class="chip ${needsYou(s) ? 'medium' : 'ok'}" title="${esc(SCALE_CHECK.includes(s.scale_status) ? 'Scale to check' : '')}">${esc(s.drg_no || `p.${s.page_no}`)}</a>`).join(' ') : '<span class="muted small">Missing</span>'}</td>`;
    }).join('')}<td>${other.filter(s => s.floor_id === f.id).map(s => `<a href="${P}upload/${s.id}" data-sheet="${s.id}" class="chip" title="${esc(typeLabel(s.sheet_type))}">${esc(s.drg_no || `p.${s.page_no}`)}</a>`).join(' ')}</td></tr>`).join('')}</tbody>
  </table></div>`).join('')}
  ${loose.length ? `<div class="pad small stack"><b>Not placed yet</b><div class="row">${loose.map(s => `<a href="${P}upload/${s.id}" data-sheet="${s.id}" class="chip medium">${esc(s.drg_no || `${s.filename} p.${s.page_no}`)}</a>`).join('')}</div><span class="muted">Open each one and pick its type and floor.</span></div>` : ''}`;
}

function scaleBlock(s, canEdit) {
  if (!s.scale_status || s.scale_status === 'not_needed') return s.sheet_type && !s.scale_status ? '<div class="pad small muted">Checking the scale…</div>' : '';
  const label = { ok: ['Scale checked', 'ok'], differs: ['Scale differs', 'medium'], borrowed: ['Scale borrowed', 'medium'], unchecked: ['Scale not checked', 'medium'], confirmed: ['Scale confirmed', 'ok'] }[s.scale_status];
  const shown = s.scale_ratio ? `1:${s.scale_ratio}` : '';
  return `<form class="pad small stack" id="scaleForm">
    <div class="row">${chip(label)}<span>${shown ? `In use: <b class="mono">${shown}</b>` : 'No scale yet'}${s.scale_text ? ` · printed ${esc(s.scale_text)}` : ''}</span></div>
    ${s.scale_note ? `<div class="${SCALE_CHECK.includes(s.scale_status) ? 'note warn' : 'muted'}">${esc(s.scale_note)}</div>` : ''}
    ${canEdit && s.scale_status !== 'ok' ? `<div class="row">
      ${s.pt_per_in && s.scale_status !== 'confirmed' ? `<button class="btn sm pri" name="accept" type="submit">Use ${shown}</button><span class="muted">or</span>` : ''}
      <label class="row">Scale is <input class="f mono" name="ratio" style="width:90px" placeholder="1:100" value="${s.scale_status === 'confirmed' ? shown : ''}"></label>
      <button class="btn sm" name="set" type="submit">Save scale</button></div>` : ''}
  </form>`;
}

/* Members read off a beam layout, drawn over the sheet: beams by confidence, columns, and labels with no beam. */
const confColor = c => c >= 0.7 ? '#15803D' : c >= 0.4 ? '#EA580C' : '#B42318';
async function showMembers(vw, s) {
  let ms;
  try { ms = await api(`/api/projects/${encodeURIComponent(PID)}/sheets/${s.id}/members`); } catch { return; }
  if (!S.viewers.includes(vw)) return;
  const beams = ms.filter(m => m.kind === 'beam' && m.placed), cols = ms.filter(m => m.kind === 'column');
  const lost = ms.filter(m => m.kind === 'beam' && !m.placed);
  const plates = ms.filter(m => m.kind === 'slab_panel' && m.points);
  vw.setOverlay(plates.map(m => `<polygon points="${m.points.map(p => p.join(',')).join(' ')}" class="plate"/>`).join('') +
    beams.map(m => `<line x1="${m.x1}" y1="${m.y1}" x2="${m.x2}" y2="${m.y2}" stroke="${confColor(m.confidence)}"/>`).join('') +
    cols.map(m => `<rect x="${m.x1}" y="${m.y1}" width="${m.x2 - m.x1}" height="${m.y2 - m.y1}"/>`).join(''));
  const byId = Object.fromEntries(ms.map(m => [m.id, m]));
  vw.setHotspots([...beams, ...lost].filter(m => m.label_box).map(m => ({
    id: m.id, label: m.name, color: m.placed ? confColor(m.confidence) : '#B42318',
    x: (m.label_box[0] + m.label_box[2]) / 2, y: (m.label_box[1] + m.label_box[3]) / 2 })));
  vw.onPick = h => {
    const m = byId[h.id]; vw.select(h.id);
    const size = m.label_b_in ? `${m.label_b_in}"×${m.label_d_in}" (label)` : '';
    $('#memberInfo').innerHTML = `<b>${esc(m.name)}</b> ${m.placed ? `· ${size} · drawn ${fx(m.b_in, 1)}" wide · span ${m.length_in ? `${fx(m.length_in / 12, 1)} ft` : '—'}` : '· label read, no beam found next to it'}
      · <span style="color:${confColor(m.confidence)}">confidence ${fx(m.confidence * 100)}%</span>${m.note ? ` · ${esc(m.note)}` : ''}`;
  };
  const info = document.createElement('div'); info.className = 'pad small'; info.id = 'memberInfo';
  info.innerHTML = `${esc(s.layout_note || '')} <span class="legend-inline"><span class="sw" style="background:#15803D"></span>likely <span class="sw" style="background:#EA580C"></span>check <span class="sw" style="background:#B42318"></span>doubtful or no beam found <span class="sw" style="background:#2563EB"></span>column</span> · Click a label for details; check them in <a href="${P}review">Review</a>.`;
  $('#sheetVw').before(info);
}

/* Where OCR and the vision model differ, show every reading side by side; nothing is picked silently. */
const FIELD_STATUS = { agree: ['agree', 'ok'], model_only: ['model only', 'medium'], ocr_only: ['OCR only', 'medium'], resolved: ['resolved by 2nd model', 'medium'], disagree: ['disagree', 'high'], missing: ['not read', 'high'] };
function specCompare(b) {
  if (!b.field_status) return '';
  const r = b.readings || {}, m = r.model || {}, e = r.escalated || {};
  const roleKey = { 'Top through': 'top_through', 'Top main': 'top_main', 'Extra top @ support': 'extra_top', 'Bottom through': 'bottom_through', 'Bottom extra': 'bottom_extra' };
  const bars = list => (list || []).map(g => `${g.nos}×${g.dia}φ`).join(' + ') || '—';
  const ocrBars = role => bars((r.ocr?.bars || []).filter(x => x[0] === role).map(x => ({ nos: x[1], dia: x[2] })));
  const val = (src, f) => {
    if (!src || !Object.keys(src).length) return '—';
    if (f === 'size') return src === r.ocr ? `${src.size?.[0] ?? '?'}×${src.size?.[1] ?? '?'}` : `${src.b_in ?? '?'}×${src.D_in ?? '?'}`;
    if (f.startsWith('stirrup_')) { const k = { stirrup_dia: 0, stirrup_end_in: 1, stirrup_mid_in: 2 }[f]; return src === r.ocr ? (src.stirrup?.[k] ?? '—') : (src.stirrups?.[['dia', 'end_in', 'mid_in'][k]] ?? '—'); }
    return src === r.ocr ? ocrBars(f) : bars(src[roleKey[f]]);
  };
  const rows = Object.entries(b.field_status).filter(([, st]) => st !== 'agree');
  if (!rows.length) return '<div class="muted">OCR and the vision model agree on every field.</div>';
  return `<div class="tw"><table class="small"><thead><tr><th>Field</th><th></th><th>OCR</th><th>Model</th>${r.escalated ? '<th>2nd model</th>' : ''}</tr></thead><tbody>
    ${rows.map(([f, st]) => `<tr><td>${esc(f.replace('stirrup_', 'stirrup ').replace('_in', ''))}</td><td>${chip(FIELD_STATUS[st] || [st, ''])}</td><td class="mono">${esc(String(val(r.ocr, f)))}</td><td class="mono">${esc(String(val(m, f)))}</td>${r.escalated ? `<td class="mono">${esc(String(val(e, f)))}</td>` : ''}</tr>`).join('')}
  </tbody></table></div>`;
}

/* Beams read off a schedule sheet: each elevation boxed by how complete its reading is; click for the reading. */
async function showSpecs(vw, s) {
  let specs;
  try { specs = await api(`/api/projects/${encodeURIComponent(PID)}/sheets/${s.id}/specs`); } catch { return; }
  if (!S.viewers.includes(vw)) return;
  const col = b => b.duplicate ? '#9CA3AF' : confColor(b.confidence);
  vw.setOverlay(specs.filter(b => b.box).map(b => { const [x0, y0, x1, y1] = b.box;
    return `<rect x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}" style="stroke:${col(b)};fill:${col(b)};fill-opacity:.06"/>`; }).join(''));
  const byId = Object.fromEntries(specs.map(b => [b.id, b]));
  vw.setHotspots(specs.filter(b => b.box).map(b => ({ id: b.id, label: b.name, color: col(b), x: b.box[0] + 6, y: b.box[1] + 6 })));
  const ROLE_ORDER = ['Top through', 'Top main', 'Extra top @ support', 'Bottom through', 'Bottom extra'];
  vw.onPick = h => {
    const b = byId[h.id]; vw.select(h.id);
    const bars = [...b.bars].sort((p, q) => ROLE_ORDER.indexOf(p.role) - ROLE_ORDER.indexOf(q.role));
    $('#memberInfo').innerHTML = `<b>${esc(b.name)}</b> ${fx(b.b_in)}″ × ${fx(b.D_in)}″${b.kind === 'C' ? ' · cantilever' : ''}${b.duplicate ? ' · <span class="muted">repeat of another sheet, counted once</span>' : ''}
      · <span style="color:${col(b)}">${b.source === 'ocr' ? `reading ${fx(b.confidence * 100)}% complete (OCR only)` : `confidence ${fx(b.confidence * 100)}% (OCR and vision model)`}</span>
      <div class="tw"><table class="small"><tbody>${bars.map(x => `<tr><td>${esc(x.role)}</td><td class="mono">${x.nos} × ${x.dia}φ${x.extra_top_in ? ` · ${ftIn(x.extra_top_in)}` : ''}</td></tr>`).join('') || '<tr><td class="muted">No bars read</td></tr>'}
        <tr><td>Stirrups</td><td class="mono">${b.stirrup.dia ? `${b.stirrup.dia}φ` : '?φ'} @ ${b.stirrup.end_in ?? '?'}″ ends, ${b.stirrup.mid_in ?? '?'}″ middle</td></tr></tbody></table></div>
      ${b.notes ? `<div class="note warn">${esc(b.notes)}</div>` : ''}${specCompare(b)}<div class="muted">OCR read: ${b.tokens.map(esc).join(' · ') || '—'}</div>`;
  };
  const info = document.createElement('div'); info.className = 'pad small'; info.id = 'memberInfo';
  const done = specs.filter(b => b.confidence >= 0.9 && !b.duplicate).length;
  info.innerHTML = `${specs.length} beam elevations read by OCR; ${done} look complete. Readings are not used for quantities until checked. Click an elevation for what was read.`;
  $('#sheetVw').before(info);
}

function showSheet(id) {
  const s = S.upSheets?.[id], el = $('#sheetView'); if (!el) return;
  S.viewers.forEach(v => v.destroy()); S.viewers = [];
  if (!s || s.status !== 'ready') { el.hidden = true; return; }
  const canEdit = ['owner', 'editor'].includes(S.project.role);
  const meta = [s.drg_no, s.revision, s.scale_text && `scale ${s.scale_text}`, s.drawn_on].filter(Boolean).map(esc).join(' · ');
  el.hidden = false;
  el.innerHTML = `<div class="hd"><div><h2>${esc(s.title || `${s.filename} · page ${s.page_no}`)}</h2><div class="small muted">${meta || esc(s.filename)}</div></div><button class="btn sm" id="closeSheet">Close</button></div>
    ${s.classify_note ? `<div class="pad small"><div class="note warn">${esc(s.classify_note)}</div></div>` : ''}
    <form class="pad row small" id="placeForm">
      <label class="stack small">Type<select class="f" name="sheet_type" ${canEdit ? '' : 'disabled'}><option value="">Not known</option>${(S.sheetTypes || []).map(t => `<option value="${t.key}" ${t.key === s.sheet_type ? 'selected' : ''}>${esc(t.label)}</option>`).join('')}</select></label>
      <label class="stack small">Floor<select class="f" name="floor_id" ${canEdit ? '' : 'disabled'}><option value="">Not known</option>${floorsList().map(f => `<option value="${f.id}" ${f.id === s.floor_id ? 'selected' : ''}>${esc(f.label)}</option>`).join('')}</select></label>
      ${canEdit ? `<button class="btn pri sm" type="submit">${s.classify_status === 'confirmed' ? 'Save' : 'Confirm'}</button>` : ''}
      <span class="muted">${{ auto: `Read from the title block${s.text_source === 'ocr' ? ' (by OCR)' : ''}`, needs_review: 'Needs you', confirmed: 'Checked by a person' }[s.classify_status] || ''}</span>
    </form>
    ${scaleBlock(s, canEdit)}
    <div id="sheetVw" style="height:62vh"></div>`;
  const vw = new Viewer($('#sheetVw'), { title: `${s.filename} page ${s.page_no}`, width: s.width_pt, height: s.height_pt }, { src: s.image_url });
  S.viewers.push(vw);
  if (s.sheet_type === 'beam_layout') showMembers(vw, s);
  if (s.sheet_type === 'beam_schedule') showSpecs(vw, s);
  $('#closeSheet').addEventListener('click', () => { S.viewers.forEach(v => v.destroy()); S.viewers = []; el.hidden = true; history.replaceState(null, '', `${P}upload`); });
  $('#scaleForm')?.addEventListener('submit', async e => {
    e.preventDefault();
    const f = e.target, n = parseInt(String(f.ratio.value).replace(/^\s*1\s*:\s*/, ''), 10);
    const body = e.submitter?.name === 'accept' ? { accept_scale: true } : { scale_ratio: n };
    if (!body.accept_scale && !(n > 0)) { toast('Enter the scale as 1:N, for example 1:100'); f.ratio.focus(); return; }
    try {
      const r = await api(`/api/projects/${encodeURIComponent(PID)}/sheets/${id}`, body, 'PATCH');
      S.upSheets[id] = { ...S.upSheets[id], ...r }; toast('Scale saved'); refreshUploads(); showSheet(id);
    } catch (err) { toast(err.message); }
  });
  $('#placeForm').addEventListener('submit', async e => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector('button'); btn.disabled = true;
    try {
      const r = await api(`/api/projects/${encodeURIComponent(PID)}/sheets/${id}`, { sheet_type: f.sheet_type.value || null, floor_id: f.floor_id.value || null }, 'PATCH');
      S.upSheets[id] = { ...S.upSheets[id], ...r };
      toast('Saved'); refreshUploads(); showSheet(id);
    } catch (err) { btn.disabled = false; toast(err.message); }
  });
  el.scrollIntoView({ block: 'nearest' });
}

/* ---------- review: check what the layout reader found ---------- */
const RV = { q: null, i: 0, mode: null, picks: [], sheetId: null, vw: null, members: {} };
const ftIn = inch => inch == null ? '—' : `${Math.floor(inch / 12)}'-${Math.round(inch % 12)}"`;

async function review(memberId) {
  const canEdit = ['owner', 'editor'].includes(S.project.role);
  main.innerHTML = header('Review', 'Check each beam and column the drawings were read into. Least certain first.', '<span id="rvProg" class="small muted"></span>') + `
  <div class="progress"><i id="rvBar"></i></div>
  <div class="grid g-main rv">
    <section class="panel"><div id="rvVw" style="height:70vh"></div></section>
    <section class="panel pad stack" id="rvForm"><div class="muted small">Loading…</div></section>
  </div>`;
  RV.vw = null; RV.sheetId = null; RV.mode = null; RV.picks = []; RV.members = {};
  RV.q = await api(`/api/projects/${encodeURIComponent(PID)}/review`);
  if (S.view !== 'review') return;
  RV.canEdit = canEdit;
  const at = memberId ? RV.q.items.findIndex(m => m.id === memberId) : 0;
  RV.i = Math.max(0, at);
  rvShow();
}

function rvProgress() {
  const { total, checked } = RV.q;
  $('#rvProg').textContent = total ? `${checked} of ${total} checked` : '';
  $('#rvBar').style.width = total ? `${checked / total * 100}%` : '0';
  const c = $('#reviewCount'); if (c) { c.hidden = !(total - checked); c.textContent = total - checked; }
}

async function rvShow() {
  rvProgress();
  const form = $('#rvForm'); if (!form) return;
  const m = RV.q.items[RV.i];
  if (!m) {
    form.innerHTML = RV.q.total ? `<h2>All checked</h2><p class="small">Every member on this project's layouts has been looked at.</p>${RV.canEdit && RV.sheetId ? '<div><button class="btn" id="rvAdd">Add a beam the reader missed</button></div>' : ''}`
      : `<h2>Nothing to review yet</h2><p class="small">Upload a beam layout on <a href="${P}upload">Add drawings</a>. Once it has been read, its beams and columns show up here.</p>`;
    $('#rvAdd')?.addEventListener('click', () => rvStartPick('add'));
    return;
  }
  history.replaceState(null, '', `${P}review/${m.id}`);
  if (RV.sheetId !== m.sheet.id) {
    S.viewers.forEach(v => v.destroy()); S.viewers = [];
    RV.vw = new Viewer($('#rvVw'), { title: m.sheet.title || m.sheet.drg_no, width: m.sheet.width_pt, height: m.sheet.height_pt }, { src: m.sheet.image_url });
    S.viewers.push(RV.vw); RV.sheetId = m.sheet.id;
    RV.vw.onSheetClick = pt => rvPick(pt);
    try { RV.members[m.sheet.id] = await api(`/api/projects/${encodeURIComponent(PID)}/sheets/${m.sheet.id}/members`); } catch { RV.members[m.sheet.id] = []; }
  }
  const box = m.label_box, cx = m.placed ? (m.x1 + m.x2) / 2 : (box ? (box[0] + box[2]) / 2 : m.x1), cy = m.placed ? (m.y1 + m.y2) / 2 : (box ? (box[1] + box[3]) / 2 : m.y1);
  requestAnimationFrame(() => RV.vw.focus(cx, cy, 3.2));
  rvDraw();
  const kind = m.kind === 'column' ? 'Column' : 'Beam';
  const dis = RV.canEdit ? '' : 'disabled';
  form.innerHTML = `
    <div class="row between"><h2>${kind} ${esc(m.name || '')}</h2><span class="small muted">${RV.i + 1} of ${RV.q.items.length} to check</span></div>
    <div class="small">${esc(m.sheet.drg_no || '')} · <span style="color:${confColor(m.confidence)}">confidence ${fx(m.confidence * 100)}%</span></div>
    ${m.note && m.placed ? `<div class="note warn small">${esc(m.note)}</div>` : ''}
    ${!m.placed ? '<div class="note warn small">The label was read but no beam was found next to it. Redraw it on the sheet, or mark it not on the drawing.</div>' : ''}
    <dl class="kv small">
      ${m.kind === 'beam' ? `<dt>Drawn width</dt><dd>${m.b_in ? `${fx(m.b_in, 1)}"` : '—'}</dd><dt>Span (centre to centre)</dt><dd>${ftIn(m.length_in)}</dd><dt>Direction</dt><dd>${m.placed ? (m.vertical ? 'Vertical on sheet' : 'Horizontal on sheet') : '—'}</dd>`
        : `<dt>Size as drawn</dt><dd>${m.sheet.pt_per_in ? `${fx((m.x2 - m.x1) / m.sheet.pt_per_in, 0)}" × ${fx((m.y2 - m.y1) / m.sheet.pt_per_in, 0)}"` : '—'}</dd>`}
    </dl>
    ${m.kind === 'beam' ? `<form id="rvEdit" class="stack small">
      <label class="stack">Name<input class="f" name="name" value="${esc(m.name || '')}" ${dis} autocomplete="off"></label>
      <div class="row"><label class="stack grow">Width b (in)<input class="f num" name="b" type="number" step="0.5" value="${m.label_b_in ?? ''}" ${dis}></label>
        <label class="stack grow">Depth D (in)<input class="f num" name="d" type="number" step="0.5" value="${m.label_d_in ?? ''}" ${dis}></label></div>
      <span class="muted">Size as written on the drawing label, e.g. B13(9"X18").</span>
    </form>` : ''}
    ${RV.canEdit ? `<div class="row rvbtns">
      <button class="btn pri" data-a="accept" ${m.placed ? '' : 'disabled'}>Looks right <kbd>A</kbd></button>
      ${m.kind === 'beam' ? `<button class="btn" data-a="save">Save edits <kbd>E</kbd></button><button class="btn" data-a="redraw">Redraw on sheet <kbd>R</kbd></button>` : ''}
      <button class="btn" data-a="reject">Not on drawing <kbd>X</kbd></button></div>` : '<p class="small muted">You can view this project but not change it.</p>'}
    <div class="row"><button class="btn sm" data-a="prev" ${RV.i ? '' : 'disabled'}>← Back <kbd>P</kbd></button><button class="btn sm" data-a="next">Skip <kbd>N</kbd></button>
      ${RV.canEdit ? '<span class="sp"></span><button class="btn sm" data-a="add">Add a missed beam</button>' : ''}</div>
    <div id="rvPick" class="small"></div>`;
  form.querySelectorAll('[data-a]').forEach(b => b.addEventListener('click', e => { e.preventDefault(); rvAct(b.dataset.a); }));
  $('#rvEdit')?.addEventListener('submit', e => { e.preventDefault(); rvAct('save'); });
}

function rvDraw() {
  const m = RV.q.items[RV.i]; if (!RV.vw) return;
  const all = RV.members[RV.sheetId] || [];
  const faint = all.filter(o => !m || o.id !== m.id).map(o => o.kind === 'column'
    ? `<rect x="${o.x1}" y="${o.y1}" width="${o.x2 - o.x1}" height="${o.y2 - o.y1}" style="stroke:#9CA3AF"/>`
    : (o.x1 !== o.x2 || o.y1 !== o.y2) ? `<line x1="${o.x1}" y1="${o.y1}" x2="${o.x2}" y2="${o.y2}" stroke="#9CA3AF"/>` : '').join('');
  let cur = '';
  if (m && m.kind === 'column') cur = `<rect x="${m.x1}" y="${m.y1}" width="${m.x2 - m.x1}" height="${m.y2 - m.y1}" style="stroke:#C2410C;stroke-width:4"/>`;
  else if (m && m.placed) cur = `<line x1="${m.x1}" y1="${m.y1}" x2="${m.x2}" y2="${m.y2}" stroke="#C2410C" style="stroke-width:5;opacity:.9"/>`;
  if (m && m.label_box) { const [a, b, c, d] = m.label_box; cur += `<rect x="${a - 2}" y="${b - 2}" width="${c - a + 4}" height="${d - b + 4}" style="stroke:#C2410C;stroke-dasharray:3 2"/>`; }
  const picks = RV.picks.map(p => `<circle cx="${p.x}" cy="${p.y}" r="2.5" fill="#2563EB"/>`).join('');
  RV.vw.setOverlay(faint + cur + picks);
}

function rvStartPick(mode) {
  RV.mode = mode; RV.picks = []; rvDraw();
  const el = $('#rvPick');
  if (el) el.innerHTML = `<div class="note">${mode === 'add' ? 'Add a beam:' : 'Redraw:'} click one end of the beam on the sheet, then the other. Drag to move around. <button class="btn sm" id="rvCancel">Cancel</button></div>`;
  $('#rvCancel')?.addEventListener('click', () => { RV.mode = null; RV.picks = []; rvDraw(); $('#rvPick').innerHTML = ''; });
}

async function rvPick(pt) {
  if (!RV.mode) return;
  RV.picks.push(pt); rvDraw();
  if (RV.picks.length < 2) return;
  const [a, b] = RV.picks, mode = RV.mode;
  RV.mode = null;
  const ends = { x1: a.x, y1: a.y, x2: b.x, y2: b.y };
  if (mode === 'redraw') return rvSend(ends);
  $('#rvPick').innerHTML = `<form id="rvNew" class="row note"><label>Name <input class="f" name="name" placeholder="B48" style="width:90px" autofocus></label>
    <label>b×D <input class="f num" name="b" style="width:50px" placeholder="9">×<input class="f num" name="d" style="width:50px" placeholder="18"></label>
    <button class="btn sm pri">Add</button></form>`;
  $('#rvNew').addEventListener('submit', async e => {
    e.preventDefault(); const f = e.target;
    try {
      const m = await api(`/api/projects/${encodeURIComponent(PID)}/sheets/${RV.sheetId}/members`, { ...ends, name: f.name.value.trim().toUpperCase(), label_b_in: +f.b.value || null, label_d_in: +f.d.value || null });
      RV.members[RV.sheetId].push(m); RV.q.total++; RV.q.checked++; RV.picks = []; toast(`${m.name} added`); rvShow();
    } catch (err) { toast(err.message); }
  });
  $('#rvNew').name.focus();
}

async function rvSend(body) {
  const m = RV.q.items[RV.i];
  try {
    const r = await api(`/api/projects/${encodeURIComponent(PID)}/members/${m.id}`, body, 'PATCH');
    const list = RV.members[RV.sheetId] || []; const k = list.findIndex(o => o.id === r.id); if (k >= 0) list[k] = r;
    RV.picks = [];
    if (r.status === 'auto') { RV.q.items[RV.i] = r; rvShow(); return; }
    RV.q.items.splice(RV.i, 1); RV.q.checked++;
    toast({ accepted: 'Accepted', edited: 'Saved', rejected: 'Marked not on drawing' }[r.status] || 'Saved');
    if (RV.i >= RV.q.items.length) RV.i = Math.max(0, RV.q.items.length - 1);
    rvShow();
  } catch (err) { toast(err.message); }
}

function rvAct(a) {
  const m = RV.q.items[RV.i];
  if (a === 'next') { if (RV.i < RV.q.items.length - 1) { RV.i++; rvShow(); } return; }
  if (a === 'prev') { if (RV.i > 0) { RV.i--; rvShow(); } return; }
  if (!RV.canEdit) return;
  if (a === 'add') return rvStartPick('add');
  if (!m) return;
  if (a === 'accept') return rvSend({ action: 'accept' });
  if (a === 'reject') return rvSend({ action: 'reject' });
  if (a === 'redraw') return m.kind === 'beam' && rvStartPick('redraw');
  if (a === 'save') {
    const f = $('#rvEdit'); if (!f) return;
    const body = {}, name = f.name.value.trim().toUpperCase(), b = parseFloat(f.b.value), d = parseFloat(f.d.value);
    if (name && name !== m.name) body.name = name;
    if (b && b !== m.label_b_in) body.label_b_in = b;
    if (d && d !== m.label_d_in) body.label_d_in = d;
    if (!Object.keys(body).length) { f.name.focus(); toast('Change the name or size, then save'); return; }
    if (m.placed) body.action = 'accept';
    return rvSend(body);
  }
}

document.addEventListener('keydown', e => {
  if (S.view !== 'review' || e.metaKey || e.ctrlKey || e.altKey || e.target.closest('input,select,textarea')) return;
  const a = { a: 'accept', e: 'edit', x: 'reject', n: 'next', p: 'prev', r: 'redraw' }[e.key.toLowerCase()];
  if (!a) { if (e.key === 'Escape' && RV.mode) { RV.mode = null; RV.picks = []; rvDraw(); const el = $('#rvPick'); if (el) el.innerHTML = ''; } return; }
  e.preventDefault();
  if (a === 'edit') { $('#rvEdit')?.name.focus(); $('#rvEdit')?.name.select(); return; }
  rvAct(a);
});

boot().catch(e => { main.innerHTML = `<div class="pad">Could not load Takeoff. ${esc(e.message)}</div>`; console.error(e); });
