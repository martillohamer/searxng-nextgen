# Design: Grafo de Conocimiento Cross-Context (Pilar 2 · Fase C)

- **Fecha:** 2026-10-15
- **Proyecto:** searxng-nextgen
- **Fase:** C (Singularidad Cognitiva Distribuida) — Pilar 2
- **Spec maestra:** [`2026-10-15-distributed-cognitive-singularity-design.md`](./2026-10-15-distributed-cognitive-singularity-design.md)
- **Estado:** Aprobado e implementado

## Objetivo

Dotar a SearXNG-NextGen de un **grafo de conocimiento personal y local** que
extrae entidades y relaciones de cada par `(consulta, resumen)`, las persiste en
un grafo dirigido y ponderado, y las **cruza con los proyectos del usuario**
para inyectar contexto relevante en el summarizer. El valor diferencial es el
cross-context: una consulta nueva recupera no solo entidades parecidas, sino las
**relaciones** de esas entidades y los **proyectos** en los que aparecen.

Características clave:

- **Opt-in y kill-switch doble**: deshabilitado por defecto
  (`search.nextgen.enabled` **y** `search.nextgen.graph.enabled`).
- **Todo local y stdlib puro**: grafo en SQLite; extracción vía **LLM local
  (Ollama)** por `urllib.request`, con **fallback heurístico** si no hay modelo.
- **Fail-safe absoluto**: cualquier error degrada a "sin grafo" / no-op; la
  búsqueda continúa intacta.
- **Asíncrono y no bloqueante**: la extracción e ingesta corren en un hilo
  *daemon* con cola; nunca afectan a la latencia del request.
- **Modular**: paquete `searx/graph/` sobre la fundación `searx/nextgen/`.

## Alcance

**Dentro del alcance:**

- Fundación transversal `searx/nextgen/` (contrato) y sección `search.nextgen`.
- Paquete `searx/graph/` con `store.py`, `extractor.py`, `context.py` y la
  fachada `__init__.py`.
- Integración mínima en `searx/summarizer/__init__.py::add_summary()` (recall
  antes de resumir, remember después).

**Fuera del alcance:**

- Extracción con modelos remotos / APIs de pago.
- Visualización o edición de UI del grafo (gestión por configuración/DB).
- Razonamiento multi-salto (el contexto son 1-hop; la potenciación queda para
  futuras iteraciones).
- Resolución de entidades con desambiguación externa (el id es por
  `(kind, key)` normalizado).

## Arquitectura

```
searx/summarizer/__init__.py::add_summary()
  │
  ├─ knowledge_graph.recall(query)
  │       └─ heuristic_entities(query)               # candidatos rápidos (sin red)
  │       └─ store.find_entities(names)               # SQLite: id, name
  │       └─ store.neighbors(entity_ids)              # 1-hop (src y dst)
  │       └─ store.projects_for_entities(entity_ids)  # proyectos vinculados
  │       └─ render bloque de contexto ──► graph_block
  │
  └─ knowledge_graph.remember(query, summary)
          └─ _queue.put((query, summary))  ──► hilo daemon "searx-graph-worker"
                                                    │
                                    extractor.extract_triples(query, summary)
                                       ├─ LLM local (Ollama /api/generate)
                                       └─ fallback heuristic_triples()
                                                    │
                                    store.upsert_triples(triples)
                                    _autolink_projects(...)  # entidad → proyecto
```

Componentes:

- **`store`**: persiste entidades/relaciones/proyectos y resuelve lecturas.
- **`extractor`**: traduce `(query, summary)` → tripletas.
- **`context`**: orquesta `recall`/`remember`, el worker y el autolink.
- **`nextgen` (contrato)**: kill-switch, normalización e identificadores.

## Componentes e interfaces

### `searx/graph/__init__.py` (fachada)

```python
__all__ = ["is_enabled", "recall", "remember", "add_project", "reset",
           "reset_store", "Triple"]

def is_enabled() -> bool: ...
def recall(query: str) -> str | None: ...
def remember(query: str, summary: str) -> None: ...
def add_project(name: str, description: str = "", entities=()) -> bool: ...
def reset() -> None: ...        # utilidad para tests (para el worker)
def reset_store() -> None: ...  # utilidad para tests (libera el singleton)
```

`extract_triples`, `heuristic_triples` y `heuristic_entities` viven en
`searx.graph.extractor` (no se reexportan en la fachada).

### `searx/graph/store.py` (nuevo)

`GraphStore`: SQLite local + conexiones *thread-local*.

```python
class GraphStore:
    def __init__(self, db_path: str = ..., max_rows: int = 20000) -> None: ...
    def upsert_entity(self, name: str, kind: str = "") -> str | None: ...
    def upsert_triples(self, triples, source_query: str = "") -> int: ...
    def add_project(self, name: str, description: str = "") -> str | None: ...
    def link_project_entity(self, project: str, entity: str, weight: float = 1.0) -> bool: ...
    def find_entities(self, keys, limit: int = 10) -> list[tuple[str, str]]: ...
    def neighbors(self, entity_ids, limit: int = 8) -> list[tuple[str, str, float]]: ...
    def projects_for_entities(self, entity_ids, limit: int = 5) -> list[tuple[str, str, float]]: ...
    def list_projects(self, limit: int = 50) -> list[tuple[str, str]]: ...
    def count(self) -> tuple[int, int, int]: ...
    def close(self) -> None: ...

def get_store() -> GraphStore | None: ...
def reset_store() -> None: ...
```

Diseño:

- **Esquema:**
  - `entity(id PK, key, name, kind, created)` + índice `entity_key_idx`.
  - `relation(id PK, src_id, rel, dst_id, weight, created)` + índices
    `relation_src_idx`, `relation_dst_idx`.
  - `project(id PK, name, description, created)`.
  - `project_entity(project_id, entity_id, weight, created, PK(project_id, entity_id))`.
- **Refuerzo:** el upsert de relación hace `weight = weight + 1.0` (frecuencia);
  el vínculo proyecto↔entidad acumula `weight`.
- **Normalización:** `_clean()` colapsa espacios y limita longitud
  (`_KEY_MAX_LEN=120`); `normalize_key()` da la clave comparable.
- **Auto-aristas:** `upsert_triples` rechaza auto-relaciones (`src_key == dst_key`).
- **Purga FIFO:** `_SQL_PURGE_RELATIONS` conserva las `max_rows` relaciones más
  recientes.
- **Concurrencia:** `threading.RLock` + conexiones *thread-local* con
  `PRAGMA journal_mode=WAL`, `timeout=3.0`.
- **Ruta por defecto:** `os.path.join(tempfile.gettempdir(), "searx_graph.db")`.
- **Singleton:** `get_store()` lee `search.nextgen.graph.max_rows` en el primer uso.

### `searx/graph/extractor.py` (nuevo)

```python
class Triple(t.NamedTuple):
    source: str
    relation: str
    target: str

def extract_triples(query: str, summary: str) -> list[Triple]: ...      # LLM → heurístico
def heuristic_triples(query: str, summary: str, max_triples: int) -> list[Triple]: ...
def heuristic_entities(text: str, max_entities: int = 12) -> list[str]: ...
```

- `_config()`: reutiliza `base_url`/`local_only`/`timeout` del summarizer y usa
  `search.nextgen.graph.extract_model` (vacío → hereda `summarizer.model`,
  defecto `llama3`).
- `_is_loopback()`: `local_only` rechaza hosts no locales (`127.0.0.1`,
  `localhost`, `::1`).
- `_post_blocking()`: POST a Ollama `/api/generate` con `format: "json"` y
  timeout estricto (`_DEFAULT_TIMEOUT=8.0`); nunca lanza.
- `_parse_llm_response()` / `_llm_triples()`: tolera respuestas malformadas.
- **Fallback heurístico** (`heuristica_triples`): extrae sujeto→relación→objeto
  con regexes de frases patrón (`_PHRASE`) y secuencias capitalizadas
  (`_CAP_SEQ`), filtra `_STOPWORDS`, deduplica y usa `_RELATION_FALLBACK="related_to"`
  cuando no infiere relación. `_MAX_TEXT_CHARS=2000`, `_DEFAULT_MAX_TRIPLES=20`.
- **Regexes:** `_CAP_SEQ`/`_PHRASE` usan `[ \t]+` (no cruzan saltos de línea) y
  recortan puntuación final (`_TRAILING_PUNCT = ".,;:!?"`).

### `searx/graph/context.py` (nuevo)

```python
def is_enabled() -> bool: ...
def recall(query: str) -> str | None: ...
def remember(query: str, summary: str) -> None: ...
def add_project(name: str, description: str = "", entities=()) -> bool: ...
def reset() -> None: ...
```

- `recall()`: gate `is_enabled()`; extrae entidades candidatas con
  `heuristic_entities` (sin red); busca ids; reúne 1-hop
  (`neighbors`, límite `context_relations`) y proyectos
  (`projects_for_entities`); renderiza el bloque. Si no hay nada → `None`.
- `remember()`: gate `is_enabled()`; encola `(query, summary)` y asegura el
  worker. Nunca bloquea.
- `_worker_loop()`: obtiene `get_store()` por iteración → `extract_triples` →
  `upsert_triples` → `_autolink_projects`.
- `_autolink_projects()`: vincula entidades extraídas a proyectos cuyo
  nombre/descripción (normalizados) las contiene.
- `add_project()`: crea el proyecto y enlaza las entidades indicadas.

Bloque de contexto (formato):

```
Known context from your knowledge graph (use only if it helps; ignore if unrelated):
- <Entidad 1>
- <Entidad 2>
  · <relación>: <otra entidad>
  · <relación>: <otra entidad>
Related projects:
  · <Proyecto>
```

### Integración en `searx/summarizer/__init__.py`

```python
from searx import graph as knowledge_graph
...
memory_block, query_vector = memory_recall(search_query.query)
graph_block = knowledge_graph.recall(search_query.query)
context_block = "\n\n".join(p for p in (memory_block, graph_block) if p) or None
text = _run_summarize(summarizer, results, query, config.timeout, context_block)
...
memory_remember(query, text, query_vector)
knowledge_graph.remember(query, text)
```

La memoria (Fase A) y el grafo (Fase C) co-inyectan contexto de forma
independiente; si uno está vacío, el otro se inyecta igual.

## Configuración

En `searx/settings_defaults.py`, bajo `search.nextgen`:

| Clave | Tipo | Defecto | Descripción |
|---|---|---|---|
| `enabled` | bool | `false` | Interruptor global de la Fase C. |
| `graph.enabled` | bool | `false` | Interruptor del grafo. |
| `graph.extract_model` | str | `''` | Modelo LLM local (vacío → hereda summarizer). |
| `graph.max_triples` | int | `20` | Máximo de tripletas por extracción. |
| `graph.max_rows` | int | `20000` | Tope FIFO de relaciones. |
| `graph.context_entities` | int | `5` | Máximo de entidades en el bloque. |
| `graph.context_relations` | int | `6` | Máximo de relaciones en el bloque. |

`base_url`/`local_only`/`timeout` de la extracción se reutilizan de
`search.summarizer` (no se duplican).

## Manejo de errores y privacidad

- **Aislamiento total**: `recall()`, `remember()`, `add_project()` y todos los
  métodos del store envuelven en `try/except`; log a nivel `debug`; jamás
  propagan a la búsqueda.
- **Gate local**: con `local_only` se rechazan hosts no locales antes de
  cualquier petición de red.
- **Sin modelo → heurístico**: si el LLM local no responde en el timeout, la
  extracción cae al fallback heurístico (siempre local, sin red).
- **Privacy-first**: entidades, relaciones y proyectos viven solo en SQLite
  local; nada se envía a terceros.
- **No bloqueante**: la extracción ocurre en el hilo daemon; en el peor caso se
  pierde una ingesta, nunca una respuesta.

## Limitaciones conocidas

- **Contexto 1-hop**: no hay razonamiento multi-salto todavía.
- **Extracción dependiente del modelo**: la calidad del LLM local condiciona las
  relaciones; el fallback heurístico es conservador.
- **Latencia de extracción**: cada item puede tardar hasta el timeout del LLM
  (~5–8 s) en segundo plano; es un hilo daemon, no afecta al request.
- **Un fichero por proceso/host**: la BD es local al host.
- **Sin gestión de UI**: grafo y proyectos se administran por configuración/DB.

## Testing / verificación

- `py_compile` (exit 0) y `pyright` (0 errores, 0 warnings) de `searx/nextgen/`,
  `searx/graph/` y los archivos modificados de `searx/summarizer/`.
- `contract`: `is_enabled` respeta el doble kill-switch y rechaza pilares
  desconocidos; `node_id`/`edge_id` son estables; `normalize_key` colapsa.
- `store`: `upsert_triples` escribe y refuerza `weight`; `neighbors` devuelve
  1-hop; proyectos se crean/enlazan; purga FIFO respeta `max_rows`.
- `extractor`: `heuristic_entities("Intro to Rust\nRust emphasizes safety")` →
  `['Intro', 'Rust']`; `heuristic_triples` produce tripletas con relación de
  fallback; `extract_triples` cae al heurístico sin servicio local.
- `context`: kill-switch apagado → `recall()` `None` y `remember()` no-op;
  con grafo poblado, `recall()` devuelve el bloque; el worker ingesta en
  segundo plano (test con `extract_triples` parcheado a heurístico para evitar
  red y *flakes*).
- Prueba funcional `graph_check.py` (6/6 OK), ejecutada en bucle sin *flakes*.

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Romper la búsqueda por el grafo | try/except en todos los puntos; degradación a "sin grafo". |
| Fuga de consultas a terceros | `local_only` + LLM local; fallback heurístico local. |
| Bloqueo por I/O del LLM | Timeout estricto + extracción en hilo daemon. |
| Crecimiento ilimitado de la BD | Purga FIFO por `max_rows`. |
| Falsos enlaces proyecto↔entidad | Solo coincidencia por clave normalizada; autolink best-effort. |
| Ruido en el contexto | Topes `context_entities`/`context_relations` + encabezado "ignore if unrelated". |

## Decisiones registradas

- **Estructura:** paquete `searx/graph/` sobre la fundación `searx/nextgen/`.
- **Extracción:** LLM local (Ollama) **+ fallback heurístico**; `local_only`.
- **Proyectos:** **híbridos** — explícitos (`add_project`) + autolink por
  coincidencia de clave con clusters promovibles.
- **Contexto:** 1-hop, con tope de entidades y relaciones.
- **Integración:** co-inyección con la memoria (Fase A) sin acoplamiento; el
  grafo es independiente y opcional.
- **Kill-switch:** jerárquico (`nextgen.enabled` **y** `graph.enabled`).
