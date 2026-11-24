// Draws a beam elevation and two cross-sections from the engine output, to scale along the length.
const IN = 25.4;
export const ROLE = {
  'Top through': { c: 'r-top', short: 'top through', mark: 'T' },
  'Top main': { c: 'r-top', short: 'top main', mark: 'T' },
  'Bottom through': { c: 'r-bot', short: 'bottom', mark: 'B' },
  'Bottom extra': { c: 'r-ext', short: 'bottom extra', mark: 'E' },
  'Extra top @ support': { c: 'r-xtop', short: 'extra top', mark: 'X' },
  'Stirrups': { c: 'r-stir', short: 'stirrups', mark: 'S' },
};
const fx = (v, n = 0) => Number(v).toLocaleString('en-IN', { maximumFractionDigits: n, minimumFractionDigits: n });
const ftin = mm => { const t = Math.round(mm / IN); return `${Math.floor(t / 12)}′-${t % 12}″`; };

export function marks(beam) {
  const out = []; const cnt = {};
  for (const x of beam.bars) { const m = ROLE[x.role].mark; cnt[m] = (cnt[m] || 0) + 1; out.push({ ...x, mark: m + cnt[m] }); }
  out.push({ ...beam.stirrups, mark: 'S1' });
  return out;
}

export function elevation(beam, a) {
  const L = beam.clear_in * IN, b = beam.b_in * IN, D = beam.D_in * IN, t = a.slab_t_in * IN, cov = a.cover_mm;
  const cant = beam.kind === 'C';
  const bars = marks(beam);
  const maxTopD = Math.max(12, ...beam.bars.filter(x => x.role.startsWith('Top')).map(x => x.dia));
  const Ld = a.ld_factor * maxTopD;
  // horizontal extent (mm): span from 0..L, supports outside
  const left = cant ? -(L + b + 250) : -(b + Ld + 250);
  const right = cant ? L + 350 : L + b + Ld + 250;
  const W = 760, padL = 16, k = (W - 2 * padL) / (right - left);
  const X = mm => +(padL + (mm - left) * k).toFixed(1);
  const kv = Math.min(0.42, 170 / D);               // vertical scale (exaggerated)
  const top = 92, bot = top + D * kv;
  const o = [];
  const line = (x1, y1, x2, y2, c) => o.push(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" class="${c}"/>`);
  const text = (x, y, s, c = 's-lbl', an = 'start') => o.push(`<text x="${x}" y="${y}" class="${c}" text-anchor="${an}">${s}</text>`);
  // slab + beam + supports
  o.push(`<rect x="${X(left)}" y="${top}" width="${X(right) - X(left)}" height="${t * kv}" class="s-slab"/>`);
  o.push(`<rect x="${X(left)}" y="${top + t * kv}" width="${X(right) - X(left)}" height="${(D - t) * kv}" class="s-conc"/>`);
  const sups = cant ? [-b] : [-b, L];
  for (const s of sups) {
    o.push(`<rect x="${X(s)}" y="${top}" width="${X(s + b) - X(s)}" height="${bot - top + 46}" class="s-col"/>`);
    text((X(s) + X(s + b)) / 2, bot + 60, 'support', 's-lbl', 'middle');
  }
  if (cant) { // free end: concrete stops at L
    o.push(`<rect x="${X(L)}" y="${top + t * kv - 1}" width="${X(right) - X(L) + 1}" height="${(D - t) * kv + 2}" class="s-bg"/>`);
    line(X(L), top + t * kv, X(L), bot, 's-edge');
    text(X(L) + 6, bot - 6, 'free end');
  }
  line(X(left), top, X(right), top, 's-edge'); line(X(left), bot, X(right), bot, 's-edge');
  // stirrups
  const st = beam.stirrups, se = st.spacing.end_in * IN, sm = st.spacing.mid_in * IN;
  const pos = [];
  if (cant) { for (let i = 0; i < st.nos; i++) pos.push(50 + i * se); }
  else {
    const z = L / 3;
    for (let x = 50; x <= z; x += se) { pos.push(x); pos.push(L - x); }
    for (let x = z + sm / 2; x < 2 * z; x += sm) pos.push(x);
  }
  for (const p of pos) if (p <= L) line(X(p), top + cov * kv, X(p), bot - cov * kv, 's-stir');
  // bars: lanes
  const y = { top: top + (cov + 9) * kv, xtop: top + (cov + 9) * kv + 7, bot: bot - (cov + 9) * kv, ext: bot - (cov + 9) * kv - 8 };
  const labels = [];
  for (const m of bars) {
    if (m.role === 'Stirrups') continue;
    const r = ROLE[m.role]; let segs = [], yy;
    if (m.role === 'Top through' || m.role === 'Top main') {
      yy = y.top;
      segs = cant ? [[-(L + b), L, true]] : [[-a.ld_factor * m.dia, L + a.ld_factor * m.dia]];
    } else if (m.role === 'Bottom through') {
      yy = y.bot; segs = cant ? [[-b, L, true]] : [[-a.bot_anch_mm, L + a.bot_anch_mm]];
    } else if (m.role === 'Bottom extra') {
      yy = y.ext; const al = a.extra_bot_frac; segs = [[(1 - al) / 2 * L, (1 + al) / 2 * L]];
    } else {
      yy = y.xtop; const into = m.cut_mm - b / 2;
      segs = cant ? [[-b / 2, into]] : [[-b / 2, into], [L - into, L + b / 2]];
    }
    for (const [s1, s2, hook] of segs) {
      o.push(`<line x1="${X(s1)}" y1="${yy}" x2="${X(s2)}" y2="${yy}" class="s-bar ${r.c}"/>`);
      if (hook) o.push(`<line x1="${X(s2)}" y1="${yy}" x2="${X(s2)}" y2="${yy + (yy < (top + bot) / 2 ? 14 : -14)}" class="s-bar ${r.c}"/>`);
    }
    labels.push({ m, x: X((segs[0][0] + segs[0][1]) / 2), y: yy, up: yy < (top + bot) / 2 });
  }
  // labels: top ones above the beam, bottom ones below
  const ups = labels.filter(l => l.up), dns = labels.filter(l => !l.up);
  const place = (arr, baseY, dir) => arr.forEach((l, i) => {
    const ly = baseY + dir * i * 17, lx = X(cant ? L * 0.15 : L * 0.12) + i * 4;
    line(l.x, l.y, lx + 4, ly + (dir < 0 ? 4 : -4), 's-lead');
    o.push(`<rect x="${lx - 2}" y="${ly - 11}" width="22" height="15" rx="3" class="s-bg"/>`);
    text(lx, ly, l.m.mark, `s-lbl b`);
    text(lx + 24, ly, `${l.m.nos}-${l.m.dia}φ ${ROLE[l.m.role].short} · L ${fx(l.m.cut_mm)}`);
  });
  place(ups, top - 14, -1);
  // dims
  const dim = (a1, a2, yy, s) => {
    line(X(a1), yy, X(a2), yy, 's-dim');
    [a1, a2].forEach(v => { line(X(v) - 3, yy + 3, X(v) + 3, yy - 3, 's-dim'); line(X(v), yy - 6, X(v), yy + 6, 's-dim'); });
    const w = s.length * 6.7 + 8, cx = (X(a1) + X(a2)) / 2;
    if (X(a2) - X(a1) > w) { o.push(`<rect x="${cx - w / 2}" y="${yy - 8}" width="${w}" height="15" class="s-bg"/>`); text(cx, yy + 4, s, 's-dimt', 'middle'); }
    else text(X(a2) + 4, yy + 4, s, 's-dimt');
  };
  let dy = bot + 82;
  place(dns, dy, 1); dy += dns.length * 17 + 14;
  dim(0, L, dy, `${cant ? 'OVERHANG' : 'CLEAR SPAN'} ${fx(L)} (${ftin(L)})`); dy += 26;
  if (!cant) {
    const z = L / 3;
    dim(0, z, dy, `@${fx(se)} (${st.spacing.end_in}″)`); dim(z, 2 * z, dy, `@${fx(sm)} (${st.spacing.mid_in}″)`); dim(2 * z, L, dy, `@${fx(se)} (${st.spacing.end_in}″)`); dy += 26;
    if (beam.bars.some(x => x.role === 'Bottom extra')) { const al = a.extra_bot_frac; dim((1 - al) / 2 * L, (1 + al) / 2 * L, dy, `${al}L = ${fx(al * L)} extra bottom`); dy += 26; }
    dim(-a.ld_factor * maxTopD, 0, 50, `Ld ${fx(a.ld_factor * maxTopD)}`);
  } else { dim(0, L, dy, `stirrups @${fx(se)} (${st.spacing.end_in}″) · ${st.nos} nos`); dy += 26; }
  if (!cant) {
    const xa = X(L * 0.06), xb = X(L / 2 - 0.04 * L);
    for (const [xc, n] of [[xa, 'A'], [xb, 'B']]) { line(xc, top - 4, xc, bot + 18, 's-cut'); text(xc, bot + 32, n, 's-h', 'middle'); }
  }
  const H = dy + 8;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Elevation of ${beam.name}" style="min-width:620px">${o.join('')}</svg>`;
}

export function sections(beam, a) {
  const b = beam.b_in * IN, D = beam.D_in * IN, t = a.slab_t_in * IN, c = a.cover_mm;
  const s = Math.min(0.42, 175 / D);
  const groups = r => beam.bars.filter(x => x.role === r);
  const cant = beam.kind === 'C';
  const one = (ox, title, rows) => {
    const W = b * s, H = D * s, o = [];
    o.push(`<path d="M${ox - 50} 20 H${ox + W + 50} V${20 + t * s} H${ox + W} V${20 + H} H${ox} V${20 + t * s} H${ox - 50} Z" class="s-conc" stroke="var(--muted)"/>`);
    o.push(`<rect x="${ox + c * s}" y="${20 + c * s}" width="${W - 2 * c * s}" height="${H - 2 * c * s}" rx="4" class="s-loop"/>`);
    const lane = (y, list) => {
      const n = list.reduce((p, x) => p + x.nos, 0); if (!n) return;
      let i = 0;
      for (const x of list) for (let j = 0; j < x.nos; j++, i++) {
        const xx = n === 1 ? ox + W / 2 : ox + (c + 10) * s + i * (W - 2 * (c + 10) * s) / (n - 1);
        o.push(`<circle cx="${xx}" cy="${y}" r="${Math.max(2.6, x.dia * s / 2)}" class="${ROLE[x.role].c}"/>`);
      }
    };
    let ly = 20 + (c + 10) * s;
    for (const [y, list] of rows) lane(20 + y * s, list);
    o.push(`<text x="${ox + W / 2}" y="${20 + H + 22}" class="s-dimt" text-anchor="middle">${fx(b)} × ${fx(D)}</text>`);
    o.push(`<text x="${ox + W / 2}" y="${20 + H + 40}" class="s-h" text-anchor="middle">${title}</text>`);
    return o.join('');
  };
  const top = groups('Top through').concat(groups('Top main')), xt = groups('Extra top @ support'), bt = groups('Bottom through'), ex = groups('Bottom extra');
  const ytop = c + 10, ybot = D - c - 10;
  const A = one(110, cant ? 'AT SUPPORT' : 'A–A NEAR SUPPORT', [[ytop, top], [ytop + 26, xt], [ybot, bt]]);
  const B = cant ? '' : one(330, 'B–B MID-SPAN', [[ytop, top], [ybot, bt], [ybot - 26, ex]]);
  const H = D * s + 70;
  return `<svg viewBox="0 0 ${cant ? 300 : 520} ${H}" role="img" aria-label="Sections of ${beam.name}" style="max-width:${cant ? 300 : 520}px">${A}${B}</svg>`;
}
