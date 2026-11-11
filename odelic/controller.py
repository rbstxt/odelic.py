"""High-level controller shared by the CLI, the HTTP API and the MCP server.

* connects lazily, serialises operations and reconnects once if the link dropped
* disconnects after `idle_timeout` seconds without commands, so the phone app can use the mesh again
* accepts light names from the config file ("living"), "all", or a raw vaddr ("00000000")
"""
import asyncio
import logging

from bleak.exc import BleakError

from . import protocol as P
from .client import OdelicMesh
from .config import Config

log = logging.getLogger("odelic")


class Controller:
    def __init__(self, app_id=None, *, node=None, config=None, idle_timeout=None):
        """idle_timeout: seconds before an idle link is released; 0 = stay connected;
        None = $ODELIC_IDLE_TIMEOUT / config file / 60 s."""
        self.config = config or Config.load()
        aid = self.config.app_id(app_id)
        if not aid:
            raise ValueError("no app ID: pass --id, set ODELIC_ID, or run `odelic setup --id <ID>`")
        self.mesh = OdelicMesh(aid, device=node or self.config.node)
        self.idle_timeout = self.config.idle(idle_timeout)
        self._lock = None
        self._idle = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # ------------------------------------------------------------ connection handling
    async def _run(self, fn):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._cancel_idle()
            try:
                for attempt in (0, 1):
                    if not self.mesh.is_connected:
                        await self.mesh.connect()
                    try:
                        return await fn(self.mesh)
                    except BleakError:
                        if attempt:
                            raise
                        log.info("link lost, reconnecting")
                        await self.mesh.disconnect()
            finally:
                self._arm_idle()

    def _cancel_idle(self):
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    def _arm_idle(self):
        if self.idle_timeout and self.mesh.is_connected:
            loop = asyncio.get_running_loop()
            self._idle = loop.call_later(self.idle_timeout, lambda: asyncio.ensure_future(self._idle_close()))

    async def _idle_close(self):
        async with self._lock:
            self._idle = None
            if self.mesh.is_connected:
                log.info("idle, disconnecting")
                await self.mesh.disconnect()

    async def connect(self):
        """Connect now (otherwise the first command connects)."""
        await self._run(lambda m: asyncio.sleep(0))

    async def close(self):
        self._cancel_idle()
        await self.mesh.disconnect()

    @property
    def connected(self):
        return self.mesh.is_connected

    @property
    def keep_connected(self):
        return not self.idle_timeout

    def info(self):
        return {"connected": self.connected, "node": self.mesh.node_name,
                "mode": "keep" if self.keep_connected else "idle", "idle_timeout": self.idle_timeout}

    # ------------------------------------------------------------ helpers
    def _label(self, v):
        return "all" if v == P.BROADCAST else (self.config.name_of(v) or v.hex())

    def _done(self, action, v, **extra):
        return dict({"ok": True, "action": action, "target": self._label(v)}, **extra)

    # ------------------------------------------------------------ actions
    async def on(self, target="all"):
        v = self.config.resolve(target)
        await self._run(lambda m: m.on(v))
        return self._done("on", v)

    async def off(self, target="all"):
        v = self.config.resolve(target)
        await self._run(lambda m: m.off(v))
        return self._done("off", v)

    async def set_light(self, target="all", color=50, brightness=100):
        """color 0 (warm) .. 100 (cool), brightness 1..100 %"""
        v = self.config.resolve(target)
        color = max(0, min(100, int(color)))
        brightness = max(1, min(100, int(brightness)))
        await self._run(lambda m: m.set_light(color, brightness, v))
        return self._done("light", v, color=color, brightness=brightness)

    async def night_light(self, target="all", level=0):
        v = self.config.resolve(target)
        level = max(0, min(2, int(level)))
        await self._run(lambda m: m.night_light(level, v))
        return self._done("night", v, level=level)

    async def group(self, group_id, action="on", color=50, brightness=100):
        if action == "light":
            await self._run(lambda m: m.set_group(int(group_id), color, brightness))
        elif action in ("on", "off"):
            await self._run(lambda m: m.set_group(int(group_id), on=(action == "on")))
        else:
            raise ValueError("action must be on, off or light")
        return {"ok": True, "action": "group_" + action, "group": int(group_id)}

    async def scene(self, index, save=False):
        await self._run(lambda m: (m.save_scene if save else m.play_scene)(int(index)))
        return {"ok": True, "action": "scene_save" if save else "scene_play", "index": int(index)}

    async def change_color(self, target):
        v = self.config.resolve(target)
        await self._run(lambda m: m.change_color(v))
        return self._done("change_color", v)

    async def lights(self, timeout=3.0):
        """Lights answering a mesh ping, plus named lights that did not answer (online=False)."""
        devs = await self._run(lambda m: m.devices(timeout))
        out, seen = [], set()
        for d in sorted(devs, key=lambda d: d.vaddr):
            seen.add(d.vaddr.hex())
            out.append({"name": self.config.name_of(d.vaddr), "vaddr": d.vaddr.hex(), "online": True,
                        "mac": d.mac, "product": d.product, "version": d.version})
        for name, v in self.config.lights.items():
            if v not in seen:
                out.append({"name": name, "vaddr": v, "online": False})
        return out

    async def status(self, target):
        """State of one light, or of every responding light for 'all'."""
        v = self.config.resolve(target)
        if v == P.BROADCAST:
            devs = await self._run(lambda m: m.devices())
            return [await self.status(d.vaddr) for d in sorted(devs, key=lambda d: d.vaddr)]
        st = await self._run(lambda m: m.status(v))
        if st is None:
            return {"name": self.config.name_of(v), "vaddr": v.hex(), "online": False}
        return dict({"name": self.config.name_of(v), "online": True}, **st.as_dict())

    # ------------------------------------------------------------ names (no BLE needed)
    def name_light(self, name, target):
        self.config.set_name(name, target)
        self.config.save()
        return {"ok": True, "name": name, "vaddr": self.config.lights[name]}

    def forget_name(self, name):
        self.config.remove_name(name)
        self.config.save()
        return {"ok": True, "removed": name}
