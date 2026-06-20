# PS-600 BLE プロトコル解析ノート

EPSON PULSENSE PS-600 の BLE 通信プロトコルを、公式 Android アプリ（PULSENSE View 2.2.6）の APK を jadx で逆コンパイルして解析した結果をまとめたものです。

> English version → [PROTOCOL.md](PROTOCOL.md)

---

## BLE サービス・キャラクタリスティック

すべてのキャラクタリスティックは単一の GATT サービスに属します。

**サービス UUID**
```
b5a60000-2ed3-e993-896b-cceb986f8d73
```

| 役割 | UUID | 方向 | プロパティ |
|---|---|---|---|
| DL_SIZE | b5a60020-... | app → device | Write (with response) |
| DL_DATA | b5a60022-... | app → device | Write Without Response |
| DL_ORDER | b5a60021-... | device → app | Indicate（送信 ACK） |
| UL_SIZE | b5a60010-... | device → app | Indicate（受信データ総サイズ通知） |
| UL_DATA | b5a60012-... | device → app | Notify（受信パケット） |
| UL_ORDER | b5a60011-... | app → device | Write (with response)（受信 ACK） |
| CONTROL_POINT | b5a60030-... | app → device | Write (with response) |
| DATA_EVENT | b5a60031-... | device → app | Notify（SummaryReady 等のイベント） |

UUID の末尾はすべて共通: `-2ed3-e993-896b-cceb986f8d73`

---

## 接続手順

```
1. BleakClient / Web Bluetooth で接続
2. DL_ORDER / UL_DATA / UL_SIZE / DATA_EVENT の通知を購読
3. ConnectCommand を送信
4. GetIndexTable (MeasurementLog) を送信
5. GetIndexTable (MeasurementSummary) を送信
6. 以降、レコードごとに GetSize → GetBody を繰り返す
7. 切断前に CONTROL_POINT へ UNLOCK_MUTEX (0x20) を書き込む
```

ConnectCommand の直後に必ず IndexTable を取得する順序が必要です。ConnectCommand → データコマンド（IndexTable なし）の順序ではデバイスが切断します。

---

## パケットフレーミング

すべてのコマンド・レスポンスは **20 バイト固定長パケット**に分割されます。

```
byte 0: フラグ + シーケンス番号 + ペイロード長 (上位1bit)
  bit 7: IS_FIRST (先頭パケット)
  bit 6: IS_LAST  (末尾パケット)
  bit 5-1: シーケンス番号 (0〜31)
  bit 0: ペイロード長 上位1bit

byte 1: ペイロード長 下位8bit (最大 255、実質 18 で固定)

byte 2〜19: ペイロード (最大 18 バイト、未使用部分は 0xAA で埋める)
```

### パケット分割

コマンドは 18 バイト単位でパケットに分割します。シーケンス番号は 0〜31 を循環します（32 パケットで 1 ブロック）。

---

## 送受信フロー

### 送信（app → device）

```
1. DL_SIZE に コマンド総バイト数 (uint32 LE) を write
2. DL_DATA に パケットを順番に write-without-response（パケット間に 10ms 待機）
3. 32 パケット送信するごとに DL_ORDER の Indicate を待つ (ACK)
4. DL_ORDER が 0xAA (ACK_OK) なら次のブロックへ
   0〜31 なら再送要求（その seq から再送）
5. 全パケット送信後、DL_ORDER の最終 ACK を待つ
```

### 受信（device → app）

```
1. デバイスが UL_SIZE で総バイト数を通知（無視してよい）
2. デバイスが UL_DATA でパケットを送信してくる
3. 32 パケット受信するごとに UL_ORDER へ 0xAA を write（ブロック ACK）
4. IS_LAST パケットを受信したら UL_ORDER へ 0xAA を write（最終 ACK）
5. 受信完了
```

### ⚠️ 重要バグと修正

**IS_LAST と seq==31 が同時に立つパケット（ブロック境界が最終パケットと一致する場合）に 2 回 ACK を送るとデバイスが切断する。**

デバイスはブロック ACK（seq==31）を受け取った時点で切断するため、IS_LAST の ACK を送る前に切断される。

```python
# 誤り（2回 ACK を送る）
if seq == 31:
    await ul_ack()   # ← デバイスがここで切断
if is_last:
    await ul_ack()   # ← BleakError: disconnected

# 正しい（IS_LAST のときはブロック ACK をスキップ）
if seq == 31 and not is_last:
    await ul_ack()
if is_last:
    await ul_ack()
```

---

## コマンド仕様

すべてのコマンドは共通ヘッダー（5 バイト）から始まります。

```
byte 0:   CommandId (uint8)
byte 1-4: PayloadSize (uint32 LE)
byte 5〜: ペイロード
```

### CommandId

| 名前 | 値 |
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

ペイロードなし（5 バイト固定）。接続後最初に送る必須コマンド。

### DisconnectCommand

```
[0x01][0x05 0x00 0x00 0x00][0x00 0x00 0x00 0x00 0x00]
```

### GetDataClassIndexTable

```
[0x20][0x04 0x00 0x00 0x00][ClassId(2B LE)][ElementId=0x02][Filter(1B)]
```

**IndexTableFilter 値**

| 名前 | 値 | 意味 |
|---|---|---|
| None | 0x00 | すべてのレコード |
| NotUploaded | 0x01 | 未アップロードのみ |
| Uploaded | 0x02 | アップロード済みのみ |
| PartiallyUploaded | 0x03 | 部分アップロードのみ |

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

**UploadFlag 値**

| 名前 | 値 |
|---|---|
| Uploaded | 0x00 |
| NotUploaded | 0x01 |
| PartiallyUploaded | 0x02 |

---

## DataClassElementId

| 名前 | 値 | 意味 |
|---|---|---|
| BODY | 0x00 | レコード本体 |
| COUNT | 0x01 | 件数 |
| INDEX_TABLE | 0x02 | インデックステーブル |
| BODY_SIZE | 0x41 | レコードサイズ（バイト数） |
| UPLOAD_FLAG | 0x42 | アップロードフラグ |

---

## データクラス ID

| 名前 | 値 (uint16 LE) | 内容 |
|---|---|---|
| MeasurementLog | 0x5100 (=20736) | 心拍データ本体（可変長） |
| MeasurementSummary | 0x5110 (=20752) | セッション情報・タイムスタンプ（128B 固定） |

---

## レスポンス共通ヘッダー

```
byte 0:   CommandId (echo)
byte 1-4: PayloadSize (uint32 LE)
byte 5:   ResultCode (0x00 = 成功)
byte 6-7: ClassId (uint16 LE)
byte 8:   ElementId
...       コマンドごとの追加フィールド
```

### IndexTable レスポンス

```
byte 0-4:  共通ヘッダー
byte 5:    ResultCode
byte 6-7:  ClassId
byte 8:    ElementId (0x02)
byte 9:    Filter
byte 10-13: TableSize (uint32 LE, インデックス数 × 2 バイト)
byte 14〜: インデックス配列 (uint16 LE × N)
```

### GetBodySize レスポンス

```
byte 0-4:  共通ヘッダー
byte 5:    ResultCode
byte 6-7:  ClassId
byte 8:    ElementId (0x41)
byte 9-10: Index (uint16 LE)
byte 11-14: Size (uint32 LE, バイト数)
```

### GetBody レスポンス

```
byte 0-4:  共通ヘッダー
byte 5:    ResultCode
byte 6-7:  ClassId
byte 8:    ElementId (0x00)
byte 9-10: Index (uint16 LE)
byte 11-14: Offset (uint32 LE)
byte 15-18: Size (uint32 LE)
byte 19〜: ボディデータ
```

---

## MeasurementLog ボディのデータ形式

4 バイト × サンプル数の配列。1 サンプル = 1 分。

```
byte 0: 心拍数 (BPM)  ※ 0x00 または 0xFF は無効サンプル
byte 1: flags1 (詳細不明)
byte 2: 不明
byte 3: flags3 (詳細不明)
```

実測値は 50〜200 BPM の範囲。

---

## MeasurementSummary のタイムスタンプ形式

128 バイト固定長。オフセット 20 からタイムスタンプが格納されています。

```
offset 20: 年 (uint16 LE, 例: 0x07EA = 2026)
offset 22: 月 (uint8)
offset 23: 日 (uint8)
offset 24: 時 (uint8)
offset 25: 分 (uint8)
offset 26: 秒 (uint8)
offset 27: タイムゾーン (uint8, 15分単位, JST=36 → 36×15=540分=UTC+9)
```

**タイムスタンプのフィールドは UTC 時刻を保存している。** タイムゾーンフィールドは表示用のローカルタイムゾーンを示すのみで、UTC を求めるために引き算する必要はない。

ローカル時刻への変換: `ローカル時刻 = UTC + タイムゾーンオフセット`

---

## CONTROL_POINT (b5a60030-...) の制御値

| 名前 | 値 | 用途 |
|---|---|---|
| SET_HIGH_SPEED | 0x08 | 高速転送モード有効化 |
| LOCK_MUTEX | 0x10 | デバイスの排他ロック取得 |
| UNLOCK_MUTEX | 0x20 | 排他ロック解放（切断前に送る） |
| FORCE_DISCONNECT | 0x80 | 強制切断 |

切断前に `UNLOCK_MUTEX (0x20)` を送ることを推奨します。送らなくてもデバイスは機能しますが、次回接続時に問題が生じる可能性があります。

---

## BleManager のサービスレベル状態遷移

APK の `BleManager.java` に実装されている接続後の初期化シーケンス（serviceLevel）。

```
0 → 接続完了
1 → DATA_EVENT (b5a60031) の notify 購読完了
2 → 心拍リアルタイム通知設定（標準 HRS: 0x180D）
3 → 高速転送モード設定（CONTROL_POINT に SET_HIGH_SPEED 送信）
7 → 初期化完了
```

データ取得のみ行う場合は、状態遷移を厳密に再現しなくても動作します（ConnectCommand → IndexTable → GetBody の順序さえ守れば十分）。

---

## Python 実装

`python/` ディレクトリに macOS 向けの実装があります。

```
python/
├── 09_get_history.py     # メイン: 全レコードを CSV に保存（チェックポイント再開対応）
└── 10_fix_timestamps.py  # オフライン処理: CSV にタイムスタンプを付与
```

### 実行方法

```bash
cd python
pip install bleak
python 09_get_history.py <BLE_ADDRESS>
```

macOS の BLE アドレスは UUID 形式（例: `276C6E62-0794-F8E4-DF48-33B52C0460C5`）。

### 出力ファイル

| ファイル | 内容 |
|---|---|
| `heart_rate_history.csv` | 取得した生データ（summary_hex 含む） |
| `history_checkpoint.json` | 進捗（中断後の再開に使用） |
| `history_indices.json` | インデックスキャッシュ（再接続時の高速化） |

---

## 解析に使用したツール・ファイル

- `jadx` による APK 逆コンパイル（PULSENSE View 2.2.6）
- 参照した主要なクラス:
  - `WristableDataStream.java` — BLE 送受信プロトコル本体
  - `BleManager.java` — 接続ライフサイクル・状態機械
  - `CommandId.java` — コマンド ID 定数
  - `DataClassElementId.java` — 要素 ID 定数
  - `DataClassId.java` — データクラス ID 定数
  - `IndexTableFilter.java` — フィルタ値定数
  - `UploadFlag.java` — アップロードフラグ定数
  - `SetDataClassUploadFlagCommand.java` — フラグ書き込みコマンド
  - `BleFlagger.java` — アップロードフラグ一括設定ロジック
  - `DisconnectCommand.java` / `ResetCommand.java` — 切断・リセットコマンド
