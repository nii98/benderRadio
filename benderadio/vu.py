"""Уровень звука из цифрового потока (замена VolAnalyzer с ADC).

На Orange Pi One аналогового входа нет, поэтому «громкость для рта»
считается прямо из PCM, который играет. Это даже удобнее: уровень не зависит
от шума в комнате и от положения ручки усилителя.
"""

import array
import math


class AudioLevel:
    """RMS -> 0..100 с порогом, attack/release и детектором бита."""

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.db_floor = float(cfg.get("db_floor", -60.0))   # ниже = тишина
        self.threshold = float(cfg.get("threshold", 20.0))  # порог шума, 0..100
        self.attack = float(cfg.get("attack", 0.55))        # 0..1, больше = быстрее
        self.release = float(cfg.get("release", 0.12))
        self.pulse_min = float(cfg.get("pulse_min", 30.0))
        self.pulse_max = float(cfg.get("pulse_max", 80.0))
        self.pulse_timeout = float(cfg.get("pulse_timeout", 0.25))

        self.vol = 0.0
        self.max_vol = 0.0
        self._peak_hold = 0.0
        self._above = False
        self._pulse_pending = False
        self._pulse_seen = 0.0

    # ------------------------------------------------------------------
    def update(self, pcm):
        """Скормить кусок PCM s16le (стерео/моно) — вернуть волюм 0..100."""
        if not pcm:
            return int(self.vol)
        try:
            samples = array.array("h")
            samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        except Exception:
            return int(self.vol)
        if not samples:
            return int(self.vol)

        # считаем RMS по каждому 4-му отсчёту — для VU более чем достаточно
        acc = 0
        count = 0
        for i in range(0, len(samples), 4):
            s = samples[i]
            acc += s * s
            count += 1
        if not count:
            return int(self.vol)
        rms = math.sqrt(acc / count) / 32768.0

        if rms <= 0.0:
            target = 0.0
        else:
            db = 20.0 * math.log10(rms)
            target = (db - self.db_floor) / (-self.db_floor) * 100.0
            target = max(0.0, min(100.0, target))

        # порог шума: всё, что ниже, считаем тишиной
        if target < self.threshold:
            target = 0.0

        # сглаживание как у VolAnalyzer: быстро вверх, медленно вниз
        k = self.attack if target > self.vol else self.release
        self.vol += (target - self.vol) * k

        if self.vol > self.max_vol:
            self.max_vol = self.vol
        self._peak_hold += (self.vol - self._peak_hold) * 0.1

        self._pulse_track()
        return int(self.vol)

    def _pulse_track(self):
        if self.vol >= self.pulse_max:
            self._above = True
            self._pulse_seen = 0.0
        elif self._above:
            if self.vol <= self.pulse_min:
                self._above = False
                self._pulse_pending = True
        else:
            self._pulse_seen += 1.0

    # ------------------------------------------------------------------
    def pulse(self):
        """Был ли «бит» с прошлого вызова (для подёргивания зрачков)."""
        if self._pulse_pending:
            self._pulse_pending = False
            return True
        return False

    def peak(self):
        return self._peak_hold

    def take_max(self):
        """Максимум за период — для автоподстройки порога (3 клика)."""
        value = self.max_vol
        self.max_vol = 0.0
        return value

    def set_threshold(self, value):
        self.threshold = max(0.0, min(100.0, float(value)))
