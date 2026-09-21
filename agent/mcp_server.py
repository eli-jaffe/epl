"""MCP server exposing the shared tool layer -- see
docs/agent_ui_architecture_plan.md.

Two kinds of tools, both registered on the same server:
- The raw TOOL_REGISTRY functions (thin adapters, no domain-glossary
  grounding or Reason/Plan/Execute/Reflect structure, no observability
  logging) -- a power-user escape hatch for direct data access, written
  once and shared with the web app's agent loop (the shared-tool-layer
  pattern from the workforce-data-explorer reference in references/).
- ask_epl_agent, the default way MCP clients should use this server: it
  runs the same run_agent_query loop /chat uses, so MCP clients get the
  actual agent (planning, tool selection, reflection, synthesis) instead of
  bare data access. Confirmed reversible later -- trimming which tools get
  registered here has no effect on the web app's own use of the same
  underlying functions.

Run directly (`python -m agent.mcp_server`).
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from agent.auth.service_user import get_or_create_service_user_id
from agent.loop import run_agent_query
from agent.tools.registry import TOOL_REGISTRY

mcp = MCPServer("epl-fantasy")

for tool in TOOL_REGISTRY:
    mcp.add_tool(tool.fn, name=tool.name, description=tool.description)


async def ask_epl_agent(query: str) -> str:
    """Ask the EPL fantasy agent a natural-language question. It plans,
    gathers data via its own tools, and returns a synthesized answer --
    prefer this over calling the raw tools directly.
    """
    user_id = await get_or_create_service_user_id()
    return await run_agent_query(user_id, query)


mcp.add_tool(
    ask_epl_agent,
    name="ask_epl_agent",
    description=(
        "Ask the EPL fantasy agent a natural-language question about players, "
        "fixtures, or fantasy strategy. Runs the full Reason->Plan->Execute->"
        "Reflect->Synthesize loop and returns a plain-language answer."
    ),
)


if __name__ == "__main__":
    mcp.run()
