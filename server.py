from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from database import get_db_connection, init_db
import os

# Инициализируем БД при старте
init_db()

app = FastAPI(title="Telegram Gift Graph API")

# Разрешаем CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/users/search")
async def search_users(q: str = Query(..., min_length=2)):
    conn = get_db_connection()
    cursor = conn.cursor()
    # Поиск через FTS5
    cursor.execute("""
        SELECT rowid as id, username, first_name 
        FROM users_fts 
        WHERE users_fts MATCH ? 
        LIMIT 20
    """, (f"{q}*",))
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results

@app.get("/api/users/{identifier}/graph")
async def get_user_graph(identifier: str, depth: int = 1):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Находим user_id по username или id
    user_id = None
    if identifier.isdigit():
        user_id = int(identifier)
    else:
        cursor.execute("SELECT id FROM users WHERE username = ?", (identifier,))
        row = cursor.fetchone()
        if row:
            user_id = row['id']
            
    if not user_id:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    # Получаем рёбра (ego-graph depth=1)
    cursor.execute("""
        SELECT e.*, u1.username as from_username, u2.username as to_username
        FROM edges e
        JOIN users u1 ON e.from_user_id = u1.id
        JOIN users u2 ON e.to_user_id = u2.id
        WHERE e.from_user_id = ? OR e.to_user_id = ?
    """, (user_id, user_id))
    edges_rows = cursor.fetchall()
    
    nodes_ids = {user_id} # Всегда включаем искомого пользователя
    edges = []
    for row in edges_rows:
        nodes_ids.add(row['from_user_id'])
        nodes_ids.add(row['to_user_id'])
        edges.append({
            "from": row['from_user_id'],
            "to": row['to_user_id'],
            "weight": row['weight'],
            "label": str(row['weight']), # Число подарков над стрелкой
            "title": f"Подарков: {row['weight']}\nПоследний: {row['last_gift_title']}"
        })
    
    # Получаем информацию об узлах (включая кластеры и источник)
    nodes = []
    if nodes_ids:
        placeholders = ', '.join(['?'] * len(nodes_ids))
        cursor.execute(f"""
            SELECT u.id, u.username, u.first_name, u.discovery_source, u.has_photo, c.cluster_id
            FROM users u
            LEFT JOIN user_clusters c ON u.id = c.user_id
            WHERE u.id IN ({placeholders})
        """, list(nodes_ids))
        for row in cursor.fetchall():
            label = row['first_name'] if row['first_name'] else (row['username'] if row['username'] else f"id{row['id']}")
            if len(label) > 16:
                label = label[:13] + "..."
            nodes.append({
                "id": row['id'],
                "label": label,
                "username": row['username'],
                "first_name": row['first_name'],
                "cluster": row['cluster_id'],
                "source": row['discovery_source'],
                "has_photo": bool(row['has_photo'])
            })

    # Статистика пользователя
    cursor.execute("SELECT SUM(weight) as s FROM edges WHERE from_user_id = ?", (user_id,))
    outgoing = cursor.fetchone()['s'] or 0
    
    cursor.execute("SELECT SUM(weight) as s FROM edges WHERE to_user_id = ?", (user_id,))
    incoming = cursor.fetchone()['s'] or 0
    
    cursor.execute("SELECT reach_count FROM user_reach WHERE user_id = ?", (user_id,))
    reach_row = cursor.fetchone()
    reach_5 = reach_row['reach_count'] if reach_row else 0

    stats = {
        "user_id": user_id,
        "incoming_gifts": incoming,
        "outgoing_gifts": outgoing,
        "neighbors": len(nodes_ids) - 1 if user_id in nodes_ids else len(nodes_ids),
        "reach_5": reach_5
    }

    conn.close()
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": stats
    }

@app.get("/api/graph/global")
async def get_global_graph(limit: int = 100):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Получаем топ-пользователей по количеству связей
    cursor.execute("""
        SELECT u.id, u.username, u.first_name, u.has_photo, c.cluster_id
        FROM users u
        LEFT JOIN user_clusters c ON u.id = c.user_id
        WHERE u.id IN (SELECT from_user_id FROM edges UNION SELECT to_user_id FROM edges)
        LIMIT ?
    """, (limit,))
    nodes_rows = cursor.fetchall()
    
    nodes_ids = [row['id'] for row in nodes_rows]
    nodes = []
    for row in nodes_rows:
        display_name = row['first_name'] if row['first_name'] else (row['username'] if row['username'] else f"id{row['id']}")
        if len(display_name) > 16:
            display_name = display_name[:13] + "..."
            
        nodes.append({
            "id": row['id'],
            "label": display_name,
            "username": row['username'],
            "first_name": row['first_name'],
            "cluster": row['cluster_id'],
            "has_photo": bool(row['has_photo'])
        })
    
    # Получаем рёбра между всеми пользователями, которые входят в список узлов
    if nodes_ids:
        placeholders = ', '.join(['?'] * len(nodes_ids))
        cursor.execute(f"""
            SELECT from_user_id, to_user_id, weight, last_gift_title
            FROM edges
            WHERE from_user_id IN ({placeholders}) OR to_user_id IN ({placeholders})
        """, nodes_ids + nodes_ids)
        edges_rows = cursor.fetchall()
        
        edges = []
        for row in edges_rows:
            # Добавляем ребро только если оба узла есть в нашем списке отображаемых узлов
            if row['from_user_id'] in nodes_ids and row['to_user_id'] in nodes_ids:
                length = max(50, 300 - row['weight'] * 20)
                edges.append({
                    "from": row['from_user_id'],
                    "to": row['to_user_id'],
                    "weight": row['weight'],
                    "label": str(row['weight']), # Число подарков над стрелкой
                    "length": length,
                    "title": f"Подарков: {row['weight']}"
                })
    else:
        edges = []

    conn.close()
    return {"nodes": nodes, "edges": edges}

@app.get("/api/stats/top-reach")
async def get_top_reach(limit: int = 10):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.id, u.username, u.first_name, u.has_photo, r.reach_count
        FROM user_reach r
        JOIN users u ON r.user_id = u.id
        ORDER BY r.reach_count DESC
        LIMIT ?
    """, (limit,))
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results

@app.post("/api/analytics/run")
async def run_analytics_endpoint():
    from analytics import run_analytics
    try:
        run_analytics()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/stats/summary")
async def get_summary():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) as count FROM users")
    users_count = cursor.fetchone()['count']
    
    cursor.execute("SELECT COUNT(*) as count FROM edges")
    edges_count = cursor.fetchone()['count']
    
    cursor.execute("SELECT COUNT(DISTINCT cluster_id) as count FROM user_clusters")
    clusters_count = cursor.fetchone()['count']
    
    # Добавляем информацию о очереди и последних сканированиях для отладки
    cursor.execute("SELECT COUNT(*) as count FROM crawl_queue")
    queue_count = cursor.fetchone()['count']
    
    cursor.execute("SELECT id, username, first_name, last_scanned FROM users WHERE last_scanned > 0 ORDER BY last_scanned DESC LIMIT 5")
    last_scanned = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return {
        "total_users": users_count,
        "total_edges": edges_count,
        "total_clusters": clusters_count,
        "queue_count": queue_count,
        "last_scanned": last_scanned
    }

# Раздача статики (если не через Caddy)
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
