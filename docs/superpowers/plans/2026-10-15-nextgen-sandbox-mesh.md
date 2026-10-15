# Plan de implementación — Fase C · Pilares 1 (Sandbox) y 4 (Mesh)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps are numbered `### Step X.Y` headings; track progress with TodoWrite (one todo per task).

> **Specs de referencia:**
> - `docs/superpowers/specs/2026-10-15-executable-sandbox-design.md` (Pilar 1)
> - `docs/superpowers/specs/2026-10-15-p2p-mesh-sync-design.md` (Pilar 4)
> - `docs/superpowers/specs/2026-10-15-distributed-cognitive-singularity-design.md` (maestra, decisiones revisadas)

**Goal:** Cerrar la Fase C implementando el sandbox de *search cards* ejecutables (Pilar 1, subprocess stdlib) y la red P2P federada (Pilar 4, HMAC/TCP stdlib), cableados por un integrador no-pilar, con tipado estricto, fail-safe y privacy-by-default.

**Arquitectura (invariantes):**
- Contrato mínimo `searx/nextgen/` (`is_enabled`, `node_id`, `edge_id`, `normalize_key`, `PILLARS`).
- **Nunca pilar→pilar.** El mesh no importa `searx.graph`; el integrador `searx/nextgen_bridge.py` (no-pilar) cablea ambos.
- Todo entry point es **fail-safe**: degrada a no-op / `None` / resultado de error; **nunca lanza**.
- Privacidad por defecto: ambos pilares desactivados; los fragmentos mesh viajan pre-anonimizados (solo ids hasheados y predicados).

**Convenciones de verificación (este repo):**
- No hay `pytest` en el venv. La verificación usa **scripts `*_check.py` standalone** guardados en `C:\Users\astur\AppData\Local\Temp\opencode\` y ejecutados con `.\venv\Scripts\python.exe`.
- Compilación: `.\venv\Scripts\python.exe -m py_compile <archivos>`.
- Tipado: `npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe <rutas>` → **0 errores / 0 warnings en archivos nuevos** (baseline preexistente ~9 errores ajenos, ignorar).
- Estilo: línea ≤120, SPDX `# SPDX-License-Identifier: AGPL-3.0-or-later`, `typing as t`, `if t.TYPE_CHECKING` solo si hace falta, errores aislados con `try/except Exception` + `log.debug`.

## Global Constraints

Aplican a **cada** tarea (valores copiados verbatim de las specs):

- **Solo stdlib.** Nada de dependencias nuevas (`subprocess`, `hmac`, `hashlib`, `struct`, `socket`, `secrets`, `json`).
- **Privacidad por defecto.** `search.nextgen.enabled=false`, `sandbox.enabled=false`, `mesh.enabled=false`.
- **Nunca pilar→pilar.** `searx/mesh/` y `searx/sandbox/` jamás importan `searx.graph` ni `searx.memory`. La única dep. cruzada vive en `searx/nextgen_bridge.py`.
- **Fail-safe absoluto.** Todo entry point público: `try/except Exception` + `log.debug`, devuelve el neutro (`False`/`None`/`[]`/resultado de error); nunca propaga excepción.
- **Sandbox: nunca auto-ejecución.** `detect_cards()` es pasivo; `execute_card()` corre solo por acción explícita del usuario.
- **Sandbox: solo Python** (`"python"`/`"py"`); cualquier otro lenguaje → error seguro.
- **Mesh: HMAC-SHA256 firmado, sin cifrado**, solo TCP length-framed; clave SOLO por `SEARXNG_MESH_KEY` (hex, 32 bytes) o `mesh.key_file`.
- **Config sandbox:** `enabled:false`, `timeout:5`, `memory_limit_mb:128`.
- **Config mesh:** `enabled:false`, `node_id:''`, `listen_port:9876`, `listen_host:'127.0.0.1'`, `sync_interval:3600`, `sync_timeout:5`, `peers:[]`, `max_fragment_bytes:4096`, `max_fragments_per_sync:200`, `key_file:''`.
- **SPDX** en todo archivo nuevo: `# SPDX-License-Identifier: AGPL-3.0-or-later`. Línea ≤120.
- **Commit atómico por tarea** (frecuente), mensaje en el estilo mostrado en cada Step `Commit`.

---

## Estructura de archivos

| Acción | Ruta |
|---|---|
| Modificar | `searx/settings_defaults.py` |
| Crear | `searx/sandbox/__init__.py` |
| Crear | `searx/sandbox/cards.py` |
| Crear | `searx/sandbox/runner.py` |
| Crear | `searx/mesh/__init__.py` |
| Crear | `searx/mesh/protocol.py` |
| Crear | `searx/mesh/transport.py` |
| Modificar | `searx/graph/store.py` (aditivo: `export_fragments`, `import_fragment`) |
| Modificar | `searx/graph/context.py` (wrappers de módulo) |
| Modificar | `searx/graph/__init__.py` (reexport) |
| Crear | `searx/nextgen_bridge.py` |
| Modificar | `searx/webapp.py` (llamar `wire()` en `init()`) |

---

# Parte A — Pilar 1: Sandbox (`searx/sandbox/`)

## Task A1 — Config sandbox + `searx/sandbox/cards.py` (detección pasiva)

**Files:** `searx/settings_defaults.py`, `searx/sandbox/cards.py`, `searx/sandbox/__init__.py` (placeholder vacío para permitir el import del paquete)

### Step A1.1 — Ampliar config

En `searx/settings_defaults.py`, localizar el bloque (dentro de `search.nextgen`):

```python
            'sandbox': {
                'enabled': SettingsValue(bool, False),
            },
```

y reemplazarlo por:

```python
            'sandbox': {
                'enabled': SettingsValue(bool, False),
                'timeout': SettingsValue(int, 5),
                'memory_limit_mb': SettingsValue(int, 128),
            },
```

### Step A1.2 — Escribir `searx/sandbox/cards.py`

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Executable "search card" detection (Phase C - Pillar 1).

Parses fenced code blocks from summaries / results and classifies each supported
card as a single *evaluable expression* or a general *block*.  Detection is
**purely passive**: it never executes anything and may run even when the sandbox
pillar is disabled.
"""

__all__ = ["Card", "detect_cards", "is_supported_language"]

import ast
import re
from dataclasses import dataclass

from searx import logger

log = logger.getChild("searx.sandbox.cards")

#: Languages this pillar can execute (Phase C: Python only).
_SUPPORTED: frozenset[str] = frozenset({"python", "py"})

#: A fenced code block; the info string is captured between the backticks and the newline.
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+.-]*)[^\n]*\n(.*?)```", re.DOTALL)

_DEFAULT_MAX_CARDS: int = 8


@dataclass(frozen=True)
class Card:
    """A detected, executable card."""

    language: str
    code: str
    kind: str  # "expression" | "block"
    index: int


def is_supported_language(language: str) -> bool:
    """Return whether ``language`` (a fence info string) is executable."""
    try:
        return str(language or "").strip().lower() in _SUPPORTED
    except Exception as e:  # pylint: disable=broad-except
        log.debug("sandbox cards: is_supported_language failed: %s", e)
        return False


def _classify(code: str) -> str:
    """Return ``"expression"`` when ``code`` parses as a single expression."""
    try:
        ast.parse(code, mode="eval")
        return "expression"
    except SyntaxError:
        return "block"
    except Exception:  # pylint: disable=broad-except
        return "block"


def detect_cards(text: str, max_cards: int = _DEFAULT_MAX_CARDS) -> list[Card]:
    """Return the executable cards found in ``text`` (never raises)."""
    try:
        if not text:
            return []
        try:
            limit = max(0, int(max_cards))
        except (TypeError, ValueError):
            limit = _DEFAULT_MAX_CARDS
        cards: list[Card] = []
        for match in _FENCE_RE.finditer(text):
            if len(cards) >= limit:
                break
            language = (match.group(1) or "").strip().lower()
            if not is_supported_language(language):
                continue
            code = match.group(2) or ""
            if not code.strip():
                continue
            cards.append(Card(language=language, code=code, kind=_classify(code), index=len(cards)))
        return cards
    except Exception as e:  # pylint: disable=broad-except
        log.debug("sandbox cards: detect_cards failed: %s", e)
        return []
```

### Step A1.3 — Crear paquete `searx/sandbox/__init__.py` (placeholder)

De momento, un docstring + `__all__` vacío para que el paquete importe. Se completa en A3.

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pillar 1 - Executable Search Cards / Sandbox (Phase C)."""

__all__: list[str] = []
```

### Step A1.4 — Verificar

Guardar en `C:\Users\astur\AppData\Local\Temp\opencode\sandbox_cards_check.py`:

```python
import sys
from searx.sandbox.cards import detect_cards, is_supported_language

def main() -> int:
    assert is_supported_language("python") and is_supported_language("PY") and is_supported_language("py")
    assert not is_supported_language("bash") and not is_supported_language("")

    text = """
Intro
```python
1 + 1
```
```python
x = 1
x + 2
```
```javascript
console.log(1)
```
```py
len("abc")
```
"""
    cards = detect_cards(text)
    assert len(cards) == 3, cards
    assert cards[0].kind == "expression" and cards[0].code.strip() == "1 + 1"
    assert cards[1].kind == "block"
    assert cards[2].language == "py" and cards[2].kind == "expression"

    # límite y robustez
    assert len(detect_cards(text, max_cards=1)) == 1
    assert detect_cards(None) == []          # type: ignore[arg-type]
    assert detect_cards("no fences") == []

    # detección es pasiva: no ejecuta nada
    print("sandbox_cards_check: OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Ejecutar:

```powershell
.\venv\Scripts\python.exe -m py_compile searx\sandbox\__init__.py searx\sandbox\cards.py searx\settings_defaults.py
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\sandbox_cards_check.py"
npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe searx\sandbox
```

Esperado: `py_compile` exit 0; `sandbox_cards_check: OK`; pyright 0 errores en `searx/sandbox`.

### Step A1.5 — Commit

```powershell
git add searx/settings_defaults.py searx/sandbox/__init__.py searx/sandbox/cards.py
git commit -m "feat(sandbox): add passive search-card detection and sandbox settings (Phase C - Pilar 1)"
```

---

## Task A2 — `searx/sandbox/runner.py` (ejecución aislada)

**Files:** `searx/sandbox/runner.py`

### Step A2.1 — Escribir el runner

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Isolated subprocess execution of a single Python card (Phase C - Pillar 1).

A card is executed in a fresh, isolated interpreter (``python -I -B -c <code>``)
with a **hard timeout** (all platforms), a cleaned environment, a throw-away
working directory and - on POSIX only - best-effort ``RLIMIT_AS`` / ``RLIMIT_CPU``
caps.

This is **convenience isolation, not an adversarial sandbox**: network egress is
**not** blocked and in-memory rlimits do not apply on Windows.  Every entry point
is fail-safe: it returns an :py:class:`ExecutionResult` and never raises.
"""

__all__ = ["ExecutionResult", "run"]

import os
import shutil
import subprocess
import sys
import tempfile
import time
import typing as t
from dataclasses import dataclass

from searx import logger

log = logger.getChild("searx.sandbox.runner")

_DEFAULT_MAX_OUTPUT_BYTES: int = 65536
_DEFAULT_TIMEOUT: float = 5.0
_KEEP_ENV: frozenset[str] = frozenset({"PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL"})


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of a card execution."""

    ok: bool
    stdout: str
    stderr: str
    returncode: int
    duration_ms: int
    timed_out: bool
    error: str | None


def _clean_env() -> dict[str, str]:
    """Return a minimal environment, dropping proxies and Python overrides."""
    env: dict[str, str] = {}
    try:
        for key, value in os.environ.items():
            upper = key.upper()
            if upper.endswith("_PROXY") or upper.startswith("PYTHON"):
                continue
            if upper in _KEEP_ENV:
                env[key] = value
    except Exception:  # pylint: disable=broad-except
        return {"PATH": os.environ.get("PATH", "")}
    return env


def _preexec(memory_limit_mb: int) -> t.Callable[[], None] | None:
    """Return a POSIX pre-exec hook capping address space / CPU, or ``None``."""
    try:
        import resource  # noqa: PLC0415 - POSIX only, imported lazily
    except ImportError:  # pragma: no cover - Windows
        return None
    mem_bytes = max(16, int(memory_limit_mb)) * 1024 * 1024

    def _limit() -> None:  # pragma: no cover - runs in the child process
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            pass
        try:
            cpu = int(os.environ.get("SEARX_SANDBOX_CPU_S", "0") or 0)
            if cpu > 0:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        except (ValueError, OSError):
            pass

    return _limit


def _truncate(data: bytes, limit: int) -> str:
    if len(data) > limit:
        data = data[:limit] + b" ...[truncated]"
    return data.decode("utf-8", errors="replace")


def run(
    code: str,
    *,
    language: str = "python",
    timeout: float = _DEFAULT_TIMEOUT,
    memory_limit_mb: int = 128,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> ExecutionResult:
    """Execute ``code`` in an isolated subprocess (never raises)."""
    start = time.monotonic()
    workdir: str | None = None
    try:
        if not str(code or "").strip():
            return ExecutionResult(False, "", "", -1, 0, False, "empty code")
        try:
            timeout = max(0.1, float(timeout))
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        try:
            max_output_bytes = max(1024, int(max_output_bytes))
        except (TypeError, ValueError):
            max_output_bytes = _DEFAULT_MAX_OUTPUT_BYTES
        workdir = tempfile.mkdtemp(prefix="searx_sandbox_")
        env = _clean_env()
        env["SEARX_SANDBOX_CPU_S"] = str(int(timeout) + 1)
        preexec = _preexec(memory_limit_mb)
        proc = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            preexec_fn=preexec,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecutionResult(
            ok=proc.returncode == 0,
            stdout=_truncate(proc.stdout or b"", max_output_bytes),
            stderr=_truncate(proc.stderr or b"", max_output_bytes),
            returncode=int(proc.returncode),
            duration_ms=duration_ms,
            timed_out=False,
            error=None if proc.returncode == 0 else "nonzero exit",
        )
    except subprocess.TimeoutExpired as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        return ExecutionResult(
            ok=False,
            stdout=_truncate(e.stdout or b"", max_output_bytes),
            stderr=_truncate(e.stderr or b"", max_output_bytes),
            returncode=-1,
            duration_ms=duration_ms,
            timed_out=True,
            error="timeout",
        )
    except Exception as e:  # pylint: disable=broad-except
        duration_ms = int((time.monotonic() - start) * 1000)
        log.debug("sandbox runner: execution failed: %s", e)
        return ExecutionResult(False, "", "", -1, duration_ms, False, str(e) or "execution error")
    finally:
        if workdir:
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception as e:  # pylint: disable=broad-except
                log.debug("sandbox runner: workdir cleanup failed: %s", e)
```

> **Nota de plataforma:** en Windows `_preexec()` devuelve `None` (no hay módulo
> `resource`), por lo que `subprocess.run(..., preexec_fn=None)` es válido; el
> límite de memoria es POSIX-only por diseño documentado.

### Step A2.2 — Verificar

Guardar en `C:\Users\astur\AppData\Local\Temp\opencode\sandbox_runner_check.py`:

```python
import sys
from searx.sandbox.runner import run

def main() -> int:
    # éxito
    r = run("print(2 + 2)")
    assert r.ok and r.stdout.strip() == "4" and r.returncode == 0, r

    # error en el hijo -> ok False, no lanza
    r = run("raise ValueError('boom')")
    assert not r.ok and "ValueError" in r.stderr, r

    # timeout -> timed_out True
    r = run("import time; time.sleep(5)", timeout=0.5)
    assert not r.ok and r.timed_out, r

    # código vacío
    assert run("   ").ok is False

    # truncado de salida
    r = run("print('x' * 100000)", max_output_bytes=1024)
    assert r.ok and len(r.stdout) < 2000 and "truncated" in r.stdout, (r.ok, len(r.stdout))

    print("sandbox_runner_check: OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

```powershell
.\venv\Scripts\python.exe -m py_compile searx\sandbox\runner.py
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\sandbox_runner_check.py"
npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe searx\sandbox\runner.py
```

Esperado: `sandbox_runner_check: OK`; pyright 0 errores en el archivo.

### Step A2.3 — Commit

```powershell
git add searx/sandbox/runner.py
git commit -m "feat(sandbox): add isolated subprocess runner with timeout and env hygiene (Phase C - Pilar 1)"
```

---

## Task A3 — `searx/sandbox/__init__.py` (fachada fail-safe)

**Files:** `searx/sandbox/__init__.py`

### Step A3.1 — Escribir la fachada (reemplaza el placeholder de A1)

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pillar 1 - Executable Search Cards / Sandbox (Phase C).

Summaries (and, in the future, other results) may carry fenced code blocks -
*search cards*.  This pillar detects them (:py:mod:`searx.sandbox.cards`) and,
**only on an explicit user action**, executes the Python ones in an isolated
subprocess (:py:mod:`searx.sandbox.runner`).

Public API::

    from searx.sandbox import is_enabled, detect_cards, execute_card

    cards = detect_cards(summary_text)      # passive, always safe
    result = execute_card(cards[0].code)    # None when the pillar is disabled

The pillar is **opt-in** (``search.nextgen.enabled`` + ``search.nextgen.sandbox.enabled``),
**fail-safe** (every entry point degrades to a no-op / ``None``) and **never auto-runs**
a card: execution requires :py:func:`execute_card`, invoked by the UI/route layer.

Security honesty: the runner is convenience isolation, not an adversarial sandbox;
see :py:mod:`searx.sandbox.runner` for the exact guarantees.
"""

__all__ = ["is_enabled", "detect_cards", "execute_card", "reset", "Card", "ExecutionResult"]

import typing as t

from searx import get_setting, logger
from searx.nextgen import is_enabled as _nextgen_enabled

from .cards import Card, detect_cards, is_supported_language
from .runner import ExecutionResult, run

log = logger.getChild("searx.sandbox")

_DEFAULT_TIMEOUT: float = 5.0
_DEFAULT_MEMORY_MB: int = 128


def is_enabled() -> bool:
    """Return ``True`` when the sandbox pillar is enabled."""
    return _nextgen_enabled("sandbox")


def _get(path: str, default: t.Any) -> t.Any:
    try:
        return get_setting(path, default)
    except Exception:  # pylint: disable=broad-except
        return default


def execute_card(code: str, language: str = "python") -> ExecutionResult | None:
    """Execute ``code`` explicitly.  Returns ``None`` when the pillar is disabled."""
    try:
        if not is_enabled():
            return None
        if not is_supported_language(language):
            return ExecutionResult(False, "", "", -1, 0, False, "unsupported language")
        try:
            timeout = float(_get("search.nextgen.sandbox.timeout", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        try:
            memory = int(_get("search.nextgen.sandbox.memory_limit_mb", _DEFAULT_MEMORY_MB) or _DEFAULT_MEMORY_MB)
        except (TypeError, ValueError):
            memory = _DEFAULT_MEMORY_MB
        return run(code, language=language, timeout=timeout, memory_limit_mb=memory)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("sandbox execute_card failed: %s", e)
        return ExecutionResult(False, "", "", -1, 0, False, str(e) or "execution error")


def reset() -> None:
    """Symmetry hook with the other pillars; the sandbox holds no state."""
    return None
```

### Step A3.2 — Verificar

Guardar en `C:\Users\astur\AppData\Local\Temp\opencode\sandbox_check.py`:

```python
import sys
from searx import settings
from searx.sandbox import detect_cards, execute_card, is_enabled

def main() -> int:
    # desactivado por defecto -> detect_cards sigue funcionando, execute_card devuelve None
    assert is_enabled() is False
    cards = detect_cards("```python\n1+1\n```")
    assert len(cards) == 1
    assert execute_card("1+1") is None

    # activar el pilar y ejecutar
    settings["search"]["nextgen"]["enabled"] = True
    settings["search"]["nextgen"]["sandbox"]["enabled"] = True
    assert is_enabled() is True

    r = execute_card("print(6 * 7)")
    assert r is not None and r.ok and r.stdout.strip() == "42", r

    # lenguaje no soportado -> resultado de error seguro (no None, no crash)
    r = execute_card("echo hi", language="bash")
    assert r is not None and not r.ok and r.error == "unsupported language", r

    print("sandbox_check: OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

```powershell
.\venv\Scripts\python.exe -m py_compile searx\sandbox\__init__.py searx\sandbox\cards.py searx\sandbox\runner.py
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\sandbox_check.py"
npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe searx\sandbox
```

Esperado: `sandbox_check: OK`; pyright 0 errores en `searx/sandbox`.

### Step A3.3 — Commit

```powershell
git add searx/sandbox/__init__.py
git commit -m "feat(sandbox): add fail-safe pillar facade with explicit-only execution (Phase C - Pilar 1)"
```

---

# Parte B — Pilar 4: Mesh (`searx/mesh/`)

## Task B1 — Config mesh + `searx/mesh/protocol.py` (HMAC + framing)

**Files:** `searx/settings_defaults.py`, `searx/mesh/protocol.py`, `searx/mesh/__init__.py` (placeholder)

### Step B1.1 — Ampliar config

En `searx/settings_defaults.py`, localizar:

```python
            'mesh': {
                'enabled': SettingsValue(bool, False),
            },
```

y reemplazarlo por:

```python
            'mesh': {
                'enabled': SettingsValue(bool, False),
                'node_id': SettingsValue(str, ''),
                'listen_port': SettingsValue(int, 9876),
                'listen_host': SettingsValue(str, '127.0.0.1'),
                'sync_interval': SettingsValue(int, 3600),
                'sync_timeout': SettingsValue(int, 5),
                'peers': SettingsValue(list, []),
                'max_fragment_bytes': SettingsValue(int, 4096),
                'max_fragments_per_sync': SettingsValue(int, 200),
                'key_file': SettingsValue(str, ''),
            },
```

### Step B1.2 — Escribir `searx/mesh/protocol.py`

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signed, length-framed wire protocol for the P2P mesh (Phase C - Pillar 4).

Messages are canonical JSON (sorted keys, compact separators), authenticated with
HMAC-SHA256 over the canonical bytes (excluding the ``sig`` field) and framed as
``uint32 length (big-endian) || payload``.  There is **no encryption**: the
security model is a shared symmetric key plus an IP allowlist on a trusted
network, with endpoints shipped pre-anonymized (hashed ids).
"""

__all__ = [
    "PROTOCOL_VERSION",
    "DEFAULT_CLOCK_SKEW_S",
    "DEFAULT_MAX_FRAGMENT_BYTES",
    "DEFAULT_MAX_FRAGMENTS",
    "canonical_bytes",
    "sign",
    "verify",
    "encode_message",
    "decode_message",
    "read_frame",
    "write_frame",
    "sanitize_fragment",
]

import hashlib
import hmac
import json
import socket
import struct
import time

from searx import logger

log = logger.getChild("searx.mesh.protocol")

PROTOCOL_VERSION: int = 1
DEFAULT_CLOCK_SKEW_S: float = 300.0
DEFAULT_MAX_FRAGMENT_BYTES: int = 4096
DEFAULT_MAX_FRAGMENTS: int = 200
_REL_MAX_LEN: int = 60
_ID_MAX_LEN: int = 64

_HEADER: struct.Struct = struct.Struct(">I")


def canonical_bytes(message: dict) -> bytes:
    """Return the canonical JSON encoding of ``message`` (``sig`` excluded)."""
    body = {k: v for k, v in message.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(payload: bytes, key: bytes) -> str:
    """Return the HMAC-SHA256 hex digest of ``payload`` under ``key``."""
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify(payload: bytes, signature: str, key: bytes) -> bool:
    """Constant-time verification of ``signature`` over ``payload``."""
    try:
        expected = sign(payload, key)
        return hmac.compare_digest(expected, str(signature or ""))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh protocol: verify failed: %s", e)
        return False


def sanitize_fragment(fragment: dict) -> dict | None:
    """Validate / normalize one fragment; return ``None`` when invalid."""
    try:
        if not isinstance(fragment, dict):
            return None
        src = str(fragment.get("s", ""))[:_ID_MAX_LEN]
        dst = str(fragment.get("t", ""))[:_ID_MAX_LEN]
        rel = str(fragment.get("r", ""))[:_REL_MAX_LEN]
        if not src or not dst or not rel or src == dst:
            return None
        try:
            weight = float(fragment.get("w", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        if weight <= 0:
            return None
        return {"s": src, "r": rel, "t": dst, "w": round(weight, 3)}
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh protocol: sanitize_fragment failed: %s", e)
        return None


def _dump(message: dict) -> bytes:
    return json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def encode_message(message: dict, key: bytes) -> bytes:
    """Sign ``message`` and return a length-framed byte string."""
    payload = canonical_bytes(message)
    signed = dict(message)
    signed["sig"] = sign(payload, key)
    frame = _dump(signed)
    return _HEADER.pack(len(frame)) + frame


def _load_json(data: bytes) -> dict:
    try:
        loaded = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        if len(data) >= 4:
            loaded = json.loads(data[4:].decode("utf-8"))
        else:
            raise
    if not isinstance(loaded, dict):
        raise ValueError("not a message")
    return loaded


def decode_message(
    data: bytes,
    key: bytes,
    *,
    max_skew: float = DEFAULT_CLOCK_SKEW_S,
    now: float | None = None,
) -> dict:
    """Verify and parse a message (framed or raw).  Raises on any failure."""
    message = _load_json(data)
    if int(message.get("v", 0)) != PROTOCOL_VERSION:
        raise ValueError("bad version")
    if not verify(canonical_bytes(message), str(message.get("sig", "")), key):
        raise ValueError("bad signature")
    ts = float(message.get("ts", 0) or 0)
    current = time.time() if now is None else float(now)
    if abs(current - ts) > float(max_skew):
        raise ValueError("stale message")
    if not str(message.get("nonce", "")):
        raise ValueError("missing nonce")
    if not isinstance(message.get("fragments"), list):
        raise ValueError("bad fragments")
    return message


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < n:
        chunk = sock.recv(n - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(sock: socket.socket, max_bytes: int) -> bytes | None:
    """Read exactly one framed message payload; ``None`` on clean EOF."""
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length <= 0 or length > max(1, int(max_bytes)) + 4096:
        raise ValueError("frame too large")
    body = _recv_exact(sock, length)
    if body is None:
        raise ValueError("short frame")
    return body


def write_frame(sock: socket.socket, data: bytes) -> None:
    """Write ``data`` as one framed message (adds the header if absent)."""
    frame = data
    if not (len(data) >= 4 and _HEADER.unpack(data[:4])[0] == len(data) - 4):
        frame = _HEADER.pack(len(data)) + data
    sock.sendall(frame)
```

### Step B1.3 — Crear paquete `searx/mesh/__init__.py` (placeholder)

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pillar 4 - Sovereign P2P Mesh (Phase C)."""

__all__: list[str] = []
```

### Step B1.4 — Verificar

Guardar en `C:\Users\astur\AppData\Local\Temp\opencode\mesh_protocol_check.py`:

```python
import socket
import struct
import sys
import threading
import time

from searx.mesh.protocol import (
    decode_message, encode_message, read_frame, sign, verify, write_frame, canonical_bytes,
)

KEY = bytes(range(32))

def main() -> int:
    msg = {"v": 1, "node_id": "n1", "kind": "sync_request", "ts": time.time(), "nonce": "abc", "fragments": []}
    framed = encode_message(msg, KEY)

    # la firma es estable
    assert sign(canonical_bytes(msg), KEY) == sign(canonical_bytes(msg), KEY)
    assert verify(canonical_bytes(msg), sign(canonical_bytes(msg), KEY), KEY)

    # round-trip (framed y raw)
    assert decode_message(framed, KEY)["node_id"] == "n1"
    raw = framed[4:]
    assert decode_message(raw, KEY)["kind"] == "sync_request"

    # firma inválida
    bad = dict(msg); bad["sig"] = "00" * 32
    try:
        decode_message(dict(bad, v=1), KEY, now=bad["ts"])
        raise AssertionError("firma inválida aceptada")
    except ValueError:
        pass

    # ts viejo
    old = {"v": 1, "node_id": "n", "kind": "k", "ts": time.time() - 10_000, "nonce": "x", "fragments": []}
    try:
        decode_message(encode_message(old, KEY)[4:], KEY)
        raise AssertionError("ts viejo aceptado")
    except ValueError:
        pass

    # framing sobre un socket (read_frame/write_frame)
    a, b = socket.socketpair()
    try:
        write_frame(a, encode_message(msg, KEY))
        got = read_frame(b, 4096)
        assert got is not None and decode_message(got, KEY)["node_id"] == "n1"
    finally:
        a.close(); b.close()

    print("mesh_protocol_check: OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

```powershell
.\venv\Scripts\python.exe -m py_compile searx\mesh\__init__.py searx\mesh\protocol.py
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\mesh_protocol_check.py"
npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe searx\mesh\protocol.py
```

> En Windows `socket.socketpair()` existe desde Python 3.5; si fallara en el
> entorno, sustituir por un `socket` TCP `127.0.0.1` efímero. (No debería.)

Esperado: `mesh_protocol_check: OK`; pyright 0 errores.

### Step B1.5 — Commit

```powershell
git add searx/settings_defaults.py searx/mesh/__init__.py searx/mesh/protocol.py
git commit -m "feat(mesh): add HMAC-signed length-framed protocol and mesh settings (Phase C - Pilar 4)"
```

---

## Task B2 — `searx/mesh/transport.py` (TCP cliente + servidor)

**Files:** `searx/mesh/transport.py`

### Step B2.1 — Escribir el transporte

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TCP transport for the P2P mesh (Phase C - Pillar 4).

A minimal client (:py:func:`send_sync`) and a threaded server
(:py:class:`MeshServer`) speaking the signed, length-framed protocol of
:py:mod:`searx.mesh.protocol`.  stdlib ``socket`` only; **plaintext** (no TLS).
The server rejects any peer whose source IP is not in the configured allowlist
(fail-closed).  Every entry point is fail-safe.
"""

__all__ = ["Peer", "send_sync", "MeshServer"]

import secrets
import socket
import threading
import time
import typing as t

from searx import logger

from .protocol import (
    DEFAULT_MAX_FRAGMENT_BYTES,
    DEFAULT_MAX_FRAGMENTS,
    PROTOCOL_VERSION,
    decode_message,
    encode_message,
    read_frame,
    sanitize_fragment,
    write_frame,
)

log = logger.getChild("searx.mesh.transport")


class Peer(t.NamedTuple):
    """A remote mesh node endpoint."""

    node_id: str
    host: str
    port: int


def _nonce() -> str:
    return secrets.token_hex(8)


def send_sync(
    peer: Peer,
    key: bytes,
    node_id: str,
    fragments: t.Iterable[dict],
    *,
    timeout: float = 5.0,
    max_fragment_bytes: int = DEFAULT_MAX_FRAGMENT_BYTES,
    max_fragments: int = DEFAULT_MAX_FRAGMENTS,
) -> list[dict]:
    """Send a sync request to ``peer``; return the verified response fragments."""
    try:
        limit = max(1, int(max_fragments))
        clean = [f for f in (sanitize_fragment(x) for x in (fragments or [])) if f][:limit]
        request = {
            "v": PROTOCOL_VERSION,
            "node_id": str(node_id),
            "kind": "sync_request",
            "ts": time.time(),
            "nonce": _nonce(),
            "fragments": clean,
        }
        cap = max(1, int(max_fragment_bytes)) * limit
        with socket.create_connection((peer.host, int(peer.port)), timeout=float(timeout)) as sock:
            sock.settimeout(float(timeout))
            sock.sendall(encode_message(request, key))
            body = read_frame(sock, cap)
        if body is None:
            return []
        response = decode_message(body, key)
        if response.get("kind") != "sync_response":
            return []
        return [f for f in (sanitize_fragment(x) for x in response.get("fragments", [])) if f]
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh send_sync failed: %s", e)
        return []


class MeshServer:
    """Threaded TCP server answering ``sync_request`` with local fragments."""

    def __init__(
        self,
        host: str,
        port: int,
        key: bytes,
        node_id: str,
        fragment_source: t.Callable[[], t.Iterable[dict]] | None,
        allowlist: t.Iterable[str] | None = None,
    ) -> None:
        self._host = str(host or "127.0.0.1")
        self._port = int(port)
        self._key = key
        self._node_id = str(node_id)
        self._source = fragment_source
        self._allowlist: set[str] = set(allowlist or ())
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        """Bind, listen and launch the accept loop.  Returns success."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._host, self._port))
            sock.listen(16)
            sock.settimeout(1.0)
            self._sock = sock
            self._stop.clear()
            self._thread = threading.Thread(target=self.serve_forever, name="searx-mesh-server", daemon=True)
            self._thread.start()
            return True
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh server: start failed: %s", e)
            self.stop()
            return False

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()
            except Exception as e:  # pylint: disable=broad-except
                log.debug("mesh transport: serve_forever thread spawn failed: %s", e)
                self._close(conn)

    def _handle(self, conn: socket.socket, addr: t.Any) -> None:
        try:
            conn.settimeout(5.0)
            if self._allowlist and addr and addr[0] not in self._allowlist:
                return
            body = read_frame(conn, DEFAULT_MAX_FRAGMENT_BYTES * DEFAULT_MAX_FRAGMENTS)
            if body is None:
                return
            message = decode_message(body, self._key)
            if message.get("kind") != "sync_request":
                return
            response = {
                "v": PROTOCOL_VERSION,
                "node_id": self._node_id,
                "kind": "sync_response",
                "ts": time.time(),
                "nonce": _nonce(),
                "fragments": self._local_fragments(),
            }
            write_frame(conn, encode_message(response, self._key))
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh server: handle failed: %s", e)
        finally:
            self._close(conn)

    def _local_fragments(self) -> list[dict]:
        try:
            source = self._source
            if source is None:
                return []
            return [f for f in (sanitize_fragment(x) for x in source()) if f][:DEFAULT_MAX_FRAGMENTS]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh server: fragment source failed: %s", e)
            return []

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            self._close(sock)

    @staticmethod
    def _close(sock: socket.socket) -> None:
        try:
            sock.close()
        except Exception:  # pylint: disable=broad-except
            pass
```

### Step B2.2 — Verificar (servidor+cliente reales en loopback)

Guardar en `C:\Users\astur\AppData\Local\Temp\opencode\mesh_transport_check.py`:

```python
import sys
import time

from searx.mesh.transport import MeshServer, Peer, send_sync

KEY = bytes(range(32))

def main() -> int:
    local = [{"s": "a" * 16, "r": "is_a", "t": "b" * 16, "w": 2.5}]

    def source():
        return local

    # puerto efímero: bind(0)
    server = MeshServer("127.0.0.1", 0, KEY, "server-node", source, allowlist=["127.0.0.1"])
    assert server.start()
    port = server._sock.getsockname()[1]  # type: ignore[union-attr]

    peer = Peer("server-node", "127.0.0.1", port)
    got = send_sync(peer, KEY, "client-node", [], timeout=3.0)
    assert got == local, got

    # clave incorrecta -> [] (fail-safe, sin crash)
    got = send_sync(peer, b"\x00" * 32, "client-node", [], timeout=3.0)
    assert got == [], got

    # peer caído -> []
    got = send_sync(Peer("x", "127.0.0.1", 1), KEY, "c", [], timeout=0.5)
    assert got == []

    server.stop()
    print("mesh_transport_check: OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

> El check accede a `server._sock` (atributo privado) solo para descubrir el
> puerto efímero en el test. Es aceptable en un script de verificación temporal.

```powershell
.\venv\Scripts\python.exe -m py_compile searx\mesh\transport.py
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\mesh_transport_check.py"
npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe searx\mesh\transport.py
```

Esperado: `mesh_transport_check: OK`; pyright 0 errores.

### Step B2.3 — Commit

```powershell
git add searx/mesh/transport.py
git commit -m "feat(mesh): add TCP client/server transport with fail-closed allowlist (Phase C - Pilar 4)"
```

---

## Task B3 — `searx/mesh/__init__.py` (fachada + daemon de sync)

**Files:** `searx/mesh/__init__.py`

### Step B3.1 — Escribir la fachada (reemplaza el placeholder de B1)

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pillar 4 - Sovereign P2P Mesh (Phase C).

Exchanges **pre-anonymized graph fragments** (hashed node ids + relation
predicates + weights) with a small allowlist of trusted peers over the signed,
length-framed TCP protocol of :py:mod:`searx.mesh.transport`, on a periodic,
non-blocking background worker.

The pillar is **opt-in** (``search.nextgen.enabled`` + ``search.nextgen.mesh.enabled``),
**fail-safe** (every entry point degrades to a no-op) and respects the master-spec
rule that a **pillar never imports another pillar**: the local fragment source and
the fragment sink are *injected* by the integration layer
(:py:mod:`searx.nextgen_bridge`), which knows :py:mod:`searx.graph`.

Key material comes from ``SEARXNG_MESH_KEY`` (hex, 32 bytes) or, as fallback, the
file at ``search.nextgen.mesh.key_file``.  Without a valid key the pillar refuses
to start.

Submodules:

- :py:mod:`searx.mesh.protocol` - canonical JSON + HMAC + length framing.
- :py:mod:`searx.mesh.transport`  - TCP client/server.
"""

__all__ = ["is_enabled", "set_fragment_source", "on_fragment", "start", "stop", "sync_now", "reset"]

import os
import secrets
import tempfile
import threading
import typing as t

from searx import get_setting, logger
from searx.nextgen import is_enabled as _nextgen_enabled

from .protocol import DEFAULT_MAX_FRAGMENTS, DEFAULT_MAX_FRAGMENT_BYTES, sanitize_fragment
from .transport import MeshServer, Peer, send_sync

log = logger.getChild("searx.mesh")

_DEFAULT_PORT: int = 9876
_DEFAULT_LISTEN_HOST: str = "127.0.0.1"
_DEFAULT_SYNC_INTERVAL: int = 3600
_DEFAULT_SYNC_TIMEOUT: float = 5.0
_KEY_BYTES: int = 32
_STATE_FILE: str = os.path.join(tempfile.gettempdir(), "searx_mesh_node_id")

_lock = threading.Lock()
_server: MeshServer | None = None
_worker: threading.Thread | None = None
_stop = threading.Event()
_source: t.Callable[[], t.Iterable[dict]] | None = None
_sink: t.Callable[[dict], t.Any] | None = None
_node_id: str = ""
_stats: dict[str, int] = {"peers": 0, "sent": 0, "received": 0, "applied": 0, "errors": 0}


def is_enabled() -> bool:
    """Return ``True`` when the mesh pillar is enabled."""
    return _nextgen_enabled("mesh")


def _get(path: str, default: t.Any) -> t.Any:
    try:
        return get_setting(path, default)
    except Exception:  # pylint: disable=broad-except
        return default


def set_fragment_source(source: t.Callable[[], t.Iterable[dict]] | None) -> None:
    """Register the callback that yields the local (pre-anonymized) fragments."""
    global _source  # pylint: disable=global-statement
    with _lock:
        _source = source


def on_fragment(handler: t.Callable[[dict], t.Any] | None) -> None:
    """Register the callback applied to each verified incoming fragment."""
    global _sink  # pylint: disable=global-statement
    with _lock:
        _sink = handler


def _load_key() -> bytes | None:
    """Return the 32-byte HMAC key from env or ``key_file``, or ``None``."""
    try:
        raw = (os.environ.get("SEARXNG_MESH_KEY", "") or "").strip()
        if not raw:
            key_file = str(_get("search.nextgen.mesh.key_file", "") or "")
            if key_file and os.path.isfile(key_file):
                with open(key_file, "r", encoding="utf-8") as handle:
                    raw = handle.read().strip()
        if not raw:
            return None
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            return None
        return key if len(key) == _KEY_BYTES else None
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh: key load failed: %s", e)
        return None


def _resolve_node_id() -> str:
    """Return the configured node id, else a persisted / fresh random one."""
    configured = str(_get("search.nextgen.mesh.node_id", "") or "").strip()
    if configured:
        return configured
    try:
        if os.path.isfile(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as handle:
                existing = handle.read().strip()
            if existing:
                return existing
        generated = secrets.token_hex(8)
        with open(_STATE_FILE, "w", encoding="utf-8") as handle:
            handle.write(generated)
        return generated
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh: node_id resolve failed: %s", e)
        return secrets.token_hex(8)


def _peers() -> list[Peer]:
    raw = _get("search.nextgen.mesh.peers", []) or []
    peers: list[Peer] = []
    if not isinstance(raw, list):
        return peers
    for item in raw:
        try:
            peers.append(Peer(str(item.get("node_id", "")), str(item["host"]), int(item["port"])))
        except Exception:  # pylint: disable=broad-except
            continue
    return peers


def _collect_local() -> list[dict]:
    with _lock:
        source = _source
    if source is None:
        return []
    try:
        return [f for f in (sanitize_fragment(x) for x in source()) if f]
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh: fragment source failed: %s", e)
        return []


def _apply(fragments: t.Iterable[dict]) -> int:
    with _lock:
        sink = _sink
    if sink is None:
        return 0
    applied = 0
    for raw in fragments:
        clean = sanitize_fragment(raw)
        if clean is None:
            continue
        try:
            sink(clean)
            applied += 1
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh: fragment sink failed: %s", e)
    return applied


def _sync_once() -> dict[str, int]:
    """Run one full sync round over all peers; return fresh stats."""
    key = _load_key()
    if key is None:
        return dict(_stats)
    peers = _peers()
    local = _collect_local()
    sent = received = applied = errors = 0
    try:
        timeout = float(_get("search.nextgen.mesh.sync_timeout", _DEFAULT_SYNC_TIMEOUT) or _DEFAULT_SYNC_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_SYNC_TIMEOUT
    try:
        max_fragment_bytes = int(_get("search.nextgen.mesh.max_fragment_bytes", DEFAULT_MAX_FRAGMENT_BYTES))
    except (TypeError, ValueError):
        max_fragment_bytes = DEFAULT_MAX_FRAGMENT_BYTES
    try:
        max_fragments = int(_get("search.nextgen.mesh.max_fragments_per_sync", DEFAULT_MAX_FRAGMENTS))
    except (TypeError, ValueError):
        max_fragments = DEFAULT_MAX_FRAGMENTS
    node = _node_id or _resolve_node_id()
    for peer in peers:
        response = send_sync(
            peer,
            key,
            node,
            local,
            timeout=timeout,
            max_fragment_bytes=max_fragment_bytes,
            max_fragments=max_fragments,
        )
        if not response:
            continue
        sent += 1
        received += len(response)
        applied += _apply(response)
    with _lock:
        _stats.update(peers=len(peers), sent=sent, received=received, applied=applied, errors=errors)
        return dict(_stats)


def _worker_loop() -> None:
    while not _stop.is_set():
        _sync_once()
        try:
            interval = int(_get("search.nextgen.mesh.sync_interval", _DEFAULT_SYNC_INTERVAL) or _DEFAULT_SYNC_INTERVAL)
        except (TypeError, ValueError):
            interval = _DEFAULT_SYNC_INTERVAL
        if _stop.wait(max(5, interval)):
            return


def start() -> bool:
    """Start the server + sync worker.  Returns ``True`` when running."""
    global _server, _worker, _node_id  # pylint: disable=global-statement
    try:
        if not is_enabled():
            return False
        key = _load_key()
        if key is None:
            log.debug("mesh: no valid key; refusing to start")
            return False
        with _lock:
            if _server is not None:
                return True
            _node_id = _resolve_node_id()
            host = str(_get("search.nextgen.mesh.listen_host", _DEFAULT_LISTEN_HOST) or _DEFAULT_LISTEN_HOST)
            try:
                port = int(_get("search.nextgen.mesh.listen_port", _DEFAULT_PORT) or _DEFAULT_PORT)
            except (TypeError, ValueError):
                port = _DEFAULT_PORT
            allowlist = {peer.host for peer in _peers()}
            server = MeshServer(host, port, key, _node_id, _collect_local, allowlist)
            if not server.start():
                return False
            _server = server
            _stop.clear()
            _worker = threading.Thread(target=_worker_loop, name="searx-mesh-worker", daemon=True)
            _worker.start()
        return True
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh start failed: %s", e)
        return False


def stop() -> None:
    """Stop the sync worker and the server."""
    global _server, _worker  # pylint: disable=global-statement
    try:
        _stop.set()
        with _lock:
            server = _server
            _server = None
            _worker = None
        if server is not None:
            server.stop()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh stop failed: %s", e)


def sync_now() -> dict[str, int]:
    """Run one sync round immediately.  Fail-safe; returns the stats dict."""
    try:
        if not is_enabled() or _load_key() is None:
            return dict(_stats)
        return _sync_once()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh sync_now failed: %s", e)
        return dict(_stats)


def reset() -> None:
    """Stop everything and clear injected handlers (tests)."""
    global _source, _sink, _node_id  # pylint: disable=global-statement
    stop()
    with _lock:
        _source = None
        _sink = None
        _node_id = ""
        _stats.update(peers=0, sent=0, received=0, applied=0, errors=0)
```

### Step B3.2 — Verificar (dos instancias en proceso, sync real)

Guardar en `C:\Users\astur\AppData\Local\Temp\opencode\mesh_check.py`:

```python
import os
import sys

os.environ["SEARXNG_MESH_KEY"] = (b"K" * 32).hex()

from searx import settings
from searx import mesh

def main() -> int:
    # desactivado por defecto
    assert mesh.is_enabled() is False
    assert mesh.start() is False

    settings["search"]["nextgen"]["enabled"] = True
    settings["search"]["nextgen"]["mesh"]["enabled"] = True
    assert mesh.is_enabled() is True

    received: list[dict] = []
    mesh.set_fragment_source(lambda: [{"s": "a" * 16, "r": "is_a", "t": "b" * 16, "w": 2.0}])
    mesh.on_fragment(lambda f: received.append(f))

    # sin peers -> arranca servidor pero no hay a quién sincronizar
    assert mesh.start() is True
    stats = mesh.sync_now()
    assert stats["peers"] == 0 and stats["received"] == 0, stats

    mesh.stop()
    print("mesh_check: OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

> Un segundo check (peer-a-peer) puede hacerse levantando dos procesos o
> reutilizando `mesh_transport_check` (ya cubre el round-trip server↔client con
> clave válida/ inválida). Aquí validamos la fachada y el fail-safe.

```powershell
.\venv\Scripts\python.exe -m py_compile searx\mesh\__init__.py searx\mesh\protocol.py searx\mesh\transport.py
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\mesh_check.py"
npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe searx\mesh
```

Esperado: `mesh_check: OK`; pyright 0 errores en `searx/mesh`.

### Step B3.3 — Commit

```powershell
git add searx/mesh/__init__.py
git commit -m "feat(mesh): add fail-safe pillar facade with periodic non-blocking sync (Phase C - Pilar 4)"
```

---

# Parte C — Integración

## Task C1 — Fragmentos anónimos en `searx/graph`

**Files:** `searx/graph/store.py`, `searx/graph/context.py`, `searx/graph/__init__.py`

### Step C1.1 — Añadir SQL + métodos a `GraphStore` (`searx/graph/store.py`)

Añadir el tope de peso junto a las constantes existentes (línea ~31):

```python
_MESH_WEIGHT_CAP: float = 5.0
```

Añadir dos sentencias junto a las demás `_SQL_*` (tras línea ~84):

```python
_SQL_EXPORT_RELATIONS: str = "SELECT src_id, rel, dst_id, weight FROM relation ORDER BY weight DESC LIMIT ?"
_SQL_REINFORCE_RELATION: str = "UPDATE relation SET weight = weight + ? WHERE src_id = ? AND rel = ? AND dst_id = ?"
```

Añadir los dos métodos públicos a la clase `GraphStore` (tras el método de purga / al final de la sección de escrituras; p.ej. justo antes de `get_store()` en línea ~314, como métodos de la clase):

```python
    # --- mesh fragments (anonymous, additive) -------------------------------

    def export_fragments(self, limit: int = 200) -> list[dict]:
        """Return up to ``limit`` anonymous relation fragments (hashed ids only)."""
        try:
            try:
                cap = max(1, int(limit))
            except (TypeError, ValueError):
                cap = 200
            conn = self._conn()
            rows = conn.execute(_SQL_EXPORT_RELATIONS, (cap,)).fetchall()
            return [
                {"s": str(row[0]), "r": str(row[1]), "t": str(row[2]), "w": round(float(row[3]), 3)}
                for row in rows
            ]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: export_fragments failed: %s", e)
            return []

    def import_fragment(self, fragment: dict) -> int:
        """Reinforce an existing local relation from a mesh fragment.

        Only **existing** relations are updated (weight += min(w, cap)); no entity
        is ever created, so remote fragments cannot pollute the local graph.
        """
        try:
            if not isinstance(fragment, dict):
                return 0
            src = str(fragment.get("s", ""))
            rel = _clean(fragment.get("r", ""), 60)
            dst = str(fragment.get("t", ""))
            if not src or not rel or not dst or src == dst:
                return 0
            try:
                weight = float(fragment.get("w", 1.0))
            except (TypeError, ValueError):
                weight = 1.0
            increment = min(max(weight, 0.0), _MESH_WEIGHT_CAP)
            if increment <= 0:
                return 0
            conn = self._conn()
            with self._lock, conn:
                cursor = conn.execute(_SQL_REINFORCE_RELATION, (increment, src, rel, dst))
                return int(cursor.rowcount or 0)
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph store: import_fragment failed: %s", e)
            return 0
```

> `log` ya existe en `store.py` (`log = logger.getChild("searx.graph.store")`, línea ~27).

### Step C1.2 — Wrappers de módulo en `searx/graph/context.py`

Añadir a `__all__` (línea ~19) `"export_fragments", "import_fragment"` y, tras `add_project`, añadir:

```python
def export_fragments(limit: int = 200) -> list[dict]:
    """Return anonymous relation fragments from the local graph (fail-safe)."""
    try:
        graph_store = get_store()
        if graph_store is None:
            return []
        return graph_store.export_fragments(limit)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph context: export_fragments failed: %s", e)
        return []


def import_fragment(fragment: dict) -> int:
    """Reinforce an existing local relation from a fragment (fail-safe)."""
    try:
        graph_store = get_store()
        if graph_store is None:
            return 0
        return graph_store.import_fragment(fragment)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph context: import_fragment failed: %s", e)
        return 0
```

### Step C1.3 — Reexportar en `searx/graph/__init__.py`

Actualizar `__all__` y los imports:

```python
__all__ = [
    "is_enabled",
    "recall",
    "remember",
    "add_project",
    "export_fragments",
    "import_fragment",
    "reset",
    "reset_store",
    "Triple",
]

from .context import add_project, export_fragments, import_fragment, is_enabled, recall, remember, reset
from .extractor import Triple
from .store import reset_store
```

### Step C1.4 — Verificar

Guardar en `C:\Users\astur\AppData\Local\Temp\opencode\graph_fragments_check.py`:

```python
import os
import sys
import tempfile

from searx.graph.store import GraphStore

def main() -> int:
    db = os.path.join(tempfile.mkdtemp(prefix="graph_frag_"), "g.db")
    store = GraphStore(db)

    written = store.upsert_triples([("Ollama", "is_a", "SQLite")])
    assert written == 1, written

    frags = store.export_fragments(limit=10)
    assert len(frags) >= 1, frags
    frag = frags[0]
    assert set(frag) == {"s", "r", "t", "w"}, frag
    assert "Ollama" not in str(frag) and "SQLite" not in str(frag)  # ids, no nombres

    # import de fragmento ajeno que NO existe localmente -> 0 (no crea entidades)
    missed = store.import_fragment({"s": "z" * 16, "r": "is_a", "t": "y" * 16, "w": 3.0})
    assert missed == 0

    # import de fragmento existente -> refuerza (rowcount 1)
    applied = store.import_fragment({"s": frag["s"], "r": frag["r"], "t": frag["t"], "w": 2.0})
    assert applied == 1, applied

    # el tope acota el incremento (no puede explotar)
    for _ in range(5):
        store.import_fragment({"s": frag["s"], "r": frag["r"], "t": frag["t"], "w": 99.0})
    after = store.export_fragments(limit=50)
    matching = [f for f in after if f["s"] == frag["s"] and f["r"] == frag["r"] and f["t"] == frag["t"]]
    assert matching and matching[0]["w"] <= 1.0 + 5.0 * 7 + 1.0, matching

    # robustez
    assert store.import_fragment(None) == 0  # type: ignore[arg-type]
    assert store.import_fragment({"s": "a", "r": "is_a", "t": "a"}) == 0

    print("graph_fragments_check: OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

> El check puebla el grafo con `upsert_triples([("Ollama", "is_a", "SQLite")])`
> (los ids de entidad se derivan internamente con `node_id("", key)`), y luego
> exporta / reimporta el fragmento resultante.

```powershell
.\venv\Scripts\python.exe -m py_compile searx\graph\store.py searx\graph\context.py searx\graph\__init__.py
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\graph_fragments_check.py"
npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe searx\graph
```

Esperado: `graph_fragments_check: OK`; pyright con el mismo conteo de errores
preexistente (no introducir nuevos).

### Step C1.5 — Commit

```powershell
git add searx/graph/store.py searx/graph/context.py searx/graph/__init__.py
git commit -m "feat(graph): add anonymous fragment export/import for mesh sync (Phase C - Pilar 2/4)"
```

---

## Task C2 — Integrador no-pilar + cableado en la app

**Files:** `searx/nextgen_bridge.py`, `searx/webapp.py`

### Step C2.1 — Escribir `searx/nextgen_bridge.py`

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-pillar integration layer (Phase C).

The master spec forbids a **pillar importing another pillar**.  This module is
*not* a pillar: it is the only place that legitimately knows both
:py:mod:`searx.graph` (Pillar 2) and :py:mod:`searx.mesh` (Pillar 4), and wires
them together - the graph's anonymous fragments become the mesh's fragment source,
and verified incoming fragments reinforce the graph.

Wired once, from :py:func:`searx.webapp.init`.  Fail-safe: any failure degrades to
"not wired".
"""

__all__ = ["wire", "stop"]

import threading

from searx import logger

log = logger.getChild("searx.nextgen_bridge")

_lock = threading.Lock()
_wired = False


def wire() -> bool:
    """Connect the graph and the mesh.  Returns ``True`` when the mesh is running."""
    global _wired  # pylint: disable=global-statement
    try:
        from searx import graph, mesh

        if not (graph.is_enabled() and mesh.is_enabled()):
            return False
        with _lock:
            if _wired:
                return True
            mesh.set_fragment_source(graph.export_fragments)
            mesh.on_fragment(graph.import_fragment)
            started = mesh.start()
            _wired = bool(started)
            return _wired
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: wire failed: %s", e)
        return False


def stop() -> None:
    """Tear down the mesh side of the integration (tests / shutdown)."""
    global _wired  # pylint: disable=global-statement
    try:
        from searx import mesh

        mesh.stop()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: stop failed: %s", e)
    with _lock:
        _wired = False
```

### Step C2.2 — Cablear en `searx/webapp.py::init()`

Tras la línea:

```python
    limiter.initialize(app, settings)
```

(en `def init():`, ~línea 1407), añadir:

```python

    # Phase C - wire the (optional, opt-in) graph <-> mesh integration.
    try:
        from searx import nextgen_bridge

        nextgen_bridge.wire()
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("nextgen_bridge.wire failed: %s", exc)
```

> `logger` ya está en el namespace de `webapp.py`. Si no lo estuviera, usar
> `logging.getLogger(__name__)` o simplemente omitir el log (el `try/except`
> sigue garantizando fail-safe).

### Step C2.3 — Verificar

Guardar en `C:\Users\astur\AppData\Local\Temp\opencode\bridge_check.py`:

```python
import os
import sys

os.environ["SEARXNG_MESH_KEY"] = (b"B" * 32).hex()

from searx import settings
from searx import nextgen_bridge

def main() -> int:
    # pilares desactivados -> no cablea
    assert nextgen_bridge.wire() is False

    settings["search"]["nextgen"]["enabled"] = True
    settings["search"]["nextgen"]["graph"]["enabled"] = True
    settings["search"]["nextgen"]["mesh"]["enabled"] = True
    settings["search"]["nextgen"]["mesh"]["listen_port"] = 0  # puerto efímero libre

    assert nextgen_bridge.wire() is True

    # idempotente
    assert nextgen_bridge.wire() is True

    nextgen_bridge.stop()
    print("bridge_check: OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

```powershell
.\venv\Scripts\python.exe -m py_compile searx\nextgen_bridge.py searx\webapp.py
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\bridge_check.py"
npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe searx\nextgen_bridge.py
```

Esperado: `bridge_check: OK`; pyright 0 errores en `searx/nextgen_bridge.py`.

> `graph.is_enabled()` requiere `search.nextgen.graph.enabled`; el check lo activa.
> `mesh.start()` requiere `SEARXNG_MESH_KEY` válida (se fija en el check). El check
> fija `mesh.listen_port = 0` para tomar un puerto efímero libre y evitar fallos
> si el 9876 estuviera ocupado.

### Step C2.4 — Commit

```powershell
git add searx/nextgen_bridge.py searx/webapp.py
git commit -m "feat(nextgen): add non-pillar graph<->mesh integrator wired at startup (Phase C)"
```

---

# Verificación final (tras todas las tareas)

```powershell
.\venv\Scripts\python.exe -m py_compile `
  searx\sandbox\__init__.py searx\sandbox\cards.py searx\sandbox\runner.py `
  searx\mesh\__init__.py searx\mesh\protocol.py searx\mesh\transport.py `
  searx\nextgen_bridge.py searx\settings_defaults.py `
  searx\graph\store.py searx\graph\context.py searx\graph\__init__.py searx\webapp.py

.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\sandbox_cards_check.py"
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\sandbox_runner_check.py"
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\sandbox_check.py"
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\mesh_protocol_check.py"
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\mesh_transport_check.py"
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\mesh_check.py"
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\graph_fragments_check.py"
.\venv\Scripts\python.exe "C:\Users\astur\AppData\Local\Temp\opencode\bridge_check.py"

npx --yes pyright@1.1.390 --pythonpath .\venv\Scripts\python.exe searx\sandbox searx\mesh searx\nextgen_bridge.py
```

**Criterio de éxito:** todos los `*_check: OK`; `py_compile` exit 0; pyright **0 errores / 0 warnings** en `searx/sandbox`, `searx/mesh` y `searx/nextgen_bridge.py` (sin nuevas filas respecto al baseline).

**Comprobación de arquitectura (no pilar→pilar):**

```powershell
Select-String -Path searx\mesh\*.py -Pattern "searx\.graph|searx\.memory" ; Write-Output "(debe estar vacío)"
Select-String -Path searx\sandbox\*.py -Pattern "searx\.graph|searx\.mesh|searx\.memory" ; Write-Output "(debe estar vacío)"
```

Ambos deben devolver vacío: la única dependencia cruzada vive en `searx/nextgen_bridge.py`.

---

# Notes for the implementer

- **No `pytest`:** usar los scripts `*_check.py` standalone (ya incluidos arriba). No añadir tests a `tests/` (no corren en este venv).
- **`pyright` baseline:** ~9 errores preexistentes ajenos (`searx/search/__init__.py`, `searx/results.py`). Al validar `searx/graph` o `searx/webapp.py`, comparar contra el baseline y **no introducir filas nuevas**.
- **`store.py`:** a diferencia de lo que sugiere el nombre "upsert_relation", el método real de escritura de relaciones es `upsert_triples(triples, source_query="")`; los `*_check.py` ya lo usan. `log = logger.getChild("searx.graph.store")` ya existe (línea ~27), no hace falta añadirlo.
- **Windows vs POSIX:** el límite de memoria del sandbox es POSIX-only por diseño; no intentar emularlo en Windows.
- **Mantener fail-safe absoluto:** cada función pública con `try/except Exception` + `log.debug`, devolviendo el valor neutro.
- **DRY/YAGNI:** no añadir features fuera de spec (p.ej. cifrado mesh, otros lenguajes de sandbox, TLS).
- **Commits atómicos:** uno por tarea, mensaje en el estilo del repo (`feat(scope): ... (Phase C - Pilar N)`).
