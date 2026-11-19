"""Основная логика BendeRadio для Orange Pi One.

Один поток: аудио-чанк (~20 мс) задаёт темп, между чанками обновляем лицо,
опрос энкодера, переподключение и сохранение настроек.
"""

import argparse
import json
import logging
import os
import signal
import sys
import time

import encoder
import face as face_mod
import font
import max7219
import noise
import player as player_mod
import settings as settings_mod
import vu
import wifi

log = logging.getLogger("benderadio")

DEFAULT_CONFIG = "config.json"


# ======================================================================
#  конфиг
# ======================================================================
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    radio = cfg.setdefault("radio", {})
    stations = radio.get("stations") or []
    norm = []
    for item in stations:
        if isinstance(item, str):
            norm.append({"name": item, "url": item})
        elif isinstance(item, dict) and item.get("url"):
            norm.append({"name": item.get("name") or item["url"], "url": item["url"]})
    if not norm:
        raise ValueError("в конфиге нет ни одной станции (radio.stations)")
    radio["stations"] = norm

    cfg.setdefault("audio", {})
    cfg.setdefault("matrix", {})
    cfg.setdefault("encoder", {})
    cfg.setdefault("ui", {})
    cfg.setdefault("storage", {})
    cfg.setdefault("wifi", {})
    return cfg


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _writable_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


# ======================================================================
#  приложение
# ======================================================================
class App:
    def __init__(self, cfg, simulate=False, log_fn=log.info):
        self.cfg = cfg
        self.simulate = simulate
        self.log = log_fn

        radio = cfg["radio"]
        self.stations = radio["stations"]
        self.vol_steps = int(radio.get("volume_steps", 21))

        self.state_path = cfg.get("storage", {}).get("state_file", "state.json")
        if not _writable_dir(os.path.dirname(os.path.abspath(self.state_path))):
            fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state.json")
            self.log("нет прав на %s — настройки будут в %s" % (self.state_path, os.path.abspath(fallback)))
            self.state_path = os.path.abspath(fallback)
        st = settings_mod.Settings(self.state_path, self.log).load()

        ui = cfg["ui"]
        self.state = bool(st.get("state", ui.get("state", True)))
        self.vol = int(_clamp(st.get("vol", radio.get("volume", 10)), 0, self.vol_steps))
        self.station = int(st.get("station", radio.get("station", 0))) % len(self.stations)
        self.mode = int(st.get("mode", ui.get("analyzer_mode", 0))) % 2
        self.bright_mouth = int(_clamp(st.get("bright_mouth", ui.get("brightness_mouth", 2)), 0, 16))
        self.bright_eyes = int(_clamp(st.get("bright_eyes", ui.get("brightness_eyes", 5)), 0, 16))
        self.threshold = int(_clamp(st.get("threshold", ui.get("noise_threshold", 20)), 0, 100))

        self.settings = settings_mod.Settings(self.state_path, self.log)

        self.matrix = max7219.create_matrix(cfg, self.log, simulate)
        face_cfg = dict(ui)
        face_cfg.setdefault("brightness_mouth", self.bright_mouth)
        face_cfg.setdefault("brightness_eyes", self.bright_eyes)
        face_cfg.setdefault("analyzer_mode", self.mode)
        self.face = face_mod.Face(self.matrix, face_cfg, self.log)
        self.face.set_brightness(self.bright_mouth, self.bright_eyes)
        self.face.set_mode(self.mode)

        level_cfg = dict(ui.get("level", {}))
        level_cfg["threshold"] = self.threshold
        self.level = vu.AudioLevel(level_cfg)

        self.encoder = encoder.Encoder(cfg["encoder"], self.log, simulate)
        if simulate:
            self.player = player_mod.FakePlayer(cfg["audio"], self.log)
        else:
            self.player = player_mod.Player(cfg["audio"], self.log)
        self.player.set_volume(self.vol if self.state else 0, self.vol_steps)

        self._running = True
        self._dirty = False
        self._next_save = 0.0
        self._next_retry = 0.0
        self._next_wifi_try = 0.0
        self._station_pending = False
        self._edit = None            # None | "mouth" | "eyes"
        self._edit_until = 0.0
        self._held = False           # был long-press: следующий «clicks» игнорируем
        self.current_url = None

    # ------------------------------------------------------------------
    def _save_state(self):
        data = {
            "state": self.state,
            "vol": self.vol,
            "station": self.station,
            "mode": self.mode,
            "bright_mouth": self.bright_mouth,
            "bright_eyes": self.bright_eyes,
            "threshold": self.threshold,
        }
        self.settings.save(data)
        self._dirty = False

    def _touch(self):
        self._dirty = True

    def _apply_volume(self):
        self.player.set_volume(self.vol if self.state else 0, self.vol_steps)

    # ------------------------------------------------------------------
    def _play_station(self, now):
        url = self.stations[self.station]["url"]
        self.log("станция %d/%d: %s" % (self.station + 1, len(self.stations), self.stations[self.station]["name"]))
        try:
            self.player.play(url)
            self.current_url = url
            self._next_retry = 0.0
            return True
        except Exception as exc:
            self.log("не удалось запустить поток: %s" % exc)
            self.current_url = url
            self._next_retry = now + 5.0
            return False

    # ------------------------------------------------------------------
    def _handle_events(self, events, now):
        for ev in events:
            kind = ev[0]

            if kind == "turn":
                step = ev[1]
                if self._edit is not None:
                    if self._edit == "mouth":
                        self.bright_mouth = int(_clamp(self.bright_mouth + step, 0, 16))
                        self.face.set_brightness(self.bright_mouth, self.bright_eyes)
                        self.face.show_value("b", self.bright_mouth)
                    else:
                        self.bright_eyes = int(_clamp(self.bright_eyes + step, 0, 16))
                        self.face.set_brightness(self.bright_mouth, self.bright_eyes)
                        self.face.show_value("o", self.bright_eyes)
                    self._edit_until = now + 8.0
                    self._touch()
                elif self.encoder.pressed:
                    self.station = int((self.station + step) % len(self.stations))
                    self.face.show_value("s", self.station)
                    self._station_pending = True
                    self._touch()
                else:
                    self.vol = int(_clamp(self.vol + step, 0, self.vol_steps))
                    self._apply_volume()
                    self.face.angry()
                    self.face.show_value("v", self.vol)
                    self._touch()

            elif kind == "hold":
                self._held = True
                if self._edit is None:
                    self._edit = "mouth"
                    self.face.show_value("b", self.bright_mouth)
                elif self._edit == "mouth":
                    self._edit = "eyes"
                    self.face.show_value("o", self.bright_eyes)
                else:
                    self._edit = None
                self._edit_until = now + 8.0

            elif kind == "release":
                if self._station_pending:
                    self._station_pending = False
                    self._play_station(now)

            elif kind == "clicks":
                count = ev[1]
                if self._held:
                    # это «клик» от того же длинного нажатия — не считаем
                    self._held = False
                    continue
                if self._edit is not None:
                    self._edit = None
                    self.face.set_state(self.state)
                    continue
                if count == 1:
                    self.state = not self.state
                    self._apply_volume()
                    self.face.set_state(self.state)
                    self._touch()
                elif count == 2:
                    self.mode = (self.mode + 1) % 2
                    self.face.set_mode(self.mode)
                    self._touch()
                elif count >= 3:
                    self.threshold = int(self.face.set_threshold_hint(self.level.take_max()))
                    self.level.set_threshold(self.threshold)
                    self.log("порог шума: %d" % self.threshold)
                    self.face.show_value("E", self.threshold)
                    self._touch()

    # ------------------------------------------------------------------
    def run(self, duration=None):
        self.log("=== BendeRadio (Orange Pi One) ===")
        self.log("станций: %d, громкость: %d/%d, звук: %s" % (
            len(self.stations), self.vol, self.vol_steps, "вкл" if self.state else "выкл"))

        self.face.init()
        self.face.set_state(self.state)

        if not self.simulate:
            if self.cfg["wifi"].get("connect_on_start", True):
                wifi.ensure_connected(self.cfg["wifi"], self.log)

        now = time.monotonic()
        self._play_station(now)
        self._next_wifi_try = now + 30.0

        started = time.monotonic()
        while self._running:
            now = time.monotonic()
            if duration is not None and (now - started) >= duration:
                break

            # --- звук ---
            pcm = b""
            if self.player.alive():
                pcm = self.player.pump()
            level_vol = self.level.update(pcm) if pcm else int(self.level.vol)

            # --- лицо ---
            self.face.step(level_vol if self.state else 0, self.level.pulse(),
                           self.encoder.pressed, now)

            # --- энкодер ---
            self._handle_events(self.encoder.poll(now), now)

            # --- переподключение ---
            if not self.player.alive() and now >= self._next_retry:
                self.log("поток не идёт — переподключаюсь")
                if not self.simulate and now >= self._next_wifi_try:
                    self._next_wifi_try = now + 30.0
                    if not wifi.internet_ok():
                        self.log("сети нет — пробую поднять Wi-Fi")
                        wifi.ensure_connected(self.cfg["wifi"], self.log, timeout=20)
                self._play_station(now)
                self._next_retry = now + 5.0

            # --- выход из режима яркости по таймауту ---
            if self._edit is not None and now >= self._edit_until:
                self._edit = None
                self.face.set_state(self.state)

            # --- сохранение настроек не чаще раза в 5 с ---
            if self._dirty and now >= self._next_save:
                self._next_save = now + 5.0
                self._save_state()

            if not self.player.alive():
                time.sleep(0.02)

        self.shutdown()
        return 0

    def shutdown(self):
        self.log("остановка")
        try:
            self.player.close()
        except Exception:
            pass
        try:
            self.encoder.close()
        except Exception:
            pass
        try:
            self.matrix.set_brightness([0] * self.matrix.modules)
            self.matrix.close()
        except Exception:
            pass
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, signal.SIG_DFL)
            except Exception:
                pass


# ======================================================================
#  самопроверка (можно гонять на любом компьютере с Python 3)
# ======================================================================
def selftest(cfg_path):
    import array
    import math

    results = []

    def check(name, cond, extra=""):
        results.append(bool(cond))
        print(("  OK   " if cond else "  FAIL ") + name + ((" — " + str(extra)) if extra else ""))

    def skip(name, why):
        print("  SKIP " + name + " — " + why)

    print("== самопроверка BendeRadio ==")

    cfg = load_config(cfg_path)
    check("конфиг читается", bool(cfg))
    stations = cfg["radio"]["stations"]
    check("станции заданы", len(stations) >= 1, "%d шт." % len(stations))
    check("у станций http(s) адреса", all(s["url"].startswith("http") for s in stations))
    check("Wi-Fi ssid из конфига", bool(cfg["wifi"].get("ssid")), cfg["wifi"].get("ssid"))

    vals = [noise.inoise8(x * 37, y * 53) for x in range(32) for y in range(32)]
    check("noise: диапазон 0..255", min(vals) >= 0 and max(vals) <= 255)
    check("noise: не константа", (max(vals) - min(vals)) > 60, "размах %d" % (max(vals) - min(vals)))

    check("шрифт: ширина 'v10'", font.text_width("v10") == 3 * 6 - 1)
    check("шрифт: глифы цифр", all(font.glyph(d) for d in "0123456789"))
    check("шрифт: глифы b/o/s/v", all(font.glyph(c) for c in "bosv"))

    m = max7219.SimMatrix(5, quiet=True)
    m.init_chip()
    m.rect(0, 0, 39, 7, True)
    m.flush()
    check("матрица: 5 модулей", m.modules == 5)
    check("матрица: строка = 5 байт", len(m.row_bytes(0)) == 5)
    check("матрица: заливка реально залила", all(b == 0xFF for b in m.row_bytes(3)))
    m.set_brightness([3, 3, 3, 9, 9])
    check("матрица: яркость по модулям", m.brightness == [3, 3, 3, 9, 9])

    f = face_mod.Face(m, {}, lambda *a: None)
    f.init()
    f.search()
    f.step(60, True, False, time.monotonic() + 1.0)
    f.show_value("v", 12)
    f.set_state(False)
    f.set_state(True)
    check("лицо: тики без исключений", True)

    def pcm(amp, n=1764):
        buf = array.array("h")
        for i in range(n):
            v = int(amp * math.sin(i * 0.1))
            buf.append(v)
            buf.append(v)
        return buf.tobytes()

    lvl = vu.AudioLevel({"threshold": 10})
    quiet = lvl.update(pcm(0))
    loud = 0
    for _ in range(12):
        loud = lvl.update(pcm(9000))
    check("VU: тишина = 0", quiet == 0, quiet)
    check("VU: громкий сигнал > 40", loud > 40, loud)

    lvl2 = vu.AudioLevel({"threshold": 0, "pulse_max": 50, "pulse_min": 30, "release": 0.6})
    for _ in range(12):
        lvl2.update(pcm(20000))
    for _ in range(12):
        lvl2.update(pcm(300))
    check("VU: детектор бита", lvl2.pulse() is True)

    import shutil as _shutil
    tmp = None
    for candidate in (os.path.join(".", "selftest-state"), os.path.join(os.path.expanduser("~"), ".benderadio-selftest")):
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".probe")
            with open(probe, "w") as pf:
                pf.write("ok")
            os.remove(probe)
            tmp = candidate
            break
        except Exception:
            continue
    if tmp is None:
        skip("настройки: запись/чтение", "нет папки с правом записи (на самой плате такого не будет)")
    else:
        st = settings_mod.Settings(os.path.join(tmp, "state.json"), lambda *a: None)
        st.save({"vol": 7, "station": 2})
        check("настройки: запись/чтение", st.load().get("vol") == 7)
        _shutil.rmtree(tmp, ignore_errors=True)

    enc = encoder.Encoder({}, lambda *a: None, simulate=True)
    events = []
    base = time.monotonic()
    for i in range(80):
        events.extend(enc.poll(base + i * 0.4))
    check("энкодер (sim): генерирует события", len(events) > 5, "%d событий" % len(events))
    check("энкодер (sim): есть повороты", any(e[0] == "turn" for e in events))

    # тестовые режимы в симуляции — чтобы они не падали на реальной плате
    import testmode
    quiet_cfg = json.loads(json.dumps(cfg))
    quiet_cfg.setdefault("matrix", {})["quiet"] = True
    broken = []
    for target in ("matrix", "eyes", "encoder", "all"):
        try:
            testmode.run(target, quiet_cfg, log=lambda *a: None, duration=0.3, simulate=True)
        except Exception as exc:
            broken.append("%s: %s" % (target, exc))
    check("тестовые режимы запускаются", not broken, "; ".join(broken))

    ok = all(results)
    print("== итог: %s ==" % ("ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ"))
    return 0 if ok else 1


# ======================================================================
def main(argv=None):
    parser = argparse.ArgumentParser(description="BendeRadio для Orange Pi One")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="путь к config.json")
    parser.add_argument("--selftest", action="store_true", help="прогнать самопроверку и выйти")
    parser.add_argument("--simulate", action="store_true", help="без железа: матрица в консоль, звук синтетический")
    parser.add_argument("--test", choices=("diag", "eyes", "encoder", "matrix", "all"), default=None,
                        help="тест железа без Wi-Fi и звука: diag / глаза / энкодер / матрица / всё")
    parser.add_argument("--brightness", type=int, default=None,
                        help="яркость матрицы 0..15 (быстро проверить, не в яркости ли дело)")
    parser.add_argument("--driver", choices=("sysfs", "spi", "sim"), default=None,
                        help="принудительный драйвер матрицы (перекрывает config.json)")
    parser.add_argument("--cs", type=int, default=None, help="GPIO-линия CS (перекрывает config.json)")
    parser.add_argument("--din", type=int, default=None, help="GPIO-линия DIN")
    parser.add_argument("--clk", type=int, default=None, help="GPIO-линия CLK")
    parser.add_argument("--duration", type=float, default=None, help="остановиться через N секунд")
    parser.add_argument("--state", default=None, help="переопределить файл настроек")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    cfg["_path"] = args.config
    if args.state:
        cfg.setdefault("storage", {})["state_file"] = args.state

    # быстрые перекрытия для проверки железа без правки конфига
    matrix_cfg = cfg.setdefault("matrix", {})
    if args.driver:
        matrix_cfg["driver"] = args.driver
    if args.cs is not None or args.din is not None or args.clk is not None:
        gpio_cfg = matrix_cfg.setdefault("gpio", {})
        if args.cs is not None:
            gpio_cfg["cs"] = args.cs
        if args.din is not None:
            gpio_cfg["din"] = args.din
        if args.clk is not None:
            gpio_cfg["clk"] = args.clk
    if args.brightness is not None:
        bright = max(0, min(15, args.brightness))
        matrix_cfg["brightness"] = [bright] * int(matrix_cfg.get("modules", 5))
        matrix_cfg["test_brightness"] = bright  # его используют тестовые режимы
        ui_cfg = cfg.setdefault("ui", {})
        ui_cfg["brightness_mouth"] = bright
        ui_cfg["brightness_eyes"] = bright

    level = (args.log_level or cfg.get("logging", {}).get("level") or "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    if args.selftest:
        return selftest(args.config)

    if args.test:
        import testmode
        log.info("тестовый режим: %s (без Wi-Fi и без звука)", args.test)
        try:
            return testmode.run(args.test, cfg, log=log.info,
                                duration=args.duration, simulate=args.simulate)
        except KeyboardInterrupt:
            print()
            log.info("прервано пользователем")
            return 0

    app = App(cfg, simulate=args.simulate, log_fn=log.info)

    def _stop(signum, frame):
        log.info("получен сигнал %s — выходим", signum)
        app._running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except Exception:
            pass

    return app.run(duration=args.duration)


if __name__ == "__main__":
    sys.exit(main())
