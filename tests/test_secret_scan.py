import codecs
import json
import random
import unittest

from olympus.preservation import PreservationError, guard_no_secrets
from olympus.secret_scan import StreamingSecretGuard


def complete_match(data):
    try:
        guard_no_secrets(data)
        return False
    except PreservationError:
        return True


def streamed_match(data, chunk):
    scanner = StreamingSecretGuard()
    decoder = codecs.getincrementaldecoder("utf8")(errors="ignore")
    try:
        for offset in range(0, len(data), chunk):
            scanner.feed(decoder.decode(data[offset:offset + chunk]))
        scanner.feed(decoder.decode(b"", final=True)); scanner.finish()
        return False
    except PreservationError:
        return True


class StreamingSecretTests(unittest.TestCase):
    def test_generated_contexts_agree_with_complete_detector(self):
        rng = random.Random(8092026)
        prefixes = ["ghp_", "sk-proj-", "AGE-SECRET-KEY-", "Bearer", "password", "client_secret", "https://", "-----BEGIN "]
        chars = "abcABC012_- .:/@=\n\t\"'éſЖ"
        for case in range(2000):
            value = ("".join(rng.choices(chars, k=rng.randrange(5))) + rng.choice(prefixes)
                     + "".join(rng.choices(chars, k=rng.randrange(90)))
                     + "".join(rng.choices(chars, k=rng.randrange(5)))).encode()
            with self.subTest(case=case):
                self.assertEqual(streamed_match(value, rng.randrange(1, 45)), complete_match(value))

    def test_streaming_matches_complete_policy_across_all_small_boundaries(self):
        examples = ["plain source", "ghp_" + "a" * 19, "ghp_" + "a" * 20,
            "ghp_" + "a" * 19 + "-", "ghp_" + "a" * 20 + "-", "ghp_" + "a" * 30 + "é",
            "aghp_" + "a" * 30, "Жghp_" + "a" * 30, "ghp_" + "-" * 30,
            "sk-proj-" + "a" * 16, "Bearer " + "a" * 20, "xBearer " + "a" * 20,
            "Bearer\n\t " + "a" * 19, 'password="' + "a" * 12, 'password""=' + "a" * 30,
            "client_secret = " + "a" * 11, "paſſword=" + "a" * 12,
            "https://user:password@host", "https://:password@host", "https://user:@host",
            "https://user:password/host@other", "xhttps://user:password@host",
            "AGE-SECRET-KEY-" + "A" * 30, "AGE-SECRET-KEY-" + "A" * 30 + "é",
            "AGE-SECRET-KEY-" + "A" * 30 + "ſ",
            "-----BEGIN PRIVATE KEY-----body-----END PRIVATE KEY-----",
            "-----BEGIN RSA PRIVATE KEY-----body-----END EC PRIVATE KEY-----",
            "-----END PRIVATE KEY-----safe-----BEGIN PRIVATE KEY-----",
            "-----BEGIN PRIVATE PRIVATE KEY-----body-----END RSA PRIVATE KEY-----"]
        data = [s.encode() for s in examples] + [b"github_\xffpat_" + b"a" * 30]
        for value in data:
            expected = complete_match(value)
            for chunk in (1, 2, 7, 16, 64):
                with self.subTest(case=data.index(value), chunk=chunk):
                    self.assertEqual(streamed_match(value, chunk), expected)

    def test_unbounded_fields_and_body_do_not_require_unbounded_buffer(self):
        cases = [b"password" + b" " * 200000 + b"=abcdefghijklm",
            b"Bearer" + b"\n" * 200000 + b"a" * 20,
            b"https://" + b"x" * 200000 + b":" + b"y" * 200000 + b"@host",
            b"-----BEGIN " + b"A" * 200000 + b" PRIVATE KEY-----" + b"x" * 200000 + b"-----END PRIVATE KEY-----",
            b"github_pat_" + b"x" * 200000 + "é".encode()]
        for case, value in enumerate(cases):
            with self.subTest(case=case):
                self.assertEqual(streamed_match(value, 4096), complete_match(value))

    def test_checkpoint_contains_no_source_boundary_and_resumes_match(self):
        scanner = StreamingSecretGuard()
        prefix = "private-context-marker password" + " " * 100
        scanner.feed(prefix)
        state = scanner.checkpoint()
        self.assertNotIn("private-context-marker", json.dumps(state))
        self.assertNotIn("tail", state)
        resumed = StreamingSecretGuard(state, previous_text=prefix[-512:])
        with self.assertRaisesRegex(PreservationError, "credential_pattern_detected"):
            resumed.feed("=abcdefghijkl")


if __name__ == "__main__":
    unittest.main()
