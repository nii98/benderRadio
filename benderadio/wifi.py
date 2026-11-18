"""Подключение к Wi-Fi по SSID/паролю из config.json.

Работает через NetworkManager (nmcli), если он есть (Armbian по умолчанию),
иначе через wpa_supplicant. Ошибки не фатальны: если сеть не поднялась,
программа продолжит крутить лицо и ждать сеть.
"""

import os
import shutil
import socket
import subprocess
import time


def _run(cmd, timeout=60):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=timeout)


def has_ip(iface):
    try:
        out = _run(["ip", "-4", "-o", "addr", "show", "dev", iface], timeout=5).stdout
    except Exception:
        return False
    return "inet " in out


def is_root():
    try:
        return os.geteuid() == 0
    except AttributeError:  # Windows — только для отладки
        return False


def ensure_connected(cfg=None, log=print, timeout=45):
    """Поднять Wi-Fi. Возвращает True, если интерфейс получил IP."""
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return True

    iface = str(cfg.get("interface", "wlan0"))
    if has_ip(iface):
        log("Wi-Fi уже подключён (%s)" % iface)
        return True

    ssid = cfg.get("ssid")
    psk = cfg.get("password", "")
    if not ssid:
        log("в конфиге нет wifi.ssid — сеть не настраиваю")
        return False

    log("подключаю Wi-Fi '%s' на %s" % (ssid, iface))

    if shutil.which("nmcli"):
        try:
            res = _run(["nmcli", "-w", "30", "device", "wifi", "connect", str(ssid),
                        "password", str(psk), "ifname", iface], timeout=50)
            log("nmcli: %s" % (res.stdout or "").strip().replace("\n", " ")[:200])
        except Exception as exc:
            log("nmcli не сработал: %s" % exc)
    elif shutil.which("wpa_supplicant") and shutil.which("wpa_passphrase"):
        conf = "/etc/wpa_supplicant/wpa_supplicant-%s.conf" % iface
        try:
            hashed = _run(["wpa_passphrase", str(ssid), str(psk)], timeout=10).stdout
            country = str(cfg.get("country", "RU"))
            text = (
                "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
                "update_config=1\ncountry=%s\n\n%s" % (country, hashed)
            )
            os.makedirs(os.path.dirname(conf), exist_ok=True)
            with open(conf, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(conf, 0o600)
            if shutil.which("systemctl"):
                _run(["systemctl", "restart", "wpa_supplicant@%s" % iface], timeout=30)
            else:
                _run(["wpa_supplicant", "-B", "-i", iface, "-c", conf], timeout=20)
            if shutil.which("dhclient"):
                _run(["dhclient", iface], timeout=30)
            elif shutil.which("udhcpc"):
                _run(["udhcpc", "-i", iface, "-b"], timeout=30)
            log("wpa_supplicant: конфиг %s записан" % conf)
        except PermissionError:
            log("нет прав записать %s — запустите от root или настройте сеть через nmtui" % conf)
            return False
        except Exception as exc:
            log("wpa_supplicant не сработал: %s" % exc)
    else:
        log("нет ни nmcli, ни wpa_supplicant — настройте Wi-Fi вручную (nmtui/armbian-config)")
        return False

    for _ in range(int(timeout)):
        if has_ip(iface):
            log("Wi-Fi подключён (%s)" % iface)
            return True
        time.sleep(1)

    log("Wi-Fi не поднялся за %s с" % timeout)
    return False


def internet_ok(host="1.1.1.1", port=53, timeout=3):
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except Exception:
        return False
