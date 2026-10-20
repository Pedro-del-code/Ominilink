import base64
import json
import time
import unittest

from app.api import create_app
from app.capabilities.profiles import (browser_receiver, constrained_receiver, legacy_receiver, python_receiver,
                                       python_sender)
from app.codec import get_codec
from tests.helpers import ENROLL_KEY, image_bytes, make_settings, noise_png


class ApiCase(unittest.TestCase):
    overrides: dict = {}

    def setUp(self):
        self.settings = make_settings(**self.overrides)
        self.app = create_app(self.settings)
        self.c = self.app.test_client()
        self.co = self.app.extensions["omnilink"].coordinator

    # ---- auxiliares
    def register(self, caps, key=ENROLL_KEY):
        resp = self.c.post("/v1/devices", json={"capabilities": caps.to_dict()}, headers={"X-Enrollment-Key": key})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    @staticmethod
    def H(dev):
        return {"Authorization": "Bearer " + dev["token"]}

    def pair(self, receiver_caps=None, grant=("text", "image", "html")):
        sender, receiver = self.register(python_sender()), self.register(receiver_caps or python_receiver())
        if grant:
            self.assertEqual(self.c.put(f"/v1/grants/{sender['device_id']}", json={"kinds": list(grant)}, headers=self.H(receiver)).status_code, 200)
        return sender, receiver

    def send(self, sender, receiver, kind="text", body="olá", encoding="utf-8", **extra):
        return self.c.post("/v1/messages", headers=self.H(sender),
                           json={"to": receiver["device_id"], "kind": kind, "body": body, "encoding": encoding, **extra})

    def send_image(self, sender, receiver, data, **extra):
        return self.send(sender, receiver, "image", base64.b64encode(data).decode(), "base64", **extra)

    def code(self, resp):
        return resp.get_json()["error"]["code"]


class PublicEndpointTests(ApiCase):
    def test_health(self):
        resp = self.c.get("/health")
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((body["status"], body["store"], body["persistent"]), ("ok", "memory", False))
        self.assertNotIn(ENROLL_KEY, resp.get_data(as_text=True))

    def test_health_reports_degraded_store(self):
        self.co.store.ping = lambda: False
        self.assertEqual(self.c.get("/health").status_code, 503)

    def test_index_receiver_page_and_static_assets(self):
        self.assertEqual(self.c.get("/").get_json()["receiver_page"], "/receiver")
        page = self.c.get("/receiver")
        self.addCleanup(page.close)
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.content_type)
        csp = page.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp); self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("<script>", page.get_data(as_text=True))          # sem script inline
        js = self.c.get("/static/receiver.js")
        self.addCleanup(js.close)
        self.assertEqual(js.status_code, 200); self.assertIn("Content-Security-Policy", js.headers)

    def test_security_headers_and_no_store(self):
        resp = self.c.get("/health")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(resp.headers["Cache-Control"], "no-store")

    def test_errors_are_json_without_internals(self):
        for resp, status, code in ((self.c.get("/nada"), 404, "NOT_FOUND"), (self.c.put("/health"), 405, "INVALID_REQUEST")):
            self.assertEqual(resp.status_code, status)
            self.assertEqual(self.code(resp), code)
            self.assertTrue(resp.get_json()["ok"] is False)

    def test_unexpected_exception_returns_generic_500(self):
        dev = self.register(python_receiver())
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        self.co.list_devices = lambda: 1 / 0
        resp = self.c.get("/v1/devices", headers=self.H(dev))
        self.assertEqual((resp.status_code, self.code(resp)), (500, "INTERNAL_ERROR"))
        self.assertNotIn("ZeroDivision", resp.get_data(as_text=True)); self.assertNotIn("Traceback", resp.get_data(as_text=True))


class RegistrationTests(ApiCase):
    def test_wrong_or_missing_enrollment_key(self):
        for headers in ({}, {"X-Enrollment-Key": "errada-errada-errada"}, {"X-Enrollment-Key": ""}):
            resp = self.c.post("/v1/devices", json={"capabilities": python_receiver().to_dict()}, headers=headers)
            self.assertEqual((resp.status_code, self.code(resp)), (401, "ENROLLMENT_DENIED"))

    def test_registration_returns_token_once_and_never_leaks_it(self):
        dev = self.register(python_receiver())
        self.assertRegex(dev["token"], r"^dev_[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$")
        other = self.register(python_sender())
        listing = self.c.get("/v1/devices", headers=self.H(other))
        text = listing.get_data(as_text=True)
        self.assertNotIn(dev["token"], text); self.assertNotIn("token_hash", text); self.assertNotIn(dev["token"].split(".")[1], text)
        caps = self.c.get(f"/v1/devices/{dev['device_id']}/capabilities", headers=self.H(other)).get_json()["capabilities"]
        self.assertEqual(caps["name"], "python-receiver")

    def test_invalid_payloads(self):
        K = {"X-Enrollment-Key": ENROLL_KEY}
        for body in ({}, {"capabilities": "x"}, {"capabilities": {"name": ""}}, {"capabilities": {"name": "a", "roles": ["root"]}}):
            self.assertEqual(self.c.post("/v1/devices", json=body, headers=K).status_code, 400, body)
        self.assertEqual(self.c.post("/v1/devices", data="{não é json", headers={**K, "Content-Type": "application/json"}).status_code, 400)
        self.assertEqual(self.c.post("/v1/devices", data="[1,2]", headers={**K, "Content-Type": "application/json"}).status_code, 400)


class RegistrationRateLimitTests(ApiCase):
    overrides = {"ip_rate_limit_per_minute": 3}

    def test_enrollment_attempts_are_rate_limited_per_ip(self):
        statuses = [self.c.post("/v1/devices", json={}, headers={"X-Enrollment-Key": "x" * 20}).status_code for _ in range(5)]
        self.assertEqual(statuses[:3], [401, 401, 401])
        self.assertEqual(statuses[3:], [429, 429])

    def test_failed_authentication_locks_the_ip_even_for_a_valid_token_afterwards(self):
        dev = self.c.post("/v1/devices", json={"capabilities": python_receiver().to_dict()}, headers={"X-Enrollment-Key": ENROLL_KEY}).get_json()
        bad = {"Authorization": "Bearer dev_0123456789abcdef." + "a" * 43}
        results = [self.c.get("/v1/devices", headers=bad).status_code for _ in range(3)]
        self.assertEqual(results, [401, 401, 401])
        resp = self.c.get("/v1/devices", headers=self.H(dev))
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Retry-After", resp.headers)


class AuthenticationTests(ApiCase):
    def test_every_protected_route_requires_a_valid_token(self):
        dev = self.register(python_receiver())
        routes = [("get", "/v1/devices"), ("get", f"/v1/devices/{dev['device_id']}/capabilities"), ("post", "/v1/negotiate"),
                  ("post", "/v1/messages"), ("get", "/v1/messages/msg_" + "a" * 24), ("get", "/v1/inbox"), ("get", "/v1/stream"),
                  ("post", "/v1/inbox/ack"), ("get", "/v1/grants"), ("put", "/v1/grants/" + dev["device_id"]),
                  ("delete", "/v1/devices/me"), ("post", "/v1/devices/me/rotate-token")]
        for method, path in routes:
            with self.subTest(route=f"{method} {path}"):
                resp = getattr(self.c, method)(path)
                self.assertEqual((resp.status_code, self.code(resp)), (401, "AUTH_REQUIRED"))
                self.assertIn("WWW-Authenticate", resp.headers)

    def test_malformed_and_wrong_credentials(self):
        dev = self.register(python_receiver())
        wrong_secret = {"Authorization": f"Bearer {dev['device_id']}." + "A" * 43}
        for headers in ({"Authorization": "Basic abc"}, {"Authorization": "Bearer lixo"}, wrong_secret):
            resp = self.c.get("/v1/devices", headers=headers)
            self.assertEqual(resp.status_code, 401)

    def test_revoked_and_rotated_credentials(self):
        a, b = self.register(python_receiver()), self.register(python_sender())
        rotated = self.c.post("/v1/devices/me/rotate-token", headers=self.H(a)).get_json()
        self.assertEqual(self.c.get("/v1/devices", headers=self.H(a)).status_code, 401)          # token antigo
        self.assertEqual(self.c.get("/v1/devices", headers=self.H(rotated)).status_code, 200)
        self.assertEqual(self.c.delete("/v1/devices/me", headers=self.H(rotated)).status_code, 200)
        resp = self.c.get("/v1/devices", headers=self.H(rotated))
        self.assertEqual((resp.status_code, self.code(resp)), (401, "DEVICE_REVOKED"))
        self.assertEqual(self.c.get("/v1/devices", headers=self.H(b)).status_code, 200)

    def test_admin_revocation_requires_the_enrollment_key(self):
        a, b = self.register(python_receiver()), self.register(python_sender())
        self.assertEqual(self.c.delete(f"/v1/admin/devices/{a['device_id']}", headers=self.H(b)).status_code, 401)
        self.assertEqual(self.c.delete(f"/v1/admin/devices/{a['device_id']}", headers={"X-Enrollment-Key": ENROLL_KEY}).status_code, 200)
        self.assertEqual(self.c.get("/v1/devices", headers=self.H(a)).status_code, 401)

    def test_token_expiry(self):
        dev = self.register(python_receiver())
        self.co.clock = lambda: time.time() + 100 * 86400
        resp = self.c.get("/v1/devices", headers=self.H(dev))
        self.assertEqual((resp.status_code, self.code(resp)), (401, "AUTH_EXPIRED"))


class AuthorizationFlowTests(ApiCase):
    def test_full_consent_flow(self):
        sender, receiver = self.pair(grant=None)
        resp = self.send(sender, receiver)
        self.assertEqual((resp.status_code, self.code(resp)), (403, "NOT_AUTHORIZED"))
        rid = resp.get_json()["error"]["detail"]["request_id"]
        pending = self.c.get("/v1/grants", headers=self.H(receiver)).get_json()["requests"]
        self.assertEqual([(p["id"], p["sender"], p["kinds"]) for p in pending], [(rid, sender["device_id"], ["text"])])
        self.assertEqual(self.c.post(f"/v1/grants/requests/{rid}", json={"decision": "approve"}, headers=self.H(receiver)).get_json()["status"], "approved")
        self.assertEqual(self.send(sender, receiver).status_code, 202)
        self.assertEqual(self.c.delete(f"/v1/grants/{sender['device_id']}", headers=self.H(receiver)).get_json()["revoked"], True)
        self.assertEqual(self.send(sender, receiver).status_code, 403)

    def test_no_content_reaches_the_receiver_before_approval(self):
        sender, receiver = self.pair(grant=None)
        self.send(sender, receiver, body="segredo-que-nao-deve-vazar")
        items = self.c.get("/v1/inbox?wait=0", headers=self.H(receiver)).get_json()["items"]
        self.assertEqual([i["type"] for i in items], ["grant.request"])
        self.assertNotIn("segredo-que-nao-deve-vazar", json.dumps(items))

    def test_only_the_receiver_decides(self):
        sender, receiver = self.pair(grant=None)
        rid = self.send(sender, receiver).get_json()["error"]["detail"]["request_id"]
        resp = self.c.post(f"/v1/grants/requests/{rid}", json={"decision": "approve"}, headers=self.H(sender))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.c.post(f"/v1/grants/requests/{rid}", json={"decision": "x"}, headers=self.H(receiver)).status_code, 400)
        self.assertEqual(self.c.post("/v1/grants/requests/lixo", json={"decision": "approve"}, headers=self.H(receiver)).status_code, 400)

    def test_deny_keeps_the_sender_out(self):
        sender, receiver = self.pair(grant=None)
        rid = self.send(sender, receiver).get_json()["error"]["detail"]["request_id"]
        self.c.post(f"/v1/grants/requests/{rid}", json={"decision": "deny"}, headers=self.H(receiver))
        self.assertEqual(self.send(sender, receiver).status_code, 403)
        self.assertEqual(self.c.get("/v1/grants", headers=self.H(receiver)).get_json()["requests"], [])

    def test_a_sender_cannot_grant_itself_access(self):
        sender, receiver = self.pair(grant=None)
        resp = self.c.put(f"/v1/grants/{sender['device_id']}", json={"kinds": ["text"]}, headers=self.H(sender))
        self.assertEqual(resp.status_code, 200)               # cria autorização *para o próprio sender como receptor*...
        self.assertEqual(self.send(sender, receiver).status_code, 403)    # ...o que não dá acesso ao receptor alheio


class SendingTests(ApiCase):
    def test_text_html_and_image_are_accepted_with_a_negotiation(self):
        sender, receiver = self.pair(browser_receiver())
        for resp in (self.send(sender, receiver), self.send(sender, receiver, "html", "<p>oi</p>"),
                     self.send_image(sender, receiver, image_bytes())):
            body = resp.get_json()
            self.assertEqual((resp.status_code, body["status"]), (202, "queued"))
            self.assertEqual(body["negotiation"]["selection"]["transport"], "sse")
            self.assertRegex(body["message_id"], r"^msg_[0-9a-f]{24}$")

    def test_html_is_sanitized_before_it_is_queued(self):
        sender, receiver = self.pair()
        resp = self.send(sender, receiver, "html", "<p onclick='x()'>oi</p><script>alert(1)</script>")
        self.assertEqual(resp.get_json()["adapted"], ["html_sanitized"])
        item = self.c.get("/v1/inbox?wait=0", headers=self.H(receiver)).get_json()["items"][0]
        self.assertEqual(item["body"], "<p>oi</p>")

    def test_invalid_requests(self):
        sender, receiver = self.pair()
        bad_bodies = [
            {"to": "dev_xxx", "kind": "text", "body": "x", "encoding": "utf-8"},
            {"to": receiver["device_id"], "kind": "video", "body": "x", "encoding": "utf-8"},
            {"to": receiver["device_id"], "kind": "text", "body": "", "encoding": "utf-8"},
            {"to": receiver["device_id"], "kind": "text", "body": 5, "encoding": "utf-8"},
            {"to": receiver["device_id"], "kind": "text", "body": "x", "encoding": "base64"},
            {"to": receiver["device_id"], "kind": "image", "body": "@@@", "encoding": "base64"},
            {"to": receiver["device_id"], "kind": "image", "body": "aGVsbG8=", "encoding": "base64"},
            {"to": receiver["device_id"], "kind": "text", "body": "x", "encoding": "utf-8", "client_id": "a b"},
        ]
        for body in bad_bodies:
            resp = self.c.post("/v1/messages", json=body, headers=self.H(sender))
            self.assertEqual(resp.status_code, 400, (body, resp.get_json()))
            self.assertFalse(resp.get_json()["ok"])

    def test_unknown_receiver(self):
        sender, _ = self.pair()
        resp = self.c.post("/v1/messages", headers=self.H(sender),
                           json={"to": "dev_" + "7" * 16, "kind": "text", "body": "x", "encoding": "utf-8"})
        self.assertEqual((resp.status_code, self.code(resp)), (404, "RECEIVER_UNKNOWN"))

    def test_hostile_images_are_rejected(self):
        sender, receiver = self.pair()
        good = image_bytes()
        cases = {"truncada": good[:50], "texto": b"nao sou imagem", "svg-com-script": b"<svg onload='alert(1)'/>",
                 "dados-anexados": good + b"<?php system($_GET[0]); ?>",
                 "bomba-de-pixels": image_bytes("PNG", (4000, 4000), (0, 0, 0))}
        for name, data in cases.items():
            resp = self.send_image(sender, receiver, data)
            self.assertEqual(resp.status_code, 400, name)
            self.assertEqual(self.code(resp), "INVALID_IMAGE", name)

    def test_oversized_inputs(self):
        sender, receiver = self.pair()
        resp = self.send(sender, receiver, body="x" * (self.settings.max_text_chars + 1))
        self.assertEqual((resp.status_code, self.code(resp)), (413, "SIZE_LIMIT_EXCEEDED"))
        resp = self.send(sender, receiver, "html", "<p>" + "x" * (self.settings.max_html_bytes + 1))
        self.assertEqual(resp.status_code, 413)
        resp = self.send(sender, receiver, "image", "A" * (self.settings.max_image_bytes * 2), "base64")
        self.assertEqual(resp.status_code, 413)

    def test_incompatible_receiver_returns_422_with_diagnosis_and_alternatives(self):
        sender, receiver = self.pair(legacy_receiver(), grant=("text",))
        resp = self.send(sender, receiver)
        body = resp.get_json()
        self.assertEqual((resp.status_code, body["status"]), (422, "incompatible"))
        codes = {d["code"] for d in body["negotiation"]["diagnostics"]}
        self.assertTrue({"PROTOCOL_VERSION_MISMATCH", "NO_COMMON_TRANSPORT"} <= codes)
        self.assertTrue(body["negotiation"]["alternatives"])
        self.assertEqual(self.c.get("/v1/inbox?wait=0", headers=self.H(receiver)).get_json()["items"], [])

    def test_adaptation_flag(self):
        sender, receiver = self.pair(constrained_receiver())
        resp = self.send(sender, receiver, "html", "<h1>x</h1>", allow_adaptation=False)
        self.assertEqual((resp.status_code, self.code(resp)), (422, "CONTENT_NEEDS_ADAPTATION"))
        resp = self.send(sender, receiver, "html", "<h1>x</h1>")
        self.assertIn("html_to_text", resp.get_json()["adapted"])

    def test_dry_run_negotiation_endpoint(self):
        sender, receiver = self.pair(constrained_receiver(), grant=None)
        resp = self.c.post("/v1/negotiate", headers=self.H(sender),
                           json={"to": receiver["device_id"], "kind": "image", "mime": "image/png", "size": 500000, "width": 800, "height": 600})
        neg = resp.get_json()["negotiation"]
        self.assertTrue(neg["compatible"]); self.assertEqual(neg["selection"]["adaptations"][0]["op"], "fit_image")
        self.assertEqual(self.c.post("/v1/negotiate", headers=self.H(sender), json={"to": receiver["device_id"], "kind": "bogus"}).status_code, 400)
        self.assertEqual(self.c.post("/v1/negotiate", headers=self.H(sender), json={"to": "dev_" + "1" * 16, "kind": "text"}).status_code, 404)

    def test_idempotent_retries_do_not_duplicate(self):
        sender, receiver = self.pair()
        first = self.send(sender, receiver, client_id="reenvio-1").get_json()
        second = self.send(sender, receiver, client_id="reenvio-1").get_json()
        self.assertTrue(second["duplicate"]); self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(len(self.c.get("/v1/inbox?wait=0", headers=self.H(receiver)).get_json()["items"]), 1)


class ReceivingTests(ApiCase):
    def test_json_inbox_ack_and_status(self):
        sender, receiver = self.pair()
        mid = self.send(sender, receiver, body="Olá, ação").get_json()["message_id"]
        inbox = self.c.get("/v1/inbox?wait=0", headers=self.H(receiver)).get_json()
        item = inbox["items"][0]
        self.assertEqual((inbox["transport"], inbox["codec"], item["type"], item["body"], item["id"]), ("http-longpoll", "json-b64", "message", "Olá, ação", mid))
        decoded = get_codec("json-b64").decode(json.dumps(item), max_bytes=10_000)
        self.assertEqual(decoded.data.decode(), "Olá, ação")
        self.assertEqual(self.c.get(f"/v1/messages/{mid}", headers=self.H(sender)).get_json()["status"], "sent")
        ack = self.c.post("/v1/inbox/ack", json={"id": mid, "status": "displayed"}, headers=self.H(receiver)).get_json()
        self.assertEqual(ack["status"], "displayed")
        view = self.c.get(f"/v1/messages/{mid}", headers=self.H(sender)).get_json()
        self.assertEqual((view["status"], view["delivered_via"]), ("displayed", "http-longpoll"))
        self.assertEqual(self.c.get("/v1/inbox?wait=0", headers=self.H(receiver)).get_json()["items"], [])

    def test_binary_codec_uses_descriptor_plus_frame_download(self):
        sender, receiver = self.pair()
        png = noise_png()
        mid = self.send_image(sender, receiver, png).get_json()["message_id"]
        item = self.c.get("/v1/inbox?wait=0&codec=binary-frame", headers=self.H(receiver)).get_json()["items"][0]
        self.assertEqual((item["encoding"], item["frame"], "body" in item), ("binary", f"/v1/messages/{mid}/frame", False))
        frame = self.c.get(item["frame"], headers=self.H(receiver))
        self.assertEqual(frame.content_type, "application/vnd.omnilink.frame")
        self.assertEqual(get_codec("binary-frame").decode(frame.data, max_bytes=2_000_000).data, png)
        self.assertLess(len(frame.data), len(json.dumps(get_codec("json-b64").to_dict(get_codec("binary-frame").decode(frame.data, max_bytes=2_000_000)))))

    def test_third_parties_cannot_read_status_frame_or_ack(self):
        sender, receiver = self.pair()
        spy = self.register(python_sender("espiao"))
        mid = self.send(sender, receiver).get_json()["message_id"]
        for resp in (self.c.get(f"/v1/messages/{mid}", headers=self.H(spy)), self.c.get(f"/v1/messages/{mid}/frame", headers=self.H(spy)),
                     self.c.post("/v1/inbox/ack", json={"id": mid, "status": "displayed"}, headers=self.H(spy))):
            self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.c.get(f"/v1/messages/{mid}/frame", headers=self.H(sender)).status_code, 404)

    def test_bad_ack_and_codec_parameters(self):
        sender, receiver = self.pair()
        mid = self.send(sender, receiver).get_json()["message_id"]
        self.assertEqual(self.c.post("/v1/inbox/ack", json={"id": mid, "status": "ok"}, headers=self.H(receiver)).status_code, 400)
        self.assertEqual(self.c.post("/v1/inbox/ack", json={"id": "x", "status": "displayed"}, headers=self.H(receiver)).status_code, 400)
        self.assertEqual(self.c.get("/v1/inbox?codec=zip", headers=self.H(receiver)).status_code, 400)
        self.assertEqual(self.c.get("/v1/inbox?wait=abc", headers=self.H(receiver)).status_code, 400)
        self.assertEqual(self.c.get("/v1/messages/lixo", headers=self.H(receiver)).status_code, 400)

    def test_message_status_long_wait_returns_when_acknowledged(self):
        sender, receiver = self.pair()
        mid = self.send(sender, receiver).get_json()["message_id"]
        self.c.get("/v1/inbox?wait=0", headers=self.H(receiver))
        self.c.post("/v1/inbox/ack", json={"id": mid, "status": "delivered"}, headers=self.H(receiver))
        t0 = time.monotonic()
        self.assertEqual(self.c.get(f"/v1/messages/{mid}?wait=10", headers=self.H(sender)).get_json()["status"], "delivered")
        self.assertLess(time.monotonic() - t0, 2)


class StreamTests(ApiCase):
    overrides = {"sse_max_seconds": 2, "sse_keepalive_seconds": 1}

    def events(self, resp, until, limit=12):
        out, buffer = [], ""
        for chunk in resp.iter_encoded():
            buffer += chunk.decode()
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                if block.startswith(":"):
                    continue
                fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
                out.append((fields.get("event"), json.loads(fields["data"]) if "data" in fields else None))
                if out[-1][0] == until or len(out) >= limit:
                    return out
        return out

    def test_sse_delivers_ready_grant_request_and_message_then_ends(self):
        sender, receiver = self.pair(grant=None)
        resp = self.c.get("/v1/stream", headers=self.H(receiver), buffered=False)
        self.assertTrue(resp.content_type.startswith("text/event-stream"))
        self.assertEqual(resp.headers["Cache-Control"], "no-cache, no-transform")
        self.send(sender, receiver)                                          # gera pedido de autorização
        self.c.put(f"/v1/grants/{sender['device_id']}", json={"kinds": ["text"]}, headers=self.H(receiver))
        self.send(sender, receiver, body="via sse")
        names = [(n, d.get("body") if n == "message" else None) for n, d in self.events(resp, "bye")]
        self.assertEqual(names[0][0], "ready")
        self.assertIn(("message", "via sse"), names)
        self.assertEqual(names[-1][0], "bye")

    def test_sse_announces_revocation_and_closes(self):
        _, receiver = self.pair()
        resp = self.c.get("/v1/stream", headers=self.H(receiver), buffered=False)
        stream = resp.iter_encoded()
        self.assertIn(b"event: ready", next(stream))
        self.c.delete(f"/v1/admin/devices/{receiver['device_id']}", headers={"X-Enrollment-Key": ENROLL_KEY})
        self.assertIn(b"event: revoked", next(stream))
        self.assertEqual(list(stream), [])


class LimitsTests(ApiCase):
    overrides = {"rate_limit_per_minute": 3, "max_request_bytes": 5000, "queue_max_per_device": 100}

    def test_message_rate_limit_per_device(self):
        sender, receiver = self.pair()
        statuses = [self.send(sender, receiver, body=f"m{i}").status_code for i in range(5)]
        self.assertEqual(statuses, [202, 202, 202, 429, 429])
        resp = self.send(sender, receiver)
        self.assertEqual(self.code(resp), "RATE_LIMITED"); self.assertIn("Retry-After", resp.headers)
        other = self.register(python_sender("outro"))
        self.c.put(f"/v1/grants/{other['device_id']}", json={"kinds": ["text"]}, headers=self.H(receiver))
        self.assertEqual(self.send(other, receiver).status_code, 202)      # limite é por dispositivo

    def test_request_body_limit(self):
        dev = self.register(python_receiver())
        resp = self.c.post("/v1/messages", data=json.dumps({"to": dev["device_id"], "kind": "text", "encoding": "utf-8", "body": "x" * 6000}),
                           headers={**self.H(dev), "Content-Type": "application/json"})
        self.assertEqual((resp.status_code, self.code(resp)), (413, "PAYLOAD_TOO_LARGE"))


class TransportSecurityTests(ApiCase):
    overrides = {"require_https": True, "trust_proxy": True}

    def test_plain_http_is_refused_but_health_checks_still_work(self):
        self.assertEqual(self.c.get("/health").status_code, 200)
        resp = self.c.get("/v1/devices")
        self.assertEqual((resp.status_code, self.code(resp)), (426, "TLS_REQUIRED"))
        self.assertEqual(self.c.get("/receiver").status_code, 426)

    def test_requests_forwarded_by_the_trusted_proxy_as_https_are_accepted(self):
        resp = self.c.post("/v1/devices", json={"capabilities": python_receiver().to_dict()},
                           headers={"X-Enrollment-Key": ENROLL_KEY, "X-Forwarded-Proto": "https"})
        self.assertEqual(resp.status_code, 201)
        self.assertIn("max-age", resp.headers["Strict-Transport-Security"])

    def test_forwarded_header_is_ignored_when_proxy_is_not_trusted(self):
        app = create_app(make_settings(require_https=True, trust_proxy=False))
        resp = app.test_client().get("/v1/devices", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(resp.status_code, 426)


class AuditTests(ApiCase):
    def test_audit_trail_records_events_without_secrets_or_content(self):
        sender, receiver = self.pair(grant=None)
        self.send(sender, receiver, body="conteudo-secreto-xyz")
        self.c.get("/v1/devices", headers={"Authorization": "Bearer dev_0123456789abcdef." + "a" * 43})
        self.assertEqual(self.c.get("/v1/admin/events").status_code, 401)
        events = self.c.get("/v1/admin/events?limit=50", headers={"X-Enrollment-Key": ENROLL_KEY}).get_json()["events"]
        names = {e["event"] for e in events}
        self.assertTrue({"device.registered", "grant.requested", "auth.failed"} <= names)
        blob = json.dumps(events)
        for secret in (sender["token"], sender["token"].split(".")[1], receiver["token"].split(".")[1], ENROLL_KEY, "conteudo-secreto-xyz"):
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()
