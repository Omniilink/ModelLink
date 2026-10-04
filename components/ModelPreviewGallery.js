/*!
 * Omniilink ModelPreviewGallery
 * A framework-agnostic 3D model gallery + interactive Three.js viewport.
 *
 * Loads the app's self-hosted Three.js (window.THREE plus its example
 * loaders/exporters) and renders a responsive model grid beside a large
 * interactive viewport with OrbitControls. Supports .glb/.gltf/.obj/.stl,
 * real downloads, an onModifyModel callback, and full GPU resource cleanup.
 *
 * Usage (vanilla):
 *   const gallery = new ModelPreviewGallery(document.getElementById('models-body'), {
 *     apiBase: API,
 *     fetch: authFetch,
 *     onModifyModel: (model) => { ...open the AI coding chat... },
 *     onGenerate: (spec) => { ...ask the AI to build a model... }
 *   });
 *   gallery.load();
 */
(function (global) {
  'use strict';

  var FORMATS = ['glb', 'gltf', 'obj', 'stl', 'mtl'];

  function h(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function ext(path) {
    var m = /\.([a-z0-9]+)(?:\?|#|$)/i.exec(String(path || ''));
    return m ? m[1].toLowerCase() : '';
  }
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  // ---- Procedural texture helpers (canvas -> CanvasTexture) --------------
  var Procedural = {
    _canvas: function (size, draw) {
      var c = document.createElement('canvas');
      c.width = c.height = size || 256;
      var ctx = c.getContext('2d');
      draw(ctx, c.width, c.height);
      return c;
    },
    _noiseCanvas: function (size, scale, octaves) {
      scale = scale || 8; octaves = octaves || 4;
      return Procedural._canvas(size, function (ctx, w, hh) {
        var img = ctx.createImageData(w, hh);
        var seed = 1;
        function rnd() { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; }
        // value-noise lattice
        var grids = [];
        for (var o = 0; o < octaves; o++) {
          var n = Math.max(2, scale << o);
          var g = new Float32Array((n + 1) * (n + 1));
          for (var i = 0; i < g.length; i++) g[i] = rnd();
          grids.push({ n: n, g: g });
        }
        function sample(g, n, x, y) {
          var xi = Math.floor(x), yi = Math.floor(y);
          var xf = x - xi, yf = y - yi;
          function at(a, b) { return g.g[(Math.min(b, n) * (n + 1)) + Math.min(a, n)]; }
          var v00 = at(xi, yi), v10 = at(xi + 1, yi), v01 = at(xi, yi + 1), v11 = at(xi + 1, yi + 1);
          var sx = xf * xf * (3 - 2 * xf), sy = yf * yf * (3 - 2 * yf);
          var a = v00 + (v10 - v00) * sx, b = v01 + (v11 - v01) * sx;
          return a + (b - a) * sy;
        }
        for (var y = 0; y < hh; y++) {
          for (var x = 0; x < w; x++) {
            var v = 0, amp = 1, norm = 0;
            for (var oi = 0; oi < grids.length; oi++) {
              v += amp * sample(grids[oi], grids[oi].n, x / w * grids[oi].n, y / hh * grids[oi].n);
              norm += amp; amp *= 0.5;
            }
            v = v / norm;
            var p = (y * w + x) * 4;
            var c8 = Math.max(0, Math.min(255, Math.round(v * 255)));
            img.data[p] = img.data[p + 1] = img.data[p + 2] = c8;
            img.data[p + 3] = 255;
          }
        }
        ctx.putImageData(img, 0, 0);
      });
    },
    // Wood planks with grain
    wood: function (size) {
      return Procedural._canvas(size || 256, function (ctx, w, hh) {
        ctx.fillStyle = '#7a4a22'; ctx.fillRect(0, 0, w, hh);
        var planks = 5, ph = hh / planks;
        for (var i = 0; i < planks; i++) {
          var base = 90 + (i % 2 ? 12 : 0);
          ctx.fillStyle = 'rgb(' + (base + 40) + ',' + (base + 10) + ',' + (base - 40) + ')';
          ctx.fillRect(0, i * ph, w, ph - 2);
          ctx.strokeStyle = 'rgba(40,22,10,.6)'; ctx.lineWidth = 2;
          ctx.beginPath(); ctx.moveTo(0, i * ph + ph - 1); ctx.lineTo(w, i * ph + ph - 1); ctx.stroke();
          for (var g = 0; g < 30; g++) {
            var y = i * ph + Math.random() * ph;
            ctx.strokeStyle = 'rgba(60,35,15,' + (0.06 + Math.random() * 0.12) + ')';
            ctx.beginPath(); ctx.moveTo(0, y);
            for (var x = 0; x <= w; x += 16) ctx.lineTo(x, y + (Math.random() - 0.5) * 3);
            ctx.stroke();
          }
        }
      });
    },
    // Roughness map (grayscale) from fractal noise
    roughness: function (size) { return Procedural._noiseCanvas(size || 128, 6, 4); },
    // Paint scratches / wear overlay onto a base colour canvas
    wornMetal: function (size) {
      return Procedural._canvas(size || 256, function (ctx, w, hh) {
        var grad = ctx.createLinearGradient(0, 0, w, hh);
        grad.addColorStop(0, '#9aa2ad'); grad.addColorStop(1, '#6f7681');
        ctx.fillStyle = grad; ctx.fillRect(0, 0, w, hh);
        for (var i = 0; i < 220; i++) {
          ctx.strokeStyle = 'rgba(255,255,255,' + (0.03 + Math.random() * 0.08) + ')';
          ctx.beginPath();
          var x = Math.random() * w, y = Math.random() * hh;
          ctx.moveTo(x, y); ctx.lineTo(x + (Math.random() - 0.5) * 40, y + (Math.random() - 0.5) * 6);
          ctx.stroke();
        }
        for (var r = 0; r < 40; r++) {
          ctx.fillStyle = 'rgba(150,80,30,' + (0.05 + Math.random() * 0.15) + ')';
          var rx = Math.random() * w, ry = Math.random() * hh, rr = 2 + Math.random() * 8;
          ctx.beginPath(); ctx.arc(rx, ry, rr, 0, Math.PI * 2); ctx.fill();
        }
      });
    }
  };

  function texFromCanvas(canvas, asColor) {
    var THREE = global.THREE;
    var t = new THREE.CanvasTexture(canvas);
    if (asColor !== false) t.colorSpace = THREE.SRGBColorSpace !== undefined ? THREE.SRGBColorSpace : undefined;
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.anisotropy = 4;
    t.needsUpdate = true;
    return t;
  }

  // ---- A small procedural model library (demonstrates the pipeline) ------
  var Generators = {
    crate: function () {
      var THREE = global.THREE;
      var g = new THREE.Group();
      var wood = texFromCanvas(Procedural.wood(256));
      var rough = texFromCanvas(Procedural.roughness(128), false);
      var mat = new THREE.MeshStandardMaterial({ map: wood, roughnessMap: rough, roughness: 0.9, metalness: 0.0 });
      g.add(new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), mat));
      // 12 edge beams form the frame around the box
      var frameMat = new THREE.MeshStandardMaterial({ color: 0x3a2412, roughness: 0.8 });
      var s = 1.05, t = 0.09;
      function beam(dims, pos) {
        var m = new THREE.Mesh(new THREE.BoxGeometry(dims[0], dims[1], dims[2]), frameMat);
        m.position.set(pos[0], pos[1], pos[2]);
        g.add(m);
      }
      [-1, 1].forEach(function (a) { [-1, 1].forEach(function (b) {
        beam([t, s, t], [a * s / 2, 0, b * s / 2]);   // vertical edges
        beam([s, t, t], [0, a * s / 2, b * s / 2]);   // edges along x
        beam([t, t, s], [a * s / 2, b * s / 2, 0]);   // edges along z
      }); });
      g.userData.suggestedName = 'Procedural Crate';
      return g;
    },
    barrel: function () {
      var THREE = global.THREE;
      var g = new THREE.Group();
      var metal = texFromCanvas(Procedural.wornMetal(256));
      var mat = new THREE.MeshStandardMaterial({ map: metal, metalness: 0.85, roughness: 0.45 });
      var shell = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.5, 1.2, 32, 1, true), mat);
      shell.material.side = THREE.DoubleSide;
      g.add(shell);
      var ring = new THREE.MeshStandardMaterial({ color: 0x2a2f38, metalness: 0.9, roughness: 0.4 });
      [-0.45, 0.45].forEach(function (y) {
        var r = new THREE.Mesh(new THREE.TorusGeometry(0.52, 0.03, 8, 32), ring);
        r.rotation.x = Math.PI / 2; r.position.y = y; g.add(r);
      });
      g.userData.suggestedName = 'Procedural Barrel';
      return g;
    }
  };

  // ---- AI design spec -> geometry ---------------------------------------
  var SPEC_KINDS = ['crate', 'barrel', 'sphere', 'cylinder', 'cone', 'slab', 'ring', 'column'];

  // Models rarely answer with our exact enum — map their words onto the
  // primitives we can actually build. Insertion order is also the priority
  // order used when scanning free text.
  var KIND_ALIASES = {
    sphere: 'sphere', ball: 'sphere', globe: 'sphere', orb: 'sphere', marble: 'sphere',
    barrel: 'barrel', drum: 'barrel', canister: 'barrel', tank: 'barrel', bucket: 'barrel',
    pail: 'barrel', keg: 'barrel', can: 'barrel',
    cone: 'cone', pyramid: 'cone', wedge: 'cone', spike: 'cone', chevron: 'cone',
    ring: 'ring', hoop: 'ring', torus: 'ring', donut: 'ring', tire: 'ring',
    column: 'column', pillar: 'column', post: 'column', pole: 'column', tower: 'column',
    rod: 'column', trunk: 'column', tree: 'column',
    slab: 'slab', plate: 'slab', panel: 'slab', shelf: 'slab', table: 'slab',
    platform: 'slab', disk: 'slab', counter: 'slab', tile: 'slab',
    cylinder: 'cylinder', tube: 'cylinder', pipe: 'cylinder', roll: 'cylinder',
    crate: 'crate', box: 'crate', chest: 'crate', case: 'crate', cube: 'crate',
    block: 'crate', package: 'crate', parcel: 'crate', cabinet: 'crate', bin: 'crate'
  };

  // Aliases that are also ordinary English words. They may be trusted in the
  // model's own short label, but never when scanning the user's sentence —
  // "can you make a wooden crate" must not resolve to a barrel.
  var AMBIGUOUS_ALIASES = { can: 1, case: 1, post: 1, roll: 1, block: 1 };

  // Word-boundary scan of text. Enum kinds are checked first (unambiguous by
  // definition), then the aliases.
  function inferKind(text, allowAmbiguous) {
    var t = String(text || '').toLowerCase();
    if (!t) return '';
    for (var j = 0; j < SPEC_KINDS.length; j++) {
      if (new RegExp('\\b' + SPEC_KINDS[j] + '\\b').test(t)) return SPEC_KINDS[j];
    }
    var keys = Object.keys(KIND_ALIASES);
    for (var i = 0; i < keys.length; i++) {
      if (allowAmbiguous === false && AMBIGUOUS_ALIASES[keys[i]]) continue;
      if (new RegExp('\\b' + keys[i] + '\\b').test(t)) return KIND_ALIASES[keys[i]];
    }
    return '';
  }

  // Resolve whatever the model called it into a buildable kind, falling back
  // to the user's own wording before giving up.
  function coerceKind(raw, hint) {
    var k = String(raw || '').toLowerCase().trim();
    if (SPEC_KINDS.indexOf(k) >= 0) return k;
    if (KIND_ALIASES[k]) return KIND_ALIASES[k];
    var words = k.split(/[^a-z]+/).filter(Boolean);
    for (var i = 0; i < words.length; i++) if (SPEC_KINDS.indexOf(words[i]) >= 0) return words[i];
    for (var j = 0; j < words.length; j++) if (KIND_ALIASES[words[j]]) return KIND_ALIASES[words[j]];
    var fromLabel = inferKind(k, true);
    if (fromLabel) return fromLabel;
    return inferKind(hint, false);
  }

  var SpecParser = {
    // Pull the first JSON object out of a model reply (tolerates prose/fences).
    parse: function (text, hint) {
      var t = String(text || '').replace(/```[a-z]*/gi, '');
      var s = t.indexOf('{'), e = t.lastIndexOf('}');
      if (s < 0 || e <= s) return null;
      var raw = null;
      try { raw = JSON.parse(t.slice(s, e + 1)); } catch (err) { return null; }
      return SpecParser.normalize(raw, hint);
    },
    normalize: function (raw, hint) {
      if (!raw || typeof raw !== 'object') return null;
      var kind = coerceKind(raw.kind, hint || raw.name || '');
      var size = [1, 1, 1], src = raw.size;
      if (Array.isArray(src) && src.length >= 3) size = [src[0], src[1], src[2]];
      else if (src && typeof src === 'object') size = [src.width || src.x || 1, src.height || src.y || 1, src.depth || src.z || 1];
      else if (typeof src === 'number') size = [src, src, src];
      size = size.map(function (n) {
        var v = Number(n);
        if (!isFinite(v) || v <= 0) return 1;
        return Math.min(4, Math.max(0.2, v));
      });
      var num = function (v, d, lo, hi) { v = Number(v); if (!isFinite(v)) return d; return Math.min(hi, Math.max(lo, v)); };
      var parts = [];
      if (Array.isArray(raw.parts)) {
        raw.parts.forEach(function (p) {
          if (!p || typeof p !== 'object') return;
          var pk = coerceKind(p.kind, p.name || '');
          if (SPEC_KINDS.indexOf(pk) < 0) return;
          parts.push({
            kind: pk,
            size: [num(p.size && p.size[0], size[0], 0.02, 4), num(p.size && p.size[1], size[1], 0.02, 4), num(p.size && p.size[2], size[2], 0.02, 4)],
            pos: [num(p.pos && p.pos[0], 0, -4, 4), num(p.pos && p.pos[1], 0, -4, 4), num(p.pos && p.pos[2], 0, -4, 4)],
            rot: [num(p.rot && p.rot[0], 0, -360, 360) * (Math.PI / 180), num(p.rot && p.rot[1], 0, -360, 360) * (Math.PI / 180), num(p.rot && p.rot[2], 0, -360, 360) * (Math.PI / 180)],
            color: String(p.color || '')
          });
        });
      }
      return {
        kind: kind || 'crate',
        name: String(raw.name || '').replace(/\s+/g, ' ').trim().slice(0, 46),
        size: size,
        color: /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(String(raw.color || '')) ? String(raw.color) : '#9aa2ad',
        roughness: num(raw.roughness, 0.8, 0, 1),
        metalness: num(raw.metalness, 0.1, 0, 1),
        detail: (raw.detail === 'low' || raw.detail === 'high') ? raw.detail : 'medium',
        parts: parts
      };
    }
  };

  // Build a mesh group from a normalised design spec.
   // Build a single primitive (box/cylinder/sphere/...) as a small group.
   // Shared by fromSpec for both the whole-object path and each part of a
   // multi-part composition.
   Generators.buildPrimitive = function (kind, w, h, d, seg, mat, trim) {
     var THREE = global.THREE;
     var g = new THREE.Group();
     function add(mesh) { g.add(mesh); return mesh; }
     switch (kind) {
       case 'barrel': {
         var shell = add(new THREE.Mesh(new THREE.CylinderGeometry(w / 2, w / 2, h, seg, 1), mat));
         shell.material.side = THREE.DoubleSide;
         [-h * 0.36, h * 0.36].forEach(function (y) {
           var r = add(new THREE.Mesh(new THREE.TorusGeometry(w / 2 + 0.02, Math.min(0.04, w * 0.05), 8, seg), trim));
           r.rotation.x = Math.PI / 2; r.position.y = y;
         });
         break;
       }
       case 'sphere':
         add(new THREE.Mesh(new THREE.SphereGeometry(w / 2, seg, Math.max(8, Math.round(seg / 2))), mat));
         break;
       case 'cylinder':
         add(new THREE.Mesh(new THREE.CylinderGeometry(w / 2, w / 2, h, seg), mat));
         break;
       case 'cone':
         add(new THREE.Mesh(new THREE.ConeGeometry(w / 2, h, seg), mat));
         break;
       case 'slab': {
         var slab = add(new THREE.Mesh(new THREE.BoxGeometry(w, Math.max(0.1, h * 0.35), d), mat));
         [-w / 2, w / 2].forEach(function (x) {
           var leg = add(new THREE.Mesh(new THREE.BoxGeometry(0.08, h, 0.08), trim));
           leg.position.set(x * 0.9, 0, d * 0.4);
           var leg2 = leg.clone(); leg2.position.z = -d * 0.4; g.add(leg2);
         });
         slab.position.y = h * 0.3;
         break;
       }
       case 'ring': {
         var ring = add(new THREE.Mesh(new THREE.TorusGeometry(w / 2, Math.max(0.04, Math.min(w, h) * 0.12), 10, seg), mat));
         ring.rotation.x = Math.PI / 2;
         break;
       }
       case 'column': {
         add(new THREE.Mesh(new THREE.CylinderGeometry(w / 2, w / 2 * 1.05, h, seg), mat));
         [h / 2, -h / 2].forEach(function (y) {
           var cap = add(new THREE.Mesh(new THREE.BoxGeometry(w * 1.25, Math.min(0.12, h * 0.12), w * 1.25), trim));
           cap.position.y = y;
         });
         break;
       }
       case 'crate':
       default: {
         add(new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat));
         var t = Math.min(0.07, Math.min(w, d) * 0.12);
         [-1, 1].forEach(function (sx) {
           [-1, 1].forEach(function (sz) {
             var post = add(new THREE.Mesh(new THREE.BoxGeometry(t, h * 1.02, t), trim));
             post.position.set(sx * (w / 2 - t / 2), 0, sz * (d / 2 - t / 2));
           });
         });
         break;
       }
     }
     return g;
   };

   Generators.fromSpec = function (spec) {
     var THREE = global.THREE;
     var s = SpecParser.normalize(spec) || SpecParser.normalize({});
     var g = new THREE.Group();
     var w = s.size[0], h = s.size[1], d = s.size[2];
     var seg = s.detail === 'low' ? 12 : (s.detail === 'high' ? 64 : 32);
     var round = (s.kind === 'barrel' || s.kind === 'cylinder' || s.kind === 'sphere' ||
       s.kind === 'ring' || s.kind === 'column' || s.kind === 'cone');
     var mat = new THREE.MeshStandardMaterial({
       color: new THREE.Color(s.color),
       map: texFromCanvas(round ? Procedural.wornMetal(256) : Procedural.wood(256)),
       roughness: s.roughness,
       metalness: s.metalness
     });
     var trim = new THREE.MeshStandardMaterial({
       color: new THREE.Color(s.color).multiplyScalar(0.55),
       roughness: Math.min(1, s.roughness + 0.15),
       metalness: Math.max(0.2, s.metalness)
     });
     function add(mesh) { g.add(mesh); return mesh; }
     if (Array.isArray(s.parts) && s.parts.length) {
       s.parts.forEach(function (p) {
         var pk = (SPEC_KINDS.indexOf(p.kind) >= 0) ? p.kind : s.kind;
         var pw = Math.min(4, Math.max(0.02, Number(p.size[0]) || w));
         var ph = Math.min(4, Math.max(0.02, Number(p.size[1]) || h));
         var pd = Math.min(4, Math.max(0.02, Number(p.size[2]) || d));
         var pm = mat;
         if (p.color && /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(String(p.color))) {
           pm = new THREE.MeshStandardMaterial({
             color: new THREE.Color(p.color),
             map: mat.map, roughness: s.roughness, metalness: s.metalness
           });
         }
         var part = Generators.buildPrimitive(pk, pw, ph, pd, seg, pm, trim);
         part.position.set(Number(p.pos[0]) || 0, Number(p.pos[1]) || 0, Number(p.pos[2]) || 0);
         part.rotation.set(Number(p.rot[0]) || 0, Number(p.rot[1]) || 0, Number(p.rot[2]) || 0);
         g.add(part);
       });
     } else {
       g.add(Generators.buildPrimitive(s.kind, w, h, d, seg, mat, trim));
     }
     g.userData.suggestedName = s.name || '';
     g.userData.spec = s;
     return g;
   };

  // Map a natural-language prompt onto an available generator.
  function pickKind(prompt) {
    var p = String(prompt || '').toLowerCase();
    if (/barrel|drum|cylinder|canister|tank|pipe|tube|pail|bin\b/.test(p)) return 'barrel';
    if (/crate|box|chest|case|block|package|cube/.test(p)) return 'crate';
    return 'crate';
  }
  // Turn a prompt into a readable model name.
  function nameFromPrompt(prompt) {
    var p = String(prompt || '').replace(/[\r\n]+/g, ' ').replace(/\s+/g, ' ').trim();
    if (!p) return '';
    p = p.replace(/^(a|an|the)\s+/i, '');
    if (p.length > 46) p = p.slice(0, 46).replace(/\s+\S*$/, '');
    return p.charAt(0).toUpperCase() + p.slice(1);
  }

  function ModelPreviewGallery(root, opts) {
    if (!(this instanceof ModelPreviewGallery)) return new ModelPreviewGallery(root, opts);
    opts = opts || {};
    this.root = root;
    this.apiBase = opts.apiBase || '';
    this._fetch = opts.fetch || (global.fetch ? global.fetch.bind(global) : null);
    this.onModifyModel = typeof opts.onModifyModel === 'function' ? opts.onModifyModel : function () {};
    this.onModelCreated = typeof opts.onModelCreated === 'function' ? opts.onModelCreated : null;
    this.getModels = typeof opts.getModels === 'function' ? opts.getModels : null;
    this.models = [];
    this.selected = null;
    this.filter = 'all';
    this.query = '';
    this.viewport = null;
    this.busy = false;
    this._loadToken = 0;
    this._disposed = false;
    this.modelChoice = opts.model || ModelPreviewGallery._storageGet('omni_models_model') || 'omniilink';
    this.build();
  }

  ModelPreviewGallery._storageGet = function (k) {
    try { return global.localStorage ? global.localStorage.getItem(k) : ''; } catch (e) { return ''; }
  };
  ModelPreviewGallery._storageSet = function (k, v) {
    try { if (global.localStorage) global.localStorage.setItem(k, v); } catch (e) {}
  };

  ModelPreviewGallery.prototype.build = function () {
    var self = this;
    var wrap = h('div', 'mpg');
    // toolbar
    var bar = h('div', 'mpg-bar');
    this.searchEl = h('input', 'mpg-search');
    this.searchEl.type = 'search';
    this.searchEl.placeholder = 'Search models or prompts...';
    this.searchEl.oninput = function () { self.query = this.value.trim().toLowerCase(); self.renderGrid(); };
    bar.appendChild(this.searchEl);
    var filters = h('div', 'mpg-filters');
    [['all', 'All'], ['gltf', 'GLTF / GLB'], ['obj', 'OBJ'], ['stl', 'STL'], ['recent', 'Recent'], ['high', 'High quality']].forEach(function (f) {
      var b = h('button', 'mpg-filter' + (f[0] === 'all' ? ' active' : ''), f[1]);
      b.dataset.f = f[0];
      b.onclick = function () { self.filter = f[0]; self._syncFilters(); self.renderGrid(); };
      filters.appendChild(b);
    });
    this.filtersEl = filters;
    bar.appendChild(filters);
    var spacer = h('span'); spacer.style.flex = '1'; bar.appendChild(spacer);
    var gen = h('button', 'btn btn-primary btn-sm mpg-create', '+ Create 3D model');
    gen.onclick = function () { self.openComposer(); };
    this.createBtn = gen;
    bar.appendChild(gen);
    var refresh = h('button', 'btn btn-ghost btn-sm', 'Refresh');
    refresh.onclick = function () { self.load(); };
    bar.appendChild(refresh);
    wrap.appendChild(bar);

    // chat bar: pick a model, describe the model, then generate
    this.composer = h('div', 'mpg-composer');
    this.composer.hidden = true;
    var selRow = h('div', 'mpg-selrow');
    var selLabel = h('label', 'mpg-sel-label', 'AI model');
    this.modelSel = h('select', 'level-sel mpg-model-sel');
    this.modelSel.setAttribute('aria-label', 'AI model to generate with');
    this.modelSel.onchange = function () {
      self.modelChoice = self.modelSel.value;
      ModelPreviewGallery._storageSet('omni_models_model', self.modelChoice);
    };
    selLabel.appendChild(this.modelSel);
    selRow.appendChild(selLabel);
    var hint = h('span', 'mpg-sel-hint', 'Uses this model to design the shape, then builds it in 3D.');
    selRow.appendChild(hint);
    this.composer.appendChild(selRow);
    this._fillModels();

    this.composerInput = h('textarea', 'mpg-composer-input');
    this.composerInput.rows = 2;
    this.composerInput.placeholder = 'Describe the 3D model you want… e.g. "a weathered wooden shipping crate with iron corners"';
    this.composerInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); self._submitPrompt(); }
    });
    var send = h('button', 'btn btn-primary mpg-composer-send');
    send.appendChild(h('span', 'mpg-send-label', 'Generate'));
    send.onclick = function () { self._submitPrompt(); };
    var close = h('button', 'mpg-composer-close', '×');
    close.setAttribute('aria-label', 'Close');
    close.onclick = function () { if (!self.busy) self.closeComposer(); };
    this.composerSend = send;
    this.composer.appendChild(this.composerInput);
    this.composer.appendChild(send);
    this.composer.appendChild(close);
    wrap.appendChild(this.composer);

    var cols = h('div', 'mpg-cols');
    this.gridEl = h('div', 'mpg-grid');
    cols.appendChild(this.gridEl);
    var vp = h('div', 'mpg-viewport');
    this.statusEl = h('div', 'mpg-status');
    this.canvasHost = h('div', 'mpg-canvas');
    this.infoEl = h('div', 'mpg-info');
    vp.appendChild(this.canvasHost);
    vp.appendChild(this.statusEl);
    vp.appendChild(this.infoEl);
    cols.appendChild(vp);
    wrap.appendChild(cols);
    this.root.appendChild(wrap);
    this._initThree();
  };

  ModelPreviewGallery.prototype._initThree = function () {
    var THREE = global.THREE;
    if (!THREE) { this.setStatus('Three.js not loaded.', 'error'); return; }
    var host = this.canvasHost;
    var scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0e0f12);
    var camera = new THREE.PerspectiveCamera(50, 1, 0.01, 1000);
    camera.position.set(2.4, 1.8, 3.2);
    var renderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false }); }
    catch (e) { this.setStatus('WebGL is unavailable in this browser.', 'error'); return; }
    renderer.setPixelRatio(Math.min(2, global.devicePixelRatio || 1));
    host.appendChild(renderer.domElement);

    scene.add(new THREE.HemisphereLight(0xffffff, 0x333344, 1.1));
    var key = new THREE.DirectionalLight(0xffffff, 1.6); key.position.set(4, 6, 5); scene.add(key);
    var fill = new THREE.DirectionalLight(0x99bbff, 0.6); fill.position.set(-5, 2, -4); scene.add(fill);
    var grid = new THREE.GridHelper(10, 20, 0x2a2f38, 0x1b1e24); grid.position.y = -0.001; scene.add(grid);

    var controls = THREE.OrbitControls ? new THREE.OrbitControls(camera, renderer.domElement) : null;
    if (controls) { controls.enableDamping = true; controls.dampingFactor = 0.08; }

    this.viewport = { scene: scene, camera: camera, renderer: renderer, controls: controls, grid: grid, group: null };
    this._resize();
    if (global.ResizeObserver) {
      this._ro = new ResizeObserver(function () { self._resize(); });
      this._ro.observe(host);
    } else {
      this._onResize = function () { self._resize(); };
      global.addEventListener('resize', this._onResize);
    }
    var self = this;
    (function loop() {
      if (self._disposed) return;
      self._raf = global.requestAnimationFrame(loop);
      if (controls) controls.update();
      renderer.render(scene, camera);
    })();
  };

  ModelPreviewGallery.prototype._resize = function () {
    var v = this.viewport; if (!v) return;
    var host = this.canvasHost;
    var w = host.clientWidth || 640, hh = host.clientHeight || 360;
    v.camera.aspect = w / hh; v.camera.updateProjectionMatrix();
    v.renderer.setSize(w, hh, false);
  };

  ModelPreviewGallery.prototype._syncFilters = function () {
    if (!this.filtersEl) return;
    var self = this;
    this.filtersEl.querySelectorAll('.mpg-filter').forEach(function (b) {
      b.classList.toggle('active', b.dataset.f === self.filter);
    });
  };

  ModelPreviewGallery.prototype.setStatus = function (msg, kind) {
    if (!this.statusEl) return;
    this.statusEl.textContent = msg || '';
    this.statusEl.className = 'mpg-status' + (kind ? ' ' + kind : '');
    this.statusEl.style.display = msg ? '' : 'none';
  };

  ModelPreviewGallery.prototype._fetchJson = async function (path, opts) {
    if (!this._fetch) throw new Error('no fetch');
    var r = await this._fetch(this.apiBase + path, opts);
    if (!r.ok) { var e = await r.json().catch(function () { return {}; }); throw new Error((e && e.error) || ('HTTP ' + r.status)); }
    return r.json();
  };

  ModelPreviewGallery.prototype.load = async function () {
    this.setStatus('Loading models...');
    try {
      var data = await this._fetchJson('/api/models');
      this.models = (data && data.models) || [];
      this.renderGrid();
      if (!this.models.length) this._showEmpty();
      this.setStatus('');
    } catch (e) {
      this.models = [];
      this.renderGrid();
      this.setStatus('Could not load models: ' + e.message, 'error');
    }
  };

  ModelPreviewGallery.prototype._visible = function () {
    var self = this;
    return this.models.filter(function (m) {
      if (self.filter === 'gltf' && !/^(glb|gltf)$/i.test(m.format || '')) return false;
      if (self.filter === 'obj' && !/^obj$/i.test(m.format || '')) return false;
      if (self.filter === 'stl' && !/^stl$/i.test(m.format || '')) return false;
      if (self.filter === 'high' && !/high|ultra/i.test(m.quality || '')) return false;
      if (self.filter === 'recent') { /* handled by sort below */ }
      if (self.query) {
        var hay = ((m.name || '') + ' ' + (m.prompt || '') + ' ' + (m.format || '')).toLowerCase();
        if (hay.indexOf(self.query) < 0) return false;
      }
      return true;
    });
  };

  ModelPreviewGallery.prototype.renderGrid = function () {
    var self = this;
    this.gridEl.innerHTML = '';
    var list = this._visible();
    if (this.filter === 'recent') {
      list = list.slice().sort(function (a, b) { return (b.createdAt || '').localeCompare(a.createdAt || ''); }).slice(0, 6);
    }
    if (!list.length) { this._showEmpty(); return; }
    list.forEach(function (m) {
      var card = h('div', 'mpg-card' + (self.selected && self.selected.id === m.id ? ' active' : ''));
      var thumb = h('div', 'mpg-thumb');
      if (m.thumbnail) {
        var img = document.createElement('img'); img.src = m.thumbnail; img.alt = m.name || 'model'; thumb.appendChild(img);
      } else {
        thumb.appendChild(h('span', 'mpg-thumb-ico', '3D'));
      }
      card.appendChild(thumb);
      var body = h('div', 'mpg-card-body');
      body.appendChild(h('div', 'mpg-card-name', m.name || 'Untitled model'));
      var meta = h('div', 'mpg-card-meta');
      meta.textContent = [(m.format || '').toUpperCase(), m.polygonCount ? m.polygonCount.toLocaleString() + ' tris' : '',
        m.generationMethod || '', m.quality || ''].filter(Boolean).join(' • ');
      body.appendChild(meta);
      if (m.model || m.prompt) {
        var sub = h('div', 'mpg-card-sub');
        if (m.model) { var chip = h('span', 'mpg-chip', m.model); sub.appendChild(chip); }
        if (m.shape) sub.appendChild(h('span', 'mpg-chip mpg-chip-quiet', m.shape));
        body.appendChild(sub);
      }
      if (m.prompt) { var pr = h('div', 'mpg-card-prompt', m.prompt); body.appendChild(pr); }
      var status = h('div', 'mpg-card-status ' + (m.status || 'ready'), m.status || 'ready');
      body.appendChild(status);
      var actions = h('div', 'mpg-card-actions');
      var open = h('button', 'btn btn-ghost btn-sm', 'Open');
      open.onclick = function (ev) { ev.stopPropagation(); self.select(m.id); };
      actions.appendChild(open);
      var dl = h('button', 'btn btn-ghost btn-sm', 'Download');
      dl.onclick = function (ev) { ev.stopPropagation(); self.download(m); };
      actions.appendChild(dl);
      var mod = h('button', 'btn btn-ghost btn-sm', 'Modify');
      mod.onclick = function (ev) { ev.stopPropagation(); self.onModifyModel(m); };
      actions.appendChild(mod);
      var del = h('button', 'btn btn-ghost btn-sm mpg-del', 'Delete');
      del.onclick = function (ev) { ev.stopPropagation(); self.remove(m); };
      actions.appendChild(del);
      body.appendChild(actions);
      card.appendChild(body);
      card.onclick = function () { self.select(m.id); };
      self.gridEl.appendChild(card);
    });
  };

  // Populate the AI-model dropdown from the app's discovered model list.
  ModelPreviewGallery.prototype._fillModels = function () {
    if (!this.modelSel) return;
    var list = [];
    try { list = (this.getModels ? this.getModels() : []) || []; } catch (e) { list = []; }
    list = list.filter(function (m) { return m && m.id; });
    if (!list.length) list = [{ id: 'omniilink', provider: 'auto' }];
    var chosen = this.modelChoice;
    if (!list.some(function (m) { return m.id === chosen; })) chosen = list[0].id;
    this.modelChoice = chosen;
    this.modelSel.innerHTML = '';
    var self = this;
    list.forEach(function (m) {
      var o = document.createElement('option');
      o.value = m.id;
      o.textContent = m.id + (m.provider && m.provider !== 'auto' ? ' · ' + m.provider : '');
      self.modelSel.appendChild(o);
    });
    this.modelSel.value = chosen;
  };

  ModelPreviewGallery.prototype._showEmpty = function () {
    var self = this;
    this.gridEl.innerHTML = '';
    var empty = h('div', 'mpg-empty');
    empty.appendChild(h('div', 'mpg-empty-title', 'No 3D models yet'));
    empty.appendChild(h('div', 'mpg-empty-sub', 'Generate your first model with Omniilink AI.'));
    var b = h('button', 'btn btn-primary btn-sm', 'Create 3D model');
    b.onclick = function () { self.openComposer(); };
    empty.appendChild(b);
    this.gridEl.appendChild(empty);
  };

  ModelPreviewGallery.prototype.select = function (id) {
    var m = null;
    for (var i = 0; i < this.models.length; i++) if (this.models[i].id === id) m = this.models[i];
    if (!m) return;
    this.selected = m;
    this.renderGrid();
    this._loadIntoViewport(m);
  };

  ModelPreviewGallery.prototype._clearViewport = function () {
    var v = this.viewport; if (!v || !v.group) return;
    v.group.traverse(function (o) {
      if (o.geometry && o.geometry.dispose) o.geometry.dispose();
      if (o.material) {
        var mats = Array.isArray(o.material) ? o.material : [o.material];
        mats.forEach(function (mat) {
          ['map', 'normalMap', 'roughnessMap', 'metalnessMap', 'aoMap', 'displacementMap'].forEach(function (k) {
            if (mat[k] && mat[k].dispose) mat[k].dispose();
          });
          if (mat.dispose) mat.dispose();
        });
      }
    });
    v.scene.remove(v.group);
    v.group = null;
  };

  ModelPreviewGallery.prototype._addAndFrame = function (obj) {
    var THREE = global.THREE, v = this.viewport;
    if (!v) return;
    this._clearViewport();
    v.group = obj;
    v.scene.add(obj);
    var box = new THREE.Box3().setFromObject(obj);
    var size = box.getSize(new THREE.Vector3());
    var center = box.getCenter(new THREE.Vector3());
    obj.position.sub(center);                      // centre at origin
    var radius = Math.max(size.x, size.y, size.z) * 0.5 || 1;
    var dist = radius / Math.tan((v.camera.fov * Math.PI / 180) / 2) * 1.8;
    v.camera.position.set(dist * 0.7, radius * 1.1, dist);
    v.camera.near = Math.max(0.001, radius / 100); v.camera.far = radius * 100;
    v.camera.updateProjectionMatrix();
    if (v.controls) { v.controls.target.set(0, 0, 0); v.controls.update(); }
    var tri = 0, mats = {};
    obj.traverse(function (o) { if (o.geometry && o.geometry.attributes && o.geometry.attributes.position) { tri += (o.geometry.index ? o.geometry.index.count : o.geometry.attributes.position.count) / 3; } if (o.material) mats[o.material.uuid || Math.random()] = 1; });
    this.infoEl.textContent = (this.selected ? this.selected.name : 'Model') + ' — ' + Math.round(tri).toLocaleString() + ' triangles, ' + Object.keys(mats).length + ' material(s)';
  };

  // Human-readable text for anything a loader can throw at us.
  function loadErrorText(e) {
    var m = (e && e.message) ? String(e.message) : String(e);
    if (e && e.name === 'AbortError') return 'the request timed out';
    if (/Failed to fetch/i.test(m) || /Load failed/i.test(m)) {
      return 'the browser blocked the request (network or content-security policy)';
    }
    if (!m || /^\[object (Event|ProgressEvent)\]$/.test(m)) return 'a network error interrupted the download';
    return m;
  }

  // Fetch a model asset ourselves so we can report the exact HTTP status.
  ModelPreviewGallery.prototype._fetchAsset = async function (url) {
    if (!this._fetch) throw new Error('no fetch');
    var r = await this._fetch(url, { credentials: 'same-origin' });
    if (!r.ok) {
      var known = {
        401: 'your session expired — sign in again',
        403: 'you do not have access to this model',
        404: 'the file is missing on the server',
        413: 'the file is too large for the server',
        500: 'the server hit an error reading the file'
      };
      var detail = known[r.status] || ('HTTP ' + r.status);
      try {
        var j = await r.clone().json();
        if (j && j.error) detail = 'HTTP ' + r.status + ' — ' + j.error;
      } catch (e) {}
      throw new Error(detail);
    }
    return await r.arrayBuffer();
  };

  ModelPreviewGallery.prototype._loadIntoViewport = function (m) {
    var THREE = global.THREE;
    if (!THREE) { this.setStatus('Three.js not loaded.', 'error'); return; }
    var self = this;
    var url = this.apiBase + '/api/models/' + encodeURIComponent(m.id) + '/asset';
    var f = (m.format || ext(m.path) || '').toLowerCase();
    var token = ++this._loadToken;
    this.setStatus('Loading ' + (m.name || 'model') + '…');
    function stale() { return token !== self._loadToken; }
    function ok(obj) { if (stale()) return; self._addAndFrame(obj); self.setStatus(''); }
    function bad(e) { if (stale()) return; self.setStatus('Could not load model: ' + loadErrorText(e), 'error'); }
    this._fetchAsset(url).then(function (buf) {
      if (stale()) return;
      if (f === 'glb' || f === 'gltf') {
        new THREE.GLTFLoader().parse(buf, '', function (g) {
          ok(g.scene || (g.scenes && g.scenes[0]));
        }, bad);
      } else if (f === 'obj') {
        ok(new THREE.OBJLoader().parse(new TextDecoder().decode(buf)));
      } else if (f === 'stl') {
        var geo = new THREE.STLLoader().parse(buf);
        geo.computeVertexNormals();
        ok(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0x9aa2ad, metalness: 0.2, roughness: 0.7 })));
      } else {
        bad(new Error('unsupported format "' + (f || 'unknown') + '"'));
      }
    }).catch(bad);
  };

  ModelPreviewGallery.prototype.openComposer = function () {
    if (!this.composer) return;
    this._fillModels();
    this.composer.hidden = false;
    this.composerInput.value = '';
    this.composerInput.focus();
    this.composer.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };

  ModelPreviewGallery.prototype.closeComposer = function () {
    if (!this.composer || this.busy) return;
    this.composer.hidden = true;
  };

  // Spinner + disabled controls while a generation is in flight.
  ModelPreviewGallery.prototype._setBusy = function (busy, label) {
    this.busy = !!busy;
    if (this.composer) this.composer.classList.toggle('busy', !!busy);
    if (this.composerInput) this.composerInput.disabled = !!busy;
    if (this.composerSend) {
      this.composerSend.disabled = !!busy;
      var lab = this.composerSend.querySelector('.mpg-send-label');
      if (lab) lab.textContent = busy ? (label || 'Generating…') : 'Generate';
      var sp = this.composerSend.querySelector('.mpg-spin');
      if (busy && !sp) this.composerSend.insertBefore(h('span', 'mpg-spin'), this.composerSend.firstChild);
      if (!busy && sp) sp.remove();
    }
    if (this.createBtn) {
      this.createBtn.disabled = !!busy;
      this.createBtn.textContent = busy ? (label || 'Generating…') : '+ Create 3D model';
    }
  };

  ModelPreviewGallery.prototype._stage = function (msg) {
    this.setStatus(msg || '');
  };

  // Ask the selected AI model for a compact design spec (JSON).
  ModelPreviewGallery.prototype._askForSpec = async function (prompt, model) {
    if (!this._fetch) throw new Error('no fetch');
    var sys = 'You are an expert 3D model designer. Before answering, think carefully about the object the ' +
      'user describes: its real-world size, its silhouette, and which simple primitive best captures its ' +
      'overall form and proportions. A gun is long and thin, a ball is round, a crate is a box — match the ' +
      'proportions to the real object and never turn an elongated object into a cube. ' +
      'Reply with ONLY one compact JSON object, no markdown, no prose: ' +
      '{"kind":"...","name":"short name, max 6 words",' +
      '"size":[width,height,depth] in meters, each between 0.2 and 4,' +
      '"color":"#rrggbb","roughness":0.0-1.0,"metalness":0.0-1.0,"detail":"low|medium|high"}. ' +
      '"kind" MUST be exactly one of: crate, barrel, sphere, cylinder, cone, slab, ring, column. ' +
      'Choose the kind whose shape best matches the object\'s overall form. Set "size" to the object\'s real ' +
      'proportions (length, height, depth) so the result is recognizable. Use "detail":"high" for anything ' +
      'with curves or intricate parts. For complex objects (vehicles, weapons, tools, furniture), also add a ' +
      '"parts" array describing the object as 2-5 simple parts positioned together, where each part is ' +
      '{"kind":"...","size":[w,h,d],"pos":[x,y,z],"rot":[x,y,z],"color":"#rrggbb"}. Use DEGREES for rot ' +
      '(e.g. 90 means a quarter turn). For example a table is a slab top plus 4 column legs; a rifle is a long ' +
      'cylinder barrel plus a box body plus a box stock. Keep every part to the allowed kinds. Simple objects ' +
      '(a ball, a crate) need no parts. The object must be built from boxes, cylinders, spheres, cones and tori.';
    var body = {
      model: model || 'omniilink',
      messages: [{ role: 'system', content: sys }, { role: 'user', content: prompt }],
      stream: false, temperature: 0.3, max_tokens: 18000,
      effort: 'high', thinking: 'high'
    };
    var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { try { ctrl.abort(); } catch (e) {} }, 45000) : null;
    try {
      var r = await this._fetch(this.apiBase + '/v1/chat/completions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: ctrl ? ctrl.signal : undefined
      });
      if (!r.ok) { var er = await r.json().catch(function () { return {}; }); throw new Error((er && er.error) || ('HTTP ' + r.status)); }
      var data = await r.json();
      var text = data && data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content;
      if (!text) throw new Error('the model returned no design');
      var spec = ModelPreviewGallery.parseSpec(text, prompt);
      if (!spec) throw new Error('the model returned an unreadable design');
      return spec;
    } finally { if (timer) clearTimeout(timer); }
  };

  // Send the chat-bar prompt: ask the chosen model, build geometry, save.
  ModelPreviewGallery.prototype._submitPrompt = async function () {
    if (this.busy) return;
    var prompt = (this.composerInput.value || '').trim();
    if (!prompt) {
      this.composerInput.focus();
      this._stage('Describe what you want to create.');
      if (this.statusEl) this.statusEl.classList.add('error');
      return;
    }
    var model = this.modelChoice || 'omniilink';
    this._setBusy(true, 'Generating…');
    if (this.statusEl) this.statusEl.classList.remove('error');
    var spec = null;
    try {
      this._stage('Asking ' + model + ' for a design…');
      try {
        spec = await this._askForSpec(prompt, model);
      } catch (e) {
        spec = null;
        this._stage('Design from ' + model + ' unavailable — using the built-in generator…');
        if (global.showToast) {
          global.showToast('The ' + model + ' design step failed (' + (e && e.message ? e.message : e)
            + ') — built the model with the built-in generator instead', 'info');
        }
      }
      this._stage('Building geometry…');
      await this.generate(null, prompt, 'MEDIUM', spec, model);
      this.composerInput.value = '';
      this.closeComposer();
      this._stage('');
    } catch (e) {
      this._stage('Generation failed: ' + (e && e.message ? e.message : e));
      if (this.statusEl) this.statusEl.classList.add('error');
    } finally {
      this._setBusy(false);
      if (!this.composer.hidden) this.composerInput.focus();
    }
  };

  // Delete a model from the workspace (with confirmation).
  ModelPreviewGallery.prototype.remove = async function (m) {
    if (!m || !m.id) return false;
    var label = m.name || m.id;
    var ask = global.appConfirm ? await global.appConfirm('Delete "' + label + '" from your workspace? This cannot be undone.')
      : (global.confirm ? global.confirm('Delete "' + label + '" from your workspace?') : true);
    if (!ask) return false;
    this.setStatus('Deleting ' + label + '...');
    try {
      var r = await this._fetch(this.apiBase + '/api/models/' + encodeURIComponent(m.id), { method: 'DELETE' });
      if (!r.ok) { var e = await r.json().catch(function () { return {}; }); throw new Error((e && e.error) || ('HTTP ' + r.status)); }
      if (this.selected && this.selected.id === m.id) {
        this.selected = null;
        this._clearViewport();
        if (this.infoEl) this.infoEl.textContent = '';
      }
      await this.load();
      this.setStatus('');
      return true;
    } catch (e) {
      this.setStatus('Delete failed: ' + (e && e.message ? e.message : e), 'error');
      return false;
    }
  };

  ModelPreviewGallery.prototype.download = function (m) {
    var url = this.apiBase + '/api/models/' + encodeURIComponent(m.id) + '/asset';
    var a = document.createElement('a');
    a.href = url; a.download = (m.name || 'model') + '.' + (m.format || 'glb');
    document.body.appendChild(a); a.click(); a.remove();
  };

  // Build a model client-side (from an AI spec when we have one) and save it.
  ModelPreviewGallery.prototype.generate = async function (kind, promptText, quality, spec, model) {
    var THREE = global.THREE; if (!THREE) throw new Error('Three.js not loaded');
    var prompt = String(promptText || '').trim();
    var group, shape;
    if (spec) {
      group = Generators.fromSpec(spec);
      shape = spec.kind;
    } else {
      if (!kind) kind = pickKind(prompt);
      group = (Generators[kind] || Generators.crate)();
      shape = kind;
    }
    var suggested = (group.userData && group.userData.suggestedName) || ('Procedural ' + (shape || 'model'));
    var name = nameFromPrompt(prompt) || spec && spec.name || suggested;
    var method = spec ? 'ai-spec-three' : 'procedural-keyword';
    return await this._exportAndRegister(group, name, prompt || suggested, quality || 'MEDIUM',
      model || this.modelChoice, shape, method);
  };

  ModelPreviewGallery.prototype._exportAndRegister = function (group, name, promptText, quality, model, shape, method) {
    var THREE = global.THREE, self = this;
    return new Promise(function (resolve, reject) {
      var exporter = new THREE.GLTFExporter();
      exporter.parse(group, async function (result) {
        try {
          var bin = result; // r128 parse() without options returns ArrayBuffer (GLB)
          var b64 = self._ab2b64(bin);
          var dims = new THREE.Box3().setFromObject(group).getSize(new THREE.Vector3());
          var tri = 0, mats = {};
          group.traverse(function (o) { if (o.geometry) { var g = o.geometry; tri += (g.index ? g.index.count : (g.attributes && g.attributes.position ? g.attributes.position.count : 0)) / 3; } if (o.material) mats[o.material.uuid || Math.random()] = 1; });
          var body = {
            name: name, prompt: promptText, format: 'glb',
            generationMethod: method || 'procedural-three',
            quality: quality, polygonCount: Math.round(tri), materialCount: Object.keys(mats).length,
            dimensions: { x: +dims.x.toFixed(3), y: +dims.y.toFixed(3), z: +dims.z.toFixed(3) },
            model: model || '', shape: shape || '',
            asset_b64: b64
          };
          self._stage('Saving…');
          var saved = await self._fetchJson('/api/models', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
          });
          await self.load();
          if (saved && saved.model) self.select(saved.model.id);
          if (self.onModelCreated && saved && saved.model) self.onModelCreated(saved.model);
          resolve(saved);
        } catch (e) { reject(e); }
      }, { binary: true });
    });
  };

  ModelPreviewGallery.prototype._ab2b64 = function (buf) {
    var bytes = new Uint8Array(buf), bin = '';
    var chunk = 0x8000;
    for (var i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  };

  ModelPreviewGallery.prototype.dispose = function () {
    this._disposed = true;
    if (this._raf) global.cancelAnimationFrame(this._raf);
    if (this._ro && this._ro.disconnect) this._ro.disconnect();
    if (this._onResize) global.removeEventListener('resize', this._onResize);
    this._clearViewport();
    var v = this.viewport;
    if (v) {
      if (v.controls && v.controls.dispose) v.controls.dispose();
      if (v.renderer) { try { v.renderer.dispose(); } catch (e) {} if (v.renderer.domElement && v.renderer.domElement.parentNode) v.renderer.domElement.parentNode.removeChild(v.renderer.domElement); }
      if (v.grid && v.grid.geometry) v.grid.geometry.dispose();
      v.scene = null; v.camera = null; v.renderer = null; v.controls = null;
    }
    this.viewport = null;
    if (this.root) this.root.innerHTML = '';
  };

  ModelPreviewGallery.Procedural = Procedural;
  ModelPreviewGallery.Generators = Generators;
  ModelPreviewGallery.pickKind = pickKind;
  ModelPreviewGallery.nameFromPrompt = nameFromPrompt;
  ModelPreviewGallery.parseSpec = SpecParser.parse;
  ModelPreviewGallery.normalizeSpec = SpecParser.normalize;
  ModelPreviewGallery.coerceKind = coerceKind;
  ModelPreviewGallery.inferKind = inferKind;
  ModelPreviewGallery.SPEC_KINDS = SPEC_KINDS;
  ModelPreviewGallery.loadErrorText = loadErrorText;

  global.ModelPreviewGallery = ModelPreviewGallery;
  if (typeof module !== 'undefined' && module.exports) module.exports = ModelPreviewGallery;
})(typeof window !== 'undefined' ? window : this);
