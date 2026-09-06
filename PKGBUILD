# Maintainer: Yevhen <friedpuppet@pm.me>
pkgname=ha-server-monitor-agent
pkgver=1.0.1
pkgrel=1
pkgdesc="Storage-monitoring agent that discovers and pairs with a Home Assistant instance, then reports RAID/SMART/disk-space topology to it"
arch=('any')
license=('custom')
options=('!debug')
depends=('python' 'smartmontools' 'avahi')
optdepends=('mdadm: for RAID array health monitoring hooks')
backup=('etc/ha-server-monitor-agent/agent.conf'
        'etc/avahi/services/ha-server-monitor-agent-mdns.service')
install=ha-server-monitor-agent.install
source=(
    ha-server-monitor-agentd.py
    mdadm-ha-alert.sh
    smartd-ha-alert.sh
    ha-server-monitor-agent.service
    ha-server-monitor-agent-mdns.service
    agent.conf.example
)
# Local sources versioned alongside this PKGBUILD, not fetched over the
# network -- nothing to verify a checksum against.
sha256sums=('SKIP'
            'SKIP'
            'SKIP'
            'SKIP'
            'SKIP'
            'SKIP')

package() {
    install -Dm755 "$srcdir/ha-server-monitor-agentd.py" \
        "$pkgdir/usr/bin/ha-server-monitor-agentd.py"
    install -Dm755 "$srcdir/mdadm-ha-alert.sh" \
        "$pkgdir/usr/bin/mdadm-ha-alert.sh"
    install -Dm755 "$srcdir/smartd-ha-alert.sh" \
        "$pkgdir/usr/bin/smartd-ha-alert.sh"

    install -Dm644 "$srcdir/ha-server-monitor-agent.service" \
        "$pkgdir/usr/lib/systemd/system/ha-server-monitor-agent.service"

    # Avahi on Arch only ever watches /etc/avahi/services -- there is no
    # /usr/lib/avahi/services convention here (unlike systemd's /usr/lib
    # vs /etc split), confirmed against both avahi-daemon's own startup log
    # ("No service file found in /etc/avahi/services") and `pacman -Ql avahi`,
    # which only ever provisions the /etc path. Marked in backup=() since it's
    # package-provided content living in /etc only because that's where the
    # consumer looks, not because it's meant to be hand-edited independently
    # of agent.conf's PORT.
    install -Dm644 "$srcdir/ha-server-monitor-agent-mdns.service" \
        "$pkgdir/etc/avahi/services/ha-server-monitor-agent-mdns.service"

    # Ships as the live default config, not just an example -- backup=()
    # above tells pacman to .pacnew it on future upgrades if the admin has
    # edited it.
    install -Dm644 "$srcdir/agent.conf.example" \
        "$pkgdir/etc/ha-server-monitor-agent/agent.conf"

    # Runtime pairing state (webhook_url/token) lives here once paired --
    # daemon-written, not shipped with any content.
    install -d -m700 "$pkgdir/var/lib/ha-server-monitor-agent"
}
