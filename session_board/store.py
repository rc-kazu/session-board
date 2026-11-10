"""data/ 配下の JSON ファイルをスレッドセーフに読み書きする。"""

import copy
import json
import os
import tempfile
import threading


class JsonStore:
    def __init__(self, path, default):
        self.path = path
        self.lock = threading.Lock()
        self.default = default
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            pass
        return copy.deepcopy(self.default)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def update(self, fn):
        """fn(data) でデータを変更し、ディスクへ原子的に書き出す。"""
        with self.lock:
            result = fn(self.data)
            self._write()
            return result

    def _write(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
