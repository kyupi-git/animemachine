// Keep deployment commands usable on both HTTP LAN pages and HTTPS sites.
export function installCopyButtons() {
  const pending = new WeakMap();
  window.addEventListener('click', async event => {
    const button = event.target.closest?.('div[class*="language-"] > button.copy');
    if (!button) return;
    event.stopImmediatePropagation();
    const text = button.parentElement.querySelector('pre code')?.textContent;
    if (!text) return;
    let copied = false;
    try { await navigator.clipboard.writeText(text); copied = true; } catch { /* HTTP or denied permission: use a selected field. */ }
    if (!copied) {
      const previous = document.activeElement, field = document.createElement('textarea');
      field.value = text; field.readOnly = true;
      field.style.cssText = 'position:fixed;left:0;top:0;opacity:0;font-size:16px';
      document.body.append(field);
      try {
        field.focus({preventScroll:true}); field.select(); field.setSelectionRange(0, text.length);
        copied = document.activeElement === field && document.execCommand('copy');
      } catch { /* The complete command remains available in the prompt below. */ }
      finally { field.remove(); previous?.focus({preventScroll:true}); }
    }
    if (!copied) {
      const lang = document.documentElement.lang;
      window.prompt(lang === 'zh-Hans' ? '按 Ctrl+C 复制命令' : lang === 'ja' ? 'Ctrl+C でコマンドをコピー' : 'Press Ctrl+C to copy the command', text);
      return;
    }
    clearTimeout(pending.get(button)); button.classList.add('copied');
    pending.set(button, setTimeout(() => { button.classList.remove('copied'); pending.delete(button); }, 2000));
  }, true);
}
