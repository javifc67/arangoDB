# Despliegue de ArangoDB 4.0 con soporte para la cláusula MATCH

Este directorio contiene la configuración de Docker Compose para desplegar una instancia de **ArangoDB 4.0 (pre-release)** con soporte para la nueva sentencia de coincidencia de patrones **`MATCH`** en AQL.

---

## 🔍 Confirmación técnica sobre la cláusula `MATCH`

1. **¿Qué versión tiene la cláusula `MATCH`?**
   * La cláusula `MATCH` ha sido introducida en el desarrollo de **ArangoDB 4.0** (rama `4.0` / `devel`, PR `#22531` *"AQL MATCH Statement MVP"* y mejoras subsiguientes como `#23038` y `#23442`).
   * No está disponible en la versión estable estándar 3.x (como 3.12).
   * Requiere desplegar la imagen de previsualización:
     ```yaml
     image: arangodb/core-preview:4.0-nightly
     # o bien: arangodb/enterprise-preview:4.0-nightly
     ```

2. **¿Cómo se activa el flag experimental?**
   * Por defecto, la sentencia viene desactivada en el motor.
   * Si intentas ejecutar `MATCH` sin el flag, ArangoDB devuelve:
     ```
     HTTP 501: "the MATCH statement is an experimental feature. If you want to use it, please specify `matchStatement: 'experimental'` in the query options."
     ```
   * Para habilitarla en las consultas, debes pasar la opción **`matchStatement: "experimental"`** en el payload de la query:
     ```json
     {
       "query": "MATCH (a :users) -[ e :knows ]-> (b :users) RETURN [a.name, e.rel, b.name]",
       "options": {
         "matchStatement": "experimental"
       }
     }
     ```

> **Nota importante sobre la interfaz Web:**  
> Las imágenes nightly/preview de ArangoDB 4.0 son imágenes del motor *headless* (enfocadas en el core de la base de datos y la API REST). No incluyen la interfaz gráfica antigua de Aardvark en `/`, por lo que las pruebas se realizan mediante la API HTTP (Postman, cURL, Python, Node.js, etc.).

---

## 🚀 Cómo arrancar el servicio

1. Asegúrate de tener configurado el archivo `.env` (puerto y contraseña `root`).
2. Levanta el contenedor con Docker Compose o Docker CLI:
   ```bash
   docker compose up -d
   # O si no tienes instalado el plugin de compose:
   docker run -d --name arangodb -p 8529:8529 -e ARANGO_ROOT_PASSWORD=rootpassword arangodb/enterprise-preview:4.0-nightly
   ```
3. Verifica el estado:
   ```bash
   docker compose ps
   ```

---

## 🧪 Cómo probar la cláusula MATCH

Hemos dejado un script en Python ([test_match.py](file:///home/javier/Escritorio/arangoDB/test_match.py)) sin dependencias externas (solo usa la librería estándar de Python):

* **En Linux / macOS:**
  ```bash
  python3 test_match.py
  ```
* **En Windows (PowerShell / CMD):**
  ```powershell
  py test_match.py
  # o bien:
  python test_match.py
  ```

El script realiza:
1. Conexión y verificación de versión (ArangoDB 4.0).
2. Creación de colecciones `users` (vértices) y `knows` (aristas).
3. Inserción de nodos y relaciones (`Alice -> Bob -> Charlie`).
4. Demostración del error si no se pasa el flag experimental.
5. Ejecución con éxito de:
   * **Matching simple:** `MATCH (u :users) RETURN u`
   * **Recorrido de 1 salto:** `MATCH (a :users) -[ e :knows ]-> (b :users) RETURN [a.name, e.rel, b.name]`
   * **Recorrido multi-hop (2 saltos):** `MATCH (a :users) -[ e1 :knows ]-> (b :users) -[ e2 :knows ]-> (c :users) RETURN { origen: a.name, intermedio: b.name, destino: c.name }`

---

## 🎬 Dataset Movies (Grafo de Películas)

Ya se ha exportado y empaquetado el grafo completo de **Movies** (en `data/movies/`) obtenido de la base de datos remota.

### 1. Importar a tu ArangoDB local

Una vez que tengas el contenedor en marcha (`docker compose up -d`), simplemente ejecuta:

```bash
python3 import_movies.py
```

Esto creará automáticamente:
* **Colecciones de Vértices:** `Movie` (38) y `Person` (133).
* **Colecciones de Aristas:** `ACTED_IN` (172), `DIRECTED` (44), `PRODUCED` (15), `WROTE` (10), `REVIEWED` (9) y `FOLLOWS` (3).
* **Named Graph:** `Movies` con todas las definiciones de aristas registradas.

### 2. Probar consultas sobre Movies

```bash
python3 test_movies.py
```

### 3. Re-exportar desde el servidor remoto (opcional)

Si deseas sincronizar nuevamente los datos desde el servidor remoto definido en `.env`:
```bash
python3 export_movies.py
```

---

## 🛑 Detener el contenedor

```bash
docker compose down
```
*(Para eliminar también los volúmenes de datos, añade la opción `-v`: `docker compose down -v`).*

