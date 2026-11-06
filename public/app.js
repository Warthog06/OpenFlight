/* OpenFlight front end: MapLibre (OpenStreetMap tiles) + deck.gl for aircraft in 2D and 3D. */
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const FT_TO_M = 0.3048;
  const MAX_RADIUS_NM = 250;
  const TRAIL_MAX = 600;

  const state = {
    config: null,
    mode: 'live',            // 'live' | 'replay'
    playing: true,
    speed: 1,
    anchorSim: 0,            // replay clock: sim time at anchorReal
    anchorReal: 0,
    serverOffset: 0,         // server clock minus browser clock
    aircraft: new Map(),     // hex -> entry
    selected: null,
    follow: false,
    selTrail: null,          // { hex, points } from /api/trail
    is3D: false,
    exag: 4,
    showTrails: true,
    showLabels: true,
    showGround: true,
    gen: 0,                  // bumped on every time jump so stale responses are ignored
    inflight: false,
    source: '',
    queryCircle: null,
    noDataWarned: false,
  };

  // ------------------------------------------------------------------ clock

  const realNow = () => Date.now() / 1000;
  const liveNow = () => realNow() + state.serverOffset;
  function simNow() {
    if (state.mode === 'live') return liveNow();
    return state.playing ? state.anchorSim + (realNow() - state.anchorReal) * state.speed : state.anchorSim;
  }
  function setClock(t) { state.anchorSim = t; state.anchorReal = realNow(); }

  // ------------------------------------------------------------------ colours

  const ALT_STOPS = [
    [0, [255, 77, 46]], [2000, [255, 154, 31]], [6000, [255, 214, 10]], [10000, [87, 209, 63]],
    [20000, [25, 195, 195]], [30000, [47, 123, 255]], [40000, [193, 60, 255]],
  ];
  function altColor(ft, gnd, alpha = 255) {
    if (gnd) return [150, 150, 150, alpha];
    if (ft == null) return [200, 200, 200, alpha];
    if (ft <= ALT_STOPS[0][0]) return [...ALT_STOPS[0][1], alpha];
    for (let i = 1; i < ALT_STOPS.length; i++) {
      const [a1, c1] = ALT_STOPS[i - 1];
      const [a2, c2] = ALT_STOPS[i];
      if (ft <= a2) {
        const f = (ft - a1) / (a2 - a1);
        return [c1[0] + (c2[0] - c1[0]) * f, c1[1] + (c2[1] - c1[1]) * f, c1[2] + (c2[2] - c1[2]) * f, alpha];
      }
    }
    return [...ALT_STOPS[ALT_STOPS.length - 1][1], alpha];
  }

  // ------------------------------------------------------------------ icons

  const svgUrl = (body) => 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">${body}</svg>`);
  const PLANE_PATH = 'M32 3c2.5 0 4 3 4 6v15l24 14v6l-24-7v13l7 5v5l-11-3-11 3v-5l7-5V37L4 44v-6l24-14V9c0-3 1.5-6 4-6z';
  const ICONS = {
    plane: { id: 'plane', url: svgUrl(`<path fill="#fff" d="${PLANE_PATH}"/>`), width: 64, height: 64, mask: true },
    heli: {
      id: 'heli', width: 64, height: 64, mask: true,
      url: svgUrl('<g fill="#fff"><ellipse cx="32" cy="30" rx="9" ry="13"/><rect x="30" y="40" width="4" height="20" rx="2"/>' +
        '<rect x="24" y="56" width="16" height="4" rx="2"/><rect x="4" y="27" width="56" height="4" rx="2" transform="rotate(35 32 29)"/>' +
        '<rect x="4" y="27" width="56" height="4" rx="2" transform="rotate(-35 32 29)"/></g>'),
    },
    ground: { id: 'ground', url: svgUrl('<rect x="20" y="14" width="24" height="36" rx="6" fill="#fff"/>'), width: 64, height: 64, mask: true },
  };
  function iconFor(d) {
    if (d.cat === 'A7') return ICONS.heli;
    if (d.cat === 'C1' || d.cat === 'C2' || d.cat === 'C3') return ICONS.ground;
    return ICONS.plane;
  }
  function sizeFor(d) {
    switch (d.cat) {
      case 'A1': case 'B1': case 'B4': return 20;
      case 'A2': return 23;
      case 'A5': case 'A4': return 31;
      case 'A7': return 24;
      case 'C1': case 'C2': case 'C3': return 12;
      default: return 27;
    }
  }

  // ------------------------------------------------------------------ formatting

  const pad = (n) => String(n).padStart(2, '0');
  const fmtAlt = (d) => d.gnd ? 'Ground' : d.alt == null ? '—' : `${Math.round(d.alt).toLocaleString()} ft`;
  const fmtFL = (d) => d.gnd ? 'GND' : d.alt == null ? '' : d.alt >= 18000 ? `FL${Math.round(d.alt / 100)}` : `${Math.round(d.alt / 100) * 100} ft`;
  function fmtLocal(t, withDate) {
    const d = new Date(t * 1000);
    const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    return withDate ? `${d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })} ${time}` : time;
  }
  function fmtUTC(t) {
    const d = new Date(t * 1000);
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`;
  }
  function toInputValue(t) {
    const d = new Date(t * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ------------------------------------------------------------------ storage (best effort)

  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem('openflight.' + k)); } catch { return null; } },
    set(k, v) { try { localStorage.setItem('openflight.' + k, JSON.stringify(v)); } catch { /* ignore */ } },
  };

  // ------------------------------------------------------------------ API

  async function getJSON(url) {
    const res = await fetch(url, { cache: 'no-store' });
    const text = await res.text();
    let j;
    try {
      j = JSON.parse(text);
    } catch {
      throw new Error(res.status === 404
        ? `${new URL(url, location.href).pathname} was not found (HTTP 404). The page is being served without the OpenFlight API: ` +
          'run "python3 openflight.py", or deploy with the api/ folder (see README)'
        : `the server sent a non-JSON reply (HTTP ${res.status})`);
    }
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    return j;
  }

  // ------------------------------------------------------------------ toast

  let toastTimer = null;
  function toast(msg, kind = 'error', ms = 0) {
    const el = $('toast');
    el.textContent = msg;
    el.className = 'toast' + (kind === 'info' ? ' info' : '');
    el.hidden = false;
    clearTimeout(toastTimer);
    if (ms) toastTimer = setTimeout(() => { el.hidden = true; }, ms);
  }
  const hideToast = () => { $('toast').hidden = true; };

  // ------------------------------------------------------------------ motion

  // Dead-reckon an aircraft from its last fix to time t, using speed, track and climb rate.
  function project(e, t) {
    const d = e.d;
    let lat = d.lat, lon = d.lon, alt = d.alt;
    let dt = Math.max(0, Math.min(t - e.fixT, 30));
    if (d.gnd && !(d.gs > 3)) dt = 0;
    if (dt > 0 && d.gs != null && d.trk != null) {
      const distNm = d.gs * dt / 3600;
      const trk = d.trk * Math.PI / 180;
      lat += distNm * Math.cos(trk) / 60;
      lon += distNm * Math.sin(trk) / (60 * Math.max(0.05, Math.cos(lat * Math.PI / 180)));
      if (alt != null && d.vr) alt = Math.max(0, alt + d.vr * dt / 60);
    }
    return { lat, lon, alt };
  }
  // Same, plus a short blend so a new fix never makes the icon jump.
  function displayPos(e, t, rn) {
    const p = project(e, t);
    if (e.corr) {
      const k = 1 - (rn - e.corr.t0) / 1.2;
      if (k > 0) {
        p.lat += e.corr.lat * k; p.lon += e.corr.lon * k;
        if (p.alt != null) p.alt += e.corr.alt * k;
      } else e.corr = null;
    }
    return p;
  }

  function ingest(list, t) {
    const now = simNow();
    const rn = realNow();
    const seen = new Set();
    for (const a of list) {
      seen.add(a.hex);
      let e = state.aircraft.get(a.hex);
      const fixT = t != null ? t : a.ts;
      if (!e) {
        e = { hex: a.hex, d: a, fixT, trail: [], corr: null, lastSeen: now };
        state.aircraft.set(a.hex, e);
      } else {
        const before = displayPos(e, now, rn);
        e.d = a; e.fixT = fixT;
        const after = project(e, now);
        const dLat = before.lat - after.lat, dLon = before.lon - after.lon;
        e.corr = Math.abs(dLat) < 0.05 && Math.abs(dLon) < 0.05
          ? { lat: dLat, lon: dLon, alt: (before.alt ?? 0) - (after.alt ?? 0), t0: rn } : null;
      }
      e.lastSeen = now;
      const last = e.trail[e.trail.length - 1];
      if (!last || Math.abs(last[0] - a.lon) + Math.abs(last[1] - a.lat) > 0.002 || Math.abs((last[2] ?? 0) - (a.alt ?? 0)) > 150) {
        e.trail.push([a.lon, a.lat, a.gnd ? 0 : a.alt, !!a.gnd]);
        if (e.trail.length > TRAIL_MAX) e.trail.splice(0, e.trail.length - TRAIL_MAX);
      }
    }
    const grace = state.mode === 'live' ? 20 : Math.max(20, state.speed * 3);
    for (const [hex, e] of state.aircraft) {
      if (!seen.has(hex) && Math.abs(now - e.lastSeen) > grace && hex !== state.selected) state.aircraft.delete(hex);
      else if (!seen.has(hex) && Math.abs(now - e.lastSeen) > grace * 3) state.aircraft.delete(hex);
    }
  }

  function clearTraffic() {
    state.gen++;
    state.aircraft.clear();
    state.selTrail = null;
    state.noDataWarned = false;
    if (state.selected) loadSelectedTrail();
  }

  // ------------------------------------------------------------------ map

  const map = new maplibregl.Map({
    container: 'map',
    style: { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#0f1720' } }] },
    center: [-0.2, 51.5],
    zoom: 7,
    maxPitch: 85,
    hash: 'map',
    attributionControl: false,
    dragRotate: true,
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'nautical' }), 'bottom-left');
  map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

  if (!location.hash.includes('map=')) {
    const v = store.get('view');
    if (v && Array.isArray(v.center)) map.jumpTo(v);
  }
  map.on('moveend', () => {
    const c = map.getCenter();
    store.set('view', { center: [c.lng, c.lat], zoom: map.getZoom(), pitch: map.getPitch(), bearing: map.getBearing() });
    scheduleTick();
  });

  function addBasemap(cfg) {
    map.addSource('osm', { type: 'raster', tiles: [cfg.tiles], tileSize: 256, maxzoom: 19, attribution: cfg.tileAttribution });
    map.addLayer({ id: 'osm', type: 'raster', source: 'osm', paint: { 'raster-saturation': -0.35, 'raster-contrast': -0.05 } });
  }

  const overlay = new deck.MapboxOverlay({
    interleaved: false,
    layers: [],
    getCursor: ({ isHovering, isDragging }) => (isDragging ? 'grabbing' : isHovering ? 'pointer' : 'grab'),
    getTooltip: ({ object, layer }) => {
      if (!object || !layer || layer.id !== 'aircraft') return null;
      const d = object.e.d;
      return {
        html: `<b>${esc(d.cs || d.reg || d.hex.toUpperCase())}</b> ${esc(d.type || '')}<br>${esc(fmtAlt({ ...d, alt: object.alt }))}` +
          `${d.gs != null ? ` · ${Math.round(d.gs)} kt` : ''}`,
        style: { background: 'rgba(17,25,35,.94)', color: '#e8eef5', borderRadius: '8px', padding: '6px 8px', fontSize: '12px' },
      };
    },
  });
  map.addControl(overlay);

  map.on('click', (ev) => {
    const info = overlay.pickObject({ x: ev.point.x, y: ev.point.y, radius: 8, layerIds: ['aircraft'] });
    if (info && info.object) select(info.object.e.hex);
    else select(null);
  });
  map.on('dragstart', () => setFollow(false));

  // Which area to ask the server about: the visible map, capped at 250 nm.
  function viewQuery() {
    const c = map.getCenter();
    const b = map.getBounds();
    const corners = [b.getNorthWest(), b.getNorthEast(), b.getSouthWest(), b.getSouthEast()];
    let r = 0;
    for (const p of corners) r = Math.max(r, haversineNm(c.lat, c.lng, p.lat, p.lng));
    const radius = Math.max(5, Math.min(MAX_RADIUS_NM, r * 1.05));
    state.queryCircle = r > MAX_RADIUS_NM ? { lat: c.lat, lon: c.lng, r: MAX_RADIUS_NM } : null;
    return { lat: c.lat.toFixed(4), lon: c.lng.toFixed(4), radius: Math.ceil(radius) };
  }
  function haversineNm(lat1, lon1, lat2, lon2) {
    const r = Math.PI / 180;
    const a = Math.sin((lat2 - lat1) * r / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin((lon2 - lon1) * r / 2) ** 2;
    return 2 * 3440.065 * Math.asin(Math.min(1, Math.sqrt(a)));
  }
  function circlePath(c) {
    const pts = [];
    for (let i = 0; i <= 72; i++) {
      const b = (i / 72) * 2 * Math.PI;
      const lat = c.lat + (c.r / 60) * Math.cos(b);
      const lon = c.lon + (c.r / 60) * Math.sin(b) / Math.cos(c.lat * Math.PI / 180);
      pts.push([lon, lat]);
    }
    return pts;
  }

  // ------------------------------------------------------------------ data loop (once per second)

  let tickTimer = null;
  function scheduleTick() { clearTimeout(tickTimer); tickTimer = setTimeout(tick, 150); }

  async function tick() {
    if (state.inflight || !state.config) return;
    const gen = state.gen;
    const q = viewQuery();
    const params = new URLSearchParams(q);
    let path = '/api/live';
    let t = null;
    if (state.mode === 'replay') {
      t = simNow();
      params.set('t', t.toFixed(1));
      path = '/api/history';
    }
    state.inflight = true;
    try {
      const j = await getJSON(`${path}?${params}`);
      if (gen !== state.gen) return;
      if (state.mode === 'live') {
        state.serverOffset = j.now - realNow();
        ingest(j.aircraft, null);
        hideToast();
      } else {
        ingest(j.aircraft, j.t);
        if (j.source === 'none' && !state.aircraft.size && !state.noDataWarned) {
          state.noDataWarned = true;
          toast(state.config.serverless
            ? 'No flight history for this time. The hosted version can only replay the 1st day of each month; ' +
              'run openflight.py on your own computer to record and replay any time.'
            : 'No recorded flights here at this time. OpenFlight can replay times it was running and ' +
              'recording (see --record in the README), plus the 1st day of each month worldwide.', 'info', 9000);
        } else if (j.source !== 'none') {
          state.noDataWarned = false;
        }
      }
      state.source = j.source;
    } catch (err) {
      if (gen === state.gen) {
        toast(state.mode === 'live'
          ? `Live data unavailable (${err.message}). Retrying every second…`
          : `Could not load history: ${err.message}`);
      }
    } finally {
      state.inflight = false;
    }
  }

  // ------------------------------------------------------------------ rendering

  let lastFrame = 0;
  let lastPanel = 0;
  function frame(ts) {
    requestAnimationFrame(frame);
    if (ts - lastFrame < 40) return; // ~25 fps is plenty for aircraft
    lastFrame = ts;
    render();
    if (ts - lastPanel > 250) { lastPanel = ts; updatePanel(); updateClock(); }
  }

  function zOf(altFt, gnd) {
    if (!state.is3D || gnd || altFt == null) return 0;
    return Math.max(0, altFt) * FT_TO_M * state.exag;
  }

  function render() {
    const t = simNow();
    const rn = realNow();
    const visible = [];
    for (const e of state.aircraft.values()) {
      if (!state.showGround && e.d.gnd) continue;
      const p = displayPos(e, t, rn);
      visible.push({ e, lon: p.lon, lat: p.lat, alt: p.alt, z: zOf(p.alt, e.d.gnd) });
    }
    $('count').textContent = visible.length;

    const sel = state.selected ? visible.find((v) => v.e.hex === state.selected) : null;
    if (sel && state.follow) map.jumpTo({ center: [sel.lon, sel.lat] });

    const layers = [];

    if (state.queryCircle) {
      layers.push(new deck.PathLayer({
        id: 'coverage', data: [circlePath(state.queryCircle)], getPath: (d) => d,
        getColor: [245, 179, 1, 140], getWidth: 1.5, widthUnits: 'pixels', getDashArray: [6, 4],
      }));
    }

    if (state.showTrails || sel) {
      const trails = [];
      for (const v of visible) {
        const isSel = v.e.hex === state.selected;
        if (!state.showTrails && !isSel) continue;
        let pts = v.e.trail;
        if (isSel && state.selTrail && state.selTrail.hex === v.e.hex && state.selTrail.points.length) {
          const older = state.selTrail.points;
          pts = older.concat(pts);
        }
        if (!pts.length) continue;
        const path = pts.map((p) => [p[0], p[1], zOf(p[2], p[3])]);
        path.push([v.lon, v.lat, v.z]);
        const colors = pts.map((p) => altColor(p[2], p[3], isSel ? 255 : 150));
        colors.push(altColor(v.alt, v.e.d.gnd, isSel ? 255 : 150));
        trails.push({ path, colors, sel: isSel });
      }
      layers.push(new deck.PathLayer({
        id: 'trails', data: trails, getPath: (d) => d.path, getColor: (d) => d.colors,
        getWidth: (d) => (d.sel ? 3.5 : 1.6), widthUnits: 'pixels', jointRounded: true, capRounded: true,
        parameters: { depthTest: false },
      }));
    }

    if (state.is3D) {
      const air = visible.filter((v) => v.z > 0);
      layers.push(new deck.ScatterplotLayer({
        id: 'shadows', data: air, getPosition: (v) => [v.lon, v.lat, 0], getRadius: 2.5, radiusUnits: 'pixels',
        getFillColor: [0, 0, 0, 110],
      }));
      layers.push(new deck.LineLayer({
        id: 'stalks', data: air, getSourcePosition: (v) => [v.lon, v.lat, 0], getTargetPosition: (v) => [v.lon, v.lat, v.z],
        getColor: (v) => altColor(v.alt, false, 110), getWidth: 1, widthUnits: 'pixels',
      }));
    }

    if (sel) {
      layers.push(new deck.ScatterplotLayer({
        id: 'selected', data: [sel], getPosition: (v) => [v.lon, v.lat, v.z], getRadius: 22, radiusUnits: 'pixels',
        stroked: true, filled: true, getFillColor: [245, 179, 1, 40], getLineColor: [245, 179, 1, 255],
        lineWidthUnits: 'pixels', getLineWidth: 2, billboard: true, parameters: { depthTest: false },
      }));
    }

    const iconCommon = {
      data: visible, billboard: false, sizeUnits: 'pixels',
      getPosition: (v) => [v.lon, v.lat, v.z], getIcon: (v) => iconFor(v.e.d), getAngle: (v) => -(v.e.d.trk || 0),
      parameters: { depthTest: false },
    };
    layers.push(new deck.IconLayer({
      ...iconCommon, id: 'aircraft-outline', getSize: (v) => sizeFor(v.e.d) + 5, getColor: [10, 14, 20, 200],
    }));
    layers.push(new deck.IconLayer({
      ...iconCommon, id: 'aircraft', pickable: true,
      getSize: (v) => sizeFor(v.e.d) + (v.e.hex === state.selected ? 6 : 0),
      getColor: (v) => (v.e.hex === state.selected ? [245, 179, 1, 255] : altColor(v.alt, v.e.d.gnd)),
    }));

    if (state.showLabels && map.getZoom() >= 6.5) {
      const labelled = visible.length > 700 ? visible.filter((v) => v.e.hex === state.selected || v.alt > 20000 || map.getZoom() > 9) : visible;
      layers.push(new deck.TextLayer({
        id: 'labels', data: labelled.slice(0, 1500),
        getPosition: (v) => [v.lon, v.lat, v.z],
        getText: (v) => {
          const name = v.e.d.cs || v.e.d.reg || v.e.hex.toUpperCase();
          return state.is3D ? `${name}\n${fmtFL({ gnd: v.e.d.gnd, alt: v.alt })}` : name;
        },
        getSize: 11, getPixelOffset: [0, 22], getColor: [232, 238, 245, 255],
        background: true, getBackgroundColor: [17, 25, 35, 190], backgroundPadding: [4, 2],
        fontFamily: 'system-ui, -apple-system, Segoe UI, Roboto, sans-serif', fontWeight: 600, lineHeight: 1.15,
        billboard: true, parameters: { depthTest: false },
      }));
    }

    overlay.setProps({ layers });
  }

  // ------------------------------------------------------------------ selection & panel

  function select(hex) {
    state.selected = hex;
    state.selTrail = null;
    $('panel').hidden = !hex;
    $('p-route').hidden = true;
    if (!hex) { setFollow(false); return; }
    const e = state.aircraft.get(hex);
    lastRouteFor = null;
    if (e) loadRoute(e);
    loadSelectedTrail();
    updatePanel();
  }

  async function loadSelectedTrail() {
    const hex = state.selected;
    if (!hex) return;
    const to = simNow();
    try {
      const res = await fetch(`/api/trail?hex=${hex}&from=${(to - 3 * 3600).toFixed(0)}&to=${to.toFixed(0)}`);
      const j = await res.json();
      if (state.selected !== hex || !res.ok) return;
      state.selTrail = { hex, points: (j.points || []).map((p) => [p.lon, p.lat, p.gnd ? 0 : p.alt, !!p.gnd]) };
    } catch { /* trail is optional */ }
  }

  let lastRouteFor = null;
  async function loadRoute(e) {
    const cs = e.d.cs;
    if (!cs || lastRouteFor === cs) return;
    lastRouteFor = cs;
    try {
      const res = await fetch(`/api/route?callsign=${encodeURIComponent(cs)}&lat=${e.d.lat}&lon=${e.d.lon}`);
      const j = await res.json();
      if (state.selected !== e.hex || !j.route || !j.route.airports || j.route.airports.length < 2) return;
      const aps = j.route.airports;
      const ap = (a) => `<div class="ap"><b>${esc(a.iata || a.icao || '?')}</b><span>${esc(a.city || a.name || '')}</span></div>`;
      $('p-route').innerHTML = `${ap(aps[0])}<div class="arrow">✈</div>${ap(aps[aps.length - 1])}`;
      $('p-route').hidden = false;
    } catch { /* route is optional */ }
  }

  function setFollow(on) {
    state.follow = on;
    $('p-follow').classList.toggle('on', on);
    $('p-follow').textContent = on ? 'Following' : 'Follow';
  }
  $('p-follow').onclick = () => setFollow(!state.follow);
  $('panel-close').onclick = () => select(null);

  function updatePanel() {
    if (!state.selected) return;
    const e = state.aircraft.get(state.selected);
    if (!e) {
      $('p-seen').textContent = 'Signal lost';
      return;
    }
    const d = e.d;
    const p = displayPos(e, simNow(), realNow());
    $('p-callsign').textContent = d.cs || d.reg || d.hex.toUpperCase();
    $('p-sub').textContent = [d.type, d.reg && d.cs ? d.reg : null, d.cat ? `cat ${d.cat}` : null].filter(Boolean).join(' · ') || 'Unknown type';
    $('p-alt').textContent = fmtAlt({ gnd: d.gnd, alt: p.alt });
    $('p-vr').textContent = d.vr == null ? '—' : `${d.vr > 0 ? '▲ +' : d.vr < 0 ? '▼ ' : ''}${Math.round(d.vr).toLocaleString()} ft/min`;
    $('p-gs').textContent = d.gs == null ? '—' : `${Math.round(d.gs)} kt (${Math.round(d.gs * 1.852)} km/h)`;
    $('p-trk').textContent = d.trk == null ? '—' : `${Math.round(d.trk)}°`;
    $('p-reg').textContent = d.reg || '—';
    $('p-type').textContent = d.type || '—';
    $('p-sq').textContent = d.sq || '—';
    $('p-hex').textContent = d.hex.toUpperCase();
    $('p-pos').textContent = `${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}`;
    const age = Math.max(0, simNow() - e.fixT);
    $('p-seen').textContent = age < 2 ? 'just now' : `${Math.round(age)} s ago`;
    $('p-ext').href = `https://globe.adsb.lol/?icao=${d.hex}`;
  }

  // ------------------------------------------------------------------ search

  const searchInput = $('search');
  const results = $('search-results');
  let searchHits = [];
  let findTimer = null;

  function localMatches(q) {
    q = q.trim().toLowerCase();
    if (!q) return [];
    const out = [];
    for (const e of state.aircraft.values()) {
      const d = e.d;
      if ([d.cs, d.reg, d.hex, d.type].some((x) => x && x.toLowerCase().includes(q))) out.push(e.d);
      if (out.length >= 8) break;
    }
    return out;
  }
  function showHits(hits, note) {
    searchHits = hits;
    results.innerHTML = hits.map((d, i) =>
      `<li data-i="${i}"><span><b>${esc(d.cs || d.reg || d.hex.toUpperCase())}</b> ${esc(d.type || '')}</span>` +
      `<span class="muted">${esc(d.reg || d.hex.toUpperCase())}</span></li>`).join('') +
      (note ? `<li class="muted" data-i="-1">${esc(note)}</li>` : '');
    results.hidden = !hits.length && !note;
  }
  function goTo(d) {
    results.hidden = true;
    searchInput.blur();
    if (!state.aircraft.has(d.hex)) {
      state.aircraft.set(d.hex, { hex: d.hex, d, fixT: state.mode === 'live' ? d.ts : simNow(), trail: [], corr: null, lastSeen: simNow() });
    }
    map.flyTo({ center: [d.lon, d.lat], zoom: Math.max(map.getZoom(), 8) });
    select(d.hex);
  }
  searchInput.addEventListener('input', () => {
    const q = searchInput.value;
    const local = localMatches(q);
    showHits(local, local.length || q.trim().length < 3 ? '' : 'Press Enter to search worldwide');
    clearTimeout(findTimer);
  });
  searchInput.addEventListener('keydown', async (ev) => {
    if (ev.key === 'Escape') { results.hidden = true; searchInput.blur(); return; }
    if (ev.key !== 'Enter') return;
    const q = searchInput.value.trim();
    if (!q) return;
    if (searchHits.length) { goTo(searchHits[0]); return; }
    if (state.mode !== 'live') { showHits([], 'Worldwide search only works in live mode'); return; }
    showHits([], 'Searching…');
    try {
      const j = await getJSON(`/api/find?q=${encodeURIComponent(q)}`);
      if (j.aircraft.length) showHits(j.aircraft.slice(0, 8));
      else showHits([], `No aircraft found for “${q}”`);
    } catch (err) {
      showHits([], `Search failed: ${err.message}`);
    }
  });
  results.addEventListener('mousedown', (ev) => {
    const li = ev.target.closest('li');
    if (!li) return;
    const i = Number(li.dataset.i);
    if (i >= 0 && searchHits[i]) { ev.preventDefault(); goTo(searchHits[i]); }
  });
  searchInput.addEventListener('blur', () => setTimeout(() => { results.hidden = true; }, 150));

  // ------------------------------------------------------------------ time controls

  const dtInput = $('dt');
  let dtDirty = false; // true while the user has typed a time that has not been applied yet
  dtInput.addEventListener('input', () => { dtDirty = true; });
  dtInput.addEventListener('change', () => { dtDirty = true; });

  function updateModeUI() {
    const badge = $('mode-badge');
    if (state.mode === 'live') {
      badge.textContent = 'LIVE'; badge.className = 'badge live';
    } else if (state.playing) {
      badge.textContent = state.speed > 1 ? `REPLAY ${state.speed}×` : 'REPLAY'; badge.className = 'badge replay';
    } else {
      badge.textContent = 'PAUSED'; badge.className = 'badge paused';
    }
    $('btn-live').classList.toggle('on', state.mode === 'live');
    $('btn-play').textContent = state.mode === 'live' || state.playing ? '❚❚' : '▶';
    const url = new URL(location.href);
    if (state.mode === 'live') url.searchParams.delete('t');
    else url.searchParams.set('t', Math.round(simNow()));
    history.replaceState(null, '', url);
  }

  function goLive() {
    dtDirty = false;
    state.mode = 'live';
    state.playing = true;
    clearTraffic();
    updateModeUI();
    tick();
  }

  function goReplay(t, playing = true) {
    if (t >= liveNow() - 1) return goLive();
    state.mode = 'replay';
    state.playing = playing;
    setClock(t);
    clearTraffic();
    updateModeUI();
    tick();
  }

  $('btn-live').onclick = goLive;
  $('btn-go').onclick = () => {
    if (!dtInput.value) return;
    const t = new Date(dtInput.value).getTime() / 1000;
    if (Number.isNaN(t)) return;
    dtDirty = false;
    goReplay(t, true);
  };
  dtInput.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') $('btn-go').click(); });

  function togglePlay() {
    if (state.mode === 'live') {
      // Freeze the live picture: switch to a paused replay of right now, keeping what is on screen.
      state.mode = 'replay';
      setClock(liveNow());
      state.playing = false;
    } else if (state.playing) {
      setClock(simNow());
      state.playing = false;
    } else {
      setClock(state.anchorSim);
      state.playing = true;
    }
    updateModeUI();
  }
  $('btn-play').onclick = togglePlay;

  document.querySelectorAll('[data-step]').forEach((b) => {
    b.onclick = () => goReplay(simNow() + Number(b.dataset.step), state.mode === 'live' ? true : state.playing);
  });

  $('speed').onchange = () => {
    if (state.mode === 'replay') setClock(simNow());
    state.speed = Number($('speed').value);
    updateModeUI();
  };

  function updateClock() {
    const t = simNow();
    if (state.mode === 'replay' && state.playing && t >= liveNow() - 1) { goLive(); return; }
    $('clock-main').textContent = fmtLocal(t, state.mode === 'replay');
    $('clock-sub').textContent = fmtUTC(t) + (state.source ? ` · ${state.source}` : '');
    $('source').textContent = state.source ? `via ${state.source}` : '';
    if (!dtDirty && document.activeElement !== dtInput) dtInput.value = toInputValue(t);
    dtInput.max = toInputValue(liveNow());
  }

  function updateCoverage() {
    const c = state.config;
    if (!c) return;
    const el = $('coverage');
    if (c.demo) {
      el.textContent = 'Demo mode: simulated traffic, any date and time works.';
      return;
    }
    if (c.serverless) {
      el.textContent = c.samples
        ? 'Hosted version: replay works for the 1st of each month. Run openflight.py yourself to record any time.'
        : 'Hosted version: live only. Run openflight.py yourself to record and replay.';
      return;
    }
    const parts = [];
    if (c.recorded && c.recorded.from) parts.push(`Replay recorded from ${fmtLocal(c.recorded.from, true)}`);
    else parts.push('Nothing recorded yet; replay fills in as you watch');
    if (c.samples) parts.push('plus the 1st of every month worldwide');
    el.textContent = parts.join(', ') + '.';
  }

  // ------------------------------------------------------------------ view options

  function set3D(on) {
    state.is3D = on;
    $('btn-3d').classList.toggle('on', on);
    $('exag-wrap').hidden = !on;
    map.easeTo(on ? { pitch: 60, bearing: map.getBearing() || -20, duration: 900 } : { pitch: 0, bearing: 0, duration: 700 });
    store.set('is3D', on);
  }
  $('btn-3d').onclick = () => set3D(!state.is3D);
  map.on('pitchend', () => {
    // Tilting the map by hand also turns on 3D altitudes.
    if (map.getPitch() > 15 && !state.is3D) { state.is3D = true; $('btn-3d').classList.add('on'); $('exag-wrap').hidden = false; }
  });

  const bindCheck = (id, key) => {
    const el = $(id);
    const saved = store.get(key);
    if (typeof saved === 'boolean') el.checked = saved;
    state[key] = el.checked;
    el.onchange = () => { state[key] = el.checked; store.set(key, el.checked); };
  };
  bindCheck('opt-trails', 'showTrails');
  bindCheck('opt-labels', 'showLabels');
  bindCheck('opt-ground', 'showGround');

  const controls = $('controls');
  const savedOpts = store.get('optsOpen');
  controls.classList.toggle('collapsed', typeof savedOpts === 'boolean' ? !savedOpts : window.innerWidth < 760);
  $('btn-opts').onclick = () => {
    controls.classList.toggle('collapsed');
    store.set('optsOpen', !controls.classList.contains('collapsed'));
  };

  const exag = $('opt-exag');
  const savedExag = store.get('exag');
  if (typeof savedExag === 'number') exag.value = savedExag;
  const applyExag = () => { state.exag = Number(exag.value); $('exag-val').textContent = exag.value; store.set('exag', state.exag); };
  exag.oninput = applyExag;
  applyExag();

  $('btn-locate').onclick = () => {
    if (!navigator.geolocation) return toast('Location is not available in this browser.', 'error', 4000);
    navigator.geolocation.getCurrentPosition(
      (pos) => map.flyTo({ center: [pos.coords.longitude, pos.coords.latitude], zoom: 8 }),
      () => toast('Could not get your location.', 'error', 4000),
      { timeout: 10000 });
  };

  document.addEventListener('keydown', (ev) => {
    if (ev.target.closest('input, select, textarea')) return;
    if (ev.key === ' ') { ev.preventDefault(); togglePlay(); }
    else if (ev.key === 'Escape') select(null);
    else if (ev.key === '3') set3D(!state.is3D);
  });

  // ------------------------------------------------------------------ start

  async function loadConfig() {
    try {
      const cfg = await getJSON('/api/config');
      const first = !state.config;
      state.config = cfg;
      if (first) {
        state.serverOffset = cfg.now - realNow();
        addBasemap(cfg);
      }
      updateCoverage();
      return true;
    } catch (err) {
      toast(`Cannot reach the OpenFlight server: ${err.message}`);
      return false;
    }
  }

  map.on('load', async () => {
    while (!(await loadConfig())) await new Promise((r) => setTimeout(r, 3000));
    if (store.get('is3D') || map.getPitch() > 15) {
      state.is3D = true; $('btn-3d').classList.add('on'); $('exag-wrap').hidden = false;
    }
    const tParam = Number(new URL(location.href).searchParams.get('t'));
    if (tParam > 0) goReplay(tParam, true);
    else { updateModeUI(); tick(); }
    setInterval(tick, 1000);
    setInterval(loadConfig, 60000);
    requestAnimationFrame(frame);
  });

  window.openflight = { state, map, overlay }; // handy for debugging in the console
})();
