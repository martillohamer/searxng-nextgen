# Design: Memoria Semántica Local (context injection en el summarizer)

- **Fecha:** 2026-10-15
- **Proyecto:** searxng-nextgen
- **Mejora:** Fase A (Memoria Semántica Local) del salto A+B
- **Estado:** Aprobado e implementado

## Objetivo

Dotar a SearXNG-NextGen de una **memoria semántica local y privada** que
almacena pares `(consulta → resumen)` generados por el summarizer y, ante una
consulta nueva, recupera los resúmenes más similares para **inyectarlos como
contexto** en el prompt del summarizer. El objetivo es mejorar la calidad y la
coherencia de los resúmenes para consultas recurrentes o relacionadas, sin salir
del host y sin romper jamás el flujo de búsqueda.

Características clave:

- **Opt-in y privacy-by-default**: deshabilitada por defecto (`enabled: false`);
  solo embebe/sirve vectores contra servicios **locales** (`local_only`).
- **Todo local y stdlib puro**: embeddings vía servicio local (Ollama) usando
  `urllib.request`; sin `numpy`, sin `httpx`, sin dependencias nuevas.
- **Fail-safe absoluto**: cualquier error (servicio caído, timeout, SQLite,
  embedding ausente) degrada a "sin memoria"; la búsqueda continúa con
  normalidad.
- **Asíncrono y no bloqueante**: las escrituras se hacen en un hilo *daemon* con
  cola; nunca bloquean la respuesta.
- **Modular**: un único paquete nuevo `searx/memory/` + puntos de integración
  mínimos en `searx/summarizer/`.

## Alcance

**Dentro del alcance:**

- Nuevo paquete `searx/memory/` con `embedder.py`, `store.py`, `context.py` y la
  fachada `__init__.py`.
- Nueva sección de configuración `search.memory` en `searx/settings_defaults.py`.
- Inyección de contexto en `searx/summarizer/` (`base.py`, `backends.py`,
  `__init__.py`): parámetro `context` opcional + `recall` antes y `remember`
  después de generar el resumen.

**Fuera del alcance:**

- Embeddings con modelos remotos / APIs de pago.
- Búsqueda vectorial aproximada (ANN); el índice es plano y en memoria.
- Persistencia más allá del archivo SQLite local.
- Interfaz de usuario para consultar/editar la memoria (gestión manual/DB).
- Re-ranking o modificación de resultados de búsqueda (solo contexto del
  summarizer).

## Arquitectura

```
searx/summarizer/__init__.py::add_summary()
  │
  ├─ memory.recall(query) ──► embedder.embed(query) ──► POST local (Ollama)
  │        │                                          └─ local_only gate
  │        └─ store.search(vec, top_k, threshold) ──► coseno (índice en memoria)
  │                        └─ (context_block | None, query_vector | None)
  │
  ├─ _run_summarize(summarizer, results, query, timeout, context_block)
  │        └─ Summarizer.build_prompt(..., context)  # bloque de contexto
  │
  └─ memory.remember(query, summary, query_vector) ──► store.enqueue(...)
                                                             │
                                                    hilo daemon "searx-memory-writer"
                                                             │
                                                    SQLite (WAL) + purga FIFO
```

Componentes:

- **`embedder`**: traduce texto → vector `list[float]` usando un servicio local.
- **`store`**: persiste pares y vectores y resuelve la similitud del coseno.
- **`context`**: orquesta `recall`/`remember` y formatea el bloque de contexto.
- **`summarizer`**: acepta y propaga el contexto al `build_prompt`.

## Componentes e interfaces

### `searx/memory/__init__.py` (fachada)

```python
__all__ = ["is_enabled", "recall", "remember", "reset_store"]

def is_enabled() -> bool: ...
def recall(query: str) -> tuple[str | None, list[float] | None]: ...
def remember(query: str, summary: str, vector: list[float] | None = None) -> None: ...
def reset_store() -> None: ...  # utilidad para tests
```

### `searx/memory/embedder.py` (nuevo)

Embeddings con **stdlib** (`urllib.request`), reutilizando `base_url`,
`local_only` y `timeout` del summarizer, y `search.memory.embedding_model`.

```python
def embed(text: str) -> list[float] | None:
    """Vector de ``text`` o ``None`` ante cualquier fallo (fail-safe)."""
```

- `_config()`: arma `_EmbedderConfig(base_url, model, timeout, local_only)`.
- `_is_loopback()`: `local_only` rechaza hosts no locales (`127.0.0.1`,
  `localhost`, `::1`).
- `_request_spec()`: si la `base_url` contiene `/v1` usa la API OpenAI
  (`/embeddings`, campo `input`, respuesta `data[0].embedding`); en caso
  contrario usa la API de Ollama (`/api/embeddings`, campo `prompt`, respuesta
  `embedding`).
- `_post_blocking()`: POST HTTP con timeout estricto; nunca lanza.
- `_extract_vector()`: tolera ambas formas de respuesta.

### `searx/memory/store.py` (nuevo)

`VectorStore`: SQLite local + índice plano en memoria + escritor daemon.

```python
class VectorStore:
    def __init__(self, db_path: str = ..., max_rows: int = 10000) -> None: ...
    def search(self, query_vector, top_k: int, threshold: float) -> list[tuple[float, str]]: ...
    def enqueue(self, query: str, summary: str, vector) -> None: ...
    def close(self) -> None: ...

def get_store() -> VectorStore | None: ...
def reset_store() -> None: ...
```

Diseño:

- **Esquema** (tabla `memory`): `id INTEGER PK AUTOINCREMENT`, `query TEXT NOT
  NULL UNIQUE`, `summary TEXT NOT NULL`, `model TEXT`, `dim INTEGER`, `vec
  BLOB NOT NULL`, `created REAL NOT NULL`; índice sobre `created`.
- **Vectores**: `array('f')` **L2-normalizado** serializado con `.tobytes()`; la
  similitud del coseno se reduce a un **producto punto** en Python puro.
- **Índice plano en memoria**: listas `_queries`/`_summaries` + `_vectors`; se
  carga de forma perezosa (`_load`) en el primer uso.
- **Escrituras asíncronas**: `queue.Queue` + hilo daemon `searx-memory-writer`;
  `enqueue()` solo encola (nunca bloquea ni lanza).
- **Upsert**: `INSERT ... ON CONFLICT(query) DO UPDATE` (una consulta = un
  registro; se actualiza su resumen).
- **Purga FIFO**: si el índice supera `max_rows`, se eliminan los registros más
  antiguos tanto en SQLite como en el índice.
- **Concurrencia**: `threading.RLock`, conexiones *thread-local* con
  `PRAGMA journal_mode=WAL`.
- **Ruta por defecto**: `os.path.join(tempfile.gettempdir(), "searx_memory.db")`.

### `searx/memory/context.py` (nuevo)

```python
def is_enabled() -> bool: ...
def recall(query: str) -> tuple[str | None, list[float] | None]: ...
def remember(query: str, summary: str, vector: list[float] | None = None) -> None: ...
```

- `recall()`: si la memoria está deshabilitada o la consulta está vacía → `(None,
  None)`. Embebe la consulta; con el vector llama a `store.search(vec, top_k,
  threshold)`. Si hay aciertos, formatea el bloque de contexto; **devuelve
  siempre el vector** (aunque no haya aciertos) para reutilizarlo en `remember`.
- `remember()`: reutiliza el vector si se le pasa, si no embebe la consulta; solo
  guarda si hay consulta, resumen y vector. Encola la escritura.
- `_format_block()`: cabecera fija + una línea por resumen, truncando cada
  entrada a `_MAX_CONTEXT_ENTRY_CHARS` (400).

Bloque de contexto (formato):

```
Relevant context from previous similar searches (use only if it helps; ignore if unrelated):
- <resumen 1>
- <resumen 2>
```

### Integración en `searx/summarizer/`

```python
# base.py
async def summarize(self, results, query: str, context: str | None = None) -> str | None: ...
def build_prompt(self, results, query: str, context: str | None = None) -> str: ...

# backends.py (DisabledSummarizer / OfflineSummarizer)
async def summarize(self, results, query, context=None) -> ...: ...
# OfflineSummarizer pasa context a build_prompt

# __init__.py
def _run_summarize(summarizer, results, query, timeout, context=None) -> str | None: ...
# add_summary(): context_block, query_vector = memory_recall(query)
#                text = _run_summarize(..., context_block)
#                memory_remember(query, text, query_vector)
```

`build_prompt()` inserta el bloque de contexto entre la pregunta y las fuentes,
solo si `context` es no vacío (el prompt sin contexto permanece idéntico).

## Configuración

Nueva sección en `searx/settings_defaults.py` bajo `search`:

```python
'memory': {
    'enabled': SettingsValue(bool, False),
    'embedding_model': SettingsValue(str, 'nomic-embed-text'),
    'max_rows': SettingsValue(int, 10000),
    'similarity_threshold': SettingsValue(numbers.Real, 0.82),
    'top_k': SettingsValue(int, 3),
},
```

| Clave | Tipo | Defecto | Descripción |
|---|---|---|---|
| `enabled` | bool | `false` | Activa la memoria (opt-in). |
| `embedding_model` | str | `nomic-embed-text` | Modelo de embeddings local. |
| `max_rows` | int | `10000` | Tope FIFO de registros. |
| `similarity_threshold` | real | `0.82` | Similitud mínima (coseno) para inyectar. |
| `top_k` | int | `3` | Máximo de resúmenes a inyectar. |

La `base_url`/`local_only`/`timeout` del embedding se reutilizan de
`search.summarizer` (no se duplican).

## Manejo de errores y privacidad

- **Aislamiento total**: `embed()`, `recall()`, `remember()` y `store.*` envuelven
  todo en `try/except` y registran a nivel `debug`; jamás propagan a la búsqueda.
- **Gate local**: con `local_only` (por defecto) se rechazan hosts no locales
  antes de cualquier petición.
- **Sin modelo → sin memoria**: si el servicio de embeddings no responde, la
  memoria se comporta como ausente (no guarda ni inyecta).
- **Privacy-first**: los pares `(consulta, resumen)` se guardan **solo en local**
  (SQLite en el sistema de ficheros local); nada se envía a terceros.
- **No bloqueante**: las escrituras son asíncronas; en el peor caso se pierde una
  escritura, nunca una respuesta.

## Limitaciones conocidas

- **Índice en memoria y plano**: O(n) por consulta; adecuado para `max_rows`
  moderado (10.000 por defecto).
- **Un fichero por proceso/host**: la BD es local al host que ejecuta SearXNG.
- **Semántica sujeta al modelo**: la calidad depende del modelo de embeddings
  local elegido.
- **Sin gestión de UI**: la memoria se administra por configuración/DB.

## Testing / verificación

- `py_compile` y `pyright` (0 errores, 0 warnings) de `searx/memory/` y de los
  archivos modificados de `searx/summarizer/`.
- `embed()` devuelve `None` de forma fail-safe cuando no hay servicio local y
  cuando `local_only` rechaza la `base_url`.
- `_normalize()` maneja vector vacío y vector cero → `None`; normaliza
  correctamente.
- `VectorStore`: `search` devuelve aciertos por encima del umbral y ordenados;
  upsert por `query` no duplica; persistencia tras reapertura; purga FIFO
  respeta `max_rows`.
- Memoria deshabilitada por defecto: `recall()` → `(None, None)`; `remember()` no
  lanza.
- `build_prompt()` sin contexto no cambia; con contexto incluye el bloque.
- Prueba funcional reproducible (todas las comprobaciones anteriores) ejecutada
  en bucle sin *flakes*.

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Romper la búsqueda por la memoria | try/except en todos los puntos; degradación a "sin memoria". |
| Fuga de consultas a terceros | `local_only` por defecto; solo servicios locales. |
| Bloqueo por I/O de embeddings | Timeout estricto + escrituras en hilo daemon. |
| Crecimiento ilimitado de la BD | Purga FIFO por `max_rows`. |
| Corrupción/ausencia de vectores | Validación de `dim`/longitud al cargar; se ignoran filas inválidas. |
| Coste de memoria del índice | Índice plano acotado por `max_rows`. |

## Decisiones registradas

- **Opt-in global** (`enabled: false`) por instancia, con gate `local_only`.
- **Solo pares** `(consulta, resumen)` que produjeron resumen.
- **Reutilizar** `base_url`/`local_only`/`timeout` del summarizer + modelo propio
  de embeddings.
- **Tope FIFO** `max_rows` (10.000) con escritor daemon.
- **Enfoque**: SQLite + índice `array('f')` en memoria + coseno stdlib.
- **Integración** vía parámetro `context` opcional, sin alterar el prompt cuando
  no hay contexto.
