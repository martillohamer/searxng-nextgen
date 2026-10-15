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
