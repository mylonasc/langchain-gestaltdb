"""Factory for consistently configured schema/query tools."""

from __future__ import annotations

from langchain_core.tools import BaseTool, BaseToolkit
from pydantic import ConfigDict, Field, StrictBool

from langchain_gestaltdb.graph import GestaltDBGraph
from langchain_gestaltdb.tools import GestaltDBQueryTool, GestaltDBSchemaTool


class GestaltDBToolkit(BaseToolkit):
    """Default read-only toolkit; writes require trusted application opt-in."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    graph: GestaltDBGraph = Field(exclude=True)
    allow_writes: StrictBool = Field(default=False, exclude=True)
    allowed_procedures: tuple[str, ...] = Field(default=(), exclude=True)
    max_rows: int = Field(default=100, ge=1, le=1000, exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)

    def get_tools(self) -> list[BaseTool]:
        """Construct tools without transferring ownership of the graph handle."""
        return [
            GestaltDBSchemaTool(graph=self.graph, max_chars=self.max_chars),
            GestaltDBQueryTool(
                graph=self.graph,
                allow_writes=self.allow_writes,
                allowed_procedures=self.allowed_procedures,
                max_rows=self.max_rows,
                max_chars=self.max_chars,
            ),
        ]
