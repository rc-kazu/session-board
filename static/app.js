"use strict";

// session-board の画面。設計は docs/ui-spec.md。
// 信頼できないデータ (タイトル・依頼文・PR タイトル・タブ名・メモ・LLM の出力) は必ず
// textContent / createElement / setAttribute で入れる。HTML 文字列を流し込む API は使わない。SVG も createElementNS で組む。
//
// 前半は表示ルールの純粋関数 (DOM を触らない)。Node からも読めるようにして tests/js/rules.test.js で確かめる。
// 後半 (boot 以降) がブラウザでの描画と操作。

// ================================================================== 表示ルール

const DONE_PHASES = ["マージ済み", "デプロイ済み", "完了"];
// 自分の番 (#1〜#5) の順。最上段の並びにも使う
const MINE_ORDER = ["input", "question", "fix", "merge", "user"];
const BALL_ORDER = MINE_ORDER.concat(["claude", "review", "stop"]);
const STALE_SUMMARY_TIP = "(要約時点) 要約の後にも動きがあります。いまも必要か確認してください。";
const PR_STATE_LABEL = { OPEN: "オープン", MERGED: "マージ済み", CLOSED: "クローズ" };
const KIND_LABEL = { "app": "アプリ", "infra-tf": "tf", "infra-k8s": "k8s" };
const LANE_LABEL = { app: "アプリ", infra: "インフラ" };
const TRACK = {
  "調査": [0, "調査"],
  "実装中": [1, "実装"],
  "レビュー待ち": [2, "レビュー"],
  "マージ済み": [3, "マージ済み"],
  "デプロイ済み": [4, "完了"],
  "完了": [4, "完了"],
};
const MACHINE_NOW = {
  input: "Claude があなたの指示を待っています",
  question: "Claude が質問しています",
  fix: "PR に修正依頼が来ています",
  merge: "PR が承認されました。マージできます",
};

function isDone(it) {
  return DONE_PHASES.includes(it.phase);
}

function parseTs(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

function openPrs(it) {
  return (it.prs || []).filter((p) => p.state === "OPEN");
}

function changesPr(it) {
  return openPrs(it).find((p) => p.reviewDecision === "CHANGES_REQUESTED") || null;
}

function approvedPr(it) {
  const open = openPrs(it);
  if (open.some((p) => p.reviewDecision === "CHANGES_REQUESTED")) return null;
  return open.find((p) => p.reviewDecision === "APPROVED") || null;
}

function isBusy(it) {
  return (it.sessions || []).some((s) => s.live && String(s.live.status || "").toLowerCase() === "busy");
}

function reviewWaiting(it) {
  return openPrs(it).some((p) => !p.isDraft && (p.reviewDecision === "REVIEW_REQUIRED" || !p.reviewDecision));
}

function daysSince(iso, now) {
  const t = parseTs(iso);
  return t === null ? null : Math.max(0, Math.floor((now - t) / 86400000));
}

// 表示中の要約が最終活動より前に作られたか (要約の後に動きがあった)
function summaryIsStale(it) {
  if (typeof it.summarized_at !== "number" || !it.summarized_at) return false;
  const t = parseTs(it.last_activity);
  return t !== null && it.summarized_at * 1000 < t;
}

function makeBall(key, label, cls, extra) {
  return Object.assign({
    key, label, cls, mine: MINE_ORDER.includes(key), rank: BALL_ORDER.indexOf(key), staleSummary: false, tip: null,
  }, extra || {});
}

// 3.1 ボール = 次に動くのは誰か。上から順に最初に当たったものを 1 つだけ返す。無ければ null
function ballFor(it, now) {
  if (!it || it.hidden) return null;
  if (it.kind === "ticket") return ticketBall(it, now);
  // 手で完了の区分にした作業には、次に動く人を出さない (タブが入力待ちのままでも、利用者の中では終わっている)
  if (isManualClosed(it)) return null;
  const done = isDone(it);
  if (it.needs_input) return makeBall("input", "指示待ち", "ball--mine");
  if (!done && it.blocked_question) return makeBall("question", "質問に回答", "ball--mine", { tip: it.blocked_question });
  if (!done && it.changes_requested) return makeBall("fix", "直す", "ball--mine");
  if (approvedPr(it)) return makeBall("merge", "マージする", "ball--mine");
  // LLM の ball は機械で分からない「Claude Code の外の作業」を拾うためだけに使う。完了の作業では無視する
  if (!done && it.ball === "user") {
    const stale = summaryIsStale(it);
    return makeBall("user", it.ball_reason || "自分で対応", "ball--mine", {
      staleSummary: stale,
      tip: stale ? STALE_SUMMARY_TIP : "要約 (LLM) の判断: Claude Code の外で、自分が手を動かす段階です。",
    });
  }
  if (isBusy(it)) return makeBall("claude", "Claude 実行中", "ball--claude");
  if (reviewWaiting(it)) return makeBall("review", "レビュアー待ち", "ball--review");
  if (it.stale) {
    const n = daysSince(it.last_activity, now);
    return makeBall("stop", (n === null ? "?" : n) + "日 動きなし", "ball--stop", { days: n });
  }
  return null;
}

// チケットのボール: 中の会話のボールのうち、いちばん急ぐもの。
// 動きなしはチケット全体の最後の動きで決める。チケットが終わっていれば、質問と「自分で対応」は出さない
function ticketBall(it, now) {
  const done = isDone(it);
  let best = null;
  for (const m of it.members || []) {
    const b = ballFor(Object.assign({}, m, { hidden: false }), now);
    if (!b || b.key === "stop") continue;
    if (done && (b.key === "question" || b.key === "user")) continue;
    if (!best || b.rank < best.rank) best = b;
  }
  if (best) return best;
  if (it.stale) {
    const n = daysSince(it.last_activity, now);
    return makeBall("stop", (n === null ? "?" : n) + "日 動きなし", "ball--stop", { days: n });
  }
  return null;
}

// チケットに入れる: ドラッグした (か「チケットに入れる」を押した) 会話の作業 src を、dst に落としたときにすること。
// 会話どうしなら新しいチケットを作って 2 つを入れ (名前は落とした先の会話のタイトル)、チケットならそこに入れる。できなければ null
function dropAction(src, dst) {
  if (!src || !dst || src.id === dst.id) return null;
  // チケットはチケットにだけ落とせる。落とした先に取り込み、落としたチケットは削除する (確認してから)
  if (src.kind === "ticket") {
    if (dst.kind !== "ticket") return null;
    const n = (src.members || []).length;
    return { path: "/api/tickets/merge", body: { ticket_id: src.id, into: dst.id }, confirm: true,
      message: "「" + clipText(src.title, 40) + "」を「" + clipText(dst.title, 40) + "」に取り込みました。" + (n ? "会話 " + n + " 件を移しました。" : "") };
  }
  // チケットの中の会話には落とせない (チケットのほうに落とす)
  if (dst.kind !== "ticket" && dst.ticket_id) return null;
  const sid = ((src.sessions || [])[0] || {}).session_id;
  if (!sid) return null;
  if (dst.kind === "ticket") {
    if (src.ticket_id === dst.id) return null;
    return { path: "/api/tickets/add", body: { ticket_id: dst.id, session_id: sid },
      message: "「" + clipText(dst.title, 40) + "」に入れました。" };
  }
  const dsid = ((dst.sessions || [])[0] || {}).session_id;
  if (!dsid) return null;
  return { path: "/api/tickets/create", body: { name: dst.title, session_ids: [dsid, sid] },
    message: "チケット「" + clipText(dst.title, 40) + "」を作成し、会話を 2 件入れました。" };
}

// チケットの詳細パネルの会話の一覧で、会話ごとに出す一言
function memberLine(m) {
  const s = (m.sessions || [])[0] || {};
  return m.now_short || (s.summary && s.summary.title) || clipText(s.first_prompt || "", 120) || "";
}

// 3.2 最上段: 自分の番の作業を理由の順、同じ理由の中は activity_ts 降順 (同時刻はサーバの順)
function mineList(items, now) {
  return items
    .map((it, i) => ({ item: it, ball: ballFor(it, now), i }))
    .filter((x) => x.ball && x.ball.mine)
    .sort((a, b) => (a.ball.rank - b.ball.rank)
      || ((parseTs(b.item.activity_ts || b.item.last_activity) || 0) - (parseTs(a.item.activity_ts || a.item.last_activity) || 0))
      || (a.i - b.i));
}

// 3.3 5 つの点 (調査 / 実装 / レビュー / マージ / 完了) とフェーズ名
function trackFor(it) {
  const t = TRACK[it.phase];
  if (!t) return { dots: ["", "", "", "", ""], other: true, stage: it.phase || "その他", label: it.phase || "その他" };
  const [idx, name] = t;
  const stage = it.phase === "レビュー待ち" && approvedPr(it) ? "承認済み" : name;
  return {
    dots: [0, 1, 2, 3, 4].map((i) => (i < idx ? "d" : i === idx ? "n" : "")),
    other: false,
    stage,
    label: "5 段階中 " + (idx + 1) + " " + stage,
  };
}

// 3.4 列幅: 列ごとの layout (targets.json) が auto なら進行中が threshold 件 (settings の column_wide_min_items、
// 既定 8) 以上で重み 2 の 2 段組み。wide は常に 2 段組み、normal は常に 1 段
const DEFAULT_WIDE_MIN_ITEMS = 8;

function isWideColumn(openCount, layout, threshold) {
  if (layout === "wide") return true;
  if (layout === "normal") return false;
  return openCount >= (threshold || DEFAULT_WIDE_MIN_ITEMS);
}

function columnTemplate(openCounts, layouts, threshold) {
  return openCounts.map((n, i) => (isWideColumn(n, (layouts || [])[i], threshold)
    ? "minmax(560px, 2fr)" : "minmax(300px, 1fr)")).join(" ");
}

// 列の色 (targets.json の color)。#rgb / #rrggbb 以外は使わない
function safeColor(c) {
  return typeof c === "string" && /^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$/.test(c) ? c : null;
}

// 3.5 作業なしのタブ: cwd の末尾 1 段を名前に (同名が並ぶときは 2 段)
function looseTabNames(tabs) {
  const segs = (t) => String(t.cwd || "").split("/").filter(Boolean);
  const last = (t, n) => segs(t).slice(-n).join("/");
  const count = new Map();
  for (const t of tabs) {
    const k = last(t, 1);
    if (k) count.set(k, (count.get(k) || 0) + 1);
  }
  return tabs.map((t) => {
    const s = segs(t);
    const one = last(t, 1);
    const title = String(t.title || "").trim();
    const name = one ? (count.get(one) > 1 && s.length > 1 ? last(t, 2) : one) : (title || t.surface_ref || t.id || "(不明)");
    const cwd = t.cwd || "";
    return {
      tab: t,
      name,
      path: s.length > 3 ? "…/" + s.slice(-3).join("/") : (cwd || title),
      kind: t.session_id || /^[\u2733\u273b\u2736\u2722\u00b7*\u2800-\u28ff]/.test(title) ? "claude" : "shell",
      worktree: /\/(\.claude\/worktrees|\.worktrees)\//.test(cwd + "/"),
      place: tabPlace(t),
    };
  });
}

// 同じパスのタブを見分けるための位置 (cmux のときだけ)。ペインが 1 つなら何番目のタブかだけ
function tabPlace(t) {
  const n = Number(t && t.tab_index);
  if (!(n >= 1)) return null;
  const p = Number(t.pane_index);
  return (Number(t.pane_count) > 1 && p >= 1 ? "ペイン " + p + " の " : "") + n + " 番目のタブ";
}

function clipText(s, n) {
  s = String(s || "").trim();
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// 依頼文などを 1 文に: 最初の空でない行の、最初の文
function firstSentence(text, max) {
  const line = String(text || "").replace(/\r/g, "").split("\n").map((x) => x.trim()).find(Boolean) || "";
  const m = line.match(/^(.+?[。！？!?])/);
  return clipText(m ? m[1] : line, max || 60);
}

function pad2(n) {
  return String(n).padStart(2, "0");
}

function fmtShort(iso) {
  const t = parseTs(iso);
  if (t === null) return "";
  const d = new Date(t);
  return (d.getMonth() + 1) + "/" + d.getDate() + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

function safeGithubUrl(url) {
  return typeof url === "string" && /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/\d+$/.test(url)
    ? url : null;
}

function prStateText(p) {
  if (p.state === "OPEN") {
    if (p.reviewDecision === "CHANGES_REQUESTED") return "修正依頼";
    if (p.reviewDecision === "APPROVED") return "承認済み";
    return p.isDraft ? "ドラフト" : "オープン";
  }
  return PR_STATE_LABEL[p.state] || "不明";
}

function prLaneKind(p) {
  return p.label === "アプリ PR" ? "app" : p.label === "インフラ PR" ? "infra" : null;
}

// PR の label が分からないとき: 同じリポジトリの PR の label、無ければ作業の種別が 1 系統ならそれ
function guessLane(it, repo) {
  for (const p of it.prs || []) {
    if (p.repo === repo && prLaneKind(p)) return prLaneKind(p);
  }
  const kinds = it.kinds || [];
  if (kinds.length && kinds.every((k) => k === "app")) return "app";
  if (kinds.length && kinds.every((k) => k.startsWith("infra"))) return "infra";
  return null;
}

// 箱を押したときに出す中身: full (切る前の文) / detail (要約の説明) / noDetail (説明が入る前の要約) /
// facts ([見出し, 値] の並び) / substeps (レーンにまとめた段) / href (PR を開くリンク)
function flowNode(o) {
  return Object.assign({
    type: "node", kind: "", text: "", small: "", meta: "", metaMine: false, state: "plain",
    mine: false, here: false, href: null, title: null,
    full: null, detail: null, noDetail: false, facts: [], substeps: [],
  }, o);
}

// 要約の段 (flow_steps の要素) から、押したときに出す中身を作る
function stepDetail(s) {
  return { full: s.label_full || s.label, detail: s.detail || null, noDetail: !("detail" in s) };
}

// 押して開く中身があるか。文の全文が箱の文と同じで、ほかに何も無ければ押せない箱にする
function hasFlowDetail(n) {
  return !!((n.full && n.full !== n.text) || n.detail || n.noDetail || (n.facts && n.facts.length)
    || (n.substeps && n.substeps.length) || n.href);
}

// 4.1 アプリ / インフラのレーン。PR とブランチの箱は LLM の出力を使わず prs / branches から描く
function buildLanes(it, steps) {
  const lanes = [];
  const laneFor = (kind, repo) => {
    let l = lanes.find((x) => x.kind === kind && x.repo === repo);
    if (!l) {
      l = { kind, repo, branches: [], prs: [] };
      lanes.push(l);
    }
    return l;
  };
  const prs = (it.prs || []).slice().sort((a, b) => (a.repo < b.repo ? -1 : a.repo > b.repo ? 1 : a.number - b.number));
  for (const p of prs) laneFor(prLaneKind(p) || guessLane(it, p.repo), p.repo).prs.push(p);
  for (const b of it.branches || []) {
    const p = prs.find((x) => x.repo === b.repo && x.headRefName === b.branch);
    laneFor(p ? (prLaneKind(p) || guessLane(it, p.repo)) : guessLane(it, b.repo), b.repo).branches.push(b);
  }
  const order = { app: 0, infra: 1 };
  lanes.sort((a, b) => ((order[a.kind] ?? 2) - (order[b.kind] ?? 2)) || (a.repo < b.repo ? -1 : a.repo > b.repo ? 1 : 0));
  return lanes.map((l) => {
    const laneSteps = (steps || []).filter((s) => s.state === "done" && s.lane && s.lane === l.kind && s.kind !== "依頼");
    const nodes = [];
    for (const b of l.branches) {
      nodes.push(flowNode({
        kind: "ブランチ",
        text: b.ahead ? "修正 " + b.ahead + " コミット" : b.branch,
        small: b.ahead ? b.branch : "",
        meta: b.dirty ? "未コミットあり" : "",
        state: "done",
        title: b.repo + ":" + b.branch + (b.worktree ? " (worktree)" : ""),
        facts: [
          ["ブランチ", b.repo + ":" + b.branch],
          b.ahead ? ["main との差", "main より " + b.ahead + " コミット先"] : null,
          b.dirty ? ["未コミット", "未コミットの変更があります"] : null,
          b.worktree ? ["worktree", b.worktree] : null,
        ].filter(Boolean),
      }));
    }
    for (const p of l.prs) {
      nodes.push(flowNode({
        kind: "PR", text: "PR #" + p.number, meta: prStateText(p), state: "done", href: safeGithubUrl(p.url),
        title: (p.label || "PR") + " #" + p.number + (p.title ? ": " + p.title : ""), pr: p,
        facts: [
          p.title ? ["タイトル", p.title] : null,
          ["状態", prStateText(p)],
          ["種類", (p.label || "PR") + " · " + p.repo],
        ].filter(Boolean),
      }));
    }
    if (laneSteps.length && nodes.length) {
      nodes[0].small = laneSteps.map((s) => s.label).join(" · ");
      nodes[0].substeps = laneSteps.map((s) => Object.assign({ kind: s.kind }, stepDetail(s)));
    }
    return {
      kind: l.kind,
      name: (LANE_LABEL[l.kind] ? LANE_LABEL[l.kind] + " · " : "") + l.repo,
      nodes,
      stepsUsed: laneSteps.length > 0,
    };
  });
}

// 4.1 流れ図のモデル。要素は {type:"node", ...} と {type:"lanes", lanes:[{name, nodes}]}。描画は renderFlow
function flowModel(it, ball) {
  const done = isDone(it);
  const steps = Array.isArray(it.flow_steps) ? it.flow_steps : null;
  const hasSummary = !!(steps || it.now_short || it.next_short || it.goal || it.next_action || it.summarized_at);
  const out = [];

  // 1. 依頼
  const sess = (it.sessions || []).filter((s) => s.first_prompt || s.first_ts)
    .slice().sort((a, b) => (parseTs(a.first_ts) || 0) - (parseTs(b.first_ts) || 0));
  const req = steps && steps.find((s) => s.kind === "依頼");
  if (sess.length || req) {
    const prompt = sess.length ? String(sess[0].first_prompt || "").trim() : "";
    out.push(flowNode(Object.assign({
      kind: "依頼",
      text: req ? req.label : (firstSentence(prompt) || "(依頼文なし)"),
      meta: sess.length ? fmtShort(sess[0].first_ts) : "",
      state: "done",
      title: prompt || null,
      facts: prompt ? [["最初の依頼", clipText(prompt, 1000)]] : [],
    }, req ? stepDetail(req) : { full: prompt || null })));
  }

  const lanes = buildLanes(it, steps);
  const laneKinds = new Set(lanes.filter((l) => l.stepsUsed).map((l) => l.kind));

  // 2. 調査・作業
  if (steps) {
    for (const s of steps) {
      if (s.state !== "done" || s.kind === "依頼" || (s.lane && laneKinds.has(s.lane))) continue;
      out.push(flowNode(Object.assign({ kind: s.kind, text: s.label, state: "done" }, stepDetail(s))));
    }
  } else if (hasSummary || !lanes.length) {
    const ai = (it.sessions || []).map((s) => s.ai_title).find(Boolean);
    if (ai || (!lanes.length && sess.length)) {
      out.push(flowNode({ kind: it.phase === "調査" ? "調査" : "作業", text: ai || it.title, state: "done", work: true,
        facts: [it.goal ? ["目的", it.goal] : null, it.done_so_far ? ["ここまで", it.done_so_far] : null].filter(Boolean) }));
    }
  }

  // 3. 分岐 (2 本ならレーン、1 本ならそのまま縦に)
  if (lanes.length >= 2) {
    out.push({ type: "lanes", lanes: lanes.map((l) => ({ name: l.name, nodes: l.nodes })) });
  } else if (lanes.length === 1) {
    const l = lanes[0];
    if (l.nodes.length && !l.stepsUsed && l.nodes[0].kind === "PR") l.nodes[0].small = l.name;
    out.push(...l.nodes);
  }

  // 4. 合流後: フェーズから決まる箱
  let reviewBox = null;
  if (it.phase === "レビュー待ち") {
    const open = openPrs(it).filter((p) => !p.isDraft);
    let text = open.length >= 2 ? open.length + " 本ともレビュー待ち" : "レビュー待ち";
    let mine = false;
    let meta = "レビュアー";
    if (ball && ball.key === "fix") { text = "修正依頼が来ています"; mine = true; meta = ""; }
    else if (ball && ball.key === "merge") { text = "承認済み。マージする"; mine = true; meta = ""; }
    else if (approvedPr(it)) text = "承認済み";
    reviewBox = flowNode({ kind: "レビュー", text, meta, metaMine: mine, mine, state: "plain", review: true,
      facts: openPrs(it).map((p) => [(p.label || "PR") + " #" + p.number, prStateText(p) + (p.title ? " — " + p.title : "")]) });
    out.push(reviewBox);
  } else if (it.phase === "マージ済み") {
    const merged = (it.prs || []).map((p) => p.mergedAt).filter(Boolean).sort();
    out.push(flowNode({ kind: "マージ", text: "マージ済み", meta: fmtShort(merged[merged.length - 1]), state: "done" }));
  } else if (it.phase === "デプロイ済み" || it.phase === "完了") {
    out.push(flowNode({ kind: "完了", text: it.phase, state: "done" }));
  }

  // いまの段 (LLM)。完了の作業では LLM の先の段を描かない
  if (steps && !done) {
    for (const s of steps) {
      if (s.state !== "now" || s.kind === "依頼") continue;
      if (reviewBox && s.who === "reviewer") continue; // レビューの箱と重なる
      out.push(flowNode(Object.assign({ kind: s.kind, text: s.label, mine: s.who === "user", metaMine: s.who === "user",
        nowStep: true }, stepDetail(s))));
    }
  }

  // 5. 質問 (箱には頭の 40 字。押すと全文)
  if (!done && it.blocked_question) {
    out.push(flowNode({ kind: "質問", text: clipText(it.blocked_question, 40), full: it.blocked_question,
      mine: true, metaMine: true, question: true }));
  }

  // 6. これから
  if (!done) {
    const future = steps ? steps.filter((s) => s.state === "next" || s.state === "later") : [];
    for (const s of future) {
      out.push(flowNode(Object.assign({ kind: s.kind, text: s.label, state: "future", mine: s.who === "user",
        metaMine: s.who === "user" }, stepDetail(s))));
    }
    if (!future.length && hasSummary && (it.next_short || it.next_action)) {
      const mine = !!(ball && ball.key === "user");
      out.push(flowNode({ kind: "次", text: it.next_short || firstSentence(it.next_action, 40), state: "future", mine, metaMine: mine,
        full: it.next_action || null }));
    }
  }

  // 7. 今ここは 1 つだけ: 質問 > ボールが自分の段 > PR のレビュー > 最新の作業
  const all = [];
  for (const e of out) {
    if (e.type === "lanes") for (const l of e.lanes) all.push(...l.nodes);
    else all.push(e);
  }
  let here = all.find((n) => n.question) || null;
  if (!here && ball && ball.key === "user") here = all.find((n) => n.mine && (n.nowStep || n.state === "future")) || null;
  if (!here && ball && (ball.key === "fix" || ball.key === "merge")) here = reviewBox;
  if (!here) here = reviewBox;
  if (!here) here = all.slice().reverse().find((n) => n.nowStep) || null;
  if (!here) here = all.slice().reverse().find((n) => n.state !== "future") || null;
  if (here) {
    here.here = true;
    here.state = "now";
  }
  return out;
}

// 4 いま / 次。機械で分かる自分の番 (#1〜#4) はその文を優先する (要約より新しい)
function nowNext(it, ball) {
  let now;
  if (ball && MACHINE_NOW[ball.key]) now = MACHINE_NOW[ball.key];
  else if (it.now_short) now = it.now_short;
  else if (ball) {
    now = {
      user: "自分の番: " + ball.label,
      claude: "Claude が実行しています",
      review: "レビュアーの確認を待っています",
      stop: ball.label.replace("動きなし", "動きがありません"),
    }[ball.key];
  }
  if (!now) now = isDone(it) ? it.phase + "です" : (it.phase || "") + "の段階です";
  const next = it.next_short || (it.next_action ? clipText(it.next_action, 160) : null);
  return { now, next };
}

function searchText(it) {
  const t = it.ticket || {};
  return [
    it.title, t.project, t.url, it.memo, it.next_action, it.goal, it.done_so_far, it.blocked_question, it.now_short, it.next_short,
    it.target, (it.targets || []).join(" "), (it.kinds || []).map((k) => k + " " + (KIND_LABEL[k] || "")).join(" "),
    ...(it.repo_branches || []).map((b) => b.repo + ":" + b.branch),
    ...(it.branches || []).map((b) => b.repo + ":" + b.branch),
    ...(it.prs || []).map((p) => p.repo + "#" + p.number + " " + (p.label || "") + " " + (p.title || "") + " " + (p.headRefName || "")),
    ...(it.sessions || []).map((s) => [s.ai_title, s.first_prompt, s.last_prompt, s.session_id,
      s.summary && s.summary.title].filter(Boolean).join(" ")),
    ...(it.tabs || []).map((t) => (t.workspace_title || "") + " " + (t.title || "")),
  ].filter(Boolean).join("\n").toLowerCase();
}

// 空白区切りの AND、大文字小文字は無視
function matchesSearch(it, q) {
  q = String(q || "").trim().toLowerCase();
  if (!q) return true;
  const hay = searchText(it);
  return q.split(/\s+/).every((w) => hay.includes(w));
}

function relTime(iso, now) {
  const t = parseTs(iso);
  if (t === null) return "不明";
  const sec = Math.max(0, ((now || Date.now()) - t) / 1000);
  if (sec < 60) return "たった今";
  if (sec < 3600) return Math.floor(sec / 60) + "分前";
  if (sec < 86400) return Math.floor(sec / 3600) + "時間前";
  return Math.floor(sec / 86400) + "日前";
}

// o キーや最上段の「PR を開く」で開く PR
function pickPr(it, ball) {
  const prs = it.prs || [];
  const cands = [];
  if (ball && ball.key === "fix") cands.push(changesPr(it));
  if (ball && ball.key === "merge") cands.push(approvedPr(it));
  cands.push(...openPrs(it), ...prs);
  return cands.find((p) => p && safeGithubUrl(p.url)) || null;
}

// ターミナル (settings.json の terminal)。terminal が無い旧サーバは cmux とみなす
function terminalInfo(d) {
  const t = d && d.terminal;
  if (!t || typeof t !== "object") {
    return { name: "cmux", label: "cmux", caps: { focus: true, close: true, needs_input: true }, error: null };
  }
  const c = t.capabilities || {};
  return {
    name: String(t.name || ""), label: String(t.label || t.name || ""),
    caps: { focus: c.focus === true, close: c.close === true, needs_input: c.needs_input === true },
    error: t.error || null,
  };
}

// /api/focus の要求。新しいサーバのタブは id を持つ
function focusPayload(tab) {
  if (tab && tab.id) return { tab_id: tab.id };
  return { workspace_ref: tab.workspace_ref, surface_ref: tab.surface_ref };
}

function tabTitle(t) {
  const g = (t && (t.group || t.workspace_title)) || "";
  const title = (t && t.title) || "";
  return g ? g + " / " + title : title;
}

// ゴミ箱へ: 確認ダイアログに出す行 ({key, text, items?})。plan は GET /api/items/<id>/cleanup の plan
function cleanupLines(plan, termLabel) {
  const lines = [];
  lines.push({ key: "hide", text: plan.already_hidden ? "ボードには、すでに表示していません"
    : "ボードに表示しなくなります (「非表示」から再表示できます)" });
  const kept = (plan.kept_tabs || []).map((t) => "閉じないタブ: " + tabTitle(t) + " (" + t.reason + ")");
  if (!plan.can_close) {
    lines.push({ key: "tabs", text: "タブは閉じません (" + termLabel + " では、タブを閉じられません)" });
  } else if ((plan.tabs || []).length) {
    lines.push({ key: "tabs", text: termLabel + " のタブを " + plan.tabs.length + " 個閉じます",
      items: plan.tabs.map((t) => tabTitle(t)).concat(kept) });
  } else {
    lines.push({ key: "tabs", text: "閉じるタブはありません", items: kept.length ? kept : undefined });
  }
  const sessions = plan.sessions || [];
  const moving = sessions.filter((s) => s.action !== "skip");
  const items = sessions.map((s) => {
    const id = String(s.session_id || "").slice(0, 8);
    if (s.action === "skip") return id + ": " + s.reason;
    const n = (s.paths || []).length;
    return id + ": ファイル " + n + " 個" + (s.action === "after_close" ? " (Claude が動いているため、タブを閉じて終了を確認してから移します)" : "");
  });
  lines.push({ key: "sessions", text: moving.length
    ? "会話の記録 " + moving.length + " 件を Mac のゴミ箱に移します (削除はしません。Finder のゴミ箱から元の場所に移せます)"
    : "ゴミ箱に移す会話の記録はありません", items: items.length ? items : undefined });
  lines.push({ key: "untouched", text: "ブランチと worktree は変更しません",
    items: (plan.untouched || []).length ? plan.untouched.slice() : undefined });
  return lines;
}

// ゴミ箱へ移した結果のトースト ({text, error, problems})
function cleanupResult(r) {
  const tabs = r.tabs || [];
  const sessions = r.sessions || [];
  const problems = [];
  for (const t of tabs) {
    if (!t.ok) problems.push("タブ " + (t.title || t.id || "") + ": " + (t.warning || t.error || "閉じられませんでした"));
  }
  let trashed = 0;
  let kept = 0;
  for (const s of sessions) {
    trashed += (s.trashed || []).length;
    if (s.skipped && s.planned_skip) kept += 1;
    if (s.skipped && !s.planned_skip) problems.push(String(s.session_id || "").slice(0, 8) + ": " + s.skipped);
    for (const e of s.errors || []) problems.push(e);
  }
  const parts = [];
  const closed = tabs.filter((t) => t.ok).length;
  if (closed) parts.push("タブを " + closed + " 個閉じました。");
  if (trashed) parts.push("会話の記録のファイルを " + trashed + " 個ゴミ箱に移しました。");
  if (kept) parts.push("会話の記録 " + kept + " 件は残しました。");
  const error = r.ok === false || problems.length > 0;
  const head = error ? "作業をゴミ箱に移しましたが、一部の処理に失敗しました。" : "作業をゴミ箱に移しました。";
  return { text: head + parts.join(""), error, problems };
}

// ゴミ箱へ: ツールチップに出す件数。サーバの計画 (cleanup.build_plan) と同じ決まりで、画面の状態から数える。
// 計画との違いはボード自身のタブと、記録のファイルが消えている会話だけ (確認ダイアログには計画の正確な件数が出る)
function trashCounts(it, items, caps) {
  const tabs = caps && caps.close ? (it.tabs || []).filter((t) => t && t.id) : [];
  const closing = new Set(tabs.map((t) => t.session_id).filter(Boolean));
  const others = new Set();
  for (const o of items || []) {
    if (o.id === it.id) continue;
    for (const s of o.sessions || []) others.add(s.session_id);
  }
  let records = 0;
  for (const s of it.sessions || []) {
    if (!s.session_id || others.has(s.session_id)) continue;
    if (s.live && !closing.has(s.session_id)) continue;
    records += 1;
  }
  return { tabs: tabs.length, records };
}

const TIP_DONE = "この作業を完了にして、列の下の「完了」にまとめます。タブと会話の記録はそのままです。";
const TIP_UNDONE = "完了を取り消します。フェーズは PR やブランチなどから自動で決めます。";
// PR のマージなどで自動で完了の区分 (マージ済み・デプロイ済み) になった作業を、進行中に戻す
function reopenTip(it) {
  return "フェーズが「" + it.phase + "」" + (it.phase_source && it.phase_source !== "手動" ? " (根拠: " + it.phase_source + ")" : "") +
    "なので、完了にまとめています。押すと、マージ済みの PR を除いてフェーズを自動で決め直し、進行中に戻します (git の変更・編集・要約から決まります)。";
}
const TIP_UNHIDE = "この作業をボードに再表示します。";
const TIP_KEEP_GIT = "ブランチと worktree は変更しません。";

function hideTip(hiddenAfter) {
  return "ボードに表示しないようにします。タブと会話の記録はそのままです。「非表示 (" + hiddenAfter + ")」から再表示できます。";
}

// counts が null なら件数なしの文
function trashTip(counts) {
  if (!counts) {
    return "この作業のタブを閉じ、会話の記録を Mac のゴミ箱に移します。ボードにも表示しなくなります。" + TIP_KEEP_GIT;
  }
  const { tabs, records } = counts;
  let head;
  if (tabs && records) head = "この作業のタブ " + tabs + " 個を閉じ、会話の記録 " + records + " 件を Mac のゴミ箱に移します。ボードにも表示しなくなります。";
  else if (tabs) head = "この作業のタブ " + tabs + " 個を閉じます。ゴミ箱に移す会話の記録はありません。ボードにも表示しなくなります。";
  else if (records) head = "この作業の会話の記録 " + records + " 件を Mac のゴミ箱に移します。ボードにも表示しなくなります。";
  else head = "この作業をボードに表示しないようにします。閉じるタブと、ゴミ箱に移す会話の記録はありません。";
  return head + TIP_KEEP_GIT;
}

function isManualClosed(it) {
  return it.phase_source === "手動" && isDone(it);
}
function isManualDone(it) {
  return it.phase === "完了" && it.phase_source === "手動";
}

// 各作業の右上のボタン (完了 / 非表示 / ゴミ箱へ の順)。env: {items, caps, hiddenCount}
function itemActions(it, env) {
  env = env || {};
  const acts = [];
  if (isManualDone(it)) acts.push({ key: "undone", label: "完了を取り消す", icon: "undo", tip: TIP_UNDONE });
  else if (isDone(it)) acts.push({ key: "reopen", label: "進行中に戻す", icon: "undo", tip: reopenTip(it) });
  else acts.push({ key: "done", label: "完了", icon: "check", tip: TIP_DONE });
  acts.push(it.hidden
    ? { key: "unhide", label: "再表示", icon: "eye", tip: TIP_UNHIDE }
    : { key: "hide", label: "非表示", icon: "eyeOff", tip: hideTip((env.hiddenCount || 0) + 1) });
  // チケットはゴミ箱に移さない (中の会話ごとに移す)
  if (it.kind !== "ticket") {
    acts.push({ key: "trash", label: "ゴミ箱へ", icon: "trash",
      tip: trashTip(env.items ? trashCounts(it, env.items, env.caps) : null) });
  }
  return acts;
}

// 完了・非表示の上書きと、取り消し用の上書きとトーストの文
function actionPatch(it, key) {
  const prevPhase = it.phase_source === "手動" ? it.phase : "";
  switch (key) {
    case "done": return { patch: { phase: "完了" }, undo: { phase: prevPhase }, message: "完了にしました。" };
    case "undone": return { patch: { phase: "" }, undo: { phase: "完了" }, message: "完了を取り消しました。" };
    case "reopen": return { patch: { reopened: true }, undo: { reopened: false }, message: "進行中に戻しました。フェーズは、マージ済みの PR を除いて自動で決め直します。" };
    case "hide": return { patch: { hidden: true }, undo: { hidden: false }, message: "非表示にしました。" };
    case "unhide": return { patch: { hidden: false }, undo: { hidden: true }, message: "再表示しました。" };
    default: return null;
  }
}

// 失敗のトーストの文。what は「{対象}の{操作}」(例: "タブへの移動")
const UNEXPECTED_ERROR = "予期しないエラーが発生しました。時間をおいて、やり直してください。";
function errorText(e, what) {
  const status = e && e.status;
  const msg = e && e.message ? String(e.message) : "";
  if (!status) {
    return (what ? what + "に失敗しました。" : "") + "サーバに接続できません。時間をおいて、やり直してください。";
  }
  if (status >= 500 || !msg) return what ? what + "に失敗しました。時間をおいて、やり直してください。" : UNEXPECTED_ERROR;
  return (what ? what + "に失敗しました。" : "") + msg;
}

// エディタで開く: 詳細パネルのボタン。開き先 (open_targets) はサーバが決めて検証する
const OPEN_KIND = { worktree: "worktree", main: "クローン", cwd: "会話を始めた場所" };
function editorButtons(it, editorName) {
  const targets = (it && it.open_targets) || [];
  const base = (editorName ? editorName + " で" : "エディタで") + "開く";
  return targets.map((t) => ({
    path: t.path,
    label: targets.length > 1 ? base + ": " + t.label : base,
    title: (OPEN_KIND[t.kind] || t.kind) + ": " + t.path,
    note: t.note || null,
  }));
}

// resume のコマンド: 会話のフォルダ (サーバが path_aliases で読み替えた後の cwd) へ移動してから resume する。
// パスはシングルクォートで囲む (zsh / bash 共通。中の ' は '\'' にする)。場所が分からなければ resume だけ
function shellQuote(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'";
}
function resumeCommand(sessionId, cwd) {
  const cmd = "claude --resume " + sessionId;
  return cwd ? "cd " + shellQuote(cwd) + " && " + cmd : cmd;
}
function sessionResumeCommand(it, sessionId, fallbackCwd) {
  const s = ((it && it.sessions) || []).find((x) => x.session_id === sessionId);
  return resumeCommand(sessionId, (s ? s.cwd : null) || fallbackCwd || null);
}
const RESUME_TIP = "会話のフォルダに移動して resume するコマンドをコピーします。";

// 質問できる会話: 作業の会話と、チケットなら中の会話。いちばん新しい会話を先にする
function askSessions(it) {
  const out = [];
  const add = (s, title) => {
    if (s && s.session_id && !out.some((x) => x.session_id === s.session_id)) {
      out.push({ session_id: s.session_id, title: title || s.ai_title || s.first_prompt || s.session_id, last_ts: s.last_ts || "" });
    }
  };
  for (const m of (it && it.members) || []) for (const s of m.sessions || []) add(s, m.title);
  for (const s of (it && it.sessions) || []) add(s, it.kind === "ticket" ? null : it.title);
  return out.sort((a, b) => (b.last_ts > a.last_ts ? 1 : b.last_ts < a.last_ts ? -1 : 0));
}

// チケットの会話をつないだ図の形。members は古い順の会話の作業、links はチケットのまとめのつながり
// ({from, to, kind: 続き|分かれた}、会話の ID)。つながりが無ければ時刻から決める (inferred: true)。
// 戻り値: nodes [{sid, n (会話の番号), row, lane, item}]、edges [{from, to, kind}]、lanes (列の数)
function sessionGraph(members, links) {
  const list = (members || []).map((m, i) => ({ sid: ((m.sessions || [])[0] || {}).session_id, n: i + 1, row: i, item: m }))
    .filter((x) => x.sid);
  const ids = new Set(list.map((x) => x.sid));
  const pos = new Map(list.map((x, i) => [x.sid, i]));
  let edges = (links || []).filter((l) => ids.has(l.from) && ids.has(l.to) && pos.get(l.from) < pos.get(l.to));
  // まとめがまだ無い (つながりが 1 つも無い) ときだけ、時刻から決める
  let inferred = false;
  if (!edges.length && list.length > 1) {
    inferred = true;
    edges = [];
    list.forEach((x, i) => {
      if (!i) return;
      const s = (x.item.sessions || [])[0] || {};
      const before = list.slice(0, i);
      const ended = before.filter((b) => (((b.item.sessions || [])[0] || {}).last_ts || "") <= (s.first_ts || ""));
      edges.push(ended.length ? { from: ended[ended.length - 1].sid, to: x.sid, kind: "続き" }
        : { from: before[before.length - 1].sid, to: x.sid, kind: "分かれた" });
    });
  }
  const parent = new Map();
  for (const e of edges) if (!parent.has(e.to)) parent.set(e.to, e);
  const tails = []; // 列ごとの、いちばん下の会話
  const laneOf = new Map();
  for (const x of list) {
    const e = parent.get(x.sid);
    let lane;
    if (e && e.kind === "続き" && tails[laneOf.get(e.from)] === e.from) lane = laneOf.get(e.from);
    else if (!e && !tails.length) lane = 0;
    else lane = tails.length;
    tails[lane] = x.sid;
    laneOf.set(x.sid, lane);
    x.lane = lane;
  }
  return { nodes: list, edges: [...parent.values()], lanes: Math.max(1, tails.length), inferred };
}

// 詳細パネルの幅: 最小 420px、画面の左に 160px は残す。最小より狭い画面では最小のまま (画面幅いっぱいの表示は CSS が決める)
const DRAWER_MIN = 420;
const DRAWER_DEFAULT = 600;
function clampDrawerWidth(w, viewport) {
  const max = Math.max(DRAWER_MIN, viewport - 160);
  return Math.round(Math.min(max, Math.max(DRAWER_MIN, Number(w) || DRAWER_DEFAULT)));
}

// キーボード操作の一覧 (「?」で開く一覧に出す。onKeydown で受けるキーと合わせる)
const SHORTCUTS = [
  { group: "ボード (入力欄にフォーカスがないとき)", keys: [
    [["/"], "検索"], [["j", "k"], "行を移動"], [["Enter"], "詳細パネルを開く"], [["t"], "タブへ移動"],
    [["o"], "PR を開く"], [["e"], "エディタで開く"], [["m"], "自分の対応待ちだけ"], [["f"], "絞り込み"],
    [["r"], "更新"], [["w"], "作業報告"], [["?"], "キーボード操作の一覧"],
    [["Esc"], "パネル・ポップオーバー・ダイアログを閉じる"],
  ] },
  { group: "作業報告", keys: [[["c"], "まだコピーしていない次の項目をコピー"]] },
  { group: "入力欄", keys: [
    [["Enter"], "名前・タイトルを保存"], [["⌘", "Enter"], "Claude Code に質問を送る"], [["Esc"], "入力をやめる"],
  ] },
];

// 表示する列: 絞り込みで外した列 (colsOff) を除く。すべて外れていたら (列の名前が変わったときなど) すべて出す
function visibleTargets(targets, colsOff) {
  const off = colsOff || [];
  const shown = (targets || []).filter((t) => !off.includes(t));
  return shown.length ? shown : (targets || []).slice();
}

// 次の質問に添える、直前の質問と答え (答えが出たものだけ、新しい 3 つ)
function askHistory(log) {
  return (log || []).filter((x) => x && x.q && x.a).slice(-3).map((x) => ({ q: x.q, a: x.a }));
}

const Rules = {
  DONE_PHASES, MINE_ORDER, isDone, ballFor, ticketBall, dropAction, memberLine, mineList, trackFor, columnTemplate, isWideColumn,
  safeColor, looseTabNames, flowModel, hasFlowDetail, nowNext, matchesSearch, relTime, firstSentence, safeGithubUrl, prStateText,
  pickPr, terminalInfo, shellQuote, resumeCommand, sessionResumeCommand, focusPayload, tabTitle, cleanupLines, cleanupResult, editorButtons,
  trashCounts, trashTip, hideTip, itemActions, actionPatch, isManualDone, errorText, UNEXPECTED_ERROR,
  askSessions, askHistory, visibleTargets, SHORTCUTS, sessionGraph, clampDrawerWidth, DRAWER_DEFAULT,
};
if (typeof module !== "undefined" && module.exports) module.exports = Rules;

// ================================================================== 画面 (ブラウザ)

const SVG_NS = "http://www.w3.org/2000/svg";
const ICONS = {
  search: [["circle", { cx: 7, cy: 7, r: 4.5 }], ["path", { d: "m10.5 10.5 3 3" }]],
  filter: [["path", { d: "M2.5 4h11M4.5 8h7M6.5 12h3" }]],
  refresh: [["path", { d: "M13 8a5 5 0 1 1-1.5-3.5" }], ["path", { d: "M13 2.5V5h-2.5" }]],
  jump: [["path", { d: "M6 3H3v10h10v-3" }], ["path", { d: "M9 3h4v4" }], ["path", { d: "M13 3 7 9" }]],
  chev: [["path", { d: "m6 4 4 4-4 4" }]],
  close: [["path", { d: "m4 4 8 8M12 4l-8 8" }]],
  copy: [["rect", { x: 5, y: 5, width: 8, height: 8, rx: 1.5 }], ["path", { d: "M3 10.5V4a1 1 0 0 1 1-1h6.5" }]],
  shell: [["path", { d: "m3.5 5 3 3-3 3" }], ["path", { d: "M8 11h4.5" }]],
  claude: [["path", { d: "M8 2.5v11M2.5 8h11M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6" }]],
  ok: [["circle", { cx: 8, cy: 8, r: 6 }], ["path", { d: "m5.2 8.2 2 2 3.6-4" }]],
  check: [["path", { d: "m3.2 8.4 3.1 3.1 6.5-7" }]],
  undo: [["path", { d: "M4.5 6.5h6a2.75 2.75 0 0 1 0 5.5H7.5" }], ["path", { d: "M6.8 4.2 4.5 6.5l2.3 2.3" }]],
  eye: [["path", { d: "M1.8 8S4 3.8 8 3.8 14.2 8 14.2 8 12 12.2 8 12.2 1.8 8 1.8 8z" }], ["circle", { cx: 8, cy: 8, r: 2 }]],
  eyeOff: [["path", { d: "M1.8 8S4 3.8 8 3.8 14.2 8 14.2 8 12 12.2 8 12.2 1.8 8 1.8 8z" }], ["circle", { cx: 8, cy: 8, r: 2 }],
    ["path", { d: "M2.8 13.2 13.2 2.8" }]],
  trash: [["path", { d: "M2.8 4.5h10.4M6.3 4.5V2.8h3.4v1.7" }], ["path", { d: "m4.3 4.5.7 8.7h6l.7-8.7" }],
    ["path", { d: "M6.8 7v4M9.2 7v4" }]],
};
const STORE = {
  filters: "session-board:filters",
  onlyMine: "session-board:only-attention",
  mineCollapsed: "session-board:mine-collapsed",
  drawerWidth: "session-board:drawer-width",
  openGroups: "session-board:open-groups",
};
// 名前を変える前 (cmux-board) に保存した値を、新しい名前へ移す (同じ 127.0.0.1:<port> で開いている間は読める)
function migrateOldStorage() {
  for (const key of Object.values(STORE)) {
    const old = key.replace(/^session-board:/, "cmux-board:");
    const v = storageGet(old);
    if (v !== null && storageGet(key) === null) storageSet(key, v);
    if (v !== null) storageRemove(old);
  }
}
const FLAG_FILTERS = [
  { key: "needs_input", label: "指示待ち", test: (it) => it.needs_input },
  { key: "stale", label: "動きなし", test: (it) => it.stale },
  { key: "changes_requested", label: "修正依頼", test: (it) => it.changes_requested },
];
const PHASE_CHIP = { "実装中": "実装", "レビュー待ち": "レビュー" };
const COLLECT_STAGES = ["開始", "タブとプロセス", "トランスクリプト", "git", "GitHub PR"];
const STAGE_LABEL = { "トランスクリプト": "会話の記録" };
const WEEKDAYS = ["日", "月", "火", "水", "木", "金", "土"];

let S = null; // 画面の状態 (boot で作る)

function $(id) {
  return document.getElementById(id);
}

// localStorage は使えない環境 (プライベートモード等) でも落ちないように包む
function storageGet(key) {
  try { return window.localStorage.getItem(key); } catch (e) { return null; }
}

function storageSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (e) { /* 保存できなくても表示は続ける */ }
}

function storageRemove(key) {
  try { window.localStorage.removeItem(key); } catch (e) { /* 消せなくても表示は続ける */ }
}

function loadJson(key, fallback) {
  try {
    const v = JSON.parse(storageGet(key) || "null");
    return v === null ? fallback : v;
  } catch (e) {
    return fallback;
  }
}

function loadFilters() {
  const f = { phasesOff: [], flagsOff: [], colsOff: [], showHidden: false, project: "" };
  const raw = loadJson(STORE.filters, {});
  if (raw && typeof raw === "object") {
    if (Array.isArray(raw.phasesOff)) f.phasesOff = raw.phasesOff.filter((x) => typeof x === "string");
    if (Array.isArray(raw.flagsOff)) f.flagsOff = raw.flagsOff.filter((x) => typeof x === "string");
    if (Array.isArray(raw.colsOff)) f.colsOff = raw.colsOff.filter((x) => typeof x === "string");
    f.showHidden = raw.showHidden === true;
    if (typeof raw.project === "string") f.project = raw.project;
  }
  return f;
}

function saveFilters() {
  storageSet(STORE.filters, JSON.stringify(S.filters));
}

function saveOpenGroups() {
  storageSet(STORE.openGroups, JSON.stringify([...S.openGroups]));
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    }
  }
  for (const c of children || []) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function icon(name, cls) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "ico" + (cls ? " " + cls : ""));
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const [tag, attrs] of ICONS[name] || []) {
    const n = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    svg.appendChild(n);
  }
  return svg;
}

function hhmm(epochSec) {
  const d = new Date(epochSec * 1000);
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

function relEpoch(sec) {
  if (typeof sec !== "number" || !sec) return null;
  return relTime(new Date(sec * 1000).toISOString());
}

function itemTime(it) {
  return it.activity_ts || it.last_activity;
}

function absTime(iso) {
  const t = parseTs(iso);
  return t === null ? null : new Date(t).toLocaleString("ja-JP");
}

function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");
}

function isTyping(t) {
  if (!t || !t.tagName) return false;
  if (t.isContentEditable) return true;
  if (t.tagName === "TEXTAREA" || t.tagName === "SELECT") return true;
  // 日付欄は文字を打たないので、作業報告を開いた直後 (日付欄にフォーカス) でも c などの単キーを効かせる
  if (t.tagName === "INPUT") return !["checkbox", "radio", "button", "submit", "reset", "date"].includes(t.type);
  return false;
}

function reduceMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// ------------------------------------------------------------------ API・トースト

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Board": "1" },
    body: JSON.stringify(body || {}),
    credentials: "same-origin",
  });
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  if (!res.ok) {
    const err = new Error(data.error || ("HTTP " + res.status));
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(text, opts) {
  opts = opts || {};
  const box = $("toasts");
  const t = el("div", { class: "toast", role: opts.error ? "alert" : "status" }, [el("span", { text })]);
  if (opts.action) {
    t.appendChild(el("button", {
      type: "button", class: "btn btn--sm", text: opts.action,
      onclick: () => { t.remove(); opts.onAction(); },
    }));
  }
  box.appendChild(t);
  while (box.children.length > 3) box.firstChild.remove();
  setTimeout(() => t.remove(), opts.ms || (opts.action ? 8000 : 4000));
}

// what は失敗したときの「{対象}の{操作}」(例: "タイトルの変更")
async function act(fn, what) {
  try {
    await fn();
    await poll(true);
  } catch (e) {
    toast(errorText(e, what), { error: true });
  }
}

function copyByExecCommand(text) {
  const ta = el("textarea", { class: "copy-buffer", readonly: true, "aria-hidden": "true" });
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* execCommand に落とす */ }
  return copyByExecCommand(text);
}

async function copyWithToast(text) {
  const ok = await copyText(text);
  toast(ok ? "コピーしました。" : "コピーに失敗しました。", { error: !ok });
}

function term() {
  return terminalInfo(S && S.data);
}

function canFocus() {
  return term().caps.focus;
}

function latestSessionId(it) {
  const s = (it && it.sessions || [])[0];
  return s ? s.session_id : null;
}

async function focusTab(tab, it) {
  try {
    const r = await api("/api/focus", focusPayload(tab));
    if (r.ok) toast("タブへ移動しました。");
    else toast("タブへの移動に失敗しました。" + (r.errors || []).join(" / "), { error: true });
  } catch (e) {
    if (e.status === 404) {
      const sid = tab.session_id || latestSessionId(it);
      toast("タブがありません。閉じられた可能性があります。", sid ? {
        action: "claude --resume をコピー",
        onAction: () => copyWithToast(sessionResumeCommand(it, sid, tab.cwd)),
      } : {});
      poll(true);
    } else {
      toast(errorText(e, "タブへの移動"), { error: true });
    }
  }
}

async function openEditor(it, path) {
  try {
    const r = await api("/api/open-editor", { item_id: it.id, path });
    toast(r.editor + " で開きました: " + r.path);
  } catch (e) {
    toast(errorText(e, "エディタの起動"), { error: true, ms: 10000 });
  }
}

function editorName() {
  return (S.data && S.data.editor && S.data.editor.name) || "";
}

function openPrUrl(url) {
  if (url) window.open(url, "_blank", "noopener,noreferrer");
}

// 完了・非表示 (とその取り消し) は確認なしで実行し、8 秒間「元に戻す」を出す
const ACTION_WHAT = { done: "完了の設定", undone: "完了の取り消し", reopen: "進行中に戻す操作", hide: "非表示の設定", unhide: "再表示" };
async function runAction(it, key) {
  if (key === "trash") {
    openCleanup(it);
    return;
  }
  const a = actionPatch(it, key);
  if (!a) return;
  try {
    await api("/api/override", { item_id: it.id, patch: a.patch });
    hideTooltip();
    if (key === "hide" && !S.filters.showHidden && S.drawer && S.drawer.id === it.id) closeDrawer(false);
    toast(a.message, {
      action: "元に戻す",
      ms: 8000,
      onAction: () => act(() => api("/api/override", { item_id: it.id, patch: a.undo }), "元に戻す操作"),
    });
    await poll(true);
  } catch (e) {
    toast(errorText(e, ACTION_WHAT[key]), { error: true });
  }
}

// 作業の右上のボタン列。説明は aria-describedby の隠れた要素と、浮かぶツールチップ (#tooltip) の両方に出す
function actionsEl(it, ctx, uid) {
  const acts = itemActions(it, { items: ctx.items, caps: term().caps, hiddenCount: ctx.hiddenCount });
  const box = el("span", { class: "acts", role: "group", "aria-label": "「" + clipText(it.title, 40) + "」の操作" });
  for (const a of acts) {
    const tipId = "tip-" + uid + "-" + a.key;
    box.appendChild(el("button", {
      type: "button", class: "act act--" + a.key, "aria-label": a.label, "aria-describedby": tipId, "data-tip": tipId,
      "aria-haspopup": a.key === "trash" ? "dialog" : null,
      onclick: (ev) => { ev.stopPropagation(); runAction(it, a.key); },
    }, [icon(a.icon)]));
    box.appendChild(el("span", { id: tipId, class: "act__desc", hidden: true, text: a.tip }));
  }
  return box;
}

// ------------------------------------------------------------------ ツールチップ
// ホバーとフォーカスで出す。Esc で消せて、ツールチップの上にポインタを移しても消えない (WCAG 1.4.13)

const tip = { anchor: null, timer: null };

function tooltipNode() {
  return $("tooltip");
}

function showTooltip(anchor) {
  const node = tooltipNode();
  const desc = anchor && $(anchor.getAttribute("data-tip"));
  if (!node || !desc) return;
  clearTimeout(tip.timer);
  tip.anchor = anchor;
  node.textContent = desc.textContent;
  node.hidden = false;
  const r = anchor.getBoundingClientRect();
  const w = node.offsetWidth;
  const h = node.offsetHeight;
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  const left = Math.max(8, Math.min(vw - w - 8, r.right - w));
  const below = r.bottom + 6;
  const top = below + h + 8 > vh ? Math.max(8, r.top - h - 6) : below;
  node.style.left = left + "px";
  node.style.top = top + "px";
}

function hideTooltip() {
  clearTimeout(tip.timer);
  tip.anchor = null;
  const node = tooltipNode();
  if (node) node.hidden = true;
}

function hideTooltipSoon() {
  clearTimeout(tip.timer);
  tip.timer = setTimeout(hideTooltip, 150);
}

function tooltipIsOpen() {
  const node = tooltipNode();
  return !!(node && !node.hidden);
}

function setupTooltip() {
  const node = tooltipNode();
  const anchorOf = (t) => (t && t.closest ? t.closest("[data-tip]") : null);
  document.addEventListener("mouseover", (ev) => {
    const a = anchorOf(ev.target);
    if (a) showTooltip(a);
    else if (node.contains(ev.target)) clearTimeout(tip.timer);
  });
  document.addEventListener("mouseout", (ev) => {
    const a = anchorOf(ev.target);
    if ((a || node.contains(ev.target)) && !(ev.relatedTarget && (node.contains(ev.relatedTarget) || anchorOf(ev.relatedTarget) === tip.anchor))) {
      if (!(tip.anchor && tip.anchor === document.activeElement)) hideTooltipSoon();
    }
  });
  document.addEventListener("focusin", (ev) => {
    const a = anchorOf(ev.target);
    if (a) showTooltip(a);
    else hideTooltip();
  });
  document.addEventListener("focusout", (ev) => {
    if (anchorOf(ev.target)) hideTooltipSoon();
  });
  document.addEventListener("scroll", hideTooltip, true);
}

// ------------------------------------------------------------------ 描画の材料

function baseVisible(it) {
  const f = S.filters;
  if (it.hidden && !f.showHidden) return false;
  if (f.phasesOff.includes(it.phase)) return false;
  if (f.colsOff.includes(it.target) && hiddenCols().has(it.target)) return false;
  if (f.project && ((it.ticket || {}).project || "") !== f.project) return false;
  if (FLAG_FILTERS.some((x) => f.flagsOff.includes(x.key) && x.test(it))) return false;
  return true;
}

// ボードのすべての列: 設定の列の順に、作業にだけ出てくる列を足す
function allTargets() {
  const d = S.data;
  const targets = d ? d.targets.slice() : [];
  for (const it of (d && d.items) || []) if (!targets.includes(it.target)) targets.push(it.target);
  return targets;
}

// 絞り込みで外している列 (すべて外れているときは、どれも外さない)
function hiddenCols() {
  const all = allTargets();
  const shown = visibleTargets(all, S.filters.colsOff);
  return new Set(all.filter((t) => !shown.includes(t)));
}

function buildCtx() {
  const d = S.data;
  const now = Date.now();
  const items = (d && d.items) || [];
  const balls = new Map(items.map((it) => [it.id, ballFor(it, now)]));
  const members = items.flatMap((it) => it.members || []);
  for (const m of members) balls.set(m.id, ballFor(m, now));
  const offCols = hiddenCols();
  const targets = allTargets().filter((t) => !offCols.has(t));
  // 列の表示のヒント (targets.json の projects: color / layout / cloud)
  const meta = new Map(((d && d.projects) || []).map((p) => [p.name, p]));
  const cols = new Map(targets.map((t) => {
    const m = meta.get(t) || {};
    return [t, { target: t, label: m.label || t, other: !!m.other, all: [], open: [], done: [], mine: 0, filtered: 0,
      layout: m.layout || "auto", color: safeColor(m.color), cloud: m.cloud || null }];
  }));
  for (const it of items) {
    const c = cols.get(it.target);
    if (!c) continue; // 絞り込みで外した列
    if (!it.hidden) c.all.push(it);
    const ball = balls.get(it.id);
    if (!baseVisible(it)) {
      if (!it.hidden) c.filtered += 1;
      continue;
    }
    if (S.onlyMine && !(ball && ball.mine)) continue;
    if (S.stopOnly && !(ball && ball.key === "stop")) continue;
    if (!matchesSearch(it, S.query)) continue;
    (isDone(it) ? c.done : c.open).push(it);
    if (ball && ball.mine) c.mine += 1;
  }
  const loose = new Map();
  for (const t of (d && d.unlinked_tabs) || []) {
    if (!loose.has(t.target)) loose.set(t.target, []);
    loose.get(t.target).push(t);
  }
  let claude = 0;
  let review = 0;
  let stop = 0;
  for (const b of balls.values()) {
    if (!b) continue;
    if (b.key === "claude") claude += 1;
    if (b.key === "review") review += 1;
    if (b.key === "stop") stop += 1;
  }
  const wideMin = (d && d.settings && d.settings.column_wide_min_items) || DEFAULT_WIDE_MIN_ITEMS;
  return {
    now, items, balls, targets, cols, loose, wideMin, mine: mineList(items.filter((it) => !offCols.has(it.target)), now), counts: { claude, review, stop },
    byId: new Map(items.concat(members).map((it) => [it.id, it])), hiddenCount: items.filter((it) => it.hidden).length,
  };
}

// ------------------------------------------------------------------ 描画: 全体

function deferRender() {
  if (S.dragId) return true; // ドラッグ中に描き直すと、ドラッグしている行が消える
  if (S.resizing) return true; // 詳細パネルの幅を変えている間に描き直すと、つまみが消える
  const a = document.activeElement;
  return !!(a && a.closest && (a.closest("#board") || a.closest("#drawer"))
    && ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName));
}

function focusables(container) {
  return [...container.querySelectorAll("a[href], button, input, select, textarea, summary, [tabindex]")]
    .filter((n) => !n.disabled && !n.closest("[hidden]"));
}

function render() {
  if (deferRender()) {
    S.pendingRender = true;
    renderStatus();
    return;
  }
  S.pendingRender = false;
  const ctx = buildCtx();
  const active = document.activeElement;
  const navKey = active && active.dataset ? active.dataset.nav : null;
  const drawer = $("drawer");
  const drawerFocus = drawer.contains(active) ? focusables(drawer).indexOf(active) : -1;
  const pop = $("filter-pop");
  const popFocus = pop.contains(active) ? focusables(pop).indexOf(active) : -1;

  renderHeader(ctx);
  renderBanners();
  renderPickbar(ctx);
  renderMine(ctx);
  renderBoard(ctx);
  renderDrawer(ctx);

  if (navKey) {
    const n = document.querySelector('[data-nav="' + cssEscape(navKey) + '"]');
    if (n) n.focus({ preventScroll: true });
  }
  if (drawerFocus >= 0) {
    const f = focusables(drawer);
    const n = f[drawerFocus] || f[f.length - 1];
    if (n) n.focus({ preventScroll: true });
  }
  if (popFocus >= 0) {
    const f = focusables(pop);
    const n = f[popFocus] || f[f.length - 1];
    if (n) n.focus({ preventScroll: true });
  }
  S.summAt = new Map(ctx.items.map((it) => [it.id, it.summarized_at]));
}

// ------------------------------------------------------------------ 描画: ヘッダー

function hiddenByFilterCount() {
  const items = (S.data && S.data.items) || [];
  return items.filter((it) => !it.hidden && !baseVisible(it)).length;
}

function renderHeader() {
  const f = S.filters;
  const offCols = hiddenCols();
  const n = f.phasesOff.length + f.flagsOff.length + (f.project ? 1 : 0) + offCols.size;
  const badge = $("filter-badge");
  badge.hidden = n === 0;
  badge.textContent = String(n);
  $("filter-btn").setAttribute("aria-label", n ? "絞り込み (" + n + " 種類を表示していません)" : "絞り込み");
  $("mine-switch").setAttribute("aria-checked", S.onlyMine ? "true" : "false");
  const hiddenN = ((S.data && S.data.items) || []).filter((it) => it.hidden).length;
  const hb = $("hidden-btn");
  hb.textContent = "非表示 (" + hiddenN + ")";
  hb.setAttribute("aria-pressed", f.showHidden ? "true" : "false");
  hb.title = f.showHidden ? "非表示にした作業を、一覧に表示しないようにします。"
    : "非表示にした作業を、一覧に薄く表示します。各作業の［再表示］で、ふだんの表示にできます。";
  const notes = $("filter-notes");
  notes.replaceChildren();
  if (offCols.size) {
    notes.appendChild(el("span", { class: "filter-note" }, [
      "表示していない列 " + offCols.size + " つ",
      el("button", { type: "button", class: "icon-btn", "aria-label": "列の絞り込みを解除して、すべての列を表示",
        onclick: () => { f.colsOff = []; saveFilters(); render(); } }, [icon("close", "ico--sm")]),
    ]));
  }
  if (S.stopOnly) {
    notes.appendChild(el("span", { class: "filter-note" }, [
      "止まっている作業だけ",
      el("button", { type: "button", class: "icon-btn", "aria-label": "「止まっている作業だけ」を解除",
        onclick: () => { S.stopOnly = false; render(); } }, [icon("close", "ico--sm")]),
    ]));
  }
  renderStatus();
  renderPopover();
}

function problems(d) {
  const out = (d.errors || []).slice();
  const p = d.progress || {};
  if (p.error) out.push("情報の収集に失敗しました: " + p.error);
  if (d.llm && d.llm.last_error) out.push("要約に失敗した会話 " + d.llm.failed + " 件 (最後のエラー: " + d.llm.last_error + ")");
  return out;
}

function renderStatus() {
  const root = $("status");
  root.replaceChildren();
  if (!S.connected) root.appendChild(el("span", { class: "status status--err", text: "サーバに接続できません" }));
  const d = S.data;
  if (!d) return;
  const p = d.progress || {};
  if (p.running) {
    const idx = COLLECT_STAGES.indexOf(p.stage);
    const meter = el("span", { class: "meter", "aria-hidden": "true" },
      COLLECT_STAGES.map((_, i) => el("span", { class: "meter__seg" + (i < idx ? " meter__seg--done" : "") })));
    root.appendChild(el("span", { class: "status status--busy" }, [
      el("span", { class: "spinner", "aria-hidden": "true" }), "集めています: " + (STAGE_LABEL[p.stage] || p.stage || ""), meter]));
  } else if (d.generated_at) {
    root.appendChild(el("span", { class: "status", title: p.seconds ? "収集 " + p.seconds + " 秒" : null,
      text: hhmm(d.generated_at) + " 更新" }));
  }
  const l = d.llm;
  if (l && l.queued > 0) {
    root.appendChild(el("span", { class: "status status--busy", title: "完了 " + l.done + " 件"
      + (l.avg_seconds ? " (平均 " + l.avg_seconds + " 秒)" : "") }, [
      el("span", { class: "spinner", "aria-hidden": "true" }), "要約の残り " + l.queued]));
  }
  if (!d.llm_enabled) root.appendChild(el("span", { class: "status muted", text: "要約: オフ" }));
  const probs = problems(d);
  if (probs.length) {
    root.appendChild(el("span", { class: "status status--err" }, [
      "うまくいかなかった処理 " + probs.length + " 件",
      el("button", { type: "button", class: "btn btn--sm", "aria-expanded": S.showProblems ? "true" : "false",
        text: S.showProblems ? "閉じる" : "見る",
        onclick: () => { S.showProblems = !S.showProblems; renderStatus(); renderBanners(); } }),
    ]));
  }
}

function renderBanners() {
  const box = $("banners");
  box.replaceChildren();
  const d = S.data;
  if (!S.connected) {
    box.appendChild(el("div", { class: "banner banner--err", role: "alert" }, [
      el("span", { class: "banner__text" }, [
        el("b", { text: "サーバに接続できません" }),
        (d && d.generated_at ? "表示は " + hhmm(d.generated_at) + " のまま。" : "") + "10 秒ごとに再接続します。",
      ]),
      el("button", { type: "button", class: "btn btn--sm", text: "今すぐ再接続", onclick: () => poll(true) }),
    ]));
  }
  if (d && d.config && !d.config.configured) {
    box.appendChild(el("div", { class: "banner banner--warn", role: "status" }, [
      el("span", { class: "banner__text" }, [
        el("b", { text: "追跡するリポジトリが設定されていません" }), d.config.message || "",
      ]),
    ]));
  }
  const tm = terminalInfo(d);
  if (d && (tm.error || (!d.terminal && (d.errors || []).some((e) => e.includes("cmux tree"))))) {
    box.appendChild(el("div", { class: "banner banner--warn", role: "status" }, [
      el("span", { class: "banner__text" }, [
        el("b", { text: "「タブへ移動」と「指示待ち」は使えません" }),
        tm.label + " からのタブの一覧の取得に失敗しました。" + tm.label + " が起動しているか確認してください。",
      ]),
    ]));
  }
  if (d && !d.llm_enabled && !S.dismissed.has("llm-off")) {
    box.appendChild(el("div", { class: "banner banner--info" }, [
      el("span", { class: "banner__text" }, [
        el("b", { text: "要約はオフです" }), "流れ図は PR とブランチだけで描きます。作業報告は使えません。",
      ]),
      el("button", { type: "button", class: "icon-btn", "aria-label": "閉じる",
        onclick: () => { S.dismissed.add("llm-off"); renderBanners(); } }, [icon("close", "ico--sm")]),
    ]));
  }
  if (d && S.showProblems) {
    const probs = problems(d);
    if (probs.length) {
      box.appendChild(el("div", { class: "banner banner--warn" }, [
        el("span", { class: "banner__text" }, [
          el("b", { text: "うまくいかなかった処理" }),
          el("ul", { class: "banner__list" }, probs.map((p) => el("li", { text: p }))),
        ]),
        el("button", { type: "button", class: "icon-btn", "aria-label": "閉じる",
          onclick: () => { S.showProblems = false; renderStatus(); renderBanners(); } }, [icon("close", "ico--sm")]),
      ]));
    }
  }
  box.hidden = box.children.length === 0;
}

function togglePopover(open) {
  S.popover = open === undefined ? !S.popover : open;
  renderPopover();
  if (S.popover) {
    const f = focusables($("filter-pop"));
    if (f[0]) f[0].focus();
  } else {
    $("filter-btn").focus();
  }
}

function chip(label, n, off, title, onclick) {
  return el("button", { type: "button", class: "chip", "aria-pressed": off ? "false" : "true", title, onclick }, [
    el("span", { class: "chip__label", text: label }), el("span", { class: "chip__n", text: String(n) }),
  ]);
}

function switchButton(label, checked, onclick) {
  return el("button", { type: "button", class: "switch switch--neutral", role: "switch",
    "aria-checked": checked ? "true" : "false", onclick }, [el("span", { class: "switch__track" }), label]);
}

function renderPopover() {
  const pop = $("filter-pop");
  $("filter-btn").setAttribute("aria-expanded", S.popover ? "true" : "false");
  pop.hidden = !S.popover;
  if (!S.popover) {
    pop.replaceChildren();
    return;
  }
  const d = S.data;
  const f = S.filters;
  const sum = (d && d.summary) || {};
  const phases = el("div", { class: "popover__row" });
  for (const ph of (d && d.phases) || []) {
    const n = (sum.phases || {})[ph] || 0;
    const off = f.phasesOff.includes(ph);
    if (!n && !off) continue;
    phases.appendChild(chip(PHASE_CHIP[ph] || ph, n, off, off ? "「" + ph + "」を表示" : "「" + ph + "」を表示しない",
      () => { toggleInList(f.phasesOff, ph); saveFilters(); render(); }));
  }
  const flags = el("div", { class: "popover__row" }, FLAG_FILTERS.map((x) => {
    const off = f.flagsOff.includes(x.key);
    return chip(x.label, sum[x.key] || 0, off, off ? "「" + x.label + "」の作業を表示" : "「" + x.label + "」の作業を表示しない",
      () => { toggleInList(f.flagsOff, x.key); saveFilters(); render(); });
  }));
  const hiddenN = ((d && d.items) || []).filter((it) => it.hidden).length;
  const more = el("div", { class: "popover__row" }, [
    switchButton("非表示の作業 (" + hiddenN + ") も表示", f.showHidden, () => { f.showHidden = !f.showHidden; saveFilters(); render(); }),
  ]);
  // 列: 押すと表示・非表示が切り替わる。最後の 1 列は外せない
  const all = allTargets();
  const offCols = hiddenCols();
  const counts = new Map();
  for (const it of (d && d.items) || []) if (!it.hidden && !isDone(it)) counts.set(it.target, (counts.get(it.target) || 0) + 1);
  const cols = el("div", { class: "popover__row" }, all.map((t) => {
    const off = offCols.has(t);
    const last = !off && all.length - offCols.size <= 1;
    return chip(colLabel(t), counts.get(t) || 0, off,
      off ? "「" + colLabel(t) + "」の列を表示" : last ? "最後の 1 列は表示しないようにできません" : "「" + colLabel(t) + "」の列を表示しない",
      () => {
        if (last) return;
        f.colsOff = off ? f.colsOff.filter((x) => x !== t) : f.colsOff.filter((x) => all.includes(x)).concat([t]);
        saveFilters();
        render();
      });
  }));
  const hiding = hiddenByFilterCount();
  const projs = (d && d.ticket_projects) || [];
  let proj = null;
  if (projs.length || f.project) {
    const sel = el("select", { id: "filter-project", class: "select" });
    sel.appendChild(el("option", { value: "", text: "すべて" }));
    for (const name of projs.includes(f.project) || !f.project ? projs : projs.concat([f.project])) {
      const opt = el("option", { value: name, text: name });
      if (name === f.project) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.addEventListener("change", () => { f.project = sel.value; saveFilters(); render(); });
    proj = el("div", { class: "popover__row" }, [el("label", { class: "sr-only", for: "filter-project", text: "案件" }), sel]);
  }
  pop.replaceChildren(
    all.length > 1 ? el("h3", { class: "popover__h", text: "表示する列 (押すと切り替わります)" }) : null, all.length > 1 ? cols : null,
    proj ? el("h3", { class: "popover__h", text: "案件 (チケットの案件の名前で絞り込みます)" }) : null, proj,
    el("h3", { class: "popover__h", text: "表示するフェーズ (押すと切り替わります)" }), phases,
    el("h3", { class: "popover__h", text: "表示する状態 (押すと切り替わります)" }), flags,
    el("h3", { class: "popover__h", text: "ほかに表示するもの" }), more,
    el("div", { class: "popover__foot" }, [
      el("span", { class: "muted", text: hiding ? hiding + " 件を絞り込みで表示していません" : "すべて表示しています" }),
      el("button", { type: "button", class: "btn btn--sm", text: "絞り込みを解除", disabled: !(f.phasesOff.length || f.flagsOff.length || f.project || offCols.size),
        onclick: resetFilters }),
    ]),
  );
}

function toggleInList(list, value) {
  const i = list.indexOf(value);
  if (i >= 0) list.splice(i, 1); else list.push(value);
}

function resetFilters() {
  S.filters.phasesOff = [];
  S.filters.flagsOff = [];
  S.filters.project = "";
  S.filters.colsOff = [];
  saveFilters();
  S.stopOnly = false;
  setOnlyMine(false);
}

function setOnlyMine(v) {
  S.onlyMine = v;
  storageSet(STORE.onlyMine, v ? "1" : "0");
  render();
}

// ------------------------------------------------------------------ 描画: 自分の対応待ち

function renderMine(ctx) {
  const sec = $("mine");
  sec.replaceChildren();
  if (!S.data) {
    sec.hidden = true;
    return;
  }
  sec.hidden = false;
  const list = ctx.mine;
  const stopLink = ctx.counts.stop ? el("button", {
    type: "button", class: "linkbtn", "aria-pressed": S.stopOnly ? "true" : "false",
    title: "動きの止まっている作業だけを表示します。",
    onclick: () => { S.stopOnly = !S.stopOnly; render(); },
  }, ["止まっている作業 " + ctx.counts.stop]) : null;
  if (!list.length) {
    sec.className = "mine mine--calm";
    sec.appendChild(el("div", { class: "mine__head" }, [
      icon("ok", "ico--lg mine__ok"),
      el("h2", { class: "mine__title", id: "mine-h", text: "自分の対応待ちはありません" }),
      el("div", { class: "mine__aside" }, [
        "Claude 実行中 " + ctx.counts.claude + " · レビュアー待ち " + ctx.counts.review, stopLink,
      ]),
    ]));
    return;
  }
  sec.className = "mine";
  sec.appendChild(el("div", { class: "mine__head" }, [
    el("h2", { class: "mine__title", id: "mine-h" }, ["自分の対応待ち ", el("span", { class: "mine__n", text: String(list.length) })]),
    el("div", { class: "mine__aside" }, [
      stopLink,
      el("button", { type: "button", class: "btn btn--ghost btn--sm", "aria-expanded": S.mineCollapsed ? "false" : "true",
        "aria-controls": "mine-list", text: S.mineCollapsed ? "開く" : "畳む",
        onclick: () => {
          S.mineCollapsed = !S.mineCollapsed;
          storageSet(STORE.mineCollapsed, JSON.stringify(S.mineCollapsed));
          render();
        } }),
    ]),
  ]));
  if (S.mineCollapsed) return;
  const cap = S.onlyMine ? list.length : 8;
  sec.appendChild(el("ul", { class: "mine__list", id: "mine-list" }, list.slice(0, cap).map((x) => mineRow(x, ctx))));
  if (list.length > cap) {
    const rest = list.slice(cap);
    const same = rest.every((x) => x.ball.label === rest[0].ball.label);
    sec.appendChild(el("div", { class: "mine__more" }, [
      el("button", { type: "button", class: "linkbtn", text: "ほか " + rest.length + " 件" + (same ? " (" + rest[0].ball.label + ")" : ""),
        title: "「自分の対応待ちだけ」をオンにして、すべて表示します。", onclick: () => setOnlyMine(true) }),
    ]));
  }
}

function mineRow(x, ctx) {
  const { item: it, ball } = x;
  const tab = canFocus() ? (it.tabs || [])[0] : null;
  let action;
  const pr = (ball.key === "fix" || ball.key === "merge") ? pickPr(it, ball) : null;
  if (tab) {
    action = el("button", { type: "button", class: "btn btn--jump btn--sm", title: "タブへ移動", onclick: () => focusTab(tab, it) }, ["タブへ"]);
  } else if (pr) {
    action = el("a", { class: "btn btn--sm", href: safeGithubUrl(pr.url), target: "_blank", rel: "noopener noreferrer",
      title: pr.label + " #" + pr.number + (pr.title ? ": " + pr.title : "") }, ["PR を開く"]);
  } else {
    action = el("button", { type: "button", class: "btn btn--sm", onclick: (ev) => openItem(it.id, "m:" + it.id, ev) }, ["開く"]);
  }
  return el("li", { class: "mrow" }, [
    el("span", { class: "mrow__why ball ball--mine", title: ball.tip, text: ball.label }),
    el("span", { class: "ptag", text: colLabel(it.target) }),
    el("button", { type: "button", class: "mrow__title", "data-nav": "m:" + it.id, "data-item": it.id,
      title: it.title + (ball.staleSummary ? "\n" + STALE_SUMMARY_TIP : ""),
      onclick: (ev) => openItem(it.id, "m:" + it.id, ev) }, [
      ticketTag(it), it.title, ball.staleSummary ? el("span", { class: "mrow__stale", text: " (要約時点)" }) : null,
    ]),
    el("span", { class: "mrow__time", title: absTime(itemTime(it)), text: relTime(itemTime(it), ctx.now) }),
    action,
  ]);
}

// ------------------------------------------------------------------ 描画: ボード

function trackEl(tr) {
  return el("span", { class: "track" + (tr.other ? " track--other" : ""), role: "img", "aria-label": tr.label },
    tr.dots.map((dot) => el("i", { class: dot || null })));
}

function ballEl(ball, it) {
  if (!ball) {
    if (it.llm_status === "pending" && !it.summarized_at && S.data.llm_enabled) {
      return el("span", { class: "ball" }, [el("span", { class: "spinner", "aria-hidden": "true" }), "要約待ち"]);
    }
    return null;
  }
  return el("span", { class: "ball " + ball.cls, title: ball.tip, text: ball.label });
}

function isFresh(it) {
  if (!S.summAt.size || reduceMotion()) return false;
  const prev = S.summAt.get(it.id);
  return prev !== undefined && !!it.summarized_at && prev !== it.summarized_at;
}

function rowEl(it, ctx) {
  const ball = ctx.balls.get(it.id);
  const tr = trackFor(it);
  const navKey = "r:" + it.id;
  const selected = S.drawer && S.drawer.kind === "item" && S.drawer.id === it.id;
  const cls = ["row", "row--acts"];
  if (ball && ball.mine) cls.push("row--mine");
  else if (ball && ball.key === "stop") cls.push("row--stop");
  if (it.hidden) cls.push("row--hidden");
  if (selected) cls.push("row--selected");
  if (isFresh(it)) cls.push("row--fresh");
  const main = el("button", {
    type: "button", class: "row__main", "data-nav": navKey, "data-item": it.id,
    "aria-current": selected ? "true" : null,
    title: it.title + (it.title_source ? "\nタイトルの出典: " + it.title_source : ""),
    onclick: (ev) => openItem(it.id, navKey, ev),
  }, [
    el("span", { class: "row__title" }, [ticketTag(it), it.title]),
    el("span", { class: "row__sub" }, [
      ticketMark(it),
      trackEl(tr),
      el("span", { class: "stage", title: "フェーズ: " + it.phase + (it.phase_source ? " (根拠: " + it.phase_source + ")" : ""), text: tr.stage }),
      ballEl(ball, it),
      it.hidden ? el("span", { class: "tag", text: "非表示" }) : null,
      el("span", { class: "row__time", title: absTime(itemTime(it)), text: relTime(itemTime(it), ctx.now) }),
    ]),
  ]);
  const pick = pickButton(it);
  let jump = null;
  if (!pick && canFocus() && (it.tabs || []).length) {
    const t = it.tabs[0];
    jump = el("button", { type: "button", class: "row__jump", "aria-label": "タブへ移動",
      title: "タブへ移動: " + tabTitle(t), onclick: () => focusTab(t, it) }, [icon("jump")]);
    cls.push("row--jump");
  }
  if (pick) cls.splice(cls.indexOf("row--acts"), 1, "row--picking");
  return dropTarget(el("article", { class: cls.join(" "), "data-id": it.id }, [
    main,
    pick ? null : actionsEl(it, ctx, "r-" + it.id),
    pick || jump,
  ]), it);
}

// チケットの印 (案件の名前と、中の会話の数)。一覧の行では 2 行目の先頭、完了の行ではタイトルの前に付ける
// ボードの行のタイトルの前に付ける「チケット」の札。開く前に、会話 1 つの作業と見分けられるようにする
function ticketTag(it) {
  return it.kind === "ticket" ? el("span", { class: "ttag", text: "チケット" }) : null;
}

function ticketMark(it) {
  if (it.kind !== "ticket") return null;
  const t = it.ticket || {};
  const n = (it.members || []).length;
  return el("span", { class: "tmark", title: "チケット" + (t.project ? " · 案件: " + t.project : "") + " · 会話 " + n + " 件" },
    [t.project ? t.project + " · " : "", "会話 " + n]);
}

// ------------------------------------------------------------------ チケットに入れる (ドラッグ＆ドロップと 2 段階の操作)

async function runDrop(src, dst) {
  const a = dropAction(src, dst);
  if (!a) return;
  if (a.confirm) {
    openMergeTicket(src, dst, a);
    return;
  }
  try {
    const r = await api(a.path, a.body);
    S.ticketSource = null;
    toast(a.message);
    await poll(true);
    if (r && r.ticket_id) startRename(r.ticket_id);
  } catch (e) {
    toast(errorText(e, "チケットへの追加"), { error: true });
  }
}

// 会話の作業はドラッグでき、会話とチケットの作業はどちらも落とし先になる
function dropTarget(node, it) {
  // 会話の行もチケットの行もドラッグできる (チケットはチケットにだけ落とせる)
  node.setAttribute("draggable", "true");
  node.addEventListener("dragstart", (ev) => {
    S.dragId = it.id;
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", it.title);
    node.classList.add("row--dragging");
  });
  node.addEventListener("dragend", () => {
    S.dragId = null;
    node.classList.remove("row--dragging");
    if (S.pendingRender) render();
  });
  const can = () => !!(S.dragId && dropAction(currentCtxItem(S.dragId), it));
  node.addEventListener("dragover", (ev) => {
    if (!can()) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "move";
    node.classList.add("row--drop");
  });
  node.addEventListener("dragleave", (ev) => {
    if (!node.contains(ev.relatedTarget)) node.classList.remove("row--drop");
  });
  node.addEventListener("drop", (ev) => {
    node.classList.remove("row--drop");
    if (!can()) return;
    ev.preventDefault();
    const src = currentCtxItem(S.dragId);
    S.dragId = null;
    runDrop(src, it);
  });
  return node;
}

function currentCtxItem(id) {
  const items = (S.data && S.data.items) || [];
  return items.find((x) => x.id === id) || null;
}

// 「チケットに入れる」を押した後、入れ先の行に出すボタン
function pickButton(it) {
  const src = S.ticketSource ? currentCtxItem(S.ticketSource.id) : null;
  if (!src || !dropAction(src, it)) return null;
  return el("button", { type: "button", class: "btn btn--sm", text: src.kind === "ticket" ? "ここに取り込む" : "ここに入れる",
    onclick: () => runDrop(src, it) });
}

function startPick(it) {
  S.ticketSource = { id: it.id, title: it.title, ticket: it.kind === "ticket" };
  render();
  toast(it.kind === "ticket" ? "取り込み先のチケットで［ここに取り込む］を押してください。"
    : "入れ先の作業かチケットで［ここに入れる］を押してください。会話の作業に入れると、チケットを新しく作成します。");
}

// チケットを別のチケットに取り込む (確認ダイアログ)。取り込んだほうのチケットは削除されるので、確認してから実行する
function openMergeTicket(src, dst, a) {
  if (cleanupDlg.running) return;
  closeCleanup(false);
  hideTooltip();
  cleanupDlg.opener = document.activeElement;
  const n = (src.members || []).length;
  const lines = [
    n ? "「" + clipText(src.title, 40) + "」の会話 " + n + " 件を、「" + clipText(dst.title, 40) + "」に移します" : "「" + clipText(src.title, 40) + "」に会話はありません",
    "「" + clipText(dst.title, 40) + "」の名前・案件・表示する列・フェーズは、そのままです",
    "「" + clipText(src.title, 40) + "」の URL とメモは、「" + clipText(dst.title, 40) + "」のメモの後ろに書き足します (URL が無いチケットなら、URL として入れます)",
    "そのあと「" + clipText(src.title, 40) + "」を削除します。会話の記録・タブ・ブランチは変更しません",
  ];
  const body = el("div", { class: "dialog__body", id: "tkm-desc" }, [
    el("ul", { class: "cleanup" }, lines.map((x) => el("li", { class: "cleanup__line" }, [el("span", { text: x })]))),
  ]);
  const cancel = el("button", { type: "button", class: "btn", text: "キャンセル", onclick: () => closeCleanup() });
  const run = el("button", { type: "button", class: "btn btn--primary", text: "取り込む" });
  const dialog = el("div", { class: "dialog dialog--narrow", role: "alertdialog", "aria-modal": "true",
    "aria-labelledby": "tkm-title", "aria-describedby": "tkm-desc" }, [
    el("div", { class: "dialog__head" }, [
      el("h2", { class: "dialog__title", id: "tkm-title", text: "「" + clipText(dst.title, 40) + "」に取り込みますか？" }),
      el("div", { class: "hdr__spacer" }),
      el("button", { type: "button", class: "icon-btn", "aria-label": "閉じる (Esc)", onclick: () => closeCleanup() }, [icon("close")]),
    ]),
    body,
    el("div", { class: "dialog__foot" }, [cancel, run]),
  ]);
  dialog.addEventListener("keydown", (ev) => trapFocusIn(dialog, ev));
  const scrim = el("div", { class: "scrim" }, [dialog]);
  scrim.addEventListener("mousedown", (ev) => { if (ev.target === scrim) closeCleanup(); });
  $("app").appendChild(scrim);
  cleanupDlg.node = scrim;
  cancel.focus();
  run.addEventListener("click", async () => {
    if (cleanupDlg.running) return;
    cleanupDlg.running = true;
    try {
      await api(a.path, a.body);
      cleanupDlg.running = false;
      closeCleanup(false);
      S.ticketSource = null;
      toast(a.message);
      await poll(true);
      openItem(dst.id, null);
    } catch (e) {
      cleanupDlg.running = false;
      toast(errorText(e, "チケットの取り込み"), { error: true });
    }
  });
}

function renderPickbar() {
  const bar = $("pickbar");
  if (S.ticketSource && !currentCtxItem(S.ticketSource.id)) S.ticketSource = null;
  if (!S.ticketSource) {
    bar.hidden = true;
    bar.replaceChildren();
    return;
  }
  bar.hidden = false;
  bar.replaceChildren(
    el("span", { text: S.ticketSource.ticket
      ? "『" + clipText(S.ticketSource.title, 60) + "』を取り込む先のチケットで［ここに取り込む］を押してください"
      : "『" + clipText(S.ticketSource.title, 60) + "』を入れる先の作業かチケットで［ここに入れる］を押してください" }),
    el("button", { type: "button", class: "btn btn--sm", text: "キャンセル", onclick: () => { S.ticketSource = null; render(); } }),
  );
}

// 「チケットを作成」: 名前と列を入れるダイアログ。「作成」を押したときだけ作る (キャンセルなら何も作らない)
function openCreateTicket() {
  if (cleanupDlg.running) return;
  closeCleanup(false);
  hideTooltip();
  cleanupDlg.opener = document.activeElement;
  const nameIn = el("input", { id: "tkc-name", class: "input", type: "text", maxlength: "200", placeholder: "例: ログイン画面の修正" });
  const col = el("select", { id: "tkc-col", class: "select" });
  for (const t of (S.data && S.data.targets) || []) col.appendChild(el("option", { value: t, text: colLabel(t) }));
  // 狭い画面で 1 列ずつ見ているときは、その列を選んでおく
  if (S.narrow && S.activeTab) col.value = S.activeTab;
  const err = el("p", { class: "field__error", role: "alert", hidden: true });
  const cancel = el("button", { type: "button", class: "btn", text: "キャンセル", onclick: () => closeCleanup() });
  const run = el("button", { type: "button", class: "btn btn--primary", text: "作成" });
  const create = async () => {
    if (cleanupDlg.running) return;
    if (!nameIn.value.trim()) {
      err.textContent = "チケットの名前を入力してください。";
      err.hidden = false;
      nameIn.focus();
      return;
    }
    cleanupDlg.running = true;
    try {
      const r = await api("/api/tickets/create", { name: nameIn.value, session_ids: [], target: col.value });
      cleanupDlg.running = false;
      closeCleanup(false);
      await poll(true);
      openItem(r.ticket_id, null);
      toast("チケットを作成しました。会話の行をこのチケットの行にドラッグすると、会話を入れられます。");
    } catch (e) {
      cleanupDlg.running = false;
      toast(errorText(e, "チケットの作成"), { error: true });
    }
  };
  run.addEventListener("click", create);
  nameIn.addEventListener("keydown", (ev) => {
    if (ev.isComposing) return; // 変換を確定する Enter では作らない
    if (ev.key === "Enter") { ev.preventDefault(); create(); }
  });
  const dialog = el("div", { class: "dialog dialog--narrow", role: "dialog", "aria-modal": "true", "aria-labelledby": "tkc-title" }, [
    el("div", { class: "dialog__head" }, [
      el("h2", { class: "dialog__title", id: "tkc-title", text: "チケットを作成" }),
      el("div", { class: "hdr__spacer" }),
      el("button", { type: "button", class: "icon-btn", "aria-label": "閉じる (Esc)", onclick: () => closeCleanup() }, [icon("close")]),
    ]),
    el("div", { class: "dialog__body" }, [
      el("div", { class: "field" }, [el("label", { for: "tkc-name", text: "名前" }), nameIn, err]),
      el("div", { class: "field" }, [el("label", { for: "tkc-col", text: "表示する列" }), col]),
    ]),
    el("div", { class: "dialog__foot" }, [cancel, run]),
  ]);
  dialog.addEventListener("keydown", (ev) => trapFocusIn(dialog, ev));
  const scrim = el("div", { class: "scrim" }, [dialog]);
  scrim.addEventListener("mousedown", (ev) => { if (ev.target === scrim) closeCleanup(); });
  $("app").appendChild(scrim);
  cleanupDlg.node = scrim;
  nameIn.focus();
}

function groupOpen(id) {
  return S.query ? true : S.openGroups.has(id);
}

function dayRange(list, now) {
  const days = list.map((it) => {
    const t = parseTs(itemTime(it));
    return t === null ? null : Math.floor((now - t) / 86400000);
  }).filter((x) => x !== null);
  if (!days.length) return "";
  const lo = Math.min(...days);
  const hi = Math.max(...days);
  const f = (n) => (n === 0 ? "今日" : n + "日前");
  if (lo === hi) return f(lo);
  return (lo === 0 ? "今日" : lo) + "〜" + f(hi);
}

function doneRow(it, ctx) {
  const navKey = "d:" + it.id;
  const pr = (it.prs || [])[0];
  const pick = pickButton(it);
  return dropTarget(el("li", { class: "drow" + (pick ? "" : " drow--acts") + (it.hidden ? " drow--hidden" : "") }, [
    el("button", { type: "button", class: "drow__title", "data-nav": navKey, "data-item": it.id,
      title: it.title + (pr && pr.title ? "\n" + pr.label + " #" + pr.number + ": " + pr.title : ""),
      onclick: (ev) => openItem(it.id, navKey, ev) }, [ticketTag(it), ticketMark(it), it.title]),
    el("span", { class: "drow__time", title: absTime(itemTime(it)), text: relTime(itemTime(it), ctx.now) }),
    pick || actionsEl(it, ctx, "d-" + it.id),
  ]), it);
}

function emptyColumn(c) {
  const d = S.data;
  if (!d.generated_at && d.progress && d.progress.running) return el("div", { class: "empty", text: "集めています…" });
  if (S.query && c.all.length) {
    return el("div", { class: "empty" }, [
      el("span", { class: "empty__title", text: "「" + S.query + "」に一致する作業はありません。" }),
      el("span", { class: "empty__actions" }, [
        el("button", { type: "button", class: "btn btn--sm", text: "検索を解除", onclick: clearSearch }),
        S.filters.showHidden ? null : el("button", { type: "button", class: "btn btn--sm", text: "非表示の作業も探す",
          onclick: () => { S.filters.showHidden = true; saveFilters(); render(); } }),
      ]),
    ]);
  }
  if (c.all.length && (c.filtered || S.onlyMine || S.stopOnly)) {
    const n = c.all.length;
    return el("div", { class: "empty" }, [
      el("span", { class: "empty__title", text: n + " 件を絞り込みで表示していません" }),
      el("button", { type: "button", class: "btn btn--sm", text: "絞り込みを解除", onclick: resetFilters }),
    ]);
  }
  return el("div", { class: "empty", text: "作業はありません。" });
}

function columnEl(c, ctx, index) {
  const parts = [];
  if (c.open.length) parts.push("進行中 " + c.open.length);
  if (c.done.length) parts.push("完了 " + c.done.length);
  const shown = c.open.length + c.done.length;
  const countText = parts.length ? parts.join(" · ") : (c.all.length ? "0 / " + c.all.length + " 件を表示" : "0 件");
  const headId = "col-h-" + index;
  const body = el("div", { class: "col__body" });

  if (c.open.length) {
    const stack = el("div", { class: isWideColumn(c.open.length, c.layout, ctx.wideMin) ? "stack--2" : "stack" });
    for (const it of c.open) stack.appendChild(rowEl(it, ctx));
    body.appendChild(stack);
  }

  if (c.done.length) {
    const doneId = "done:" + c.target;
    const det = el("details", { class: "done" });
    det.open = groupOpen(doneId);
    det.addEventListener("toggle", () => {
      if (S.query) return; // 検索中の自動の開閉は保存しない
      if (det.open) S.openGroups.add(doneId); else S.openGroups.delete(doneId);
      saveOpenGroups();
    });
    const list = el("ul", { class: "done__list" });
    for (const it of c.done) list.appendChild(doneRow(it, ctx));
    det.append(
      el("summary", {}, [icon("chev", "ico--sm done__chev"), "完了 " + c.done.length,
        el("span", { class: "done__hint", text: dayRange(c.done, ctx.now) })]),
      list,
    );
    body.appendChild(det);
  }

  if (!shown) body.appendChild(emptyColumn(c));

  const loose = ctx.loose.get(c.target) || [];
  if (loose.length) {
    const lid = "loose:" + c.target;
    const det = el("details", { class: "loose" });
    det.open = S.openLoose.has(lid);
    det.addEventListener("toggle", () => { if (det.open) S.openLoose.add(lid); else S.openLoose.delete(lid); });
    det.append(
      el("summary", {}, [icon("chev", "ico--sm"), "作業と結び付いていないタブ " + loose.length]),
      el("ul", { class: "loose__list" }, looseTabNames(loose).map((x) => el("li", { class: "ltab" }, [
        el("span", { class: "ltab__kind", title: x.kind === "claude" ? "Claude" : "シェル" }, [icon(x.kind === "claude" ? "claude" : "shell", "ico--sm")]),
        el("span", { class: "ltab__main" }, [
          el("span", { class: "ltab__name", title: tabTitle(x.tab) }, [
            x.name, el("span", { class: "ltab__repo", text: " · " + (x.tab.repo || "") + (x.worktree ? " · worktree" : "") }),
          ]),
          el("span", { class: "ltab__sub" }, [
            x.kind === "claude" ? null : el("span", { class: "ltab__state", text: "Claude は停止中" }),
            x.place ? el("span", { class: "ltab__place", title: x.tab.surface_ref || null, text: x.place }) : null,
            el("span", { class: "ltab__path", title: x.tab.cwd || null, text: x.path }),
          ]),
        ]),
        canFocus() ? el("button", { type: "button", class: "btn btn--sm", text: "移動",
          "aria-label": x.name + (x.place ? " (" + x.place + ")" : "") + " のタブへ移動",
          onclick: () => focusTab(x.tab, null) }) : null,
      ]))),
    );
    body.appendChild(det);
  }

  const name = colNameEl(c, headId);
  // 色は CSSOM で変数に入れる (style 属性は使わない。CSP の style-src 'self' のまま動く)
  if (c.color) name.style.setProperty("--proj-color", c.color);
  return el("section", { class: "col", "data-target": c.target, "aria-labelledby": headId }, [
    el("div", { class: "col__head" }, [
      name,
      el("span", { class: "col__count", text: countText }),
      c.mine ? el("span", { class: "col__mine ball ball--mine", title: "自分の対応待ち", text: "自分 " + c.mine }) : null,
    ]),
    body,
  ]);
}

// 列の見出し。押すとその場で表示名を変えられる (targets.json の label に書く)。「その他」の列は変えない
function colNameEl(c, headId) {
  const cls = "col__name" + (c.color ? " col__name--color" : "");
  const tip = (c.label !== c.target ? "設定の名前: " + c.target + "\n" : "") + (c.cloud ? "クラウド: " + c.cloud + "\n" : "");
  if (c.other) return el("h2", { class: cls, id: headId, text: c.label, title: tip || null });
  if (S.renameCol !== c.target) {
    return el("h2", { class: cls, id: headId }, [
      el("button", { type: "button", class: "col__namebtn", title: tip + "押すと、列の表示名を変更できます",
        "aria-label": c.label + " (押すと、列の表示名を変更できます)",
        onclick: () => startColRename(c.target) }, [c.label]),
    ]);
  }
  const input = el("input", { id: "col-name-input", class: "input col__nameinput", type: "text", maxlength: "60",
    placeholder: c.target });
  input.value = c.label === c.target ? "" : c.label;
  const stop = () => {
    S.renameCol = null;
    input.blur(); // 入力欄にフォーカスがあると描き直しを待つので、先に外す
    render();
  };
  const save = () => act(async () => {
    await api("/api/columns/label", { name: c.target, label: input.value });
    toast(input.value.trim() ? "列の表示名を「" + input.value.trim() + "」にしました。" : "列の表示名を外して、「" + c.target + "」に戻しました。");
    stop();
  }, "列の表示名の変更");
  input.addEventListener("keydown", (ev) => {
    if (ev.isComposing) return; // 変換を確定する Enter では保存しない
    if (ev.key === "Enter") { ev.preventDefault(); save(); }
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); stop(); }
  });
  return el("div", { class: "col__namerow", id: headId }, [
    el("label", { class: "sr-only", for: "col-name-input", text: "「" + c.target + "」の列の表示名 (空にすると「" + c.target + "」に戻します)" }),
    input,
    el("button", { type: "button", class: "btn btn--sm", text: "保存", onclick: save }),
    el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "キャンセル", onclick: stop }),
  ]);
}

function startColRename(name) {
  S.renameCol = name;
  render();
  const input = $("col-name-input");
  if (input) {
    input.focus();
    input.select();
  }
}

function renderBoard(ctx) {
  const board = $("board");
  const scroll = new Map();
  for (const col of board.querySelectorAll(".col")) {
    const b = col.querySelector(".col__body");
    if (b) scroll.set(col.getAttribute("data-target"), b.scrollTop);
  }
  const left = board.scrollLeft;
  board.replaceChildren();
  const ptabs = $("ptabs");
  if (!S.data) {
    ptabs.hidden = true;
    board.className = "board board--loading";
    board.style.gridTemplateColumns = "";
    board.appendChild(el("div", { class: "col col--loading" }, [
      el("div", { class: "banner banner--info", role: "status" }, [
        el("span", { class: "spinner", "aria-hidden": "true" }),
        el("span", { class: "banner__text" }, [el("b", { text: "集めています" }), (term().name === "none" ? "" : term().label + " のタブ · ") + "会話の記録 · git · GitHub。10 秒ほどで表示します。"]),
      ]),
      ...["a", "c", "a"].map((w) => el("div", { class: "skel-row", "aria-hidden": "true" }, [
        el("div", { class: "skel skel--" + w }), el("div", { class: "skel skel--b" })])),
    ]));
    return;
  }
  board.className = "board";
  const cols = ctx.targets.map((t) => ctx.cols.get(t));
  if (S.narrow) {
    if (!S.activeTab || !ctx.cols.has(S.activeTab)) S.activeTab = ctx.targets[0];
    renderPtabs(ctx, cols);
    const i = ctx.targets.indexOf(S.activeTab);
    board.setAttribute("role", "tabpanel");
    board.setAttribute("aria-labelledby", "ptab-" + i);
    board.style.gridTemplateColumns = "minmax(0, 1fr)";
    board.appendChild(columnEl(ctx.cols.get(S.activeTab), ctx, i));
  } else {
    ptabs.hidden = true;
    ptabs.replaceChildren();
    board.removeAttribute("role");
    board.removeAttribute("aria-labelledby");
    board.style.gridTemplateColumns = columnTemplate(cols.map((c) => c.open.length), cols.map((c) => c.layout),
      ctx.wideMin);
    cols.forEach((c, i) => board.appendChild(columnEl(c, ctx, i)));
  }
  for (const col of board.querySelectorAll(".col")) {
    const t = col.getAttribute("data-target");
    if (scroll.has(t)) col.querySelector(".col__body").scrollTop = scroll.get(t);
  }
  board.scrollLeft = left;
}

function renderPtabs(ctx, cols) {
  const ptabs = $("ptabs");
  ptabs.hidden = false;
  const buttons = cols.map((c, i) => {
    const sel = c.target === S.activeTab;
    return el("button", {
      type: "button", class: "ptab", role: "tab", id: "ptab-" + i, "aria-selected": sel ? "true" : "false",
      "aria-controls": "board", tabindex: sel ? "0" : "-1",
      onclick: () => { S.activeTab = c.target; render(); },
      onkeydown: (ev) => {
        if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
        ev.preventDefault();
        const j = (i + (ev.key === "ArrowRight" ? 1 : -1) + cols.length) % cols.length;
        S.activeTab = cols[j].target;
        render();
        const n = $("ptab-" + j);
        if (n) n.focus();
      },
    }, [c.label, " ", el("span", { class: "ptab__n", text: String(c.open.length) }),
      c.mine ? el("span", { class: "ball ball--mine", text: String(c.mine), title: "自分の対応待ち" }) : null]);
  });
  ptabs.replaceChildren(...buttons);
}

// ------------------------------------------------------------------ 詳細パネル

function openItem(id, navKey, ev) {
  if (ev) ev.stopPropagation();
  if (S.renameItem && S.renameItem !== id) S.renameItem = null;
  S.openMore.delete(id + ":files");
  S.drawer = { kind: "item", id, opener: navKey || null };
  S.drawerScroll = 0;
  S.drawerShown = null;
  render();
  const t = $("drawer-title");
  if (t) t.focus();
}

function closeDrawer(restoreFocus) {
  if (!S.drawer) return;
  S.renameItem = null;
  const opener = S.drawer.opener;
  S.drawer = null;
  render();
  if (restoreFocus !== false && opener) {
    const n = document.querySelector('[data-nav="' + cssEscape(opener) + '"]');
    if (n) n.focus();
  }
}

function renderDrawer(ctx) {
  const dr = $("drawer");
  const oldBody = dr.querySelector(".drawer__body");
  const key = S.drawer ? S.drawer.kind + ":" + S.drawer.id : null;
  if (oldBody && S.drawerShown === key) S.drawerScroll = oldBody.scrollTop;
  if (!S.drawer) {
    dr.hidden = true;
    dr.replaceChildren();
    S.drawerShown = null;
    return;
  }
  const it = S.drawer.kind === "item" ? ctx.byId.get(S.drawer.id) : null;
  const parts = it ? (it.kind === "ticket" ? ticketDrawer(it, ctx) : itemDrawer(it, ctx)) : null;
  if (!parts) {
    // 作業が消えた (期間外・ゴミ箱へ移したなど)
    S.drawer = null;
    dr.hidden = true;
    dr.replaceChildren();
    S.drawerShown = null;
    return;
  }
  dr.replaceChildren(resizeHandle(), ...parts);
  dr.hidden = false;
  const body = dr.querySelector(".drawer__body");
  if (body) body.scrollTop = S.drawerShown === key ? S.drawerScroll : 0;
  S.drawerShown = key;
}

// 詳細パネルの幅を当てる (CSS の変数 --drawer-w)。save なら次に開いたときのために保存する
function setDrawerWidth(w, save) {
  S.drawerWidth = clampDrawerWidth(w, window.innerWidth);
  document.documentElement.style.setProperty("--drawer-w", S.drawerWidth + "px");
  if (save) storageSet(STORE.drawerWidth, String(S.drawerWidth));
  const h = document.querySelector(".drawer__resize");
  if (h) h.setAttribute("aria-valuenow", String(S.drawerWidth));
}

// 詳細パネルの左の縁のつまみ: ドラッグで幅を変える。矢印キーで 40px ずつ、ダブルクリックで元の幅に戻す
function resizeHandle() {
  const h = el("div", { class: "drawer__resize", role: "separator", tabindex: "0", "aria-orientation": "vertical",
    "aria-label": "詳細パネルの幅 (ドラッグか左右の矢印キーで変える。ダブルクリックで元の幅に戻す)",
    "aria-valuemin": String(DRAWER_MIN), "aria-valuemax": String(clampDrawerWidth(1e9, window.innerWidth)),
    "aria-valuenow": String(S.drawerWidth), title: "ドラッグして幅を変えます。ダブルクリックで元の幅に戻します。" });
  h.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    h.setPointerCapture(ev.pointerId);
    S.resizing = true;
    document.body.classList.add("is-resizing");
    const move = (e) => setDrawerWidth(window.innerWidth - e.clientX, false);
    const end = () => {
      h.removeEventListener("pointermove", move);
      h.removeEventListener("pointerup", end);
      h.removeEventListener("pointercancel", end);
      S.resizing = false;
      document.body.classList.remove("is-resizing");
      setDrawerWidth(S.drawerWidth, true);
      if (S.pendingRender) render();
    };
    h.addEventListener("pointermove", move);
    h.addEventListener("pointerup", end);
    h.addEventListener("pointercancel", end);
  });
  h.addEventListener("dblclick", () => setDrawerWidth(DRAWER_DEFAULT, true));
  h.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    ev.preventDefault();
    ev.stopPropagation();
    setDrawerWidth(S.drawerWidth + (ev.key === "ArrowLeft" ? 40 : -40), true);
  });
  return h;
}

// 列の表示名 (targets.json の label)。無ければ列名
function colLabel(name) {
  const p = ((S && S.data && S.data.projects) || []).find((x) => x.name === name);
  return (p && p.label) || name;
}

function drawerTop(target, metaText) {
  return el("div", { class: "drawer__top" }, [
    el("span", { class: "ptag", text: colLabel(target) }),
    el("span", { class: "muted drawer__meta", text: metaText }),
    el("div", { class: "hdr__spacer" }),
    el("button", { type: "button", class: "icon-btn", "aria-label": "閉じる (Esc)", onclick: () => closeDrawer() }, [icon("close")]),
  ]);
}

function itemDrawer(it, ctx) {
  const ball = ctx.balls.get(it.id);
  const tabs = it.tabs || [];
  const sessions = it.sessions || [];
  const tm = term();
  const meta = relTime(itemTime(it), ctx.now) + (!tabs.length && sessions.length && tm.name !== "none" ? " · タブは閉じています" : "");
  const actions = el("div", { class: "drawer__actions" });
  if (tm.caps.focus) {
    tabs.forEach((t, i) => {
      actions.appendChild(i === 0
        ? el("button", { type: "button", class: "btn btn--jump", title: tabTitle(t),
          onclick: () => focusTab(t, it) }, [icon("jump", "ico--sm"), "タブへ移動"])
        : el("button", { type: "button", class: "btn", title: tabTitle(t),
          onclick: () => focusTab(t, it) }, ["タブ " + (i + 1)]));
    });
  }
  if (!tabs.length && sessions.length) {
    const resumeCmd = resumeCommand(sessions[0].session_id, sessions[0].cwd);
    actions.appendChild(el("button", { type: "button", class: "btn", title: RESUME_TIP + "\n" + resumeCmd,
      onclick: () => copyWithToast(resumeCmd) }, [icon("copy", "ico--sm"), "resume のコマンドをコピー"]));
  }
  for (const p of it.prs || []) {
    const url = safeGithubUrl(p.url);
    const label = (p.label || "PR") + " #" + p.number;
    actions.appendChild(url
      ? el("a", { class: "btn", href: url, target: "_blank", rel: "noopener noreferrer", title: (p.title || "") + " (" + prStateText(p) + ")" }, [label])
      : el("span", { class: "btn", "aria-disabled": "true", title: "GitHub の PR の URL ではないため、開けません。", text: label }));
  }
  const eds = editorButtons(it, editorName());
  for (const b of eds) {
    actions.appendChild(el("button", { type: "button", class: "btn", title: b.title + (b.note ? "\n" + b.note : ""),
      "aria-keyshortcuts": b === eds[0] ? "e" : null, onclick: () => openEditor(it, b.path) }, [b.label]));
  }
  const ticket = it.ticket_id ? ctx.byId.get(it.ticket_id) : null;
  const pick = pickButton(it);
  if (pick) actions.appendChild(pick);
  else if (!ticket) {
    actions.appendChild(el("button", { type: "button", class: "btn",
      title: "この会話を、ほかの会話かチケットと一緒にします。入れ先の作業で［ここに入れる］を押します。ボードの行をドラッグして、ほかの行に落としても入れられます。",
      onclick: () => startPick(it) }, ["チケットに入れる"]));
    actions.appendChild(el("button", { type: "button", class: "btn",
      title: "この会話だけが入ったチケットを作成します。あとから会話を入れられます。",
      onclick: () => createTicketFrom(it) }, ["この会話でチケットを作成"]));
  }
  const notes = eds.filter((b) => b.note);
  const head = el("div", { class: "drawer__head" }, [
    drawerTop(it.target, meta),
    titleEl(it),
    ticket ? el("p", { class: "drawer__note" }, [
      "チケット「" + clipText(ticket.title, 60) + "」に入っています。",
      el("button", { type: "button", class: "linkbtn", text: "チケットを表示", onclick: () => openItem(ticket.id, S.drawer && S.drawer.opener) }),
    ]) : null,
    actions.children.length ? actions : null,
    notes.length ? el("p", { class: "drawer__note muted" }, [notes.map((b) => (eds.length > 1 ? b.label.split(": ").slice(1).join(": ") + " の" : "クローンの") + b.note + " (切り替えはしません)").join(" · ")]) : null,
  ]);

  // 会話のまとめ (digest) があれば、いま / 次と流れ図の代わりに、その要約を出す (4.3)
  const sid = latestSessionId(it);
  const dgst = sid && S.data && S.data.digests ? S.data.digests[sid] : null;
  let top;
  if (dgst) {
    top = digestBlock(dgst, sid);
  } else {
    const nn = nowNext(it, ball);
    top = [
      el("dl", { class: "now" + (ball && ball.mine ? " now--mine" : "") }, [
        el("dt", { text: "いま" }), el("dd", { title: ball && ball.tip ? ball.tip : null, text: nn.now }),
        nn.next ? el("dt", { text: "次" }) : null, nn.next ? el("dd", { title: it.next_action || null, text: nn.next }) : null,
      ]),
      el("div", {}, [el("h3", { class: "sect__h", text: "流れ" }), renderFlow(flowModel(it, ball), it.id)]),
    ];
  }
  const recHint = ["目的", "経緯", "ブランチ " + (it.branches || []).length, "会話 " + sessions.length,
    "ファイル " + (it.files || []).length].join(" · ");
  const body = el("div", { class: "drawer__body" }, [
    ...top, askBlock(it),
    moreBlock(it, "rec", "詳細", recHint, () => recordBody(it)),
    moreBlock(it, "ops", "編集", "メモ · フェーズ", () => opsBody(it)),
  ]);
  return [head, body];
}

// 詳細パネルを開き、上のタイトル (チケットなら名前) を入力できる状態にする (チケットを作った直後と、タイトルを押したとき)
function startRename(itemId) {
  S.renameItem = itemId;
  openItem(itemId, S.drawer && S.drawer.opener);
  const input = $("title-input");
  if (input) {
    input.focus();
    input.select();
  }
}

function stopRename() {
  S.renameItem = null;
  const input = $("title-input");
  if (input) input.blur(); // 入力欄にフォーカスがあると描き直しを待つので、先に外す
  render();
  const t = $("drawer-title");
  if (t) t.focus();
}

// タイトルを保存する。チケットは tickets.json の名前、会話の作業は上書きのタイトル (空なら自動のタイトルに戻す)
function titlePatch(it, value) {
  return it.kind === "ticket"
    ? { path: "/api/tickets/update", body: { ticket_id: it.id, patch: { name: value } } }
    : { path: "/api/override", body: { item_id: it.id, patch: { title: value.trim() } } };
}

// 詳細パネルの上のタイトル。押すとその場で入力欄になる (Enter で保存、Esc でやめる)
function titleEl(it) {
  const what = it.kind === "ticket" ? "名前" : "タイトル";
  if (S.renameItem !== it.id) {
    const src = it.kind !== "ticket" && it.title_source ? "\nタイトルの出典: " + it.title_source : "";
    return el("div", { class: "drawer__titlerow" }, [
      el("h2", { class: "drawer__title", id: "drawer-title", tabindex: "-1" }, [
        el("button", { type: "button", class: "drawer__titlebtn", title: "押すと" + what + "を変更できます" + src,
          "aria-label": it.title + " (押すと" + what + "を変更できます)", onclick: () => startRename(it.id) }, [it.title]),
      ]),
    ]);
  }
  const input = el("input", { id: "title-input", class: "input drawer__nameinput", type: "text", maxlength: "200" });
  input.value = it.title;
  const save = async (value) => {
    await act(async () => {
      const x = titlePatch(it, value);
      await api(x.path, x.body);
      S.renameItem = null;
      input.blur(); // 入力欄にフォーカスがあると描き直しを待つので、先に外す
      toast(value.trim() || it.kind === "ticket" ? what + "を変更しました。" : "自動のタイトルにしました。");
    }, what + "の変更");
    if (!S.renameItem) {
      const t = $("drawer-title");
      if (t) t.focus();
    }
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.isComposing) return; // 変換を確定する Enter では保存しない
    if (ev.key === "Enter") { ev.preventDefault(); save(input.value); }
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); stopRename(); }
  });
  return el("div", { class: "drawer__titlerow" }, [
    el("h2", { class: "sr-only", id: "drawer-title", tabindex: "-1", text: it.title }),
    el("label", { class: "sr-only", for: "title-input", text: it.kind === "ticket" ? "チケットの名前" : "作業のタイトル" }),
    input,
    el("button", { type: "button", class: "btn btn--sm", text: "保存", onclick: () => save(input.value) }),
    el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "キャンセル", onclick: stopRename }),
    it.kind !== "ticket" && it.title_source === "手動"
      ? el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "自動のタイトルを使う",
        title: "変更したタイトルを取り消して、要約や PR から決まるタイトルを使います。", onclick: () => save("") })
      : null,
  ]);
}

// この会話だけでチケットを作る。あとから会話を入れていく作業を、先にチケットにしておくため
async function createTicketFrom(it) {
  const sid = ((it.sessions || [])[0] || {}).session_id;
  if (!sid) return;
  try {
    const r = await api("/api/tickets/create", { name: it.title, session_ids: [sid] });
    await poll(true);
    startRename(r.ticket_id);
    toast("この会話でチケットを作成しました。名前を確認してください。");
  } catch (e) {
    toast(errorText(e, "チケットの作成"), { error: true });
  }
}

// チケットの URL は http(s) の形のものだけをリンクにする
function safeTicketUrl(url) {
  return typeof url === "string" && /^https?:\/\/[^\s]+$/.test(url) ? url : null;
}

function ticketDrawer(it, ctx) {
  const ball = ctx.balls.get(it.id);
  const t = it.ticket || {};
  const members = it.members || [];
  const actions = el("div", { class: "drawer__actions" });
  const url = safeTicketUrl(t.url);
  if (url) {
    actions.appendChild(el("a", { class: "btn", href: url, target: "_blank", rel: "noopener noreferrer", title: url },
      [icon("jump", "ico--sm"), "チケットを開く"]));
  }
  for (const p of it.prs || []) {
    const purl = safeGithubUrl(p.url);
    const label = (p.label || "PR") + " #" + p.number;
    actions.appendChild(purl
      ? el("a", { class: "btn", href: purl, target: "_blank", rel: "noopener noreferrer", title: (p.title || "") + " (" + prStateText(p) + ")" }, [label])
      : el("span", { class: "btn", "aria-disabled": "true", title: "GitHub の PR の URL ではないため、開けません。", text: label }));
  }
  const pick = pickButton(it);
  if (pick) actions.appendChild(pick);
  else {
    actions.appendChild(el("button", { type: "button", class: "btn",
      title: "このチケットの会話を、別のチケットに移して一緒にします。取り込み先のチケットで［ここに取り込む］を押します。ボードのチケットの行をドラッグして、別のチケットの行に落としても取り込めます。",
      onclick: () => startPick(it) }, ["別のチケットに取り込む"]));
  }
  const meta = ["チケット", t.project ? "案件: " + t.project : null, members.length ? relTime(itemTime(it), ctx.now) : null]
    .filter(Boolean).join(" · ");
  const head = el("div", { class: "drawer__head" }, [
    drawerTop(it.target, meta),
    titleEl(it),
    actions.children.length ? actions : null,
  ]);
  const log = el("ol", { class: "tl" }, members.map((m) => memberEl(it, m, ctx)));
  const logHint = "会話 " + members.length + (it.missing_sessions ? " (ほかに期間外 " + it.missing_sessions + ")" : "");
  if (!members.length) {
    return [head, el("div", { class: "drawer__body" }, [
      el("p", { class: "muted", text: "まだ会話が入っていません。会話の行をこのチケットの行にドラッグするか、会話の詳細パネルの［チケットに入れる］から入れてください。" }),
      moreBlock(it, "ops", "編集", "URL · 案件 · 列 · メモ · フェーズ · 削除", () => ticketOpsBody(it)),
    ])];
  }
  // チケットのまとめがまだ無いときに出す文
  const waiting = it.llm_status === "pending" ? "まとめを作っています。" : "まだまとめがありません。";
  const story = !!(it.background || it.state_now);
  // 中の会話が変わって、まとめを作り直しているところ (それまでは前のまとめを出す)
  const redoing = story && it.llm_item_refresh;
  const nn = nowNext(it, ball);
  const body = el("div", { class: "drawer__body" }, [
    el("section", { class: "tsect" }, [
      el("h3", { class: "sect__h", text: "どういう話か" }),
      el("p", { class: "tsect__text" + (story ? "" : " muted"), text: it.background || it.goal || waiting }),
      redoing ? el("p", { class: "drawer__note muted", role: "status",
        text: "中の会話が変わったので、まとめを作り直しています。終わるまでは前のまとめを出しています (新しく入った会話は、まだ進み方に入っていません)。" }) : null,
    ]),
    el("section", { class: "tsect" }, [
      el("h3", { class: "sect__h", text: "今どうなっているか" }),
      el("dl", { class: "now" + (ball && ball.mine ? " now--mine" : "") }, [
        el("dt", { text: "いま" }), el("dd", { title: ball && ball.tip ? ball.tip : null, text: nn.now }),
        nn.next ? el("dt", { text: "次" }) : null, nn.next ? el("dd", { title: it.next_action || null, text: nn.next }) : null,
      ]),
      it.state_now ? el("p", { class: "tsect__text", text: it.state_now }) : null,
    ]),
    el("section", { class: "tsect" }, [
      el("h3", { class: "sect__h", text: "進み方" }),
      graphEl(it, members),
      stepsEl(it, members),
    ]),
    askBlock(it),
    moreBlock(it, "log", "会話の記録", logHint, () => el("div", { class: "more__body" }, [log])),
    moreBlock(it, "rec", "詳細", ["PR", "ブランチ", "メモ", "ファイル " + (it.files || []).length].join(" · "), () => recordBody(it)),
    moreBlock(it, "ops", "編集", "URL · 案件 · 列 · メモ · フェーズ · 削除", () => ticketOpsBody(it)),
  ]);
  return [head, body];
}

function svgEl(tag, attrs, children) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) n.setAttribute(k, String(v));
  for (const c of children || []) if (c) n.appendChild(c);
  return n;
}

// 会話をつないだ図: 上から始めた順、続きは同じ列で下へ、分かれた会話は右の列へ。箱を押すと、会話の記録でその会話を開く
const GRAPH = { colW: 200, nodeW: 184, nodeH: 46, rowH: 66 };
function graphEl(it, members) {
  const g = sessionGraph(members, it.links);
  const W = g.lanes * GRAPH.colW - (GRAPH.colW - GRAPH.nodeW) + 2;
  const H = g.nodes.length * GRAPH.rowH - (GRAPH.rowH - GRAPH.nodeH) + 2;
  const at = new Map(g.nodes.map((x) => [x.sid, { x: 1 + x.lane * GRAPH.colW, y: 1 + x.row * GRAPH.rowH }]));
  const svg = svgEl("svg", { class: "sgraph", width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img",
    "aria-label": "会話のつながり: " + g.edges.map((e) => "会話 " + nodeNo(g, e.from) + " から会話 " + nodeNo(g, e.to) + " (" + e.kind + ")").join("、") });
  for (const e of g.edges) {
    const a = at.get(e.from);
    const b = at.get(e.to);
    const x1 = a.x + GRAPH.nodeW / 2;
    const y1 = a.y + GRAPH.nodeH;
    const x2 = b.x + GRAPH.nodeW / 2;
    const y2 = b.y;
    const my = (y1 + y2) / 2;
    svg.appendChild(svgEl("path", { class: "sgraph__edge" + (e.kind === "分かれた" ? " sgraph__edge--branch" : ""),
      d: "M" + x1 + " " + y1 + " C" + x1 + " " + my + " " + x2 + " " + my + " " + x2 + " " + y2 }));
  }
  for (const x of g.nodes) {
    const p = at.get(x.sid);
    const s = (x.item.sessions || [])[0] || {};
    const node = svgEl("g", { class: "sgraph__node" + (openMemberOf(it) === x.item.id ? " sgraph__node--open" : ""), tabindex: "0", role: "button",
      "aria-label": "会話 " + x.n + ": " + x.item.title + " (押すと、会話の記録でこの会話を開きます)" }, [
      svgEl("title", {}, [document.createTextNode(x.item.title + "\n" + absTime(s.first_ts) + " 〜 " + absTime(s.last_ts))]),
      svgEl("rect", { x: p.x, y: p.y, width: GRAPH.nodeW, height: GRAPH.nodeH, rx: 8 }),
      svgEl("text", { x: p.x + 10, y: p.y + 18, class: "sgraph__no" }, [document.createTextNode("会話 " + x.n + " · " + fmtShort(s.first_ts))]),
      svgEl("text", { x: p.x + 10, y: p.y + 36, class: "sgraph__title" }, [document.createTextNode(clipText(x.item.title, 14))]),
    ]);
    const go = () => openMemberInLog(it, x.item);
    node.addEventListener("click", go);
    node.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); } });
    svg.appendChild(node);
  }
  return el("div", { class: "sgraph__wrap" }, [
    el("div", { class: "sgraph__scroll" }, [svg]),
    g.edges.length ? el("p", { class: "muted sgraph__legend",
      text: "実線は続き (前の会話の作業を引き継いだ)、破線は分かれた (前の会話から出てきた別の作業)。" +
        (g.inferred ? "まとめがまだ無いので、つながりは時刻から決めています。" : "") }) : null,
  ]);
}

function nodeNo(g, sid) {
  const x = g.nodes.find((n) => n.sid === sid);
  return x ? x.n : "?";
}

// 図の箱を押したとき: 会話の記録を開き、その会話を開いて見える所まで動かす
function openMemberInLog(ticket, m) {
  S.openMore.add(ticket.id + ":log");
  S.ticketOpen.set(ticket.id, m.id);
  render();
  const node = document.getElementById("tl-" + m.id);
  if (node) {
    node.scrollIntoView({ block: "nearest", behavior: reduceMotion() ? "auto" : "smooth" });
    const b = node.querySelector(".tl__toggle");
    if (b) b.focus();
  }
}

// 進み方の段: 古い順に、段の名前・どの会話でやったか・説明
const STEP_STATE = { done: "済", now: "いま", next: "次", later: "この先" };
function stepsEl(it, members) {
  const steps = it.flow_steps || [];
  if (!steps.length) return null;
  const no = new Map(members.map((m, i) => [((m.sessions || [])[0] || {}).session_id, i + 1]));
  return el("ol", { class: "tsteps" }, steps.map((st) => el("li", { class: "tsteps__item tsteps__item--" + st.state }, [
    el("div", { class: "tsteps__head" }, [
      el("span", { class: "tsteps__state", text: STEP_STATE[st.state] || "" }),
      el("span", { class: "tsteps__label", text: st.label_full || st.label }),
      st.session && no.has(st.session) ? el("span", { class: "ptag", text: "会話 " + no.get(st.session) }) : null,
    ]),
    st.detail ? el("p", { class: "tsteps__detail muted", text: st.detail }) : null,
  ])));
}

// チケットの会話の記録の 1 件 (古い順に並べる)。見出しを押すと、別のパネルに切り替えずに、その場でいま・流れ・詳細を開く。
// 開くのは一度に 1 つだけ (どれを見ているか分かるように、開いた会話は枠を太くし、図の箱も強調する)
function memberEl(ticket, m, ctx) {
  const s = (m.sessions || [])[0] || {};
  const ball = ctx.balls.get(m.id);
  const tabs = m.tabs || [];
  const btns = [];
  if (canFocus() && tabs.length) {
    btns.push(el("button", { type: "button", class: "btn btn--sm", title: tabTitle(tabs[0]), onclick: () => focusTab(tabs[0], m) },
      [icon("jump", "ico--sm"), "タブへ移動"]));
  } else if (s.session_id) {
    const cmd = resumeCommand(s.session_id, s.cwd);
    btns.push(el("button", { type: "button", class: "btn btn--sm", title: RESUME_TIP + "\n" + cmd, onclick: () => copyWithToast(cmd) },
      [icon("copy", "ico--sm"), "resume のコマンドをコピー"]));
  }
  // フェーズを手で決める (終わった会話のフェーズを「完了」にしておけば、チケットの「いま」や次に動く人の根拠から外れる)
  const phId = "tl-phase-" + m.id;
  const manual = m.phase_source === "手動";
  const ph = el("select", { id: phId, class: "select select--sm" });
  ph.appendChild(el("option", { value: "", text: manual ? "自動で決める" : "自動で決める (いまは「" + m.phase + "」)" }));
  for (const x of (S.data && S.data.phases) || []) {
    const opt = el("option", { value: x, text: x });
    if (manual && m.phase === x) opt.selected = true;
    ph.appendChild(opt);
  }
  ph.addEventListener("change", () => act(async () => {
    ph.blur(); // 入力欄にフォーカスがあると描き直しを待つので、先に外す
    await api("/api/override", { item_id: m.id, patch: { phase: ph.value } });
    toast(ph.value ? "この会話のフェーズを「" + ph.value + "」にしました。チケットのまとめを作り直します。"
      : "この会話のフェーズを自動で決めるようにしました。チケットのまとめを作り直します。");
  }, "フェーズの変更"));
  btns.push(el("span", { class: "tl__phase" }, [el("label", { for: phId, text: "フェーズ" }), ph]));
  btns.push(el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "チケットから外す",
    title: "この会話をチケットから外して、ボードに会話の作業として戻します。会話の記録はそのままです。",
    onclick: () => act(async () => {
      await api("/api/tickets/remove", { ticket_id: ticket.id, session_id: s.session_id });
      toast("チケットから外しました。");
    }, "チケットから外す操作") }));
  btns.push(el("button", { type: "button", class: "btn btn--ghost btn--sm", text: "ゴミ箱へ", "aria-haspopup": "dialog",
    onclick: () => openCleanup(m) }));
  const prs = (m.prs || []).map((p) => (p.label || "PR") + " #" + p.number + " " + prStateText(p)).join(" · ");
  const open = openMemberOf(ticket) === m.id;
  // 見出しの行全体が開閉のボタン。番号は「進み方」の図の「会話 N」と同じ
  const no = (ticket.members || []).indexOf(m) + 1;
  return el("li", { class: "tl__item" + (open ? " tl__item--open" : ""), id: "tl-" + m.id }, [
    el("button", { type: "button", class: "tl__toggle", "data-item": m.id, "aria-expanded": open ? "true" : "false",
      title: (open ? "閉じます" : "この会話のいま・流れ・詳細を開きます") + "\n" + absTime(s.first_ts) + " 〜 " + absTime(s.last_ts),
      onclick: () => { S.ticketOpen.set(ticket.id, open ? null : m.id); render(); } }, [
      icon("chev", "ico--sm tl__chev"),
      el("span", { class: "tl__no", text: "会話 " + no }),
      el("span", { class: "tl__title", text: m.title }),
      el("span", { class: "tl__time", text: fmtShort(s.first_ts) + " 〜 " + relTime(s.last_ts, ctx.now) }),
    ]),
    el("div", { class: "tl__meta" }, [
      m.phase_source === "手動" ? el("span", { class: "tag", title: "フェーズを手で決めています", text: m.phase }) : null,
      ballEl(ball, m), !open && memberLine(m) ? el("span", { class: "tl__line", text: memberLine(m) }) : null]),
    open && prs ? el("div", { class: "tl__prs muted", text: prs }) : null,
    open ? memberBody(m, ball) : null,
    open ? el("div", { class: "tl__acts" }, btns) : null,
  ]);
}

// チケットの会話の記録で開いている会話 (一度に 1 つだけ)。押して閉じたら null
function openMemberOf(ticket) {
  return S.ticketOpen.has(ticket.id) ? S.ticketOpen.get(ticket.id) : null;
}

// チケットの中で開いた会話の中身: いま / 次、流れ、詳細。会話のまとめがあれば、いま / 次と流れの代わりにその要約 (4.3)
function memberBody(m, ball) {
  const sid = latestSessionId(m);
  const dgst = sid && S.data && S.data.digests ? S.data.digests[sid] : null;
  let top;
  if (dgst) {
    top = digestBlock(dgst, sid);
  } else {
    const nn = nowNext(m, ball);
    top = [
      el("dl", { class: "now" + (ball && ball.mine ? " now--mine" : "") }, [
        el("dt", { text: "いま" }), el("dd", { title: ball && ball.tip ? ball.tip : null, text: nn.now }),
        nn.next ? el("dt", { text: "次" }) : null, nn.next ? el("dd", { title: m.next_action || null, text: nn.next }) : null,
      ]),
      el("div", {}, [el("h4", { class: "sect__h", text: "流れ" }), renderFlow(flowModel(m, ball), m.id)]),
    ];
  }
  return el("div", { class: "tl__body" }, [
    ...top,
    moreBlock(m, "rec", "詳細", ["目的", "経緯", "ブランチ " + (m.branches || []).length, "ファイル " + (m.files || []).length].join(" · "),
      () => recordBody(m)),
  ]);
}

// 「Claude Code に質問」: 会話の続きとして答えてもらう。質問と答えは会話の記録に残らず、画面を読み込み直すと消える
function askBlock(it) {
  const list = askSessions(it);
  if (!list.length) return null;
  const st = S.ask.get(it.id) || { sid: list[0].session_id, draft: "" };
  if (!list.some((x) => x.session_id === st.sid)) st.sid = list[0].session_id;
  S.ask.set(it.id, st);
  const log = S.askLog.get(st.sid) || [];
  const box = el("div", { class: "ask" }, [el("h3", { class: "sect__h", text: "Claude Code に質問" })]);
  if (list.length > 1) {
    const sid = "ask-sess-" + it.id;
    const sel = el("select", { id: sid, class: "select" });
    for (const x of list) {
      const opt = el("option", { value: x.session_id, text: clipText(x.title, 50) + " (" + fmtShort(x.last_ts) + ")" });
      if (x.session_id === st.sid) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.addEventListener("change", () => { st.sid = sel.value; sel.blur(); render(); });
    box.appendChild(el("div", { class: "field" }, [el("label", { for: sid, text: "質問する会話" }), sel]));
  }
  const ol = el("ol", { class: "ask__log" });
  for (const x of log) {
    ol.appendChild(el("li", { class: "ask__entry" }, [
      el("p", { class: "ask__q", text: x.q }),
      x.pending ? el("p", { class: "ask__a muted", role: "status", text: "答えを待っています。数秒から十数秒かかります。" })
        : x.error ? el("p", { class: "ask__a field__error", text: x.error })
          : el("p", { class: "ask__a", text: x.a }),
    ]));
  }
  if (log.length) box.appendChild(ol);
  const qid = "ask-input-" + it.id;
  const input = el("textarea", { id: qid, class: "textarea", rows: "2", maxlength: "2000",
    placeholder: "例: この作業は今どうなっていますか？" });
  input.value = st.draft;
  input.addEventListener("input", () => { st.draft = input.value; });
  const busy = log.some((x) => x.pending);
  const send = () => {
    const q = input.value.trim();
    if (!q || busy) return;
    st.draft = "";
    input.blur(); // 入力欄にフォーカスがあると描き直しを待つので、先に外す
    askClaude(st.sid, it.id, q);
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.isComposing) return;
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); send(); }
  });
  box.appendChild(el("div", { class: "field" }, [
    el("label", { for: qid, text: "質問を入力 (⌘ + Enter で送る)" }), input,
    el("div", { class: "field__row" }, [
      el("button", { type: "button", class: "btn btn--sm", text: "質問する", disabled: busy || null, onclick: send }),
    ]),
  ]));
  box.appendChild(el("p", { class: "drawer__note muted",
    text: "会話の続きとして Claude Code が答えます。質問と答えは会話の記録に残りません。ファイルは読むだけで、編集はしません。" +
      "タブで動いている会話では、まだ記録に書かれていない最新のやり取りは答えに入りません。" }));
  return box;
}

async function askClaude(sid, itemId, q) {
  const log = S.askLog.get(sid) || [];
  S.askLog.set(sid, log);
  const history = askHistory(log);
  const entry = { q, a: null, error: null, pending: true };
  log.push(entry);
  render();
  try {
    const r = await api("/api/ask", { item_id: itemId, session_id: sid, question: q, history });
    entry.a = r.answer;
  } catch (e) {
    entry.error = errorText(e, "質問");
  }
  entry.pending = false;
  render();
}

// 「編集」(チケット): 名前・URL・案件・メモ・フェーズと、チケットを削除
function ticketOpsBody(it) {
  const t = it.ticket || {};
  const box = el("div", { class: "more__body" });
  const save = (patch, message, what) => act(async () => {
    await api("/api/tickets/update", { ticket_id: it.id, patch });
    toast(message);
  }, what);
  const field = (id, label, input, onSave) => el("div", { class: "field" }, [
    el("label", { for: id, text: label }),
    el("div", { class: "field__row" }, [input, el("button", { type: "button", class: "btn btn--sm", text: "保存", onclick: onSave })]),
  ]);
  const uid = "tk-url-" + it.id;
  const urlIn = el("input", { id: uid, class: "input", type: "url", maxlength: "2000", placeholder: "https://" });
  urlIn.value = t.url || "";
  box.appendChild(field(uid, "URL を変更 (タスク管理ツールのチケットなど)", urlIn,
    () => save({ url: urlIn.value }, urlIn.value.trim() ? "URL を保存しました。" : "URL を削除しました。", "URL の保存")));

  // 案件: 一度付けた名前から選ぶ。新しい名前は「新しい案件を追加」から入れる (表記のばらつきを防ぐ)
  const pid = "tk-proj-" + it.id;
  const projs = (S.data && S.data.ticket_projects) || [];
  const sel = el("select", { id: pid, class: "select" });
  sel.appendChild(el("option", { value: "", text: "なし" }));
  for (const name of projs) {
    const opt = el("option", { value: name, text: name });
    if (name === t.project) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.appendChild(el("option", { value: "__new__", text: "新しい案件を追加…" }));
  const newId = "tk-proj-new-" + it.id;
  const newIn = el("input", { id: newId, class: "input", type: "text", maxlength: "100", placeholder: "案件の名前" });
  const newRow = el("div", { class: "field__row", hidden: true }, [
    el("label", { class: "sr-only", for: newId, text: "新しい案件の名前" }), newIn,
    el("button", { type: "button", class: "btn btn--sm", text: "追加",
      onclick: () => save({ project: newIn.value }, "案件を「" + newIn.value.trim() + "」にしました。", "案件の変更") }),
  ]);
  sel.addEventListener("change", () => {
    if (sel.value === "__new__") {
      newRow.hidden = false;
      newIn.focus();
      return;
    }
    newRow.hidden = true;
    save({ project: sel.value }, sel.value ? "案件を「" + sel.value + "」にしました。" : "案件を外しました。", "案件の変更");
  });
  box.appendChild(el("div", { class: "field" }, [el("label", { for: pid, text: "案件を変更" }), sel, newRow]));

  const cid = "tk-col-" + it.id;
  const col = el("select", { id: cid, class: "select" });
  col.appendChild(el("option", { value: "", text: it.target_manual ? "中の会話から自動で決める" : "中の会話から自動で決める (いまは「" + colLabel(it.target) + "」)" }));
  for (const x of (S.data && S.data.targets) || []) {
    const opt = el("option", { value: x, text: colLabel(x) });
    if (it.target_manual && it.target === x) opt.selected = true;
    col.appendChild(opt);
  }
  col.addEventListener("change", () => save({ target: col.value },
    col.value ? "「" + colLabel(col.value) + "」の列に表示するようにしました。" : "列を中の会話から自動で決めるようにしました。", "列の変更"));
  box.appendChild(el("div", { class: "field" }, [el("label", { for: cid, text: "表示する列を変更" }), col]));

  const mid = "tk-memo-" + it.id;
  const memo = el("textarea", { id: mid, class: "textarea", rows: "3", maxlength: "4000" });
  memo.value = it.memo || "";
  box.appendChild(el("div", { class: "field" }, [
    el("label", { for: mid, text: "メモを編集" }), memo,
    el("div", { class: "field__row" }, [
      el("button", { type: "button", class: "btn btn--sm", text: "保存", onclick: () => save({ memo: memo.value }, "メモを保存しました。", "メモの保存") }),
    ]),
  ]));
  const phid = "tk-phase-" + it.id;
  const manual = it.phase_source === "手動";
  const ph = el("select", { id: phid, class: "select" });
  ph.appendChild(el("option", { value: "", text: manual ? "自動で決める" : "自動で決める (いまは「" + it.phase + "」)" }));
  for (const x of S.data.phases) {
    const opt = el("option", { value: x, text: x });
    if (manual && it.phase === x) opt.selected = true;
    ph.appendChild(opt);
  }
  ph.addEventListener("change", () => save({ phase: ph.value },
    ph.value ? "フェーズを「" + ph.value + "」に変更しました。" : "フェーズを自動で決めるようにしました。", "フェーズの変更"));
  box.appendChild(el("div", { class: "field" }, [el("label", { for: phid, text: "フェーズを変更" }), ph]));
  box.appendChild(el("div", { class: "field" }, [
    el("div", { class: "field__row" }, [
      el("button", { type: "button", class: "btn btn--danger btn--sm", text: "チケットを削除", "aria-haspopup": "dialog",
        title: "チケットを削除して、中の会話をボードに戻します。会話の記録は削除しません。",
        onclick: () => openDeleteTicket(it) }),
    ]),
  ]));
  return box;
}

// チケットを削除 (確認ダイアログ)。名前・URL・案件・メモが消えるので、確認してから実行する
function openDeleteTicket(it) {
  if (cleanupDlg.running) return;
  closeCleanup(false);
  hideTooltip();
  cleanupDlg.opener = document.activeElement;
  const n = (it.members || []).length;
  const lines = [
    "チケットの名前・URL・案件・メモを削除します",
    n ? "中の会話 " + n + " 件は、会話の作業としてボードに戻ります" : "中に会話はありません",
    "会話の記録・タブ・ブランチは変更しません",
  ];
  const body = el("div", { class: "dialog__body", id: "tkd-desc" }, [
    el("p", { class: "cleanup__lead", text: "「" + clipText(it.title, 60) + "」について、次のことをします。" }),
    el("ul", { class: "cleanup" }, lines.map((x) => el("li", { class: "cleanup__line" }, [el("span", { text: x })]))),
  ]);
  const cancel = el("button", { type: "button", class: "btn", text: "キャンセル", onclick: () => closeCleanup() });
  const run = el("button", { type: "button", class: "btn btn--danger-fill", text: "削除" });
  const dialog = el("div", { class: "dialog dialog--narrow", role: "alertdialog", "aria-modal": "true",
    "aria-labelledby": "tkd-title", "aria-describedby": "tkd-desc" }, [
    el("div", { class: "dialog__head" }, [
      el("h2", { class: "dialog__title", id: "tkd-title", text: "チケットを削除しますか？" }),
      el("div", { class: "hdr__spacer" }),
      el("button", { type: "button", class: "icon-btn", "aria-label": "閉じる (Esc)", onclick: () => closeCleanup() }, [icon("close")]),
    ]),
    body,
    el("div", { class: "dialog__foot" }, [cancel, run]),
  ]);
  dialog.addEventListener("keydown", (ev) => trapFocusIn(dialog, ev));
  const scrim = el("div", { class: "scrim" }, [dialog]);
  scrim.addEventListener("mousedown", (ev) => { if (ev.target === scrim) closeCleanup(); });
  $("app").appendChild(scrim);
  cleanupDlg.node = scrim;
  cancel.focus();
  run.addEventListener("click", async () => {
    if (cleanupDlg.running) return;
    cleanupDlg.running = true;
    try {
      const r = await api("/api/tickets/delete", { ticket_id: it.id });
      cleanupDlg.running = false;
      closeCleanup(false);
      if (S.drawer && S.drawer.id === it.id) closeDrawer(false);
      const k = (r.session_ids || []).length;
      toast("チケットを削除しました。" + (k ? "会話 " + k + " 件をボードに戻しました。" : ""));
      await poll(true);
    } catch (e) {
      cleanupDlg.running = false;
      toast(errorText(e, "チケットの削除"), { error: true });
    }
  });
}

// 4.3 会話のまとめの要約: 主題 → いま / 次 → 話の進み方 → 片づいていない脇の話 → まとめを開くリンク
function digestBlock(d, sid) {
  const next = d.next && d.next.text ? d.next : null;
  const mine = !!(next && next.who === "user");
  const nowEl = el("dl", { class: "now" + (mine ? " now--mine" : "") }, [
    el("dt", { text: "いま" }), el("dd", { text: d.now || "—" }),
    next ? el("dt", { text: "次" }) : null,
    next ? el("dd", { text: (mine ? "あなたが" : "") + next.text }) : null,
  ]);
  const steps = el("ol", { class: "dsteps" }, (d.steps || []).map((st) => {
    const dec = st.kind === "決めた";
    return el("li", { class: "dsteps__i" + (st.state === "now" ? " dsteps__i--now" : "") }, [
      el("span", { class: "dsteps__k", text: st.state === "now" ? "いま" : st.kind }),
      el("span", { class: dec ? "dsteps__t dsteps__t--dec" : "dsteps__t", text: st.text }),
      dec ? el("span", { class: "dpill", text: "決めたこと" }) : null,
      st.side ? el("span", { class: "dside", text: "脇の話 " + st.side }) : null,
    ]);
  }));
  const counts = new Map();
  for (const o of d.open || []) counts.set(o.kind, (counts.get(o.kind) || 0) + 1);
  const asks = (d.open || []).filter((o) => o.kind === "あなたへの質問");
  const open = counts.size ? el("div", {}, [
    el("h3", { class: "sect__h", text: "まだ片づいていない脇の話" }),
    el("div", { class: "dcounts" }, [...counts].map(([k, n]) =>
      el("span", { class: "dcount" + (k === "あなたへの質問" ? " dcount--mine" : ""), text: k + " " + n }))),
    asks.length ? el("ul", { class: "dasks" }, asks.map((o) => el("li", { text: o.title }))) : null,
  ]) : null;
  const when = relEpoch(d.updated_at);
  const notes = [when ? when + "に整理" : null, d.flows ? "処理の図 " + d.flows : null,
    d.check === "left" ? "直しきれなかった文あり" : d.check === "not_run" ? "文は未確認" : null].filter(Boolean);
  return [
    el("div", { class: "dtheme" }, [el("h3", { class: "sect__h", text: "この会話の主題" }), el("p", { class: "dtheme__t", text: d.theme })]),
    nowEl,
    el("div", {}, [el("h3", { class: "sect__h", text: "話の進み方" }), steps]),
    open,
    el("div", { class: "dopen" }, [
      el("a", { class: "btn", href: "/digest/" + encodeURIComponent(sid), target: "_blank", rel: "noopener noreferrer",
        title: "この会話のまとめを、新しいタブで開きます。主題、話の進み方、脇の話、処理の図が載っています。" }, ["まとめを見る"]),
      notes.length ? el("span", { class: "muted dopen__note", text: notes.join(" · ") }) : null,
    ]),
  ];
}

function moreBlock(it, key, label, hint, builder) {
  const id = it.id + ":" + key;
  const det = el("details", { class: "more" });
  det.open = S.openMore.has(id);
  det.addEventListener("toggle", () => {
    if (det.open) {
      S.openMore.add(id);
      if (!det.dataset.built) { det.appendChild(builder()); det.dataset.built = "1"; }
    } else {
      S.openMore.delete(id);
    }
  });
  det.appendChild(el("summary", {}, [icon("chev", "ico--sm done__chev"), label, el("span", { class: "more__summary-hint", text: hint })]));
  if (det.open) { det.appendChild(builder()); det.dataset.built = "1"; }
  return det;
}

// 箱の下に開く中身: 切る前の文 → 要約の説明 → データ (PR・ブランチなど) → レーンにまとめた段 → PR を開くリンク
function flowDetailEl(n, itemId) {
  const box = el("div", { class: "fn__d" });
  if (n.full && n.full !== n.text) box.appendChild(el("p", { class: "fn__full", text: n.full }));
  if (n.detail) box.appendChild(el("p", { text: n.detail }));
  if (n.noDetail) {
    box.appendChild(el("p", { class: "muted" }, [
      "詳しい説明はまだありません。要約し直すと表示されます。",
      S.data && S.data.llm_enabled && itemId ? el("button", { type: "button", class: "btn btn--sm fn__resum", text: "要約し直す",
        onclick: () => act(async () => {
          const r = await api("/api/summarize", { item_id: itemId });
          toast(r.queued ? "会話 " + r.queued + " 件の要約し直しを始めました。" : "要約し直す会話はありません。");
        }, "要約") }) : null,
    ]));
  }
  if (n.facts && n.facts.length) {
    box.appendChild(el("dl", { class: "kv fn__facts" }, n.facts.flatMap(([k, v]) => kvRow(k, v, "kv__pre"))));
  }
  if (n.substeps && n.substeps.length) {
    box.appendChild(el("ul", { class: "fn__subs" }, n.substeps.map((s) => el("li", {}, [
      el("b", { text: s.full }), s.detail ? el("span", { text: s.detail }) : null,
    ]))));
  }
  if (n.href) box.appendChild(el("a", { href: n.href, target: "_blank", rel: "noopener noreferrer", text: "GitHub で PR を開く" }));
  return box;
}

let flowDetailSeq = 0;

// key: 開いている箱を覚えておく鍵 (ポーリングで描き直しても開いたままにする)
function nodeEl(n, inLane, key, itemId) {
  const cls = ["fn"];
  if (n.state === "done") cls.push("fn--done");
  else if (n.state === "now") cls.push("fn--now");
  else if (n.state === "future") cls.push("fn--future");
  if (n.mine) cls.push("fn--mine");
  const kids = [];
  if (n.here) kids.push(el("span", { class: "fn__here", text: "今ここ" }));
  if (!inLane) kids.push(el("span", { class: "fn__k", text: n.kind }));
  kids.push(el("span", { class: "fn__t" }, [n.text, n.small ? el("small", { text: n.small }) : null]));
  kids.push(el("span", { class: "fn__m" }, [n.metaMine ? el("span", { class: "ball ball--mine", text: "自分" }) : (n.meta || "")]));
  if (!hasFlowDetail(n)) {
    return el("div", { class: "fitem", role: "listitem" }, [el("div", { class: cls.join(" "), title: n.title || null }, kids)]);
  }
  cls.push("fn--btn");
  const open = S.openMore.has(key);
  const panel = flowDetailEl(n, itemId);
  panel.id = "fn-d-" + (++flowDetailSeq);
  panel.hidden = !open;
  kids.push(icon("chev", "ico--sm fn__chev"));
  const btn = el("button", {
    type: "button", class: cls.join(" ") + (open ? " fn--open" : ""), "aria-expanded": String(open), "aria-controls": panel.id,
    title: open ? "押すと閉じます。" : "押すと詳しい内容を表示します。",
    onclick: () => {
      const now = !S.openMore.has(key);
      if (now) S.openMore.add(key); else S.openMore.delete(key);
      panel.hidden = !now;
      btn.setAttribute("aria-expanded", String(now));
      btn.classList.toggle("fn--open", now);
      btn.title = now ? "押すと閉じます。" : "押すと詳しい内容を表示します。";
    },
  }, kids);
  return el("div", { class: "fitem", role: "listitem" }, [btn, panel]);
}

function firstNodeOf(e) {
  return e.type === "lanes" ? (e.lanes[0] && e.lanes[0].nodes[0]) || {} : e;
}

function renderFlow(model, itemId) {
  const box = el("div", { class: "flow", role: "list", "aria-label": "作業の流れ" });
  const keyOf = (n, path) => (itemId || "") + ":flow:" + path + ":" + n.kind + ":" + n.text;
  model.forEach((e, i) => {
    if (i > 0) box.appendChild(el("div", { class: "fe" + (firstNodeOf(e).state === "future" ? " fe--future" : ""), "aria-hidden": "true" }));
    if (e.type !== "lanes") {
      box.appendChild(nodeEl(e, false, keyOf(e, i), itemId));
      return;
    }
    box.appendChild(el("div", { class: "fsplit", "aria-hidden": "true" }));
    box.appendChild(el("div", { class: "flanes" }, e.lanes.map((l, li) => {
      const lane = el("div", { class: "flane" }, [el("span", { class: "flane__h", text: l.name })]);
      l.nodes.forEach((n, j) => {
        if (j > 0) lane.appendChild(el("div", { class: "fe", "aria-hidden": "true" }));
        lane.appendChild(nodeEl(n, true, keyOf(n, i + "." + li + "." + j), itemId));
      });
      return lane;
    })));
    box.appendChild(el("div", { class: "fjoin", "aria-hidden": "true" }));
  });
  if (!model.length) box.appendChild(el("p", { class: "muted", text: "流れを描く材料がまだありません" }));
  return box;
}

function kvRow(label, value, cls) {
  if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) return [];
  return [el("dt", { text: label }), el("dd", { class: cls || null }, Array.isArray(value) ? value : [value])];
}

function recordBody(it) {
  const box = el("div", { class: "more__body" });
  const at = relEpoch(it.summarized_at);
  const canResummarize = S.data.llm_enabled && (it.sessions || []).length;
  box.appendChild(el("div", { class: "more__head" }, [
    el("span", { class: "muted", text: at ? "要約: " + at + "に作成" : "要約: まだありません" }),
    canResummarize ? el("button", { type: "button", class: "btn btn--sm", text: "要約し直す",
      title: "この作業の会話を、いますぐ要約し直します。",
      onclick: () => act(async () => {
        const r = await api("/api/summarize", { item_id: it.id });
        toast(r.queued ? "会話 " + r.queued + " 件の要約し直しを始めました。" : "要約し直す会話はありません。");
      }, "要約") }) : null,
  ]));
  const prs = (it.prs || []).map((p) => {
    const url = safeGithubUrl(p.url);
    const label = (p.label || "PR") + " #" + p.number;
    return el("li", {}, [
      url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: label }) : label,
      " " + prStateText(p) + (p.title ? " — " + p.title : ""),
    ]);
  });
  const branches = (it.branches || []).map((b) => {
    const flags = [];
    if (b.ahead) flags.push("main より +" + b.ahead);
    if (b.dirty) flags.push("未コミットの変更あり");
    if (b.worktree) flags.push("worktree");
    return el("li", { class: "mono", title: b.worktree || null, text: b.repo + ":" + b.branch + (flags.length ? " (" + flags.join(", ") + ")" : "") });
  });
  const doneOld = isDone(it) && it.blocked_question;
  box.appendChild(el("dl", { class: "kv" }, [
    ...kvRow("目的", it.goal),
    ...kvRow("ここまで", it.done_so_far),
    ...kvRow("次", it.next_action),
    ...kvRow(doneOld ? "古い質問" : "質問", it.blocked_question),
    ...kvRow("メモ", it.memo, "kv__pre"),
    ...kvRow("PR", prs.length ? [el("ul", {}, prs)] : null),
    ...kvRow(it.kind === "ticket" ? "中の会話で触ったブランチ" : "この会話で触ったブランチ", branches.length ? [el("ul", {}, branches)] : null),
    ...kvRow("種別", (it.kinds || []).map((k) => KIND_LABEL[k] || k).join(" · ")),
    ...kvRow("関連", (it.targets || []).length > 1 ? it.targets.join(", ") : null),
    ...kvRow("フェーズ", it.phase + (it.phase_source ? " (根拠: " + it.phase_source + ")" : "")),
  ]));
  const sessions = it.kind === "ticket" ? [] : it.sessions || [];
  if (sessions.length) {
    box.appendChild(el("h3", { class: "sect__h", text: "会話 " + sessions.length }));
    for (const s of sessions) {
      const live = s.live ? (String(s.live.status).toLowerCase() === "busy" ? "実行中" : "起動中") : null;
      const sm = s.summary;
      const cmd = resumeCommand(s.session_id, s.cwd);
      box.appendChild(el("div", { class: "sess" }, [
        el("div", { class: "sess__head" }, [
          el("span", { class: "sess__id", title: s.session_id, text: (s.session_id || "").slice(0, 8) }),
          live ? el("span", { class: "tag", text: live }) : null,
          el("span", { text: (s.repo ? s.repo + ":" + (s.rel || "/") : s.cwd || "") + (s.branch ? "  " + s.branch : "") }),
          el("span", { title: absTime(s.last_ts), text: relTime(s.last_ts) }),
        ]),
        sm && sm.title ? el("div", { text: "要約: " + sm.title + (relEpoch(sm.summarized_at) ? " (" + relEpoch(sm.summarized_at) + ")" : "") }) : null,
        s.ai_title ? el("div", { class: "muted", text: "自動タイトル: " + s.ai_title }) : null,
        s.first_prompt ? el("div", { class: "sess__prompt", title: s.first_prompt, text: "最初: " + clipText(s.first_prompt, 300) }) : null,
        s.last_prompt && s.last_prompt !== s.first_prompt
          ? el("div", { class: "sess__prompt", title: s.last_prompt, text: "最後: " + clipText(s.last_prompt, 300) }) : null,
        el("div", { class: "cmd" }, [
          el("span", { text: cmd }),
          el("button", { type: "button", class: "icon-btn", "aria-label": "コピー: " + cmd, onclick: () => copyWithToast(cmd) }, [icon("copy", "ico--sm")]),
        ]),
      ]));
    }
  }
  const files = it.files || [];
  if (files.length) {
    // 既定で畳む。詳細パネルを開き直すと畳んだ状態に戻る (再描画の間だけ開閉を保つ)
    const fid = it.id + ":files";
    const det = el("details", { class: "more more--files" });
    det.open = S.openMore.has(fid);
    det.addEventListener("toggle", () => { if (det.open) S.openMore.add(fid); else S.openMore.delete(fid); });
    det.append(
      el("summary", {}, [icon("chev", "ico--sm done__chev"), "触ったファイル " + files.length]),
      el("ul", { class: "files" }, files.map((f) => el("li", { text: f }))),
    );
    box.appendChild(det);
  }
  return box;
}

// 「編集」: メモ・フェーズ。タイトルは上のタイトルを押して変える。完了・非表示・ゴミ箱へは各作業の右上にある
function opsBody(it) {
  const box = el("div", { class: "more__body" });
  const mid = "ops-memo-" + it.id;
  const memo = el("textarea", { id: mid, class: "textarea", rows: "3", maxlength: "4000" });
  memo.value = it.memo || "";
  box.appendChild(el("div", { class: "field" }, [
    el("label", { for: mid, text: "メモを編集" }), memo,
    el("div", { class: "field__row" }, [
      el("button", { type: "button", class: "btn btn--sm", text: "保存",
        onclick: () => act(async () => {
          await api("/api/override", { item_id: it.id, patch: { memo: memo.value } });
          toast("メモを保存しました。");
        }, "メモの保存") }),
    ]),
  ]));
  const pid = "ops-phase-" + it.id;
  const manual = it.phase_source === "手動";
  const sel = el("select", { id: pid, class: "select" });
  sel.appendChild(el("option", { value: "", text: manual ? "自動で決める" : "自動で決める (いまは「" + it.phase + "」)" }));
  for (const ph of S.data.phases) {
    const opt = el("option", { value: ph, text: ph });
    if (manual && it.phase === ph) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", () => act(async () => {
    await api("/api/override", { item_id: it.id, patch: { phase: sel.value } });
    toast(sel.value ? "フェーズを「" + sel.value + "」に変更しました。" : "フェーズを自動で決めるようにしました。");
  }, "フェーズの変更"));
  box.appendChild(el("div", { class: "field" }, [el("label", { for: pid, text: "フェーズを変更" }), sel]));
  return box;
}

// ------------------------------------------------------------------ ゴミ箱へ (確認ダイアログ)
// ワンクッション必要な操作なので、何がどれだけ起きるかを確認してから実行する。実行ボタンは無効にしない

const cleanupDlg = { node: null, opener: null, running: false };

function cleanupIsOpen() {
  return !!(cleanupDlg.node && cleanupDlg.node.isConnected);
}

function closeCleanup(restoreFocus) {
  if (!cleanupDlg.node || cleanupDlg.running) return;
  cleanupDlg.node.remove();
  cleanupDlg.node = null;
  if (restoreFocus !== false && cleanupDlg.opener && cleanupDlg.opener.isConnected) cleanupDlg.opener.focus();
}

async function openCleanup(it) {
  if (cleanupDlg.running) return;
  closeCleanup(false);
  hideTooltip();
  const opener = document.activeElement;
  const url = "/api/items/" + encodeURIComponent(it.id) + "/cleanup";
  let plan = null;
  try {
    const res = await fetch(url, { credentials: "same-origin" });
    let d = {};
    try { d = await res.json(); } catch (e) { d = {}; }
    if (!res.ok) {
      const err = new Error(d.error || "");
      err.status = res.status;
      throw err;
    }
    plan = d.plan;
  } catch (e) {
    toast(errorText(e, "ゴミ箱に移す対象の確認"), { error: true });
    return;
  }
  cleanupDlg.opener = opener;
  const lines = cleanupLines(plan, term().label);
  const counts = plan.counts || {};
  const summary = [];
  if (counts.tabs) summary.push("タブ " + counts.tabs + " 個");
  if (counts.trash) summary.push("会話の記録 " + counts.trash + " 件");
  const body = el("div", { class: "dialog__body" }, [
    el("p", { class: "cleanup__lead", text: "「" + clipText(it.title, 60) + "」"
      + (summary.length ? "の" + summary.join("と") + "を対象に、次のことをします。" : "について、次のことをします。") }),
    el("ul", { class: "cleanup" }, lines.map((l) => el("li", { class: "cleanup__line" }, [
      el("span", { text: l.text }),
      l.items ? el("ul", { class: "cleanup__items" }, l.items.map((x) => el("li", { text: x }))) : null,
    ]))),
  ]);
  const cancel = el("button", { type: "button", class: "btn", text: "キャンセル", onclick: () => closeCleanup() });
  const run = el("button", { type: "button", class: "btn btn--danger-fill", text: "ゴミ箱へ" });
  const dialog = el("div", { class: "dialog dialog--narrow", role: "alertdialog", "aria-modal": "true",
    "aria-labelledby": "cl-title", "aria-describedby": "cl-desc" }, [
    el("div", { class: "dialog__head" }, [
      el("h2", { class: "dialog__title", id: "cl-title", text: "作業をゴミ箱に移しますか？" }),
      el("div", { class: "hdr__spacer" }),
      el("button", { type: "button", class: "icon-btn", "aria-label": "閉じる (Esc)", onclick: () => closeCleanup() }, [icon("close")]),
    ]),
    body,
    el("div", { class: "dialog__foot" }, [cancel, run]),
  ]);
  body.id = "cl-desc";
  dialog.addEventListener("keydown", (ev) => trapFocusIn(dialog, ev));
  const scrim = el("div", { class: "scrim" }, [dialog]);
  scrim.addEventListener("mousedown", (ev) => { if (ev.target === scrim) closeCleanup(); });
  $("app").appendChild(scrim);
  cleanupDlg.node = scrim;
  cancel.focus();
  run.addEventListener("click", async () => {
    if (cleanupDlg.running) return; // 2 回押しても 1 回だけ実行する
    cleanupDlg.running = true;
    run.textContent = "ゴミ箱に移しています…";
    run.setAttribute("aria-busy", "true");
    try {
      const r = await api(url, { dry_run: false });
      cleanupDlg.running = false;
      closeCleanup(false);
      if (S.drawer && S.drawer.kind === "item" && S.drawer.id === it.id) closeDrawer(false);
      const x = cleanupResult(r);
      toast(x.text + (x.problems.length ? " " + x.problems.join(" / ") : ""), { error: x.error, ms: x.error ? 15000 : 6000 });
      await poll(true);
    } catch (e) {
      cleanupDlg.running = false;
      run.textContent = "ゴミ箱へ";
      run.removeAttribute("aria-busy");
      toast(errorText(e, "ゴミ箱への移動"), { error: true });
    }
  });
}

// ------------------------------------------------------------------ キーボード

function navNodes() {
  return [...document.querySelectorAll("#mine [data-nav], #board [data-nav]")].filter((n) => n.offsetParent !== null);
}

function moveNav(dir) {
  const nodes = navNodes();
  if (!nodes.length) return;
  let i = nodes.indexOf(document.activeElement);
  i = i < 0 ? (dir > 0 ? 0 : nodes.length - 1) : Math.max(0, Math.min(nodes.length - 1, i + dir));
  nodes[i].focus();
  nodes[i].scrollIntoView({ block: "nearest" });
}

function currentItem() {
  const items = (S.data && S.data.items) || [];
  const a = document.activeElement;
  const id = (a && a.closest && a.closest("[data-item]") && a.closest("[data-item]").dataset.item)
    || (S.drawer && S.drawer.kind === "item" ? S.drawer.id : null);
  return items.find((it) => it.id === id) || null;
}

function clearSearch() {
  const s = $("search");
  s.value = "";
  S.query = "";
  render();
}

// キーボード操作の一覧 (ダイアログ)。ゴミ箱へ・チケットを作成と同じ枠を使う
function openShortcuts() {
  if (cleanupDlg.running) return;
  closeCleanup(false);
  hideTooltip();
  const opener = document.activeElement;
  if (S.popover) togglePopover(false);
  cleanupDlg.opener = opener;
  const close = el("button", { type: "button", class: "btn", text: "閉じる", onclick: () => closeCleanup() });
  const body = el("div", { class: "dialog__body" });
  for (const g of SHORTCUTS) {
    body.appendChild(el("h3", { class: "popover__h", text: g.group }));
    body.appendChild(el("dl", { class: "keys" }, g.keys.flatMap(([ks, what]) => [
      el("dt", {}, ks.flatMap((k, i) => [i ? (ks[0] === "⌘" ? " + " : " / ") : null, el("kbd", { text: k })])),
      el("dd", { text: what }),
    ])));
  }
  const dialog = el("div", { class: "dialog dialog--narrow", role: "dialog", "aria-modal": "true", "aria-labelledby": "keys-title" }, [
    el("div", { class: "dialog__head" }, [
      el("h2", { class: "dialog__title", id: "keys-title", text: "キーボード操作" }),
      el("div", { class: "hdr__spacer" }),
      el("button", { type: "button", class: "icon-btn", "aria-label": "閉じる (Esc)", onclick: () => closeCleanup() }, [icon("close")]),
    ]),
    body,
    el("div", { class: "dialog__foot" }, [close]),
  ]);
  dialog.addEventListener("keydown", (ev) => trapFocusIn(dialog, ev));
  const scrim = el("div", { class: "scrim" }, [dialog]);
  scrim.addEventListener("mousedown", (ev) => { if (ev.target === scrim) closeCleanup(); });
  $("app").appendChild(scrim);
  cleanupDlg.node = scrim;
  close.focus();
}

function onKeydown(ev) {
  if (ev.defaultPrevented) return;
  if (ev.key === "Escape") {
    if (tooltipIsOpen()) { ev.preventDefault(); hideTooltip(); return; }
    if (cleanupIsOpen()) { ev.preventDefault(); closeCleanup(); return; }
    if (reportIsOpen()) { ev.preventDefault(); closeReport(); return; }
    const t = ev.target;
    if (t && t.id === "search" && t.value) { ev.preventDefault(); clearSearch(); return; }
    if (isTyping(t)) { t.blur(); return; }
    if (S.popover) { ev.preventDefault(); togglePopover(false); return; }
    if (S.ticketSource) { ev.preventDefault(); S.ticketSource = null; render(); return; }
    if (S.drawer) { ev.preventDefault(); closeDrawer(); return; }
    return;
  }
  if (ev.metaKey || ev.ctrlKey || ev.altKey || isTyping(ev.target)) return;
  if (cleanupIsOpen()) return;
  if (reportIsOpen()) {
    if (ev.key === "c") { ev.preventDefault(); copyNextSection(); }
    return;
  }
  const it = () => currentItem();
  switch (ev.key) {
    case "/": ev.preventDefault(); $("search").focus(); $("search").select(); break;
    case "j": ev.preventDefault(); moveNav(1); break;
    case "k": ev.preventDefault(); moveNav(-1); break;
    case "m": ev.preventDefault(); setOnlyMine(!S.onlyMine); break;
    case "r": ev.preventDefault(); refresh(); break;
    case "w": ev.preventDefault(); openReport(); break;
    case "f": ev.preventDefault(); togglePopover(); break;
    case "?": ev.preventDefault(); openShortcuts(); break;
    case "t": {
      const x = it();
      if (x && canFocus() && (x.tabs || []).length) { ev.preventDefault(); focusTab(x.tabs[0], x); }
      break;
    }
    case "e": {
      const x = it();
      const b = x ? editorButtons(x, editorName())[0] : null;
      if (b) { ev.preventDefault(); openEditor(x, b.path); }
      break;
    }
    case "o": {
      const x = it();
      const p = x ? pickPr(x, ballFor(x, Date.now())) : null;
      if (p) { ev.preventDefault(); openPrUrl(safeGithubUrl(p.url)); }
      break;
    }
    default:
  }
}

function onMousedown(ev) {
  const t = ev.target;
  if (S.popover && !t.closest(".hdr__filter")) {
    S.popover = false;
    renderPopover();
  }
  if (S.drawer && !t.closest("#drawer") && !t.closest("[data-nav]") && !t.closest("#toasts") && !t.closest(".scrim")
    && !t.closest(".row") && !t.closest(".mrow") && !t.closest("#pickbar")) {
    closeDrawer(false);
  }
}

// ------------------------------------------------------------------ 更新

let pollTimer = null;

async function poll(force) {
  clearTimeout(pollTimer);
  try {
    const res = await fetch("/api/state", { credentials: "same-origin" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const d = await res.json();
    const wasDown = !S.connected;
    S.connected = true;
    const changed = force || wasDown || d.version !== S.version || !S.data;
    S.data = d;
    S.version = d.version;
    if (changed) render();
    else { renderStatus(); renderBanners(); }
    if (S.pendingRender) render();
  } catch (e) {
    S.connected = false;
    renderStatus();
    renderBanners();
  }
  const d = S.data;
  const busy = d && ((d.progress && d.progress.running) || (d.llm && d.llm.queued > 0));
  pollTimer = setTimeout(() => poll(false), busy ? 2000 : 10000);
}

function refresh() {
  act(async () => {
    await api("/api/refresh", { llm: true });
    toast("更新を始めました。");
  }, "更新");
}

// ------------------------------------------------------------------ 作業報告

const REPORT_SECTIONS = [
  ["status", "今日の進み具合"],
  ["done", "今日終わったこと"],
  ["carryover", "残っていること"],
  ["tomorrow", "明日やること"],
  ["share", "共有・相談したいこと"],
];
const STATUS_CHOICES = ["予定通り", "やや遅れ", "遅れ"];
const report = {
  node: null, dialog: null, dateInput: null, dateHint: null, stateNode: null, metaNode: null, editedNode: null,
  regenButton: null, confirmBox: null, secs: {}, statusValue: "", seg: [], date: null, data: null, shown: null,
  timer: null, requested: new Set(), opener: null, copied: new Set(),
};

function reportIsOpen() {
  return !!(report.node && !report.node.hidden);
}

function localToday() {
  const d = new Date();
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}

function formatEpoch(sec) {
  if (typeof sec !== "number" || !sec) return "";
  const d = new Date(sec * 1000);
  return (d.getMonth() + 1) + "/" + d.getDate() + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

function dateHint(date) {
  const [y, m, d] = date.split("-").map(Number);
  if (!y) return "";
  const day = new Date(y, m - 1, d);
  const [ty, tm, td] = localToday().split("-").map(Number);
  const diff = Math.round((new Date(ty, tm - 1, td) - day) / 86400000);
  const wd = "(" + WEEKDAYS[day.getDay()] + ")";
  return diff === 0 ? "今日 " + wd : diff === 1 ? "昨日 " + wd : wd;
}

function reportStoreKey(date) {
  return "session-board:report:" + date;
}

function loadReportEdits(date, generatedAt) {
  const raw = loadJson(reportStoreKey(date), null);
  if (raw && raw.generated_at === generatedAt && raw.texts && typeof raw.texts === "object") return raw.texts;
  return null;
}

function sectionValue(key) {
  return key === "status" ? report.statusValue : report.secs[key].area.value;
}

function saveReportEdits() {
  const d = report.data;
  if (!d || !d.generated_at || !report.shown || report.shown.date !== report.date) return;
  const texts = {};
  for (const [key] of REPORT_SECTIONS) texts[key] = sectionValue(key);
  storageSet(reportStoreKey(report.date), JSON.stringify({ generated_at: d.generated_at, texts }));
}

function editedSections() {
  const d = report.data;
  if (!d || !d.texts || !report.shown || report.shown.generated_at !== d.generated_at) return [];
  return REPORT_SECTIONS.filter(([key]) => sectionValue(key) !== (d.texts[key] || ""));
}

function renderEdited() {
  const edited = new Set(editedSections().map(([k]) => k));
  for (const [key] of REPORT_SECTIONS) report.secs[key].edited.hidden = !edited.has(key);
  report.editedNode.hidden = edited.size === 0;
}

function fitHeight(ta) {
  ta.style.height = "auto";
  ta.style.height = (ta.scrollHeight + 2) + "px";
}

function reportFullText() {
  const lines = [];
  for (const [key, label] of REPORT_SECTIONS) {
    const body = sectionValue(key).replace(/\s+$/, "");
    if (key === "status") {
      lines.push(label + ": " + body.trim());
    } else {
      lines.push(label + ":");
      if (body) lines.push(body);
    }
  }
  return lines.join("\n");
}

function markCopied(key, ok) {
  const sec = report.secs[key];
  sec.feedback.textContent = ok ? "コピーしました" : "コピーに失敗しました";
  sec.feedback.classList.toggle("status--err", !ok);
  clearTimeout(sec.feedback._timer);
  sec.feedback._timer = setTimeout(() => { sec.feedback.textContent = ""; }, 1800);
  if (!ok) return;
  report.copied.add(key);
  sec.node.classList.add("rsec--copied");
  sec.no.textContent = "\u2713";
  sec.no.setAttribute("aria-label", "コピー済み");
}

async function copySection(key) {
  markCopied(key, await copyText(sectionValue(key)));
}

function copyNextSection() {
  const next = REPORT_SECTIONS.find(([key]) => !report.copied.has(key));
  if (next) copySection(next[0]);
}

function resetCopied() {
  report.copied.clear();
  REPORT_SECTIONS.forEach(([key], i) => {
    const sec = report.secs[key];
    sec.node.classList.remove("rsec--copied");
    sec.no.textContent = String(i + 1);
    sec.no.removeAttribute("aria-label");
  });
}

function setStatusValue(v) {
  report.statusValue = v;
  const idx = STATUS_CHOICES.indexOf(v);
  report.seg.forEach((b, i) => {
    b.setAttribute("aria-checked", i === idx ? "true" : "false");
    b.tabIndex = i === (idx < 0 ? 0 : idx) ? 0 : -1;
  });
}

function buildStatusSeg(labelId) {
  const seg = el("div", { class: "seg", role: "radiogroup", "aria-labelledby": labelId });
  report.seg = STATUS_CHOICES.map((v, i) => el("button", {
    type: "button", role: "radio", "aria-checked": "false", text: v,
    onclick: () => { setStatusValue(v); saveReportEdits(); renderEdited(); },
    onkeydown: (ev) => {
      if (!["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(ev.key)) return;
      ev.preventDefault();
      const j = (i + (ev.key === "ArrowRight" || ev.key === "ArrowDown" ? 1 : -1) + STATUS_CHOICES.length) % STATUS_CHOICES.length;
      setStatusValue(STATUS_CHOICES[j]);
      saveReportEdits();
      renderEdited();
      report.seg[j].focus();
    },
  }));
  report.seg.forEach((b) => seg.appendChild(b));
  return seg;
}

function buildReportModal() {
  report.dateInput = el("input", { id: "rp-date", type: "date", class: "date" });
  report.dateInput.addEventListener("change", () => {
    if (report.dateInput.value && report.dateInput.value !== report.date) loadReport(report.dateInput.value, true);
  });
  report.dateHint = el("span", { class: "dialog__hint" });
  report.regenButton = el("button", { type: "button", class: "btn", text: "作り直す", onclick: askRegenerate });
  const allButton = el("button", { type: "button", class: "btn btn--primary", onclick: async () => {
    const ok = await copyText(reportFullText());
    allFeedback.textContent = ok ? "コピーしました" : "コピーに失敗しました";
    clearTimeout(allFeedback._timer);
    allFeedback._timer = setTimeout(() => { allFeedback.textContent = ""; }, 1800);
  } }, [icon("copy"), "全部コピー"]);
  const allFeedback = el("span", { class: "copied", "aria-live": "polite" });
  report.confirmBox = el("div", { class: "confirm", role: "alert", hidden: true });
  report.stateNode = el("span", { class: "status" });
  report.metaNode = el("span");
  report.editedNode = el("span", { class: "rsec__edited", text: "編集あり (この日付で保存済み)", hidden: true });

  const sections = REPORT_SECTIONS.map(([key, label], i) => {
    const labelId = "rp-l-" + key;
    const no = el("span", { class: "rsec__no", text: String(i + 1) });
    const feedback = el("span", { class: "copied", "aria-live": "polite" });
    const edited = el("span", { class: "rsec__edited", text: "編集済み", hidden: true });
    const copyBtn = el("button", { type: "button", class: "btn btn--sm", text: "コピー", "aria-label": label + " をコピー",
      onclick: () => copySection(key) });
    const headKids = [el("span", { class: "rsec__label", id: labelId, text: label })];
    let area = null;
    if (key === "status") {
      headKids.push(buildStatusSeg(labelId));
    } else {
      area = el("textarea", { id: "rp-" + key, class: "rsec__text", rows: "3", spellcheck: "false", "aria-labelledby": labelId });
      area.addEventListener("input", () => { fitHeight(area); saveReportEdits(); renderEdited(); });
    }
    headKids.push(edited, el("span", { class: "rsec__copy" }, [feedback, " ", copyBtn]));
    const node = el("section", { class: "rsec" + (key === "status" ? " rsec--status" : ""), "aria-labelledby": labelId }, [
      no, el("div", { class: "rsec__head" }, headKids), area,
    ]);
    report.secs[key] = { node, no, area, feedback, edited };
    return node;
  });

  report.dialog = el("div", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": "rp-title" }, [
    el("div", { class: "dialog__head" }, [
      el("h2", { class: "dialog__title", id: "rp-title", text: "作業報告" }),
      el("label", { class: "sr-only", for: "rp-date", text: "日付" }),
      report.dateInput,
      report.dateHint,
      el("div", { class: "hdr__spacer" }),
      report.regenButton,
      allButton,
      allFeedback,
      el("button", { type: "button", class: "icon-btn", "aria-label": "閉じる (Esc)", onclick: closeReport }, [icon("close")]),
    ]),
    el("div", { class: "dialog__status", "aria-live": "polite" }, [
      report.stateNode, report.metaNode, report.editedNode,
      el("span", { class: "dialog__keys" }, ["上から順に 1 → 5。", el("kbd", { text: "c" }), " で次の項目をコピー"]),
    ]),
    el("div", { class: "dialog__body" }, [report.confirmBox, ...sections]),
  ]);
  report.dialog.addEventListener("keydown", trapFocus);
  const scrim = el("div", { class: "scrim", hidden: true }, [report.dialog]);
  scrim.addEventListener("mousedown", (ev) => { if (ev.target === scrim) closeReport(); });
  $("app").appendChild(scrim);
  report.node = scrim;
}

function trapFocus(ev) {
  trapFocusIn(report.dialog, ev);
}

function trapFocusIn(dialog, ev) {
  if (ev.key !== "Tab") return;
  const f = focusables(dialog).filter((n) => n.tabIndex >= 0 && !n.disabled);
  if (!f.length) return;
  const first = f[0];
  const last = f[f.length - 1];
  if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
  else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
}

function openReport() {
  if (!report.node) buildReportModal();
  if (reportIsOpen()) return;
  report.opener = document.activeElement;
  const today = localToday();
  report.dateInput.max = today;
  report.dateInput.value = today;
  report.dateHint.textContent = dateHint(today);
  report.node.hidden = false;
  report.dateInput.focus();
  loadReport(today, true);
}

function closeReport() {
  if (!reportIsOpen()) return;
  saveReportEdits();
  clearTimeout(report.timer);
  report.node.hidden = true;
  report.confirmBox.hidden = true;
  resetCopied();
  if (report.opener && report.opener.focus) report.opener.focus();
}

function fillReportAreas(d) {
  const edits = loadReportEdits(d.date, d.generated_at);
  for (const [key] of REPORT_SECTIONS) {
    const v = edits && typeof edits[key] === "string" ? edits[key] : ((d.texts || {})[key] || "");
    if (key === "status") setStatusValue(v.trim());
    else report.secs[key].area.value = v;
  }
  report.shown = { date: d.date, generated_at: d.generated_at };
  requestAnimationFrame(() => {
    for (const [key] of REPORT_SECTIONS) if (report.secs[key].area) fitHeight(report.secs[key].area);
  });
}

function renderReport() {
  const d = report.data;
  if (!d) return;
  const st = report.stateNode;
  st.replaceChildren();
  st.className = "status";
  if (d.running) {
    const sec = d.started_at ? Math.max(0, Math.round(Date.now() / 1000 - d.started_at)) : null;
    st.classList.add("status--busy");
    st.append(el("span", { class: "spinner", "aria-hidden": "true" }), (d.stage || "準備中") + "..." + (sec !== null ? " (" + sec + "秒)" : ""));
  } else if (d.error) {
    st.classList.add("status--err");
    st.textContent = "作業報告の作成に失敗しました。" + d.error;
  } else if (!d.texts) {
    st.textContent = "まだ作成していません";
  }
  report.metaNode.textContent = d.generated_at
    ? "作成: " + formatEpoch(d.generated_at) + (typeof d.seconds === "number" ? " (" + d.seconds + " 秒)" : "") + (d.model ? " · " + d.model : "")
    : "";
  report.regenButton.disabled = !!d.running;
  report.regenButton.textContent = d.texts ? "作り直す" : "作成";
  const shown = report.shown;
  if (d.texts && (!shown || shown.date !== d.date || shown.generated_at !== d.generated_at)) {
    fillReportAreas(d);
    resetCopied();
  } else if (!d.texts && (!shown || shown.date !== d.date)) {
    for (const [key] of REPORT_SECTIONS) {
      if (key === "status") setStatusValue("");
      else { report.secs[key].area.value = ""; fitHeight(report.secs[key].area); }
    }
    report.shown = { date: d.date, generated_at: null };
    resetCopied();
  }
  renderEdited();
}

async function loadReport(date, autoGenerate) {
  clearTimeout(report.timer);
  if (date !== report.date) saveReportEdits();
  report.date = date;
  report.dateHint.textContent = dateHint(date);
  report.confirmBox.hidden = true;
  try {
    const res = await fetch("/api/report?date=" + encodeURIComponent(date), { credentials: "same-origin" });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || ("HTTP " + res.status));
    if (report.date !== date) return; // 途中で日付を変えた
    report.data = d;
    renderReport();
    if (autoGenerate && !d.texts && !d.running && !d.error && !report.requested.has(date)) {
      report.requested.add(date);
      await generateReport();
      return;
    }
    if (d.running && reportIsOpen()) report.timer = setTimeout(() => loadReport(date, false), 1500);
  } catch (e) {
    report.stateNode.className = "status status--err";
    report.stateNode.textContent = errorText(e, "作業報告の読み込み");
  }
}

// 編集済みのときだけ、作り直す前にインラインで確かめる (消える項目名を挙げる)
function askRegenerate() {
  const edited = editedSections();
  if (!edited.length) {
    generateReport();
    return;
  }
  const box = report.confirmBox;
  const cancel = el("button", { type: "button", class: "btn btn--sm", text: "キャンセル",
    onclick: () => { box.hidden = true; report.regenButton.focus(); } });
  box.replaceChildren(
    el("span", { text: "作り直すと、編集した " + edited.length + " 項目 (「" + edited.map(([, label]) => label).join("」「") + "」) の内容が消えます。作り直しますか？" }),
    el("button", { type: "button", class: "btn btn--sm btn--danger", text: "作り直す",
      onclick: () => { box.hidden = true; generateReport(); } }),
    cancel,
  );
  box.hidden = false;
  cancel.focus();
}

async function generateReport() {
  const date = report.date;
  try {
    await api("/api/report/generate", { date });
    storageRemove(reportStoreKey(date));
    report.shown = null;
  } catch (e) {
    report.stateNode.className = "status status--err";
    report.stateNode.textContent = errorText(e, "作業報告の作成");
    return;
  }
  await loadReport(date, false);
}

// ------------------------------------------------------------------ 起動

function boot() {
  migrateOldStorage();
  S = {
    data: null, version: -1, connected: true,
    filters: loadFilters(),
    onlyMine: storageGet(STORE.onlyMine) === "1",
    mineCollapsed: loadJson(STORE.mineCollapsed, false) === true,
    openGroups: new Set((() => {
      const v = loadJson(STORE.openGroups, []);
      return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
    })()),
    openLoose: new Set(), openMore: new Set(), dismissed: new Set(),
    stopOnly: false, query: "", drawer: null, drawerScroll: 0, drawerShown: null,
    summAt: new Map(), popover: false, ticketSource: null, dragId: null, renameItem: null, renameCol: null, showProblems: false,
    activeTab: null, narrow: false, pendingRender: false,
    drawerWidth: DRAWER_DEFAULT, resizing: false,
    ticketOpen: new Map(), // チケットの id -> 会話の記録で開いている会話の作業の id (一度に 1 つ)
    ask: new Map(), askLog: new Map(), // 質問: 作業の id -> 選んだ会話と書きかけの質問 / 会話の ID -> 質問と答え
  };
  for (const node of document.querySelectorAll("[data-icon]")) node.insertBefore(icon(node.dataset.icon), node.firstChild);
  setupTooltip();
  setDrawerWidth(storageGet(STORE.drawerWidth) || DRAWER_DEFAULT, false);
  window.addEventListener("resize", () => setDrawerWidth(Number(storageGet(STORE.drawerWidth)) || S.drawerWidth, false));
  const mq = window.matchMedia("(max-width: 899px)");
  S.narrow = mq.matches;
  mq.addEventListener("change", () => { S.narrow = mq.matches; render(); });
  $("search").addEventListener("input", (ev) => { S.query = ev.target.value.trim(); render(); });
  $("filter-btn").addEventListener("click", () => togglePopover());
  $("mine-switch").addEventListener("click", () => setOnlyMine(!S.onlyMine));
  $("hidden-btn").addEventListener("click", () => { S.filters.showHidden = !S.filters.showHidden; saveFilters(); render(); });
  $("refresh").addEventListener("click", refresh);
  $("ticket-new").addEventListener("click", openCreateTicket);
  $("keys-open").addEventListener("click", openShortcuts);
  $("report-open").addEventListener("click", openReport);
  document.addEventListener("keydown", onKeydown);
  document.addEventListener("mousedown", onMousedown);
  document.addEventListener("focusout", () => {
    setTimeout(() => { if (S.pendingRender) render(); }, 0);
  });
  render();
  poll(true);
}

if (typeof document !== "undefined" && document.getElementById("app")) boot();
