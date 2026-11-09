#!/usr/bin/env python3
"""Update cf_ips.txt with Cloudflare's current IP ranges.

    python update_cf_ips.py            # update if Cloudflare's list changed
    python update_cf_ips.py --check    # only report, never write

The file is rewritten only when the set of ranges differs from what it holds;
otherwise nothing is touched. If the download fails or looks wrong, the file
is left as is. The main server picks up a new file automatically (no restart).

Exit codes: 0 = up to date or updated, 1 = error, 2 = changes found with --check.
Example cron (daily at 04:17):
    17 4 * * * cd /path/to/in-Time/main_server && .venv/bin/python update_cf_ips.py >> cf_ips_update.log 2>&1
"""
import argparse
import ipaddress
import os
import sys
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES = {
    "IPv4": "https://www.cloudflare.com/ips-v4",
    "IPv6": "https://www.cloudflare.com/ips-v6",
}
MIN_RANGES = {"IPv4": 5, "IPv6": 3}  # sanity check against truncated/garbage answers


def default_file() -> Path:
    try:  # same location the server uses (CF_IPS_FILE in .env), if set
        from dotenv import dotenv_values
        value = dotenv_values(HERE / ".env").get("CF_IPS_FILE")
    except ImportError:
        value = None
    value = os.environ.get("CF_IPS_FILE", value)
    path = Path(value) if value else HERE / "cf_ips.txt"
    return path if path.is_absolute() else HERE / path


def parse_ranges(text: str):
    """CIDRs from a text (comments/blank lines ignored). Raises ValueError on garbage."""
    nets = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            nets.append(ipaddress.ip_network(line, strict=False))
    return nets


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "in-Time update_cf_ips"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"{url}: HTTP {r.status}")
        return r.read().decode("utf-8", "replace")


def download(sources):
    result = {}
    for family, url in sources.items():
        nets = parse_ranges(fetch(url))
        expected = 4 if family == "IPv4" else 6
        if any(n.version != expected for n in nets):
            raise ValueError(f"{url}: unexpected address family in answer")
        if len(nets) < MIN_RANGES[family]:
            raise ValueError(f"{url}: only {len(nets)} ranges received, refusing to update")
        result[family] = nets
    return result


def render(ranges) -> str:
    lines = [
        "# Cloudflare IP ranges, used when CF_PROXY=true to trust the CF-Connecting-IP header.",
        f"# Source: {SOURCES['IPv4']} and {SOURCES['IPv6']}",
        f"# Updated: {date.today().isoformat()} by update_cf_ips.py",
        "# One CIDR per line. Lines starting with # are ignored.",
    ]
    for family in ("IPv4", "IPv6"):
        lines += ["", f"# {family}", *(str(n) for n in ranges[family])]
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cf_ips.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description="Update cf_ips.txt with Cloudflare's IP ranges")
    ap.add_argument("--file", type=Path, default=None, help="file to update (default: cf_ips.txt / CF_IPS_FILE)")
    ap.add_argument("--check", action="store_true", help="report changes without writing")
    ap.add_argument("--source-v4", default=SOURCES["IPv4"], help=argparse.SUPPRESS)
    ap.add_argument("--source-v6", default=SOURCES["IPv6"], help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    path = args.file or default_file()

    try:
        fresh = download({"IPv4": args.source_v4, "IPv6": args.source_v6})
    except Exception as exc:
        print(f"ERROR: could not get Cloudflare's IP list, {path.name} left unchanged: {exc}", file=sys.stderr)
        return 1

    new_set = set(fresh["IPv4"]) | set(fresh["IPv6"])
    try:
        old_set = set(parse_ranges(path.read_text(encoding="utf-8"))) if path.exists() else set()
    except ValueError as exc:
        print(f"WARNING: {path} contains an invalid line ({exc}); it will be rewritten")
        old_set = None

    if old_set == new_set:
        print(f"{path.name} is up to date ({len(new_set)} ranges), nothing to do.")
        return 0

    if old_set is not None:
        for n in sorted(new_set - old_set, key=lambda n: (n.version, n)):
            print(f"  + {n}")
        for n in sorted(old_set - new_set, key=lambda n: (n.version, n)):
            print(f"  - {n}")

    if args.check:
        print(f"{path.name} is out of date (run without --check to update).")
        return 2

    write_atomic(path, render(fresh))
    print(f"{path.name} updated: {len(new_set)} ranges.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
