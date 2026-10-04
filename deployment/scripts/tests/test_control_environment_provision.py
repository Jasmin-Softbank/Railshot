"""Central trusted provisioner contracts; real Vault/CA flow is in live_control_vault.py."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control_vault as control


class ProvisionTest(unittest.TestCase):
    def test_periodic_seal_token_has_self_renew_and_bound_transit_policy(self):
        vault = Mock()
        vault.call.side_effect = [None, {}, {}, {"auth": {"client_token": "synthetic-token", "accessor": "synthetic-accessor"}}]
        self.assertEqual(control.issue_token(vault, "env-a"), ("synthetic-token", "synthetic-accessor"))
        calls = vault.call.call_args_list
        self.assertEqual(calls[0].args[:2], ("GET", "transit/keys/railshot-env-a"))
        self.assertEqual(calls[1].args[2], {"type": "aes256-gcm96", "exportable": False, "allow_plaintext_backup": False})
        policy = calls[2].args[2]["policy"]
        self.assertIn('transit/decrypt/railshot-env-a', policy)
        self.assertIn('auth/token/renew-self', policy)
        self.assertNotIn('railshot-env-b', policy)
        self.assertEqual(calls[3].args[1], "auth/token/create/railshot-seal")
        self.assertTrue(calls[3].args[2]["no_default_policy"])

    def test_existing_transit_key_is_not_recreated(self):
        vault = Mock()
        vault.call.side_effect = [{"data": {"name": "railshot-env-a"}}, {}, {"auth": {"client_token": "new-token", "accessor": "new-accessor"}}]
        control.issue_token(vault, "env-a")
        self.assertFalse(any(call.args[0] == "POST" and call.args[1].startswith("transit/keys/") for call in vault.call.call_args_list))

    def test_configure_operator_uses_no_parent_and_readback_before_success(self):
        vault = Mock()
        vault.call.side_effect = [{"data": {"transit/": {}}}, {"data": {"file/": {}}}, {}, {}, {"auth": {"client_token": "operator-token"}}, {}]
        with patch.object(control, "read_config", return_value={}), patch.object(control, "client", return_value=vault), patch.object(control, "exclusive_write") as write:
            result = control.configure("config", "/private/operator")
        token_call = vault.call.call_args_list[-2]
        self.assertEqual(token_call.args[1], "auth/token/create")
        self.assertTrue(token_call.args[2]["no_parent"])
        self.assertNotIn("orphan", token_call.args[2])
        self.assertEqual(vault.call.call_args_list[-1].args, ("GET", "auth/token/lookup-self"))
        write.assert_called_once_with("/private/operator", b"operator-token\n")
        self.assertTrue(result["configured"])

    def test_reuse_requires_manager_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            profile = {"environment_id": "env-a", "credentials_generation": "existing", "seal": {"key_name": "railshot-env-a"}, "seal_env_file": "seal-env", "vault_tls": {}, "recovery": {"client_key_file": "runtime-key", "client_cert_file": "runtime-cert", "ca_file": "ca"}, "delivery_recovery_config_file": "manager-config"}
            def read(path):
                if str(path).endswith("transit-profile.json"):
                    return json.dumps(profile).encode()
                if str(path) == "manager-config":
                    return json.dumps({"ca_file": "ca", "client_cert_file": "manager-cert", "client_key_file": "manager-key"}).encode()
                if str(path) == "manager-key":
                    raise FileNotFoundError()
                return b"synthetic"
            with patch.object(control, "read_config", return_value={}), patch.object(control, "private_directory"), patch.object(control, "private_read", side_effect=read), patch.object(control, "issue_token") as issue:
                with self.assertRaises(FileNotFoundError):
                    control.provision("config", "env-a", output)
                issue.assert_not_called()

    def test_revoke_rejects_other_environment_policy(self):
        vault = Mock()
        vault.call.return_value = {"data": {"policies": ["railshot-env-b"]}}
        with patch.object(control, "private_read", return_value=b'{"environment_id":"env-a","accessor":"non-secret-accessor"}'), patch.object(control, "read_config", return_value={}), patch.object(control, "client", return_value=vault):
            with self.assertRaises(ValueError):
                control.revoke("config", "env-a", "accessor")
        self.assertEqual(vault.call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
