# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post-processing pipeline for search results."""

__all__ = ["get_postprocessors", "process_container", "PostProcessor"]

from .base import PostProcessor
from .pipeline import get_postprocessors, process_container
