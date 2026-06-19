#!/usr/bin/env python3
"""
PS-600 心拍履歴取得スクリプト
APK (WristableDataStream.java) のプロトコルを Python で再実装

プロトコル:
  送信: B5A60020(コマンドサイズ4B LE) → B5A60022(20Bパケット列, no-rsp)
  送信ACK: B5A60021(indicate: 0xAA=OK / 0..31=再送要求) ← デバイスから
  受信: B5A60012(notify: 20Bパケット列) ← デバイスから
  受信ACK: B5A60011(write: 0xAA=OK / 0..31=再送要求) → デバイスへ

使い方:
    python 09_get_history.py <ADDRESS>
"""

from __future__ import annotations
import asyncio
import struct
import sys
from datetime import datetime, timezone, timedelta
from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

# --- UUID定義 (WristableDataStream.java より) ---
UUID_DL_SIZE  = "b5a60020-2ed3-e993-896b-cceb986f8d73"  # write: コマンドサイズ
UUID_DL_DATA  = "b5a60022-2ed3-e993-896b-cceb986f8d73"  # write-no-rsp: コマンドパケット
UUID_DL_ORDER = "b5a60021-2ed3-e993-896b-cceb986f8d73"  # indicate: 送信ACK(デバイス→app)
UUID_UL_DATA  = "b5a60012-2ed3-e993-896b-cceb986f8d73"  # notify: レスポンスパケット(デバイス→app)
UUID_UL_ORDER = "b5a60011-2ed3-e993-896b-cceb986f8d73"  # write: 受信ACK(app→デバイス)
UUID_UL_SIZE  = "b5a60010-2ed3-e993-896b-cceb986f8d73"  # indicate: レスポンスサイズ通知
UUID_DATA_EVENT    = "b5a60031-2ed3-e993-896b-cceb986f8d73"  # notify: データイベント通知
UUID_CONTROL_POINT = "b5a60030-2ed3-e993-896b-cceb986f8d73"  # write: 制御ポイント

# CONTROL_POINT コマンド値 (WristableDataStream.java DATA_CONTROL_POINT_* 定数)
CP_SET_HIGH_SPEED = 0x08   # SET_HIGH_SPEED
CP_LOCK_MUTEX     = 0x10   # LOCK_MUTEX (ConnectCommand前に必須)
CP_UNLOCK_MUTEX   = 0x20   # UNLOCK_MUTEX (切断前に必要)

# --- 定数 ---
CMD_CONNECT  = 0x00   # CommandId.CONNECT
CMD_GET_DATA = 0x20   # CommandId.GET_DATA
ELEM_BODY       = 0x00  # DataClassElementId.BODY
ELEM_INDEX_TABLE= 0x02  # DataClassElementId.INDEX_TABLE
ELEM_BODY_SIZE  = 0x41  # DataClassElementId.BODY_SIZE (65)
FILTER_NONE     = 0x00  # IndexTableFilter.None
DATA_CLASS_MEASUREMENT_LOG     = 20736  # 0x5100  MeasurementLog (心拍履歴, 可変長)
DATA_CLASS_MEASUREMENT_SUMMARY = 20752  # 0x5110  MeasurementSummary (128B固定, タイムスタンプ含む)

PACKET_SIZE  = 20
PAYLOAD_SIZE = 18
IS_FIRST = 0x80
IS_LAST  = 0x40
ACK_OK   = 0xAA     # PACKET_SEQUENCE_OK
BLOCK_SIZE = 32


# --- パケット生成 ---

def make_packet(seq: int, payload: bytes, first: bool, last: bool) -> bytes:
    length = len(payload)
    b0 = ((IS_FIRST if first else 0) | (IS_LAST if last else 0)
          | ((seq & 0x1F) << 1) | ((length >> 8) & 0x01))
    b1 = length & 0xFF
    buf = bytearray(PACKET_SIZE)
    buf[0] = b0
    buf[1] = b1
    for i in range(PAYLOAD_SIZE):
        buf[2 + i] = payload[i] if i < length else 0xAA
    return bytes(buf)


def divide_command(data: bytes) -> list[bytes]:
    packets, seq, off = [], 0, 0
    while off < len(data):
        end = min(off + PAYLOAD_SIZE, len(data))
        chunk = data[off:end]
        packets.append(make_packet(seq % BLOCK_SIZE, chunk, off == 0, end >= len(data)))
        seq += 1
        off = end
    return packets


# --- コマンド生成 (各Commandクラスの createRequest() 相当) ---

def cmd_connect() -> bytes:
    return struct.pack('<BI', CMD_CONNECT, 0)  # 5 bytes

def cmd_get_index_table(class_id: int, filter_val: int = FILTER_NONE) -> bytes:
    payload = struct.pack('<HBB', class_id, ELEM_INDEX_TABLE, filter_val)
    return struct.pack('<BI', CMD_GET_DATA, len(payload)) + payload  # 9 bytes

def cmd_get_size(class_id: int, index: int) -> bytes:
    payload = struct.pack('<HBH', class_id, ELEM_BODY_SIZE, index)
    return struct.pack('<BI', CMD_GET_DATA, len(payload)) + payload  # 10 bytes

def cmd_get_body(class_id: int, index: int, offset: int, size: int) -> bytes:
    payload = struct.pack('<HBHII', class_id, ELEM_BODY, index, offset, size)
    return struct.pack('<BI', CMD_GET_DATA, len(payload)) + payload  # 18 bytes


# --- プロトコル実装 ---

class PS600Protocol:
    def __init__(self, client: BleakClient):
        self.client = client
        self._dl_ack = asyncio.Event()
        self._dl_ack_val: int | None = None
        self._ul_queue: asyncio.Queue = asyncio.Queue()

    async def setup(self):
        await self.client.start_notify(UUID_DL_ORDER,    self._on_dl_order)
        await self.client.start_notify(UUID_UL_DATA,     self._on_ul_data)
        await self.client.start_notify(UUID_UL_SIZE,     self._on_ul_size)
        await self.client.start_notify(UUID_DATA_EVENT,  self._on_data_event)
        print("  通知購読完了")

    def _on_data_event(self, _: BleakGATTCharacteristic, data: bytearray):
        pass  # SummaryReady等のイベント通知 (現時点では無視)

    async def teardown(self):
        for uuid in [UUID_DL_ORDER, UUID_UL_DATA, UUID_UL_SIZE]:
            try:
                await self.client.stop_notify(uuid)
            except Exception:
                pass

    def _on_dl_order(self, _: BleakGATTCharacteristic, data: bytearray):
        self._dl_ack_val = data[0] if data else None
        self._dl_ack.set()

    def _on_ul_size(self, _: BleakGATTCharacteristic, data: bytearray):
        pass  # verbose suppressed

    def _on_ul_data(self, _: BleakGATTCharacteristic, data: bytearray):
        if len(data) >= 2:
            self._ul_queue.put_nowait(bytes(data))

    async def _ul_ack(self, val: int = ACK_OK):
        await self.client.write_gatt_char(UUID_UL_ORDER, bytes([val]), response=True)

    async def send(self, command: bytes, timeout: float = 30.0,
                   _dbg: str = "") -> bytes | None:
        packets = divide_command(command)

        # キューをリセット
        while not self._ul_queue.empty():
            self._ul_queue.get_nowait()
        self._dl_ack.clear()

        # コマンドサイズ送信
        if _dbg:
            print(f"  [dbg:{_dbg}] DL_SIZE 送信...")
        await self.client.write_gatt_char(UUID_DL_SIZE, struct.pack('<I', len(command)), response=True)

        # パケット送信 (ブロック単位でACKを待つ)
        if _dbg:
            print(f"  [dbg:{_dbg}] DL_DATA 送信 ({len(packets)}pkt)...")
        deadline = asyncio.get_event_loop().time() + timeout
        for block_start in range(0, len(packets), BLOCK_SIZE):
            block = packets[block_start:block_start + BLOCK_SIZE]
            self._dl_ack.clear()

            for pkt in block:
                await self.client.write_gatt_char(UUID_DL_DATA, pkt, response=False)
                await asyncio.sleep(0.01)

            remaining = deadline - asyncio.get_event_loop().time()
            try:
                await asyncio.wait_for(self._dl_ack.wait(), max(remaining, 1.0))
            except asyncio.TimeoutError:
                print("  [error] 送信ACKタイムアウト")
                return None

            ack = self._dl_ack_val
            if ack is not None and 0 <= ack <= 31:
                print(f"  [warn] 再送要求: seq={ack}")
            elif ack != ACK_OK:
                print(f"  [warn] 不明なACK: {ack:#04x}")

        # レスポンス受信 (ブロックごとにACKを送りながらパケットを組み立てる)
        if _dbg:
            print(f"  [dbg:{_dbg}] DL_ACK受信OK → レスポンス待機...")
        return await self._receive_response(deadline, _dbg=bool(_dbg))

    async def _receive_response(self, deadline: float, _dbg: bool = False) -> bytes | None:
        result = bytearray()
        pkt_count = 0

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                if _dbg:
                    print(f"  [recv] queue待機中 (残り{remaining:.1f}s)...")
                pkt = await asyncio.wait_for(self._ul_queue.get(), max(remaining, 1.0))
                if _dbg:
                    print(f"  [recv] pkt受信: {pkt.hex()}")
            except asyncio.TimeoutError:
                print("  [error] レスポンスタイムアウト")
                return None

            if len(pkt) < 2:
                continue

            seq    = (pkt[0] & 0x3E) >> 1   # bits 5-1
            is_last = bool(pkt[0] & IS_LAST)
            length = ((pkt[0] & 0x01) << 8) | pkt[1]
            result.extend(pkt[2:2 + length])
            pkt_count += 1

            # IS_LASTのときはブロックACKをスキップして最終ACKのみ送る
            # (seq==31 && IS_LAST の両方でACKを2回送るとデバイスが切断する)
            if seq == 31 and not is_last:
                if _dbg: print(f"  [recv] block ACK送信...")
                await self._ul_ack(ACK_OK)
                if _dbg: print(f"  [recv] block ACK完了")

            if is_last:
                if _dbg: print(f"  [recv] IS_LAST確認 → final ACK送信...")
                await self._ul_ack(ACK_OK)
                if _dbg: print(f"  [recv] final ACK完了, sleep(0.3)...")
                await asyncio.sleep(0.3)
                if _dbg: print(f"  [recv] 返却: {len(result)}bytes")
                return bytes(result)


# --- レスポンスパーサー ---

def parse_common_header(resp: bytes) -> tuple[int, int]:
    """共通ヘッダー: [cmd_id(1), payload_size(4)] → (cmd_id, payload_size)"""
    if len(resp) < 5:
        return -1, -1
    cmd_id, payload_size = struct.unpack_from('<BI', resp, 0)
    return cmd_id, payload_size

def parse_index_table(resp: bytes, class_id: int) -> list[int] | None:
    """GetDataClassIndexTable レスポンスのパース"""
    if len(resp) < 14:
        return None
    # common(5) + result(1) + classId(2) + elemId(1) + filter(1) + tableSize(4)
    result_code = resp[5]
    table_size = struct.unpack_from('<I', resp, 10)[0]
    indices = []
    for i in range(14, 14 + table_size, 2):
        if i + 1 < len(resp):
            indices.append(struct.unpack_from('<H', resp, i)[0])
    return indices if result_code == 0 else None

def parse_data_size(resp: bytes) -> int | None:
    """GetDataClassSize レスポンスのパース
    common(5) + cmd_hdr(10): result(1)+classId(2)+elemId(1)+index(2)+size(4)
    """
    if len(resp) < 15:
        return None
    result_code = resp[5]
    # offset: 5(common) + 1(result) + 2(classId) + 1(elemId) + 2(index) = 11
    data_size = struct.unpack_from('<I', resp, 11)[0]
    return data_size if result_code == 0 else None

def parse_body(resp: bytes) -> bytes | None:
    """GetDataClassBody レスポンスのパース
    common(5) + cmd_hdr(14): result(1)+classId(2)+elemId(1)+index(2)+offset(4)+size(4)
    payload starts at offset 19
    """
    if len(resp) < 19:
        return None
    result_code = resp[5]
    if result_code != 0:
        return None
    return resp[19:]


# --- メイン ---

async def main():
    import csv, json, os
    if len(sys.argv) < 2:
        print(f"使い方: python {sys.argv[0]} <ADDRESS>")
        sys.exit(1)

    address          = sys.argv[1]
    idx_cache_path   = "history_indices.json"
    checkpoint_path  = "history_checkpoint.json"
    csv_path         = "heart_rate_history.csv"

    # チェックポイントとキャッシュを読み込む
    checkpoint: dict = {}
    if os.path.exists(checkpoint_path):
        with open(checkpoint_path) as f:
            checkpoint = json.load(f)

    cached_indices: list[int] | None = None
    cached_summary: set[int] | None = None
    if os.path.exists(idx_cache_path):
        with open(idx_cache_path) as f:
            c = json.load(f)
        cached_indices   = c["log_indices"]
        cached_summary   = set(c["summary_indices"])
        print(f"インデックスキャッシュ読込: {len(cached_indices)}件 (処理済み: {len(checkpoint)}件)")

    print(f"\n{address} に接続中...")

    async with BleakClient(address) as client:
        print("接続成功")
        proto = PS600Protocol(client)
        await proto.setup()

        # Step 1: ConnectCommand
        resp = await proto.send(cmd_connect())
        if resp is None:
            print("ConnectCommand 失敗"); return
        print("ConnectCommand OK")

        # Step 2: IndexTable (ConnectCommand後に必ず実行)
        # デバイスはConnectCommand→IndexTable→データコマンドの順序を要求する
        print("インデックス取得中...")
        resp = await proto.send(cmd_get_index_table(DATA_CLASS_MEASUREMENT_LOG))
        if resp is None:
            print("MeasLog IndexTable 取得失敗"); return
        fresh_log = parse_index_table(resp, DATA_CLASS_MEASUREMENT_LOG) or []

        resp = await proto.send(cmd_get_index_table(DATA_CLASS_MEASUREMENT_SUMMARY))
        fresh_sum = (parse_index_table(resp, DATA_CLASS_MEASUREMENT_SUMMARY) or []) if resp else []

        if cached_indices is None:
            cached_indices = fresh_log
            cached_summary = set(fresh_sum)
        # キャッシュがある場合は使い続ける (fresh_logは順序確認のみ)
        print(f"  MeasLog: {len(fresh_log)}件, Summary: {len(fresh_sum)}件")

        with open(idx_cache_path, "w") as f:
            json.dump({"log_indices": cached_indices,
                       "summary_indices": list(cached_summary or set())}, f)

        indices        = cached_indices
        summary_indices = cached_summary or set()
        remaining_cnt  = sum(1 for idx in indices if str(idx) not in checkpoint)
        print(f"\n未処理: {remaining_cnt} / {len(indices)}件\n")

        if remaining_cnt == 0:
            print("すべて処理済み")
            await proto.teardown()
            return

        # Step 3: レコード取得ループ
        write_header = not os.path.exists(csv_path)
        csv_file   = open(csv_path, "a", newline="")
        csv_writer = csv.writer(csv_file)
        if write_header:
            csv_writer.writerow(["unix_timestamp", "record_index", "sample_num",
                                  "heart_rate_bpm", "flags1", "flags3", "summary_hex"])

        total_hr_rows = 0
        i_total       = len(indices)

        _dbg_remaining = 3  # 最初の3コマンドだけ詳細デバッグ
        try:
            for i, idx in enumerate(indices):
                if str(idx) in checkpoint:
                    continue

                dbg = f"GetSize[{idx}]" if _dbg_remaining > 0 else ""
                if _dbg_remaining > 0: _dbg_remaining -= 1
                resp = await proto.send(cmd_get_size(DATA_CLASS_MEASUREMENT_LOG, idx),
                                        _dbg=dbg)
                if resp is None:
                    checkpoint[str(idx)] = None; continue
                data_size = parse_data_size(resp)
                if not data_size:
                    checkpoint[str(idx)] = None; continue

                dbg2 = f"GetBody[{idx},{data_size}]" if _dbg_remaining > 0 else ""
                if _dbg_remaining > 0: _dbg_remaining -= 1
                resp = await proto.send(cmd_get_body(DATA_CLASS_MEASUREMENT_LOG, idx, 0, data_size),
                                        _dbg=dbg2)
                if resp is None:
                    checkpoint[str(idx)] = None; continue
                payload = parse_body(resp)
                if not payload:
                    checkpoint[str(idx)] = None; continue

                # Summary からタイムスタンプ
                start_ts_epoch: int | None = None
                summary_hex = ""
                if idx in summary_indices:
                    sr = await proto.send(cmd_get_size(DATA_CLASS_MEASUREMENT_SUMMARY, idx))
                    if sr is not None:
                        ss = parse_data_size(sr)
                        if ss:
                            sr2 = await proto.send(
                                cmd_get_body(DATA_CLASS_MEASUREMENT_SUMMARY, idx, 0, ss))
                            if sr2 is not None:
                                sp = parse_body(sr2)
                                if sp and len(sp) >= 28:
                                    summary_hex = sp.hex()
                                    try:
                                        year = struct.unpack_from('<H', sp, 20)[0]
                                        mo,d,h,mi,s = sp[22],sp[23],sp[24],sp[25],sp[26]
                                        tz = timezone(timedelta(minutes=sp[27]*15))
                                        dt = datetime(year,mo,d,h,mi,s,tzinfo=tz)
                                        start_ts_epoch = int(dt.timestamp())
                                    except (ValueError, struct.error):
                                        pass

                num_samples = len(payload) // 4
                valid = 0
                for s in range(num_samples):
                    off = s * 4
                    hr = payload[off]; f1 = payload[off+1]; f3 = payload[off+3]
                    if hr == 0 or hr == 0xFF: continue
                    ts = (start_ts_epoch + s * 60) if start_ts_epoch else ""
                    csv_writer.writerow([ts, idx, s, hr, f1, f3,
                                         summary_hex if s == 0 else ""])
                    valid += 1

                csv_file.flush()
                total_hr_rows += valid
                checkpoint[str(idx)] = {"samples": num_samples, "valid_hr": valid}
                if valid > 0:
                    print(f"  [{len(checkpoint)}/{i_total}] index={idx}: "
                          f"{num_samples}サンプル, 有効HR={valid}件")

                if len(checkpoint) % 50 == 0:
                    with open(checkpoint_path, "w") as f:
                        json.dump(checkpoint, f)

        except Exception as e:
            print(f"\n  [中断] {e}")
        finally:
            csv_file.close()
            # UNLOCK_MUTEX: デバイスのmutexを解放してから切断
            try:
                await client.write_gatt_char(UUID_CONTROL_POINT,
                                             bytes([CP_UNLOCK_MUTEX]), response=True)
                print("  UNLOCK_MUTEX OK")
            except Exception:
                pass
            await proto.teardown()
            with open(checkpoint_path, "w") as f:
                json.dump(checkpoint, f)
            print(f"\n  保存: +{total_hr_rows}行, checkpoint={len(checkpoint)}/{i_total}件")

    print("\n完了")


if __name__ == "__main__":
    asyncio.run(main())
