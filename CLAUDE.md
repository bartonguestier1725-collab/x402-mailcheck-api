# x402 Mailcheck API プロジェクトルール

## プロジェクト概要

x402プロトコル（Coinbase開発）を使ったメール検証API。
AIエージェント向けに、メールアドレスの構文・MX・使い捨て・フリーメール・ロールベース検証を
マイクロペイメント（$0.01/リクエスト、USDC on Base）で販売する。

## 技術スタック

- Python 3.10+ / FastAPI / x402 Python SDK v2
- パッケージ管理: uv
- 決済: x402 (EVM/Base USDC, Solana USDC - `SOLANA_PAY_TO` env varで有効化)
- 検証ライブラリ: email-validator, dnspython, disposable-email-domains, free-email-domains
- 公開: Cloudflare Tunnel（自宅Linux PCから配信）

## 受取ウォレット

`0x29322Ea7EcB34aA6164cb2ddeB9CE650902E4f60`（Ethereum/Base共通アドレス）

## ネットワーク設定

- 本番: `eip155:8453`（Base Mainnet）← **現在の設定**
- テストネット: `eip155:84532`（Base Sepolia）

## Facilitator

- **CDP** (`api.cdp.coinbase.com/platform/v2/x402`) — JWT認証・Bazaar Discovery対応
- `cdp-sdk` パッケージの `create_facilitator_config()` で自動認証
- `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` 環境変数で認証

## 既知の技術的注意点

- `cdp-sdk` → `web3` → `nest_asyncio` が `asyncio.run` をパッチする
- uvicorn 0.41+ は `loop_factory` 引数を使うため、パッチ版と互換性がない
- `main.py` の `__main__` ブロックで `asyncio.runners.run` に復元して回避済み

## エンドポイント

| Route | Method | 価格 | 説明 |
|-------|--------|------|------|
| `/validate` | POST | $0.01 | フル検証（構文+MX+使い捨て+フリー+ロール+タイポ） |
| `/disposable` | GET | $0.01 | 使い捨てドメイン判定 |
| `/mx` | GET | $0.01 | MXレコード検証 |
| `/health` | GET | Free | ヘルスチェック |
| `/.well-known/x402` | GET | Free | x402 ディスカバリ |

## 禁止事項

- 秘密鍵・シードフレーズをコードやファイルに書くこと
- .envをgitにコミットすること（.gitignoreで除外済み）
- x402 SDKのバージョンを勝手に下げること（v2必須）

## ファイル構成

| ファイル | 役割 |
|---------|------|
| `main.py` | FastAPI + x402ミドルウェア + RapidAPIバイパス + エンドポイント定義 |
| `mailcheck.py` | メール検証ビジネスロジック |
| `self_pay.py` | Bazaar登録用セルフペイスクリプト |
| `pyproject.toml` | 依存関係 |
| `.env` | 設定（git管理外） |
| `data/role_prefixes.txt` | ロールベースプレフィックス一覧 |
| `tests/` | ユニットテスト + エンドポイントテスト |
