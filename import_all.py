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
PORT     = env.get("ARANGO_PORT", "8529")
LOCAL_URL = f"http://localhost:{PORT}"
USER     = "root"
PASSWORD = env.get("ARANGO_ROOT_PASSWORD", "rootpassword")
TARGET_DB = env.get("LOCAL_DB_NAME", "_system")

AUTH_HEADER = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

def api_request(path, method="GET", body=None, db=None):
    _db = db if db else TARGET_DB
    url = f"{LOCAL_URL}/_db/{_db}{path}"
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

def root_request(path, method="GET", body=None):
    """Petición al endpoint raíz (_db/_system) para operaciones de admin."""
    url = f"{LOCAL_URL}/_db/_system{path}"
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
    """Elimina _id y _rev para evitar conflictos al insertar, conserva _key, _from, _to."""
    d = dict(doc)
    d.pop("_id", None)
    d.pop("_rev", None)
    return d

def ensure_database(db_name):
    """Crea la base de datos local si no existe."""
    # Verificar si existe
    status, res = root_request("/_api/database")
    if status == 200:
        existing = res.get("result", [])
        if db_name not in existing:
            st, r = root_request("/_api/database", method="POST", body={"name": db_name})
            if st in (200, 201):
                print(f"  ✓ Base de datos '{db_name}' creada localmente.")
            else:
                print(f"  ❌ No se pudo crear la base de datos '{db_name}': {r}")
        else:
            print(f"  ✓ Base de datos '{db_name}' ya existe.")

def import_all():
    # Buscar directorio de datos: usa LOCAL_DB_NAME o el primer manifest que encuentre
    data_base = os.path.join(os.path.dirname(__file__), "data")

    # Intentar usar LOCAL_DB_NAME como nombre de carpeta, o preguntar al usuario
    db_folder = env.get("LOCAL_DB_NAME", TARGET_DB)
    data_dir = os.path.join(data_base, db_folder)

    # Si no existe, buscar cualquier carpeta con manifest.json
    if not os.path.exists(os.path.join(data_dir, "manifest.json")):
        found = []
        if os.path.isdir(data_base):
            for d in os.listdir(data_base):
                candidate = os.path.join(data_base, d)
                if os.path.isfile(os.path.join(candidate, "manifest.json")):
                    found.append(d)
        if len(found) == 1:
            data_dir = os.path.join(data_base, found[0])
            print(f"  → Usando carpeta de datos: '{found[0]}'")
        elif len(found) > 1:
            print(f"Se encontraron varias carpetas de datos: {found}")
            print("Configura LOCAL_DB_NAME en .env con el nombre de la carpeta a importar.")
            return
        else:
            print(f"Error: No se encontró ningún manifest.json en '{data_base}'.")
            print("Ejecuta primero export_all.py")
            return

    manifest_path = os.path.join(data_dir, "manifest.json")
    print(f"--> Verificando conexión a ArangoDB local en {LOCAL_URL}...")
    status, res = root_request("/_api/version")
    if status != 200:
        print(f"❌ Error al conectar con ArangoDB local ({res.get('errorMessage')}).")
        print("   Asegúrate de que el contenedor de Docker esté iniciado: 'docker compose up -d'")
        return

    print(f"✓ Conectado a ArangoDB v{res.get('version')} ({res.get('license')})")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    original_db = manifest.get("db_name", "imported_db")

    # Determinar la base de datos de destino
    dest_db = TARGET_DB
    print(f"\n--> Base de datos de destino: '{dest_db}'")

    # Crear la base de datos si no es _system
    if dest_db != "_system":
        ensure_database(dest_db)

    # 1. Crear colecciones e importar documentos
    collections = manifest.get("collections", [])
    print(f"\n--> Importando {len(collections)} colecciones...")

    # Primero las de vértices (tipo 2), luego las de aristas (tipo 3)
    for col_type_filter in [2, 3]:
        for col_info in collections:
            if col_info["type"] != col_type_filter:
                continue

            col_name = col_info["name"]
            col_type = col_info["type"]
            file_name = col_info["file"]

            # Crear colección si no existe
            api_request("/_api/collection", method="POST", body={"name": col_name, "type": col_type}, db=dest_db)

            json_file = os.path.join(data_dir, file_name)
            if not os.path.exists(json_file):
                print(f"  ⚠️  Archivo no encontrado: {file_name}, omitiendo colección '{col_name}'")
                continue

            with open(json_file, "r", encoding="utf-8") as f:
                raw_docs = json.load(f)

            docs = [clean_doc(d) for d in raw_docs]

            if not docs:
                print(f"  ✓ {col_name:30} (vacía, 0 docs)")
                continue

            # Insertar en lotes de 500 para evitar payloads muy grandes
            batch_size = 500
            total_ok = 0
            errores = []
            for i in range(0, len(docs), batch_size):
                batch = docs[i:i + batch_size]
                st, insert_res = api_request(
                    f"/_api/document/{col_name}?overwriteMode=replace",
                    method="POST", body=batch, db=dest_db
                )
                if st in (201, 202):
                    total_ok += len(batch)
                else:
                    errores.append(insert_res)

            tipo = "aristas" if col_type == 3 else "vértices"
            if not errores:
                print(f"  ✓ {col_name:30} ({tipo}): {total_ok} docs importados")
            else:
                print(f"  ❌ {col_name:30} ({tipo}): {total_ok} ok, errores: {errores[:2]}")

    # 2. Recrear grafos nombrados
    graphs_file = os.path.join(data_dir, manifest.get("graphs_file", "_graphs.json"))
    if os.path.exists(graphs_file):
        with open(graphs_file, "r", encoding="utf-8") as f:
            graphs = json.load(f)

        if graphs:
            print(f"\n--> Registrando {len(graphs)} grafos...")
            for graph in graphs:
                graph_name = graph.get("name") or graph.get("_key")
                if not graph_name:
                    continue

                # Eliminar si ya existe
                api_request(f"/_api/gharial/{graph_name}", method="DELETE", db=dest_db)

                payload = {
                    "name": graph_name,
                    "edgeDefinitions": graph.get("edgeDefinitions", []),
                    "orphanCollections": graph.get("orphanCollections", [])
                }
                st, g_res = api_request("/_api/gharial", method="POST", body=payload, db=dest_db)
                if st in (201, 202):
                    print(f"  ✓ Grafo '{graph_name}' creado correctamente.")
                else:
                    print(f"  ⚠️  Aviso al crear grafo '{graph_name}': {g_res}")

    total_docs = sum(c["count"] for c in collections)
    print(f"\n🎉 ¡Importación completa! {len(collections)} colecciones, {total_docs} documentos totales en '{dest_db}'.")

if __name__ == "__main__":
    import_all()
