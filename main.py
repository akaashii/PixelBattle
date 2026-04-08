"""
main.py — FastAPI-бэкенд для Pixel Battle.

Отвечает за:
  1. Отдачу статики (index.html) — Web App.
  2. REST: GET /api/canvas/{game_id} — полный холст для начальной загрузки.
  3. REST: GET /api/me?game_id=...&user_id=... — данные игрока (команда, кулдаун).
  4. WebSocket /ws/{game_id} — приём пикселей и broadcast дельт.

Валидация на сервере:
  • Кулдаун 60 сек (Redis SET NX EX — атомарно).
  • Принадлежность к команде (PostgreSQL).
  • Статус игры == ACTIVE.
  • Координаты в пределах поля.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, unquote

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

import canvas
from canvas import CooldownError, InvalidColorError, OutOfBoundsError, PALETTE
from config import (
    BOT_TOKEN,
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    PIXEL_COOLDOWN,
    TEAM_COLORS,
    TEAM_NAMES,
)
from models import Game, GameStatus, Player, async_session, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════
# МЕНЕДЖЕР ПОДКЛЮЧЕНИЙ (WebSocket)
# ════════════════════════════════════════════════════════
class ConnectionManager:
    """Управляет WebSocket-подключениями, сгруппированными по game_id."""

    def __init__(self) -> None:
        # game_id → set of active WebSocket connections
        self._rooms: dict[int, set[WebSocket]] = {}

    async def connect(self, game_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self._rooms.setdefault(game_id, set()).add(ws)
        logger.info("WS connected: game=%s, total=%s", game_id, len(self._rooms[game_id]))

    def disconnect(self, game_id: int, ws: WebSocket) -> None:
        room = self._rooms.get(game_id)
        if room:
            room.discard(ws)
            if not room:
                del self._rooms[game_id]

    async def broadcast(self, game_id: int, data: dict[str, Any]) -> None:
        """Рассылает JSON-дельту всем подключённым клиентам в комнате."""
        room = self._rooms.get(game_id)
        if not room:
            return
        payload = json.dumps(data)
        dead: list[WebSocket] = []
        for ws in room:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            room.discard(ws)


manager = ConnectionManager()


# ════════════════════════════════════════════════════════
# LIFESPAN (startup/shutdown)
# ════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await canvas.get_redis()  # инициализация пула
    yield
    r = await canvas.get_redis()
    await r.aclose()


app = FastAPI(title="Pixel Battle", lifespan=lifespan)

# Статика (фронтенд)
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")


# ════════════════════════════════════════════════════════
# ВАЛИДАЦИЯ TELEGRAM INIT DATA
# ════════════════════════════════════════════════════════
def validate_telegram_init_data(init_data: str) -> dict | None:
    """
    Проверяет подпись initData от Telegram WebApp.
    Возвращает распарсенные данные или None если невалидно.

    Это КРИТИЧЕСКИ ВАЖНО для безопасности — без этой проверки
    любой может подделать user_id и team.
    """
    try:
        parsed = parse_qs(init_data)
        received_hash = parsed.get("hash", [None])[0]
        if not received_hash:
            return None

        # Собираем data_check_string (все параметры кроме hash, отсортированы)
        items = []
        for key, values in parsed.items():
            if key == "hash":
                continue
            items.append(f"{key}={unquote(values[0])}")
        items.sort()
        data_check_string = "\n".join(items)

        # HMAC-SHA256
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            return None

        # Парсим user
        user_raw = parsed.get("user", [None])[0]
        if user_raw:
            parsed["user_parsed"] = json.loads(unquote(user_raw))

        return parsed
    except Exception:
        return None


# ════════════════════════════════════════════════════════
# СТРАНИЦА WEB APP
# ════════════════════════════════════════════════════════
@app.get("/app")
async def serve_webapp():
    """Отдаёт HTML-файл Telegram Web App."""
    return FileResponse("frontend/index.html", media_type="text/html")


# ════════════════════════════════════════════════════════
# REST API
# ════════════════════════════════════════════════════════
@app.get("/api/canvas/{game_id}")
async def get_canvas(game_id: int):
    """
    Возвращает весь холст как base64-строку.
    Клиент декодирует и рисует на Canvas.
    """
    data = await canvas.get_canvas(game_id)
    encoded = base64.b64encode(data).decode("ascii")
    return JSONResponse({
        "width": CANVAS_WIDTH,
        "height": CANVAS_HEIGHT,
        "data": encoded,  # base64, 2 bytes per pixel: [team, color_index]
        "bytes_per_pixel": 2,
        "palette": PALETTE,
        "teams": {str(k): {"name": v, "color": TEAM_COLORS[k]} for k, v in TEAM_NAMES.items()},
    })


@app.get("/api/me")
async def get_player_info(game_id: int = Query(...), user_id: int = Query(...)):
    """Возвращает информацию об игроке: команда, оставшийся кулдаун."""
    async with async_session() as session:
        result = await session.execute(
            select(Player).where(
                Player.game_id == game_id,
                Player.user_id == user_id,
            )
        )
        player = result.scalar_one_or_none()

    if not player:
        return JSONResponse({"error": "Player not found"}, status_code=404)

    # Проверяем кулдаун
    r = await canvas.get_redis()
    cd_key = canvas._cooldown_key(game_id, user_id)
    ttl = await r.ttl(cd_key)
    cooldown_remaining = max(ttl, 0)

    return JSONResponse({
        "user_id": user_id,
        "team": player.team,
        "team_name": TEAM_NAMES.get(player.team, "?"),
        "team_color": TEAM_COLORS.get(player.team, "#999"),
        "cooldown_remaining": cooldown_remaining,
        "cooldown_total": PIXEL_COOLDOWN,
    })


# ════════════════════════════════════════════════════════
# WEBSOCKET — РЕАЛТАЙМ СИНХРОНИЗАЦИЯ
# ════════════════════════════════════════════════════════
@app.websocket("/ws/{game_id}")
async def websocket_endpoint(ws: WebSocket, game_id: int):
    """
    Клиент подключается и может:
      • Получать broadcast дельт ({"type":"pixel", ...}).
      • Отправлять {"type":"place", "x": N, "y": N, "user_id": N}.

    Сервер валидирует ВСЁ:
      1. Игра существует и ACTIVE.
      2. Игрок принадлежит к игре и имеет команду.
      3. Координаты в пределах поля.
      4. Кулдаун не нарушен (Redis SET NX EX).
    """
    await manager.connect(game_id, ws)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "msg": "Invalid JSON"}))
                continue

            if msg.get("type") != "place":
                continue

            user_id = msg.get("user_id")
            x = msg.get("x")
            y = msg.get("y")
            color_index = msg.get("color", 27)  # default: чёрный

            if not all(isinstance(v, int) for v in [user_id, x, y, color_index]):
                await ws.send_text(json.dumps({"type": "error", "msg": "Invalid params"}))
                continue

            # ── Проверка: игра активна? ──
            async with async_session() as session:
                game = await session.get(Game, game_id)
                if not game or game.status != GameStatus.ACTIVE:
                    await ws.send_text(json.dumps({"type": "error", "msg": "Game not active"}))
                    continue

                # ── Проверка: игрок в этой игре? ──
                result = await session.execute(
                    select(Player).where(
                        Player.game_id == game_id,
                        Player.user_id == user_id,
                    )
                )
                player = result.scalar_one_or_none()

            if not player or not player.team:
                await ws.send_text(json.dumps({"type": "error", "msg": "Not a participant"}))
                continue

            # ── Ставим пиксель (с проверкой кулдауна) ──
            try:
                cd = await canvas.place_pixel(game_id, user_id, x, y, player.team, color_index)
            except CooldownError as e:
                await ws.send_text(json.dumps({
                    "type": "cooldown",
                    "remaining": e.remaining,
                }))
                continue
            except (OutOfBoundsError, InvalidColorError):
                await ws.send_text(json.dumps({"type": "error", "msg": "Invalid pixel"}))
                continue

            # ── Успех: рассылаем дельту всем клиентам ──
            delta = {
                "type": "pixel",
                "x": x,
                "y": y,
                "team": player.team,
                "color": color_index,
                "user_id": user_id,
                "cooldown": cd,
            }
            await manager.broadcast(game_id, delta)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("WS error: %s", e)
    finally:
        manager.disconnect(game_id, ws)
