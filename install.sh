#!/usr/bin/env bash
# Установка BendeRadio на Orange Pi One (Armbian/Debian).
# Запускать от root:  sudo ./install.sh
set -euo pipefail

PREFIX="${PREFIX:-/opt/benderadio}"
CONFDIR="${CONFDIR:-/etc/benderadio}"
STATEDIR="${STATEDIR:-/var/lib/benderadio}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== 1. пакеты =="
apt-get update
apt-get install -y ffmpeg alsa-utils python3
# необязательные, но полезные: быстрый SPI и быстрая программная громкость
apt-get install -y python3-spidev || echo "  python3-spidev не поставился — будет sysfs-режим (это нормально)"
apt-get install -y python3-numpy   || echo "  python3-numpy не поставился — громкость пойдёт через amixer"

echo "== 2. файлы =="
install -d "$PREFIX/benderadio" "$CONFDIR" "$STATEDIR"
install -m 755 "$HERE/run.py" "$PREFIX/run.py"
for f in "$HERE"/benderadio/*.py; do
    install -m 644 "$f" "$PREFIX/benderadio/$(basename "$f")"
done

if [ -f "$CONFDIR/config.json" ]; then
    echo "  конфиг уже есть: $CONFDIR/config.json (не перезаписываю)"
else
    install -m 600 "$HERE/config.json" "$CONFDIR/config.json"
    echo "  конфиг установлен: $CONFDIR/config.json"
fi

echo "== 3. systemd =="
install -m 644 "$HERE/benderadio.service" /etc/systemd/system/benderadio.service
systemctl daemon-reload
systemctl enable benderadio

cat <<EOF

Готово. Что дальше:

 1. Проверьте пины и звуковую карту в $CONFDIR/config.json
    (aplay -l — узнать card/device; amixer scontrols — узнать громкость).

 2. Проверьте программу без железа:
      python3 $PREFIX/run.py --config $CONFDIR/config.json --selftest
      python3 $PREFIX/run.py --config $CONFDIR/config.json --simulate --duration 5

 3. Запуск как сервиса:
      systemctl start benderadio
      journalctl -u benderadio -f

Для матрицы через аппаратный SPI включите overlay в /boot/armbianEnv.txt:
      overlays=spi-spidev
      param_spidev_spi_bus=0
и перезагрузитесь, затем в конфиге поставьте  "matrix": { "driver": "spi", ... }
EOF
