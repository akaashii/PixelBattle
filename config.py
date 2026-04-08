"""
config.py — Единая точка конфигурации.
Все секреты читаются из переменных окружения.
"""

import os

# ── Telegram ────────────────────────────────────────────
BOT_TOKEN: str = os.environ["BOT_TOKEN"]

# URL, по которому доступен FastAPI-бэкенд (для формирования WebApp-ссылки)
WEBAPP_BASE_URL: str = os.environ.get("WEBAPP_BASE_URL", "https://pixel.example.com")

# ── Redis ───────────────────────────────────────────────
REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# ── PostgreSQL ──────────────────────────────────────────
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://pixel:pixel@localhost:5432/pixel_battle"
)

# ── Игровые константы ──────────────────────────────────
CANVAS_WIDTH: int = 100        # пикселей
CANVAS_HEIGHT: int = 100
PIXEL_COOLDOWN: int = 60       # секунд
RECRUIT_TIME: int = 120        # секунд (2 минуты сбор)
GAME_DURATION: int = 86_400    # секунд (24 часа)
STATS_INTERVAL: int = 7_200    # секунд (каждые 2 часа)

# Цвета команд (индекс → название)
TEAM_NAMES: dict[int, str] = {1: "🔴 Красные", 2: "🔵 Синие"}
TEAM_COLORS: dict[int, str] = {1: "#E63946", 2: "#457B9D"}
