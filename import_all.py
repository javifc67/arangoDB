import os
import json
import time
import base64
import hashlib
import urllib.request
import urllib.error

from env_utils import load_env

env = load_env()
PORT = env.get("ARANGO_PORT", "8529")
TARGET_URL = env.get("TARGET_URL", f"http://localhost:{PORT}").rstrip("/")
USER = env.get("TARGET_USERNAME", "root")
PASSWORD = env.get("TARGET_PASSWORD", env.get("ARANGO_ROOT_PASSWORD", "rootpassword"))
TARGET_DB = env.get("TARGET_DB_NAME", env.get("LOCAL_DB_NAME", "_system"))
SOURCE_FOLDER = env.get("SOURCE_FOLDER", env.get("LOCAL_DB_NAME", TARGET_DB))

IS_REMOTE = not (TARGET_URL.startswith("http://localhost") or TARGET_URL.startswith("http://127.0.0.1"))
BATCH_SIZE = int(env.get("IMPORT_BATCH_SIZE", "2000" if IS_REMOTE else "1000"))
BATCH_DELAY = float(env.get("IMPORT_BATCH_DELAY", "0.05" if IS_REMOTE else "0"))
MAX_RETRIES = int(env.get("IMPORT_RETRIES", "6"))
RETRY_BASE_WAIT = float(env.get("IMPORT_RETRY_WAIT", "5"))
REQUEST_TIMEOUT = int(env.get("IMPORT_TIMEOUT", "120"))
CIRCUIT_BREAKER_THRESHOLD = int(env.get("IMPORT_CIRCUIT_THRESHOLD", "3"))
CIRCUIT_BREAKER_WAIT = float(env.get("IMPORT_CIRCUIT_WAIT", "30"))

AUTH_HEADER = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
consecutive_failures = 0


def _http_request(url, method="GET", body=None, label=""):
    """Petición HTTP con reintentos automáticos y backoff exponencial."""
    global consecutive_failures
    headers = {"Authorization": AUTH_HEADER, "Content-Type": "application/json"}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    tag = f"[{label}] " if label else ""

    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                consecutive_failures = 0
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                body_err = json.loads(e.read().decode("utf-8"))
            except Exception:
                body_err = {"errorMessage": str(e)}
            code = e.code
            is_retryable = code in (503, 429, 502, 504, 408)
            if is_retryable and attempt < MAX_RETRIES:
                wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
                print(f"    ⚠  {tag}HTTP {code}, reintentando en {wait:.0f}s ({attempt}/{MAX_RETRIES})...", flush=True)
                time.sleep(wait)
            else:
                if code >= 500:
                    consecutive_failures += 1
                return code, body_err
        except Exception as e:
            err_str = str(e).lower()
            is_retryable = any(
                k in err_str
                for k in ("reset", "timed out", "timeout", "unreachable", "refused", "broken pipe", "eof", "ssl", "connection", "incomplete")
            )
            if is_retryable and attempt < MAX_RETRIES:
                wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
                print(f"    ⚠  {tag}Error de red ({e}), reintentando en {wait:.0f}s ({attempt}/{MAX_RETRIES})...", flush=True)
                time.sleep(wait)
            else:
                consecutive_failures += 1
                return None, {"errorMessage": f"No se pudo conectar a {TARGET_URL}: {e}"}

    consecutive_failures += 1
    return None, {"errorMessage": "Se agotaron los reintentos"}


def api_request(path, method="GET", body=None, db=None, label=""):
    _db = db if db else TARGET_DB
    url = f"{TARGET_URL}/_db/{_db}{path}"
    return _http_request(url, method=method, body=body, label=label)


def root_request(path, method="GET", body=None, label=""):
    """Petición al endpoint raíz (_db/_system) para operaciones de admin."""
    url = f"{TARGET_URL}/_db/_system{path}"
    return _http_request(url, method=method, body=body, label=label)


def circuit_breaker_check():
    global consecutive_failures
    if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
        print(f"\n  🔴 {consecutive_failures} fallos consecutivos — pausando {CIRCUIT_BREAKER_WAIT}s...", flush=True)
        time.sleep(CIRCUIT_BREAKER_WAIT)
        consecutive_failures = 0
        print("  🟢 Reanudando...\n", flush=True)


def clean_doc(doc):
    """Elimina _id y _rev para evitar conflictos al insertar, conserva _key, _from, _to."""
    d = dict(doc)
    d.pop("_id", None)
    d.pop("_rev", None)
    return d


def ensure_database(db_name):
    """Crea la base de datos si no existe (requiere permisos sobre _system)."""
    status, res = root_request("/_api/database", label="listar BDs")
    if status == 200:
        existing = res.get("result", [])
        if db_name not in existing:
            st, r = root_request("/_api/database", method="POST", body={"name": db_name}, label=f"crear BD {db_name}")
            if st in (200, 201):
                print(f"  ✓ Base de datos '{db_name}' creada.")
                return True
            print(f"  ❌ No se pudo crear la base de datos '{db_name}': {r}")
            return False
        print(f"  ✓ Base de datos '{db_name}' ya existe.")
        return True
    print(f"  ⚠️  No se pudo consultar _system para crear '{db_name}' (HTTP {status}): {res}")
    return False


def _progress_path(data_dir, target_url, target_db):
    key = hashlib.sha1(f"{target_url}|{target_db}".encode("utf-8")).hexdigest()[:10]
    return os.path.join(data_dir, f".import_progress_{key}.json")


def load_progress(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"completed_collections": []}


def save_progress(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def clean_index_payload(idx):
    """Elimina campos de solo lectura antes de recrear un índice."""
    drop = {"id", "selectivityEstimate", "isNewlyCreated", "error", "code", "bestIndexedLevel", "worstIndexedLevel"}
    return {k: v for k, v in idx.items() if k not in drop}


def clean_view_payload(view):
    """Elimina campos de solo lectura antes de crear una ArangoSearch view."""
    drop = {"id", "globallyUniqueId", "error", "code"}
    return {k: v for k, v in view.items() if k not in drop}


def import_all():
    data_base = os.path.join(os.path.dirname(__file__), "data")
    data_dir = os.path.join(data_base, SOURCE_FOLDER)

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
            print("Configura SOURCE_FOLDER o LOCAL_DB_NAME con el nombre de la carpeta a importar.")
            return
        else:
            print(f"Error: No se encontró ningún manifest.json en '{data_base}'.")
            print("Ejecuta primero export_all.py")
            return

    manifest_path = os.path.join(data_dir, "manifest.json")
    dest_db = TARGET_DB

    print(f"--> Verificando conexión a ArangoDB en {TARGET_URL} (BD destino: '{dest_db}')...", flush=True)
    # Primero probar directamente contra la BD destino (funciona para usuarios sin acceso a _system)
    status, res = api_request("/_api/version", db=dest_db, label="version")
    if status == 404 and dest_db != "_system":
        # La BD aún no existe; intentar crearla vía _system
        st_sys, res_sys = root_request("/_api/version", label="version _system")
        if st_sys != 200:
            print(f"❌ Error al conectar con ArangoDB ({res_sys.get('errorMessage')}).")
            return
        print(f"✓ Conectado a ArangoDB v{res_sys.get('version')} ({res_sys.get('license')})")
        if not ensure_database(dest_db):
            return
    elif status != 200:
        print(f"❌ Error al conectar con ArangoDB en '{dest_db}' (HTTP {status}): {res.get('errorMessage', res)}")
        return
    else:
        print(f"✓ Conectado a ArangoDB v{res.get('version')} ({res.get('license')})", flush=True)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    prog_file = _progress_path(data_dir, TARGET_URL, dest_db)
    progress = load_progress(prog_file)
    completed = set(progress.get("completed_collections", []))

    collections = manifest.get("collections", [])
    print(f"\n--> Carpeta origen: '{data_dir}' → Destino: '{dest_db}' en {TARGET_URL}")
    print(f"--> Importando {len(collections)} colecciones (batch={BATCH_SIZE}, ya completadas={len(completed)})...", flush=True)

    failed_collections = []

    # 1. Crear colecciones e importar documentos (primero vértices tipo 2, luego aristas tipo 3)
    ordered_cols = [c for t in (2, 3) for c in collections if c["type"] == t]
    total_cols = len(ordered_cols)

    for idx_col, col_info in enumerate(ordered_cols, 1):
        col_name = col_info["name"]
        col_type = col_info["type"]
        file_name = col_info["file"]

        if col_name in completed:
            print(f"  ⏭  [{idx_col}/{total_cols}] {col_name:35} (ya importada)", flush=True)
            continue

        circuit_breaker_check()

        # Crear colección si no existe (HTTP 409 = ya existe, es seguro)
        c_st, c_res = api_request(
            "/_api/collection",
            method="POST",
            body={"name": col_name, "type": col_type},
            db=dest_db,
            label=f"crear {col_name}",
        )
        if c_st not in (200, 201, 409):
            print(f"  ❌ [{idx_col}/{total_cols}] No se pudo crear colección '{col_name}': HTTP {c_st} {c_res}", flush=True)
            failed_collections.append(col_name)
            continue

        json_file = os.path.join(data_dir, file_name)
        if not os.path.exists(json_file):
            print(f"  ⚠️  Archivo no encontrado: {file_name}, omitiendo colección '{col_name}'", flush=True)
            failed_collections.append(col_name)
            continue

        with open(json_file, "r", encoding="utf-8") as f:
            raw_docs = json.load(f)

        docs = [clean_doc(d) for d in raw_docs]
        del raw_docs

        if not docs:
            print(f"  ✓ [{idx_col}/{total_cols}] {col_name:35} (vacía, 0 docs)", flush=True)
            completed.add(col_name)
            progress["completed_collections"] = sorted(completed)
            save_progress(prog_file, progress)
            continue

        total_ok = 0
        errores = []
        n_batches = (len(docs) + BATCH_SIZE - 1) // BATCH_SIZE

        for b_idx, i in enumerate(range(0, len(docs), BATCH_SIZE), 1):
            batch = docs[i:i + BATCH_SIZE]
            st, insert_res = api_request(
                f"/_api/document/{col_name}?overwriteMode=replace",
                method="POST",
                body=batch,
                db=dest_db,
                label=f"{col_name} {b_idx}/{n_batches}",
            )
            if st in (201, 202):
                if isinstance(insert_res, list):
                    doc_errs = [r for r in insert_res if isinstance(r, dict) and r.get("error")]
                    if doc_errs:
                        errores.extend(doc_errs[:3])
                    total_ok += len(batch) - len(doc_errs)
                else:
                    total_ok += len(batch)
            else:
                errores.append({"batch": b_idx, "status": st, "error": insert_res})

            if n_batches >= 20 and b_idx % 20 == 0:
                print(f"    ... {col_name}: {total_ok:,}/{len(docs):,} docs ({b_idx}/{n_batches} lotes)", flush=True)

            if BATCH_DELAY > 0:
                time.sleep(BATCH_DELAY)

        tipo = "aristas" if col_type == 3 else "vértices"
        if not errores and total_ok == len(docs):
            print(f"  ✓ [{idx_col}/{total_cols}] {col_name:35} ({tipo}): {total_ok:,} docs importados", flush=True)
            completed.add(col_name)
            progress["completed_collections"] = sorted(completed)
            save_progress(prog_file, progress)
        else:
            print(f"  ❌ [{idx_col}/{total_cols}] {col_name:35} ({tipo}): {total_ok:,}/{len(docs):,} ok, errores: {errores[:2]}", flush=True)
            failed_collections.append(col_name)

    # 2. Recrear índices secundarios
    indexes_file = os.path.join(data_dir, manifest.get("indexes_file", "_indexes.json"))
    if os.path.exists(indexes_file):
        with open(indexes_file, "r", encoding="utf-8") as f:
            indexes_by_col = json.load(f)
        if indexes_by_col:
            print(f"\n--> Creando índices secundarios en {len(indexes_by_col)} colecciones...", flush=True)
            for col_name, idx_list in indexes_by_col.items():
                for idx in idx_list:
                    payload = clean_index_payload(idx)
                    st, i_res = api_request(
                        f"/_api/index?collection={col_name}",
                        method="POST",
                        body=payload,
                        db=dest_db,
                        label=f"idx {col_name}",
                    )
                    if st in (200, 201):
                        print(f"  ✓ Índice '{payload.get('name', payload.get('type'))}' en '{col_name}' ({payload.get('type')} {payload.get('fields')})")
                    else:
                        print(f"  ⚠️  Aviso índice en '{col_name}': HTTP {st} {i_res}")

    # 3. Recrear grafos nombrados
    graphs_file = os.path.join(data_dir, manifest.get("graphs_file", "_graphs.json"))
    if os.path.exists(graphs_file):
        with open(graphs_file, "r", encoding="utf-8") as f:
            graphs = json.load(f)

        if graphs:
            print(f"\n--> Registrando {len(graphs)} grafos...", flush=True)
            for graph in graphs:
                graph_name = graph.get("name") or graph.get("_key")
                if not graph_name:
                    continue

                # Si ya existe este mismo grafo, recrearlo para dejar su definición al día
                api_request(f"/_api/gharial/{graph_name}", method="DELETE", db=dest_db, label=f"del grafo {graph_name}")

                payload = {
                    "name": graph_name,
                    "edgeDefinitions": graph.get("edgeDefinitions", []),
                    "orphanCollections": graph.get("orphanCollections", []),
                }
                st, g_res = api_request("/_api/gharial", method="POST", body=payload, db=dest_db, label=f"grafo {graph_name}")
                if st in (201, 202):
                    print(f"  ✓ Grafo '{graph_name}' creado correctamente.")
                else:
                    print(f"  ⚠️  Aviso al crear grafo '{graph_name}': HTTP {st} {g_res}")

    # 4. Recrear views (ArangoSearch / search-alias)
    views_file = os.path.join(data_dir, manifest.get("views_file", "_views.json"))
    if os.path.exists(views_file):
        with open(views_file, "r", encoding="utf-8") as f:
            views = json.load(f)

        if views:
            print(f"\n--> Creando {len(views)} views...", flush=True)
            for view in views:
                v_name = view.get("name")
                if not v_name:
                    continue
                payload = clean_view_payload(view)
                st, v_res = api_request("/_api/view", method="POST", body=payload, db=dest_db, label=f"view {v_name}")
                if st in (200, 201):
                    print(f"  ✓ View '{v_name}' ({payload.get('type')}) creada correctamente.")
                elif st == 409:
                    # Ya existe -> actualizar propiedades
                    props_payload = {k: v for k, v in payload.items() if k not in ("name", "type")}
                    p_st, p_res = api_request(
                        f"/_api/view/{v_name}/properties",
                        method="PUT",
                        body=props_payload,
                        db=dest_db,
                        label=f"update view {v_name}",
                    )
                    if p_st == 200:
                        print(f"  ✓ View '{v_name}' ya existía; propiedades actualizadas.")
                    else:
                        print(f"  ⚠️  Aviso al actualizar view '{v_name}': HTTP {p_st} {p_res}")
                else:
                    print(f"  ⚠️  Aviso al crear view '{v_name}': HTTP {st} {v_res}")

    total_docs = sum(c["count"] for c in collections)
    print(f"\n{'='*60}")
    if failed_collections:
        print(f"⚠️  Importación finalizada con {len(failed_collections)} colecciones fallidas: {failed_collections}")
        print("💡 Vuelve a ejecutar import_all.py — las completadas se omitirán automáticamente.")
    else:
        print(f"🎉 ¡Importación completa! {len(collections)} colecciones, {total_docs:,} documentos en '{dest_db}' ({TARGET_URL}).")


if __name__ == "__main__":
    import_all()
