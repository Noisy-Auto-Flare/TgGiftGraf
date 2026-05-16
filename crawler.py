import asyncio
import random
import logging
import os
from logging.handlers import RotatingFileHandler
import time
from telethon import TelegramClient, functions, types, errors
from database import get_db_connection, init_db
from config import (
    API_ID, API_HASH, SESSION_NAME, START_USERNAMES, TARGET_CHATS,
    CRAWL_DELAY_MIN, CRAWL_DELAY_MAX, MAX_CRAWL_QUEUE_SIZE,
    CHAT_SCAN_INTERVAL, RESCAN_THRESHOLD_DAYS, CRAWL_SINGLE_RUN,
    SCAN_SELF_DIALOGS, MAX_AVATARS_SIZE_MB, AVATARS_DIR
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

# Снижаем шум от Telethon
logging.getLogger('telethon').setLevel(logging.WARNING)

async def human_delay(min_sec=1.0, max_sec=3.0):
    """Небольшая пауза между API запросами для имитации человека."""
    await asyncio.sleep(random.uniform(float(min_sec), float(max_sec)))

def get_dir_size(path='.'):
    """Возвращает размер папки в мегабайтах."""
    total_size = 0
    try:
        if not os.path.exists(path):
            return 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                # пропускаем, если это символическая ссылка
                if not os.path.islink(fp):
                    total_size += os.path.getsize(fp)
    except Exception as e:
        logger.error(f"Ошибка при подсчете размера папки {path}: {e}")
    return total_size / (1024 * 1024)

async def download_profile_photo(client, user_id, entity):
    """Скачивает фото профиля пользователя максимально быстро (только если доступно напрямую)."""
    try:
        if not os.path.exists(AVATARS_DIR):
            os.makedirs(AVATARS_DIR, exist_ok=True)
            
        path = f"{AVATARS_DIR}/{user_id}.jpg"
        
        # Если файл уже есть и он не пустой, не тратим ресурсы
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True

        # Проверка лимита места
        current_size = get_dir_size(AVATARS_DIR)
        if current_size >= MAX_AVATARS_SIZE_MB:
            logger.debug(f"Лимит аватарок превышен ({current_size:.1f}MB >= {MAX_AVATARS_SIZE_MB}MB). Пропуск.")
            return False

        # Пробуем скачать только если фото доступно в базовом объекте
        if hasattr(entity, 'photo') and entity.photo:
            logger.debug(f"Скачивание фото для {user_id}...")
            await client.download_profile_photo(entity, file=path, download_big=False)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return True
    except Exception as e:
        logger.debug(f"Не удалось скачать фото для {user_id}: {e}")
    return False

async def get_user_info(client, entity_id):
    """Получает базовую информацию о пользователе БЕЗ тяжелых запросов."""
    try:
        await human_delay(0.2, 0.5) # Минимальная пауза
        entity = await client.get_entity(entity_id)
        
        has_photo = await download_profile_photo(client, entity.id, entity)
        
        if isinstance(entity, types.User):
            return {
                'id': entity.id,
                'username': entity.username,
                'first_name': (entity.first_name or "") + (" " + entity.last_name if entity.last_name else ""),
                'is_bot': entity.bot,
                'has_photo': 1 if has_photo else 0
            }
        return {
            'id': entity.id,
            'username': getattr(entity, 'username', None),
            'first_name': getattr(entity, 'title', str(entity.id)),
            'is_bot': False,
            'has_photo': 1 if has_photo else 0
        }
    except Exception as e:
        logger.error(f"Ошибка при получении инфо {entity_id}: {e}")
        return None

async def add_to_queue(conn, user_id, priority=0, source='unknown'):
    """Добавляет пользователя в очередь с проверкой лимита."""
    try:
        cursor = conn.cursor()
        
        # Завершаем текущую транзакцию, если она есть, чтобы не блокировать чтение
        conn.commit()
        
        # 1. Проверка лимита очереди
        cursor.execute("SELECT COUNT(*) as count FROM crawl_queue")
        current_count = cursor.fetchone()['count']
        if current_count >= MAX_CRAWL_QUEUE_SIZE:
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
    except Exception as e:
        logger.error(f"Ошибка при добавлении в очередь {user_id}: {e}")

async def scan_chats(client, conn):
    """Сканирует целевые чаты с паузами. Если нет прав админа, собирает из последних сообщений."""
    logger.info("Запуск сканирования целевых чатов...")
    for chat_username in TARGET_CHATS:
        try:
            logger.info(f"Сбор участников из чата @{chat_username}")
            count = 0
            try:
                # Пробуем получить полный список участников (работает в группах или если мы админ канала)
                async for user in client.iter_participants(chat_username, limit=500):
                    if isinstance(user, types.User) and not user.bot:
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
                    if count % 10 == 0: await human_delay(0.5, 1.0)
            except (errors.ChatAdminRequiredError, errors.ChannelPrivateError):
                logger.info(f"Нет прав для получения списка участников @{chat_username}. Собираем из сообщений...")
                # Собираем авторов последних сообщений
                async for message in client.iter_messages(chat_username, limit=100):
                    user = message.sender
                    if isinstance(user, types.User) and not user.bot:
                        cursor = conn.cursor()
                        cursor.execute('''
                            INSERT INTO users (id, username, first_name, discovery_source, is_bot) 
                            VALUES (?, ?, ?, 'chat_msg', ?)
                            ON CONFLICT(id) DO UPDATE SET 
                                username = COALESCE(excluded.username, users.username),
                                first_name = COALESCE(excluded.first_name, users.first_name),
                                is_bot = excluded.is_bot
                        ''', (user.id, user.username, (user.first_name or "") + (" " + user.last_name if user.last_name else ""), 0))
                        await add_to_queue(conn, user.id, priority=0, source='chat')
                        count += 1
                    if count % 5 == 0: await human_delay(0.5, 1.0)
                    
            await human_delay(3, 7) # Пауза между чатами
        except Exception as e:
            logger.error(f"Ошибка при сканировании чата {chat_username}: {e}")
    conn.commit()

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
        # Получаем полную сущность для надежности
        await human_delay(0.5, 1)
        try:
            full_entity = await client.get_entity(target_user_id)
            input_entity = await client.get_input_entity(full_entity)
            
            # Пытаемся скачать фото
            has_photo_now = await download_profile_photo(client, target_user_id, full_entity)
            cursor.execute("UPDATE users SET has_photo = ? WHERE id = ?", (1 if has_photo_now else 0, target_user_id))
        except (errors.UserIdInvalidError, ValueError) as e:
            logger.warning(f"Не удалось получить сущность для {target_user_id}: {e}. Удаляем из очереди.")
            cursor.execute("DELETE FROM crawl_queue WHERE user_id = ?", (target_user_id,))
            conn.commit()
            return
        
        # Нативный запрос подарков
        logger.info(f"Запрос подарков для {target_user_id}...")

        # Динамически ищем методы
        GetUserGiftsRequest = getattr(functions.payments, 'GetUserGiftsRequest', None)
        GetSavedStarGiftsRequest = getattr(functions.payments, 'GetSavedStarGiftsRequest', None)
        
        all_gifts = []
        all_users = []
        
        # 1. Пробуем GetSavedStarGiftsRequest (Star Gifts)
        if GetSavedStarGiftsRequest:
            offset = ''
            limit = 50
            while True:
                try:
                    await human_delay(0.5, 1.0)
                    res = await client(GetSavedStarGiftsRequest(peer=input_entity, offset=offset, limit=limit))
                    if not res or not res.gifts:
                        break
                    
                    all_gifts.extend(res.gifts)
                    if hasattr(res, 'users'):
                        all_users.extend(res.users)
                    
                    logger.info(f"Получено {len(res.gifts)} Star Gifts (всего {len(all_gifts)}) для {target_user_id}")
                    
                    # Если получили меньше, чем просили, и нет следующего офсета - значит всё
                    next_offset = getattr(res, 'next_offset', '')
                    if not next_offset or (len(res.gifts) < limit and not next_offset):
                        break
                    
                    offset = next_offset
                    # Увеличиваем лимит для следующего запроса до максимума (100)
                    limit = 100 
                except Exception as e:
                    logger.debug(f"GetSavedStarGiftsRequest loop error: {e}")
                    break

        # 2. Пробуем GetUserGiftsRequest (Старые подарки)
        if GetUserGiftsRequest:
            offset = ''
            limit = 50
            while True:
                try:
                    await human_delay(0.5, 1.0)
                    res = await client(GetUserGiftsRequest(user_id=input_entity, offset=offset, limit=limit))
                    if not res or not res.gifts:
                        break
                    
                    all_gifts.extend(res.gifts)
                    if hasattr(res, 'users'):
                        all_users.extend(res.users)
                    
                    logger.info(f"Получено {len(res.gifts)} старых подарков (всего {len(all_gifts)}) для {target_user_id}")
                    
                    next_offset = getattr(res, 'next_offset', '')
                    if not next_offset or (len(res.gifts) < limit and not next_offset):
                        break
                        
                    offset = next_offset
                    limit = 100
                except Exception as e:
                    logger.debug(f"GetUserGiftsRequest loop error: {e}")
                    break

        if not all_gifts:
            logger.info(f"Подарки для {target_user_id} не найдены ни одним из методов.")
            cursor.execute("UPDATE users SET last_scanned = ? WHERE id = ?", (int(time.time()), target_user_id))
            cursor.execute("DELETE FROM crawl_queue WHERE user_id = ?", (target_user_id,))
            conn.commit()
            return

        if len(all_gifts) > 0:
            # Сохраняем пользователей, пришедших в ответе (это отправители подарков)
            user_map = {}
            for u in all_users:
                if isinstance(u, types.User):
                    user_map[u.id] = u
                    # Сохраняем/обновляем инфо о пользователе (НЕ сбрасываем has_photo)
                    cursor.execute('''
                        INSERT INTO users (id, username, first_name, discovery_source, is_bot) 
                        VALUES (?, ?, ?, 'gift_list', 0)
                        ON CONFLICT(id) DO UPDATE SET 
                            username = COALESCE(excluded.username, users.username),
                            first_name = COALESCE(excluded.first_name, users.first_name)
                    ''', (u.id, u.username, (u.first_name or "") + (" " + u.last_name if u.last_name else "")))

            for gift_attr in all_gifts:
                from_id = getattr(gift_attr, 'from_id', None)
                name_hidden = getattr(gift_attr, 'name_hidden', False)
                
                # Конвертируем дату
                gift_date_obj = getattr(gift_attr, 'date', 0)
                if isinstance(gift_date_obj, int):
                    gift_date = gift_date_obj
                elif hasattr(gift_date_obj, 'timestamp'):
                    gift_date = int(gift_date_obj.timestamp())
                else:
                    gift_date = int(time.time())
                
                gift_title = "Подарок"
                if hasattr(gift_attr, 'gift'):
                    g_obj = gift_attr.gift
                    if hasattr(g_obj, 'id'):
                        gift_title = f"Gift #{g_obj.id}"
                    if hasattr(g_obj, 'sticker') and hasattr(g_obj.sticker, 'alt'):
                        gift_title = f"Gift {g_obj.sticker.alt}"

                # Если отправитель скрыт, но мы можем его найти в message (иногда там есть упоминание)
                sender_id = None
                if isinstance(from_id, types.PeerUser):
                    sender_id = from_id.user_id
                elif isinstance(from_id, int):
                    sender_id = from_id
                
                if name_hidden:
                    logger.debug(f"У подарка {gift_title} для {target_user_id} отправитель скрыт.")

                if sender_id:
                    # Проверяем, есть ли отправитель в нашем мапе из ответа
                    u_info = None
                    if sender_id in user_map:
                        u = user_map[sender_id]
                        u_info = {
                            'id': u.id,
                            'username': u.username,
                            'first_name': (u.first_name or "") + (" " + u.last_name if u.last_name else ""),
                            'is_bot': u.bot
                        }
                    else:
                        # Если нет в мапе, запрашиваем отдельно
                        u_info = await get_user_info(client, sender_id)

                    if u_info and not u_info['is_bot']:
                        # Пытаемся скачать фото для отправителя, если его еще нет на диске
                        photo_path = f"{AVATARS_DIR}/{u_info['id']}.jpg"
                        if not (os.path.exists(photo_path) and os.path.getsize(photo_path) > 0):
                            try:
                                sender_entity = await client.get_entity(u_info['id'])
                                if await download_profile_photo(client, u_info['id'], sender_entity):
                                    u_info['has_photo'] = 1
                            except Exception as e:
                                logger.debug(f"Не удалось скачать фото для отправителя {u_info['id']}: {e}")

                        # Сохраняем пользователя (на случай если его нет в БД)
                        cursor.execute('''
                            INSERT INTO users (id, username, first_name, discovery_source, is_bot, has_photo) 
                            VALUES (?, ?, ?, 'gift', 0, ?)
                            ON CONFLICT(id) DO UPDATE SET 
                                username = COALESCE(excluded.username, users.username),
                                first_name = COALESCE(excluded.first_name, users.first_name),
                                has_photo = CASE WHEN users.has_photo = 1 THEN 1 ELSE excluded.has_photo END
                        ''', (u_info['id'], u_info['username'], u_info['first_name'], u_info.get('has_photo', 0)))

                        # Сохраняем связь
                        cursor.execute('''
                            INSERT INTO edges (from_user_id, to_user_id, weight, last_gift_title, last_gift_date)
                            VALUES (?, ?, 1, ?, ?)
                            ON CONFLICT(from_user_id, to_user_id) DO UPDATE SET
                                weight = weight + 1,
                                last_gift_title = excluded.last_gift_title,
                                last_gift_date = excluded.last_gift_date
                        ''', (u_info['id'], target_user_id, gift_title, gift_date))
                        
                        # Добавляем отправителя в очередь на сканирование
                        await add_to_queue(conn, u_info['id'], priority=0, source='gift')
        else:
            logger.info(f"У пользователя {target_user_id} 0 подарков в ответе.")

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
            conn.commit()
            logger.info(f"Добавлено {len(contacts.users)} контактов в очередь.")
    except Exception as e:
        logger.error(f"Ошибка при получении контактов: {e}")

async def scan_dialogs(client, conn):
    """Автоматически собирает пользователей из всех активных диалогов аккаунта."""
    logger.info("Запуск автоматического сканирования ваших диалогов...")
    try:
        count = 0
        async for dialog in client.iter_dialogs(limit=100):
            entity = dialog.entity
            if isinstance(entity, (types.Chat, types.Channel)):
                # Собираем участников из групп или последние сообщения из каналов
                try:
                    if isinstance(entity, types.Chat) or (isinstance(entity, types.Channel) and entity.megagroup):
                        async for user in client.iter_participants(entity, limit=50):
                            if isinstance(user, types.User) and not user.bot:
                                cursor = conn.cursor()
                                cursor.execute('''
                                    INSERT INTO users (id, username, first_name, discovery_source, is_bot) 
                                    VALUES (?, ?, ?, 'dialog', ?)
                                    ON CONFLICT(id) DO UPDATE SET 
                                        username = COALESCE(excluded.username, users.username),
                                        first_name = COALESCE(excluded.first_name, users.first_name),
                                        is_bot = excluded.is_bot
                                ''', (user.id, user.username, (user.first_name or "") + (" " + user.last_name if user.last_name else ""), 0))
                                await add_to_queue(conn, user.id, priority=0, source='dialog')
                                count += 1
                            if count % 10 == 0: await human_delay(0.2, 0.5)
                    else:
                        # Обычный канал - берем авторов последних сообщений
                        async for message in client.iter_messages(entity, limit=20):
                            user = message.sender
                            if isinstance(user, types.User) and not user.bot:
                                cursor = conn.cursor()
                                cursor.execute('''
                                    INSERT INTO users (id, username, first_name, discovery_source, is_bot) 
                                    VALUES (?, ?, ?, 'dialog_msg', ?)
                                    ON CONFLICT(id) DO UPDATE SET 
                                        username = COALESCE(excluded.username, users.username),
                                        first_name = COALESCE(excluded.first_name, users.first_name),
                                        is_bot = excluded.is_bot
                                ''', (user.id, user.username, (user.first_name or "") + (" " + user.last_name if user.last_name else ""), 0))
                                await add_to_queue(conn, user.id, priority=0, source='dialog')
                                count += 1
                            if count % 5 == 0: await human_delay(0.2, 0.5)
                except Exception:
                    continue
            elif isinstance(entity, types.User) and not entity.bot:
                # Прямой диалог с пользователем
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO users (id, username, first_name, discovery_source, is_bot) 
                    VALUES (?, ?, ?, 'dialog_direct', ?)
                    ON CONFLICT(id) DO UPDATE SET 
                        username = COALESCE(excluded.username, users.username),
                        first_name = COALESCE(excluded.first_name, users.first_name),
                        is_bot = excluded.is_bot
                ''', (entity.id, entity.username, (entity.first_name or "") + (" " + entity.last_name if entity.last_name else ""), 0))
                await add_to_queue(conn, entity.id, priority=0, source='dialog')
                count += 1
            
            if count > 500: break # Лимит за один проход
        conn.commit()
        logger.info(f"Авто-сканирование диалогов завершено. Найдено {count} потенциальных целей.")
    except Exception as e:
        logger.error(f"Ошибка при сканировании диалогов: {e}")

async def crawl():
    if not API_ID or not API_HASH:
        logger.error("API_ID или API_HASH не заданы в .env")
        return

    try:
        client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    except ValueError as e:
        if "unpack" in str(e):
            logger.error(f"Критическая ошибка сессии: {e}")
            logger.error(f"Скорее всего, файл {SESSION_NAME}.session поврежден или несовместим (это часто случается при обновлении Python или Telethon).")
            logger.error(f"РЕШЕНИЕ: Удалите файл {SESSION_NAME}.session в папке проекта и запустите краулер снова.")
            return
        raise e

    async with client:
        # Инициализация БД и миграции
        init_db()
        conn = get_db_connection()
        
        # 0. Сброс зависших сканирований (для исправления прошлых ошибок)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET last_scanned = 0 WHERE id NOT IN (SELECT from_user_id FROM edges) AND id NOT IN (SELECT to_user_id FROM edges)")
        conn.commit()
        logger.info("Сброшен статус сканирования для пользователей без найденных подарков.")

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
        last_dialog_scan = 0

        while True:
            now = int(time.time())
            
            # Проверяем очередь на резолв (юзернеймы из веб-интерфейса)
            cursor.execute('SELECT identifier, priority FROM resolve_queue ORDER BY priority DESC, added_at ASC LIMIT 1')
            resolve_row = cursor.fetchone()
            if resolve_row:
                ident = resolve_row['identifier']
                prio = resolve_row['priority']
                logger.info(f"Резолвим юзернейм из очереди: {ident}")
                u_info = await get_user_info(client, ident)
                if u_info:
                    cursor.execute('''
                        INSERT INTO users (id, username, first_name, discovery_source, is_bot) 
                        VALUES (?, ?, ?, 'web_add', ?)
                        ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, is_bot=excluded.is_bot
                    ''', (u_info['id'], u_info['username'], u_info['first_name'], 1 if u_info['is_bot'] else 0))
                    if not u_info['is_bot']:
                        await add_to_queue(conn, u_info['id'], priority=prio, source='web_add')
                
                cursor.execute('DELETE FROM resolve_queue WHERE identifier = ?', (ident,))
                conn.commit()
                continue # Сразу переходим к обработке этого пользователя (он теперь в crawl_queue)

            # Периодическое сканирование чатов (только в режиме цикла)
            if not CRAWL_SINGLE_RUN:
                if now - last_chat_scan > (CHAT_SCAN_INTERVAL * 60):
                    await scan_chats(client, conn)
                    last_chat_scan = now
                
                if SCAN_SELF_DIALOGS and (now - last_dialog_scan > (CHAT_SCAN_INTERVAL * 60)):
                    await scan_dialogs(client, conn)
                    last_dialog_scan = now

            # Выборка пользователя
            cursor = conn.cursor()
            cursor.execute('''
                SELECT user_id FROM crawl_queue 
                ORDER BY priority DESC, added_at ASC 
                LIMIT 1
            ''')
            row = cursor.fetchone()
            
            if not row:
                if SCAN_SELF_DIALOGS and not CRAWL_SINGLE_RUN:
                    logger.info("Очередь пуста. Пробуем экстренное сканирование диалогов...")
                    await scan_dialogs(client, conn)
                    last_dialog_scan = int(time.time())
                    # Проверяем еще раз после сканирования
                    cursor.execute('SELECT user_id FROM crawl_queue ORDER BY priority DESC, added_at ASC LIMIT 1')
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
