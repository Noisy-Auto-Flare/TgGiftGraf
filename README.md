# Telegram Gift Graph

Приложение для сбора, анализа и визуализации графа подарков в Telegram.

## Возможности
- **Deep Crawling**: Сбор всей истории подарков пользователя с обходом лимитов API.
- **Локальные графы**: Визуализация связей конкретного человека на любую глубину.
- **Глобальный граф**: Фильтрация и сортировка всей базы (по связям, сумме подарков, дате добавления).
- **Авто-обнаружение**: Сбор целей из ваших контактов, диалогов и публичных чатов.
- **Умное хранение**: Ограничение размера папки с аватарами для экономии места на сервере.

---

## Подготовка (API Telegram)

Перед установкой вам необходимо получить ключи API:
1. Зайдите на [my.telegram.org](https://my.telegram.org/).
2. Перейдите в раздел **API development tools**.
3. Создайте приложение (App title и Short name любые).
4. Сохраните `api_id` и `api_hash`.

---

## Установка на сервер (VPS) с нуля

Рекомендуется использовать сервер с ОС **Ubuntu 22.04+** и минимум **1 ГБ ОЗУ**.

### Вариант А: Через Docker (Рекомендуется)

Самый быстрый способ, включающий авто-обновление SSL и изоляцию сервисов.

#### 1. Установка Docker
```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

#### 2. Клонирование и настройка
```bash
git clone https://github.com/youruser/TgGiftGraf.git
cd TgGiftGraf
cp .env.example .env
nano .env # Введите ваши API_ID, API_HASH и другие настройки
```

#### 3. Настройка домена
Откройте `Caddyfile` и замените `your-domain.com` на ваш реальный домен:
```bash
nano Caddyfile
```

#### 4. Первая авторизация
Поскольку контейнеры работают в фоне, нужно один раз войти в аккаунт интерактивно:
```bash
docker compose run --rm crawler python crawler.py
```
Введите номер телефона и код из Telegram.

#### 5. Запуск
```bash
docker compose up -d
```
Сайт будет доступен по вашему домену с автоматическим HTTPS (SSL).

---

### Вариант Б: Ручная установка (без Docker)

Если вы не хотите использовать Docker, можно настроить всё вручную через Nginx и systemd.

#### 1. Установка системных зависимостей
```bash
sudo apt update
sudo apt install python3-venv python3-pip nginx -y
```

#### 2. Настройка проекта
```bash
git clone https://github.com/youruser/TgGiftGraf.git
cd TgGiftGraf
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env # Настройте API_ID и API_HASH
```

#### 3. Авторизация
```bash
python3 crawler.py # Введите код
```

#### 4. Настройка автозапуска (Systemd)
Создайте сервис для API:
```bash
sudo nano /etc/systemd/system/gift-api.service
```
Вставьте (заменив пути на свои):
```ini
[Unit]
Description=Gift Graph API
After=network.target

[Service]
User=root
WorkingDirectory=/root/TgGiftGraf
ExecStart=/root/TgGiftGraf/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```
Повторите аналогично для `crawler.py` и `analytics.py` (для аналитики можно использовать cron).

#### 5. Настройка Nginx и домена
```bash
sudo nano /etc/nginx/sites-available/giftgraph
```
```nginx
server {
    server_name your-domain.com;

    location / {
        root /root/TgGiftGraf/static;
        index index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```
Активируйте конфиг и получите SSL через Certbot:
```bash
sudo ln -s /etc/nginx/sites-available/giftgraph /etc/nginx/sites-enabled/
sudo certbot --nginx -d your-domain.com
```

---

## Обновление приложения
```bash
git pull
# Если через Docker:
docker compose up -d --build
# Если вручную:
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart gift-api gift-crawler
```

## Полезные команды
- **Просмотр логов (Docker)**: `docker compose logs -f crawler`
- **Размер базы**: `du -h gifts.db`
- **Очистка аватарок**: Папка `static/avatars` очищается автоматически при достижении лимита (настраивается в `.env`).
