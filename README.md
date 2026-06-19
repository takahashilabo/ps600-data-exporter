# PS-600 Data Exporter

A browser-based tool to retrieve heart rate data recorded on your **EPSON PULSENSE PS-600** via Bluetooth and save it as a CSV file.

This tool was created following the shutdown of the official PULSENSE View app (March 31, 2025), so that PS-600 owners can still access their data.

> 日本語はこちら → [README_ja.md](README_ja.md)

---

## Supported environments

| OS | Browser |
|---|---|
| Windows | Chrome / Edge |
| macOS | Chrome / Edge |
| Android | Chrome / Edge |
| iOS (iPhone / iPad) | **Not supported** (Web Bluetooth is not available on iOS) |

---

## How to use

### Step 1 — Put the watch into pairing mode

Long-press the **top-left button** on the watch until the menu appears. Select **"Bluetooth Pairing"** from the menu.

### Step 2 — Open the page in your browser

Open the following URL in Chrome or Edge:

👉 **https://takahashilabo.github.io/ps600-reader/**

### Step 3 — Connect to the watch

Press the **"Connect to watch"** button. A Bluetooth device picker will appear. Select your PS-600 from the list.

### Step 4 — Wait for data transfer

Data transfer starts automatically after connecting. The time required depends on the number of records stored on the watch (roughly **8 minutes per 1,000 records**). Keep the browser tab open and wait.

### Step 5 — Download CSV

Once the transfer is complete, a list of measurement sessions is displayed. Check the sessions you want to download (all sessions are pre-selected), enter a label for the watch if you have multiple devices, then press **"Download CSV"**.

---

## CSV format

| Column | Description |
|---|---|
| `datetime_jst` | Measurement timestamp (JST, e.g. `2026-04-24 10:30:00`) |
| `unix_timestamp` | Unix timestamp (seconds) |
| `record_index` | Record index on the device |
| `sample_num` | Sample number within the session (1 sample ≈ 1 minute) |
| `heart_rate_bpm` | Heart rate (BPM) |

---

## Notes

- Do not close the browser tab while data is being transferred. If the connection is lost, you will need to start over.
- The watch's Bluetooth pairing screen may time out. If the connection drops, re-enter pairing mode and press **"Start over"** in the browser.
- When using multiple watches, enter a label (e.g. "Unit-1", "John") to distinguish files by name.

---

## Technical details / Protocol reference

For BLE protocol analysis, command specifications, and the Python implementation, see → [PROTOCOL.md](PROTOCOL.md)
