// 会話のまとめのページ (/digest/<会話の ID>) のライト / ダークの切り替え。
// 最初は OS の設定に従い、押したらその選び方を localStorage に覚える。head で読み、描く前に当てる
(function () {
  "use strict";
  var KEY = "session-board:digest-theme";
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) { /* 保存できない環境では OS の設定のまま */ }

  function current() {
    var t = root.getAttribute("data-theme");
    if (t) return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    function label() {
      btn.textContent = current() === "dark" ? "ライトに切り替える" : "ダークに切り替える";
    }
    btn.hidden = false;
    label();
    btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* 覚えられなくても切り替えはする */ }
      label();
    });
  });
})();
