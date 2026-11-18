"""Энкодер с кнопкой: sysfs GPIO, libgpiod или симуляция.

События, которые отдаёт poll():
    ("turn", +1/-1)  — поворот
    ("press",)       — кнопка нажата
    ("release",)     — кнопка отпущена
    ("clicks", n)    — закончился «клик-таймаут», накопилось n кликов
"""

import os
import time

# классическая таблица квадратурного декодера: индекс = (prev<<2)|cur
_QTABLE = (0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0)


class _SysfsPins:
    def __init__(self, lines):
        self.fds = []
        for line in lines:
            path = "/sys/class/gpio/gpio%d/value" % int(line)
            if not os.path.exists(path):
                with open("/sys/class/gpio/export", "w") as f:
                    f.write(str(int(line)))
                time.sleep(0.05)
            with open("/sys/class/gpio/gpio%d/direction" % int(line), "w") as f:
                f.write("in")
            self.fds.append(os.open(path, os.O_RDONLY))

    def read(self):
        out = []
        for fd in self.fds:
            os.lseek(fd, 0, os.SEEK_SET)
            out.append(1 if os.read(fd, 1).strip() == b"1" else 0)
        return out


class _GpiodPins:
    def __init__(self, chip_name, lines):
        import gpiod  # python3-libgpiod

        self.chip = gpiod.Chip(chip_name)
        self.lines = self.chip.get_lines([int(x) for x in lines])
        self.lines.request(consumer="benderadio", type=gpiod.LINE_REQ_DIR_IN,
                           flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP)

    def read(self):
        return [int(v) for v in self.lines.get_values()]


class Encoder:
    def __init__(self, cfg=None, log=print, simulate=False):
        cfg = cfg or {}
        self.log = log
        self.pressed = False
        self._pins = None
        self._driver = str(cfg.get("driver", "sysfs")).lower()
        self.button_active_low = bool(cfg.get("button_active_low", True))
        self.debounce = float(cfg.get("debounce", 0.02))
        self.click_timeout = float(cfg.get("click_timeout", 0.35))
        self.hold_time = float(cfg.get("hold_time", 1.2))

        self._prev_state = None
        self._raw_btn = None
        self._btn_stable = False
        self._btn_change_at = 0.0
        self._clicks = 0
        self._clicks_deadline = 0.0
        self._press_at = 0.0
        self._hold_emitted = False
        self._turned_while_pressed = False
        self._events = []

        # симуляция: расписание событий
        self._simulate = simulate or self._driver == "sim"
        self._sim_t = 0.0
        self._sim_next = 1.0

        if not self._simulate:
            lines = [cfg.get("a", 6), cfg.get("b", 7), cfg.get("button", 1)]
            if self._driver == "gpiod":
                self._pins = _GpiodPins(cfg.get("chip", "gpiochip0"), lines)
            else:
                self._pins = _SysfsPins(lines)
            self.log("энкодер: %s, линии %s" % (self._driver, lines))

    # ------------------------------------------------------------------
    def read_raw(self):
        return self._pins.read()

    def poll(self, now=None):
        """Опрос; возвращает список событий с прошлого вызова."""
        now = time.monotonic() if now is None else now
        out = []

        if self._simulate:
            out.extend(self._sim_events(now))
            # «клики» в симуляции тоже идут через общий таймаут
        else:
            a, b, btn = self.read_raw()
            state = (a << 1) | b
            if self._prev_state is None:
                self._prev_state = state
            else:
                delta = _QTABLE[(self._prev_state << 2) | state]
                self._prev_state = state
                if delta:
                    if self.pressed:
                        # кнопку используют как «модификатор» (выбор станции),
                        # поэтому длинное удержание не должно включать режим яркости
                        self._turned_while_pressed = True
                    out.append(("turn", delta))

            # кнопка: активна низким уровнем
            raw = btn == 0 if self.button_active_low else btn == 1
            if self._raw_btn is None:
                self._raw_btn = raw
                self._btn_stable = raw
            elif raw != self._raw_btn:
                self._raw_btn = raw
                self._btn_change_at = now
            elif raw != self._btn_stable and (now - self._btn_change_at) >= self.debounce:
                self._btn_stable = raw
                if raw:
                    self.pressed = True
                    self._press_at = now
                    self._hold_emitted = False
                    self._turned_while_pressed = False
                    out.append(("press",))
                else:
                    self.pressed = False
                    self._hold_emitted = False
                    out.append(("release",))
                    self._clicks += 1
                    self._clicks_deadline = now + self.click_timeout

            # длинное удержание -> режим яркости (если энкодер при этом не крутили)
            if (self.pressed and not self._hold_emitted and not self._turned_while_pressed
                    and (now - self._press_at) >= self.hold_time):
                self._hold_emitted = True
                out.append(("hold",))

        if self._clicks and now >= self._clicks_deadline:
            out.append(("clicks", self._clicks))
            self._clicks = 0

        return out

    # ------------------------------------------------------------------
    def _sim_events(self, now):
        """Сценарий для --simulate: повороты, клики, зажатая кнопка."""
        out = []
        if now < self._sim_next:
            return out
        self._sim_t += 1.0
        self._sim_next = now + 0.35

        step = int(self._sim_t) % 12
        if step in (1, 2, 3, 4, 5):
            out.append(("turn", 1))
        elif step == 6:
            out.append(("clicks", 1))
        elif step == 7:
            out.append(("turn", 1))
            self.pressed = True
            out.append(("press",))
        elif step == 8:
            out.append(("turn", 1))
            self.pressed = False
            out.append(("release",))
        elif step == 9:
            out.append(("clicks", 2))
        elif step == 10:
            out.append(("clicks", 3))
        return out

    def close(self):
        self._pins = None
