"""
Browse an OPC UA server (e.g. UNICORN) and list node ids, so you can fill in
the "node" fields in config/akta.json.

    python -m akta_autosampler.tools.opcua_browse opc.tcp://UNICORN-PC:4840
    python -m akta_autosampler.tools.opcua_browse opc.tcp://UNICORN-PC:4840 --find "digital|phase|state" --out nodes.txt
    python -m akta_autosampler.tools.opcua_browse opc.tcp://... --user me --password-env AKTA_OPCUA_PASSWORD

Each line shows: path, node id, node class, and for variables the data type,
current value and whether it is writable (W).
"""

import argparse
import os
import re
import sys
from typing import List, Optional, TextIO


def browse(endpoint: str, out: TextIO, depth: int = 8, find: Optional[str] = None,
           user: Optional[str] = None, password: Optional[str] = None,
           security: Optional[str] = None, root: Optional[str] = None, max_nodes: int = 20000):
    from asyncua import ua
    from asyncua.sync import Client

    pattern = re.compile(find, re.I) if find else None
    client = Client(endpoint, timeout=10)
    if user:
        client.set_user(user)
    if password:
        client.set_password(password)
    if security:
        client.set_security_string(security)
    client.connect()
    try:
        ns = client.get_namespace_array()
        out.write(f"# {endpoint}\n# namespaces:\n")
        for i, uri in enumerate(ns):
            out.write(f"#   ns={i}  {uri}\n")
        out.write("#\n# path | node id | class | type | value | access\n")

        start = client.get_node(root) if root else client.get_objects_node()
        seen = set()
        count = 0

        def visit(node, path: List[str], level: int):
            nonlocal count
            key = node.nodeid.to_string()
            if key in seen or count >= max_nodes:
                return
            seen.add(key)
            count += 1
            try:
                name = node.read_display_name().Text or node.read_browse_name().Name
                node_class = node.read_node_class()
            except Exception as e:
                out.write(f"{'/'.join(path)}/? | {key} | <error {e}>\n")
                return
            here = path + [name]
            line_path = "/".join(here)

            if pattern is None or pattern.search(line_path):
                detail = ""
                if node_class == ua.NodeClass.Variable:
                    try:
                        vtype = node.read_data_type_as_variant_type().name
                    except Exception:
                        vtype = "?"
                    try:
                        value = repr(node.read_value())
                        value = value if len(value) < 80 else value[:77] + "..."
                    except Exception as e:
                        value = f"<{type(e).__name__}>"
                    try:
                        access = node.get_user_access_level()
                        rw = ("R" if ua.AccessLevel.CurrentRead in access else "") + \
                             ("W" if ua.AccessLevel.CurrentWrite in access else "")
                    except Exception:
                        rw = "?"
                    detail = f" | {vtype} | {value} | {rw}"
                out.write(f"{line_path} | {key} | {node_class.name}{detail}\n")
                out.flush()

            if level < depth and node_class in (ua.NodeClass.Object, ua.NodeClass.Variable):
                try:
                    children = node.get_children()
                except Exception:
                    children = []
                for child in children:
                    visit(child, here, level + 1)

        visit(start, [], 0)
        out.write(f"# {count} nodes{' (limit reached)' if count >= max_nodes else ''}\n")
    finally:
        client.disconnect()


def main(argv=None):
    p = argparse.ArgumentParser(description="Browse an OPC UA server and list node ids")
    p.add_argument("endpoint", help="e.g. opc.tcp://UNICORN-PC:4840")
    p.add_argument("--find", help="only print nodes whose path matches this regex")
    p.add_argument("--depth", type=int, default=8)
    p.add_argument("--root", help="start node id (default: Objects folder)")
    p.add_argument("--user")
    p.add_argument("--password-env", help="environment variable holding the password")
    p.add_argument("--security", help='e.g. "Basic256Sha256,SignAndEncrypt,cert.der,key.pem"')
    p.add_argument("--out", help="write to this file instead of stdout")
    args = p.parse_args(argv)

    password = os.environ.get(args.password_env) if args.password_env else None
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        browse(args.endpoint, out, args.depth, args.find, args.user, password, args.security, args.root)
    finally:
        if args.out:
            out.close()
            print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
