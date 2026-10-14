# Design: Módulo Summarizer AI-First (Resúmenes Automáticos)

- **Fecha:** 2026-10-14
- **Proyecto:** searxng-nextgen
- **Mejora:** 3 (Módulo Interceptor / AI-First)
- **Estado:** Aprobado (diseño)

## Objetivo

Añadir a SearXNG-NextGen un módulo de resumen automático *privacy-first* que, tras
consolidar y post-procesar los resultados de una búsqueda, genere un resumen
opcional de fuentes múltiples mediante un backend local (Ollama / llama.cpp) y lo
inyecte en la respuesta como un `Answer`.

Características clave:

- **Desactivado por defecto** (`enabled: false`), coste cero si no se usa.
- **Privacy-first**: backend local por defecto, `local_only: true`, sin telemetría.
- **No bloqueante con cota estricta**: se ejecuta en un hilo aislado con event loop
  propio y timeout de pared; nunca bloquea indefinidamente ni rompe la búsqueda.
- **Modular y extensible**: backend abstracto + registro de backends.
- **Mínima invasión**: se reutiliza el tipo `Answer` y el mecanismo existente de
  `result_container.answers`, que ya fluye a HTML, JSON y CSV.

## Alcance

**Dentro del alcance:**

- Nuevo paquete `searx/summarizer/` (`__init__.py`, `base.py`, `backends.py`).
- Nueva plantilla `searx/templates/simple/answer/summary.html`.
- Integración en `SearchWithPlugins.search()`.
- Nuevos valores por defecto en `searx/settings_defaults.py`.
- Backends `DisabledSummarizer` y `OfflineSummarizer`.

**Fuera del alcance:**

- Endpoint HTTP dedicado o modo streaming del resumen.
- Backends externos/cloud (OpenAI, Anthropic, etc.).
- Caché persistente de resúmenes (posible mejora posterior).
- Cambios en `webapp.py`, `webutils.py` o plantillas de resultados (no necesarios).

## Estructura de archivos

```
searx/summarizer/
├── __init__.py      # Orquestador: get_summarizer(), add_summary()
├── base.py          # Summarizer (ABC) + SummarizerConfig + build_prompt()
└── backends.py      # DisabledSummarizer, OfflineSummarizer, BACKENDS

searx/templates/simple/answer/summary.html   # plantilla del resumen

searx/search/__init__.py        # MODIFICADO: integración del orquestador
searx/settings_defaults.py      # MODIFICADO: search.summarizer
```

## Componentes e interfaces

### `base.py`

`SummarizerConfig` (estructura de configuración):

| Campo | Tipo | Descripción |
|---|---|---|
| `backend` | `str` | Nombre del backend (`disabled`, `offline`). |
| `model` | `str` | Modelo a usar (p.ej. `llama3`). |
| `base_url` | `str` | URL base del servicio local. |
| `timeout` | `float` | Timeout estricto en segundos. |
| `local_only` | `bool` | Rechaza hosts no-loopback. |
| `max_tokens` | `int` | Máximo de tokens de salida. |
| `temperature` | `float` | Temperatura de muestreo. |
| `min_results` | `int` | Mínimo de resultados para disparar. |
| `categories` | `list[str]` | Categorías permitidas. |
| `max_results` | `int` | Máximo de resultados incluidos en el prompt. |
| `max_input_chars` | `int` | Presupuesto máximo de caracteres del prompt. |

`Summarizer` (ABC):

```python
class Summarizer(abc.ABC):
    name: str = "base"

    def __init__(self, config: SummarizerConfig) -> None: ...

    @abc.abstractmethod
    async def summarize(self, results: list, query: str) -> str | None:
        """Devuelve el texto del resumen o None si no está disponible."""

    def build_prompt(self, results: list, query: str) -> str:
        """Construye el prompt a partir de los resultados, aplicando
        max_results y max_input_chars con truncado."""
```

`build_prompt` formatea: consulta + lista de resultados (`title`, `url`,
`content` truncado) hasta agotar `max_input_chars`, e instrucciones de sistema
concisas (resumen neutral, sin inventar, en el idioma de la consulta).

### `backends.py`

`DisabledSummarizer`:

- `summarize(...)` → `None` inmediatamente. Backend por defecto.

`OfflineSummarizer`:

- Construye el prompt y realiza `POST {base_url}/api/generate` (Ollama) con
  `{"model", "prompt", "stream": false, "options": {"num_predict", "temperature"}}`.
- Usa **solo stdlib** (`urllib.request`) con `timeout` estricto, ejecutado vía
  `asyncio.to_thread` para no bloquear el event loop del hilo aislado.
- Extrae el texto de `response["response"]` y lo devuelve (`.strip()`).
- En cualquier error, timeout, JSON inválido o ausencia de servicio → `None`.
- Aplica `local_only`: valida que el host de `base_url` sea loopback
  (`127.0.0.1`, `localhost`, `::1`); si no y `local_only` es `true`, se
  deshabilita y registra un warning.

`BACKENDS: dict[str, type[Summarizer]] = {"disabled": ..., "offline": ...}`.

### `__init__.py`

- `get_summarizer() -> Summarizer`: lee `settings.search.summarizer`, valida el
  backend contra `BACKENDS` y devuelve una instancia. Ante config inválida →
  `DisabledSummarizer`.
- `add_summary(container, search_query) -> None` (recibe el `SearchQuery`, no un
  `str`, para poder resolver las categorías seleccionadas):
  1. Si `enabled` es `False` → retorna.
  2. Comprueba `min_results` (sobre el número de resultados ordenados) y
     `categories`: si `categories` no está vacío, debe existir intersección
     entre `search_query.categories` y las categorías configuradas. El `query`
     de texto se toma de `search_query.query`.
  3. Ejecuta el coroutine `summarize` en un hilo con event loop propio y
     `join(timeout + grace)` (cota dura de pared).
  4. Si hay texto, construye un `Answer(answer=text, template="answer/summary.html",
     engine="summarizer")` y lo añade a `container.answers`.
  5. Cualquier excepción se captura y registra; nunca propaga.

Bridging async→sync:

```python
def _run_summarize(summarizer, results, query, timeout):
    box: dict = {}
    def _target():
        try:
            box["value"] = asyncio.run(asyncio.wait_for(
                summarizer.summarize(results, query), timeout))
        except Exception as e:
            box["error"] = e
    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout + 1.0)
    if th.is_alive():
        return None
    if "error" in box:
        log.debug(...); return None
    return box.get("value")
```

## Flujo e integración

En `SearchWithPlugins.search()` (`searx/search/__init__.py`):

```python
searx.plugins.STORAGE.post_search(self.request, self)

process_container(self.result_container, self)   # Mejora 1

self.result_container.close()

try:
    add_summary(self.result_container, self.search_query)
except Exception as e:  # pylint: disable=broad-except
    logger.exception("summarizer failed: %s", e)

return self.result_container
```

`add_summary` obtiene los resultados finales con `get_ordered_results()` (el
contenedor ya está cerrado → lista ordenada en caché) y agrega el `Answer`
directamente a `container.answers`.

### Diagrama de flujo

```
search() -> external bang? -> answerers? -> standard (threads) -> ResultContainer
  -> plugins.post_search
  -> postprocess pipeline (Dedup, Filter)
  -> close (scores/sort)
  -> add_summary(container, search_query):
        enabled? min_results? categories?
          -> run OfflineSummarizer in isolated thread (strict timeout)
          -> Answer -> container.answers
  -> return container -> webapp render (HTML/JSON/CSV)
```

## Configuración (`search.summarizer`)

```python
'postprocess': { ... },   # Mejora 1 (ya existente)
'summarizer': {
    'enabled': SettingsValue(bool, False),
    'backend': SettingsValue(str, 'offline'),
    'model': SettingsValue(str, 'llama3'),
    'base_url': SettingsValue(str, 'http://127.0.0.1:11434'),
    'timeout': SettingsValue(numbers.Real, 5.0),
    'local_only': SettingsValue(bool, True),
    'max_tokens': SettingsValue(int, 256),
    'temperature': SettingsValue(numbers.Real, 0.2),
    'min_results': SettingsValue(int, 3),
    'categories': SettingsValue(list, ['general']),
    'max_results': SettingsValue(int, 8),
    'max_input_chars': SettingsValue(int, 4000),
},
```

## Manejo de errores y privacidad

- **Aislamiento total**: cada capa captura excepciones y devuelve `None`; la
  integración envuelve la llamada en `try/except` para no romper la búsqueda.
- **Cota dura**: `asyncio.wait_for` interno + `join(timeout + 1s)`; si el hilo
  sigue vivo se descarta (hilo daemon).
- **Privacy-first**: `enabled=false` por defecto; solo loopback por defecto;
  sin telemetría; logs sin contenido sensible.
- **Gated**: requiere `min_results` y categorías permitidas.
- **Local-only**: rechazo explícito de hosts no-loopback cuando `local_only=true`.

## Testing / verificación

- Unit `DisabledSummarizer.summarize` → `None`.
- Selección de backend a partir de settings (`get_summarizer`).
- `local_only`: rechazo de `base_url` no-loopback.
- `build_prompt`: respeta `max_results` y `max_input_chars`.
- Orquestador `add_summary` con backend mockeado:
  - añade `Answer` con `template="answer/summary.html"` en éxito;
  - devuelve sin cambios ante timeout/error;
  - respeta `enabled=false`, `min_results` y `categories`.
- Verificación manual con el entorno `venv` (imports, resolución de settings,
  flujo `SearchWithPlugins`) y `py_compile`.

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Bloqueo de la request por servicio lento | Timeout estricto + hilo aislado daemon. |
| Fuga de datos a terceros | `local_only=true` por defecto; sin backends cloud. |
| Impacto en latencia por defecto | `enabled=false` por defecto. |
| Romper el flujo de búsqueda | Try/except en la integración; errores → `None`. |
| Config inválida | Fallback a `DisabledSummarizer`. |

## Decisiones registradas

- **Inyección**: reutilizar `Answer` + plantilla `answer/summary.html` (fluye a
  HTML/JSON/CSV sin tocar `webapp.py`/`webutils.py`).
- **Trigger**: umbral mínimo de resultados + categorías permitidas.
- **Backend por defecto**: `offline` (Ollama/llama.cpp local) intentando HTTP y
  degradando a `None`; `disabled` como base segura.
