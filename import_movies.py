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
PORT = env.get("ARANGO_PORT", "8529")
LOCAL_URL = f"http://localhost:{PORT}"
USER = "root"
PASSWORD = env.get("ARANGO_ROOT_PASSWORD", "rootpassword")
TARGET_DB = env.get("LOCAL_DB_NAME", "_system")

AUTH_HEADER = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

def api_request(path, method="GET", body=None):
    url = f"{LOCAL_URL}/_db/{TARGET_DB}{path}"
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
    except urllib.error.URLError as e:
        return None, {"errorMessage": f"No se pudo conectar a {LOCAL_URL}: {e.reason}"}

def clean_doc(doc):
    # Conservamos _key, _from, _to y eliminamos _id y _rev de la instancia remota
    d = dict(doc)
    d.pop("_id", None)
    d.pop("_rev", None)
    return d

def import_movies():
    data_dir = os.path.join(os.path.dirname(__file__), "data", "movies")
    manifest_path = os.path.join(data_dir, "manifest.json")

    if not os.path.exists(manifest_path):
        print(f"Error: No se encontró {manifest_path}. Ejecuta primero export_movies.py")
        return

    print(f"--> Verificando conexión a ArangoDB local en {LOCAL_URL} (DB: {TARGET_DB})...")
    status, res = api_request("/_api/version")
    if status != 200:
        print(f"❌ Error al conectar con ArangoDB local ({res.get('errorMessage')}).")
        print("   Asegúrate de que el contenedor de Docker esté iniciado: 'docker compose up -d'")
        return

    print(f"✓ Conectado a ArangoDB v{res.get('version')} ({res.get('license')})")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # 1. Crear colecciones e importar documentos
    print("\n--> Creando colecciones e importando documentos...")
    for col_info in manifest["collections"]:
        col_name = col_info["name"]
        col_type = col_info["type"]
        file_name = col_info["file"]

        # Crear colección si no existe
        api_request("/_api/collection", method="POST", body={"name": col_name, "type": col_type})

        json_file = os.path.join(data_dir, file_name)
        with open(json_file, "r", encoding="utf-8") as f:
            raw_docs = json.load(f)

        docs = [clean_doc(d) for d in raw_docs]

        # Insertar en lotes con overwriteMode=replace
        st, insert_res = api_request(f"/_api/document/{col_name}?overwriteMode=replace", method="POST", body=docs)
        if st in (201, 202):
            print(f"  ✓ {col_name:10} ({'aristas' if col_type == 3 else 'vértices'}): {len(docs)} documentos importados")
        else:
            print(f"  ❌ Error importando en {col_name}: {insert_res}")

    # 2. Recrear el Named Graph 'Movies'
    graph_def = manifest.get("graph", {})
    if graph_def:
        print("\n--> Registrando el Grafo 'Movies'...")
        # Eliminar si ya existía previamente
        api_request("/_api/gharial/Movies", method="DELETE")

        edge_definitions = graph_def.get("edgeDefinitions", [])
        graph_payload = {
            "name": "Movies",
            "edgeDefinitions": edge_definitions
        }
        st, g_res = api_request("/_api/gharial", method="POST", body=graph_payload)
        if st in (201, 202):
            print("  ✓ Grafo 'Movies' creado con éxito con sus relaciones.")
        else:
            print(f"  Aviso al crear grafo: {g_res}")

    print("\n🎉 ¡Base de datos Movies desplegada en local con éxito!")

if __name__ == "__main__":
    import_movies()
