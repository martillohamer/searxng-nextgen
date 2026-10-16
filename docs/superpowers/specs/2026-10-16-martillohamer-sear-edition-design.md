# Design: Martillohamer Sear Edition (Pilar 5 · Fase C)

- **Fecha:** 2026-10-16
- **Proyecto:** searxng-nextgen
- **Fase:** C (Singularidad Cognitiva Distribuida) — Pilar 5
- **Spec maestra:** [`2026-10-15-distributed-cognitive-singularity-design.md`](./2026-10-15-distributed-cognitive-singularity-design.md)
- **Estado:** Implementado (commit `d9cffd30c`)
- **Handoff:** [`HANDOFF.md`](../../../HANDOFF.md)

> **Nota de honestidad técnica.** Esta sub-spec documenta el **estado real** del
> código entregado. En particular, el cliente de deepweb es **síncrono** (no
> asíncrono) y **no impone una allowlist de hosts**; y la garantía de *stealth* es
> a **nivel de aplicación** (desvía la persistencia **propia** del proceso a RAM
> volátil), **no** un sandbox de kernel que bloquee toda escritura a disco. Las
> limitaciones se detallan en [Limitaciones](#limitaciones) y
> [Seguridad y honestidad](#seguridad-y-honestidad).

## Objetivo

Dotar a SearXNG-NextGen de una edición orientada a **trabajo encubierto y
análisis forense**, componiendo tres módulos independientes y una guarda
transversal:

- **Amnesic / stealth** — operar sin dejar rastro local de la actividad, desviando
  la persistencia propia (grafo, memoria, anticipación) a un búfer volátil en RAM
  y purgando cachés/métricas al salir.
- **Forensics** — toolkit **offline** de OSINT: catálogo de dorks y extracción de
  indicadores (IOCs) de texto ya recolectado.
- **DeepWeb** — acceso a servicios ocultos Tor mediante un cliente SOCKS5 sin
  dependencias.
- **Guarda LLM** — impedir que un payload de chat termine en turno de modelo.

Características clave:

- **Opt-in y kill-switch doble**: deshabilitado por defecto (`search.nextgen.enabled`
  **y** `search.nextgen.martillohamer_sear.enabled`); el stealth añade una tercera
  puerta (`stealth_mode`).
- **Fail-safe absoluto**: cada punto de entrada público queda envuelto en
  `try/except` y degrada a un valor neutro; nunca rompe el flujo de búsqueda.
- **stdlib puro**: `socket`, `ssl`, `ssl`-compatible `ssl`, `threading`, `re`,
  `hashlib`, `html`; sin `httpx`/`requests`/`numpy`.
- **Aislamiento de pilares**: ningún módulo importa a otro pilar; el acoplamiento
  se hace en `searx/nextgen_bridge.py` (capa **no-pilar**).
- **Modular**: paquetes `searx/stealth/`, `searx/forensics/`, `searx/deepweb/` y el
  helper `searx/nextgen/llm.py` sobre la fundación `searx/nextgen/`.

## Alcance

### Dentro del alcance

- Kill-switch del Pilar 5 reconocido por el contrato (`PILLARS`).
- Supresión de escrituras de los stores propios (grafo/memoria/anticipación) y
  purga de caché de resultados y métricas.
- Catálogo de dorks + extractores de indicadores + fachada `investigate`.
- Cliente HTTP-over-SOCKS5 + parser HTML + fachadas `fetch`/`search`.
- Saneamiento de mensajes de chat hacia LLM.

### Fuera de alcance

- Bloqueo a nivel de sistema operativo/kernel de escrituras a disco.
- Ejecución async/paralela de peticiones deepweb.
- Allowlist de hosts para el cliente deepweb (hoy solo filtra plantillas de motor
  conocidas).
- Integración UI dedicada (los módulos se exponen como API Python).

## Fundación transversal — `searx/nextgen/`

El Pilar 5 se apoya en el contrato existente:

```python
# searx/nextgen/contract.py
NEXTGEN_SCHEMA_VERSION: int = 1
PILLARS: frozenset[str] = frozenset(
    {"graph", "anticipate", "sandbox", "mesh", "martillohamer_sear"}
)

def is_enabled(pillar: str | None = None) -> bool: ...   # global AND pillar
```

y añade un helper compartido:

```python
# searx/nextgen/llm.py
USER_ROLE: str = "user"
ASSISTANT_ROLES: frozenset[str] = frozenset({"assistant", "model"})

def sanitize_messages(messages, default_user_content="Continue.") -> list[dict]: ...
```

## Módulo Stealth — `searx/stealth/`

### Modelo de amenaza y alcance de la garantía

Mientras un `StealthContext` está activo, las capas de persistencia **propias** de
la aplicación consultan `suppress()` antes de escribir. Si la supresión está
activa, el payload se **desvía a un búfer volátil en RAM** (bounded) en lugar de
persistirse; al salir del contexto el búfer se descarta y se ejecutan los *purge
callbacks* registrados, de modo que el bloque no deja traza local.

Explícitamente **fuera** de la garantía: no se intercepta I/O de terceros ni se
bloquean escrituras ajenas al proceso; no se toca el sistema de archivos ni el
kernel.

### API (`searx/stealth/context.py`)

```python
ASSISTANT_STATE_NS: str = "stealth"
_MAX_BUFFER_PER_NAMESPACE: int = 1000

def is_enabled() -> bool                 # nextgen.enabled AND martillohamer_sear.enabled AND stealth_mode
def is_active() -> bool                  # profundidad > 0
def activate() -> int                    # +1 (ref-contado)
def deactivate() -> int                  # -1; a 0 → purge()
def suppress(namespace, payload=None) -> bool
def buffered(namespace=None) -> list
def drain() -> dict[str, list]
def register_purge(callback) -> bool
def purge() -> int                       # descarta búfer + ejecuta purges
def reset() -> None

class StealthContext:                    # context manager ref-contado
    def __enter__(self) -> "StealthContext": ...
    def __exit__(self, *exc) -> bool: ... # False → no traga excepciones
```

### Inyección en los stores (regla de aislamiento intacta)

Cada store persistente expone `set_write_guard(guard | None)` y consulta
`_suppressed(payload)` antes de escribir. Un guard que lanza excepción se trata
como **fail-open** (`False`): nunca se bloquea la escritura normal por un fallo
del guardián. Valores neutros al suprimir:

| Store | Operación | Neutro |
|---|---|---|
| `searx/graph/store.py` | `upsert_entity` / `upsert_triples` / `add_project` / `import_fragment` | `None` / `0` / `None` / `0` |
| `searx/memory/store.py` | `enqueue` | sin efecto (`return`) |
| `searx/anticipate/store.py` | `record_transition` / `store_artifact` | `False` |

### Integración — `searx/nextgen_bridge.py`

Capa **no-pilar** (única autorizada a acoplar pilares):

```python
def wire_stealth() -> bool                       # instala guard + registra purges
def stealth_session(namespace="nextgen")         # context manager → yields bool
def _register_stealth_purges()                   # result cache + metrics
```

Los purges vacían `searx.resultcache.reset_result_cache()` y las
`searx.metrics.counter_storage`/`histogram_storage` (vía `getattr(storage,"clear")`).

## Módulo Forensics — `searx/forensics/`

Puro, **sin red**: solo *construye* dorks y *extrae* indicadores de texto ya
recolectado. Trivial de habilitar y de testear.

### `searx/forensics/dorks.py`

```python
CATEGORIES: tuple[str, ...] = (
    "documents", "credentials", "directories", "infrastructure",
    "code", "social", "archives",
)
DORK_CATALOG: tuple[tuple[str, str, str, str, str], ...]   # 24 entradas
# (id, category, engine, template, description) — template lleva un único {q}

def list_categories() -> list[str]
def build_dorks(query, categories=None) -> list[dict]   # {... "query": dork renderizado}
def dork_url(dork) -> str                                # URL de búsqueda compartible
```

### `searx/forensics/extractors.py`

```python
EXTRACTORS: dict[str, re.Pattern[str]] = { ... }   # 14 patrones
def list_extractors() -> list[str]
def extract(text, names=None) -> dict[str, list[str]]
```

Extractores: `email`, `url`, `ipv4`, `ipv6`, `onion`, `aws_access_key`,
`google_api_key`, `btc_address`, `eth_address`, `aws_arn` y hashes de credencial
(`email`/`url`/`ip`/…). Todos regex-only y fail-safe.

### Fachada `investigate`

```python
def is_enabled() -> bool
def investigate(query, text="", dork_categories=None, indicator_names=None) -> dict
```

Devuelve un dict serializable con `query`, `enabled`, `dork_count`, `dorks`
(cada uno con `url`), `indicators`, `pivots` (`onion`/`url`/`email`/`ipv4`/`ipv6`)
e `indicator_count`.

## Módulo DeepWeb — `searx/deepweb/`

Único módulo del Pilar 5 con **I/O de red saliente**. Tor se alcanza por su proxy
SOCKS5 (`socks5h://127.0.0.1:9050`).

### `searx/deepweb/tor_client.py` (síncrono, stdlib: `socket`+`ssl`)

```python
DEFAULT_TOR_PROXY: str = "socks5h://127.0.0.1:9050"

class TorClientError(RuntimeError): ...
@dataclass class Response: status, headers, text, url, ...

def request(method, url, timeout=15, headers=None, proxy=None,
            body=None, verify_tls=False) -> Response
def get(url, **kwargs) -> Response
def post(url, data=None, **kwargs) -> Response
def is_tor_available(proxy=DEFAULT_TOR_PROXY, timeout=2.0) -> bool
```

- `socks5h` resuelve el **nombre en el proxy** (requisito para `.onion`).
- Handshake RFC 1929 + RFC 1928; soporta redirecciones y `chunked` (`_dechunk`).
- `verify_tls=False` por defecto: para `.onion` el propio servicio oculto es el
  autenticador; en clearnet, pasar `verify_tls=True`.

### `searx/deepweb/parsers.py` (stdlib HTML → texto)

`extract_title`, `to_text`, `extract_links`, `parse_page`. `_SKIP_TAGS` excluye
`script`, `style`, `noscript`, `template`.

### Fachada (`searx/deepweb/__init__.py`)

```python
ENGINE_TEMPLATES = {
    "ahmia":   "https://ahmia.fi/search/?q={query}",     # índice Tor en clearnet
    "torch":   "http://…noyd.onion/search?query={query}", # motor oculto
    "haystak": "http://…pxfid.onion/search?q={query}",    # motor oculto
}

def is_enabled() -> bool
def configured_engines() -> list[str]           # solo plantillas conocidas
def tor_available(timeout=2.0) -> bool
def fetch(url, timeout=None, proxy=None) -> dict  # {ok,status,title,text,links,error}
def search(query, engines=None, timeout=None, max_results=20) -> dict
```

`search` recoge errores por motor en `errors` y nunca lanza.

## Configuración — `search.nextgen.martillohamer_sear`

```python
'martillohamer_sear': {
    'enabled': SettingsValue(bool, False),
    'stealth_mode': SettingsValue(bool, False),
    'tor_proxy': SettingsValue(str, 'socks5h://127.0.0.1:9050'),
    'timeout': SettingsValue(int, 15),
    'osint_dorks_enabled': SettingsValue(bool, True),
    'deepweb_engines': SettingsValue(list, ['ahmia', 'torch', 'haystak']),
},
```

| Clave | Tipo | Defecto | Descripción |
|---|---|---|---|
| `nextgen.enabled` | bool | `false` | Interruptor global de la Fase C. |
| `martillohamer_sear.enabled` | bool | `false` | Kill-switch del Pilar 5. |
| `martillohamer_sear.stealth_mode` | bool | `false` | Tercera puerta del modo amnesic. |
| `martillohamer_sear.tor_proxy` | str | `socks5h://127.0.0.1:9050` | Proxy SOCKS5 de Tor. |
| `martillohamer_sear.timeout` | int | `15` | Timeout (s) por petición. |
| `martillohamer_sear.osint_dorks_enabled` | bool | `true` | Catálogo de dorks activo. |
| `martillohamer_sear.deepweb_engines` | list | `[ahmia, torch, haystak]` | Motores de `search`. |

## Requisitos e instalación

- **Python** y entorno del proyecto; el venv puede ser **parcial** (el Pilar 5 no
  añade dependencias): usa solo la **stdlib**.
- **Validación estática** (recomendada antes de commitear):
  ```powershell
  npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe searx/stealth searx/forensics searx/deepweb searx/nextgen
  ```
  Objetivo: **0 errores, 0 warnings**.
- **DeepWeb (opcional):** un **demonio Tor local** escuchando en el SOCKS5
  `127.0.0.1:9050` (protocolo `socks5h`). Sin Tor, `fetch`/`search` degradan de
  forma fail-safe. Forensics **no** requiere red.

## Privacidad

- **Stealth:** 100 % local; nada sale del host. Reduce la huella de la
  **persistencia propia** (RAM volátil + purga al salir).
- **Forensics:** 100 % offline; su salida es texto (dorks/indicadores) que el
  operador decide si envía.
- **DeepWeb:** única salida de red, **explícita** (opt-in), vía **Tor**. El modo
  stealth **no** anonimiza tráfico de terceros: la anonimidad depende de operar
  Tor y `stealth_mode`.
- **Guarda LLM:** evita enviar payloads malformados a la API del modelo.

## Manejo de errores

Todos los puntos de entrada públicos están envueltos en `try/except` y degradan a
un valor neutro, registrando en `log.debug` como máximo:

| Módulo | Neutro |
|---|---|
| `stealth` | `is_enabled/is_active` → `False`; `activate/deactivate` → profundidad/0; `suppress` → `False` |
| `forensics.investigate` | dict con contadores a 0 |
| `deepweb.fetch` | `{"ok": False, ..., "error": <msg>}` |
| `deepweb.search` | dict con `results: []` y `errors` por motor |
| `sanitize_messages` | `[{"role": "user", "content": "Continue."}]` |

## Limitaciones

- **Deepweb síncrono:** las peticiones bloquean el hilo llamante; no hay
  concurrencia/async.
- **Sin allowlist de hosts:** el cliente acepta cualquier `http`/`https`; solo se
  validan las **plantillas de motor** conocidas al seleccionar motores.
- **Stealth a nivel de aplicación:** no intercepta I/O ajeno al proceso ni bloquea
  escrituras a nivel de SO.
- **`webapp.py`:** persisten errores de pyright **preexistentes**, ajenos a este
  pilar.

## Estrategia de verificación

- `py_compile` de todos los archivos nuevos/modificados.
- `pyright` (0 errores, 0 warnings) sobre `searx/stealth/`, `searx/forensics/`,
  `searx/deepweb/` y `searx/nextgen/`.
- Smoke tests reproducibles (bucle sin *flakes*):
  - `stealth core + graph guard`
  - `forensics dorks + extractors + investigate`
  - `deepweb parsers + tor_client + facade`
- Re-ejecución de las pruebas de Fase A/B para descartar regresiones.

## Decisiones registradas

- **Alcance del stealth:** supresión de la persistencia **propia** (grafo/memoria/
  anticipación) + purga de caché/métricas; **no** sandbox de kernel.
- **Guarda LLM:** saneamiento de *payload* (garantizar fin en `user`), no cambios de
  protocolo por cliente.
- **Forensics offline:** dorks + indicadores puros y sin red; el operador decide el
  uso.
- **DeepWeb síncrono y sin allowlist** en esta edición (limitación aceptada y
  documentada).
- **Aislamiento:** acoplamiento solo vía `searx/nextgen_bridge.py` (no-pilar);
  ningún import pilar→pilar.

## Seguridad y honestidad

- **Fail-open en el guard:** `suppress()` devuelve `False` ante cualquier excepción,
  por lo que un fallo del guardián **nunca** bloquea la escritura legítima.
- **No se promete más de lo entregado:** «stealth» es privacidad local de la
  persistencia del proceso, no un sistema anti-forense a nivel de disco; «deepweb»
  es acceso por Tor, no garantía de anonimato del operador.
