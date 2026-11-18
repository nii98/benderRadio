"""Лицо Бендера: порт ui.cpp с ESP32-версии на Python + MAX7219.

Раскладка та же: слева 3 модуля (24 пикселя) — «рот»-анализатор,
справа 2 модуля (16 пикселей) — «глаза».
"""

import time

import noise

ANALYZ_WIDTH = 24


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _map(v, in_lo, in_hi, out_lo, out_hi):
    if in_hi == in_lo:
        return out_lo
    v = _clamp(v, min(in_lo, in_hi), max(in_lo, in_hi))
    return out_lo + (v - in_lo) * (out_hi - out_lo) // (in_hi - in_lo)


class Face:
    def __init__(self, matrix, cfg=None, log=print):
        cfg = cfg or {}
        self.m = matrix
        self.log = log

        self.bright_mouth = int(cfg.get("brightness_mouth", 2))
        self.bright_eyes = int(cfg.get("brightness_eyes", 5))
        self.mode = int(cfg.get("analyzer_mode", 0))
        self.enabled = bool(cfg.get("enabled", True))

        self.eye_interval = float(cfg.get("eye_interval", 0.15))
        self.analyzer_interval = float(cfg.get("analyzer_interval", 1.0))
        self.angry_timeout = float(cfg.get("angry_timeout", 0.8))
        self.search_interval = float(cfg.get("search_interval", 0.05))

        self._next_eye = 0.0
        self._next_analyzer = 0.0
        self._next_search = 0.0
        self._eyes_block_until = 0.0
        self._analyzer_block_until = 0.0
        self._angry_until = 0.0

        self._search_pos = 4
        self._search_dir = 1
        self._eye_pos = 0
        self._bar_hist = [0] * ANALYZ_WIDTH
        self._offs = 0

    # ------------------------------------------------------------ служебное
    def _upd_bright(self):
        self.m.set_brightness([self.bright_mouth] * 3 + [self.bright_eyes] * (self.m.modules - 3))

    def init(self):
        self.m.init_chip()
        self._upd_bright()
        self.set_state(self.enabled)

    def set_brightness(self, mouth=None, eyes=None):
        if mouth is not None:
            self.bright_mouth = int(_clamp(mouth, 0, 16))
        if eyes is not None:
            self.bright_eyes = int(_clamp(eyes, 0, 16))
        self._upd_bright()

    def set_mode(self, mode):
        self.mode = int(mode) % 2

    def set_threshold_hint(self, level_max):
        """3 клика: автоподстройка порога (аналог sound.getMax()*2/3)."""
        return int(_clamp(level_max * 2 / 3, 0, 100))

    def angry(self):
        self._angry_until = time.monotonic() + self.angry_timeout

    # ------------------------------------------------------------ рисование
    def _draw_eye(self, i):
        x = ANALYZ_WIDTH + i * 8
        self.m.rect(1 + x, 1, 6 + x, 6, True)
        self.m.line_v(0 + x, 2, 5, False)
        self.m.line_v(7 + x, 2, 5, False)
        self.m.line_h(0, 2 + x, 5 + x, False)
        self.m.line_h(7, 2 + x, 5 + x, False)

    def _draw_eyeb(self, i, x, y, w=2):
        x += ANALYZ_WIDTH + i * 8
        self.m.rect(x, y, x + w - 1, y + w - 1, False)

    def show_text(self, text, x=None):
        """Текст в области «рта» (например 'v 10', 's 3', 'EP')."""
        import font

        self.m.rect(0, 0, ANALYZ_WIDTH - 1, 7, False)
        if x is None:
            x = (ANALYZ_WIDTH - font.text_width(text)) // 2
            x = max(0, x)
        self.m.text(max(0, x), 0, text)
        self._analyzer_block_until = time.monotonic() + self.analyzer_interval
        self.m.flush()

    def show_value(self, ch, value):
        self.show_text("%s%2d" % (ch, int(value)))

    def set_state(self, on):
        self.enabled = bool(on)
        self.m.clear()
        if self.enabled:
            self._upd_bright()
            self._eyes_block_until = time.monotonic() + 0.6
            self._draw_eye(0)
            self._draw_eye(1)
            self._draw_eyeb(0, 2, 2, 4)
            self._draw_eyeb(1, 2, 2, 4)
        else:
            self.m.set_brightness([0] * self.m.modules)
            self._draw_eye(0)
            self._draw_eye(1)
            self.m.rect(ANALYZ_WIDTH, 0, ANALYZ_WIDTH + 15, 2, False)
            self._draw_eyeb(0, 3, 5)
            self._draw_eyeb(1, 3, 5)
        self.m.flush()

    # ------------------------------------------------------------ анимации
    def search(self, now=None):
        """Анимация поиска Wi-Fi: зрачки бегают, рот залит."""
        now = time.monotonic() if now is None else now
        if now < self._next_search:
            return
        self._next_search = now + self.search_interval

        self._search_pos += self._search_dir
        if self._search_pos >= 6:
            self._search_dir = -1
        elif self._search_pos <= 0:
            self._search_dir = 1

        self.m.rect(ANALYZ_WIDTH, 2, ANALYZ_WIDTH + 15, 5, True)
        self._draw_eyeb(0, self._search_pos, 3)
        self._draw_eyeb(1, self._search_pos, 3)
        self.m.flush()

    def _analyzer_draw(self, vol):
        vol = int(_clamp(vol, 0, 100))
        if self.mode == 0:
            self._offs = (self._offs + 20 * vol // 100) & 0xFFFF
            for i in range(ANALYZ_WIDTH):
                val = noise.inoise8(i * 50, self._offs) - 128
                val = val * vol // 100
                val += 128
                self.m.dot(i, _map(val, 45, 210, 0, 7))
        else:
            hist = self._bar_hist
            for i in range(ANALYZ_WIDTH - 1):
                hist[i] = hist[i + 1]
            hist[ANALYZ_WIDTH - 1] = 9 * vol // 101
            for i in range(ANALYZ_WIDTH):
                p = hist[i]
                mask = ((1 << p) - 1) << ((8 - p) >> 1) if p else 0
                for n in range(8):
                    if mask & 1:
                        self.m.dot(i, n)
                    mask >>= 1

    def step(self, vol, pulse=False, pressed=False, now=None):
        """Один тик отрисовки. Вызывать как можно чаще, внутри всё по таймерам."""
        now = time.monotonic() if now is None else now
        if not self.enabled:
            return

        # ---- глаза ----
        if now >= self._eyes_block_until and now >= self._next_eye:
            self._next_eye = now + self.eye_interval
            self._draw_eye(0)
            self._draw_eye(1)

            if now < self._angry_until:
                self._draw_eyeb(0, 3, 3)
                self._draw_eyeb(1, 3, 3)
                self.m.line_h(0, ANALYZ_WIDTH, ANALYZ_WIDTH + 15, False)
                self.m.line_h(1, ANALYZ_WIDTH + 5, ANALYZ_WIDTH + 10, False)
                self.m.line_h(2, ANALYZ_WIDTH + 6, ANALYZ_WIDTH + 9, False)
                self.m.line_h(3, ANALYZ_WIDTH + 7, ANALYZ_WIDTH + 8, False)
            elif pressed:
                self._draw_eyeb(0, 4, 3, 3)
                self._draw_eyeb(1, 1, 3, 3)
            else:
                self._eye_pos = (self._eye_pos + 15) & 0xFFFF
                x = noise.inoise8(self._eye_pos)
                y = noise.inoise8((self._eye_pos + 0x3FFF) & 0xFFFF)
                x = _map(_clamp(x, 40, 215), 40, 215, 2, 5)
                y = _map(_clamp(y, 40, 215), 40, 215, 2, 5)
                if pulse:
                    dx = 1 if (self._eye_pos & 1) else -1
                    dy = 1 if (self._eye_pos & 2) else -1
                    self._draw_eyeb(0, x + dx, y + dy, 3)
                    self._draw_eyeb(1, x + dx, y + dy, 3)
                else:
                    self._draw_eyeb(0, x, y)
                    self._draw_eyeb(1, x, y)
            self.m.flush()

        # ---- анализатор ----
        if now >= self._analyzer_block_until and now >= self._next_analyzer:
            self._next_analyzer = now + self.analyzer_interval
            self.m.rect(0, 0, ANALYZ_WIDTH - 1, 7, False)
            self._analyzer_draw(vol)
            self.m.flush()
