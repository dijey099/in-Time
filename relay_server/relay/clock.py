"""Trusted clock: Google time over HTTPS instead of the PC clock.

Every `check_interval` seconds the relay requests TIME_URL (Google by default)
and reads the HTTP `Date` header. The answer comes over HTTPS, so it can't be
forged on the local network, and the request doubles as the internet check.

Between two checks, time advances with time.monotonic(), which is NOT affected
when someone changes the Windows clock. Changing the PC date/time therefore has
no effect on check-in times.

As soon as a check fails (or the last success is older than `max_age`
seconds), the clock is "not trusted" and the relay refuses new check-ins
until the next successful check (retried every `retry_interval` seconds).
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger("relay.clock")


class TrustedClock(threading.Thread):
    def __init__(self, urls, tz, check_interval=60, retry_interval=10, max_age=90, timeout=6):
        super().__init__(daemon=True, name="clock")
        self.urls = urls
        self.tz = tz
        self.check_interval = check_interval
        self.retry_interval = retry_interval
        self.max_age = max_age
        self.timeout = timeout
        self._lock = threading.Lock()
        self._ref_utc = None      # trusted UTC time at the last successful check
        self._ref_mono = None     # time.monotonic() at that moment
        self._message = "Checking internet connection…"
        self._pc_offset = None    # PC clock minus trusted time, in seconds
        self._failed = True       # last check failed (or none done yet)
        self._source = None       # host that gave the last trusted time
        self._logged_offset = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "in-Time relay"

    # ------------------------------------------------------------ public API
    @property
    def ok(self) -> bool:
        with self._lock:
            return (not self._failed and self._ref_mono is not None
                    and time.monotonic() - self._ref_mono <= self.max_age)

    def now_utc(self):
        """Trusted UTC time, or None when the clock can't be trusted."""
        with self._lock:
            if self._failed or self._ref_mono is None:
                return None
            elapsed = time.monotonic() - self._ref_mono
            if elapsed > self.max_age:
                return None
            return self._ref_utc + timedelta(seconds=elapsed)

    def now(self):
        """Trusted local time (configured time zone), or None."""
        utc = self.now_utc()
        return utc.astimezone(self.tz) if utc else None

    def today(self):
        n = self.now()
        return n.date() if n else None

    def status(self):
        with self._lock:
            age = None if self._ref_mono is None else time.monotonic() - self._ref_mono
            return {"ok": not self._failed and age is not None and age <= self.max_age, "message": self._message,
                    "source": self._source,
                    "last_sync_age": age, "pc_offset": self._pc_offset}

    def check_now(self):
        self._wake.set()

    def stop(self):
        self._stopping.set()
        self._wake.set()

    # --------------------------------------------------------------- worker
    def run(self):
        while not self._stopping.is_set():
            ok = self.sync_once()
            self._wake.wait(self.check_interval if ok else self.retry_interval)
            self._wake.clear()

    def sync_once(self) -> bool:
        errors = []
        bad_date = False
        for url in self.urls:
            try:
                t0 = time.monotonic()
                r = self.http.get(url, timeout=self.timeout, allow_redirects=False)
                t1 = time.monotonic()
                header = r.headers.get("Date")
                if not header:
                    raise ValueError("no Date header")
                server = parsedate_to_datetime(header)
                if server.tzinfo is None:
                    server = server.replace(tzinfo=timezone.utc)
                # The Date header has 1 s resolution and is produced while the
                # request is in flight: take the middle of the round trip.
                rtt = t1 - t0
                mono_mid = t0 + rtt / 2
                pc_offset = (datetime.now(timezone.utc) - server).total_seconds() - rtt / 2
                with self._lock:
                    self._ref_utc = server.astimezone(timezone.utc) + timedelta(seconds=0.5)
                    self._ref_mono = mono_mid
                    self._pc_offset = pc_offset
                    self._failed = False
                    self._source = url.split("/")[2]
                    self._message = "Internet OK — time from " + self._source
                # log the PC clock gap once, and again only when it changes
                if abs(pc_offset) > 120 and (self._logged_offset is None or abs(pc_offset - self._logged_offset) > 60):
                    log.warning("PC clock is off by %.0f min (ignored, trusted time is used)", pc_offset / 60)
                    self._logged_offset = pc_offset
                elif abs(pc_offset) <= 120 and self._logged_offset is not None:
                    log.info("PC clock is correct again")
                    self._logged_offset = None
                return True
            except Exception as exc:  # noqa: BLE001 - any failure means "no trusted time"
                text = str(exc)
                # A secure connection also fails when the PC date is far off
                # (certificate "not yet valid" / "expired"): say so, not "no internet".
                bad_date = bad_date or "not yet valid" in text or "has expired" in text
                errors.append(f"{url.split('/')[2]}: {type(exc).__name__}: {text[-160:]}")
        with self._lock:
            self._failed = True
            self._message = ("Secure time check failed: the date of this PC is wrong — fix it in Windows "
                             "(check-ins disabled)" if bad_date else
                             "No internet connection — check-ins disabled")
        log.warning("Internet/time check failed: %s", "; ".join(errors))
        return False
