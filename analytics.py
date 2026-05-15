import time
import logging
from collections import Counter, deque
from database import get_db_connection, init_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def label_propagation(nodes, edges_list):
    """Простой алгоритм распространения меток для поиска сообществ."""
    # labels: {node_id: cluster_id}
    labels = {node: node for node in nodes}
    
    # adjacency list
    adj = {node: [] for node in nodes}
    for u, v in edges_list:
        if u in adj and v in adj:
            adj[u].append(v)
            adj[v].append(u)

    # Максимум 10 итераций
    for _ in range(10):
        changed = False
        node_list = list(nodes)
        import random
        random.shuffle(node_list)
        
        for node in node_list:
            if not adj[node]:
                continue
            
            neighbor_labels = [labels[neighbor] for neighbor in adj[node]]
            if not neighbor_labels:
                continue
                
            most_common = Counter(neighbor_labels).most_common(1)[0][0]
            if labels[node] != most_common:
                labels[node] = most_common
                changed = True
        
        if not changed:
            break
            
    return labels

def calculate_reach(node, adj, max_depth=5):
    """BFS для расчета охвата на заданную глубину."""
    visited = {node}
    queue = deque([(node, 0)])
    count = 0
    
    while queue:
        curr, depth = queue.popleft()
        if depth >= max_depth:
            continue
            
        for neighbor in adj.get(curr, []):
            if neighbor not in visited:
                visited.add(neighbor)
                count += 1
                queue.append((neighbor, depth + 1))
    return count

def run_analytics():
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()

    logger.info("Загрузка данных для аналитики...")
    
    # Загружаем всех пользователей и рёбра
    cursor.execute("SELECT id FROM users")
    nodes = [row['id'] for row in cursor.fetchall()]
    
    cursor.execute("SELECT from_user_id, to_user_id FROM edges")
    edges_list = [(row['from_user_id'], row['to_user_id']) for row in cursor.fetchall()]
    
    if not nodes:
        logger.info("Нет данных для анализа.")
        return

    # 1. Сообщества (Label Propagation)
    logger.info(f"Расчет сообществ для {len(nodes)} узлов...")
    clusters = label_propagation(nodes, edges_list)
    
    cursor.execute("DELETE FROM user_clusters")
    for node_id, cluster_id in clusters.items():
        cursor.execute("INSERT INTO user_clusters (user_id, cluster_id) VALUES (?, ?)", (node_id, cluster_id))
    
    # 2. Охват (BFS)
    logger.info("Расчет охвата на 5 шагов...")
    adj = {node: set() for node in nodes}
    for u, v in edges_list:
        if u in adj and v in adj:
            adj[u].add(v)
            adj[v].add(u)
    
    # Для экономии времени считаем охват только для активных пользователей (у кого есть рёбра)
    active_nodes = [node for node in nodes if adj[node]]
    
    cursor.execute("DELETE FROM user_reach")
    now = int(time.time())
    
    for node in active_nodes:
        reach_count = calculate_reach(node, adj, 5)
        cursor.execute(
            "INSERT INTO user_reach (user_id, reach_count, updated_at) VALUES (?, ?, ?)",
            (node, reach_count, now)
        )
    
    conn.commit()
    conn.close()
    logger.info("Аналитика завершена.")

if __name__ == "__main__":
    run_analytics()
