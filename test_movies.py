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

def query(aql, experimental_match=False):
    url = f"{LOCAL_URL}/_db/{TARGET_DB}/_api/cursor"
    headers = {"Authorization": AUTH_HEADER, "Content-Type": "application/json"}
    body = {"query": aql}
    if experimental_match:
        body["options"] = {"matchStatement": "experimental"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

print("=== 1. Consulta estándar AQL: Actores en 'The Matrix' ===")
st, res = query("""
FOR m IN Movie
  FILTER m.title == 'The Matrix'
  FOR p, e IN 1..1 INBOUND m ACTED_IN
    RETURN { actor: p.name, roles: e.roles }
""")
print(f"Estado: {st}")
if st in (200, 201):
    for row in res.get("result", []):
        print(f"  - {row.get('actor')} interpretó a {row.get('roles')}")
else:
    print(f"  Error: {res.get('errorMessage')}")

print("\n=== 2. Consulta con MATCH (ArangoDB 4.0): ¿Quién actuó y dirigió la misma película? ===")
# En el soporte de MATCH de ArangoDB 4.0:
# - No se puede repetir la misma variable de nodo en el patrón MATCH (ej: p ... p da error 'variable p is assigned multiple times').
#   En su lugar se usan variables distintas y se compara con FILTER: FILTER director == actor.
# - RETURN DISTINCT en el nivel superior no es sintaxis AQL válida.
st, res = query("""
MATCH (director :Person) -[ :DIRECTED ]-> (m :Movie) <-[ :ACTED_IN ]- (actor :Person)
FILTER director == actor
RETURN { persona: director.name, pelicula: m.title }
""", experimental_match=True)
print(f"Estado: {st}")
if st in (200, 201):
    for row in res.get("result", []):
        print(f"  - {row.get('persona')} actuó y dirigió en '{row.get('pelicula')}'")
else:
    print(f"  Error: {res.get('errorMessage')}")

print("\n=== 3. Consulta con MATCH (ArangoDB 4.0): Co-actores de Keanu Reeves en The Matrix ===")
st, res = query("""
MATCH (keanu :Person) -[ :ACTED_IN ]-> (m :Movie) <-[ :ACTED_IN ]- (coactor :Person)
FILTER keanu.name == 'Keanu Reeves' AND m.title == 'The Matrix' AND coactor != keanu
RETURN { coactor: coactor.name, pelicula: m.title }
""", experimental_match=True)
print(f"Estado: {st}")
if st in (200, 201):
    for row in res.get("result", []):
        print(f"  - {row.get('coactor')} en '{row.get('pelicula')}'")
else:
    print(f"  Error: {res.get('errorMessage')}")
