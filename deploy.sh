#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# deploy.sh — Полное развёртывание Pixel Battle на чистом
#              Ubuntu 22/24 VPS (только IP, без домена).
#
# Запуск:
#   1. Скопируйте всю папку pixel-battle на VPS
#   2. chmod +x deploy.sh
#   3. sudo ./deploy.sh
#
# Скрипт:
#   • Полностью очищает сервер (Docker, Nginx, БД, кэши)
#   • Обновляет систему
#   • Ставит Docker + Docker Compose
#   • Ставит Nginx
#   • Генерирует самоподписанный SSL-сертификат
#   • Настраивает Nginx как HTTPS-прокси → FastAPI :8000
#   • Просит ввести BOT_TOKEN
#   • Запускает всё через docker compose
# ═══════════════════════════════════════════════════════════

set -euo pipefail

# ── Цвета для вывода ──────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Проверка root ─────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "Запустите с sudo:  sudo ./deploy.sh"
fi

# ── Определяем IP сервера ─────────────────────────────
SERVER_IP=$(hostname -I | awk '{print $1}')
info "Обнаружен IP сервера: ${SERVER_IP}"

# ── Папка проекта ─────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
info "Папка проекта: ${PROJECT_DIR}"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  PIXEL BATTLE — Развёртывание на VPS"
echo "═══════════════════════════════════════════════════"
echo ""

# ══════════════════════════════════════════════════════
# 0. ПОЛНАЯ ОЧИСТКА СЕРВЕРА
# ══════════════════════════════════════════════════════
echo ""
echo -e "${RED}⚠️  ВНИМАНИЕ: Сейчас будет полная очистка сервера!${NC}"
echo "   Будут удалены: Docker (все контейнеры, образы, тома),"
echo "   Nginx, PostgreSQL, Redis, Python-окружения, старые данные."
echo ""
echo -e "${YELLOW}SSH-доступ и системные пакеты НЕ будут затронуты.${NC}"
echo ""
read -rp "Продолжить очистку? (y/N): " CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
    err "Отменено пользователем"
fi

info "Шаг 0/7 — Полная очистка сервера..."

# ── Docker: остановить и удалить всё ─────────────────
if command -v docker &>/dev/null; then
    info "Останавливаем все Docker-контейнеры..."
    docker stop $(docker ps -aq) 2>/dev/null || true
    docker rm -f $(docker ps -aq) 2>/dev/null || true

    info "Удаляем все Docker-образы, тома, сети..."
    docker system prune -af --volumes 2>/dev/null || true

    info "Удаляем Docker..."
    apt-get purge -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin \
        docker docker-engine docker.io containerd runc 2>/dev/null || true
    rm -rf /var/lib/docker /var/lib/containerd
    rm -rf /etc/docker
    rm -f /etc/apt/sources.list.d/docker.list
    rm -f /etc/apt/keyrings/docker.gpg
fi

# ── Nginx: удалить полностью ─────────────────────────
if command -v nginx &>/dev/null; then
    info "Удаляем Nginx..."
    systemctl stop nginx 2>/dev/null || true
    apt-get purge -y -qq nginx nginx-common nginx-full 2>/dev/null || true
    rm -rf /etc/nginx
fi

# ── PostgreSQL: удалить если стоял вне Docker ────────
if dpkg -l | grep -q postgresql; then
    info "Удаляем PostgreSQL..."
    systemctl stop postgresql 2>/dev/null || true
    apt-get purge -y -qq 'postgresql*' 2>/dev/null || true
    rm -rf /var/lib/postgresql /etc/postgresql
fi

# ── Redis: удалить если стоял вне Docker ─────────────
if dpkg -l | grep -q redis; then
    info "Удаляем Redis..."
    systemctl stop redis-server 2>/dev/null || true
    apt-get purge -y -qq 'redis*' 2>/dev/null || true
    rm -rf /var/lib/redis /etc/redis
fi

# ── Python-окружения и pip-пакеты ────────────────────
info "Удаляем Python venv и pip-кэш..."
find /root /home -maxdepth 3 -type d -name ".venv" -exec rm -rf {} + 2>/dev/null || true
find /root /home -maxdepth 3 -type d -name "venv" -exec rm -rf {} + 2>/dev/null || true
rm -rf /root/.cache/pip /root/.local/lib/python* 2>/dev/null || true

# ── Старые SSL-сертификаты ───────────────────────────
rm -rf /etc/nginx/ssl 2>/dev/null || true

# ── Очистка apt ──────────────────────────────────────
apt-get autoremove -y -qq 2>/dev/null || true
apt-get autoclean -y -qq 2>/dev/null || true

ok "Сервер полностью очищен"
echo ""

# ══════════════════════════════════════════════════════
# 1. ОБНОВЛЕНИЕ СИСТЕМЫ
# ══════════════════════════════════════════════════════
info "Шаг 1/7 — Обновление системы..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
ok "Система обновлена"

# ══════════════════════════════════════════════════════
# 2. УСТАНОВКА DOCKER
# ══════════════════════════════════════════════════════
info "Шаг 2/7 — Установка Docker..."

if command -v docker &>/dev/null; then
    ok "Docker уже установлен: $(docker --version)"
else
    # Удаляем старые версии если есть
    apt-get remove -y -qq docker docker-engine docker.io containerd runc 2>/dev/null || true

    # Зависимости
    apt-get install -y -qq ca-certificates curl gnupg lsb-release

    # GPG-ключ Docker
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    # Репозиторий
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    systemctl enable docker
    systemctl start docker
    ok "Docker установлен: $(docker --version)"
fi

# Проверяем docker compose
if docker compose version &>/dev/null; then
    ok "Docker Compose: $(docker compose version --short)"
else
    err "Docker Compose plugin не установлен"
fi

# ══════════════════════════════════════════════════════
# 3. УСТАНОВКА NGINX
# ══════════════════════════════════════════════════════
info "Шаг 3/7 — Установка Nginx..."

if command -v nginx &>/dev/null; then
    ok "Nginx уже установлен"
else
    apt-get install -y -qq nginx
    systemctl enable nginx
    ok "Nginx установлен"
fi

# ══════════════════════════════════════════════════════
# 4. САМОПОДПИСАННЫЙ SSL-СЕРТИФИКАТ
# ══════════════════════════════════════════════════════
info "Шаг 4/7 — Генерация SSL-сертификата..."

SSL_DIR="/etc/nginx/ssl"
mkdir -p "$SSL_DIR"

if [[ -f "$SSL_DIR/pixel.crt" && -f "$SSL_DIR/pixel.key" ]]; then
    warn "Сертификат уже существует, пропускаем генерацию"
else
    openssl req -x509 -nodes -days 3650 \
        -newkey rsa:2048 \
        -keyout "$SSL_DIR/pixel.key" \
        -out "$SSL_DIR/pixel.crt" \
        -subj "/C=US/ST=State/L=City/O=PixelBattle/CN=${SERVER_IP}"
    ok "SSL-сертификат создан (действителен 10 лет)"
fi

# ══════════════════════════════════════════════════════
# 5. НАСТРОЙКА NGINX
# ══════════════════════════════════════════════════════
info "Шаг 5/7 — Настройка Nginx..."

# Убираем default-сайт
rm -f /etc/nginx/sites-enabled/default

cat > /etc/nginx/sites-available/pixel-battle <<'NGINX_CONF'
# ── Pixel Battle — HTTPS reverse proxy ────────────────
# Проксирует запросы на FastAPI (порт 8000 в Docker).
# WebSocket проксируется на /ws/.

server {
    listen 80;
    server_name _;

    # Редирект HTTP → HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     /etc/nginx/ssl/pixel.crt;
    ssl_certificate_key /etc/nginx/ssl/pixel.key;

    # Современные SSL-параметры
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # ── Обычные HTTP-запросы → FastAPI ────────────────
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ── WebSocket /ws/ → FastAPI ─────────────────────
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400s;   # 24 часа (длительность игры)
        proxy_send_timeout 86400s;
    }
}
NGINX_CONF

ln -sf /etc/nginx/sites-available/pixel-battle /etc/nginx/sites-enabled/pixel-battle

# Проверяем конфиг
nginx -t || err "Ошибка в конфиге Nginx"
systemctl restart nginx
ok "Nginx настроен: HTTP→HTTPS, проксирование на :8000"

# ══════════════════════════════════════════════════════
# 6. НАСТРОЙКА ПРОЕКТА И ЗАПУСК
# ══════════════════════════════════════════════════════
info "Шаг 6/7 — Запуск Pixel Battle..."

cd "$PROJECT_DIR"

# Спрашиваем токен если .env ещё нет
if [[ ! -f .env ]]; then
    echo ""
    echo -e "${YELLOW}Введите токен Telegram-бота (от @BotFather):${NC}"
    read -r BOT_TOKEN_INPUT

    if [[ -z "$BOT_TOKEN_INPUT" ]]; then
        err "Токен не может быть пустым"
    fi

    cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN_INPUT}
WEBAPP_BASE_URL=https://${SERVER_IP}
EOF

    ok "Файл .env создан"
else
    warn ".env уже существует, используем его"
    # Обновляем WEBAPP_BASE_URL на текущий IP
    if grep -q "WEBAPP_BASE_URL" .env; then
        sed -i "s|WEBAPP_BASE_URL=.*|WEBAPP_BASE_URL=https://${SERVER_IP}|" .env
    else
        echo "WEBAPP_BASE_URL=https://${SERVER_IP}" >> .env
    fi
    ok "WEBAPP_BASE_URL обновлён на https://${SERVER_IP}"
fi

# Открываем порты в UFW (если включён)
if command -v ufw &>/dev/null && ufw status | grep -q "active"; then
    info "Настраиваем файрвол (UFW)..."
    ufw allow 22/tcp   >/dev/null 2>&1   # SSH — не потерять доступ!
    ufw allow 80/tcp   >/dev/null 2>&1
    ufw allow 443/tcp  >/dev/null 2>&1
    ok "Порты 22, 80, 443 открыты в UFW"
fi

# Останавливаем старые контейнеры если были
docker compose down 2>/dev/null || true

# Собираем и запускаем
docker compose up --build -d

echo ""
echo "═══════════════════════════════════════════════════"
echo -e "${GREEN}  ✅  PIXEL BATTLE ЗАПУЩЕН!${NC}"
echo "═══════════════════════════════════════════════════"
echo ""
echo "  Бот:      работает (aiogram)"
echo "  Backend:  http://127.0.0.1:8000 (внутри)"
echo "  Nginx:    https://${SERVER_IP} (снаружи)"
echo ""
echo "  ── Что делать дальше ──────────────────────────"
echo "  1. Откройте @BotFather → /mybots → Bot Settings"
echo "     → Group Privacy → Turn OFF"
echo "  2. Добавьте бота в групповой чат"
echo "  3. Отправьте /start_battle"
echo ""
echo "  ── Полезные команды ───────────────────────────"
echo "  docker compose logs -f          # все логи"
echo "  docker compose logs -f bot      # логи бота"
echo "  docker compose logs -f backend  # логи API"
echo "  docker compose restart          # перезапуск"
echo "  docker compose down             # остановка"
echo ""
echo "  ── Важно ──────────────────────────────────────"
echo "  Telegram может показать предупреждение о"
echo "  самоподписанном сертификате — это нормально"
echo "  для разработки. Для продакшена привяжите домен"
echo "  и используйте Let's Encrypt (certbot)."
echo ""
