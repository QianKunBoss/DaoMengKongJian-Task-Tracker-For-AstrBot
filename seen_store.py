"""持久化去重：记录已处理过的活动 ID，避免每分钟重复判断/推送。"""
import json
import os


class SeenStore:
    def __init__(self, path: str):
        self.path = path
        self._seen = set()
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._seen = set(data.get("seen", []))
        except (FileNotFoundError, json.JSONDecodeError):
            self._seen = set()

    def _save(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        # 上限保留最近 5000 条，避免无限增长
        lst = list(self._seen)[-5000:]
        self._seen = set(lst)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"seen": lst}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def is_new(self, aid) -> bool:
        return aid not in self._seen

    def add(self, aid):
        if aid is None:
            return
        self._seen.add(aid)
        self._save()

    def filter_new(self, activities: list) -> list:
        out = []
        for a in activities:
            aid = a.get("activityId")
            if aid is not None and self.is_new(aid):
                out.append(a)
        return out

    def __len__(self) -> int:
        return len(self._seen)
