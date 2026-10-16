# HANDOFF — searxng-nextgen

- **Fecha:** 2026-10-16
- **Proyecto:** searxng-nextgen (fork de SearXNG)
- **Fase:** C — Singularidad Cognitiva Distribuida
- **Edición:** **Martillohamer Sear Edition (Pilar 5)**
- **Estado:** COMPLETADO — `py_compile` OK · `pyright` **0 errores / 0 warnings** · smoke tests OK
- **Commit de la edición:** `d9cffd30c` — *feat(nextgen): add Martillohamer Sear edition: stealth, forensics, deepweb (Phase C - Pilar 5)* (18 archivos, +1591 / −32)
- **Spec técnica:** [`docs/superpowers/specs/2026-10-16-martillohamer-sear-edition-design.md`](docs/superpowers/specs/2026-10-16-martillohamer-sear-edition-design.md)

## Resumen

El **Pilar 5** convierte la fundación NextGen (Pilares 1–4) en una edición de
trabajo encubierto y análisis: modo **amnesic/stealth** (persistencia del propio
proceso desviada a RAM volátil y purgada al salir), toolkit **forensics** offline
(dorks OSINT + extracción de indicadores) y acceso a **deepweb** vía Tor sobre un
cliente SOCKS5 sin dependencias. Se añade además una guarda transversal que
impide enviar a un LLM una conversación que termine en turno de modelo.

Todo es **opt-in** (`search.nextgen.enabled` **y**
`search.nextgen.martillohamer_sear.enabled`, ambos `false` por defecto),
**fail-safe** (cada punto de entrada público queda envuelto en `try/except` y
degrada a un valor neutro) y **stdlib puro**.

## Pilares de la Fase C

| # | Pilar | Paquete | Estado |
|---|---|---|---|
| 1 | Search Cards ejecutables | `searx/sandbox/` | Implementado |
| 2 | Grafo de conocimiento | `searx/graph/` | Implementado |
| 3 | Caché neural de anticipación | `searx/anticipate/` | Implementado |
| 4 | Malla P2P soberana | `searx/mesh/` | Implementado |
| 5 | **Martillohamer Sear Edition** | `searx/stealth/`, `searx/forensics/`, `searx/deepweb/` | **Implementado** |

## Componentes entregados

### 1. Contrato y configuración

- `searx/nextgen/contract.py:34` — `PILLARS` ahora incluye `martillohamer_sear`:
  ```python
  PILLARS: frozenset[str] = frozenset(
      {"graph", "anticipate", "sandbox", "mesh", "martillohamer_sear"}
  )
  ```
  Con ello el kill-switch jerárquico (`is_enabled`) reconoce el pilar; un nombre
  desconocido sigue tratándose como deshabilitado.
- `searx/settings_defaults.py:289` — bloque por defecto
  `search.nextgen.martillohamer_sear` (ver *Configuración*).

### 2. Guarda LLM — `sanitize_messages`

- `searx/nextgen/llm.py` — `sanitize_messages(messages, default_user_content="Continue.")`
  devuelve una **lista nueva**, no vacía y que **termina estrictamente en rol `user`**.
  Varias APIs de chat rechazan payloads que terminan en turno de modelo
  (`Requests ending with a model turn are not supported`).
  - `USER_ROLE = "user"`, `ASSISTANT_ROLES = frozenset({"assistant", "model"})`.
  - Conserva el turno inicial `assistant`/`system`; solo elimina los turnos
    **finales** que no son `user`.
  - Ante entrada inesperada degrada a `[{"role": "user", "content": "Continue."}]`.
  - Exportado como `searx.nextgen.sanitize_messages`.
- Aplicado en `searx/summarizer/backends.py` y `searx/engines/cloudflareai.py`.

### 3. Módulo Stealth — `searx/stealth/`

- `searx/stealth/context.py` — `StealthContext` (context manager ref-contado) y
  API de módulo: `is_enabled`, `is_active`, `activate`, `deactivate`, `suppress`,
  `buffered`, `drain`, `register_purge`, `purge`, `reset`.
  - **Garantía (a nivel de aplicación):** mientras hay un contexto activo, las
    capas de **persistencia propias** del proceso consultan `suppress()` antes de
    escribir; al estar activo, el payload se desvía a un búfer **volátil en RAM**
    (tope `_MAX_BUFFER_PER_NAMESPACE = 1000` por namespace) y al salir del bloque
    se descarta y se ejecutan los *purge callbacks* registrados.
  - **No** es un sandbox de kernel que bloquee «toda» escritura a disco: no toca
    el sistema de archivos ni el kernel.
  - Anidamiento ref-contado: solo la salida más externa purga.
  - `is_enabled()` exige tres puertas: `nextgen.enabled` **y**
    `martillohamer_sear.enabled` **y** `martillohamer_sear.stealth_mode`.
- Guardas `set_write_guard()` inyectadas en `searx/graph/store.py`,
  `searx/memory/store.py` y `searx/anticipate/store.py`. Mientras el guard está
  activo, esas stores devuelven neutros (`upsert_entity`→`None`, `upsert_triples`→`0`,
  `add_project`→`None`, `import_fragment`→`0`, `enqueue`→sin efecto,
  `record_transition`→`False`, `store_artifact`→`False`).
- `searx/nextgen_bridge.py` — integrador **no-pilar** que respeta la regla de
  aislamiento (ningún pilar importa a otro): `wire_stealth()` instala el guard y
  registra los purges; `stealth_session()` ejecuta un bloque en modo amnesic y
  devuelve `True`/`False` según esté activo.

### 4. Módulo Forensics — `searx/forensics/`

Toolkit **offline y sin red** (nunca realiza peticiones).

- `searx/forensics/dorks.py` — `CATEGORIES` (7), `DORK_CATALOG` (**24** dorks
  curados, tuplas `(id, category, engine, template, description)`), `build_dorks`,
  `dork_url`, `list_categories`.
  - Categorías: `documents`, `credentials`, `directories`, `infrastructure`,
    `code`, `social`, `archives`.
- `searx/forensics/extractors.py` — `EXTRACTORS` (**14** patrones regex:
  `email`, `url`, `ipv4`, `ipv6`, `onion`, `hash`/`aws`/`google` keys, wallets
  `btc_address`/`eth_address`, `aws_arn`…), `extract`, `list_extractors`.
- `searx/forensics/__init__.py` — `is_enabled`, `investigate(query, text, ...)`:
  construye los dorks y mina `text` en busca de indicadores, devolviendo un dict
  serializable con `dorks`, `indicators`, `pivots` y contadores.

### 5. Módulo DeepWeb — `searx/deepweb/`

Único pilar con **I/O de red saliente**; opt-in.

- `searx/deepweb/tor_client.py` — cliente HTTP **síncrono** y sin dependencias
  sobre SOCKS5 (stdlib `socket`+`ssl`). `DEFAULT_TOR_PROXY = "socks5h://127.0.0.1:9050"`
  (`socks5h` resuelve el nombre *en el proxy*, requisito para `.onion`).
  `Response`, `TorClientError`, `request`/`get`/`post`, `is_tor_available`,
  `_parse_proxy`, `_socks5_handshake`, `_dechunk`; sigue redirecciones y
  des-`chunkea` la respuesta. `verify_tls=False` por defecto (la dirección `.onion`
  es el autenticador); `verify_tls=True` para hosts clearnet con CA real.
- `searx/deepweb/parsers.py` — parsing HTML con stdlib (`extract_title`, `to_text`,
  `extract_links`, `parse_page`); se excluyen `script`/`style`/`noscript`/`template`.
- `searx/deepweb/__init__.py` — `is_enabled`, `configured_engines`, `tor_available`,
  `fetch`, `search`; `ENGINE_TEMPLATES` para `ahmia` (clearnet), `torch` y
  `haystak` (`.onion`). Filtra a plantillas conocidas y recoge errores por motor
  sin lanzar excepciones.
- **Limitación conocida (estado actual):** el cliente es **bloqueante** (no async)
  y **no impone allowlist de hosts**; opera contra los `deepweb_engines`
  configurados. La supresión stealth afecta a la persistencia **propia** de la
  app, no al tráfico de terceros; para anonimato hay que operar con Tor activo y
  `stealth_mode` en `true`.

## Configuración

Árbol YAML por defecto (`settings_defaults.py`):

```yaml
search:
  nextgen:
    enabled: false                       # global de la Fase C
    martillohamer_sear:
      enabled: false                     # kill-switch del Pilar 5
      stealth_mode: false                # activa la supresión amnesic
      tor_proxy: 'socks5h://127.0.0.1:9050'
      timeout: 15
      osint_dorks_enabled: true
      deepweb_engines: ['ahmia', 'torch', 'haystak']
```

| Clave | Tipo | Defecto | Descripción |
|---|---|---|---|
| `nextgen.enabled` | bool | `false` | Interruptor global de la Fase C. |
| `martillohamer_sear.enabled` | bool | `false` | Interruptor del Pilar 5. |
| `martillohamer_sear.stealth_mode` | bool | `false` | Habilita supresión de persistencia (tercera puerta de `stealth.is_enabled`). |
| `martillohamer_sear.tor_proxy` | str | `socks5h://127.0.0.1:9050` | Proxy SOCKS5 de Tor. |
| `martillohamer_sear.timeout` | int | `15` | Timeout (s) de las peticiones deepweb. |
| `martillohamer_sear.osint_dorks_enabled` | bool | `true` | Habilita el catálogo de dorks. |
| `martillohamer_sear.deepweb_engines` | list | `[ahmia, torch, haystak]` | Motores usados por `deepweb.search`. |

## Verificación

- `py_compile` de los 18 archivos tocados → **exit 0**.
- `pyright@1.1.390` sobre los módulos nuevos/modificados → **0 errores, 0 warnings**
  (se corrigieron 3 `reportOptionalMemberAccess` en `searx/nextgen_bridge.py:69-71`).
- Smoke tests reproducibles → **OK**:
  - `stealth core + graph guard`
  - `forensics dorks + extractors + investigate`
  - `deepweb parsers + tor_client + facade`
- Nota: `searx/webapp.py` tiene errores de pyright **preexistentes** (ajenos a la
  edición): líneas 166, 491, 525, 532, …

## Aislamiento y seguridad

- **Sin imports pilar→pilar.** El acoplamiento se hace solo en
  `searx/nextgen_bridge.py` (capa no-pilar) mediante dependencias inyectadas.
- **FAIL-SAFE absoluto:** `suppress()` devuelve `False` ante cualquier excepción
  del guard (fail-open): la escritura normal nunca se bloquea por un fallo del
  guardián.
- `pyrightconfig.json` y `run` sin cambios de dependencias: **stdlib puro**
  (`venv` parcial, sin `httpx`/`requests`).
