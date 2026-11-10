# 設定とデータ

`targets.json` と `settings.json` の全部の項目、ターミナルごとの違い、データのファイルの一覧。

## 設定

設定は `~/.config/session-board/` (`SESSION_BOARD_CONFIG_DIR`) に置く。リポジトリには例 (`config/targets.example.json`、`config/settings.example.json`) だけがある。JSON にはコメントを書けないので、`_` か `$` で始まる項目 (`"_comment"` など) を注記として使える (読み飛ばす)。知らない項目は警告を出して無視する。

### `targets.json` (必須)

追跡するリポジトリと、ボードの列 (作業のまとまり。以下 project) と、(リポジトリ, パス) から列と種別を決めるルール。

```json
{
  "github_owner": "example-org",
  "repos": {
    "my-app": {"owner": "example-org", "path": "~/src/my-app"},
    "my-infra": {"owner": "example-org", "path": "~/src/my-infra", "default_kind": "infra-tf"},
    "tools": {"path": "~/src/tools"}
  },
  "projects": [
    {
      "name": "my-app",
      "keywords": ["my-app"],
      "cloud": "my-gcp-project",
      "rules": [
        {"repo": "my-app", "kind": "app", "paths": ["services/api", "services/web"]},
        {"repo": "my-app", "kind": "infra-k8s", "paths": ["deploy/k8s/my-app"]},
        {"repo": "my-infra", "kind": "infra-tf", "paths": ["envs/*/my-app", "modules/**my-app*"]}
      ]
    },
    {"name": "tools", "color": "#2f9e44", "layout": "normal",
     "rules": [{"repo": "tools", "kind": "app", "paths": [""]}]}
  ],
  "other_label": "その他",
  "path_aliases": {"~/old/src": "~/src"}
}
```

| 項目 | 必須 | 説明 |
|---|---|---|
| `github_owner` | - | `repos` の各リポジトリで `owner` を省いたときの GitHub の owner (組織名かユーザー名) |
| `repos` | 必須 | 追跡するリポジトリ。キーは GitHub のリポジトリ名 (英数字・`-` `_` `.`) |
| `repos.<名前>.path` | 必須 | ローカルのクローンの場所 (`~` 可)。worktree はこの下の `git worktree list` から見つける。文字列だけを書くと `path` とみなす |
| `repos.<名前>.owner` | `github_owner` が無ければ必須 | GitHub の owner。`gh pr list --repo <owner>/<名前>` に使い、トランスクリプト中の PR の URL もこの owner のものだけを拾う |
| `repos.<名前>.default_kind` | - | `app` / `infra-tf` / `infra-k8s`。PR の変更ファイルがどのルールにも当たらないときに、PR の表示 (アプリ PR / インフラ PR) に使う種別 |
| `projects` | - | ボードの列。書いた順に左から並ぶ。空なら全部が `other_label` の列に入る |
| `projects[].name` | 必須 | 列の名前 (列の目印。チケットの「表示する列」もこの名前で覚える・作業報告の見出し)。重複と `other_label` と同じ名前は不可 |
| `projects[].label` | - | 画面に出す列の表示名 (60 字まで)。無ければ `name` を出す。ボードで列の見出しを押すと、その場で変えられる (Enter で保存、空にすると `name` に戻す)。画面から変えたときは、書く前に元のファイルを `targets.json.bak-YYYYmmdd-HHMMSS` に残す |
| `projects[].keywords` | - | パスで判定できないときの補助。ブランチ名・PR タイトル・セッションのタイトル・cmux のワークスペース名に含まれていればこの列にする (大文字小文字は区別しない)。既定は `[name]` |
| `projects[].rules` | - | `{"repo", "kind", "paths"}` の配列。`repo` は `repos` のキー、`kind` は `app` / `infra-tf` / `infra-k8s` |
| `projects[].rules[].paths` | 必須 | worktree のルートからの相対パスに対する接頭辞の glob。`*` は 1 階層、`**` は複数階層にまたがる、`?` は 1 文字。`""` はリポジトリ全体 |
| `projects[].color` | - | `#rgb` / `#rrggbb`。列名の前に色の点を出す |
| `projects[].layout` | - | `auto` (既定。進行中が `column_wide_min_items` 件以上で 2 倍幅の 2 段組み) / `wide` (常に 2 倍幅) / `normal` (常に 1 倍幅) |
| `projects[].cloud` | - | クラウドのプロジェクト名などの覚え書き。列名のツールチップに出す |
| `other_label` | - | どの列にも当たらない作業の列名 (常に一番右)。既定は「その他」 |
| `path_aliases` | - | `{"古い場所": "新しい場所"}`。作業ディレクトリを移したとき、Claude Code の記録に残る古いパスを新しいパスとして読む ([作業ディレクトリを移したとき](#作業ディレクトリを移したとき)) |

パスの書き方の例:

- 環境ごとのディレクトリ (`alpha/`、`beta/` など) の下に `stacks/<名前>` があるなら `*/stacks/<名前>`
- `envs/<env>/<名前>/` と `envs/<env>/vars.<名前>.tf` の両方を当てたいなら `envs/*/**<名前>*`
- アプリのコードが `services/<名前>/` にあるなら `services/<名前>`

作業の列は、セッションの cwd・編集ファイル・PR の変更ファイル・ブランチの変更ファイルをルールで分類し、最も多く当たったもの (同数なら `projects` の順) にする。どれにも当たらなければ `keywords`、それでも決まらなければ `other_label` の列。

### 作業ディレクトリを移したとき

クローンを置くディレクトリを移した (例: `~/old/src` → `~/src`) とき、`repos` の `path` を新しい場所に直すだけでは足りない。Claude Code の記録 (`~/.claude/projects/*/<sessionId>.jsonl`) には記録した時点の cwd・編集したファイル・worktree のパスがそのまま残るので、古いパスのセッションがどのリポジトリにも当たらず、ボードに出てこない。

`path_aliases` に古い場所と新しい場所を書くと、記録から読んだパスを読み替えてから分類・まとめ・タブとの結び付けをする。

```json
"path_aliases": {"~/old/src": "~/src", "/Volumes/old-disk/work": "~/work"}
```

- 読み替えるもの: トランスクリプトの cwd (と、そこから推定する worktree)、編集したファイル、稼働中のセッション (`~/.claude/sessions/<pid>.json`) の cwd、作業報告で読むセッションの cwd と編集したファイル。詳細パネルの cwd、「エディタで開く」の開き先、コピーする resume のコマンドの `cd` 先も読み替えた後のパスになる
- 一致はパスの区切りの上だけ (`~/old/src` は `~/old/src/app` に当たり、`~/old/src2` には当たらない)。複数当たるときは長い方を使い、読み替えは 1 回だけ
- `~/.claude/projects` のディレクトリ名は新しい場所の名前に加えて古い場所の名前も探す (ディレクトリを改名していなくても読める)
- 書けるのは `/` か `~/` で始まるパスだけ。`/` そのもの、古い場所と新しい場所が同じもの、同じ古い場所の重複は誤りとして知らせる。`check` は読み替えの一覧を出す
- 記録のファイル自体は書き換えない

### `settings.json` (任意)

書いた項目だけ既定値を上書きする。型が違う (整数に文字列など) と起動時に誤りとして知らせる。

| キー | 既定値 | 説明 |
|---|---|---|
| `port` | 8765 | 待ち受けポート (ホストは常に 127.0.0.1。`host` を書いてもループバック以外にはならない) |
| `window_days` | 21 | この日数より古いセッション・ブランチ・PR は対象外 (未マージの PR は常に対象) |
| `stale_days` | 3 | 最終活動からこの日数を超え、終わっていない作業を「N日 動きなし」(止まっている作業) にする |
| `facts_refresh_seconds` | 60 | 事実の自動更新の間隔 (15 秒未満は 15 秒) |
| `gh_cache_seconds` | 300 | 自動更新時に `gh` の結果を使い回す秒数 (「更新」ボタンは常に取り直す) |
| `llm_enabled` | true | LLM 要約と作業報告を使うか |
| `llm_on_startup` | true | 起動時に要約を実行するか (`--no-llm` で一時的に false) |
| `llm_model` | sonnet | 要約で `claude -p --model` に渡す値 |
| `llm_claude_path` | "" | 要約に使う `claude` のパス。空なら PATH から cmux の shim を除いて探し、なければ `~/.local/bin/claude` |
| `llm_concurrency` | 3 | 同時に走らせる `claude -p` の数 |
| `llm_timeout_seconds` | 120 | 1 回の要約のタイムアウト |
| `llm_input_chars` | 5000 | 1 セッションあたりのプロンプト本文の上限 (文字数) |
| `llm_resummarize_min_new_user_msgs` | 3 | 稼働中のセッションを要約し直すのに必要な、前回の要約以降のユーザーの依頼の数 |
| `llm_resummarize_min_interval_minutes` | 60 | 前回の要約からこの分数が経つまでは要約し直さない |
| `report_input_chars` | 12000 | 作業報告のプロンプトに入れる事実の上限 (文字数) |
| `report_model` | sonnet | 作業報告だけに使う `claude -p --model` の値 (要約は `llm_model`) |
| `report_timeout_seconds` | 300 | 作業報告の 1 回のタイムアウト |
| `ask_model` | sonnet | 詳細パネルの「Claude Code に質問」に使う `claude -p --model` の値。空なら Claude Code の既定のモデル |
| `ask_timeout_seconds` | 180 | 「Claude Code に質問」の 1 回のタイムアウト |
| `digest_enabled` | true | 会話のまとめを作るか |
| `digest_model` | sonnet | 会話の整理に使う `claude -p --model` の値 |
| `digest_timeout_seconds` | 300 | 会話の整理の 1 回のタイムアウト |
| `digest_input_chars` | 30000 | 会話の整理のプロンプトに入れる発言の上限 (文字数)。超えたら最初の 1/4 と最後の 3/4 を残す |
| `digest_lint_rounds` | 2 | yomiyasu の lint に引っかかった文を書き直させる回数の上限 (0 なら書き直させない) |
| `yomiyasu_dir` | "" | yomiyasu のスキルのフォルダ。空なら `~/.claude/skills/yomiyasu`、`~/.agents/skills/yomiyasu` の順に探す |
| `column_wide_min_items` | 8 | `layout` が `auto` の列を 2 倍幅にする進行中の件数 |
| `terminal` | cmux | タブを扱うターミナル。`cmux` / `wezterm` / `orca` / `none` ([ターミナル](#ターミナル)) |
| `editor` | cursor | 「エディタで開く」のコマンド名か絶対パス (`code`、`zed`、`/usr/local/bin/subl` など)。起動時に見つからなければ警告だけ出す |
| `editor_args` | [] | エディタに渡す追加の引数 (例: `["-n"]`)。開く場所はその後に付く |

### ターミナル

`settings.json` の `terminal` で、タブの一覧・「タブへ移動」・「指示待ち」に使うターミナルを選ぶ (`session_board/terminals/`)。
画面の「タブへ移動」などのボタンは、選んだターミナルができること (`/api/state` の `terminal.capabilities`) に合わせて出す。

| `terminal` | タブの一覧 | タブへ移動 | タブを閉じる | 指示待ち | 実機で確認 |
|---|---|---|---|---|---|
| `cmux` (既定) | `cmux tree --all --json` | `cmux select-workspace` + `cmux rpc surface.focus` | `cmux close-surface` | `cmux sidebar-state` の Needs input | 済み |
| `wezterm` | `wezterm cli list --format json` (ペイン単位) | `wezterm cli activate-pane --pane-id N` | `wezterm cli kill-pane --pane-id N` | セッションの status で補う | **未確認** |
| `orca` | `orca terminal list --json` (下記) | `orca terminal switch --terminal <handle> --json` | `orca terminal close --terminal <handle> --json` | セッションの status で補う | **未確認** |
| `none` | 扱わない | ボタンを出さない | しない | セッションの status で補う | - |

- **WezTerm と orca は実機で試していない。** ドキュメントにある出力の形 (フィクスチャ) と偽のコマンドでだけテストしている。WezTerm の `cwd` (`file://host/path`) はパスに直し、`tty_name` (新しい版だけ) があれば tty で結び付ける
- orca の一覧: 全 worktree を取るために `orca terminal list --worktree all --json` → (失敗したら) `orca worktree list --json` の各 worktree で `orca terminal list --worktree <id> --json` → (それも失敗したら) `orca terminal list --json` の順に試す。これは CLI の引数の推測で、版によっては 3 番目 (orca の既定の範囲) だけになる。`truncated` のときは「一部だけ」と知らせる。終了した端末 (`exitCause` あり / `connected: false`) は出さない。`close` の結果が `unverifiable` のときは成功ではなく警告として出す
- タブとセッションの結び付け: (1) tty が同じ (`~/.claude/sessions/<pid>.json` の pid → `ps` の tty) (2) pid が同じ (3) タブの cwd (orca は worktree のパス) が同じ稼働中のセッションが 1 つだけで、その cwd のタブも 1 つだけ。セッションに結び付かないタブは、列の下の「作業と結び付いていないタブ」に出す (ブランチを手がかりに作業へは付けない)
- 指示待ち: cmux はサイドバーの Needs input (ワークスペース単位) を、結び付いた稼働中でない Claude のタブに付ける。それ以外のターミナルでは `~/.claude/sessions/<pid>.json` (`CLAUDE_CONFIG_DIR` に従う) の `status` を使う。Claude Code 2.1 系の値は `busy` (作業中) / `idle` (返答を終えて止まっている) / `waiting` (許可や回答を待っている。`waitingFor` 付き)。**`waiting` だけを指示待ちにする** (idle を指示待ちにすると開いている全セッションに付くため)
- `none` のときもセッション・git・PR の収集や要約などほかの機能はそのまま使える

### データ (`~/.local/share/session-board/`、`SESSION_BOARD_DATA_DIR`)

| ファイル | 内容 |
|---|---|
| `tickets.json` | チケット (名前、URL、案件の名前、表示する列 (空なら自動)、メモ、フェーズ、非表示、中の会話の ID の一覧) |
| `overrides.json` | 手動の上書き (タイトル、フェーズ、メモ、非表示) |
| `cleanup-log.jsonl` | 「ゴミ箱へ」の記録 (閉じたタブ、ゴミ箱へ移した会話の記録の元の場所と移動先) |
| `llm_cache.json` | 要約のキャッシュ (セッション ID ごとの最新の要約と、チケットの id ごとのチケットのまとめ `items`) |
| `reports/<YYYY-MM-DD>.json` | 作業報告のキャッシュ (事実のハッシュ、整理結果、作成時刻、所要時間・費用) |
| `llm-work/` | `claude -p` を実行する作業ディレクトリ |
| `digests/<sessionId>.json` / `.html` | 会話のまとめのデータとページ。`digests/work/` は整理の `claude -p` を実行する作業ディレクトリ、`digests/digest.log` は裏の処理の出力 |
