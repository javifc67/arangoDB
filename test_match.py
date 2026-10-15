import json
import urllib.request
import urllib.error
import base64

ARANGO_URL = "http://localhost:8529"
USER = "root"
PASSWORD = "rootpassword"

auth_header = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

def request(path, method="GET", body=None):
    url = f"{ARANGO_URL}{path}"
    headers = {"Authorization": auth_header, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

print("=== 1. Comprobando versión de ArangoDB ===")
st, res = request("/_api/version")
print(f"Versión: {res.get('version')} ({res.get('license')})")

print("\n=== 2. Creando colecciones e insertando datos ===")
# Limpiar colecciones si ya existen de ejecuciones previas (idempotencia)
request("/_api/collection/knows", "DELETE")
request("/_api/collection/users", "DELETE")

# Crear colección de vértices 'users'
request("/_api/collection", "POST", {"name": "users", "type": 2})
# Crear colección de aristas 'knows'
request("/_api/collection", "POST", {"name": "knows", "type": 3})

users = [
    {"_key": "alice", "name": "Alice", "role": "Engineer"},
    {"_key": "bob", "name": "Bob", "role": "Architect"},
    {"_key": "charlie", "name": "Charlie", "role": "Lead"}
]
for u in users:
    request("/_api/document/users", "POST", u)

edges = [
    {"_from": "users/alice", "_to": "users/bob", "since": 2020, "rel": "works_with"},
    {"_from": "users/bob", "_to": "users/charlie", "since": 2022, "rel": "reports_to"}
]
for e in edges:
    request("/_api/document/knows", "POST", e)
print("Datos creados: Alice -> Bob -> Charlie")

print("\n=== 3. Prueba A: MATCH SIN el flag experimental ===")
q_test = "MATCH (u :users) RETURN u"
print(f"Query: {q_test}")
st, res = request("/_api/cursor", "POST", {"query": q_test})
print(f"Respuesta HTTP {st}:")
print(f"  Error {res.get('errorNum')}: {res.get('errorMessage')}")

print("\n=== 4. Prueba B: MATCH CON el flag { matchStatement: 'experimental' } ===")
st, res = request("/_api/cursor", "POST", {
    "query": q_test,
    "options": {"matchStatement": "experimental"}
})
print(f"Query: {q_test}")
print(f"Respuesta HTTP {st}: Éxito!")
print("Resultados devueltos:")
for doc in res.get("result", []):
    print(f"  - {doc.get('name')} ({doc.get('role')})")

print("\n=== 5. Prueba C: MATCH en Grafo (1 salto: Alice -> Bob) ===")
q_graph1 = "MATCH (a :users) -[ e :knows ]-> (b :users) RETURN [a.name, e.rel, b.name]"
print(f"Query: {q_graph1}")
st, res = request("/_api/cursor", "POST", {
    "query": q_graph1,
    "options": {"matchStatement": "experimental"}
})
print(f"Respuesta HTTP {st}:")
print(json.dumps(res.get("result", []), indent=2))

print("\n=== 6. Prueba D: MATCH en Grafo Multi-Hop (2 saltos: Alice -> Bob -> Charlie) ===")
q_graph2 = "MATCH (a :users) -[ e1 :knows ]-> (b :users) -[ e2 :knows ]-> (c :users) RETURN { origen: a.name, intermedio: b.name, destino: c.name }"
print(f"Query: {q_graph2}")
st, res = request("/_api/cursor", "POST", {
    "query": q_graph2,
    "options": {"matchStatement": "experimental"}
})
print(f"Respuesta HTTP {st}:")
print(json.dumps(res.get("result", []), indent=2))
