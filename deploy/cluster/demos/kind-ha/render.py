"""Render one phase of the DSec kind HA demo as YAML-compatible JSON."""
import argparse
import json
import re

NAMESPACE = "dsec-ha-demo"
DOMAIN = "dsec.demo/domain"
DOMAINS = ("a", "b", "c")
API_PORT, ETCD_PORT = 30443, 30379
PIN = re.compile(r"[\w./:-]+@sha256:[0-9a-f]{64}")


def topology():
    nodes = [{"role": "control-plane"}]
    for i, domain in enumerate(DOMAINS):
        ports = [{"listenAddress": "127.0.0.1", "containerPort": port, "hostPort": base+i}
                 for port, base in ((API_PORT, 25443), (ETCD_PORT, 25379))]
        nodes.append({"role": "worker", "labels": {DOMAIN: domain}, "extraPortMappings": ports})
    return {"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4",
            "networking": {"apiServerAddress": "127.0.0.1"}, "nodes": nodes}


def resource(kind, name, spec, api="v1"):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": NAMESPACE}, "spec": spec}


def volume(name, source, value):
    return {"name": name, source: value}


def mount(name, path, readonly=False):
    return {"name": name, "mountPath": path, "readOnly": readonly}


def container(name, image, **fields):
    return {"name": name, "image": image, "imagePullPolicy": "IfNotPresent",
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}}, **fields}


def pod(domain, containers, volumes):
    return {"nodeSelector": {DOMAIN: domain}, "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": containers, "volumes": volumes}


def deployment(name, role, spec):
    labels = {"app": role, "instance": name}
    return resource("Deployment", name, {"replicas": 1, "strategy": {"type": "Recreate"},
        "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": spec}}, "apps/v1")


def external_service(role, port, nodeport):
    return resource("Service", role+"-external", {"type": "NodePort", "externalTrafficPolicy": "Local",
        "selector": {"app": role}, "ports": [{"port": port, "targetPort": port, "nodePort": nodeport}]})


def quorum(image):
    peers = ",".join(f"etcd{i}=https://etcd{i}:2380" for i in range(1, 4))
    items = [external_service("etcd", 2379, ETCD_PORT)]
    for i, domain in enumerate(DOMAINS, 1):
        name = f"etcd{i}"
        items.append(resource("Service", name, {"clusterIP": "None", "publishNotReadyAddresses": True,
            "selector": {"instance": name}, "ports": [{"name": "peer", "port": 2380}, {"name": "client", "port": 2379}]}))
        items.append(resource("PersistentVolumeClaim", name, {"accessModes": ["ReadWriteOnce"],
            "storageClassName": "standard", "resources": {"requests": {"storage": "1Gi"}}}))
        args = [f"--name={name}", "--data-dir=/data/member-data", "--initial-cluster="+peers,
                "--initial-cluster-token="+NAMESPACE, "--initial-cluster-state=new",
                "--listen-client-urls=https://0.0.0.0:2379", "--listen-peer-urls=https://0.0.0.0:2380",
                f"--advertise-client-urls=https://{name}:2379", f"--initial-advertise-peer-urls=https://{name}:2380",
                "--trusted-ca-file=/tls/ca.crt", "--cert-file=/tls/member.crt", "--key-file=/tls/member.key",
                "--client-cert-auth", "--peer-trusted-ca-file=/tls/ca.crt", "--peer-client-cert-auth",
                "--peer-cert-file=/tls/member.crt", "--peer-key-file=/tls/member.key",
                "--auth-token=jwt,pub-key=/tls/jwt.pub,priv-key=/tls/jwt.key,sign-method=RS256,ttl=24h",
                "--quota-backend-bytes=268435456"]
        member = container(name, image, command=["/usr/local/bin/etcd"], args=args,
            resources={"requests":{"cpu":"100m","memory":"128Mi"}, "limits":{"memory":"384Mi"}},
            volumeMounts=[mount("data","/data"), mount("tls","/tls",True)])
        spec = pod(domain, [member], [volume("data","persistentVolumeClaim",{"claimName":name}),
                                     volume("tls","secret",{"secretName":name+"-tls","defaultMode":0o440})])
        items.append(deployment(name, "etcd", spec))
    return items


# DSec opens credentials with O_NOFOLLOW and requires regular files owned by the
# service UID. Stage only the selected role Secret into a private memory volume.
STAGE = """import os, pathlib, shutil, sys
os.umask(0o077)
for target in sys.argv[1:]:
    for src in pathlib.Path('/input' + target).iterdir():
        if not src.name.startswith('.') and src.is_file():
            shutil.copyfile(src, pathlib.Path(target) / src.name)
"""


def api_pod(domain, image, initializing=False):
    roles = {"secrets": "api-secrets"}
    if initializing:
        roles["initialize"] = "init-secrets"
    volumes = [volume("config","configMap",{"name":"role-config"})]
    stage_mounts, mounts = [], [mount("config","/config",True)]
    for target, secret in roles.items():
        volumes += [volume(target,"emptyDir",{"medium":"Memory"}),
                    volume(target+"-input","secret",{"secretName":secret,"defaultMode":0o440})]
        stage_mounts += [mount(target+"-input","/input/"+target,True), mount(target,"/"+target)]
        mounts.append(mount(target,"/"+target,not initializing))
    if initializing:
        command = ["python", "/opt/dsec-initialize.py", "/config/initialize.json"]
    else:
        volumes += [volume("driver","configMap",{"name":"checkpoint-driver"}),volume("gate","emptyDir",{})]
        mounts += [mount("driver","/demo",True),mount("gate","/gate")]
        command = ["python","/demo/verify_cluster_node.py","--root","/gate","--child-config","/config/controller.json"]
    service = container("api" if not initializing else "initialize", image, command=command, volumeMounts=mounts,
                        resources={"requests":{"cpu":"100m","memory":"64Mi"},"limits":{"memory":"256Mi"}})
    if not initializing:
        service["readinessProbe"] = {"tcpSocket":{"port":8443},"periodSeconds":2}
    spec = pod(domain, [service], volumes)
    spec["initContainers"] = [container("stage", image, command=["python","-c",STAGE,*["/"+k for k in roles]],
                                        volumeMounts=stage_mounts)]
    return spec


def render(phase, image=None):
    if phase == "kind":
        return topology()
    if not image or not PIN.fullmatch(image):
        raise ValueError("Supply the role image as repository@sha256:<64 lowercase hex digits>")
    if phase == "quorum":
        items = quorum(image)
    elif phase == "initialize":
        spec = api_pod("a", image, initializing=True)
        spec["restartPolicy"] = "Never"
        items = [resource("Job","initialize",{"backoffLimit":0,"template":{"spec":spec}}, "batch/v1")]
    elif phase == "api":
        items = [external_service("api",8443,API_PORT)]
        items.extend(deployment("api-"+domain,"api",api_pod(domain,image)) for domain in DOMAINS)
    else:
        raise ValueError("Unknown manifest phase")
    return {"apiVersion":"v1","kind":"List","items":items}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["kind","quorum","initialize","api"])
    parser.add_argument("--image")
    args = parser.parse_args()
    try:
        print(json.dumps(render(args.phase, args.image), indent=2))
    except ValueError as exc:
        parser.error(str(exc))
