# Spec: I-2026-08-15-ac-gap01-claude-reader-ttl-leftover

## R1. スコープ
テスト追加のみ（agent_cost/** の実装は無変更）。対象は docs/claims/gaps.yaml で特定済みのギャップ1件。

## R2. テスト品質
実装の実挙動を正として fixture を構築し、既存テストの流儀（pytest）に従う。実装バグを発見した場合は
修正せずテストで現挙動を固定し報告する（fail-closed、判断は人間へ）。既存カバレッジと重複するテストは追加しない。

## R3. 完了条件
pytest 全 green / 追加テストが対象分岐を実際に通る（test_cache_creation_partial_ttl_breakdown_leftover_is_unknown）。
