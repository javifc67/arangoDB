import os
import json
import base64
import urllib.request
import urllib.error

def load_env(env_path=".env"):
    env = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env

env = load_env()
HOST_URL = env.get("host_url", "https://z6cxx3dl.rnd.pilot.arango.ai:443").rstrip("/")
USERNAME = env.get("host_username", "upm_user")
PASSWORD = env.get("host_password", "xmn4NCGlymb8wxn_wrw")
DB_NAME = env.get("db_name", "Cacharreo_UPM")

AUTH_HEADER = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

def api_request(path, method="GET", body=None):
    url = f"{HOST_URL}/_db/{DB_NAME}{path}"
    headers = {"Authorization": AUTH_HEADER, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"errorMessage": str(e)}

def fetch_all_documents(col_name):
    query = {"query": "FOR doc IN @@collection RETURN doc", "bindVars": {"@collection": col_name}, "batchSize": 1000}
    status, res = api_request("/_api/cursor", method="POST", body=query)
    if status not in (200, 201):
        raise RuntimeError(f"Error consultando colección {col_name}: {res}")
    
    docs = list(res.get("result", []))
    cursor_id = res.get("id")
    has_more = res.get("hasMore", False)
    
    while has_more and cursor_id:
        status, res = api_request(f"/_api/cursor/{cursor_id}", method="PUT")
        docs.extend(res.get("result", []))
        has_more = res.get("hasMore", False)
        cursor_id = res.get("id")
        
    return docs

def export_movies():
    print(f"--> Conectando a {HOST_URL} (Base de datos: {DB_NAME})...")
    output_dir = os.path.join(os.path.dirname(__file__), "data", "movies")
    os.makedirs(output_dir, exist_ok=True)
    
    # 1. Obtener grafo Movies
    status, graph_res = api_request("/_api/gharial/Movies")
    if status == 200:
        graph_def = graph_res.get("graph", {})
        with open(os.path.join(output_dir, "graph_definition.json"), "w", encoding="utf-8") as f:
            json.dump(graph_def, f, indent=2, ensure_ascii=False)
        print("✓ Grafo 'Movies' exportado (graph_definition.json)")
    else:
        print(f"Aviso: no se pudo obtener definición de grafo ({graph_res})")
        graph_def = {}

    # Colecciones de vértices y aristas que componen Movies
    collections = [
        {"name": "Person", "type": 2},
        {"name": "Movie", "type": 2},
        {"name": "ACTED_IN", "type": 3},
        {"name": "DIRECTED", "type": 3},
        {"name": "FOLLOWS", "type": 3},
        {"name": "PRODUCED", "type": 3},
        {"name": "REVIEWED", "type": 3},
        {"name": "WROTE", "type": 3}
    ]

    manifest = {"graph": graph_def, "collections": []}

    print("\n--> Descargando colecciones...")
    for col in collections:
        name = col["name"]
        col_type = col["type"]
        docs = fetch_all_documents(name)
        
        file_path = os.path.join(output_dir, f"{name}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(docs, f, indent=2, ensure_ascii=False)
            
        print(f"  ✓ {name:10} ({'aristas' if col_type == 3 else 'vértices'}): {len(docs)} docs guardados en {name}.json")
        manifest["collections"].append({"name": name, "type": col_type, "count": len(docs), "file": f"{name}.json"})

    with open(os.path.join(output_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"\n¡Exportación completada exitosamente en '{output_dir}'!")

if __name__ == "__main__":
    export_movies()
