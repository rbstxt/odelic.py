"""odelic command line.  `odelic setup --id <APP_ID>` once, then e.g. `odelic off`, `odelic light living --color 0`."""
import argparse
import asyncio
import json
import logging
import os
import sys

from .client import find_nodes
from .config import Config, config_path, parse_idle
from .controller import Controller
from .crypto import LoginError
from .protocol import parse_app_id


def idle_opts(p, what):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--idle", metavar="SECONDS", help="release the BLE link after N idle seconds (%s)" % what)
    g.add_argument("--keep-connected", action="store_true",
                   help="never disconnect (best when the phone app is not used)")


def idle_value(a):
    return 0 if a.keep_connected else parse_idle(a.idle)


def build_parser():
    ap = argparse.ArgumentParser(prog="odelic", description="Control ODELIC Bluetooth lights")
    ap.add_argument("--id", help="ID shown in the app (ID:xxxx); default: $ODELIC_ID or the config file")
    ap.add_argument("--node", help="BLE name/address of the node to connect through")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="save the app ID (and optionally a node) to the config file")
    p.add_argument("--id", dest="setup_id")
    p.add_argument("--node", dest="setup_node")
    idle_opts(p, "idle disconnect default for serve / MCP")

    sub.add_parser("scan", help="list nearby mesh nodes (no login)")
    sub.add_parser("lights", aliases=["devices"], help="list lights in the mesh")

    def target(p):
        p.add_argument("target", nargs="?", default="all", help="light name, vaddr or 'all' (default)")

    target(sub.add_parser("on", help="turn on (previous colour/brightness)"))
    target(sub.add_parser("off", help="turn off"))
    p = sub.add_parser("light", help="turn on with colour/brightness")
    target(p)
    p.add_argument("--color", type=int, default=50, help="0 = warm (電球色) .. 100 = cool (昼光色)")
    p.add_argument("--brightness", type=int, default=100, help="1..100 %%")
    p = sub.add_parser("night", help="night light")
    target(p)
    p.add_argument("--level", type=int, default=0, choices=[0, 1, 2])
    target(sub.add_parser("status", help="show on/off, colour and brightness"))

    p = sub.add_parser("group", help="control a group (id as configured in the app)")
    p.add_argument("group_id", type=int)
    p.add_argument("action", choices=["on", "off", "light"])
    p.add_argument("--color", type=int, default=50)
    p.add_argument("--brightness", type=int, default=100)
    p = sub.add_parser("scene", help="save or play a scene")
    p.add_argument("action", choices=["save", "play"])
    p.add_argument("index", type=int)

    p = sub.add_parser("name", help="manage light names")
    nsub = p.add_subparsers(dest="name_cmd", required=True)
    q = nsub.add_parser("add", help="name a light: odelic name add living 00000000")
    q.add_argument("name")
    q.add_argument("vaddr")
    q = nsub.add_parser("rm", help="remove a name")
    q.add_argument("name")
    nsub.add_parser("list")

    p = sub.add_parser("serve", help="run the HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", default=os.environ.get("ODELIC_HTTP_TOKEN"), help="bearer token (required off-localhost)")
    idle_opts(p, "override the configured connection mode")

    sub.add_parser("mcp", help="run the MCP server on stdio (needs odelic[mcp])")
    return ap


def show(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2) if not isinstance(obj, str) else obj)


async def run(a, cfg):
    if a.cmd == "scan":
        aid = cfg.app_id(a.id)
        homeid = parse_app_id(aid)[0] if aid else None
        for dev, hid, rssi in await find_nodes(homeid):
            print("%-20s %s  HomeID=%d  rssi=%d" % (dev.name, dev.address, hid, rssi))
        return

    if a.cmd == "serve":
        from .server import serve
        ctl = Controller(a.id, node=a.node, config=cfg, idle_timeout=idle_value(a))
        await serve(ctl, a.host, a.port, a.token)
        return

    async with Controller(a.id, node=a.node, config=cfg, idle_timeout=0) as ctl:  # closed on exit
        if a.cmd in ("lights", "devices"):
            for d in await ctl.lights():
                if d["online"]:
                    print("%-12s vaddr=%s mac=%s product=%s ver=%s"
                          % (d["name"] or "-", d["vaddr"], d["mac"], d["product"], d["version"]))
                else:
                    print("%-12s vaddr=%s (offline)" % (d["name"], d["vaddr"]))
            return
        if a.cmd == "on":
            show(await ctl.on(a.target))
        elif a.cmd == "off":
            show(await ctl.off(a.target))
        elif a.cmd == "light":
            show(await ctl.set_light(a.target, a.color, a.brightness))
        elif a.cmd == "night":
            show(await ctl.night_light(a.target, a.level))
        elif a.cmd == "status":
            show(await ctl.status(a.target))
        elif a.cmd == "group":
            show(await ctl.group(a.group_id, a.action, a.color, a.brightness))
        elif a.cmd == "scene":
            show(await ctl.scene(a.index, save=(a.action == "save")))
        await asyncio.sleep(0.5)  # let write-without-response packets drain before disconnecting


def main():
    a = build_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING, format="%(message)s")
    if a.verbose:
        logging.getLogger("bleak").setLevel(logging.INFO)
    cfg = Config.load()
    try:
        if a.cmd == "setup":
            if a.setup_id:
                parse_app_id(a.setup_id)
                cfg.id = a.setup_id
            if a.setup_node:
                cfg.node = a.setup_node
            idle = idle_value(a)
            if idle is not None:
                cfg.idle_timeout = idle
            cfg.save()
            mode = cfg.idle()
            print("saved to %s (id: %s, connection: %s)" % (
                config_path(), "set" if cfg.id else "not set",
                "keep connected" if not mode else "disconnect after %gs idle" % mode))
        elif a.cmd == "name":
            if a.name_cmd == "add":
                cfg.set_name(a.name, a.vaddr)
                cfg.save()
            elif a.name_cmd == "rm":
                cfg.remove_name(a.name)
                cfg.save()
            for name, v in cfg.lights.items():
                print("%-12s %s" % (name, v))
        elif a.cmd == "mcp":
            from .mcp_server import main as mcp_main
            mcp_main()
        else:
            asyncio.run(run(a, cfg))
    except (ValueError, KeyError, LoginError, ConnectionError) as e:
        sys.exit("error: %s" % e)
    except ImportError as e:
        sys.exit("error: %s (install with: pip install 'odelic[mcp]')" % e)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
