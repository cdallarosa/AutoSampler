"""
ÄKTA I/O backends.

Each backend reads/writes *signals* described by a spec dict from
config/akta.json. Values are kept in ÄKTA logic as UNICORN shows it: for the
I/O-box E9, 1 = open circuit and 0 = closed circuit.

- OpcUaBackend: UNICORN OPC UA server (status, run data, optional writes)
- LabJackBackend: LabJack T4/T7 (LJM) or U3 (LabJackPython) wired to the I/O-box
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class Backend:
    name = "backend"

    def __init__(self):
        self.connected = False
        self.error: Optional[str] = None

    def connect(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        self.connected = False

    def read_many(self, specs: Dict[str, dict]) -> Dict[str, Any]:
        return {name: self.read(spec) for name, spec in specs.items()}

    def read(self, spec: dict) -> Any:
        raise NotImplementedError

    def write(self, spec: dict, value: Any) -> None:
        raise NotImplementedError


# ============================================================================
# OPC UA (UNICORN)
# ============================================================================

def _plain(value: Any) -> Any:
    """Convert OPC UA values (LocalizedText, enums, ...) to plain Python."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    text = getattr(value, "Text", None)  # LocalizedText
    if text is not None:
        return text
    if hasattr(value, "name") and hasattr(value, "value"):  # Enum
        return value.name
    return str(value)


def cert_application_uri(cert_path: str) -> Optional[str]:
    """The URI SubjectAltName of a client certificate (der or pem)."""
    from cryptography import x509

    raw = open(cert_path, "rb").read()
    cert = x509.load_der_x509_certificate(raw) if raw[:1] == b"\x30" else x509.load_pem_x509_certificate(raw)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    uris = san.get_values_for_type(x509.UniformResourceIdentifier)
    return uris[0] if uris else None


def configure_opcua_client(client, *, username: Optional[str] = None, password: Optional[str] = None,
                           security: Optional[dict] = None, security_string: Optional[str] = None):
    """
    Apply UNICORN's security requirements to an asyncua sync Client.

    UNICORN's servers publish only secured endpoints (Basic256Sha256 /
    Aes128_Sha256_RsaOaep, Sign or SignAndEncrypt; UserName or Certificate
    tokens, no anonymous), so a client certificate is mandatory:

    - the certificate must already be *trusted* by the server; a new
      self-signed one lands in UNICORN's rejected/ folder until an admin
      moves it to trusted/;
    - the client application_uri must equal the certificate's URI SAN, or the
      session is refused (read from the cert here);
    - the server certificate is needed for the secure channel; asyncua
      fetches it from endpoint discovery when ``server_cert`` is not given.
    """
    if security:
        from asyncua import ua
        from asyncua.crypto import security_policies as sp

        cert, key = security["cert"], security["key"]
        policy = getattr(sp, f"SecurityPolicy{security.get('policy', 'Basic256Sha256')}")
        mode = getattr(ua.MessageSecurityMode, security.get("mode", "SignAndEncrypt"))
        app_uri = security.get("application_uri") or cert_application_uri(cert)
        if app_uri:
            client.application_uri = app_uri
        client.set_security(policy, cert, key, server_certificate=security.get("server_cert"), mode=mode)
    elif security_string:
        client.set_security_string(security_string)
    if username:
        client.set_user(username)
    if password:
        client.set_password(password)


class OpcUaBackend(Backend):
    """
    Polls UNICORN OPC UA nodes. Node ids come from config (find them with
    ``python -m akta_autosampler.tools.opcua_browse <endpoint>``).
    """
    name = "opcua"

    def __init__(self, endpoint: str, username: Optional[str] = None,
                 password_env: Optional[str] = None, security_string: Optional[str] = None,
                 timeout_s: float = 4.0, security: Optional[dict] = None,
                 username_env: Optional[str] = None):
        super().__init__()
        self.endpoint = endpoint
        self.username = username
        self.username_env = username_env
        self.password_env = password_env
        self.security_string = security_string
        self.security = security
        self.timeout_s = timeout_s
        self._client = None
        self._nodes: Dict[str, Any] = {}

    def connect(self) -> None:
        from asyncua.sync import Client

        client = Client(self.endpoint, timeout=self.timeout_s)
        username = os.environ.get(self.username_env or "") or self.username
        try:
            configure_opcua_client(client, username=username,
                                   password=os.environ.get(self.password_env or "") or None,
                                   security=self.security, security_string=self.security_string)
            client.connect()
        except Exception:
            # The sync Client owns an event-loop thread; stop it or every failed
            # (re)connect attempt leaks a thread and blocks interpreter exit.
            try:
                client.disconnect()
            except Exception:
                pass
            raise
        self._client = client
        self._nodes.clear()
        self.connected = True
        self.error = None
        logger.info(f"OPC UA connected: {self.endpoint}")

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self.connected = False

    def _node(self, node_id: str):
        if node_id not in self._nodes:
            self._nodes[node_id] = self._client.get_node(node_id)
        return self._nodes[node_id]

    def read_many(self, specs: Dict[str, dict]) -> Dict[str, Any]:
        names = list(specs)
        if not names:
            return {}
        nodes = [self._node(specs[n]["node"]) for n in names]
        values = self._client.read_values(nodes)
        return {n: _plain(v) for n, v in zip(names, values)}

    def read(self, spec: dict) -> Any:
        return _plain(self._node(spec["node"]).read_value())

    def write(self, spec: dict, value: Any) -> None:
        from asyncua import ua

        node = self._node(spec["node"])
        variant_type = getattr(ua.VariantType, spec["type"]) if spec.get("type") else None
        if variant_type is None:
            node.write_value(value)
        else:
            node.write_value(ua.DataValue(ua.Variant(value, variant_type)))


# ============================================================================
# LabJack (wired to I/O-box E9)
# ============================================================================

class LabJackBackend(Backend):
    """
    Digital lines on a LabJack wired to the ÄKTA I/O-box E9 D-sub
    (DI 1-4 = pins 1-4, DO 1-4 = pins 6-9, common ground).

    Reading an ÄKTA *Digital out*: wire it to a LabJack line (internal
    pull-up). Closed contact -> 0, open -> 1, same as UNICORN.

    Driving an ÄKTA *Digital in* (open = 1, closed/0-0.8 V = 0): the default
    ``"drive": "open_drain"`` pulls the line low for 0 and releases it
    (input / high-Z) for 1, so the 3.3 V LabJack never has to meet the
    ÄKTA's 3.5 V logic-high threshold.
    """
    name = "labjack"

    def __init__(self, device: str = "T4", connection: str = "ANY", identifier: str = "ANY"):
        super().__init__()
        self.device = device.upper()
        self.connection = connection
        self.identifier = identifier
        self._handle = None  # LJM handle (T4/T7)
        self._u3 = None  # LabJackPython U3 device

    # -- connection ------------------------------------------------------

    def connect(self) -> None:
        if self.device == "U3":
            import u3  # LabJackPython + UD driver
            self._u3 = u3.U3()
            self._u3.configIO(FIOAnalog=0)  # all FIO digital
        else:
            from labjack import ljm
            self._handle = ljm.openS(self.device, self.connection, self.identifier)
        self.connected = True
        self.error = None
        logger.info(f"LabJack {self.device} connected")

    def disconnect(self) -> None:
        try:
            if self._handle is not None:
                from labjack import ljm
                ljm.close(self._handle)
            if self._u3 is not None:
                self._u3.close()
        except Exception:
            pass
        self._handle = None
        self._u3 = None
        self.connected = False

    # -- I/O -------------------------------------------------------------

    @staticmethod
    def _u3_index(line: str) -> int:
        """FIO0-7 -> 0-7, EIO0-7 -> 8-15, CIO0-3 -> 16-19."""
        line = line.upper()
        base = {"FIO": 0, "EIO": 8, "CIO": 16}[line[:3]]
        return base + int(line[3:])

    def read_many(self, specs: Dict[str, dict]) -> Dict[str, Any]:
        # Never read output lines: on a LabJack, reading a DIO makes it an input.
        inputs = {n: s for n, s in specs.items() if not s.get("output")}
        if self._handle is not None and inputs:
            from labjack import ljm
            names = list(inputs)
            values = ljm.eReadNames(self._handle, len(names), [inputs[n]["line"] for n in names])
            return {n: int(v) for n, v in zip(names, values)}
        return {n: self.read(s) for n, s in inputs.items()}

    def read(self, spec: dict) -> Any:
        line = spec["line"]
        if self._handle is not None:
            from labjack import ljm
            return int(ljm.eReadName(self._handle, line))
        return int(self._u3.getDIState(self._u3_index(line)))

    def write(self, spec: dict, value: Any) -> None:
        line = spec["line"]
        level = 1 if int(value) else 0
        open_drain = spec.get("drive", "open_drain") == "open_drain"
        if self._handle is not None:
            from labjack import ljm
            if open_drain and level == 1:
                ljm.eReadName(self._handle, line)  # release: reading makes the line an input
            else:
                ljm.eWriteName(self._handle, line, level)
        else:
            idx = self._u3_index(line)
            if open_drain and level == 1:
                self._u3.getDIState(idx)  # sets direction to input
            else:
                self._u3.setDOState(idx, level)


def make_backends(config: dict) -> List[Backend]:
    backends: List[Backend] = []
    if config.get("opcua", {}).get("endpoint"):
        o = config["opcua"]
        backends.append(OpcUaBackend(o["endpoint"], o.get("username"), o.get("password_env"),
                                     o.get("security_string"), o.get("timeout_s", 4.0),
                                     o.get("security"), o.get("username_env")))
    if config.get("labjack", {}).get("enabled", True) and "labjack" in config:
        lj = config["labjack"]
        backends.append(LabJackBackend(lj.get("device", "T4"), lj.get("connection", "ANY"),
                                       lj.get("identifier", "ANY")))
    return backends
