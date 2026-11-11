# odelic.py

ODELIC（オーデリック）の Bluetooth 照明を PC から操作するための**非公式** Python ライブラリです。

公式 Android アプリ（`jp.co.odelic.smt.remote10` v1.10.39）の通信方式を解析して作りました。対象はアプリが使っている Pairlink connected-mesh 系の機器です。クラウドは使わず、BLE でローカルに直接操作します。

- 依存ライブラリは [bleak](https://github.com/hbldh/bleak) だけです。暗号処理（AES-128）は純 Python で実装しています。
- macOS で動作を確認しています。bleak が動く環境（Linux / Windows）でも動く想定です。
- メーカー非公認のツールです。利用は自己責任でお願いします。

## セットアップ

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

### 必要な情報：アプリの ID

アプリのメニュー画面に `ID:12345678` のような表示があります。これは **HomeID とパスワード（末尾4文字）をつなげたもの**です。この例では HomeID が 1234、パスワードが `5678` になります。
CLI では `--id` で指定するか、環境変数 `ODELIC_ID` に設定してください。

## CLI

```sh
export ODELIC_ID=12345678   # 自分のアプリに表示される ID

odelic scan                                   # 近くのメッシュノードを探す（ログインしない）
odelic devices                                # メッシュ内の照明一覧（vaddr / MAC / 製品コード）
odelic on                                     # 全点灯（前回の色・明るさに戻る）
odelic off                                    # 全消灯
odelic light --color 0 --brightness 100       # 暖色（電球色）で最大の明るさ
odelic light --color 100 --brightness 30      # 寒色（昼光色）で 30%
odelic night 0                                # 常夜灯（0〜2）
odelic -t 00000000 off                        # 1台だけ操作（vaddr は devices で確認）
odelic group 1 on                             # グループ単位（グループ ID はアプリでの設定）
odelic scene save 1 / odelic scene play 1     # シーンの保存・呼び出し
odelic -v ...                                 # 送受信パケットを表示する
```

## ライブラリとして使う

```python
import asyncio
import os
from odelic import OdelicMesh

async def main():
    async with OdelicMesh(os.environ["ODELIC_ID"]) as mesh:
        await mesh.set_light(color=0, brightness=100)   # 0 = 暖色 .. 100 = 寒色
        for d in await mesh.devices():
            print(d)
        await mesh.off()

asyncio.run(main())
```

| メソッド | 内容 |
|---|---|
| `on(target)` / `off(target)` | 点灯・消灯。`target` を省略すると全照明が対象 |
| `set_light(color, brightness, target, x=, y=)` | 色と明るさの指定。生の値 x (0–20) / y (0–19, 0 が最も明るい) でも指定可 |
| `night_light(level, target)` | 常夜灯（暖色の最も暗い 3 段階） |
| `set_group(group_id, color, brightness, on=)` | グループ単位の操作 |
| `change_color(target)` | 色プリセットの切り替え（対応機種のみ） |
| `save_scene(i)` / `play_scene(i)` | シーンの保存・呼び出し |
| `devices()` | ping でメッシュ内の照明を列挙 |
| `request_status(target)` | 状態の問い合わせ（応答は未デコードの生データ） |
| `send(packet)` | 任意の type-3 パケットを暗号化して送信（`odelic.protocol` を参照） |

## 注意

- メッシュノードは **同時に 1 台のコントローラとしか接続できません**。スマホのアプリが接続中だと
  `node is already in use by another controller` になります。アプリを閉じるか、スマホの Bluetooth を切ってから試してください。
- macOS では初回実行時に、ターミナル（または実行元のアプリ）への Bluetooth 許可を求められます。
- **新しい照明の登録**には未対応です。アプリはスマホから独自形式の電波を発信して登録していますが、macOS の標準 Bluetooth 機能ではその形式を発信できません。登録はアプリで行ってください。

## プロトコル概要

| 項目 | 内容 |
|---|---|
| GATT サービス | `9e5d1e47-5c13-43a0-8635-82adffc0386f`（書き込み用 `…ffc1…` / 通知用 `…ffc2…`） |
| 鍵 | HomeID（4B, little endian）とパスワード（4B）を 1 バイトずつ交互に並べた 8B に、`"LOGINKEY"` または `"EVENTKEY"` を連結した AES-128 鍵 |
| ログイン | `01 20` を送る → ノードが `02 20 + AES_L(homeid, session4)` を返す → `01 16 03 + AES_L(homeid, pwd, session4)` を返す → `02 16 00` が来れば成功（`08` は他の端末が接続中） → `01 0a <own vaddr>` で自分のアドレスが割り当てられる |
| データ | 平文 `03 <dst4> <ch> <payload>` を `06 <dst4> <ch> XOR(AES_E(pkcs7(payload)), session4)` に変換 |
| 照明命令 | `<own vaddr> C0 <index> <x> <y> 00 00 00 00`、ch=0x20（全体宛は 0x2A）。x=55,y=55 で点灯、50,50 で消灯 |

`tools/native_oracle.py` は解析に使った補助ツールです。アプリの `libnative-lib.so` を Unicorn エミュレータで実行し、純 Python 版の出力と照合できます。使うには `unicorn` `pyelftools` と APK が必要ですが、ライブラリ本体は依存していません。

テスト（元アプリのネイティブライブラリで生成したテストベクタとの照合）:

```sh
.venv/bin/python -m unittest discover -s tests
```
