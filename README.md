# BendeRadio для Orange Pi One

Интернет-радио в форме головы Бендера: матрица MAX7219 5×8×8 («рот»-анализатор и «глаза»),
энкодер с кнопкой, потоковое аудио и конфиг с радиостанциями и Wi-Fi.

Это **порт ESP-прошивки на Linux**: вместо микроконтроллера — Orange Pi One (Allwinner H3),
вместо ESP32-audioI2S — `ffmpeg` + `aplay`, вместо EEPROM — JSON-файл настроек.
Логика лица, энкодера и анализатора перенесена один в один.

---

## Сначала про железо Orange Pi One (важно!)

| Чего нет в Orange Pi One | Как быть |
|---|---|
| **Wi-Fi** (плата только с Ethernet) | нужен USB Wi-Fi донгл; SSID/пароль из конфига применяются к нему (`wlan0`) |
| **Аналогового аудиовыхода** (только HDMI) | USB-звуковая карта (`hw:1,0`) или HDMI-звук; `audio.device` в конфиге |
| **АЦП / аналогового входа** | анализатор считает уровень **из самого аудиопотока** — делить сигнал не нужно |

Если у вас не «One», а Orange Pi PC/Plus/Zero с 3.5 мм разъёмом — программа та же,
поменяется только `audio.device` (`default`/`hw:0,0`) и, возможно, пины.

Всё остальное (MAX7219, энкодер) подключается к 40-пиновому разъёму.

---

## Что нужно установить

* Python **3.9+** (в Armbian уже есть);
* `ffmpeg` — декодирование потока;
* `alsa-utils` — `aplay` и `amixer`;
* по желанию: `python3-spidev` (быстрый SPI для матрицы), `python3-numpy`
  (программная громкость), `python3-libgpiod` (энкодер через libgpiod).

`install.sh` ставит всё сам:

```bash
sudo ./install.sh
```

Скрипт разложит программу в `/opt/benderadio`, конфиг в `/etc/benderadio/config.json`,
состояние в `/var/lib/benderadio/` и включит systemd-сервис `benderadio`.

---

## Быстрый старт

```bash
# 1. правим станции/пины/звук
sudo nano /etc/benderadio/config.json

# 2. проверка без железа (конфиг, шум, шрифт, VU, настройки, энкодер)
python3 /opt/benderadio/run.py --config /etc/benderadio/config.json --selftest

# 3. прогон без железа: лицо рисуется в консоль, звук синтетический
python3 /opt/benderadio/run.py --config /etc/benderadio/config.json --simulate --duration 5

# 4. боевой запуск
systemctl start benderadio
journalctl -u benderadio -f
```

Из папки проекта — то же самое, но `python3 run.py ...` и `--config config.json`.

---

## Конфиг: `config.json`

Файл лежит рядом с программой, при установке копируется в `/etc/benderadio/config.json`.
После правок: `systemctl restart benderadio`.

### Станции

```json
"radio": {
  "station": 0,           // с какой станции начинать (если в state.json ещё пусто)
  "volume": 10,           // 0..volume_steps
  "volume_steps": 21,
  "stations": [
    { "name": "Radio 1 Rock", "url": "http://stream81.metacast.eu/radio1rock128" },
    { "name": "Европа Плюс",  "url": "https://ep128.hostingradio.ru:8030/ep128" }
  ]
}
```

Ссылок может быть сколько угодно, `name` показывается в логе. Подходят любые форматы,
которые умеет `ffmpeg`: MP3, AAC/AAC+, OGG/Vorbis, Opus, FLAC — и `http://`, и `https://`
(в отличие от ESP8266, здесь TLS не проблема). Все 6 станций в конфиге проверены рабочими.

### Wi-Fi

```json
"wifi": {
  "enabled": true,
  "ssid": "NewBuilding",
  "password": "MMH_new2023",
  "interface": "wlan0",
  "country": "RU",
  "connect_on_start": true
}
```

Как это работает:

* если интерфейс уже с IP — ничего не делает;
* если установлен **NetworkManager** (в Armbian по умолчанию) — `nmcli device wifi connect`;
* иначе через `wpa_supplicant`: пароль хешируется `wpa_passphrase`, пишется
  `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf`, затем `dhclient`/`udhcpc`;
* нужны права root (сервис и так работает от root);
* если сеть не поднялась — программа не падает, продолжает крутить «поиск сети» и
  пробует снова раз в 30 секунд, когда поток отваливается.

Сменить сеть можно просто правкой `ssid`/`password` + `systemctl restart benderadio`.

### Звук

```json
"audio": {
  "device": "default",        // hw:1,0 для USB-карты, hdmi для HDMI (ALSA-имена)
  "sample_rate": 44100,
  "channels": 2,
  "buffer_us": 400000,        // буфер aplay, мкс (больше = стабильнее, но инертнее)
  "chunk_seconds": 0.02,      // размер чанка, с которым работает VU
  "ffmpeg": "ffmpeg",
  "aplay": "aplay",
  "volume_control": "amixer", // amixer | software | none
  "amixer_card": "",          // "" = карта по умолчанию, иначе номер из aplay -l
  "amixer_control": "PCM"     // имя из amixer scontrols
}
```

Найти устройства:

```bash
aplay -l                 # список карт: card 0, card 1 ...
aplay -L | head -40      # именованные устройства (default, sysdefault, hdmi, ...)
amixer scontrols         # имя регулятора громкости: PCM, Master, Speaker...
```

Если `amixer` не сработал, громкость автоматически уедет в «программную» (нужен `numpy`),
и в логе будет предупреждение.

### Матрица MAX7219

```json
"matrix": {
  "driver": "sysfs",                     // sysfs | spi | sim
  "modules": 5,
  "brightness": [2, 2, 2, 5, 5],         // стартовая яркость по модулям
  "gpio": { "chip": "gpiochip0", "cs": 67, "din": 64, "clk": 66, "half_period_us": 4 },
  "spi":  { "bus": 0, "device": 0, "speed_hz": 1000000 }
}
```

* `driver: sysfs` — работает без overlay'ев и пакетов: три пина дёргаются через
  `/sys/class/gpio`. Полный кадр ~20–40 мс, этого хватает (лицо обновляется 6 раз в секунду).
* `driver: spi` — быстрее и аккуратнее. Нужен overlay в `/boot/armbianEnv.txt`:
  ```
  overlays=spi-spidev
  param_spidev_spi_bus=0
  ```
  после перезагрузки появится `/dev/spidev0.0`.
* `driver: sim` — кадр в консоль.

Номера линий GPIO: `банк_смещение + номер`, для H3: PA = 0, PB = 32, PC = 64, PD = 96, PG = 192.
То есть `PC0 = 64`, `PC2 = 66`, `PC3 = 67`.

### Энкодер

```json
"encoder": {
  "driver": "sysfs",          // sysfs | gpiod | sim
  "chip": "gpiochip0",
  "a": 6,                     // PA6
  "b": 1,                     // PA1
  "button": 0,                // PA0
  "button_active_low": true,  // кнопка на GND, внутренняя подтяжка
  "debounce": 0.02,
  "click_timeout": 0.35,
  "hold_time": 1.2
}
```

Проверить, что пины те, что вы воткнули: `gpio readall` (пакет wiringOP) или
`gpiodetect` / `gpioinfo` из libgpiod.

### Остальное

```json
"ui": {
  "state": true,                 // при первом запуске звук включён
  "brightness_mouth": 2,
  "brightness_eyes": 5,
  "analyzer_mode": 0,            // 0 = шум, 1 = столбики
  "noise_threshold": 20,         // порог тишины, 0..100
  "eye_interval": 0.15,          // как часто двигаются зрачки
  "analyzer_interval": 1.0,      // как часто перерисовывается «рот»
  "angry_timeout": 0.8,          // «злой» взгляд после кручения громкости
  "level": {
    "db_floor": -60,             // ниже этого — тишина
    "threshold": 20,
    "attack": 0.55,              // скорость атаки 0..1
    "release": 0.12,             // скорость спада 0..1
    "pulse_min": 30,             // гистерезис детектора бита, %
    "pulse_max": 80
  }
},
"storage": { "state_file": "/var/lib/benderadio/state.json" },
"logging": { "level": "INFO" }
```

---

## Распиновка (40-пиновый разъём Orange Pi One, H3)

### Матрица MAX7219 — те же 3 пина и для `sysfs`, и для `spi`

| MAX7219 | Пин платы | GPIO (линия) |
|---|---|---|
| DIN / MOSI | **19** | PC0 = 64 |
| CLK / SCLK | **23** | PC2 = 66 |
| CS / LOAD | **24** | PC3 = 67 |
| VCC | 5V (пин 2 или 4) | — |
| GND | GND (пин 6, 9, 14, 20, 25) | — |

Если включаете `driver: spi`, те же провода остаются на месте — их берёт драйвер SPI.

### Энкодер

| Энкодер | Пин платы | GPIO (линия) |
|---|---|---|
| S1 / CLK | **7** | PA6 = 6 |
| S2 / DT | **11** | PA1 = 1 |
| Кнопка (на GND) | **13** | PA0 = 0 |

Внутренние подтяжки включаются программой (`INPUT`/`PULL_UP`-эквивалент), внешние
резисторы не обязательны. Конденсаторы 0.1 мкФ с S1/S2 на GND сильно уменьшают дребезг.

### Звук

* **USB-звуковая карта**: вставляете в USB, `aplay -l` покажет `card 1`, в конфиге
  `"device": "hw:1,0"`.
* **HDMI**: `"device": "hdmi"` (или `hw:0,0`), если в системе есть HDMI-звук.
* **Питание платы**: micro-USB 5 В ≥ 2 А. При слабом питании Wi-Fi-донгл будет отваливаться.

---

## Управление

| Действие | Что делает |
|---|---|
| Вращение энкодера | громкость (0…21), на «рту» `v NN` |
| Короткий клик | вкл/выкл звук (при выключении матрица гаснет) |
| 2 клика | режим анализатора (шум ↔ столбики) |
| 3 клика | автоподстройка порога тишины (`E NN`) |
| Зажать кнопку + вращать | выбор станции (`s NN`), поток перезапускается при отпускании |
| Зажать кнопку на 1.2 с | режим яркости: вращение меняет яркость `b NN` («рот»); ещё одно удержание — `o NN` («глаза»); выход — клик или 8 с бездействия |

Настройки (громкость, станция, режим, яркость, порог) сохраняются в
`/var/lib/benderadio/state.json` и переживают перезагрузку.

---

## Структура проекта

```
benderadio-orangepi/
├── run.py                  точка входа
├── config.json             СТАНЦИИ + WI-FI + ПИНЫ  ← правится здесь
├── benderadio/
│   ├── app.py              главный цикл, события, самопроверка
│   ├── player.py           ffmpeg → PCM → aplay, громкость
│   ├── face.py             лицо: глаза, анализатор, надписи
│   ├── max7219.py          драйвер матрицы: sysfs / spi / sim
│   ├── encoder.py          энкодер + кнопка (sysfs / libgpiod / sim)
│   ├── vu.py               уровень звука из PCM (аналог VolAnalyzer)
│   ├── wifi.py             подключение по ssid/паролю
│   ├── settings.py         сохранение настроек (бывший EEPROM)
│   ├── noise.py            шум для «рта» и зрачков (замена FastLED)
│   └── font.py             шрифт 5x7 для `v NN` / `s NN`
├── install.sh              установка пакетов, файлов и systemd-сервиса
├── benderadio.service      unit для автозапуска
└── README.md
```

---

## Отладка

```bash
# все подробности
python3 run.py --config config.json --log-level DEBUG

# только конфиг/логика, без GPIO и звука
python3 run.py --config config.json --simulate --duration 5

# самопроверка
python3 run.py --config config.json --selftest

# посмотреть, что видит звук
aplay -l; amixer scontrols; speaker-test -D hw:1,0 -c2 -t sine -l 1
```

Типичные проблемы:

| Симптом | Причина / лечение |
|---|---|
| `aplay отвалился` | неверный `audio.device`; проверьте `aplay -l` и попробуйте `hw:X,0` |
| `нет прав на /var/lib/benderadio` | запуск не от root; сервис запускается от root |
| Матрица «мигает ерундой» | проверьте полярность CS и порядок модулей (первый модуль = левый) |
| `GPIO busy` / `export` не работает | включён overlay SPI, а драйвер `sysfs`; выберите что-то одно |
| Энкодер крутится «не туда» | поменяйте местами `a` и `b` (или переверните провода) |
| Звук рвётся | увеличьте `buffer_us` до 600000, проверьте питание и Wi-Fi |
| Громкость не меняется | неверный `amixer_control`; `amixer scontrols` покажет имя |

---

## Чем это отличается от ESP-прошивки

| | ESP8266/ESP32 | Orange Pi One |
|---|---|---|
| Звук | ESP8266Audio / ESP32-audioI2S | `ffmpeg` → `aplay` (любой кодек, HTTPS) |
| Потоки | MP3, без TLS (на ESP8266) | MP3/AAC/OGG/Opus/FLAC, http и https |
| Анализатор | ADC-пин (шум, делитель) | RMS прямо из PCM (точно и без проводов) |
| Настройки | EEPROM | JSON-файл |
| GPIO | регистры портов | sysfs / SPI / libgpiod |
| Wi-Fi | встроенный | USB-донгл + nmcli/wpa_supplicant |
| Обновление | перепрошить | поменять `config.json` и `systemctl restart` |
