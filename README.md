# odelic.py

ODELIC（オーデリック）の Bluetooth 照明を PC から操作するための**非公式** Python ライブラリです。CLI、HTTP API、AI アシスタント向けの MCP サーバーも付いています。

公式 Android アプリ（`jp.co.odelic.smt.remote10` v1.10.39）の通信方式を解析して作りました。対象はアプリが使っている Pairlink connected-mesh 系の機器です。クラウドは使わず、BLE でローカルに直接操作します。

- 本体の依存ライブラリは [bleak](https://github.com/hbldh/bleak) だけです。暗号処理（AES-128）は純 Python で実装しています。
- macOS で動作を確認しています。bleak が動く環境（Linux / Windows）でも動く想定です。
- メーカー非公認のツールです。利用は自己責任でお願いします。

## インストール

```sh
pip install odelic            # ライブラリと CLI
pip install "odelic[mcp]"     # MCP サーバーも使う場合（Python 3.10 以上）
```

## 初期設定

アプリのメニュー画面に `ID:12345678` のような表示があります。これは **HomeID とパスワード（末尾4文字）をつなげたもの**です。この例では HomeID が 1234、パスワードが `5678` になります。

```sh
odelic setup --id 12345678          # 自分のアプリに表示される ID
odelic lights                       # 照明の一覧（vaddr を確認）
odelic name add リビング 00000000    # 照明に名前を付ける
```

設定は `~/.config/odelic/config.json`（権限 600）に保存されます。保存場所は `ODELIC_CONFIG` で変えられます。ID は `--id` や環境変数 `ODELIC_ID` でも指定できます。

### 接続モード：自動切断か常駐か

メッシュは**同時に 1 台のコントローラとしかつながりません**。そのため HTTP API と MCP サーバーは、どちらのモードで動かすかを選べます。

| モード | 動作 | 向いている使い方 |
|---|---|---|
| 自動切断（既定） | 最初の操作で接続し、操作が N 秒なければ切断する（既定 60 秒） | スマホのアプリも併用する |
| 常駐 | 起動時に接続し、つなぎっぱなしにする。切れたら次の操作で再接続 | スマホのアプリを使わない。反応が最も速い |

```sh
odelic setup --keep-connected       # 常駐をデフォルトにする
odelic setup --idle 120             # 自動切断（120 秒）をデフォルトにする
odelic serve --keep-connected       # その場だけ切り替える（--idle N も可）
export ODELIC_IDLE_TIMEOUT=0        # 環境変数で指定（0 = 常駐）
```

指定が複数ある場合の優先順位は、オプション → `ODELIC_IDLE_TIMEOUT` → 設定ファイル → 既定値（60 秒）です。CLI の単発コマンドは、毎回接続して終わったら切断します。

## CLI

照明の指定には、名前・`all`（省略時の既定）・vaddr のどれでも使えます。

```sh
odelic on                                   # 全点灯（前回の色・明るさに戻る）
odelic off リビング                          # 1台だけ消灯
odelic light --color 0 --brightness 100     # 暖色（電球色）で最大の明るさ
odelic light リビング --color 100 --brightness 30
odelic night --level 1                      # 常夜灯（0〜2）
odelic status リビング                       # 点灯状態・色・明るさを表示
odelic group 1 on                           # グループ単位（グループ ID はアプリでの設定）
odelic scene play 1                         # シーンの呼び出し（save で保存）
odelic scan                                 # 近くのメッシュノードを探す（ログインしない）
odelic name list / odelic name rm リビング   # 名前の一覧・削除
odelic -v ...                               # 送受信パケットを表示する
```

色は `0`＝電球色（暖色）〜`100`＝昼光色（寒色）、明るさは `1`〜`100`（%）で指定します。

## MCP サーバー（AI から操作する）

Claude などの AI アシスタントから照明を操作できます。ID は設定ファイル（`odelic setup`）か環境変数 `ODELIC_ID` から読み込みます。

Claude Code の場合:

```sh
claude mcp add odelic -- uvx --from "odelic[mcp]" odelic-mcp
```

Claude Desktop など JSON で設定する場合:

```json
{
  "mcpServers": {
    "odelic": {
      "command": "uvx",
      "args": ["--from", "odelic[mcp]", "odelic-mcp"],
      "env": { "ODELIC_IDLE_TIMEOUT": "60" }
    }
  }
}
```

pip でインストール済みなら、`command` を `odelic-mcp`（またはそのフルパス）にしても動きます。

提供するツール:

| ツール | 内容 |
|---|---|
| `list_lights` / `light_status` | 照明の一覧、点灯状態・色・明るさの取得 |
| `turn_on` / `turn_off` / `set_light` / `night_light` | 点灯・消灯・色と明るさ・常夜灯 |
| `control_group` / `play_scene` / `save_scene` | グループ、シーン |
| `name_light` / `forget_light_name` | 照明の名前の登録・削除 |

macOS では、MCP サーバーを起動するアプリ（ターミナルや Claude Desktop など）に Bluetooth の許可が必要です。

## HTTP API

```sh
odelic serve                                    # http://127.0.0.1:8765
odelic serve --host 0.0.0.0 --token <秘密の文字列> --keep-connected   # LAN に公開する場合（トークン必須）
```

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/health` | 接続状態・モード |
| GET | `/lights` | 照明一覧 |
| GET | `/lights/<target>` | 状態（`all` で全台） |
| POST | `/lights/<target>/on` / `off` | 点灯・消灯 |
| POST | `/lights/<target>/light` | `{"color": 0, "brightness": 100}` |
| POST | `/lights/<target>/night` | `{"level": 0}` |
| POST | `/groups/<id>/on` / `off` / `light` | グループ |
| POST | `/scenes/<n>/play` / `save` | シーン |

パラメータは JSON ボディでもクエリ文字列でも渡せます（例: `POST /lights/all/light?color=0&brightness=100`）。トークンを設定した場合は、`Authorization: Bearer <token>` ヘッダか `?token=` を付けてください。iPhone のショートカットや Home Assistant などから呼び出せます。

```sh
curl -X POST localhost:8765/lights/リビング/off
curl localhost:8765/lights/all
```

## ライブラリとして使う

```python
import asyncio
from odelic import Controller

async def main():
    async with Controller() as ctl:            # 設定ファイル / ODELIC_ID を使う
        await ctl.set_light("all", color=0, brightness=100)
        print(await ctl.status("リビング"))
        await ctl.off("リビング")

asyncio.run(main())
```

`Controller` は接続の管理（自動接続、自動切断／常駐、再接続）と名前の解決を受け持ちます。もっと低いレベルで操作したい場合は `OdelicMesh`（1 本の BLE 接続）と `odelic.protocol`（パケットの組み立て）を直接使ってください。

## 注意

- スマホのアプリが接続中だと `node is already in use by another controller` になります。アプリを閉じるか、スマホの Bluetooth を切ってから試してください。
- macOS では初回実行時に、実行元のアプリへの Bluetooth 許可を求められます。
- **新しい照明の登録**には未対応です。アプリはスマホから独自形式の電波を発信して登録していますが、macOS の標準 Bluetooth 機能ではその形式を発信できません。登録はアプリで行ってください。

## プロトコル概要

| 項目 | 内容 |
|---|---|
| GATT サービス | `9e5d1e47-5c13-43a0-8635-82adffc0386f`（書き込み用 `…ffc1…` / 通知用 `…ffc2…`） |
| 鍵 | HomeID（4B, little endian）とパスワード（4B）を 1 バイトずつ交互に並べた 8B に、`"LOGINKEY"` または `"EVENTKEY"` を連結した AES-128 鍵 |
| ログイン | `01 20` を送る → ノードが `02 20 + AES_L(homeid, session4)` を返す → `01 16 03 + AES_L(homeid, pwd, session4)` を返す → `02 16 00` が来れば成功（`08` は他の端末が接続中） → `01 0a <own vaddr>` で自分のアドレスが割り当てられる |
| データ | 平文 `03 <dst4> <ch> <payload>` を `06 <dst4> <ch> XOR(AES_E(pkcs7(payload)), session4)` に変換 |
| 照明命令 | `<own vaddr> C0 <index> <x> <y> 00 00 00 00`、ch=0x20（全体宛は 0x2A）。x=0..20（色）、y=0..19（0 が最も明るい）、55/55 で点灯、50/50 で消灯 |
| 状態 | `<own vaddr> 70` を送ると `<src vaddr> 71 <x> <y> …` が返る |

`tools/native_oracle.py` は解析に使った補助ツールです。アプリの `libnative-lib.so` を Unicorn エミュレータで実行し、純 Python 版の出力と照合できます。使うには `unicorn` `pyelftools` と APK が必要ですが、ライブラリ本体は依存していません。

## 開発

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[mcp]"
.venv/bin/python -m unittest discover -s tests
```

GitHub でリリースを作ると、GitHub Actions が PyPI に自動で公開します（Trusted Publishing）。リリースのタグは `pyproject.toml` のバージョンと一致させてください（例: `v0.2.0`）。

## ライセンス

MIT License（[LICENSE](LICENSE)）
