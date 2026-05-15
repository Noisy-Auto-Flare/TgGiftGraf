from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from database import get_db_connection
import os

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
        SELECT id, username, first_name 
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
    # Выбираем рёбра, где пользователь отправитель или получатель
    cursor.execute("""
        SELECT e.*, u1.username as from_username, u2.username as to_username
        FROM edges e
        JOIN users u1 ON e.from_user_id = u1.id
        JOIN users u2 ON e.to_user_id = u2.id
        WHERE e.from_user_id = ? OR e.to_user_id = ?
    """, (user_id, user_id))
    edges_rows = cursor.fetchall()
    
    nodes_ids = set()
    edges = []
    for row in edges_rows:
        nodes_ids.add(row['from_user_id'])
        nodes_ids.add(row['to_user_id'])
        edges.append({
            "from": row['from_user_id'],
            "to": row['to_user_id'],
            "weight": row['weight'],
            "title": f"Последний подарок: {row['last_gift_title']}"
        })
    
    # Получаем информацию об узлах (включая кластеры и источник)
    nodes = []
    if nodes_ids:
        placeholders = ', '.join(['?'] * len(nodes_ids))
        cursor.execute(f"""
            SELECT u.id, u.username, u.first_name, u.discovery_source, c.cluster_id
            FROM users u
            LEFT JOIN user_clusters c ON u.id = c.user_id
            WHERE u.id IN ({placeholders})
        """, list(nodes_ids))
        for row in cursor.fetchall():
            label = row['username'] if row['username'] else f"id{row['id']}"
            nodes.append({
                "id": row['id'],
                "label": label,
                "first_name": row['first_name'],
                "cluster": row['cluster_id'],
                "source": row['discovery_source']
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

@app.get("/api/stats/top-reach")
async def get_top_reach(limit: int = 10):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.id, u.username, u.first_name, r.reach_count
        FROM user_reach r
        JOIN users u ON r.user_id = u.id
        ORDER BY r.reach_count DESC
        LIMIT ?
    """, (limit,))
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results

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
    
    conn.close()
    return {
        "total_users": users_count,
        "total_edges": edges_count,
        "total_clusters": clusters_count
    }

# Раздача статики (если не через Caddy)
if os.path.exists("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
