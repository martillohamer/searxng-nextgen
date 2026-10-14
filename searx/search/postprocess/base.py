# SPDX-License-Identifier: AGPL-3.0-or-later
"""Abstract base class for result post-processors."""

import abc
import typing as t

if t.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.results import ResultContainer


class PostProcessor(abc.ABC):
    """Base abstract class for post-processing result containers."""

    name: str = "base"
    priority: int = 100  # lower number runs first
    enabled: bool = True

    @abc.abstractmethod
    def process(self, container: "ResultContainer", search: "SearchWithPlugins") -> None:
        """Process the result container. Subclasses must implement this method."""
        raise NotImplementedError
