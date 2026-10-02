import copy
import unittest

from container_preflight import READ_ONLY, READ_WRITE, TOKEN, validate_container


class ContainerBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.container = {
            "HostConfig": {"NetworkMode": "host", "Privileged": False, "PidMode": "", "CapAdd": ["NET_ADMIN"]},
            "Mounts": [
                {"Type": "bind", "Source": source, "Destination": target, "RW": writable}
                for paths, writable in ((READ_ONLY, False), (READ_WRITE, True))
                for target, source in paths.items()
            ] + [{"Type": "bind", "Source": "/private/token", "Destination": TOKEN, "RW": False}],
        }

    def test_same_path_private_vm_profile(self):
        validate_container(self.container)

    def test_wrong_host_privilege_or_network(self):
        for key, value in (("NetworkMode", "bridge"), ("Privileged", True), ("PidMode", "host"),
                           ("CapAdd", ["NET_ADMIN", "SYS_ADMIN"])):
            with self.subTest(key=key):
                container = copy.deepcopy(self.container)
                container["HostConfig"][key] = value
                with self.assertRaises(ValueError):
                    validate_container(container)

    def test_unexpected_credentials_mount(self):
        self.container["Mounts"].append({"Type": "bind", "Source": "/root/.kube", "Destination": "/root/.kube", "RW": False})
        with self.assertRaises(ValueError):
            validate_container(self.container)

    def test_path_mapping_receipt_and_token_are_not_relaxed(self):
        for target, field, value in (("/var/lib/railshot-runner/work", "Source", "/another/path"),
                                     ("/var/lib/railshot-ci", "RW", True), (TOKEN, "RW", True)):
            with self.subTest(target=target):
                container = copy.deepcopy(self.container)
                next(m for m in container["Mounts"] if m["Destination"] == target)[field] = value
                with self.assertRaises(ValueError):
                    validate_container(container)


if __name__ == "__main__":
    unittest.main()
