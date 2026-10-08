"""Закрытые списки из config/taxonomy.yaml: СКЮ, форматы, вердикты, темы, каналы.

В базе и в ответе модели — ключи, на экране — подписи (ARCHITECTURE.md, «Данные»).
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from cusdev.config import get_settings

_LISTS = ("skus", "formats", "verdicts", "themes", "channels")


@dataclass(frozen=True)
class Taxonomy:
    skus: dict[str, str]
    formats: dict[str, str]
    verdicts: dict[str, str]
    themes: dict[str, str]
    channels: dict[str, str]
    default_channel: str

    @classmethod
    def load(cls, path: Path) -> "Taxonomy":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        lists = {}
        for name in _LISTS:
            items = raw.get(name) or {}
            if not items:
                raise ValueError(f"taxonomy: список {name!r} пуст")
            bad = [k for k in items if not str(k).isascii() or not str(k).islower()]
            if bad:
                raise ValueError(
                    f"taxonomy: ключи {name} должны быть латиницей в нижнем регистре: {bad}"
                )
            lists[name] = {str(k): str(v) for k, v in items.items()}
        default_channel = raw.get("default_channel", "")
        if default_channel not in lists["channels"]:
            raise ValueError(f"taxonomy: default_channel {default_channel!r} нет среди channels")
        return cls(**lists, default_channel=default_channel)

    def label(self, kind: str, key: str | None) -> str:
        """Подпись для экрана; неизвестный ключ показываем как есть, а не падаем."""
        if key is None:
            return "—"
        return getattr(self, kind).get(key, key)


@lru_cache
def get_taxonomy() -> Taxonomy:
    return Taxonomy.load(get_settings().taxonomy_path)
