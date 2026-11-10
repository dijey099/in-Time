"""Public IP address of this relay, as seen from the internet (https://ifconfig.me).

Shown in the relay window: it is the address to put in the relay's "Allowed IPs" on the main
server. Fetched in a background thread so the window never waits for the network.
"""
import ipaddress
import logging
import threading

import requests

log = logging.getLogger("relay.public_ip")

URL = "https://ifconfig.me/ip"  # plain-text answer (the home page returns HTML to browsers)
REFRESH_S = 600                 # the address of an internet box can change: check every 10 min
RETRY_S = 60                    # after a failure (no internet, service down)


class PublicIP(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="public-ip")
        self.value = None   # "203.0.113.10", None until known
        self.error = None   # last error message, None when the last attempt worked
        self._wake = threading.Event()

    def refresh_now(self):
        self._wake.set()

    def run(self):
        while True:
            ok = self._fetch()
            self._wake.wait(REFRESH_S if ok else RETRY_S)
            self._wake.clear()

    def _fetch(self) -> bool:
        try:
            r = requests.get(URL, timeout=10, headers={"Accept": "text/plain", "User-Agent": "curl/8 (in-Time relay)"})
            r.raise_for_status()
            ip = str(ipaddress.ip_address(r.text.strip()))  # only an IP address is accepted
        except (requests.RequestException, ValueError) as exc:
            self.error = "ifconfig.me not reachable" if isinstance(exc, requests.RequestException) \
                else "unexpected answer from ifconfig.me"
            log.info("Public IP: %s (%s)", self.error, exc)
            return False
        if ip != self.value:
            log.info("Public IP: %s", ip)
        self.value, self.error = ip, None
        return True
