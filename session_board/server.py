"""HTTP サーバ (127.0.0.1 のみ) と収集・要約のオーケストレーション。"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import cleanup as cl
from . import collectors, summary_cache
from . import editor as ed
from . import config as cfg
from . import overrides as ovr
from . import report as rp
from . import digest as dg
from . import tickets as tk
from .classify import Classifier, resolve_path
from .config import STATIC_DIR, ConfigError, load_settings, load_targets
from .grouping import build_items, is_session_override
from .llm import (LlmRunner, build_ask_prompt, build_session_prompt, build_ticket_prompt, parse_ticket_output,
                  resolve_claude_path, run_ask, run_claude)
from .phase import PHASES
from .store import JsonStore
from .terminals import default_runner, make_terminal

REF_RE = {
    "workspace": re.compile(r"^workspace:\d+$"),
    "surface": re.compile(r"^surface:\d+$"),
    "window": re.compile(r"^window:\d+$"),
}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/static/digest.js": ("digest.js", "text/javascript; charset=utf-8"),
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
# 会話のまとめのページ (digest.render_html) は CSS を中に書くので、style だけ inline を許す。
# script は /static/digest.js (ライト / ダークの切り替え) だけ
DIGEST_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'; "
              "base-uri 'none'; form-action 'none'")
DIGEST_RE = re.compile(r"^/digest/([A-Za-z0-9_-]{1,100})$")
MAX_BODY = 64 * 1024
NO_ITEM = "作業がありません。表示を更新してください。"
CLEANUP_RE = re.compile(r"^/api/items/([0-9a-f]{6,40})/cleanup$")
ASK_SLOTS = threading.BoundedSemaphore(2)  # 同時に答えを待てる質問の数


def model_label(model):
    """画面に出すモデル名。"sonnet" -> "Sonnet"。"""
    model = str(model or "")
    return model[:1].upper() + model[1:]


class Board:
    def __init__(self, settings=None, targets=None, data_dir=None, config_dir=None):
        self.settings = settings or load_settings()
        self.targets = targets or load_targets()
        self.classifier = Classifier(self.targets)
        self.terminal = make_terminal(self.settings.get("terminal") or "cmux")
        self.claude_dir = collectors.CLAUDE_DIR
        self.trash_dir = os.path.join(os.path.expanduser("~"), ".Trash")
        self.trash_runner = default_runner
        self.cleanup_wait_seconds = 6.0
        self.data_dir = data_dir or cfg.data_dir()
        self.config_dir = config_dir or cfg.config_dir()
        os.makedirs(self.data_dir, exist_ok=True)
        self.report_jobs = {}  # 日付 -> 作業報告の生成状況
        ov_path = os.path.join(self.data_dir, "overrides.json")
        try:
            stats = ovr.migrate_file(ov_path)
            if stats:
                print("session-board: 手動の上書きを新しい形に移しました (「不要」→「完了」%d 件、アーカイブ→非表示 %d 件、"
                      "元のファイルは %s)" % (stats["phase"], stats["archived"], stats["backup"]), flush=True)
        except OSError:
            traceback.print_exc()
        # 前の版が書いた merges / detached_sessions は読まないが、消さずに残す
        self.overrides = JsonStore(ov_path, {"items": []})
        self.tickets = JsonStore(os.path.join(self.data_dir, "tickets.json"), tk.empty())
        cache_path = os.path.join(self.data_dir, "llm_cache.json")
        try:
            stats = summary_cache.migrate_file(cache_path)
            if stats:
                print("session-board: 要約キャッシュを新しい形式に移行しました (旧 %d 件 -> セッション %d 件 / 案件 %d 件、"
                      "元のファイルは llm_cache.json.bak)" % (stats["old_entries"], stats["sessions"],
                                                            stats["legacy_items"]), flush=True)
        except OSError:
            traceback.print_exc()
        self.llm_cache = JsonStore(cache_path, summary_cache.empty())
        self.transcripts = collectors.TranscriptCache()
        self.gh = collectors.GhCache()
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.facts = None
        self.result = {"items": [], "unlinked_tabs": [], "summary": {}}
        self.version = 0
        self.generated_at = None
        self.progress = {"running": False, "stage": None, "started_at": None, "finished_at": None,
                         "error": None, "seconds": None}
        self.llm_run_started = None
        self.runner = None
        if self.settings.get("llm_enabled", True):
            self.runner = LlmRunner(self.llm_cache, self.settings, os.path.join(self.data_dir, "llm-work"),
                                    self._on_llm_update)

    # ------------------------------------------------------------ 状態

    def _llm_view(self):
        """build_items に渡す要約: 保存されている最新の要約を常に返す (取り直し中も前回のものを表示)。"""
        data = self.llm_cache.snapshot()
        stored = data.get("sessions") or {}
        sessions = {}
        for s in (self.facts or {}).get("sessions") or []:
            e = stored.get(s["session_id"])
            if e and e.get("summary"):
                sessions[s["session_id"]] = dict(e["summary"], summarized_at=e.get("summarized_at"))
        return {"sessions": sessions, "items": data.get("items") or {}}

    def rebuild(self):
        with self.lock:
            if self.facts is None:
                return
            result = build_items(self.facts, self.classifier, self.settings, self.overrides.snapshot(),
                                 self._llm_view(), self._tickets_snapshot())
            repos_info = self.facts.get("repos") or {}
            for it in result.get("items", []):
                for x in [it] + (it.get("members") or []):
                    x["open_targets"] = ed.open_targets(x, self.targets["repos"], repos_info)
            self.result = result
            self.version += 1
            self.generated_at = time.time()

    def _tickets_snapshot(self):
        # テストなどで tickets を持たない Board も動くようにする
        store = getattr(self, "tickets", None)
        return store.snapshot() if store else tk.empty()

    def projects_view(self):
        """画面の列: targets.json の projects の順、最後にどれにも当たらない列 (other_label)。"""
        other = self.classifier.other_label
        cols = [{"name": p["name"], "label": p.get("label"), "color": p.get("color"), "layout": p.get("layout") or "auto",
                 "cloud": p.get("cloud"), "other": False}
                for p in self.targets.get("projects", self.targets.get("targets", []))]
        cols.append({"name": other, "label": None, "color": None, "layout": "auto", "cloud": None, "other": True})
        return cols

    def state(self):
        with self.lock:
            projects = self.projects_view()
            return {
                "version": self.version,
                "generated_at": self.generated_at,
                "items": self.result.get("items", []),
                "unlinked_tabs": self.result.get("unlinked_tabs", []),
                "summary": self.result.get("summary", {}),
                "targets": [p["name"] for p in projects],
                "projects": projects,
                "other_label": self.classifier.other_label,
                "ticket_projects": tk.projects(self._tickets_snapshot()),
                "config": {"configured": self.targets.get("configured", True), "message": self.targets.get("message"),
                           "dir": getattr(self, "config_dir", cfg.CONFIG_DIR)},
                "phases": PHASES,
                "digests": self.digest_briefs(self.result.get("items", [])),
                "progress": dict(self.progress),
                "llm": self.runner.status() if self.runner else None,
                "llm_enabled": bool(self.runner),
                "errors": (self.facts or {}).get("errors", []),
                "terminal": dict(self.terminal.info(), error=(self.facts or {}).get("terminal_error")),
                "editor": {"name": ed.display_name(self.settings.get("editor"))},
                "settings": {k: self.settings.get(k) for k in ("window_days", "stale_days", "facts_refresh_seconds",
                                                                 "column_wide_min_items")},
            }

    def digest_briefs(self, items):
        """会話のまとめがある会話の、詳細パネルに出す分。{sessionId: brief}。チケットの中の会話も含む。"""
        if not getattr(self, "data_dir", None):
            return {}
        cache = getattr(self, "_digest_cache", None)
        if cache is None:
            cache = self._digest_cache = dg.BriefCache(self.data_dir)
        out = {}
        for it in [x for it in items for x in [it] + list(it.get("members") or [])]:
            for s in it.get("sessions") or []:
                sid = s.get("session_id")
                if sid and sid not in out:
                    b = cache.get(sid)
                    if b:
                        out[sid] = b
        return out

    def digest_page(self, session_id):
        """会話のまとめのページ。パンくずリストに、その会話の作業の列とタイトルを入れて描き直す。無ければ None。

        チケットに入った会話は「列 › チケットの名前 › 会話のタイトル」にする。
        """
        d = dg.load(session_id, self.data_dir)
        if not d or not d.get("theme"):
            return None
        crumbs = None
        with self.lock:
            items = list(self.result.get("items", []))
            labels = {p["name"]: p.get("label") or p["name"] for p in self.projects_view()}

        def has(it):
            return any(s.get("session_id") == session_id for s in it.get("sessions") or [])

        for it in items:
            col = labels.get(it.get("target"), it.get("target"))
            if it.get("kind") == "ticket":
                m = next((m for m in it.get("members") or [] if has(m)), None)
                if m:
                    crumbs = [col, it.get("title"), m.get("title")]
                    break
            elif has(it):
                crumbs = [col, it.get("title")]
                break
        return dg.render_html(d, d.get("meta"), crumbs)

    # ------------------------------------------------------------ 収集

    def refresh(self, force_gh=False, run_llm=False):
        if not self.refresh_lock.acquire(blocking=False):
            if run_llm:
                # 実行中の収集が終わってから要約を始める
                threading.Thread(target=self._refresh_after, args=(run_llm,), daemon=True).start()
            return False
        t0 = time.time()
        try:
            with self.lock:
                self.progress.update(running=True, stage="開始", started_at=t0, error=None)

            def stage(name):
                with self.lock:
                    self.progress["stage"] = name

            facts = collectors.collect_all(self.targets, self.settings, self.transcripts, self.gh, force_gh, stage,
                                           terminal=self.terminal)
            with self.lock:
                self.facts = facts
            self.rebuild()
            if run_llm and self.runner:
                self.enqueue_llm()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            with self.lock:
                self.progress["error"] = str(e)
        finally:
            with self.lock:
                self.progress.update(running=False, stage=None, finished_at=time.time(),
                                     seconds=round(time.time() - t0, 1))
            self.refresh_lock.release()
        return True

    def _refresh_after(self, run_llm):
        with self.refresh_lock:
            pass
        if run_llm and self.runner:
            self.enqueue_llm()

    def enqueue_llm(self, session_ids=None, force=False):
        """要約し直す価値のあるセッションだけをキューに入れる (summary_cache.session_needs_summary)。

        起動時・「更新」ボタンはこの判定に従う。カードの「要約し直す」は force=True で判定を飛ばす。
        """
        if not self.runner:
            return 0
        with self.lock:
            self.llm_run_started = time.time()
            sessions = list((self.facts or {}).get("sessions") or [])
        stored = self.llm_cache.snapshot().get("sessions") or {}
        repo_paths = self.targets["repos"]
        worktrees = {n: r.get("worktrees") or [] for n, r in ((self.facts or {}).get("repos") or {}).items()}
        now = time.time()
        n = 0
        sessions.sort(key=lambda s: s.get("last_ts") or "", reverse=True)
        for s in sessions:
            sid = s["session_id"]
            if session_ids is not None and sid not in session_ids:
                continue
            if not summary_cache.session_needs_summary(stored.get(sid), s, now, self.settings, force=force):
                continue
            res = resolve_path(s.get("cwd_last"), repo_paths, worktrees)
            prompt = build_session_prompt(s, res, max_chars=int(self.settings.get("llm_input_chars", 5000)))
            if self.runner.submit("s:" + sid, prompt, _session_store(sid, s)):
                n += 1
        n += self._enqueue_merges()
        return n

    def _enqueue_merges(self):
        """チケットのまとめが要るもの (中の会話か、その要約が変わったもの) をキューに入れる。

        中の会話の要約がまだ処理中なら、それが終わってから 1 回だけ作る。会話が 1 つでも作る。
        """
        if not self.runner or self.llm_run_started is None:
            return 0
        failures = self.llm_cache.snapshot().get("failures") or {}
        tdata = self._tickets_snapshot()
        n = 0
        with self.lock:
            items = list(self.result.get("items", []))
        for it in items:
            if not it.get("llm_item_refresh"):
                continue
            key = "i:" + it["id"]
            if failures.get(key, 0) > self.llm_run_started:
                continue
            if any(self.runner.is_pending("s:" + s["session_id"]) for s in it["sessions"]):
                continue
            # 中の会話を古い順に S1, S2, ... と呼ぶ。まとめの中のラベルは、保存するときに会話の ID に戻す。
            # 要約がまだ無い会話も、時刻と依頼文だけで入れる (入れないと、その会話がまとめから抜け落ちる)
            facts = []
            for m in it.get("members") or []:
                s = (m.get("sessions") or [None])[0]
                if not s:
                    continue
                manual = m.get("phase") if m.get("phase_source") == "手動" else None
                facts.append({"label": "S%d" % (len(facts) + 1), "session_id": s["session_id"],
                              "first_ts": s.get("first_ts"), "last_ts": s.get("last_ts"), "cwd": s.get("cwd"),
                              "branch": s.get("branch"), "first_prompt": s.get("first_prompt"),
                              "last_prompt": s.get("last_prompt"), "summary": s.get("summary") or {},
                              "manual_phase": manual})
            if not facts:
                continue
            t = tk.find(tdata, it["id"]) or {}
            members = {f["session_id"]: {"summarized_at": summary_cache.ticket_member_stamp(
                f["summary"].get("summarized_at"), f["manual_phase"])} for f in facts}
            prompt = build_ticket_prompt(t.get("name") or it.get("title"), t.get("memo"), facts,
                                         [p.get("title") for p in it["prs"]],
                                         ["%s:%s" % (b["repo"], b["branch"]) for b in it["repo_branches"]])
            labels = [f["label"] for f in facts]
            to_sid = {f["label"]: f["session_id"] for f in facts}
            if self.runner.submit(key, prompt, _ticket_store(it["id"], members, to_sid),
                                  parse=lambda out, labels=labels: parse_ticket_output(out, labels)):
                n += 1
        return n

    def _on_llm_update(self):
        self.rebuild()
        self._enqueue_merges()

    def auto_refresh_loop(self):
        interval = max(15, int(self.settings.get("facts_refresh_seconds", 60)))
        while True:
            time.sleep(interval)
            try:
                self.refresh(force_gh=False, run_llm=False)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    # ------------------------------------------------------------ 操作

    def find_item(self, item_id):
        """作業を id で探す。チケットの中の会話の作業 (members) も探す。"""
        with self.lock:
            for it in self.result.get("items", []):
                if it["id"] == item_id:
                    return it
                for m in it.get("members") or []:
                    if m["id"] == item_id:
                        return m
        return None

    def set_override(self, item_id, patch):
        it = self.find_item(item_id)
        if not it:
            raise KeyError(NO_ITEM)
        clean = {}
        if "title" in patch:
            clean["title"] = _text(patch["title"], 200)
        if "memo" in patch:
            clean["memo"] = _text(patch["memo"], 4000)
        if "phase" in patch:
            ph = patch["phase"] or ""
            if ph and ph not in PHASES:
                raise ValueError("「%s」は、フェーズに設定できません。" % _text(ph, 20))
            clean["phase"] = ph
        if "hidden" in patch:
            clean["hidden"] = bool(patch["hidden"])
        if "reopened" in patch:
            clean["reopened"] = bool(patch["reopened"])
        if not clean:
            raise ValueError("変更する内容がありません。")
        if it.get("kind") == "ticket":
            # チケットの上書きは tickets.json のチケットに書く (タイトルはチケットの名前)
            if "title" in clean:
                clean["name"] = clean.pop("title")
            self.update_ticket(it["id"], clean)
            return
        ss_key = it["keys"][0]

        def apply(d):
            items = d.setdefault("items", [])
            # 会話 1 つだけの記録に書く。会話が 2 つ以上だった作業の記録には書き足さない (build_items が読まないため)
            rec = next((ov for ov in items if isinstance(ov, dict) and is_session_override(ov, ss_key)), None)
            if rec is None:
                rec = {"keys": [ss_key]}
                items.append(rec)
            rec.update(clean)
            # 旧い値 (phase 不要 / archived) が残っていれば新しい値に直して書く
            fixed, _changed = ovr.normalize_record(rec)
            rec.clear()
            rec.update(fixed)
            rec["updated_at"] = time.time()

        self.overrides.update(apply)
        self.rebuild()
        if it.get("ticket_id"):
            # チケットの中の会話のフェーズを手で変えたら、チケットのまとめを作り直す
            self._resummarize_tickets()

    def set_column_label(self, name, label):
        """列の表示名を targets.json に書き、読み込み済みの設定にも当てる。列名 (目印) は変えない。"""
        if not isinstance(name, str) or name not in self.classifier.target_names:
            raise KeyError("「%s」という列はありません。表示を更新してください。" % str(name)[:40])
        try:
            written = cfg.set_project_label(getattr(self, "config_dir", cfg.CONFIG_DIR), name, label)
        except (ConfigError, OSError) as e:
            raise ValueError("targets.json に書き込めませんでした (%s)。" % (" / ".join(getattr(e, "errors", None) or [str(e)]))[:300])
        with self.lock:
            for p in self.targets.get("projects", self.targets.get("targets", [])):
                if p["name"] == name:
                    p["label"] = written
            self.version += 1
        return written

    # ------------------------------------------------------------ チケット

    def _known_session(self, sid):
        if not tk.valid_session_id(sid):
            raise ValueError("会話の ID の形が正しくありません。")
        with self.lock:
            sessions = (self.facts or {}).get("sessions") or []
        if not any(s.get("session_id") == sid for s in sessions):
            raise KeyError("会話がありません。表示を更新してください。")
        return sid

    def _ticket_op(self, fn):
        out = {}

        def apply(d):
            out["value"] = fn(d)

        try:
            self.tickets.update(apply)
        except KeyError:
            raise KeyError("チケットがありません。表示を更新してください。")
        self.rebuild()
        self._resummarize_tickets()
        return out.get("value")

    def _resummarize_tickets(self):
        """チケットの中の会話が変わったら、すぐにまとめを作り直す (起動や「更新」を待たない)。

        中の会話で要約がまだ無いものも、要約し直す価値があればキューに入れる。終わったら _on_llm_update が
        もう一度まとめを作り直す。
        """
        if not self.runner:
            return
        with self.lock:
            sids = {s["session_id"] for it in self.result.get("items", []) if it.get("llm_item_refresh")
                    for s in it.get("sessions") or []}
        if sids:
            run_later = getattr(self, "run_later", None) or (
                lambda fn, **kw: threading.Thread(target=fn, kwargs=kw, daemon=True).start())
            run_later(self.enqueue_llm, session_ids=sids)

    def _check_target(self, target):
        if target in (None, ""):
            return ""
        if target not in self.classifier.target_names + [self.classifier.other_label]:
            raise ValueError("「%s」という列はありません。" % str(target)[:40])
        return target

    def create_ticket(self, name, session_ids, target=None):
        if not isinstance(session_ids, list) or len(session_ids) > 50:
            raise ValueError("session_ids の形が正しくありません。")
        sids = [self._known_session(sid) for sid in session_ids]
        if name is not None and not isinstance(name, str):
            raise ValueError("文字列で指定してください。")
        target = self._check_target(target)
        return self._ticket_op(lambda d: tk.create(d, name, sids, time.time(), target=target))

    def add_to_ticket(self, ticket_id, session_id):
        sid = self._known_session(session_id)
        return self._ticket_op(lambda d: tk.add(d, ticket_id, sid, time.time()))

    def remove_from_ticket(self, ticket_id, session_id):
        if not tk.valid_session_id(session_id):
            raise ValueError("会話の ID の形が正しくありません。")
        return self._ticket_op(lambda d: tk.remove(d, ticket_id, session_id, time.time()))

    def update_ticket(self, ticket_id, patch):
        clean = tk.clean_patch(patch)
        if "target" in clean:
            clean["target"] = self._check_target(clean["target"])
        self._ticket_op(lambda d: tk.update(d, ticket_id, clean, time.time()))

    def merge_ticket(self, ticket_id, into):
        if not tk.valid_id(ticket_id) or not tk.valid_id(into):
            raise ValueError("チケットの id の形が正しくありません。")
        return self._ticket_op(lambda d: tk.merge(d, ticket_id, into, time.time()))

    def delete_ticket(self, ticket_id):
        return self._ticket_op(lambda d: tk.delete(d, ticket_id))

    def summarize_item(self, item_id):
        it = self.find_item(item_id)
        if not it:
            raise KeyError(NO_ITEM)
        if not self.runner:
            raise ValueError("要約がオフのときは、要約し直すことはできません。")
        return self.enqueue_llm(session_ids={s["session_id"] for s in it["sessions"]}, force=True)

    def ask(self, item_id, session_id, question, history=None):
        """作業の会話の続きとして Claude Code に質問し、答えの文を返す。質問と答えは会話の記録に残さない。"""
        it = self.find_item(item_id)
        if not it:
            raise KeyError(NO_ITEM)
        if not isinstance(question, str) or not isinstance(session_id, str):
            raise ValueError("文字列で指定してください。")
        if history is not None and (not isinstance(history, list) or len(history) > 20):
            raise ValueError("history の形が正しくありません。")
        sessions = list(it.get("sessions") or [])
        for m in it.get("members") or []:
            sessions.extend(m.get("sessions") or [])
        s = next((x for x in sessions if x.get("session_id") == session_id), None)
        if not s:
            raise KeyError("この作業に、その会話はありません。表示を更新してください。")
        prompt = build_ask_prompt(question, history)
        # 道具 (Read など) が会話の場所のファイルを読めるよう、会話が最後にいたディレクトリで実行する
        cwd = s.get("cwd") if s.get("cwd") and os.path.isdir(s["cwd"]) else os.path.join(self.data_dir, "llm-work")
        os.makedirs(cwd, exist_ok=True)
        if not ASK_SLOTS.acquire(blocking=False):
            raise ValueError("ほかの質問の答えを待っています。答えが出てから、もう一度質問してください。")
        try:
            return run_ask(session_id, prompt, self.settings, cwd,
                           resolve_claude_path(self.settings.get("llm_claude_path")))
        except subprocess.TimeoutExpired:
            raise ValueError("%d 秒たっても答えが返りませんでした。質問を短くするか、時間をおいてやり直してください。"
                             % int(self.settings.get("ask_timeout_seconds", 180)))
        except OSError as e:
            raise ValueError("Claude Code を起動できませんでした (%s)。" % e)
        finally:
            ASK_SLOTS.release()

    def find_tab(self, tab_id):
        """最後に集めたタブの中から ID で探す。ID の形が不正なら ValueError、無ければ KeyError。"""
        if not self.terminal.valid_id(tab_id):
            raise ValueError("タブの ID の形が正しくありません。")
        with self.lock:
            tabs = (self.facts or {}).get("tabs") or []
        tab = next((t for t in tabs if t.get("id") == tab_id), None)
        if not tab:
            raise KeyError("タブがありません。閉じられた可能性があります。")
        return tab

    def focus_tab(self, tab_id):
        """既知のタブだけを前面に出す (アダプタは引数リストでコマンドを実行する)。戻り値はエラーの一覧。"""
        tab = self.find_tab(tab_id)
        if not self.terminal.capabilities.get("focus"):
            raise ValueError("このターミナル (%s) では、タブへ移動できません。" % self.terminal.label)
        return self.terminal.focus(tab)

    def focus(self, workspace_ref, surface_ref):
        """旧い形の要求 (cmux の workspace_ref / surface_ref)。"""
        if not (isinstance(workspace_ref, str) and REF_RE["workspace"].match(workspace_ref)):
            raise ValueError("不正な workspace です")
        if not (isinstance(surface_ref, str) and REF_RE["surface"].match(surface_ref)):
            raise ValueError("不正な surface です")
        return self.focus_tab("%s/%s" % (workspace_ref, surface_ref))

    # ------------------------------------------------------------ エディタで開く

    def open_editor(self, item_id, path):
        """案件の開き先の 1 つをエディタで開く。開き先はサーバが決めたものだけ (検証してから実行)。"""
        it = self.find_item(item_id)
        if not it:
            raise KeyError(NO_ITEM)
        with self.lock:
            facts = self.facts or {}
        if path not in [t["path"] for t in it.get("open_targets") or []]:
            raise ValueError("この作業の開き先ではありません。")
        cwds = [s.get("cwd_last") for s in facts.get("sessions") or []]
        cwds += [(s.get("live") or {}).get("cwd") for s in facts.get("sessions") or []]
        allowed = ed.allowed_dirs(self.targets["repos"], facts.get("repos") or {}, [c for c in cwds if c])
        allowed.update(os.path.realpath(s.get("cwd")) for s in it.get("sessions") or [] if s.get("cwd"))
        real = ed.validate_target(path, allowed)
        name = self.settings.get("editor") or "cursor"
        cmd = ed.editor_command(name, self.settings.get("editor_args") or [], real, ed.resolve_editor(name))
        if not cmd:
            raise ValueError("エディタ \"%s\" が見つかりません。settings.json の editor にコマンド名か絶対パスを書いてください"
                             % name)
        try:
            ed.launch(cmd)
        except ValueError as e:
            if cmd[0] == "open":
                raise ValueError("エディタ \"%s\" が PATH に無く、open -a %s でも開けませんでした。settings.json の editor に"
                                 "絶対パスを書いてください (%s)" % (name, cmd[2], e))
            raise
        return {"ok": True, "path": real, "editor": ed.display_name(name), "via_open": cmd[0] == "open"}

    # ------------------------------------------------------------ 片付ける

    def refresh_later(self):
        threading.Thread(target=self.refresh, kwargs={"force_gh": False, "run_llm": False}, daemon=True).start()

    def _cleanup_plan(self, it):
        projects = os.path.join(self.claude_dir, "projects")
        sids = [s.get("session_id") for s in it.get("sessions") or []]
        paths = {sid: cl.session_paths(projects, sid) for sid in sids if cl.valid_session_id(sid)}
        live = cl.live_session_ids(os.path.join(self.claude_dir, "sessions"))
        shared = {}
        with self.lock:
            # チケットの作業は中の会話を並べているだけなので、ほかの作業と共有しているとは数えない
            others = [o for o in self.result.get("items", []) if o["id"] != it["id"] and o.get("kind") != "ticket"]
        for o in others:
            for s in o.get("sessions") or []:
                if s.get("session_id") in paths:
                    shared.setdefault(s["session_id"], []).append(o.get("title") or o["id"])
        if not hasattr(self, "_own_tty"):
            self._own_tty = cl.own_tty(default_runner)
        return cl.build_plan(it, paths, live, bool(self.terminal.capabilities.get("close")), shared,
                             own_tty=self._own_tty)

    def cleanup(self, item_id, dry_run=True):
        """作業をゴミ箱へ移す (旧名: 片付ける)。dry_run なら何をするかだけを返す。ブランチと worktree には触らない。"""
        it = self.find_item(item_id)
        if not it:
            raise KeyError(NO_ITEM)
        if it.get("kind") == "ticket":
            raise ValueError("チケットはゴミ箱に移せません。中の会話ごとに、ゴミ箱に移してください。")
        plan = self._cleanup_plan(it)
        if dry_run:
            return {"dry_run": True, "plan": plan}
        # 1. 隠す (残りがあっても再び出てこないように、上書きで隠す)
        self.set_override(item_id, {"hidden": True})
        # 2. タブを閉じる
        tabs = []
        closed_sids = set()
        for t in plan["tabs"]:
            try:
                r = self.terminal.close(t)
            except (ValueError, OSError) as e:
                r = {"ok": False, "warning": None, "error": str(e)}
            tabs.append({"id": t["id"], "title": t["title"], "group": t["group"], "ok": bool(r.get("ok")),
                         "warning": r.get("warning"), "error": r.get("error")})
            if r.get("ok") and t.get("session_id"):
                closed_sids.add(t["session_id"])
        # 3. セッションの記録をゴミ箱へ (閉じたタブの Claude は終了を少し待つ)
        sessions_dir = os.path.join(self.claude_dir, "sessions")
        projects = os.path.join(self.claude_dir, "projects")
        waiting = {s["session_id"] for s in plan["sessions"] if s["action"] == "after_close"}
        deadline = time.time() + float(self.cleanup_wait_seconds)
        live = cl.live_session_ids(sessions_dir)
        while waiting & set(live) and time.time() < deadline:
            time.sleep(0.2)
            live = cl.live_session_ids(sessions_dir)
        sessions = []
        for s in plan["sessions"]:
            out = {"session_id": s["session_id"], "trashed": [], "skipped": None, "planned_skip": s["action"] == "skip",
                   "errors": []}
            sessions.append(out)
            if s["action"] == "skip":
                out["skipped"] = s["reason"]
                continue
            if s["session_id"] in live:
                out["skipped"] = ("タブを閉じた後も Claude が動いているため、残しました。" if s["action"] == "after_close"
                                  else "Claude が動き始めたため、残しました。")
                continue
            for path in s["paths"]:
                # 計画を作った後に置き換えられていないか、移す直前にもう一度確かめる
                if not cl.safe_session_path(path, projects, s["session_id"]):
                    out["errors"].append("場所を確かめられないため、ゴミ箱に移しませんでした: " + path)
                    continue
                try:
                    out["trashed"].append(cl.move_to_trash(path, self.trash_dir, self.trash_runner))
                except OSError as e:
                    out["errors"].append("ゴミ箱への移動に失敗しました: %s (%s)" % (path, e.strerror or e))
        ok = all(t["ok"] for t in tabs) and all(
            not x["errors"] and (x["planned_skip"] or not x["skipped"]) for x in sessions)
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "item_id": item_id,
            "title": it.get("title"),
            "keys": it.get("keys"),
            "hidden": True,
            "tabs_closed": [t for t in tabs if t["ok"]],
            "tabs_failed": [t for t in tabs if not t["ok"]],
            "sessions": sessions,
        }
        log = cl.append_log(self.data_dir, record)
        self.refresh_later()
        return {"dry_run": False, "ok": ok, "plan": plan, "hidden": True, "tabs": tabs, "sessions": sessions,
                "log": log}

    # ------------------------------------------------------------ 作業報告

    def report_view(self, date):
        """保存済みの作業報告 (無ければ texts は None) と生成状況。"""
        rp.parse_date(date)
        rec = rp.load_report(self.data_dir, date)
        with self.lock:
            job = dict(self.report_jobs.get(date) or {})
        out = {
            "date": date,
            "today": rp.today().isoformat(),
            "running": bool(job.get("running")),
            "stage": job.get("stage"),
            "started_at": job.get("started_at"),
            "error": job.get("error"),
            "texts": None,
            "full": None,
            "generated_at": None,
            "seconds": None,
            "model": self._report_model(),
        }
        if rec:
            texts = rp.render_texts(rec["result"])
            out.update(texts=texts, full=rp.render_full(texts), generated_at=rec.get("generated_at"),
                       seconds=rec.get("seconds"), llm=rec.get("llm") or {})
        return out

    def start_report(self, date, background=True):
        """作業報告の生成を始める。すでに生成中なら False。"""
        d = rp.parse_date(date)
        if d > rp.today():
            raise ValueError("未来の日付は、指定できません。")
        if not self.settings.get("llm_enabled", True):
            raise ValueError("要約がオフのときは、作業報告を作成できません (settings.json の llm_enabled)。")
        with self.lock:
            job = self.report_jobs.get(date)
            if job and job.get("running"):
                return False
            self.report_jobs[date] = {"running": True, "stage": "事実の収集", "started_at": time.time(), "error": None}
        if background:
            threading.Thread(target=self._generate_report, args=(date,), daemon=True).start()
        else:
            self._generate_report(date)
        return True

    def _report_stage(self, date, stage):
        with self.lock:
            self.report_jobs.setdefault(date, {})["stage"] = stage

    def _generate_report(self, date):
        try:
            facts = self._report_facts(date)
            text = rp.facts_text(facts, int(self.settings.get("report_input_chars", rp.DEFAULT_MAX_CHARS)))
            self._report_stage(date, "%sで整理中" % model_label(self._report_model()))
            t0 = time.time()
            stdout = self.report_llm(rp.build_report_prompt(date, text))
            result = rp.parse_report_output(stdout)
            if result is None:
                raise ValueError("LLM の出力を作業報告として読み取れませんでした。［作り直す］で、やり直してください。")
            seconds = round(time.time() - t0, 1)
            rp.save_report(self.data_dir, date, rp.facts_hash(text), result, seconds, time.time(),
                           rp.envelope_meta(stdout))
            with self.lock:
                self.report_jobs[date].update(error=None)
        except Exception as e:  # noqa: BLE001 - 失敗は画面に出す
            traceback.print_exc()
            with self.lock:
                self.report_jobs.setdefault(date, {})["error"] = str(e) or e.__class__.__name__
        finally:
            with self.lock:
                self.report_jobs.setdefault(date, {}).update(running=False, stage=None, finished_at=time.time())

    def report_llm(self, prompt):
        """要約と同じ方法 (本物の claude、消毒した環境、フック・セッション記録なし) で 1 回だけ実行する。"""
        timeout = int(self.settings.get("report_timeout_seconds", 300))
        return run_claude(prompt, self.settings, os.path.join(self.data_dir, "llm-work"),
                          resolve_claude_path(self.settings.get("llm_claude_path")),
                          system_prompt=rp.REPORT_SYSTEM_PROMPT, timeout=timeout, model=self._report_model())

    def _report_model(self):
        """作業報告に使うモデル。要約 (llm_model) とは別に設定する。"""
        return self.settings.get("report_model") or self.settings.get("llm_model", "sonnet")

    def _report_facts(self, date):
        """その日の事実 (全セッション、PR、コミット、進行中の案件) を集める。"""
        if self.facts is None and not self.refresh(force_gh=False, run_llm=False):
            with self.refresh_lock:  # 実行中の収集を待つ
                pass
        start, end = rp.day_bounds(rp.parse_date(date))
        with self.lock:
            facts = self.facts or {}
            items = list(self.result.get("items", []))
        repo_paths = self.targets["repos"]
        worktrees = {n: r.get("worktrees") or [] for n, r in (facts.get("repos") or {}).items()}
        sessions = collectors.scan_day_sessions(os.path.join(collectors.CLAUDE_DIR, "projects"), start, end,
                                                exclude=[os.path.join(self.data_dir, "llm-work")],
                                                aliases=self.targets.get("path_aliases") or [])
        roots = {}
        known_prs = set()
        prs = rp.prs_for_day(facts.get("prs") or [], start, end, self.classifier)
        for p in prs:
            known_prs.add((p["repo"], p["number"]))
        for s in sessions:
            root = None
            if not resolve_path(s.get("cwd"), repo_paths, worktrees):
                root = collectors.git_common_root(s.get("cwd"), roots)
            s["project"] = rp.project_name(s.get("cwd"), s.get("files"), s.get("branch"), self.classifier,
                                           repo_paths, worktrees, root)
            for url in s.get("prs_created") or []:
                m = rp.PR_URL_RE.search(url)
                if m and (m.group(2), int(m.group(3))) not in known_prs:
                    known_prs.add((m.group(2), int(m.group(3))))
                    prs.append({"repo": m.group(2), "number": int(m.group(3)), "title": None, "state": None,
                                "events": ["作成"], "project": s["project"]})
        commits = collectors.collect_commits(repo_paths, start, end, self.classifier)
        return rp.build_facts(date, sessions, prs, commits,
                              rp.active_items(items, end, other=self.classifier.other_label))


def _session_store(sid, s):
    snap = {k: s.get(k) for k in ("n_user", "last_ts", "mtime")}

    def store(d, result, at):
        d.setdefault("sessions", {})[sid] = summary_cache.session_record(result, snap, at)

    return store


def _ticket_store(ticket_id, members, to_sid):
    """チケットのまとめを保存する。links と段の session に入っているラベル (S1 など) を会話の ID に戻す。"""
    def store(d, result, at):
        result = dict(result)
        result["links"] = [dict(ln, **{"from": to_sid[ln["from"]], "to": to_sid[ln["to"]]})
                           for ln in result.get("links") or [] if ln["from"] in to_sid and ln["to"] in to_sid]
        steps = []
        for st in result.get("flow_steps") or []:
            st = dict(st)
            st["session"] = to_sid.get(st.get("session"))
            steps.append(st)
        result["flow_steps"] = steps if result.get("flow_steps") is not None else None
        d.setdefault("items", {})[ticket_id] = summary_cache.item_record(result, members, at)

    return store


def _text(v, n):
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("文字列で指定してください。")
    return v[:n]


class Handler(BaseHTTPRequestHandler):
    board = None
    port = None
    server_version = "session-board"
    sys_version = ""

    def log_message(self, fmt, *args):  # noqa: D401 - 標準出力を汚さない
        if os.environ.get("SESSION_BOARD_DEBUG"):
            super().log_message(fmt, *args)

    def _allowed_hosts(self):
        return {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}

    def _host_ok(self):
        # DNS リバインディング対策: Host ヘッダがループバック以外なら拒否
        return self.headers.get("Host", "") in self._allowed_hosts()

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        extra = dict(extra or {})
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", extra.pop("Content-Security-Policy", CSP))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        url = urlsplit(self.path)
        path = url.path
        if path == "/api/state":
            return self._send(200, self.board.state())
        m = CLEANUP_RE.match(path)
        if m:
            try:
                return self._send(200, self.board.cleanup(m.group(1), dry_run=True))
            except KeyError as e:
                return self._send(404, {"error": str(e.args[0]) if e.args else "not found"})
        if path == "/api/report":
            date = (parse_qs(url.query).get("date") or [rp.today().isoformat()])[0]
            try:
                return self._send(200, self.board.report_view(date))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        m = DIGEST_RE.match(path)
        if m:
            page = self.board.digest_page(m.group(1))
            if page is None:
                return self._send(404, "この会話のまとめは、まだありません。".encode("utf-8"),
                                  "text/plain; charset=utf-8")
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8",
                              {"Content-Security-Policy": DIGEST_CSP})
        if path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                return self._send(200, f.read(), ctype)
        return self._send(404, {"error": "not found"})

    def _post_ok(self):
        if not self._host_ok():
            return "forbidden host"
        if self.headers.get("X-Board") != "1":
            return "missing X-Board header"
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {"http://" + h for h in self._allowed_hosts()}:
            return "forbidden origin"
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return "forbidden fetch site"
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return "content-type must be application/json"
        return None

    def do_POST(self):
        err = self._post_ok()
        if err:
            return self._send(403, {"error": err})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return self._send(413, {"error": "body too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("JSON object expected")
        except ValueError:
            return self._send(400, {"error": "invalid json"})
        b = self.board
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/refresh":
                llm = bool(body.get("llm", True))
                threading.Thread(target=b.refresh, kwargs={"force_gh": True, "run_llm": llm}, daemon=True).start()
                return self._send(202, {"ok": True})
            if path == "/api/override":
                b.set_override(body.get("item_id"), body.get("patch") or {})
                return self._send(200, {"ok": True, "version": b.version})
            if path == "/api/columns/label":
                label = b.set_column_label(body.get("name"), body.get("label"))
                return self._send(200, {"ok": True, "label": label})
            if path == "/api/tickets/create":
                tid = b.create_ticket(body.get("name"), body.get("session_ids") or [], body.get("target"))
                return self._send(200, {"ok": True, "ticket_id": tid})
            if path == "/api/tickets/add":
                b.add_to_ticket(body.get("ticket_id"), body.get("session_id"))
                return self._send(200, {"ok": True})
            if path == "/api/tickets/remove":
                b.remove_from_ticket(body.get("ticket_id"), body.get("session_id"))
                return self._send(200, {"ok": True})
            if path == "/api/tickets/update":
                b.update_ticket(body.get("ticket_id"), body.get("patch") or {})
                return self._send(200, {"ok": True})
            if path == "/api/tickets/merge":
                n = b.merge_ticket(body.get("ticket_id"), body.get("into"))
                return self._send(200, {"ok": True, "moved": n})
            if path == "/api/tickets/delete":
                sids = b.delete_ticket(body.get("ticket_id"))
                return self._send(200, {"ok": True, "session_ids": sids})
            if path == "/api/summarize":
                n = b.summarize_item(body.get("item_id"))
                return self._send(200, {"ok": True, "queued": n})
            if path == "/api/ask":
                answer = b.ask(body.get("item_id"), body.get("session_id"), body.get("question"), body.get("history"))
                return self._send(200, {"ok": True, "answer": answer})
            if path == "/api/report/generate":
                date = body.get("date") or rp.today().isoformat()
                started = b.start_report(date)
                return self._send(202, {"ok": True, "started": started})
            m = CLEANUP_RE.match(path)
            if m:
                dry = body.get("dry_run", False)
                if not isinstance(dry, bool):
                    raise ValueError("dry_run は true か false で指定してください。")
                return self._send(200, b.cleanup(m.group(1), dry_run=dry))
            if path == "/api/open-editor":
                return self._send(200, b.open_editor(body.get("item_id"), body.get("path")))
            if path == "/api/focus":
                if "tab_id" in body:
                    errors = b.focus_tab(body.get("tab_id"))
                else:
                    errors = b.focus(body.get("workspace_ref"), body.get("surface_ref"))
                return self._send(200, {"ok": not errors, "errors": errors})
        except KeyError as e:
            return self._send(404, {"error": str(e.args[0]) if e.args else "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})


COMMANDS = ("start", "init", "check", "version")


def _print_errors(title, errors):
    print("session-board: " + title, file=sys.stderr)
    for e in errors:
        print("  - " + e, file=sys.stderr)


def _load_config(cdir):
    """設定を読んで検証する。誤りがあれば日本語の一覧を出して終了する (終了コード 2)。"""
    warnings = []
    try:
        settings = load_settings(cdir, warnings=warnings)
        targets = load_targets(cdir, warnings=warnings)
    except ConfigError as e:
        _print_errors("設定に誤りがあります。直してから起動し直してください。", e.errors)
        sys.exit(2)
    if not ed.resolve_editor(settings.get("editor")):
        warnings.append("エディタ \"%s\" が見つかりません (settings.json の editor)。「エディタで開く」は open -a で"
                        "開こうとします" % settings.get("editor"))
    for w in warnings:
        print("session-board: 注意: " + w, file=sys.stderr)
    return settings, targets


def cmd_init(cdir):
    created = cfg.init_config(cdir)
    for path in created:
        print("作成しました: " + path)
    if not created:
        print("既に設定があります (上書きしません): " + cdir)
    print("次に %s を自分のリポジトリと列に書き換えてください。" % os.path.join(cdir, "targets.json"))


def cmd_check(cdir):
    settings, targets = _load_config(cdir)
    if not targets.get("configured", True):
        print(targets.get("message"))
        sys.exit(1)
    print("設定に問題ありません: %s (リポジトリ %d 件、列 %d 件 + %s)" % (
        cdir, len(targets["repos"]), len(targets["projects"]), targets["other_label"]))
    for name, path in targets["repos"].items():
        if not os.path.isdir(path):
            print("  注意: repos.%s のクローンが見つかりません: %s" % (name, path))
    for old, new in targets.get("path_aliases") or []:
        print("  記録の %s を %s として読みます (path_aliases)" % (old, new))


def main(argv=None):
    import argparse

    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv.pop(0) if argv and argv[0] in COMMANDS else "start"
    parser = argparse.ArgumentParser(
        prog="session-board", description="Claude Code の会話を作業単位で表示するローカルボード",
        epilog="サブコマンド: start (既定) / init (例の設定をコピー) / check (設定を確かめる) / version")
    if command == "start":
        parser.add_argument("--port", type=int, help="待ち受けポート (既定: settings.json)")
        parser.add_argument("--no-llm", action="store_true", help="起動時に LLM 要約を実行しない")
    args = parser.parse_args(argv)

    for old, new in cfg.migrate_old_dirs():
        print("session-board: 前の名前の置き場所 %s を %s へ移しました" % (old, new), file=sys.stderr)
    cdir = cfg.config_dir()
    if command == "version":
        print(cfg.version())
        return
    if command == "init":
        cmd_init(cdir)
        return
    if command == "check":
        cmd_check(cdir)
        return

    settings, targets = _load_config(cdir)
    if args.port:
        settings["port"] = args.port
    if args.no_llm:
        settings["llm_on_startup"] = False
    if not targets.get("configured", True):
        print("session-board: " + targets["message"], file=sys.stderr)
    data = cfg.data_dir()
    board = Board(settings=settings, targets=targets, data_dir=data, config_dir=cdir)
    Handler.board = board
    Handler.port = settings["port"]
    httpd = ThreadingHTTPServer(("127.0.0.1", settings["port"]), Handler)
    httpd.daemon_threads = True
    print("session-board %s: http://127.0.0.1:%d/ (設定 %s / データ %s)" % (cfg.version(), settings["port"], cdir, data),
          flush=True)
    threading.Thread(
        target=board.refresh,
        kwargs={"force_gh": True, "run_llm": bool(settings.get("llm_on_startup"))},
        daemon=True,
    ).start()
    threading.Thread(target=board.auto_refresh_loop, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
