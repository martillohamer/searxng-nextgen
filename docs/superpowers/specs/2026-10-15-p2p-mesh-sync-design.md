# Design: Malla P2P de sincronización (Pilar 4 · Fase C)

- **Fecha:** 2026-10-15
- **Proyecto:** searxng-nextgen
- **Fase:** C (Singularidad Cognitiva Distribuida) — Pilar 4
- **Spec maestra:** [`2026-10-15-distributed-cognitive-singularity-design.md`](./2026-10-15-distributed-cognitive-singularity-design.md)
- **Estado:** Aprobado; implementación pendiente

> **Nota de revisión de decisiones.** Esta sub-spec **supersede** la decisión
> registrada en la spec maestra para el Pilar 4 ("identidad **Ed25519**,
> *keypair* persistente, verificación de firma asimétrica"). Ed25519 requiere
> una dependencia no-stdlib, lo que rompe el principio de dependencias mínimas.
> Se acuerda **HMAC-SHA256** (clave simétrica compartida, stdlib puro) **sin
> cifrado**. La confidencialidad se delega en **fragmentos pre-anonimizados** y
> en la **allowlist** estática. Ver [Seguridad y honestidad](#seguridad-y-honestidad).

## Objetivo

Permitir que varias instancias de SearXNG-NextGen **intercambien fragmentos
anonimizados** de su grafo de conocimiento (Pilar 2) entre pares de confianza en
una red local o privada, de forma **firmada, verificable y no bloqueante**, para
converger conocimiento entre dispositivos del mismo usuario.

Características clave:

- **Opt-in y kill-switch doble**: deshabilitado por defecto
  (`search.nextgen.enabled` **y** `search.nextgen.mesh.enabled`).
- **Fragmentos opacos + inyección de dependencias**: el mesh **no conoce** el
  grafo ni la memoria; expone `set_fragment_source()`/`on_fragment()`. El
  cableado lo hace un integrador que **no es un pilar**.
- **stdlib puro**: `socket`, `hmac`, `hashlib`, `json`, `struct`, `threading`,
  `secrets`.
- **Solo TCP** con *framing* por longitud; sin UDP ni descubrimiento abierto.
- **Allowlist estática** de pares + **bind por defecto a loopback**.
- **Fail-safe absoluto**: cualquier error degrada a "sin sync"; la búsqueda
  continúa intacta.
- **Modular**: paquete `searx/mesh/` sobre la fundación `searx/nextgen/`.

## Alcance

**Dentro del alcance:**

- Sección `search.nextgen.mesh` en `searx/settings_defaults.py`.
- Paquete `searx/mesh/` con `protocol.py`, `transport.py` y la fachada
  `__init__.py`.
- Adiciones **aditivas** a `searx/graph/`: `export_fragments()` /
  `import_fragment()`.
- Integrador no-pilar `searx/nextgen_bridge.py::wire()` que conecta grafo↔malla.

**Fuera del alcance:**

- Cifrado del transporte (sin TLS) ni del payload (sin cifrado de fragmentos).
- Descubrimiento automático de pares (DHT/broadcast); la lista es explícita.
- Identidad asimétrica / PKI (sustituida por clave simétrica compartida).
- Sincronización en tiempo real; es **periódica** (polling).
- Sincronización de otros subsistemas (memoria, caché); solo grafo (Pilar 2).

## Arquitectura

```
 searx/nextgen_bridge.py::wire()          # integrador NO-pilar (puede importar ambos)
   │   si mesh.is_enabled() and graph.is_enabled():
   │       mesh.set_fragment_source(graph.export_fragments)
   │       mesh.on_fragment(graph.import_fragment)
   │       mesh.start()
   │
   ├─ hilo daemon "searx-mesh-server"     # transport.MeshServer.serve_forever()
   └─ hilo daemon "searx-mesh-worker"     # cada mesh.sync_interval s:
           para cada peer en mesh.peers:
               transport.send_sync(peer, key, node_id, source())
                   └─ respuesta verificada → on_fragment(frag) por fragmento

 searx/graph/store.py
   ├─ export_fragments(limit)   # relation → [{"s","r","t","w"}]  (solo hashes)
   └─ import_fragment(frag)     # refuerza relación local por (src_id, rel, dst_id)
```

Componentes:

- **`protocol`**: *framing*, firma/verificación HMAC y validación de mensajes.
- **`transport`**: cliente TCP (`send_sync`) y servidor TCP (`MeshServer`).
- **`__init__` (fachada)**: gate, worker de sync y ciclo de vida.
- **`graph` (aditivo)**: produce y aplica fragmentos anonimizados.
- **`nextgen_bridge` (integrador)**: único punto de acoplamiento grafo↔malla.

## Componentes e interfaces

### `searx/mesh/__init__.py` (fachada)

```python
__all__ = ["is_enabled", "set_fragment_source", "on_fragment",
           "start", "stop", "sync_now", "reset"]

def is_enabled() -> bool: ...                                    # nextgen AND mesh
def set_fragment_source(fn: Callable[[], list[dict]] | None) -> None: ...
def on_fragment(fn: Callable[[dict], None] | None) -> None: ...
def start() -> bool: ...                                         # arranca server+worker; False si no operable
def stop() -> None: ...
def sync_now() -> dict: ...                                      # {"peers","sent","received","applied","errors"}
def reset() -> None: ...                                         # utilidad para tests
```

- `is_enabled()` delega en `searx.nextgen.is_enabled("mesh")` (doble kill-switch).
- `node_id`: si `mesh.node_id == ""` se genera `secrets.token_hex(8)` **una vez**
  y se persiste en un fichero de estado (`searx_mesh_node_id` en el directorio de
  datos de SearXNG; fallback a `tempfile.gettempdir()`); se reutiliza en
  arranques posteriores.
- `start()` exige **clave HMAC** válida; si falta, registra y devuelve `False`
  (no-op). Con clave válida **arranca siempre el servidor** (para poder ser
  consultado) y arranca el worker, que es no-op si `mesh.peers` está vacío.
  Idempotente.
- `stop()` detiene server y worker de forma limpia (evento de parada).

### `searx/mesh/protocol.py` (nuevo)

```python
PROTOCOL_VERSION = 1
DEFAULT_CLOCK_SKEW_S = 300
DEFAULT_MAX_FRAGMENT_BYTES = 4096
DEFAULT_MAX_FRAGMENTS = 200

def canonical_bytes(message: dict) -> bytes: ...                 # json sort_keys, separators=(",",":")
def sign(payload: bytes, key: bytes) -> str: ...                 # hmac-sha256 hex
def verify(payload: bytes, signature: str, key: bytes) -> bool: ...  # compare_digest
def encode_message(message: dict, key: bytes) -> bytes: ...      # firma + frame
def decode_message(data: bytes, key: bytes) -> dict: ...         # unframe + verify + valida ts
def read_frame(sock: socket.socket, max_bytes: int) -> bytes | None: ...
def write_frame(sock: socket.socket, data: bytes) -> None: ...
def sanitize_fragment(fragment: dict) -> dict | None: ...
```

Mensaje:

```json
{"v": 1, "node_id": "<hex>", "kind": "sync_request" | "sync_response",
 "ts": 1750000000.0, "nonce": "<hex>", "fragments": [ ... ], "sig": "<hex>"}
```

Diseño:

- **Framing:** 4 bytes big-endian (largo) + `JSON` UTF-8. Cada frame ≤
  `max_fragment_bytes` acumulado + cabecera.
- **Canónico:** `json.dumps(sort_keys=True, separators=(",", ":"))` sobre el
  mensaje **sin** `sig`.
- **Firma:** `hmac.new(key, canonical, hashlib.sha256).hexdigest()`;
  verificación con `hmac.compare_digest` (tiempo constante).
- **Validación de `decode_message`:** versión soportada, firma válida,
  `|now - ts| ≤ clock_skew_s` (300 s), `nonce` no vacío, `fragments` lista con
  forma válida. Rechaza (lanza excepción interna) si algo falla; la fachada la
  captura.
- **`sanitize_fragment`:** admite solo claves `{"s","r","t","w"}` con tipos
  correctos y longitudes acotadas; descarta el resto.

### `searx/mesh/transport.py` (nuevo)

```python
class Peer(t.NamedTuple):
    node_id: str
    host: str
    port: int

def send_sync(peer: Peer, key: bytes, node_id: str,
              fragments: list[dict], *, timeout: float,
              max_fragment_bytes: int, max_fragments: int) -> list[dict]: ...

class MeshServer:
    def __init__(self, host: str, port: int, key: bytes, node_id: str,
                 fragment_source: Callable[[], list[dict]], allowlist: set[str]) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def serve_forever(self) -> None: ...
```

- **Cliente (`send_sync`):** `socket.create_connection((host, port), timeout)`;
  envía `sync_request` con los fragmentos locales (recortados a
  `max_fragments`); lee la respuesta, la verifica y devuelve sus `fragments`.
  Cualquier excepción → `[]` (fail-safe).
- **Servidor (`MeshServer`):** `socket(AF_INET, SOCK_STREAM)`,
  `SO_REUSEADDR`, `bind(host, port)`, `listen()`; bucle `accept()` en un hilo
  daemon (`searx-mesh-server`), un hilo corto por conexión con timeout. Verifica
  HMAC; comprueba que la **IP de origen está en `allowlist`** (fail-closed);
  responde con `sync_response` (fragmentos de `fragment_source()`).
- **`allowlist`:** conjunto de IPs derivado de `mesh.peers[].host`. Los pares
  deben declararse por **IP** (no por nombre) para que la comprobación de origen
  sea fiable; documentado.

### Adiciones a `searx/graph/` (aditivo, sin migración de esquema)

```python
# searx/graph/store.py
def export_fragments(self, limit: int = 200) -> list[dict]: ...
def import_fragment(self, fragment: dict) -> int: ...
```

- **`export_fragments(limit)`** — desde la tabla `relation`:
  `[{"s": src_id, "r": rel, "t": dst_id, "w": round(weight, 3)}]`, ordenado por
  `weight` desc. Los extremos son ya `sha1(kind + "\x1f" + key)[:16]`
  (`searx.nextgen.node_id`), de modo que **no sale texto crudo del host**.
- **`import_fragment(frag)`** — refuerza una **relación local existente** por
  `(src_id, rel, dst_id)`:
  `UPDATE relation SET weight = weight + min(w, _MESH_WEIGHT_CAP) WHERE src_id=? AND rel=? AND dst_id=?`
  (constante de módulo `_MESH_WEIGHT_CAP = 5.0`).
  **Nunca crea entidades con nombre** → la tabla `entity` (y por tanto el
  contexto de `recall()`) no se contamina. Devuelve el número de filas
  reforzadas. La operación es **idempotente y acotada** (el tope evita inflar
  pesos con reenvíos repetidos).
- `searx/graph/__init__.py` reexporta ambos.
- **Fuga residual aceptada y documentada:** el **predicado** `r` (≤60 caracteres,
  p. ej. `is_a`) viaja en claro; las identidades de entidad van hasheadas.

### `searx/nextgen_bridge.py` (integrador, NO es un pilar)

```python
def wire() -> bool: ...   # idempotente; conecta graph↔mesh si ambos habilitados
def stop() -> None: ...
```

- Importa `searx.graph` **y** `searx.mesh` (permitido: **no** es un pilar) y los
  conecta vía DI: `set_fragment_source(graph.export_fragments)`,
  `on_fragment(graph.import_fragment)`, `mesh.start()`.
- Invocado **una vez** desde el arranque de la app (`searx/webapp.py`).
- Respeta la regla de arquitectura "**nunca pilar→pilar**": ni `graph` importa
  `mesh` ni viceversa.

## Configuración

En `searx/settings_defaults.py`, bajo `search.nextgen.mesh`:

| Clave | Tipo | Defecto | Descripción |
|---|---|---|---|
| `enabled` | bool | `false` | Interruptor del pilar (requiere `nextgen.enabled`). |
| `node_id` | str | `''` | Identificador del nodo (vacío → autogenerado y persistido). |
| `listen_port` | int | `9876` | Puerto de escucha TCP. |
| `listen_host` | str | `'127.0.0.1'` | Interfaz de escucha (loopback por defecto). |
| `sync_interval` | int | `3600` | Periodo de sincronización (s). |
| `sync_timeout` | int | `5` | Timeout de conexión/lectura por par (s). |
| `peers` | list | `[]` | Lista de `{node_id, host, port}` de confianza (host = IP). |
| `max_fragment_bytes` | int | `4096` | Tope de bytes por frame/mensaje codificado. |
| `max_fragments_per_sync` | int | `200` | Tope de fragmentos por mensaje. |
| `key_file` | str | `''` | Ruta opcional a la clave HMAC (alternativa a la env). |

**Clave HMAC:** se lee de la variable de entorno `SEARXNG_MESH_KEY` (hex de 64
caracteres = 32 bytes); si está vacía, de `mesh.key_file`. **Nunca** se define en
`settings.yml`. Sin clave válida el pilar no opera.

## Seguridad y honestidad

- **Sin cifrado:** el tráfico va en claro sobre TCP. Debe operar en una red
  privada de confianza (LAN, VPN/mesh) — **no** exponer a Internet.
- **Autenticación:** HMAC-SHA256 con clave simétrica compartida; verificación en
  tiempo constante (`compare_digest`). Igualdad de clave ⟹ confianza mutua.
- **Autorización:** **allowlist** de IP de origen (fail-closed) + bind por
  defecto a `127.0.0.1`.
- **Confidencialidad:** fragmentos **pre-anonimizados** (identidades hasheadas);
  el predicado (`r`) sí se comparte. Sin nombres ni claves crudas.
- **Frescura:** ventana anti-replay por `ts` (±300 s) + `nonce`. No hay caché de
  nonces persistente (mitigación parcial; documentado).

## Manejo de errores

- **Aislamiento total**: `start`, `stop`, `sync_now`, `send_sync` y el worker
  envuelven en `try/except`; log a nivel `debug`; jamás propagan.
- **Peer inalcanzable / timeout / firma mala / origen no autorizado** → se
  descarta ese peer/fragmento; se incrementa `errors`; el resto continúa.
- **Sin clave** → `start()` devuelve `False` (no-op).
- **Fragmento inválido** → descartado por `sanitize_fragment` / `import`.

## Limitaciones conocidas

- **Sin cifrado** (transporte y payload); requiere red de confianza.
- **Anti-replay parcial**: ventana temporal + `nonce`, sin almacén persistente.
- **Sin descubrimiento**: pares declarados manualmente por IP.
- **Refuerzo, no fusión**: solo se refuerzan relaciones que **ya existen** en
  local; las desconocidas se cuentan y se descartan (no contaminan el grafo).
- **Predicados en claro** (fuga de metadatos aceptada).
- **Sin TLS/rotación de clave**: una clave compartida única.

## Testing / verificación

- `py_compile` (exit 0) y `pyright` (0 errores, 0 warnings) de `searx/mesh/`,
  `searx/graph/`, `searx/nextgen_bridge.py` y `searx/settings_defaults.py`.
- `protocol`: `encode_message`→`decode_message` ida y vuelta con clave correcta;
  firma alterada → rechazo; `ts` fuera de ventana → rechazo; frame malformado →
  rechazo; `sanitize_fragment` descarta claves/tipos inválidos.
- `transport`: cliente↔servidor locales (loopback) intercambian fragmentos;
  origen fuera de allowlist → rechazo; peer caído → `send_sync` devuelve `[]`.
- `graph`: `export_fragments` produce solo `{"s","r","t","w"}` sin nombres;
  `import_fragment` refuerza una relación existente y **no** crea entidades.
- Fachada: kill-switch apagado → `start()` no arranca; `sync_now()` devuelve
  ceros; `node_id` se genera y **persiste** entre llamadas.
- Integrador: `wire()` es idempotente y solo enlaza con **ambos** pilares
  habilitados.
- Prueba funcional `mesh_check.py`, ejecutada en bucle sin *flakes*.

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Interceptación del tráfico | Red de confianza + fragmentos anonimizados; sin exponer a Internet. |
| Suplantación de par | HMAC-SHA256 con clave compartida + allowlist de IP. |
| Replay de mensajes | Ventana temporal (`ts` ±300 s) + `nonce`. |
| Polución del grafo | `import_fragment` solo **refuerza** relaciones existentes; no crea entidades. |
| Inflado de pesos | Tope `_MESH_WEIGHT_CAP` por refuerzo; operación acotada. |
| Bloqueo por peer lento | Timeouts por operación + worker en hilo daemon. |
| Acoplamiento pilar→pilar | DI vía `nextgen_bridge`; `graph` y `mesh` no se importan entre sí. |
| Frame gigante / DoS | `max_fragment_bytes` + `max_fragments_per_sync` + timeout. |

## Decisiones registradas

- **Criptografía:** **HMAC-SHA256** simétrica (supersede Ed25519 de la spec
  maestra); **sin cifrado**; confianza mutua por clave compartida + allowlist.
- **Transporte:** **solo TCP** con *framing* por longitud; sin UDP ni
  descubrimiento.
- **Sync:** **periódica** en hilo daemon (`sync_interval`), no bloqueante.
- **Modelo de fragmento:** **opacos** (`{"s","r","t","w"}` anonimizados) con
  **DI** (`set_fragment_source`/`on_fragment`); sin pilar→pilar.
- **Integrador:** `searx/nextgen_bridge.py` (no-pilar), invocado en el arranque.
- **Clave:** env `SEARXNG_MESH_KEY` (± `mesh.key_file`); nunca en `settings.yml`.
- **Kill-switch:** jerárquico (`nextgen.enabled` **y** `mesh.enabled`).
