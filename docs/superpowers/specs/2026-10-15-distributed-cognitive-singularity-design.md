# Design maestro: Fase C — Singularidad Cognitiva Distribuida

- **Fecha:** 2026-10-15
- **Proyecto:** searxng-nextgen
- **Fase:** C (Singularidad Cognitiva Distribuida)
- **Estado:** Aprobado en arquitectura; **Pilares 1–5 implementados** (el Pilar 5, *Martillohamer Sear Edition*, se añade el 2026-10-16)
- **Specs por pilar:** ver [Sub-specs](#sub-specs-por-pilar)

## Objetivo

Llevar SearXNG-NextGen de un buscador privado con memoria a un **sustrato
cognitivo local** que aprende del uso del operador. La Fase C se compone de
**cinco pilares independientes pero relacionados** que comparten una fundación
mínima común. Ninguno de ellos debe poder romper la búsqueda, ningún dato debe
salir del host por defecto (el pilar 5 añade una salida de red **explícita** y
opt-in vía Tor) y todos son **opt-in**.

## Los cinco pilares

| # | Pilar | Identificador | Propósito |
|---|---|---|---|
| 1 | **Search Cards ejecutables** | `sandbox` | Ejecutar consultas/motores como "tarjetas" en un contenedor efímero, aislado y sin red. |
| 2 | **Grafo de conocimiento cross-context** | `graph` | Extraer entidades/relaciones de búsquedas y resúmenes, y cruzarlas con **proyectos** del usuario para enriquecer el contexto. |
| 3 | **Caché neural de anticipación** | `anticipate` | Precomputar en local respuestas/resultados probables para reducir latencia percibida. |
| 4 | **Malla P2P soberana** | `mesh` | Federar instancias de confianza (allowlist + clave mutua) intercambiando fragmentos anonimizados, sin DHT. |
| 5 | **Martillohamer Sear Edition** | `martillohamer_sear` | Edición encubierta/análisis: modo *amnesic* (stealth), toolkit **forensics** offline y acceso **deepweb** por Tor. |

## Enfoque arquitectónico (Enfoque 2)

**"Pilares independientes sobre sustrato compartido".** En lugar de un motor
monolítico o de módulos acoplados, cada pilar es un paquete autónomo
(`searx/<pilar>/`) que:

- depende **solo** de la fundación transversal `searx/nextgen/` (contrato) y de
  los subsistemas ya existentes (`searx/memory`, `searx/resultcache`,
  `searx/metrics`);
- expone una fachada mínima (`is_enabled`, `recall`/`remember` u operaciones
  equivalentes) que **degrada siempre a no-op**;
- se activa/desactiva de forma independiente mediante su propio *kill-switch*.

Las dependencias son **unidireccionales** hacia los subsistemas existentes: un
pilar nunca importa a otro pilar.

## Fundación transversal — `searx/nextgen/`

Contrato mínimo compartido por todos los pilares. No contiene lógica de negocio.

### `searx/nextgen/contract.py`

```python
NEXTGEN_SCHEMA_VERSION: int = 1
PILLARS: frozenset[str] = frozenset({"graph", "anticipate", "sandbox", "mesh", "martillohamer_sear"})

def is_enabled(pillar: str | None = None) -> bool: ...   # global AND pillar
def normalize_key(value) -> str: ...                     # colapsa espacios + minúsculas
def node_id(kind: str, key: str) -> str: ...             # sha1[:16] determinista
def edge_id(src_id: str, relation: str, dst_id: str) -> str: ...
```

- **Kill-switch jerárquico** (ambos niveles requeridos):
  `search.nextgen.enabled` (global) **y** `search.nextgen.<pilar>.enabled`.
  Un pilar deshabilitado se comporta **exactamente** como si su código no
  existiera.
- **Identificadores deterministas**: `node_id`/`edge_id` son estables entre
  procesos (sha1 truncado a 16 hex con separador `\x1f`), lo que permite
  referenciar entidades/aristas sin coordinación.

### `searx/nextgen/__init__.py`

Fachada que reexporta el contrato.

## Orden de implementación (por riesgo)

El orden no es el orden numérico de los pilares sino el de **riesgo creciente**
(primero lo más contenido y de mayor valor inmediato):

1. **Pilar 2 — Grafo de conocimiento** (`graph`) — local, sin red, sin
   ejecución de código. **[IMPLEMENTADO]**
2. **Pilar 3 — Caché neural de anticipación** (`anticipate`) — local, sin red,
   sin ejecución de código. **[IMPLEMENTADO]**
3. **Pilar 1 — Search Cards ejecutables** (`sandbox`) — ejecuta código/CGI, el
   de mayor superficie de riesgo. **[IMPLEMENTADO]**
4. **Pilar 4 — Malla P2P soberana** (`mesh`) — comunicación entre hosts; el de
   mayor superficie de privacidad. **[IMPLEMENTADO]**
5. **Pilar 5 — Martillohamer Sear Edition** (`martillohamer_sear`) — edición
   encubierta/análisis: stealth (persistencia→RAM), forensics offline y deepweb
   por Tor (única salida de red, opt-in). **[IMPLEMENTADO]**

## Principios transversales

| Principio | Aplicación |
|---|---|
| **Opt-in / kill-switch** | Global + por pilar; deshabilitado por defecto. |
| **Fail-safe absoluto** | Todo punto de entrada envuelto en `try/except`; degrada a "sin efecto". |
| **Local-first** | Nada sale del host salvo el pilar 4, que es explícito y con allowlist. |
| **stdlib puro** | Sin `numpy`, `httpx` ni dependencias nuevas; HTTP con `urllib`. |
| **Tipado estricto** | Type hints + `if t.TYPE_CHECKING`; `py_compile` + `pyright` 0/0. |
| **Sin bloqueo del request** | El trabajo pesado va en hilos *daemon* con cola. |
| **Dependencias unidireccionales** | Pilar → fundación + subsistemas; nunca pilar → pilar. |

## Configuración — `search.nextgen`

```python
'nextgen': {
    'enabled': SettingsValue(bool, False),
    'graph': {
        'enabled': SettingsValue(bool, False),
        'extract_model': SettingsValue(str, ''),
        'max_triples': SettingsValue(int, 20),
        'max_rows': SettingsValue(int, 20000),
        'context_entities': SettingsValue(int, 5),
        'context_relations': SettingsValue(int, 6),
    },
    'anticipate': {'enabled': SettingsValue(bool, False)},
    'sandbox':    {'enabled': SettingsValue(bool, False)},
    'mesh':       {'enabled': SettingsValue(bool, False)},
    'martillohamer_sear': {
        'enabled': SettingsValue(bool, False),
        'stealth_mode': SettingsValue(bool, False),
        'tor_proxy': SettingsValue(str, 'socks5h://127.0.0.1:9050'),
        'timeout': SettingsValue(int, 15),
        'osint_dorks_enabled': SettingsValue(bool, True),
        'deepweb_engines': SettingsValue(list, ['ahmia', 'torch', 'haystak']),
    },
},
```

| Clave | Tipo | Defecto | Descripción |
|---|---|---|---|
| `nextgen.enabled` | bool | `false` | Interruptor global de toda la Fase C. |
| `nextgen.<pilar>.enabled` | bool | `false` | Interruptor por pilar. |
| `graph.extract_model` | str | `''` | Modelo LLM local para extracción (vacío → hereda del summarizer). |
| `graph.max_triples` | int | `20` | Máximo de tripletas por extracción. |
| `graph.max_rows` | int | `20000` | Tope FIFO de relaciones. |
| `graph.context_entities` | int | `5` | Máximo de entidades en el bloque de contexto. |
| `graph.context_relations` | int | `6` | Máximo de relaciones en el bloque de contexto. |
| `martillohamer_sear.stealth_mode` | bool | `false` | Tercera puerta del modo amnesic (además de `enabled` global y de pilar). |
| `martillohamer_sear.tor_proxy` | str | `socks5h://127.0.0.1:9050` | Proxy SOCKS5 de Tor para deepweb. |
| `martillohamer_sear.timeout` | int | `15` | Timeout (s) por petición deepweb. |
| `martillohamer_sear.osint_dorks_enabled` | bool | `true` | Catálogo de dorks del módulo forensics. |
| `martillohamer_sear.deepweb_engines` | list | `[ahmia, torch, haystak]` | Motores usados por `deepweb.search`. |

## Integración con subsistemas existentes

```
searx/summarizer/__init__.py::add_summary()
  │
  ├─ memory.recall(query)        ──► memory_block   (Fase A)
  ├─ knowledge_graph.recall(query) ─► graph_block    (Fase C · Pilar 2)
  ├─ context_block = "\n\n".join([memory_block, graph_block])
  ├─ _run_summarize(..., context_block)
  ├─ memory.remember(query, text, vector)            (Fase A)
  └─ knowledge_graph.remember(query, text)           (Fase C · Pilar 2)
```

- **Memoria (Fase A)** y **grafo (Fase C)** co-inyectan contexto; se concatenan
  con doble salto y cada uno es opcional e independiente.
- **Observabilidad (Fase B)**: los pilares pueden publicar métricas a través de
  `searx/metrics/nextgen.py` (DI), sin importar `searx.metrics` directamente.

## Privacidad

- **Pilares 1–3:** 100 % locales; ningún dato abandona el host.
- **Pilar 2:** el grafo y los proyectos viven en un SQLite local; la extracción
  usa **LLM local** (Ollama) con `local_only`, con fallback heurístico.
- **Pilar 3:** solo **precomputa en local**; nunca pre-consulta motores externos.
- **Pilar 4:** salida de red, explícita y gobernada por **allowlist
  estática** de peers + **clave simétrica HMAC-SHA256** + **fragmentos
  anonimizados**; sin DHT ni descubrimiento abierto. (Revisión: HMAC supersede la
  clave mutua Ed25519 original — ver sub-spec del Pilar 4.)
- **Pilar 5 (Martillohamer Sear):** `forensics` es 100 % offline; `deepweb` es la
  otra salida de red, **explícita (opt-in) y vía Tor**. El modo *stealth* reduce la
  huella de la **persistencia propia** (búfer RAM + purga); **no** anonimiza
  tráfico de terceros (para eso, operar Tor con `stealth_mode`).

## Sub-specs por pilar

| Pilar | Spec | Estado |
|---|---|---|
| 2 — Grafo | [`2026-10-15-knowledge-graph-design.md`](./2026-10-15-knowledge-graph-design.md) | Implementado |
| 3 — Anticipación | [`2026-10-15-cognitive-anticipation-design.md`](./2026-10-15-cognitive-anticipation-design.md) | Implementado |
| 1 — Sandbox | [`2026-10-15-executable-sandbox-design.md`](./2026-10-15-executable-sandbox-design.md) | Implementado |
| 4 — Malla P2P | [`2026-10-15-p2p-mesh-sync-design.md`](./2026-10-15-p2p-mesh-sync-design.md) | Implementado |
| 5 — Martillohamer Sear | [`2026-10-16-martillohamer-sear-edition-design.md`](./2026-10-16-martillohamer-sear-edition-design.md) | Implementado |

Cada pilar tendrá su propia spec con objetivos, interfaces, configuración,
manejo de errores, límites y plan de verificación.

## Estrategia de verificación transversal

- `py_compile` de todos los módulos nuevos/modificados.
- `pyright` (0 errores, 0 warnings) sobre `searx/nextgen/` y cada `searx/<pilar>/`.
- Prueba funcional reproducible por pilar (kill-switch, contrato, operaciones
  principales, degradación fail-safe), ejecutada en bucle sin *flakes*.
- Re-ejecución de las pruebas de Fase A/B para descartar regresiones.

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Un pilar rompe la búsqueda | Kill-switch doble + try/except total + degradación a no-op. |
| Fuga de datos del operador | Local-first en 1–3 y en forensics/stealth (5); allowlist + HMAC + anonimización en 4; deepweb (5) solo vía Tor. |
| Bloqueo del request | Hilos daemon con cola; nunca en el hilo de request. |
| Crecimiento ilimitado de estado | Topes FIFO (`max_rows`) en cada almacén. |
| Acoplamiento entre pilares | Dependencias unidireccionales a la fundación; sin imports pilar→pilar (el acoplamiento se hace vía integrador no-pilar). |
| Ejecución de código no confiable (Pilar 1) | Subproceso aislado (`-I`), timeout duro, entorno limpio, cwd temporal, límites de memoria (POSIX); **ejecución solo explícita**. |
| Tráfico saliente por deepweb (Pilar 5) | Opt-in (`martillohamer_sear.enabled`), **solo vía Tor**, fail-safe (sin Tor degrada, nunca rompe la búsqueda). |
| Retención de estado en RAM (Pilar 5) | Búfer volátil acotado (`_MAX_BUFFER_PER_NAMESPACE`) + purga en la salida del contexto. |
| Payload LLM inválido | `sanitize_messages` garantiza fin en rol `user`; degrada a mensaje neutro. |

## Decisiones registradas

- **Estructura:** spec maestra + una sub-spec por pilar.
- **Orden:** por riesgo → `graph` → `anticipate` → `sandbox` → `mesh` → `martillohamer_sear`.
- **Arquitectura:** Enfoque 2 (pilares independientes sobre sustrato compartido).
- **Fundación:** `searx/nextgen/` (contrato: versión, kill-switch, ids).
- **Kill-switch:** jerárquico (global **y** pilar), deshabilitado por defecto.
- **Pilar 1 (sandbox):** subproceso de **stdlib puro** (`subprocess`), **manual /
  ejecución solo explícita**, timeout duro, entorno limpio, cwd temporal, límites
  de memoria (POSIX). *(Revisión: supersede el contenedor Docker original — ver
  sub-spec del Pilar 1.)*
- **Pilar 2 (grafo):** extracción **LLM local + fallback heurístico**; proyectos
  **híbridos** (explícitos + clusters promovibles).
- **Pilar 3 (anticipación):** **solo precomputar en local**; nunca pre-consultar
  motores.
- **Pilar 4 (mesh):** **allowlist + clave simétrica HMAC-SHA256 + fragmentos
  anonimizados**; solo TCP; sync periódica; sin DHT. *(Revisión: HMAC supersede
  Ed25519 — ver sub-spec del Pilar 4.)*
- **Pilar 5 (Martillohamer Sear):** **stealth a nivel de aplicación** (persistencia
  propia → RAM volátil + purga de caché/métricas; **no** sandbox de kernel),
  **forensics offline** (dorks + IOCs, sin red) y **deepweb por Tor** (cliente
  SOCKS5 síncrono, sin allowlist en esta edición). Se añade `sanitize_messages`
  (garantizar fin en rol `user`) como guarda transversal para clientes LLM.
