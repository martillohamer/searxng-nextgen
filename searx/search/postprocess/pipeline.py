# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post-processing pipeline manager."""

import typing as t

from searx import get_setting, logger
from searx.metrics import nextgen

from .base import PostProcessor
from .processors import DeduplicateAdvanced, FilterResults

if t.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.results import ResultContainer


log: t.Any = logger.getChild("searx.postprocess.pipeline")


def _get_processor_classes() -> dict[str, type[PostProcessor]]:
    return {
        "DeduplicateAdvanced": DeduplicateAdvanced,
        "FilterResults": FilterResults,
    }


def get_postprocessors() -> list[PostProcessor]:
    """Return list of enabled postprocessors in execution order."""
    if not get_setting("search.postprocess.enabled", True):
        return []

    proc_cfg = get_setting("search.postprocess.processors", {}) or {}
    classes = _get_processor_classes()
    enabled: list[PostProcessor] = []
    for name, cfg in proc_cfg.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", True):
            continue
        cls = classes.get(name)
        if not cls:
            log.warning("Unknown postprocessor: %s", name)
            continue
        try:
            proc = cls()
            proc.enabled = bool(cfg.get("enabled", True))
            if "priority" in cfg:
                try:
                    proc.priority = int(cfg["priority"])
                except Exception:
                    pass
            enabled.append(proc)
        except Exception as e:  # pylint: disable=broad-except
            log.exception("Failed to initialize postprocessor %s: %s", name, e)
            continue

    enabled.sort(key=lambda p: p.priority)
    return enabled


def process_container(container: "ResultContainer", search: "SearchWithPlugins") -> None:
    """Run all enabled postprocessors against container."""
    if not get_setting("search.postprocess.enabled", True):
        return

    removed = 0
    for proc in get_postprocessors():
        if not proc.enabled:
            continue
        before = len(container.main_results_map)
        try:
            proc.process(container, search)
        except Exception as e:  # pylint: disable=broad-except
            log.exception("Postprocessor %s failed: %s", proc.name, e)
            continue
        removed += max(before - len(container.main_results_map), 0)

    nextgen.postprocess_removed(removed)
