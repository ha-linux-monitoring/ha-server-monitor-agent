#!/usr/bin/env python3
"""HA Server Monitor storage-monitoring agent daemon.

Ships with zero prior knowledge of the Home Assistant instance it reports to.
It advertises itself on the LAN via Avahi/mDNS (see the accompanying
ha-server-monitor-agent-mdns.service file) and waits to be paired: the
ha_server_monitor Home Assistant integration discovers it (or is pointed at it
manually), then calls this daemon's own /register endpoint to hand over its
webhook URL and a token generated at that moment. From then on, this daemon
pushes storage-topology heartbeats and mdadm/smartd-triggered alerts to that
URL, authenticated with that token.

Generic topology introspection (lsblk/mdstat/smartctl/df) is unchanged from
the previous oneshot ha-report.py -- only the outer shell changed, from a
systemd-timer-triggered script to an always-running daemon with its own
small local HTTP API:

  GET  /info           -> {hostname, version}                  (any caller)
  POST /register       -> {webhook_url, token}                 (any caller,
                           but accepted exactly once; 409 after that)
  POST /trigger-event  -> {raw_id, message}                     (127.0.0.1
                           only -- called by mdadm-ha-alert.sh/smartd-ha-alert.sh)

See agent/README.md for the full pairing flow and the security tradeoff of
exposing /register on the network.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AGENT_VERSION = "1.0.0"

# Overridable via environment for testing; production installs use the defaults.
CONFIG_FILE = Path(
    os.environ.get("HSM_AGENT_CONFIG_FILE", "/etc/ha-server-monitor-agent/agent.conf")
)
STATE_FILE = Path(
    os.environ.get("HSM_AGENT_STATE_FILE", "/var/lib/ha-server-monitor-agent/state.json")
)

DEFAULT_PORT = 8477
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 900

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ha-server-monitor-agentd")


# --- Settings (admin-editable, /etc) ----------------------------------------


class Settings:
    def __init__(self) -> None:
        self.port = int(os.environ.get("HSM_AGENT_PORT", DEFAULT_PORT))
        self.heartbeat_interval_seconds = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if key.strip() == "PORT":
                    self.port = int(value)
                elif key.strip() == "HEARTBEAT_INTERVAL_SECONDS":
                    self.heartbeat_interval_seconds = int(value)


# --- Pairing state (daemon-written, /var/lib) -------------------------------


class PairingState:
    """Holds the webhook_url/token this agent was registered with, if any."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.webhook_url: str | None = None
        self.token: str | None = None
        self._load()

    def _load(self) -> None:
        if not STATE_FILE.exists():
            return
        data = json.loads(STATE_FILE.read_text())
        self.webhook_url = data.get("webhook_url")
        self.token = data.get("token")

    @property
    def paired(self) -> bool:
        return self.webhook_url is not None and self.token is not None

    def register(self, webhook_url: str, token: str) -> bool:
        """Store a new pairing. Returns False if already paired (no-op)."""
        with self._lock:
            if self.paired:
                return False
            self.webhook_url = webhook_url
            self.token = token
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            STATE_FILE.write_text(json.dumps({"webhook_url": webhook_url, "token": token}))
            STATE_FILE.chmod(0o600)
            return True


# --- Topology introspection (unchanged from ha-report.py) ------------------


def run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout


def service_active(name: str) -> bool:
    return (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", name], timeout=10
        ).returncode
        == 0
    )


def smart_health(device: str) -> tuple[bool, str]:
    """Best-effort SMART overall-health check; never raises."""
    try:
        out = run("smartctl", "-H", f"/dev/{device}").lower()
    except Exception as exc:  # pragma: no cover - defensive
        return True, f"smartctl error: {exc}"

    if "self-assessment test result: passed" in out or (
        "smart health status:" in out and "ok" in out
    ):
        return True, "SMART: PASSED"
    if "self-assessment test result: failed" in out or (
        "smart health status:" in out and "ok" not in out
    ):
        return False, "SMART: FAILED"
    if (
        "smart support is: unavailable" in out
        or "smart support is: disabled" in out
        or "does not support smart" in out
        or "inquiry failed" in out
    ):
        return True, "SMART not supported"
    return True, "SMART: status unknown"


def fs_id_for(mount_point: str) -> str:
    if mount_point == "/":
        return "root"
    return mount_point.strip("/").replace("/", "_")


def gather_topology() -> tuple[list[dict], list[dict], dict[str, str]]:
    tree = json.loads(
        run("lsblk", "-J", "-o", "NAME,TYPE,SIZE,MOUNTPOINT,FSTYPE,MODEL,PKNAME")
    )

    all_disks: dict[str, dict] = {}
    physical_disks: dict[str, dict] = {}
    raid_arrays: dict[str, dict] = {}
    filesystems_raw: dict[str, dict] = {}

    def walk(node: dict, parent_name: str | None) -> None:
        name = node["name"]
        ntype = node["type"]

        if node.get("mountpoint"):
            filesystems_raw[node["mountpoint"]] = {
                "mount_point": node["mountpoint"],
                "fs_type": node.get("fstype") or "unknown",
                "parent_id": node.get("pkname") or parent_name,
            }

        if ntype == "disk":
            all_disks[name] = {"model": node.get("model"), "size": node.get("size")}
            if node.get("fstype") != "linux_raid_member":
                physical_disks[name] = node
        elif ntype.startswith("raid"):
            entry = raid_arrays.setdefault(
                name, {"level": ntype, "size": node.get("size"), "members": set()}
            )
            if parent_name:
                entry["members"].add(parent_name)

        for child in node.get("children", []):
            walk(child, name)

    for top in tree.get("blockdevices", []):
        walk(top, None)

    physical_devices: list[dict] = []

    for name, node in physical_disks.items():
        healthy, detail = smart_health(name)
        model = node.get("model") or "unknown model"
        size = node.get("size") or "?"
        physical_devices.append(
            {
                "id": name,
                "type": "simple",
                "description": f"{model} ({size})",
                "healthy": healthy,
                "health_detail": detail,
            }
        )

    member_to_array: dict[str, str] = {}

    for name, entry in raid_arrays.items():
        members = sorted(entry["members"])
        for m in members:
            member_to_array[m] = name

        member_model = next(
            (
                all_disks[m]["model"]
                for m in members
                if all_disks.get(m, {}).get("model")
            ),
            None,
        )

        degraded_raw = "1"
        try:
            degraded_raw = Path(f"/sys/block/{name}/md/degraded").read_text().strip()
        except OSError:
            pass
        degraded = degraded_raw != "0"

        mdmon_ok = service_active("mdmonitor")
        smartd_ok = service_active("smartd")

        bad_members = [m for m in members if not smart_health(m)[0]]

        healthy = not degraded and mdmon_ok and smartd_ok and not bad_members
        detail = (
            f"degraded={degraded_raw}, mdmonitor={'up' if mdmon_ok else 'down'}, "
            f"smartd={'up' if smartd_ok else 'down'}"
        )
        if bad_members:
            detail += f", SMART FAILED on: {', '.join(bad_members)}"

        model_str = f"{member_model} " if member_model else ""
        description = (
            f"{entry['level'].upper()}, {len(members)}x {model_str}"
            f"({entry['size']}) [{', '.join(members)}], mdadm /dev/{name}"
        )

        physical_devices.append(
            {
                "id": name,
                "type": "raid",
                "description": description,
                "healthy": healthy,
                "health_detail": detail,
            }
        )

    filesystems: list[dict] = []
    for fs in filesystems_raw.values():
        mount_point = fs["mount_point"]
        try:
            df_out = run(
                "df", "--output=size,avail", "-B1", mount_point
            ).strip().splitlines()
            size_bytes, avail_bytes = df_out[-1].split()
            total_gb = round(int(size_bytes) / 1e9, 1)
            free_gb = round(int(avail_bytes) / 1e9, 1)
        except Exception:
            total_gb = free_gb = 0.0

        filesystems.append(
            {
                "id": fs_id_for(mount_point),
                "parent_id": fs["parent_id"],
                "mount_point": mount_point,
                "fs_type": fs["fs_type"],
                "total_gb": total_gb,
                "free_gb": free_gb,
            }
        )

    return physical_devices, filesystems, member_to_array


# --- Reporting ---------------------------------------------------------------


def send_report(state: PairingState, event: dict | None = None) -> None:
    if not state.paired:
        log.warning("send_report called while unpaired, skipping")
        return

    physical_devices, filesystems, member_to_array = gather_topology()
    payload: dict = {"physical_devices": physical_devices, "filesystems": filesystems}

    if event:
        raw_id = event["raw_id"]
        physical_id = member_to_array.get(raw_id, raw_id)
        payload["event"] = {"physical_id": physical_id, "message": event["message"]}

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        state.webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {state.token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:
        log.error("failed to post report to %s: %s", state.webhook_url, exc)


def heartbeat_loop(state: PairingState, settings: Settings) -> None:
    while True:
        time.sleep(settings.heartbeat_interval_seconds)
        if state.paired:
            send_report(state)


# --- HTTP API -----------------------------------------------------------------


def make_handler(state: PairingState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            log.info("%s - %s", self.client_address[0], fmt % args)

        def _send_json(self, status: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/info":
                self._send_json(
                    200, {"hostname": socket.gethostname(), "version": AGENT_VERSION}
                )
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/register":
                try:
                    data = self._read_json_body()
                    webhook_url = data["webhook_url"]
                    token = data["token"]
                except Exception:
                    self._send_json(400, {"error": "expected {webhook_url, token}"})
                    return
                if state.register(webhook_url, token):
                    log.info("paired with %s", webhook_url)
                    # Report immediately rather than making Home Assistant wait
                    # up to a full heartbeat interval to see anything at all.
                    threading.Thread(
                        target=send_report, args=(state,), daemon=True
                    ).start()
                    self._send_json(200, {"status": "paired"})
                else:
                    self._send_json(
                        409,
                        {
                            "error": "already paired; clear "
                            f"{STATE_FILE} and restart to re-pair"
                        },
                    )
                return

            if self.path == "/trigger-event":
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    self._send_json(403, {"error": "local callers only"})
                    return
                try:
                    data = self._read_json_body()
                    event = {"raw_id": data["raw_id"], "message": data["message"]}
                except Exception:
                    self._send_json(400, {"error": "expected {raw_id, message}"})
                    return
                if not state.paired:
                    self._send_json(503, {"error": "not paired with Home Assistant yet"})
                    return
                threading.Thread(
                    target=send_report, args=(state,), kwargs={"event": event}, daemon=True
                ).start()
                self._send_json(202, {"status": "accepted"})
                return

            self._send_json(404, {"error": "not found"})

    return Handler


def main() -> None:
    settings = Settings()
    state = PairingState()

    if state.paired:
        # Otherwise a service restart (or a system reboot) leaves Home
        # Assistant with stale data for up to a full heartbeat interval,
        # same as the pairing-time gap this whole fix addresses.
        threading.Thread(target=send_report, args=(state,), daemon=True).start()

    threading.Thread(
        target=heartbeat_loop, args=(state, settings), daemon=True
    ).start()

    server = ThreadingHTTPServer(("0.0.0.0", settings.port), make_handler(state))
    log.info("listening on 0.0.0.0:%d (paired=%s)", settings.port, state.paired)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
