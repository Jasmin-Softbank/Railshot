import importlib.util
import os
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]

spec = importlib.util.spec_from_file_location("platform_render", Path(__file__).parents[1] / "render-platform.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PlatformTests(unittest.TestCase):
    def test_build_job_uses_dedicated_agent_and_scoped_read_only_identity(self):
        images = {"ci-runner": "ghcr.io/jasmin-softbank/railshot-ci-runner@sha256:" + "a" * 64}
        result = module.render_build_runner(images, "build-test-1", "https://github.com/Jasmin-Softbank/railshot-apps", "railshot-build-worker-aws-01")
        resources = {item["kind"]: item for item in result["items"]}
        self.assertEqual(resources['Role']['rules'], [{'apiGroups':[''], 'resources':['pods'], 'verbs':['get']}])
        self.assertEqual(resources['ClusterRole']['rules'], [{'apiGroups':[''], 'resources':['nodes'], 'resourceNames':['railshot-build-worker-aws-01'], 'verbs':['get']}])
        job = resources['Job']
        self.assertEqual(job['spec']['backoffLimit'], 0)
        pod = job['spec']['template']['spec']
        self.assertEqual(pod['nodeSelector']['railshot.io/node-role'], 'build')
        self.assertEqual(pod['nodeSelector']['kubernetes.io/hostname'], 'railshot-build-worker-aws-01')
        self.assertEqual(pod['containers'][0]['image'], images['ci-runner'])
        self.assertFalse(any('/rancher' in v.get('hostPath', {}).get('path', '') for v in pod['volumes']))
        for name, url, node in [('bad/name', 'https://github.com/Jasmin-Softbank/apps', 'worker'),
                                ('runner', 'https://github.com/unreviewed/apps', 'worker'),
                                ('runner', 'https://github.com/Jasmin-Softbank/apps', '../node')]:
            with self.assertRaises(ValueError):
                module.render_build_runner(images, name, url, node)
        with self.assertRaises(ValueError):
            module.render_build_runner({'ci-runner':'ghcr.io/jasmin-softbank/railshot-ci-runner:latest'}, 'runner', 'https://github.com/Jasmin-Softbank/apps', 'worker')

    def test_gitops_manifests_keep_platform_permissions_and_sync_explicit(self):
        install = yaml.safe_load((ROOT / "gitops/argo/kustomization.yaml").read_text())
        self.assertEqual(install["kind"], "Kustomization")
        self.assertEqual(install["namespace"], "argocd")
        self.assertEqual(len(install["resources"]), 1)
        self.assertRegex(install["resources"][0],
                         r"^https://raw\.githubusercontent\.com/argoproj/argo-cd/[a-f0-9]{40}/manifests/install\.yaml$")
        documents = list(yaml.safe_load_all((ROOT / "gitops/applications/railshot-platform.yaml").read_text()))
        self.assertEqual(len(documents), 2)
        resources = {item["kind"]: item for item in documents}
        self.assertEqual(set(resources), {"AppProject", "Application"})
        project, application = resources["AppProject"], resources["Application"]
        for item in documents:
            self.assertEqual(item["apiVersion"], "argoproj.io/v1alpha1")
            self.assertEqual(item["metadata"]["namespace"], "argocd")
            self.assertNotIn("finalizers", item["metadata"])  # No cascading workload removal.
        self.assertEqual(application["spec"]["project"], project["metadata"]["name"])
        self.assertEqual(project["spec"]["sourceRepos"], ["https://github.com/Jasmin-Softbank/Railshot.git"])
        self.assertEqual(project["spec"]["clusterResourceWhitelist"], [])
        self.assertEqual({(item["group"], item["kind"]) for item in project["spec"]["namespaceResourceWhitelist"]},
                         {("apps", "Deployment"), ("", "Service"), ("", "PersistentVolumeClaim"), ("networking.k8s.io", "NetworkPolicy")})
        destination = {"server": "https://kubernetes.default.svc", "namespace": "railshot-system"}
        self.assertEqual(project["spec"]["destinations"], [destination])
        self.assertEqual(application["spec"]["destination"], destination)
        source = application["spec"]["source"]
        self.assertEqual(source["repoURL"], project["spec"]["sourceRepos"][0])
        self.assertEqual(source["path"], "gitops/applications/railshot-platform")
        self.assertEqual(source["directory"], {"include": "workload.json"})
        self.assertRegex(source["targetRevision"], r"^(REPLACE_WITH_REVIEWED_CONFIG_COMMIT|[a-f0-9]{40})$")
        self.assertNotIn("automated", application["spec"].get("syncPolicy", {}))

    @unittest.skipUnless(os.environ.get("RAILSHOT_ARGO_SCHEMA_MANIFEST"),
                         "Set RAILSHOT_ARGO_SCHEMA_MANIFEST to the kubectl kustomize output for native Argo schema validation")
    def test_platform_declarations_match_rendered_upstream_argo_crds(self):
        from jsonschema import Draft7Validator
        rendered = Path(os.environ["RAILSHOT_ARGO_SCHEMA_MANIFEST"])
        schemas = {}
        for item in yaml.safe_load_all(rendered.read_text()):
            if item["kind"] != "CustomResourceDefinition" or item["spec"]["group"] != "argoproj.io":
                continue
            for version in item["spec"]["versions"]:
                if version["name"] == "v1alpha1" and version["served"]:
                    schemas[item["spec"]["names"]["kind"]] = version["schema"]["openAPIV3Schema"]
        self.assertTrue({"Application", "AppProject"} <= set(schemas), "Rendered Argo CRDs are missing")
        for item in yaml.safe_load_all((ROOT / "gitops/applications/railshot-platform.yaml").read_text()):
            with self.subTest(kind=item["kind"]):
                errors = sorted(Draft7Validator(schemas[item["kind"]]).iter_errors(item), key=lambda error: str(error.path))
                self.assertFalse(errors, "\n".join(f"{list(error.path)}: {error.message}" for error in errors))

    def test_pinned_images_private_api_and_configured_readiness(self):
        images = {name: f"ghcr.io/jasmin-softbank/railshot-{name}@sha256:" + "a" * 64 for name in ("dashboard", "api")}
        output = module.render(images, "k3s-aws")
        api = next(item for item in output["items"] if item["kind"] == "Deployment" and item["metadata"]["name"] == "railshot-api")
        container = api["spec"]["template"]["spec"]["containers"][0]
        for item in output['items']:
            if item['kind'] == 'Deployment':
                self.assertEqual(item['spec']['template']['spec']['nodeSelector']['railshot.io/node-role'], 'platform')
        self.assertEqual(container["image"], images["api"])
        self.assertEqual(api['spec']['strategy']['type'], 'Recreate')
        self.assertEqual(api['spec']['replicas'], 1)
        self.assertEqual(api['spec']['template']['spec']['initContainers'][0]['image'], images['api'])
        self.assertTrue(any(v.get('persistentVolumeClaim') for v in api['spec']['template']['spec']['volumes']))
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
