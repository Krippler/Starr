"""Auto-discovery of the *arr containers via the Docker socket.

Strategy
--------
We list every running container the Docker daemon knows about and match by:

  1. Container name (case-insensitive: "sonarr", "radarr", "lidarr",
     "sportarr") — strongest signal.
  2. Image repo (linuxserver/sonarr, lscr.io/linuxserver/sonarr,
     hotio/sonarr, ghcr.io/hotio/sonarr, …).
  3. Internal listen port: 8989 (sonarr), 7878 (radarr), 8686 (lidarr),
     1867 (sportarr).

For each matched container we extract:

  - The runtime-reachable URL (container bridge IP + internal port +
    any urlbase set as an env var on the container itself).
  - The host path that's mounted at the container's /config (so we
    can map it through Starr's own /appdata mount and read the DB).

Translating "Sonarr's host /config" to a path inside Starr requires
knowing where Starr itself mounted the host appdata root. We inspect
Starr's own container at startup to learn that.

All discovery output is a small JSON document, no caches or side-effects.
The web UI is responsible for asking when to refresh.
"""

import logging
import os
import socket
from pathlib import PurePosixPath
from typing import Any

log = logging.getLogger("starr-repair.discovery")

# Maps the canonical app name onto (default-port, image-keywords). The image
# keywords are matched as substrings against the container's repo:tag string.
# dbname is the path of the SQLite file relative to the container's /config.
# Almost always "<app>.db"; Bazarr keeps it under db/.
APP_FINGERPRINTS = {
    "sonarr":   {"port": 8989, "image_keywords": ["sonarr"],   "dbname": "sonarr.db"},
    "radarr":   {"port": 7878, "image_keywords": ["radarr"],   "dbname": "radarr.db"},
    "lidarr":   {"port": 8686, "image_keywords": ["lidarr"],   "dbname": "lidarr.db"},
    "sportarr": {"port": 1867, "image_keywords": ["sportarr"], "dbname": "sportarr.db"},
    "readarr":  {"port": 8787, "image_keywords": ["readarr"],  "dbname": "readarr.db"},
    "prowlarr": {"port": 9696, "image_keywords": ["prowlarr"], "dbname": "prowlarr.db"},
    "whisparr": {"port": 6969, "image_keywords": ["whisparr"], "dbname": "whisparr.db"},
    "bazarr":   {"port": 6767, "image_keywords": ["bazarr"],   "dbname": "db/bazarr.db"},
}


def _docker_client():
    """Lazy Docker client. Returns None if the SDK isn't installed or the
    daemon is unreachable — callers handle the fallback."""
    try:
        import docker
    except ImportError:
        return None
    try:
        # 30s (matching server._docker_client) so a busy daemon doesn't time
        # out the ping and make us report Docker as unavailable — a false
        # "unavailable" leaves the discovery cache holding stale container IPs.
        client = docker.from_env(timeout=30)
        client.ping()
        return client
    except Exception as e:
        log.debug("Docker daemon unreachable: %s", e)
        return None


def _classify(container) -> str | None:
    """Return the canonical app name (sonarr/radarr/…) this container is, or
    None if it doesn't look like an *arr."""
    name = (container.name or "").lower()
    for app, fp in APP_FINGERPRINTS.items():
        if name == app or name.startswith(app + "-") or name.endswith("-" + app):
            return app

    image_tags = []
    try:
        image_tags = list(container.image.tags or [])
    except Exception:
        pass
    image_tags.append(container.attrs.get("Config", {}).get("Image") or "")
    image_blob = " ".join(image_tags).lower()
    for app, fp in APP_FINGERPRINTS.items():
        if any(kw in image_blob for kw in fp["image_keywords"]):
            return app

    # Port match — last resort.
    exposed = (container.attrs.get("Config", {}).get("ExposedPorts") or {})
    ports = [int(p.split("/")[0]) for p in exposed.keys() if "/" in p]
    for app, fp in APP_FINGERPRINTS.items():
        if fp["port"] in ports:
            return app
    return None


def _bridge_ip(container) -> str | None:
    """First non-empty IP on any network the container is attached to."""
    networks = (container.attrs.get("NetworkSettings", {}).get("Networks") or {})
    for net in networks.values():
        ip = net.get("IPAddress")
        if ip:
            return ip
    return None


def _container_env(container) -> dict[str, str]:
    out = {}
    for entry in (container.attrs.get("Config", {}).get("Env") or []):
        if "=" in entry:
            k, v = entry.split("=", 1)
            out[k] = v
    return out


def _published_port(container, internal_port: int) -> int | None:
    """Return the host port the container's <internal_port> is bound to,
    e.g. for `docker run -p 8989:8989 sonarr` => 8989. None if not bound."""
    bindings = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
    entries = bindings.get(f"{internal_port}/tcp") or []
    for e in entries:
        hp = e.get("HostPort")
        if hp:
            try:
                return int(hp)
            except ValueError:
                pass
    return None


def _config_host_path(container) -> str | None:
    """Where on the host is mounted at the container's /config."""
    for m in container.attrs.get("Mounts", []) or []:
        if m.get("Destination") == "/config":
            return m.get("Source")
    return None


def _self_appdata_root() -> tuple[str | None, str]:
    """Resolve Starr's own /appdata mount: returns (host_root, container_root)
    so we can translate other containers' host paths to a path Starr can read.

    If the discovery is running outside Docker, returns (None, "/appdata")
    so callers can still pass through paths unchanged."""
    container_root = os.environ.get("APPDATA_DIR", "/appdata")
    client = _docker_client()
    if not client:
        return None, container_root
    try:
        my_id = socket.gethostname()
        me = client.containers.get(my_id)
    except Exception as e:
        log.debug("Could not look up own container (hostname=%s): %s", socket.gethostname(), e)
        return None, container_root
    for m in me.attrs.get("Mounts", []) or []:
        if m.get("Destination") == container_root:
            return m.get("Source"), container_root
    return None, container_root


def _translate_host_to_internal(host_path: str | None,
                                host_root: str | None,
                                container_root: str) -> str | None:
    """Map an arbitrary host path under Starr's /appdata mount to the path
    Starr can read inside its own container."""
    if not host_path:
        return None
    if not host_root:
        # Discovery returns the host path verbatim and lets the caller try.
        return host_path
    try:
        rel = PurePosixPath(host_path).relative_to(host_root)
    except ValueError:
        # The *arr's /config isn't under our appdata mount — caller will
        # report a clear error to the user.
        return None
    return str(PurePosixPath(container_root) / rel)


def discover() -> dict[str, Any]:
    """Run the full scan. Returns a JSON-serialisable summary."""
    result: dict[str, Any] = {
        "docker_available": False,
        "appdata":          {"host_root": None, "container_root": "/appdata"},
        "apps":             [],
        "warnings":         [],
    }
    client = _docker_client()
    if not client:
        result["warnings"].append("Docker daemon not reachable — auto-discovery skipped.")
        return result
    result["docker_available"] = True

    host_root, container_root = _self_appdata_root()
    result["appdata"] = {"host_root": host_root, "container_root": container_root}
    if not host_root:
        result["warnings"].append(
            "Starr's /appdata is not mounted; database paths cannot be auto-resolved.")

    by_app: dict[str, dict] = {}
    for container in client.containers.list(all=False):
        app = _classify(container)
        if not app or app in by_app:
            continue
        env = _container_env(container)
        ip   = _bridge_ip(container)
        port = APP_FINGERPRINTS[app]["port"]
        urlbase = (env.get(f"{app.upper()}__APP__URLBASE")  # Sonarr/Radarr
                   or env.get("URLBASE") or "").strip().rstrip("/")
        url = f"http://{ip}:{port}{urlbase}" if ip else None
        published = _published_port(container, port)
        config_host = _config_host_path(container)
        dbname = APP_FINGERPRINTS[app].get("dbname", f"{app}.db")
        db_internal = _translate_host_to_internal(
            f"{config_host}/{dbname}" if config_host else None,
            host_root, container_root)
        by_app[app] = {
            "app":              app,
            "container_name":   container.name,
            "url":              url,
            "internal_port":    port,
            "published_port":   published,
            "urlbase":          urlbase,
            "config_host_path": config_host,
            "db_path":          db_internal,
            "missing_appdata":  bool(config_host and host_root and db_internal is None),
        }
    result["apps"] = [by_app[a] for a in sorted(by_app)]
    # Surface a useful warning when we found containers but couldn't translate
    # their DB paths (almost always means the host appdata layout doesn't match
    # what's mounted at /appdata).
    if host_root and any(a["missing_appdata"] for a in result["apps"]):
        result["warnings"].append(
            f"Some *arr config dirs live outside {host_root!r}; "
            "they won't be reachable via /appdata.")
    return result
