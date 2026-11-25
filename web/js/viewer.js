// Pan / zoom drawing viewer. The SVG is re-rasterised at each zoom (img width changes) so lines stay sharp.
export class Viewer {
  constructor(el, sheet, { src, hotspots = [], onPick, legend, minZoom = 0.6, maxZoom = 14, tools = true } = {}) {
    this.el = el; this.sheet = sheet; this.onPick = onPick;
    el.classList.add('viewer');
    el.innerHTML = `<div class="canvas"><img alt="${sheet.title}" draggable="false"></div>` +
      (tools ? `<div class="tools"><button data-z="in" aria-label="Zoom in">+</button><button data-z="out" aria-label="Zoom out">−</button><button data-z="fit" aria-label="Fit sheet">⤢</button></div>` : '') +
      (legend ? `<div class="legend">${legend}</div>` : '');
    this.canvas = el.querySelector('.canvas'); this.img = el.querySelector('img');
    this.img.src = src;
    this.W = sheet.width; this.H = sheet.height;
    this.minZoom = minZoom; this.maxZoom = maxZoom;
    this.s = 1; this.tx = 0; this.ty = 0;
    this.hot = [];
    this.setHotspots(hotspots);
    this.bind();
    requestAnimationFrame(() => this.fit());
  }
  get base() { return this.el.clientWidth / this.W; }      // px per pt at zoom 1 (fit width)
  setHotspots(list) {
    this.hot.forEach(h => h.node.remove()); this.hot = [];
    for (const h of list) {
      const b = document.createElement('button');
      b.className = 'hot'; b.title = h.label || h.id; b.setAttribute('aria-label', h.label || h.id);
      if (h.color) b.style.setProperty('--hc', h.color);
      b.addEventListener('click', e => { e.stopPropagation(); this.onPick && this.onPick(h); });
      this.canvas.appendChild(b);
      this.hot.push({ ...h, node: b });
    }
    this.layout();
  }
  select(id) { this.hot.forEach(h => h.node.classList.toggle('sel', h.id === id)); }
  // Vector overlay in sheet points (e.g. members read off the sheet); scales with the image.
  setOverlay(inner) {
    if (!this.svg) {
      this.svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      this.svg.setAttribute('class', 'overlay');
      this.svg.setAttribute('viewBox', `0 0 ${this.W} ${this.H}`);
      this.canvas.insertBefore(this.svg, this.img.nextSibling);
    }
    this.svg.innerHTML = inner;
    this.layout();
  }
  layout() {
    const k = this.base * this.s;
    this.img.style.width = (this.W * k) + 'px';
    if (this.svg) { this.svg.style.width = (this.W * k) + 'px'; this.svg.style.height = (this.H * k) + 'px'; }
    this.canvas.style.transform = `translate(${this.tx}px,${this.ty}px)`;
    const r = Math.max(6, Math.min(22, 6.5 * k));
    for (const h of this.hot) {
      h.node.style.left = (h.x * k) + 'px'; h.node.style.top = (h.y * k) + 'px';
      h.node.style.width = h.node.style.height = (2 * r) + 'px';
    }
  }
  zoomAt(f, cx, cy) {
    const s2 = Math.min(this.maxZoom, Math.max(this.minZoom, this.s * f));
    const r = s2 / this.s;
    this.tx = cx - (cx - this.tx) * r; this.ty = cy - (cy - this.ty) * r; this.s = s2;
    this.layout();
  }
  fit() {
    const k = Math.min(this.el.clientWidth / this.W, this.el.clientHeight / this.H) / this.base;
    this.s = k || 1;
    this.tx = (this.el.clientWidth - this.W * this.base * this.s) / 2;
    this.ty = (this.el.clientHeight - this.H * this.base * this.s) / 2;
    this.layout();
  }
  focus(x, y, zoom = 4) {        // centre on sheet point (pt)
    this.s = zoom;
    const k = this.base * this.s;
    this.tx = this.el.clientWidth / 2 - x * k; this.ty = this.el.clientHeight / 2 - y * k;
    this.layout();
  }
  bind() {
    const el = this.el;
    el.addEventListener('wheel', e => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      this.zoomAt(Math.exp(-e.deltaY * 0.0018), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    const pts = new Map(); let last = null, pinch = null;
    el.addEventListener('pointerdown', e => {
      if (e.target.closest('.tools,.hot')) return;
      el.setPointerCapture(e.pointerId); pts.set(e.pointerId, e); el.classList.add('drag');
      last = { x: e.clientX, y: e.clientY }; pinch = null;
    });
    el.addEventListener('pointermove', e => {
      if (!pts.has(e.pointerId)) return;
      pts.set(e.pointerId, e);
      if (pts.size === 2) {
        const [a, b] = [...pts.values()];
        const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
        const r = el.getBoundingClientRect();
        const cx = (a.clientX + b.clientX) / 2 - r.left, cy = (a.clientY + b.clientY) / 2 - r.top;
        if (pinch) this.zoomAt(d / pinch, cx, cy);
        pinch = d; return;
      }
      this.tx += e.clientX - last.x; this.ty += e.clientY - last.y; last = { x: e.clientX, y: e.clientY };
      this.layout();
    });
    const up = e => { pts.delete(e.pointerId); if (!pts.size) el.classList.remove('drag'); pinch = null; const p = [...pts.values()][0]; if (p) last = { x: p.clientX, y: p.clientY }; };
    el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
    el.querySelectorAll('.tools button').forEach(b => b.addEventListener('click', () => {
      const z = b.dataset.z, cx = el.clientWidth / 2, cy = el.clientHeight / 2;
      if (z === 'fit') this.fit(); else this.zoomAt(z === 'in' ? 1.5 : 1 / 1.5, cx, cy);
    }));
    this.ro = new ResizeObserver(() => this.layout()); this.ro.observe(el);
  }
  destroy() { this.ro && this.ro.disconnect(); }
}
