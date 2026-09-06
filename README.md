# HA Server Monitor agent

An Arch Linux package (`ha-server-monitor-agent`) that reports a host's storage
topology and health to a paired `ha_server_monitor` Home Assistant integration
(see [ha-linux-monitoring/ha-server-monitor](https://github.com/ha-linux-monitoring/ha-server-monitor)).
Runs on the machine that actually owns the disks, not on the Home Assistant host.

**Ships with zero prior knowledge of Home Assistant.** It advertises itself
on the LAN via mDNS (Avahi); the HA integration discovers it (or is pointed
at its host/IP manually as a fallback) and *hands the agent* its webhook URL
plus a token generated at that moment — a proper pairing handshake, not a
URL copy-pasted into a config file by hand.

Generic by design: introspects whatever storage topology actually exists on
the host it runs on (via `lsblk`, `/proc/mdstat`-backed sysfs, `smartctl`,
`df`) — no hardcoded device list, and installable on any Linux host with
software RAID (or none at all).

## How pairing works

1. `ha-server-monitor-agentd` starts up unpaired, listening on `:8477`
   (`GET /info`, `POST /register`, `POST /trigger-event`), and is advertised
   via Avahi as `_ha-server-monitor._tcp.local.`.
2. In Home Assistant, adding the `HA Server Monitor` integration either
   auto-discovers the agent via mDNS or takes a manual host/IP entry.
3. HA generates a random token, registers its own webhook, then calls this
   agent's `POST /register` with `{"webhook_url": ..., "token": ...}`.
4. The agent persists that to `/var/lib/ha-server-monitor-agent/state.json`
   (mode 600) and starts pushing heartbeats (every
   `HEARTBEAT_INTERVAL_SECONDS`, default 15 min) and mdadm/smartd-triggered
   events to that URL, with `Authorization: Bearer <token>` on every push.

**Registration is accepted exactly once.** Once paired, further `/register`
calls get `409`. To re-pair (e.g. moved to a new HA instance): stop the
service, `rm /var/lib/ha-server-monitor-agent/state.json`, start it again.

**Security note, stated plainly**: this does add one listening port that
didn't exist before (previous iterations of this agent were pure outbound
push, zero attack surface). `/register` is reachable by anything on the LAN
until the first successful pairing closes that window. This matches the
trust model Home Assistant's own zeroconf-discovered integrations already
use elsewhere, but if you want to harden it further, firewall port 8477 to
only accept connections from your HA host's IP.

## Contents

- `ha-server-monitor-agentd.py` — the daemon (stdlib only, no pip dependencies).
- `mdadm-ha-alert.sh` / `smartd-ha-alert.sh` — thin wrappers that POST to the
  daemon's own local `/trigger-event` (mdadm's `PROGRAM` directive and
  smartd's `-M exec` call these).
- `ha-server-monitor-agent.service` — systemd unit for the daemon.
- `ha-server-monitor-agent-mdns.service` — Avahi service definition (not a
  systemd unit, despite the extension — Avahi requires it).
- `agent.conf.example` — becomes the real `/etc/ha-server-monitor-agent/agent.conf`
  default (`PORT`, `HEARTBEAT_INTERVAL_SECONDS`); a `pacman` `backup=()` entry,
  so your edits survive package upgrades (`.pacnew` if the default changes).
- `PKGBUILD` / `ha-server-monitor-agent.install` — the Arch package itself.
- `publish.sh` — builds the package **on the target host, from this
  project's GitHub source** (not from a local checkout) and adds it to a
  custom local pacman repository there. Installs nothing itself.

## Installing

This ships as a real pacman package via a small custom repository living on
the target host, not via a one-off `makepkg -si`/`scp` — so install/upgrade
is standard `pacman`, and works the same way through `paru` or any other
AUR helper (they just defer to pacman for anything a configured repo
already provides).

**One-time setup, on the target host:**

```
./publish.sh <user>@<host>
```

clones (or updates) `ha-linux-monitoring/ha-server-monitor-agent` from
GitHub into `~/ha-server-monitor-agent-src` on the host, builds it with
`makepkg -f`, and publishes the resulting package into
`/srv/pacman-repo/ha-linux-monitoring/` (override with a second argument).
Then register that repo in the host's `/etc/pacman.conf`:

```
[ha-linux-monitoring]
SigLevel = Optional TrustAll
Server = file:///srv/pacman-repo/ha-linux-monitoring
```

(`TrustAll` since this is an unsigned personal repo — fine for a
local-only, non-networked repo.) `sudo pacman -Sy` to pick it up, then:

```
sudo pacman -S ha-server-monitor-agent
```

The package's `.install` scriptlet enables and starts both `avahi-daemon`
and `ha-server-monitor-agent.service` for you on install, and restarts
`ha-server-monitor-agent.service` on upgrade (this deliberately departs from
the usual Arch packaging convention of never auto-enabling services, by
request — if you'd rather manage that yourself, comment out the
`systemctl` calls in `ha-server-monitor-agent.install`). Removing the package
(`pacman -R`) stops and disables `ha-server-monitor-agent.service`; it leaves
`avahi-daemon` alone since other things may depend on it.

Just add the `HA Server Monitor` integration in Home Assistant (see
[ha-linux-monitoring/ha-server-monitor](https://github.com/ha-linux-monitoring/ha-server-monitor))
to discover and pair it.

**Shipping an update**: bump `pkgver`/`pkgrel` in `PKGBUILD` as part of the
change that motivates it, commit, push to `main`, then re-run
`./publish.sh <user>@<host>` — the repo now offers the new version, and
`sudo pacman -Syu` on the host picks it up like any other package update.
Publishing the same version twice (no source change) is a no-op — the repo
only ever advertises a new version when `pkgver`/`pkgrel` actually changed.

## Payload contract

One JSON POST per report to the paired webhook URL, `Authorization: Bearer
<token>` header on every request:

```json
{
  "physical_devices": [
    {"id": "md127", "type": "raid", "description": "...", "healthy": true, "health_detail": "..."}
  ],
  "filesystems": [
    {"id": "mnt_raid", "parent_id": "md127", "mount_point": "/mnt/raid", "fs_type": "ext4", "total_gb": 8171.0, "free_gb": 207.6}
  ],
  "event": {"physical_id": "md127", "message": "mdadm on host: NewArray /dev/md127"}
}
```

`physical_devices`/`filesystems` are sent on every push (heartbeat or event
alike) — the agent always re-introspects the full topology. `event` is only
present on mdadm/smartd-triggered pushes; it's what latches a physical
device's Alert entity on in Home Assistant (see
[ha-linux-monitoring/ha-server-monitor](https://github.com/ha-linux-monitoring/ha-server-monitor)
for what the integration does with all this).
