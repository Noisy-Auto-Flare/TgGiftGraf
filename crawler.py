import asyncio
import random
import logging
from logging.handlers import RotatingFileHandler
import time
from telethon import TelegramClient, functions, types, errors
from database import get_db_connection
from config import (
    API_ID, API_HASH, SESSION_NAME, START_USERNAMES, TARGET_CHATS,
    CRAWL_DELAY_MIN, CRAWL_DELAY_MAX, MAX_CRAWL_QUEUE_SIZE,
    CHAT_SCAN_INTERVAL, RESCAN_THRESHOLD_DAYS, CRAWL_SINGLE_RUN
)

# Настройка логирования с ротацией
log_handler = RotatingFileHandler("crawler.log", maxBytes=5*1024*1024, backupCount=3)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        log_handler,
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

async def human_delay(min_sec=1.0, max_sec=3.0):
    """Небольшая пауза между API запросами для имитации человека."""
    await asyncio.sleep(random.uniform(float(min_sec), float(max_sec)))

async def get_user_info(client, entity_id):
    """Получает информацию о пользователе с обработкой ошибок и паузой."""
    try:
        # Пауза перед запросом
        await human_delay(0.5, 1.5)
        entity = await client.get_entity(entity_id)
        if isinstance(entity, types.User):
            return {
                'id': entity.id,
                'username': entity.username,
                'first_name': (entity.first_name or "") + (" " + entity.last_name if entity.last_name else ""),
                'is_bot': entity.bot
            }
        return {
            'id': entity.id,
            'username': getattr(entity, 'username', None),
            'first_name': getattr(entity, 'title', str(entity.id)),
            'is_bot': False
        }
    except (errors.UserDeactivatedError, errors.UsernameInvalidError, errors.UserIdInvalidError):
        logger.warning(f"Пользователь {entity_id} деактивирован или невалиден.")
        return None
    except Exception as e:
        if "PrivacyError" in str(e) or "PrivateUserError" in str(e):
            logger.warning(f"Профиль {entity_id} скрыт настройками приватности.")
            return None
        logger.debug(f"Не удалось получить инфо для {entity_id}: {e}")
        return None

async def add_to_queue(conn, user_id, priority=0, source='unknown'):
    """Добавляет пользователя в очередь с проверкой лимита."""
    cursor = conn.cursor()
    
    # 1. Проверка лимита очереди
    cursor.execute("SELECT COUNT(*) as count FROM crawl_queue")
    current_count = cursor.fetchone()['count']
    if current_count >= MAX_CRAWL_QUEUE_SIZE:
        # Если очередь полна, можем попробовать вытеснить записи с низким приоритетом
        # Но для простоты и стабильности — просто не добавляем.
        return

    # 2. Проверка, есть ли уже в очереди
    cursor.execute("SELECT 1 FROM crawl_queue WHERE user_id = ?", (user_id,))
    if cursor.fetchone():
        return

    # 3. Проверка когда сканировали последний раз
    cursor.execute("SELECT last_scanned, is_bot FROM users WHERE id = ?", (user_id,))
    user_row = cursor.fetchone()
    
    # Не добавляем ботов
    if user_row and user_row['is_bot']:
        return

    now = int(time.time())
    if not user_row or (now - user_row['last_scanned']) > (RESCAN_THRESHOLD_DAYS * 86400):
        cursor.execute('''
            INSERT OR IGNORE INTO crawl_queue (user_id, priority) 
            VALUES (?, ?)
        ''', (user_id, priority))
        
        if not user_row:
            cursor.execute('''
                INSERT OR IGNORE INTO users (id, discovery_source) 
                VALUES (?, ?)
            ''', (user_id, source))
        conn.commit()

async def scan_chats(client, conn):
    """Сканирует целевые чаты с паузами."""
    logger.info("Запуск сканирования целевых чатов...")
    for chat_username in TARGET_CHATS:
        try:
            logger.info(f"Сбор участников из чата @{chat_username}")
            count = 0
            async for user in client.iter_participants(chat_username, limit=500):
                if not user.bot:
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO users (id, username, first_name, discovery_source, is_bot) 
                        VALUES (?, ?, ?, 'chat', ?)
                        ON CONFLICT(id) DO UPDATE SET 
                            username = COALESCE(excluded.username, users.username),
                            first_name = COALESCE(excluded.first_name, users.first_name),
                            is_bot = excluded.is_bot
                    ''', (user.id, user.username, (user.first_name or "") + (" " + user.last_name if user.last_name else ""), 0))
                    
                    await add_to_queue(conn, user.id, priority=0, source='chat')
                    count += 1
                
                # Пауза каждые 5 пользователей для имитации человеческого чтения списка
                if count % 5 == 0:
                    await human_delay(0.5, 1.0)
                    
            await human_delay(3, 7) # Пауза между чатами
        except Exception as e:
            logger.error(f"Ошибка при сканировании чата {chat_username}: {e}")

async def process_user(client, conn, target_user_id):
    """Обрабатывает одного пользователя из очереди."""
    cursor = conn.cursor()
    
    # Проверка на бота перед запросом (на всякий случай)
    cursor.execute("SELECT is_bot FROM users WHERE id = ?", (target_user_id,))
    row = cursor.fetchone()
    if row and row['is_bot']:
        cursor.execute("DELETE FROM crawl_queue WHERE user_id = ?", (target_user_id,))
        conn.commit()
        return

    try:
        # Получаем input_entity с обработкой ошибок
        await human_delay(1, 2)
        try:
            input_entity = await client.get_input_entity(target_user_id)
        except (errors.UserIdInvalidError, ValueError):
            logger.warning(f"Не удалось получить input_entity для {target_user_id}. Удаляем из очереди.")
            cursor.execute("DELETE FROM crawl_queue WHERE user_id = ?", (target_user_id,))
            conn.commit()
            return

        # Нативный запрос подарков
        logger.info(f"Запрос подарков для {target_user_id}...")
        await human_delay(1, 2)
        gifts_res = await client(functions.payments.GetUserGiftsRequest(
            user_id=input_entity,
            offset='',
            limit=100
        ))
        
        if hasattr(gifts_res, 'gifts'):
            for gift_attr in gifts_res.gifts:
                from_id = getattr(gift_attr, 'from_id', None)
                gift_date = getattr(gift_attr, 'date', 0)
                
                gift_title = "Подарок"
                if hasattr(gift_attr, 'gift'):
                    gift_title = f"Gift #{gift_attr.gift.id}"

                if from_id:
                    # Обработка отправителя
                    u_info = await get_user_info(client, from_id)
                    if u_info and not u_info['is_bot']:
                        cursor.execute('''
                            INSERT INTO users (id, username, first_name, discovery_source, is_bot) 
                            VALUES (?, ?, ?, 'gift', ?) 
                            ON CONFLICT(id) DO UPDATE SET 
                                username = COALESCE(excluded.username, users.username), 
                                first_name = COALESCE(excluded.first_name, users.first_name),
                                is_bot = excluded.is_bot
                        ''', (u_info['id'], u_info['username'], u_info['first_name'], 0))
                        
                        cursor.execute('''
                            INSERT INTO edges (from_user_id, to_user_id, weight, last_gift_title, last_gift_date)
                            VALUES (?, ?, 1, ?, ?)
                            ON CONFLICT(from_user_id, to_user_id) DO UPDATE SET
                                weight = weight + 1,
                                last_gift_title = excluded.last_gift_title,
                                last_gift_date = excluded.last_gift_date
                        ''', (u_info['id'], target_user_id, gift_title, gift_date))
                        
                        await add_to_queue(conn, u_info['id'], priority=0, source='gift')

        # Помечаем как просканированный
        cursor.execute("UPDATE users SET last_scanned = ? WHERE id = ?", (int(time.time()), target_user_id))
        cursor.execute("DELETE FROM crawl_queue WHERE user_id = ?", (target_user_id,))
        conn.commit()
        
    except errors.FloodWaitError as e:
        logger.warning(f"Flood wait на {e.seconds} секунд")
        await asyncio.sleep(e.seconds)
    except (errors.UserDeactivatedError, errors.InputUserDeactivatedError):
        logger.warning(f"Пользователь {target_user_id} удален. Чистим БД.")
        cursor.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
        cursor.execute("DELETE FROM crawl_queue WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM edges WHERE from_user_id = ? OR to_user_id = ?", (target_user_id, target_user_id))
        conn.commit()
    except Exception as e:
        err_str = str(e)
        if "PrivacyError" in err_str or "PrivateUserError" in err_str:
            logger.info(f"Профиль {target_user_id} приватный. Пропускаем.")
            cursor.execute("UPDATE users SET last_scanned = ? WHERE id = ?", (int(time.time()), target_user_id))
            cursor.execute("DELETE FROM crawl_queue WHERE user_id = ?", (target_user_id,))
            conn.commit()
        elif "METHOD_NOT_AVAILABLE" in err_str or "RPCError 400" in err_str:
            logger.warning(f"Метод GetUserGifts недоступен для {target_user_id}: {e}")
            cursor.execute("UPDATE users SET last_scanned = ? WHERE id = ?", (int(time.time()), target_user_id))
            cursor.execute("DELETE FROM crawl_queue WHERE user_id = ?", (target_user_id,))
            conn.commit()
        else:
            logger.error(f"Ошибка при обработке {target_user_id}: {e}")
            # В случае неизвестной ошибки удаляем из очереди, чтобы не зацикливаться
            cursor.execute("DELETE FROM crawl_queue WHERE user_id = ?", (target_user_id,))
            conn.commit()

async def init_contacts(client, conn):
    """Инициализирует очередь из контактов пользователя."""
    logger.info("Запуск инициализации из контактов...")
    try:
        contacts = await client(functions.contacts.GetContactsRequest(hash=0))
        if isinstance(contacts, types.contacts.Contacts):
            for user in contacts.users:
                # Проверяем, что это действительно пользователь, а не пустая запись
                if isinstance(user, types.User) and not user.bot:
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO users (id, username, first_name, discovery_source, is_bot) 
                        VALUES (?, ?, ?, 'contact', ?)
                        ON CONFLICT(id) DO UPDATE SET 
                            username = COALESCE(excluded.username, users.username),
                            first_name = COALESCE(excluded.first_name, users.first_name),
                            is_bot = excluded.is_bot
                    ''', (user.id, user.username, (user.first_name or "") + (" " + user.last_name if user.last_name else ""), 0))
                    await add_to_queue(conn, user.id, priority=1, source='contact')
            logger.info(f"Добавлено {len(contacts.users)} контактов в очередь.")
    except Exception as e:
        logger.error(f"Ошибка при получении контактов: {e}")

async def crawl():
    if not API_ID or not API_HASH:
        logger.error("API_ID или API_HASH не заданы в .env")
        return

    async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
        conn = get_db_connection()
        
        # 1. Инициализация
        # Если START_USERNAMES пуст, пробуем контакты
        if not START_USERNAMES:
            # Проверяем, пуста ли база пользователей
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as count FROM users")
            if cursor.fetchone()['count'] == 0:
                await init_contacts(client, conn)
        else:
            for username in START_USERNAMES:
                u_info = await get_user_info(client, username)
                if u_info:
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO users (id, username, first_name, discovery_source, is_bot) 
                        VALUES (?, ?, ?, 'start', ?)
                        ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, is_bot=excluded.is_bot
                    ''', (u_info['id'], u_info['username'], u_info['first_name'], 1 if u_info['is_bot'] else 0))
                    if not u_info['is_bot']:
                        await add_to_queue(conn, u_info['id'], priority=1, source='start')

        last_chat_scan = 0

        while True:
            now = int(time.time())
            
            # Периодическое сканирование чатов (только в режиме цикла)
            if not CRAWL_SINGLE_RUN and (now - last_chat_scan > (CHAT_SCAN_INTERVAL * 60)):
                await scan_chats(client, conn)
                last_chat_scan = now

            # Выборка пользователя
            cursor = conn.cursor()
            cursor.execute('''
                SELECT user_id FROM crawl_queue 
                ORDER BY priority DESC, added_at ASC 
                LIMIT 1
            ''')
            row = cursor.fetchone()
            
            if not row:
                if CRAWL_SINGLE_RUN:
                    logger.info("Очередь пуста. Завершение работы (SINGLE_RUN).")
                    break
                logger.info("Очередь пуста. Ждем 60 секунд...")
                await asyncio.sleep(60)
                continue
                
            await process_user(client, conn, row['user_id'])

            if CRAWL_SINGLE_RUN:
                logger.info("Пользователь обработан. Завершение работы (SINGLE_RUN).")
                break

            # Глобальная пауза между пользователями
            await human_delay(CRAWL_DELAY_MIN, CRAWL_DELAY_MAX)

if __name__ == "__main__":
    try:
        asyncio.run(crawl())
    except KeyboardInterrupt:
        logger.info("Краулер остановлен пользователем.")
