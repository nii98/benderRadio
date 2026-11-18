"""Хранение настроек между запусками (замена EEPROM из ESP-версии)."""

import json
import os
import tempfile


class Settings:
    def __init__(self, path, log=print):
        self.path = path or "state.json"
        self.log = log
        self.data = {}

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}
        except Exception as exc:
            self.log("не прочитал %s: %s" % (self.path, exc))
            self.data = {}
        return self.data

    def save(self, data):
        self.data = dict(data)
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".state-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return True
        except Exception as exc:
            self.log("не сохранил %s: %s" % (self.path, exc))
            return False
