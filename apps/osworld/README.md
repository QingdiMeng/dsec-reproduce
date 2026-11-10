# OSWorld FullVM desktop regression

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
   ```

The comparison reports the first changed checkpoint using the active-window
name and an action-specific screenshot region. Request IDs, submitted events,
receipts, screenshot hashes and PNGs remain available for diagnosis.
A successful input receipt establishes that the API accepted the operation.
The checkpoint and official metric separately establish its visible effect.
