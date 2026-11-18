"""Value-noise совместимый по духу с FastLED inoise8().

В ESP-версии шум давала FastLED, здесь он свой: та же решётка 16x16,
smoothstep-интерполяция, диапазон 0..255. Нужен только для «рта»-анализатора
и блуждающих зрачков.
"""


def _hash(x, y):
    h = ((x & 0xFF) * 0x9E37) ^ ((y & 0xFF) * 0x85EB) ^ 0x7F4A
    h ^= h >> 7
    h = (h * 0x2C1B) & 0xFFFF
    h ^= h >> 9
    return h & 0xFF


def _fade(t):
    """smoothstep 3t^2 - 2t^3 для t = 0..255."""
    t2 = t * t // 255
    return (3 * t2 - 2 * t2 * t // 255) & 0xFFFF


def inoise8(x, y=0):
    """Шум 0..255 по координатам (x, y) — как FastLED inoise8()."""
    x &= 0xFFFF
    y &= 0xFFFF

    cx, cy = (x >> 4) & 0xFF, (y >> 4) & 0xFF
    fx, fy = (x & 0x0F) * 17, (y & 0x0F) * 17

    v00 = _hash(cx, cy)
    v10 = _hash(cx + 1, cy)
    v01 = _hash(cx, cy + 1)
    v11 = _hash(cx + 1, cy + 1)

    ax, ay = _fade(fx), _fade(fy)
    a = v00 + (v10 - v00) * ax // 255
    b = v01 + (v11 - v01) * ax // 255
    val = a + (b - a) * ay // 255
    return 0 if val < 0 else (255 if val > 255 else int(val))
