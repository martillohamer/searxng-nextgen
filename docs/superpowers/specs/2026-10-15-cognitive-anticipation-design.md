# Design: Precarga Neuronal Predictiva — Caché de Anticipación (Pilar 3 · Fase C)

- **Fecha:** 2026-10-15
- **Proyecto:** searxng-nextgen
- **Fase:** C (Singularidad Cognitiva Distribuida) — Pilar 3
- **Spec maestra:** [`2026-10-15-distributed-cognitive-singularity-design.md`](./2026-10-15-distributed-cognitive-singularity-design.md)
- **Estado:** Aprobado e implementado

## Objetivo

Dotar a SearXNG-NextGen de una **caché neural de anticipación**: aprender de la
historia local de consultas cuáles tienden a sucederse y **precomputar en
segundo plano** el contexto que la próxima búsqueda necesitará, de modo que el
siguiente *request* se resuelva de forma prácticamente instantánea.

El valor diferencial no es memorizar respuestas, sino **anticipar el trabajo
costoso**: el *embedding* de la consulta y el bloque de contexto local
(memoria + grafo) se calculan antes de que el usuario los pida. Cuando la
predicción acierta, el summarizer se salta las llamadas a `memory_recall()` y
`knowledge_graph.recall()`.

Características clave:

- **Opt-in y kill-switch doble**: deshabilitado por defecto
  (`search.nextgen.enabled` **y** `search.nextgen.anticipate.enabled`).
- **Todo local y stdlib puro**: transiciones y artefactos en SQLite; **nunca**
  se consultan motores de búsqueda externos para precalentar.
- **Fail-safe absoluto**: cualquier error degrada a *miss* / no-op; la búsqueda
  continúa intacta.
- **Asíncrono y no bloqueante**: el aprendizaje y el *warm* corren en un hilo
  *daemon* con cola; nunca afectan a la latencia del request.
- **Modular y desacoplado**: el pilar **no importa** `searx.memory` ni
  `searx.graph`; recibe el cálculo de contexto por **inyección de
  dependencias** (*warmer*), respetando la regla "un pilar nunca importa a otro
  pilar".

## Alcance

**Dentro del alcance:**

- Paquete `searx/anticipate/` con `store.py`, `predictor.py` y la fachada
  `__init__.py` sobre la fundación `searx/nextgen/`.
- Predictor Markov de primer orden sobre transiciones de consultas
  normalizadas.
- Almacén SQLite con artefactos precomputados, TTL y purga FIFO.
- Integración en `searx/summarizer/__init__.py::add_summary()` (recall antes de
  resumir, observe después) mediante el *warmer* inyectado.

**Fuera del alcance:**

- Modelos de anticipación (RNN/transformer) o de n-gramas de orden superior;
  el pilar usa transiciones de primer orden.
- Preconsulta a motores de búsqueda externos (violación de privacidad).
- Persistencia del estado de sesión entre procesos/reinicios.
- Gestión o visualización del caché por UI.

## Arquitectura

```
searx/summarizer/__init__.py::add_summary()
  │
  ├─ anticipation.recall(query)
  │       └─ store.get_artifact(query)          # hit dentro del TTL
  │              ├─ HIT  → (embedding, context)  ─┐
  │              └─ MISS → (None)                 │
  │                                               ▼
  │       (miss) memory_recall(query) + knowledge_graph.recall(query)
  │                                               │
  └─ _run_summarize(..., context_block) ◄─────────┘
          │
          ├─ memory_remember(query, text, vector)
          ├─ knowledge_graph.remember(query, text)
          └─ anticipation.observe(query)
                  ├─ set_warmer(_warm_context)   # DI: el summarizer sabe de memory+graph
                  ├─ predictor.advance(query)    # (prev, current) si dentro de la ventana
                  └─ _queue.put((prev, current)) ► hilo daemon "searx-anticipate-worker"
                                                        │
                                        record_transition(prev, current)
                                        predictions_for(current)   # top-k
                                        _warm(predicted, store)    # warmer(query)
                                        store.purge()              # TTL + FIFO
```

Componentes:

- **`store`**: persiste transiciones y artefactos; resuelve lecturas con TTL.
- **`predictor`**: mantiene la sesión en memoria y aprende/predice transiciones.
- **`__init__` (fachada)**: orquesta `observe`/`recall`, el worker y el *warmer*.
- **`nextgen` (contrato)**: kill-switch, normalización de claves.

### Flujo de datos

1. **En el request**: `add_summary` intenta `anticipation.recall(query)`.
   - **Hit**: usa el `(embedding, contexto)` precomputado y se salta
     `memory_recall()` + `knowledge_graph.recall()`.
   - **Miss**: ejecuta el camino actual (memoria + grafo) y obtiene el
     `query_vector` y el `context_block`.
2. **Tras el éxito**: se persisten el resumen (`memory_remember`) y las
   tripletas (`knowledge_graph.remember`), y se llama a
   `anticipation.observe(query)` con el *warmer* ya registrado.
3. **En segundo plano**: el worker registra la transición `prev → current`
   (si `prev` existe y está dentro de `session_window`), predice las
   `top_k_predictions` consultas siguientes, las precalienta con el *warmer* y
   purga.

## Componentes e interfaces

### `searx/anticipate/__init__.py` (fachada)

```python
__all__ = ["is_enabled", "set_warmer", "observe", "store_artifact", "recall",
           "reset", "reset_store"]

WarmerResult = tuple[list[float], str | None]     # (embedding, context_block)
Warmer = Callable[[str], WarmerResult | None]

def is_enabled() -> bool: ...
def set_warmer(warmer: Warmer | None) -> None: ...
def observe(query: str) -> None: ...
def store_artifact(query: str, vector, context: str | None = None) -> bool: ...
def recall(query: str) -> WarmerResult | None: ...
def reset() -> None: ...        # utilidad para tests (para el worker)
def reset_store() -> None: ...  # utilidad para tests (libera el singleton)
```

- `is_enabled()` delega en `nextgen.is_enabled("anticipate")` (doble
  kill-switch).
- `set_warmer()` registra el callback con el que se precalcula el contexto; es
  el único punto de acoplamiento con memoria/grafo y se resuelve **por
  inyección**, no por import.
- `observe()` es no bloqueante: normaliza la consulta, avanza la sesión y
  encola `(prev, current)` para el worker.
- `recall()` devuelve el artefacto precomputado o `None` (miss / deshabilitado).

### `searx/anticipate/store.py` (nuevo)

`AnticipateStore`: SQLite local + conexiones *thread-local*.

```python
class AnticipateStore:
    def __init__(self, db_path: str = ..., max_rows: int = 5000, ttl: int = 1800) -> None: ...
    def record_transition(self, src: str, dst: str) -> bool: ...
    def predict(self, src: str, limit: int = 2) -> list[str]: ...
    def store_artifact(self, query: str, vector, context: str = "", model: str = "") -> bool: ...
    def get_artifact(self, query: str) -> tuple[list[float], str] | None: ...
    def purge(self) -> None: ...
    def count(self) -> tuple[int, int]: ...   # (transiciones, artefactos)
    def close(self) -> None: ...

def get_store() -> AnticipateStore | None: ...
def reset_store() -> None: ...
```

Diseño:

- **Esquema:**
  - `transition(src, dst, weight, updated, PK(src, dst))` + índices
    `transition_src_idx`, `transition_updated_idx`.
  - `artifact(query PK, model, dim, vec, context, created)` + índice
    `artifact_created_idx`.
- **Refuerzo:** el upsert de transición hace `weight = weight + 1.0`
  (`_SQL_UPSERT_TRANSITION`); las auto-transiciones (`src == dst`) se rechazan.
- **Clave de consulta:** `searx.nextgen.normalize_key()` (`_QUERY_MAX_LEN=200`),
  estable y libre de PII; un mismo artefacto se recupera con consultas
  equivalentes en mayúsculas/espacios.
- **Vectores:** se serializan como `array('f')` (float32) con
  `_to_blob`/`_from_blob`; se guardan **fieles** (sin normalización L2: el
  consumidor espera el embedding exacto).
- **TTL:** `get_artifact` trata un artefacto con `created < now - ttl` como
  *miss*; `purge` lo borra.
- **Purga FIFO:** `_SQL_PURGE_ARTIFACT_FIFO` y `_SQL_PURGE_TRANSITION_FIFO`
  conservan las `max_rows` filas más recientes.
- **Concurrencia:** `threading.RLock` + conexiones *thread-local* con
  `PRAGMA journal_mode=WAL`, `timeout=3.0`.
- **Ruta por defecto:** `os.path.join(tempfile.gettempdir(), "searx_anticipate.db")`.
- **Singleton:** `get_store()` lee `search.nextgen.anticipate.max_rows` y
  `.ttl` en el primer uso.

### `searx/anticipate/predictor.py` (nuevo)

Predictor Markov de primer orden; **stateless en disco** (solo la sesión vive en
memoria del proceso).

```python
def session_window() -> float: ...          # search.nextgen.anticipate.session_window
def top_k_predictions() -> int: ...         # search.nextgen.anticipate.top_k_predictions
def advance(query: str) -> tuple[str | None, str]: ...   # (prev, current)
def record_transition(prev: str, current: str) -> bool: ...
def predictions_for(query: str, limit: int | None = None) -> list[str]: ...
def reset() -> None: ...
```

- **Estado de sesión:** `_last_query`/`_last_ts` protegidos por `_lock`.
- **`advance()`:** normaliza, y devuelve `prev` solo si la consulta anterior se
  vio dentro de `session_window` (medido con `time.monotonic()`) y difiere de
  `current`; actualiza el estado.
- **`record_transition`/`predictions_for`:** envuelven el store; fail-safe.

### Integración en `searx/summarizer/__init__.py`

```python
from searx import anticipate as anticipation

def _warm_context(query: str) -> tuple[list[float], str | None] | None:
    """Warmer (DI): combina memoria + grafo en un artefacto local."""
    memory_block, vector = memory_recall(query)
    if not vector:
        return None
    graph_block = knowledge_graph.recall(query)
    context_block = "\n\n".join(p for p in (memory_block, graph_block) if p) or None
    return vector, context_block

...
artifact = anticipation.recall(query)
if artifact is not None:
    query_vector, context_block = artifact          # HIT: se saltan memoria+grafo
else:
    memory_block, query_vector = memory_recall(query)
    graph_block = knowledge_graph.recall(query)
    context_block = "\n\n".join(p for p in (memory_block, graph_block) if p) or None

text = _run_summarize(summarizer, results, query, config.timeout, context_block)
...
memory_remember(query, text, query_vector)
knowledge_graph.remember(query, text)
anticipation.set_warmer(_warm_context)
anticipation.observe(query)
```

El summarizer es la **capa de integración legítima**: ya conoce
`searx.memory` y `searx.graph`, así que construye el *warmer* y lo inyecta en el
pilar. El pilar permanece sin dependencias hacia ellos.

## Configuración

En `searx/settings_defaults.py`, bajo `search.nextgen.anticipate`:

| Clave | Tipo | Defecto | Descripción |
|---|---|---|---|
| `enabled` | bool | `false` | Interruptor del pilar (junto a `nextgen.enabled`). |
| `session_window` | int | `900` | Ventana (s) para encadenar dos consultas como transición. |
| `max_rows` | int | `5000` | Tope FIFO de transiciones y artefactos. |
| `ttl` | int | `1800` | Vida (s) de un artefacto precomputado antes de considerarse *miss*. |
| `top_k_predictions` | int | `2` | Nº de consultas siguientes a predecir y precalentar. |

Los modelos de embedding y los parámetros de red se reutilizan de
`search.summarizer` y `search.memory` (no se duplican).

## Manejo de errores y privacidad

- **Aislamiento total**: cada método público del store y de la fachada envuelve
  en `try/except`; log a nivel `debug`; jamás propagan a la búsqueda.
- **Sin red**: el pilar nunca contacta motores de búsqueda ni servicios
  externos; solo lee/escribe SQLite local y ejecuta el *warmer* inyectado.
- **Privacy-first**: las claves de consulta son normalizadas; transiciones y
  artefactos viven solo en SQLite local.
- **No bloqueante**: el aprendizaje y el *warm* ocurren en el hilo daemon; en el
  peor caso se pierde un *warm*, nunca una respuesta.
- **Warmer ausente**: si no hay embedding disponible (`_warm_context` → `None`)
  o el *warmer* no está registrado, simplemente no se persiste artefacto.

## Limitaciones conocidas

- **Markov de primer orden**: la predicción ignora contexto más allá de la
  consulta inmediatamente anterior.
- **Sesión por proceso**: el estado `prev`/`ts` no se comparte entre workers ni
  sobrevive a reinicios.
- **Artefacto dependiente de la memoria**: sin *embedding* local disponible no
  hay artefacto que precargar (el pilar queda inerte, sin afectar la búsqueda).
- **Aciertos dependen de la repetición**: secuencias nuevas raramente aciertan
  hasta acumular transiciones.
- **Un fichero por proceso/host**: la BD es local al host.

## Testing / verificación

- `py_compile` (exit 0) y `pyright` (0 errores, 0 warnings) de `searx/anticipate/`
  y de los archivos modificados (`searx/summarizer/__init__.py`,
  `searx/settings_defaults.py`).
- `store`: `record_transition` escribe y refuerza `weight`; `predict` ordena por
  peso; auto-transición rechazada; `store_artifact`/`get_artifact` round-trip
  (float32) y recuperación por consulta normalizada; artefacto expirado por TTL
  → *miss*; `purge` limpia.
- `predictor`: `advance` devuelve `(prev, current)` dentro de la ventana; la
  consulta en blanco se ignora.
- `fachada`: kill-switch apagado → `recall()` `None`, `observe()`/`store_artifact`
  no-op; con el pilar activo, el worker ingesta la transición y precalienta los
  artefactos predichos (test con *warmer* sintético para evitar red y *flakes*).
- Prueba funcional `anticipate_check.py` (5/5 OK): kill-switch, store, TTL,
  sesión y fachada end-to-end con worker.

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Romper la búsqueda por el caché | try/except en todos los puntos; degradación a *miss*/no-op. |
| Fuga de consultas a terceros | El pilar no usa red; el *warmer* es local (memoria/grafo locales). |
| Precalentar consultas inútiles | Tope `top_k_predictions` + predicción por peso observado. |
| Crecimiento ilimitado de la BD | TTL de artefactos + purga FIFO por `max_rows`. |
| Artefacto obsoleto (contexto rancio) | TTL (`ttl`) fuerza re-cálculo tras expirar. |
| Bloqueo por I/O de SQLite | Conexiones *thread-local*, WAL, `timeout=3.0`, en hilo daemon. |
| Acoplamiento pilar→pilar | Contexto por **DI** (*warmer*); el pilar no importa memoria/grafo. |

## Decisiones registradas

- **Señal de aprendizaje:** historial de **transiciones de consulta**
  (Markov de primer orden desde el log local).
- **Artefacto precomputado:** **embedding + bloque de contexto combinado**
  (memoria + grafo).
- **Disparador:** **en segundo plano** tras cada búsqueda (hilo daemon; nunca
  bloquea el request).
- **Almacén:** paquete nuevo `searx/anticipate/` con SQLite propio.
- **Enfoque de integración:** **predictor + caché genérica** (Approach A) — el
  pilar expone predicción y caché, y recibe el cálculo de contexto por *warmer*
  inyectado; no importa memoria ni grafo.
- **Kill-switch:** jerárquico (`nextgen.enabled` **y** `anticipate.enabled`).
- **Privacidad:** prohibido preconsultar motores externos; solo precalentado
  local.
