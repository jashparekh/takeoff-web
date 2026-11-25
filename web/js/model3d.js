// 3D framing view: measured floor solid and clickable, other floors ghosted. Units: metres.
import * as THREE from '../vendor/three.module.min.js';
import { OrbitControls } from '../vendor/OrbitControls.js';

const IN = 0.0254;

const DISPLAY_SLAB_IN = 5;            // slab thickness for the picture only, before assumptions exist

export class Model3D {
  constructor(el, { model, calc, floorH = 3.0, level = 2, levels = 4, onPick, onHover }) {
    this.el = el; this.model = model; this.calc = calc; this.onPick = onPick; this.onHover = onHover;
    this.floorH = floorH; this.level = level; this.levels = levels;
    this.Y0 = level * floorH;                       // top of measured slab
    const css = getComputedStyle(document.documentElement);
    this.tok = n => css.getPropertyValue(n).trim() || '#888';
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    el.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(this.tok('--sunk'));
    this.camera = new THREE.PerspectiveCamera(40, 1, 0.1, 2000);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    Object.assign(this.controls, { enableDamping: true, dampingFactor: 0.12, screenSpacePanning: true, minDistance: 2, maxDistance: 250, zoomToCursor: true });
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8a8a, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.4); sun.position.set(30, 60, 20); this.scene.add(sun);
    this.pickables = []; this.groups = {};
    this.build();
    this.ray = new THREE.Raycaster(); this.mouse = new THREE.Vector2();
    this.bind();
    this.resize(); this.home();
    this.ro = new ResizeObserver(() => this.resize()); this.ro.observe(el);
    this.loop = () => { this.raf = requestAnimationFrame(this.loop); this.controls.update(); this.renderer.render(this.scene, this.camera); };
    this.loop();
  }

  build() {
    const m = this.model, a = this.calc?.assumptions, Y0 = this.Y0;
    // Before a calculation exists (a project read from uploads), sizes come from the drawing labels and the slab is
    // shown at a display thickness; the side panel says so.
    const t = (a ? a.slab_t_in : DISPLAY_SLAB_IN) * IN;
    const byName = this.calc ? Object.fromEntries(this.calc.beams.map(b => [b.name, b]))
      : Object.fromEntries(m.beams.map(b => [b.name, { b_in: b.b_in || 9, D_in: b.D_in, base: b.name.split('-')[0] }]));
    const [W, Dd] = m.size; this.center = new THREE.Vector3(W / 2, Y0, Dd / 2); this.extent = Math.max(W, Dd);

    // measured slab
    const pts = m.slab.outline.length >= 3 ? m.slab.outline : [[0, 0], [W, 0], [W, Dd], [0, Dd]];
    const shape = new THREE.Shape(pts.map(([x, y]) => new THREE.Vector2(x, -y)));
    const slabGeo = new THREE.ExtrudeGeometry(shape, { depth: t, bevelEnabled: false });
    slabGeo.rotateX(-Math.PI / 2); slabGeo.translate(0, Y0 - t, 0);
    this.slabMat = new THREE.MeshStandardMaterial({ color: this.tok('--slab'), transparent: true, opacity: 0.55, roughness: 0.9, depthWrite: false });
    const slab = new THREE.Mesh(slabGeo, this.slabMat);
    slab.userData = { kind: 'slab', id: 'slab' }; slab.renderOrder = 2;
    slab.add(new THREE.LineSegments(new THREE.EdgesGeometry(slabGeo, 25), new THREE.LineBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.3 })));
    if (m.slab.outline.length >= 3) { this.add('slab', slab); this.pickables.push(slab); }
    this.slab = slab;

    // beams
    this.beamMeshes = {};
    for (const bm of m.beams) {
      const c = byName[bm.name]; if (!c) continue;
      const b = c.b_in * IN, D = c.D_in * IN;
      const dx = bm.p2[0] - bm.p1[0], dz = bm.p2[1] - bm.p1[1], len = Math.hypot(dx, dz);
      const g = new THREE.BoxGeometry(bm.vertical ? b : len, D - t, bm.vertical ? len : b);
      const mat = new THREE.MeshStandardMaterial({ color: this.tok('--b-top'), roughness: 0.7 });
      const mesh = new THREE.Mesh(g, mat);
      mesh.position.set((bm.p1[0] + bm.p2[0]) / 2, Y0 - t - (D - t) / 2, (bm.p1[1] + bm.p2[1]) / 2);
      mesh.userData = { kind: 'beam', id: c.base, part: bm.name, cant: bm.cant };
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(g), new THREE.LineBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.25 }));
      mesh.add(edges);
      this.add('beams', mesh); this.pickables.push(mesh);
      (this.beamMeshes[c.base] ||= []).push(mesh);
    }

    // columns: storey below the measured slab solid, the rest ghosted
    const colMat = new THREE.MeshStandardMaterial({ color: this.tok('--col'), roughness: 0.8 });
    const ghostCol = new THREE.MeshStandardMaterial({ color: this.tok('--muted'), transparent: true, opacity: 0.12, depthWrite: false });
    for (const [i, cl] of m.columns.entries()) {
      const h = this.floorH - 0.46;
      const g = new THREE.BoxGeometry(cl.w, h, cl.d); const mesh = new THREE.Mesh(g, colMat);
      mesh.position.set(cl.c[0], Y0 - 0.46 - h / 2, cl.c[1]);
      mesh.userData = { kind: 'column', id: `C${i + 1}` };
      this.add('columns', mesh); this.pickables.push(mesh);
      const gh = new THREE.Mesh(new THREE.BoxGeometry(cl.w, this.levels * this.floorH, cl.d), ghostCol);
      gh.position.set(cl.c[0], this.levels * this.floorH / 2, cl.c[1]); gh.userData = { kind: 'ghost' };
      this.add('ghost', gh);
    }

    // ghost floors (same outline, not measured)
    const ghostSlab = new THREE.MeshStandardMaterial({ color: this.tok('--muted'), transparent: true, opacity: 0.07, depthWrite: false, side: THREE.DoubleSide });
    const edgeMat = new THREE.LineBasicMaterial({ color: this.tok('--muted'), transparent: true, opacity: 0.35 });
    const outline = this.outline();
    for (let L = 1; L <= this.levels; L++) {
      if (L === this.level) continue;
      const y = L * this.floorH;
      const gm = new THREE.Mesh(slabGeo.clone().translate(0, y - Y0, 0), ghostSlab);
      gm.userData = { kind: 'ghost', level: L }; gm.renderOrder = 1;
      this.add('ghost', gm); this.pickables.push(gm);
      const ln = new THREE.LineSegments(outline.clone().translate(0, y - Y0, 0), edgeMat); this.add('ghost', ln);
    }
    // ground
    const grid = new THREE.GridHelper(Math.ceil(this.extent * 1.6), Math.ceil(this.extent * 1.6 / 2), this.tok('--line2'), this.tok('--line'));
    grid.position.set(W / 2, 0, Dd / 2); this.add('ground', grid);

    this.highlight = new THREE.Color(this.tok('--accent'));
  }

  outline() { // edges of the measured slab footprint (top face) as line segments
    const g = new THREE.EdgesGeometry(this.slab.geometry, 30);
    return g;
  }

  add(group, obj) {
    if (!this.groups[group]) { this.groups[group] = new THREE.Group(); this.scene.add(this.groups[group]); }
    this.groups[group].add(obj);
  }
  show(group, on) { if (this.groups[group]) this.groups[group].visible = on; }

  colorBeams(fn) {      // fn(baseName, mesh) -> css colour
    for (const [n, list] of Object.entries(this.beamMeshes)) for (const m of list) {
      const c = fn(n, m); m.material.color.set(c); m.userData.base = m.material.color.clone();
    }
    this.restyle();
  }
  restyle() {
    for (const list of Object.values(this.beamMeshes)) for (const m of list) {
      const sel = this.sel && this.sel.kind === 'beam' && this.sel.id === m.userData.id;
      m.material.emissive.set(sel ? 0x10245e : 0x000000);
      m.scale.set(1, 1, 1);
      if (sel) m.material.color.set('#2563EB'); else if (m.userData.base) m.material.color.copy(m.userData.base);
    }
    this.slabMat.color.set(this.sel?.kind === 'slab' ? this.tok('--accent-soft') : this.tok('--slab'));
  }
  select(sel) { this.sel = sel; this.restyle(); }

  bind() {
    const dom = this.renderer.domElement;
    let down = null;
    dom.addEventListener('pointerdown', e => { down = { x: e.clientX, y: e.clientY }; });
    dom.addEventListener('pointerup', e => {
      if (!down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) return;
      const hit = this.pick(e); this.select(hit); this.onPick && this.onPick(hit);
    });
    dom.addEventListener('dblclick', e => { const hit = this.pick(e); if (hit) this.focus(hit); });
    dom.addEventListener('pointermove', e => {
      if (e.buttons) return;
      const hit = this.pick(e); dom.style.cursor = hit && hit.kind !== 'ghost' ? 'pointer' : 'grab';
      this.onHover && this.onHover(hit, e);
    });
    dom.addEventListener('pointerleave', () => this.onHover && this.onHover(null));
  }
  pick(e) {
    const r = this.renderer.domElement.getBoundingClientRect();
    this.mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    this.ray.setFromCamera(this.mouse, this.camera);
    const vis = this.pickables.filter(o => { let p = o; while (p) { if (!p.visible) return false; p = p.parent; } return true; });
    const hits = this.ray.intersectObjects(vis, false);
    if (!hits.length) return null;
    // prefer structure over the semi-transparent slab and ghosts
    const pref = hits.find(h => h.object.userData.kind === 'beam' || h.object.userData.kind === 'column') || hits.find(h => h.object.userData.kind === 'slab') || hits[0];
    return { ...pref.object.userData, point: pref.point };
  }
  focus(sel) {
    let box = new THREE.Box3();
    if (sel.kind === 'beam') (this.beamMeshes[sel.id] || []).forEach(m => box.expandByObject(m));
    else if (sel.kind === 'slab') box.setFromObject(this.slab);
    else if (sel.point) box.setFromCenterAndSize(sel.point, new THREE.Vector3(4, 4, 4));
    if (box.isEmpty()) return;
    const c = box.getCenter(new THREE.Vector3()), s = box.getSize(new THREE.Vector3()).length();
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    this.controls.target.copy(c);
    this.camera.position.copy(c.clone().add(dir.multiplyScalar(Math.max(9, s * 1.6))));
  }
  home() {
    const c = this.center, e = this.extent;
    this.controls.target.set(c.x, this.Y0 - 1.2, c.z);
    this.camera.position.set(c.x + e * 0.5, this.Y0 + e * 0.48, c.z + e * 0.62);
  }
  view(kind) {
    const c = this.center, e = this.extent;
    this.controls.target.set(c.x, this.Y0 - 0.5, c.z);
    if (kind === 'top') this.camera.position.set(c.x, this.Y0 + e * 1.25, c.z + 0.01);
    else if (kind === 'below') this.camera.position.set(c.x + e * 0.4, this.Y0 - e * 0.55, c.z + e * 0.6);
    else this.home();
  }
  zoom(f) {
    const d = this.camera.position.clone().sub(this.controls.target);
    this.camera.position.copy(this.controls.target.clone().add(d.multiplyScalar(f)));
  }
  resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight; if (!w || !h) return;
    this.renderer.setSize(w, h, false); this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
  }
  destroy() { cancelAnimationFrame(this.raf); this.ro.disconnect(); this.controls.dispose(); this.renderer.dispose(); this.renderer.forceContextLoss(); }
}
