"""CLI:  python -m odelic --id <APP_ID> <command> ...   (or set ODELIC_ID)"""
import argparse
import asyncio
import logging
import os
import sys

from . import OdelicMesh, find_nodes
from .protocol import BROADCAST, parse_app_id


def build_parser():
    ap = argparse.ArgumentParser(prog="odelic", description="Control ODELIC Bluetooth lights")
    ap.add_argument("--id", default=os.environ.get("ODELIC_ID"), help="ID shown in the app (ID:xxxx), or $ODELIC_ID")
    ap.add_argument("--node", help="BLE name/address of the node to connect through")
    ap.add_argument("-t", "--target", default="ffffffff", help="light vaddr (hex, see `devices`); default: all")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="list nearby mesh nodes (no login)")
    sub.add_parser("devices", help="list lights in the mesh")
    sub.add_parser("on")
    sub.add_parser("off")
    p = sub.add_parser("light", help="set colour/brightness")
    p.add_argument("--color", type=int, default=50, help="0 = warm (電球色) .. 100 = cool (昼光色)")
    p.add_argument("--brightness", type=int, default=100, help="1..100 %%")
    p = sub.add_parser("night", help="night light")
    p.add_argument("level", type=int, nargs="?", default=0, choices=[0, 1, 2])
    p = sub.add_parser("group", help="control a group")
    p.add_argument("group_id", type=int)
    p.add_argument("action", choices=["on", "off", "light"])
    p.add_argument("--color", type=int, default=50)
    p.add_argument("--brightness", type=int, default=100)
    p = sub.add_parser("scene", help="save or play a scene")
    p.add_argument("action", choices=["save", "play"])
    p.add_argument("index", type=int)
    p = sub.add_parser("status", help="raw status of one light (-t required)")
    return ap


async def run(a):
    if a.cmd == "scan":
        homeid = parse_app_id(a.id)[0] if a.id else None
        for dev, hid, rssi in await find_nodes(homeid):
            print("%-20s %s  HomeID=%d  rssi=%d" % (dev.name, dev.address, hid, rssi))
        return
    if not a.id:
        sys.exit("--id (or ODELIC_ID) is required")
    target = bytes.fromhex(a.target)
    async with OdelicMesh(a.id, device=a.node) as m:
        print("logged in via %s (own vaddr %s)" % (m.node_name, m.own.hex()))
        if a.cmd == "devices":
            for d in await m.devices():
                print(d)
        elif a.cmd == "on":
            await m.on(target)
        elif a.cmd == "off":
            await m.off(target)
        elif a.cmd == "light":
            await m.set_light(a.color, a.brightness, target)
        elif a.cmd == "night":
            await m.night_light(a.level, target)
        elif a.cmd == "group":
            if a.action == "light":
                await m.set_group(a.group_id, a.color, a.brightness)
            else:
                await m.set_group(a.group_id, on=(a.action == "on"))
        elif a.cmd == "scene":
            await (m.save_scene if a.action == "save" else m.play_scene)(a.index)
        elif a.cmd == "status":
            if target == BROADCAST:
                sys.exit("status needs -t <vaddr>")
            for p in await m.request_status(target):
                print(p.hex())
        await asyncio.sleep(0.5)  # let write-without-response packets drain


def main():
    a = build_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING, format="%(message)s")
    if a.verbose:
        logging.getLogger("bleak").setLevel(logging.INFO)
    asyncio.run(run(a))


if __name__ == "__main__":
    main()
