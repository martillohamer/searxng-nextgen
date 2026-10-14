# Design: Observabilidad NextGen (métricas de caché, summarizer y post-procesado)

- **Fecha:** 2026-10-14
- **Proyecto:** searxng-nextgen
- **Mejora:** Fase B (Observabilidad) del salto A+B
- **Estado:** Aprobado (diseño)

## Objetivo

Dotar a SearXNG-NextGen de métricas de observabilidad para las capacidades
NextGen (caché de resultados, summarizer AI y pipeline de post-procesado),
**integrándose en el subsistema `searx.metrics` existente** en lugar de crear uno
paralelo. El objetivo es medir en tiempo real el impacto de las mejoras (p. ej.
la tasa de aciertos de caché y el coste del summarizer) y exponerlas en la página
`/stats` ya existente, manteniendo estrictamente el principio privacy-by-default.

Características clave:

- **Integración, no reinvención**: reutiliza `CounterStorage`/`HistogramStorage`
  (thread-safe, en memoria, per-proceso) y su modo *Void* (fail-safe).
- **Solo agregados**: nunca se registra texto de consulta, URLs ni contenido.
- **Fail-safe**: toda la instrumentación se aísla; si las métricas están
  desactivadas, los storages *Void* hacen no-op.
- **Privacy-first**: sin endpoint nuevo; se muestra en `/stats` (ya existente) y
  en la cabecera `Server-Timing`.
- **Modular**: un único módulo nuevo `searx/metrics/nextgen.py` + puntos de
  instrumentación mínimos.

## Alcance

**Dentro del alcance:**

- Nuevo módulo `searx/metrics/nextgen.py` (`configure()` y `get_nextgen_stats()`).
- Registro de contadores/histogramas NextGen desde `searx.metrics.initialize()`.
- Instrumentación de `searx/resultcache.py`, `searx/summarizer/__init__.py` y
  `searx/search/postprocess/pipeline.py`.
- Panel "NextGen" en `searx/templates/simple/stats.html` y paso de datos desde
  `searx/webapp.py::stats()`.

**Fuera del alcance:**

- Endpoint nuevo de métricas (ya existe `/metrics`, gated, y no se modifica).
- Agregación cross-worker / persistencia (limitación existente de
  `searx.metrics`: el estado es per-proceso).
- Métricas de la futura memoria semántica (Fase A); se instrumentarán cuando
  exista A.
- Alarmas, exportadores o dashboards externos.

## Arquitectura e integración

`searx.metrics` es el subsistema en uso:

- `histogram_storage` / `counter_storage`: instancias globales del módulo.
- `initialize(engine_names, enabled)` las crea: con `enabled=True` usa
  `HistogramStorage`/`CounterStorage`; con `enabled=False` usa `VoidHistogram` /
  `VoidCounterStorage` (no-op).
- **Restricción crítica**: `CounterStorage.get()/add()` acceden a
  `self.counters[args]` y lanzan `KeyError` si la clave no se **configuró** antes.
  Por tanto, `configure()` debe registrar todas las claves NextGen dentro de
  `initialize()`.

El módulo NextGen no crea almacenamiento propio: usa las funciones públicas
`counter_inc`, `counter_add`, `counter`, `histogram_observe` y `histogram`, y
`configure()` accede a los storages del paquete hermano para registrarse.

```
searx.metrics.initialize()
  ├─ (existente) configura métricas de motores
  └─ (nuevo)     nextgen.configure()   # registra claves NextGen

searx.resultcache / summarizer / postprocess.pipeline
  └─ nextgen instrumentation  ─► counter_inc / histogram_observe

webapp.stats()  ─►  nextgen.get_nextgen_stats()  ─►  stats.html (panel NextGen)
```

## Componentes e interfaces

### `searx/metrics/nextgen.py` (nuevo)

```python
def configure() -> None:
    """Registra los contadores e histogramas NextGen.

    Se invoca desde searx.metrics.initialize(). Idempotente y fail-safe."""

def get_nextgen_stats() -> dict[str, t.Any]:
    """Agrega las métricas NextGen en un dict apto para la plantilla.

    Defensivo: ante claves ausentes o storages no inicializados devuelve
    ceros/None en lugar de lanzar."""
```

Instrumentación expuesta por el módulo (helpers tipados que envuelven
`counter_inc`/`histogram_observe` en `try/except`):

```python
def cache_hit() -> None: ...
def cache_miss() -> None: ...
def cache_lookup_time(seconds: float) -> None: ...
def summarizer_run() -> None: ...
def summarizer_ok(duration: float) -> None: ...
def summarizer_fail(duration: float) -> None: ...
def summarizer_timeout(duration: float) -> None: ...
def postprocess_removed(count: int) -> None: ...
```

### Métricas registradas

| Clave | Tipo | Descripción |
|---|---|---|
| `nextgen`, `cache`, `hit` | counter | Aciertos de caché. |
| `nextgen`, `cache`, `miss` | counter | Fallos de caché (búsqueda real). |
| `nextgen`, `cache`, `lookup_time` | histogram | Tiempo de `load_into` (segundos). |
| `nextgen`, `summarizer`, `run` | counter | Invocaciones del summarizer. |
| `nextgen`, `summarizer`, `ok` | counter | Resúmenes generados. |
| `nextgen`, `summarizer`, `fail` | counter | Fallos/`None`. |
| `nextgen`, `summarizer`, `timeout` | counter | Timeouts (hilo vivo). |
| `nextgen`, `summarizer`, `duration` | histogram | Duración del summarizer (segundos). |
| `nextgen`, `postprocess`, `removed` | counter | Resultados eliminados (dedupe/filtro). |

Anchura/resolución de histogramas: ancho 0.05 s con tamaño acotado (p. ej. 600
buckets = 30 s) para lookup/cache; ancho 0.5 s para duración del summarizer.

### `get_nextgen_stats()` (salida)

```python
{
  "cache": {"hit": int, "miss": int, "hit_ratio": float | None,
            "lookup_avg_ms": float | None, "lookup_p95_ms": float | None},
  "summarizer": {"run": int, "ok": int, "fail": int, "timeout": int,
                 "duration_avg_s": float | None, "duration_p95_s": float | None},
  "postprocess": {"removed": int},
}
```

`hit_ratio = hit / (hit + miss)` si `(hit + miss) > 0`, si no `None`.

## Puntos de instrumentación

| Archivo | Punto | Llamada |
|---|---|---|
| `searx/resultcache.py::load_into` | tras decidir hit/miss y medir `started` | `cache_hit()` + `cache_lookup_time(dt)` o `cache_miss()` |
| `searx/summarizer/__init__.py::add_summary` | alrededor de `_run_summarize` | `summarizer_run()`, y `summarizer_ok/fail/timeout(dt)` según resultado |
| `searx/search/postprocess/pipeline.py::process_container` | tras aplicar processors | `postprocess_removed(total_removed)` |

Notas:

- En caché se distingue *hit* de *miss* en `load_into`: se registra *miss*
  cuando la caché está activa pero `load()` devuelve `None`. Si la caché está
  desactivada (`NullResultCache`), **no** se registra ni hit ni miss, para no
  sesgar el `hit_ratio`.
- `process_container` ya suma los resultados eliminados por processor; se agrega
  ese total.
- Toda llamada va envuelta por los helpers (try/except); el pipeline de búsqueda
  nunca se rompe por métricas.

## UI: panel en `/stats`

En `searx/webapp.py::stats()`, añadir al `render(...)`:

```python
nextgen_stats = get_nextgen_stats()
```

En `searx/templates/simple/stats.html`, un bloque **"NextGen"** (reutilizando el
marcado de tarjetas/estadísticas de la propia página) con:

- Caché: hits, misses y **hit-ratio** (porcentaje) + lookup avg/p95 (ms).
- Summarizer: runs, ok/fail/timeout + duración avg/p95 (s).
- Post-procesado: total de resultados eliminados.

El bloque se renderiza siempre que la página `/stats` esté accesible; los valores
son agregados y pueden mostrar `-` cuando no haya datos.

## Server-Timing

Sin cambios obligatorios: el `Timing("cache", …)` ya añadido en `load_into`
fluye a la cabecera `Server-Timing` a través de `result_container.get_timings()`.
Opcional (fuera de este incremento): añadir `Timing("summary", …)`.

## Configuración

- Se reutiliza `general.enable_metrics` de SearXNG para habilitar/deshabilitar el
  cómputo de métricas (controla los storages *Void*).
- Opcional `search.nextgen_stats.enabled` (por defecto `true`) solo para ocultar
  el panel en `/stats` sin tocar la instrumentación.

## Manejo de errores y privacidad

- **Aislamiento total**: cada helper envuelve el acceso a métricas en
  `try/except` y registra a nivel `debug`; jamás propaga.
- **Defensivo en lectura**: `get_nextgen_stats()` tolera storages no inicializados
  o claves ausentes (devuelve 0/None).
- **Privacy-first**: únicamente contadores y duraciones agregadas; **sin** texto
  de consulta, URLs, cabeceras ni identificadores.
- **Sin endpoint nuevo**: se muestra en `/stats` (existente) y `Server-Timing`.

## Limitaciones conocidas

- **Per-proceso**: con varios workers de SearXNG, cada proceso mantiene sus
  propios contadores; `/stats` refleja el worker que atiende la petición. Es una
  limitación heredada de `searx.metrics` y se documenta aquí.
- **Volátil**: los datos se reinician al reiniciar el proceso.

## Testing / verificación

- `configure()` idempotente y no lanza; registra todas las claves declaradas.
- Con `initialize(enabled=True)`: `cache_hit()`/`cache_miss()` incrementan los
  contadores; `hit_ratio` correcto.
- `get_nextgen_stats()` con storages vacíos/sin inicializar devuelve 0/None (no
  lanza).
- Instrumentación integrada: una búsqueda con acierto de caché incrementa
  `hit`; una con fallo incrementa `miss`; el summarizer incrementa
  `run`/`ok|fail|timeout`.
- `postprocess_removed()` acumula el total eliminado por los processors.
- `py_compile` y `pyright` (0 errores) de los archivos tocados.
- Render de `stats.html` con `nextgen_stats` (valores y `None`) sin errores.

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Romper la búsqueda por métricas | Helpers con try/except; storages Void si desactivado. |
| `KeyError` por claves no configuradas | `configure()` obligatorio en `initialize()`. |
| Fuga de datos del usuario | Solo agregados; nunca texto/URLs. |
| Confusión por métricas per-proceso | Documentado en la propia spec y limitación conocida. |
| Ruido/impacto en latencia | Contadores O(1) e histogramas con lock breve. |

## Decisiones registradas

- **Integración** con `searx.metrics` en vez de subsistema nuevo.
- **Exposición** solo en `/stats` + `Server-Timing` (sin endpoint nuevo).
- **Conjunto de métricas** centrado en caché, summarizer y post-procesado.
- **Fail-safe**: helpers aislados + modo Void.
- **Privacidad**: solo datos agregados, sin contenido de consulta.
