"""
Browse an OPC UA server (e.g. UNICORN) and list node ids, so you can fill in
the "node" fields in config/akta.json.

    python -m akta_autosampler.tools.opcua_browse opc.tcp://UNICORN-PC:4840
    python -m akta_autosampler.tools.opcua_browse opc.tcp://UNICORN-PC:4840 --find "digital|phase|state" --out nodes.txt
    python -m akta_autosampler.tools.opcua_browse opc.tcp://... --endpoints-only
    python -m akta_autosampler.tools.opcua_browse opc.tcp://... --cert client.der --key client_key.pem         --user-env AKTA_OPCUA_USER --password-env AKTA_OPCUA_PASSWORD
    python -m akta_autosampler.tools.opcua_browse opc.tcp://... --from-config   # use config/akta.json

UNICORN servers only offer secured endpoints, so --cert/--key (a client
certificate the server already trusts) are required for anything beyond
--endpoints-only. The application URI is taken from the certificate.

Each line shows: path, node id, node class, and for variables the data type,
current value and whether it is writable (W).
"""

import argparse
import os
import re
import sys
from typing import List, Optional, TextIO


def list_endpoints(endpoint: str, out: TextIO):
    """Endpoint discovery - needs no certificate or credentials."""
    from asyncua.sync import Client

    client = Client(endpoint, timeout=10)
    out.write(f"# {endpoint}\n")
    try:
        endpoints = client.connect_and_get_server_endpoints()
    finally:
        _close(client)
    for e in endpoints:
        policy = e.SecurityPolicyUri.rsplit("#", 1)[-1]
        tokens = ",".join(t.TokenType.name for t in e.UserIdentityTokens)
        out.write(f"{e.EndpointUrl} | {policy} | {e.SecurityMode.name} | tokens={tokens} | "
                  f"{e.Server.ApplicationName.Text} ({e.Server.ApplicationUri})\n")


def _close(client):
    """Disconnect and stop the sync Client's event-loop thread (also after a failed connect)."""
    try:
        client.disconnect()
    except Exception:
        pass


def browse(endpoint: str, out: TextIO, depth: int = 8, find: Optional[str] = None,
           user: Optional[str] = None, password: Optional[str] = None,
           security: Optional[str] = None, root: Optional[str] = None, max_nodes: int = 20000,
           cert: Optional[str] = None, key: Optional[str] = None, server_cert: Optional[str] = None,
           policy: str = "Basic256Sha256", mode: str = "SignAndEncrypt"):
    from asyncua import ua
    from asyncua.sync import Client

    from ..akta.backends import configure_opcua_client

    pattern = re.compile(find, re.I) if find else None
    client = Client(endpoint, timeout=10)
    sec = {"cert": cert, "key": key, "server_cert": server_cert, "policy": policy, "mode": mode} \
        if cert and key else None
    configure_opcua_client(client, username=user, password=password, security=sec,
                           security_string=security)
    try:
        client.connect()
    except Exception as e:
        _close(client)
        msg = f"{type(e).__name__}: {e}"
        if "BadSecurityChecksFailed" in msg or "BadCertificate" in msg:
            msg += ("\n  -> secure channel refused: the client certificate is not trusted by the "
                    "server (check UNICORN's rejected/ certificate folder) or has expired.")
        elif "BadIdentityTokenRejected" in msg or "BadUserAccessDenied" in msg:
            msg += "\n  -> certificate accepted; the user name / password was rejected."
        raise SystemExit(f"Connect failed: {msg}")
    print(f"Connected to {endpoint} - browsing (depth {depth})...", file=sys.stderr)
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
    p.add_argument("endpoint", nargs="?", help="e.g. opc.tcp://opcsrv:60434/OPC/HistoricalAccessServer")
    p.add_argument("--find", help="only print nodes whose path matches this regex")
    p.add_argument("--depth", type=int, default=8)
    p.add_argument("--root", help="start node id (default: Objects folder)")
    p.add_argument("--endpoints-only", action="store_true",
                   help="list the server's endpoints and exit (no certificate needed)")
    p.add_argument("--from-config", action="store_true",
                   help="take endpoint, certificate and credential env vars from config/akta.json")
    p.add_argument("--cert", help="client certificate (der/pem) already trusted by the server")
    p.add_argument("--key", help="client private key (pem)")
    p.add_argument("--server-cert", help="server certificate (default: fetched from discovery)")
    p.add_argument("--policy", default="Basic256Sha256")
    p.add_argument("--mode", default="SignAndEncrypt")
    p.add_argument("--user")
    p.add_argument("--user-env", help="environment variable holding the user name")
    p.add_argument("--password-env", help="environment variable holding the password")
    p.add_argument("--prompt-password", action="store_true",
                   help="ask for the password interactively (hidden input)")
    p.add_argument("--security", help='raw security string, e.g. "Basic256Sha256,SignAndEncrypt,cert.der,key.pem"')
    p.add_argument("--out", help="write to this file instead of stdout")
    args = p.parse_args(argv)

    if args.from_config:
        from ..akta import load_akta_config
        o = load_akta_config()["opcua"]
        sec = o.get("security") or {}
        args.endpoint = args.endpoint or o["endpoint"]
        args.cert, args.key = args.cert or sec.get("cert"), args.key or sec.get("key")
        args.server_cert = args.server_cert or sec.get("server_cert")
        args.user_env = args.user_env or o.get("username_env")
        args.password_env = args.password_env or o.get("password_env")
        args.user = args.user or o.get("username")
    if not args.endpoint:
        p.error("endpoint required (or --from-config)")

    user = (os.environ.get(args.user_env) if args.user_env else None) or args.user
    password = os.environ.get(args.password_env) if args.password_env else None
    if args.prompt_password and not args.endpoints_only:
        import getpass
        password = getpass.getpass(f"OPC UA password for {user or '(no user)'}: ")
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        if args.endpoints_only:
            list_endpoints(args.endpoint, out)
        else:
            browse(args.endpoint, out, args.depth, args.find, user, password, args.security, args.root,
                   cert=args.cert, key=args.key, server_cert=args.server_cert,
                   policy=args.policy, mode=args.mode)
    finally:
        if args.out:
            out.close()
            print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
