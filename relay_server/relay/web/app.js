// in-Time phone client. The phone only detects that a face is in front of the camera
// (face-api.js tiny detector) and sends the picture: the relay recognises the person,
// and accepts the check-in only with the one-time token it gave for that picture.
(() => {
  const MODEL_URL = 'vendor/face-api/model';
  const DETECT_EVERY_MS = 250;
  const FRAMES_TO_CONFIRM = 3;   // a face on N consecutive frames before sending the picture
  const RETRY_MS = 1500;         // pause after "not recognised"
  const AUTO_CONFIRM_S = 4;      // suggested direction is sent automatically
  const RESULT_MS = 3500;
  const GEO_MAX_AGE_MS = 60000;  // a location fix older than this no longer allows a check-in
  const GEO_RETRY_MS = 5000;     // retry after "location unavailable" (GPS off, no fix yet)

  const $ = (id) => document.getElementById(id);
  const svg = (body) => `<svg viewBox="0 0 24 24" width="64" height="64" aria-hidden="true">${body}</svg>`;
  const ICONS = {
    ok: svg('<circle cx="12" cy="12" r="11" fill="currentColor"/><path d="M7 12.5l3.2 3.2L17 9" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>'),
    ko: svg('<path d="M12 2.5L23 21.5H1z" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><rect x="11" y="9" width="2" height="6.5" rx="1" fill="#fff"/><circle cx="12" cy="18.2" r="1.25" fill="#fff"/>'),
    err: svg('<circle cx="12" cy="12" r="11" fill="currentColor"/><path d="M8.2 8.2l7.6 7.6M15.8 8.2l-7.6 7.6" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/>'),
  };
  const video = $('video'), frame = $('frame'), hint = $('hint'), footer = $('footer');

  let busy = false, streak = 0, countdownTimer = null, modelsOk = true;

  const setHint = (t) => { hint.textContent = t; };

  // Clock = relay's trusted time (internet), not the phone clock
  let timeOffset = null, siteOffsetMin = null, online = true;
  setInterval(() => {
    if (timeOffset === null) { $('clock').textContent = '--:--'; return; }
    const d = new Date(Date.now() + timeOffset + siteOffsetMin * 60000);
    $('clock').textContent = `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
  }, 1000);

  function applyStatus(st) {
    if (st.server_time) {
      timeOffset = st.server_time - Date.now();
      siteOffsetMin = st.utc_offset_min || 0;
    } else {
      timeOffset = null;
    }
    const wasOnline = online;
    online = st.internet_ok !== false;
    document.body.classList.toggle('offline', !online);
    if (!online) {
      setHint('No internet connection — check-in is unavailable for now');
      frame.className = '';
    } else if (!wasOnline) {
      setHint('Look at the camera');
    }
  }

  async function pollStatus() {
    try { await loadStatus(); footer.textContent = ''; }
    catch (e) { footer.textContent = 'Relay not reachable'; }
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    let body = {};
    try { body = await r.json(); } catch (e) { /* ignore */ }
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
    return body;
  }

  async function loadStatus() {
    const st = await api('api/status');
    applyStatus(st);
    $('site').textContent = st.site ? `in-Time · ${st.site}` : 'in-Time';
    modelsOk = st.models_ok !== false;
    if (!modelsOk && online) setHint(st.models_message || 'Face recognition not ready on the relay');
  }

  // GPS location is mandatory: no check-in can start without a recent fix from an
  // authorised, enabled location service. The relay refuses requests without it too.
  let geo = null, geoMessage = 'Waiting for GPS location…', geoWatch = null, geoRetry = null;

  const locationOk = () => geo !== null && Date.now() - geo.ts < GEO_MAX_AGE_MS;
  const locationPayload = () => ({lat: geo.lat, lng: geo.lng, accuracy: geo.accuracy, ts: geo.ts});

  function onPosition(p) {
    geo = {lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy, ts: Date.now()};
    geoMessage = '';
    document.body.classList.remove('no-geo');
  }

  function onPositionError(err) {
    geo = null;
    document.body.classList.add('no-geo');
    if (err.code === 1) {  // PERMISSION_DENIED: the browser will not ask again by itself
      geoMessage = 'Location access is blocked. Allow location for this site in the browser settings, then reload.';
    } else if (err.code === 2) {  // POSITION_UNAVAILABLE: location / GPS switched off
      geoMessage = 'Location unavailable. Turn on GPS / location services on this phone.';
    } else {  // TIMEOUT
      geoMessage = 'Searching GPS location…';
    }
    setHint(geoMessage);
    if (err.code !== 1) {
      clearTimeout(geoRetry);
      geoRetry = setTimeout(startLocation, GEO_RETRY_MS);
    }
  }

  function startLocation() {
    if (!navigator.geolocation) {
      geoMessage = 'This browser cannot share its location: check-in is not possible.';
      document.body.classList.add('no-geo');
      return;
    }
    if (geoWatch !== null) navigator.geolocation.clearWatch(geoWatch);
    geoWatch = navigator.geolocation.watchPosition(onPosition, onPositionError,
      {enableHighAccuracy: true, maximumAge: 10000, timeout: 20000});
  }

  // Resolves once a first fix is available (this is when the browser asks for permission).
  async function waitForLocation() {
    if (!window.isSecureContext) {
      throw new Error('Location needs HTTPS. Open the https:// address shown on the relay screen.');
    }
    if (!navigator.geolocation) {
      throw new Error('This browser cannot share its location: check-in is not possible.');
    }
    // re-start the watch as soon as the collaborator re-allows location in the browser settings
    if (navigator.permissions && navigator.permissions.query) {
      try {
        const perm = await navigator.permissions.query({name: 'geolocation'});
        perm.onchange = () => { if (perm.state !== 'denied') startLocation(); };
      } catch (e) { /* not supported */ }
    }
    startLocation();
    setHint(geoMessage);
    while (!locationOk()) {
      await new Promise((r) => setTimeout(r, 500));
      setHint(geoMessage || 'Waiting for GPS location…');
    }
  }

  async function startCamera() {
    if (!window.isSecureContext || !navigator.mediaDevices) {
      throw new Error('Camera needs HTTPS. Open the https:// address shown on the relay screen.');
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: false, video: {facingMode: 'user', width: {ideal: 640}, height: {ideal: 480}},
    });
    video.srcObject = stream;
    await new Promise((res) => { video.onloadedmetadata = res; });
    await video.play();
  }

  function snapshot() {
    // true orientation (not mirrored like the preview): the relay compares it with the collaborator's photo
    const w = Math.min(640, video.videoWidth), h = Math.round(video.videoHeight * (w / video.videoWidth));
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    c.getContext('2d').drawImage(video, 0, 0, w, h);
    return c.toDataURL('image/jpeg', 0.85);
  }

  const detectorOptions = new faceapi.TinyFaceDetectorOptions({inputSize: 320, scoreThreshold: 0.5});

  async function loop() {
    if (!busy && video.readyState >= 2) {
      try {
        await detectOnce();
      } catch (e) {
        console.error(e);
      }
    }
    setTimeout(loop, DETECT_EVERY_MS);
  }

  async function detectOnce() {
    if (!online || !modelsOk) return;  // the relay refuses check-ins
    if (!locationOk()) {
      frame.className = ''; streak = 0;
      document.body.classList.add('no-geo');
      setHint(geoMessage || 'Location lost. Turn on GPS / location services on this phone.');
      return;
    }
    document.body.classList.remove('no-geo');
    const det = await faceapi.detectSingleFace(video, detectorOptions);
    if (busy || !online) return;  // status may have changed during detection
    if (!det) {
      frame.className = ''; streak = 0;
      setHint('Look at the camera');
      return;
    }
    if (det.box.width < video.videoWidth * 0.18) {
      frame.className = 'seen'; streak = 0;
      setHint('Come closer');
      return;
    }
    frame.className = 'seen';
    setHint('Hold still…');
    if (++streak < FRAMES_TO_CONFIRM) return;
    busy = true;
    streak = 0;
    await identify(snapshot());
  }

  // The relay recognises the person in the picture (nothing is decided on the phone)
  async function identify(snap) {
    if (!locationOk()) { busy = false; return; }
    setHint('Recognising…');
    let res;
    try {
      res = await api('api/identify', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({snapshot: snap, location: locationPayload()}),
      });
    } catch (e) {
      if (/internet/i.test(e.message)) pollStatus();
      frame.className = ''; setHint(e.message);
      setTimeout(() => { busy = false; }, RETRY_MS);
      return;
    }
    if (!res.match) {
      frame.className = 'seen';
      setHint(res.reason === 'no_face' ? (res.message.charAt(0).toUpperCase() + res.message.slice(1))
                                       : 'Face not recognised');
      setTimeout(() => { busy = false; }, RETRY_MS);
      return;
    }
    frame.className = 'match';
    showConfirm(res);
  }

  function showConfirm(res) {
    const collaborator = res.collaborator, suggested = res.direction;
    $('c-name').textContent = collaborator.full_name;
    $('c-id').textContent = collaborator.id;
    $('c-photo').src = res.photo;
    document.querySelectorAll('.dir').forEach((b) => {
      b.classList.toggle('suggested', b.dataset.dir === suggested);
      b.onclick = () => submit(collaborator, b.dataset.dir, res.token);
    });
    $('c-cancel').onclick = reset;
    $('confirm').classList.remove('hidden');
    let left = AUTO_CONFIRM_S;
    const tick = () => {
      $('c-countdown').textContent = `Checking ${suggested} automatically in ${left}s`;
      if (left-- <= 0) submit(collaborator, suggested, res.token);
    };
    tick();
    countdownTimer = setInterval(tick, 1000);
  }

  async function submit(collaborator, direction, token) {
    clearInterval(countdownTimer);
    $('confirm').classList.add('hidden');
    if (!locationOk()) {
      showResult('err', ICONS.err, 'Not recorded', geoMessage || 'GPS location is required to check in.');
      return;
    }
    try {
      const res = await api('api/checkins', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token, direction, location: locationPayload()}),
      });
      const c = res.checkin;
      const reason = c.reason === 'late' ? 'Late arrival' : c.reason === 'early' ? 'Early departure' : 'On time';
      showResult(c.status === 'OK' ? 'ok' : 'ko', c.status === 'OK' ? ICONS.ok : ICONS.ko,
        `${c.direction} · ${c.time}`, `${c.full_name} — ${reason}`);
    } catch (e) {
      showResult('err', ICONS.err, 'Not recorded', e.message);
    }
  }

  function showResult(cls, icon, title, detail) {
    $('result').className = `sheet ${cls}`;
    $('r-icon').innerHTML = icon;  // trusted constant SVG (ICONS)
    $('r-title').textContent = title;
    $('r-detail').textContent = detail;
    setTimeout(reset, RESULT_MS);
  }

  function reset() {
    clearInterval(countdownTimer);
    $('confirm').classList.add('hidden');
    $('result').className = 'sheet hidden';
    frame.className = '';
    // wait for the person to leave the frame before recognising again
    setTimeout(() => { busy = false; }, 1500);
  }

  async function main() {
    try {
      // The relay may still be downloading the model files on first start.
      for (;;) {
        const st = await api('api/status');
        if (st.models_ok !== false) break;
        setHint(st.models_message || 'Face models not ready on the relay');
        await new Promise((r) => setTimeout(r, 5000));
      }
      setHint('Loading face models…');
      await faceapi.nets.tinyFaceDetector.loadFromUri(MODEL_URL);
      await loadStatus();
      footer.textContent = 'GPS location is required to check in';
      await waitForLocation();
      footer.textContent = '';
      setHint('Starting camera…');
      await startCamera();
      setHint('Look at the camera');
      loop();
      pollStatus();
      setInterval(pollStatus, 10000);
    } catch (e) {
      setHint(e.message);
      footer.textContent = 'Error';
      console.error(e);
    }
  }

  main();
})();
