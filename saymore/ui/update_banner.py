# -*- coding: utf-8 -*-
"""主界面的「发现新版本」横条 + 强制更新遮罩（CSS/HTML/JS 片段）。

main_window 把 SNIPPET 注入页面 </body> 前；数据来自语音进程写的 .update_status.json
（经 api.update_status() 读），按钮经 api.update_install()/update_quit() 写 .update_control.json。
后端逻辑见 saymore/updater.py。复用主界面的 CSS 变量，深浅色自动跟随。"""

SNIPPET = r"""
<style>
.upd{margin:14px 0 6px;padding:12px 16px;border-radius:12px;font-size:14px;
  background:rgba(10,132,255,.10);border:1px solid rgba(10,132,255,.28)}
.upd-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.upd-row .msg{flex:1;min-width:200px}
.upd-row .msg b{color:var(--accent)}
.upd button{border:0;font:inherit;font-size:13px;font-weight:500;padding:7px 16px;border-radius:8px;cursor:pointer}
.upd button.go{background:var(--accent);color:#fff}
.upd button.go:active{opacity:.85}
.upd button.link{background:transparent;color:var(--accent);padding:7px 6px}
.upd button:disabled{opacity:.5;cursor:not-allowed}
.upd-notes{display:none;margin-top:10px;padding:10px 12px;border-radius:8px;background:var(--card);
  border:1px solid var(--border);max-height:220px;overflow-y:auto;white-space:pre-wrap;
  line-height:1.55;font-size:13px;user-select:text}
.upd-notes.show{display:block}
.upd-prog{display:none;margin-top:10px;height:8px;border-radius:4px;background:var(--border);overflow:hidden}
.upd-prog.show{display:block}
.upd-prog div{height:100%;width:0%;background:var(--accent);transition:width .3s ease-out}
.upd-err{color:#ff453a;font-size:12.5px;margin-top:6px}
/* 强制更新：盖住整个窗口，只能更新或退出 */
.upd-mask{position:fixed;inset:0;z-index:200;display:none;align-items:center;justify-content:center;
  background:rgba(0,0,0,.45)}
.upd-mask.show{display:flex}
.upd-mask .upd{width:min(560px,90vw);margin:0;background:var(--white);border-color:var(--border);
  box-shadow:0 10px 40px rgba(0,0,0,.3);padding:20px 22px}
.upd-mask h3{margin:0 0 8px;font-size:17px}
.upd-mask .upd-notes{display:block}
</style>
<div class="upd-mask" id="updMask"><div class="upd" id="updForce"></div></div>
<script>
(function () {
  const bar = document.createElement('div');
  bar.className = 'upd'; bar.style.display = 'none';
  document.querySelector('.content .body').prepend(bar);
  const mask = document.getElementById('updMask'), forceBox = document.getElementById('updForce');
  let notesOpen = false, lastKey = '';
  const mb = n => (n / 1048576).toFixed(0);

  function render(box, s, forced) {
    const L = s.latest, ph = s.phase;
    const busy = ph === 'downloading' || ph === 'verifying' || ph === 'launching';
    let msg;
    if (ph === 'downloading') msg = `正在下载新版本 <b>${L.version}</b>：${mb(s.done)} / ${mb(s.total)} MB`;
    else if (ph === 'verifying') msg = '下载完成，正在校验安装包…';
    else if (ph === 'launching') msg = '正在启动安装程序，Saymore 将自动退出并在安装后重新启动…';
    else msg = `发现新版本 <b>${L.version}</b>（当前 ${s.current}）`;
    const pct = s.total ? Math.min(100, s.done * 100 / s.total) : 0;
    box.innerHTML =
      (forced ? '<h3>需要更新后才能继续使用</h3>' : '') +
      `<div class="upd-row"><span class="msg">${msg}</span>` +
      (forced ? '' : `<button class="link" data-a="notes">${notesOpen ? '收起' : '更新内容'}</button>`) +
      `<button class="go" data-a="install" ${busy ? 'disabled' : ''}>${ph === 'error' ? '重试' : '立即更新'}</button>` +
      (forced ? `<button class="link" data-a="quit" ${busy ? 'disabled' : ''}>退出程序</button>` : '') +
      '</div>' +
      `<div class="upd-prog ${busy ? 'show' : ''}"><div style="width:${pct}%"></div></div>` +
      (ph === 'error' ? '<div class="upd-err"></div>' : '') +
      `<div class="upd-notes ${notesOpen || forced ? 'show' : ''}"></div>`;
    box.querySelector('.upd-notes').textContent = L.notes || '（无更新说明）';
    if (ph === 'error') box.querySelector('.upd-err').textContent = '更新失败：' + s.error;
    box.querySelectorAll('button').forEach(b => b.onclick = () => {
      const a = b.dataset.a;
      if (a === 'notes') { notesOpen = !notesOpen; lastKey = ''; tick(); }
      else if (a === 'install') { b.disabled = true; window.pywebview.api.update_install(); }
      else if (a === 'quit') window.pywebview.api.update_quit();
    });
  }

  async function tick() {
    let s;
    try { s = await window.pywebview.api.update_status(); } catch (e) { return; }
    if (!s || !s.latest) { bar.style.display = 'none'; mask.classList.remove('show'); return; }
    const key = JSON.stringify([s.phase, s.done, s.error, notesOpen, s.latest.version]);
    if (key === lastKey) return;  // 没变化就不重绘，免得把用户正在滚动的更新说明刷回顶部
    lastKey = key;
    const forced = !!s.latest.force;
    mask.classList.toggle('show', forced);
    bar.style.display = forced ? 'none' : '';
    render(forced ? forceBox : bar, s, forced);
  }
  whenApiReady(() => { tick(); setInterval(tick, 1000); });
})();
</script>
"""
