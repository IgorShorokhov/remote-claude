#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C.UTF-8
CHANGED=0
note() { echo "[20-base] $*"; CHANGED=1; }

APT="apt-get -o DPkg::Lock::Timeout=300 -y"
pkg_ok() { [[ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" == *"install ok installed"* ]]; }

$APT update >/dev/null
UPG=$(apt-get -s full-upgrade | grep -c '^Inst ' || true)
if [ "$UPG" -gt 0 ]; then
  note "обновление пакетов: $UPG"
  $APT -o Dpkg::Options::=--force-confold full-upgrade >/dev/null
fi

MISSING=""
for p in curl ca-certificates gnupg ufw unattended-upgrades locales; do
  pkg_ok "$p" || MISSING="$MISSING $p"
done
if [ -n "$MISSING" ]; then
  note "установка пакетов:$MISSING"
  $APT install $MISSING >/dev/null
fi

for l in en_US.UTF-8 ru_RU.UTF-8; do
  if ! locale -a | grep -qix "${l/UTF-8/utf8}"; then
    note "локаль $l"
    sed -i "s/^# *$l UTF-8/$l UTF-8/" /etc/locale.gen
    grep -q "^$l UTF-8" /etc/locale.gen || echo "$l UTF-8" >> /etc/locale.gen
    locale-gen >/dev/null
  fi
done
if ! grep -qx 'LANG=en_US.UTF-8' /etc/default/locale 2>/dev/null; then
  note "LANG=en_US.UTF-8"
  update-locale LANG=en_US.UTF-8
fi

WANT_SIZE=4294967296
swap_sig_ok() { [ "$(blkid -p -o value -s TYPE /swapfile 2>/dev/null)" = "swap" ]; }
if [ -e /swapfile ]; then
  if [ ! -f /swapfile ] || [ "$(stat -c %s /swapfile)" != "$WANT_SIZE" ] || ! swap_sig_ok; then
    note "swap: пересоздание /swapfile (размер или сигнатура не те)"
    if swapon --show=NAME --noheadings | grep -qx /swapfile; then swapoff /swapfile; fi
    rm -f /swapfile
  elif [ "$(stat -c %a /swapfile)" != "600" ]; then
    note "swap: права /swapfile -> 600"
    chmod 600 /swapfile
  fi
fi
if [ ! -f /swapfile ]; then
  note "swap 4G: создание"
  fallocate -l 4G /swapfile || { note "fallocate не удался, использую dd"; rm -f /swapfile; dd if=/dev/zero of=/swapfile bs=1M count=4096 status=none; }
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
fi
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  note "swapon"
  swapon /swapfile
fi
if ! grep -qE '^/swapfile[[:space:]]' /etc/fstab; then
  note "fstab: swap"
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
SYSCTL=/etc/sysctl.d/99-agent-swap.conf
if [ "$(cat $SYSCTL 2>/dev/null)" != "vm.swappiness=10" ]; then
  note "swappiness=10"
  echo "vm.swappiness=10" > $SYSCTL
fi
if [ "$(sysctl -n vm.swappiness)" != "10" ]; then
  sysctl -q -p $SYSCTL
fi

AU=/etc/apt/apt.conf.d/20auto-upgrades
WANT='APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";'
if [ "$(cat $AU 2>/dev/null)" != "$WANT" ]; then
  note "20auto-upgrades"
  echo "$WANT" > $AU
fi
AR=/etc/apt/apt.conf.d/52agent-no-autoreboot
WANT='Unattended-Upgrade::Automatic-Reboot "false";'
if [ "$(cat $AR 2>/dev/null)" != "$WANT" ]; then
  note "автоперезагрузка выключена"
  echo "$WANT" > $AR
fi
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

[ -f /var/run/reboot-required ] && echo "[20-base] нужна перезагрузка (шаг 3)"
[ "$CHANGED" = 0 ] && echo "[20-base] уже сделано, изменений нет"
exit 0
