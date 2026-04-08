# 🎨 Pixel Battle — Telegram Mini App

Pixel Battle для групповых чатов Telegram. Две команды захватывают холст 1000×1000, рисуя пиксель-арты палитрой из 32 цветов. Побеждает команда, захватившая больше пикселей за 24 часа.

## Стек

- **Bot**: Python, aiogram 3.x
- **Backend**: FastAPI + WebSocket
- **Frontend**: Telegram Mini App (HTML5 Canvas)
- **Storage**: Redis (холст) + PostgreSQL (мета-данные)
- **Deploy**: Docker Compose + Nginx

## Быстрый старт

1. Создайте бота через @BotFather, получите токен
2. Создайте WebApp через `/newapp` в BotFather
3. Зарегистрируйте домен (например, DuckDNS)
4. Склонируйте репозиторий на VPS:

```bash
git clone https://github.com/YOUR_USERNAME/pixel-battle.git
cd pixel-battle
chmod +x deploy.sh
sudo ./deploy.sh
```

## Команды бота

| Команда | Доступ | Описание |
|---------|--------|----------|
| `/start_battle` | все | Начать новую игру |
| `/stats` | все | Текущая статистика |
| `/teams` | все | Список команд и участников |
| `/help` | все | Список команд |
| `/swap_team @user` | админ | Перевести игрока в другую команду |
| `/stop_battle` | админ | Досрочно завершить игру |

## Структура проекта

```
pixel-battle/
├── bot.py              # Telegram-бот (aiogram 3.x)
├── main.py             # FastAPI backend + WebSocket
├── canvas.py           # Redis-сервис для холста (32-цветная палитра)
├── models.py           # SQLAlchemy-модели (PostgreSQL)
├── config.py           # Конфигурация
├── frontend/
│   └── index.html      # Telegram Mini App (Canvas + палитра)
├── deploy.sh           # Скрипт развёртывания на Ubuntu VPS
├── docker-compose.yml  # Docker Compose (production)
├── Dockerfile
├── requirements.txt
└── ARCHITECTURE.md     # Подробная архитектура
```

## Лицензия

MIT
