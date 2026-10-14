# Design: Caché Inteligente Híbrida de Resultados

- **Fecha:** 2026-10-14
- **Proyecto:** searxng-nextgen
- **Mejora:** 2 (Caché Inteligente de Resultados)
- **Estado:** Aprobado e implementado

## Objetivo

Añadir a SearXNG-NextGen una caché de *resultados de motores* que evite repetir
peticiones de red para consultas idénticas ya vistas, reduciendo latencia, carga
sobre los motores y consumo. La caché es **privacy-first** (la clave nunca
contiene datos del usuario), **versionada y firmada** (HMAC) y **fail-safe**
(cualquier fallo degrada a un backend no-op).

Características clave:

- **Activada por defecto** con TTL corto (`enabled: true`, `ttl: 600`).
- **Privacy-first**: clave = hash SHA-256 de parámetros de búsqueda normalizados,
  sin IP, cabeceras, cookies ni id de usuario.
- **Integridad**: los valores se guardan como pickle *versionado* y *firmado* con
  HMAC-SHA256 (`server.secret_key`); la firma se verifica antes de `pickle.loads`.
- **Híbrida**: backend SQLite local y backend `Valkey`/Redis distribuido, con
  selección `auto` (Valkey si está disponible, si no SQLite).
- **Fail-safe**: cualquier error en build/get/set/restore degrada a
  `NullResultCache` sin romper la búsqueda.
- **Consistencia configurable**: se cachea solo la fase de motores; el resto del
  pipeline (`post_search`, post-procesado y summarizer) se re-ejecuta en cada
  *hit*, por lo que refleja siempre la configuración vigente.

## Alcance

**Dentro del alcance:**

- Nuevo módulo `searx/resultcache.py` (clave, códec HMAC, backends, factory, API).
- Integración de *lookup*/*store* en `searx/search/__init__.py`.
- Nuevo flag `from_cache` en `searx/results.py` (`ResultContainer`).
- Nuevos valores por defecto `search.result_cache` en `searx/settings_defaults.py`.
- Marca de origen de caché: campo `from_cache` en JSON (`searx/webutils.py`) y
  guard para el timing `cache` en `searx/templates/simple/elements/engines_msg.html`.

**Fuera del alcance:**

- Caché del resumen AI-First (Mejora 3) o de los `Answer` de forma aislada.
- Invalidación por cambio de configuración de motores (acotada por TTL).
- Compresión de blobs (los snapshots son pequeños; pickle directo).
- Backends externos distintos de SQLite/Valkey.
- Inclusión de `engine_data` en la clave (limitación documentada, ver abajo).

## Estructura de archivos

```
searx/resultcache.py            # NUEVO: clave, códec HMAC, backends, factory, API

searx/results.py                # MODIFICADO: ResultContainer.from_cache
searx/search/__init__.py        # MODIFICADO: cache_load() en search_standard(),
                                #            cache_store() en search()
searx/settings_defaults.py      # MODIFICADO: search.result_cache
searx/webutils.py               # MODIFICADO: from_cache en la respuesta JSON
searx/templates/simple/elements/engines_msg.html   # MODIFICADO: guard timing 'cache'
```

## Componentes e interfaces

### Clave de caché — `build_cache_key(search_query) -> str`

Combina, con separador `\x1f`, y devuelve el SHA-256 hex (64 chars) de:

| Componente | Origen | Normalización |
|---|---|---|
| Versión | `CACHE_VERSION` | `v{n}` en la clave y en el blob. |
| Texto de consulta | `search_query.query` | colapso de espacios (`" ".join(query.split())`). |
| Categorías | `search_query.categories` | ordenadas y sin duplicados. |
| Motores | `search_query.engineref_list` | `(name, category)` ordenados. |
| Idioma | `search_query.lang` | tal cual. |
| Safesearch | `search_query.safesearch` | tal cual. |
| Rango temporal | `search_query.time_range` | tal cual. |
| Página | `search_query.pageno` | `or 1`. |

La clave **no** contiene datos de usuario. **`pageno` se incluye** para evitar
servir una página equivocada (mismo texto, distinta página ⇒ claves distintas).

### Códec versionado + HMAC

Formato de blob: `b"SXC1" + bytes([CACHE_VERSION]) + HMAC-SHA256(payload) + payload`,
donde `payload = pickle.dumps(CachedResults, HIGHEST_PROTOCOL)` y la clave HMAC se
deriva de `server.secret_key` (`get_setting("server.secret_key")`).

- `_pack(snapshot) -> bytes`.
- `_unpack(blob) -> CachedResults | None`:
  1. valida tipo y longitud del encabezado;
  2. comprueba el *magic* `SXC1`;
  3. comprueba `CACHE_VERSION` (versión distinta ⇒ `None`, invalida todo);
  4. verifica el HMAC con `hmac.compare_digest` **antes** de `pickle.loads`;
  5. deserializa y comprueba `isinstance(obj, CachedResults)`.
  Cualquier fallo ⇒ `None` (con log de warning si falla el HMAC).

### `CachedResults` (snapshot serializable)

| Campo | Tipo | Descripción |
|---|---|---|
| `main_results` | `list[MainResult \| LegacyResult]` | Resultados principales de motores. |
| `answers` | `list[BaseAnswer]` | Respuestas (answerers). |
| `infoboxes` | `list[LegacyResult]` | Infoboxes. |
| `suggestions` | `list[str]` | Sugerencias. |
| `corrections` | `list[str]` | Correcciones ortográficas. |
| `engine_data` | `dict[str, dict[str, str]]` | Datos auxiliares por motor. |
| `paging` | `bool` | Indica si hay más páginas. |
| `created` | `float` | Marca temporal de creación. |

### Backends — `ResultCache` (ABC)

```python
class ResultCache(abc.ABC):
    name: str = "base"
    def get(self, key: str) -> CachedResults | None: ...
    def set(self, key: str, value: CachedResults, ttl: int) -> bool: ...
```

- **`NullResultCache`** (`name="null"`): `get` → `None`, `set` → `False`. Backend
  por defecto cuando la caché está desactivada o un backend falló.
- **`SQLiteResultCache`** (`name="sqlite"`): envuelve `ExpireCacheSQLite` con el
  contexto/tabla `search_results`; subclase `_BlobCache` con `serialize`/
  `deserialize` identidad (guarda el blob ya empaquetado tal cual).
  `MAX_VALUE_LEN = 1 MiB`.
- **`ValkeyResultCache`** (`name="valkey"`): usa un cliente `valkey.Valkey`
  (API compatible con redis-py). Prefijo `searxng:search_results:`; `get` → `GET`;
  `set` → `SETEX` con `max(1, int(ttl))`. Errores de cliente ⇒ `None`/`False`.

Factory:

- `_build_sqlite()` → `SQLiteResultCache()` o `NullResultCache()` ante error.
- `_build_valkey()` → `ValkeyResultCache(client)` si hay cliente (llamando a
  `valkeydb.initialize()` si hace falta), o `None` si no hay/falla.
- `_build_cache()`: respeta `enabled`; `backend` = `sqlite` | `valkey` | `auto`
  (y cualquier otro valor cae a `sqlite` con warning). `valkey`/`auto` intentan
  Valkey y degradan a SQLite.
- `get_result_cache()` → singleton perezoso; error ⇒ `NullResultCache`.
- `reset_result_cache()` → descarta el singleton (tests / recarga de config).

### API de alto nivel

- `load(search_query) -> CachedResults | None`: respeta `enabled`; `build_cache_key`
  + `backend.get`; cualquier error ⇒ `None`.
- `store(search_query, container) -> bool`: respeta `enabled`; **omite** redirects
  (bangs) y contenedores vacíos (`main_results`/`answers`/`infoboxes` vacíos);
  toma `ttl` de settings y llama a `backend.set`.
- `load_into(container, search_query) -> bool`: `True` en *hit*; restaura el
  contenedor (`_restore`) y añade `Timing("cache", ...)`.
- `_make_snapshot(container)` / `_restore(container, snapshot)`: convierten entre
  `ResultContainer` y `CachedResults`; `_restore` marca `container.from_cache = True`.

## Flujo e integración

En `searx/search/__init__.py`:

```python
from searx.resultcache import load_into as cache_load
from searx.resultcache import store as cache_store

def search_standard(self):
    try:
        if cache_load(self.result_container, self.search_query):
            self.actual_timeout = 0.0
            return True
    except Exception as e:  # pylint: disable=broad-except
        logger.exception("result cache lookup failed: %s", e)
    requests, self.actual_timeout = self._get_requests()
    if requests:
        self.search_multiple_requests(requests)
    return True

def search(self) -> ResultContainer:
    self.start_time = default_timer()
    if not self.search_external_bang():
        if not self.search_answerers():
            self.search_standard()
            if not self.result_container.from_cache:
                try:
                    cache_store(self.search_query, self.result_container)
                except Exception as e:  # pylint: disable=broad-except
                    logger.exception("result cache store failed: %s", e)
    return self.result_container
```

`SearchWithPlugins.search()` hereda este comportamiento: en un *hit* el
contenedor se puebla al instante, y **a continuación** se ejecutan
`post_search`, `process_container` (Mejora 1), `close()` y el summarizer
(Mejora 3). No se vuelve a almacenar (`from_cache` ya es `True`).

### Diagrama de flujo

```
search()
  -> external bang?  -> answerers?  -> search_standard()
        -> cache_load(container, query)?
             HIT  -> _restore() + Timing("cache"), actual_timeout=0   [sin red]
             MISS -> _get_requests() -> search_multiple_requests()
        -> if not from_cache: cache_store(query, container)
  -> plugins.post_search
  -> postprocess pipeline (Dedup, Filter)      # se re-ejecuta en hit
  -> close (scores/sort)
  -> add_summary(container, search_query)      # se re-ejecuta en hit
  -> return container -> webapp render (HTML/JSON/CSV + from_cache)
```

## Configuración (`search.result_cache`)

```python
'postprocess': { ... },   # Mejora 1
'summarizer': { ... },    # Mejora 3
'result_cache': {
    'enabled': SettingsValue(bool, True),
    'ttl': SettingsValue(int, 600),
    'backend': SettingsValue(str, 'sqlite'),   # 'sqlite' | 'valkey' | 'auto'
},
```

El backend Valkey reutiliza `valkey.url` (ver `searx/valkeydb.py`).

## Marca de origen de caché

- `ResultContainer.from_cache: bool = False`.
- `Timing("cache", dur, 0.0)` añadido por `load_into` (se refleja en `Server-Timing`).
- Plantilla `engines_msg.html`: si `engine_name == 'cache'` se muestra "from cache"
  sin enlace a `stats` (evita URL inválida y división por cero).
- JSON (`webutils.get_json_response`): campo `"from_cache": rc.from_cache`.

## Manejo de errores y privacidad

- **Aislamiento total**: build/get/set/restore y la integración envuelven todo en
  `try/except`; ningún fallo propaga ni rompe la búsqueda.
- **Integridad**: HMAC verificado antes de deserializar; versión en el blob y en
  la clave. Manipulación/versión distinta ⇒ entrada descartada.
- **Privacy-first**: la clave es un hash sin datos de usuario; no se registran
  consultas ni contenido en logs (solo debug de errores).
- **Fail-safe**: `auto` degrada Valkey → SQLite → Null.

## Limitaciones conocidas

- **`engine_data` no forma parte de la clave**: datos específicos de motor con
  salida dependiente de estado externo podrían quedar *stale* hasta el TTL. Se
  acepta por ser difícil de normalizar; el TTL acota la ventana.
- **Doble conteo de métricas en hit**: `close()` se re-ejecuta sobre resultados
  restaurados y vuelve a emitir `counter_add`; efecto menor y acotado.
- **Config de motores no invalida**: cambios de pesos/opciones no invalidan
  entradas existentes; acotado por TTL.

## Testing / verificación

- **Clave**: estabilidad ante reordenación de motores y espacios; cambios de
  `pageno`/`safesearch`/`lang`/`time_range` producen claves distintas; 64 hex.
- **Códec**: round-trip; rechazo de HMAC manipulado, versión incorrecta y basura.
- **SQLite**: `store` → `load` restaura `main_results`/`answers`/infoboxes/
  suggestions/corrections/`engine_data`/`paging`; TTL expira.
- **Valkey**: con cliente *stub* (`get`/`setex`) → set/get/miss y prefijo correcto.
- **Factory**: `auto` cae a SQLite sin Valkey; `enabled=false` ⇒ `null`, sin
  `store`/`load`.
- **`load_into`**: puebla el contenedor, `from_cache=True`, añade `Timing("cache")`.
- **Integración `Search.search()`**: 1ª llamada = miss (motores consultados +
  store); 2ª llamada = hit (motores **no** consultados), mismos resultados.
- **Estático**: `py_compile`, imports sin ciclos y `pyright` (0 errores en
  `searx/resultcache.py`; los errores preexistentes en `search/__init__.py` y
  `results.py` no se ven afectados).

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Datos obsoletos | TTL corto (600 s) + `pageno` en la clave. |
| Fuga de datos del usuario | Clave hash sin PII; sin logs de consultas. |
| Ejecución de código por store manipulado | HMAC verificado antes de `pickle.loads`. |
| Fallo de backend distribuido | Degradación `auto` → SQLite → Null. |
| Romper la búsqueda | Try/except en todas las capas; fallo ⇒ caché no-op. |
| Resultados incoherentes por config | Re-ejecutar post-procesado y summarizer en cada hit. |

## Decisiones registradas

- **Qué se cachea**: snapshot de resultados de motores **tras** `search_standard`
  (post-`on_result`); en *hit* se re-ejecutan `post_search`, post-procesado y
  summarizer.
- **Formato**: pickle versionado + firmado con HMAC (`server.secret_key`).
- **Backends**: SQLite + Valkey completos, con `auto` y degradación segura.
- **Campos de clave**: query, categorías, motores, lang, safesearch, time_range y
  `pageno`.
- **`engine_data`**: excluido de la clave (limitación documentada).
- **`CACHE_VERSION`**: constante interna que invalida todas las entradas al subir.
