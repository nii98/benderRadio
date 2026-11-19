"""Тестовые режимы: проверить железо без Wi-Fi, ffmpeg и звука.

    python3 run.py --test eyes      # глаза + анализатор (анимация)
    python3 run.py --test encoder   # энкодер и кнопка (события в консоль и на матрицу)
    python3 run.py --test matrix    # шаблоны на матрице: порядок модулей, битые пиксели
    python3 run.py --test all       # глаза + энкодер одновременно

Ничего не подключает к сети и не трогает аудио: только GPIO/SPI и матрица.
"""

import math
import time

import encoder as encoder_mod
import face as face_mod
import font
import max7219

PHASE_IDS = ("поиск сети", "глаза + анализатор", "показ громкости", "выключенное лицо")


def _log(log):
    if callable(log):
        return log
    return print


# ======================================================================
#  матрица: шаблоны
# ======================================================================
def _pattern_all(matrix):
    matrix.fill()


def _pattern_module(matrix, index):
    matrix.clear()
    for x in range(index * 8, index * 8 + 8):
        for y in range(8):
            matrix.dot(x, y)


def _pattern_checker(matrix):
    for y in range(8):
        for x in range(matrix.width):
            matrix.dot(x, y, (x + y) % 2 == 0)


def _pattern_stripes(matrix):
    for x in range(matrix.width):
        if x % 2 == 0:
            for y in range(8):
                matrix.dot(x, y)


def run_matrix_test(cfg, log=print, duration=None, simulate=False):
    """Проверка: заливка, порядок модулей, яркость, битые пиксели."""
    log = _log(log)
    matrix = max7219.create_matrix(cfg, log, simulate)
    matrix.init_chip()
    n = matrix.modules
    log("== тест матрицы: %d модулей, %d x %d ==" % (n, matrix.width, matrix.height))
    log("1) заливка: должны гореть ВСЕ точки на всех модулях")
    log("2) по одному модулю слева-направо: слева должен загораться первый,")
    log("   и так до правого. Если порядок обратный — модули подключены задом наперёд.")
    log("3) ступеньки яркости 0..15 на модуле 0")
    log("4) шахматка и полоски: ищем битые столбцы")

    started = time.monotonic()
    cycle = 0
    try:
        while True:
            cycle += 1
            matrix.set_brightness([8] * n)

            _pattern_all(matrix)
            matrix.flush()
            log("  [%d] все точки" % cycle)
            if _wait(duration, started, 1.5):
                break

            for i in range(n):
                _pattern_module(matrix, i)
                matrix.flush()
                log("  [%d] модуль %d (столбцы %d..%d)" % (cycle, i, i * 8, i * 8 + 7))
                if _wait(duration, started, 0.45):
                    return 0

            for b in (0, 2, 4, 6, 8, 10, 12, 15):
                matrix.set_brightness([b] + [0] * (n - 1))
                matrix.flush()
                log("  [%d] яркость модуля 0: %d" % (cycle, b))
                if _wait(duration, started, 0.3):
                    return 0

            matrix.set_brightness([8] * n)
            _pattern_checker(matrix)
            matrix.flush()
            log("  [%d] шахматка" % cycle)
            if _wait(duration, started, 1.0):
                break

            _pattern_stripes(matrix)
            matrix.flush()
            log("  [%d] полоски" % cycle)
            if _wait(duration, started, 1.0):
                break
    except KeyboardInterrupt:
        pass
    finally:
        matrix.set_brightness([0] * n)
        matrix.clear()
        matrix.flush()
        matrix.close()
    log("== тест матрицы завершён ==")
    return 0


# ======================================================================
#  глаза + анализатор
# ======================================================================
def run_eyes_test(cfg, log=print, duration=None, simulate=False):
    """Анимация лица с искусственным уровнем звука (без сети и без аудио)."""
    log = _log(log)
    matrix = max7219.create_matrix(cfg, log, simulate)
    ui = dict(cfg.get("ui", {}))
    face = face_mod.Face(matrix, ui, log)
    face.init()
    face.set_state(True)

    log("== тест глаз ==")
    log("смотрите на матрицу: 1) зрачки «ищут сеть», 2) блуждают и реагируют на «биты»,")
    log("3) на «рту» бежит анализатор, 4) показывается громкость, 5) лицо «засыпает».")
    log("Ctrl+C — выход")

    started = time.monotonic()
    phase_len = 2.5
    try:
        while True:
            now = time.monotonic()
            elapsed = now - started
            if duration is not None and elapsed >= duration:
                break
            phase = int(elapsed // phase_len) % len(PHASE_IDS)

            if phase == 0:
                face.search(now)
            else:
                # «дыхание» уровня + редкие биты, чтобы зрачки дёргались
                if phase == 1:
                    vol = 45 + 45 * math.sin(elapsed * 2.2)
                    pulse = int(elapsed * 2) % 3 == 0
                elif phase == 2:
                    vol = 30
                    pulse = False
                    if int(elapsed * 2) % 4 == 0:
                        face.show_value("v", int((elapsed * 7) % 21))
                else:
                    vol = 0
                    pulse = False
                    if int(elapsed * 2) % 4 == 0:
                        face.set_state(False)
                    else:
                        face.set_state(True)

                if phase == 1 and int(elapsed * 4) % 9 == 0:
                    face.angry()
                face.step(vol, pulse, pressed=int(elapsed * 3) % 10 == 0, now=now)

            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        face.set_state(False)
        matrix.set_brightness([0] * matrix.modules)
        matrix.close()
    log("== тест глаз завершён ==")
    return 0


# ======================================================================
#  энкодер
# ======================================================================
def run_encoder_test(cfg, log=print, duration=None, simulate=False):
    """Только энкодер: каждое событие — в консоль и на матрицу."""
    log = _log(log)
    matrix = max7219.create_matrix(cfg, log, simulate)
    ui = dict(cfg.get("ui", {}))
    face = face_mod.Face(matrix, ui, log)
    face.init()
    face.set_state(True)

    enc = encoder_mod.Encoder(cfg.get("encoder", {}), log, simulate)

    log("== тест энкодера ==")
    log("крутите ручку — на матрице растёт/убывает число, в консоли события 'turn'")
    log("нажмите кнопку — 'press'/'release', удержите 1.2 с — 'hold' (яркость)")
    log("1 клик — 'clicks 1', 2 клика — 'clicks 2', 3 клика — 'clicks 3'")
    log("Ctrl+C — выход")

    counter = 0
    bright = int(ui.get("brightness_mouth", 2))
    value_mod = 100
    started = time.monotonic()
    last_event = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if duration is not None and (now - started) >= duration:
                break

            events = enc.poll(now)
            for ev in events:
                last_event = now
                kind = ev[0]
                if kind == "turn":
                    counter = (counter + ev[1]) % value_mod
                    log("  %6.2f  turn %+d   (счётчик %d)" % (now - started, ev[1], counter))
                    face.show_value("v", counter)
                elif kind == "press":
                    log("  %6.2f  press" % (now - started))
                    face.show_text("P")
                elif kind == "release":
                    log("  %6.2f  release" % (now - started))
                elif kind == "hold":
                    log("  %6.2f  hold (режим яркости)" % (now - started))
                    bright = 16 if bright < 8 else 2
                    face.set_brightness(bright, ui.get("brightness_eyes", 5))
                    face.show_value("b", bright)
                elif kind == "clicks":
                    log("  %6.2f  clicks %d" % (now - started, ev[1]))
                    face.show_text("E%d" % min(ev[1], 9))

            # «лицо» продолжает жить: глаза двигаются, «рот» показывает счётчик
            face.step(40, pulse=False, pressed=enc.pressed, now=now)

            if now - last_event > 1.2:
                # после паузы возвращаем счётчик на «рот», чтобы видеть состояние
                face.show_value("v", counter)
                last_event = now

            time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        face.set_state(False)
        matrix.set_brightness([0] * matrix.modules)
        enc.close()
        matrix.close()
    log("== тест энкодера завершён ==")
    return 0


# ======================================================================
#  всё сразу
# ======================================================================
def run_all_test(cfg, log=print, duration=None, simulate=False):
    """Энкодер + глаза одновременно: полный прогон «железа» без сети."""
    log = _log(log)
    matrix = max7219.create_matrix(cfg, log, simulate)
    ui = dict(cfg.get("ui", {}))
    face = face_mod.Face(matrix, ui, log)
    face.init()
    face.set_state(True)

    enc = encoder_mod.Encoder(cfg.get("encoder", {}), log, simulate)
    log("== общий тест: глаза + энкодер ==")
    log("крутите/нажимайте энкодер — лицо реагирует (злой взгляд, прищур, 'v NN')")
    log("Ctrl+C — выход")

    counter = 0
    started = time.monotonic()
    while True:
        now = time.monotonic()
        if duration is not None and (now - started) >= duration:
            break

        vol = 50 + 40 * math.sin((now - started) * 2.0)
        pulse = int(now - started) % 3 == 0

        for ev in enc.poll(now):
            kind = ev[0]
            if kind == "turn":
                counter = (counter + ev[1]) % 100
                log("  %6.2f  turn %+d (счётчик %d)" % (now - started, ev[1], counter))
                face.show_value("v", counter)
            else:
                log("  %6.2f  %s %s" % (now - started, kind, ev[1] if len(ev) > 1 else ""))
                if kind == "clicks" and ev[1] == 1:
                    face.set_state(not face.enabled)
                elif kind == "clicks":
                    face.set_mode(face.mode + 1)

        if enc.pressed:
            face.angry()
        face.step(vol, pulse, pressed=enc.pressed, now=now)
        time.sleep(0.01)

    face.set_state(False)
    matrix.set_brightness([0] * matrix.modules)
    enc.close()
    matrix.close()
    log("== общий тест завершён ==")
    return 0


# ======================================================================
def _wait(duration, started, seconds):
    """Подождать seconds, но не дольше duration. True — пора выходить."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if duration is not None and (time.monotonic() - started) >= duration:
            return True
        time.sleep(0.02)
    return False


def run(target, cfg, log=print, duration=None, simulate=False):
    """Запустить нужный тест по имени."""
    targets = {
        "eyes": run_eyes_test,
        "encoder": run_encoder_test,
        "matrix": run_matrix_test,
        "all": run_all_test,
    }
    if target not in targets:
        raise ValueError("неизвестный тест: %r (есть: %s)" % (target, ", ".join(targets)))
    return targets[target](cfg, log=log, duration=duration, simulate=simulate)
