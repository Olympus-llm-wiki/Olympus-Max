"""No live probes: real HTTP parsing over synthetic sockets and mocked DNS."""
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from olympus import sources
from olympus.preservation import Store, PreservationError

PUBLIC = "93.184.216.34"


class SyntheticSocket:
    def __init__(self, response):
        self.response = response
        self.sent = []
        self.timeouts = []
        self.connected = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def connect(self, target):
        self.connected = target

    def getpeername(self):
        return self.connected

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, mode, buffering=0):
        return io.BytesIO(self.response)

    def close(self):
        self.closed = True


def response(body=b"Synthetic full text.", *, status="200 OK", headers=None, length=True):
    values = {"Content-Type": "text/plain; charset=utf-8", "Connection": "close"}
    if length:
        values["Content-Length"] = str(len(body))
    values.update(headers or {})
    return ("HTTP/1.1 " + status + "\r\n" + "".join(k + ": " + v + "\r\n" for k, v in values.items()) + "\r\n").encode() + body


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state")

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, wire=None, *, url="http://source.example/path", dns=None, **kwargs):
        network = SyntheticSocket(wire or response())
        with patch("olympus.sources._lookup", return_value=dns or [[socket.AF_INET, PUBLIC]]) as lookup:
            with patch("olympus.sources.socket.socket", return_value=network):
                receipt = sources.capture_url(self.store, url, scope="synthetic", title="Synthetic source", **kwargs)
        return receipt, network, lookup

    def test_public_dns_is_pinned_and_host_path_are_preserved(self):
        receipt, network, lookup = self.capture(url="http://source.example:8080/path?q=one#section")
        self.assertEqual(network.connected, (PUBLIC, 8080))
        self.assertEqual(lookup.call_count, 1)
        request = b"".join(network.sent)
        self.assertIn(b"GET /path?q=one HTTP/1.1", request)
        self.assertEqual(request.count(b"Host:"), 1)
        self.assertIn(b"Host: source.example:8080", request)
        self.assertNotIn(b"section", request)
        self.assertEqual(self.store.read_version(receipt.version_id)["original"], b"Synthetic full text.")

    def test_dns_rebinding_cannot_trigger_a_second_resolution(self):
        network = SyntheticSocket(response())
        with patch("olympus.sources._lookup", side_effect=[[[socket.AF_INET, PUBLIC]], [[socket.AF_INET, "127.0.0.1"]]]) as lookup:
            with patch("olympus.sources.socket.socket", return_value=network):
                sources.capture_url(self.store, "http://changing.example/", scope="one", title="Synthetic")
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(network.connected[0], PUBLIC)

    def test_private_or_mixed_dns_answers_never_open_a_socket(self):
        for address in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.169.254", "100.64.1.2",
                        "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "fe80::1", "64:ff9b::7f00:1"):
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            with self.subTest(address=address):
                with patch("olympus.sources._lookup", return_value=[[socket.AF_INET, PUBLIC], [family, address]]):
                    with patch("olympus.sources.socket.socket") as open_socket:
                        with self.assertRaisesRegex(PreservationError, "source_nonpublic_address"):
                            sources.capture_url(self.store, "http://changing.example/", scope="one", title="Synthetic")
                        open_socket.assert_not_called()

    def test_literal_addresses_are_checked_without_dns(self):
        for url in ("http://127.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                    "http://[2002:7f00:1::]/", "http://localhost/", "http://metadata.google.internal/"):
            with self.subTest(url=url), patch("olympus.sources._lookup") as dns, patch("olympus.sources.socket.socket") as network:
                with self.assertRaises(PreservationError):
                    sources.capture_url(self.store, url, scope="one", title="Synthetic")
                dns.assert_not_called()
                network.assert_not_called()
        _, _, lookup = self.capture(url="http://" + PUBLIC + "/")
        lookup.assert_not_called()

    def test_https_uses_hostname_for_sni_certificate_verification_and_host(self):
        network = SyntheticSocket(response())
        context = Mock()
        context.wrap_socket.return_value = network
        with patch("olympus.sources._lookup", return_value=[[socket.AF_INET, PUBLIC]]):
            with patch("olympus.sources.socket.socket", return_value=network):
                with patch("olympus.sources.ssl.create_default_context", return_value=context) as factory:
                    sources.capture_url(self.store, "https://source.example/", scope="one", title="Synthetic")
        factory.assert_called_once_with()
        context.set_alpn_protocols.assert_called_once_with(["http/1.1"])
        context.wrap_socket.assert_called_once_with(network, server_hostname="source.example")
        self.assertEqual(network.connected, (PUBLIC, 443))
        self.assertIn(b"Host: source.example", b"".join(network.sent))
        actual = ssl.create_default_context()
        self.assertTrue(actual.check_hostname)
        self.assertEqual(actual.verify_mode, ssl.CERT_REQUIRED)

    def test_bad_tls_certificate_fails_without_storing_or_leaking_error(self):
        network = SyntheticSocket(response())
        context = Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("SYNTHETIC_PRIVATE_ERROR")
        with patch("olympus.sources._lookup", return_value=[[socket.AF_INET, PUBLIC]]), patch("olympus.sources.socket.socket", return_value=network), patch("olympus.sources.ssl.create_default_context", return_value=context):
            with self.assertRaisesRegex(PreservationError, "^source_fetch_failed$"):
                sources.capture_url(self.store, "https://source.example/", scope="one", title="Synthetic")
        self.assertEqual(self.store.status()["versions"], 0)

    def test_redirects_to_private_or_public_urls_are_not_followed(self):
        for destination in ("http://127.0.0.1/private", "https://other.example/public"):
            network = SyntheticSocket(response(status="302 Found", headers={"Location": destination}))
            with patch("olympus.sources._lookup", return_value=[[socket.AF_INET, PUBLIC]]) as lookup, patch("olympus.sources.socket.socket", return_value=network) as connection:
                with self.assertRaisesRegex(PreservationError, "source_redirect_requires_explicit_url"):
                    sources.capture_url(self.store, "http://source.example/", scope="one", title="Synthetic")
            self.assertEqual(lookup.call_count, 1)
            self.assertEqual(connection.call_count, 1)
        self.assertEqual(self.store.status()["versions"], 0)

    def test_proxy_environment_and_netrc_are_never_used(self):
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:9999", "HTTPS_PROXY": "http://127.0.0.1:9999", "NETRC": "/synthetic/credentials"}):
            _, network, _ = self.capture()
        request = b"".join(network.sent)
        self.assertEqual(network.connected, (PUBLIC, 80))
        self.assertNotIn(b"Authorization", request)
        self.assertNotIn(b"Cookie", request)
        self.assertNotIn(b"Proxy-", request)

    def test_credentials_or_auth_query_urls_are_rejected_before_dns(self):
        for url in ("http://user:password@source.example/", "http://user@source.example/",
                    "https://source.example/?api_key=short", "https://source.example/?X-Amz-Signature=abc",
                    "https://source.example/#access_token=short"):
            with self.subTest(url=url), patch("olympus.sources._lookup") as lookup:
                with self.assertRaises(PreservationError):
                    sources.capture_url(self.store, url, scope="one", title="Synthetic")
                lookup.assert_not_called()

    def test_invalid_urls_and_header_injection_are_rejected(self):
        for url in ("file:///etc/passwd", "ftp://source.example/file", "http://source.example:0/",
                    "http://source.example:65536/", "http://source.example/\r\nCookie: value",
                    "http://[fe80::1%25en0]/", "http://source.example/a b"):
            with self.subTest(url=url), patch("olympus.sources._lookup") as lookup:
                with self.assertRaises(PreservationError):
                    sources.capture_url(self.store, url, scope="one", title="Synthetic")
                lookup.assert_not_called()

    def test_body_size_limit_with_and_without_content_length(self):
        for length in (True, False):
            with self.subTest(length=length):
                with self.assertRaisesRegex(PreservationError, "source_size_limit"):
                    self.capture(response(b"0123456789", length=length), max_bytes=5)
        self.assertEqual(self.store.status()["versions"], 0)

    def test_partial_truncated_or_ambiguous_response_is_not_full_capture(self):
        cases = [
            (response(b"part", status="206 Partial Content"), "source_partial_response"),
            (response(b"part", headers={"Content-Length": "20"}), "source_incomplete_response"),
            (response(b"part", headers={"Content-Range": "bytes 0-3/99"}), "source_partial_response"),
            (response(b"part", headers={"Content-Encoding": "gzip"}), "source_content_encoding_requires_extractor"),
            (response(b"part", headers={"Transfer-Encoding": "chunked"}), "source_ambiguous_response_length"),
        ]
        for wire, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(PreservationError, code):
                self.capture(wire)
        self.assertEqual(self.store.status()["versions"], 0)

    def test_chunked_text_is_complete_and_raw_entity_bytes_are_preserved(self):
        wire = response(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n", length=False, headers={"Transfer-Encoding": "chunked"})
        receipt, _, _ = self.capture(wire)
        self.assertEqual(self.store.read_version(receipt.version_id)["original"], b"hello world")

    def test_html_original_is_exact_and_structural_text_preserves_code_indentation(self):
        html = b"<h1>Heading &amp; title</h1><script>not structural text</script><style>css</style><p>Full detail.</p><pre>  line one\n    line two</pre>"
        receipt, _, _ = self.capture(response(html, headers={"Content-Type": "text/html; charset=utf-8"}))
        saved = self.store.read_version(receipt.version_id)
        self.assertEqual(saved["original"], html)
        self.assertIn("Heading & title", saved["text"])
        self.assertIn("  line one\n    line two", saved["text"])
        self.assertNotIn("not structural text", saved["text"])
        self.assertEqual(saved["metadata"]["extraction"], "html-structural-text-v1")

    def test_declared_encoding_decodes_to_utf8_text_and_retains_original_bytes(self):
        raw = "Полный синтетический текст".encode("cp1251")
        receipt, _, _ = self.capture(response(raw, headers={"Content-Type": "text/plain; charset=windows-1251"}))
        saved = self.store.read_version(receipt.version_id)
        self.assertEqual(saved["original"], raw)
        self.assertEqual(saved["text"], "Полный синтетический текст")

    def test_pdf_binary_and_bad_charset_require_extraction(self):
        for wire, code in (
            (response(b"%PDF synthetic", headers={"Content-Type": "application/pdf"}), "source_requires_file_extractor"),
            (response(b"\x00binary"), "source_requires_file_extractor"),
            (response(b"\xff"), "source_encoding_requires_extractor"),
            (response(b"text", headers={"Content-Type": "text/plain; charset=invalid-charset"}), "source_encoding_requires_extractor"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(PreservationError, code):
                self.capture(wire)

    def test_error_body_reason_and_protocol_error_are_not_persisted_or_printed(self):
        for wire, code in (
            (response(b"SYNTHETIC_PRIVATE_ERROR_BODY", status="403 SYNTHETIC_PRIVATE_REASON"), "source_http_status_403"),
            (b"SYNTHETIC_PRIVATE_BAD_STATUS\r\n\r\n", "source_fetch_failed"),
        ):
            with self.assertRaisesRegex(PreservationError, "^" + code + "$"):
                self.capture(wire)
        self.assertFalse(list(self.store.versions.iterdir()))

    def test_repeated_fetch_deduplicates_observation_but_changed_bytes_preserve_version(self):
        headers = {"ETag": '"synthetic-v1"', "Last-Modified": "Sat, 05 Sep 2026 00:00:00 GMT"}
        one, _, _ = self.capture(response(b"First evidence", headers=headers))
        same, _, _ = self.capture(response(b"First evidence", headers=headers))
        changed, _, _ = self.capture(response(b"Different evidence", headers=headers))
        self.assertEqual(one.version_id, same.version_id)
        self.assertNotEqual(one.version_id, changed.version_id)
        self.assertEqual(one.source_id, changed.source_id)
        self.assertFalse(self.store.pending_changes())

    def test_known_secret_in_public_response_is_rejected_before_store(self):
        with self.assertRaisesRegex(PreservationError, "credential_pattern_detected"):
            self.capture(response(("github_pat_" + "a" * 40).encode()))
        self.assertFalse(list(self.store.versions.iterdir()))

    def test_dns_helper_timeout_is_bounded_and_stderr_is_not_exposed(self):
        with patch("olympus.sources.subprocess.run", side_effect=subprocess.TimeoutExpired("synthetic", 1)) as run:
            with self.assertRaisesRegex(PreservationError, "source_timeout"):
                sources.capture_url(self.store, "http://source.example/", scope="one", title="Synthetic", timeout=1)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 1)
        self.assertEqual(run.call_args.args[0][1:3], ["-I", "-c"])

    def test_malformed_dns_output_and_dns_errors_are_safe(self):
        for value in (b"not json", b"[]", b"{}", b'[[2,"127.0.0.1"]]'):
            with patch("olympus.sources.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=value)):
                with self.assertRaises(PreservationError):
                    sources.capture_url(self.store, "http://source.example/", scope="one", title="Synthetic")

    def test_deadline_applies_during_many_small_header_reads(self):
        clock = [0.0]
        class Drip(io.RawIOBase):
            def __init__(self):
                self.buffer = io.BytesIO(response())
            def readable(self):
                return True
            def readinto(self, target):
                clock[0] += 0.2
                chunk = self.buffer.read(1)
                target[:len(chunk)] = chunk
                return len(chunk)
        network = SyntheticSocket(response())
        network.makefile = lambda mode, buffering=0: Drip()
        with patch("olympus.sources.time.monotonic", side_effect=lambda: clock[0]), patch("olympus.sources._lookup", return_value=[[socket.AF_INET, PUBLIC]]), patch("olympus.sources.socket.socket", return_value=network):
            with self.assertRaisesRegex(PreservationError, "source_timeout"):
                sources.capture_url(self.store, "http://source.example/", scope="one", title="Synthetic", timeout=1)
        self.assertLess(clock[0], 1.5)
        self.assertEqual(self.store.status()["versions"], 0)

    def test_invalid_limits_are_rejected(self):
        for options in ({"timeout": 0}, {"timeout": float("inf")}, {"max_bytes": 0}, {"timeout": 61}):
            with self.assertRaises(PreservationError):
                sources.capture_url(self.store, "http://source.example/", scope="one", title="Synthetic", **options)


if __name__ == "__main__":
    unittest.main()
