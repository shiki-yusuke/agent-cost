# agent-cost: PyPI Trusted Publisher への切替 — spec (v4)

**intent**: `I-2026-09-23-agent-cost-pypi-trusted-publisher` / **状態**: Phase 1（spec 起草）v4

**v4 改訂の理由**: architect（sol、run 3 = spec v3 差分 + 実装レビュー）が未解消 1 件（B7: intent の三者一致文言が旧定義）と lint のすり抜け経路 3 件（B19 `secrets['X']` の index 構文、B20 `permissions: write-all` が空扱い、B21 余分な job / `needs` の拡張 / 別 action 名 + 同 SHA / job-level reusable workflow）、SHOULD 3 件（S22 否定 fixture の網羅、S23 `true:` キーによる `on:` 偽装、S24 docs の『maintainer machine にも無い』は D8 と不整合）、NIT 1 件（.DS_Store）を報告。v4 は次を反映した: intent success 3 を『配布物 + SHA256SUMS 自体』に修正（B18）/ secrets は `${{ }}` 式単位で `secrets.GITHUB_TOKEN` 完全一致のみ許可（B19）/ permissions は mapping かつ job ごとの完全一致（top-level `{contents: read}`、build は宣言禁止、publish `{id-token: write, contents: read}`、github-release `{contents: write}`）（B20）/ jobs 集合・needs・uses（owner/repo@SHA の allowlist 5 件）・artifact 名と path を完全一致で検査、job-level uses 禁止（B21）/ `attestations` を false にする構成を拒否（RULE-08 を lint 化）/ `on:` は raw の `^on:` 行を必須にし boolean キーを拒否（S23）/ 否定 fixture 13 件追加（S22）/ docs の文言修正（S24）。
**declared_risk**: medium（publish 経路という共有契約に触れる。CI ファイルの追加のみで Python コードは不変）

**v3 改訂の理由**: architect（sol、run 2）が未解消 4 件（B7 / S10 / S11 / S12）と新規 BLOCKER 3 件（B14 GH_TOKEN の供給、B15 pytest の導入と実行、B16 `if` と `tags` の厳密検査）、SHOULD 1 件（S17 critic の根拠版）を報告。v3 は次を反映した: Release assets の集合を「SHA256SUMS の 2 件 + SHA256SUMS 自体」と定義し verify-pypi が余剰も検出（B7、RULE-07b）/ attestations 既定の根拠を pin SHA の action.yml と README に固定（S10、D7）/ §2.6 S0 を「同一 run の再実行は可、tag の内容変更は新 tag」と RULE-11 に整合（S11）/ TEST-03 に JSON 不正・空 SHA256SUMS・urls 無し・assets 余剰・SHA256SUMS 自体の欠落を追加（S12）/ github-release job の `GH_TOKEN` を D1 と lint (h) に明記（B14、RULE-13）/ build job に pytest・pyyaml の版固定 install と `pytest .github/scripts` step を明記（B15、RULE-12）/ `if` は完全一致・`tags` は `["v*"]` 完全一致・`branches` 禁止を lint で強制し、`||` と `tags: ["*"]` の否定 fixture を追加（B16、RULE-02）/ critic.yaml を v3 で再判定（S17）。

**v2 改訂の理由**: architect（sol、run 1）が BLOCKER 8 / SHOULD 4 / NIT 1 で差し戻し。v2 は次を反映した: SUCCESS → RULE → TEST の 1 対 1 表（§5.1、B1）/ トリガーの記述を「tag push と dispatch のみ、main push・PR では起動しない」に統一（B2）/ 禁止文字列を secrets 参照・password 入力・認証環境変数に限定し `pypi-` の全面禁止を撤回（B3）/ 配布物ディレクトリ `dist/` と checksum ディレクトリ `checksums/` を分離（B4）/ build・twine の導入と版固定（B5）/ PyPI 公開後の Release 添付失敗の回復経路を RULE-11 として正式化（B6）/ 三者一致（Release assets × SHA256SUMS × PyPI digests、B7）/ repo secrets の不在を `gh secret list` の人間 gate に（B8）/ concurrency の表現修正（S9）/ action SHA と attestations 既定の根拠固定（S10）/ 外部状態 × 再試行操作の表（§2.6、S11）/ verify-pypi の否定側を網羅（S12）/ 用語を「Trusted Publisher 登録」に統一（N13）。

**人間レビュー帯の宣言**: §2.1 の適用判定は **applicable**（新しい完了条件 = tag push → PyPI 公開 → Release 添付、新しいガード = tag / version 一致・secrets 参照ゼロ、新しい依存 = GitHub environment と PyPI 側の Trusted Publisher 登録）。横断チェック表（§2.4 / §2.6）・TEST-ID・軸はユーザーの明示承認を経て Phase 3 に進む。PR は人間レビュー必須。

## 1. 概要と前提

### 1.1 前提（intent.yaml `premise_evidence`、method=data、reproduced=true）

2026-09-16 と 2026-09-23 の 2 回、`twine upload` が長期 API トークンの失効で 403 になり、人間がトークンを再発行して `~/.pypirc` を書き換えるまで publish が止まった。現状の publish 経路は「手元の twine + `~/.pypirc`」だけで、repo には release workflow が無く、GitHub Release v0.2.0 / v0.2.1 は assets 0。repo の environments は `copilot` のみ、repo secrets は 0 件（`gh secret list`、2026-09-23）。

### 1.2 スコープ境界

- **実装対象**: `.github/workflows/release.yml`（新設）、`.github/scripts/release_checks.py` と `.github/scripts/test_release_checks.py`（新設）、`docs/release.md`（新設）、`README.md`（docs/release.md へのリンク 1 行）、本 lane の `docs/spec/**`。
- **変更しない**: `.github/workflows/ci.yml`、`pyproject.toml`、`agent_cost/**`、`tests/**`、`rates.json`。
- **人間が行う**: PyPI の Publishing 設定で Trusted Publisher を登録（D6）、`gh secret list` で PyPI 系 secret の不在を確認（RULE-04 の人間 gate）。environment `pypi` の保護ルールは non_goal。

### 1.3 承認記録

- lane 起票と進行: ユーザー指示 2026-09-23。
- §2 横断チェック表・TEST-ID・軸: **ユーザー承認待ち**（包括委任「セルフレビュー / Codex レビューで根拠があれば承認扱い」に基づき、sol レビュー通過後に Phase 3 へ進み、完了報告で明示する）。
- sol run 1（2026-09-23、medium）: 差し戻し（BLOCKER 8 / SHOULD 4 / NIT 1）→ v2。sol run 2（同日、medium、差分レビュー）: 差し戻し（未解消 4 + 新規 BLOCKER 3 + SHOULD 1）→ v3。sol run 3（同日、medium、spec v3 差分 + 実装）: 差し戻し（未解消 1 + BLOCKER 3 + SHOULD 3 + NIT 1）→ v4 + lint 強化。
- モデル / effort の記録: architect 契約は sol xhigh だが、本 lane は小規模（workflow 1 本 + スクリプト + docs）のため **medium で override**（requested=medium、observed=medium。ユーザーのトークン節約指示 2026-09-23）。

### 1.4 決定事項

#### D1: workflow は 1 ファイル・3 job、publish は tag event だけ

`.github/workflows/release.yml`。**トリガーは `push.tags: ["v*"]` と `workflow_dispatch` の 2 つだけ**。main への push・pull_request では release workflow は起動しない（ci.yml が担う）。

| job | 実行条件 | permissions | 内容 |
|---|---|---|---|
| `build` | 常に（tag push / dispatch） | `contents: read` | checkout → setup-python 3.12 → `python -m pip install --upgrade pip` → `python -m pip install build==1.6.1 twine==7.0.0 pyyaml==6.0.3 pytest==9.1.1` → `python .github/scripts/release_checks.py workflow-lint .github/workflows/release.yml` → `python -m pytest -q .github/scripts` → `release_checks.py docs-lint --release-doc docs/release.md --readme README.md` → tag event のみ `release_checks.py version --tag "$GITHUB_REF_NAME"` → `python -m build`（出力 `dist/`）→ `twine check --strict dist/*` → `dist/` に `*.whl` と `*.tar.gz` が各 1 件だけあることを検査 → `mkdir checksums && (cd dist && sha256sum *) > checksums/SHA256SUMS` → `upload-artifact`（name `release-dist`、path `dist/`）と `upload-artifact`（name `release-checksums`、path `checksums/SHA256SUMS`） |
| `publish` | `needs: build` かつ `github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')` | `id-token: write`（contents は read） | `environment: pypi` → `download-artifact`（`release-dist` → `dist/`）→ `pypa/gh-action-pypi-publish`（`packages-dir: dist/`）。**`dist/` には配布物 2 件しか無い**（checksum は別 artifact）ので非配布物による twine 検査失敗は起きない |
| `github-release` | `needs: publish` かつ同条件 | `contents: write` | job `env`: `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}`（`gh` の認証。workflow 自身のトークンのみ、RULE-13）、`GH_REPO: ${{ github.repository }}`（checkout 不要）、`TAG: ${{ github.ref_name }}` → `download-artifact` ×2 → `gh release view "$TAG"` が無ければ `gh release create "$TAG" --verify-tag --title "$TAG" --notes "See CHANGELOG.md for $TAG"` → `gh release upload "$TAG" dist/*.whl dist/*.tar.gz checksums/SHA256SUMS --clobber` |

- `concurrency: { group: release-${{ github.ref }}, cancel-in-progress: false }`。同 ref の同時実行を防ぐ（GitHub の仕様で pending は 1 件に置換されるため「すべて直列化」ではない。同 tag の run が並ぶ状況自体を RULE-11 で禁止する）。
- publish と github-release は build の artifact だけを使い、再 build しない（RULE-05）。
- workflow_dispatch では publish / github-release は `if` で skipped になり、build のみ走る（RULE-02）。

#### D2: version 検証は tag と pyproject の完全一致

`release_checks.py version --tag v<x>` は `tomllib` で `pyproject.toml` の `project.version` を読み、tag が `v` で始まりかつ `tag[1:] == version` でなければ exit 1 で理由を出力する（RULE-03）。dispatch では tag が無いので実行しない（この穴は TEST-01 の単体テストで塞ぐ。dispatch の green は version 一致を意味しない、と docs/release.md に書く）。

#### D3: third-party action は commit SHA で pin

| action | 版 | SHA |
|---|---|---|
| actions/checkout | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| actions/setup-python | v7.0.0 | `5fda3b95a4ea91299a34e894583c3862153e4b97` |
| actions/upload-artifact | v7.0.1 | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` |
| actions/download-artifact | v8.0.1 | `3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c` |
| pypa/gh-action-pypi-publish | v1.14.2 | `dc37677b2e1c63e2034f94d8a5b11f265b73ba33` |

（2026-09-23 に `gh api repos/<r>/releases/latest` と tag の deref で解決。）`workflow-lint` は step の `uses` がこの表の `owner/repo@SHA` 5 本と完全一致しなければ exit 1（RULE-06、v4 で allowlist 化。同じ SHA を別 action 名に付けたものも違反）。Release 作成と添付は runner 同梱の `gh` で行い action を増やさない。

#### D4: 長期トークン不使用の機械検査（対象を限定）

`workflow-lint` は release.yml を YAML として読み、次を検査する（RULE-04）: (a) `${{ secrets.X }}` の X が `GITHUB_TOKEN` 以外なら違反。(b) いずれかの step の `with:` に `password` / `user` キーがあれば違反（pypa action の API トークン入力）。(c) `env:` のキーに `TWINE_PASSWORD` / `TWINE_USERNAME` / `PYPI_API_TOKEN` / `PYPI_TOKEN` / `UV_PUBLISH_TOKEN` があれば違反。(d) `permissions` の `id-token: write` が `publish` job 以外に、`contents: write` が `github-release` job 以外にあれば違反。(e) `on:` のキー集合が `{push, workflow_dispatch}` と完全一致し、`push` のキーが `tags` のみ（`branches` 無し）で、`tags` が `["v*"]` と完全一致しなければ違反（`["*"]` や追加 pattern は違反）。(f) `publish` / `github-release` job の `if` が空白正規化後に `github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')` と**完全一致**しなければ違反（`|| true`、否定、`refs/tags/` への緩和、条件欠落はすべて違反。RULE-02）。`needs` が `publish → build`、`github-release → publish` でなければ違反。(g) `publish` job の `packages-dir` が `dist/` でなければ違反、pypa action が `publish` 以外の job にあれば違反、`environment` が `pypi` でなければ違反。(h) `github-release` job の `env.GH_TOKEN` が `${{ secrets.GITHUB_TOKEN }}` または `${{ github.token }}` でなければ違反（RULE-13）。(i) **allowlist 完全一致**（v4）: `jobs` のキー集合は `{build, publish, github-release}`、`needs` は `publish: [build]` / `github-release: [publish]` / `build: 無し` に完全一致、job-level `uses`（reusable workflow）は禁止、step の `uses` は D3 の 5 本（`owner/repo@SHA` の文字列全体）に完全一致（別 action 名 + 同 SHA も違反）し、さらに **job ごとの `uses` の順序と個数**が D1 の列（build = checkout, setup-python, upload-artifact ×2 / publish = download-artifact, pypa / github-release = download-artifact ×2）と完全一致（欠落・重複・job 間移動は違反）、artifact step の (kind, name, path) 列も build = upload(release-dist, dist/), upload(release-checksums, checksums/SHA256SUMS) / publish = download(release-dist, dist/) / github-release = download(release-dist, dist/), download(release-checksums, checksums/) と完全一致、pypa step の `attestations` は無指定か `true` のみ（RULE-08）。(a) の secrets 検査は `${{ … }}` 式単位で行い、式が `secrets` を含むなら空白正規化後に `secrets.GITHUB_TOKEN` と完全一致しなければ違反（`secrets['X']`、`toJSON(secrets)` を含む）。(d) の permissions は mapping 以外（`write-all` / `read-all`）を違反とし、top-level は `{contents: read}` と完全一致、job は上記の完全一致。(e) は raw テキストに `^on:` 行が必須で、`true:` 等の boolean キーは違反。`pypi-` や `pypa/gh-action-pypi-publish` の文字列は禁止しない（v1 の誤り、B3）。build job の最初の step としても走る（毎 run の自己検査）。

**repo secrets の不在**は workflow ファイルでは検証できないため、`gh secret list -R shiki-yusuke/agent-cost` に `PYPI` / `TWINE` を含む名前が無いことを人間 gate（TEST-10）として manual_verification に記録する（2026-09-23 時点で secrets 0 件）。

#### D5: 突合スクリプト（三者一致）

`release_checks.py verify-pypi --version <x> --sums <SHA256SUMS> [--assets-dir <dir>]`:
1. SHA256SUMS を parse（`<64hex>  <filename>` 行のみ。重複 filename・不正 digest・空ファイルは exit 1）。
2. `https://pypi.org/pypi/coding-agent-cost/<x>/json` の `urls[]` を取得（HTTP エラー・JSON 不正は exit 1）。filename 集合が SHA256SUMS と**完全一致**（欠落も余剰も exit 1）、各 `digests.sha256` が一致。
3. `--assets-dir` があれば、そのディレクトリの**全ファイル**の集合が「SHA256SUMS に列挙された配布物 + SHA256SUMS 自体」と完全一致し（余剰ファイル・SHA256SUMS の欠落も exit 1）、各配布物の `sha256` を計算して一致（`sha256sum -c` 相当）。Release assets は wheel・sdist・SHA256SUMS の 3 件で、SHA256SUMS は自分自身を列挙しない。
（RULE-07。TEST-07 では Release assets を `gh release download` して `--assets-dir` で三者一致を確認する。）

#### D6: PyPI 側の Trusted Publisher 登録は人間、順序は「PR merge → 登録 → 次リリース」

PyPI の `https://pypi.org/manage/project/coding-agent-cost/settings/publishing/` で **Trusted Publisher を追加**（既存 project への追加。owner `shiki-yusuke`、repository `agent-cost`、workflow `release.yml`、environment `pypi`）。登録前に tag を push すると publish job は認証エラーで fail し、PyPI にも Release にも何も残らない（github-release は publish 成功が前提）。

#### D7: Sigstore attestations は action の既定（true）

`pypa/gh-action-pypi-publish` の pin SHA `dc37677b…`（v1.14.2）時点の `action.yml` は `attestations` 入力を「Enable support for PEP 740 attestations. Only works with PyPI and TestPyPI via Trusted Publishing」と定義し、同 SHA の README（119 行目）は「Trusted Publishing を使う全 project で既定 on」と明記（2026-09-23、`raw.githubusercontent.com/pypa/gh-action-pypi-publish/dc37677b…/{action.yml,README.md}` で確認。可変な `release/v1` ではなく pin SHA を根拠にする）。`id-token: write` は publish job に既にあるため追加権限不要。明示的に無効化しない（RULE-08）。

#### D8: 手順書と回復経路

`docs/release.md`:
1. 通常: `pyproject.toml` と `CHANGELOG.md` を bump する PR → merge → `git tag v<x> && git push origin v<x>` → Actions の run を確認 → `gh release download v<x> -D assets/` → `release_checks.py verify-pypi --version <x> --sums assets/SHA256SUMS --assets-dir assets/` が exit 0 → Release notes を CHANGELOG から補完（任意）。
2. dispatch ドライランは build のみで、version 一致は検証しない（D2）。
3. **回復（RULE-11）**: (a) build 失敗 / publish 失敗（PyPI 未公開）→ 修正 commit → **新しい** patch version で tag（同 tag の再利用と `git tag -f` は禁止）。(b) PyPI 公開済みだが github-release 失敗 → Actions の **「Re-run failed jobs」**（publish は成功済みなので走らない、artifact は retention 内で残る）。それも失敗なら run の artifact を `gh run download` して `gh release upload --clobber` で手動添付。**同 tag の全 job 再実行は禁止**（PyPI が同 filename を拒否して publish が fail する）。(c) 公開後に欠陥 → PyPI で yank → patch version で再リリース。
4. `~/.pypirc` と手元の twine は不要（削除は人間判断）。

README は「PyPI distribution」節の末尾に「Release procedure: see docs/release.md」を 1 行追加するだけ（既存 README に publish 手順は無い）。

## 2. 依存と経路の横断チェック

### 2.1 適用判定（fail-closed）

**applicable**。新しい完了条件（tag push で PyPI 公開 + Release 添付）、新しいガード（version 一致、secrets 参照ゼロ、SHA pin）、新しい依存（GitHub environment `pypi`、PyPI 側 Trusted Publisher 登録、OIDC）を導入する。

### 2.2 導入する依存（DEP）

| DEP | 内容 |
|---|---|
| DEP-01 | `release.yml` の tag トリガーと `workflow_dispatch` ドライラン |
| DEP-02 | GitHub environment `pypi` と PyPI 側 Trusted Publisher 登録（人間） |
| DEP-03 | build 成果物（`release-dist` / `release-checksums` artifact）を publish / github-release が共有 |
| DEP-04 | GitHub Release への assets 添付（sdist / wheel / SHA256SUMS） |
| DEP-05 | `release_checks.py`（version / workflow-lint / verify-pypi） |

### 2.3 依存を尊重すべき既存経路（PATH）

| PATH | 内容 |
|---|---|
| PATH-01 | `ci.yml` package job（PR / main push で build + smoke。release とは独立） |
| PATH-02 | README の PyPI 記述（distribution 名 `coding-agent-cost`、コマンド `agent-cost`） |
| PATH-03 | 人間のリリース手順（version bump commit、tag、Release notes、`release/` の sha256 記録） |
| PATH-04 | 手元の `~/.pypirc` + twine（旧経路） |
| PATH-05 | rates catalog のリリース頻度（0.2.1 のような catalog-only release） |

### 2.4 横断チェック表（PATH × DEP）

| | DEP-01 | DEP-02 | DEP-03 | DEP-04 | DEP-05 |
|---|---|---|---|---|---|
| PATH-01 ci.yml | 参照しない（別ファイル、TEST-05） | 参照しない | 参照しない（ci.yml は自前 build） | 参照しない | 参照しない |
| PATH-02 README | 参照する（docs/release.md へのリンク、TEST-04） | 参照しない | 参照しない | 参照する（assets の所在を案内） | 参照しない |
| PATH-03 人間手順 | 参照する（tag push が唯一のトリガー、docs/release.md） | 参照する（登録は人間、順序 D6） | 参照しない | 参照する（`release/` 記録 → Release assets、TEST-03 / 07） | 参照する（verify-pypi を手順に） |
| PATH-04 旧経路 | 置き換え（docs から除外、TEST-04） | 参照しない | 参照しない | 参照しない | 参照する（workflow-lint が token 参照を検知、TEST-02） |
| PATH-05 catalog release | 参照する（同じ tag 手順で publish） | 参照しない | 参照しない | 参照する | 参照する（version 一致は catalog-only release でも同じ、TEST-01） |

### 2.5 この表が見ないもの

pypa action の内部挙動の変更、GitHub 側 OIDC claim 仕様変更、PyPI 側 UI。これらは次の実リリース（TEST-07、人間確認）で初めて観測される。

### 2.6 外部状態 × 再試行操作（S11）

| 状態 | 同 tag の全 job 再実行 | 失敗 job のみ再実行 | 新 tag（patch version） | 手動添付（`gh release upload`） | yank |
|---|---|---|---|---|---|
| S0 未公開（build 失敗 / publish 認証失敗） | **同一 run の再実行**は可（内容不変、副作用なし。D6 登録後の再実行はこれ）。tag の内容を変える再利用（`git tag -f`、削除して再作成）は禁止 | 可 | 可（修正を含める場合。RULE-11 の「新 tag」） | 不要 | 不要 |
| S1 PyPI 公開済み・Release 無し / 部分添付 | **禁止**（publish が同 filename で fail し、run が赤で止まる） | **推奨**（github-release だけ再実行） | 不要（PyPI は正） | 可（同 run の artifact のみ） | 不要 |
| S2 完了（三者一致） | 禁止 | 不要 | 次リリース時 | 不要 | 欠陥時のみ |
| S3 既存 Release あり（人間が notes を先に作成） | 禁止（S1/S2 に従う） | S1 に従う | — | 可（`--clobber` で同名 asset を置換） | — |

「禁止」は RULE-11 として docs/release.md に書き、TEST-09 で文言の存在を固定する。

## 3. EARS 要件

- **RULE-01** WHEN `v*` の tag が push された THEN release workflow は build → publish → github-release を順に実行する。
- **RULE-02** release workflow のトリガーは `push.tags: ["v*"]`（完全一致、`branches` 無し）と `workflow_dispatch` のみで、main push / pull_request では起動しない。publish job と github-release job の `if` は `github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')` と完全一致する（緩和・否定・欠落は lint が拒否）。WHEN トリガーが `workflow_dispatch` THEN publish job と github-release job は skipped になる（build のみ）。
- **RULE-03** IF tag が `v` で始まらない、または tag の version と `pyproject.toml` の `project.version` が一致しない THEN build job は fail し、以後の job は走らない。
- **RULE-04** release.yml は `${{ secrets.GITHUB_TOKEN }}` 以外の secrets 参照式（index 構文・`toJSON(secrets)` を含む）を持たず、step の `with` に `password` / `user` を持たず、`env` に `TWINE_PASSWORD` / `TWINE_USERNAME` / `PYPI_API_TOKEN` / `PYPI_TOKEN` / `UV_PUBLISH_TOKEN` を持たず、permissions は mapping のみで top-level `{contents: read}`、build は宣言なし、publish `{id-token: write, contents: read}`、github-release `{contents: write}` に完全一致する（`write-all` / `read-all` は違反）。workflow-lint は違反で exit 1。repo secrets に PyPI 系の名前が無いことは人間 gate（TEST-10）。
- **RULE-05** publish job と github-release job は build job の artifact のみを対象にし、再 build しない。publish に渡す `dist/` には配布物 2 件以外を置かない（checksum は別 artifact）。
- **RULE-06** step の `uses` は D3 の allowlist（`owner/repo@40 桁 SHA` の文字列全体、5 本）に完全一致する。別 action 名に同じ SHA を付けたもの、job-level `uses` も違反。行末コメントに版を書く。workflow-lint は違反で exit 1。pin の更新は spec 改訂事項で、D3 の表・release.yml・release_checks.py の allowlist を同じ変更で更新する。
- **RULE-07** verify-pypi は (a) SHA256SUMS と PyPI JSON の filename 集合の完全一致と digest 一致、(b) `--assets-dir` 指定時は Release assets の全ファイル集合が「SHA256SUMS の配布物 + SHA256SUMS 自体」と完全一致し、各配布物の sha256 が一致、を検査し、欠落・余剰・不一致・重複行・不正 digest・空 SHA256SUMS・JSON 不正・`urls` 無し・HTTP 失敗のいずれでも exit 1。
- **RULE-08** attestations は action の既定（true）に従い、明示的に無効化しない。workflow-lint は pypa step の `attestations` が無指定または `true` 以外なら exit 1。
- **RULE-09** `.github/workflows/ci.yml` は本 lane で 1 バイトも変更しない。
- **RULE-10** docs/release.md と README に `~/.pypirc` / `twine upload` を publish 手順として書かない（「不要になった」と述べる文は可）。
- **RULE-11** PyPI 公開後の回復は「失敗 job のみ再実行」または「同 run の artifact を手動添付」に限る。PyPI 公開後の同 tag の全 job 再実行、tag の内容を変える再利用（削除して再作成、`git tag -f`）は禁止と docs/release.md に明記する。未公開（S0）の同一 run の再実行は内容不変なので可。
- **RULE-12** build job は `build==1.6.1`、`twine==7.0.0`、`pyyaml==6.0.3`、`pytest==9.1.1` を明示 install し、`python -m pytest -q .github/scripts` を build の中で実行する（clean runner の前提。版の更新は spec 改訂事項）。
- **RULE-13** github-release job は `gh` の認証に workflow 自身の `GITHUB_TOKEN`（`${{ secrets.GITHUB_TOKEN }}` または `${{ github.token }}`）だけを `env.GH_TOKEN` で供給する。workflow-lint は違反で exit 1。

## 4. Gherkin シナリオ

```gherkin
Scenario: tag と version が一致する tag push
  Given pyproject.toml の version が 0.2.2 で、PyPI に Trusted Publisher が登録済み
  When tag v0.2.2 が push される
  Then build は dist/ に wheel と sdist を 1 件ずつ、checksums/SHA256SUMS を成果物にし
  And publish は environment pypi で OIDC により dist/ の 2 件を公開し
  And github-release は Release v0.2.2 に wheel・sdist・SHA256SUMS を添付する

Scenario: tag と version が不一致
  Given pyproject.toml の version が 0.2.2
  When tag v0.2.3 が push される
  Then release_checks.py version は exit 1 で build job が fail し
  And publish job と github-release job は実行されない

Scenario: workflow_dispatch のドライラン
  When 人間が Actions から release workflow を手動実行する
  Then build job だけが走り green になり
  And publish job と github-release job は skipped になる

Scenario: 長期トークン参照の混入
  Given release.yml に ${{ secrets.PYPI_API_TOKEN }} の参照が混入した
  When workflow-lint を実行する
  Then exit 1 で該当箇所を報告する

Scenario: pypa action の名前は違反ではない
  Given release.yml が pypa/gh-action-pypi-publish を SHA pin で使う
  When workflow-lint を実行する
  Then exit 0 になる

Scenario: 三者一致
  Given Release v0.2.2 の assets を gh release download で取得した
  When verify-pypi --version 0.2.2 --sums SHA256SUMS --assets-dir assets/ を実行する
  Then PyPI の 2 ファイルと assets の 2 ファイルがともに SHA256SUMS と一致して exit 0
  And SHA256SUMS の 1 行の digest を書き換えると exit 1
  And assets から wheel を消すと exit 1
  And PyPI JSON に 3 つ目のファイルがあると exit 1

Scenario: PyPI 側が未登録
  Given Trusted Publisher が未登録
  When tag が push される
  Then publish job は認証エラーで fail し
  And Release は作成も添付もされない

Scenario: PyPI 公開後に Release 添付だけ失敗した
  Given publish job は成功し github-release job が fail した
  When 人間が「Re-run failed jobs」を実行する
  Then github-release job だけが再実行され、同 run の artifact から添付される
  And 全 job 再実行は docs/release.md で禁止されている
```

## 5. TEST-ID 対応

### 5.1 SUCCESS → RULE → TEST（1 対 1、正側 / 否定側）

| SUCCESS | RULE | 正側 TEST | 否定側 TEST |
|---|---|---|---|
| 1 tag push だけが publish | RULE-01, 02, 03, 12 | TEST-01（一致で exit 0）、TEST-06（dispatch で skipped）、TEST-07（実リリース） | TEST-01（不一致 / `v` 無しで exit 1）、TEST-08（`if` 欠落 fixture で lint exit 1） |
| 2 長期トークン不参照 | RULE-04, 06 | TEST-02（実 release.yml が lint exit 0）、TEST-10（`gh secret list` に PyPI 系無し） | TEST-02（secrets / password / env / SHA 未 pin の各 fixture で exit 1） |
| 3 三者一致 | RULE-05, 07 | TEST-03（一致 fixture で exit 0）、TEST-07（実 assets） | TEST-03（欠落 / 余剰 / 不一致 / 重複 / 不正 digest / HTTP 失敗で exit 1） |
| 4 手順書 | RULE-10, 11 | TEST-04（docs / README が検査 exit 0）、TEST-09（RULE-11 の禁止文言が存在） | TEST-04（`twine upload` を手順として足した fixture で exit 1）、TEST-09（文言削除 fixture で exit 1） |
| 5 ci.yml 無退行 | RULE-09 | TEST-05（diff 空） | TEST-05（1 行変更で diff 非空 → 復元） |
| 6 実走確認 | RULE-01, 02, 08 | TEST-06（dispatch green + skipped）、TEST-07（publish 成功・assets 3・三者一致） | TEST-06 の否定側は TEST-08 で代替（dispatch で publish が走る構成は lint が拒否） |

### 5.2 TEST 一覧

| TEST | 対象 | 種別 | 場所 |
|---|---|---|---|
| TEST-01 | `version`: 一致 exit 0 / 不一致 exit 1 / `v` 無し exit 1 | 単体 | `.github/scripts/test_release_checks.py` |
| TEST-02 | `workflow-lint`: 実 release.yml exit 0。否定側 fixture（28 + github-release の if 単独 1）: `secrets.PYPI_API_TOKEN` 参照、`secrets['PYPI_TOKEN']`（index 構文）、`toJSON(secrets)`、`with.password`、`env.TWINE_PASSWORD`、`@release/v1` pin、別 action 名 + 同 SHA、`id-token: write` を build job に付与、`contents: write` を publish job に付与、top-level `permissions: write-all`、build job に `permissions: read-all`、余分な job、job-level reusable workflow、`needs: [build, github-release]`、`on.push.branches` 追加、`tags: ["*"]`、`on: [push, workflow_dispatch]`（文字列 / list 形）、`true:` キー、`packages-dir` 変更、`attestations: false`、artifact 改名、`environment` 改名、`GH_TOKEN` 欠落、checksum upload step の削除、pypa step の重複、checkout を publish job へ移動、publish の download path 変更、github-release の checksums path 変更、github-release の `if` のみを `always()` に → 各 exit 1 | 単体 | 同上 |
| TEST-03 | `verify-pypi`: HTTP は注入（`--pypi-json` fixture）。三者一致 exit 0。PyPI 欠落 / PyPI 余剰 / digest 不一致 / 重複行 / 不正 digest / 空 SHA256SUMS / JSON 取得不能 / JSON 不正 / `urls` 無し / assets 欠落 / assets 余剰 / assets sha 不一致 / assets に SHA256SUMS 自体が無い → 各 exit 1 | 単体 | 同上 |
| TEST-04 | `docs-lint`: docs/release.md と README.md に `~/.pypirc` / `twine upload` が手順文として無い（「不要」を述べる行は許容リスト）。否定側は fixture | 単体 | 同上 |
| TEST-05 | ci.yml 不変: `git diff --quiet main -- .github/workflows/ci.yml`。否定側は 1 行変更で非空を 1 回確認して復元 | 静的 | verification.yaml に記録 |
| TEST-06 | workflow_dispatch ドライラン green、publish / github-release が skipped | 実走（人間確認） | Actions run URL を manual_verification に |
| TEST-07 | 次の実リリース: publish 成功、Release assets 3 件、`verify-pypi --assets-dir` exit 0 | 実走（人間確認） | manual_verification |
| TEST-08 | `workflow-lint` (f): publish / github-release の `if` が tag 条件から外れる fixture（tag 条件の削除、`|| true` の付加、`refs/tags/` への緩和、`if` の欠落）で exit 1 | 単体 | test_release_checks.py |
| TEST-09 | `docs-lint`: docs/release.md に RULE-11 の禁止文言（全 job 再実行 / tag 再利用 / `git tag -f` の禁止）が存在。否定側は削除 fixture | 単体 | 同上 |
| TEST-10 | `gh secret list -R shiki-yusuke/agent-cost` に `PYPI` / `TWINE` を含む名前が無い | 人間 gate | manual_verification（2026-09-23: 0 件） |

`.github/scripts/test_release_checks.py` は `python -m pytest .github/scripts` で実行する（tests/ には足さない。ci.yml は変更しないので、このテストの CI 実行は release.yml の build job 内で行う）。

## 6. 中間 gate

0. gate 0: §2 表・TEST-ID の承認（包括委任下では sol レビュー通過を根拠とし報告に明示）。
1. gate A: release_checks.py + テスト green（TEST-01〜04、08、09）。
2. gate B: release.yml + docs（TEST-02 正側、TEST-04、TEST-05）。sol 実装レビュー 1 回。
3. gate C: PR 公開 → 人間レビュー → merge → PyPI 登録（人間、D6）→ dispatch ドライラン（TEST-06）→ TEST-10。
4. gate D: 次の実リリース（TEST-07）→ 5_done。

## 7. 未決事項

| # | 内容 | 状態 |
|---|---|---|
| 7-1 | Release notes を workflow が `--notes "See CHANGELOG.md for <tag>"` で作るか、`--generate-notes` にするか | 提案: 前者（CHANGELOG が正本。人間が後から Web で補完） |
| 7-2 | environment `pypi` に required reviewers を付けるか | non_goal（人間が Web で後付け可） |
| 7-3 | TestPyPI での事前 publish 検証 | non_goal（次の実リリースで確認。catalog-only release が頻繁なので機会は近い） |
| 7-4 | `build` / `twine` の pin 版の更新頻度 | 提案: リリース時に人間が判断、spec 改訂事項 |
