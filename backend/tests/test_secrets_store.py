import json
import os
import unittest
from tempfile import TemporaryDirectory

from app.secrets_store import (
    EnvFileSecretStore,
    InMemorySecretStore,
    SecretStoreError,
    _ref_to_env_key,
    secret_store_from_environment,
)


class RefMappingTests(unittest.TestCase):
    def test_ref_maps_to_env_key(self):
        self.assertEqual(_ref_to_env_key("secret://xero/demo/client-secret"), "SECRET_XERO_DEMO_CLIENT_SECRET")


class InMemorySecretStoreTests(unittest.TestCase):
    def test_resolve_and_store_round_trip(self):
        store = InMemorySecretStore({"secret://a": "one"})
        self.assertEqual(store.resolve("secret://a"), "one")
        store.store("secret://a", "two")
        self.assertEqual(store.resolve("secret://a"), "two")

    def test_rejects_non_reference(self):
        store = InMemorySecretStore()
        with self.assertRaises(SecretStoreError):
            store.resolve("plain-value")
        with self.assertRaises(SecretStoreError):
            store.store("plain-value", "x")

    def test_refuses_empty_store(self):
        store = InMemorySecretStore()
        with self.assertRaises(SecretStoreError):
            store.store("secret://a", "")


class EnvFileSecretStoreTests(unittest.TestCase):
    def test_resolves_bootstrap_from_env(self):
        with TemporaryDirectory() as tmp:
            store = EnvFileSecretStore(
                os.path.join(tmp, "store.json"),
                {"SECRET_XERO_DEMO_CLIENT_SECRET": "boot-secret"},
            )
            self.assertEqual(store.resolve("secret://xero/demo/client-secret"), "boot-secret")

    def test_rejects_placeholder_env_value(self):
        with TemporaryDirectory() as tmp:
            store = EnvFileSecretStore(
                os.path.join(tmp, "store.json"),
                {"SECRET_XERO_DEMO_REFRESH_TOKEN": "replace-with-real"},
            )
            with self.assertRaises(SecretStoreError):
                store.resolve("secret://xero/demo/refresh-token")

    def test_stored_value_overrides_env_and_persists(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "store.json")
            env = {"SECRET_XERO_DEMO_REFRESH_TOKEN": "seed-token"}
            store = EnvFileSecretStore(path, env)
            store.store("secret://xero/demo/refresh-token", "rotated-token")
            self.assertEqual(store.resolve("secret://xero/demo/refresh-token"), "rotated-token")
            # A fresh instance (simulating restart) reads the rotated token from disk.
            reloaded = EnvFileSecretStore(path, env)
            self.assertEqual(reloaded.resolve("secret://xero/demo/refresh-token"), "rotated-token")

    def test_missing_reference_raises(self):
        with TemporaryDirectory() as tmp:
            store = EnvFileSecretStore(os.path.join(tmp, "store.json"), {})
            with self.assertRaises(SecretStoreError):
                store.resolve("secret://xero/demo/unknown")

    def test_malformed_persistence_file_raises(self):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "store.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not json")
            with self.assertRaises(SecretStoreError):
                EnvFileSecretStore(path, {})

    def test_from_environment_uses_default_relative_path(self):
        store = secret_store_from_environment({"ACCOUNTINGOS_SECRET_STORE_PATH": ""})
        # Empty override falls back to the documented default.
        self.assertTrue(str(store._path).endswith("store.json"))


if __name__ == "__main__":
    unittest.main()
