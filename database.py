import sqlite3
import os
from config import DB_PATH

def get_db_connection():
    """Создает подключение к базе данных с поддержкой конкурентного доступа."""
    conn = sqlite3.connect('gifts.db', timeout=30.0) # Увеличиваем таймаут ожидания
    conn.row_factory = sqlite3.Row
    
    # Включаем режим WAL (Write-Ahead Logging)
    # Это позволяет одновременно читать и писать в базу без блокировок
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as e:
        print(f"Ошибка при включении WAL мода: {e}")
        
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Таблица пользователей
    # last_scanned: время последнего успешного сканирования подарков
    # discovery_source: откуда узнали о пользователе (start, chat, gift)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE,
        first_name TEXT,
        last_scanned INTEGER DEFAULT 0,
        discovery_source TEXT DEFAULT 'unknown',
        is_bot INTEGER DEFAULT 0,
        has_photo INTEGER DEFAULT 0
    )
    ''')

    # Таблица ребер (подарков)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS edges (
        from_user_id INTEGER NOT NULL,
        to_user_id INTEGER NOT NULL,
        weight INTEGER DEFAULT 1,
        last_gift_title TEXT,
        last_gift_date INTEGER,
        PRIMARY KEY (from_user_id, to_user_id)
    )
    ''')

    # Очередь краулера
    # priority: 0 - обычный, 1 - высокий (например, из START_USERNAMES или новых чатов)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS crawl_queue (
        user_id INTEGER PRIMARY KEY,
        added_at INTEGER DEFAULT (strftime('%s','now')),
        priority INTEGER DEFAULT 0
    )
    ''')

    # Индексы для ускорения работы очереди и поиска связей
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_priority ON crawl_queue(priority DESC, added_at ASC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_last_scanned ON users(last_scanned)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_to_user ON edges(to_user_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_from_user ON edges(from_user_id)')

    # Таблицы для аналитики
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_reach (
        user_id INTEGER PRIMARY KEY,
        reach_count INTEGER DEFAULT 0,
        updated_at INTEGER DEFAULT (strftime('%s','now'))
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_clusters (
        user_id INTEGER PRIMARY KEY,
        cluster_id INTEGER
    )
    ''')

    # Полнотекстовый поиск FTS5
    cursor.execute('''
    CREATE VIRTUAL TABLE IF NOT EXISTS users_fts USING fts5(
        username,
        first_name,
        content='users',
        content_rowid='id'
    )
    ''')

    # Проверка и добавление колонки has_photo если её нет (миграция)
    try:
        cursor.execute("SELECT has_photo FROM users LIMIT 1")
    except sqlite3.OperationalError:
        print("Добавление колонки has_photo в таблицу users...")
        cursor.execute("ALTER TABLE users ADD COLUMN has_photo INTEGER DEFAULT 0")

    # Триггеры для обновления FTS
    cursor.execute('''
    CREATE TRIGGER IF NOT EXISTS users_ai AFTER INSERT ON users BEGIN
      INSERT INTO users_fts(rowid, username, first_name) VALUES (new.id, new.username, new.first_name);
    END;
    ''')

    cursor.execute('''
    CREATE TRIGGER IF NOT EXISTS users_ad AFTER DELETE ON users BEGIN
      INSERT INTO users_fts(users_fts, rowid, username, first_name) VALUES('delete', old.id, old.username, old.first_name);
    END;
    ''')

    cursor.execute('''
    CREATE TRIGGER IF NOT EXISTS users_au AFTER UPDATE ON users BEGIN
      INSERT INTO users_fts(users_fts, rowid, username, first_name) VALUES('delete', old.id, old.username, old.first_name);
      INSERT INTO users_fts(rowid, username, first_name) VALUES (new.id, new.username, new.first_name);
    END;
    ''')

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print(f"База данных {DB_PATH} инициализирована.")
