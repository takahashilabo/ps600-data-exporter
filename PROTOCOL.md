# PS-600 BLE Protocol Reference

This document summarizes the BLE communication protocol of the **EPSON PULSENSE PS-600**, reverse-engineered by decompiling the official Android app (PULSENSE View 2.2.6) with jadx.

> 日本語版 → [PROTOCOL_ja.md](PROTOCOL_ja.md)

---

## GATT Service and Characteristics

All characteristics belong to a single GATT service.

**Service UUID**
```
b5a60000-2ed3-e993-896b-cceb986f8d73
```

| Role | UUID | Direction | Properties |
|---|---|---|---|
| DL_SIZE | b5a60020-... | app → device | Write (with response) |
| DL_DATA | b5a60022-... | app → device | Write Without Response |
| DL_ORDER | b5a60021-... | device → app | Indicate (TX ACK) |
| UL_SIZE | b5a60010-... | device → app | Indicate (total response size) |
| UL_DATA | b5a60012-... | device → app | Notify (incoming packets) |
| UL_ORDER | b5a60011-... | app → device | Write (with response) (RX ACK) |
| CONTROL_POINT | b5a60030-... | app → device | Write (with response) |
| DATA_EVENT | b5a60031-... | device → app | Notify (SummaryReady etc.) |

The UUID suffix is the same for all: `-2ed3-e993-896b-cceb986f8d73`

---

## Connection Sequence

```
1. Connect via Bleak / Web Bluetooth
2. Subscribe to notifications on DL_ORDER, UL_DATA, UL_SIZE, DATA_EVENT
3. Send ConnectCommand
4. Send GetIndexTable (MeasurementLog)
5. Send GetIndexTable (MeasurementSummary)
6. For each record: send GetSize → GetBody
7. Before disconnecting, write UNLOCK_MUTEX (0x20) to CONTROL_POINT
```

ConnectCommand **must** be followed by an IndexTable command. Sending a data command (e.g. GetBody) without first fetching the IndexTable causes the device to disconnect.

---

## Packet Framing

All commands and responses are split into **20-byte fixed-length packets**.

```
byte 0: flags + sequence number + payload length (upper 1 bit)
  bit 7: IS_FIRST (first packet of a message)
  bit 6: IS_LAST  (last packet of a message)
  bit 5-1: sequence number (0–31)
  bit 0: payload length, upper bit

byte 1: payload length, lower 8 bits (practical maximum is 18)

byte 2–19: payload (up to 18 bytes; unused bytes filled with 0xAA)
```

### Packet splitting

Commands are split into 18-byte chunks. The sequence number cycles 0–31 (32 packets = 1 block).

---

## Send/Receive Flow

### Sending (app → device)

```
1. Write total command byte count (uint32 LE) to DL_SIZE
2. Write packets sequentially to DL_DATA (write-without-response, 10ms between packets)
3. After every 32 packets, wait for an Indicate on DL_ORDER (block ACK)
4. If DL_ORDER is 0xAA (ACK_OK), proceed to next block
   If it is 0–31, re-send from that sequence number
5. After all packets are sent, wait for the final ACK on DL_ORDER
```

### Receiving (device → app)

```
1. Device notifies total response size on UL_SIZE (can be ignored)
2. Device sends packets on UL_DATA
3. After every 32 received packets, write 0xAA to UL_ORDER (block ACK)
4. After receiving the IS_LAST packet, write 0xAA to UL_ORDER (final ACK)
5. Transfer complete
```

### ⚠️ Critical bug and fix

**When a packet has both IS_LAST and seq==31 set (the last packet of a message falls exactly on a block boundary), sending two ACKs causes the device to disconnect.**

The device disconnects upon receiving the block ACK (seq==31), so the subsequent IS_LAST ACK write raises a BLE error.

```python
# WRONG — sends two ACKs
if seq == 31:
    await ul_ack()   # ← device disconnects here
if is_last:
    await ul_ack()   # ← BleakError: disconnected

# CORRECT — skip block ACK when IS_LAST is set
if seq == 31 and not is_last:
    await ul_ack()
if is_last:
    await ul_ack()
```

---

## Command Specification

All commands start with a 5-byte common header.

```
byte 0:   CommandId (uint8)
byte 1-4: PayloadSize (uint32 LE)
byte 5+:  payload
```

### CommandId values

| Name | Value |
|---|---|
| CONNECT | 0x00 |
| DISCONNECT | 0x01 |
| GET_DATA | 0x20 |
| SET_DATA | 0x21 |
| DELETE_DATA | 0x22 |
| RESET | 0x30 |

### ConnectCommand

```
[0x00][0x00 0x00 0x00 0x00]
```

No payload (5 bytes total). Must be the first command sent after connecting.

### DisconnectCommand

```
[0x01][0x05 0x00 0x00 0x00][0x00 0x00 0x00 0x00 0x00]
```

### GetDataClassIndexTable

```
[0x20][0x04 0x00 0x00 0x00][ClassId(2B LE)][ElementId=0x02][Filter(1B)]
```

**IndexTableFilter values**

| Name | Value | Meaning |
|---|---|---|
| None | 0x00 | All records |
| NotUploaded | 0x01 | Not yet uploaded |
| Uploaded | 0x02 | Already uploaded |
| PartiallyUploaded | 0x03 | Partially uploaded |

### GetDataClassBodySize

```
[0x20][0x05 0x00 0x00 0x00][ClassId(2B LE)][ElementId=0x41][Index(2B LE)]
```

### GetDataClassBody

```
[0x20][0x0D 0x00 0x00 0x00][ClassId(2B LE)][ElementId=0x00][Index(2B LE)][Offset(4B LE)][Size(4B LE)]
```

### SetDataClassUploadFlag

```
[0x21][0x06 0x00 0x00 0x00][ClassId(2B LE)][ElementId=0x42][Index(2B LE)][Flag(1B)]
```

**UploadFlag values**

| Name | Value |
|---|---|
| Uploaded | 0x00 |
| NotUploaded | 0x01 |
| PartiallyUploaded | 0x02 |

---

## DataClassElementId

| Name | Value | Meaning |
|---|---|---|
| BODY | 0x00 | Record body data |
| COUNT | 0x01 | Number of records |
| INDEX_TABLE | 0x02 | Index table |
| BODY_SIZE | 0x41 | Record size in bytes |
| UPLOAD_FLAG | 0x42 | Upload status flag |

---

## Data Class IDs

| Name | Value (uint16 LE) | Contents |
|---|---|---|
| MeasurementLog | 0x5100 (= 20736) | Heart rate body data (variable length) |
| MeasurementSummary | 0x5110 (= 20752) | Session metadata and timestamp (128 bytes fixed) |

---

## Response Common Header

```
byte 0:   CommandId (echoed)
byte 1-4: PayloadSize (uint32 LE)
byte 5:   ResultCode (0x00 = success)
byte 6-7: ClassId (uint16 LE)
byte 8:   ElementId
...       command-specific fields
```

### IndexTable response

```
byte 0-4:  common header
byte 5:    ResultCode
byte 6-7:  ClassId
byte 8:    ElementId (0x02)
byte 9:    Filter
byte 10-13: TableSize (uint32 LE, number of indices × 2 bytes)
byte 14+:  index array (uint16 LE × N)
```

### GetBodySize response

```
byte 0-4:  common header
byte 5:    ResultCode
byte 6-7:  ClassId
byte 8:    ElementId (0x41)
byte 9-10: Index (uint16 LE)
byte 11-14: Size (uint32 LE, bytes)
```

### GetBody response

```
byte 0-4:  common header
byte 5:    ResultCode
byte 6-7:  ClassId
byte 8:    ElementId (0x00)
byte 9-10: Index (uint16 LE)
byte 11-14: Offset (uint32 LE)
byte 15-18: Size (uint32 LE)
byte 19+:  body data
```

---

## MeasurementLog Body Format

Array of 4-byte entries, one per sample. 1 sample = 1 minute.

```
byte 0: heart rate (BPM)  — 0x00 or 0xFF means invalid sample
byte 1: flags1 (details unknown)
byte 2: unknown
byte 3: flags3 (details unknown)
```

Observed values range from 50 to 200 BPM.

---

## MeasurementSummary Timestamp Format

128 bytes fixed length. Timestamp is stored starting at offset 20.

```
offset 20: year  (uint16 LE, e.g. 0x07EA = 2026)
offset 22: month (uint8)
offset 23: day   (uint8)
offset 24: hour  (uint8)
offset 25: minute (uint8)
offset 26: second (uint8)
offset 27: timezone (uint8, in 15-minute units; JST = 36 → 36×15 = 540 min = UTC+9)
```

**The timestamp fields store UTC time.** The timezone field indicates the local timezone for display only — do **not** subtract it to derive UTC.

Converting to local time: `local_time = UTC + timezone_offset`

---

## CONTROL_POINT (b5a60030-...) values

| Name | Value | Purpose |
|---|---|---|
| SET_HIGH_SPEED | 0x08 | Enable high-speed transfer mode |
| LOCK_MUTEX | 0x10 | Acquire exclusive device lock |
| UNLOCK_MUTEX | 0x20 | Release exclusive lock (send before disconnecting) |
| FORCE_DISCONNECT | 0x80 | Force disconnect |

It is recommended to write `UNLOCK_MUTEX (0x20)` before disconnecting. Omitting it does not immediately break things, but may cause issues on the next connection attempt.

---

## BleManager Service-Level State Machine

The post-connect initialization sequence (`serviceLevel`) implemented in `BleManager.java` in the APK.

```
0 → connection established
1 → DATA_EVENT (b5a60031) notify subscription complete
2 → real-time HR notification configured (standard HRS: 0x180D)
3 → high-speed transfer mode set (write SET_HIGH_SPEED to CONTROL_POINT)
7 → initialization complete
```

For data export purposes, reproducing the full state machine is not required. Following the order **ConnectCommand → IndexTable → GetBody** is sufficient.

---

## Python Implementation

A macOS implementation is available in the `python/` directory.

```
python/
├── 09_get_history.py     # Main: downloads all records to CSV (supports checkpoint resume)
└── 10_fix_timestamps.py  # Offline: adds timestamps to the CSV output
```

### Running

```bash
cd python
pip install bleak
python 09_get_history.py <BLE_ADDRESS>
```

On macOS, BLE addresses are UUIDs (e.g. `276C6E62-0794-F8E4-DF48-33B52C0460C5`).

### Output files

| File | Contents |
|---|---|
| `heart_rate_history.csv` | Raw downloaded data (includes summary_hex) |
| `history_checkpoint.json` | Progress file for resuming after interruption |
| `history_indices.json` | Index cache for faster reconnection |

---

## Tools and Source Files Used for Analysis

- APK decompiled with **jadx** (PULSENSE View 2.2.6)
- Key classes referenced:
  - `WristableDataStream.java` — core BLE send/receive protocol
  - `BleManager.java` — connection lifecycle and state machine
  - `CommandId.java` — command ID constants
  - `DataClassElementId.java` — element ID constants
  - `DataClassId.java` — data class ID constants
  - `IndexTableFilter.java` — filter value constants
  - `UploadFlag.java` — upload flag constants
  - `SetDataClassUploadFlagCommand.java` — flag write command
  - `BleFlagger.java` — bulk upload flag logic
  - `DisconnectCommand.java` / `ResetCommand.java` — disconnect and reset commands
