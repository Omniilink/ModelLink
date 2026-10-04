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
      var body = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), mat);
      g.add(body);
      var frameMat = new THREE.MeshStandardMaterial({ color: 0x3a2412, roughness: 0.8 });
      var s = 1.06, t = 0.07;
      [[0, s / 2, 0], [0, -s / 2, 0], [s / 2, 0, 0], [-s / 2, 0, 0]].forEach(function (p) {
        var bar = new THREE.Mesh(new THREE.BoxGeometry(p[1] ? t : s, p[0] || s, p[2] || s), frameMat);
        bar.position.set(p[0], p[1], p[2]);
        if (!p[1] && p[0]) bar.geometry = new THREE.BoxGeometry(t, s, s);
        if (!p[2] && p[0]) bar.geometry = new THREE.BoxGeometry(t, s, s);
        g.add(bar);
      });
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
    this.onGenerate = typeof opts.onGenerate === 'function' ? opts.onGenerate : null;
    this.models = [];
    this.selected = null;
    this.filter = 'all';
    this.query = '';
    this.viewport = null;
    this._disposed = false;
    this.build();
  }

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
    if (this.onGenerate) {
      var gen = h('button', 'btn btn-primary btn-sm', '+ Create 3D model');
      gen.onclick = function () { self.openComposer(); };
      bar.appendChild(gen);
    }
    var refresh = h('button', 'btn btn-ghost btn-sm', 'Refresh');
    refresh.onclick = function () { self.load(); };
    bar.appendChild(refresh);
    wrap.appendChild(bar);

    // chat bar: describe the model, then generate
    this.composer = h('div', 'mpg-composer');
    this.composer.hidden = true;
    this.composerInput = h('textarea', 'mpg-composer-input');
    this.composerInput.rows = 2;
    this.composerInput.placeholder = 'Describe the 3D model you want… e.g. "a weathered wooden shipping crate with iron corners"';
    this.composerInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); self._submitPrompt(); }
    });
    var send = h('button', 'btn btn-primary mpg-composer-send', 'Generate');
    send.onclick = function () { self._submitPrompt(); };
    var close = h('button', 'mpg-composer-close', '×');
    close.setAttribute('aria-label', 'Close');
    close.onclick = function () { self.closeComposer(); };
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

  ModelPreviewGallery.prototype._showEmpty = function () {
    var self = this;
    this.gridEl.innerHTML = '';
    var empty = h('div', 'mpg-empty');
    empty.appendChild(h('div', 'mpg-empty-title', 'No 3D models yet'));
    empty.appendChild(h('div', 'mpg-empty-sub', 'Generate your first model with Omniilink AI.'));
    if (this.onGenerate) {
      var b = h('button', 'btn btn-primary btn-sm', 'Create 3D model');
      b.onclick = function () { self.openComposer(); };
      empty.appendChild(b);
    }
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

  ModelPreviewGallery.prototype._loadIntoViewport = function (m) {
    var THREE = global.THREE; if (!THREE) { this.setStatus('Three.js not loaded.', 'error'); return; }
    var self = this;
    var url = this.apiBase + '/api/models/' + encodeURIComponent(m.id) + '/asset';
    var f = (m.format || ext(m.path) || '').toLowerCase();
    this.setStatus('Loading ' + (m.name || 'model') + '...');
    function done() { self.setStatus(''); }
    function fail(e) { self.setStatus('Could not load model: ' + (e && e.message ? e.message : e), 'error'); }
    try {
      if (f === 'glb' || f === 'gltf') {
        new THREE.GLTFLoader().load(url, function (g) { self._addAndFrame(g.scene || g.scenes[0]); done(); }, undefined, fail);
      } else if (f === 'obj') {
        new THREE.OBJLoader().load(url, function (o) { self._addAndFrame(o); done(); }, undefined, fail);
      } else if (f === 'stl') {
        new THREE.STLLoader().load(url, function (geo) {
          geo.computeVertexNormals();
          self._addAndFrame(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0x9aa2ad, metalness: 0.2, roughness: 0.7 })));
          done();
        }, undefined, fail);
      } else {
        fail(new Error('Unsupported format: ' + f));
      }
    } catch (e) { fail(e); }
  };

  ModelPreviewGallery.prototype.openComposer = function () {
    if (!this.composer) return;
    this.composer.hidden = false;
    this.composerInput.value = '';
    this.composerInput.focus();
    this.composer.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };

  ModelPreviewGallery.prototype.closeComposer = function () {
    if (!this.composer) return;
    this.composer.hidden = true;
  };

  // Send the chat-bar prompt to the app's AI handler (or generate locally).
  ModelPreviewGallery.prototype._submitPrompt = async function () {
    var prompt = (this.composerInput.value || '').trim();
    if (!prompt) { this.composerInput.focus(); this.setStatus('Describe what you want to create.', 'error'); return; }
    this.composerInput.disabled = true;
    this.composerSend.disabled = true;
    this.setStatus('Creating "' + prompt.slice(0, 80) + '"...');
    try {
      if (this.onGenerate) { await this.onGenerate(prompt); }
      else { await this.generate(null, prompt, 'MEDIUM'); }
      this.composerInput.value = '';
      this.closeComposer();
      this.setStatus('');
    } catch (e) {
      this.setStatus('Generation failed: ' + (e && e.message ? e.message : e), 'error');
    } finally {
      this.composerInput.disabled = false;
      this.composerSend.disabled = false;
      this.composerInput.focus();
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

  // Build a procedural model client-side and register it to the workspace.
  ModelPreviewGallery.prototype.generate = async function (kind, promptText, quality) {
    var THREE = global.THREE; if (!THREE) throw new Error('Three.js not loaded');
    var prompt = String(promptText || '').trim();
    if (!kind) kind = pickKind(prompt);
    var make = Generators[kind] || Generators.crate;
    var group = make();
    var suggested = (group.userData && group.userData.suggestedName) || ('Procedural ' + kind);
    var name = nameFromPrompt(prompt) || suggested;
    return await this._exportAndRegister(group, name, prompt || suggested, quality || 'MEDIUM');
  };

  ModelPreviewGallery.prototype._exportAndRegister = function (group, name, promptText, quality) {
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
            name: name, prompt: promptText, format: 'glb', generationMethod: 'procedural-three',
            quality: quality, polygonCount: Math.round(tri), materialCount: Object.keys(mats).length,
            dimensions: { x: +dims.x.toFixed(3), y: +dims.y.toFixed(3), z: +dims.z.toFixed(3) },
            asset_b64: b64
          };
          var saved = await self._fetchJson('/api/models', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
          });
          await self.load();
          if (saved && saved.model) self.select(saved.model.id);
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

  global.ModelPreviewGallery = ModelPreviewGallery;
  if (typeof module !== 'undefined' && module.exports) module.exports = ModelPreviewGallery;
})(typeof window !== 'undefined' ? window : this);
