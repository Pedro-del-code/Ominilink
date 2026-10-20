import logging
import unittest

from app.config import ConfigError, Settings
from app.diagnostics import AuditLog, RedactingFilter, make, redact
from app.errors import AuthError, ValidationFailure
from app.security import RateLimiter, check_enrollment_key, hash_secret, issue_token, new_device_id, parse_token, verify_secret
from app.security.validation import (DEVICE_ID_RE, bounded_int, check_device_id, check_message_id, clean_filename,
                                     clean_label, string_list)


class FakeClock:
    def __init__(self): self.now = 1000.0
    def __call__(self): return self.now


class TokenTests(unittest.TestCase):
    def test_issue_parse_verify(self):
        device_id = new_device_id()
        self.assertTrue(DEVICE_ID_RE.match(device_id))
        token, stored = issue_token(device_id)
        parsed_id, secret = parse_token(token)
        self.assertEqual(parsed_id, device_id)
        self.assertTrue(verify_secret(secret, stored))
        self.assertNotIn(secret, stored)             # só o hash é persistido
        self.assertFalse(verify_secret(secret + "x", stored))
        self.assertFalse(verify_secret(secret, ""))

    def test_tokens_are_unique_and_high_entropy(self):
        secrets_seen = {issue_token("dev_" + "0" * 16)[0] for _ in range(200)}
        self.assertEqual(len(secrets_seen), 200)
        self.assertGreaterEqual(len(next(iter(secrets_seen)).split(".", 1)[1]), 43)

    def test_malformed_tokens(self):
        for bad in ("", "abc", "dev_0123456789abcdef", "dev_0123456789abcdef.curto", "x.y", None,
                    "dev_0123456789abcdef." + "a" * 100, "DEV_0123456789ABCDEF." + "a" * 43):
            with self.subTest(token=bad), self.assertRaises(AuthError):
                parse_token(bad)

    def test_enrollment_key_comparison(self):
        self.assertTrue(check_enrollment_key("segredo-longo-123", "segredo-longo-123"))
        for provided in (None, "", "segredo-longo-124", "x"):
            self.assertFalse(check_enrollment_key(provided, "segredo-longo-123"))
        self.assertFalse(check_enrollment_key("qualquer", ""))


class RateLimiterTests(unittest.TestCase):
    def test_burst_then_refill(self):
        clock = FakeClock()
        rl = RateLimiter(60, burst=3, clock=clock)
        self.assertEqual([rl.check("k")[0] for _ in range(4)], [True, True, True, False])
        allowed, retry = rl.check("k")
        self.assertFalse(allowed); self.assertGreater(retry, 0)
        clock.now += 2.0
        self.assertEqual([rl.check("k")[0] for _ in range(3)], [True, True, False])

    def test_keys_are_independent_and_blocked_does_not_consume(self):
        rl = RateLimiter(60, burst=1, clock=FakeClock())
        self.assertTrue(rl.check("a")[0]); self.assertFalse(rl.check("a")[0]); self.assertTrue(rl.check("b")[0])
        self.assertTrue(rl.blocked("a")[0]); self.assertTrue(rl.blocked("a")[0]); self.assertFalse(rl.blocked("zzz")[0])

    def test_disabled_and_bounded_memory(self):
        self.assertTrue(RateLimiter(0).check("x")[0])
        clock = FakeClock()
        rl = RateLimiter(60, burst=1, clock=clock, max_keys=50)
        for i in range(200):
            rl.check(f"k{i}")
        clock.now += 1000
        rl.check("novo")
        self.assertLess(len(rl._buckets), 60)


class ValidationTests(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(check_device_id("dev_0123456789abcdef"), "dev_0123456789abcdef")
        for bad in ("dev_xyz", "dev_0123456789ABCDEF", "../etc/passwd", None, 5, "dev_0123456789abcdef\n"):
            with self.subTest(v=bad), self.assertRaises(ValidationFailure):
                check_device_id(bad)
        with self.assertRaises(ValidationFailure):
            check_message_id("msg_curto")

    def test_labels_and_filenames(self):
        self.assertEqual(clean_label("  Sala\u200b de estar\x07 ", "name"), "Sala de estar")
        for bad in ("", "   ", "x" * 65, 3):
            with self.assertRaises(ValidationFailure):
                clean_label(bad, "name")
        self.assertEqual(clean_filename("../../etc/passwd"), "passwd")
        self.assertEqual(clean_filename("C:\\Users\\x\\foto?.png"), "foto_.png")
        self.assertIsNone(clean_filename("...")); self.assertIsNone(clean_filename(None))
        self.assertLessEqual(len(clean_filename("a" * 500 + ".png") or ""), 100)

    def test_trailing_newline_is_never_accepted(self):
        """Regressão: em Python, `$` casa antes de um '\\n' final; os padrões usam \\Z."""
        from app.capabilities import ContentCap
        from app.discovery.udp import _NAME_RE, _NONCE_RE, _URL_RE
        from app.security.validation import CLIENT_ID_RE, NAME_RE, check_request_id
        token, _ = issue_token("dev_" + "0" * 16)
        with self.assertRaises(AuthError):
            parse_token(token + "\n")
        with self.assertRaises(ValidationFailure): check_message_id("msg_" + "a" * 24 + "\n")
        with self.assertRaises(ValidationFailure): check_request_id("grq_" + "a" * 16 + "\n")
        with self.assertRaises(ValidationFailure): string_list(["sse\n"], "transports")
        with self.assertRaises(ValidationFailure): ContentCap.from_dict("text", {"max_bytes": 5, "mimes": ["text/plain\n"]})
        for pattern, value in ((CLIENT_ID_RE, "abc\n"), (NAME_RE, "abc\n"), (_NAME_RE, "nome\n"),
                               (_URL_RE, "http://a.b\n"), (_NONCE_RE, b"abcd\n")):
            self.assertIsNone(pattern.match(value), f"{pattern.pattern!r} aceitou {value!r}")

    def test_lists_and_ints(self):
        self.assertEqual(string_list(["a", "b", "a"], "x"), ("a", "b"))
        for bad in ("a", ["A"], [str(i) for i in range(17)], [1]):
            with self.assertRaises(ValidationFailure):
                string_list(bad, "x")
        self.assertEqual(bounded_int(5, "n", 1, 9), 5)
        for bad in (True, "5", 0, 10, 1.5):
            with self.assertRaises(ValidationFailure):
                bounded_int(bad, "n", 1, 9)


class LoggingTests(unittest.TestCase):
    def test_redaction_masks_credentials(self):
        secret = "dev_0123456789abcdef." + "Ab-_" * 11
        for line in (f"Authorization: Bearer {secret}", f"token={secret}", f"falhou {secret} fim",
                     "enrollment_key=minha-chave-secreta", "password: hunter2"):
            out = redact(line)
            self.assertNotIn(secret, out); self.assertNotIn("minha-chave-secreta", out); self.assertNotIn("hunter2", out)
            self.assertIn("***", out)

    def test_filter_applies_to_formatted_records(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s", ("dev_0123456789abcdef." + "a" * 43,), None)
        RedactingFilter().filter(record)
        self.assertNotIn("a" * 43, record.getMessage())

    def test_audit_log_is_bounded(self):
        audit = AuditLog(maxlen=5)
        for i in range(20):
            audit.emit("evento", n=i)
        self.assertEqual([e["n"] for e in audit.recent()], [15, 16, 17, 18, 19])

    def test_diagnostic_factory_and_unknown_code(self):
        d = make("RATE_LIMITED", retry_after=12.4)
        self.assertEqual((d.code, d.severity), ("RATE_LIMITED", "error")); self.assertIn("12", d.message)
        self.assertEqual(make("CODIGO_NOVO").message, "CODIGO_NOVO")
        self.assertIn("grq_x", make("NOT_AUTHORIZED", kind="text", request_id="grq_x").suggestion)


class ConfigTests(unittest.TestCase):
    def test_production_requires_strong_key_and_defaults_to_secure(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({"OMNILINK_ENV": "production"})
        with self.assertRaises(ConfigError):
            Settings.from_env({"OMNILINK_ENV": "production", "OMNILINK_ENROLLMENT_KEY": "curta"})
        s = Settings.from_env({"OMNILINK_ENV": "production", "OMNILINK_ENROLLMENT_KEY": "x" * 20})
        self.assertTrue(s.require_https); self.assertTrue(s.log_json); self.assertFalse(s.ephemeral_key)

    def test_development_generates_ephemeral_key(self):
        a, b = Settings.from_env({}), Settings.from_env({})
        self.assertTrue(a.ephemeral_key); self.assertNotEqual(a.enrollment_key, b.enrollment_key)
        self.assertFalse(a.require_https)

    def test_render_environment_enables_proxy_trust_and_database_url(self):
        s = Settings.from_env({"OMNILINK_ENV": "production", "OMNILINK_ENROLLMENT_KEY": "x" * 20, "RENDER": "true",
                               "DATABASE_URL": "postgresql://u:p@h/db"})
        self.assertTrue(s.trust_proxy); self.assertEqual(s.database_url, "postgresql://u:p@h/db")

    def test_invalid_values_fail_fast(self):
        for env in ({"OMNILINK_ENV": "staging"}, {"OMNILINK_MAX_IMAGE_BYTES": "abc"}, {"OMNILINK_MAX_IMAGE_BYTES": "0"},
                    {"OMNILINK_ENROLLMENT_KEY": "abc"}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                Settings.from_env(env)


if __name__ == "__main__":
    unittest.main()
