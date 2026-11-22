"""Build a synthetic C2PA-signed JPEG in tests (throwaway CA + leaf, never shipped, never organiser data)."""

from __future__ import annotations

import datetime
import io
from pathlib import Path

AI_SOURCE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
CAPTURE_SOURCE = "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"


def _name(cn: str):
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Synthetic Test Org"),
                      x509.NameAttribute(NameOID.COUNTRY_NAME, "PL")])


def _ku(ca: bool):
    from cryptography import x509

    return x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                         data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                         encipher_only=False, decipher_only=False)


def test_signer():
    """Return a c2pa.Signer backed by a fresh throwaway ES256 chain."""
    import c2pa
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID

    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("Synthetic CA")).issuer_name(_name("Synthetic CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(_ku(True), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(_name("Synthetic Signer")).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(_ku(False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    chain = leaf.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM)
    pk = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
    return c2pa.Signer.from_info(c2pa.C2paSignerInfo(alg=b"es256", sign_cert=chain, private_key=pk, ta_url=None))


def signed_jpeg(path: Path, *, ai: bool = True, generator: str = "SyntheticGen") -> Path:
    import c2pa
    from PIL import Image

    action = {"action": "c2pa.created", "digitalSourceType": AI_SOURCE if ai else CAPTURE_SOURCE}
    manifest = {"claim_generator_info": [{"name": generator, "version": "1.0"}], "title": path.name,
                "assertions": [{"label": "c2pa.actions", "data": {"actions": [action]}}]}
    src = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(src, "JPEG")
    src.seek(0)
    out = io.BytesIO()
    with c2pa.Builder(manifest) as b:
        b.sign(test_signer(), "image/jpeg", src, out)
    path.write_bytes(out.getvalue())
    return path


if __name__ == "__main__":
    import sys

    import c2pa

    p = signed_jpeg(Path(sys.argv[1]))
    with c2pa.Reader(str(p)) as r:
        print(r.json()[:4000])
        print(r.get_validation_state())
