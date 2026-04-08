"""
canvas.py — Сервис работы с холстом через Redis.

Формат хранения: 2 байта на пиксель.
  Байт 0: team (0=пусто, 1=команда1, 2=команда2)
  Байт 1: color_index (индекс цвета в палитре, 0-31)

Итого: 100x100 = 20 KB, 500x500 = 500 KB.

Палитра — 32 цвета, вдохновлённая reddit r/place.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from config import CANVAS_HEIGHT, CANVAS_WIDTH, PIXEL_COOLDOWN, REDIS_URL

pool: aioredis.Redis | None = None

# ── 32-цветная палитра (r/place-стиль) ─────────────────
PALETTE: list[str] = [
    "#6D001A",  #  0 — бордовый
    "#BE0039",  #  1 — красный
    "#FF4500",  #  2 — оранжево-красный
    "#FFA800",  #  3 — оранжевый
    "#FFD635",  #  4 — жёлтый
    "#FFF8B8",  #  5 — кремовый
    "#00A368",  #  6 — зелёный
    "#00CC78",  #  7 — светло-зелёный
    "#7EED56",  #  8 — салатовый
    "#00756F",  #  9 — тёмно-бирюзовый
    "#009EAA",  # 10 — бирюзовый
    "#00CCC0",  # 11 — аквамарин
    "#2450A4",  # 12 — тёмно-синий
    "#3690EA",  # 13 — синий
    "#51E9F4",  # 14 — голубой
    "#493AC1",  # 15 — индиго
    "#6A5CFF",  # 16 — фиолетовый
    "#94B3FF",  # 17 — лавандовый
    "#811E9F",  # 18 — пурпурный
    "#B44AC0",  # 19 — сиреневый
    "#E4ABFF",  # 20 — розово-лиловый
    "#DE107F",  # 21 — маджента
    "#FF3881",  # 22 — розовый
    "#FF99AA",  # 23 — светло-розовый
    "#6D482F",  # 24 — коричневый
    "#9C6926",  # 25 — охра
    "#FFB470",  # 26 — персиковый
    "#000000",  # 27 — чёрный
    "#515252",  # 28 — тёмно-серый
    "#898D90",  # 29 — серый
    "#D4D7D9",  # 30 — светло-серый
    "#FFFFFF",  # 31 — белый
]

BYTES_PER_PIXEL = 2


async def get_redis() -> aioredis.Redis:
    global pool
    if pool is None:
        pool = aioredis.from_url(REDIS_URL, decode_responses=False)
    return pool


def _canvas_key(game_id: int) -> str:
    return f"canvas:{game_id}"


def _cooldown_key(game_id: int, user_id: int) -> str:
    return f"cd:{game_id}:{user_id}"


async def create_canvas(game_id: int) -> None:
    r = await get_redis()
    size = CANVAS_WIDTH * CANVAS_HEIGHT * BYTES_PER_PIXEL
    await r.set(_canvas_key(game_id), b"\x00" * size)


async def get_canvas(game_id: int) -> bytes:
    r = await get_redis()
    data = await r.get(_canvas_key(game_id))
    if data is None:
        size = CANVAS_WIDTH * CANVAS_HEIGHT * BYTES_PER_PIXEL
        return b"\x00" * size
    return data


class CooldownError(Exception):
    def __init__(self, remaining: int):
        self.remaining = remaining
        super().__init__(f"Cooldown: {remaining}s remaining")


class OutOfBoundsError(Exception):
    pass


class InvalidColorError(Exception):
    pass


async def place_pixel(
    game_id: int,
    user_id: int,
    x: int,
    y: int,
    team: int,
    color_index: int,
) -> int:
    if not (0 <= x < CANVAS_WIDTH and 0 <= y < CANVAS_HEIGHT):
        raise OutOfBoundsError(f"({x}, {y}) out of bounds")

    if not (0 <= color_index < len(PALETTE)):
        raise InvalidColorError(f"color_index {color_index} out of range")

    r = await get_redis()

    cd_key = _cooldown_key(game_id, user_id)
    ok = await r.set(cd_key, b"1", nx=True, ex=PIXEL_COOLDOWN)
    if not ok:
        ttl = await r.ttl(cd_key)
        raise CooldownError(remaining=max(ttl, 1))

    offset = (y * CANVAS_WIDTH + x) * BYTES_PER_PIXEL
    await r.setrange(_canvas_key(game_id), offset, bytes([team, color_index]))

    return PIXEL_COOLDOWN


async def count_pixels(game_id: int) -> dict[int, int]:
    """Считает захваченные пиксели по team-байту. Цвет не важен."""
    data = await get_canvas(game_id)
    counts: dict[int, int] = {1: 0, 2: 0}
    for i in range(0, len(data), BYTES_PER_PIXEL):
        team = data[i]
        if team in counts:
            counts[team] += 1
    return counts
