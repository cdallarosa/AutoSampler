"""Secured OPC UA (UNICORN-style: Basic256Sha256 + SignAndEncrypt only) against a local server."""

import datetime
import io
import socket

import pytest

from akta_autosampler.akta import AktaLink
from akta_autosampler.akta.backends import cert_application_uri


def _self_signed(tmp_path, name: str, uri: str):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(uri), x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True,
                                     key_encipherment=True, data_encipherment=True,
                                     key_agreement=False, key_cert_sign=False, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH,
                                              ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / f"{name}.der", tmp_path / f"{name}_key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def secure_server(tmp_path):
    from asyncua import ua
    from asyncua.sync import Server

    srv_cert, srv_key = _self_signed(tmp_path, "server", "urn:test:UA:HistoricalAccessServer")
    port = _free_port()
    server = Server()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}/OPC/HistoricalAccessServer")
    server.set_server_name("UNICORN OPC SERVER - HDA (test)")
    server.set_security_policy([ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
    server.load_certificate(srv_cert)
    server.load_private_key(srv_key)
    idx = server.register_namespace("urn:test:unicorn")
    server.nodes.objects.add_variable(f"ns={idx};s=RunState", "RunState", "Running")
    server.start()
    yield {"endpoint": f"opc.tcp://127.0.0.1:{port}/OPC/HistoricalAccessServer", "idx": idx,
           "tmp": tmp_path}
    server.stop()


def test_cert_application_uri(tmp_path):
    cert, _ = _self_signed(tmp_path, "client", "urn:SI-TEST:UnifiedAutomation:UaExpert")
    assert cert_application_uri(cert) == "urn:SI-TEST:UnifiedAutomation:UaExpert"


def test_secured_link_reads(secure_server):
    cert, key = _self_signed(secure_server["tmp"], "client", "urn:SI-TEST:UnifiedAutomation:UaExpert")
    cfg = {
        "opcua": {"endpoint": secure_server["endpoint"], "timeout_s": 10,
                  "security": {"policy": "Basic256Sha256", "mode": "SignAndEncrypt",
                               "cert": cert, "key": key, "server_cert": None}},
        "signals": {"run_state": {"source": "opcua", "node": f"ns={secure_server['idx']};s=RunState"}},
        "poll_s": 0.05,
    }
    link = AktaLink(cfg)
    try:
        assert link.connect(), link.status()["backends"]
        assert link.wait_for("run_state", {"equals": "Running"}, timeout=5)
        client = link.backends["opcua"]._client
        assert client.application_uri == "urn:SI-TEST:UnifiedAutomation:UaExpert"
    finally:
        link.disconnect()


def test_unsecured_client_is_refused(secure_server):
    cfg = {"opcua": {"endpoint": secure_server["endpoint"], "timeout_s": 5},
           "signals": {"run_state": {"source": "opcua", "node": "ns=2;s=RunState"}}}
    link = AktaLink(cfg)
    try:
        assert not link.connect()
    finally:
        link.disconnect()


def test_browse_endpoints_and_secured_browse(secure_server):
    from akta_autosampler.tools.opcua_browse import browse, list_endpoints

    out = io.StringIO()
    list_endpoints(secure_server["endpoint"], out)
    assert "Basic256Sha256 | SignAndEncrypt" in out.getvalue()

    cert, key = _self_signed(secure_server["tmp"], "client2", "urn:SI-TEST:browse")
    out = io.StringIO()
    browse(secure_server["endpoint"], out, find="RunState", cert=cert, key=key)
    assert "s=RunState" in out.getvalue()
