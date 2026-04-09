"""
bot.py — Telegram-бот на aiogram 3.x.

Команды:
  /start_battle  — начать сбор участников в группе
  /swap_team     — (админ) перекинуть участника в другую команду
  /stop_battle   — (админ) досрочно завершить игру
  /help          — список всех команд
  Кнопка «Участвовать» — присоединиться к игре
  Кнопка «Открыть Карту» — WebApp с холстом

Фоновые задачи:
  • Через 2 мин после /start_battle — деление на команды и старт.
  • Каждые 2 часа — отправка статистики в чат.
  • Через 24 часа — завершение и объявление победителя.
  • При старте бота — восстановление активных игр из БД.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.token import TokenValidationError
from sqlalchemy import select

import canvas
from config import (
    BOT_TOKEN,
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    GAME_DURATION,
    RECRUIT_TIME,
    STATS_INTERVAL,
    TEAM_COLORS,
    TEAM_NAMES,
    WEBAPP_BASE_URL,
)
from models import Game, GameStatus, Player, async_session, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

# ════════════════════════════════════════════════════════
# КЕШ ПРАВ АДМИНИСТРАТОРА
# ════════════════════════════════════════════════════════
_admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}
ADMIN_CACHE_TTL = 60  # секунд

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Проверяет права администратора с кешированием на 60 секунд."""
    now = datetime.now(timezone.utc).timestamp()
    key = (chat_id, user_id)
    if key in _admin_cache:
        result, ts = _admin_cache[key]
        if now - ts < ADMIN_CACHE_TTL:
            return result
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        result = member.status in ("administrator", "creator")
    except Exception:
        result = False
    _admin_cache[key] = (result, now)
    return result

# Хранилище фоновых задач (чтобы их можно было отменить)
_background_tasks: dict[int, list[asyncio.Task]] = {}

# Хранилище message_id сообщений о сборе (game_id → (chat_id, message_id))
_recruit_messages: dict[int, tuple[int, int]] = {}


def _build_recruit_text(player_names: list[str]) -> str:
    """Формирует текст сообщения о сборе участников."""
    text = (
        "🎨 <b>Pixel Battle начинается!</b>\n\n"
        f"Сбор участников: <b>{RECRUIT_TIME // 60} минуты</b>.\n"
        "Нажмите кнопку ниже, чтобы присоединиться.\n"
    )

    if player_names:
        text += f"\n👥 <b>Участники ({len(player_names)}):</b>\n"
        for name in player_names:
            text += f"  • {name}\n"
    else:
        text += "\nПока никто не записался…\n"

    text += (
        "\n─────────────────────\n"
        "📋 <b>Команды бота:</b>\n"
        "/start_battle — начать новую игру\n"
        "/stats — текущая статистика\n"
        "/swap_team — перевести игрока (админ)\n"
        "/stop_battle — завершить игру (админ)\n"
        "/help — список команд"
    )
    return text


# ════════════════════════════════════════════════════════
# КОМАНДА /start_battle
# ════════════════════════════════════════════════════════
@router.message(Command("start_battle"))
async def cmd_start_battle(message: types.Message, bot: Bot) -> None:
    """Создаёт новую игру и запускает сбор участников."""
    chat_id = message.chat.id

    # Проверяем, нет ли уже активной игры в этом чате
    async with async_session() as session:
        existing = await session.execute(
            select(Game).where(
                Game.chat_id == chat_id,
                Game.status.in_([GameStatus.RECRUITING, GameStatus.ACTIVE]),
            )
        )
        if existing.scalar_one_or_none():
            await message.reply("⚠️ В этом чате уже идёт игра или сбор участников!")
            return

        game = Game(chat_id=chat_id, status=GameStatus.RECRUITING)
        session.add(game)
        await session.commit()
        await session.refresh(game)
        game_id = game.id

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✋ Участвовать",
                    callback_data=f"join:{game_id}",
                )
            ]
        ]
    )

    sent = await message.answer(
        _build_recruit_text([]),
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    # Сохраняем message_id чтобы обновлять список участников
    _recruit_messages[game_id] = (chat_id, sent.message_id)

    # Запускаем таймер сбора
    task = asyncio.create_task(_recruitment_timer(bot, chat_id, game_id))
    _background_tasks.setdefault(game_id, []).append(task)


# ════════════════════════════════════════════════════════
# CALLBACK «Участвовать»
# ════════════════════════════════════════════════════════
@router.callback_query(F.data.startswith("join:"))
async def cb_join(callback: types.CallbackQuery, bot: Bot) -> None:
    game_id = int(callback.data.split(":")[1])
    user = callback.from_user

    async with async_session() as session:
        game = await session.get(Game, game_id)
        if not game or game.status != GameStatus.RECRUITING:
            await callback.answer("Сбор уже завершён.", show_alert=True)
            return

        # Проверка: уже записан?
        exists = await session.execute(
            select(Player).where(
                Player.game_id == game_id,
                Player.user_id == user.id,
            )
        )
        if exists.scalar_one_or_none():
            await callback.answer("Вы уже в списке!", show_alert=True)
            return

        player = Player(
            game_id=game_id,
            user_id=user.id,
            username=user.username or user.first_name,
        )
        session.add(player)
        await session.commit()

        # Получаем обновлённый список участников
        all_players = await session.execute(
            select(Player).where(Player.game_id == game_id)
        )
        player_names = [p.username or str(p.user_id) for p in all_players.scalars()]

    await callback.answer(f"✅ {user.first_name}, вы записаны!", show_alert=False)

    # Обновляем сообщение о сборе — добавляем ник в список
    msg_info = _recruit_messages.get(game_id)
    if msg_info:
        chat_id, message_id = msg_info
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text=f"✋ Участвовать ({len(player_names)})",
                    callback_data=f"join:{game_id}",
                )
            ]]
        )
        try:
            await bot.edit_message_text(
                text=_build_recruit_text(player_names),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception:
            pass  # Сообщение могло быть удалено


# ════════════════════════════════════════════════════════
# АДМИН-КОМАНДА /swap_team
# ════════════════════════════════════════════════════════
# Использование:
#   /swap_team @username        — перекинуть участника в другую команду
#   /swap_team 123456789        — то же самое по user_id
#   Также можно ответить на сообщение участника: reply + /swap_team
#
# Доступно только администраторам и создателю чата.
# ════════════════════════════════════════════════════════
@router.message(Command("swap_team"))
async def cmd_swap_team(message: types.Message, bot: Bot, command: CommandObject) -> None:
    """Перекидывает участника в противоположную команду."""
    chat_id = message.chat.id
    caller_id = message.from_user.id

    # ── 1. Проверяем права вызывающего ─────────────────
    if not await is_admin(bot, chat_id, caller_id):
        await message.reply("🚫 Эта команда доступна только администраторам чата.")
        return

    # ── 2. Определяем целевого пользователя ────────────
    target_user_id: int | None = None
    target_display: str = ""

    # Способ A: reply на сообщение участника
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
        target_display = (
            f"@{message.reply_to_message.from_user.username}"
            if message.reply_to_message.from_user.username
            else message.reply_to_message.from_user.first_name
        )

    # Способ B: аргумент команды (@username или числовой id)
    elif command.args:
        arg = command.args.strip()
        if arg.startswith("@"):
            # По @username — ищем в базе среди участников текущей игры
            target_display = arg
            # username сохраняется без @
            username_clean = arg.lstrip("@")
            async with async_session() as session:
                game = (
                    await session.execute(
                        select(Game).where(
                            Game.chat_id == chat_id,
                            Game.status == GameStatus.ACTIVE,
                        )
                    )
                ).scalar_one_or_none()
                if game:
                    player_row = (
                        await session.execute(
                            select(Player).where(
                                Player.game_id == game.id,
                                Player.username == username_clean,
                            )
                        )
                    ).scalar_one_or_none()
                    if player_row:
                        target_user_id = player_row.user_id
        else:
            # Пробуем распарсить как числовой user_id
            try:
                target_user_id = int(arg)
                target_display = str(target_user_id)
            except ValueError:
                pass

    if target_user_id is None:
        await message.reply(
            "❓ <b>Как использовать:</b>\n\n"
            "• Ответьте (reply) на сообщение участника и напишите /swap_team\n"
            "• Или: <code>/swap_team @username</code>\n"
            "• Или: <code>/swap_team 123456789</code> (user ID)",
            parse_mode="HTML",
        )
        return

    # ── 3. Находим активную игру в этом чате ───────────
    async with async_session() as session:
        game = (
            await session.execute(
                select(Game).where(
                    Game.chat_id == chat_id,
                    Game.status == GameStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()

        if not game:
            await message.reply("⚠️ В этом чате нет активной игры.")
            return

        # ── 4. Находим игрока и меняем команду ─────────
        player = (
            await session.execute(
                select(Player).where(
                    Player.game_id == game.id,
                    Player.user_id == target_user_id,
                )
            )
        ).scalar_one_or_none()

        if not player:
            await message.reply(f"⚠️ Пользователь {target_display} не найден среди участников игры.")
            return

        old_team = player.team
        # Переключаем: 1 → 2, 2 → 1
        new_team = 2 if old_team == 1 else 1
        player.team = new_team
        await session.commit()

    old_name = TEAM_NAMES.get(old_team, "?")
    new_name = TEAM_NAMES.get(new_team, "?")

    await message.reply(
        f"🔄 <b>Перевод игрока</b>\n\n"
        f"Игрок {target_display} переведён:\n"
        f"{old_name} → {new_name}\n\n"
        f"<i>Изменение вступает в силу немедленно. "
        f"Игроку нужно переоткрыть Web App, чтобы увидеть новый цвет.</i>",
        parse_mode="HTML",
    )


# ════════════════════════════════════════════════════════
# КОМАНДА /teams
# ════════════════════════════════════════════════════════
@router.message(Command("teams"))
async def cmd_teams(message: types.Message) -> None:
    """Выводит список участников по командам."""
    chat_id = message.chat.id

    async with async_session() as session:
        game = (
            await session.execute(
                select(Game).where(
                    Game.chat_id == chat_id,
                    Game.status == GameStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()

        if not game:
            await message.reply("⚠️ В этом чате нет активной игры.")
            return

        players = list(game.players)

    red = [p.username or str(p.user_id) for p in players if p.team == 1]
    blue = [p.username or str(p.user_id) for p in players if p.team == 2]

    red_list = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(red)) or "  (пусто)"
    blue_list = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(blue)) or "  (пусто)"

    await message.reply(
        f"🔴 <b>Красные ({len(red)}):</b>\n{red_list}\n\n"
        f"🔵 <b>Синие ({len(blue)}):</b>\n{blue_list}",
        parse_mode="HTML",
    )


# ════════════════════════════════════════════════════════
# КОМАНДА /help
# ════════════════════════════════════════════════════════
@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    await message.reply(
        "📋 <b>Команды Pixel Battle</b>\n\n"
        "👤 <b>Для всех:</b>\n"
        "/start_battle — начать новую игру и сбор участников\n"
        "/stats — текущая статистика игры\n"
        "/teams — список команд и участников\n"
        "/help — показать это сообщение\n\n"
        "🔧 <b>Для администраторов:</b>\n"
        "/swap_team <code>@username</code> — перевести игрока в другую команду\n"
        "/stop_battle — досрочно завершить текущую игру\n",
        parse_mode="HTML",
    )


# ════════════════════════════════════════════════════════
# КОМАНДА /stats
# ════════════════════════════════════════════════════════
@router.message(Command("stats"))
async def cmd_stats(message: types.Message) -> None:
    """Выводит текущую статистику активной игры в чате."""
    chat_id = message.chat.id

    async with async_session() as session:
        game = (
            await session.execute(
                select(Game).where(
                    Game.chat_id == chat_id,
                    Game.status == GameStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()

        if not game:
            await message.reply("⚠️ В этом чате нет активной игры.")
            return

        game_id = game.id
        ends_at = game.ends_at
        players = list(game.players)

    counts = await canvas.count_pixels(game_id)
    total = counts[1] + counts[2]
    total_pixels = CANVAS_WIDTH * CANVAS_HEIGHT

    if total == 0:
        pct1, pct2 = 0.0, 0.0
    else:
        pct1 = round(counts[1] / total * 100, 1)
        pct2 = round(counts[2] / total * 100, 1)

    fill_pct = round(total / total_pixels * 100, 1)

    # Оставшееся время
    if ends_at:
        remaining = (ends_at - datetime.utcnow()).total_seconds()
        if remaining > 0:
            hours = int(remaining // 3600)
            mins = int((remaining % 3600) // 60)
            time_left = f"{hours}ч {mins}мин"
        else:
            time_left = "завершается…"
    else:
        time_left = "—"

    # Количество игроков по командам
    team1_count = sum(1 for p in players if p.team == 1)
    team2_count = sum(1 for p in players if p.team == 2)

    # Прогресс-бар
    bar_len = 20
    if total > 0:
        red_blocks = round(counts[1] / total * bar_len)
    else:
        red_blocks = 0
    blue_blocks = bar_len - red_blocks
    bar = "🟥" * red_blocks + "🟦" * blue_blocks

    await message.reply(
        f"📊 <b>Статистика игры</b>\n\n"
        f"{bar}\n\n"
        f"🔴 Красные: <b>{counts[1]}</b> px ({pct1}%) — {team1_count} игроков\n"
        f"🔵 Синие:   <b>{counts[2]}</b> px ({pct2}%) — {team2_count} игроков\n\n"
        f"🖌 Закрашено: {total} / {total_pixels} px ({fill_pct}%)\n"
        f"⏳ Осталось: <b>{time_left}</b>",
        parse_mode="HTML",
    )


# ════════════════════════════════════════════════════════
# АДМИН-КОМАНДА /stop_battle
# ════════════════════════════════════════════════════════
@router.message(Command("stop_battle"))
async def cmd_stop_battle(message: types.Message, bot: Bot) -> None:
    """Досрочно завершает текущую игру. Только для админов."""
    chat_id = message.chat.id
    caller_id = message.from_user.id

    # Проверяем права
    if not await is_admin(bot, chat_id, caller_id):
        await message.reply("🚫 Эта команда доступна только администраторам чата.")
        return

    async with async_session() as session:
        game = (
            await session.execute(
                select(Game).where(
                    Game.chat_id == chat_id,
                    Game.status.in_([GameStatus.RECRUITING, GameStatus.ACTIVE]),
                )
            )
        ).scalar_one_or_none()

        if not game:
            await message.reply("⚠️ В этом чате нет активной игры.")
            return

        game_id = game.id
        was_active = game.status == GameStatus.ACTIVE
        game.status = GameStatus.FINISHED
        await session.commit()

    # Отменяем фоновые задачи
    for t in _background_tasks.pop(game_id, []):
        t.cancel()

    if was_active:
        counts = await canvas.count_pixels(game_id)
        if counts[1] > counts[2]:
            winner = TEAM_NAMES[1]
        elif counts[2] > counts[1]:
            winner = TEAM_NAMES[2]
        else:
            winner = "Ничья! 🤝"

        await message.reply(
            f"🛑 <b>Игра досрочно завершена администратором!</b>\n\n"
            f"🔴 Красные: {counts[1]} px\n"
            f"🔵 Синие:   {counts[2]} px\n\n"
            f"🏆 Победитель: <b>{winner}</b>",
            parse_mode="HTML",
        )
    else:
        await message.reply("🛑 Сбор участников отменён администратором.")


# ════════════════════════════════════════════════════════
# ТАЙМЕР СБОРА → ДЕЛЕНИЕ НА КОМАНДЫ
# ════════════════════════════════════════════════════════
async def _recruitment_timer(bot: Bot, chat_id: int, game_id: int) -> None:
    await asyncio.sleep(RECRUIT_TIME)

    async with async_session() as session:
        game = await session.get(Game, game_id)
        if not game or game.status != GameStatus.RECRUITING:
            return

        players = list(game.players)
        if len(players) < 2:
            game.status = GameStatus.FINISHED
            await session.commit()
            await bot.send_message(chat_id, "😔 Недостаточно участников (нужно минимум 2). Игра отменена.")
            return

        # Перемешиваем и делим пополам
        random.shuffle(players)
        half = len(players) // 2
        for i, p in enumerate(players):
            p.team = 1 if i < half else 2

        now = datetime.utcnow()
        game.status = GameStatus.ACTIVE
        game.started_at = now
        game.ends_at = now + timedelta(seconds=GAME_DURATION)
        await session.commit()

    # Создаём холст в Redis
    await canvas.create_canvas(game_id)

    # Формируем список команд
    team_lines = {1: [], 2: []}
    for p in players:
        team_lines[p.team].append(f"  • {p.username or p.user_id}")

    teams_text = ""
    for tid, name in TEAM_NAMES.items():
        members = "\n".join(team_lines.get(tid, []))
        teams_text += f"\n{name}:\n{members}\n"

    # Кнопка WebApp
    webapp_url = f"https://t.me/pixelbattlesavage_bot/play?startapp={game_id}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗺 Открыть Карту",
                    url=webapp_url,
                )
            ]
        ]
    )

    await bot.send_message(
        chat_id,
        f"⚔️ <b>Битва началась!</b>\n{teams_text}\n"
        f"Игра продлится <b>24 часа</b>. Ставьте пиксели!",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    # Запускаем фоновые задачи: статистика + завершение
    tasks = _background_tasks.setdefault(game_id, [])
    tasks.append(asyncio.create_task(_stats_loop(bot, chat_id, game_id)))
    tasks.append(asyncio.create_task(_game_end_timer(bot, chat_id, game_id)))


# ════════════════════════════════════════════════════════
# ПЕРИОДИЧЕСКАЯ СТАТИСТИКА
# ════════════════════════════════════════════════════════
async def _stats_loop(bot: Bot, chat_id: int, game_id: int) -> None:
    """Каждые STATS_INTERVAL секунд шлёт статистику в чат."""
    while True:
        await asyncio.sleep(STATS_INTERVAL)

        async with async_session() as session:
            game = await session.get(Game, game_id)
            if not game or game.status != GameStatus.ACTIVE:
                return

        counts = await canvas.count_pixels(game_id)
        total = counts[1] + counts[2]
        if total == 0:
            pct1, pct2 = 0, 0
        else:
            pct1 = round(counts[1] / total * 100, 1)
            pct2 = round(counts[2] / total * 100, 1)

        await bot.send_message(
            chat_id,
            f"📊 <b>Промежуточная статистика</b>\n\n"
            f"🔴 Красные: {counts[1]} px ({pct1}%)\n"
            f"🔵 Синие:   {counts[2]} px ({pct2}%)\n"
            f"Всего закрашено: {total} px",
            parse_mode="HTML",
        )


# ════════════════════════════════════════════════════════
# ЗАВЕРШЕНИЕ ИГРЫ
# ════════════════════════════════════════════════════════
async def _game_end_timer(bot: Bot, chat_id: int, game_id: int) -> None:
    await asyncio.sleep(GAME_DURATION)

    async with async_session() as session:
        game = await session.get(Game, game_id)
        if not game or game.status != GameStatus.ACTIVE:
            return
        game.status = GameStatus.FINISHED
        await session.commit()

    counts = await canvas.count_pixels(game_id)
    if counts[1] > counts[2]:
        winner = TEAM_NAMES[1]
    elif counts[2] > counts[1]:
        winner = TEAM_NAMES[2]
    else:
        winner = "Ничья! 🤝"

    await bot.send_message(
        chat_id,
        f"🏁 <b>Игра окончена!</b>\n\n"
        f"🔴 Красные: {counts[1]} px\n"
        f"🔵 Синие:   {counts[2]} px\n\n"
        f"🏆 Победитель: <b>{winner}</b>",
        parse_mode="HTML",
    )

    # Очистка задач
    for t in _background_tasks.pop(game_id, []):
        t.cancel()


# ════════════════════════════════════════════════════════
# ВОССТАНОВЛЕНИЕ АКТИВНЫХ ИГР ПОСЛЕ РЕСТАРТА
# ════════════════════════════════════════════════════════
async def _resume_active_games(bot: Bot) -> None:
    """
    При старте бота проверяет БД на активные/рекрутинговые игры
    и восстанавливает для них фоновые задачи (таймеры).
    """
    async with async_session() as session:
        # ── Восстанавливаем RECRUITING игры ────────────
        recruiting = await session.execute(
            select(Game).where(Game.status == GameStatus.RECRUITING)
        )
        for game in recruiting.scalars():
            elapsed = (datetime.utcnow() - game.created_at).total_seconds()
            remaining = max(0, RECRUIT_TIME - elapsed)

            if remaining <= 0:
                # Время сбора истекло пока бот был выключен — завершаем
                game.status = GameStatus.FINISHED
                await session.commit()
                logger.info("Game %s: recruitment expired, cancelled", game.id)
            else:
                logger.info("Game %s: resuming recruitment timer (%ds left)", game.id, int(remaining))
                task = asyncio.create_task(
                    _delayed_recruitment(bot, game.chat_id, game.id, remaining)
                )
                _background_tasks.setdefault(game.id, []).append(task)

        # ── Восстанавливаем ACTIVE игры ────────────────
        active = await session.execute(
            select(Game).where(Game.status == GameStatus.ACTIVE)
        )
        for game in active.scalars():
            if not game.ends_at:
                continue

            remaining = (game.ends_at - datetime.utcnow()).total_seconds()

            if remaining <= 0:
                # Игра должна была завершиться — завершаем
                game.status = GameStatus.FINISHED
                await session.commit()
                counts = await canvas.count_pixels(game.id)
                if counts[1] > counts[2]:
                    winner = TEAM_NAMES[1]
                elif counts[2] > counts[1]:
                    winner = TEAM_NAMES[2]
                else:
                    winner = "Ничья! 🤝"
                await bot.send_message(
                    game.chat_id,
                    f"🏁 <b>Игра окончена!</b> (результат после рестарта)\n\n"
                    f"🔴 Красные: {counts[1]} px\n"
                    f"🔵 Синие:   {counts[2]} px\n\n"
                    f"🏆 Победитель: <b>{winner}</b>",
                    parse_mode="HTML",
                )
                logger.info("Game %s: was finished during downtime", game.id)
            else:
                # Восстанавливаем таймеры
                logger.info("Game %s: resuming (%ds left)", game.id, int(remaining))
                tasks = _background_tasks.setdefault(game.id, [])
                tasks.append(asyncio.create_task(
                    _stats_loop(bot, game.chat_id, game.id)
                ))
                tasks.append(asyncio.create_task(
                    _game_end_timer_remaining(bot, game.chat_id, game.id, remaining)
                ))

                # Шлём уведомление о восстановлении
                try:
                    webapp_url = f"https://t.me/pixelbattlesavage_bot/play?startapp={game.id}"
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(
                                text="🗺 Открыть Карту",
                                url=webapp_url,
                            )
                        ]]
                    )
                    await bot.send_message(
                        game.chat_id,
                        f"🔄 <b>Бот перезапущен — игра продолжается!</b>\n"
                        f"Осталось: <b>{int(remaining // 3600)}ч {int((remaining % 3600) // 60)}мин</b>",
                        reply_markup=keyboard,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("Resume message failed for game %s: %s", game.id, e)
                    await bot.send_message(
                        game.chat_id,
                        f"🔄 <b>Бот перезапущен — игра продолжается!</b>\n"
                        f"Осталось: <b>{int(remaining // 3600)}ч {int((remaining % 3600) // 60)}мин</b>",
                        parse_mode="HTML",
                    )


async def _delayed_recruitment(bot: Bot, chat_id: int, game_id: int, delay: float) -> None:
    """Recruitment timer с произвольной задержкой (для восстановления)."""
    await asyncio.sleep(delay)
    # Дальше — та же логика что в _recruitment_timer, начиная с деления
    async with async_session() as session:
        game = await session.get(Game, game_id)
        if not game or game.status != GameStatus.RECRUITING:
            return

        players = list(game.players)
        if len(players) < 2:
            game.status = GameStatus.FINISHED
            await session.commit()
            await bot.send_message(chat_id, "😔 Недостаточно участников (нужно минимум 2). Игра отменена.")
            return

        random.shuffle(players)
        half = len(players) // 2
        for i, p in enumerate(players):
            p.team = 1 if i < half else 2

        now = datetime.utcnow()
        game.status = GameStatus.ACTIVE
        game.started_at = now
        game.ends_at = now + timedelta(seconds=GAME_DURATION)
        await session.commit()

    await canvas.create_canvas(game_id)

    team_lines = {1: [], 2: []}
    for p in players:
        team_lines[p.team].append(f"  • {p.username or p.user_id}")

    teams_text = ""
    for tid, name in TEAM_NAMES.items():
        members = "\n".join(team_lines.get(tid, []))
        teams_text += f"\n{name}:\n{members}\n"

    webapp_url = f"https://t.me/pixelbattlesavage_bot/play?startapp={game_id}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🗺 Открыть Карту",
                url=webapp_url,
            )
        ]]
    )

    await bot.send_message(
        chat_id,
        f"⚔️ <b>Битва началась!</b>\n{teams_text}\n"
        f"Игра продлится <b>24 часа</b>. Ставьте пиксели!",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    tasks = _background_tasks.setdefault(game_id, [])
    tasks.append(asyncio.create_task(_stats_loop(bot, chat_id, game_id)))
    tasks.append(asyncio.create_task(_game_end_timer(bot, chat_id, game_id)))


async def _game_end_timer_remaining(bot: Bot, chat_id: int, game_id: int, remaining: float) -> None:
    """Таймер завершения с произвольным оставшимся временем."""
    await asyncio.sleep(remaining)
    # Та же логика завершения
    async with async_session() as session:
        game = await session.get(Game, game_id)
        if not game or game.status != GameStatus.ACTIVE:
            return
        game.status = GameStatus.FINISHED
        await session.commit()

    counts = await canvas.count_pixels(game_id)
    if counts[1] > counts[2]:
        winner = TEAM_NAMES[1]
    elif counts[2] > counts[1]:
        winner = TEAM_NAMES[2]
    else:
        winner = "Ничья! 🤝"

    await bot.send_message(
        chat_id,
        f"🏁 <b>Игра окончена!</b>\n\n"
        f"🔴 Красные: {counts[1]} px\n"
        f"🔵 Синие:   {counts[2]} px\n\n"
        f"🏆 Победитель: <b>{winner}</b>",
        parse_mode="HTML",
    )

    for t in _background_tasks.pop(game_id, []):
        t.cancel()


# ════════════════════════════════════════════════════════
# ЗАПУСК
# ════════════════════════════════════════════════════════
async def main() -> None:
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Восстанавливаем активные игры
    await _resume_active_games(bot)

    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
