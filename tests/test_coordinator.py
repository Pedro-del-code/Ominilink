import base64
import os
import tempfile
import threading
import time
import unittest

from app.capabilities.profiles import (browser_receiver, constrained_receiver, legacy_receiver, python_receiver,
                                       python_sender)
from app.content import verify_for_receiver
from app.coordinator.service import Coordinator, Incompatible
from app.coordinator.store import MemoryStore, SqlStore
from app.errors import AuthError, Forbidden, NotFound, OmniLinkError, ValidationFailure
from tests.helpers import image_bytes, make_settings, noise_png


class FakeClock:
    def __init__(self): self.now = 1_000_000.0
    def __call__(self): return self.now


class CoordinatorCase(unittest.TestCase):
    def build(self, store=None, **overrides):
        self.clock = FakeClock()
        self.store = store or MemoryStore()
        self.store.clock = self.clock
        self.co = Coordinator(make_settings(**overrides), self.store, clock=self.clock)

    def setUp(self):
        self.build()

    def device(self, caps):
        reg = self.co.register_device(caps.to_dict())
        return self.co.authenticate(reg["token"]), reg

    def pair(self, receiver_caps=None, kinds=("text", "image", "html")):
        sender, _ = self.device(python_sender())
        receiver, _ = self.device(receiver_caps or python_receiver())
        if kinds:
            self.co.put_grant(receiver, sender.id, list(kinds))
        return sender, receiver

    def text(self, sender, receiver, body="oi", **kw):
        return self.co.send(sender, {"to": receiver.id, "kind": "text", "encoding": "utf-8", "body": body, **kw})

    def status(self, who, message_id):
        return self.co.message_status(who, message_id)["status"]


class DeviceLifecycleTests(CoordinatorCase):
    def test_token_expiry_and_rotation(self):
        self.build(token_ttl_days=1)
        dev, reg = self.device(python_receiver())
        self.clock.now += 0.9 * 86400
        rotated = self.co.rotate_token(dev)
        with self.assertRaises(AuthError) as ctx:
            self.co.authenticate(reg["token"])           # token antigo deixa de valer
        self.assertEqual(ctx.exception.code, "AUTH_INVALID")
        self.clock.now += 0.6 * 86400                     # >1 dia desde o registro, <1 dia desde a rotação
        self.assertEqual(self.co.authenticate(rotated["token"]).id, dev.id)
        self.clock.now += 1.0 * 86400
        with self.assertRaises(AuthError) as ctx:
            self.co.authenticate(rotated["token"])
        self.assertEqual(ctx.exception.code, "AUTH_EXPIRED")

    def test_device_limit_and_revocation_frees_a_slot(self):
        self.build(max_devices=2)
        a, _ = self.device(python_receiver("a")); self.device(python_receiver("b"))
        with self.assertRaises(Forbidden) as ctx:
            self.co.register_device(python_receiver("c").to_dict())
        self.assertEqual(ctx.exception.code, "DEVICE_LIMIT")
        self.co.revoke_device(a.id, by="teste")
        self.co.register_device(python_receiver("c").to_dict())

    def test_stored_credentials_are_hashed_only(self):
        dev, reg = self.device(python_receiver())
        record = self.store.get("devices", dev.id)
        secret = reg["token"].split(".", 1)[1]
        self.assertNotIn(secret, str(record))
        self.assertEqual(len(record["token_hash"]), 64)

    def test_capability_update_is_validated(self):
        dev, _ = self.device(python_receiver())
        with self.assertRaises(ValidationFailure):
            self.co.update_capabilities(dev, {"name": "x", "roles": ["root"]})
        new = self.co.update_capabilities(dev, browser_receiver("novo-nome").to_dict())
        self.assertEqual(new["platform"], "browser")
        self.assertEqual(self.co.list_devices()[0]["name"], "novo-nome")

    def test_revocation_cleans_up_everything(self):
        sender, receiver = self.pair()
        self.text(sender, receiver)
        other, _ = self.device(python_sender("outro"))
        with self.assertRaises(Forbidden):
            self.text(other, receiver)                    # cria pedido pendente
        self.co.revoke_device(receiver.id, by="teste")
        self.assertEqual(self.store.count("grants"), 0)
        self.assertEqual(self.store.count("grant_requests"), 0)
        self.assertEqual(self.store.count("messages", receiver.id), 0)
        with self.assertRaises(AuthError) as ctx:
            self.co.collect_inbox(receiver)
        self.assertEqual(ctx.exception.code, "DEVICE_REVOKED")
        self.assertNotIn(receiver.id, [d["id"] for d in self.co.list_devices()])
        with self.assertRaises(NotFound):
            self.text(sender, receiver)


class AuthorizationTests(CoordinatorCase):
    def test_request_is_created_once_and_kinds_are_merged(self):
        sender, receiver = self.pair(kinds=None)
        with self.assertRaises(Forbidden) as first:
            self.text(sender, receiver)
        with self.assertRaises(Forbidden) as second:
            self.co.send(sender, {"to": receiver.id, "kind": "html", "encoding": "utf-8", "body": "<p>x</p>"})
        self.assertEqual(first.exception.diagnostic.detail["request_id"], second.exception.diagnostic.detail["request_id"])
        requests = self.co.list_grants(receiver)["requests"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["kinds"], ["html", "text"])

    def test_approval_is_limited_to_chosen_kinds(self):
        sender, receiver = self.pair(kinds=None)
        with self.assertRaises(Forbidden):
            self.text(sender, receiver)
        request = self.co.list_grants(receiver)["requests"][0]
        self.assertEqual(self.co.decide_grant(receiver, request["id"], "approve", ["text"])["status"], "approved")
        self.text(sender, receiver)
        with self.assertRaises(Forbidden):               # imagem não foi autorizada
            self.co.send(sender, {"to": receiver.id, "kind": "image", "encoding": "base64", "body": base64.b64encode(image_bytes()).decode()})

    def test_denial_blocks_new_notifications(self):
        sender, receiver = self.pair(kinds=None)
        with self.assertRaises(Forbidden) as first:
            self.text(sender, receiver)
        self.co.decide_grant(receiver, first.exception.diagnostic.detail["request_id"], "deny")
        with self.assertRaises(Forbidden) as again:
            self.text(sender, receiver)
        self.assertEqual(again.exception.diagnostic.detail["request_id"], first.exception.diagnostic.detail["request_id"])
        self.assertEqual(self.co.list_grants(receiver)["requests"], [])
        self.assertEqual(self.co.collect_inbox(receiver), [])      # nada foi notificado ao receptor

    def test_only_the_receiver_can_decide_and_decisions_are_final(self):
        sender, receiver = self.pair(kinds=None)
        with self.assertRaises(Forbidden):
            self.text(sender, receiver)
        rid = self.co.list_grants(receiver)["requests"][0]["id"]
        with self.assertRaises(NotFound):
            self.co.decide_grant(sender, rid, "approve")
        with self.assertRaises(ValidationFailure):
            self.co.decide_grant(receiver, rid, "talvez")
        self.co.decide_grant(receiver, rid, "approve")
        self.assertEqual(self.co.decide_grant(receiver, rid, "deny")["status"], "approved")   # idempotente

    def test_request_expires(self):
        sender, receiver = self.pair(kinds=None)
        with self.assertRaises(Forbidden) as first:
            self.text(sender, receiver)
        self.clock.now += 3601
        self.assertEqual(self.co.list_grants(receiver)["requests"], [])
        with self.assertRaises(Forbidden) as second:
            self.text(sender, receiver)
        self.assertNotEqual(first.exception.diagnostic.detail["request_id"], second.exception.diagnostic.detail["request_id"])

    def test_revoking_a_grant_takes_effect_immediately(self):
        sender, receiver = self.pair()
        self.text(sender, receiver)
        self.assertTrue(self.co.revoke_grant(receiver, sender.id))
        with self.assertRaises(Forbidden):
            self.text(sender, receiver)

    def test_pending_requests_are_capped_against_flooding(self):
        receiver, _ = self.device(python_receiver())
        for i in range(25):
            sender, _ = self.device(python_sender(f"s{i}"))
            with self.assertRaises(Forbidden):
                self.text(sender, receiver)
        self.assertLessEqual(len(self.co.list_grants(receiver)["requests"]), 20)

    def test_grant_for_unknown_sender_is_rejected(self):
        receiver, _ = self.device(python_receiver())
        with self.assertRaises(NotFound):
            self.co.put_grant(receiver, "dev_" + "9" * 16, ["text"])
        with self.assertRaises(ValidationFailure):
            self.co.put_grant(receiver, "dev_" + "9" * 16, ["video"])


class DeliveryTests(CoordinatorCase):
    def test_status_lifecycle_and_content_purge(self):
        sender, receiver = self.pair()
        mid = self.text(sender, receiver)["message_id"]
        self.assertEqual(self.status(sender, mid), "queued")
        (kind, msg), = self.co.collect_inbox(receiver, channel="sse")
        self.assertEqual((kind, msg.id, msg.data), ("message", mid, b"oi"))
        self.assertEqual(self.status(sender, mid), "sent")
        self.assertEqual(self.co.ack(receiver, mid, "delivered")["status"], "delivered")
        self.assertIsNone(self.store.get("messages", mid)["data_b64"])       # conteúdo não é retido após a entrega
        self.assertEqual(self.co.ack(receiver, mid, "displayed")["status"], "displayed")
        self.assertEqual(self.co.ack(receiver, mid, "delivered")["status"], "displayed")   # sem rebaixar

    def test_rejection_is_reported_to_the_sender(self):
        sender, receiver = self.pair()
        mid = self.text(sender, receiver)["message_id"]
        self.co.collect_inbox(receiver)
        view = self.co.ack(receiver, mid, "rejected", "INTEGRITY_FAILED")
        self.assertEqual((view["status"], view["reason"]), ("rejected", "INTEGRITY_FAILED"))
        self.assertEqual(self.co.message_status(sender, mid)["reason"], "INTEGRITY_FAILED")

    def test_only_participants_can_see_or_ack(self):
        sender, receiver = self.pair()
        stranger, _ = self.device(python_sender("intruso"))
        mid = self.text(sender, receiver)["message_id"]
        for who in (stranger,):
            with self.assertRaises(NotFound): self.co.message_status(who, mid)
            with self.assertRaises(NotFound): self.co.ack(who, mid, "displayed")
            with self.assertRaises(NotFound): self.co.message_frame(who, mid)
        with self.assertRaises(NotFound):
            self.co.ack(sender, mid, "displayed")          # emissor também não pode confirmar pelo receptor
        with self.assertRaises(ValidationFailure):
            self.co.ack(receiver, mid, "visto")

    def test_redelivery_until_acknowledged_with_attempt_limit(self):
        self.build(redeliver_after_seconds=20, max_delivery_attempts=2)
        sender, receiver = self.pair()
        mid = self.text(sender, receiver)["message_id"]
        self.assertEqual(len(self.co.collect_inbox(receiver)), 1)      # tentativa 1
        self.assertEqual(self.co.collect_inbox(receiver), [])          # ainda dentro da janela
        self.clock.now += 21
        self.assertEqual(len(self.co.collect_inbox(receiver)), 1)      # tentativa 2 (reentrega)
        self.clock.now += 21
        self.assertEqual(self.co.collect_inbox(receiver), [])          # limite atingido
        view = self.co.message_status(sender, mid)
        self.assertEqual((view["status"], view["reason"]), ("failed", "DELIVERY_ATTEMPTS_EXCEEDED"))
        self.assertIsNone(self.store.get("messages", mid)["data_b64"])

    def test_new_session_gets_everything_unacknowledged_immediately(self):
        sender, receiver = self.pair()
        self.text(sender, receiver)
        self.assertEqual(len(self.co.collect_inbox(receiver)), 1)
        self.assertEqual(self.co.collect_inbox(receiver), [])
        self.assertEqual(len(self.co.collect_inbox(receiver, resend_all=True)), 1)   # reconexão: reentrega já

    def test_ttl_expiry(self):
        self.build(queue_ttl_seconds=10)
        sender, receiver = self.pair()
        mid = self.text(sender, receiver)["message_id"]
        self.clock.now += 11
        self.assertEqual(self.co.collect_inbox(receiver), [])
        self.assertEqual(self.status(sender, mid), "expired")
        self.assertIsNone(self.store.get("messages", mid)["data_b64"])

    def test_expiry_is_detected_even_without_the_receiver_polling(self):
        self.build(queue_ttl_seconds=10)
        sender, receiver = self.pair()
        mid = self.text(sender, receiver)["message_id"]
        self.clock.now += 11
        self.assertEqual(self.status(sender, mid), "expired")

    def test_status_records_are_purged_after_retention(self):
        self.build(queue_ttl_seconds=10, status_retention_seconds=60)
        sender, receiver = self.pair()
        mid = self.text(sender, receiver)["message_id"]
        self.clock.now += 10 + 60 + 1
        self.store.purge_expired()
        with self.assertRaises(NotFound):
            self.co.message_status(sender, mid)

    def test_queue_limit(self):
        self.build(queue_max_per_device=3)
        sender, receiver = self.pair()
        for i in range(3):
            self.text(sender, receiver, f"m{i}")
        with self.assertRaises(OmniLinkError) as ctx:
            self.text(sender, receiver, "m3")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("QUEUE_FULL", 429))

    def test_idempotent_send_with_client_id(self):
        sender, receiver = self.pair()
        first = self.text(sender, receiver, client_id="envio-1")
        again = self.text(sender, receiver, client_id="envio-1")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["message_id"], first["message_id"])
        self.assertEqual(len(self.co.collect_inbox(receiver, limit=10)), 1)
        with self.assertRaises(ValidationFailure):
            self.text(sender, receiver, client_id="com espaço")

    def test_messages_arrive_in_order(self):
        sender, receiver = self.pair()
        for i in range(5):
            self.clock.now += 1
            self.text(sender, receiver, f"m{i}")
        self.assertEqual([m.data.decode() for _, m in self.co.collect_inbox(receiver, limit=10)], [f"m{i}" for i in range(5)])

    def test_offline_receiver_is_reported_as_warning_and_online_is_not(self):
        sender, receiver = self.pair()
        codes = [d["code"] for d in self.text(sender, receiver)["diagnostics"]]
        self.assertIn("RECEIVER_OFFLINE", codes)
        self.co.mark_active(receiver.id, "sse", 30)
        codes = [d["code"] for d in self.text(sender, receiver)["diagnostics"]]
        self.assertNotIn("RECEIVER_OFFLINE", codes)

    def test_binary_frame_download_matches_the_original(self):
        from app.codec import get_codec
        sender, receiver = self.pair()
        png = noise_png()
        mid = self.co.send(sender, {"to": receiver.id, "kind": "image", "encoding": "base64",
                                    "body": base64.b64encode(png).decode()})["message_id"]
        msg = get_codec("binary-frame").decode(self.co.message_frame(receiver, mid), max_bytes=2_000_000)
        self.assertEqual(msg.data, png)


class NegotiationIntegrationTests(CoordinatorCase):
    def test_incompatible_receiver_carries_full_diagnosis(self):
        sender, receiver = self.pair(legacy_receiver(), kinds=("text",))
        with self.assertRaises(Incompatible) as ctx:
            self.text(sender, receiver)
        result = ctx.exception.negotiation
        self.assertFalse(result.compatible)
        self.assertTrue(result.alternatives)
        self.assertEqual(self.store.count("messages", receiver.id), 0)      # nada foi enfileirado

    def test_adapted_content_always_fits_the_receiver(self):
        sender, receiver = self.pair(constrained_receiver())
        reply = self.co.send(sender, {"to": receiver.id, "kind": "image", "encoding": "base64",
                                      "body": base64.b64encode(noise_png((600, 400))).decode()})
        self.assertTrue(any(op.startswith("fit_image") for op in reply["adapted"]))
        (_, msg), = self.co.collect_inbox(receiver)
        verify_for_receiver(msg, constrained_receiver())
        self.assertEqual(msg.mime, "image/jpeg")

    def test_adaptation_can_be_disabled_per_message(self):
        sender, receiver = self.pair(constrained_receiver())
        with self.assertRaises(Incompatible) as ctx:
            self.co.send(sender, {"to": receiver.id, "kind": "html", "encoding": "utf-8", "body": "<p>x</p>",
                                  "allow_adaptation": False})
        self.assertEqual(ctx.exception.code, "CONTENT_NEEDS_ADAPTATION")
        with self.assertRaises(ValidationFailure):
            self.co.send(sender, {"to": receiver.id, "kind": "text", "encoding": "utf-8", "body": "x", "allow_adaptation": "sim"})

    def test_dry_run_requires_no_authorization_and_sends_nothing(self):
        sender, receiver = self.pair(kinds=None)
        neg = self.co.negotiate_preview(sender, {"to": receiver.id, "kind": "image", "mime": "image/png", "size": 900_000,
                                                 "width": 640, "height": 480})
        self.assertTrue(neg["compatible"])
        self.assertEqual(neg["selection"]["transport"], "http-longpoll")
        self.assertEqual(self.store.count("messages"), 0)
        self.assertEqual(self.store.count("grant_requests"), 0)

    def test_only_a_sender_role_device_can_send(self):
        receiver_only, _ = self.device(python_receiver("so-recebe"))
        target, _ = self.device(python_receiver("alvo"))
        self.co.put_grant(target, receiver_only.id, ["text"])
        with self.assertRaises(Incompatible) as ctx:
            self.text(receiver_only, target)
        self.assertEqual(ctx.exception.code, "ROLE_MISMATCH")


class WaitingTests(unittest.TestCase):
    """Usa relógio e threads reais: a espera deve acordar assim que a mensagem chega."""

    def setUp(self):
        self.co = Coordinator(make_settings(), MemoryStore())
        self.sender = self.co.authenticate(self.co.register_device(python_sender().to_dict())["token"])
        self.receiver = self.co.authenticate(self.co.register_device(python_receiver().to_dict())["token"])
        self.co.put_grant(self.receiver, self.sender.id, ["text"])

    def test_long_poll_wakes_up_on_new_message(self):
        result = {}

        def wait():
            t0 = time.monotonic()
            result["items"] = self.co.fetch_inbox(self.receiver, wait=5, limit=5, channel="http-longpoll")
            result["elapsed"] = time.monotonic() - t0

        thread = threading.Thread(target=wait); thread.start()
        time.sleep(0.3)
        self.co.send(self.sender, {"to": self.receiver.id, "kind": "text", "encoding": "utf-8", "body": "acorda"})
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result["items"]), 1)
        self.assertLess(result["elapsed"], 1.5)

    def test_long_poll_times_out_with_empty_list(self):
        t0 = time.monotonic()
        self.assertEqual(self.co.fetch_inbox(self.receiver, wait=0.4, limit=5, channel="http-longpoll"), [])
        self.assertGreaterEqual(time.monotonic() - t0, 0.35)

    def test_status_wait_returns_when_the_ack_arrives(self):
        mid = self.co.send(self.sender, {"to": self.receiver.id, "kind": "text", "encoding": "utf-8", "body": "x"})["message_id"]
        self.co.collect_inbox(self.receiver)
        out = {}
        thread = threading.Thread(target=lambda: out.update(self.co.message_status(self.sender, mid, wait=5)))
        thread.start(); time.sleep(0.3)
        self.co.ack(self.receiver, mid, "displayed")
        thread.join(3)
        self.assertEqual(out.get("status"), "displayed")

    def test_concurrent_senders_stay_consistent(self):
        others = [self.co.authenticate(self.co.register_device(python_sender(f"s{i}").to_dict())["token"]) for i in range(6)]
        for o in others:
            self.co.put_grant(self.receiver, o.id, ["text"])
        ids, lock = [], threading.Lock()

        def work(sender):
            for i in range(10):
                mid = self.co.send(sender, {"to": self.receiver.id, "kind": "text", "encoding": "utf-8", "body": f"{i}"})["message_id"]
                with lock:
                    ids.append(mid)

        threads = [threading.Thread(target=work, args=(o,)) for o in others]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(len(set(ids)), 60)
        delivered = []
        while True:
            batch = self.co.collect_inbox(self.receiver, limit=50)
            if not batch:
                break
            delivered += [m.id for _, m in batch]
        self.assertEqual(sorted(delivered), sorted(ids))


class PersistenceTests(unittest.TestCase):
    def test_everything_important_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "sqlite:///" + os.path.join(tmp, "omnilink.db")
            first = Coordinator(make_settings(), SqlStore(url))
            s_reg = first.register_device(python_sender().to_dict())
            r_reg = first.register_device(python_receiver().to_dict())
            sender, receiver = first.authenticate(s_reg["token"]), first.authenticate(r_reg["token"])
            first.put_grant(receiver, sender.id, ["text"])
            mid = first.send(sender, {"to": receiver.id, "kind": "text", "encoding": "utf-8", "body": "sobrevive"})["message_id"]

            second = Coordinator(make_settings(), SqlStore(url))        # "reinício" do servidor (ex.: spin-down)
            sender2, receiver2 = second.authenticate(s_reg["token"]), second.authenticate(r_reg["token"])
            (_, msg), = second.collect_inbox(receiver2)
            self.assertEqual((msg.id, msg.data), (mid, b"sobrevive"))
            second.send(sender2, {"to": receiver2.id, "kind": "text", "encoding": "utf-8", "body": "grant persistiu"})
            second.revoke_device(receiver2.id, by="teste")
            third = Coordinator(make_settings(), SqlStore(url))
            with self.assertRaises(AuthError) as ctx:
                third.authenticate(r_reg["token"])
            self.assertEqual(ctx.exception.code, "DEVICE_REVOKED")


if __name__ == "__main__":
    unittest.main()
