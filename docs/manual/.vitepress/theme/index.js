import DefaultTheme from 'vitepress/theme-without-fonts';
import { h } from 'vue';
import { useData } from 'vitepress';
import './style.css';
import { installCopyButtons } from './copy.js';

export default {
  ...DefaultTheme,
  Layout() {
    const { lang, site } = useData();
    return h(DefaultTheme.Layout, null, {
      'nav-bar-content-after': () => site.value.base === '/help/'
        ? h('a', { href: '/', class: 'back-to-app' }, lang.value === 'zh-Hans' ? '返回应用' : lang.value === 'ja' ? 'アプリに戻る' : 'Back to app') : null,
      'nav-screen-content-after': () => site.value.base === '/help/'
        ? h('a', { href: '/', class: 'back-to-app-mobile' }, lang.value === 'zh-Hans' ? '返回应用' : lang.value === 'ja' ? 'アプリに戻る' : 'Back to app') : null,
    });
  },
  enhanceApp({ router }) {
    if (typeof window === 'undefined') return;
    installCopyButtons();
    const update = () => {
      for (const image of document.querySelectorAll('.vp-doc img')) {
        if (image.closest('a')) continue;
        const anchor = document.createElement('a');
        anchor.href = image.currentSrc || image.src;
        anchor.target = '_blank'; anchor.rel = 'noopener';
        anchor.setAttribute('aria-label', image.alt);
        image.replaceWith(anchor); anchor.append(image);
      }
    };
    router.onAfterRouteChanged = () => requestAnimationFrame(update);
    window.addEventListener('load', update, { once: true });
  },
};
