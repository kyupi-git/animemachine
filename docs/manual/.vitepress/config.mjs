const chapters = {
  'zh-Hans': ['开始使用', '电脑部署', 'NAS 联合部署', '首次打开', '订阅与下载', '开始播放', '一键继续观看', '使用已有收藏', '日常管理', '更新与备份', '常见问题'],
  en: ['Start here', 'Desktop setup', 'Linux and Docker', 'First login', 'Subscribe and download', 'Start watching', 'Continue watching', 'Existing collections', 'Daily use', 'Updates and backups', 'Troubleshooting'],
  ja: ['はじめに', 'PC に導入', 'Linux・Docker に導入', '初回ログイン', '購読とダウンロード', '再生する', 'まとめて続きを見る', '既存のコレクション', '日常の管理', '更新とバックアップ', '困ったときは'],
};
const slugs = ['index', 'desktop', 'docker', 'first-use', 'subscribe', 'playback', 'continue-watching', 'collections', 'manage', 'updates', 'troubleshooting'];
const labels = {
  'zh-Hans': { label: '中文', lang: 'zh-Hans', title: '用户手册', contents: '本页内容', menu: '目录', prev: '上一章', next: '下一章', search: '搜索手册', back: '返回 AnimeMachine' },
  en: { label: 'English', lang: 'en', title: 'User manual', contents: 'On this page', menu: 'Menu', prev: 'Previous', next: 'Next', search: 'Search manual', back: 'AnimeMachine' },
  ja: { label: '日本語', lang: 'ja', title: 'ユーザーマニュアル', contents: 'このページの内容', menu: '目次', prev: '前の章', next: '次の章', search: 'マニュアルを検索', back: 'AnimeMachine に戻る' },
};
const online = Boolean(process.env.ANM_MANUAL_BASE);
const manualBase = process.env.ANM_MANUAL_BASE || '/help/';
export default {
  base: manualBase,
  title: 'AnimeMachine',
  description: 'Deploy, subscribe, and watch with AnimeMachine.',
  cleanUrls: false,
  lastUpdated: false,
  ignoreDeadLinks: [/^http:\/\/localhost:8787$/],
  appearance: true,
  head: [['meta', { name: 'referrer', content: 'no-referrer' }], ['link', { rel: 'icon', href: 'data:,' }]],
  locales: Object.fromEntries(Object.entries(labels).map(([locale, value]) => [locale, {
    label: value.label, lang: value.lang, description: value.title,
    themeConfig: {
      nav: [{ text: value.title, link: `/${locale}/` }, ...(online ? [{ text: 'GitHub', link: 'https://github.com/kyupi-git/animemachine' }] : [])],
      sidebar: [{ text: value.title, items: slugs.map((slug, index) => ({ text: `${index ? index + '. ' : ''}${chapters[locale][index]}`, link: `/${locale}/${slug === 'index' ? '' : slug + '.html'}` })) }],
      outline: { label: value.contents, level: [2, 3] },
      sidebarMenuLabel: value.menu, docFooter: { prev: value.prev, next: value.next },
      langMenuLabel: locale === 'zh-Hans' ? '选择语言' : locale === 'ja' ? '言語を選択' : 'Language',
      darkModeSwitchLabel: locale === 'zh-Hans' ? '外观' : locale === 'ja' ? '表示' : 'Appearance',
      lightModeSwitchTitle: locale === 'zh-Hans' ? '切换浅色' : locale === 'ja' ? 'ライトモード' : 'Switch to light theme',
      darkModeSwitchTitle: locale === 'zh-Hans' ? '切换深色' : locale === 'ja' ? 'ダークモード' : 'Switch to dark theme',
      skipToContentLabel: locale === 'zh-Hans' ? '跳到正文' : locale === 'ja' ? '本文へ移動' : 'Skip to content',
      returnToTopLabel: locale === 'zh-Hans' ? '返回顶部' : locale === 'ja' ? 'ページ上部へ' : 'Back to top',
    },
  }])),
  markdown: { image: { lazyLoading: true }, config(md) {
    md.core.ruler.push('manual-downloads', state => {
      for (const block of state.tokens) for (const token of block.children || []) {
        if (token.type === 'link_open' && token.attrGet('href') === '../../../deploy/compose/compose.yaml') {
          token.attrSet('href', manualBase + 'downloads/compose.yaml'); token.attrSet('download', 'compose.yaml');
        }
      }
    });
  } },
  themeConfig: {
    search: { provider: 'local', options: { miniSearch: { options: {
      tokenize: text => Array.from(new Intl.Segmenter('ja', { granularity: 'word' }).segment(text)).filter(part => part.isWordLike).map(part => part.segment),
    } }, locales: Object.fromEntries(Object.entries(labels).map(([locale, value]) => [locale, { translations: {
      button: { buttonText: value.search, buttonAriaLabel: value.search },
      modal: {
        displayDetails: locale === 'zh-Hans' ? '显示详细结果' : locale === 'ja' ? '詳細を表示' : 'Show details',
        resetButtonTitle: locale === 'zh-Hans' ? '清空搜索' : locale === 'ja' ? '検索をリセット' : 'Clear search',
        backButtonTitle: locale === 'zh-Hans' ? '关闭搜索' : locale === 'ja' ? '検索を閉じる' : 'Close search',
        noResultsText: locale === 'zh-Hans' ? '没有找到相关内容' : locale === 'ja' ? '該当する項目はありません' : 'No results found',
        footer: { selectText: locale === 'zh-Hans' ? '选择' : locale === 'ja' ? '選択' : 'Select',
          navigateText: locale === 'zh-Hans' ? '切换' : locale === 'ja' ? '移動' : 'Navigate',
          closeText: locale === 'zh-Hans' ? '关闭' : locale === 'ja' ? '閉じる' : 'Close' },
      },
    } }])) } },
    socialLinks: [{ icon: 'github', link: 'https://github.com/kyupi-git/animemachine' }],
    externalLinkIcon: true,
  },
  vite: { build: { assetsInlineLimit: 0 } },
};
