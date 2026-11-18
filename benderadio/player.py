"""Звук: ffmpeg (поток -> PCM) + aplay (PCM -> ALSA) + amixer (громкость).

На Orange Pi One аналогового выхода нет: звук идёт либо в USB-звуковуху,
либо в HDMI. Устройство задаётся в config.json -> audio.device.

Почему не mpv/ffplay: нужен доступ к самим сэмплам, чтобы считать уровень
для «рта»-анализатора. Поэтому PCM идёт через нас, а aplay его проигрывает.
"""

import array
import math
import os
import select
import shutil
import subprocess
import time

try:  # numpy не обязателен, но с ним программная громкость быстрее
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None


class VolumeControl:
    """Громкость через amixer (ALSA mixer) либо программно (numpy)."""

    def __init__(self, cfg=None, log=print):
        cfg = cfg or {}
        self.log = log
        self.mode = str(cfg.get("volume_control", "amixer")).lower()
        self.card = str(cfg.get("amixer_card", ""))
        self.control = str(cfg.get("amixer_control", "PCM"))
        self._gain = 1.0
        self._warned = False
        self._amixer = shutil.which("amixer")
        if self.mode == "amixer" and not self._amixer:
            self.log("amixer не найден — громкость будет программной (если есть numpy)")
            self.mode = "software"

    def apply(self, percent):
        percent = max(0, min(100, int(percent)))
        if self.mode == "amixer" and self._amixer:
            cmd = [self._amixer, "-q"]
            if self.card:
                cmd += ["-c", self.card]
            cmd += ["sset", self.control, "%d%%" % percent]
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5)
            except Exception as exc:
                if not self._warned:
                    self.log("amixer не сработал (%s) — переключаюсь на программную громкость" % exc)
                    self._warned = True
                self.mode = "software"
        if self.mode == "software":
            self._gain = (percent / 100.0) ** 2

    @property
    def software_gain(self):
        return self._gain


class Player:
    """Поток: ffmpeg -i <url> -f s16le | [мы] | aplay."""

    def __init__(self, cfg=None, log=print):
        cfg = cfg or {}
        self.log = log
        self.ffmpeg = str(cfg.get("ffmpeg", "ffmpeg"))
        self.aplay = str(cfg.get("aplay", "aplay"))
        self.sample_rate = int(cfg.get("sample_rate", 44100))
        self.channels = int(cfg.get("channels", 2))
        self.device = str(cfg.get("device", "default"))
        self.buffer_us = int(cfg.get("buffer_us", 400000))
        self.chunk_seconds = float(cfg.get("chunk_seconds", 0.02))
        self.volume = VolumeControl(cfg.get("volume", {}) if isinstance(cfg.get("volume"), dict) else cfg, log)

        self._ff = None
        self._ap = None
        self.url = None
        self._stalled = False

    # ------------------------------------------------------------------
    def _chunk_bytes(self):
        return int(self.sample_rate * self.chunk_seconds) * self.channels * 2

    def play(self, url):
        self.stop()
        if not shutil.which(self.ffmpeg):
            raise RuntimeError("не найден ffmpeg (apt install ffmpeg)")

        ff_cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-i", url,
            "-vn", "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate), "-ac", str(self.channels), "-",
        ]
        self._ff = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    bufsize=0, close_fds=True)

        ap_cmd = [
            self.aplay, "-q", "-t", "raw", "-f", "S16_LE",
            "-r", str(self.sample_rate), "-c", str(self.channels),
            "-B", str(self.buffer_us), "-D", self.device, "-",
        ]
        try:
            self._ap = subprocess.Popen(ap_cmd, stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        bufsize=0, close_fds=True)
        except Exception:
            self._ff.kill()
            self._ff = None
            raise

        self.url = url
        self._stalled = False
        self.log("играем: %s" % url)
        return True

    # ------------------------------------------------------------------
    def pump(self, timeout=2.0):
        """Перекачать один чанк (~20 мс). Вернуть PCM или b'' если поток умер."""
        if not self._ff or not self._ap:
            return b""
        try:
            ready, _, _ = select.select([self._ff.stdout], [], [], timeout)
        except (OSError, ValueError):
            self.stop()
            return b""
        if not ready:
            self.log("поток молчит %.0f с — перезапускаю" % timeout)
            self.stop()
            return b""

        pcm = self._ff.stdout.read(self._chunk_bytes())
        if not pcm:
            self.stop()
            return b""

        out = pcm
        gain = self.volume.software_gain
        if self.volume.mode == "software" and gain < 0.999 and _np is not None:
            arr = _np.frombuffer(pcm, dtype=_np.int16).astype(_np.int32)
            arr = (arr * gain).astype(_np.int16)
            out = arr.tobytes()

        try:
            self._ap.stdin.write(out)
            self._ap.stdin.flush()
        except (BrokenPipeError, OSError):
            self.log("aplay отвалился (проверьте audio.device в конфиге)")
            self.stop()
            return b""
        return pcm

    def alive(self):
        if not self._ff or not self._ap:
            return False
        if self._ff.poll() is not None or self._ap.poll() is not None:
            return False
        return True

    def set_volume(self, vol, steps=21):
        vol = max(0, min(int(steps), int(vol)))
        self.volume.apply(int(round(vol * 100.0 / steps)))

    def stop(self):
        for proc, what in ((self._ap, "aplay"), (self._ff, "ffmpeg")):
            if not proc:
                continue
            try:
                if what == "aplay":
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                proc.terminate()
            except Exception:
                pass
        for proc in (self._ap, self._ff):
            if not proc:
                continue
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._ff = None
        self._ap = None
        self.url = None

    def close(self):
        self.stop()


class FakePlayer:
    """Заглушка для --simulate: синус с «битами», без ffmpeg и звука."""

    def __init__(self, cfg=None, log=print):
        self.log = log
        self.sample_rate = int((cfg or {}).get("sample_rate", 44100))
        self.channels = int((cfg or {}).get("channels", 2))
        self.chunk_seconds = float((cfg or {}).get("chunk_seconds", 0.02))
        self.url = None
        self._phase = 0.0
        self._t = 0.0
        self._vol = 10
        self._steps = 21

    def _chunk_bytes(self):
        return int(self.sample_rate * self.chunk_seconds) * self.channels * 2

    def play(self, url):
        self.url = url
        self.log("(sim) играем: %s" % url)
        return True

    def pump(self, timeout=2.0):
        if not self.url:
            return b""
        frames = int(self.sample_rate * self.chunk_seconds)
        amp = (self._vol / float(self._steps)) * 9000.0
        # медленная огибающая «музыки» + биты
        env = 0.55 + 0.45 * math.sin(self._t * 2.0) ** 2
        beat = 1.0 if (int(self._t * 2) % 4 == 0) else 0.6
        a = amp * env * beat
        step = 2.0 * math.pi * 440.0 / self.sample_rate
        buf = array.array("h")
        ph = self._phase
        for _ in range(frames):
            v = int(a * math.sin(ph))
            ph += step
            if ph > 2 * math.pi:
                ph -= 2 * math.pi
            if self.channels == 2:
                buf.append(v)
                buf.append(v)
            else:
                buf.append(v)
        self._phase = ph
        self._t += self.chunk_seconds
        time.sleep(self.chunk_seconds * 0.5)  # не жрём 100% CPU в симуляции
        return buf.tobytes()

    def alive(self):
        return True

    def set_volume(self, vol, steps=21):
        self._vol = max(0, min(int(steps), int(vol)))
        self._steps = int(steps)

    def stop(self):
        self.url = None

    def close(self):
        self.stop()
