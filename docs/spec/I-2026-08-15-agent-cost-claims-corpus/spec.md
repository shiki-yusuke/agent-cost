# Spec: agent-cost pricing ドメインへの Claim Corpus 導入（evidence-docs 第2リポジトリ実証）

## R1. 対象範囲
corpus は docs/claims/** のみ。対象ドメインは pricing 系（rates.json 検証規則 / cache_write_unknown の
lower_bound 意味論 / unpriced の扱い / Decimal 使用 / measure/v1 契約の cross-repo 所有分担）に限定し、
agent_cost/** と tests/** の実装は変更しない。

## R2. corpus 品質規則（evidence-docs docs/schema.md に準拠）
- テストが実際に assert している claim のみ execution_verified（selector にテスト名）
- 文書に記録された判断は recorded_decision、コード読解のみは single_source_observation
- 数値 confidence 禁止 / 未確認を確認済みに見せない / drift 探索は最低1回、結果を gaps.yaml に正直に記録

## R3. 再現性
HEAD commit を repo-commit として validate/generate がエラーなく完走し、同一入力の generate 2回が
byte-identical（corpus_digest 一致）であること。context クエリの実行例を docs/claims/README.md に記録。

## R4. 実証の意味
これは evidence-docs の「第2リポジトリ（非TS）での e2e」実証であり、9月末 DoD の垂直スライス
（意図→lane→帰属付き計測→docs projection）を sketch-web 以外で通す確認を兼ねる。
