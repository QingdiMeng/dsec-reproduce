"""Offline contract for the single-host kind HA demo manifests."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("kind_ha_demo", ROOT/"deploy/cluster/demos/kind-ha/render.py")
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)
IMAGE = "example.invalid/dsec@sha256:" + "1"*64


class KindHADesignTests(unittest.TestCase):
    def test_worker_loss_removes_the_target_api_and_member_only(self):
        nodes = demo.topology()["nodes"]
        self.assertEqual([n["role"] for n in nodes], ["control-plane", "worker", "worker", "worker"])
        resources = demo.render("quorum", IMAGE)["items"] + demo.render("api", IMAGE)["items"]
        deployments = [r for r in resources if r["kind"] == "Deployment"]
        pvcs = {r["metadata"]["name"] for r in resources if r["kind"] == "PersistentVolumeClaim"}
        self.assertEqual(len(deployments), 6)
        self.assertEqual(len(pvcs), 3)
        all_ports = set()
        for i, node in enumerate(nodes[1:], 1):
            domain = node["labels"][demo.DOMAIN]
            group = [r["spec"]["template"] for r in deployments
                     if r["spec"]["template"]["spec"]["nodeSelector"] == {demo.DOMAIN:domain}]
            self.assertEqual(sorted(t["metadata"]["labels"]["app"] for t in group), ["api", "etcd"])
            etcd = next(t for t in group if t["metadata"]["labels"]["app"] == "etcd")
            member = f"etcd{i}"
            self.assertEqual(etcd["metadata"]["labels"]["instance"], member)
            claim = next(v["persistentVolumeClaim"]["claimName"] for v in etcd["spec"]["volumes"]
                         if "persistentVolumeClaim" in v)
            self.assertIn(claim, pvcs)
            self.assertEqual(claim, member)
            for mapping in node["extraPortMappings"]:
                self.assertEqual(mapping["listenAddress"], "127.0.0.1")
                self.assertNotIn(mapping["hostPort"], all_ports)
                all_ports.add(mapping["hostPort"])
                services = [r for r in resources if r["kind"] == "Service" and
                            r["spec"]["ports"][0].get("nodePort") == mapping["containerPort"]]
                self.assertEqual(len(services), 1)
                self.assertEqual(services[0]["spec"]["externalTrafficPolicy"], "Local")
                target = services[0]["spec"]["selector"]
                self.assertEqual(sum(all(t["metadata"]["labels"].get(k) == v for k,v in target.items())
                                     for t in group), 1)
        self.assertEqual(all_ports, {25379,25380,25381,25443,25444,25445})

    def test_role_secrets_and_checkpoint_are_scoped(self):
        quorum = demo.render('quorum', IMAGE)['items']
        peers = {item['metadata']['name'] for item in quorum if item['kind'] == 'Service'
                 and item['spec'].get('publishNotReadyAddresses') is True}
        self.assertEqual(peers, {'etcd1', 'etcd2', 'etcd3'})
        for item in quorum:
            if item['kind'] != 'Deployment':
                continue
            member = item['metadata']['name']
            args = item['spec']['template']['spec']['containers'][0]['args']
            self.assertIn('--client-cert-auth', args)
            self.assertIn('--peer-client-cert-auth', args)
            self.assertIn('--name='+member, args)
            self.assertIn('--initial-cluster='+','.join(f'{p}=https://{p}:2380' for p in sorted(peers)), args)
        for phase, secrets in (("api", {"api-secrets"}), ("initialize", {"api-secrets","init-secrets"})):
            for obj in demo.render(phase, IMAGE)["items"]:
                if obj["kind"] == "Service":
                    continue
                pod = obj["spec"]["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertEqual({v["secret"]["secretName"] for v in pod["volumes"] if "secret" in v}, secrets)
                mounts = pod["containers"][0]["volumeMounts"]
                self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
                self.assertIn(demo.mount("tmp","/tmp"), mounts)
                self.assertTrue(any(v["name"] == "tmp" and v["emptyDir"].get("sizeLimit") for v in pod["volumes"]))
                self.assertFalse(any(m["name"].endswith("-input") for m in mounts))
                self.assertEqual(any(m["mountPath"] == "/gate" for m in mounts), phase == "api")
                self.assertEqual(pod["containers"][0]["image"], IMAGE)
        for bad in (None, "dsec:latest", "dsec@sha256:abc"):
            with self.subTest(image=bad), self.assertRaises(ValueError):
                demo.render("quorum", bad)
