"""Find a Hermes the user already runs, for "I already have Hermes".

Where it looks, cheapest first:
  1. a host Hermes home mounted into the container (compose.yaml: ``~/.hermes:/hermes-host:ro``): its config says where
     the API server listens and its .env holds the key, so that one connects without typing anything;
  2. addresses given in JARVIS_HERMES_URLS;
  3. this container's host (host.docker.internal and the default gateway);
  4. the computer the browser is on, then the rest of its /24 network and any JARVIS_SCAN_SUBNETS.
A candidate is anything whose /health says ``"platform": "hermes-agent"``.
"""

import asyncio
import ipaddress
import json
import socket
from typing import Optional

import aiohttp

from . import config

PROBE_TIMEOUT = 1.5
SCAN_CONCURRENCY = 64
PORTS = (8642, 8643)


def _host_home_candidate() -> Optional[dict]:
    home = config.HOST_HERMES_HOME
    cfg_path = home / "config.yaml"
    if not cfg_path.is_file():
        return None
    try:
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    api = ((cfg.get("platforms") or {}).get("api_server") or {}) if isinstance(cfg, dict) else {}
    port = int(api.get("port") or 8642)
    key = ""
    try:
        for line in (home / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("API_SERVER_KEY="):
                key = line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return {"url": f"http://host.docker.internal:{port}", "source": f"Hermes home mounted at {home}",
            "api_key": key, "api_server_enabled": bool(api.get("enabled")), "listen_host": str(api.get("host") or "")}


def _default_gateway() -> str:
    try:
        with open("/proc/net/route", encoding="ascii") as routes:
            for line in routes.readlines()[1:]:
                fields = line.split()
                if fields[1] == "00000000":
                    return socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
    except (OSError, IndexError, ValueError):
        pass
    return ""


def _resolves(name: str) -> bool:
    try:
        socket.getaddrinfo(name, None)
        return True
    except OSError:
        return False


async def _probe(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    try:
        async with session.get(f"{url}/health", timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT)) as resp:
            if resp.status != 200:
                return None
            health = json.loads(await resp.read() or b"{}")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        return None
    if not isinstance(health, dict) or health.get("platform") != "hermes-agent":
        return None
    found = {"url": url, "hermes_version": str(health.get("version") or ""), "connector_version": ""}
    try:
        async with session.get(f"{url}/vesper/connector", timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT)) as resp:
            if resp.status == 200:
                found["connector_version"] = str((json.loads(await resp.read() or b"{}")).get("version") or "")
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        pass
    return found


def _private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_private and not addr.is_loopback


def _ip_of(name: str) -> str:
    try:
        return socket.gethostbyname(name)
    except OSError:
        return ""


async def find(client_ip: str = "", own_addresses: tuple = (), host_ip: str = "") -> list:
    """Every Hermes found, each with how it was found and whether its key is already known. ``host_ip`` is this
    computer's address as the browser used it (the Host header), so it and the Docker host's internal addresses can be
    shown as the one computer they are."""
    targets: dict = {}   # url -> source

    def add(url: str, source: str) -> None:
        targets.setdefault(url.rstrip("/"), source)

    mounted = _host_home_candidate()
    for url in config.HERMES_CANDIDATES:
        add(url, "JARVIS_HERMES_URLS")
    if _resolves("host.docker.internal"):
        for port in PORTS:
            add(f"http://host.docker.internal:{port}", "this computer (Docker host)")
    gateway = _default_gateway()
    if gateway:
        for port in PORTS:
            add(f"http://{gateway}:{port}", "this computer (Docker host)")
    if _private(client_ip):
        for port in PORTS:
            add(f"http://{client_ip}:{port}", "the computer this browser is on")
    subnets = list(config.SCAN_SUBNETS)
    if _private(client_ip):
        subnets.append(str(ipaddress.ip_network(f"{client_ip}/24", strict=False)))
    for subnet in subnets:
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            continue
        if network.num_addresses > 1024:      # a /22 at most: a bigger scan takes too long to be useful
            continue
        for host in network.hosts():
            if str(host) not in own_addresses:
                add(f"http://{host}:8642", f"your network ({network})")

    found: list = []
    semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)
    async with aiohttp.ClientSession() as session:
        async def one(url: str, source: str):
            async with semaphore:
                hit = await _probe(session, url)
            if hit:
                found.append({**hit, "source": source, "key_known": False})

        await asyncio.gather(*(one(url, source) for url, source in targets.items()))

    if mounted:
        match = next((f for f in found if f["url"] == mounted["url"]), None)
        if match:
            match.update(source=mounted["source"], key_known=bool(mounted["api_key"]))
        else:
            # Mounted but not answering: still worth showing, with what is wrong.
            found.insert(0, {"url": mounted["url"], "source": mounted["source"], "hermes_version": "",
                             "connector_version": "", "key_known": bool(mounted["api_key"]),
                             "problem": ("its API server is not enabled" if not mounted["api_server_enabled"]
                                         else "its API server is not answering (is the gateway running, on 0.0.0.0?)")})
    # The Docker host answers on several addresses (its bridge gateway, host.docker.internal, its LAN address): one row
    # per port, under the LAN address when known since that is the one the user recognises.
    this_computer = {ip for ip in (gateway, _ip_of("host.docker.internal"), host_ip) if ip}
    merged, seen_ports = [], {}
    for item in found:
        host, _, port = item["url"].split("://", 1)[-1].partition(":")
        if host in this_computer or host == "host.docker.internal":
            item["source"] = "this computer"
            if port in seen_ports:
                kept = seen_ports[port]
                if host == host_ip or item.get("key_known"):
                    kept.update({k: v for k, v in item.items() if k != "key_known" or v})
                continue
            if host_ip and _private(host_ip) and host != host_ip:
                item["url"] = f"http://{host_ip}:{port}"
            seen_ports[port] = item
        merged.append(item)
    # Best first: a known key, then this computer, then the wider network.
    return sorted(merged, key=lambda f: (not f.get("key_known"), "network" in f["source"], f["url"]))


def mounted_key(url: str) -> str:
    """The API key from the mounted Hermes home, when ``url`` is that Hermes."""
    mounted = _host_home_candidate()
    return mounted["api_key"] if mounted and mounted["url"].rstrip("/") == url.rstrip("/") else ""
