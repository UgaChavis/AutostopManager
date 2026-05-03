"""Базовый интерфейс коннектора источника.

Реальные ключи, логины и пароли нельзя хранить в коде. Коннекторы должны читать секреты
из переменных окружения или защищенного vault (хранилище секретов).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from models import Offer, PartRequest


@dataclass
class SourceQuery:
    source_id: str
    query_type: str
    article: str | None = None
    brand: str | None = None
    include_crosses: bool = False
    city: str = "Красноярск"
    raw: dict[str, Any] | None = None


class PartsSourceConnector(ABC):
    source_id: str

    @abstractmethod
    def search(self, request: PartRequest, query: SourceQuery) -> list[Offer]:
        """Вернуть список предложений в единой модели Offer."""
        raise NotImplementedError

    def check_rate_limit(self) -> None:
        """Проверить лимиты источника. Не обходить ограничения."""
        return None

    def healthcheck(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "status": "not_implemented"}


class ManualOnlyConnector(PartsSourceConnector):
    """Коннектор для источников, где автоматический сбор запрещен или не подключен."""

    def search(self, request: PartRequest, query: SourceQuery) -> list[Offer]:
        return []
