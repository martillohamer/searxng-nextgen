# Design: Search Cards ejecutables / Sandbox (Pilar 1 · Fase C)

- **Fecha:** 2026-10-15
- **Proyecto:** searxng-nextgen
- **Fase:** C (Singularidad Cognitiva Distribuida) — Pilar 1
- **Spec maestra:** [`2026-10-15-distributed-cognitive-singularity-design.md`](./2026-10-15-distributed-cognitive-singularity-design.md)
- **Estado:** Aprobado; implementación pendiente

> **Nota de revisión de decisiones.** Esta sub-spec **supersede** la decisión
> registrada en la spec maestra para el Pilar 1 ("contenedor Docker efímero,
> manual, sin red, read-only + tmpfs, no-root, límites CPU/RAM/tiempo; **sin
> fallback a `subprocess`**"). Tras revisión de arquitectura se acuerda un
> ejecutor **`subprocess` de stdlib puro**, coherente con el principio de
> dependencias mínimas del proyecto. Las garantías son **best-effort y
> honestas** (ver [Seguridad](#seguridad-y-honestidad) y
> [Limitaciones](#limitaciones-conocidas)); no equivalen al aislamiento de un
> contenedor.

## Objetivo

Permitir que el operador ejecute, **de forma explícita y bajo demanda**,
**"tarjetas ejecutables"** (bloques de código o expresiones Python) que aparecen
en resúmenes o texto de resultados, en un subproceso efímero con **timeout
duro**, **límites de memoria (POSIX)**, **entorno limpio** y **directorio de
trabajo temporal**. El valor es ejecutar snippets sugeridos por el motor de
resumen sin comprometer el proceso de búsqueda.

Características clave:

- **Opt-in y kill-switch doble**: deshabilitado por defecto
  (`search.nextgen.enabled` **y** `search.nextgen.sandbox.enabled`).
- **Gate obligatorio**: la detección es pasiva; la **ejecución solo ocurre por
  una llamada explícita** del operador (CLI/endpoint). **Nunca auto-ejecución.**
- **Solo Python** en esta entrega (el propio intérprete del proceso).
- **stdlib puro**: `subprocess`, `ast`, `re`, `os`, `resource` (POSIX).
- **Fail-safe absoluto**: ninguna función propaga excepciones; degradan a
  resultado vacío / no-op.
- **Modular**: paquete `searx/sandbox/` sobre la fundación `searx/nextgen/`.

## Alcance

**Dentro del alcance:**

- Sección `search.nextgen.sandbox` en `searx/settings_defaults.py`.
- Paquete `searx/sandbox/` con `cards.py`, `runner.py` y la fachada `__init__.py`.
- Detección pasiva de tarjetas y ejecución explícita bajo gate.

**Fuera del alcance:**

- Auto-ejecución al generar un resumen (descartado por riesgo).
- Lenguajes distintos de Python (shell, JS, etc.).
- Aislamiento tipo contenedor (Docker/namespaces/seccomp).
- Bloqueo real de red del subproceso (no garantizable con stdlib puro; ver
  [Seguridad](#seguridad-y-honestidad)).
- UI de aprobación; el gate es la llamada explícita a la API.

## Arquitectura

```
 Operador (CLI / endpoint)
   │
   ├─ sandbox.detect_cards(texto)          # pasivo, sin efectos, siempre permitido
   │       └─ regex de bloques ```python```  →  Card(language, code, kind, index)
   │       └─ kind = "expression" | "block"  (vía ast.parse, sin ejecutar)
   │
   └─ sandbox.execute_card(code, language)  # GATE: solo llamada explícita
           └─ is_enabled()?  ── no ──► None (no-op)
           └─ is_supported_language()? ── no ──► ExecutionResult(ok=False, error=...)
           └─ runner.run(code, timeout, memory_limit_mb)
                   └─ subprocess.run([sys.executable, "-I", "-B", "-c", code],
                                     cwd=<tmp>, env=_clean_env(),
                                     preexec_fn=_preexec(mem)  # POSIX
                                     capture_output=True, timeout=timeout)
                   └─ ExecutionResult(ok, stdout, stderr, returncode,
                                      duration_ms, timed_out, error)
```

Componentes:

- **`cards`**: parsea texto y produce tarjetas candidatas. No ejecuta nada.
- **`runner`**: ejecuta un snippet en subproceso aislado con límites.
- **`__init__` (fachada)**: gate de kill-switch, validación de lenguaje y
  fail-safe.

## Componentes e interfaces

### `searx/sandbox/__init__.py` (fachada)

```python
__all__ = ["is_enabled", "detect_cards", "execute_card", "reset", "Card", "ExecutionResult"]

def is_enabled() -> bool: ...                                    # nextgen AND sandbox
def detect_cards(text: str, max_cards: int = 8) -> list[Card]: ...  # pasivo; siempre permitido
def execute_card(code: str, language: str = "python") -> ExecutionResult | None: ...  # gate
def reset() -> None: ...                                         # utilidad para tests
```

- `is_enabled()` delega en `searx.nextgen.is_enabled("sandbox")` (aplica el
  doble kill-switch).
- `detect_cards()` es **puro** (solo lee texto) y puede llamarse aunque el pilar
  esté deshabilitado; nunca ejecuta.
- `execute_card()` es el **único** punto de ejecución y **solo se invoca de
  forma explícita**. Devuelve `None` si el pilar está deshabilitado.

### `searx/sandbox/cards.py` (nuevo)

```python
_SUPPORTED: frozenset[str] = frozenset({"python", "py"})
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[^\n]*\n(.*?)```", re.DOTALL)

@dataclass(frozen=True)
class Card:
    language: str
    code: str
    kind: str      # "expression" | "block"
    index: int

def is_supported_language(language: str) -> bool: ...
def detect_cards(text: str, max_cards: int = 8) -> list[Card]: ...
def _classify(code: str) -> str: ...   # ast.parse(code, mode="eval") → "expression"
```

- `detect_cards()` recorre bloques cercados; conserva los que tienen **info
  string soportado** (`python`/`py`) y código no vacío.
- `_classify()` usa `ast.parse(code, mode="eval")`: si compila como expresión
  → `"expression"`; en caso contrario → `"block"`. **No evalúa**, solo parsea.
- Tope `max_cards` (defecto 8) para acotar el trabajo.
- Nunca lanza: cualquier error → lista vacía.

### `searx/sandbox/runner.py` (nuevo)

```python
_DEFAULT_MAX_OUTPUT_BYTES: int = 65536
_KEEP_ENV: frozenset[str] = frozenset({"PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL"})

@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    duration_ms: int
    timed_out: bool
    error: str | None

def run(code: str, *, language: str = "python", timeout: float,
        memory_limit_mb: int, max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES) -> ExecutionResult: ...
def _clean_env() -> dict[str, str]: ...
def _preexec(memory_limit_mb: int) -> Callable[[], None] | None: ...  # POSIX; None en Windows
```

- **Invocación:** `[sys.executable, "-I", "-B", "-c", code]`.
  - `-I` = *isolated*: ignora variables `PYTHON*`, sin *user site-packages* y sin
    el directorio actual en `sys.path`.
  - `-B` = no escribe `.pyc`.
- **Directorio:** `cwd = tempfile.mkdtemp(prefix="searx_sandbox_")`; se elimina en
  un bloque `finally` (best-effort).
- **Entorno limpio:** `_clean_env()` conserva solo `_KEEP_ENV` (y elimina
  cualquier `*_PROXY`/`PYTHON*`). Evita que el subproceso herede credenciales o
  proxies del proceso padre.
- **Timeout:** `subprocess.run(..., timeout=timeout)`; `TimeoutExpired` →
  `ok=False`, `timed_out=True` (el subproceso ya se ha terminado por
  `subprocess`). Timeout por defecto `sandbox.timeout` (5 s).
- **Memoria:** en POSIX, `preexec_fn` fija `resource.setrlimit(RLIMIT_AS, ...)`
  a `memory_limit_mb` MiB y `RLIMIT_CPU` al `timeout` (+ margen). En **Windows
  no aplica** (`_preexec` devuelve `None`) → límite **no aplicado** (documentado).
- **Salida:** `stdout`/`stderr` truncados a `max_output_bytes` con marca de
  truncamiento; decodificación `errors="replace"`.
- **Nunca lanza:** todo va en `try/except`; devuelve siempre un
  `ExecutionResult`.

## Configuración

En `searx/settings_defaults.py`, bajo `search.nextgen.sandbox`:

| Clave | Tipo | Defecto | Descripción |
|---|---|---|---|
| `enabled` | bool | `false` | Interruptor del pilar (requiere `nextgen.enabled`). |
| `timeout` | int | `5` | Timeout duro de ejecución en segundos. |
| `memory_limit_mb` | int | `128` | Tope de memoria (MiB) — **solo POSIX**. |

## Seguridad y honestidad

Este pilar es **aislamiento de conveniencia, NO una sandbox adversarial**. Se
documenta con precisión para que nadie asuma garantías que no existen:

- **Timeout:** garantizado en todas las plataformas (lo impone `subprocess`).
- **Memoria:** aplicada vía `RLIMIT_AS` **solo en POSIX**; en Windows **no**.
- **Red:** **no se bloquea la salida a red**. Un snippet **sí puede** abrir
  sockets. No se promete lo contrario.
- **Sistema de ficheros:** el snippet corre como el usuario del proceso; el
  `cwd` temporal y el entorno limpio **reducen** el riesgo, pero **no** confinan
  el acceso al FS.
- **Mitigaciones reales:** opt-in explícito, **ejecución solo por acción
  explícita** (jamás automática), solo Python, timeout duro, entorno limpio,
  cwd temporal y salida acotada.

## Manejo de errores

- **Aislamiento total**: `detect_cards()` y `execute_card()` envuelven en
  `try/except`; log a nivel `debug`; jamás propagan excepciones.
- **Pilar deshabilitado** → `execute_card()` devuelve `None` (no-op exacto).
- **Lenguaje no soportado** → `ExecutionResult(ok=False, error="unsupported
  language")` sin ejecutar nada.
- **Código vacío** → `ExecutionResult(ok=False, error="empty code")`.
- **Timeout / OOM / salida anómala** → resultado con `ok=False` y `error`
  descriptivo; el proceso principal continúa intacto.

## Limitaciones conocidas

- Sin bloqueo de red ni de FS (ver [Seguridad](#seguridad-y-honestidad)).
- Límite de memoria efectivo solo en POSIX.
- Solo Python.
- La detección es heurística (regex de bloques cercados); puede no capturar
  formatos alternativos.
- Sin sandbox de sistema (contenedor/namespace); si se requiere aislamiento
  fuerte, esto no sustituye a ejecutar el código en una VM/contenedor externo.

## Testing / verificación

- `py_compile` (exit 0) y `pyright` (0 errores, 0 warnings) de `searx/sandbox/`
  y `searx/settings_defaults.py`.
- `cards`: detecta bloques `python`/`py`; ignora otros lenguajes; clasifica
  `1 + 1` como `"expression"` y `x = 1\nprint(x)` como `"block"`; respeta
  `max_cards`.
- `runner`: `print(1 + 1)` → `ok=True`, `stdout` contiene `2`, `returncode=0`;
  bucle infinito → `timed_out=True`, `ok=False`; código con excepción →
  `returncode != 0`, `ok=False`; el entorno del hijo no hereda `*_PROXY`
  (verificado imprimiendo `os.environ`).
- Fachada: kill-switch apagado → `execute_card()` `None`; lenguaje no soportado
  → error seguro; **la detección funciona con el pilar apagado**.
- Prueba funcional `sandbox_check.py`, ejecutada en bucle sin *flakes*.

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Ejecutar código no confiable | Opt-in + **ejecución solo explícita** + timeout duro + entorno limpio + cwd temporal. |
| Bucle infinito / consumo de CPU | Timeout duro (`timeout`) + `RLIMIT_CPU` (POSIX). |
| Consumo de memoria | `RLIMIT_AS` (POSIX); en Windows documentado como no aplicado. |
| Fuga por entorno/proxies | `_clean_env()` elimina `PYTHON*` y `*_PROXY`. |
| Salida gigante | Truncado a `max_output_bytes`. |
| Sobrecarga por detección | `max_cards` + regex acotada; detección O(n) sobre el texto. |

## Decisiones registradas

- **Mecanismo:** `subprocess` de **stdlib puro** (supersede Docker de la spec
  maestra); sin fallback a nada más.
- **Gate:** **ejecución solo por acción explícita**; la detección es pasiva.
- **Lenguajes:** **solo Python** en esta entrega.
- **Aislamiento:** timeout universal; memoria solo POSIX; **sin** bloqueo de red
  (documentado explícitamente).
- **Fachada:** `is_enabled`, `detect_cards`, `execute_card`, `reset`.
- **Config:** `search.nextgen.sandbox.{enabled, timeout, memory_limit_mb}`.
- **Kill-switch:** jerárquico (`nextgen.enabled` **y** `sandbox.enabled`).
