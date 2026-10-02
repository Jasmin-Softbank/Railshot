import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("platform_render", Path(__file__).parents[1] / "render-platform.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PlatformTests(unittest.TestCase):
    def test_pinned_images_private_api_and_configured_readiness(self):
        images = {name: f"ghcr.io/jasmin-softbank/railshot-{name}@sha256:" + "a" * 64 for name in ("dashboard", "api")}
        output = module.render(images, "k3s-aws")
        api = next(item for item in output["items"] if item["kind"] == "Deployment" and item["metadata"]["name"] == "railshot-api")
        container = api["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], images["api"])
        self.assertIn("configured", container["readinessProbe"]["exec"]["command"][-1])
        self.assertTrue(all(item["spec"]["type"] == "ClusterIP" for item in output["items"] if item["kind"] == "Service"))
        public = module.render(images, "k3s-aws", 31080)
        services = {item["metadata"]["name"]: item["spec"] for item in public["items"] if item["kind"] == "Service"}
        self.assertEqual(services["railshot-dashboard"]["ports"][0]["nodePort"], 31080)
        self.assertEqual(services["railshot-api"]["type"], "ClusterIP")
        with self.assertRaises(ValueError):
            module.render(images, "k3s-aws", 443)
        for bad in ("ghcr.io/jasmin-softbank/railshot-api:latest", "attacker.invalid/api@sha256:" + "a" * 64):
            with self.assertRaises(ValueError):
                module.render({**images, "api": bad}, "k3s-aws")
        with self.assertRaises(ValueError):
            module.render(images, "../target")
