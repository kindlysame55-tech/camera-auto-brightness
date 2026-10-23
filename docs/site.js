(() => {
  'use strict';
  const buttons = [...document.querySelectorAll('[data-lang]')];
  const texts = [...document.querySelectorAll('[data-zh][data-en]')];
  const range = document.getElementById('light-level');
  const preview = document.getElementById('app-preview');
  function language(value) {
    const lang = value === 'en' ? 'en' : 'zh';
    document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
    texts.forEach(element => { element.textContent = element.dataset[lang].replace(/\\n/g, '\n'); });
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.lang === lang)));
    preview.alt = lang === 'en' ? 'Camera Auto Brightness interface with manual brightness, transition speed and pause controls; illustrative diagnostic state.' : '摄像头自动亮度主界面，展示亮度滑块、过渡速度与暂停按钮；画面为模拟状态';
    range.setAttribute('aria-label', lang === 'en' ? 'Website brightness demonstration; changes preview only' : '网页明暗示意，仅改变预览');
    try { localStorage.setItem('camab-language',lang); } catch (_) { /* Storage can be unavailable in private contexts. */ }
  }
  buttons.forEach(button => button.addEventListener('click', () => language(button.dataset.lang)));
  let initialLanguage = 'zh';
  try { if (localStorage.getItem('camab-language') === 'en') initialLanguage = 'en'; } catch (_) { /* Chinese remains the default. */ }
  language(initialLanguage);
  range.addEventListener('input', () => {
    preview.style.filter = `brightness(${(0.55 + Number(range.value) * 0.009).toFixed(3)})`;
  });
})();
