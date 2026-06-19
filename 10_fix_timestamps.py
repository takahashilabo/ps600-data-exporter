#!/usr/bin/env python3
"""
heart_rate_history.csv のタイムスタンプを修正するオフライン処理スクリプト
BLE接続不要。既存CSVのsummary_hexからタイムスタンプを復元する。

Summary バイト構造 (offset 20〜):
  +0  uint16 LE: 年 (e.g. 0x07ea = 2026)
  +2  uint8: 月
  +3  uint8: 日
  +4  uint8: 時
  +5  uint8: 分
  +6  uint8: 秒
  +7  uint8: タイムゾーン (15分単位, 0x24=36=UTC+9=JST)
"""

import csv
import struct
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

SUMMARY_DT_OFFSET = 20
SAMPLE_INTERVAL_SEC = 60  # 1サンプル = 1分と仮定

def parse_summary_dt(hex_str: str) -> datetime | None:
    if not hex_str:
        return None
    try:
        sp = bytes.fromhex(hex_str)
        if len(sp) < SUMMARY_DT_OFFSET + 7:
            return None
        year   = struct.unpack_from('<H', sp, SUMMARY_DT_OFFSET)[0]
        month  = sp[SUMMARY_DT_OFFSET + 2]
        day    = sp[SUMMARY_DT_OFFSET + 3]
        hour   = sp[SUMMARY_DT_OFFSET + 4]
        minute = sp[SUMMARY_DT_OFFSET + 5]
        second = sp[SUMMARY_DT_OFFSET + 6]
        tz_q   = sp[SUMMARY_DT_OFFSET + 7] if len(sp) > SUMMARY_DT_OFFSET + 7 else 36
        tz     = timezone(timedelta(minutes=tz_q * 15))
        return datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except (ValueError, struct.error):
        return None


def main():
    src = Path("heart_rate_history.csv")
    dst = Path("heart_rate_timestamps.csv")

    if not src.exists():
        print(f"{src} が見つかりません")
        return

    # 1. CSVを読み込み、record_indexごとに開始時刻を解決
    rows_by_index: dict[int, list[dict]] = defaultdict(list)
    start_dts: dict[int, datetime] = {}

    with open(src, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            idx = int(row["record_index"])
            rows_by_index[idx].append(row)
            if int(row["sample_num"]) == 0 and row.get("summary_hex"):
                dt = parse_summary_dt(row["summary_hex"])
                if dt:
                    start_dts[idx] = dt

    total = sum(len(v) for v in rows_by_index.values())
    with_ts = sum(1 for idx in rows_by_index if idx in start_dts)
    print(f"読込: {total}行, {len(rows_by_index)}レコード")
    print(f"タイムスタンプあり: {with_ts}レコード / {len(rows_by_index)}件")

    if start_dts:
        dts = sorted(start_dts.values())
        print(f"期間: {dts[0].strftime('%Y-%m-%d %H:%M:%S %Z')} 〜 {dts[-1].strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # 2. タイムスタンプ付きCSVを書き出し
    out_rows = 0
    with open(dst, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["datetime_jst", "unix_timestamp", "record_index",
                         "sample_num", "heart_rate_bpm", "flags1", "flags3"])

        for idx in sorted(rows_by_index.keys()):
            start_dt = start_dts.get(idx)
            for row in rows_by_index[idx]:
                sample = int(row["sample_num"])
                hr     = int(row["heart_rate_bpm"])
                f1     = row.get("flags1", "")
                f3     = row.get("flags3", "")

                if start_dt:
                    dt  = start_dt + timedelta(seconds=sample * SAMPLE_INTERVAL_SEC)
                    ts  = int(dt.timestamp())
                    dts = dt.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    ts  = ""
                    dts = ""

                writer.writerow([dts, ts, idx, sample, hr, f1, f3])
                out_rows += 1

    print(f"\n出力: {dst} ({out_rows}行)")

    # 3. サマリー表示
    print("\n=== 日付別 心拍数サマリー ===")
    by_date: dict[str, list[int]] = defaultdict(list)
    for idx, dt in start_dts.items():
        date_str = dt.strftime("%Y-%m-%d")
        for row in rows_by_index[idx]:
            hr = int(row["heart_rate_bpm"])
            by_date[date_str].append(hr)

    for date in sorted(by_date.keys()):
        hrs = by_date[date]
        print(f"  {date}: {len(hrs):4d}件  avg={sum(hrs)/len(hrs):.0f}  min={min(hrs)}  max={max(hrs)} BPM")


if __name__ == "__main__":
    main()
