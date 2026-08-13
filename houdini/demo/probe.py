# SPDX-License-Identifier: MIT
"""Time a scene transfer with nothing but the socket in the way.

No Houdini, no USD, no caching -- it connects, asks for the scene, and prints
when each message arrives and how fast bytes are moving. If this is slow too,
the bridge is not the problem and the evidence is a twenty-line client.

    hython demo/probe.py 10.0.0.2          (or python3, if you have numpy-free deps)
    hython demo/probe.py 10.0.0.2 --minimal   advertise almost nothing
    hython demo/probe.py 10.0.0.2 --ping 10   send a keepalive every 10s

Ctrl-C to stop.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))

from nomad_link import transport  # noqa: E402

FULL = ["selection_transfer", "scene_transfer", "scene_edits", "object_state",
        "session_config", "mesh_delta_receive", "mesh_instance", "hierarchy",
        "scene_batch", "skew", "ngon", "material", "light", "camera_object",
        "texture", "display_config"]
MINIMAL = ["scene_transfer", "object_state"]


def main():
    args = sys.argv[1:]
    host = args[0] if args and not args[0].startswith("-") else ""
    minimal = "--minimal" in args
    ping_every = float(args[args.index("--ping") + 1]) if "--ping" in args else 0.0

    port = 48312
    if not host:
        print("searching for Nomad...")
        found = transport.discover(port, timeout=2.0)
        if not found:
            return "no Nomad answered; pass its address"
        host, port = found
    capabilities = MINIMAL if minimal else FULL
    print("connecting to %s:%d\ncapabilities: %s\nkeepalive: %s\n"
          % (host, port, ", ".join(capabilities),
             ("every %.1fs" % ping_every) if ping_every else "off"))

    link = transport.Connection("Probe", capabilities)
    link.connect(host, port, "", transport.VERSION, 1)

    started = time.monotonic()
    last = started
    last_ping = started
    counts = {}
    total = 0
    asked = False
    try:
        while True:
            time.sleep(0.01)
            now = time.monotonic()
            if link.status == "Error":
                return "connection failed: %s" % link.error
            for header, binary in link.poll():
                kind = header.get("type", "?")
                counts[kind] = counts.get(kind, 0) + 1
                total += len(binary)
                if kind == "hello":
                    print("paired with Nomad %s" % header.get("nomad_version"))
                elif kind == "pairing_pending":
                    print("waiting for approval in Nomad's Link menu...")
                name = header.get("name") or header.get("mesh_id", "")
                size = " %6.2f MB" % (len(binary) / 1048576.0) if binary else ""
                print("%7.2fs  +%6.3f  %-15s %-24s%s"
                      % (now - started, now - last, kind, str(name)[:24], size))
                last = now
            if link.status == "Connected" and not asked and now - started > 1.0:
                asked = True
                print("\n--- asking for the scene ---")
                link.send({"type": "request_scene", "request_id": "probe"})
            if ping_every and now - last_ping > ping_every:
                last_ping = now
                link.send({"type": "ping"})
            if asked and now - last > 20.0:
                break
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.monotonic() - started
        print("\n%.1f MB in %.1fs (%.2f MB/s)"
              % (total / 1048576.0, elapsed, total / 1048576.0 / max(elapsed, 0.01)))
        print("messages: %s" % ", ".join("%s x%d" % (k, v) for k, v in sorted(counts.items())))
        link.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
