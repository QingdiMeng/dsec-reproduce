# OSWorld FullVM desktop regression

**English** · [简体中文](README.zh-CN.md)

This optional replay suite uses an installed DSec SDK and a reachable HTTPS
API Server. The installed SDK must expose `DSecFullVMRunArgs`, native file
transfer, `screenshot()` and `desktop_input()`. It drives two Calc tasks through
FullVM screenshot and keyboard
and pointer operations. The desktop replay tools do not require a model.

Use a pinned Ubuntu/Xfce FullVM with Thunar, LibreOffice Calc, a 1024×768
virtio-2d display and a fresh Calc profile. The recorded coordinates were
verified with Calc 7.3.7 using the English default UI. Changes to the desktop
image, locale, DPI or application layout require revalidating the fixtures.
The test configuration uses the `client`, `token_file` and `paths.fullvm`
fields of the [installed SDK acceptance configuration](../../docs/guides/DSEC_HOST_CONFIGURATION.md).

1. Check out OSWorld commit `8903ebeb0efdf74331bd7dc37ed91ccbbff22317`.
   Read the two task manifests in `evaluation_examples/examples/libreoffice_calc`
   and download each input and reference workbook using the URLs in its manifest.
   Verify the inputs against [workbook-sha256.json](workbook-sha256.json)
   and keep the references outside the guest.
2. Replay a recorded action sequence through the installed SDK. Use a new
   output directory per run.

   ```bash
   python apps/osworld/desktop_regression.py \
     --config fullvm-acceptance.json --workbook BoomerangSales.xlsx \
     --actions apps/osworld/boomerang.json --output out/boomerang
   python apps/osworld/desktop_regression.py \
     --config fullvm-acceptance.json --workbook WeeklySales.xlsx \
     --actions apps/osworld/weekly-sales.json --output out/weekly-sales
   ```

3. In a separate scoring environment with `openpyxl pandas rapidfuzz formulas
   lxml cssselect xmltodict tldextract`, run the pinned upstream
   `compare_table` metric. The GUI replay saves and confirms the XLSX format
   before the SDK downloads the result.

   ```bash
   python apps/osworld/score_calc.py --upstream /path/to/OSWorld \
     --task /path/to/OSWorld/evaluation_examples/examples/libreoffice_calc/3a7c8185-25c1-4941-bd7b-96e823c9f21f.json \
     --result out/boomerang/BoomerangSales.xlsx --expected 6_BoomerangSales_gt1.xlsx
   python apps/osworld/score_calc.py --upstream /path/to/OSWorld \
     --task /path/to/OSWorld/evaluation_examples/examples/libreoffice_calc/12382c62-0cd1-4bf2-bdc8-1d20bf9b2371.json \
     --result out/weekly-sales/WeeklySales.xlsx --expected 13_WeeklySales_gt1.xlsx
   ```

4. Use Pillow to compare two runs of `boomerang.json`, which declares the
   screenshot checkpoints. Weekly Sales is the separate official-score regression.

   ```bash
   python apps/osworld/compare_trace.py out/reference out/candidate
   python apps/osworld/compare_trace.py --diagnose out/reference out/candidate
   ```

The comparison reports the first changed checkpoint using the active-window
name and an action-specific screenshot region. Request IDs, submitted events,
receipts, screenshot hashes and PNGs remain available for diagnosis.
A successful input receipt establishes that the API accepted the operation.
The checkpoint and official metric separately establish its visible effect.

The diagnostic report separates the first submitted-event difference, the
first frame-source difference, button end-state differences and the first
visible checkpoint difference. Button state is inferred from acknowledged
input events; an incomplete receipt makes the state unknown. This is SDK-side
evidence and does not prove guest delivery. Frame provenance identifies which
SDK capture the runner returned and its local monotonic return time. If the
runner returns the before-action frame, the after-action PNG is kept
separately. This metadata does not measure guest rendering time. Older
traces still support visual and input comparisons with frame provenance
marked unavailable.

## Official Ubuntu image on DSec FullVM

Use the pinned OSWorld Ubuntu QCOW2 and its SHA-256 together with matching OVMF
code and variable images. On the Linux KVM host, prepare a read-only derivative
with the existing DSec guest agent and a systemd service.

```bash
python apps/osworld/prepare_uefi_guest.py \
  --base /path/to/Ubuntu.qcow2 --base-sha256 <official-image-sha256> \
  --source . --code /path/to/OVMF_CODE_4M.fd \
  --variables /path/to/OVMF_VARS_4M.fd --out /path/to/osworld-agent.qcow2
```

The preparation tool requires Python Requests, QEMU/KVM, qemu-img and a
static C toolchain. The stock OSWorld guest service is reachable over a
loopback port during preparation, with guest egress blocked. The derivative
keeps the official image as its backing file.

In the Edge QEMU execution catalog, use an offline `fullvm` environment with
`display: "virtio-2d"` and pin the derivative in `template`. Select the
`uefi` boot profile in place of the direct-boot kernel field. Its
`code`, `variables`, `image_tool` and `base` entries are ordinary
`{"path": "...", "sha256": "..."}` artifact pins for OVMF code, initial
OVMF variables, qemu-img and the official Ubuntu image respectively. Set
`disk_mb` to the image's virtual size in MiB, 51200 for this release.
Reserve the virtual disk size plus QCOW2 metadata and firmware variables in
the Node budget. The SDK continues to use `run_fullvm` with the new
environment ID. Each run gets a private QCOW2 write layer and private
firmware variables, which are retired after stop.

For AppArmor and cgroup limits, render the broker from the same catalog and
start Edge in its delegated service. The administrator approves the exact
read-only OVMF code and backing images. See the
[QEMU host confinement guide](../../docs/guides/DSEC_HOST_CONFIGURATION.md#qemu-host-confinement).
