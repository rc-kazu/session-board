"""claude -p による要約。プロンプト生成と応答の解析は純粋関数、実行はバックグラウンドのワーカー。"""

import json
import os
import queue
import subprocess
import threading
import time

PHASE_GUESSES = {"調査", "実装中", "レビュー待ち", "その他"}
# 日本語の書き方。yomiyasu (AI が書いた日本語を推敲するスキル) の主な決まりを短くまとめたもの。
# claude -p はスキルを読み込まないので、決まりを文で渡す。要約・チケットのまとめ・作業報告・質問の答えで使う
WRITING_RULES = "\n".join([
    "日本語の書き方:",
    "- 記録にある事実だけを書く。記録に無い主体・原因・条件・数値は足さない。言い切りと推量の強さは記録のとおりにする",
    "- 誰が何をしたか (利用者か Claude か) が分かるように書く。道具や概念を人のように書かない (「コードが語る」など)",
    "- 比喩の動詞 (倒す・潰す・溶かす・効く) や大げさな名詞 (契約・正本・ゲート・台帳) を使わず、ふだんの言葉で書く。"
    "データやシステムが「壊れる」は「おかしくなる」「使えなくなる」と書く",
    "- 漢語の名詞を、助詞を落として詰めない (「設定変更後に」ではなく「設定を変えたあとに」)。その場で短い言葉をこしらえない",
    "- 「重要なのは」「注目すべき点は」のような前置きや、中身の無い予告の文を書かない",
    "- 主語の「が」や主題の「は」のすぐあとに、間を作るだけの読点を打たない",
    "- 絵文字・ダッシュ (— や ──)・文末のコロン・言い換えるだけの括弧を使わない。「資料を、全員へ。」のようなコピー調にしない",
    "- 補助動詞と形式名詞はひらがなで書く (してください・できる・すること・するために)",
    "- 文字数・体言止め・出力の形の指示があれば、そちらを優先する",
])
SYSTEM_PROMPT = (
    "あなたは開発者の作業ログを要約するアシスタントです。"
    "出力は指定されたキーを持つ JSON オブジェクト 1 つだけにしてください。前置きやコードフェンスは不要です。\n\n"
    + WRITING_RULES
)
_NEW_SCHEMA = (
    '"ball": "user|claude|reviewer|none のいずれか", '
    '"ball_reason": "ball が user のときだけ 6 文字以内の 1 語、それ以外は null", '
    '"now_short": "いまの状態 (20 文字以内の 1 文)", "next_short": "次の 1 手 (20 文字以内の 1 文、主語なし)", '
    '"flow_steps": [{"label": "16 文字以内", "kind": "依頼|調査|作業|確認|適用|質問", "lane": "app|infra|null", '
    '"state": "done|now|next|later", "who": "user|claude|reviewer|null", '
    '"detail": "その段の詳しい説明 (2〜3 文、150 文字以内)"}]'
)
SESSION_SCHEMA = (
    '{"title": "案件名 (日本語, 25 文字以内)", "goal": "目的 (1 文)", '
    '"done_so_far": "ここまでにやったこと (2 文以内)", "next_action": "次にやること (1 文)", '
    '"phase_guess": "調査|実装中|レビュー待ち|その他 のいずれか", '
    '"blocked_question": "ユーザーの判断待ちの質問があれば 1 文、なければ null", ' + _NEW_SCHEMA + '}'
)
# 画面の「次に動く人」「いま / 次」「流れ図」に使う項目の決め方 (docs/ui-spec.md 6 章)
FIELD_RULES = "\n".join([
    "項目の決め方:",
    "- ball: 次に手を動かすのは誰か。\"user\" は Claude Code の中でなく、利用者が自分で手を動かす必要があるとき"
    " (動作確認・本番適用・他者への共有・チケット起票・設定画面の操作など)。"
    "Claude が質問しているだけなら \"user\" にしない (それは blocked_question に書く)。"
    "Claude が作業を続ける段なら \"claude\"、PR のレビューを待つ段なら \"reviewer\"、"
    "終わった案件 (マージ済みで次の作業が無い) や次の作業が無いときは \"none\"",
    "- ball_reason: ball が \"user\" のときだけ、6 文字以内の 1 語 (動作確認 / 本番適用 / 共有 / 起票 など)。それ以外は null",
    "- now_short: いまの状態を 20 文字以内の 1 文。next_short: 次の 1 手を 20 文字以内の 1 文。主語は入れない",
    "- flow_steps: 作業の流れを古い順に最大 8 件。依頼・調査・作業・確認・適用・質問の段を書く。"
    "lane はアプリ側の作業なら app、インフラ (Terraform / k8s) 側なら infra、どちらでもなければ null。"
    "state は済んだ段が done、いまの段が now、次の段が next、その先が later。"
    "終わった案件では ball を \"none\" にし、next / later の段を入れない",
    "- flow_steps[].detail: label だけでは分からないことを 2〜3 文 (150 文字以内) で書く。"
    "済んだ段は、何をして、何が分かったか・何を決めたか。"
    "いまの段は、何をしているか、何で止まっているか、誰の番か。"
    "これからの段は、何をするか、何が済んだら始められるか",
    "- PR 番号・PR の状態・コミット数は書かない (画面がデータから描く)",
])
# チケットのまとめ: 中の会話の記録から「どういう話か」「今どうなっているか」と、会話どうしのつながりを書かせる
TICKET_SCHEMA = (
    '{"title": "案件名 (日本語, 25 文字以内)", '
    '"background": "どういう話か。背景・前提・目的を 2〜4 文", '
    '"state_now": "結局いまどういう状態か。済んだこと・残っていること・止まっている理由を 2〜3 文", '
    '"goal": "目的 (1 文)", "done_so_far": "ここまでにやったこと (2 文以内)", "next_action": "次にやること (1 文)", '
    '"phase_guess": "調査|実装中|レビュー待ち|その他 のいずれか", "blocked_question": "なければ null", '
    '"links": [{"from": "会話のラベル (S1 など)", "to": "会話のラベル", "kind": "続き|分かれた"}], '
    + _NEW_SCHEMA.replace('"detail": "その段の詳しい説明 (2〜3 文、150 文字以内)"}]',
                          '"detail": "その段の詳しい説明 (2〜3 文、150 文字以内)", "session": "その段をやった会話のラベル (S1 など)"}]')
    + '}'
)
TICKET_RULES = "\n".join([
    "チケットとしてのまとめ方:",
    "- 会話 (S1, S2, ...) は手元での作業の記録。チケットは、それらを読んで「どういう話か」「結局いまどうなっているか」"
    "「どう進んできたか」を答えるもの。会話ごとの要約を並べ直すのではなく、話全体として書く",
    "- background: 何のための作業か、どんな前提や事情があるか。会話の中の依頼文と要約から読み取る",
    "- state_now: 話全体として、いま何が済んでいて、何が残っていて、何で止まっているか",
    "- links: 会話どうしのつながり。from が先の会話、to が後の会話。"
    "\"続き\" は、前の会話の作業を新しい会話で引き継いだもの (コンテキストがいっぱいになって始め直したなど)。"
    "\"分かれた\" は、前の会話で話しているうちに出てきた別の作業を、新しい会話で進めたもの (並行して進むことが多い)。"
    "最初の会話以外は、必ずどれか 1 つの前の会話とつなぐ。はっきりしないときも、内容と時刻からいちばん近い会話を選ぶ",
    "- flow_steps: 話全体の進み方を古い順に最大 8 件。各段の session に、その段をやった会話のラベルを書く",
])
LIMITS = {"background": 500, "state_now": 400, "title": 60, "goal": 300, "done_so_far": 400, "next_action": 300, "blocked_question": 300,
          "ball_reason": 6, "now_short": 20, "next_short": 20, "flow_label": 16,
          "flow_label_full": 100, "flow_detail": 300}
BALLS = ("user", "claude", "reviewer", "none")
FLOW_KINDS = ("依頼", "調査", "作業", "確認", "適用", "質問")
FLOW_LANES = ("app", "infra")
FLOW_STATES = ("done", "now", "next", "later")
FLOW_WHO = ("user", "claude", "reviewer")
MAX_FLOW_STEPS = 8
MAX_TICKET_SESSIONS = 24  # チケットのまとめに渡す会話の数 (古い順)。超えた分は、新しい会話を残して古い会話を落とす
_NULLS = ("null", "none", "なし")


def is_cmux_shim(path):
    """cmux が PATH に差し込む claude の shim / ラッパーなら True。

    cmux のターミナルでは PATH 先頭の claude が cmux-claude-wrapper になり、
    CMUX_SURFACE_ID があると --settings で cmux のフック (通知など) を注入する。
    """
    for p in (path, os.path.realpath(path)):
        parts = os.path.normpath(p).split(os.sep)
        if "cmux-cli-shims" in parts:
            return True
        if any(x.lower().startswith("cmux") and x.lower().endswith(".app") for x in parts):
            return True
    return False


def _is_exe(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_claude_path(configured=None, path_env=None, home=None):
    """要約に使う本物の claude を探す。cmux の shim は使わない。

    優先順: 設定 llm_claude_path -> PATH (shim を除く) -> ~/.local/bin/claude。
    見つからなければ "claude" を返す (その場合も claude_env でフックは無効になる)。
    """
    home = home or os.path.expanduser("~")
    if configured:
        c = configured
        if c == "~" or c.startswith("~/"):
            c = os.path.join(home, c[2:])
        if _is_exe(c) and not is_cmux_shim(c):
            return c
    if path_env is None:
        path_env = os.environ.get("PATH", "")
    for d in path_env.split(os.pathsep):
        if not d:
            continue
        c = os.path.join(d, "claude")
        if _is_exe(c) and not is_cmux_shim(c):
            return c
    c = os.path.join(home, ".local", "bin", "claude")
    if _is_exe(c) and not is_cmux_shim(c):
        return c
    return "claude"


def claude_env(base=None):
    """claude -p 用の環境変数。CMUX_* を落とし、念のため cmux のフックを無効にする。"""
    base = os.environ if base is None else base
    env = {k: v for k, v in base.items() if not k.startswith("CMUX_")}
    env["CMUX_CLAUDE_HOOKS_DISABLED"] = "1"
    return env


def run_claude(prompt, settings, workdir, claude_path, system_prompt=SYSTEM_PROMPT, timeout=None,
               model=None):
    """本物の claude を、フック・ツール・MCP・設定ファイル・セッション記録なしで 1 回実行して stdout を返す。

    cmux の shim を避けるため claude_path は resolve_claude_path の結果を渡し、環境は claude_env で消毒する。
    model を省くと設定の llm_model を使う。
    """
    cmd = [
        claude_path, "-p",
        "--model", model or settings.get("llm_model", "sonnet"),
        "--output-format", "json",
        "--no-session-persistence",
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--system-prompt", system_prompt,
    ]
    os.makedirs(workdir, exist_ok=True)
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=int(timeout or settings.get("llm_timeout_seconds", 120)),
        cwd=workdir,
        env=claude_env(),
    )
    if proc.returncode != 0 and not proc.stdout:
        raise RuntimeError("claude exited %d: %s" % (proc.returncode, proc.stderr[-300:]))
    return proc.stdout


ASK_TOOLS = "Read,Grep,Glob"  # 質問に答えるときに使わせる道具 (読むだけのもの)
ASK_MAX_CHARS = 2000
ASK_HISTORY = 3  # 続けて質問したときに添える、直前の質問と答えの数
ASK_ANSWER_CHARS = 1500  # 添える答えは、この長さで切る


def build_ask_prompt(question, history=()):
    """会話への質問の文。直前の質問と答えがあれば先に添える (記録に残さないので、Claude は前のやり取りを知らない)。"""
    q = (question or "").strip()
    if not q:
        raise ValueError("質問を入力してください。")
    lines = []
    pairs = [h for h in history or [] if isinstance(h, dict) and h.get("q") and h.get("a")][-ASK_HISTORY:]
    if pairs:
        lines.append("（この会話について、画面から続けて質問しています。直前の質問と答えは次のとおりです）")
        for h in pairs:
            lines.append("質問: " + _clip(str(h["q"]), ASK_MAX_CHARS))
            lines.append("答え: " + _clip(str(h["a"]), ASK_ANSWER_CHARS))
        lines.append("")
        lines.append("新しい質問:")
    lines.append(q[:ASK_MAX_CHARS])
    lines.append("")
    lines.append("（ファイルの編集やコマンドの実行はせず、読んで答えるだけにしてください）")
    return "\n".join(lines)


def run_ask(session_id, prompt, settings, cwd, claude_path):
    """会話の続きとして 1 回だけ質問し、答えの文を返す。質問と答えは会話の記録に残さない。

    --no-session-persistence で記録ファイルに書き足さず、新しい記録も作らない。道具は読むだけのものに絞る。
    --setting-sources "" で、利用者の設定にあるフック (通知など) を動かさない。
    """
    cmd = [
        claude_path, "-p",
        "--resume", session_id,
        "--no-session-persistence",
        "--output-format", "json",
        "--tools", ASK_TOOLS,
        "--allowedTools", ASK_TOOLS,
        "--strict-mcp-config",
        "--setting-sources", "",
        "--append-system-prompt", WRITING_RULES,
    ]
    if settings.get("ask_model"):
        cmd += ["--model", settings["ask_model"]]
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=int(settings.get("ask_timeout_seconds", 180)),
        cwd=cwd,
        env=claude_env(),
    )
    return parse_ask_output(proc.stdout, proc.returncode, proc.stderr)


def parse_ask_output(stdout, returncode=0, stderr=""):
    """claude -p --output-format json の出力から答えの文を取り出す。取れなければ ValueError。"""
    try:
        obj = json.loads(stdout or "")
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        text = obj.get("result")
        if obj.get("is_error") or not isinstance(text, str) or not text.strip():
            raise ValueError("Claude Code から答えを受け取れませんでした (%s)。" % _clip(str(text or obj.get("subtype") or "空の答え"), 200))
        return text.strip()
    detail = _clip((stderr or stdout or "").strip(), 200) or "終了コード %d" % returncode
    raise ValueError("Claude Code から答えを受け取れませんでした (%s)。" % detail)


def _clip(text, n):
    text = (text or "").replace("\r", "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def build_session_prompt(s, res, max_chars=5000):
    """セッションの事実から要約用プロンプトを組み立てる。本文は max_chars 程度に収める。"""
    lines = ["次の Claude Code セッションの記録を要約してください。", ""]
    loc = ""
    if res:
        loc = "%s:%s (branch %s)" % (res.get("repo"), res.get("rel") or "/", s.get("branch_last") or "?")
    lines.append("場所: " + (loc or (s.get("cwd_last") or "?")))
    if s.get("ai_title"):
        lines.append("自動タイトル: " + _clip(s["ai_title"], 100))
    budget = max_chars
    body = []
    body.append("## 最初の依頼")
    for p in (s.get("first_prompts") or [])[:3]:
        body.append("- " + _clip(p, 500))
    body.append("## 直近のやりとり")
    for m in (s.get("recent") or [])[-6:]:
        who = "ユーザー" if m.get("role") == "user" else "Claude"
        body.append("[%s] %s" % (who, _clip(m.get("text"), 450)))
    files = s.get("files_edited") or []
    if files:
        body.append("## 編集したファイル (%d 件)" % len(files))
        for f in files[:15]:
            body.append("- " + _clip(f, 160))
    prs = s.get("prs") or []
    if prs:
        body.append("## 関係する PR")
        for p in prs[:6]:
            body.append("- %s%s" % (p.get("url"), " (このセッションで作成)" if p.get("created") else ""))
    cmds = s.get("commands") or []
    if cmds:
        body.append("## 主なコマンド")
        for c in cmds[:5]:
            body.append("- " + _clip(c, 150))
    text = "\n".join(body)
    if len(text) > budget:
        text = text[: budget - 1] + "…"
    lines.append(text)
    lines.append("")
    lines.append(FIELD_RULES)
    lines.append("")
    lines.append("出力形式 (JSON のみ): " + SESSION_SCHEMA)
    return "\n".join(lines)


def _extract_object(text):
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _as_text(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return " / ".join(_as_text(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _short(v, n):
    """短い 1 文の項目: 空や "null" は None、長ければ n 文字に収める。"""
    if v is None:
        return None
    t = _clip(_as_text(v), n)
    return t if t and t.lower() not in _NULLS else None


def _enum(v, allowed):
    if not isinstance(v, str):
        return None
    v = v.strip().lower() if v.strip().isascii() else v.strip()
    return v if v in allowed else None


def _flow_steps(v, sessions=None):
    """flow_steps を検証する。配列でなければ None。不正な要素は捨て、多ければ新しい方の 8 件を残す。

    label は流れ図にそのまま出すので 16 字に収め、切る前の文を label_full に残す (押すと全文を見せる)。
    label_full と detail のキーは常に入れる (detail が無い古い要約と見分けるため。summary_cache.lacks_new_fields)。
    """
    if v is None or not isinstance(v, list):
        return None
    out = []
    for st in v:
        if not isinstance(st, dict):
            continue
        label = _short(st.get("label"), LIMITS["flow_label"])
        state = _enum(st.get("state"), FLOW_STATES)
        if not label or not state:
            continue
        out.append({
            "label": label,
            "kind": _enum(st.get("kind"), FLOW_KINDS) or "作業",
            "lane": _enum(st.get("lane"), FLOW_LANES),
            "state": state,
            "who": _enum(st.get("who"), FLOW_WHO),
            "label_full": _short(st.get("label"), LIMITS["flow_label_full"]),
            "detail": _short(st.get("detail"), LIMITS["flow_detail"]),
        })
        if sessions is not None:
            sess = st.get("session").strip() if isinstance(st.get("session"), str) else None
            out[-1]["session"] = sess if sess in sessions else None
    return out[-MAX_FLOW_STEPS:]


def parse_new_fields(obj):
    """6 章の項目 (ball / ball_reason / now_short / next_short / flow_steps) を検証して返す。欠けは None。"""
    ball = _enum(obj.get("ball"), BALLS)
    return {
        "ball": ball,
        "ball_reason": _short(obj.get("ball_reason"), LIMITS["ball_reason"]) if ball == "user" else None,
        "now_short": _short(obj.get("now_short"), LIMITS["now_short"]),
        "next_short": _short(obj.get("next_short"), LIMITS["next_short"]),
        "flow_steps": _flow_steps(obj.get("flow_steps")),
    }


def _ticket_links(v, labels):
    """links を検証する。ラベルは labels の中だけ、from は to より前の会話。1 つの会話につなぐのは 1 本だけ。"""
    out, seen = [], set()
    order = {x: i for i, x in enumerate(labels)}
    for ln in v if isinstance(v, list) else []:
        if not isinstance(ln, dict):
            continue
        a, b = ln.get("from"), ln.get("to")
        kind = _enum(ln.get("kind"), ("続き", "分かれた"))
        if a not in order or b not in order or order[a] >= order[b] or not kind or b in seen:
            continue
        seen.add(b)
        out.append({"from": a, "to": b, "kind": kind})
    return out


def parse_ticket_output(stdout, labels):
    """チケットのまとめを取り出す。parse_llm_output の項目に background / state_now / links と、段の session を足す。"""
    base = parse_llm_output(stdout)
    if base is None:
        return None
    text = stdout
    try:
        env = json.loads(stdout)
        if isinstance(env, dict) and "result" in env:
            text = env.get("result") or ""
    except ValueError:
        pass
    obj = _extract_object(text) or {}
    base["background"] = _clip(_as_text(obj.get("background")), LIMITS["background"]) or None
    base["state_now"] = _clip(_as_text(obj.get("state_now")), LIMITS["state_now"]) or None
    base["links"] = _ticket_links(obj.get("links"), labels)
    base["flow_steps"] = _flow_steps(obj.get("flow_steps"), labels)
    return base


def link_hints(members):
    """時刻から見た、会話どうしのつながりの候補。members は古い順の [{label, first_ts, last_ts}]。

    後の会話が始まった時点で終わっていた前の会話は「続きの候補」、まだ動いていた会話は「並行の候補」。
    決めるのは LLM (同じ時間に動いていても、片方から分かれた作業とは限らないため)。
    """
    out = []
    for i, m in enumerate(members):
        if i == 0:
            continue
        start = m.get("first_ts") or ""
        before = members[:i]
        ended = [x for x in before if (x.get("last_ts") or "") <= start]
        running = [x for x in before if (x.get("last_ts") or "") > start]
        parts = []
        if ended:
            parts.append("続きの候補: " + ended[-1]["label"] + " (始めたときに終わっていた会話のうち、最後のもの)")
        if running:
            parts.append("並行の候補: " + ", ".join(x["label"] for x in running) + " (始めたときにまだ動いていた)")
        out.append("%s: %s" % (m["label"], " / ".join(parts) or "候補なし"))
    return out


def build_ticket_prompt(name, memo, members, pr_titles, repo_branches):
    """チケットのまとめを作る文。members は古い順の会話 [{label, first_ts, last_ts, cwd, branch, first_prompt,
    last_prompt, summary}]。"""
    lines = ["次の会話 (Claude Code のセッション) は、どれも同じチケットに入っています。チケットとしてまとめてください。", ""]
    lines.append("チケットの名前: " + _clip(name, 100))
    if memo:
        lines.append("チケットのメモ: " + _clip(memo, 500))
    if repo_branches:
        lines.append("ブランチ: " + ", ".join(repo_branches[:6]))
    if pr_titles:
        lines.append("PR: " + " / ".join(_clip(t, 100) for t in pr_titles[:6] if t))
    lines.append("")
    lines.append("## 会話 (古い順)")
    for m in members[-MAX_TICKET_SESSIONS:]:
        sm = m.get("summary") or {}
        lines.append("### %s (%s 〜 %s)" % (m["label"], m.get("first_ts") or "?", m.get("last_ts") or "?"))
        if m.get("cwd") or m.get("branch"):
            lines.append("場所: %s %s" % (m.get("cwd") or "", m.get("branch") or ""))
        if m.get("manual_phase") in ("完了", "マージ済み", "デプロイ済み"):
            lines.append("状態: 利用者が手で「%s」にした。終わった作業として扱い、いま・次の段や次に動く人の根拠にしない"
                         % m["manual_phase"])
        elif m.get("manual_phase"):
            lines.append("状態: 利用者が手でフェーズを「%s」にした" % m["manual_phase"])
        if m.get("first_prompt"):
            lines.append("最初の依頼: " + _clip(m["first_prompt"], 400))
        if m.get("last_prompt") and m.get("last_prompt") != m.get("first_prompt"):
            lines.append("最後の依頼: " + _clip(m["last_prompt"], 300))
        if not sm:
            lines.append("要約: まだありません (依頼文と時刻から読み取ってください)")
            continue
        lines.append("要約: タイトル: %s / 目的: %s / 済: %s / 次: %s" % (
            _clip(sm.get("title"), 60), _clip(sm.get("goal"), 150),
            _clip(sm.get("done_so_far"), 200), _clip(sm.get("next_action"), 150)))
        for st in (sm.get("flow_steps") or [])[:MAX_FLOW_STEPS]:
            lines.append("  - %s (%s): %s" % (_clip(st.get("label_full") or st.get("label"), 40), st.get("state") or "?",
                                              _clip(st.get("detail"), 150)))
    hints = link_hints(members[-MAX_TICKET_SESSIONS:])
    if hints:
        lines.append("")
        lines.append("## 時刻から見た、会話どうしのつながりの候補")
        lines.extend("- " + h for h in hints)
    lines.append("")
    lines.append(TICKET_RULES)
    lines.append("")
    lines.append(FIELD_RULES)
    lines.append("")
    lines.append("出力形式 (JSON のみ): " + TICKET_SCHEMA)
    return "\n".join(lines)


def parse_llm_output(stdout):
    """claude -p --output-format json の出力から要約 dict を取り出す。失敗時は None。"""
    if not stdout or not stdout.strip():
        return None
    text = stdout
    try:
        env = json.loads(stdout)
        if isinstance(env, dict) and "result" in env:
            if env.get("is_error"):
                return None
            text = env.get("result") or ""
        elif isinstance(env, dict):
            text = stdout
    except ValueError:
        pass
    obj = _extract_object(text)
    if obj is None:
        return None
    title = _clip(_as_text(obj.get("title")), LIMITS["title"])
    if not title:
        return None
    out = {"title": title}
    for k in ("goal", "done_so_far", "next_action"):
        out[k] = _clip(_as_text(obj.get(k)), LIMITS[k])
    bq = _clip(_as_text(obj.get("blocked_question")), LIMITS["blocked_question"])
    out["blocked_question"] = bq if bq and bq.lower() not in ("null", "none", "なし") else None
    pg = _as_text(obj.get("phase_guess")).strip()
    out["phase_guess"] = pg if pg in PHASE_GUESSES else "その他"
    out.update(parse_new_fields(obj))
    return out


class LlmRunner:
    """claude -p を限られた並列度で実行し、結果をキャッシュに保存する。"""

    def __init__(self, cache, settings, workdir, on_update):
        self.cache = cache  # store.JsonStore
        self.settings = settings
        self.workdir = workdir
        self.on_update = on_update
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.pending = set()
        self.done = 0
        self.failed = 0
        self.total = 0
        self.timings = []
        self.last_error = None
        self.claude_path = resolve_claude_path(settings.get("llm_claude_path"))
        os.makedirs(workdir, exist_ok=True)
        for _ in range(max(1, int(settings.get("llm_concurrency", 3)))):
            threading.Thread(target=self._worker, daemon=True).start()

    def status(self):
        with self.lock:
            avg = sum(self.timings) / len(self.timings) if self.timings else None
            return {
                "queued": len(self.pending),
                "done": self.done,
                "failed": self.failed,
                "total": self.total,
                "avg_seconds": round(avg, 1) if avg else None,
                "last_error": self.last_error,
            }

    def submit(self, key, prompt, store, parse=None):
        """store(cache_data, result, at) で成功した要約をキャッシュに書き込む。parse は出力の取り出し方 (既定は parse_llm_output)。"""
        with self.lock:
            if key in self.pending:
                return False
            self.pending.add(key)
            self.total += 1
        self.q.put((key, prompt, store, parse or parse_llm_output))
        return True

    def is_pending(self, key):
        with self.lock:
            return key in self.pending

    def _run(self, prompt):
        return run_claude(prompt, self.settings, self.workdir, self.claude_path)

    def _worker(self):
        while True:
            key, prompt, store, parse = self.q.get()
            t0 = time.time()
            result = None
            try:
                result = parse(self._run(prompt))
                if result is None:
                    raise ValueError("LLM の出力を JSON として解釈できませんでした")
            except Exception as e:  # noqa: BLE001 - ワーカーは落とさない
                with self.lock:
                    self.failed += 1
                    self.last_error = "%s: %s" % (key, e)
                self.cache.update(lambda d: d.setdefault("failures", {}).__setitem__(key, time.time()))
            else:
                at = time.time()

                def write(d):
                    store(d, result, at)
                    (d.get("failures") or {}).pop(key, None)

                self.cache.update(write)
                with self.lock:
                    self.done += 1
                    self.timings.append(time.time() - t0)
            finally:
                with self.lock:
                    self.pending.discard(key)
                try:
                    self.on_update()
                except Exception:  # noqa: BLE001
                    pass
