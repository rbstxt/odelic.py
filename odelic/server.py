"""Tiny dependency-free HTTP/JSON API on top of Controller.

    GET  /health
    GET  /lights                      list lights (ping)
    GET  /lights/<target>             state ("all" = every light)
    POST /lights/<target>/on | off
    POST /lights/<target>/light       {"color": 0..100, "brightness": 1..100}
    POST /lights/<target>/night       {"level": 0..2}
    POST /groups/<id>/on | off | light
    POST /scenes/<index>/play | save

Parameters may be sent as a JSON body or as query parameters. <target> is a light name, "all" or a vaddr.
When a token is configured every request needs `Authorization: Bearer <token>` (or ?token=...).
"""
import asyncio
import hmac
import json
import logging
from urllib.parse import parse_qsl, unquote, urlsplit

log = logging.getLogger("odelic")

LOOPBACK = ("127.0.0.1", "localhost", "::1")
STATUS_TEXT = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
               405: "Method Not Allowed", 413: "Payload Too Large", 502: "Bad Gateway"}


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


async def dispatch(ctl, method, path, params):
    parts = [unquote(p) for p in path.strip("/").split("/") if p]
    if parts == ["health"]:
        return dict({"ok": True}, **ctl.info())
    if not parts:
        raise HttpError(404, "not found")
    head = parts[0]
    if head == "lights":
        if len(parts) == 1:
            _need(method, "GET")
            return await ctl.lights()
        target = parts[1]
        if len(parts) == 2:
            _need(method, "GET")
            return await ctl.status(target)
        _need(method, "POST")
        act = parts[2]
        if act == "on":
            return await ctl.on(target)
        if act == "off":
            return await ctl.off(target)
        if act == "light":
            return await ctl.set_light(target, params.get("color", 50), params.get("brightness", 100))
        if act == "night":
            return await ctl.night_light(target, params.get("level", 0))
    elif head == "groups" and len(parts) == 3:
        _need(method, "POST")
        return await ctl.group(int(parts[1]), parts[2], params.get("color", 50), params.get("brightness", 100))
    elif head == "scenes" and len(parts) == 3 and parts[2] in ("play", "save"):
        _need(method, "POST")
        return await ctl.scene(int(parts[1]), save=(parts[2] == "save"))
    raise HttpError(404, "not found")


def _need(method, expected):
    if method != expected:
        raise HttpError(405, "use %s" % expected)


async def _read_request(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    length = int(headers.get("content-length") or 0)
    if length > 65536:
        raise HttpError(413, "body too large")
    body = await reader.readexactly(length) if length else b""
    return method.upper(), target, headers, body


def make_handler(ctl, token=None):
    async def handle(reader, writer):
        status, payload = 200, None
        try:
            method, target, headers, body = await asyncio.wait_for(_read_request(reader), 10)
            url = urlsplit(target)
            params = dict(parse_qsl(url.query))
            if body:
                try:
                    data = json.loads(body)
                except ValueError:
                    raise HttpError(400, "body must be JSON") from None
                if isinstance(data, dict):
                    params.update(data)
            if token:
                given = headers.get("authorization", "")
                given = given[7:] if given.lower().startswith("bearer ") else params.get("token", "")
                if not hmac.compare_digest(str(given), token):
                    raise HttpError(401, "bad or missing token")
            params.pop("token", None)
            payload = await dispatch(ctl, method, url.path, params)
        except HttpError as e:
            status, payload = e.status, {"ok": False, "error": str(e)}
        except (ValueError, KeyError) as e:
            status, payload = 400, {"ok": False, "error": str(e)}
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            writer.close()
            return
        except Exception as e:  # BLE / login failures
            log.exception("request failed")
            status, payload = 502, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        writer.write(("HTTP/1.1 %d %s\r\nContent-Type: application/json; charset=utf-8\r\n"
                      "Content-Length: %d\r\nConnection: close\r\n\r\n"
                      % (status, STATUS_TEXT.get(status, ""), len(data))).encode("latin1") + data)
        try:
            await writer.drain()
        finally:
            writer.close()

    return handle


async def _connect_early(ctl):
    try:
        await ctl.connect()
        log.warning("connected via %s", ctl.mesh.node_name)
    except Exception as e:  # the next request retries
        log.warning("initial connect failed: %s", e)


async def serve(ctl, host="127.0.0.1", port=8765, token=None):
    if host not in LOOPBACK and not token:
        raise ValueError("refusing to listen on %s without a token (use --token or ODELIC_HTTP_TOKEN)" % host)
    server = await asyncio.start_server(make_handler(ctl, token), host, port)
    log.warning("odelic HTTP API on http://%s:%d (%s)", host, port,
                "keeping the BLE link open" if ctl.keep_connected else "idle disconnect after %gs" % ctl.idle_timeout)
    if ctl.keep_connected:
        asyncio.ensure_future(_connect_early(ctl))
    try:
        async with server:
            await server.serve_forever()
    finally:
        await ctl.close()
