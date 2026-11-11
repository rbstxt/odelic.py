"""MCP server exposing ODELIC lights to AI assistants (stdio).

    pip install "odelic[mcp]"
    odelic-mcp            # reads the ID from ODELIC_ID or the config file (`odelic setup --id ...`)

The BLE link is opened on the first command. By default it is released after 60 s without
commands so the official phone app can still connect; set ODELIC_IDLE_TIMEOUT (seconds, 0 = stay
connected) or `odelic setup --keep-connected` to change that.
"""
import logging
import sys

try:                                       # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                        # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import ToolAnnotations

from .controller import Controller

INSTRUCTIONS = """Controls ODELIC Bluetooth ceiling/room lights in the user's home.
Targets: a light name (see list_lights), or "all" for every light.
Colour is 0 = warm/電球色 (orange) ... 100 = cool/昼光色 (bluish white); brightness is 1..100 %.
Commands are fire-and-forget; use light_status to confirm the result if needed."""

mcp = _Server("odelic", instructions=INSTRUCTIONS, log_level="WARNING")
_ctl = None

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def ctl() -> Controller:
    global _ctl
    if _ctl is None:
        _ctl = Controller()   # idle timeout from $ODELIC_IDLE_TIMEOUT / config (0 = stay connected)
    return _ctl


@mcp.tool(annotations=READ_ONLY)
async def list_lights() -> list:
    """List the lights in the mesh (name, vaddr, online, product). Takes a few seconds."""
    return await ctl().lights()


@mcp.tool(annotations=READ_ONLY)
async def light_status(target: str = "all") -> dict:
    """Current state of a light: on/off, colour (0 warm..100 cool) and brightness (%).
    target: light name, vaddr, or "all"."""
    res = await ctl().status(target)
    return {"lights": res} if isinstance(res, list) else res


@mcp.tool(annotations=ACTION)
async def turn_on(target: str = "all") -> dict:
    """Turn a light (or "all") on with its previous colour and brightness."""
    return await ctl().on(target)


@mcp.tool(annotations=ACTION)
async def turn_off(target: str = "all") -> dict:
    """Turn a light (or "all") off."""
    return await ctl().off(target)


@mcp.tool(annotations=ACTION)
async def set_light(target: str = "all", color: int = 50, brightness: int = 100) -> dict:
    """Turn on with a given colour and brightness.
    color: 0 = warm/電球色 .. 50 = neutral .. 100 = cool/昼光色. brightness: 1..100 (%)."""
    return await ctl().set_light(target, color, brightness)


@mcp.tool(annotations=ACTION)
async def night_light(target: str = "all", level: int = 0) -> dict:
    """Very dim warm night light. level 0..2."""
    return await ctl().night_light(target, level)


@mcp.tool(annotations=ACTION)
async def control_group(group_id: int, action: str = "on", color: int = 50, brightness: int = 100) -> dict:
    """Control a group configured in the ODELIC app. action: "on", "off" or "light" (uses color/brightness)."""
    return await ctl().group(group_id, action, color, brightness)


@mcp.tool(annotations=ACTION)
async def play_scene(index: int) -> dict:
    """Recall a scene saved in the lights (index as in the app)."""
    return await ctl().scene(index, save=False)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
async def save_scene(index: int) -> dict:
    """Store the current state of all lights as scene `index` (overwrites that scene)."""
    return await ctl().scene(index, save=True)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def name_light(name: str, vaddr: str) -> dict:
    """Give a light (vaddr from list_lights) a friendly name such as "living" or "リビング"."""
    return ctl().name_light(name, vaddr)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
def forget_light_name(name: str) -> dict:
    """Remove a light name from the config."""
    return ctl().forget_name(name)


def main():
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(name)s: %(message)s")
    logging.getLogger("odelic").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
