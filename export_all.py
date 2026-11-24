import os
import json
import base64
import time
import urllib.request
import urllib.error

from env_utils import load_env, require_env

env = load_env()
HOST_URL = ""
USERNAME = ""
PASSWORD = ""
DB_NAME = ""
AUTH_HEADER = ""

# Pausa entre colecciones para no saturar el servidor (segundos)
DELAY_BETWEEN_COLS = float(env.get("export_delay", "0.5"))
# Número de reintentos ante errores 503 / reset de conexión
MAX_RETRIES = int(env.get("export_retries", "6"))
# Pausa base entre reintentos (se duplica en cada intento: backoff exponencial)
RETRY_BASE_WAIT = float(env.get("export_retry_wait", "5"))
# Nº de fallos consecutivos antes de hacer una pausa larga (circuit breaker)
CIRCUIT_BREAKER_THRESHOLD = int(env.get("export_circuit_threshold", "3"))
# Pausa larga del circuit breaker (segundos)
CIRCUIT_BREAKER_WAIT = float(env.get("export_circuit_wait", "30"))

# Contador global de fallos consecutivos (circuit breaker)
consecutive_failures = 0


def init_config():
    """Carga y valida las credenciales remotas exclusivamente desde .env / entorno."""
    global env, HOST_URL, USERNAME, PASSWORD, DB_NAME, AUTH_HEADER
    env = load_env()
    cfg = require_env(env, ["host_url", "host_username", "host_password", "db_name"])
    HOST_URL = cfg["host_url"].rstrip("/")
    USERNAME = cfg["host_username"]
    PASSWORD = cfg["host_password"]
    DB_NAME = cfg["db_name"]
    AUTH_HEADER = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

def api_request(path, method="GET", body=None, db=None, label=""):
    """Petición HTTP a ArangoDB con reintentos automáticos y backoff exponencial."""
    global consecutive_failures
    _db = db if db else DB_NAME
    url = f"{HOST_URL}/_db/{_db}{path}"
    headers = {"Authorization": AUTH_HEADER, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    tag = f"[{label}] " if label else ""

    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                consecutive_failures = 0  # éxito → reset contador
                return resp.status, json.loads(resp.read().decode())

        except urllib.error.HTTPError as e:
            try:
                code, body_err = e.code, json.loads(e.read().decode())
            except Exception:
                code, body_err = e.code, {"errorMessage": str(e)}
            is_retryable = code in (503, 429, 502, 504)
            if is_retryable and attempt < MAX_RETRIES:
                wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
                print(f"    ⚠  {tag}HTTP {code}, reintentando en {wait:.0f}s ({attempt}/{MAX_RETRIES})...")
                time.sleep(wait)
            else:
                consecutive_failures += 1
                return code, body_err

        except Exception as e:
            err_str = str(e).lower()
            is_retryable = any(k in err_str for k in ("reset", "timed out", "unreachable", "refused", "broken pipe", "eof", "ssl", "connection"))
            if is_retryable and attempt < MAX_RETRIES:
                wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
                print(f"    ⚠  {tag}Conexión fallida (intento {attempt}/{MAX_RETRIES}), esperando {wait:.0f}s...")
                time.sleep(wait)
            else:
                consecutive_failures += 1
                return None, {"errorMessage": str(e)}

    consecutive_failures += 1
    return None, {"errorMessage": "Se agotaron los reintentos"}


def circuit_breaker_check():
    """Si hay demasiados fallos seguidos, pausa para dejar recuperarse al servidor."""
    global consecutive_failures
    if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
        print(f"\n  🔴 {consecutive_failures} fallos consecutivos — pausando {CIRCUIT_BREAKER_WAIT}s para que el servidor se recupere...")
        time.sleep(CIRCUIT_BREAKER_WAIT)
        consecutive_failures = 0
        print("  🟢 Reanudando...\n")


def fetch_and_write_documents(col_name, out_path):
    """Descarga los documentos de una colección paginando el cursor y los escribe
    directamente al disco (streaming). Nunca acumula más de un batch en RAM.
    Devuelve el número total de documentos escritos."""
    batch_size = int(env.get("export_batch_size", "2500"))
    query = {
        "query": "FOR doc IN @@collection RETURN doc",
        "bindVars": {"@collection": col_name},
        "batchSize": batch_size,
        "ttl": 300,   # cursor vive 5 min en el servidor
        "stream": False
    }

    status, res = api_request("/_api/cursor", method="POST", body=query, label=col_name)
    if status not in (200, 201):
        raise RuntimeError(f"Error al abrir cursor para '{col_name}': {res}")

    total = 0
    cursor_id = res.get("id")
    has_more = res.get("hasMore", False)
    page = 1

    # Abrimos el fichero destino y comenzamos el array JSON manualmente
    with open(out_path, "w", encoding="utf-8") as fout:
        fout.write("[\n")
        first_doc = True

        def _write_batch(docs):
            nonlocal first_doc, total
            for doc in docs:
                if not first_doc:
                    fout.write(",\n")
                json.dump(doc, fout, ensure_ascii=False)
                first_doc = False
                total += 1

        _write_batch(res.get("result", []))

        while has_more and cursor_id:
            page += 1
            if page % 50 == 0:
                print(f"    ... {col_name}: {total:,} docs escritos (pág. {page})...")

            # ArangoDB ≥3.12 usa POST para continuar cursores; versiones anteriores usaban PUT
            status, res = api_request(f"/_api/cursor/{cursor_id}", method="POST", label=f"{col_name} pág.{page}")
            if status == 405:
                status, res = api_request(f"/_api/cursor/{cursor_id}", method="PUT", label=f"{col_name} pág.{page}")
            if status == 404:
                raise RuntimeError(
                    f"Cursor expirado para '{col_name}' en pág.{page} — se reintentará la colección completa"
                )
            if status not in (200, 201):
                raise RuntimeError(f"Error paginando cursor de '{col_name}' (pág.{page}): {res}")

            _write_batch(res.get("result", []))
            has_more = res.get("hasMore", False)
            cursor_id = res.get("id")
            # Pausa mínima entre páginas para no saturar
            time.sleep(0.1)

        fout.write("\n]\n")

    return total


def export_all():
    init_config()
    print(f"--> Conectando a {HOST_URL} (Base de datos: {DB_NAME})...")
    db_safe_name = DB_NAME.replace("/", "_")
    output_dir = os.path.join(os.path.dirname(__file__), "data", db_safe_name)
    os.makedirs(output_dir, exist_ok=True)

    # 1. Obtener todas las colecciones
    status, res = api_request("/_api/collection?excludeSystem=true", label="lista colecciones")
    if status != 200:
        print(f"❌ No se pudieron obtener las colecciones: {res}")
        return

    all_collections = res.get("result", [])
    print(f"\n--> Se encontraron {len(all_collections)} colecciones en '{DB_NAME}'")
    print(f"    delay={DELAY_BETWEEN_COLS}s | reintentos={MAX_RETRIES} | circuit_breaker={CIRCUIT_BREAKER_THRESHOLD} fallos → {CIRCUIT_BREAKER_WAIT}s pausa\n")

    # 2. Obtener todos los grafos nombrados
    status, graphs_res = api_request("/_api/gharial", label="lista grafos")
    graphs = []
    if status == 200:
        graphs = graphs_res.get("graphs", [])
        print(f"--> Se encontraron {len(graphs)} grafos en '{DB_NAME}'")
    else:
        print(f"Aviso: no se pudieron obtener los grafos ({graphs_res})")

    graphs_file = os.path.join(output_dir, "_graphs.json")
    with open(graphs_file, "w", encoding="utf-8") as f:
        json.dump(graphs, f, indent=2, ensure_ascii=False)
    print(f"✓ Definiciones de grafos guardadas en _graphs.json")

    # 2b. Obtener todas las views (ArangoSearch / search-alias)
    status, views_res = api_request("/_api/view", label="lista views")
    views = []
    if status == 200:
        for v in views_res.get("result", []):
            v_name = v.get("name")
            if not v_name:
                continue
            p_st, props = api_request(f"/_api/view/{v_name}/properties", label=f"view {v_name}")
            if p_st == 200:
                views.append(props)
            else:
                print(f"Aviso: no se pudieron obtener propiedades de la view '{v_name}' ({props})")
        print(f"--> Se encontraron {len(views)} views en '{DB_NAME}'")
    else:
        print(f"Aviso: no se pudieron obtener las views ({views_res})")

    views_file = os.path.join(output_dir, "_views.json")
    with open(views_file, "w", encoding="utf-8") as f:
        json.dump(views, f, indent=2, ensure_ascii=False)
    print(f"✓ Definiciones de views guardadas en _views.json")

    # 2c. Obtener índices secundarios (excluyendo primary y edge)
    indexes_by_col = {}
    total_extra_idx = 0
    for col in all_collections:
        col_name = col["name"]
        i_st, i_res = api_request(f"/_api/index?collection={col_name}", label=f"índices {col_name}")
        if i_st == 200:
            extra = [idx for idx in i_res.get("indexes", []) if idx.get("type") not in ("primary", "edge")]
            if extra:
                indexes_by_col[col_name] = extra
                total_extra_idx += len(extra)
    indexes_file = os.path.join(output_dir, "_indexes.json")
    with open(indexes_file, "w", encoding="utf-8") as f:
        json.dump(indexes_by_col, f, indent=2, ensure_ascii=False)
    print(f"✓ {total_extra_idx} índices secundarios guardados en _indexes.json")

    # Cargar manifest existente para reanudar si el script se interrumpió antes
    manifest_path = os.path.join(output_dir, "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["views_file"] = "_views.json"
        manifest["indexes_file"] = "_indexes.json"
        already_done = {c["name"] for c in manifest.get("collections", [])}
        print(f"    → Reanudando: {len(already_done)} colecciones ya exportadas se omitirán.")
    else:
        already_done = set()
        manifest = {
            "db_name": DB_NAME,
            "host_url": HOST_URL,
            "collections": [],
            "graphs_file": "_graphs.json",
            "views_file": "_views.json",
            "indexes_file": "_indexes.json"
        }

    # 3. Exportar cada colección
    failed = []
    pending = [c for c in all_collections if c["name"] not in already_done]
    total = len(all_collections)
    done_count = len(already_done)

    print(f"\n--> Descargando {len(pending)} colecciones pendientes ({done_count}/{total} ya hechas)...")
    for col in all_collections:
        col_name = col["name"]
        col_type = col["type"]  # 2=document, 3=edge

        if col_name in already_done:
            print(f"  ⏭  {col_name:35} (ya exportada)")
            continue

        # Circuit breaker: pausa larga si hay demasiados fallos seguidos
        circuit_breaker_check()

        try:
            file_name = f"{col_name}.json"
            out_path = os.path.join(output_dir, file_name)
            n_docs = fetch_and_write_documents(col_name, out_path)

            done_count += 1
            tipo = "aristas" if col_type == 3 else "vértices"
            print(f"  ✓ [{done_count}/{total}] {col_name:35} ({tipo}): {n_docs:,} docs")

            manifest["collections"].append({
                "name": col_name,
                "type": col_type,
                "count": n_docs,
                "file": file_name
            })
            # Guardar manifest tras cada éxito (reanudable)
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)

        except Exception as e:
            print(f"  ❌ '{col_name}': {e}")
            failed.append({"name": col_name, "type": col_type, "error": str(e)})

        time.sleep(DELAY_BETWEEN_COLS)

    # Resumen final
    total_docs = sum(c["count"] for c in manifest["collections"])
    print(f"\n{'='*60}")
    print(f"✅ Exportadas: {len(manifest['collections'])}/{total} colecciones | {total_docs} documentos totales")
    if failed:
        print(f"❌ Fallidas ({len(failed)}):")
        for fc in failed:
            print(f"   - {fc['name']}: {fc['error']}")
        print(f"\n💡 Vuelve a ejecutar export_all.py — las fallidas se reintentarán automáticamente.")
    else:
        print("🎉 ¡Todas las colecciones exportadas con éxito!")
    print(f"Datos en: '{output_dir}'")


if __name__ == "__main__":
    export_all()
