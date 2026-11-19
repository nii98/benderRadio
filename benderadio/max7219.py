"""Драйвер матрицы MAX7219 (5 модулей 8x8) для Orange Pi One / Linux.

Два бэкенда:
  * spi    — /dev/spidevX.Y (быстро, нужен overlay spi-spidev в Armbian);
  * sysfs  — побитовое дёргание GPIO через /sys/class/gpio (без зависимостей,
             медленнее: полный кадр ~20-40 мс, зато работает «из коробки»).
Плюс sim — вывод кадра в консоль ASCII-артом (для отладки без железа).
"""

import os
import sys
import time

import font


class Matrix:
    """Общая логика: кадровый буфер 40x8 и примитивы рисования."""

    def __init__(self, modules=5, brightness=None):
        self.modules = int(modules)
        self.width = self.modules * 8
        self.height = 8
        self.px = bytearray(self.width * self.height)  # 1 = точка горит
        self.dirty = True
        self.brightness = list(brightness) if brightness else [8] * self.modules
        if len(self.brightness) < self.modules:
            self.brightness += [self.brightness[-1]] * (self.modules - len(self.brightness))

    # ---------------- примитивы ----------------
    def clear(self):
        if any(self.px):
            self.px = bytearray(self.width * self.height)
            self.dirty = True

    def fill(self):
        self.px = bytearray([1]) * (self.width * self.height)
        self.dirty = True

    def dot(self, x, y, on=True):
        if 0 <= x < self.width and 0 <= y < self.height:
            i = y * self.width + x
            val = 1 if on else 0
            if self.px[i] != val:
                self.px[i] = val
                self.dirty = True

    def get(self, x, y):
        if 0 <= x < self.width and 0 <= y < self.height:
            return bool(self.px[y * self.width + x])
        return False

    def rect(self, x0, y0, x1, y1, on=True):
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.dot(x, y, on)

    def line_v(self, x, y0, y1, on=True):
        self.rect(x, y0, x, y1, on)

    def line_h(self, y, x0, x1, on=True):
        self.rect(x0, y, x1, y, on)

    def text(self, x, y, text):
        cx = x
        for ch in text:
            for gx, gy in font.glyph(ch):
                self.dot(cx + gx, y + gy)
            cx += font.WIDTH + font.SPACING

    def set_brightness(self, value):
        """Яркость: одно число или список по модулям (0..15)."""
        if isinstance(value, int):
            new = [value] * self.modules
        else:
            new = list(value)
        new = [max(0, min(15, int(v))) for v in new]
        if new != self.brightness:
            self.brightness = new
            self.apply_brightness()

    # ---------------- вывод ----------------
    def row_bytes(self, row):
        """Байты одной строки (digit row 1..8) по модулям, в порядке отправки."""
        out = []
        for m in reversed(range(self.modules)):
            bits = 0
            for x in range(8):
                if self.px[row * self.width + m * 8 + x]:
                    bits |= 1 << x
            out.append(bits & 0xFF)
        return out

    def flush(self):
        if not self.dirty:
            return
        for row in range(8):
            self._send(row + 1, self.row_bytes(row))
        self.dirty = False

    def apply_brightness(self):
        self._send(0x0A, list(reversed(self.brightness)))

    def init_chip(self):
        self._send(0x0F, [0] * self.modules)  # test mode off
        self._send(0x09, [0] * self.modules)  # no decode
        self._send(0x0B, [7] * self.modules)  # scan all 8 digits
        self.apply_brightness()
        self._send(0x0C, [1] * self.modules)  # normal operation
        self.clear()
        self.flush()

    def close(self):
        pass

    def _send(self, addr, values):
        raise NotImplementedError


class SpiMatrix(Matrix):
    """MAX7219 через аппаратный SPI."""

    def __init__(self, modules=5, bus=0, device=0, speed_hz=1000000, brightness=None):
        super().__init__(modules, brightness)
        import spidev  # ставится пакетом python3-spidev

        self.spi = spidev.SpiDev()
        self.spi.open(int(bus), int(device))
        self.spi.max_speed_hz = int(speed_hz)
        self.spi.mode = 0
        self.spi.bits_per_word = 8

    def _send(self, addr, values):
        self.spi.xfer2([int(addr) & 0xFF] + [int(v) & 0xFF for v in values])

    def close(self):
        try:
            self.spi.close()
        except Exception:
            pass


class GpioError(RuntimeError):
    """Понятная человеку ошибка работы с GPIO."""


class _SysfsPin:
    """Один пин через /sys/class/gpio с внятными сообщениями об ошибках."""

    def __init__(self, line, out=True):
        self.line = int(line)
        self.export_path = "/sys/class/gpio/export"
        self.dir_path = "/sys/class/gpio/gpio%d/direction" % self.line
        self.val_path = "/sys/class/gpio/gpio%d/value" % self.line

        if not os.path.exists("/sys/class/gpio"):
            raise GpioError(
                "нет /sys/class/gpio — ядро не отдаёт GPIO через sysfs. "
                "Используйте matrix.driver = \"spi\" (нужен overlay spi-spidev) "
                "или обновите ядро Armbian")

        if not os.path.exists(self.val_path):
            try:
                with open(self.export_path, "w") as f:
                    f.write(str(self.line))
            except PermissionError:
                raise GpioError(
                    "нет прав экспортировать GPIO %d — запускайте от root (sudo) "
                    "или добавьте пользователя в группу gpio" % self.line)
            except OSError as exc:
                if not os.path.exists(self.val_path):
                    raise GpioError("не удалось экспортировать GPIO %d: %s" % (self.line, exc))
            time.sleep(0.05)

        try:
            with open(self.dir_path, "w") as f:
                f.write("out" if out else "in")
        except PermissionError:
            raise GpioError("нет прав настроить GPIO %d (нужен root)" % self.line)
        except OSError as exc:
            text = str(exc).lower()
            if exc.errno == 16 or "busy" in text or "resource" in text:
                import glob as _glob
                has_spidev = bool(_glob.glob("/dev/spidev*"))
                hint = (" Найдены /dev/spidev* — значит overlay spi-spidev включён: "
                        "поставьте matrix.driver = \"spi\" и ничего не перевключайте."
                        if has_spidev else
                        " /dev/spidev* нет — значит overlay не включён: выключите его "
                        "или оставьте matrix.driver = \"sysfs\" и освободите пины.")
                raise GpioError(
                    "GPIO %d занят другим драйвером (частая причина — overlay в "
                    "/boot/armbianEnv.txt).%s" % (self.line, hint))
            raise GpioError("GPIO %d: %s" % (self.line, exc))

        self.fd = os.open(self.val_path, os.O_RDWR)

    def write(self, value):
        os.pwrite(self.fd, b"1" if value else b"0", 0)

    def read(self):
        os.lseek(self.fd, 0, os.SEEK_SET)
        return os.read(self.fd, 1).strip() == b"1"

    def close(self):
        try:
            os.close(self.fd)
        except Exception:
            pass


class SysfsMatrix(Matrix):
    """MAX7219 побитово через sysfs GPIO (без внешних зависимостей)."""

    def __init__(self, modules=5, cs=67, din=64, clk=66, brightness=None, half_period=0.000004):
        super().__init__(modules, brightness)
        if len({int(cs), int(din), int(clk)}) != 3:
            raise GpioError(
                "matrix.gpio: cs/din/clk должны быть тремя РАЗНЫМИ линиями "
                "(сейчас cs=%s din=%s clk=%s)" % (cs, din, clk))
        self.half = float(half_period)
        self.cs = _SysfsPin(cs)
        self.din = _SysfsPin(din)
        self.clk = _SysfsPin(clk)
        self.cs.write(1)

    def _shift(self, value):
        for bit in range(7, -1, -1):
            self.din.write((value >> bit) & 1)
            self.clk.write(1)
            self.clk.write(0)
            if self.half:
                time.sleep(self.half)

    def _send(self, addr, values):
        self.cs.write(0)
        self._shift(int(addr) & 0xFF)
        for v in values:
            self._shift(int(v) & 0xFF)
        self.cs.write(1)

    def close(self):
        for pin in (self.cs, self.din, self.clk):
            try:
                pin.close()
            except Exception:
                pass


class SimMatrix(Matrix):
    """Матрица-заглушка: печатает кадр в консоль."""

    def __init__(self, modules=5, brightness=None, out=None, quiet=False):
        super().__init__(modules, brightness)
        self.out = out
        self.quiet = quiet
        self.frames = 0

    def _send(self, addr, values):
        pass

    def flush(self):
        if not self.dirty or self.quiet:
            self.dirty = False
            return
        self.frames += 1
        if self.out:
            self.out.write("\n")
            for y in range(self.height):
                line = "".join("#" if self.px[y * self.width + x] else "." for x in range(self.width))
                self.out.write(line + "\n")
            self.out.flush()
        self.dirty = False


def create_matrix(cfg, log=print, simulate=False):
    """Собрать матрицу по конфигу: driver = sim | spi | sysfs."""
    mcfg = cfg.get("matrix", {})
    modules = int(mcfg.get("modules", 5))
    bright = mcfg.get("brightness")

    if simulate:
        return SimMatrix(modules, bright, out=sys.stdout, quiet=bool(mcfg.get("quiet", False)))

    driver = str(mcfg.get("driver", "sysfs")).lower()
    if driver == "spi":
        scfg = mcfg.get("spi", {})
        log("матрица: SPI bus=%s dev=%s %s Гц" % (scfg.get("bus", 0), scfg.get("device", 0), scfg.get("speed_hz", 1000000)))
        return SpiMatrix(modules, scfg.get("bus", 0), scfg.get("device", 0), scfg.get("speed_hz", 1000000), bright)

    if driver == "sysfs":
        gcfg = mcfg.get("gpio", {})
        log("матрица: sysfs GPIO cs=%s din=%s clk=%s" % (gcfg.get("cs"), gcfg.get("din"), gcfg.get("clk")))
        return SysfsMatrix(modules, gcfg.get("cs", 67), gcfg.get("din", 64), gcfg.get("clk", 66),
                           bright, gcfg.get("half_period_us", 4) / 1e6)

    if driver == "sim":
        return SimMatrix(modules, bright)

    raise ValueError("неизвестный matrix.driver: %r" % driver)


# ======================================================================
#  диагностика GPIO
# ======================================================================
def gpio_pins(cfg):
    """Тройка линий cs/din/clk из конфига (с дефолтами)."""
    gcfg = cfg.get("matrix", {}).get("gpio", {})
    return {
        "cs": int(gcfg.get("cs", 67)),
        "din": int(gcfg.get("din", 64)),
        "clk": int(gcfg.get("clk", 66)),
    }


def probe_pins(cfg, simulate=False):
    """Проверить, что каждый пин реально экспортируется и переключается.

    Возвращает список словарей: name, line, ok, detail.
    Ничего не бросает — чтобы диагностику можно было прогнать на битом железе.
    """
    pins = gpio_pins(cfg)
    result = []
    if simulate:
        for name, line in pins.items():
            result.append({"name": name, "line": line, "ok": True, "detail": "симуляция"})
        return result

    for name, line in pins.items():
        row = {"name": name, "line": line, "ok": False, "detail": ""}
        pin = None
        try:
            pin = _SysfsPin(line, out=True)
            pin.write(1)
            high = pin.read()
            pin.write(0)
            low = pin.read()
            if high and not low:
                row["ok"] = True
                row["detail"] = "пишется и читается (1 -> 0)"
            else:
                row["detail"] = "запись не подтверждается (прочитано %s/%s) — пин занят?" % (high, low)
        except GpioError as exc:
            row["detail"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            row["detail"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            if pin is not None:
                try:
                    pin.close()
                except Exception:
                    pass
        result.append(row)
    return result


def spi_devices():
    import glob
    return sorted(glob.glob("/dev/spidev*"))


def has_root():
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False

