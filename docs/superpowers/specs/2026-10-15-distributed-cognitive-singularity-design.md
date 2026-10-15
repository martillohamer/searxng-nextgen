# Design maestro: Fase C — Singularidad Cognitiva Distribuida

- **Fecha:** 2026-10-15
- **Proyecto:** searxng-nextgen
- **Fase:** C (Singularidad Cognitiva Distribuida)
- **Estado:** Aprobado en arquitectura; Pilar 2 implementado; Pilares 1/3/4 pendientes
- **Specs por pilar:** ver [Sub-specs](#sub-specs-por-pilar)

## Objetivo

Llevar SearXNG-NextGen de un buscador privado con memoria a un **sustrato
cognitivo local** que aprende del uso del operador. La Fase C se compone de
**cuatro pilares independientes pero relacionados** que comparten una fundación
mínima común. Ninguno de ellos debe poder romper la búsqueda, ningún dato debe
salir del host por defecto y todos son **opt-in**.

## Los cuatro pilares

| # | Pilar | Identificador | Propósito |
|---|---|---|---|
| 1 | **Search Cards ejecutables** | `sandbox` | Ejecutar consultas/motores como "tarjetas" en un contenedor efímero, aislado y sin red. |
| 2 | **Grafo de conocimiento cross-context** | `graph` | Extraer entidades/relaciones de búsquedas y resúmenes, y cruzarlas con **proyectos** del usuario para enriquecer el contexto. |
| 3 | **Caché neural de anticipación** | `anticipate` | Precomputar en local respuestas/resultados probables para reducir latencia percibida. |
| 4 | **Malla P2P soberana** | `mesh` | Federar instancias de confianza (allowlist + clave mutua) intercambiando fragmentos anonimizados, sin DHT. |

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
PILLARS: frozenset[str] = frozenset({"graph", "anticipate", "sandbox", "mesh"})

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
   sin ejecución de código.
3. **Pilar 1 — Search Cards ejecutables** (`sandbox`) — ejecuta código/CGI, el
   de mayor superficie de riesgo.
4. **Pilar 4 — Malla P2P soberana** (`mesh`) — comunicación entre hosts; el de
   mayor superficie de privacidad.

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
- **Pilar 4:** única salida de red, explícita y gobernada por **allowlist
  estática** de peers + **clave mutua Ed25519** + **fragmentos anonimizados**;
  sin DHT ni descubrimiento abierto.

## Sub-specs por pilar

| Pilar | Spec | Estado |
|---|---|---|
| 2 — Grafo | [`2026-10-15-knowledge-graph-design.md`](./2026-10-15-knowledge-graph-design.md) | Implementado |
| 3 — Anticipación | *(pendiente)* | No iniciado |
| 1 — Sandbox | *(pendiente)* | No iniciado |
| 4 — Malla P2P | *(pendiente)* | No iniciado |

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
| Fuga de datos del operador | Local-first en 1–3; allowlist + Ed25519 + anonimización en 4. |
| Bloqueo del request | Hilos daemon con cola; nunca en el hilo de request. |
| Crecimiento ilimitado de estado | Topes FIFO (`max_rows`) en cada almacén. |
| Acoplamiento entre pilares | Dependencias unidireccionales a la fundación; sin imports pilar→pilar. |
| Ejecución de código no confiable (Pilar 1) | Contenedor efímero, sin red, FS read-only + tmpfs, no-root, límites, **manual**. |

## Decisiones registradas

- **Estructura:** spec maestra + una sub-spec por pilar.
- **Orden:** por riesgo → `graph` → `anticipate` → `sandbox` → `mesh`.
- **Arquitectura:** Enfoque 2 (pilares independientes sobre sustrato compartido).
- **Fundación:** `searx/nextgen/` (contrato: versión, kill-switch, ids).
- **Kill-switch:** jerárquico (global **y** pilar), deshabilitado por defecto.
- **Pilar 1 (sandbox):** contenedor Docker efímero, **manual**, sin red, read-only
  + tmpfs, no-root, límites CPU/RAM/tiempo; sin fallback a `subprocess`.
- **Pilar 2 (grafo):** extracción **LLM local + fallback heurístico**; proyectos
  **híbridos** (explícitos + clusters promovibles).
- **Pilar 3 (anticipación):** **solo precomputar en local**; nunca pre-consultar
  motores.
- **Pilar 4 (mesh):** **allowlist + clave mutua + fragmentos anonimizados**; sin
  DHT.
