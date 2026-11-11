"""Offline tests for status parsing, config/names, the HTTP API and the MCP tool list."""
import asyncio
import json
import os
import tempfile
import unittest

from odelic import protocol as P
from odelic.config import Config
from odelic.server import HttpError, dispatch, make_handler


class TestStatus(unittest.TestCase):
    def test_parse(self):
        # reply captured from a real light: warm (x=0), brightest (y=0)
        st = P.parse_status(bytes.fromhex("03020000002700000000710000000000000000"))
        self.assertEqual(st.as_dict(), {"vaddr": "00000000", "on": True, "color": 0, "brightness": 100, "x": 0, "y": 0})
        off = P.parse_status(bytes.fromhex("0302000000270000000071323200"))
        self.assertFalse(off.on)
        self.assertEqual(off.brightness, 0)
        on = P.parse_status(bytes.fromhex("0302000000270000000071373700"))
        self.assertTrue(on.on)
        self.assertIsNone(on.color)
        self.assertIsNone(P.parse_status(bytes.fromhex("0302000000ff0000")))


class TestConfig(unittest.TestCase):
    def test_names_and_save(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "config.json")
            cfg = Config(path=path)
            cfg.id = "12345678"
            cfg.set_name("リビング", "00000000")
            cfg.save()
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            cfg2 = Config.load(path)
            self.assertEqual(cfg2.id, "12345678")
            self.assertEqual(cfg2.resolve("リビング"), b"\x00\x00\x00\x00")
            self.assertEqual(cfg2.resolve("all"), P.BROADCAST)
            self.assertEqual(cfg2.resolve("0a000000"), bytes.fromhex("0a000000"))
            self.assertEqual(cfg2.name_of(b"\x00\x00\x00\x00"), "リビング")
            with self.assertRaises(ValueError):
                cfg2.resolve("kitchen")
            with self.assertRaises(ValueError):
                cfg2.set_name("everything", "all")


class FakeController:
    def __init__(self):
        self.calls = []

    def info(self):
        return {"connected": False, "mode": "idle", "idle_timeout": 60.0}

    def __getattr__(self, name):
        async def f(*args, **kw):
            self.calls.append((name, args, kw))
            return {"ok": True, "action": name, "args": list(args)}
        return f


def run(coro):
    return asyncio.run(coro)


class TestIdle(unittest.TestCase):
    def test_resolution(self):
        from odelic.config import parse_idle
        self.assertEqual(parse_idle("never"), 0.0)
        self.assertEqual(parse_idle("30"), 30.0)
        self.assertIsNone(parse_idle(None))
        old = os.environ.pop("ODELIC_IDLE_TIMEOUT", None)
        try:
            cfg = Config({"idle_timeout": 0})
            self.assertEqual(cfg.idle(), 0.0)              # config: keep connected
            self.assertEqual(cfg.idle("15"), 15.0)         # explicit override wins
            os.environ["ODELIC_IDLE_TIMEOUT"] = "90"
            self.assertEqual(cfg.idle(), 90.0)             # env beats config
            self.assertEqual(Config({}).idle(), 90.0)
            del os.environ["ODELIC_IDLE_TIMEOUT"]
            self.assertEqual(Config({}).idle(), 60.0)      # default
        finally:
            os.environ.pop("ODELIC_IDLE_TIMEOUT", None)
            if old is not None:
                os.environ["ODELIC_IDLE_TIMEOUT"] = old


class TestHttp(unittest.TestCase):
    def test_dispatch(self):
        c = FakeController()
        run(dispatch(c, "POST", "/lights/%E3%83%AA%E3%83%93%E3%83%B3%E3%82%B0/light", {"color": 0, "brightness": 80}))
        self.assertEqual(c.calls[-1], ("set_light", ("リビング", 0, 80), {}))
        run(dispatch(c, "POST", "/lights/all/off", {}))
        self.assertEqual(c.calls[-1][0:2], ("off", ("all",)))
        run(dispatch(c, "POST", "/scenes/2/play", {}))
        self.assertEqual(c.calls[-1], ("scene", (2,), {"save": False}))
        with self.assertRaises(HttpError):
            run(dispatch(c, "GET", "/lights/all/on", {}))
        with self.assertRaises(HttpError):
            run(dispatch(c, "POST", "/nope", {}))

    def test_server_with_token(self):
        async def scenario():
            c = FakeController()
            server = await asyncio.start_server(make_handler(c, token="s3cret"), "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            async def req(raw):
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(raw)
                await w.drain()
                data = await r.read()
                w.close()
                head, body = data.split(b"\r\n\r\n", 1)
                return int(head.split()[1]), json.loads(body)

            body = b'{"level": 1}'
            ok = await req(b"POST /lights/all/night HTTP/1.1\r\nAuthorization: Bearer s3cret\r\n"
                           b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
            denied = await req(b"POST /lights/all/off HTTP/1.1\r\n\r\n")
            via_query = await req(b"GET /health?token=s3cret HTTP/1.1\r\n\r\n")
            server.close()
            await server.wait_closed()
            return c, ok, denied, via_query

        c, ok, denied, via_query = run(scenario())
        self.assertEqual(ok[0], 200)
        self.assertEqual(c.calls[0], ("night_light", ("all", 1), {}))
        self.assertEqual(denied[0], 401)
        self.assertEqual(via_query, (200, {"ok": True, "connected": False, "mode": "idle", "idle_timeout": 60.0}))


class TestMcp(unittest.TestCase):
    def test_tools_registered(self):
        try:
            from odelic import mcp_server
        except ImportError:
            self.skipTest("mcp not installed")
        tools = run(mcp_server.mcp.list_tools())
        names = {t.name for t in tools}
        self.assertTrue({"list_lights", "light_status", "turn_on", "turn_off", "set_light",
                         "night_light", "play_scene", "name_light"} <= names)


if __name__ == "__main__":
    unittest.main()
