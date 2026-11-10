// Site area picker (Leaflet + OpenStreetMap): a circle = centre + radius in metres.
// Click the map (or search an address / use this device's location) to place the centre,
// drag the marker to adjust it, and set the radius with the slider or the number field.
//
// Tile and search servers come from window.AREA_MAP (MAP_* settings of the main server). The admin
// console sends no Referer to other sites (Referrer-Policy: same-origin), but OpenStreetMap's tile
// usage policy requires one: map requests send this site's origin only (never the page path).
window.AreaPicker = function AreaPicker(root, {limits = {min: 10, max: 50000}, fallback = null, onChange = () => {}} = {}) {
  const DEFAULT_RADIUS = 150;
  const REFERRER = 'strict-origin-when-cross-origin';
  const cfg = Object.assign({
    tiles: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    search: 'https://nominatim.openstreetmap.org/search',
  }, window.AREA_MAP || {});
  root.innerHTML = `
    <div class="area-tools">
      <input type="search" class="area-search" placeholder="Search an address or place" aria-label="Search an address or place">
      <button type="button" class="secondary area-find">Search</button>
      <button type="button" class="secondary area-here" title="Use this device's current location">My location</button>
    </div>
    <div class="area-map" role="application" aria-label="Map: click to place the centre of the site area"></div>
    <div class="area-radius">
      <label>Radius
        <input type="range" class="area-range" min="${limits.min}" max="2000" step="10" value="${DEFAULT_RADIUS}">
        <input type="number" class="area-number" min="${limits.min}" max="${limits.max}" step="10" value="${DEFAULT_RADIUS}"> m
      </label>
      <span class="area-status muted"></span>
    </div>`;
  const q = (sel) => root.querySelector(sel);
  const range = q('.area-range'), number = q('.area-number'), status = q('.area-status'), search = q('.area-search');

  const map = L.map(q('.area-map'), {scrollWheelZoom: true});
  L.tileLayer(cfg.tiles, {maxZoom: 19, attribution: cfg.attribution, referrerPolicy: REFERRER}).addTo(map);
  if (!cfg.search) { q('.area-search').remove(); q('.area-find').remove(); }
  L.Icon.Default.imagePath = root.ownerDocument.querySelector('link[href*="leaflet/leaflet.css"]')
    .href.replace(/leaflet\.css.*$/, 'images/');

  let marker = null, circle = null;
  // bounds from centre + radius: works even before the map has a view (circle.getBounds() doesn't)
  const fit = () => { if (marker) map.fitBounds(marker.getLatLng().toBounds(radius() * 2), {padding: [30, 30], maxZoom: 18}); };

  const radius = () => Math.min(limits.max, Math.max(limits.min, parseFloat(number.value) || DEFAULT_RADIUS));
  const value = () => (marker ? {lat: marker.getLatLng().lat, lng: marker.getLatLng().lng, radius: radius()} : null);
  const describe = () => {
    const v = value();
    status.classList.remove('error');
    status.textContent = v ? `Centre ${v.lat.toFixed(5)}, ${v.lng.toFixed(5)} · ${Math.round(v.radius)} m`
                           : 'Click the map to place the centre of the site.';
  };
  const changed = () => { describe(); onChange(value()); };

  function place(latlng, {fit: doFit = false} = {}) {
    if (!map._loaded) map.setView(latlng, 16);  // first placement on a map without a view yet
    if (!marker) {
      marker = L.marker(latlng, {draggable: true, autoPan: true}).addTo(map);
      circle = L.circle(latlng, {radius: radius(), color: '#2e14a0', fillColor: '#5b3df5', fillOpacity: 0.15, weight: 2}).addTo(map);
      marker.on('drag', () => circle.setLatLng(marker.getLatLng()));
      marker.on('dragend', changed);
    } else {
      marker.setLatLng(latlng);
      circle.setLatLng(latlng);
    }
    if (doFit) fit();
    changed();
  }

  function clear() {
    if (marker) { map.removeLayer(marker); map.removeLayer(circle); marker = circle = null; }
    changed();
  }

  function setRadius(r, from) {
    r = Math.min(limits.max, Math.max(limits.min, r));
    if (from !== number) number.value = Math.round(r);
    if (from !== range) range.value = Math.min(range.max, r);
    if (circle) circle.setRadius(r);
    changed();
  }

  map.on('click', (e) => place(e.latlng));
  range.oninput = () => setRadius(parseFloat(range.value), range);
  number.onchange = () => setRadius(parseFloat(number.value) || DEFAULT_RADIUS, number);
  range.onchange = fit;

  async function find() {
    const text = search.value.trim();
    if (!text) return;
    status.textContent = 'Searching…';
    try {
      const url = new URL(cfg.search);
      url.searchParams.set('format', 'jsonv2'); url.searchParams.set('limit', '1'); url.searchParams.set('q', text);
      const r = await fetch(url, {headers: {'Accept': 'application/json'}, referrerPolicy: REFERRER});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const hits = await r.json();
      if (!hits.length) { api.setError('Place not found. Try another search or click the map.'); return; }
      place(L.latLng(parseFloat(hits[0].lat), parseFloat(hits[0].lon)), {fit: true});
    } catch (e) {
      api.setError('Search unavailable. Click the site on the map instead.');
    }
  }
  if (cfg.search) q('.area-find').onclick = find;
  search.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); find(); } };

  q('.area-here').onclick = () => {
    if (!navigator.geolocation) { api.setError('This browser cannot share its location.'); return; }
    status.textContent = 'Locating…';
    navigator.geolocation.getCurrentPosition(
      (p) => place(L.latLng(p.coords.latitude, p.coords.longitude), {fit: true}),
      () => api.setError('Location unavailable (allowed for this site? HTTPS?). Click the map instead.'),
      {enableHighAccuracy: true, timeout: 15000});
  };

  const api = {
    get: value,
    set(area) {
      // the map may have been hidden (dialog): recompute its size first
      setTimeout(() => {
        map.invalidateSize();
        if (area && !isNaN(area.lat) && !isNaN(area.lng)) {
          setRadius(area.radius || DEFAULT_RADIUS);
          place(L.latLng(area.lat, area.lng), {fit: true});
        } else {
          clear();
          setRadius(DEFAULT_RADIUS);
          if (fallback) map.setView([fallback.lat, fallback.lng], 14); else map.setView([0, 20], 2);
        }
      }, 0);
    },
    setError(msg) { status.textContent = msg; status.classList.add('error'); },
  };

  // initial value from data-* attributes (e.g. form re-displayed after an error)
  const lat = parseFloat(root.dataset.lat), lng = parseFloat(root.dataset.lng);
  api.set(isNaN(lat) || isNaN(lng) ? null : {lat, lng, radius: parseFloat(root.dataset.radius)});
  return api;
};
