import os
import tempfile
import time
import unittest

from app.coordinator.store import MemoryStore, SqlStore, open_store


class StoreContract:
    """Contrato comum: os dois armazenamentos devem se comportar de forma idêntica."""

    def make(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make()
        self.now = 1000.0
        self.store.clock = lambda: self.now

    def test_put_get_overwrite_delete(self):
        s = self.store
        self.assertIsNone(s.get("c", "k"))
        s.put("c", "k", {"a": 1, "txt": "ação ✓"}, owner="o")
        self.assertEqual(s.get("c", "k"), {"a": 1, "txt": "ação ✓"})
        s.put("c", "k", {"a": 2}, owner="o2")
        self.assertEqual(s.get("c", "k"), {"a": 2})
        self.assertEqual(s.count("c", "o"), 0); self.assertEqual(s.count("c", "o2"), 1)
        self.assertTrue(s.delete("c", "k")); self.assertFalse(s.delete("c", "k"))

    def test_collections_and_owners_are_isolated(self):
        s = self.store
        s.put("a", "1", {"v": 1}, owner="x"); s.put("a", "2", {"v": 2}, owner="y"); s.put("b", "1", {"v": 3}, owner="x")
        self.assertEqual(sorted(d["v"] for d in s.list("a")), [1, 2])
        self.assertEqual([d["v"] for d in s.list("a", "x")], [1])
        self.assertEqual([d["v"] for d in s.list("b")], [3])

    def test_values_are_copies(self):
        s = self.store
        original = {"lista": [1]}
        s.put("c", "k", original)
        original["lista"].append(2)
        got = s.get("c", "k"); got["lista"].append(3)
        self.assertEqual(s.get("c", "k"), {"lista": [1]})

    def test_expiration(self):
        s = self.store
        s.put("c", "curto", {"v": 1}, expires_at=1005.0); s.put("c", "longo", {"v": 2}, expires_at=2000.0); s.put("c", "eterno", {"v": 3})
        self.assertEqual(s.count("c"), 3)
        self.now = 1010.0
        self.assertIsNone(s.get("c", "curto")); self.assertEqual(s.count("c"), 2)
        self.assertGreaterEqual(s.purge_expired(), 0)
        self.now = 3000.0
        self.assertEqual([d["v"] for d in s.list("c")], [3])

    def test_ping(self):
        self.assertTrue(self.store.ping())


class MemoryStoreTests(StoreContract, unittest.TestCase):
    def make(self):
        return MemoryStore()


class SqliteStoreTests(StoreContract, unittest.TestCase):
    def make(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return SqlStore("sqlite:///" + os.path.join(self.tmp.name, "omnilink.db"))

    def test_data_survives_reopening_the_database(self):
        path = "sqlite:///" + os.path.join(self.tmp.name, "omnilink.db")
        self.store.put("devices", "dev1", {"name": "tv"}, owner="dev1")
        reopened = SqlStore(path)
        self.assertEqual(reopened.get("devices", "dev1"), {"name": "tv"})

    def test_sql_injection_attempts_are_inert(self):
        evil = "x'; DROP TABLE kv; --"
        self.store.put(evil, evil, {"v": 1}, owner=evil)
        self.assertEqual(self.store.get(evil, evil), {"v": 1})
        self.assertEqual(self.store.count(evil, evil), 1)
        self.assertTrue(self.store.ping())

    def test_thread_safety_smoke(self):
        import threading
        def work(n):
            for i in range(25):
                self.store.put("t", f"{n}-{i}", {"n": n, "i": i}, owner=str(n))
        threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(self.store.count("t"), 150)


class FactoryTests(unittest.TestCase):
    def test_open_store_selects_backend(self):
        self.assertIsInstance(open_store(""), MemoryStore)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(open_store("sqlite:///" + os.path.join(tmp, "x.db")).kind, "sqlite")
        with self.assertRaises(ValueError):
            SqlStore("mysql://x")


if __name__ == "__main__":
    unittest.main()
