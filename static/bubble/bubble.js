/* Local scene lifecycle and workspace interactions for Prompt Crawler. */
document.querySelectorAll('[data-bubble]').forEach(host => {
  const toggle = host.querySelector('[data-bubble-toggle]');
  const stage = host.querySelector('.bubble-stage');
  const caption = host.querySelector('[data-bubble-caption]');
  const layout = host.dataset.sceneLayout || 'workspace';
  let frame, ready = false, failed = false, userPaused = false, inView = true, timeout;
  function command(type, extra = {}) { frame?.contentWindow?.postMessage({ type, ...extra }, '*'); }
  function syncPlayback() {
    if (ready) command(userPaused || document.hidden || !inView ? 'ambient:pause' : 'ambient:resume');
    toggle.textContent = failed ? '重试动效' : userPaused ? '继续互动' : '暂停互动';
    toggle.setAttribute('aria-pressed', String(!userPaused && !failed));
    if (caption) caption.textContent = userPaused ? '让灵感停留片刻' : '移动鼠标，和 Bubble 打个招呼';
  }
  function mount() {
    failed = false; ready = false;
    host.classList.remove('is-live');
    frame?.remove();
    frame = document.createElement('iframe');
    frame.title = '泡泡互动背景';
    frame.referrerPolicy = 'no-referrer';
    // Opaque sandbox: the artwork has no access to login cookies or app data.
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.src = '/static/ambient/index.html?v=2&layout=' + encodeURIComponent(layout);
    stage.append(frame);
    clearTimeout(timeout);
    timeout = setTimeout(() => { if (!ready) { failed = true; syncPlayback(); } }, 15000);
    syncPlayback();
  }
  window.addEventListener('message', event => {
    if (event.source !== frame?.contentWindow || event.origin !== 'null') return;
    if (event.data?.type === 'ambient:ready') {
      ready = true; failed = false; clearTimeout(timeout);
      host.classList.add('is-live'); syncPlayback();
    } else if (event.data?.type === 'ambient:toggle') {
      userPaused = !userPaused; syncPlayback();
    } else if (event.data?.type === 'ambient:error') {
      failed = true; ready = false; clearTimeout(timeout);
      host.classList.remove('is-live'); syncPlayback();

    }
  });
  toggle.addEventListener('click', () => {
    if (failed) { userPaused = false; mount(); return; }
    userPaused = !userPaused; syncPlayback();
  });
  document.addEventListener('visibilitychange', syncPlayback);
  window.addEventListener('pageshow', syncPlayback);
  new IntersectionObserver(entries => {
    inView = entries[0].isIntersecting; syncPlayback();
  }, { threshold: 0 }).observe(host);

  mount();
});


// Workspace feedback uses native buttons, keyboard actions and short transitions.
const composerInput = document.getElementById('urlInput');
if (composerInput) {
  const composer = composerInput.closest('.composer');
  const state = document.getElementById('composerState');
  const updateComposer = () => {
    const hasText = Boolean(composerInput.value.trim());
    composer.classList.toggle('has-content', hasText);
    if (state) state.textContent = hasText ? '灵感已就位' : '等待你的灵感';
  };
  composerInput.addEventListener('input', updateComposer);
  composerInput.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.isComposing) {
      event.preventDefault(); document.getElementById('parseBtn')?.click();
    }
  });
  document.querySelectorAll('.action-tile').forEach(button => {
    new MutationObserver(() => {
      button.setAttribute('aria-busy', String(button.disabled));
      updateComposer();
    }).observe(button, { attributes: true, attributeFilter: ['disabled'] });
  });
  document.querySelectorAll('.channel-strip button').forEach(button => {
    button.setAttribute('aria-pressed', String(button.classList.contains('active')));
    new MutationObserver(() => button.setAttribute('aria-pressed', String(button.classList.contains('active'))))
      .observe(button, { attributes: true, attributeFilter: ['class'] });
  });
  updateComposer();
}

const localLabel = document.getElementById('localAiLabel');
if (localLabel) {
  fetch('/api/ai/status').then(r => r.ok ? r.json() : Promise.reject()).then(data => {
    localLabel.textContent = data.installed ? '本地 AI 已配置' : '本地 AI 待安装';
    localLabel.title = data.detail;
  }).catch(() => { localLabel.textContent = '本地 AI 状态待确认'; });
}
