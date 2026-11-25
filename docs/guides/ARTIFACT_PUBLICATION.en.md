<a id="通用-microvm-环境制品发布"></a>

# Publishing reusable MicroVM environment artifacts

**English** · [简体中文](ARTIFACT_PUBLICATION.md)

> Historical record: references marked “historical source not included”
> refer to experiment files outside this repository. They are not runnable
> instructions for the current release. Use the [documentation index](../README.md) for supported entry points.

<a id="从隔离构建沙盒发布文件增量"></a>

## Publish added files from an isolated build sandbox

A publisher can package ordinary files added in a Firecracker build sandbox with a local native ext4 root and make them available in a fresh sandbox. The first implementation supports files below `/opt`. Each delta can contain up to 64 files and 16 MiB in total. Accepted file modes are 0644 and 0755.

The parent environment maintainer must verify that the guest path `/opt` is served by the local boot ext4 and declare `"pack_diff_root": "direct-ext4-opt-v1"` before publishing the parent. Guests serving that path from OverlayFS, a mounted disk or OverlayBD are ineligible. Adding the declaration changes the environment revision. The maintainer must rebind and verify any existing execution claims. Use the **deployed parent revision** in the following steps.

1. Write a separate `policy.json` specifying the parent environment ID, digest of the complete parent entry and each target file's absolute path, size, mode and expected SHA-256. Obtain the parent revision with `MicroVMEnvironmentCatalog(parent_catalog).environment_digest(parent_id)`. Take file digests from dependencies you have reviewed or from reproducible build outputs.
2. Create an isolated builder through the project's HTTPS SDK, run your build steps and export the files with `capture`. The caller stops the builder through the normal lifecycle.

```python
import json
from pathlib import Path
from dsec.sdk import DSecMicroVMRunArgs
from dsec_image.pack_diff import capture

policy = json.loads(Path("policy.json").read_text())
# client 是已打开、已认证的 DSecClient。demand 使用 Node 公布的资源配置。
builder = await client.run_microvm(
    DSecMicroVMRunArgs(environment_id=policy["parent"]), resource_demand=demand)
try:
    result = await builder.run_shell(build_command, timeout_ms=30000)
    if result["exit_code"]:
        raise RuntimeError("Dependency build failed")
    await capture(builder, policy, Path("dependency.delta.json"))
finally:
    await builder.stop()
```

For example, `{"path":"/opt/tool","mode":493,"size":3,"sha256":"…"}` describes a three-byte executable with mode 0755. The top-level format is `{"parent":"base","revision":"…","files":[…]}`. Replace the ellipses with full hexadecimal SHA-256 digests.

3. Deliver the delta to a trusted publisher. Using the publisher's own policy, the same parent revision and Linux e2fsprogs, the publisher creates the files in a temporary copy of the pinned local parent disk and atomically publishes the child catalog entry.

```bash
python -m dsec_image.pack_diff --policy policy.json --delta dependency.delta.json \
  --parent-catalog environments.json --child with-tool \
  --destination environments.json --store /srv/dsec/artifacts
```

4. Deploy the new catalog through the usual process so the Node loads it, then start the environment using `DSecMicroVMRunArgs(environment_id="with-tool")`. Applications can execute the published `/opt/tool` directly. To advertise native capabilities to Placement, verify and bind the child's capabilities separately.

The transferred artifact contains only approved file contents and the manifest. On the publishing host, the publisher materializes a full child ext4 boot disk and reuses the parent's read-only layers. Supply the policy independently of the builder. The publisher must review approved files for sensitive content. An existing target, a symlink in a parent directory, or a mismatched parent revision aborts publication. This version supports adding files only. Replacements, deletions, whole-disk block deltas, running memory state and catalog hot reload are outside its scope.

<a id="overlaybd-根盘的派生发布task-025"></a>

### Publish a derived OverlayBD root (TASK-025)

Use the same capture format and file policy. The parent publisher declares with `pack_diff_root=overlaybd-ext4-opt-v1` that `/opt` resides on the OverlayBD-backed ext4 root filesystem. The parent catalog pins the base image's ordered lowers, sizes and SHA-256 digests. If these lowers already provide the entire root disk, the catalog may omit `boot_template` and `boot_sha256`. The publisher verifies a copy if one is supplied.

The command requires three additional paths provisioned by the administrator. The daemon needs read and write access to the example workspace and read access to the object store and its ancestors. Publish the parent revision to the same store first to reuse existing objects.

```bash
python -m dsec_image.pack_diff --policy policy.json --delta dependency.delta.json \
  --parent-catalog environments.json --child with-tool \
  --destination environments.json --store /srv/dsec/artifacts \
  --ublk-socket /srv/dsec/overlaybd/ublk.sock \
  --ublk-global-config /srv/dsec/overlaybd/global.json \
  --ublk-work-dir /srv/dsec/overlaybd/staging
```

The publisher creates a temporary writable block device over the parent lowers, installs the files, verifies them by reading them back, runs e2fsck and seals a new commit with restack. It publishes the environment to the catalog only after confirming device release. The content-addressed store preserves parent lower ordering and places the new commit on top. If the daemon receipt is lost or the device identity changes, the error identifies the workspace requiring inspection. Confirm device ownership before cleaning it up.

In Linux acceptance, both parent lowers derived from a 64 MiB Alpine boot disk were reused. Publishing one added dependency created only a 76 KiB commit and a 441-byte descriptor. A new child VM executed the program. Its contents remained isolated from the parent and sibling VMs. See the [Linux acceptance record](../reports/DSEC_V01_INSTALL_ACCEPTANCE.en.md).

<a id="跨-node-交付同一-microvm-环境"></a>

## Deliver the same MicroVM environment to another Node

Publish the local catalog and object store using the existing process, then export a manifest. The SHA-256 printed by the exporter is the SDK's `artifact_version`. The manifest retains the environment configuration, execution claims, layer order and artifact digests. Each receiver chooses its own absolute paths.

```bash
dsec-artifact-publisher export --catalog /srv/dsec/environments.json \
  --environment-id with-tool --manifest with-tool.json
```

Deliver the manifest, expected digest and required objects to the receiving Node. Objects can be pre-positioned or provided through a shared directory readable by the receiver. Obtain the expected digest from a trusted publishing channel. Run the following on B, setting `VERSION` to that digest. `--objects` points directly to the directory containing the digest-named files.

```bash
dsec-artifact-publisher import --environment-id with-tool --manifest with-tool.json \
  --artifact-version "$VERSION" --objects /mnt/artifact-transfer/objects \
  --local-store /srv/node-b/artifacts --destination-catalog /srv/node-b/environments.json
```

B verifies the manifest and object contents, rebuilds the local OverlayBD image descriptor and writes the deployment catalog atomically. Use the normal deployment flow to load the catalog in Edge. Advertise the environment and its minimum resource demand in the Node configuration. Edge pins its catalog at startup, so updates follow the usual maintenance procedure. Only healthy Nodes with a published version are eligible for version selection.

```python
box = await client.run_microvm(
    DSecMicroVMRunArgs(environment_id="with-tool"),
    artifact_version=version,
    resource_demand={"cpu": 1, "memory_mb": 512, "disk_mb": 256, "network_mbps": 0},
)
```

`artifact_version` can be reused across deployment paths. The local `environment_sha256` remains bound to the deployment path and execution claims. Both the Node and Edge check it at creation. Requests without a matching revision return `NoPlacement`. Pause, resume and retirement of an existing instance continue to route by its original sandbox ID.

Version 1 supports local EROFS layers, ext4 root disks and publisher-generated OverlayBD roots. Export rejects catalogs with 3FS lazy-read settings or unrecognized configuration. Operators choose how to deliver objects. This process pre-positions environments on other hosts. A paused instance's memory and writable disk remain owned by its original Node.

<a id="task-023-设计提案--单次发布内复用本地校验"></a>

## TASK-023 proposal: reuse local verification within one publication

In TASK-003, two environments shared EROFS layers. Even after a hit in the object pool, the publisher read both the source and destination twice. The proposed change performs one full SHA-256 per distinct file in a single publication and discards the verification record when that publication ends.

1. Source catalog resolution, local object reuse checks and final candidate catalog resolution share an in-memory record. It is keyed by path and expected digest and checks device, inode, mode, size, mtime and ctime. On a miss, compute the full hash and compare file identity before and after reading. A change requires revalidation. A digest mismatch stops publication.
2. New objects are still hashed while being copied, synced and committed by hard link. The destination is subsequently verified independently. When concurrent publishers race to create an object, verify the file that actually won.
3. Each publication and each fresh Edge gets its own verification scope. The catalog format, atomic replacement and remote 3FS verification protocol retain their existing contracts.

Acceptance reuses TASK-003's inputs and probes. Compare bytes read and read time for shared layers across six publications. Verify the original VM after two Edge restarts, rejection of a same-length replacement, and retirement of the resources used in the run. Unit tests replace the source or destination between verification phases and check that the destination catalog stays intact on failure.

<a id="当前执行能力声明2026-10-30"></a>

## Current execution capability claims (2026-10-30)

Applications declare required protocols through the public SDK. A trusted publisher supplies claims for the specific environment. Entries in the generic MicroVM catalog and QEMU schema 2 catalog can include the following optional field.

```python
from dsec.contracts.environments import artifact_revision
from dsec.contracts.native import NATIVE_FEATURE, STREAM_FEATURE

# entry 是最终制品 pins、执行模式和配置已确定的一个环境条目。
# 先对这份 guest 完成对应协议检查，再记录通过的能力。
entry["execution_contract"] = {
    "schema": 1,
    "artifact_revision": artifact_revision(entry),
    "capabilities": [NATIVE_FEATURE, STREAM_FEATURE],
}
```

If the field is absent, capabilities are unknown. Old callers can continue using the existing interface. A new creation that explicitly requests a capability cannot select the environment solely because its Node supports it. The field binds to the environment description. Recheck it after replacing the template, kernel, EROFS layer, execution mode or resource configuration. FnCall cannot advertise persistent native sessions or files. Only currently supported native protocol capability names are accepted. Model and benchmark names are invalid.

The claim is a publisher assertion. Verify the actual guest separately. Catalog SHA-256 checks establish artifact identity. A claim alone cannot establish that a tool is installed or an isolation boundary works. This iteration neither scans nor starts old guests to infer claims. Existing catalogs need no bulk migration.

The MicroVM publisher validates the source entry and claim, copies and verifies artifact bytes, then rebuilds the binding for rewritten paths. It preserves the original capability set. For QEMU the publisher generates the binding on the final entry, and catalog loading retains existing artifact verification. Because revision includes deployment paths and configuration, it is unsuitable as a cross-node content deduplication key. Publish a new version ID for a running environment. Do not mutate its catalog in place or attach claims retroactively to live instances. Existing requests retain their original durable journal and are not replayed under the new catalog.

The application's `required_capabilities` does not go directly to the guest. The control plane selects a binding, the Node checks it, and Edge verifies the revision before writing creation intent. Cached results retain their original proof. For separate storage, network, production isolation and snapshot compatibility checks, see [execution and isolation boundaries](../architecture/EXECUTION_ISOLATION.md).

<a id="可选-fs-verity-完整性路径2026-10-06实机验收通过"></a>

## Optional fs-verity integrity checks (Linux acceptance, 2026-10-06)

`artifact_integrity.py` provides integrity checking independently of any task or training framework. On a verity-capable filesystem, the administrator prepares a read-only file and runs `dsec-artifact-integrity seal --file
<artifact> --sha256 <existing-digest> --receipt <protected-receipt-path>`. At publication the kernel freezes the file and builds its verification tree. The publisher then reads the entire file to confirm its original SHA-256 and atomically publishes a receipt containing that digest, the kernel fs-verity digest and size. A digest mismatch prevents receipt publication. Account for this preparation separately from episode costs.

At runtime `verify` does not read the entire data file. It checks the root-managed receipt and file size and uses `FS_IOC_MEASURE_VERITY` to verify the kernel-pinned digest. The kernel verifies subsequent reads page by page. All ancestors of both files must be root-owned and unwritable by other users. This protects the digest binding and prevents path replacement before the VMM opens it. Neither filename, stat metadata nor a cache flag substitutes for verification. If the mode was selected and kernel protection is missing, resolution fails without fallback.

The first integration was the verifier tools disk. Add the following to the manifest.

```json
"integrity": {
  "local": {"mode": "fs-verity", "receipt": "/var/lib/dsec/artifacts/tool.receipt.json"}
}
```

Existing artifacts without this field retain full hashing. The EROFS publisher does not automatically change artifact formats or permissions. Copies lose fs-verity protection and must be sealed again. The test host's root filesystem did not have verity enabled, so acceptance used a separate volume without altering root filesystem features. The newly installed r5 candidate passed 49 core regressions. Linux acceptance confirmed rejection of writes and unsealed copies, and EIO on reading data corrupted offline. Cold resolution of a 22 GiB tools disk took about 0.516 ms, with 0 B of physical reads by the process and zero resident data pages afterwards. All 15 paired task scores were 1. One-time copying and sealing took about 43.56 s. The separate volume occupied about 16.26 GiB. Include these costs when comparing deployments. See the [r5 acceptance report](../reports/DSEC_VERITY_PILOT_REPORT.en.md). This integration covers only the local verifier tools disk. Neither 3FS FUSE nor DAX was validated, and ordinary EROFS retains its existing resolution path. See the [kernel interfaces and constraints](https://docs.kernel.org/filesystems/fsverity.html).

<a id="既有发布路径与历史验收"></a>

## Existing publication path and historical acceptance

On 2026-10-03 the generic environment publisher [`artifact_publisher.py`](../../src/dsec/storage/publication.py) replaced manual assembly of paths and SHA-256 values per environment. Input is a MicroVM environment catalog in the existing format. The publisher copies the boot ext4, guest kernel, EROFS layers, optional DAX VMM and OverlayBD lowers into a local content-addressed object pool. It rewrites the OverlayBD root-image JSON with the new lower paths and pins a new digest. Optionally supply `--threefs-mount` and `--threefs-store` to copy non-DAX layers to a content-addressed 3FS directory at publication. On creation, the catalog still selects `local` or `threefs_lazy`.

The publisher fully verifies the source SHA-256, checks source identity while copying, fsyncs each new object and commits it by hard link without overwriting an existing object. It verifies digests on pool hits and refuses publication if an object is contaminated. Another environment using identical bytes reuses the object. Finally it locks and rereads the destination catalog, rejects an existing environment ID, checks local and remote resolution of the new entry and atomically replaces the catalog file. Failure can leave **unreferenced** objects in the store but never a partially published catalog. To load the new environment, restart the idle daemon using the full runtime version deployment procedure (`ELASTIC_RUNTIME_CONVERGENCE.md`, historical source not included). The tool itself does not restart services.

Example

```bash
python3 artifact_publisher.py \
  --source-catalog candidate-catalog.json \
  --environment-id general-tools \
  --destination-catalog published-catalog.json \
  --local-store artifacts
```

For both local and 3FS sources, add `--threefs-mount /path/to/3fs/mnt --threefs-store /path/to/3fs/mnt/objects` to the same command. If remote references in the source catalog are retained rather than republished, the publisher still verifies the complete remote contents. An unavailable mount causes publication to fail. DAX layers always retain a local source.

**Trust boundary.** A content-addressed name and read-only permissions cannot prevent tampering by the same user. On its first resolution in each daemon lifetime, the runtime hashes local artifacts in full. Later resolutions check file identity. Remote 3FS contents are hashed in full at publication. Creation checks an active mount, file size and pinned content-addressed name. This work retained the full local hash on cold daemon startup. It neither enabled unverified `fs-verity` nor skipped verification.

In `/dev/shm/dsec-publish-pilot` on the test host, publication of `general-erofs-base` produced **117,347,336 B** of local artifacts and the new catalog resolved successfully. An isolated Firecracker VM then used the catalog to mount its published EROFS layer, run a command and stop. The isolated artifacts were cleaned up. The verification scripts were `smoke_artifact_publisher.py` (`experiments/3fs-single/smoke_artifact_publisher.py`, historical source not included) and `verify_published_microvm.py` (`experiments/3fs-single/verify_published_microvm.py`, historical source not included). All 15 publication and catalog tests passed on the staged Linux source. A test verifies that a published DAX VMM retains execute permission. Creation with DAX using this publication path had not been tested on hardware.

The Linux run also exposed an uninterruptible wait in `stat` on the existing 3FS FUSE mount. The previous `subprocess.run(timeout=5)` could hang while reaping the child. `environment_catalog.py` now moves all operations on remote paths into a separate probe process. On timeout the parent sends a termination signal and does not wait indefinitely. A kernel process blocked on an unavailable FUSE mount may persist until the mount recovers. This change isolates the caller from the hang without repairing 3FS itself. The root partition later recovered to about 94 GiB available. The disk cleanup record (`DISK_CLEANUP_20261003.md`, historical source not included) attributes only about 6.2 GiB to the old boot bundle explicitly removed in this run. Existing 3FS objects were successfully reread and verified by SHA-256.

Once disk space and 3FS were available again, the full runtime deployment script (`experiments/3fs-single/deploy_artifact_publication_runtime.sh`, historical source not included) published 84 top-level Python files. Before publication, changes were limited to `environment_catalog.py` and its tests plus the addition of `artifact_publisher.py` and its tests. Other production source digests matched. A real `threefs_lazy` MicroVM using the isolated staged version passed mount, execution, pause/resume and restart of both daemon and worker. All 102 records were `STOPPED` during the idle production deployment. Its backup is at `/home/xiaoxiaohu/dsec-reproduce/service-code-pre-artifact-20261003-235702`. All 102 records were recovered and the deployed source digests matched. On the production worker, DAX + OverlayBD sandboxes using either local or 3FS sources passed layer reads and pause/resume. All 104 final records were `STOPPED`, the queue was empty, both services were active and the production source passed 15 related tests. The publisher was installed as a production source tool. **No duplicate demonstration environment was added to the production catalog.** The tool was ready for the next actual publication.
