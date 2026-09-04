# docs

PoC の設計・検証記録を置く。

## 構成

```
docs/
├── README.md                    # このファイル（索引）
├── TODO.md                      # 現在地と残タスク。まずここを見る
├── architecture.md              # 全体設計。層の責務と層間インターフェース
├── decisions.md                 # 設計判断ログ。採用/不採用とその理由
└── phases/
    ├── phase1-litellm/
    │   ├── design.md            # 着手前に書く。何をどう検証し、何をもって合格とするか
    │   ├── result.md            # 実行後に書く。実際のログ・結果・考察
    │   └── assets/              # ログ抜粋、スクリーンショット
    ├── phase2-pydantic/
    ├── phase3-opa/
    ├── phase4-slack/
    └── phase5-e2e/
```

## 運用ルール

1. **design.md は着手前に書き切り、結果が出た後に判定基準を書き換えない。**
   予想と違う結果になった場合は、design.md ではなく result.md の「考察」に書く。
   判定基準を後から緩めると PoC の結論が信用できなくなる。
2. **result.md には生のエビデンスを残す。** 「フェイルオーバーした」ではなく、
   そう判断できるログ行・レスポンス・タイムスタンプを `assets/` に置いて参照する。
   ただし **`assets/` に置く前に必ず秘密情報を除去する**こと。LiteLLM は `--detailed_debug` で起動しており、
   ログには master key・AWS の識別子・リクエスト本文が含まれうる。`assets/` は gitignore していないため、
   貼り付けた内容はそのままコミットされる。
3. **設計上の選択は decisions.md に追記する。** 採用した案だけでなく、
   検討して採らなかった案とその理由を残す。この PoC の成果物として一番効くのはここ。
4. フェーズ完了時に、ルート [README.md](../README.md) のロードマップ表の「状態」列を更新する。

## 読む順番

- **作業を再開するとき**: `TODO.md`
- **初見のとき**: `architecture.md` → 各 phase の `design.md` → `result.md`
- **「なぜこの構成なのか」が気になったとき**: `decisions.md`
