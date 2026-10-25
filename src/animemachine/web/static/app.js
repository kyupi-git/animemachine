const detectSystemLanguage = (available = ["zh-Hans", "en", "ja"]) => {
    const allowed = new Set(available),
      normalize = (value) => {
        const code = String(value || "").toLowerCase();
        return code.startsWith("zh")
          ? "zh-Hans"
          : code.startsWith("ja")
            ? "ja"
            : code.startsWith("en")
              ? "en"
              : "";
      },
      detected = (navigator.languages || [navigator.language])
        .map(normalize)
        .find((value) => allowed.has(value));
    return detected || (allowed.has("en") ? "en" : available[0] || "en");
  },
  storedLanguage = () => {
    try {
      return localStorage.getItem("anm-language");
    } catch (_) {
      return null;
    }
  },
  $ = (id) => document.getElementById(id),
  results = $("results"),
  detailDialog = $("detailDialog"),
  relationDialog = $("relationDialog"),
  planDialog = $("planDialog");
let config = {},
  capabilities = {},
  items = [],
  options = {},
  groupCatalog = { serialProfiles: { zh: [], en: [], ja: [] } },
  total = 0,
  searchExpanded = false,
  page = 0,
  language = storedLanguage() || detectSystemLanguage(),
  view = "cards",
  pageSize = "12",
  sort = "recent_episode",
  configuredSort = "recent_episode",
  recentEpisodeSortAvailable = true,
  lastAniRssGeneration = null,
  direction = "asc",
  imagesEnabled = true,
  coverVersion = 0,
  coverObserver,
  coverBatch,
  searchController,
  catalogRefreshPending = false,
  catalogRenderPending = false,
  renderedResultsMarkup = "",
  timer,
  catalogStats = {},
  startupState = null;
const performanceReported = new Set();
const catalogPages = new Map();
let catalogPageRevision = 0, catalogPrefetchTimer;
function invalidateCatalogPages() { catalogPages.clear(); catalogPageRevision++; }
async function catalogPage(query, { signal, preferCache = false } = {}) {
  const key = `${authSession?.username || ""}:${query.toString()}`, cached = catalogPages.get(key);
  if (preferCache && cached && cached.expires > Date.now() && !cached.signal?.aborted) return cached.promise;
  const revision = catalogPageRevision;
  const entry = { expires: Date.now() + 30000, signal };
  entry.promise = api(`/api/anime?${query}`, { signal, timeoutMs: 15000 });
  catalogPages.set(key, entry);
  while (catalogPages.size > 12) catalogPages.delete(catalogPages.keys().next().value);
  entry.promise.then(() => {
    entry.signal = undefined;
    if (revision !== catalogPageRevision && catalogPages.get(key) === entry) catalogPages.delete(key);
  }, () => { if (catalogPages.get(key) === entry) catalogPages.delete(key); });
  return entry.promise;
}
const renderedContents = new WeakMap();
let authSession = null,
  csrfToken = "";
let seed = "";
const selected = new Set(),
  torrentSelections = new Map(),
  resourceSelections = new Map(),
  expandedTorrentWorks = new Set(),
  policyOverrides = new Map(),
  playbackSources = new Map(),
  playbackEditions = new Map(),
  planState = { id: null },
  relationHiddenCodes = new Set();
const modalStack = [];
function topModalDialog() {
  for (let index = modalStack.length - 1; index >= 0; index -= 1) {
    if (modalStack[index]?.open) return modalStack[index];
  }
  return null;
}
function showModalDialog(dialog) {
  if (!dialog || dialog.open) return;
  dialog.showModal();
  for (let index = modalStack.length - 1; index >= 0; index--) {
    if (modalStack[index] === dialog) modalStack.splice(index, 1);
  }
  modalStack.push(dialog);
}
function canScrollVertically(element, deltaY) {
  if (!(element instanceof Element) || Math.abs(deltaY) < 0.01) return false;
  const overflow = getComputedStyle(element).overflowY;
  if (!/(auto|scroll|overlay)/.test(overflow) || element.scrollHeight <= element.clientHeight + 1) return false;
  return deltaY < 0
    ? element.scrollTop > 0
    : element.scrollTop + element.clientHeight < element.scrollHeight - 1;
}
function modalWheelHasTarget(event, dialog) {
  if (!dialog.contains(event.target)) return false;
  let element = event.target instanceof Element ? event.target : event.target?.parentElement;
  while (element && dialog.contains(element)) {
    if (canScrollVertically(element, event.deltaY)) return true;
    if (element === dialog) break;
    element = element.parentElement;
  }
  return false;
}
function scrollModalFallback(event, dialog) {
  if (!dialog.contains(event.target) || dialog.id !== "settingsDialog") return false;
  const panel = dialog.querySelector(".tab-panel.active");
  if (!canScrollVertically(panel, event.deltaY)) return false;
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? Math.max(1, panel.clientHeight) : 1;
  panel.scrollTop += event.deltaY * scale;
  return true;
}
const i18n = {
  "zh-Hans": {
    pathExamples: "路径填写示例",
    pathWindowsHint: "Windows：填写本机文件夹，或当前运行账号可访问的网络共享。",
    pathDockerHint: "Docker：填写容器内的挂载路径，对应部署文件中的目录映射。",
    qbtPathHint: "填写 qBittorrent 所在设备中的保存路径；联合部署通常为 /Library。",
    pathReadable: "可读取",
    pathSaveToCheck: "保存后检查此路径",
    pathMissing: "目录不存在，请检查路径或挂载",
    pathPermissionDenied: "无权读取，请检查运行账号和共享权限",
    pathUnavailable: "暂时无法访问，请检查设备连接或挂载",
    pathCredentialsInvalid: "共享认证失败，请检查挂载凭据",
    pathNotChecked: "尚未取得路径状态",
    radarDefaultSort: "默认顺序",
    catalog: "AnimeMachine",
    catalogTagline: "全自动动画收藏库",
    aboutTitle: "关于 AnimeMachine",
    checkUpdate: "检查更新",
    updateNow: "更新",
    forceUpdateNow: "强制更新",
    updateChecking: "正在检查更新…",
    updateAvailable: "检测到新版{version}可用！",
    updateLatest: "当前已是最新版本。",
    updatePreparing: "正在下载并校验更新…",
    updateRestarting: "更新已就绪，AnimeMachine 正在自动重启…",
    updateFailed: "更新失败：{message}",
    updateRestartTimeout: "更新后服务未在预期时间内恢复。",
    playbackHandoffUnavailable: "无法生成外部播放器链接。",
    randomSeedUnavailable: "无法取得新的随机种子。",
    maintenanceUnsupported: "不支持的维护操作。",
    updateCheckFailed: "检查更新失败：{message}",
    updateDockerRequired: "当前 Docker 镜像尚不包含 Web 在线升级运行时；请先通过 Docker Compose 更新一次镜像，之后即可直接在线升级。",
    updateDockerStateUnavailable: "Docker 状态目录不可写，无法安全保存在线升级数据。",
    updatePortableRequired: "在线升级仅对官方便携 Release 启用；源码部署仍可检测新版本。",
    updateUnavailable: "当前平台缺少可用的在线升级包。",
    updateTab: "检查更新",
    updateDetailsHint: "检查新版，查看下载进度和安装结果。",
    currentVersionLabel: "当前版本",
    latestVersionLabel: "最新版本",
    downloadSourceLabel: "下载源",
    sha256StatusLabel: "SHA-256 校验",
    upgradeResultLabel: "升级结果",
    lastCheckLabel: "最近检查",
    automaticUpdateCheck: "定时自动检查更新",
    automaticUpdateEnabled: "启用定时检查",
    automaticUpdateMode: "发现新版后",
    updateNotifyOnly: "仅通知",
    updateAutoInstall: "自动安装并重启",
    updateCheckTime: "每日检查时间",
    automaticUpdateHint: "默认每天按运行设备的本地时间检查一次，发现新版时通知。可选择自动安装并重启；安装前会校验 Release 包和 SHA-256。",
    updateNetworkSources: "更新线路",
    shaPending: "等待下载校验",
    shaVerified: "已通过",
    shaFailed: "校验失败",
    shaUnavailable: "不可用",
    shaNotRequired: "无需校验",
    upgradeIdle: "尚未升级",
    upgradeDownloading: "正在下载",
    upgradeRestarting: "已安装，正在重启",
    upgradeInstalled: "升级成功",
    upgradeFailedState: "升级失败",
    upgradeStatusAvailable: "发现新版本",
    upgradeStatusLatest: "当前已是最新版本",
    upgradeStatusUnavailable: "发现新版，但当前环境不能在线安装",
    upgradeStatusInstalling: "正在自动安装",
    updateSourceDirect: "直连",
    updateSourceProxy: "代理",
    aboutLead: "AnimeMachine 帮助你浏览动画、订阅新作，并自动整理本地收藏库。",
    aboutDetail: "在作品详情中查看资源、核验收藏和播放媒体；通过下载计划确认需要新增或替换的文件。",
    aboutRelations: "作品关系图展示前作、续作、番外和衍生作品，便于按系列查找和收藏。",
    aboutComponents: "可按需连接 Ani-RSS、qBittorrent 和外部媒体库，使用订阅、下载和远程播放功能。",
    aboutArchiveSource: "提供本地动画元数据底包与作品关系基础。",
    aboutBangumiSource: "用于补全底包中缺失或明显异常的公开元数据。",
    aboutAniRssSource: "提供可选的新番订阅、状态同步与远程播放能力；AnimeMachine 通过其 HTTP API 连接。",
    aboutSubtitleSources: "是用户可选的外部字幕 API；字幕版权及使用条件由对应服务说明。",
    cards: "卡片",
    table: "表格",
    settings: "设置",
    accountSettings: "账户设置", changePassword: "修改密码", currentPassword: "当前密码", newPassword: "新密码", confirmPassword: "确认新密码",
    changePasswordHint: "新密码为 10–256 个字符；修改后，其它设备需要重新登录。", passwordMismatch: "两次输入的新密码不一致。",
    passwordChanged: "密码已修改。当前设备可继续使用，其它设备请重新登录。", currentPasswordIncorrect: "当前密码不正确，请重新输入。",
    passwordLength: "新密码须为 10–256 个字符。", passwordUnchanged: "请输入与当前密码不同的新密码。", passwordRetry: "账户状态已变化，请重新登录后再试。",
    passwordRateLimited: "尝试次数较多，请 5 分钟后重试。",
    manual: "手册",
    loginTitle: "登录 AnimeMachine",
    login: "登录",
    logout: "退出",
    username: "用户名",
    password: "密码",
    usersTab: "用户",
    usersHint: "普通用户可浏览、播放和提交任务，不能修改设置。",
    userCreateHint: "用户名为 3–64 个字母、数字、点、下划线或连字符；密码至少 10 位。",
    userRole: "角色",
    existingUsers: "现有用户",
    initialAdmin: "初始管理员",
    currentUser: "当前用户",
    disabledUser: "已停用",
    noUsers: "暂无用户。",
    userCreated: "用户已创建。",
    userUpdated: "用户状态已更新。",
    normalUser: "普通用户",
    administrator: "管理员",
    createUser: "创建用户",
    disableUser: "停用",
    disableInitialAdmin: "停用初始admin用户（建议创建新的管理员用户，停用初始admin用户）",
    enableUser: "启用",
    submissionAllowed: "允许提交任务",
    loadingDb: "正在读取数据库…",
    backgroundSync: "后台同步",
    loadingWorks: "作品加载中，请稍等...",
    resetAll: "重置全部",
    custom: "自定义",
    titleAlias: "标题 / 别名",
    searchPlaceholder: "中文、英文、日语或简称",
    era: "年",
    season: "季",
    seasonSpring: "春（4月前后）", seasonSummer: "夏（7月前后）", seasonAutumn: "秋（10月前后）", seasonWinter: "冬（1月前后）",
    mediaFormat: "媒介形式",
    seriesWork: "系列作品",
    yes: "是",
    no: "否",
    startMonth: "起始月份",
    endMonth: "截止月份",
    sourceOrigin: "原作来源",
    sourceType: "类型",
    originalName: "原作名称",
    originalAuthor: "作者",
    studio: "动画制作",
    director: "监督",
    seriesComposition: "系列构成",
    characterDesign: "角色设计",
    music: "音乐",
    voiceActor: "声优",
    personSearch: "输入姓名",
    tag: "题材 / 标签",
    all: "全部",
    works: " 部作品",
    loading: "正在检索…",
    none: "没有符合条件的作品。",
    failed: "操作失败",
    queryFailed: "查询失败",
    invalidDownloadRoute: "下载方式无效，请刷新页面后重新选择作品。",
    pending: "待完善",
    directoryDate: "目录年月",
    episodes: "集数",
    country: "地区",
    titles: "标题与别名",
    cast: "声优",
    allCast: "展开全部语言声优",
    relations: "关联作品",
    viewRelations: "作品关系图",
    relationGraph: "系列作品关系",
    relationHint:
      "箭头表示作品关系；灰色的虚框节点是相关联的不同系列作品。",
    graphUnavailable: "没有可展示的系列关系。",
    graphSelected: "已加入下载选择",
    graphUnavailableSelect: "当前没有可提交的合格资源",
    graphExistingWarning:
      "本地已有内容；计划将只补充缺失项，并将经证实的更新下载到暂存区。",
    graphContext: "关联参考",
    graphContextTruncated: "关联参考较多，当前仅显示最接近的部分节点。",
    fullscreenGraph: "全屏显示",
    exitFullscreen: "退出全屏",
    relatedWorksCount: "系列内作品数：{count}",
    exportPng: "导出 PNG",
    exportSvg: "导出 SVG",
    graphContextLine: "跨系列关联",
    globalSearchResults: "以下为全体作品的搜索结果",
    add_missing: "新增缺失",
    skip_unchanged: "跳过未变化",
    stage_replace: "暂存替换",
    conflict_review: "冲突待确认",
    previous_selection: "既有任务已选择",
    not_selected: "未选择",
    planNoDownload: "所选作品没有需要新增或替换的文件。",
    planBuilding: "正在后台生成任务预览，可继续浏览…",
    planWarnings: "计划包含需要注意的既有内容或冲突。",
    libraryPathUnreadable: "无法读取收藏库目标路径。",
    localFileUnreadable: "无法读取本地文件。",
    managedFileChanged: "本地文件与已记录的来源校验值不一致。",
    exactComparisonUnavailable: "本地校验已完成；缺少来源校验值，暂无法确认内容一致。",
    managedHashBaselineRecorded: "已为来源明确的受管文件记录首个哈希基准。",
    targetIsSymbolicLink: "目标是符号链接或重解析文件，已阻止自动写入。",
    targetNotRegularFile: "目标位置已被目录或其它非普通文件占用。",
    localFileChangedDuringHash: "本地文件在哈希过程中发生变化，请等待写入完成后重试。",
    stagedReplacement: "旧文件会保留；新版完成下载和校验前只进入暂存区。",
    sizeConflict: "同一路径的既有文件大小不同，且候选资源没有充分的修订证据。",
    summary: "简介",
    settingsHint: "修改后点击“保存”生效。",
    acceptedContent: "允许的资源类别",
    stopped: "停止",
    save: "保存",
    saved: "已保存",
    savedScanning: "已保存，正在后台扫描媒体目录",
    mediaScanProgress: "正在清点媒体 · {files} 个文件",
    availability: "可用来源",
    available: "有可用来源",
    unavailable: "无可用来源",
    torrentSource: "Torrent 来源",
    aniRssSource: "Ani-RSS 来源",
    downloadSources: "个可下载源",
    externalMediaAvailable: "外部媒体已映射",
    noAvailableSource: "无可用来源",
    libraryState: "收藏库状态",
    localLibrary: "本地入库",
    externalReadOnly: "外部只读",
    submittedDownload: "提交下载",
    notInLibrary: "暂未入库",
    existing: "已存在·已下载",
    localExisting: "本地已有",
    managedComplete: "下载完成",
    placeholder: "无媒体·仅占位",
    queued: "已提交·待开始",
    downloading: "已提交·下载中",
    absent: "未建目录",
    externalLibrary: "外部媒体来源",
    external: "外部来源（只读）",
    selectedWorks: "部作品已选择",
    clear: "清空",
    previewPlan: "预览下载计划",
    enableSubscription: "启用订阅",
    subscriptionPlan: "订阅确认",
    mixedPlan: "订阅与下载",
    taskPreview: "任务预览",
    torrents: "资源",
    reloadList: "重新载入",
    collectorWorkSearch: "作品资源检索", collectorSearchLimits: "检索数量与时限", collectorMaxResults: "最多保存资源数", collectorTimeout: "检索时限（秒）", collectorPool: "Torrent 资源池", collectorMonitor: "定时扫描资源池", collectorCollection: "自动采集 Torrent", collectorHistory: "补充历史资源", collectorHint: "作品检索可直接使用；自动采集由联合部署中的 Torrent Collector 执行。资源收录与排序沿用“常规”和“资源组”中的偏好。", collectorSourcesRequired: "请至少选择一个 Torrent 检索来源。",
    aniRssSubscriptionResources: "Ani-RSS 订阅", torrentResources: "Torrent 下载", exactResourceHint: "订阅仅使用所选资源组和版本。", resourceVariantChanged: "资源版本已更新，请重新选择。",
    noTorrent: "暂未找到资源，可尝试重新搜索。",
    excludedSourceClass: "片源类型不符",
    excludedSerialProfile: "语言不符",
    forceSubscribe: "强行订阅",
    forceDownload: "强行下载",
    forcePolicyNotice: "本次使用未勾选的资源，仅对当前选择生效。",
    excludedResourceGroup: "资源组不符",
    excludedResolution: "分辨率不符",
    excludedSubtitle: "字幕不符",
    excludedScope: "作品范围不符",
    excludedManifest: "文件映射不符",
    excludedPolicy: "策略不符",
    searchPoolNow: "检索 Torrent", torrentSearchPartial: "部分来源暂不可用，可稍后重试。", torrentSearchFailed: "检索未完成，请检查网络后重试。", torrentSearchProgress: "已查询 {done} / {total} 个来源 · 找到 {found} 项",
    searchAniRss: "通过 Ani-RSS 查询资源",
    aniRssManaged: "Ani-RSS 已订阅",
    aniRssResources: "Ani-RSS 资源可用",
    aniRssMode: "资源调用模式",
    aniRssPrefer: "优先调用 Ani-RSS",
    aniRssFallback: "备选调用 Ani-RSS",
    aniRssManual: "手动调用 Ani-RSS",
    aniRssApiKey: "API Key",
    aniRssApiKeyHint: "密钥单独保存，重启后仍可用；部署配置中的密钥优先。",
    aniRssCredentialConfigured: "API Key 已保存到受限凭据存储。",
    aniRssMediaPath: "Ani-RSS 媒体路径",
    aniRssSyncMinutes: "状态同步间隔（分钟）",
    aniRssHint: "此模式只控制资源查询与规划；订阅和 API 媒体状态仍按同步间隔更新。连接无效时自动停用远端资源，恢复后使用所选模式；媒体目录始终只读。",
    searchPoolRunning: "正在检索资源池…",
    searchPoolDone: "检索完成，已更新可用资源",
    searchPoolNone: "检索完成，暂未找到对应资源",
    verifyWork: "立即核验",
    verifyRunning: "正在核验…",
    scanIdle: "后台图片状态加载中",
    library: "收藏库",
    playback: "播放",
    noPlayableMedia: "尚无可播放的正片",
    playlistEntries: "个正片",
    systemPlayer: "下载播放列表",
    copyPlaylist: "复制播放列表地址",
    copiedPlaylist: "播放列表地址已复制",
    openVlc: "使用 VLC 打开",
    openPotPlayer: "使用 PotPlayer 打开",
    openIina: "使用 IINA 打开",
    reloadImage: "重新加载",
    imageLoading: "图片加载中",
    imageRetrying: "图片重试中",
    imageNoImage: "暂无图片",
    imageError: "图片加载失败",
    aniRssDelete: "删除",
    aniRssPause: "暂停订阅", aniRssResume: "恢复订阅", aniRssCancel: "取消订阅（保留文件）", aniRssDeleteFiles: "删除订阅及文件",
    aniRssCancelConfirm: "取消“{title}”的 Ani-RSS 订阅并保留已下载文件？",
    radarInterrupted: "检索已中断，可继续", librarySleepInvalid: "休眠开始时间和结束时间不能相同。",
    aniRssDeleteConfirm: "确认删除此 Ani-RSS 订阅及其已下载文件？",
    aniRssDeleteFailed: "Ani-RSS 删除未确认",
    themeSystem: "跟随系统",
    themeDark: "暗黑模式",
    themeLight: "浅色模式",
    languageControl: "界面语言",
    closeDialog: "关闭",
    sortDirectionLabel: "排序方向",
    settingsSectionsLabel: "设置分区",
    remotePlaybackSource: "Ani-RSS 远程播放",
    playbackSource: "播放来源",
    playbackEdition: "播放版本",
    playbackAutomatic: "自动选择",
    indexingLocalMedia: "正在识别本地视频…",
    offlineUpdate: "上传离线升级包",
    offlinePackageHint: "请选择适合当前系统的 .anm-update.zip 离线升级包。",
    offlineUpdateConfirm: "校验并安装此离线升级包？安装完成后会自动重启，配置和收藏库保持不变。",
    offlineNotNewer: "此包不是更新版本，请选择较新的升级包。",
    offlinePlatform: "此包不适用于当前部署方式，请选择对应的 Windows 或 Docker 升级包。",
    offlineChecksum: "升级包校验未通过，请重新复制完整文件后重试。",
    offlineIncomplete: "文件上传未完成，请重新选择并上传。",
    updatePending: "更新正在完成，请稍后重试。",
    updateLocalReady: "可点击“检测更新”，或直接上传离线升级包。",
    updateRolledBack: "新版未能启动，已自动恢复原版本，可继续使用。",
    offlineBaseImage: "此版本需要更新 Docker 基础镜像，请导入对应的离线镜像，再按用户指南重新建立容器。",
    upgradeHistorical: "当前版本已运行；上次安装尝试未完成",
    aniRssRemoteOnlyLibrary: "媒体由 Ani-RSS 远程提供，请在“播放”中选择来源。",
    subtitleActions: "字幕",
    playbackStartFile: "起始媒体文件",
    managed: "AnimeMachine 管理",
    preexisting: "本地既有",
    collection: "合集",
    files: "文件",
    complete: "已收集完成",
    nearComplete: "接近收集完成",
    partialHigh: "大部分已收集",
    partial: "部分已收集",
    incomplete: "收集不完整",
    unassessed: "完整度尚未评估",
    attachments: "附件",
    plan: "下载计划",
    tasks: "个任务",
    planStopped: "任务将以停止状态创建。",
    submitStopped: "确认提交",
    planUseAniRss: "批量使用 Ani-RSS 订阅",
    planUseTorrent: "仅使用 Torrent 下载",
    planRestoreDefault: "恢复默认",
    torrentUnavailable: "无可用 Torrent 资源",
    submitAccepted: "任务已提交，正在后台处理。",
    submissionDisabled: "当前部署未启用实际提交",
    sortBy: "排序",
    recentEpisodeSort: "新作追更",
    catchup: "一键继续观看", catchupEmpty: "本季度暂无可继续观看的 TV 集数。", catchupPreparing: "正在整理可播放集数…",
    catchupCount: "{works} 部作品 · {episodes} 集", catchupHint: "仅列出已订阅且有新集待看的 TV 动画，默认跳过已看完（≥90%）的集数。",
    catchupSelectHint: "勾选作品、调整集数范围，拖动排序。", catchupRangeFrom: "起始集", catchupRangeTo: "结束集", catchupRangeFromShort: "起始", catchupRangeToShort: "结束", catchupRangeInvalid: "请检查集数范围（必须在可播放集数内，且起始集不大于结束集）。", catchupSelectionOutdated: "作品或媒体状态已变化，请关闭窗口后重新打开续播列表。", catchupSelectRequired: "请先勾选要看的作品。", catchupSelectedCount: "已选择 {works} 部作品 · {episodes} 集",
    catchupDrag: "拖动排序，也可使用上下方向键",
    viewingResume: "续看：第 {episode} 集", viewingNew: "未开始", viewingEdit: "修改续看集数",
    viewingEpisode: "下次从这一集开始观看", viewingClear: "从头开始观看", viewingUndo: "撤销", viewingComplete: "已看完最新集",
    viewingSaved: "已修改", viewingConflict: "记录已在其它设备更新，请重新打开后修改。",
    potplayerInstall: "安装扩展以记忆播放进度", potplayerHint: "仅 PotPlayer 需要。安装扩展后可记录位置并续播；未安装时正常播放，只记录集数。", viewingPosition: "播放位置（时:分:秒）",
    radar: "新作雷达",
    radarPrevious: "上季度", radarCurrent: "本季度", radarNext: "下季度",
    viewingUnavailable: "观看记录暂不可用", radarUnwatched: "有新集待看", radarTitleColumn: "标题", radarProgress: "最新 / 总集数", radarUpdated: "新集更新时间",
    radarStart: "开始时间", radarWatched: "已看集数", radarBdRelease: "BD", radarResourceRelease: "资源", radarSearch: "检索本季度资源", radarSearchProgress: "已检索 {done} / {total} · 失败 {failed}",
    episodeUpdated: "第{episode}集",
    radarSubscription: "订阅状态", radarSubscribed: "已订阅", radarPaused: "已暂停",
    radarSubscribe: "订阅", radarChooseResource: "选择订阅资源",
    followWork: "资源跟踪", followMode: "跟踪方式", advanceSubscribe: "提前订阅连载", followArchive: "跟踪收藏资源",
    followPreferences: "使用设置中的资源组偏好", followCancel: "停止跟踪", followPending: "待资源", followAvailable: "发现资源",
    followSerialHint: "定期检索，找到符合偏好和所选版本的资源后，自动启用 Ani-RSS 订阅。",
    followArchiveHint: "定期检索收藏类资源，发现后提示，由你选择是否下载。",
    followOfflineHint: "保存后保留订阅意向；连接 Ani-RSS 并发现匹配资源后，自动启用订阅。", followSaved: "已保存，将在后台定期检查。",
    followChooseGroup: "请至少选择一个资源组。", groupVersions: "版本", allVersions: "全部", unknownVersion: "未标注平台",
    selectAllGroups: "全选", clearAllGroups: "全不选",
    followUnmatched: "有资源，暂未匹配偏好", followManaged: "已有 Ani-RSS 订阅", followEmpty: "暂无跟踪记录。可在作品详情中提前订阅或跟踪收藏资源。",
    followOpen: "查看资源",
    followChanged: "跟踪记录已变化，请重新打开后再修改。", followRemove: "移除记录", followManagedHint: "已有订阅继续按 Ani-RSS 的设置运行。调整资源组或暂停订阅，请使用作品详情的订阅菜单。",
    radarNoResources: "暂未找到可订阅资源，请稍后重试。", radarSubscribeHint: "选择资源组后订阅，下载由 Ani-RSS 按其配置执行。",
    radarEmpty: "这一季暂无作品。", radarLoading: "正在加载…",
    radarSubscriptionDone: "订阅已提交；状态将在同步后更新。", radarSubscriptionPending: "等待同步",
    radarSearchTimeout: "资源检索仍在后台运行，请稍后重新打开。",
    radarSync: "与 Ani-RSS 同步", radarSyncShort: "同步状态", radarSearchShort: "检索资源", radarFoldersShort: "建立目录", radarRetryShort: "重试", radarSyncing: "正在同步订阅与媒体状态…",
    radarChecking: "正在检索…", radarComplete: "检索完成", radarRetry: "重试未完成作品",
    radarSubmitting: "正在提交订阅…", radarSubmittingHint: "正在提交至 Ani-RSS，可关闭此窗口继续浏览。",
    radarPolicyExcluded: "资源不符合下载设置", radarWorks: "{count} 部作品",
    radarTaskProgress: "检索进度",
    retryAction: "重试",
    recentEpisodeRequiresAniRss: "连接 Ani-RSS 后可检索资源与管理订阅。",
    random: "随机",
    sortDate: "年月",
    sortTitle: "作品名",
    sortStudio: "制作公司",
    sortType: "作品类型",
    reshuffle: "重新随机",
    perPage: "每页",
    hideWork: "隐藏作品", hiddenWorks: "已隐藏作品", workHidden: "已隐藏", restoreWork: "还原显示", noHiddenWorks: "没有已隐藏作品。",
    hiddenWorksHint: "隐藏仅影响当前账号的列表和一键继续观看，原订阅与下载保持不变。", findRecords: "搜索记录", resourcePickHint: "选择下方资源，然后核对并提交。", resourceUnmappedHint: "已取得资源，但尚未确认作品匹配；暂不能提交下载。",
    resourceExcludedHint: "资源与当前偏好不符。可选择允许强行下载的资源，查看提示后继续。", logInfo: "信息", logWarning: "警告", logError: "错误", logAll: "全部记录", logProblems: "警告与错误", technicalDetails: "技术详情", historySource: "原位置", historyTarget: "目标位置", logTaskStatus: "后台任务状态", logTaskComplete: "后台任务已完成", logTaskFailed: "任务未完成",
    previous: "上一页",
    next: "下一页",
    priorityDimensions: "优选维度（从上到下）",
    priorityHint:
      "从上到下比较优选维度；点击任一维度可展开其二级顺序或判定说明。",
    resourcePriorityTab: "资源优先级",
    fixedPriorityHint: "此维度按内置规则判定，没有可拖动的二级项。",
    resourceCompletenessHint: "完整合集或可完整拼接的资源优先。连载作品需从第1集起连续完整，最多落后已知最新进度1集。",
    groupPriorityHint: "收藏类和不同字幕语言的连载类排序，请在“资源组”选项卡调整。",
    groupPriority: "资源组",
    classPriority: "片源类型",
    resolutionPriority: "分辨率",
    subtitlePriority: "字幕",
    catalogData: "目录数据",
    updateArchive: "检查并更新底库",
    updateArchiveHint: "检查新版底包，仅合并发生变化的资料。",
    importArchive: "导入已下载的底包",
    importArchiveHint: "离线导入 Bangumi Archive ZIP；将自动核验文件信息。",
    archiveImporting: "正在导入并核验底包…",
    repairMetadata: "检查并补全作品资料",
    repairMetadataHint: "批量补充本地缺失或明显异常的元数据。",
    auditLibraryHint: "重新扫描目录并评估已有内容的完整度。",
    archiveReady: "动画底库",
    torrentAssets: "个 Torrent 资源",
    archiveChecking: "正在检查底库更新…", archivePreparing: "正在准备新版底库…", archiveMerging: "正在合并更新资料…",
    archiveUnchanged: "动画底库已是最新",
    archiveComplete: "更新完成",
    archiveFailed: "动画底库更新失败",
    other: "其它",
    generalTab: "常规",
    resourceGroupsTab: "资源组",
    resourceGroupsHint: "收藏类资源不限制字幕；连载资源按所选语言和资源组筛选，日语允许无字幕资源。",
    serialSubtitleLanguage: "连载资源字幕语言",
    followInterfaceLanguage: "跟随界面语言",
    archiveGroups: "收藏类 / 归档类",
    archiveGroupsHint: "默认全部启用，排序不考虑字幕。",
    chineseSerialGroups: "中文连载资源",
    englishSerialGroups: "英文连载资源",
    japaneseSerialGroups: "日文连载资源",
    otherResourceGroups: "其它（未识别或不符合上述规则）",
    connectionsTab: "连接",
    diagnosticsTab: "诊断",
    diagnosticsHint: "查看当前运行状态、网络连接和近期结果。",
    systemHealth: "系统健康",
    systemHealthHint: "网络、图片和播放详情可在“诊断”中查看。",
    healthLoading: "正在汇总运行状态…",
    healthOverallNormal: "系统主要链路正常",
    healthOverallWarning: "存在需要关注的运行状态",
    healthNormal: "正常",
    healthWarning: "警告",
    healthNetwork: "网络",
    healthAniRss: "Ani-RSS",
    healthQbittorrent: "qBittorrent",
    healthStorage: "存储",
    healthImagePreload: "图片预加载",
    healthPlayback: "播放链路",
    healthReady: "就绪",
    healthUnknown: "尚无近期结果",
    healthDegraded: "近期异常",
    healthUnavailable: "不可用",
    healthDisabled: "已关闭",
    healthNotConfigured: "未配置",
    healthManual: "手动模式",
    healthWarming: "后台准备中",
    healthWarm: "准备完成",
    healthFailed: "失败",
    healthSamples: "近期样本",
    healthWarnings: "异常项",
    healthSessions: "会话",
    healthMode: "模式",
    healthState: "状态",
    healthChecked: "检查项",
    startupStatus: "启动与后台准备",
    networkDiagnostics: "网络诊断",
    recheckNetwork: "重新检测",
    currentRoute: "当前请求路径",
    networkProfile: "当前网络环境",
    performanceBaseline: "性能基线",
    perfCatalogReady: "作品目录就绪",
    perfFirstScreen: "首屏可用",
    perfFirstCover: "首张封面",
    perfWarmComplete: "后台准备完成",
    previousRun: "上次运行",
    routeDirect: "直连",
    routeEnvironment: "环境代理",
    routeWindows: "Windows 系统代理",
    routeSystem: "系统代理",
    imageSourceTrend: "图片源短期健康趋势",
    trend12h: "近 12 小时",
    noTrendData: "暂无趋势数据",
    sourceSelected: "当前优选",
    sourceConfidence: "样本置信度",
    sourceScore: "综合评分",
    selectionIncumbentBest: "当前源仍为最优",
    selectionBestQuality: "综合评分最高",
    selectionHysteresisHold: "切源保持期内，暂不切换",
    selectionHysteresisMargin: "候选优势不足，保持当前源",
    selectionIncumbentCooldown: "当前源进入冷却，切换到候选源",
    selectionCandidateCooldown: "候选源仍在冷却，保持当前源",
    selectionMeaningfullyBetter: "候选源优势已超过切换阈值",
    catalogAvailable: "作品目录已就绪",
    catalogPreparing: "作品目录准备中",
    backgroundImagesPreparing: "后台图片准备中",
    preferencesSaveFailed: "浏览器无法保存显示偏好，请允许本站使用本地存储。",
    catalogResolving: "正在检查底库更新",
    catalogDownloading: "正在下载底库",
    catalogParsing: "正在解析底库",
    catalogWriting: "正在写入底库",
    catalogPreparingFailed: "底库准备失败，请检查网络后重试。",
    errorCredentials: "用户名或密码错误，请重新输入。",
    errorPermission: "需要管理员权限，请使用管理员账户。",
    errorConnection: "连接失败，请检查服务地址、服务状态及网络后重试。",
    errorServiceEndpoint: "请填写完整的服务地址，例如 http://192.168.1.10:7789，不要附带账号、密码或网页参数。",
    errorServiceRedirect: "服务地址发生跳转，请填写跳转后的最终地址，再测试连接。",
    serviceCredentialsRequired: "请检查此服务的 API 密钥；更换地址后，请重新填写密钥再测试。",
    errorTimeout: "请求超时，请检查网络后重试。",
    errorResponse: "服务返回了无法读取的数据，请稍后重试。",
    errorReservedSpace: "下载后预计剩余 {available} TiB，低于预留的 {reserve} TiB。请在“设置 → 常规 → 自动化与容量”核对预留空间。",
    errorArchiveName: "底包文件名无效，请选择原名未改动的官方底包。",
    errorArchiveCurrent: "这不是当前官方底包，请下载最新底包后重试。",
    errorArchiveIntegrity: "底包不完整或校验不符，请重新下载官方底包。",
    errorHttp: "请求失败（{status}），请稍后重试。",
    backgroundImagesPaused: "后台图片准备已暂停",
    backgroundImagesWaiting: "后台图片等待网络恢复",
    backgroundImagesComplete: "后台图片准备完成",
    imagesPreparedThrough: "由后向前已获取至 {month} 的图片",
    imagesPreparingBackward: "正在由后向前准备图片",
    archiveBuildSummary: "使用 Bangumi Archive {version} · 共 {count} 部作品。",
    estimatedRemaining: "预计剩余",
    estimatedTime: "预计剩余时间",
    preloadRate: "近期处理速率",
    adaptiveConcurrency: "自适应后台并发",
    budgetIdle: "资源空闲",
    budgetForeground: "前台活跃",
    budgetForegroundLatency: "前台响应变慢",
    budgetCpu: "CPU 负载较高",
    budgetIo: "图片 I/O 变慢",
    budgetNetwork: "网络失败率升高",
    currentItem: "当前对象",
    successRate: "近期成功率",
    latency: "延迟",
    throughput: "吞吐",
    lastFailure: "最近失败",
    noRecentFailure: "无近期失败",
    imagePreload: "图片预加载",
    recentSixMonths: "最近 6 个月",
    historyCatalog: "历史库",
    failedRetry: "失败重试",
    pausePreload: "暂停",
    resumePreload: "恢复",
    backgroundConcurrency: "后台并发",
    backgroundBandwidth: "后台带宽 KiB/s（0 = 不限）",
    applyLimits: "应用限制",
    preloadPaused: "后台预加载已暂停；前台图片请求不受影响。",
    preloadWaitingNetwork: "已进入本地模式；网络恢复后将自动继续后台预加载。",
    preloadWarm: "后台图片准备完成。",
    preloadRunning: "后台图片准备中",
    playbackDiagnostics: "远程播放会话",
    noPlaybackSessions: "暂无近期播放会话。",
    rangeHeader: "Range",
    resumeCount: "续传",
    upstreamState: "Ani-RSS 上游",
    transferRate: "传输速率",
    startupStarting: "正在初始化",
    startupReady: "网页已就绪",
    catalogSeasonReady: "当季作品已就绪，正在后台准备其余作品",
    catalogLocalPreparing: "收藏目录资料正在后台准备，请稍后再试；现在可浏览、订阅和播放",
    catalogFullDeferred: "当季作品可以继续使用，其余资料稍后自动重试",
    catalogHistoryPreparing: "历史作品资料正在后台准备，请稍后再查。",
    copyBrowserPermission: "复制授权命令",
    copyManually: "浏览器未能自动复制。请复制下方选中的内容（Ctrl+C；Mac 使用 ⌘C）。",
    browserPermissionHint: "在本机命令提示符（cmd）中粘贴运行一次，然后重开 Edge 或 Chrome。仅允许当前账号从本站调用 PotPlayer 和 VLC，保留已有授权。",
    startupWarming: "网页已就绪，后台准备中",
    startupWarm: "后台准备完成",
    subscriptionsTab: "订阅",
    logsTab: "日志",
    historyTab: "历史记录",
    subscriptionsHint: "此处显示本地 Torrent 的后续发布监视，新资源需确认后下载。Ani-RSS 订阅可在作品详情或新作雷达中管理。",
    noWatches: "暂无本地追更任务。",
    removeWatch: "取消追更",
    watchEpisode: "第 {sequence} 集",
    watchVolume: "第 {sequence} 卷",
    watchPending: "待确认 {count} 项",
    externalSource: "外部来源",
    readOnlyMapping: "只读映射",
    originalWithKind: "原作·{kind}",
    adaptationWithKind: "改编·{kind}",
    relatedMusic: "音乐",
    relatedOriginal: "原作",
    relatedAdaptation: "改编",
    relatedOther: "关联",
    relatedAuthor: "作者",
    relatedPublisher: "出版社",
    relatedArtist: "音乐人",
    metadataNetwork: "元数据网络与镜像",
    archiveManifestEndpoints: "Archive 清单端点（每行一个）",
    archiveAssetProxies: "Archive 文件代理模板（每行一个，使用 {url}）",
    bangumiApiEndpoints: "Bangumi API 端点（每行一个）",
    metadataNetworkHint: "按健康与延迟自动选择，失败后冷却并切换；支持系统代理。Archive 文件无论来自何处都必须通过官方 SHA-256。",
    metadataEndpointsRequired: "Archive 清单和 Bangumi API 至少各保留一个端点。",
    connectionHint:
      "联合部署会自动连接服务。带“部署设置”标记的选项在部署文件中修改，重新启动后生效。",
    deploymentManaged: "部署设置",
    endpoint: "服务地址",
    qbtApiKey: "API Key",
    qbtApiKeyPlaceholder: "留空则沿用已保存或部署凭据",
    qbtApiKeyHint: "密钥单独保存，重启后仍可用；部署配置中的密钥优先。",
    qbtCredentialConfigured: "API Key 已保存到受限凭据存储",
    torrentPoolPath: "Torrent 池路径",
    torrentPoolHint: "已有 .torrent 文件可直接放入此目录。仅使用现成种子池时，保留定时扫描，关闭自动采集即可。",
    libraryPath: "动画收藏库路径",
    librarySleepEnabled: "启用每日休眠时段",
    localLibrarySettings: "本地收藏库",
    librarySleepStart: "开始时间",
    librarySleepEnd: "结束时间",
    librarySleepHint: "按运行设备的本地时间，可跨午夜。期间暂停本地库探测、扫描、播放及写入，保留缓存状态；Ani-RSS 和其它路径的外部库继续运行。",
    librarySleeping: "本地收藏库休眠中；时段结束后自动恢复访问。",
    qbtLibraryPath: "qBittorrent 保存路径",
    testConnection: "测试连接",
    connectionOk: "连接成功",
    authRequired: "服务可达，需要认证",
    connectionFailed: "连接失败",
    externalLibraries: "外部只读媒体库",
    externalReadOnlyEnabled: "映射外部只读媒体库",
    externalReadOnlyType: "目录结构",
    genericLayout: "通用目录",
    aniRssLayout: "ani-rss 兼容目录",
    externalReadOnlyPath: "媒体库路径",
    externalScanMinutes: "只读扫描间隔（分钟）",
    externalLibraryHint:
      "只读取文件名、路径、大小和修改时间，不写入外部媒体库；ani-rss 是常见的兼容来源之一。",
    playbackConnection: "外部播放器",
    preferDirectPaths: "优先使用播放器可访问的 SMB / NFS 路径",
    playbackPublicUrl: "外部设备访问 AnimeMachine 的地址（可留空）",
    playlistTtl: "播放会话空闲失效时间（秒）",
    playlistMaximum: "播放会话最长有效时间（秒）",
    directPathMappings: "直连路径映射（每行：容器路径 => 播放器路径）",
    playbackConnectionHint: "播放器无法直读媒体路径时，自动使用临时 HTTP 播放链接。",
    subtitleSources: "字幕源",
    subtitlesEnabled: "启用归档资源字幕匹配",
    assrtEndpoints: "ASSRT API 端点（每行一个）",
    assrtToken: "ASSRT Token",
    openSubtitlesEndpoint: "OpenSubtitles API 端点",
    openSubtitlesKey: "OpenSubtitles API Key",
    subtitleSourcesHint: "从已配置的字幕服务查找，按界面语言排列候选结果，选择后应用。",
    searchSubtitles: "检索字幕", useSubtitle: "使用该字幕", subtitlePresent: "字幕已存在",
    searchSubtitlesShort: "查找", useSubtitleShort: "应用",
    subtitleEmbedded: "媒体已内嵌字幕", subtitleNotFound: "未找到可靠字幕", subtitleApplied: "字幕已保存",
    logsHint: "近期运行状态、警告和错误。",
    historyHint: "查看文件建立、移动、改名和移除记录，恢复可撤销的操作。",
    noHistory: "暂无用户内容变更记录。",
    historyMove: "移动",
    historyCreate: "建立目录", historyOther: "目录操作",
    layoutPreview: "目录调整预览", layoutPreviewHint: "核对作品命名、日语版标题与附属短篇归属。", layoutPreviewExport: "下载预览",
    layoutPreviewCurrent: "当前目录", layoutPreviewProposed: "建议目录", layoutPreviewReason: "原因", layoutPreviewName: "命名规范",
    layoutPreviewSupplement: "附属短篇", layoutPreviewOwner: "主作品目录待确认", layoutPreviewConflict: "目标已存在，需核对内容",
    layoutPreviewDone: "已检查 {examined} 个目录 · {count} 项建议", layoutPreviewUnavailable: "请连接本地库后重试。",
    torrentSearchBusy: "正在处理其它检索，请稍后重试。", torrentRequiresQbt: "请先在设置中连接 qBittorrent。",
    historyRename: "重命名",
    historyRemove: "可恢复移除",
    historyRestored: "已恢复",
    restoreHistory: "恢复",
    refresh: "刷新",
    noLogs: "暂无重要日志。",
    auditLibrary: "核验全部本地资源",
    auditStarted: "本地资源核验已在后台分批启动。",
    createPlaceholders: "建立订阅目录",
    createPlaceholdersHint: "为已订阅作品建立占位目录，沿用本地库已有目录。",
    placeholdersProgress: "已处理 {done}/{total} · 新建 {created} · 已有 {existing}",
    placeholdersResult: "新建 {created} · 已有 {existing} · 跳过 {skipped} · 失败 {failed}",
    placeholdersConnectionRequired: "请在设置中连接可读写的本地库后重试。",
    auditProgress: "媒体 {files} 个文件 · 已核验 {done}/{total} · 更新 {updated} · 跳过 {skipped}",
    maintenanceRunning: "正在后台处理…",
    maintenanceFailed: "处理失败",
    maintenanceFinished: "处理完成",
    maintenanceSkippedHint: "部分作品未处理，请查看日志。",
    automation: "自动化与容量",
    pollMinutes: "资源池扫描间隔（分钟）",
    minimumFree: "最低保留空间（TiB）",
    regionPriority: "地区",
    regionChina: "中国", regionJapan: "日本", regionKorea: "韩国", regionUsa: "美国", regionEurope: "欧洲", regionOther: "其它",
    onDemandHash: "精确核验文件（默认快速核验，开启后会在存在比较基准时校验文件哈希值）",
    otherWarning:
      "取消“其它”可能导致无法匹配未识别或新出现的资源。仍要取消吗？",
    occupied_review: "目录待核验",
    deprecated: "已弃用",
    restore_required: "需要恢复",
    upgrade_staged: "升级已暂存",
    upgrade_blocked: "升级受阻",
    not_inspected_preexisting: "本地既有文件（未逐项核验）",
    catalog_state_only: "目录存在，内容未核验",
    absentInspection: "尚无本地内容可核验",
    copyPath: "复制路径",
    copiedPath: "路径已复制",
    managed_provenance: "已按下载记录核验",
    metadata_distribution: "已完成快速核验",
    hash_mismatch: "哈希核验不一致",
    no_comparison_manifest: "已盘点文件，暂无可比较清单",
    mixed: "混合来源核验",
    noneMode: "尚无核验信息",
    not_in_library_catalog: "未加入本地库",
    preexisting_local: "本地既有内容",
    managed_submission: "由 AnimeMachine 提交",
    managed_completed: "由 AnimeMachine 下载完成",
    excluded: "已排除",
    review: "需要复核",
    approved: "已核准",
    submitting: "正在提交",
    submitted: "已提交",
    active: "运行中",
    stateOther: "状态待确认",
  },
  en: {
    pathExamples: "Path examples",
    pathWindowsHint: "Windows: use a local folder or a network share accessible to the account running AnimeMachine.",
    pathDockerHint: "Docker: use the path inside the container, as mapped in your Compose file.",
    qbtPathHint: "Use the save path on the device running qBittorrent; bundled deployments normally use /Library.",
    pathReadable: "Readable",
    pathSaveToCheck: "Save to check this path",
    pathMissing: "Folder not found. Check the path or mount.",
    pathPermissionDenied: "Read access denied. Check the service account and share permissions.",
    pathUnavailable: "Temporarily unavailable. Check the device connection or mount.",
    pathCredentialsInvalid: "Share authentication failed. Check the mount credentials.",
    pathNotChecked: "Path status is not available yet",
    radarDefaultSort: "Default order",
    catalog: "AnimeMachine",
    catalogTagline: "Automated Anime Library",
    aboutTitle: "About AnimeMachine",
    checkUpdate: "Check for updates",
    updateNow: "Update",
    forceUpdateNow: "Reinstall",
    updateChecking: "Checking for updates…",
    updateAvailable: "New version {version} is available!",
    updateLatest: "You are running the latest version.",
    updatePreparing: "Downloading and verifying the update…",
    updateRestarting: "The update is ready. AnimeMachine is restarting automatically…",
    updateFailed: "Update failed: {message}",
    updateRestartTimeout: "The service did not recover within the expected time after the update.",
    playbackHandoffUnavailable: "An external player link could not be created.",
    randomSeedUnavailable: "A new random seed could not be obtained.",
    maintenanceUnsupported: "Unsupported maintenance operation.",
    updateCheckFailed: "Update check failed: {message}",
    updateDockerRequired: "This Docker image does not yet include the Web update runtime. Update the image once with Docker Compose; later releases can be installed online.",
    updateDockerStateUnavailable: "The Docker state directory is not writable, so the online update cannot be staged safely.",
    updatePortableRequired: "Online update is enabled only for official portable Releases; source installs can still check for new versions.",
    updateUnavailable: "No compatible online-update package is available for this platform.",
    updateTab: "Updates",
    updateDetailsHint: "Check for a new version and view download progress and installation results.",
    currentVersionLabel: "Current version",
    latestVersionLabel: "Latest version",
    downloadSourceLabel: "Download source",
    sha256StatusLabel: "SHA-256 verification",
    upgradeResultLabel: "Upgrade result",
    lastCheckLabel: "Last check",
    automaticUpdateCheck: "Scheduled update checks",
    automaticUpdateEnabled: "Enable scheduled checks",
    automaticUpdateMode: "When a new version is found",
    updateNotifyOnly: "Notify only",
    updateAutoInstall: "Install automatically and restart",
    updateCheckTime: "Daily check time",
    automaticUpdateHint: "Checks once a day using the host's local time and notifies you of new versions by default. You can choose automatic installation and restart; the Release asset and SHA-256 are verified before installation.",
    updateNetworkSources: "Update routes",
    shaPending: "Awaiting download verification",
    shaVerified: "Verified",
    shaFailed: "Verification failed",
    shaUnavailable: "Unavailable",
    shaNotRequired: "Not required",
    upgradeIdle: "No upgrade attempted",
    upgradeDownloading: "Downloading",
    upgradeRestarting: "Installed; restarting",
    upgradeInstalled: "Upgrade successful",
    upgradeFailedState: "Upgrade failed",
    upgradeStatusAvailable: "New version found",
    upgradeStatusLatest: "Already up to date",
    upgradeStatusUnavailable: "New version found, but online installation is unavailable",
    upgradeStatusInstalling: "Installing automatically",
    updateSourceDirect: "Direct",
    updateSourceProxy: "Proxy",
    aboutLead: "AnimeMachine helps you browse anime, follow new releases and organize your local library automatically.",
    aboutDetail: "View releases, verify your collection and play media from a title’s details. Review download plans before adding or replacing files.",
    aboutRelations: "The relationship graph connects prequels, sequels, side stories and spin-offs so you can explore and collect a series.",
    aboutComponents: "Connect Ani-RSS, qBittorrent or an external media library as needed for subscriptions, downloads and remote playback.",
    aboutArchiveSource: "supplies the local catalog base and relationship evidence.",
    aboutBangumiSource: "fills public metadata that is missing or clearly inconsistent in the archive.",
    aboutAniRssSource: "provides optional seasonal subscriptions, state synchronization and remote playback through its HTTP API.",
    aboutSubtitleSources: "is an optional external subtitle API; subtitle rights and usage terms remain with the provider.",
    cards: "Cards",
    table: "Table",
    settings: "Settings",
    accountSettings: "Account settings", changePassword: "Change password", currentPassword: "Current password", newPassword: "New password", confirmPassword: "Confirm new password",
    changePasswordHint: "Use 10–256 characters. Other devices will need to sign in again.", passwordMismatch: "The new passwords do not match.",
    passwordChanged: "Password changed. This device stays signed in; sign in again on other devices.", currentPasswordIncorrect: "The current password is incorrect. Try again.",
    passwordLength: "Use 10–256 characters for the new password.", passwordUnchanged: "Choose a password different from your current one.", passwordRetry: "Your account changed. Sign in again and retry.",
    passwordRateLimited: "Too many attempts. Try again in five minutes.",
    manual: "Manual",
    loginTitle: "Sign in to AnimeMachine",
    login: "Sign in",
    logout: "Sign out",
    username: "Username",
    password: "Password",
    usersTab: "Users",
    usersHint: "Users can browse, play media, and submit tasks, but cannot change settings.",
    userCreateHint: "Usernames use 3–64 letters, digits, dots, underscores or hyphens; passwords require at least 10 characters.",
    userRole: "Role",
    existingUsers: "Existing users",
    initialAdmin: "Initial admin",
    currentUser: "Current user",
    disabledUser: "Disabled",
    noUsers: "No users.",
    userCreated: "User created.",
    userUpdated: "User status updated.",
    normalUser: "User",
    administrator: "Administrator",
    createUser: "Create user",
    disableUser: "Disable",
    disableInitialAdmin: "Disable the initial admin user (create a new administrator first)",
    enableUser: "Enable",
    submissionAllowed: "Allow task submissions",
    loadingDb: "Loading database…",
    backgroundSync: "Background sync",
    loadingWorks: "Loading works, please wait...",
    resetAll: "Reset all",
    custom: "Custom",
    titleAlias: "Title / alias",
    searchPlaceholder: "English, Japanese, Chinese or abbreviation",
    era: "Year",
    season: "Season",
    seasonSpring: "Spring (around Apr)", seasonSummer: "Summer (around Jul)", seasonAutumn: "Autumn (around Oct)", seasonWinter: "Winter (around Jan)",
    mediaFormat: "Format",
    seriesWork: "Series work",
    yes: "Yes",
    no: "No",
    startMonth: "From",
    endMonth: "To",
    sourceOrigin: "Source",
    sourceType: "Type",
    originalName: "Original work",
    originalAuthor: "Author",
    studio: "Studio",
    director: "Director",
    seriesComposition: "Series composition",
    characterDesign: "Character design",
    music: "Music",
    voiceActor: "Voice actor",
    personSearch: "Name",
    tag: "Theme / tag",
    all: "All",
    works: " works",
    loading: "Searching…",
    none: "No matching works.",
    failed: "Operation failed",
    queryFailed: "Query failed",
    invalidDownloadRoute: "The download route is invalid. Refresh the page and select the works again.",
    pending: "Not specified",
    directoryDate: "Directory month",
    episodes: "Episodes",
    country: "Country / region",
    titles: "Titles and aliases",
    cast: "Voice cast",
    allCast: "Show all language casts",
    relations: "Related works",
    viewRelations: "Relationship graph",
    relationGraph: "Series relationship graph",
    relationHint:
      "Arrows show work relationships. Gray dashed nodes are related works from other series.",
    graphUnavailable: "No series relationship graph is available.",
    graphSelected: "Added to download selection",
    graphUnavailableSelect: "No eligible source is currently available",
    graphExistingWarning:
      "Local content exists. The plan will add only missing files and stage verified revisions before replacement.",
    graphContext: "Related context",
    graphContextTruncated:
      "Many context relations exist; only the nearest reference nodes are shown.",
    fullscreenGraph: "Full screen",
    exitFullscreen: "Exit full screen",
    relatedWorksCount: "Works in series: {count}",
    exportPng: "Export PNG",
    exportSvg: "Export SVG",
    graphContextLine: "Cross-series relation",
    globalSearchResults: "More results from the full catalog",
    add_missing: "Add missing",
    skip_unchanged: "Skip unchanged",
    stage_replace: "Stage replacement",
    conflict_review: "Review conflict",
    previous_selection: "Already selected in task",
    not_selected: "Not selected",
    planNoDownload: "The selected works need no new or replacement files.",
    planBuilding: "Preparing a task preview in the background. You can keep browsing…",
    planWarnings: "The plan contains existing-content notices or conflicts.",
    libraryPathUnreadable: "The library target path could not be read.",
    localFileUnreadable: "The local file could not be read.",
    managedFileChanged:
      "The local file differs from its recorded provenance hash.",
    exactComparisonUnavailable: "The local file was checked. A source checksum is needed to confirm an exact match.",
    managedHashBaselineRecorded: "An initial hash baseline was recorded for the provenance-backed managed file.",
    targetIsSymbolicLink: "The target is a symbolic link or reparse file; automatic writes were blocked.",
    targetNotRegularFile: "The target is occupied by a directory or another non-regular file.",
    localFileChangedDuringHash: "The local file changed while hashing; retry after all writes finish.",
    stagedReplacement:
      "The old file is preserved; the revision stays in staging until download and verification complete.",
    sizeConflict:
      "A file at the same target has a different size and the candidate lacks sufficient revision evidence.",
    summary: "Summary",
    settingsHint: "Click Save to apply your changes.",
    acceptedContent: "Allowed release classes",
    stopped: "Stopped",
    save: "Save",
    saved: "Saved",
    savedScanning: "Saved. Scanning media folders in the background.",
    mediaScanProgress: "Scanning media · {files} files",
    availability: "Available sources",
    available: "Has an available source",
    unavailable: "No available source",
    torrentSource: "Torrent source",
    aniRssSource: "Ani-RSS source",
    downloadSources: " download sources",
    externalMediaAvailable: "External media mapped",
    noAvailableSource: "No available source",
    libraryState: "Library status",
    localLibrary: "Local library",
    externalReadOnly: "External read-only",
    submittedDownload: "Submitted download",
    notInLibrary: "Not yet in library",
    existing: "Existing · downloaded",
    localExisting: "Local media",
    managedComplete: "Download complete",
    placeholder: "No media · placeholder",
    queued: "Submitted · waiting",
    downloading: "Submitted · downloading",
    absent: "No directory",
    externalLibrary: "External media source",
    external: "External source (read-only)",
    selectedWorks: "works selected",
    clear: "Clear",
    previewPlan: "Preview download plan",
    enableSubscription: "Enable subscription",
    subscriptionPlan: "Subscription preview",
    mixedPlan: "Subscriptions and downloads",
    taskPreview: "Task preview",
    torrents: "Resources",
    reloadList: "Reload",
    collectorWorkSearch: "Search releases for a work", collectorSearchLimits: "Search limits", collectorMaxResults: "Maximum saved releases", collectorTimeout: "Time limit (seconds)", collectorPool: "Torrent pool", collectorMonitor: "Scan the pool on a schedule", collectorCollection: "Collect Torrents automatically", collectorHistory: "Include older releases", collectorHint: "Per-work search is built in. Automatic collection uses the Torrent Collector service in Compose. Admission and ranking follow your General and Release group preferences.", collectorSourcesRequired: "Select at least one Torrent search source.",
    aniRssSubscriptionResources: "Ani-RSS subscriptions", torrentResources: "Torrent downloads", exactResourceHint: "The subscription will use only the selected group and version.", resourceVariantChanged: "Release options changed. Select a current version.",
    noTorrent: "No resources found yet. Try searching again.",
    excludedSourceClass: "Source type mismatch",
    excludedSerialProfile: "Language mismatch",
    forceSubscribe: "Subscribe anyway",
    forceDownload: "Download anyway",
    forcePolicyNotice: "This selection uses resources excluded by your preferences. Your settings stay unchanged.",
    excludedResourceGroup: "Release group mismatch",
    excludedResolution: "Resolution mismatch",
    excludedSubtitle: "Subtitle mismatch",
    excludedScope: "Work scope mismatch",
    excludedManifest: "File mapping mismatch",
    excludedPolicy: "Policy mismatch",
    searchPoolNow: "Search Torrents", torrentSearchPartial: "Some sources are unavailable. Try again later.", torrentSearchFailed: "Search did not complete. Check your connection and retry.", torrentSearchProgress: "Checked {done} / {total} sources · Found {found}",
    searchAniRss: "Search via Ani-RSS",
    aniRssManaged: "Ani-RSS subscribed",
    aniRssResources: "Ani-RSS resources available",
    aniRssMode: "Resource routing mode",
    aniRssPrefer: "Prefer Ani-RSS",
    aniRssFallback: "Use Ani-RSS as fallback",
    aniRssManual: "Use Ani-RSS manually",
    aniRssApiKey: "API key",
    aniRssApiKeyHint: "Keys are stored separately and remain available after restart. Deployment credentials take precedence.",
    aniRssCredentialConfigured: "API key saved to restricted credential storage.",
    aniRssMediaPath: "Ani-RSS media path",
    aniRssSyncMinutes: "State sync interval (minutes)",
    aniRssHint: "This mode controls resource discovery and planning only; subscriptions and API media state still refresh on the sync interval. An unavailable connection disables remote resources until recovery. Media access is always read-only.",
    searchPoolRunning: "Searching the torrent pool…",
    searchPoolDone: "Search complete; available sources updated",
    searchPoolNone: "Search complete; no matching resources found",
    verifyWork: "Verify now",
    verifyRunning: "Verifying…",
    scanIdle: "Loading background image status",
    library: "Library",
    playback: "Playback",
    noPlayableMedia: "No playable main feature",
    playlistEntries: "main features",
    systemPlayer: "Download playlist",
    copyPlaylist: "Copy playlist URL",
    copiedPlaylist: "Playlist URL copied",
    openVlc: "Open in VLC",
    openPotPlayer: "Open in PotPlayer",
    openIina: "Open in IINA",
    reloadImage: "Reload",
    imageLoading: "Loading image",
    imageRetrying: "Retrying image",
    imageNoImage: "No image",
    imageError: "Image failed",
    aniRssDelete: "Delete",
    aniRssPause: "Pause subscription", aniRssResume: "Resume subscription", aniRssCancel: "Unsubscribe, keep files", aniRssDeleteFiles: "Delete subscription and files",
    aniRssCancelConfirm: "Unsubscribe from “{title}” in Ani-RSS and keep downloaded files?",
    radarInterrupted: "Search interrupted; ready to resume", librarySleepInvalid: "Quiet hours must have different start and end times.",
    aniRssDeleteConfirm: "Delete this Ani-RSS subscription and its downloaded files?",
    aniRssDeleteFailed: "Ani-RSS deletion was not confirmed",
    themeSystem: "System theme",
    themeDark: "Dark mode",
    themeLight: "Light mode",
    languageControl: "Interface language",
    closeDialog: "Close",
    sortDirectionLabel: "Sort direction",
    settingsSectionsLabel: "Settings sections",
    remotePlaybackSource: "Ani-RSS remote playback",
    playbackSource: "Playback source",
    playbackEdition: "Edition",
    playbackAutomatic: "Automatic",
    indexingLocalMedia: "Identifying local videos…",
    offlineUpdate: "Upload offline update",
    offlinePackageHint: "Choose the .anm-update.zip package for this system.",
    offlineUpdateConfirm: "Verify and install this offline update? AnimeMachine will restart and keep your settings and library.",
    offlineNotNewer: "Choose a package newer than the running version.",
    offlinePlatform: "Choose the update package for this Windows or Docker installation.",
    offlineChecksum: "The checksum does not match. Copy the complete package and try again.",
    offlineIncomplete: "The upload was incomplete. Select the file and try again.",
    updatePending: "The update is finishing. Please try again shortly.",
    updateLocalReady: "Check for updates, or upload an offline update package.",
    updateRolledBack: "The new version could not start. The previous version was restored and is ready to use.",
    offlineBaseImage: "This version needs a newer Docker base image. Import its offline image and recreate the container as described in the guide.",
    upgradeHistorical: "Current version running; a previous installation attempt did not complete",
    aniRssRemoteOnlyLibrary: "Media is provided remotely by Ani-RSS. Choose it under Playback.",
    subtitleActions: "Subtitles",
    playbackStartFile: "Starting media file",
    managed: "Managed by AnimeMachine",
    preexisting: "Pre-existing",
    collection: "Collection",
    files: "Files",
    complete: "Complete",
    nearComplete: "Nearly complete",
    partialHigh: "Mostly complete",
    partial: "Partially collected",
    incomplete: "Incomplete",
    unassessed: "Completeness not assessed",
    attachments: "Attachments",
    plan: "Download plan",
    tasks: "tasks",
    planStopped: "Jobs will be created stopped.",
    submitStopped: "Confirm submission",
    planUseAniRss: "Use Ani-RSS for all",
    planUseTorrent: "Use Torrent only",
    planRestoreDefault: "Restore default",
    torrentUnavailable: "No eligible Torrent resource",
    submitAccepted: "The job was submitted and is being processed in the background.",
    submissionDisabled: "Live submission is disabled",
    sortBy: "Sort",
    recentEpisodeSort: "New Episode Follow-up",
    catchup: "Continue watching", catchupEmpty: "No playable TV episodes to continue this season.", catchupPreparing: "Preparing playable episodes…",
    catchupCount: "{works} titles · {episodes} episodes", catchupHint: "Only subscribed TV titles with new episodes to watch are listed. Completed episodes (≥90%) are skipped by default.",
    catchupSelectHint: "Select titles, adjust episode ranges, and drag to reorder.", catchupRangeFrom: "From episode", catchupRangeTo: "Through episode", catchupRangeFromShort: "From", catchupRangeToShort: "To", catchupRangeInvalid: "Check episode ranges: choose available episodes in ascending order.", catchupSelectionOutdated: "Titles or media have changed. Close this window and reopen Continue watching.", catchupSelectRequired: "Select at least one title to watch.", catchupSelectedCount: "Selected: {works} titles · {episodes} episodes",
    catchupDrag: "Drag to reorder, or use the up and down arrow keys",
    viewingResume: "Continue: Ep {episode}", viewingNew: "Not started", viewingEdit: "Change starting episode",
    viewingEpisode: "Start watching from this episode next time", viewingClear: "Watch from the beginning", viewingUndo: "Undo", viewingComplete: "Latest episode finished",
    viewingSaved: "Updated", viewingConflict: "This record changed on another device. Reopen it to make your change.",
    potplayerInstall: "Install progress extension", potplayerHint: "PotPlayer only. Install the extension to save positions and resume. Without it, playback works and records episodes only.", viewingPosition: "Position (hours:minutes:seconds)",
    radar: "Release radar",
    radarPrevious: "Previous season", radarCurrent: "Current season", radarNext: "Next season",
    viewingUnavailable: "Viewing records are unavailable", radarUnwatched: "New episodes to watch", radarTitleColumn: "Title", radarProgress: "Latest / total episodes", radarUpdated: "Episode updated",
    radarStart: "Start date", radarWatched: "Watched episode", radarBdRelease: "BD", radarResourceRelease: "Release", radarSearch: "Search this season's releases", radarSearchProgress: "Checked {done} / {total} · Failed {failed}",
    episodeUpdated: "Ep. {episode}",
    radarSubscription: "Subscription", radarSubscribed: "Subscribed", radarPaused: "Paused",
    radarSubscribe: "Subscribe", radarChooseResource: "Choose a release",
    followWork: "Track releases", followMode: "Tracking mode", advanceSubscribe: "Subscribe when available", followArchive: "Track archive releases",
    followPreferences: "Use release groups from Settings", followCancel: "Stop tracking", followPending: "Awaiting releases", followAvailable: "Releases found",
    followSerialHint: "Check periodically and enable an Ani-RSS subscription when a release matches your preferences and selected version.",
    followArchiveHint: "Check for archive releases and notify you. Choose whether to download when a match appears.",
    followOfflineHint: "Checks and automatic subscriptions begin when Ani-RSS is connected.", followSaved: "Saved. Releases will be checked in the background.",
    followChooseGroup: "Select at least one release group.", groupVersions: "Versions", allVersions: "All", unknownVersion: "Unspecified platform",
    selectAllGroups: "Select all", clearAllGroups: "Clear all",
    followUnmatched: "Releases found; preferences not matched", followManaged: "Already managed by Ani-RSS", followEmpty: "No tracked releases. Open a title to subscribe in advance or track archive releases.",
    followOpen: "View releases",
    followChanged: "This tracking record changed. Reopen it before editing.", followRemove: "Remove record", followManagedHint: "The existing subscription keeps its Ani-RSS settings. Use the subscription menu in title details to change the group or pause it.",
    radarNoResources: "No subscription releases found yet. Try again later.", radarSubscribeHint: "Choose a release group. Ani-RSS downloads using its own settings.",
    radarEmpty: "No works in this season yet.", radarLoading: "Loading…",
    radarSubscriptionDone: "Subscription submitted; status will update after synchronization.", radarSubscriptionPending: "Awaiting sync",
    radarSearchTimeout: "Release search is still running in the background. Reopen this window later.",
    radarSync: "Sync with Ani-RSS", radarSyncShort: "Sync", radarSearchShort: "Search", radarFoldersShort: "Folders", radarRetryShort: "Retry", radarSyncing: "Syncing subscriptions and media…",
    radarChecking: "Searching…", radarComplete: "Search complete", radarRetry: "Retry unfinished titles",
    radarSubmitting: "Subscribing…", radarSubmittingHint: "Sending to Ani-RSS. You can close this window and keep browsing.",
    radarPolicyExcluded: "Releases excluded by download settings", radarWorks: "{count} titles",
    radarTaskProgress: "Search progress",
    retryAction: "Retry",
    recentEpisodeRequiresAniRss: "Connect Ani-RSS to search releases and manage subscriptions.",
    random: "Random",
    sortDate: "Date",
    sortTitle: "Title",
    sortStudio: "Studio",
    sortType: "Format",
    reshuffle: "Reshuffle",
    perPage: "Per page",
    hideWork: "Hide title", hiddenWorks: "Hidden titles", workHidden: "Hidden", restoreWork: "Show again", noHiddenWorks: "No hidden titles.",
    hiddenWorksHint: "Hiding affects this account’s lists and Continue watching. Subscriptions and downloads remain unchanged.", findRecords: "Search records", resourcePickHint: "Select a release below, then review and confirm.", resourceUnmappedHint: "Releases were saved, but their title match has not been confirmed. Downloads are not available yet.",
    resourceExcludedHint: "These releases do not match your preferences. Select an overridable release and review its warning to continue.", logInfo: "Information", logWarning: "Warning", logError: "Error", logAll: "All records", logProblems: "Warnings and errors", technicalDetails: "Technical details", historySource: "From", historyTarget: "To", logTaskStatus: "Background task status", logTaskComplete: "Background task completed", logTaskFailed: "Task did not complete",
    previous: "Previous",
    next: "Next",
    priorityDimensions: "Ranking dimensions (top to bottom)",
    priorityHint:
      "Dimensions are compared from top to bottom. Select one to expand its secondary order or decision notes.",
    resourcePriorityTab: "Resource priority",
    fixedPriorityHint: "This dimension follows a built-in decision rule and has no draggable secondary items.",
    resourceCompletenessHint: "Complete collections and complete release streams rank first. Airing releases must include every episode from episode 1 and may trail the latest known episode by at most one.",
    groupPriorityHint: "Manage archive and language-specific serial group orders in the Release groups tab.",
    groupPriority: "Release groups",
    classPriority: "Source classes",
    resolutionPriority: "Resolution",
    subtitlePriority: "Subtitles",
    catalogData: "Catalog data",
    updateArchive: "Check for catalog update",
    updateArchiveHint: "Check for a new base and merge only changed metadata.",
    importArchive: "Import downloaded catalog base",
    importArchiveHint: "Import a Bangumi Archive ZIP offline; file information is verified automatically.",
    archiveImporting: "Importing and verifying catalog base…",
    repairMetadata: "Check and complete metadata",
    repairMetadataHint: "Batch-fill missing or clearly inconsistent local metadata.",
    auditLibraryHint: "Rescan directories and reassess completeness of existing content.",
    archiveReady: "Catalog base",
    torrentAssets: "torrent assets",
    archiveChecking: "Checking for a catalog update…", archivePreparing: "Preparing the new catalog…", archiveMerging: "Applying catalog changes…",
    archiveUnchanged: "Catalog base is current",
    archiveComplete: "Catalog base updated",
    archiveFailed: "Catalog update failed",
    other: "Other",
    generalTab: "General",
    resourceGroupsTab: "Release groups",
    resourceGroupsHint: "Archive releases do not require subtitles. Serial releases follow the selected language and release groups; Japanese also permits unsubtitled releases.",
    serialSubtitleLanguage: "Serial subtitle language",
    followInterfaceLanguage: "Follow interface language",
    archiveGroups: "Collection / archive",
    archiveGroupsHint: "All are enabled by default; subtitle presence does not affect this order.",
    chineseSerialGroups: "Chinese serial releases",
    englishSerialGroups: "English serial releases",
    japaneseSerialGroups: "Japanese serial releases",
    otherResourceGroups: "Other (unrecognized or unmatched)",
    connectionsTab: "Connections",
    diagnosticsTab: "Diagnostics",
    diagnosticsHint: "View current system status, connections, and recent results.",
    systemHealth: "System health",
    systemHealthHint: "See Diagnostics for network, image and playback details.",
    healthLoading: "Summarizing runtime status…",
    healthOverallNormal: "Core system paths are healthy",
    healthOverallWarning: "Some runtime states need attention",
    healthNormal: "Normal",
    healthWarning: "Warning",
    healthNetwork: "Network",
    healthAniRss: "Ani-RSS",
    healthQbittorrent: "qBittorrent",
    healthStorage: "Storage",
    healthImagePreload: "Image preload",
    healthPlayback: "Playback path",
    healthReady: "Ready",
    healthUnknown: "No recent result",
    healthDegraded: "Recently degraded",
    healthUnavailable: "Unavailable",
    healthDisabled: "Disabled",
    healthNotConfigured: "Not configured",
    healthManual: "Manual mode",
    healthWarming: "Warming",
    healthWarm: "Warm",
    healthFailed: "Failed",
    healthSamples: "Recent samples",
    healthWarnings: "Warnings",
    healthSessions: "Sessions",
    healthMode: "Mode",
    healthState: "State",
    healthChecked: "Checks",
    startupStatus: "Startup & background preparation",
    networkDiagnostics: "Network diagnostics",
    recheckNetwork: "Recheck now",
    currentRoute: "Current request route",
    networkProfile: "Current network profile",
    performanceBaseline: "Performance baseline",
    perfCatalogReady: "Catalog ready",
    perfFirstScreen: "First screen usable",
    perfFirstCover: "First cover",
    perfWarmComplete: "Background preparation complete",
    previousRun: "Previous run",
    routeDirect: "Direct",
    routeEnvironment: "Environment proxy",
    routeWindows: "Windows system proxy",
    routeSystem: "System proxy",
    imageSourceTrend: "Short-term image source health",
    trend12h: "Last 12 hours",
    noTrendData: "No trend data",
    sourceSelected: "Selected source",
    sourceConfidence: "Sample confidence",
    sourceScore: "Composite score",
    selectionIncumbentBest: "Current source remains best",
    selectionBestQuality: "Highest composite score",
    selectionHysteresisHold: "Holding current source during switch hysteresis",
    selectionHysteresisMargin: "Candidate advantage is below the switch margin",
    selectionIncumbentCooldown: "Current source is cooling down; switched candidate",
    selectionCandidateCooldown: "Candidate is cooling down; kept current source",
    selectionMeaningfullyBetter: "Candidate advantage exceeded the switch threshold",
    catalogAvailable: "Catalog ready",
    catalogPreparing: "Preparing the catalog",
    backgroundImagesPreparing: "Preparing images in background",
    preferencesSaveFailed: "Cannot save display preferences. Allow local storage for this site.",
    catalogResolving: "Checking catalog updates",
    catalogDownloading: "Downloading catalog",
    catalogParsing: "Reading catalog data",
    catalogWriting: "Saving catalog data",
    catalogPreparingFailed: "Catalog preparation failed. Check the connection and retry.",
    errorCredentials: "Incorrect username or password. Try again.",
    errorPermission: "Administrator access is required. Sign in as an administrator.",
    errorConnection: "Connection failed. Check the service address, service status and network, then retry.",
    errorServiceEndpoint: "Enter the full service address, such as http://192.168.1.10:7789, without a username, password or query parameters.",
    errorServiceRedirect: "The service redirects to another address. Enter the final address and test again.",
    serviceCredentialsRequired: "Check this service's API key. After changing the address, enter its key again and retest.",
    errorTimeout: "Request timed out. Check the network and retry.",
    errorResponse: "The service returned unreadable data. Retry shortly.",
    errorReservedSpace: "This download would leave {available} TiB, below the {reserve} TiB reserve. Review reserved space under Settings → General → Automation & capacity.",
    errorArchiveName: "Invalid archive filename. Select an official archive with its original filename.",
    errorArchiveCurrent: "This is not the current official archive. Download the latest archive and retry.",
    errorArchiveIntegrity: "The archive is incomplete or fails verification. Download the official archive again.",
    errorHttp: "Request failed ({status}). Retry shortly.",
    backgroundImagesPaused: "Background image preparation paused",
    backgroundImagesWaiting: "Background images waiting for network recovery",
    backgroundImagesComplete: "Background image preparation complete",
    imagesPreparedThrough: "Prepared backward through works from {month}",
    imagesPreparingBackward: "Preparing works backward from newest to oldest",
    archiveBuildSummary: "Bangumi base package {version} · {count} works.",
    estimatedRemaining: "Estimated remaining",
    estimatedTime: "Estimated time remaining",
    preloadRate: "Recent processing rate",
    adaptiveConcurrency: "Adaptive background concurrency",
    budgetIdle: "Resources idle",
    budgetForeground: "Foreground active",
    budgetForegroundLatency: "Foreground response slowed",
    budgetCpu: "CPU load elevated",
    budgetIo: "Image I/O slowed",
    budgetNetwork: "Network failure rate elevated",
    currentItem: "Current item",
    successRate: "Recent success",
    latency: "Latency",
    throughput: "Throughput",
    lastFailure: "Last failure",
    noRecentFailure: "No recent failure",
    imagePreload: "Image preload",
    recentSixMonths: "Recent 6 months",
    historyCatalog: "Historical catalog",
    failedRetry: "Failed retry",
    pausePreload: "Pause",
    resumePreload: "Resume",
    backgroundConcurrency: "Background concurrency",
    backgroundBandwidth: "Background bandwidth KiB/s (0 = unlimited)",
    applyLimits: "Apply limits",
    preloadPaused: "Background preload is paused; foreground image requests are unaffected.",
    preloadWaitingNetwork: "Local mode is active; background preload will resume automatically after network recovery.",
    preloadWarm: "Background image preparation is complete.",
    preloadRunning: "Preparing images in background",
    playbackDiagnostics: "Remote playback sessions",
    noPlaybackSessions: "No recent playback sessions.",
    rangeHeader: "Range",
    resumeCount: "Resumes",
    upstreamState: "Ani-RSS upstream",
    transferRate: "Transfer rate",
    startupStarting: "Starting up",
    startupReady: "Ready to use",
    catalogSeasonReady: "This season is ready; preparing the rest in the background",
    catalogLocalPreparing: "Collection folders are being prepared. Try again shortly; browsing, subscriptions and playback are available now.",
    catalogFullDeferred: "Seasonal titles remain available; preparation will retry automatically",
    catalogHistoryPreparing: "Historical titles are still being prepared. Please try again shortly.",
    copyBrowserPermission: "Copy permission command",
    copyManually: "Your browser could not copy automatically. Copy the selected text below (Ctrl+C, or ⌘C on Mac).",
    browserPermissionHint: "Run once in Command Prompt (cmd) on this PC, then reopen Edge or Chrome. Allows this site to launch PotPlayer and VLC for your Windows account and keeps existing permissions.",
    startupWarming: "Ready to use; background preparation continues",
    startupWarm: "Background preparation complete",
    subscriptionsTab: "Subscriptions",
    logsTab: "Logs",
    historyTab: "History",
    subscriptionsHint: "This list tracks later releases for local Torrent downloads. New releases await confirmation. Manage Ani-RSS subscriptions in title details or Release radar.",
    noWatches: "No local follow-up tasks yet.",
    removeWatch: "Stop tracking",
    watchEpisode: "Episode {sequence}",
    watchVolume: "Volume {sequence}",
    watchPending: "{count} pending confirmation",
    externalSource: "External source",
    readOnlyMapping: "Read-only mapping",
    originalWithKind: "Original · {kind}",
    adaptationWithKind: "Adaptation · {kind}",
    relatedMusic: "Music",
    relatedOriginal: "Original",
    relatedAdaptation: "Adaptation",
    relatedOther: "Related",
    relatedAuthor: "Author",
    relatedPublisher: "Publisher",
    relatedArtist: "Artist",
    metadataNetwork: "Metadata network & mirrors",
    archiveManifestEndpoints: "Archive manifest endpoints (one per line)",
    archiveAssetProxies: "Archive asset proxy templates (one per line; use {url})",
    bangumiApiEndpoints: "Bangumi API endpoints (one per line)",
    metadataNetworkHint: "Chooses by health and latency, cools down failures, and honors the system proxy. Every Archive asset must pass the official SHA-256.",
    metadataEndpointsRequired: "Keep at least one Archive manifest and one Bangumi API endpoint.",
    connectionHint:
      "Bundled services connect automatically. Options marked Deployment are managed in your deployment file; restart after changing them.",
    deploymentManaged: "Deployment",
    endpoint: "Endpoint",
    qbtApiKey: "API Key",
    qbtApiKeyPlaceholder: "Leave blank to keep the saved or deployment credential",
    qbtApiKeyHint: "Keys are stored separately and remain available after restart. Deployment credentials take precedence.",
    qbtCredentialConfigured: "API Key saved to restricted credential storage",
    torrentPoolPath: "Torrent pool path",
    torrentPoolHint: "Place existing .torrent files here. To use only your own pool, keep scheduled scans enabled and turn off automatic collection.",
    libraryPath: "Anime library path",
    librarySleepEnabled: "Enable daily library quiet hours",
    localLibrarySettings: "Local library",
    librarySleepStart: "Start time",
    librarySleepEnd: "End time",
    librarySleepHint: "Uses the host's local time and can cross midnight. Local library probes, scans, playback and writes pause while cached status remains visible. Ani-RSS and external libraries at other paths continue running.",
    librarySleeping: "The local library is sleeping. Access resumes when quiet hours end.",
    qbtLibraryPath: "qBittorrent save path",
    testConnection: "Test connection",
    connectionOk: "Connected",
    authRequired: "Service reachable; authentication required",
    connectionFailed: "Connection failed",
    externalLibraries: "External read-only libraries",
    externalReadOnlyEnabled: "Map an external read-only library",
    externalReadOnlyType: "Directory layout",
    genericLayout: "Generic folders",
    aniRssLayout: "ani-rss compatible",
    externalReadOnlyPath: "Media library path",
    externalScanMinutes: "Read-only scan interval (minutes)",
    externalLibraryHint:
        "Reads names, paths, sizes and modification times without writing to the external library. ani-rss is one common compatible source.",
    playbackConnection: "External players",
    preferDirectPaths: "Prefer player-accessible SMB / NFS paths",
    playbackPublicUrl: "AnimeMachine URL reachable by external devices (optional)",
    playlistTtl: "Playback idle timeout (seconds)",
    playlistMaximum: "Maximum playback session lifetime (seconds)",
    directPathMappings: "Direct path mappings (one per line: container path => player path)",
    playbackConnectionHint: "Temporary HTTP playback links are used when the player cannot access the media path directly.",
    subtitleSources: "Subtitle sources",
    subtitlesEnabled: "Enable subtitle matching for archive releases",
    assrtEndpoints: "ASSRT API endpoints (one per line)",
    assrtToken: "ASSRT token",
    openSubtitlesEndpoint: "OpenSubtitles API endpoint",
    openSubtitlesKey: "OpenSubtitles API key",
    subtitleSourcesHint: "Search configured subtitle services, then choose subtitles to apply. Results are ordered for your interface language.",
    searchSubtitles: "Find subtitles", useSubtitle: "Use subtitle", subtitlePresent: "Subtitles available",
    searchSubtitlesShort: "Find", useSubtitleShort: "Apply",
    subtitleEmbedded: "Embedded subtitles detected", subtitleNotFound: "No reliable subtitles found", subtitleApplied: "Subtitles saved",
    logsHint: "Recent status messages, warnings and errors.",
    refresh: "Refresh",
    noLogs: "No important events yet.",
    historyHint: "Review folder creation, file moves, renames and removals, and restore reversible changes.",
    noHistory: "No user-content changes yet.",
    historyMove: "Move",
    historyCreate: "Create folder", historyOther: "Folder operation",
    layoutPreview: "Preview folder changes", layoutPreviewHint: "Review names, Japanese edition titles and companion shorts.", layoutPreviewExport: "Download preview",
    layoutPreviewCurrent: "Current folder", layoutPreviewProposed: "Proposed folder", layoutPreviewReason: "Reason", layoutPreviewName: "Naming format",
    layoutPreviewSupplement: "Companion short", layoutPreviewOwner: "Main title folder needs review", layoutPreviewConflict: "Destination exists; review its contents",
    layoutPreviewDone: "Checked {examined} folders · {count} proposals", layoutPreviewUnavailable: "Connect the local library and try again.",
    torrentSearchBusy: "Other searches are running. Please try again shortly.", torrentRequiresQbt: "Connect qBittorrent in Settings first.",
    historyRename: "Rename",
    historyRemove: "Recoverable removal",
    historyRestored: "Restored",
    restoreHistory: "Restore",
    auditLibrary: "Verify all local resources",
    auditStarted: "Local verification started in background batches.",
    createPlaceholders: "Create subscription folders",
    createPlaceholdersHint: "Create placeholder folders for subscriptions, reusing existing library folders.",
    placeholdersProgress: "Processed {done}/{total} · Created {created} · Existing {existing}",
    placeholdersResult: "Created {created} · Existing {existing} · Skipped {skipped} · Failed {failed}",
    placeholdersConnectionRequired: "Connect a writable local library in Settings, then retry.",
    auditProgress: "{files} media files · Verified {done}/{total} · Updated {updated} · Skipped {skipped}",
    maintenanceRunning: "Working in the background…",
    maintenanceFailed: "Operation failed",
    maintenanceFinished: "Completed",
    maintenanceSkippedHint: "Some works were not processed. See the logs.",
    automation: "Automation & capacity",
    pollMinutes: "Pool scan interval (minutes)",
    regionPriority: "Region",
    regionChina: "China", regionJapan: "Japan", regionKorea: "Korea", regionUsa: "United States", regionEurope: "Europe", regionOther: "Other",
    minimumFree: "Reserved free space (TiB)",
    onDemandHash: "Exact file verification (fast by default; hashes are checked when a comparison baseline exists)",
    otherWarning:
      "Disabling Other may prevent unrecognized or newly introduced releases from matching. Continue?",
    occupied_review: "Directory needs review",
    deprecated: "Deprecated",
    restore_required: "Restore required",
    upgrade_staged: "Upgrade staged",
    upgrade_blocked: "Upgrade blocked",
    not_inspected_preexisting: "Pre-existing files (not itemized)",
    catalog_state_only: "Directory exists; contents not verified",
    absentInspection: "No local content is available for verification",
    copyPath: "Copy path",
    copiedPath: "Path copied",
    managed_provenance: "Verified from download records",
    metadata_distribution: "Fast verification complete",
    hash_mismatch: "Hash mismatch",
    no_comparison_manifest: "Files inventoried; no comparison manifest",
    mixed: "Mixed-source verification",
    noneMode: "No verification details",
    not_in_library_catalog: "Not yet in the local library",
    preexisting_local: "Pre-existing local content",
    managed_submission: "Submitted by AnimeMachine",
    managed_completed: "Downloaded by AnimeMachine",
    excluded: "Excluded",
    review: "Needs review",
    approved: "Approved",
    submitting: "Submitting",
    submitted: "Submitted",
    active: "Active",
    stateOther: "Status needs review",
  },
  ja: {
    pathExamples: "パスの入力例",
    pathWindowsHint: "Windows：ローカルフォルダー、または AnimeMachine の実行アカウントからアクセスできる共有フォルダーを指定します。",
    pathDockerHint: "Docker：Compose ファイルで割り当てたコンテナー内のパスを指定します。",
    qbtPathHint: "qBittorrent を実行する機器側の保存先を指定します。一括構成では通常 /Library です。",
    pathReadable: "読み取り可能",
    pathSaveToCheck: "保存後にこのパスを確認します",
    pathMissing: "フォルダーが見つかりません。パスやマウントを確認してください。",
    pathPermissionDenied: "読み取り権限がありません。実行アカウントと共有権限を確認してください。",
    pathUnavailable: "現在アクセスできません。機器の接続やマウントを確認してください。",
    pathCredentialsInvalid: "共有の認証に失敗しました。マウントの認証情報を確認してください。",
    pathNotChecked: "パスの状態はまだ取得されていません",
    radarDefaultSort: "既定の順序",
    catalog: "AnimeMachine",
    catalogTagline: "全自動アニメライブラリ",
    aboutTitle: "AnimeMachineについて",
    checkUpdate: "更新を確認",
    updateNow: "更新",
    forceUpdateNow: "強制更新",
    updateChecking: "更新を確認しています…",
    updateAvailable: "新しいバージョン {version} を利用できます！",
    updateLatest: "現在のバージョンが最新です。",
    updatePreparing: "更新をダウンロードして検証しています…",
    updateRestarting: "更新の準備が完了しました。AnimeMachine を自動的に再起動しています…",
    updateFailed: "更新に失敗しました：{message}",
    updateRestartTimeout: "更新後、所定時間内にサービスの復帰を確認できませんでした。",
    playbackHandoffUnavailable: "外部プレーヤーの起動リンクを作成できませんでした。",
    randomSeedUnavailable: "新しいランダムシードを取得できませんでした。",
    maintenanceUnsupported: "未対応のメンテナンス操作です。",
    updateCheckFailed: "更新の確認に失敗しました：{message}",
    updateDockerRequired: "この Docker イメージには Web オンライン更新ランタイムがまだ含まれていません。Docker Compose で一度イメージを更新すると、以後はオンライン更新できます。",
    updateDockerStateUnavailable: "Docker の状態ディレクトリへ書き込めないため、オンライン更新を安全に準備できません。",
    updatePortableRequired: "オンライン更新は公式ポータブル Release のみ有効です。ソース環境でも新しいバージョンの確認はできます。",
    updateUnavailable: "このプラットフォーム向けのオンライン更新パッケージはありません。",
    updateTab: "更新を確認",
    updateDetailsHint: "新しいバージョンを確認し、ダウンロードの進捗と更新結果を表示します。",
    currentVersionLabel: "現在のバージョン",
    latestVersionLabel: "最新バージョン",
    downloadSourceLabel: "ダウンロード元",
    sha256StatusLabel: "SHA-256 検証",
    upgradeResultLabel: "更新結果",
    lastCheckLabel: "最終確認",
    automaticUpdateCheck: "定期的な更新確認",
    automaticUpdateEnabled: "定期確認を有効にする",
    automaticUpdateMode: "新しいバージョンを検出したら",
    updateNotifyOnly: "通知のみ",
    updateAutoInstall: "自動インストールして再起動",
    updateCheckTime: "毎日の確認時刻",
    automaticUpdateHint: "既定では端末のローカル時刻で1日1回確認し、新しいバージョンを通知します。自動インストールと再起動も選択でき、インストール前に Release パッケージと SHA-256 を検証します。",
    updateNetworkSources: "更新経路",
    shaPending: "ダウンロード検証待ち",
    shaVerified: "検証済み",
    shaFailed: "検証失敗",
    shaUnavailable: "利用不可",
    shaNotRequired: "検証不要",
    upgradeIdle: "更新未実行",
    upgradeDownloading: "ダウンロード中",
    upgradeRestarting: "インストール済み・再起動中",
    upgradeInstalled: "更新成功",
    upgradeFailedState: "更新失敗",
    upgradeStatusAvailable: "新しいバージョンを検出",
    upgradeStatusLatest: "最新です",
    upgradeStatusUnavailable: "新しいバージョンはありますがオンライン更新できません",
    upgradeStatusInstalling: "自動インストール中",
    updateSourceDirect: "直結",
    updateSourceProxy: "プロキシ",
    aboutLead: "AnimeMachine は、アニメの閲覧、新作の購読、ローカルライブラリの自動整理を支援します。",
    aboutDetail: "作品の詳細でリリースの確認、所蔵の検証、動画の再生ができます。追加・置換するファイルはダウンロード計画で確認します。",
    aboutRelations: "作品関連図で前作、続編、番外編、スピンオフを確認し、シリーズごとに作品を探して集められます。",
    aboutComponents: "必要に応じて Ani-RSS、qBittorrent、外部メディアライブラリを接続し、購読、ダウンロード、リモート再生を利用できます。",
    aboutArchiveSource: "ローカルカタログのベースと作品関係の根拠を提供します。",
    aboutBangumiSource: "アーカイブで欠落または明らかに不整合な公開メタデータを補完します。",
    aboutAniRssSource: "HTTP API を通じて、任意の新番購読、状態同期、リモート再生機能を提供します。",
    aboutSubtitleSources: "任意で利用する外部字幕APIです。字幕の権利と利用条件は各サービスに従います。",
    cards: "カード",
    table: "表",
    settings: "設定",
    accountSettings: "アカウント設定", changePassword: "パスワードを変更", currentPassword: "現在のパスワード", newPassword: "新しいパスワード", confirmPassword: "新しいパスワードの確認",
    changePasswordHint: "10～256 文字で設定します。変更後、ほかの端末では再ログインが必要です。", passwordMismatch: "新しいパスワードが一致しません。",
    passwordChanged: "変更しました。この端末はそのまま利用できます。ほかの端末では再ログインしてください。", currentPasswordIncorrect: "現在のパスワードを確認してください。",
    passwordLength: "新しいパスワードは 10～256 文字で入力してください。", passwordUnchanged: "現在とは異なるパスワードを設定してください。", passwordRetry: "アカウントの状態が変わりました。再ログインしてお試しください。",
    passwordRateLimited: "試行回数が多いため、5 分後にもう一度お試しください。",
    manual: "マニュアル",
    loginTitle: "AnimeMachine にログイン",
    login: "ログイン",
    logout: "ログアウト",
    username: "ユーザー名",
    password: "パスワード",
    usersTab: "ユーザー",
    usersHint: "一般ユーザーは閲覧・再生・タスクの送信ができます。設定の変更は管理者が行います。",
    userCreateHint: "ユーザー名は3～64文字の英数字・ピリオド・アンダースコア・ハイフン、パスワードは10文字以上です。",
    userRole: "ロール",
    existingUsers: "既存ユーザー",
    initialAdmin: "初期管理者",
    currentUser: "現在のユーザー",
    disabledUser: "無効",
    noUsers: "ユーザーはいません。",
    userCreated: "ユーザーを作成しました。",
    userUpdated: "ユーザー状態を更新しました。",
    normalUser: "一般ユーザー",
    administrator: "管理者",
    createUser: "ユーザー作成",
    disableUser: "無効化",
    disableInitialAdmin: "初期 admin ユーザーを無効化（先に新しい管理者ユーザーを作成してください）",
    enableUser: "有効化",
    submissionAllowed: "タスクの送信を許可",
    loadingDb: "データベースを読み込み中…",
    backgroundSync: "バックグラウンド同期",
    loadingWorks: "作品を読み込んでいます。しばらくお待ちください...",
    resetAll: "すべてリセット",
    custom: "カスタム",
    titleAlias: "タイトル / 別名",
    searchPlaceholder: "日本語・英語・中国語・略称",
    era: "年",
    season: "季",
    seasonSpring: "春（4月前後）", seasonSummer: "夏（7月前後）", seasonAutumn: "秋（10月前後）", seasonWinter: "冬（1月前後）",
    mediaFormat: "形式",
    seriesWork: "シリーズ作品",
    yes: "はい",
    no: "いいえ",
    startMonth: "開始月",
    endMonth: "終了月",
    sourceOrigin: "原作区分",
    sourceType: "タイプ",
    originalName: "原作名",
    originalAuthor: "作者",
    studio: "制作会社",
    director: "監督",
    seriesComposition: "シリーズ構成",
    characterDesign: "キャラクターデザイン",
    music: "音楽",
    voiceActor: "声優",
    personSearch: "名前を入力",
    tag: "ジャンル / タグ",
    all: "すべて",
    works: " 作品",
    loading: "検索中…",
    none: "該当作品はありません。",
    failed: "処理に失敗しました",
    queryFailed: "検索失敗",
    invalidDownloadRoute: "ダウンロード経路が無効です。ページを再読み込みして作品を選び直してください。",
    pending: "未指定",
    directoryDate: "フォルダー年月",
    episodes: "話数",
    country: "国・地域",
    titles: "タイトルと別名",
    cast: "声優",
    allCast: "全言語の声優を表示",
    relations: "関連作品",
    viewRelations: "作品関係図",
    relationGraph: "シリーズ作品関係図",
    relationHint:
      "矢印は作品関係を表します。灰色の破線枠ノードは、関連する別シリーズの作品です。",
    graphUnavailable: "表示できるシリーズ関係はありません。",
    graphSelected: "ダウンロード選択に追加しました",
    graphUnavailableSelect: "現在送信できる適格なリソースはありません",
    graphExistingWarning:
      "ローカルに内容があります。不足分のみ追加し、確認済みの更新は置換前に一時保存します。",
    graphContext: "関連参照",
    graphContextTruncated:
      "関連参照が多いため、近い参照ノードのみ表示しています。",
    fullscreenGraph: "全画面表示",
    exitFullscreen: "全画面を終了",
    relatedWorksCount: "シリーズ内作品数：{count}",
    exportPng: "PNGを書き出す",
    exportSvg: "SVGを書き出す",
    graphContextLine: "シリーズ間の関連",
    globalSearchResults: "以下は全作品からの検索結果です",
    add_missing: "不足分を追加",
    skip_unchanged: "変更なしを除外",
    stage_replace: "置換を一時保存",
    conflict_review: "競合を確認",
    previous_selection: "既存タスクで選択済み",
    not_selected: "未選択",
    planNoDownload: "選択作品に追加・置換対象ファイルはありません。",
    planBuilding: "タスクの確認画面を準備しています。そのまま閲覧を続けられます…",
    planWarnings: "既存内容の注意または競合を含む計画です。",
    libraryPathUnreadable: "ライブラリの対象パスを読み取れません。",
    localFileUnreadable: "ローカルファイルを読み取れません。",
    managedFileChanged:
      "ローカルファイルが記録済みの出所ハッシュと一致しません。",
    exactComparisonUnavailable: "ローカルファイルの検証は完了しました。完全一致の確認には配信元のチェックサムが必要です。",
    managedHashBaselineRecorded: "出所が確定した管理対象ファイルの初回ハッシュ基準を記録しました。",
    targetIsSymbolicLink: "保存先がシンボリックリンクまたは再解析ファイルのため、自動書き込みを停止しました。",
    targetNotRegularFile: "保存先がディレクトリまたは通常ファイル以外の項目で占有されています。",
    localFileChangedDuringHash: "ハッシュ計算中にローカルファイルが変化しました。書き込み完了後に再試行してください。",
    stagedReplacement:
      "旧ファイルを保持し、ダウンロードと検証が完了するまで更新版を一時保存します。",
    sizeConflict:
      "同じ対象パスの既存ファイルはサイズが異なり、候補に十分な更新根拠がありません。",
    summary: "あらすじ",
    settingsHint: "変更後に「保存」を押すと設定が反映されます。",
    acceptedContent: "許可するリリース種別",
    stopped: "停止",
    save: "保存",
    saved: "保存しました",
    savedScanning: "保存しました。メディアフォルダーを確認中です。",
    mediaScanProgress: "メディアを確認中 · {files} ファイル",
    availability: "利用可能なソース",
    available: "利用可能なソースあり",
    unavailable: "利用可能なソースなし",
    torrentSource: "Torrent ソース",
    aniRssSource: "Ani-RSS ソース",
    downloadSources: "件のダウンロードソース",
    externalMediaAvailable: "外部メディア対応済み",
    noAvailableSource: "利用可能なソースなし",
    libraryState: "ライブラリ状態",
    localLibrary: "ローカル入庫",
    externalReadOnly: "外部読み取り専用",
    submittedDownload: "ダウンロード送信済み",
    notInLibrary: "未入庫",
    existing: "既存・ダウンロード済み",
    localExisting: "ローカル既存",
    managedComplete: "ダウンロード完了",
    placeholder: "メディアなし・プレースホルダー",
    queued: "登録済み・開始待ち",
    downloading: "登録済み・ダウンロード中",
    absent: "フォルダー未作成",
    externalLibrary: "外部メディアソース",
    external: "外部ソース（読み取り専用）",
    selectedWorks: "作品を選択",
    clear: "クリア",
    previewPlan: "ダウンロード計画を確認",
    enableSubscription: "購読を有効化",
    subscriptionPlan: "購読内容の確認",
    mixedPlan: "購読とダウンロード",
    taskPreview: "タスクの確認",
    torrents: "リソース",
    reloadList: "再読み込み",
    collectorWorkSearch: "作品のリリース検索", collectorSearchLimits: "検索件数と制限時間", collectorMaxResults: "保存件数の上限", collectorTimeout: "制限時間（秒）", collectorPool: "Torrent プール", collectorMonitor: "プールを定期スキャン", collectorCollection: "Torrent を自動収集", collectorHistory: "過去のリリースも収集", collectorHint: "作品の検索は単体で利用できます。自動収集は Compose の Torrent Collector が行います。収録と並び順は「一般」「リリースグループ」の設定に従います。", collectorSourcesRequired: "Torrent の検索先を 1 つ以上選んでください。",
    aniRssSubscriptionResources: "Ani-RSS の購読", torrentResources: "Torrent のダウンロード", exactResourceHint: "選択したグループとバージョンのみで購読します。", resourceVariantChanged: "リリースの選択肢が更新されました。もう一度選んでください。",
    noTorrent: "リソースがまだ見つかりません。再検索をお試しください。",
    excludedSourceClass: "ソース種別不一致",
    excludedSerialProfile: "言語不一致",
    forceSubscribe: "このリソースで購読",
    forceDownload: "このリソースでダウンロード",
    forcePolicyNotice: "今回の選択では設定対象外のリソースを使用します。設定は変更されません。",
    excludedResourceGroup: "リリースグループ不一致",
    excludedResolution: "解像度不一致",
    excludedSubtitle: "字幕不一致",
    excludedScope: "作品範囲不一致",
    excludedManifest: "ファイル対応不一致",
    excludedPolicy: "ポリシー不一致",
    searchPoolNow: "Torrent を検索", torrentSearchPartial: "一部の検索先を利用できません。後でもう一度お試しください。", torrentSearchFailed: "検索を完了できませんでした。接続を確認して再試行してください。", torrentSearchProgress: "検索済み {done} / {total} 件 · {found} 件の候補",
    searchAniRss: "Ani-RSS でリソースを検索",
    aniRssManaged: "Ani-RSS 購読中",
    aniRssResources: "Ani-RSS リソース利用可",
    aniRssMode: "リソース利用モード",
    aniRssPrefer: "Ani-RSS を優先",
    aniRssFallback: "Ani-RSS を予備として使用",
    aniRssManual: "Ani-RSS を手動で使用",
    aniRssApiKey: "API キー",
    aniRssApiKeyHint: "キーは別途保存され、再起動後も利用できます。デプロイ設定のキーがある場合はそちらを優先します。",
    aniRssCredentialConfigured: "API キーを制限付き資格情報ストレージへ保存しました。",
    aniRssMediaPath: "Ani-RSS メディアパス",
    aniRssSyncMinutes: "状態同期間隔（分）",
    aniRssHint: "このモードはリソース検索と計画だけを制御し、購読と API メディア状態は同期間隔どおり更新します。接続できない場合はリモートリソースを停止し、復旧後に選択したモードへ戻ります。メディアは常に読み取り専用です。",
    searchPoolRunning: "Torrent プールを検索中…",
    searchPoolDone: "検索が完了し、利用可能なソースを更新しました",
    searchPoolNone: "検索が完了しました。対応するリソースは見つかりませんでした",
    verifyWork: "今すぐ確認",
    verifyRunning: "確認中…",
    scanIdle: "バックグラウンド画像状態を読み込み中",
    library: "ライブラリ",
    playback: "再生",
    noPlayableMedia: "再生可能な本編がありません",
    playlistEntries: "本編",
    systemPlayer: "プレイリストを保存",
    copyPlaylist: "プレイリストURLをコピー",
    copiedPlaylist: "プレイリストURLをコピーしました",
    openVlc: "VLCで開く",
    openPotPlayer: "PotPlayerで開く",
    openIina: "IINAで開く",
    reloadImage: "再読み込み",
    imageLoading: "画像を読み込み中",
    imageRetrying: "画像を再試行中",
    imageNoImage: "画像なし",
    imageError: "画像の読み込みに失敗",
    aniRssDelete: "削除",
    aniRssPause: "購読を一時停止", aniRssResume: "購読を再開", aniRssCancel: "購読解除（ファイルを保持）", aniRssDeleteFiles: "購読とファイルを削除",
    aniRssCancelConfirm: "Ani-RSS で「{title}」の購読を解除し、ダウンロード済みファイルを残しますか？",
    radarInterrupted: "検索を中断しました。続行できます", librarySleepInvalid: "休止の開始時刻と終了時刻を別々に設定してください。",
    aniRssDeleteConfirm: "この Ani-RSS 購読とダウンロード済みファイルを削除しますか？",
    aniRssDeleteFailed: "Ani-RSS の削除を確認できませんでした",
    themeSystem: "システム設定",
    themeDark: "ダークモード",
    themeLight: "ライトモード",
    languageControl: "表示言語",
    closeDialog: "閉じる",
    sortDirectionLabel: "並び順",
    settingsSectionsLabel: "設定セクション",
    remotePlaybackSource: "Ani-RSS リモート再生",
    playbackSource: "再生元",
    playbackEdition: "再生バージョン",
    playbackAutomatic: "自動選択",
    indexingLocalMedia: "ローカル動画を確認しています…",
    offlineUpdate: "オフライン更新をアップロード",
    offlinePackageHint: "この環境用の .anm-update.zip 更新パッケージを選択してください。",
    offlineUpdateConfirm: "このパッケージを検証してインストールしますか？完了後に自動で再起動します。設定とライブラリは引き継がれます。",
    offlineNotNewer: "現在より新しいバージョンのパッケージを選択してください。",
    offlinePlatform: "この Windows または Docker 環境に対応する更新パッケージを選択してください。",
    offlineChecksum: "チェックサムが一致しません。ファイルをコピーし直して再試行してください。",
    offlineIncomplete: "アップロードが完了していません。ファイルを選び直してください。",
    updatePending: "更新を完了しています。少し待ってから再度お試しください。",
    updateLocalReady: "更新を確認するか、オフライン更新パッケージをアップロードしてください。",
    updateRolledBack: "新版を起動できなかったため、元のバージョンに戻しました。そのまま利用できます。",
    offlineBaseImage: "このバージョンには新しい Docker ベースイメージが必要です。対応するオフラインイメージを読み込み、ガイドに従ってコンテナーを再作成してください。",
    upgradeHistorical: "現在のバージョンは稼働中です。過去のインストールは完了しませんでした。",
    aniRssRemoteOnlyLibrary: "メディアは Ani-RSS からリモート提供されます。「再生」で再生元を選択してください。",
    subtitleActions: "字幕",
    playbackStartFile: "開始メディアファイル",
    managed: "AnimeMachine管理",
    preexisting: "既存ファイル",
    collection: "コレクション",
    files: "ファイル",
    complete: "収集完了",
    nearComplete: "ほぼ完了",
    partialHigh: "大部分を収集",
    partial: "一部を収集",
    incomplete: "未完了",
    unassessed: "完全度は未評価",
    attachments: "付属品",
    plan: "ダウンロード計画",
    tasks: "タスク",
    planStopped: "タスクは停止状態で作成されます。",
    submitStopped: "送信を確定",
    planUseAniRss: "すべて Ani-RSS で購読",
    planUseTorrent: "Torrent のみ使用",
    planRestoreDefault: "既定に戻す",
    torrentUnavailable: "利用可能な Torrent リソースなし",
    submitAccepted: "タスクを登録し、バックグラウンドで処理しています。",
    submissionDisabled: "実送信は無効",
    sortBy: "並び順",
    recentEpisodeSort: "新作追従",
    catchup: "まとめて続きを再生", catchupEmpty: "このシーズンには続けて再生できる TV の話数がありません。", catchupPreparing: "再生できる話数を確認中…",
    catchupCount: "{works} 作品 · {episodes} 話", catchupHint: "登録中で新着の未視聴エピソードがある TV アニメのみ表示します。90%以上視聴した話は既定で除外します。",
    catchupSelectHint: "作品と話数範囲を選択し、ドラッグで再生順を変更できます。", catchupRangeFrom: "開始話数", catchupRangeTo: "終了話数", catchupRangeFromShort: "開始", catchupRangeToShort: "終了", catchupRangeInvalid: "話数範囲を確認してください。再生可能な話数を昇順で指定してください。", catchupSelectionOutdated: "作品または動画の状態が変わりました。画面を閉じて続き再生リストを開き直してください。", catchupSelectRequired: "視聴する作品を選択してください。", catchupSelectedCount: "選択中：{works} 作品 · {episodes} 話",
    catchupDrag: "ドラッグ、または上下の方向キーで並べ替え",
    viewingResume: "続き：第 {episode} 話", viewingNew: "未再生", viewingEdit: "再開する話数を変更",
    viewingEpisode: "次回はこの話から視聴", viewingClear: "最初から視聴", viewingUndo: "元に戻す", viewingComplete: "最新話を視聴済みにする",
    viewingSaved: "変更しました", viewingConflict: "別の端末で記録が更新されました。開き直して変更してください。",
    potplayerInstall: "再生位置を記録する拡張を導入", potplayerHint: "PotPlayer 専用です。拡張を導入すると再生位置から再開できます。未導入でも通常どおり再生でき、話数のみ記録します。", viewingPosition: "再生位置（時:分:秒）",
    radar: "新作レーダー",
    radarPrevious: "前シーズン", radarCurrent: "今シーズン", radarNext: "次シーズン",
    viewingUnavailable: "視聴記録を読み込めません", radarUnwatched: "未視聴の新しい話があります", radarTitleColumn: "タイトル", radarProgress: "最新 / 総話数", radarUpdated: "新話の更新日時",
    radarStart: "開始日", radarWatched: "視聴済み話数", radarBdRelease: "BD", radarResourceRelease: "配布", radarSearch: "今シーズンのリリースを検索", radarSearchProgress: "検索済み {done} / {total} · 失敗 {failed}",
    episodeUpdated: "第{episode}話",
    radarSubscription: "購読状態", radarSubscribed: "購読中", radarPaused: "一時停止中",
    radarSubscribe: "購読", radarChooseResource: "購読リリースを選択",
    followWork: "リリース追跡", followMode: "追跡方法", advanceSubscribe: "配信開始時に購読", followArchive: "アーカイブ向けリリースを追跡",
    followPreferences: "設定のリリースグループを使用", followCancel: "追跡を停止", followPending: "リリース待ち", followAvailable: "リリースあり",
    followSerialHint: "定期的に検索し、設定と選択したバージョンに一致するリリースが見つかると Ani-RSS の購読を開始します。",
    followArchiveHint: "アーカイブ向けリリースを定期的に検索して通知します。ダウンロードは通知後に選択できます。",
    followOfflineHint: "Ani-RSS に接続すると、検索と自動購読を開始します。", followSaved: "保存しました。バックグラウンドで定期的に確認します。",
    followChooseGroup: "リリースグループを選択してください。", groupVersions: "バージョン", allVersions: "すべて", unknownVersion: "配信元の記載なし",
    selectAllGroups: "全選択", clearAllGroups: "全解除",
    followUnmatched: "リリースあり・設定に一致せず", followManaged: "Ani-RSS で購読済み", followEmpty: "追跡中の作品はありません。作品詳細から事前購読やアーカイブ向けリリースの追跡を設定できます。",
    followOpen: "リリースを表示",
    followChanged: "追跡設定が更新されました。開き直してから変更してください。", followRemove: "記録を削除", followManagedHint: "既存の購読は Ani-RSS の設定で継続します。グループの変更や一時停止は作品詳細の購読メニューから行えます。",
    radarNoResources: "購読できるリリースがありません。後でもう一度お試しください。", radarSubscribeHint: "リリースグループを選択してください。Ani-RSS の設定に従ってダウンロードします。",
    radarEmpty: "このシーズンの作品はまだありません。", radarLoading: "読み込み中…",
    radarSubscriptionDone: "購読を送信しました。同期後に状態が更新されます。", radarSubscriptionPending: "同期待ち",
    radarSearchTimeout: "バックグラウンドで検索中です。後でこの画面を開き直してください。",
    radarSync: "Ani-RSS と同期", radarSyncShort: "同期", radarSearchShort: "検索", radarFoldersShort: "フォルダー", radarRetryShort: "再検索", radarSyncing: "購読とメディアの状態を同期中…",
    radarChecking: "検索中…", radarComplete: "検索完了", radarRetry: "未完了の作品を再検索",
    radarSubmitting: "購読を登録中…", radarSubmittingHint: "Ani-RSS に登録中です。この画面を閉じて閲覧を続けられます。",
    radarPolicyExcluded: "ダウンロード設定の対象外です", radarWorks: "{count} 作品",
    radarTaskProgress: "検索の進捗",
    retryAction: "再試行",
    recentEpisodeRequiresAniRss: "Ani-RSS を接続すると、リソースの検索と購読の管理ができます。",
    random: "ランダム",
    sortDate: "年月",
    sortTitle: "作品名",
    sortStudio: "制作会社",
    sortType: "形式",
    reshuffle: "再シャッフル",
    perPage: "表示数",
    hideWork: "作品を非表示", hiddenWorks: "非表示の作品", workHidden: "非表示", restoreWork: "再表示", noHiddenWorks: "非表示の作品はありません。",
    hiddenWorksHint: "非表示は現在のアカウントの一覧とまとめて再生に適用されます。購読とダウンロードは継続します。", findRecords: "履歴を検索", resourcePickHint: "下のリリースを選び、内容を確認して送信してください。", resourceUnmappedHint: "リリースは取得しましたが、作品との一致は未確認です。まだダウンロードを送信できません。",
    resourceExcludedHint: "設定した条件に一致しません。強制取得が可能なリリースを選び、表示される内容を確認して続行できます。", logInfo: "情報", logWarning: "警告", logError: "エラー", logAll: "すべて", logProblems: "警告とエラー", technicalDetails: "技術情報", historySource: "移動元", historyTarget: "移動先", logTaskStatus: "バックグラウンド処理の状態", logTaskComplete: "バックグラウンド処理が完了", logTaskFailed: "処理を完了できませんでした",
    previous: "前へ",
    next: "次へ",
    priorityDimensions: "優先判定軸（上から下）",
    priorityHint:
      "上から順に比較します。項目を選択すると、二次順位または判定説明を展開できます。",
    resourcePriorityTab: "リソース優先順位",
    fixedPriorityHint: "この項目は内蔵ルールで判定され、並べ替え可能な二次項目はありません。",
    resourceCompletenessHint: "全話セットや欠番のないリリースを優先します。放送中の作品は第1話から連続して揃い、確認済みの最新話からの遅れが1話以内のものを表示します。",
    groupPriorityHint: "アーカイブ用と言語別の連載グループ順位は「リリースグループ」タブで調整します。",
    groupPriority: "リリースグループ",
    classPriority: "ソース種別",
    resolutionPriority: "解像度",
    subtitlePriority: "字幕",
    catalogData: "カタログデータ",
    updateArchive: "カタログベースを確認・更新",
    updateArchiveHint: "新しいベースを確認し、変更された資料だけを統合します。",
    importArchive: "ダウンロード済みベースを取り込む",
    importArchiveHint: "Bangumi Archive ZIP をオフラインで取り込み、ファイル情報を自動検証します。",
    archiveImporting: "ベースを取り込み・検証中…",
    repairMetadata: "作品情報を検査・補完",
    repairMetadataHint: "欠落または明らかに不整合なローカル情報を一括補完します。",
    auditLibraryHint: "フォルダーを再走査し、既存コンテンツの完全性を再評価します。",
    archiveReady: "カタログベース",
    torrentAssets: "件の Torrent リソース",
    archiveChecking: "カタログの更新を確認中…", archivePreparing: "新しいカタログを準備中…", archiveMerging: "カタログの変更を反映中…",
    archiveUnchanged: "カタログベースは最新です",
    archiveComplete: "カタログベースを更新しました",
    archiveFailed: "カタログ更新失敗",
    other: "その他",
    generalTab: "一般",
    resourceGroupsTab: "リリースグループ",
    resourceGroupsHint: "アーカイブ向けは字幕の有無を問いません。連載向けは選択した言語と配信グループで絞り込み、日本語では字幕なしも対象にします。",
    serialSubtitleLanguage: "連載リリースの字幕言語",
    followInterfaceLanguage: "UI 言語に合わせる",
    archiveGroups: "収集 / アーカイブ",
    archiveGroupsHint: "初期状態ですべて有効。字幕は順位に影響しません。",
    chineseSerialGroups: "中国語連載リリース",
    englishSerialGroups: "英語連載リリース",
    japaneseSerialGroups: "日本語連載リリース",
    otherResourceGroups: "その他（未識別または条件外）",
    connectionsTab: "接続",
    diagnosticsTab: "診断",
    diagnosticsHint: "現在の動作状況、接続状態、直近の結果を確認できます。",
    systemHealth: "システム状態",
    systemHealthHint: "ネットワーク、画像、再生の詳細は「診断」で確認できます。",
    healthLoading: "実行状態を集約中…",
    healthOverallNormal: "主要なシステム経路は正常です",
    healthOverallWarning: "確認が必要な実行状態があります",
    healthNormal: "正常",
    healthWarning: "警告",
    healthNetwork: "ネットワーク",
    healthAniRss: "Ani-RSS",
    healthQbittorrent: "qBittorrent",
    healthStorage: "ストレージ",
    healthImagePreload: "画像プリロード",
    healthPlayback: "再生経路",
    healthReady: "準備完了",
    healthUnknown: "直近の結果なし",
    healthDegraded: "直近で異常",
    healthUnavailable: "利用不可",
    healthDisabled: "無効",
    healthNotConfigured: "未設定",
    healthManual: "手動モード",
    healthWarming: "準備中",
    healthWarm: "準備完了",
    healthFailed: "失敗",
    healthSamples: "直近サンプル",
    healthWarnings: "異常項目",
    healthSessions: "セッション",
    healthMode: "モード",
    healthState: "状態",
    healthChecked: "確認項目",
    startupStatus: "起動・バックグラウンド準備",
    networkDiagnostics: "ネットワーク診断",
    recheckNetwork: "再検出",
    currentRoute: "現在の通信経路",
    networkProfile: "現在のネットワーク環境",
    performanceBaseline: "性能ベースライン",
    perfCatalogReady: "作品データ準備完了",
    perfFirstScreen: "初回画面利用可能",
    perfFirstCover: "最初のカバー",
    perfWarmComplete: "バックグラウンド準備完了",
    previousRun: "前回実行",
    routeDirect: "直接接続",
    routeEnvironment: "環境変数プロキシ",
    routeWindows: "Windows システムプロキシ",
    routeSystem: "システムプロキシ",
    imageSourceTrend: "画像ソースの短期ヘルス推移",
    trend12h: "直近 12 時間",
    noTrendData: "推移データなし",
    sourceSelected: "現在の優先ソース",
    sourceConfidence: "サンプル信頼度",
    sourceScore: "総合スコア",
    selectionIncumbentBest: "現在のソースが引き続き最優先",
    selectionBestQuality: "総合スコアが最上位",
    selectionHysteresisHold: "切替ヒステリシス期間中のため現在のソースを維持",
    selectionHysteresisMargin: "候補の優位差が切替閾値未満のため現在のソースを維持",
    selectionIncumbentCooldown: "現在のソースがクールダウン中のため候補へ切替",
    selectionCandidateCooldown: "候補がクールダウン中のため現在のソースを維持",
    selectionMeaningfullyBetter: "候補の優位差が切替閾値を超過",
    catalogAvailable: "作品データ準備完了",
    catalogPreparing: "作品データを準備中",
    backgroundImagesPreparing: "バックグラウンドで画像を準備中",
    preferencesSaveFailed: "表示設定を保存できません。このサイトのローカルストレージを許可してください。",
    catalogResolving: "カタログの更新を確認中",
    catalogDownloading: "カタログをダウンロード中",
    catalogParsing: "カタログを解析中",
    catalogWriting: "カタログを保存中",
    catalogPreparingFailed: "カタログの準備に失敗しました。接続を確認して再試行してください。",
    errorCredentials: "ユーザー名またはパスワードが違います。再入力してください。",
    errorPermission: "管理者権限が必要です。管理者としてログインしてください。",
    errorConnection: "接続に失敗しました。サービスのアドレス、稼働状態、ネットワークを確認して再試行してください。",
    errorServiceEndpoint: "http://192.168.1.10:7789 のようにサービスの完全なアドレスを入力してください。ユーザー名、パスワード、クエリパラメーターは含めないでください。",
    errorServiceRedirect: "サービスが別のアドレスに転送されます。転送後のアドレスを入力し、再度テストしてください。",
    serviceCredentialsRequired: "このサービスの API キーを確認してください。アドレスを変更した場合はキーを再入力してテストしてください。",
    errorTimeout: "要求がタイムアウトしました。ネットワークを確認して再試行してください。",
    errorResponse: "サービスの応答を読み取れません。しばらくして再試行してください。",
    errorReservedSpace: "ダウンロード後の空き容量 {available} TiB は、予約容量 {reserve} TiB 未満です。「設定 → 一般 → 自動化と容量」で確認してください。",
    errorArchiveName: "アーカイブ名が無効です。元のファイル名の公式アーカイブを選択してください。",
    errorArchiveCurrent: "現在の公式アーカイブではありません。最新版をダウンロードして再試行してください。",
    errorArchiveIntegrity: "アーカイブが不完全、または検証に失敗しました。公式アーカイブを再ダウンロードしてください。",
    errorHttp: "要求に失敗しました（{status}）。しばらくして再試行してください。",
    backgroundImagesPaused: "画像のバックグラウンド準備を一時停止中",
    backgroundImagesWaiting: "画像準備はネットワーク復旧待ち",
    backgroundImagesComplete: "バックグラウンド画像準備完了",
    imagesPreparedThrough: "新しい作品から遡って {month} まで画像取得済み",
    imagesPreparingBackward: "新しい作品から古い作品へ遡って画像を準備中",
    archiveBuildSummary: "現在の Bangumi ベースパッケージ {version}・全 {count} 作品。",
    estimatedRemaining: "残り見込み",
    estimatedTime: "残り時間見込み",
    preloadRate: "直近処理速度",
    adaptiveConcurrency: "適応型バックグラウンド並行数",
    budgetIdle: "リソースに余裕あり",
    budgetForeground: "前景処理が稼働中",
    budgetForegroundLatency: "前景応答が低下",
    budgetCpu: "CPU 負荷が高い",
    budgetIo: "画像 I/O が低下",
    budgetNetwork: "ネットワーク失敗率が上昇",
    currentItem: "処理中",
    successRate: "直近成功率",
    latency: "遅延",
    throughput: "スループット",
    lastFailure: "直近の失敗",
    noRecentFailure: "直近の失敗なし",
    imagePreload: "画像プリロード",
    recentSixMonths: "直近 6 か月",
    historyCatalog: "履歴カタログ",
    failedRetry: "失敗再試行",
    pausePreload: "一時停止",
    resumePreload: "再開",
    backgroundConcurrency: "バックグラウンド並行数",
    backgroundBandwidth: "バックグラウンド帯域 KiB/s（0 = 無制限）",
    applyLimits: "制限を適用",
    preloadPaused: "バックグラウンドのプリロードを一時停止中です。前景の画像要求には影響しません。",
    preloadWaitingNetwork: "ローカルモード中です。ネットワーク復旧後、バックグラウンドのプリロードを自動再開します。",
    preloadWarm: "バックグラウンド画像準備が完了しました。",
    preloadRunning: "バックグラウンドで画像を準備中",
    playbackDiagnostics: "リモート再生セッション",
    noPlaybackSessions: "直近の再生セッションはありません。",
    rangeHeader: "Range",
    resumeCount: "再開回数",
    upstreamState: "Ani-RSS 上流",
    transferRate: "転送速度",
    startupStarting: "起動中",
    startupReady: "画面を利用できます",
    catalogSeasonReady: "今期の作品を利用できます。残りの資料をバックグラウンドで準備中です",
    catalogLocalPreparing: "保存先の資料を準備中です。しばらくしてからお試しください。検索・購読・再生は利用できます。",
    catalogFullDeferred: "今期の作品は引き続き利用できます。残りの資料は自動で再試行します",
    catalogHistoryPreparing: "過去の作品情報を準備中です。しばらくしてから再検索してください。",
    copyBrowserPermission: "許可コマンドをコピー",
    copyManually: "自動コピーできませんでした。下の選択された内容をコピーしてください（Ctrl+C、Mac は ⌘C）。",
    browserPermissionHint: "この PC のコマンドプロンプト（cmd）で一度実行し、Edge または Chrome を開き直してください。現在の Windows アカウントで、このサイトから PotPlayer と VLC を起動することを許可します。既存の許可は保持されます。",
    startupWarming: "画面を利用できます。バックグラウンドで準備中です",
    startupWarm: "バックグラウンド準備完了",
    subscriptionsTab: "購読",
    logsTab: "ログ",
    historyTab: "履歴",
    subscriptionsHint: "ここではローカル Torrent の続きの配信を確認します。新しいリリースは確認後にダウンロードします。Ani-RSS の購読は作品詳細または新作レーダーで管理できます。",
    noWatches: "ローカルの続きの配信を確認するタスクはありません。",
    removeWatch: "追跡を解除",
    watchEpisode: "第 {sequence} 話",
    watchVolume: "第 {sequence} 巻",
    watchPending: "確認待ち {count} 件",
    externalSource: "外部ソース",
    readOnlyMapping: "読み取り専用マッピング",
    originalWithKind: "原作・{kind}",
    adaptationWithKind: "派生・{kind}",
    relatedMusic: "音楽",
    relatedOriginal: "原作",
    relatedAdaptation: "派生",
    relatedOther: "関連",
    relatedAuthor: "著者",
    relatedPublisher: "出版社",
    relatedArtist: "アーティスト",
    metadataNetwork: "メタデータ通信・ミラー",
    archiveManifestEndpoints: "Archiveマニフェスト接続先（1行に1件）",
    archiveAssetProxies: "Archiveファイル用プロキシテンプレート（1行に1件、{url}を使用）",
    bangumiApiEndpoints: "Bangumi API接続先（1行に1件）",
    metadataNetworkHint: "健全性と遅延で自動選択し、失敗した接続先を冷却して切り替えます。システムプロキシに対応し、Archiveは必ず公式SHA-256で検証します。",
    metadataEndpointsRequired: "ArchiveマニフェストとBangumi APIの接続先を1件以上残してください。",
    connectionHint:
      "標準構成のサービスは自動で接続します。「デプロイ設定」の項目は導入時の設定ファイルで変更し、再起動すると反映されます。",
    deploymentManaged: "デプロイ設定",
    endpoint: "接続先",
    qbtApiKey: "API Key",
    qbtApiKeyPlaceholder: "空欄の場合は保存済み／デプロイ資格情報を使用",
    qbtApiKeyHint: "キーは別途保存され、再起動後も利用できます。デプロイ設定のキーがある場合はそちらを優先します。",
    qbtCredentialConfigured: "API Key を制限付き資格情報ストレージへ保存済み",
    torrentPoolPath: "Torrent プールのパス",
    torrentPoolHint: "手持ちの .torrent をこのフォルダーに置けます。既存のプールだけを使う場合は、定期スキャンを有効にし、自動収集を無効にします。",
    libraryPath: "アニメライブラリのパス",
    librarySleepEnabled: "毎日のライブラリ休止時間を設定",
    localLibrarySettings: "ローカルライブラリ",
    librarySleepStart: "開始時刻",
    librarySleepEnd: "終了時刻",
    librarySleepHint: "実行端末のローカル時刻を使用し、日付をまたぐ設定も可能です。休止中はローカル庫の接続確認・スキャン・再生・書き込みを止め、保存済みの状態を表示します。Ani-RSS と別のパスの外部ライブラリは引き続き動作します。",
    librarySleeping: "ローカルライブラリは休止中です。終了時刻になるとアクセスを再開します。",
    qbtLibraryPath: "qBittorrent の保存先",
    testConnection: "接続テスト",
    connectionOk: "接続成功",
    authRequired: "サービスに到達しました。認証が必要です。",
    connectionFailed: "接続失敗",
    externalLibraries: "外部読み取り専用メディアライブラリ",
    externalReadOnlyEnabled: "外部読み取り専用ライブラリを対応付ける",
    externalReadOnlyType: "ディレクトリ構成",
    genericLayout: "汎用フォルダー",
    aniRssLayout: "ani-rss 互換",
    externalReadOnlyPath: "メディアライブラリのパス",
    externalScanMinutes: "読み取り専用走査間隔（分）",
    externalLibraryHint:
        "名前・パス・サイズ・更新時刻だけを読み取り、外部ライブラリへは書き込みません。ani-rss は一般的な互換元の一つです。",
    playbackConnection: "外部プレーヤー",
    preferDirectPaths: "プレーヤーから到達できるSMB / NFSパスを優先",
    playbackPublicUrl: "外部端末から到達できるAnimeMachine URL（任意）",
    playlistTtl: "再生セッションのアイドル期限（秒）",
    playlistMaximum: "再生セッションの最長有効時間（秒）",
    directPathMappings: "直接パスの対応（1行ごと：コンテナパス => プレーヤーパス）",
    playbackConnectionHint: "プレーヤーがメディアのパスに直接アクセスできない場合は、一時的な HTTP 再生リンクを使います。",
    subtitleSources: "字幕ソース",
    subtitlesEnabled: "アーカイブ作品の字幕照合を有効にする",
    assrtEndpoints: "ASSRT APIエンドポイント（1行に1件）",
    assrtToken: "ASSRTトークン",
    openSubtitlesEndpoint: "OpenSubtitles APIエンドポイント",
    openSubtitlesKey: "OpenSubtitles APIキー",
    subtitleSourcesHint: "設定した字幕サービスから検索し、表示言語に合わせて候補を並べます。使用する字幕を選んで適用してください。",
    searchSubtitles: "字幕を検索", useSubtitle: "この字幕を使用", subtitlePresent: "字幕あり",
    searchSubtitlesShort: "検索", useSubtitleShort: "適用",
    subtitleEmbedded: "内蔵字幕を検出", subtitleNotFound: "信頼できる字幕が見つかりません", subtitleApplied: "字幕を保存しました",
    logsHint: "最近の動作状況、警告、エラーを表示します。",
    refresh: "更新",
    noLogs: "重要なログはまだありません。",
    historyHint: "フォルダーの作成、ファイルの移動・名前変更・削除を確認し、元に戻せる操作を復元します。",
    noHistory: "ユーザーコンテンツの変更履歴はありません。",
    historyMove: "移動",
    historyCreate: "フォルダー作成", historyOther: "フォルダー操作",
    layoutPreview: "フォルダー変更のプレビュー", layoutPreviewHint: "名前、日本語版タイトル、付属短編の対応を確認します。", layoutPreviewExport: "プレビューを保存",
    layoutPreviewCurrent: "現在のフォルダー", layoutPreviewProposed: "変更案", layoutPreviewReason: "理由", layoutPreviewName: "命名形式",
    layoutPreviewSupplement: "付属短編", layoutPreviewOwner: "本編のフォルダーを要確認", layoutPreviewConflict: "移動先が存在します。内容を確認してください",
    layoutPreviewDone: "{examined} フォルダーを確認 · {count} 件の変更案", layoutPreviewUnavailable: "ローカルライブラリを接続して再試行してください。",
    torrentSearchBusy: "別の検索を実行中です。しばらくしてから再試行してください。", torrentRequiresQbt: "先に設定で qBittorrent を接続してください。",
    historyRename: "名称変更",
    historyRemove: "復元可能な削除",
    historyRestored: "復元済み",
    restoreHistory: "復元",
    auditLibrary: "ローカルリソースをすべて確認",
    auditStarted: "ローカル確認をバックグラウンドで分割開始しました。",
    createPlaceholders: "購読作品のフォルダーを作成",
    createPlaceholdersHint: "購読作品の空フォルダーを作成します。既存のライブラリフォルダーはそのまま使います。",
    placeholdersProgress: "処理 {done}/{total} · 作成 {created} · 既存 {existing}",
    placeholdersResult: "作成 {created} · 既存 {existing} · スキップ {skipped} · 失敗 {failed}",
    placeholdersConnectionRequired: "設定で書き込み可能なローカルライブラリを接続してから、再度お試しください。",
    auditProgress: "メディア {files} ファイル · 確認 {done}/{total} · 更新 {updated} · スキップ {skipped}",
    maintenanceRunning: "バックグラウンドで処理中…",
    maintenanceFailed: "処理に失敗しました",
    maintenanceFinished: "完了",
    maintenanceSkippedHint: "処理できなかった作品があります。ログをご確認ください。",
    automation: "自動化・容量",
    pollMinutes: "プール走査間隔（分）",
    regionPriority: "地域",
    regionChina: "中国", regionJapan: "日本", regionKorea: "韓国", regionUsa: "米国", regionEurope: "欧州", regionOther: "その他",
    minimumFree: "最低空き容量（TiB）",
    onDemandHash: "ファイルを厳密検証（通常は高速確認、比較基準がある場合のみハッシュを照合）",
    otherWarning:
      "「その他」を無効にすると、未識別または新しいリリースを照合できない場合があります。続行しますか？",
    occupied_review: "フォルダー要確認",
    deprecated: "廃止済み",
    restore_required: "復元が必要",
    upgrade_staged: "更新を一時保存済み",
    upgrade_blocked: "更新が停止中",
    not_inspected_preexisting: "既存ファイル（個別未確認）",
    catalog_state_only: "フォルダーあり・内容未確認",
    absentInspection: "確認できるローカル内容はありません",
    copyPath: "パスをコピー",
    copiedPath: "パスをコピーしました",
    managed_provenance: "ダウンロード記録で確認済み",
    metadata_distribution: "高速確認済み",
    hash_mismatch: "ハッシュ不一致",
    no_comparison_manifest: "ファイル確認済み・比較用マニフェストなし",
    mixed: "混在ソースの確認",
    noneMode: "確認情報なし",
    not_in_library_catalog: "ローカルライブラリに未登録",
    preexisting_local: "既存のローカルコンテンツ",
    managed_submission: "AnimeMachineから送信済み",
    managed_completed: "AnimeMachineでダウンロード完了",
    excluded: "除外済み",
    review: "確認が必要",
    approved: "承認済み",
    submitting: "送信中",
    submitted: "送信済み",
    active: "実行中",
    stateOther: "状態の確認が必要",
  },
};
const values = {
  mediaFilter: {
    movie: ["剧场版", "Film", "劇場版"],
    ova: ["OVA", "OVA", "OVA"],
    web: ["网络", "Web", "Web"],
  },
  media: {
    tv: ["TV", "TV", "TV"],
    movie: ["剧场版 / 电影", "Theatrical / film", "劇場版 / 映画"],
    ova: ["OVA / OAD", "OVA / OAD", "OVA / OAD"],
    web: ["网络动画", "Web anime", "Webアニメ"],
    short: ["短片", "Short", "短編"],
    motion_comic: ["动态漫画", "Motion comic", "モーションコミック"],
    other: ["其它", "Other", "その他"],
  },
  source: {
    original: ["原创", "Original", "オリジナル"],
    manga: ["漫画改", "Manga", "漫画原作"],
    light_novel: ["轻小说改", "Light novel", "ライトノベル原作"],
    novel: ["小说改", "Novel", "小説原作"],
    game: ["游戏改", "Game", "ゲーム原作"],
    unknown: ["其它", "Other", "その他"],
    other: ["其它", "Other", "その他"],
  },
  theme: {
    action: ["动作 / 战斗", "Action", "アクション"],
    adventure: ["冒险", "Adventure", "冒険"],
    comedy: ["喜剧", "Comedy", "コメディ"],
    fantasy: ["奇幻 / 魔法", "Fantasy", "ファンタジー"],
    romance: ["恋爱", "Romance", "恋愛"],
    scifi: ["科幻", "Science fiction", "SF"],
    mystery: ["悬疑 / 推理", "Mystery", "ミステリー"],
    horror: ["恐怖 / 惊悚", "Horror / thriller", "ホラー / スリラー"],
    daily_life: ["日常", "Daily life", "日常"],
    school: ["校园", "School", "学園"],
    sports: ["运动 / 竞技", "Sports", "スポーツ"],
    music: ["音乐 / 偶像", "Music / idol", "音楽 / アイドル"],
    mecha: ["机甲 / 机器人", "Mecha / robot", "メカ / ロボット"],
    historical: ["历史 / 时代", "Historical", "歴史 / 時代"],
    workplace: ["职场 / 社会人", "Workplace", "職場 / 社会人"],
    time_travel: ["穿越", "Time travel", "タイムトラベル"],
    galgame: ["Galgame", "Galgame", "美少女ゲーム"],
    yuri: ["百合", "Yuri", "百合"],
    magical_girl: ["魔法少女", "Magical girl", "魔法少女"],
    harem: ["后宫", "Harem", "ハーレム"],
    children: ["儿童向", "Children", "子供向け"],
    avant_garde: ["非常规表达", "Unconventional expression", "非定型表現"],
    josei: ["女性向", "Josei", "女性向け"],
  },
  relatedKind: {
    anime: ["动画", "Anime", "アニメ"],
    manga: ["漫画", "Manga", "漫画"],
    light_novel: ["轻小说", "Light novel", "ライトノベル"],
    novel: ["小说", "Novel", "小説"],
    book: ["书籍", "Book", "書籍"],
    music: ["音乐", "Music", "音楽"],
    game: ["游戏", "Game", "ゲーム"],
    live_action: ["真人作品", "Live action", "実写作品"],
    other: ["其它媒介", "Other medium", "その他の媒体"],
  },
  relatedRole: {
    opening: ["OP", "OP", "OP"],
    ending: ["ED", "ED", "ED"],
    theme_collection: ["主题曲集", "Theme-song collection", "主題歌集"],
    character_song: ["角色歌", "Character song", "キャラクターソング"],
    soundtrack: ["原声", "Soundtrack", "サウンドトラック"],
    music: ["音乐", "Music", "音楽"],
  },
  country: {
    JP: ["日本", "Japan", "日本"],
    CN: ["中国", "China", "中国"],
    US: ["美国", "United States", "アメリカ"],
    GB: ["英国", "United Kingdom", "イギリス"],
    FR: ["法国", "France", "フランス"],
    KR: ["韩国", "South Korea", "韓国"],
    RU: ["俄罗斯", "Russia", "ロシア"],
    CA: ["加拿大", "Canada", "カナダ"],
    DE: ["德国", "Germany", "ドイツ"],
    other: ["其它", "Other", "その他"],
  },
};
const relationLabels = {
  prequel: ["前传", "Prequel", "前日譚"],
  sequel: ["续作", "Sequel", "続編"],
  summary: ["总集篇", "Compilation", "総集編"],
  side_story: ["番外", "Side story", "番外編"],
  alternative_version: ["不同演绎", "Alternative version", "別演出"],
  spin_off: ["衍生", "Spin-off", "スピンオフ"],
  main_story: ["主线", "Main story", "本編"],
  full_story: ["全集", "Full story", "全編"],
  adaptation: ["改编", "Adaptation", "原作・派生"],
  alternative_setting: ["不同世界观", "Alternative setting", "別世界設定"],
  same_setting: ["", "", ""],
  character_appearance: [
    "角色出演",
    "Character appearance",
    "キャラクター出演",
  ],
  collaboration: ["联动", "Collaboration", "コラボレーション"],
  other: ["其它", "Other", "その他"],
};
const relationTrackPriority = [
  "sequel",
  "prequel",
  "alternative_version",
  "side_story",
  "spin_off",
  "summary",
  "alternative_setting",
  "adaptation",
  "main_story",
  "full_story",
  "character_appearance",
  "collaboration",
  "same_setting",
  "other",
];
function relationTrackCodes(graph) {
  return [...new Set(graph.edges.map((edge) => edge.relation_code))].sort(
    (left, right) => {
      const a = relationTrackPriority.indexOf(left),
        b = relationTrackPriority.indexOf(right);
      return (a < 0 ? 999 : a) - (b < 0 ? 999 : b) ||
        left.localeCompare(right);
    },
  );
}

function relationLegendText(code) {
  if (code === "same_setting") return t("graphContextLine");
  return (relationLabels[code] || relationLabels.other)[li()];
}
const li = () => (language === "zh-Hans" ? 0 : language === "en" ? 1 : 2),
  t = (k) => i18n[language]?.[k] ?? i18n.en[k] ?? k,
  label = (g, c) =>
    values[g]?.[c]?.[li()] ?? (c === "unknown" ? t("other") : c),
  esc = (v) =>
    String(v ?? "").replace(
      /[&<>'"]/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          "'": "&#39;",
          '"': "&quot;",
        })[c],
    ),
  fmt = (v) => new Intl.NumberFormat(language).format(Number(v || 0));
const humanCode = (code) =>
  i18n[language]?.[code] ?? i18n.en?.[code] ?? t("stateOther");
const sourceClassText = (value) => ({
  bdrip: "BDRip",
  webrip: "WEBRip",
  dvdrip: "DVDRip",
  tvrip: "TVRip",
  remux: "Remux",
  bdmv: "BDMV",
  iso: "ISO",
  vhsrip: "VHSRip",
  ldrip: "LDRip",
}[String(value || "").toLowerCase()] || value || t("other"));
const stableRank = (value) => {
  let hash = 2166136261;
  for (const ch of `${seed}:${value}`)
    hash = Math.imul(hash ^ ch.charCodeAt(0), 16777619) >>> 0;
  return hash;
};
const policyLabel = (dimension, value) =>
  value === "__other__"
    ? t("other")
    : dimension === "sourceClass"
      ? sourceClassText(value)
    : dimension === "resolution" && value === "480p-576p"
      ? ["标清", "Standard definition", "標準画質"][li()]
      : value;
const priorityValueLabel = (value) => ({
  bdrip_collection: ["BDRip 合集", "BDRip collection", "BDRip コレクション"],
  bdrip_volume: ["BDRip 单卷", "BDRip volume", "BDRip 単巻"],
  webrip_collection: ["WEB 合集", "WEB collection", "WEB コレクション"],
  webrip_episode: ["WEB 单集", "WEB episode", "WEB 単話"],
  tvrip_collection: ["TV 录制合集", "TV-rip collection", "TV録画コレクション"],
  tvrip_episode: ["TV 录制单集", "TV-rip episode", "TV録画単話"],
  other: ["其它", "Other", "その他"],
  collection_revision: ["修订合集", "Revised collection", "改訂コレクション"],
  collection: ["合集", "Collection", "コレクション"],
  revision: ["修订版", "Revision", "改訂版"],
  ordinary: ["普通版本", "Ordinary release", "通常版"],
  with_attachments: ["包含有效附件", "With useful extras", "有効な付属品あり"],
  without_attachments: ["仅动画本体", "Video only", "本編のみ"],
  newest: ["较新优先", "Newer first", "新しい順"],
  oldest: ["较早优先", "Older first", "古い順"],
  larger: ["较大优先", "Larger first", "大きい順"],
  smaller: ["较小优先", "Smaller first", "小さい順"],
  "10bit": ["10 bit", "10 bit", "10 bit"],
  "8bit": ["8 bit", "8 bit", "8 bit"],
  unknown: ["其它 / 未识别", "Other / unrecognized", "その他 / 未識別"],
}[value]?.[li()] || value);
function localizedError(message, status = 0) {
  if (message === "catalog_storage_unavailable") return ["作品资料暂时无法读取，请检查运行设备的磁盘空间后重试。", "The catalog could not be read. Check the server's free disk space, then try again.", "作品情報を読み取れません。稼働端末のディスク空き容量を確認してから再試行してください。"][li()];
  if (message === "catalog_metadata_preparing") return t("catalogLocalPreparing");
  if (String(message || "").includes("local_library_sleeping")) return t("librarySleeping");
  const value = String(message || "");
  const capacity = value.match(/insufficient free space: submission would leave ([\d.]+) TiB; minimum reserve is ([\d.]+) TiB/);
  if (capacity) return t("errorReservedSpace").replace("{available}", capacity[1]).replace("{reserve}", capacity[2]);
  const mappings = [
    [/Docker base image (?:update required|is missing)/, 'offlineBaseImage'],
    [/collector_search_sources_required/, "collectorSourcesRequired"],
    [/resource_variant_changed/, "resourceVariantChanged"],
    [/torrent_search_busy/, "torrentSearchBusy"],
    [/qbittorrent_not_configured/, "torrentRequiresQbt"],
    [/local_library_unavailable/, "layoutPreviewUnavailable"],
    [/^invalid_credentials$/, "errorCredentials"],
    [/^administrator_required$|^forbidden$/, "errorPermission"],
    [/^catchup_selection_required$/, "catchupSelectRequired"],
    [/^invalid_catchup_selection$|^catchup_selection_outdated$/, "catchupSelectionOutdated"],
    [/^invalid_service_endpoint$/, "errorServiceEndpoint"],
    [/^service_redirect$/, "errorServiceRedirect"],
    [/^connection_failed$|Failed to fetch|NetworkError|Load failed/, "errorConnection"],
    [/^request_timeout$/, "errorTimeout"],
    [/^invalid_response$/, "errorResponse"],
    [/invalid Bangumi Archive filename/, "errorArchiveName"],
    [/not the current Archive/, "errorArchiveCurrent"],
    [/invalid Bangumi Archive upload size|incomplete Bangumi Archive upload|Archive (?:size|SHA-256) does not match/, "errorArchiveIntegrity"],
  ];
  const found = mappings.find(([pattern]) => pattern.test(value));
  return found ? t(found[1]) : value || t("errorHttp").replace("{status}", status);
}
const api = async (url, opt = {}) => {
    const method = String(opt.method || "GET").toUpperCase(),
      headers = new Headers(opt.headers || {}),
      timeoutMs = Math.max(0, Number(opt.timeoutMs ?? (method === "GET" ? 30000 : 0))),
      timeoutController = timeoutMs > 0 ? new AbortController() : null,
      fetchOptions = { ...opt, headers, credentials: "same-origin" };
    delete fetchOptions.timeoutMs;
    if (!(["GET", "HEAD", "OPTIONS"].includes(method)) && csrfToken)
      headers.set("X-CSRF-Token", csrfToken);
    if (timeoutController) fetchOptions.signal = opt.signal
      ? AbortSignal.any([opt.signal, timeoutController.signal]) : timeoutController.signal;
    const timeout = timeoutController
      ? setTimeout(() => timeoutController.abort(), timeoutMs)
      : null;
    let r, text;
    try {
      r = await fetch(url, fetchOptions);
      text = await r.text();
    } catch (error) {
      if (timeoutController?.signal.aborted) {
        const timeoutError = new Error(localizedError("request_timeout"));
        timeoutError.code = "request_timeout";
        throw timeoutError;
      }
      if (error.name === "AbortError") throw error;
      throw new Error(localizedError("connection_failed"));
    } finally {
      if (timeout) clearTimeout(timeout);
    }
    let b = {};
    if (text) {
      try {
        b = JSON.parse(text);
      } catch (_) {
        const responseError = new Error(
          r.ok ? localizedError("invalid_response") : localizedError("", r.status),
        );
        responseError.status = r.status;
        throw responseError;
      }
    }
    if (!r.ok) {
      if (r.status === 401 && url !== "/api/auth/login") showLogin();
      const responseError = new Error(localizedError(b.error, r.status));
      responseError.status = r.status;
      throw responseError;
    }
    if (!["GET", "HEAD", "OPTIONS"].includes(method) && !/^\/api\/(playback|viewing|diagnostics)(\/|$)/.test(url))
      invalidateCatalogPages();
    return b;
  },
  languageBase = (value) =>
    String(value || "")
      .split("-")[0]
      .toLowerCase(),
  sameAsOriginalLanguage = (x) => {
    const originalBase = languageBase(x.original_language || "ja");
    if (language === "zh-Hans" && originalBase === "zh")
      return !(x.title_zh_hans && x.title_zh_hans !== x.title_ja);
    return languageBase(language) === originalBase;
  },
  englishReadableTitle = (value) => {
    const title = String(value || "").normalize("NFKC").trim();
    return title && [...title].every((char) =>
      !/\p{Letter}/u.test(char) || /\p{Script=Latin}/u.test(char)) ? title : "";
  },
  preferred = (x) =>
    language === "zh-Hans"
      ? x.title_zh_hans || x.title_zh_hans_localized || x.title_ja
      : sameAsOriginalLanguage(x)
        ? x.title_ja
        : language === "en"
          ? englishReadableTitle(x.title_en) || englishReadableTitle(x.title_en_localized) || x.title_ja
          : x.title_ja_localized || x.title_ja,
  secondaryTitle = (x) => {
    const title = sameAsOriginalLanguage(x) ? "" : x.title_ja;
    return title && title !== preferred(x) ? title : "";
  };
function localMonth(v) {
  const m = String(v || "").match(/^(\d{4})-(\d{2})$/);
  return m
    ? new Intl.DateTimeFormat(localeForLanguage(), {
        year: "numeric",
        month: "long",
        timeZone: "UTC",
      }).format(new Date(Date.UTC(+m[1], +m[2] - 1, 1)))
    : t("pending");
}
function relationMonth(v) {
  if (language !== "en") return localMonth(v);
  const m = String(v || "").match(/^(\d{4})-(\d{2})$/);
  return m
    ? new Intl.DateTimeFormat("en-US", {
        year: "numeric",
        month: "short",
        timeZone: "UTC",
      }).format(new Date(Date.UTC(+m[1], +m[2] - 1, 1)))
    : t("pending");
}
function localeForLanguage() {
  return language === "zh-Hans" ? "zh-CN" : language === "ja" ? "ja-JP" : "en-US";
}
function localizeStaticUi() {
  document.documentElement.lang = language;
  document
    .querySelectorAll("[data-i18n]")
    .forEach((e) => (e.textContent = interpolate(t(e.dataset.i18n), JSON.parse(e.dataset.i18nValues || "{}")) + (e.dataset.i18nSuffix ? ` · ${t(e.dataset.i18nSuffix)}` : "")));
  document
    .querySelectorAll("[data-i18n-placeholder]")
    .forEach((e) => (e.placeholder = t(e.dataset.i18nPlaceholder)));
  document
    .querySelectorAll("[data-i18n-aria-label]")
    .forEach((e) => e.setAttribute("aria-label", t(e.dataset.i18nAriaLabel)));
  document
    .querySelectorAll("[data-i18n-title]")
    .forEach((e) => e.setAttribute("title", t(e.dataset.i18nTitle)));
  $("language").value = language;
  ["start_from", "start_to"].forEach((id) => $(id).lang = localeForLanguage());
}
const themeModes = ["system", "dark", "light"];
let themeMode = (() => {
  try { const value = localStorage.getItem("anm-theme"); return themeModes.includes(value) ? value : "system"; } catch (_) { return "system"; }
})();
function applyTheme() {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = themeMode === "system" ? (dark ? "dark" : "light") : themeMode;
  const button = $("themeToggle");
  if (button) {
    button.textContent = themeMode === "system" ? "◐" : themeMode === "dark" ? "☾" : "☀";
    button.title = t(themeMode === "system" ? "themeSystem" : themeMode === "dark" ? "themeDark" : "themeLight");
    button.setAttribute("aria-label", button.title);
  }
}
function applyLanguage() {
  localizeStaticUi();
  $("manualLink").href = language === "en" ? "/help/en/" : language === "ja" ? "/help/ja/" : "/help/zh-Hans/";
  applyTheme();
  document
    .querySelectorAll("option[data-group]")
    .forEach(
      (x) =>
        (x.textContent =
          x.parentElement.id === "era"
            ? eraLabel(x.dataset.code)
            : x.dataset.code === "__other__" ? t("other") : label(x.dataset.group, x.dataset.code)),
    );
  updateSeasonControl();
  updateViewToggle();
  renderMediaChecks();
  if ($("settingsDialog").open) { renderPolicy(); applyDeploymentSettings(); renderConnectionPaths(); }
  render();
  updateSelection();
  updateSortControls();
  if ($("radarDialog").open) loadRadar();
  if (catalogStats.record_count)
    $("buildInfo").textContent = archiveSummary(catalogStats);
  if (catalogStats.sync || startupState) renderScanProgress(catalogStats, startupState);
}
function updateSortControls() {
  const random = sort === "random",
    recent = sort === "recent_episode",
    option = $("sort")?.querySelector('option[value="recent_episode"]');
  if (option) {
    option.disabled = !recentEpisodeSortAvailable;
    option.title = recentEpisodeSortAvailable ? "" : t("recentEpisodeRequiresAniRss");
  }
  $("reshuffle").hidden = !random || authSession?.role !== "admin";
  $("sortDirection").hidden = random || recent;
  $("radarButton").hidden = !recent;
}

function aniRssReady(state) {
  return state?.connection_state === "ready" && Boolean(state?.credentialConfigured);
}

function applyRecentEpisodeSortAvailability() {
  recentEpisodeSortAvailable = true;
  updateSortControls();
}
function eraLabel(x) {
  if (x === "before1980")
    return language === "zh-Hans"
      ? "1970年代及以前"
      : language === "en"
        ? "1970s and earlier"
        : "1970年代以前";
  if (x === "future_or_unknown") {
    const y = new Date().getFullYear() + 1;
    return language === "zh-Hans"
      ? `${y}及日期未定`
      : language === "en"
        ? `${y} and undated`
        : `${y}年以降・未定`;
  }
  return x;
}
function fill(id, rows, g) {
  $(id).innerHTML =
    `<option value="" data-i18n="${id === "era" ? "custom" : "all"}">${t(id === "era" ? "custom" : "all")}</option>` +
    rows
      .map(
        (x) =>
          `<option value="${esc(x)}" data-group="${g || ""}" data-code="${esc(x)}">${esc(id === "era" ? eraLabel(x) : x === "__other__" ? t("other") : g ? label(g, x) : x)}</option>`,
      )
      .join("");
}
function renderMediaChecks() {
  if (!options.media_types) return;
  const cur = new Set(
      [...$("media_type").querySelectorAll("input:checked")].map(
        (x) => x.value,
      ),
    ),
    def = new Set(config.ui?.filterDefaults?.mediaTypes || ["tv", "movie"]),
    use = $("media_type").querySelector("input") ? cur : def;
  $("media_type").innerHTML = options.media_types
    .map(
      (c) =>
        `<label class="check" title="${esc(label("media", c))}"><input type="checkbox" aria-label="${esc(label("media", c))}" value="${esc(c)}" ${use.has(c) ? "checked" : ""}><span>${esc(values.mediaFilter[c]?.[li()] || label("media", c))}</span></label>`,
    )
    .join("");
  $("media_type")
    .querySelectorAll("input")
    .forEach(
      (x) =>
        (x.onchange = () => {
          page = 0;
          saveFilterState();
          search();
        }),
    );
}
function personSuggestions(id, rows) {
  $(id).innerHTML = [...rows]
    .sort((a, b) => {
      const left = typeof a === "string" ? a : a.name;
      const right = typeof b === "string" ? b : b.name;
      return stableRank(left) - stableRank(right) || left.localeCompare(right);
    })
    .map((x) => `<option value="${esc(typeof x === "string" ? x : x.name)}">`)
    .join("");
}
async function updatePeople(role, q) {
  if (q.trim().length < 2) return;
  personSuggestions(
    role === "director" ? "directorSuggestions" : "voiceSuggestions",
    await api(`/api/people?role=${role}&q=${encodeURIComponent(q)}&limit=40`),
  );
}
const filters = {
    q: $("q"),
    era: $("era"),
    start_from: $("start_from"),
    start_to: $("start_to"),
    source_type: $("source_type"),
    studio: $("studio"),
    series: $("series_member"),
    director: $("director"),
    voice_actor: $("voice_actor"),
    tag: $("tag"),
  },
  statusGroups = {
    availability: $("availability"),
    library_state: $("library_state"),
  };
function checked(box) {
  const all = [...box.querySelectorAll('input[type="checkbox"]')],
    v = all.filter((x) => x.checked).map((x) => x.value);
  return v.length ? v : ["__none__"];
}
function applyStatusDefaults() {
  const defaults = {
    availability: config.ui?.filterDefaults?.availability || ["torrent", "ani-rss", "unavailable"],
    library_state: config.ui?.filterDefaults?.libraryStates || [
      "local",
      "external",
      "submitted",
      "not_in_library",
    ],
  };
  if (defaults.availability.includes("available")) defaults.availability = ["torrent", "ani-rss", "unavailable"];
  Object.entries(statusGroups).forEach(([name, box]) => {
    const selectedValues = new Set(defaults[name]);
    box
      .querySelectorAll('input[type="checkbox"]')
      .forEach((item) => (item.checked = selectedValues.has(item.value)));
  });
}
const filterStorageKey = "anm-catalog-filters-v1";
function saveFilterState() {
  try {
    localStorage.setItem(filterStorageKey, JSON.stringify({
      fields: Object.fromEntries(Object.entries(filters).map(([key, element]) => [key, element.value])),
      season: $("season").value,
      mediaTypes: [...$("media_type").querySelectorAll("input:checked")].map((item) => item.value),
      statuses: Object.fromEntries(Object.entries(statusGroups).map(([key, box]) => [key, [...box.querySelectorAll("input:checked")].map((item) => item.value)])),
    }));
  } catch (_) {}
}
function restoreFilterState() {
  try {
    const state = JSON.parse(localStorage.getItem(filterStorageKey) || "null");
    if (!state?.fields) return false;
    Object.entries(state.fields).forEach(([key, value]) => { if (filters[key]) filters[key].value = value || ""; });
    $("season").value = String(state.season || "");
    let storedSeasonYear = exactYear(state.fields.era);
    if (storedSeasonYear === null && state.season === "winter") {
      const storedFrom = monthParts(state.fields.start_from), storedTo = monthParts(state.fields.start_to);
      if (storedFrom?.month === "12" && storedTo?.month === "02" && storedTo.year === storedFrom.year + 1)
        storedSeasonYear = storedTo.year;
    }
    if (storedSeasonYear !== null && $("season").value) {
      const range = seasonDateRange(storedSeasonYear, $("season").value);
      $("start_from").value = range.from;
      $("start_to").value = range.to;
    }
    if (Array.isArray(state.mediaTypes)) {
      const values = new Set(state.mediaTypes);
      $("media_type").querySelectorAll("input").forEach((item) => (item.checked = values.has(item.value)));
    }
    Object.entries(state.statuses || {}).forEach(([key, values]) => {
      let migratedValues = key === "availability" && (values || []).includes("available")
        ? [...(values || []).filter((value) => value !== "available"), "torrent", "ani-rss"]
        : (values || []);
      if (key === "library_state") {
        const legacyLibrary = { existing: "local", external: "external", queued: "submitted", downloading: "submitted",
          placeholder: "not_in_library", occupied_review: "not_in_library", absent: "not_in_library",
          not_in_library_catalog: "not_in_library" };
        migratedValues = [...new Set(migratedValues.map((value) => legacyLibrary[value] || value))];
      }
      const selectedValues = new Set(migratedValues);
      statusGroups[key]?.querySelectorAll("input").forEach((item) => (item.checked = selectedValues.has(item.value)));
    });
    const storedEra = String(state.fields.era || "");
    syncEraSeasonFromDates();
    const storedRange = eraDateRange(storedEra);
    if (storedRange && storedRange.reversible === false
        && !String(state.fields.start_from || "") && !String(state.fields.start_to || "")) {
      $("era").value = storedEra;
      $("season").value = "";
      updateSeasonControl();
    }
    return true;
  } catch (_) {
    return false;
  }
}
function params() {
  const p = new URLSearchParams();
  Object.entries(filters).forEach(([k, e]) => {
    if (e.value) p.set(k, e.value);
  });
  $("media_type")
    .querySelectorAll("input:checked")
    .forEach((x) => p.append("media_type", x.value));
  if (!p.has("media_type")) p.set("media_type", "__none__");
  Object.entries(statusGroups).forEach(([k, b]) =>
    checked(b).forEach((v) => p.append(k, v)),
  );
  Object.entries({
    limit: pageSize,
    offset: pageSize === "all" ? 0 : page * Number(pageSize),
    language,
    sort,
    direction,
    seed,
  }).forEach(([k, v]) => p.set(k, v));
  return p;
}
async function search({ background = false } = {}) {
  clearTimeout(catalogPrefetchTimer);
  if (background && searchController) { catalogRefreshPending = true; return; }
  if (background && topModalDialog()) {
    catalogRefreshPending = true;
    return;
  }
  searchController?.abort();
  const controller = new AbortController();
  searchController = controller;
  if (!background) results.setAttribute("aria-busy", "true");
  if (!background) { $("prevPage").disabled = $("nextPage").disabled = true; $("catalogLoading").hidden = false; }
  if (!items.length) {
    results.innerHTML = `<div class="empty">${t("loading")}</div>`;
    renderedResultsMarkup = "";
  }
  try {
    const query = params(), revision = catalogPageRevision;
    const d = await catalogPage(query, { signal: controller.signal, preferCache: !background });
    if (searchController !== controller) return;
    items = d.items;
    total = d.total;
    searchExpanded = Boolean(d.searchExpanded);
    render();
    clearTimeout(catalogPrefetchTimer);
    if (!background && pageSize !== "all" && (page + 1) * Number(pageSize) < total) {
      catalogPrefetchTimer = setTimeout(() => {
        if (topModalDialog() || catalogPageRevision !== revision) return;
        const next = new URLSearchParams(query); next.set("offset", String((page + 1) * Number(pageSize)));
        const nextKey = next.toString(), existing = catalogPages.get(nextKey);
        if (existing && existing.expires > Date.now()) return;
        catalogPage(next, { preferCache: true }).catch(() => {});
      }, 250);
    }
  } catch (e) {
    if (e.name === "AbortError") return;
    if (topModalDialog()) { catalogRefreshPending = true; return; }
    results.querySelector(".query-error")?.remove();
    results.insertAdjacentHTML("afterbegin", `<div class="query-error error" role="status">${t("queryFailed")}: ${esc(e.message)}</div>`);
    renderedResultsMarkup = "";
  } finally {
    if (searchController === controller) {
      searchController = null;
      results.removeAttribute("aria-busy");
      $("catalogLoading").hidden = true;
      $("prevPage").disabled = !page;
      $("nextPage").disabled = pageSize === "all" || (page + 1) * Number(pageSize) >= total;
    }
  }
}
function reportPerformanceEvent(event) {
  if (performanceReported.has(event)) return;
  performanceReported.add(event);
  api("/api/diagnostics/performance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event }),
  }).catch(() => {});
}

const sourceBadge = (x, compact = false) => {
    const count = Number(x.usable_torrent_count || 0);
    const aniCount = Number(x.ani_rss_available_resource_count ?? x.ani_rss_resource_count ?? 0);
    const badges = [];
    if (count) badges.push(`<span class="badge">${esc(`${t("torrentSource")} · ${fmt(count)}`)}</span>`);
    if (x.ani_rss_managed || aniCount) {
      const detail = x.ani_rss_managed ? t("aniRssManaged") : `${fmt(aniCount)} ${t("aniRssResources")}`;
        const hint = !x.ani_rss_managed && aniCount && !x.ani_rss_resource_count ? t("radarPolicyExcluded") : detail;
        badges.push(`<span class="badge ${x.ani_rss_managed ? "ani-rss-managed" : "ani-rss-resource"}" title="${esc(hint)}">${esc(detail)}</span>`);
    }
    if (!badges.length) badges.push(`<span class="badge muted-badge">${esc(t("noAvailableSource"))}</span>`);
    if (x.hidden) badges.push(`<span class="badge muted-badge">${esc(t("workHidden"))}</span>`);
    return badges.join(compact ? " " : "");
  },
  stateBadge = (x) => {
    const s = x.library_state || "not_in_library";
    const text = t({ local: "localLibrary", external: "externalReadOnly", submitted: "submittedDownload", not_in_library: "notInLibrary" }[s] || "notInLibrary");
    return `<span class="badge library ${esc(s)}">${esc(text)}</span>`;
  },
  selectable = (x) =>
    (x.usable_torrent_count > 0 || x.ani_rss_resource_count > 0 || x.ani_rss_search_available) &&
    ![
      "queued",
      "downloading",
      "occupied_review",
      "deprecated",
      "upgrade_staged",
      "upgrade_blocked",
    ].includes(x.library_internal_state),
  selector = (x) =>
    selectable(x)
      ? `<label class="pick"><input type="checkbox" data-select="${x.id}" ${selected.has(x.id) ? "checked" : ""}></label>`
      : `<label class="pick"><input type="checkbox" disabled></label>`,
  relationButton = (x) =>
    Number(x.series_member_count || 1) > 1
      ? `<button class="relation-button" data-icon="graph" data-relations="${x.id}" type="button" title="${esc(t("viewRelations"))}"><span class="relation-label">${t("viewRelations")} · ${fmt(x.series_member_count)}</span></button>`
      : "";
function episodeUpdate(x) {
  const episode = Number(x.episode_progress?.current), stamp = new Date(x.last_episode_update_at || "");
  if (!Number.isInteger(episode) || episode < 1 || !Number.isFinite(stamp.getTime())) return "";
  const pad = (value) => String(value).padStart(2, "0");
  const time = `${stamp.getFullYear()}-${pad(stamp.getMonth() + 1)}-${pad(stamp.getDate())} ${pad(stamp.getHours())}:${pad(stamp.getMinutes())}`;
  return `<p class="episode-update"><time datetime="${esc(stamp.toISOString())}">${time}</time> <span>${esc(t("episodeUpdated").replace("{episode}", String(episode)))}</span></p>`;
}
function radarStartDate(item) {
  const raw = String(item.raw_date || "").trim();
  const stamp = new Date(raw);
  return /^\d{4}-\d{2}-\d{2}$/.test(raw) && Number.isFinite(stamp.getTime()) && stamp.toISOString().slice(0, 10) === raw ? raw : localMonth(item.start_month);
}
function radarReleaseDate(item) {
  return item.radar_release_date && ["bd", "resource"].includes(item.radar_release_kind)
    ? `<small class="radar-release-date">${esc(t(item.radar_release_kind === "bd" ? "radarBdRelease" : "radarResourceRelease"))} ${esc(item.radar_release_date)}</small>` : "";
}
function completeBadge(x) {
  const c = x.completeness;
  if (!c)
    return `<span class="completion unassessed" title="${t("unassessed")}" aria-label="${t("unassessed")}">?</span>`;
  const k =
    {
      complete: "complete",
      near_complete: "nearComplete",
      partial_high: "partialHigh",
      partial: "partial",
      incomplete: "incomplete",
    }[c.state] || "unassessed";
  return `<span class="completion ${esc(c.state)}" title="${t(k)} · ${Number(c.similarity).toFixed(1)}%"></span>`;
}
function bind() {
  bindCoverReloadButtons(results);
  results.querySelectorAll("[data-id]").forEach(
    (e) =>
      (e.onclick = (ev) => {
        if (!ev.target.closest(".pick,.relation-button,.cover-reload"))
          showDetail(+e.dataset.id);
      }),
  );
  results.querySelectorAll("[data-relations]").forEach(
    (button) =>
      (button.onclick = (event) => {
        event.stopPropagation();
        showRelationGraph(+button.dataset.relations);
      }),
  );
  results.querySelectorAll("[data-select]").forEach(
    (e) =>
      (e.onchange = () => {
        const id = +e.dataset.select;
        e.checked ? selected.add(id) : selected.delete(id);
        updateSelection();
      }),
  );
}
function patchRenderedContent(current, next) {
  const markup = next.innerHTML;
  if ((renderedContents.get(current) ?? current.innerHTML) !== markup) {
    const posters = [...current.querySelectorAll("img[data-cover]")];
    current.innerHTML = markup;
    for (const poster of posters) {
      const replacement = current.querySelector(`img[data-cover="${poster.dataset.cover}"]`);
      if (replacement) replacement.replaceWith(poster); else releaseCoverElement(poster);
    }
  }
  renderedContents.set(current, markup);
}
function releaseCoverElement(target) {
  const url = target.dataset.coverObjectUrl;
  if (url) {
    URL.revokeObjectURL(url);
    coverBatch?.objectUrls.delete(url);
    delete target.dataset.coverObjectUrl;
    delete target.dataset.coverEtag;
  }
  coverObserver?.unobserve(target);
}
function reconcileKeyedChildren(root, next, key, update) {
  const existing = new Map([...root.children].filter((node) => node.hasAttribute(key))
    .map((node) => [node.getAttribute(key), node]));
  let cursor = root.firstElementChild;
  for (const candidate of [...next.children]) {
    let node = existing.get(candidate.getAttribute(key));
    if (!node || node.tagName !== candidate.tagName || !update(node, candidate)) {
      node = candidate;
      renderedContents.set(node, node.innerHTML);
    }
    if (node !== cursor) root.insertBefore(node, cursor);
    cursor = node.nextElementSibling;
  }
  while (cursor) {
    const next = cursor.nextElementSibling;
    releaseCoverElement(cursor);
    cursor.remove();
    cursor = next;
  }
}
function render() {
  if (!$("total")) return;
  if (topModalDialog()) { catalogRenderPending = true; return; }
  catalogRenderPending = false;
  items.filter((x) => !selectable(x)).forEach((x) => selected.delete(x.id));
  updateSelection();
  $("total").textContent = fmt(total);
  const n = pageSize === "all" ? total : +pageSize;
  $("pageInfo").textContent = total
    ? `${page * n + 1}–${Math.min(total, (page + 1) * n)} / ${fmt(total)}`
    : "";
  $("prevPage").disabled = !page;
  $("nextPage").disabled = pageSize === "all" || (page + 1) * n >= total;
  if (!items.length) {
    const bootstrapping = Number(catalogStats?.record_count || 0) === 0 && catalogStats?.sync?.state === "running";
    const markup = `<div class="empty">${t(catalogStats?.catalog_scope === "season" ? "catalogHistoryPreparing" : bootstrapping ? "loadingWorks" : "none")}</div>`;
    if (markup !== renderedResultsMarkup) {
      cancelCoverLoads();
      results.innerHTML = markup;
      renderedResultsMarkup = markup;
    }
    return;
  }
  const dividerBefore = (index, table = false) =>
    searchExpanded &&
    items[index]?.global_search_only &&
    (index === 0 || !items[index - 1]?.global_search_only)
      ? table
        ? `<tr class="global-search-divider"><td colspan="7"><span>${t("globalSearchResults")}</span></td></tr>`
        : `<div class="global-search-divider"><span>${t("globalSearchResults")}</span></div>`
      : "";
  let markup;
  if (view === "table")
    markup = `<div class="table-wrap"><table class="data-table"><thead><tr><th></th><th>${t("startMonth")}</th><th>${t("titleAlias")}</th><th>${t("mediaFormat")}</th><th>${t("availability")}</th><th>${t("libraryState")}</th><th>${t("studio")}</th></tr></thead><tbody>${items.map((x, index) => `${dividerBefore(index, true)}<tr data-id="${x.id}"><td>${selector(x)}${completeBadge(x)}</td><td>${esc(localMonth(x.start_month))}</td><td><b>${esc(preferred(x))}</b>${secondaryTitle(x) ? `<br><span class="muted">${esc(secondaryTitle(x))}</span>` : ""}${relationButton(x)}</td><td>${esc(label("media", x.media_code))}</td><td>${sourceBadge(x, true)}</td><td>${stateBadge(x)}</td><td>${esc((x.studios || []).join(" × "))}</td></tr>`).join("")}</tbody></table></div>`;
  else
    markup = items
      .map(
        (x, index) =>
          `${dividerBefore(index)}<article class="card ${imagesEnabled ? "with-cover" : ""}" data-id="${x.id}" ${imagesEnabled ? `data-cover="${x.id}"` : ""}><div class="card-content"><div class="badges">${sourceBadge(x)}${stateBadge(x)}${completeBadge(x)}</div><div class="card-date-row"><span class="date">${esc(localMonth(x.start_month))} · ${esc(label("media", x.media_code))}</span>${selector(x)}</div><div class="card-main"><h3>${esc(preferred(x))}</h3>${secondaryTitle(x) ? `<p class="cn">${esc(secondaryTitle(x))}</p>` : ""}<div class="chips">${String(
            x.tags || "",
          )
            .split(" / ")
            .filter(Boolean)
            .map((y) => `<span class="chip">${esc(label("theme", y))}</span>`)
            .join(
              "",
            )}</div><div class="meta">${x.studios?.length ? esc(x.studios.join(" × ")) : t("pending")}</div></div><div class="card-footer">${relationButton(x)}${episodeUpdate(x)}</div></div></article>`,
      )
      .join("");
  if (markup !== renderedResultsMarkup) {
    if (view === "cards") {
      const template = document.createElement("template");
      template.innerHTML = markup;
      reconcileKeyedChildren(results, template.content, "data-id", (current, next) => {
        if (current.classList.contains("with-cover") !== next.classList.contains("with-cover")) return false;
        patchRenderedContent(current, next);
        return true;
      });
    } else {
      cancelCoverLoads();
      results.innerHTML = markup;
    }
    renderedResultsMarkup = markup;
  }
  bind();
  covers();
  reportPerformanceEvent("firstScreen");
}
function cancelCoverLoads() {
  coverObserver?.disconnect();
  coverBatch?.controller.abort();
  if (coverBatch?.scrollHandler) window.removeEventListener("scroll", coverBatch.scrollHandler);
  for (const timer of coverBatch?.timers || []) clearTimeout(timer);
  for (const url of coverBatch?.objectUrls.keys() || []) URL.revokeObjectURL(url);
  coverBatch = null;
}
function setCoverState(target, state = "available") {
  if (!target) return;
  const labels = { loading: "imageLoading", retrying: "imageRetrying", no_image: "imageNoImage", error: "imageError", missing: "imageError" };
  target.dataset.coverState = state;
  target.dataset.coverStateLabel = labels[state] ? t(labels[state]) : "";
  target.classList.toggle("cover-unavailable", state === "no_image" || state === "error" || state === "missing");
}
async function applyCoverBlob(target, blob, batch = coverBatch) {
  if (!target || !target.isConnected) return;
  const url = URL.createObjectURL(blob);
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
  } catch (error) {
    URL.revokeObjectURL(url);
    throw error;
  }
  if (!target.isConnected || (batch && coverBatch !== batch)) {
    URL.revokeObjectURL(url);
    return;
  }
  const previous = target.dataset.coverObjectUrl;
  batch?.objectUrls?.set(url, target);
  target.dataset.coverObjectUrl = url;
  if (target.tagName === "IMG") target.src = url;
  target.style.setProperty("--cover", `url('${url}')`);
  if (previous) {
    URL.revokeObjectURL(previous);
    batch?.objectUrls?.delete(previous);
  }
}
async function reloadCoverImage(button) {
  const target = button.closest("[data-cover]");
  if (!target) return;
  const animeId = Number(target.dataset.cover || button.dataset.coverReload);
  if (!Number.isFinite(animeId)) return;
  button.disabled = true;
  try {
    await api(`/api/anime/${animeId}/image/refresh`, { method: "POST" });
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const status = await api(`/api/anime/${animeId}/image/status`);
      setCoverState(target, status.state || (status.pending ? "retrying" : "error"));
      if (!status.pending) break;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    const response = await fetch(`/api/anime/${animeId}/image?v=${Date.now()}`, {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`cover ${response.status}`);
    const status = response.headers.get("X-AnimeMachine-Image-Status") || "available";
    if (status === "available") await applyCoverBlob(target, await response.blob());
    setCoverState(target, status);
    if (status === "available") reportPerformanceEvent("firstCover");
    target.dataset.coverAttempt = "0";
  } catch (error) {
    setCoverState(target, "error");
    alert(error.message);
  } finally {
    button.disabled = false;
  }
}
function bindCoverReloadButtons(root = document) {
  root.querySelectorAll("[data-cover-reload]").forEach((button) => {
    button.onclick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      reloadCoverImage(button);
    };
  });
}
function queueCoverElement(target, batch = coverBatch, priority = false) {
  if (!target || !batch || target.dataset.coverQueued === "1") return;
  const modal = topModalDialog();
  if (modal && !modal.contains(target)) { target.dataset.coverQueued = "0"; return; }
  for (const [url, element] of batch.objectUrls) {
    if (!element.isConnected) { URL.revokeObjectURL(url); batch.objectUrls.delete(url); }
  }
  target.dataset.coverQueued = "1";
  priority ? batch.queue.unshift(target) : batch.queue.push(target);
  pumpCoverLoads(batch);
}
function pumpCoverLoads(batch) {
  // Leave browser connections available for navigation and other JSON requests.
  while (coverBatch === batch && batch.active < 4 && batch.queue.length) {
    const target = batch.queue.shift();
    if (!target.isConnected) continue;
    const modal = topModalDialog();
    if (modal && !modal.contains(target)) { target.dataset.coverQueued = "0"; continue; }
    batch.active += 1;
    if (!target.dataset.coverObjectUrl) setCoverState(target, "loading");
    const suffix = coverVersion ? `?v=${coverVersion}` : "";
    fetch(`/api/anime/${target.dataset.cover}/image${suffix}`, {
      signal: batch.controller.signal,
      credentials: "same-origin",
      cache: "no-cache",
    }).then(async (response) => {
      if (!response.ok) throw new Error(`cover ${response.status}`);
      return { blob: await response.blob(), status: response.headers.get("X-AnimeMachine-Image-Status") || "available",
        pending: response.headers.get("X-AnimeMachine-Image-Pending") === "1", etag: response.headers.get("ETag") };
    }).then(async ({ blob, status, pending, etag }) => {
      if (coverBatch !== batch || !target.isConnected) return;
      const modal = topModalDialog();
      if (modal && !modal.contains(target)) {
        target.dataset.coverQueued = "0";
        return;
      }
      // Loading polls carry the same SVG placeholder. Keep the existing paint
      // surface until a real cover has decoded, including during cache upgrades.
      if (status === "available" && (!etag || !target.dataset.coverObjectUrl || target.dataset.coverEtag !== etag)) {
        await applyCoverBlob(target, blob, batch);
        if (coverBatch !== batch || !target.isConnected) return;
        target.dataset.coverEtag = etag || "";
      }
      setCoverState(target, target.dataset.coverObjectUrl ? "available" : status);
      if (status === "available") reportPerformanceEvent("firstCover");
      if (pending || status === "loading" || status === "retrying") {
        const attempt = Number(target.dataset.coverAttempt || 0) + 1;
        target.dataset.coverAttempt = String(attempt);
        const timer = setTimeout(() => {
          batch.timers.delete(timer);
          if (coverBatch !== batch || !target.isConnected) return;
          target.dataset.coverQueued = "0";
          queueCoverElement(target, batch, true);
        }, Math.min(10000, 750 * (2 ** Math.min(attempt - 1, 4))));
        batch.timers.add(timer);
      } else {
        target.dataset.coverAttempt = "0";
      }
    }).catch((error) => {
      const modal = topModalDialog();
      if (modal && !modal.contains(target)) { target.dataset.coverQueued = "0"; return; }
      if (error.name !== "AbortError") setCoverState(target, "error");
    }).finally(() => {
      batch.active -= 1;
      if (coverBatch === batch) pumpCoverLoads(batch);
    });
  }
}
function predictiveCoverWindow(batch, direction = "down") {
  if (coverBatch !== batch || topModalDialog()) return;
  const viewport = Math.max(320, window.innerHeight || document.documentElement.clientHeight || 800);
  const bounds = direction === "up"
    ? { min: -1.05 * viewport, max: 1.15 * viewport }
    : { min: -0.15 * viewport, max: 2.05 * viewport };
  batch.queue = batch.queue.filter((target) => {
    if (!target.isConnected) return false;
    const rect = target.getBoundingClientRect(), keep = rect.bottom >= bounds.min && rect.top <= bounds.max;
    if (!keep) target.dataset.coverQueued = "0";
    return keep;
  });
  const slots = Math.max(0, 18 - batch.active - batch.queue.length);
  if (!slots) return;
  const candidates = [...results.querySelectorAll("[data-cover]")].filter((target) => {
    if (target.dataset.coverQueued === "1") return false;
    const rect = target.getBoundingClientRect();
    return rect.bottom >= bounds.min && rect.top <= bounds.max;
  });
  candidates.sort((left, right) => {
    const a = left.getBoundingClientRect(), b = right.getBoundingClientRect();
    return direction === "up" ? b.bottom - a.bottom : a.top - b.top;
  });
  candidates.slice(0, slots).forEach((target) => {
    const rect = target.getBoundingClientRect(), visible = rect.bottom > 0 && rect.top < viewport;
    queueCoverElement(target, batch, visible);
    coverObserver?.unobserve(target);
  });
}
function covers() {
  if (!imagesEnabled) { cancelCoverLoads(); return; }
  if (coverBatch) {
    results.querySelectorAll("[data-cover]").forEach((target) => {
      if (target.dataset.coverQueued !== "1") coverObserver?.observe(target);
    });
    requestAnimationFrame(() => predictiveCoverWindow(coverBatch, "down"));
    return;
  }
  const batch = { controller: new AbortController(), queue: [], active: 0, objectUrls: new Map(), timers: new Set(), scrollHandler: null };
  coverBatch = batch;
  coverObserver = new IntersectionObserver(
    (es) =>
      es.forEach((e) => {
        if (e.isIntersecting) {
          queueCoverElement(e.target, batch, true);
          coverObserver.unobserve(e.target);
        }
      }),
    { rootMargin: "240px 0px 240px 0px" },
  );
  [...results.querySelectorAll("[data-cover]")].forEach((target) => coverObserver.observe(target));
  let lastY = window.scrollY, scheduled = false;
  batch.scrollHandler = () => {
    if (scheduled || coverBatch !== batch) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      const currentY = window.scrollY, direction = currentY < lastY ? "up" : "down";
      lastY = currentY;
      predictiveCoverWindow(batch, direction);
    });
  };
  window.addEventListener("scroll", batch.scrollHandler, { passive: true });
  requestAnimationFrame(() => predictiveCoverWindow(batch, "down"));
}
const suffix = (c) =>
    `(${({ zh: ["中", "CN", "中"], en: ["英", "EN", "英"], ja: ["日", "JA", "日"] }[String(c || "").split("-")[0]] || [c, c, c])[li()]})`,
  inspectionLabel = (mode) =>
    t(
      {
        none: "noneMode",
        not_inspected_preexisting: "not_inspected_preexisting",
        catalog_state_only: "catalog_state_only",
        managed_provenance: "managed_provenance",
        metadata_distribution: "metadata_distribution",
        hash_mismatch: "hash_mismatch",
        no_comparison_manifest: "no_comparison_manifest",
        mixed: "mixed",
      }[mode] || "noneMode",
    ),
  libraryHtml = (x, animeId) => {
    const targets = uniqueLibraryTargets(x.targets || []);
    return targets.length
      ? targets.map((y) => {
          const inspection = y.state === "absent" ? t("absentInspection") : inspectionLabel(y.inspectionMode),
            status = y.state === "external"
              ? `${t("externalSource")} · ${t("readOnlyMapping")}`
              : `${esc(humanCode(y.state))} · ${inspection}`,
            mappedPath = clientVisiblePath(y.path),
            count = y.fileCount != null
              ? ` · ${y.fileCount} ${t("files")}`
              : Number(y.expectedFiles) > 0 ? ` · ${y.observedFiles}/${y.expectedFiles} ${t("files")}`
                : y.observedFiles != null ? ` · ${y.observedFiles} ${t("files")}` : "",
            needsAudit = y.state === "existing" && ["not_inspected_preexisting", "catalog_state_only", "none"].includes(y.inspectionMode);
          return `<div class="library-resource"><div class="inventory"><span><b>${esc(mappedPath || y.path)}</b><div class="inventory-foot"><small>${status}${count}${needsAudit ? ` <button type="button" class="text-button audit-work" data-icon="check" data-anime-id="${animeId}">${t("verifyWork")}</button>` : ""}</small><button type="button" class="text-button copy-library-path" data-icon="copy" data-copy-path="${esc(mappedPath || "")}" ${mappedPath ? "" : "disabled"}>${t("copyPath")}</button></div></span></div></div>`;
        }).join("")
      : "";
  },
  subtitleToolsHtml = (target) => target
    ? `<div class="subtitle-tools detail-subtitle-tools" data-subtitle-target="${esc(target.path)}"><b>${t("subtitleActions")}</b><button type="button" class="tool dark-tool search-subtitles" data-icon="search" title="${esc(t("searchSubtitles"))}" aria-label="${esc(t("searchSubtitles"))}">${t("searchSubtitlesShort")}</button><select class="subtitle-candidates" disabled><option>${t("subtitleNotFound")}</option></select><button type="button" class="tool dark-tool apply-subtitle" data-icon="check" title="${esc(t("useSubtitle"))}" aria-label="${esc(t("useSubtitle"))}" disabled>${t("useSubtitleShort")}</button><small class="muted subtitle-state"></small></div>`
    : "",
  playbackSourceHtml = (targets, subscriptions, selected) => {
    const choices = [
      ...uniqueLibraryTargets(targets).map((item) => ({ value: item.path, label: clientVisiblePath(item.path) || item.path })),
      ...subscriptions.filter((item) => Number(item.playableCount || 0) > 0).map((item) => ({
        value: `ani-rss:${item.remoteId}`,
        label: `${t("remotePlaybackSource")} · ${item.title}`,
      })),
    ];
    if (!choices.length) return "";
    return `<label class="playback-source-picker"><span>${t("playbackSource")}</span><select id="playbackSource">${choices.map((item) => `<option value="${esc(item.value)}" ${item.value === selected ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select></label>`;
  },
  sequenceText = (values, prefix) => {
    const numbers = [...new Set((values || []).map(Number).filter(Number.isFinite))].sort((a, b) => a - b);
    if (!numbers.length) return "";
    const ranges = [];
    for (const number of numbers) {
      const last = ranges.at(-1);
      if (last && number === last[1] + 1) last[1] = number;
      else ranges.push([number, number]);
    }
    const pad = (value) => String(value).padStart(2, "0");
    return ranges.map(([start, end]) => `${prefix}${pad(start)}${end === start ? "" : `-${pad(end)}`}`).join(",");
  },
  subtitleText = (value) => {
    const normalized = String(value || "").replace(/[\s-]+/g, "_").toUpperCase();
    if (["JPN_CHI", "CHI_JPN", "CHT_JPN", "JPN_CHT"].includes(normalized)) return "JPTC";
    if (["JPN_CHS", "CHS_JPN"].includes(normalized)) return "JPSC";
    if (normalized === "ASSX2") return "ASSx2";
    return value || t("other");
  },
  releaseUnitText = (group) => {
    const archive = group.top.sourceFamily === "archive",
      size = group.members.length,
      unit = group.top.releaseUnit;
    return unit === "collection"
      ? (archive ? ["合卷", "Disc collection", "全巻"][li()] : ["合集", "Collection", "コレクション"][li()])
      : unit === "volume"
        ? (size > 1 ? ["多卷", "Multiple volumes", "複数巻"][li()] : ["单卷", "Single volume", "単巻"][li()])
        : (size > 1 ? ["多集", "Multiple episodes", "複数話"][li()] : ["单集", "Single episode", "単話"][li()]);
  },
  resourceResolutionText = (value, scan = "p") => {
    const raw = String(value ?? "").trim(), lower = raw.toLowerCase();
    if (!raw || ["unknown", "other", "null", "0"].includes(lower)) return "";
    if (/^\d+$/.test(raw)) return `${raw}${scan || "p"}`;
    return /^\d+[pi]$/i.test(raw) ? raw : "";
  },
  resourceSubtitleModeText = (value) => String(value || "").split("+").map((mode) => ({
    soft: ["可关闭字幕", "Soft subtitles", "切替字幕"], hard: ["固定字幕", "Hard subtitles", "焼き込み字幕"],
    external: ["独立字幕文件", "External subtitles", "外部字幕"],
  }[mode]?.[li()] || "")).filter(Boolean).join(" / "),
  eligibilityReasonText = (item) => {
    if (item?.eligible) return "";
    const reason = String(item?.eligibilityReason || "");
    if (reason === "source_class_disabled") return t("excludedSourceClass");
    if (reason === "resource_group_disabled") return t("excludedResourceGroup");
    if (reason === "serial_language_mismatch") return t("excludedSerialProfile");
    if (reason === "resolution_disabled") return t("excludedResolution");
    if (reason === "subtitle_disabled") return t("excludedSubtitle");
    if (reason === "scope_excluded") return t("excludedScope");
    if (reason === "manifest_or_partition_unavailable") return t("excludedManifest");
    return t("excludedPolicy");
  },
  torrentGroups = (rows) => {
    const buckets = new Map(), groups = [];
    for (const row of rows) {
      const sequence = row.releaseUnit === "volume" ? row.volumeSequence : row.episodeSequence,
        mergeable = (row.eligible || row.canOverride) && ["episode", "volume"].includes(row.releaseUnit) && sequence?.length,
        key = mergeable
          ? [row.eligible, row.sourceFamily, row.sourceClass, row.resourceGroup, row.subtitle, row.resolution, row.scan, row.bitDepth, row.releaseUnit, row.releaseVariantKey].join("|")
          : row.infoHash;
      if (!buckets.has(key)) buckets.set(key, { mergeable, rows: [] });
      buckets.get(key).rows.push(row);
    }
    for (const bucket of buckets.values()) {
      const pending = bucket.rows.sort((a, b) => a.effectiveRank - b.effectiveRank);
      if (!bucket.mergeable) { pending.forEach((row) => groups.push([row])); continue; }
      while (pending.length) {
        const members = [pending.shift()], sequenceKey = members[0].releaseUnit === "volume" ? "volumeSequence" : "episodeSequence",
          seen = new Set(members[0][sequenceKey] || []);
        for (let index = 0; index < pending.length;) {
          const sequence = new Set(pending[index][sequenceKey] || []);
          if (sequence.size && [...sequence].every((value) => !seen.has(value))) {
            const member = pending.splice(index, 1)[0]; members.push(member); sequence.forEach((value) => seen.add(value));
          } else index += 1;
        }
        groups.push(members);
      }
    }
    return groups.map((members) => {
      members.sort((a, b) => a.effectiveRank - b.effectiveRank);
      const top = members[0], sequenceKey = top.releaseUnit === "volume" ? "volumeSequence" : "episodeSequence",
        sequence = members.flatMap((item) => item[sequenceKey] || []),
        sample = [...members].sort((a, b) => Math.max(...(b[sequenceKey] || [0])) - Math.max(...(a[sequenceKey] || [0])))[0];
      return { members, top, sample, sequence };
    }).sort((a, b) => a.top.effectiveRank - b.top.effectiveRank);
  },
  resourceChoices = (torrents, aniResources, id) => {
    const buckets = new Map();
    const add = (provider, y, members = [y]) => {
      const selectedValue = provider === "torrent" ? torrentSelections.get(id) : resourceSelections.get(id),
        selectedMember = provider === "torrent" ? members.find((row) => row.infoHash === selectedValue) : null,
        value = `${provider}:${selectedMember?.infoHash || (provider === "torrent" ? y.infoHash : y.resourceId)}`,
        group = String(y.resourceGroup || "").trim(),
        knownGroup = group && !["unknown", "other"].includes(group.toLowerCase()),
        key = `${provider}:${knownGroup ? group.normalize("NFKC").toLowerCase() : value}`,
        source = y.sourceClass && !["unknown", "other"].includes(String(y.sourceClass).toLowerCase()) ? sourceClassText(y.sourceClass) : "",
        subtitle = y.subtitle && !["unknown", "other"].includes(String(y.subtitle).toLowerCase()) ? subtitleText(y.subtitle) : "",
        files = provider === "torrent" ? members.reduce((sum, row) => sum + Number(row.fileCount || 0), 0) : Number(y.itemCount || 0),
        details = [y.releaseVariant?.platform, subtitle, source, resourceResolutionText(y.resolution, y.scan), y.releaseVariant?.codec, y.releaseVariant?.container, resourceSubtitleModeText(y.releaseVariant?.subtitleMode), files ? `${fmt(files)} ${t("files")}` : ""].filter(Boolean).join(" · "),
        title = y.variantTitle || y.name || y.title || y.storageDirectory || value,
        option = { value, title, details, eligible: !!y.eligible, canOverride: !!y.canOverride, reason: eligibilityReasonText(y), selected: value === `${provider}:${selectedValue}` };
      if (!buckets.has(key)) buckets.set(key, { provider, name: knownGroup ? group : t("other"), variants: [] });
      buckets.get(key).variants.push(option);
    };
    torrentGroups(torrents || []).forEach((group) => add("torrent", group.top, group.members));
    (aniResources || []).forEach((row) => add("ani-rss", row));
    return [...buckets.values()].map((group) => {
      group.current = group.variants.findIndex((row) => row.selected);
      if (group.current < 0) group.current = group.variants.findIndex((row) => row.eligible);
      if (group.current < 0) group.current = Math.max(0, group.variants.findIndex((row) => row.canOverride));
      return group;
    });
  },
  resourceChoiceHtml = (group, id, index) => {
    const y = group.variants[group.current], radioId = `resource-${id}-${index}`,
      provider = group.provider === "ani-rss" ? "Ani-RSS" : "Torrent",
      options = group.variants.map((row, position) => `<option value="${position}" ${position === group.current ? "selected" : ""} ${row.eligible || row.canOverride ? "" : "disabled"}>${esc([row.details, row.title, row.reason].filter(Boolean).join(" · "))}</option>`).join(""),
      choice = group.variants.length > 1
        ? `<select class="resource-variant" aria-label="${esc(`${group.name} · ${provider}`)}" title="${esc(y.title)}">${options}</select>`
        : `<span class="resource-title" title="${esc(y.title)}">${esc(y.title)}</span>`;
    return `<div class="torrent ${y.eligible ? "" : y.canOverride ? "policy-excluded" : "disabled"}" data-resource-choice="${index}"><input id="${radioId}" type="radio" name="resource-${id}" value="${esc(y.value)}" data-policy-override="${!y.eligible && y.canOverride}" ${y.eligible || y.canOverride ? "" : "disabled"} ${y.selected && (y.eligible || policyOverrides.get(id) === y.value) ? "checked" : ""} aria-label="${esc(`${group.name} · ${provider}`)}"><div class="torrent-lines"><label for="${radioId}"><b>${esc(group.name)} <small>${provider}</small></b></label>${choice}<small class="torrent-subline"><span>${esc(y.details)}</span><span class="torrent-exclusion">${esc(y.reason)}</span></small></div></div>`;
  };

function graphTitle(node) {
  return preferred(node) || node.title_ja;
}

const relationSubjectCategoryKey = {
  music: "relatedMusic",
  original: "relatedOriginal",
  adaptation: "relatedAdaptation",
  related: "relatedOther",
};

const relatedSubjectCategoryOrder = ["music", "original", "adaptation", "related"],
  relatedSubjectRoleOrder = ["opening", "ending", "theme_collection", "character_song", "soundtrack", "music"];

function relationSubjectChips(node) {
  const categories = [...new Set((node.related_subjects || []).map((item) => item.category))]
    .sort((a, b) => relatedSubjectCategoryOrder.indexOf(a) - relatedSubjectCategoryOrder.indexOf(b));
  return categories
    .filter((category) => relationSubjectCategoryKey[category])
    .map(
      (category) =>
        `<button type="button" class="relation-subject-chip" data-related-node="${Number(node.id)}" data-related-category="${esc(category)}" title="${esc(t(relationSubjectCategoryKey[category]))}">${esc(t(relationSubjectCategoryKey[category]))}</button>`,
    )
    .join("");
}

let relationSubjectPopoverPinned = false,
  relationSubjectPopoverKey = "",
  relationSubjectPopoverAnchor = null,
  relationSubjectHideTimer;

function relatedSubjectLine(item, category) {
  const kind = label("relatedKind", item.kind || "other"),
    role = category === "music" ? label("relatedRole", item.role || "music") : "",
    relation = category === "original"
      ? t("originalWithKind").replace("{kind}", kind)
      : category === "adaptation"
        ? t("adaptationWithKind").replace("{kind}", kind)
        : role || kind,
    metadata = [];
  if (item.authors?.length) metadata.push(`${t("relatedAuthor")}：${item.authors.join(" / ")}`);
  if (item.publishers?.length) metadata.push(`${t("relatedPublisher")}：${item.publishers.join(" / ")}`);
  if (item.artists?.length) metadata.push(`${t("relatedArtist")}：${item.artists.join(" / ")}`);
  return `<li><b>${esc(relation)}</b><a href="${esc(item.url)}" target="_blank" rel="noreferrer">${esc(item.title)}</a>${metadata.length ? `<small>${esc(metadata.join(" · "))}</small>` : ""}</li>`;
}

function showRelationSubjectPopover(button, graph, pinned = false) {
  clearTimeout(relationSubjectHideTimer);
  const popover = $("relationSubjectPopover"),
    node = graph.nodes.find((item) => Number(item.id) === Number(button.dataset.relatedNode)),
    category = button.dataset.relatedCategory,
    subjects = (node?.related_subjects || []).filter((item) => item.category === category)
      .sort((a, b) => {
        const ai = relatedSubjectRoleOrder.indexOf(a.role), bi = relatedSubjectRoleOrder.indexOf(b.role);
        return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi) || String(a.title).localeCompare(String(b.title));
      });
  if (!popover || !subjects.length) return;
  relationSubjectPopoverPinned = pinned;
  relationSubjectPopoverKey = `${button.dataset.relatedNode}:${category}`;
  popover.innerHTML = `<header><b>${esc(t(relationSubjectCategoryKey[category]))}</b><span>${esc(graphTitle(node))}</span></header><ul>${subjects.map((item) => relatedSubjectLine(item, category)).join("")}</ul>`;
  popover.hidden = false;
  relationSubjectPopoverAnchor = button;
  positionRelationSubjectPopover();
}
function positionRelationSubjectPopover() {
  const popover = $("relationSubjectPopover"), button = relationSubjectPopoverAnchor;
  if (!popover || popover.hidden) return;
  if (!button?.isConnected) { popover.hidden = true; return; }
  const rect = button.getBoundingClientRect(),
    box = popover.getBoundingClientRect(),
    left = Math.max(12, Math.min(rect.left, window.innerWidth - box.width - 12)),
    below = rect.bottom + 8,
    top = below + box.height <= window.innerHeight - 12
      ? below
      : Math.max(12, rect.top - box.height - 8);
  popover.style.left = `${left}px`;
  popover.style.top = `${Math.max(12, Math.min(top, window.innerHeight - box.height - 12))}px`;
}
window.addEventListener("resize", positionRelationSubjectPopover);

function scheduleRelationSubjectPopoverHide() {
  clearTimeout(relationSubjectHideTimer);
  relationSubjectHideTimer = setTimeout(() => {
    if (!relationSubjectPopoverPinned) $("relationSubjectPopover").hidden = true;
  }, 140);
}

function explicitGraphSequenceNumber(node) {
  const title = `${node.title_ja || ""} ${node.title_zh_hans || ""} ${node.title_en || ""}`,
    arabic = title.match(/(?:^|[^\d])(\d{1,2})\s*$/),
    chinese = title.match(/第\s*([一二三四五六七八九十])\s*(?:季|期|章)/),
    roman = title.match(/(?:\b(II|III|IV|V|VI|VII|VIII|IX|X)|([ⅡⅢⅣⅤⅥⅦⅧⅨⅩ]))\s*$/i),
    chineseValues = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 },
    romanValues = { II: 2, III: 3, IV: 4, V: 5, VI: 6, VII: 7, VIII: 8, IX: 9, X: 10, Ⅱ: 2, Ⅲ: 3, Ⅳ: 4, Ⅴ: 5, Ⅵ: 6, Ⅶ: 7, Ⅷ: 8, Ⅸ: 9, Ⅹ: 10 };
  return arabic
    ? Number(arabic[1])
    : chinese
      ? chineseValues[chinese[1]]
      : roman
        ? romanValues[(roman[1] || roman[2]).toUpperCase()] || null
        : null;
}

function relaxCrossingBranchTargets(
  layoutEdges,
  positions,
  nodesById,
  mainlineNodes,
) {
  const branches = layoutEdges.filter(
      (edge) =>
        (relationGeometry[edge.relation_code] || relationGeometry.other).side !==
        0,
    ),
    moved = new Set(),
    overlapCount = (nodeId, y) => {
      const point = positions.get(nodeId);
      return [...positions.entries()].filter(([otherId, other]) => {
        if (otherId === nodeId) return false;
        return (
          point.x < other.x + 232 &&
          point.x + 232 > other.x &&
          y < other.y + 116 &&
          y + 116 > other.y
        );
      }).length;
    };
  for (let leftIndex = 0; leftIndex < branches.length; leftIndex++) {
    for (
      let rightIndex = leftIndex + 1;
      rightIndex < branches.length;
      rightIndex++
    ) {
      const left = branches[leftIndex],
        right = branches[rightIndex];
      if (
        left.source_anime_id === right.source_anime_id ||
        left.target_anime_id === right.target_anime_id
      )
        continue;
      const leftSource = positions.get(left.source_anime_id),
        rightSource = positions.get(right.source_anime_id),
        leftTarget = positions.get(left.target_anime_id),
        rightTarget = positions.get(right.target_anime_id);
      if (
        !leftSource ||
        !rightSource ||
        !leftTarget ||
        !rightTarget ||
        Math.abs(leftSource.y - rightSource.y) > 4 ||
        Math.abs(leftTarget.y - rightTarget.y) > 4 ||
        (leftSource.x - rightSource.x) * (leftTarget.x - rightTarget.x) >= 0
      )
        continue;
      const targetIds = [left.target_anime_id, right.target_anime_id]
          .filter((id) => !mainlineNodes.has(id) && !moved.has(id))
          .sort((a, b) => {
            const aNode = nodesById.get(a),
              bNode = nodesById.get(b);
            return (
              String(bNode?.start_month).localeCompare(
                String(aNode?.start_month),
              ) || b - a
            );
          }),
        targetId = targetIds[0];
      if (!targetId) continue;
      const point = positions.get(targetId),
        candidates = [28, -28, 56, -56]
          .map((offset, order) => ({
            offset,
            score: overlapCount(targetId, point.y + offset) * 10000 + order,
          }))
          .sort((a, b) => a.score - b.score);
      point.y += candidates[0].offset;
      moved.add(targetId);
    }
  }
  return moved.size;
}

function graphPositions(graph) {
  const strict = graph.nodes
      .filter((node) => node.strict_member)
      .sort(
        (a, b) =>
          String(a.start_month).localeCompare(String(b.start_month)) ||
          a.id - b.id,
      ),
    context = graph.nodes.filter((node) => !node.strict_member),
    strictIds = new Set(strict.map((node) => node.id)),
    strictById = new Map(strict.map((node) => [node.id, node])),
    levels = new Map(strict.map((node) => [node.id, 0])),
    positions = new Map(),
    layoutEdges = graph.edges.filter(
      (edge) =>
        edge.grouping &&
        strictIds.has(edge.source_anime_id) &&
        strictIds.has(edge.target_anime_id),
    ),
    nodeBaseY =
      110 + Math.max(0, relationLaneCapacity(graph, -1) - 1) * 10;

  // Mainline depth is determined by chronological sequel/prequel edges only. Branch relations may
  // place branch nodes, but never push an established mainline node away from
  // its adjacent sequel. This keeps the central sequel lane straight.
  const mainlineEdges = layoutEdges.filter(
      (edge) => ["sequel", "prequel"].includes(edge.relation_code),
    ),
    mainlineNodes = new Set(
      mainlineEdges.flatMap((edge) => [
        edge.source_anime_id,
        edge.target_anime_id,
      ]),
    ),
    relax = (edges, protectedTargets = new Set()) => {
      for (let pass = 0; pass < strict.length; pass++) {
        let changed = false;
        for (const edge of edges) {
          if (protectedTargets.has(edge.target_anime_id)) continue;
          const sourceKey =
              strictById.get(edge.source_anime_id)?.start_month || "0000-00",
            targetKey =
              strictById.get(edge.target_anime_id)?.start_month || "9999-99";
          if (sourceKey > targetKey) continue;
          const candidate = Math.min(
            strict.length - 1,
            (levels.get(edge.source_anime_id) || 0) + 1,
          );
          if (candidate > (levels.get(edge.target_anime_id) || 0)) {
            levels.set(edge.target_anime_id, candidate);
            changed = true;
          }
        }
        if (!changed) break;
      }
    };
  if (mainlineEdges.length) {
    relax(mainlineEdges);
    relax(
      layoutEdges.filter(
        (edge) => !["sequel", "prequel"].includes(edge.relation_code),
      ),
      mainlineNodes,
    );
  } else relax(layoutEdges);
  const usedLevels = [...new Set(levels.values())].sort((a, b) => a - b),
    compactLevel = new Map(usedLevels.map((value, index) => [value, index])),
    groups = new Map();
  strict.forEach((node) => {
    const level = compactLevel.get(levels.get(node.id)) || 0;
    if (!groups.has(level)) groups.set(level, []);
    groups.get(level).push(node);
  });
  const rootMedia = strictById.get(graph.rootAnimeId)?.media_code || "tv",
    mediaOrder = [
      ...new Set([
        rootMedia,
        "tv",
        "movie",
        "web",
        "ova",
        "other",
        ...strict.map((node) => node.media_code || "other"),
      ]),
    ].filter((code) =>
      strict.some((node) => (node.media_code || "other") === code),
    ),
    mediaRank = new Map(mediaOrder.map((code, index) => [code, index])),
    isMainline = (node) => mainlineNodes.has(node.id),
    sameMediaMainlineLinks = (node) =>
      mainlineEdges.filter((edge) => {
        if (edge.source_anime_id !== node.id && edge.target_anime_id !== node.id)
          return false;
        const otherId =
            edge.source_anime_id === node.id
              ? edge.target_anime_id
              : edge.source_anime_id,
          other = strictById.get(otherId);
        return other?.media_code === node.media_code;
      }).length,
    compareLaneCandidates = (a, b) =>
      sameMediaMainlineLinks(b) - sameMediaMainlineLinks(a) ||
      Number(explicitGraphSequenceNumber(b) != null) -
        Number(explicitGraphSequenceNumber(a) != null) ||
      Number(isMainline(b)) - Number(isMainline(a)) ||
      String(a.start_month).localeCompare(String(b.start_month)) ||
      a.id - b.id;
  [...groups.entries()]
    .sort(([left], [right]) => left - right)
    .forEach(([level, nodes]) => {
      const buckets = new Map();
      nodes.forEach((node) => {
        const media = node.media_code || "other";
        if (!buckets.has(media)) buckets.set(media, []);
        buckets.get(media).push(node);
      });
      buckets.forEach((bucket) => bucket.sort(compareLaneCandidates));
      const placements = [],
        overflow = [];
      mediaOrder.forEach((media) => {
        const bucket = buckets.get(media) || [];
        if (bucket.length)
          placements.push({ node: bucket[0], lane: mediaRank.get(media) });
        overflow.push(...bucket.slice(1));
      });
      overflow
        .sort(
          (a, b) =>
            mediaRank.get(a.media_code || "other") -
              mediaRank.get(b.media_code || "other") ||
            compareLaneCandidates(a, b),
        )
        .forEach((node, index) =>
          placements.push({ node, lane: mediaOrder.length + index }),
        );
      placements.forEach(({ node, lane }) =>
        positions.set(node.id, {
          x: 42 + level * 340,
          y: nodeBaseY + lane * 148,
        }),
      );
    });
  relaxCrossingBranchTargets(
    layoutEdges,
    positions,
    strictById,
    mainlineNodes,
  );
  const contextGap =
      146 + Math.max(0, relationLaneCapacity(graph, 1) - 1) * 8,
    contextBase =
      Math.max(...[...positions.values()].map((item) => item.y), nodeBaseY) +
      contextGap,
    contextColumns = Math.min(7, Math.max(1, context.length));
  context.forEach((node, index) => {
    const row = Math.floor(index / contextColumns),
      column = index % contextColumns;
    positions.set(node.id, {
      x: 42 + column * 300,
      y: contextBase + row * 138,
    });
  });
  return positions;
}

const relationGeometry = {
  sequel: { offset: 0, side: 0 },
  prequel: { offset: 0, side: 0 },
  alternative_version: { offset: -30, side: -1 },
  alternative_setting: { offset: -42, side: -1 },
  adaptation: { offset: -18, side: -1 },
  main_story: { offset: -18, side: -1 },
  side_story: { offset: 28, side: 1 },
  spin_off: { offset: 40, side: 1 },
  summary: { offset: 18, side: 1 },
  full_story: { offset: 18, side: 1 },
  character_appearance: { offset: 40, side: 1 },
  collaboration: { offset: 40, side: 1 },
  same_setting: { offset: -42, side: -1 },
  other: { offset: 28, side: 1 },
};

function relationLaneCapacity(graph, side) {
  const counts = new Map();
  graph.edges.forEach((edge) => {
    const geometry = relationGeometry[edge.relation_code] || relationGeometry.other;
    if (geometry.side !== side) return;
    [`source:${edge.source_anime_id}`, `target:${edge.target_anime_id}`].forEach(
      (endpoint) => {
        const key = `${side}:${endpoint}`;
        counts.set(key, (counts.get(key) || 0) + 1);
      },
    );
  });
  return Math.max(1, ...counts.values());
}

const relationEdgeKey = (edge) =>
  `${edge.source_anime_id}:${edge.target_anime_id}:${edge.relation_code}`;

function assignRelationPorts(edges, positions, nodesById) {
  const groups = new Map(),
    result = new Map(
      edges.map((edge) => [relationEdgeKey(edge), { source: 0, target: 0 }]),
    ),
    add = (nodeId, side, edge, endpoint) => {
      const key = `${nodeId}:${side}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push({ edge, endpoint });
    },
    centerPortPriority = (attachment) => {
      const edge = attachment.edge,
        style = relationGeometry[edge.relation_code] || relationGeometry.other;
      if (style.side !== 0) return 100;
      const from = positions.get(edge.source_anime_id),
        to = positions.get(edge.target_anime_id),
        source = nodesById.get(edge.source_anime_id),
        target = nodesById.get(edge.target_anime_id),
        sameRow = Boolean(from && to) && Math.abs(from.y - to.y) <= 4,
        sourceMonth = String(source?.start_month || ""),
        targetMonth = String(target?.start_month || ""),
        dated = Boolean(sourceMonth && targetMonth),
        chronologyCorrect =
          edge.relation_code === "sequel"
            ? dated && sourceMonth <= targetMonth
            : edge.relation_code === "prequel"
              ? dated && targetMonth <= sourceMonth
              : false,
        relationRank =
          edge.relation_code === "sequel" && chronologyCorrect
            ? 0
            : edge.relation_code === "prequel" && chronologyCorrect
              ? 1
              : 2;
      // A straight chronological continuation owns the center port. A
      // backward-looking prequel is offset before the sequel is bent.
      return Number(!sameRow) * 10 + relationRank;
    };
  edges.forEach((edge) => {
    const from = positions.get(edge.source_anime_id),
      to = positions.get(edge.target_anime_id),
      forward = to.x > from.x,
      backward = to.x < from.x;
    add(edge.source_anime_id, forward ? "right" : "left", edge, "source");
    add(
      edge.target_anime_id,
      forward ? "left" : backward ? "right" : "left",
      edge,
      "target",
    );
  });
  groups.forEach((attachments) => {
    attachments.sort((left, right) => {
      const aStyle =
          relationGeometry[left.edge.relation_code] || relationGeometry.other,
        bStyle =
          relationGeometry[right.edge.relation_code] || relationGeometry.other,
        aSource = nodesById.get(left.edge.source_anime_id),
        bSource = nodesById.get(right.edge.source_anime_id),
        aTarget = nodesById.get(left.edge.target_anime_id),
        bTarget = nodesById.get(right.edge.target_anime_id);
      return (
        aStyle.side - bStyle.side ||
        centerPortPriority(left) - centerPortPriority(right) ||
        String(aSource?.start_month).localeCompare(
          String(bSource?.start_month),
        ) ||
        String(aTarget?.start_month).localeCompare(
          String(bTarget?.start_month),
        ) ||
        aStyle.offset - bStyle.offset ||
        relationEdgeKey(left.edge).localeCompare(relationEdgeKey(right.edge))
      );
    });
    const negative = [],
      positive = [],
      neutral = [];
    attachments.forEach((attachment) => {
      const side =
        (relationGeometry[attachment.edge.relation_code] ||
          relationGeometry.other).side;
      (side < 0 ? negative : side > 0 ? positive : neutral).push(attachment);
    });
    const setPort = (attachment, offset) => {
      result.get(relationEdgeKey(attachment.edge))[attachment.endpoint] = offset;
      },
      distribute = (items, start, end, single = (start + end) / 2) =>
        items.forEach((attachment, index) =>
          setPort(
            attachment,
            items.length === 1
              ? single
              : start + (index * (end - start)) / (items.length - 1),
          ),
        );
    if (neutral.length) {
      setPort(neutral[0], 0);
      neutral.slice(1).forEach((attachment, index) =>
        setPort(
          attachment,
          (index % 2 ? 1 : -1) *
            Math.min(38, 18 + Math.floor(index / 2) * 11),
        ),
      );
      distribute(negative, -38, -18, -18);
      distribute(positive, 18, 38, 18);
    } else if (negative.length && positive.length) {
      distribute(negative, -38, -8, -8);
      distribute(positive, 8, 38, 8);
    } else if (negative.length) distribute(negative, -38, 0, 0);
    else if (positive.length) distribute(positive, 0, 38, 0);
  });
  return result;
}

function routeRelationEdge(
  edge,
  from,
  to,
  positions,
  ports,
  routedLane = 0,
  routedTotal = 1,
  sourceTurnRank = 0,
  sourceTurnTotal = 1,
  collisionNudge = 0,
) {
  const style = relationGeometry[edge.relation_code] || relationGeometry.other,
    nodeWidth = 220,
    nodeHeight = 104,
    // Branch-like relations often form dense fans around specials and OADs.
    // Give them wider lanes than mainline relations; conflict grouping below
    // still decides which routes actually need separate lanes.
    laneXSpacing =
      edge.relation_code === "spin_off"
        ? 22
        : edge.relation_code === "side_story"
          ? 18
          : 12,
    laneYSpacing =
      edge.relation_code === "spin_off"
        ? 18
        : edge.relation_code === "side_story"
          ? 15
          : 10,
    corridorLaneSpacing =
      edge.relation_code === "spin_off"
        ? 11
        : edge.relation_code === "side_story"
          ? 9
          : 5,
    sourceCenterX = from.x + nodeWidth / 2,
    targetCenterX = to.x + nodeWidth / 2,
    sourceAbove = from.y + nodeHeight <= to.y,
    sourceBelow = to.y + nodeHeight <= from.y,
    verticalGapTop = sourceAbove ? from.y + nodeHeight : to.y + nodeHeight,
    verticalGapBottom = sourceAbove ? to.y : from.y,
    verticalCorridorClear =
      (sourceAbove || sourceBelow) &&
      ![...positions.entries()].some(([id, point]) => {
        if (id === edge.source_anime_id || id === edge.target_anime_id)
          return false;
        const crossesCenter =
            point.x < Math.max(sourceCenterX, targetCenterX) + 8 &&
            point.x + nodeWidth > Math.min(sourceCenterX, targetCenterX) - 8,
          crossesGap =
            point.y < verticalGapBottom &&
            point.y + nodeHeight > verticalGapTop;
        return crossesCenter && crossesGap;
      }),
    baseCenteredLane = routedLane - (routedTotal - 1) / 2,
    candidateCenteredLane = baseCenteredLane + collisionNudge;
  const routeObstacleCount = (segments) =>
    segments.reduce(
      (count, segment) =>
        count +
        [...positions.entries()].filter(([id, point]) => {
          if (id === edge.source_anime_id || id === edge.target_anime_id)
            return false;
          const left = point.x - 6,
            right = point.x + nodeWidth + 6,
            top = point.y - 6,
            bottom = point.y + nodeHeight + 6;
          return segment.vertical
            ? segment.x > left &&
                segment.x < right &&
                Math.max(segment.y1, segment.y2) > top &&
                Math.min(segment.y1, segment.y2) < bottom
            : segment.y > top &&
                segment.y < bottom &&
                Math.max(segment.x1, segment.x2) > left &&
                Math.min(segment.x1, segment.x2) < right;
        }).length,
      0,
    );

  // Non-grouping (gray, cross-series) relations use the facing midpoint of
  // their target. An obstacle-scored side corridor keeps the long connector
  // outside unrelated nodes, then approaches just above/below the target.
  if (!edge.grouping && (sourceAbove || sourceBelow)) {
    const allPoints = [...positions.values()],
      graphLeft = Math.min(...allPoints.map((point) => point.x)),
      graphRight = Math.max(...allPoints.map((point) => point.x + nodeWidth)),
      visualLane = candidateCenteredLane,
      laneShift = visualLane * 10,
      targetX = targetCenterX,
      targetY = sourceAbove ? to.y : to.y + nodeHeight,
      terminalLead = 34 + routedLane * 8,
      trackY = targetY + (sourceAbove ? -terminalLead : terminalLead),
      candidates = [
        graphLeft - 28 + laneShift,
        graphRight + 28 + laneShift,
        from.x - 34 + laneShift,
        from.x + nodeWidth + 34 + laneShift,
        to.x - 34 + laneShift,
        to.x + nodeWidth + 34 + laneShift,
      ].map((corridorX) => {
        const sourceX = corridorX < sourceCenterX ? from.x : from.x + nodeWidth,
          sourceY = from.y + nodeHeight / 2 + (ports?.source || 0),
          segments = [
            { x1: sourceX, y: sourceY, x2: corridorX, vertical: false },
            { x: corridorX, y1: sourceY, y2: trackY, vertical: true },
            { x1: corridorX, y: trackY, x2: targetX, vertical: false },
            { x: targetX, y1: trackY, y2: targetY, vertical: true },
          ];
        return {
          corridorX,
          sourceX,
          sourceY,
          score:
            routeObstacleCount(segments) * 10000 +
            Math.abs(sourceX - corridorX) +
            Math.abs(sourceY - trackY) +
            Math.abs(corridorX - targetX),
        };
      });
    candidates.sort((left, right) => left.score - right.score);
    const chosen = candidates[0];
    return {
      path: `M ${chosen.sourceX} ${chosen.sourceY} H ${chosen.corridorX} V ${trackY} H ${targetX} V ${targetY}`,
      labelX: (chosen.corridorX + targetX) / 2,
      labelY: trackY,
      labelOrientation: "horizontal",
      direct: false,
      vertical: true,
      contextRoute: true,
      targetSegment: {
        x: targetX,
        top: Math.min(trackY, targetY),
        bottom: Math.max(trackY, targetY),
      },
      local: false,
    };
  }

  // A non-mainline relation between center-aligned nodes uses facing vertical
  // ports. Adjacent nodes connect directly; intervening nodes trigger a
  // side-corridor bypass while preserving the same top/bottom anchors.
  if (
    style.side !== 0 &&
    Math.abs(sourceCenterX - targetCenterX) <= 24 &&
    verticalGapBottom - verticalGapTop >= 12 &&
    verticalCorridorClear
  ) {
    const laneOffset = Math.max(
        -88,
        Math.min(88, candidateCenteredLane * laneXSpacing),
      ),
      sourceX = sourceCenterX + laneOffset,
      targetX = targetCenterX + laneOffset,
      sourceY = sourceAbove ? from.y + nodeHeight : from.y,
      targetY = sourceAbove ? to.y : to.y + nodeHeight,
      middleY = (sourceY + targetY) / 2,
      sourceLeadY = sourceY + (sourceAbove ? 18 : -18),
      targetLeadY = targetY + (sourceAbove ? -18 : 18),
      corridorX = sourceX + collisionNudge * laneXSpacing;
    return {
      path:
        collisionNudge
          ? `M ${sourceX} ${sourceY} V ${sourceLeadY} H ${corridorX} V ${targetLeadY} H ${targetX} V ${targetY}`
          : sourceX === targetX
          ? `M ${sourceX} ${sourceY} V ${targetY}`
          : `M ${sourceX} ${sourceY} V ${middleY} H ${targetX} V ${targetY}`,
      labelX: collisionNudge ? corridorX + 44 : (sourceX + targetX) / 2 + 44,
      labelY: middleY,
      labelOrientation: "vertical",
      direct: !collisionNudge,
      vertical: true,
      local: false,
    };
  }

  // A diagonally separated branch prefers facing top/bottom ports and a
  // short Z-shaped route. Target-entry segments are exposed to the metadata
  // pass so fan-in edges are assigned distinct ports instead of overlapping.
  if (
    style.side !== 0 &&
    (sourceAbove || sourceBelow) &&
    verticalGapBottom - verticalGapTop >= 12
  ) {
    const laneOffset = Math.max(
        -88,
        Math.min(88, candidateCenteredLane * laneXSpacing),
      ),
      sourceX = sourceCenterX + laneOffset,
      targetX = targetCenterX + laneOffset,
      sourceY = sourceAbove ? from.y + nodeHeight : from.y,
      targetY = sourceAbove ? to.y : to.y + nodeHeight,
      middleY =
        (sourceY + targetY) / 2 +
        candidateCenteredLane * laneYSpacing,
      segments = [
        { x: sourceX, y1: sourceY, y2: middleY, vertical: true },
        { x1: sourceX, y: middleY, x2: targetX, vertical: false },
        { x: targetX, y1: middleY, y2: targetY, vertical: true },
      ];
    if (!routeObstacleCount(segments))
      return {
        path: `M ${sourceX} ${sourceY} V ${middleY} H ${targetX} V ${targetY}`,
        labelX: (sourceX + targetX) / 2,
        labelY: middleY,
        labelOrientation: "horizontal",
        direct: false,
        vertical: true,
        diagonal: true,
        targetSegment: {
          x: targetX,
          top: Math.min(middleY, targetY),
          bottom: Math.max(middleY, targetY),
        },
        local: false,
      };
  }

  if (
    style.side !== 0 &&
    Math.abs(sourceCenterX - targetCenterX) <= 24 &&
    verticalGapBottom - verticalGapTop >= 12 &&
    !verticalCorridorClear
  ) {
    const laneOffset = Math.max(
        -88,
        Math.min(88, candidateCenteredLane * laneXSpacing),
      ),
      sourceX = sourceCenterX + laneOffset,
      targetX = targetCenterX + laneOffset,
      sourceY = sourceAbove ? from.y + nodeHeight : from.y,
      targetY = sourceAbove ? to.y : to.y + nodeHeight,
      sourceLeadY = sourceY + (sourceAbove ? 22 : -22),
      targetLeadY = targetY + (sourceAbove ? -22 : 22),
      allPoints = [...positions.values()],
      graphLeft = Math.min(...allPoints.map((point) => point.x)),
      graphRight = Math.max(...allPoints.map((point) => point.x + nodeWidth)),
      graphCenter = (graphLeft + graphRight) / 2,
      preferredSide =
        (sourceCenterX + targetCenterX) / 2 >= graphCenter ? 1 : -1,
      clearance =
        24 + Math.abs(laneOffset) + Math.abs(collisionNudge) * laneXSpacing,
      candidates = [
        Math.min(from.x, to.x) - clearance,
        Math.max(from.x + nodeWidth, to.x + nodeWidth) + clearance,
      ].map((corridorX, index) => {
        const side = index ? 1 : -1,
          top = Math.min(sourceLeadY, targetLeadY),
          bottom = Math.max(sourceLeadY, targetLeadY),
          obstacleCount = [...positions.entries()].filter(([id, point]) => {
            if (id === edge.source_anime_id || id === edge.target_anime_id)
              return false;
            return (
              corridorX > point.x - 8 &&
              corridorX < point.x + nodeWidth + 8 &&
              point.y < bottom &&
              point.y + nodeHeight > top
            );
          }).length,
          outside = corridorX < 8 || corridorX > graphRight + 80;
        return {
          corridorX,
          score:
            obstacleCount * 1000 + Number(outside) * 100 +
            Number(side !== preferredSide),
        };
      });
    candidates.sort((left, right) => left.score - right.score);
    const corridorX = candidates[0].corridorX;
    return {
      path: `M ${sourceX} ${sourceY} V ${sourceLeadY} H ${corridorX} V ${targetLeadY} H ${targetX} V ${targetY}`,
      labelX: corridorX,
      labelY: (sourceLeadY + targetLeadY) / 2,
      labelOrientation: "vertical",
      direct: false,
      vertical: true,
      bypass: true,
      local: false,
    };
  }

  const
    forward = to.x > from.x,
    backward = to.x < from.x,
    sourceX = forward ? from.x + 220 : from.x,
    targetX = forward ? to.x : backward ? to.x + 220 : to.x,
    sourceY = from.y + 52 + ports.source,
    targetY = to.y + 52 + ports.target,
    left = Math.min(sourceX, targetX),
    right = Math.max(sourceX, targetX),
    obstacle = [...positions.entries()].some(([id, point]) => {
      if (id === edge.source_anime_id || id === edge.target_anime_id)
        return false;
      const overlapsX = point.x < right && point.x + 220 > left,
        overlapsY = sourceY > point.y - 5 && sourceY < point.y + 109;
      return overlapsX && overlapsY;
    });

  if (!obstacle && sourceY === targetY && from.x !== to.x) {
    return {
      path: `M ${sourceX} ${sourceY} H ${targetX}`,
      labelX: (sourceX + targetX) / 2,
      labelY: sourceY,
      labelOrientation: "horizontal",
      direct: true,
    };
  }

  if (from.x !== to.x && Math.abs(to.x - from.x) <= 350) {
    const innerLeft = Math.min(sourceX, targetX) + 18,
      innerRight = Math.max(sourceX, targetX) - 18,
      preferredMiddle =
        (sourceX + targetX) / 2 + candidateCenteredLane * laneXSpacing,
      middleX = Math.max(innerLeft, Math.min(innerRight, preferredMiddle)),
      stagger = Math.abs(candidateCenteredLane) > 0.1,
      approachY =
        targetY +
        (stagger ? Math.sign(candidateCenteredLane) * Math.min(18, 8 + Math.abs(candidateCenteredLane) * 2) : 0),
      targetLeadX = forward
        ? Math.max(middleX, targetX - 18)
        : Math.min(middleX, targetX + 18),
      path = stagger
        ? `M ${sourceX} ${sourceY} H ${middleX} V ${approachY} H ${targetLeadX} V ${targetY} H ${targetX}`
        : `M ${sourceX} ${sourceY} H ${middleX} V ${targetY} H ${targetX}`;
    return {
      path,
      labelX: middleX,
      labelY: (sourceY + targetY) / 2,
      labelOrientation: "vertical",
      direct: false,
      local: true,
    };
  }

  const visualLane =
      (style.side < 0
        ? Math.max(0, routedTotal - routedLane - 1)
        : routedLane) + Math.abs(collisionNudge),
    corridorRank = routedLane + Math.abs(collisionNudge),
    baseCorridorOffset = 34 + (Math.abs(style.offset) % 4) * 5,
    // A fan of branches from one source is nested: its chronologically latest
    // (normally farthest) target turns first into the outer track, while an
    // earlier/nearer target turns later into an inner track.
    sourceCorridorOffset =
      baseCorridorOffset +
      Math.max(0, sourceTurnTotal - sourceTurnRank - 1) *
        corridorLaneSpacing,
    targetCorridorOffset =
      baseCorridorOffset + corridorRank * corridorLaneSpacing,
    sourceCorridorX = forward
      ? sourceX + sourceCorridorOffset
      : sourceX - sourceCorridorOffset,
    targetCorridorX = forward
      ? targetX - targetCorridorOffset
      : backward
        ? targetX + targetCorridorOffset
        : targetX - targetCorridorOffset,
    corridorLeft = Math.min(sourceCorridorX, targetCorridorX),
    corridorRight = Math.max(sourceCorridorX, targetCorridorX),
    relevant = [...positions.values()].filter(
      (point) => point.x < corridorRight && point.x + 220 > corridorLeft,
    ),
    upper = Math.min(...relevant.map((point) => point.y), from.y, to.y),
    lower = Math.max(
      ...relevant.map((point) => point.y + 104),
      from.y + 104,
      to.y + 104,
    ),
    upY = upper - 26 - Math.abs(Math.min(0, style.offset)) * 0.45,
    downY = lower + 26 + Math.max(0, style.offset) * 0.45,
    side =
      style.side ||
      (Math.abs(sourceY - upY) + Math.abs(targetY - upY) <=
      Math.abs(sourceY - downY) + Math.abs(targetY - downY)
        ? -1
        : 1),
    bendY =
      (side < 0 ? upY : downY) + side * visualLane * laneYSpacing;
  return {
    path: `M ${sourceX} ${sourceY} H ${sourceCorridorX} V ${bendY} H ${targetCorridorX} V ${targetY} H ${targetX}`,
    labelX: (sourceCorridorX + targetCorridorX) / 2,
    labelY: bendY,
    labelOrientation: "horizontal",
    direct: false,
    local: false,
  };
}

function relationPathSegments(path) {
  const tokens = String(path).match(/[MHV]|-?\d+(?:\.\d+)?/g) || [],
    segments = [];
  let index = 0,
    x = 0,
    y = 0;
  while (index < tokens.length) {
    const command = tokens[index++];
    if (command === "M") {
      x = Number(tokens[index++]);
      y = Number(tokens[index++]);
    } else if (command === "H") {
      const nextX = Number(tokens[index++]);
      segments.push({ x1: x, y1: y, x2: nextX, y2: y, vertical: false });
      x = nextX;
    } else if (command === "V") {
      const nextY = Number(tokens[index++]);
      segments.push({ x1: x, y1: y, x2: x, y2: nextY, vertical: true });
      y = nextY;
    }
  }
  return segments;
}

function relationSegmentConflictCost(left, right) {
  const axisTolerance = 12,
    minimumOverlap = 8;
  if (left.vertical === right.vertical) {
    if (left.vertical) {
      const overlap =
        Math.min(Math.max(left.y1, left.y2), Math.max(right.y1, right.y2)) -
        Math.max(Math.min(left.y1, left.y2), Math.min(right.y1, right.y2));
      const axisDistance = Math.abs(left.x1 - right.x1);
      return axisDistance <= axisTolerance && overlap >= minimumOverlap
        ? 1 +
            ((overlap - minimumOverlap) / 8) *
              (1 + (axisTolerance - axisDistance) / axisTolerance)
        : 0;
    }
    const overlap =
      Math.min(Math.max(left.x1, left.x2), Math.max(right.x1, right.x2)) -
      Math.max(Math.min(left.x1, left.x2), Math.min(right.x1, right.x2));
    const axisDistance = Math.abs(left.y1 - right.y1);
    return axisDistance <= axisTolerance && overlap >= minimumOverlap
      ? 1 +
          ((overlap - minimumOverlap) / 8) *
            (1 + (axisTolerance - axisDistance) / axisTolerance)
      : 0;
  }
  const vertical = left.vertical ? left : right,
    horizontal = left.vertical ? right : left,
    x = vertical.x1,
    y = horizontal.y1,
    crosses =
      x >= Math.min(horizontal.x1, horizontal.x2) &&
      x <= Math.max(horizontal.x1, horizontal.x2) &&
      y >= Math.min(vertical.y1, vertical.y2) &&
      y <= Math.max(vertical.y1, vertical.y2),
    nearEndpoint =
      Math.min(Math.abs(x - horizontal.x1), Math.abs(x - horizontal.x2)) < 12 ||
      Math.min(Math.abs(y - vertical.y1), Math.abs(y - vertical.y2)) < 12;
  return crosses && !nearEndpoint ? 1 : 0;
}

// A compact spatial index lets each fixed route candidate inspect only nearby
// segments. This avoids both relation-type special cases and all-pairs scans.
function createRelationSegmentIndex() {
  const cellSize = 96,
    margin = 12,
    buckets = new Map(),
    cellsFor = (segment) => {
      const left = Math.min(segment.x1, segment.x2) - margin,
        right = Math.max(segment.x1, segment.x2) + margin,
        top = Math.min(segment.y1, segment.y2) - margin,
        bottom = Math.max(segment.y1, segment.y2) + margin,
        cells = [];
      for (
        let x = Math.floor(left / cellSize);
        x <= Math.floor(right / cellSize);
        x++
      )
        for (
          let y = Math.floor(top / cellSize);
          y <= Math.floor(bottom / cellSize);
          y++
        )
          cells.push(`${x}:${y}`);
      return cells;
    };
  return {
    conflictScore(segments, edge) {
      let score = 0;
      segments.forEach((segment) => {
        const candidates = new Set();
        cellsFor(segment).forEach((key) =>
          (buckets.get(key) || []).forEach((entry) => candidates.add(entry)),
        );
        candidates.forEach((entry) => {
          const sameRelation = entry.edge.relation_code === edge.relation_code,
            sharedEndpoint =
              entry.edge.source_anime_id === edge.source_anime_id ||
              entry.edge.source_anime_id === edge.target_anime_id ||
              entry.edge.target_anime_id === edge.source_anime_id ||
              entry.edge.target_anime_id === edge.target_anime_id;
          score +=
            relationSegmentConflictCost(segment, entry.segment) *
            (sameRelation ? 8 : 1) *
            (sharedEndpoint ? 2 : 1);
        });
      });
      return score;
    },
    add(segments, edge) {
      segments.forEach((segment) => {
        const entry = { segment, edge };
        cellsFor(segment).forEach((key) => {
          if (!buckets.has(key)) buckets.set(key, []);
          buckets.get(key).push(entry);
        });
      });
    },
  };
}

function refineRelationRouting(
  edges,
  positions,
  portsByEdge,
  nodesById,
  initialMetadata,
) {
  const metadata = new Map(initialMetadata),
    selectedRoutes = new Map(),
    compare = (left, right) => {
      const leftSource = nodesById.get(left.source_anime_id),
        rightSource = nodesById.get(right.source_anime_id),
        leftTarget = nodesById.get(left.target_anime_id),
        rightTarget = nodesById.get(right.target_anime_id);
      return (
        String(leftSource?.start_month).localeCompare(
          String(rightSource?.start_month),
        ) ||
        String(leftTarget?.start_month).localeCompare(
          String(rightTarget?.start_month),
        ) ||
        Math.max(0, relationTrackPriority.indexOf(left.relation_code)) -
          Math.max(0, relationTrackPriority.indexOf(right.relation_code)) ||
        relationEdgeKey(left).localeCompare(relationEdgeKey(right))
      );
    },
    routeLength = (segments) =>
      segments.reduce(
        (total, segment) =>
          total +
          Math.abs(segment.x2 - segment.x1) +
          Math.abs(segment.y2 - segment.y1),
        0,
      ),
    routingOrder = [...edges].sort((left, right) => {
      const leftPriority = relationTrackPriority.indexOf(left.relation_code),
        rightPriority = relationTrackPriority.indexOf(right.relation_code);
      return (
        (leftPriority < 0 ? relationTrackPriority.length : leftPriority) -
          (rightPriority < 0 ? relationTrackPriority.length : rightPriority) ||
        compare(left, right)
      );
    }),
    candidateNudges = (edge, current) => {
      const from = positions.get(edge.source_anime_id),
        to = positions.get(edge.target_anime_id),
        key = relationEdgeKey(edge),
        baseRoute = routeRelationEdge(
          edge,
          from,
          to,
          positions,
          portsByEdge.get(key),
          current.rank,
          current.total,
          current.sourceTurnRank,
          current.sourceTurnTotal,
        ),
        centered = baseRoute.vertical || baseRoute.local,
        available = baseRoute.direct && !baseRoute.vertical
          ? [0]
          : centered
            ? [0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6, 7, -7, 8, -8]
            : [0, 1, 2, 3, 4, 5, 6, 7, 8],
        preferred = Number(current.collisionNudge || 0);
      return [preferred, ...available.filter((value) => value !== preferred)];
    },
    chooseRoute = (edge, current, segmentIndex) => {
      const from = positions.get(edge.source_anime_id),
        to = positions.get(edge.target_anime_id),
        key = relationEdgeKey(edge);
      return candidateNudges(edge, current)
        .map((nudge, order) => {
          const route = routeRelationEdge(
              edge,
              from,
              to,
              positions,
              portsByEdge.get(key),
              current.rank,
              current.total,
              current.sourceTurnRank,
              current.sourceTurnTotal,
              nudge,
            ),
            segments = relationPathSegments(route.path),
            collisionCost = segmentIndex.conflictScore(segments, edge),
            length = routeLength(segments),
            first = segments[0],
            last = segments[segments.length - 1],
            directDistance =
              Math.abs(last.x2 - first.x1) + Math.abs(last.y2 - first.y1),
            detour = Math.max(0, length - directDistance);
          return {
            nudge,
            route,
            segments,
            score:
              collisionCost * 110 +
              length +
              detour * 1.6 +
              Math.max(0, segments.length - 2) * 32 +
              Math.abs(nudge) * 18 +
              order * 3,
          };
        })
        .sort((left, right) => left.score - right.score)[0];
    },
    addSelectedRoute = (index, item) => index.add(item.segments, item.edge);

  // First pass is chronological and inexpensive. Each route avoids the fixed
  // routes before it through the spatial segment index.
  const initialIndex = createRelationSegmentIndex();
  routingOrder.forEach((edge) => {
    const from = positions.get(edge.source_anime_id),
      to = positions.get(edge.target_anime_id),
      key = relationEdgeKey(edge),
      current = metadata.get(key) || { rank: 0, total: 1 };
    if (!from || !to) return;
    const chosen = chooseRoute(edge, current, initialIndex),
      selected = { edge, segments: chosen.segments };
    metadata.set(key, { ...current, collisionNudge: chosen.nudge });
    selectedRoutes.set(key, selected);
    addSelectedRoute(initialIndex, selected);
  });

  // Two bounded coordinate-descent passes let an early route reconsider later
  // neighbors. This specifically shortens collinear sharing after a junction:
  // one crossing is cheap, while a long same-track overlap is charged by its
  // length. The spatial index keeps the extra work bounded on large series.
  for (let pass = 0; pass < 2; pass++) {
    let changed = false;
    routingOrder.forEach((edge) => {
      const key = relationEdgeKey(edge),
        current = metadata.get(key) || { rank: 0, total: 1 },
        otherIndex = createRelationSegmentIndex();
      selectedRoutes.forEach((item, otherKey) => {
        if (otherKey !== key) addSelectedRoute(otherIndex, item);
      });
      const chosen = chooseRoute(edge, current, otherIndex);
      if (chosen.nudge !== current.collisionNudge) changed = true;
      metadata.set(key, { ...current, collisionNudge: chosen.nudge });
      selectedRoutes.set(key, { edge, segments: chosen.segments });
    });
    if (!changed) break;
  }
  return metadata;
}

function routedRelationMetadata(edges, positions, portsByEdge, nodesById) {
  const outerGroups = [],
    contextGroups = [],
    verticalGroups = [],
    result = new Map();
  edges.forEach((edge) => {
    const from = positions.get(edge.source_anime_id),
      to = positions.get(edge.target_anime_id);
    if (!edge.grouping) {
      const item = {
          edge,
          left: Math.min(from.x, to.x),
          right: Math.max(from.x + 220, to.x + 220),
          top: Math.min(from.y, to.y),
          bottom: Math.max(from.y + 104, to.y + 104),
        },
        matching = contextGroups.filter((group) =>
          group.some(
            (other) =>
              other.edge.source_anime_id === edge.source_anime_id ||
              other.edge.target_anime_id === edge.target_anime_id ||
              (Math.min(other.right, item.right) >=
                Math.max(other.left, item.left) - 8 &&
                Math.min(other.bottom, item.bottom) >=
                  Math.max(other.top, item.top) - 8),
          ),
        );
      if (!matching.length) contextGroups.push([item]);
      else {
        matching[0].push(item);
        matching.slice(1).forEach((group) => {
          matching[0].push(...group);
          contextGroups.splice(contextGroups.indexOf(group), 1);
        });
      }
      return;
    }
    const route = routeRelationEdge(
      edge,
      from,
      to,
      positions,
      portsByEdge.get(relationEdgeKey(edge)),
    );
    if (route.direct && !route.vertical) return;
    if (route.vertical || route.local) {
      const pathSegments = relationPathSegments(route.path),
        verticalSegment = route.targetSegment
        ? {
            x1: route.targetSegment.x,
            y1: route.targetSegment.top,
            y2: route.targetSegment.bottom,
          }
        : pathSegments
            .filter((segment) => segment.vertical)
            .sort(
              (left, right) =>
                Math.abs(right.y2 - right.y1) - Math.abs(left.y2 - left.y1),
            )[0];
      if (!verticalSegment) return;
      const item = {
          edge,
          bypass: Boolean(route.bypass),
          targetId: edge.target_anime_id,
          centerX: verticalSegment.x1,
          top: Math.min(verticalSegment.y1, verticalSegment.y2),
          bottom: Math.max(verticalSegment.y1, verticalSegment.y2),
          horizontals: pathSegments
            .filter((segment) => !segment.vertical)
            .map((segment) => ({
              left: Math.min(segment.x1, segment.x2),
              right: Math.max(segment.x1, segment.x2),
              y: segment.y1,
            })),
        },
        matching = verticalGroups.filter((group) =>
          group.some(
            (other) =>
              other.targetId === item.targetId ||
              (Math.abs(other.centerX - item.centerX) <= 24 &&
                Math.min(other.bottom, item.bottom) >=
                  Math.max(other.top, item.top) - 4) ||
              other.horizontals.some((otherHorizontal) =>
                item.horizontals.some(
                  (horizontal) =>
                    Math.abs(otherHorizontal.y - horizontal.y) <= 24 &&
                    Math.min(otherHorizontal.right, horizontal.right) >=
                      Math.max(otherHorizontal.left, horizontal.left) - 4,
                ),
              ),
          ),
        );
      if (!matching.length) verticalGroups.push([item]);
      else {
        matching[0].push(item);
        matching.slice(1).forEach((group) => {
          matching[0].push(...group);
          verticalGroups.splice(verticalGroups.indexOf(group), 1);
        });
      }
      return;
    }
    const style = relationGeometry[edge.relation_code] || relationGeometry.other,
      item = {
        edge,
        side: style.side,
        left: Math.min(from.x, to.x),
        right: Math.max(from.x + 220, to.x + 220),
      },
      matching = outerGroups.filter((group) =>
        group.some(
          (other) =>
            other.side === item.side &&
            Math.min(other.right, item.right) >=
              Math.max(other.left, item.left) - 8,
        ),
      );
    if (!matching.length) outerGroups.push([item]);
    else {
      matching[0].push(item);
      matching.slice(1).forEach((group) => {
        matching[0].push(...group);
        outerGroups.splice(outerGroups.indexOf(group), 1);
      });
    }
  });
  const compareEdges = (left, right) => {
      const a = nodesById.get(left.source_anime_id),
        b = nodesById.get(right.source_anime_id),
        aTarget = nodesById.get(left.target_anime_id),
        bTarget = nodesById.get(right.target_anime_id);
      return (
        String(a?.start_month).localeCompare(String(b?.start_month)) ||
        String(aTarget?.start_month).localeCompare(
          String(bTarget?.start_month),
        ) ||
        left.source_anime_id - right.source_anime_id ||
        left.target_anime_id - right.target_anime_id
      );
    },
    assignGroup = (group) => {
    group.sort(compareEdges);
    group.forEach((edge, rank) =>
      result.set(relationEdgeKey(edge), { rank, total: group.length }),
    );
  };
  // All overlapping outer routes on the same side share one chronology-based
  // lane family, even when their targets or relation kinds differ. The oldest
  // source (then oldest target) gets the first visual track (top-to-bottom)
  // and the latest possible turn into its target, preventing side-story,
  // spin-off and other branch tracks from collapsing onto one another.
  outerGroups.forEach((group) => {
    assignGroup(group.map((item) => item.edge));
    const sourceFans = new Map();
    group.forEach((item) => {
      const key = `${item.side}:${item.edge.source_anime_id}`;
      if (!sourceFans.has(key)) sourceFans.set(key, []);
      sourceFans.get(key).push(item.edge);
    });
    sourceFans.forEach((fan) => {
      fan.sort(compareEdges);
      fan.forEach((edge, sourceTurnRank) => {
        const key = relationEdgeKey(edge),
          current = result.get(key);
        result.set(key, {
          ...current,
          sourceTurnRank,
          sourceTurnTotal: fan.length,
        });
      });
    });
  });
  // Gray cross-series connectors use the same conflict-component idea as
  // colored outer routes. Common endpoints or overlapping rectangles share a
  // chronological lane family, so every long connector receives its own
  // obstacle-scored track.
  contextGroups.forEach((group) => assignGroup(group.map((item) => item.edge)));
  verticalGroups.forEach((group) => {
    group.sort(
      (left, right) =>
        Number(right.bypass) - Number(left.bypass) ||
        compareEdges(left.edge, right.edge),
    );
    const center = (group.length - 1) / 2,
      laneRanks = [center];
    for (let step = 1; laneRanks.length < group.length; step++) {
      laneRanks.push(center - step);
      if (laneRanks.length < group.length) laneRanks.push(center + step);
    }
    group.forEach((item, index) =>
      result.set(relationEdgeKey(item.edge), {
        rank: laneRanks[index],
        total: group.length,
      }),
    );
  });
  return result;
}

function layoutRelationLabels(routedEdges, positions, width, height) {
  const groups = new Map(),
    placed = [],
    nodeRects = [...positions.values()].map((point) => ({
      left: point.x - 6,
      right: point.x + 226,
      top: point.y - 6,
      bottom: point.y + 110,
    })),
    lineSegments = routedEdges.flatMap((item) =>
      relationPathSegments(item.route.path).map((segment) => ({
        ...segment,
        code: item.edge.relation_code,
      })),
    ),
    overlapArea = (a, b) =>
      Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) *
      Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)),
    labelRect = (text, x, y) => {
      const unit = language === "en" ? 7 : 12,
        labelWidth = Math.max(42, Math.min(160, [...text].length * unit + 18)),
        labelHeight = 24;
      return {
        left: x - labelWidth / 2,
        right: x + labelWidth / 2,
        top: y - labelHeight / 2,
        bottom: y + labelHeight / 2,
      };
    };
  routedEdges.forEach((item) => {
    const text = relationLegendText(item.edge.relation_code);
    if (!text) return;
    if (!groups.has(item.edge.relation_code))
      groups.set(item.edge.relation_code, { text, items: [] });
    groups.get(item.edge.relation_code).items.push(item);
  });
  return [...groups.entries()]
    .sort(
      ([aCode, a], [bCode, b]) =>
        a.items.length - b.items.length || aCode.localeCompare(bCode),
    )
    .map(([code, group]) => {
      const candidates = [];
      group.items.forEach(({ route }, routeIndex) => {
        const vertical = route.labelOrientation === "vertical",
          nudges = vertical
            ? [[0, 0], [0, 24], [0, -24], [0, 48], [0, -48], [-24, 0], [24, 0]]
            : [[0, 0], [24, 0], [-24, 0], [48, 0], [-48, 0], [0, -24], [0, 24]];
        nudges.forEach(([offsetX, offsetY], nudgeIndex) => {
          const x = route.labelX + offsetX,
            y = route.labelY + offsetY,
            rect = labelRect(group.text, x, y),
            outside =
              Math.max(0, -rect.left) +
              Math.max(0, rect.right - width) +
              Math.max(0, -rect.top) +
              Math.max(0, rect.bottom - height),
            nodeOverlap = nodeRects.reduce(
              (total, node) => total + overlapArea(rect, node),
              0,
            ),
            labelOverlap = placed.reduce(
              (total, label) => total + overlapArea(rect, label),
              0,
            ),
            foreignLineOverlap = lineSegments
              .filter((segment) => segment.code !== code)
              .reduce((total, segment) => {
                const tolerance = segment.vertical ? 2 : 4,
                  lineRect = {
                    left: Math.min(segment.x1, segment.x2) - tolerance,
                    right: Math.max(segment.x1, segment.x2) + tolerance,
                    top: Math.min(segment.y1, segment.y2) - tolerance,
                    bottom: Math.max(segment.y1, segment.y2) + tolerance,
                  };
                return (
                  total +
                  overlapArea(rect, lineRect) * (segment.vertical ? 180 : 700)
                );
              }, 0);
          candidates.push({
            x,
            y,
            rect,
            score:
              outside * 100000 +
              nodeOverlap * 1000 +
              labelOverlap * 2000 +
              foreignLineOverlap +
              nudgeIndex * 12 +
              routeIndex,
          });
        });
      });
      candidates.sort((a, b) => a.score - b.score);
      const chosen = candidates[0];
      placed.push(chosen.rect);
      const relationClass = String(code).replace(/[^a-z_]/g, "other");
      return {
        code: relationClass,
        text: group.text,
        x: chosen.x,
        y: chosen.y,
        rect: chosen.rect,
      };
    });
}

function relationGraphBounds(positions, routedEdges, labels) {
  const bounds = {
    left: Infinity,
    right: -Infinity,
    top: Infinity,
    bottom: -Infinity,
  };
  const include = (left, top, right, bottom) => {
    bounds.left = Math.min(bounds.left, left);
    bounds.right = Math.max(bounds.right, right);
    bounds.top = Math.min(bounds.top, top);
    bounds.bottom = Math.max(bounds.bottom, bottom);
  };
  positions.forEach((point) =>
    include(point.x, point.y, point.x + 220, point.y + 104),
  );
  routedEdges.forEach(({ route }) =>
    relationPathSegments(route.path).forEach((segment) =>
      include(
        Math.min(segment.x1, segment.x2) - 5,
        Math.min(segment.y1, segment.y2) - 5,
        Math.max(segment.x1, segment.x2) + 14,
        Math.max(segment.y1, segment.y2) + 5,
      ),
    ),
  );
  labels.forEach(({ rect }) =>
    include(rect.left, rect.top, rect.right, rect.bottom),
  );
  return bounds;
}

function relationExportMarkup(stage) {
  const width = Math.ceil(parseFloat(stage.style.width) || stage.scrollWidth),
    height = Math.ceil(parseFloat(stage.style.height) || stage.scrollHeight),
    lineLayer = stage.querySelector(".relation-lines > g"),
    transform = lineLayer?.getAttribute("transform") || "",
    edgeElements = [...stage.querySelectorAll(".relation-edge:not(.is-hidden)")],
    markers = [],
    paths = edgeElements.map((edge, index) => {
      const path = edge.querySelector("path"),
        style = getComputedStyle(path),
        stroke = style.stroke || "#315e4d",
        markerId = `relation-export-arrow-${index}`;
      markers.push(`<marker id="${markerId}" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="${esc(stroke)}"/></marker>`);
      return `<path d="${esc(path.getAttribute("d"))}" fill="none" stroke="${esc(stroke)}" stroke-width="${esc(style.strokeWidth || "2.2")}" stroke-dasharray="${esc(style.strokeDasharray === "none" ? "" : style.strokeDasharray)}" stroke-linecap="round" stroke-linejoin="round" opacity="${esc(style.opacity || "1")}" marker-end="url(#${markerId})"/>`;
    }),
    labelSvg = [...stage.querySelectorAll(".relation-edge-label:not(.is-hidden)")]
      .map((labelElement) => {
        const x = parseFloat(labelElement.style.left),
          y = parseFloat(labelElement.style.top),
          boxWidth = Math.max(42, labelElement.offsetWidth || 70),
          boxHeight = Math.max(22, labelElement.offsetHeight || 24),
          style = getComputedStyle(labelElement);
        return `<g><rect x="${x - boxWidth / 2}" y="${y - boxHeight / 2}" width="${boxWidth}" height="${boxHeight}" rx="5" fill="${esc(style.backgroundColor || "#fffdf8")}" stroke="${esc(style.borderColor || "#c4cec8")}"/><text x="${x}" y="${y + 4}" text-anchor="middle" fill="${esc(style.color || "#315e4d")}" font-size="11" font-weight="700">${esc(labelElement.textContent)}</text></g>`;
      })
      .join(""),
    nodeSvg = [...stage.querySelectorAll(".relation-node")]
      .map((nodeElement) => {
        const x = parseFloat(nodeElement.style.left),
          y = parseFloat(nodeElement.style.top),
          style = getComputedStyle(nodeElement),
          input = nodeElement.querySelector('input[type="checkbox"]'),
          time = nodeElement.querySelector("time")?.textContent || "",
          title = nodeElement.querySelector("[data-graph-detail]")?.textContent || "",
          detail = nodeElement.querySelector("small")?.textContent || "",
          titleChars = [...title],
          firstLine = titleChars.slice(0, 25).join(""),
          secondLine = titleChars.length > 25
            ? `${titleChars.slice(25, 48).join("")}${titleChars.length > 48 ? "…" : ""}`
            : "",
          checkbox = input
            ? `<rect x="${x + 12}" y="${y + 12}" width="14" height="14" rx="2" fill="${input.checked ? "#245e4b" : "rgba(255,255,255,.86)"}" stroke="#245e4b"/>${input.checked ? `<path d="M ${x + 15} ${y + 19} l 3 3 l 6 -7" fill="none" stroke="#fff" stroke-width="2"/>` : ""}`
            : "",
          chipTexts = [...nodeElement.querySelectorAll(".relation-subject-chip")].map((chip) => chip.textContent.trim()),
          risk = nodeElement.querySelector(".relation-risk")
            ? `<circle cx="${x + 200}" cy="${y + 90}" r="8" fill="#c46b22"/><text x="${x + 200}" y="${y + 94}" text-anchor="middle" fill="#fff" font-size="10" font-weight="900">!</text>`
            : "",
          chipSvg = (() => {
            let cursor = x + 208;
            return chipTexts.reverse().map((text) => {
              const chipWidth = Math.max(20, [...text].length * (language === "en" ? 4.5 : 8) + 8);
              cursor -= chipWidth;
              const markup = `<rect x="${cursor}" y="${y + 11}" width="${chipWidth}" height="16" rx="3" fill="#ebf6ef" stroke="#92ad9d"/><text x="${cursor + chipWidth / 2}" y="${y + 22}" text-anchor="middle" fill="#315b45" font-size="8" font-weight="700">${esc(text)}</text>`;
              cursor -= 3;
              return markup;
            }).join("");
          })();
        return `<g><rect x="${x}" y="${y}" width="220" height="104" rx="9" fill="${esc(style.backgroundColor || "#fff")}" stroke="${esc(style.borderColor || "#c9d0ca")}" stroke-width="${nodeElement.classList.contains("root") ? "2" : "1"}"/>${checkbox}<text x="${x + 34}" y="${y + 23}" fill="#6b756f" font-size="11">${esc(time)}</text>${chipSvg}<text x="${x + 12}" y="${y + 51}" fill="#162a23" font-size="12" font-weight="700"><tspan x="${x + 12}" dy="0">${esc(firstLine)}</tspan>${secondLine ? `<tspan x="${x + 12}" dy="15">${esc(secondLine)}</tspan>` : ""}</text><text x="${x + 12}" y="${y + 91}" fill="#6b756f" font-size="10">${esc([...detail].slice(0, risk ? 29 : 38).join(""))}</text>${risk}</g>`;
      })
      .join("");
  return {
    width,
    height,
    markup: `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="#fffdf8"/><defs>${markers.join("")}</defs><g transform="${esc(transform)}">${paths.join("")}</g>${labelSvg}${nodeSvg}</svg>`,
  };
}

function downloadRelationBlob(blob, name) {
  const url = URL.createObjectURL(blob),
    link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function safeRelationFilename(graph) {
  const root = graph.nodes.find((node) => node.id === graph.rootAnimeId),
    title = root ? graphTitle(root) : "anime-series";
  return String(title || "anime-series")
    .replace(/[\\/:*?"<>|]+/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 100);
}

function exportRelationGraph(graph, type) {
  const stage = $("relationGraph").querySelector(".relation-stage");
  if (!stage) return;
  const exported = relationExportMarkup(stage),
    svgBlob = new Blob([exported.markup], {
      type: "image/svg+xml;charset=utf-8",
    }),
    filename = safeRelationFilename(graph);
  if (type === "svg") {
    downloadRelationBlob(svgBlob, `${filename}.svg`);
    return;
  }
  const url = URL.createObjectURL(svgBlob),
    image = new Image();
  image.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = exported.width;
    canvas.height = exported.height;
    const context = canvas.getContext("2d");
    context.fillStyle = "#fffdf8";
    context.fillRect(0, 0, canvas.width, canvas.height);
    try {
      context.drawImage(image, 0, 0, exported.width, exported.height);
      canvas.toBlob(
        (blob) => blob && downloadRelationBlob(blob, `${filename}.png`),
        "image/png",
      );
    } finally {
      URL.revokeObjectURL(url);
    }
  };
  image.onerror = () => URL.revokeObjectURL(url);
  image.src = url;
}

function renderRelationGraph(graph) {
  if (!graph.nodes.length) {
    $("relationGraph").innerHTML =
      `<p class="empty">${t("graphUnavailable")}</p>`;
    return;
  }
  const positions = graphPositions(graph),
    provisionalWidth = Math.max(
      760,
      ...[...positions.values()].map((item) => item.x + 300),
    ),
    provisionalHeight = Math.max(
      430,
      ...[...positions.values()].map(
        (item) =>
          item.y + 200 + Math.max(0, relationLaneCapacity(graph, 1) - 1) * 10,
      ),
    ),
    nodesById = new Map(graph.nodes.map((node) => [node.id, node])),
    uniqueEdges = graph.edges.filter(
        (edge, index, all) =>
          index ===
          all.findIndex(
            (candidate) =>
              candidate.source_anime_id === edge.source_anime_id &&
              candidate.target_anime_id === edge.target_anime_id &&
              candidate.relation_code === edge.relation_code,
          ),
      ),
    orderedEdges = [...uniqueEdges].sort((left, right) => {
      const a = nodesById.get(left.source_anime_id),
        b = nodesById.get(right.source_anime_id);
      return (
        String(a?.start_month).localeCompare(String(b?.start_month)) ||
        left.source_anime_id - right.source_anime_id ||
        left.target_anime_id - right.target_anime_id ||
        left.relation_code.localeCompare(right.relation_code)
      );
    }),
    portsByEdge = assignRelationPorts(orderedEdges, positions, nodesById),
    initialRoutedMetadata = routedRelationMetadata(
      orderedEdges,
      positions,
      portsByEdge,
      nodesById,
    ),
    routedMetadata = refineRelationRouting(
      orderedEdges,
      positions,
      portsByEdge,
      nodesById,
      initialRoutedMetadata,
    ),
    routedEdges = orderedEdges
      .map((edge) => {
        const from = positions.get(edge.source_anime_id),
          to = positions.get(edge.target_anime_id);
        if (!from || !to) return "";
        const routed = routedMetadata.get(relationEdgeKey(edge)) || {
            rank: 0,
            total: 1,
          },
          route = routeRelationEdge(
            edge,
            from,
            to,
            positions,
            portsByEdge.get(relationEdgeKey(edge)),
            routed.rank,
            routed.total,
            routed.sourceTurnRank,
            routed.sourceTurnTotal,
            routed.collisionNudge,
          ),
          relationClass = String(edge.relation_code).replace(/[^a-z_]/g, "other");
        return { edge, route, relationClass };
      })
      .filter(Boolean),
    labelPlacements = layoutRelationLabels(
      routedEdges,
      positions,
      provisionalWidth,
      provisionalHeight,
    ),
    bounds = relationGraphBounds(positions, routedEdges, labelPlacements),
    safePadding = 28,
    shiftX = safePadding - bounds.left,
    shiftY = safePadding - bounds.top,
    width = Math.max(520, Math.ceil(bounds.right - bounds.left + safePadding * 2)),
    height = Math.max(
      300,
      Math.ceil(bounds.bottom - bounds.top + safePadding * 2),
    ),
    edges = routedEdges
      .map(
        ({ edge, route, relationClass }) =>
          `<g class="relation-edge relation-${relationClass} ${relationHiddenCodes.has(edge.relation_code) ? "is-hidden" : ""} ${route.direct ? "direct" : "routed"} ${edge.grouping ? "grouping" : "context"}" data-source="${edge.source_anime_id}" data-target="${edge.target_anime_id}"><path d="${route.path}" marker-end="url(#relation-arrow)"/></g>`,
      )
      .join(""),
    edgeLabels = labelPlacements
      .map(
        (item) =>
          `<span class="relation-edge-label relation-${item.code} ${relationHiddenCodes.has(item.code) ? "is-hidden" : ""}" style="left:${item.x + shiftX}px;top:${item.y + shiftY}px">${esc(item.text)}</span>`,
      )
      .join(""),
    legend = relationTrackCodes(graph)
      .map((code) => ({
        code,
        text: relationLegendText(code),
      }))
      .filter((item) => item.text)
      .map(
        (item) =>
          `<button type="button" class="relation-key relation-${esc(item.code)} ${relationHiddenCodes.has(item.code) ? "is-hidden" : ""}" data-relation-toggle="${esc(item.code)}" aria-pressed="${relationHiddenCodes.has(item.code) ? "false" : "true"}" title="${esc(item.text)}"><i aria-hidden="true"></i><span>${esc(item.text)}</span></button>`,
      )
      .join(""),
    nodes = graph.nodes
      .map((node) => {
        const point = positions.get(node.id),
          canSelect = node.selectable,
          status = node.library_state || "not_in_library";
        return `<article class="relation-node ${node.strict_member ? "series" : "context"} ${node.id === graph.rootAnimeId ? "root" : ""}" data-graph-node="${node.id}" style="left:${point.x + shiftX}px;top:${point.y + shiftY}px"><div class="relation-node-head"><input type="checkbox" data-graph-select="${node.id}" ${selected.has(node.id) ? "checked" : ""} ${canSelect ? "" : "disabled"} aria-label="${esc(canSelect ? t("graphSelected") : t("graphUnavailableSelect"))}" title="${esc(canSelect ? t("graphSelected") : t("graphUnavailableSelect"))}"><time title="${esc(localMonth(node.start_month))}">${esc(relationMonth(node.start_month))}</time><span class="relation-subject-chips">${relationSubjectChips(node)}</span></div><button type="button" data-graph-detail="${node.id}">${esc(graphTitle(node))}</button><div class="relation-node-foot"><small>${esc(label("media", node.media_code))} · ${esc(t({ local: "localLibrary", external: "externalReadOnly", submitted: "submittedDownload", not_in_library: "notInLibrary" }[status] || "notInLibrary"))}${node.strict_member ? "" : ` · ${esc(t("graphContext"))}`}</small>${node.selection_warning ? `<span class="relation-risk" title="${esc(t("graphExistingWarning"))}">!</span>` : ""}</div></article>`;
      })
      .join("");
  $("relationGraph").innerHTML =
    `<header class="relation-heading"><div><p class="eyebrow">${esc(t("relationGraph"))}</p><h2>${esc(graphTitle(nodesById.get(graph.rootAnimeId)) || graph.seriesTitle)}</h2><p>${t("relationHint")}</p>${graph.contextTruncated ? `<p class="relation-context-note">${t("graphContextTruncated")}</p>` : ""}<div class="relation-legend">${legend}</div></div><div class="relation-heading-actions"><b>${esc(t("relatedWorksCount").replace("{count}", fmt(graph.strictMemberCount)))}</b><div class="relation-export-actions"><button type="button" class="relation-fullscreen" data-relation-export="png" data-icon="download" title="${esc(t("exportPng"))}" aria-label="${esc(t("exportPng"))}">PNG</button><button type="button" class="relation-fullscreen" data-relation-export="svg" data-icon="download" title="${esc(t("exportSvg"))}" aria-label="${esc(t("exportSvg"))}">SVG</button><button type="button" class="relation-fullscreen" data-relation-fullscreen data-icon="expand">${t("fullscreenGraph")}</button></div></div></header><div class="relation-scroll"><div class="relation-stage" style="width:${width}px;height:${height}px"><svg class="relation-lines" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true"><defs><marker id="relation-arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z"/></marker></defs><g transform="translate(${shiftX} ${shiftY})">${edges}</g></svg>${edgeLabels}${nodes}</div></div>`;
  const fullscreenButton = $("relationGraph").querySelector(
    "[data-relation-fullscreen]",
  );
  const updateFullscreenLabel = () =>
    (fullscreenButton.textContent = t(
      relationDialog.classList.contains("expanded")
        ? "exitFullscreen"
        : "fullscreenGraph",
    ));
  fullscreenButton.onclick = () => {
    relationDialog.classList.toggle("expanded");
    updateFullscreenLabel();
  };
  $("relationGraph")
    .querySelectorAll("[data-relation-export]")
    .forEach(
      (button) =>
        (button.onclick = () =>
          exportRelationGraph(graph, button.dataset.relationExport)),
    );
  $("relationGraph")
    .querySelectorAll("[data-relation-toggle]")
    .forEach(
      (button) =>
        (button.onclick = () => {
          const code = button.dataset.relationToggle,
            hidden = !relationHiddenCodes.has(code);
          if (hidden) relationHiddenCodes.add(code);
          else relationHiddenCodes.delete(code);
          button.classList.toggle("is-hidden", hidden);
          button.setAttribute("aria-pressed", hidden ? "false" : "true");
          $("relationGraph")
            .querySelectorAll(`.relation-stage .relation-${code}`)
            .forEach((element) => element.classList.toggle("is-hidden", hidden));
        }),
    );
  $("relationGraph")
    .querySelectorAll("[data-related-node]")
    .forEach((button) => {
      button.onmouseenter = () => {
        if (!relationSubjectPopoverPinned) showRelationSubjectPopover(button, graph, false);
      };
      button.onfocus = () => {
        if (!relationSubjectPopoverPinned) showRelationSubjectPopover(button, graph, false);
      };
      button.onmouseleave = scheduleRelationSubjectPopoverHide;
      button.onblur = scheduleRelationSubjectPopoverHide;
      button.onclick = (event) => {
        event.stopPropagation();
        const key = `${button.dataset.relatedNode}:${button.dataset.relatedCategory}`,
          samePinned = relationSubjectPopoverPinned && relationSubjectPopoverKey === key;
        if (samePinned) {
          relationSubjectPopoverPinned = false;
          relationSubjectPopoverKey = "";
          $("relationSubjectPopover").hidden = true;
        } else {
          showRelationSubjectPopover(button, graph, true);
        }
      };
    });
  const relatedPopover = $("relationSubjectPopover");
  relatedPopover.onmouseenter = () => clearTimeout(relationSubjectHideTimer);
  relatedPopover.onmouseleave = scheduleRelationSubjectPopoverHide;
  $("relationGraph")
    .querySelectorAll("[data-graph-detail]")
    .forEach(
      (button) =>
        (button.onclick = () => {
          relationDialog.close();
          showDetail(+button.dataset.graphDetail);
        }),
    );
  $("relationGraph")
    .querySelectorAll("[data-graph-select]")
    .forEach(
      (input) =>
        (input.onchange = () => {
          const id = +input.dataset.graphSelect,
            node = nodesById.get(id),
            affected = new Set([id]);
          if (input.checked && node?.preferred_collection) {
            graph.nodes
              .filter(
                (candidate) =>
                  candidate.strict_member &&
                  candidate.selectable &&
                  candidate.eligible_info_hashes?.includes(
                    node.preferred_info_hash,
                  ),
              )
              .forEach((candidate) => {
                affected.add(candidate.id);
                torrentSelections.set(candidate.id, node.preferred_info_hash);
              });
          }
          affected.forEach((animeId) => {
            if (input.checked) selected.add(animeId);
            else {
              selected.delete(animeId);
              torrentSelections.delete(animeId);
            }
          });
          updateSelection();
          renderRelationGraph(graph);
          render();
        }),
    );
}

async function showRelationGraph(id) {
  $("relationGraph").innerHTML = `<div class="empty">${t("loading")}</div>`;
  relationSubjectPopoverPinned = false;
  relationSubjectPopoverKey = "";
  $("relationSubjectPopover").hidden = true;
  relationDialog.classList.remove("expanded");
  relationHiddenCodes.clear();
  showModalDialog(relationDialog);
  try {
    renderRelationGraph(await api(`/api/anime/${id}/relations/graph?language=${encodeURIComponent(language)}`));
  } catch (error) {
    $("relationGraph").innerHTML =
      `<p class="error">${t("queryFailed")}: ${esc(error.message)}</p>`;
  }
}

function summaryLanguage(value) {
  const text = String(value || ""),
    kana = (text.match(/[\u3040-\u30ff]/g) || []).length,
    hangul = (text.match(/[\uac00-\ud7af]/g) || []).length,
    cyrillic = (text.match(/[\u0400-\u052f]/g) || []).length,
    latin = (text.match(/[A-Za-z]/g) || []).length,
    han = (text.match(/[\u3400-\u9fff]/g) || []).length;
  if (kana >= 4 && kana >= latin / 2) return "ja";
  if (hangul >= 4 && hangul >= latin / 2) return "ko";
  if (cyrillic >= 4 && cyrillic >= latin / 2) return "ru";
  if (latin > Math.max(20, han * 1.5)) return "en";
  if (han) return "zh";
  return "und";
}

function summarySimilarity(left, right) {
  const normalize = (value) => String(value || "").normalize("NFKC").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ""),
    a = normalize(left),
    b = normalize(right);
  if (!a || !b) return 0;
  if (a === b) return 1;
  const grams = (value) => {
    const result = new Map();
    for (let i = 0; i < value.length - 1; i += 1) {
      const key = value.slice(i, i + 2);
      result.set(key, (result.get(key) || 0) + 1);
    }
    return result;
  };
  const ga = grams(a), gb = grams(b);
  let overlap = 0;
  ga.forEach((count, key) => (overlap += Math.min(count, gb.get(key) || 0)));
  return (2 * overlap) / Math.max(1, a.length + b.length - 2);
}

function localizedSummary(x) {
  const raw = String(x.summary || "").trim();
  if (!raw) return "";
  const marked = raw.split(/\s*\[(?:简介原文|簡介原文|原文|original summary)\]\s*/i).filter(Boolean);
  let candidates = marked.length > 1 ? marked : raw.split(/\n{3,}/).filter(Boolean);
  candidates = candidates.filter(
    (value, index) => !candidates.slice(0, index).some((prior) => summarySimilarity(prior, value) >= 0.92),
  );
  if (marked.length <= 1) return candidates.join("\n\n");
  const desired = languageBase(language),
    original = languageBase(x.original_language || "ja");
  return candidates.find((value) => summaryLanguage(value) === desired)
    || candidates.find((value) => summaryLanguage(value) === original)
    || candidates[0];
}

function playbackHtml(state) {
  if (state?.loading) return `<section class="playback-panel">${state.viewing ? viewingButton(state.viewing.animeId, state.viewing) : ""}<p class="muted" role="status">${esc(t("radarLoading"))}</p></section>`;
  if (state?.viewing) updateViewing(state.viewing);
  const versions = (state.editions || []).length > 1 ? `<label class="playback-source-picker"><span>${t("playbackEdition")}</span><select id="playbackEdition"><option value="">${t("playbackAutomatic")}</option>${state.editions.map(item => `<option value="${esc(item.id)}" ${item.id === state.selectedEdition ? "selected" : ""}>${esc(item.label)} · ${item.count}</option>`).join("")}</select></label>` : "";
  if (!state?.available)
    return `<section class="playback-panel unavailable">${versions}${state?.viewing ? viewingButton(state.viewing.animeId, state.viewing) : ""}<p class="muted">${esc(state?.error || t(state?.librarySleeping ? "librarySleeping" : state?.indexing ? 'indexingLocalMedia' : "noPlayableMedia"))}</p></section>`;
  const episodeOptions = (state.items || []).map((item) => `<option value="${Number(item.index)}">${esc(item.title)}</option>`).join("");
  return `<section class="playback-panel">${versions}<div class="playback-scope"><select id="playbackEpisode" aria-label="${t("playbackStartFile")}">${episodeOptions}</select>${state.viewing ? viewingButton(state.viewing.animeId, state.viewing) : ""}</div><div class="playback-actions">${playerActionsHtml("data-player")}</div></section>`;
}

const viewingRecords = new Map();
let viewingEditId = 0, viewingEditRevision = 0, viewingUndoRecord = null, viewingGeneration = 0;
let viewingLatestEpisode = null;
let viewingBusy = false;
let catchupRequest = null;
let applicationUpdateBusy = false;
let catchupGroups = [], catchupBusy = false;
const catchupSelected = new Set();
function viewingLabel(record) {
  return record?.episode != null ? t("viewingResume").replace("{episode}", String(record.episode))
    + (record.positionMs ? ` · ${positionText(record.positionMs)}` : "") : t("viewingNew");
}
function positionText(ms) {
  const seconds = Math.max(0, Math.floor(Number(ms) / 1000));
  return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map(x => String(x).padStart(2, "0")).join(":");
}
function positionValue(text) {
  if (!text.trim()) return 0;
  if (!/^\d{1,3}:[0-5]\d:[0-5]\d$/.test(text.trim())) throw new Error(t("viewingPosition"));
  return text.split(":").reduce((sum, part) => sum * 60 + Number(part), 0) * 1000;
}
function viewingButton(animeId, record, bgmId = record?.bgmId, compact = false) {
  const text = compact ? record?.episode != null ? fmt(record.episode) : "—" : viewingLabel(record);
  return `<button type="button" class="text-button viewing-button" data-viewing="${animeId}" data-bgm-id="${Number(bgmId) || 0}"${compact ? ' data-viewing-compact="true"' : ""} title="${esc(t("viewingEdit"))} · ${esc(viewingLabel(record))}">${esc(text)}</button>`;
}
function updateViewing(record) {
  const previous = viewingRecords.get(record.animeId);
  if (previous && (previous.revision !== record.revision || previous.epoch !== record.epoch)) invalidateCatalogPages();
  viewingRecords.set(record.animeId, record);
  document.querySelectorAll(`[data-viewing="${record.animeId}"]`).forEach((button) => { button.textContent = button.dataset.viewingCompact ? record.episode != null ? fmt(record.episode) : "—" : viewingLabel(record); button.title = `${t("viewingEdit")} · ${viewingLabel(record)}`; });
}
async function openViewing(animeId, title = "", bgmId = 0) {
  const generation = ++viewingGeneration;
  viewingBusy = false;
  viewingEditId = animeId; viewingUndoRecord = null;
  $("viewingUndo").hidden = true;
  $("viewingSave").disabled = $("viewingClear").disabled = $('viewingComplete').disabled = true;
  viewingLatestEpisode = null;
  $("viewingWork").textContent = title;
  $("viewingEpisode").value = "";
  $("viewingEpisodes").replaceChildren();
  $("viewingStatus").textContent = t("radarLoading");
  showModalDialog($("viewingDialog"));
  try {
    const record = await api(`/api/anime/${animeId}/viewing`);
    if (generation !== viewingGeneration || !$("viewingDialog").open) return;
    if (bgmId && record.bgmId !== bgmId) throw new Error(t("viewingConflict"));
    updateViewing(record);
    viewingEditRevision = record.revision;
    viewingLatestEpisode = record.latestEpisode;
    $("viewingEpisode").value = record.episode ?? "";
    $("viewingPosition").value = positionText(record.positionMs || 0);
    $("viewingStatus").textContent = "";
    $("viewingSave").disabled = $("viewingClear").disabled = false;
    $('viewingComplete').disabled = viewingLatestEpisode == null;
    $("viewingEpisode").focus();
    // Suggestions are optional: an unavailable source must not block correction.
    api(`/api/anime/${animeId}/playback`).then((state) => {
      if (generation !== viewingGeneration || !$("viewingDialog").open) return;
      $("viewingEpisodes").innerHTML = [...new Set((state.items || []).map((item) => item.episode).filter((value) => value != null))]
        .map((value) => `<option value="${Number(value)}"></option>`).join("");
    }).catch(() => {});
  } catch (error) { $("viewingStatus").textContent = error.message; }
}
async function saveViewing(episode, undo = false, positionMs = 0, completeLatest = false) {
  if (viewingBusy) return;
  viewingBusy = true;
  const generation = viewingGeneration;
  $("viewingSave").disabled = $("viewingClear").disabled = $("viewingUndo").disabled = $('viewingComplete').disabled = true;
  try {
    const record = await api(`/api/anime/${viewingEditId}/viewing`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode, positionMs, completeLatest, latestEpisode: viewingLatestEpisode,
        ...(undo ? {restoreCompleted: viewingUndoRecord.latestCompleted} : {}),
        revision: viewingEditRevision, bgmId: viewingRecords.get(viewingEditId)?.bgmId }) });
    updateViewing(record);
    if (generation !== viewingGeneration) return;
    if ($("radarDialog").open) radarRefreshPending = true;
    viewingEditRevision = record.revision;
    viewingUndoRecord = { episode: record.previousEpisode, positionMs: record.previousPositionMs,
      latestCompleted: record.previousLatestCompleted };
    $("viewingEpisode").value = record.episode ?? "";
    $("viewingPosition").value = positionText(record.positionMs || 0);
    $("viewingUndo").hidden = undo;
    $("viewingStatus").textContent = t("viewingSaved");
    if ($('catchupDialog').open) { $('catchupDialog').close(); }
  } catch (error) {
    $("viewingStatus").textContent = error.message.includes("viewing_conflict") ? t("viewingConflict") : error.message;
  } finally {
    if (generation === viewingGeneration) {
      viewingBusy = false;
      $("viewingSave").disabled = $("viewingClear").disabled = $("viewingUndo").disabled = false;
      $('viewingComplete').disabled = viewingLatestEpisode == null;
    }
  }
}
function playerActionsHtml(attribute = "data-catchup-player") {
  const button = (kind, key) => `<button type="button" class="tool dark-tool player-action" ${attribute}="${kind}"${kind === "copy" ? ' data-icon="copy"' : kind === "system" ? ' data-icon="download"' : ""}>${playerIcon(kind)}<span>${esc(t(key))}</span></button>`;
  const windows = /Windows/i.test(navigator.userAgent), mac = /(Macintosh|Mac OS X)/i.test(navigator.userAgent);
  if (windows) return `<div class="player-grid">${button("potplayer", "openPotPlayer")}${button("vlc", "openVlc")}${button("copy", "copyPlaylist")}<button class="tool dark-tool extension-install" type="button" data-install-playback-extension title="${esc(t("potplayerHint"))}">${esc(t("potplayerInstall"))}</button><button class="tool dark-tool player-action" type="button" data-copy-browser-permission data-icon="copy" title="${esc(t("browserPermissionHint"))}">${esc(t("copyBrowserPermission"))}</button>${button("system", "systemPlayer")}</div>`;
  const primary = mac ? button("iina", "openIina") : "";
  return `${primary}<div class="player-utilities">${button("vlc", "openVlc")}${button("copy", "copyPlaylist")}${button("system", "systemPlayer")}</div>`;
}
async function deliverPlayback(kind, handoff, button) {
  if (kind === "copy") {
    if (!await copyPlainText(handoff.playlistUrl || handoff.url)) return;
    const previous = button.innerHTML;
    button.textContent = t("copiedPlaylist");
    setTimeout(() => { if (button.isConnected) button.innerHTML = previous; }, 1800);
  } else if (kind === "system") {
    const anchor = document.createElement("a"), url = new URL(handoff.playlistUrl || handoff.url);
    url.searchParams.set("download", "1"); anchor.href = url.href; anchor.download = "AnimeMachine.m3u";
    document.body.append(anchor); anchor.click(); anchor.remove();
  } else {
    if (!handoff.protocolUrl) throw new Error(t("playbackHandoffUnavailable"));
    location.href = handoff.protocolUrl;
  }
}
function catchupSelection() {
  return catchupGroups.filter((group) => catchupSelected.has(group.bgmId)).map((group) => group.bgmId);
}
function catchupRangeValid(group) {
  const episodes = group.availableEpisodes;
  return Number.isFinite(group.rangeFrom) && Number.isFinite(group.rangeTo)
    && group.rangeFrom >= episodes[0] && group.rangeTo <= episodes[episodes.length - 1]
    && group.rangeFrom <= group.rangeTo
    && episodes.some((episode) => episode >= group.rangeFrom && episode <= group.rangeTo);
}
function updateCatchupControls(showCount = true) {
  const chosen = catchupGroups.filter((group) => catchupSelected.has(group.bgmId));
  const valid = chosen.every((group) => catchupRangeValid(group) && group.count > 0);
  $("catchupPlayers").querySelectorAll("button").forEach((button) => { button.disabled = catchupBusy || !chosen.length || !valid; });
  $("catchupList").querySelectorAll("input, button").forEach((control) => { control.disabled = catchupBusy; });
  $("catchupList").querySelectorAll("li[data-catchup-id]").forEach((row) => {
    row.draggable = !catchupBusy;
    row.classList.toggle("selected", catchupSelected.has(Number(row.dataset.catchupId)));
  });
  if (showCount && catchupGroups.length) $("catchupStatus").textContent = valid
    ? t("catchupSelectedCount").replace("{works}", chosen.length).replace("{episodes}", chosen.reduce((sum, group) => sum + group.count, 0))
    : t("catchupRangeInvalid");
}
function renderCatchupGroups() {
  $("catchupList").innerHTML = `<ul class="catchup-list">${catchupGroups.map((group) => `<li draggable="true" data-catchup-id="${group.bgmId}"><button class="catchup-drag text-button" type="button" aria-label="${esc(group.title)} · ${esc(t("catchupDrag"))}" title="${esc(t("catchupDrag"))}"><svg viewBox="0 0 16 24" aria-hidden="true"><path d="M5 5h.01M11 5h.01M5 12h.01M11 12h.01M5 19h.01M11 19h.01" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg></button><label class="catchup-choice"><input type="checkbox" value="${group.bgmId}"${catchupSelected.has(group.bgmId) ? " checked" : ""}><span class="catchup-title" title="${esc(group.title)}">${esc(group.title)}</span></label><span class="catchup-range"><label>${esc(t("catchupRangeFromShort"))}<input type="number" aria-label="${esc(t("catchupRangeFrom"))}" data-range="from" min="${group.availableEpisodes[0]}" max="${group.availableEpisodes.at(-1)}" step="any" value="${group.rangeFrom ?? ""}"></label><span>–</span><label>${esc(t("catchupRangeToShort"))}<input type="number" aria-label="${esc(t("catchupRangeTo"))}" data-range="to" min="${group.availableEpisodes[0]}" max="${group.availableEpisodes.at(-1)}" step="any" value="${group.rangeTo ?? ""}"></label></span>${viewingButton(group.animeId, { episode: group.episode, bgmId: group.bgmId, positionMs: group.positionMs })}</li>`).join("")}</ul><p class="muted catchup-resume-hint">${esc(t("catchupHint"))}</p>`;
  updateCatchupControls();
}
$("catchupList").addEventListener("input", (event) => {
  const input = event.target.closest(".catchup-range input");
  if (!input || catchupBusy) return;
  const group = catchupGroups.find((value) => value.bgmId === Number(input.closest("li").dataset.catchupId));
  if (!group) return;
  const value = input.value.trim() && input.checkValidity() ? Number(input.value) : NaN;
  if (input.dataset.range === "from") group.rangeFrom = value;
  else group.rangeTo = value;
  group.rangeEdited = true;
  group.episodes = catchupRangeValid(group) ? group.availableEpisodes.filter((ep) => ep >= group.rangeFrom && ep <= group.rangeTo) : [];
  group.count = group.episodes.length;
  updateCatchupControls();
});
function moveCatchup(identity, index) {
  if (catchupBusy) return;
  const from = catchupGroups.findIndex((group) => group.bgmId === identity);
  if (from < 0 || index < 0 || index >= catchupGroups.length || from === index) return;
  const [group] = catchupGroups.splice(from, 1);
  catchupGroups.splice(index, 0, group);
  const list = $("catchupList").querySelector("ul"), row = list.querySelector(`[data-catchup-id="${identity}"]`);
  list.insertBefore(row, from < index ? list.children[index].nextSibling : list.children[index]);
}
$("catchupList").addEventListener("change", (event) => {
  const input = event.target.closest('.catchup-choice input');
  if (!input || catchupBusy) return;
  input.checked ? catchupSelected.add(Number(input.value)) : catchupSelected.delete(Number(input.value));
  updateCatchupControls();
});
$("catchupList").addEventListener("keydown", (event) => {
  const handle = event.target.closest(".catchup-drag");
  if (!handle || catchupBusy || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
  event.preventDefault();
  const identity = Number(handle.closest("li").dataset.catchupId);
  moveCatchup(identity, catchupGroups.findIndex((group) => group.bgmId === identity) + (event.key === "ArrowUp" ? -1 : 1));
  handle.focus({ preventScroll: true });
});
let catchupDragId = null, catchupTouchDrag = null, catchupDragBlocked = false;
$("catchupList").addEventListener("dragstart", (event) => {
  const row = event.target.closest("li[data-catchup-id]");
  if (!row || catchupBusy || catchupDragBlocked) { event.preventDefault(); return; }
  catchupDragId = Number(row.dataset.catchupId);
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", String(catchupDragId));
  row.classList.add("dragging");
});
$("catchupList").addEventListener("dragover", (event) => {
  const row = event.target.closest("li[data-catchup-id]");
  if (catchupDragId == null || !row || catchupBusy) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  $("catchupList").querySelectorAll(".drop-target").forEach((item) => item.classList.remove("drop-target"));
  if (Number(row.dataset.catchupId) !== catchupDragId) row.classList.add("drop-target");
});
$("catchupList").addEventListener("drop", (event) => {
  event.preventDefault();
  const row = event.target.closest("li[data-catchup-id]");
  if (row && catchupDragId != null) moveCatchup(catchupDragId, catchupGroups.findIndex((group) => group.bgmId === Number(row.dataset.catchupId)));
});
function endCatchupDrag() {
  catchupDragId = null;
  $("catchupList").querySelectorAll(".dragging,.drop-target").forEach((row) => row.classList.remove("dragging", "drop-target"));
}
$("catchupList").addEventListener("dragend", endCatchupDrag);
$("catchupList").addEventListener("pointerdown", (event) => {
  catchupDragBlocked = Boolean(event.target.closest("input,.viewing-button"));
  const handle = event.target.closest(".catchup-drag");
  if (!handle || event.pointerType === "mouse" || catchupBusy) return;
  event.preventDefault();
  const row = handle.closest("li");
  catchupTouchDrag = { pointerId: event.pointerId, identity: Number(row.dataset.catchupId) };
  row.classList.add("dragging");
});
document.addEventListener("pointermove", (event) => {
  if (!catchupTouchDrag || event.pointerId !== catchupTouchDrag.pointerId || catchupBusy) return;
  const list = $("catchupList").querySelector("ul"), bounds = list.getBoundingClientRect();
  if (event.clientY < bounds.top + 28) list.scrollTop -= 12;
  else if (event.clientY > bounds.bottom - 28) list.scrollTop += 12;
  const row = document.elementFromPoint(event.clientX, event.clientY)?.closest("#catchupList li[data-catchup-id]");
  if (row) moveCatchup(catchupTouchDrag.identity, catchupGroups.findIndex((group) => group.bgmId === Number(row.dataset.catchupId)));
});
function endCatchupTouch(event) {
  if (catchupTouchDrag && (!event || event.pointerId === catchupTouchDrag.pointerId)) {
    catchupTouchDrag = null;
    endCatchupDrag();
  }
}
document.addEventListener("pointerup", endCatchupTouch);
document.addEventListener("pointercancel", endCatchupTouch);
$("catchupDialog").addEventListener("close", () => {
  catchupRequest = null; catchupGroups = []; catchupSelected.clear(); catchupBusy = false;
  endCatchupTouch(); endCatchupDrag();
});
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-install-playback-extension]");
  if (!button || button.disabled) return;
  const anchor = document.createElement("a");
  anchor.href = "/api/playback/potplayer-extension.zip";
  anchor.download = "";
  document.body.append(anchor); anchor.click(); anchor.remove();
});
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-browser-permission]");
  if (!button || button.disabled) return;
  const previous = button.innerHTML;
  button.disabled = true;
  try {
    const value = await api(`/api/playback/browser-permission-command?origin=${encodeURIComponent(location.origin)}`);
    if (!button.isConnected || !button.closest("dialog[open]")) return;
    if (await copyPlainText(value.command, t("browserPermissionHint"))) {
      button.textContent = t("copiedPlaylist");
      alert(t("browserPermissionHint"));
    }
  } catch (error) { alert(error.message); }
  finally {
    if (button.isConnected) button.innerHTML = previous;
    if (button.closest("#catchupPlayers")) updateCatchupControls(false);
    else button.disabled = false;
  }
});
async function openCatchup() {
  const season = radarSeasons()[radarSeason];
  catchupRequest = { from: season.from, to: season.to, language, sort: radarSort, direction: radarDirection };
  const request = catchupRequest;
  catchupGroups = []; catchupSelected.clear(); catchupBusy = false;
  $("catchupSelectionHint").hidden = true;
  $("catchupStatus").textContent = t("catchupPreparing");
  $("catchupList").replaceChildren(); $("catchupPlayers").replaceChildren();
  showModalDialog($("catchupDialog"));
  try {
    const state = await api("/api/playback/catchup", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...request, preview: true }) });
    if (catchupRequest !== request || !$("catchupDialog").open) return;
    $("catchupStatus").textContent = state.count ? t("catchupCount").replace("{works}", state.groups.length).replace("{episodes}", state.count) : t("catchupEmpty");
    if (!state.groups.length) return;
    catchupGroups = state.groups.map((group) => ({ ...group, rangeFrom: group.episodes[0] ?? null,
      rangeTo: group.episodes.at(-1) ?? null, rangeEdited: false }));
    $("catchupSelectionHint").hidden = false;
    $("catchupPlayers").innerHTML = playerActionsHtml();
    renderCatchupGroups();
  } catch (error) { if (catchupRequest === request) $("catchupStatus").textContent = error.message; }
}
document.addEventListener("click", async (event) => {
  const edit = event.target.closest("[data-viewing]");
  if (edit) {
    event.stopPropagation();
    const title = edit.closest("tr")?.querySelector("[data-detail]")?.textContent
      || edit.closest("li")?.querySelector(".catchup-title")?.textContent || $("detail").querySelector("h2")?.textContent || "";
    openViewing(Number(edit.dataset.viewing), title, Number(edit.dataset.bgmId)); return;
  }
  const button = event.target.closest("[data-catchup-player]");
  if (!button || button.disabled || !catchupRequest || catchupBusy || !catchupSelection().length) return;
  const kind = button.dataset.catchupPlayer, request = catchupRequest;
  const selectedBgmIds = catchupSelection();
  const episodeRanges = catchupGroups.filter((group) => catchupSelected.has(group.bgmId) && group.rangeEdited)
    .map((group) => ({ bgmId: group.bgmId, from: group.rangeFrom, to: group.rangeTo }));
  if (catchupGroups.some((group) => catchupSelected.has(group.bgmId) && !catchupRangeValid(group))) return;
  catchupBusy = true; updateCatchupControls(false);
  $("catchupStatus").textContent = t("catchupPreparing");
  try {
    const handoff = await api("/api/playback/catchup", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...request, selectedBgmIds, episodeRanges, player: kind, enhanced: true }) });
    if (catchupRequest !== request || !$("catchupDialog").open) return;
    if (!handoff.count) { $("catchupStatus").textContent = t("catchupEmpty"); return; }
    await deliverPlayback(kind, handoff, button);
    $("catchupStatus").textContent = t("catchupCount").replace("{works}", handoff.groups.length).replace("{episodes}", handoff.count);
  } catch (error) { if (catchupRequest === request && $("catchupDialog").open) $("catchupStatus").textContent = error.message; }
  finally { if (catchupRequest === request) { catchupBusy = false; updateCatchupControls(false); } }
});
$("viewingForm").onsubmit = (event) => { event.preventDefault(); try { saveViewing($("viewingEpisode").value === "" ? null : Number($("viewingEpisode").value), false, positionValue($("viewingPosition").value)); } catch(error) { $("viewingStatus").textContent = error.message; } };
$("viewingClear").onclick = () => saveViewing(null);
$('viewingComplete').onclick = () => saveViewing(viewingLatestEpisode, false, 0, true);
$("viewingUndo").onclick = () => { if (viewingUndoRecord) saveViewing(viewingUndoRecord.episode, true, viewingUndoRecord.positionMs); };
$("radarCatchup").onclick = openCatchup;

function playerIcon(kind) {
  if (kind === "vlc")
    return `<svg class="player-logo" viewBox="0 0 24 24" aria-hidden="true"><path fill="#f47b20" d="M12 2 6.2 18h11.6L12 2Z"/><path fill="#fff" d="m9.3 9 1-2.7h3.4l1 2.7H9.3Zm-1.6 4.3 1-2.7h6.6l1 2.7H7.7Z"/><path fill="#d35c0b" d="M4.8 18h14.4v3H4.8z"/></svg>`;
  if (kind === "potplayer")
    return `<svg class="player-logo" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="#9461e5"/><path fill="#fff" d="m10 7 7 5-7 5V7Z"/></svg>`;
  if (kind === "iina")
    return `<svg class="player-logo" viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="4" fill="#56606c"/><path fill="#fff" d="m10 8 6 4-6 4V8Z"/></svg>`;
  return "";
}

function uniqueLibraryTargets(targets) {
  const rows = new Map();
  const score = item => [item.state !== "absent" ? 1 : 0,
    Number(item.fileCount || item.observedFiles || 0) > 0 ? 1 : 0,
    item.state !== "external" ? 1 : 0,
    Number(item.fileCount || item.observedFiles || 0)];
  for (const [index, target] of targets.entries()) {
    if (target.origin === "ani-rss_api") continue;
    const path = String(target.path || "");
    const windows = /^[a-z]:[\\/]|^\\\\|^\/\//i.test(path);
    const normalized = (windows ? path.replace(/\\/g, "/") : path).replace(/\/+$/, "") || "/";
    const key = path ? `${windows ? "windows" : "posix"}:${windows ? normalized.toLowerCase() : normalized}` : `missing:${index}`;
    const previous = rows.get(key);
    if (!previous) { rows.set(key, target); continue; }
    const left = score(target), right = score(previous);
    const difference = left.findIndex((value, position) => value !== right[position]);
    if (difference >= 0 && left[difference] > right[difference]) rows.set(key, target);
  }
  return [...rows.values()];
}
function clientVisiblePath(value) {
  const raw = String(value || ""), normalized = raw.replace(/\\/g, "/"),
    mappings = config.playback?.directPathMappings || [];
  for (const mapping of mappings) {
    const server = String(mapping.serverPathPrefix || "").replace(/\\/g, "/").replace(/\/$/, ""),
      client = String(mapping.clientPathPrefix || "").replace(/[\\/]$/, "");
    if (!server || !client || !(normalized === server || normalized.startsWith(`${server}/`))) continue;
    const suffix = normalized.slice(server.length).replace(/^\//, "");
    return client.startsWith("\\\\")
      ? `${client}${suffix ? `\\${suffix.replace(/\//g, "\\")}` : ""}`
      : `${client}${suffix ? `/${suffix}` : ""}`;
  }
  return raw.startsWith("/") ? "" : raw;
}

async function copyPlainText(value, hint = "") {
  const text = String(value || "");
  if (!text) throw new Error(t("copyPlaylist"));
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_error) {
      // Clipboard access is commonly rejected on non-secure LAN origins.
      // Continue with the selection-based fallback instead of failing.
    }
  }
  const field = document.createElement("textarea");
  const previous = document.activeElement;
  const selection = typeof previous?.selectionStart === "number"
    ? [previous.selectionStart, previous.selectionEnd, previous.selectionDirection] : null;
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.top = "0";
  field.style.left = "0";
  field.style.width = "1px";
  field.style.height = "1px";
  field.style.opacity = "0";
  // A modal makes the rest of the document inert. Copying from a field outside
  // it can report success while leaving the clipboard empty or unchanged.
  const host = topModalDialog() || previous?.closest("dialog[open]") || document.body;
  let copied = false;
  try {
    host.append(field);
    field.focus({ preventScroll: true });
    field.select();
    field.setSelectionRange(0, text.length);
    if (document.activeElement === field && field.selectionEnd - field.selectionStart === text.length) {
      copied = document.execCommand("copy");
    }
  } catch (_error) {
    // Browsers may disable both clipboard APIs. Keep the text available below.
  } finally {
    field.remove();
    if (previous?.isConnected) {
      previous.focus({ preventScroll: true });
      if (selection) previous.setSelectionRange(...selection);
    }
  }
  if (!copied) window.prompt([t("copyManually"), hint].filter(Boolean).join("\n\n"), text);
  return copied;
}

async function handoffPlayback(kind, animeId, button, generation = detailGeneration) {
  const selectedEpisode = Math.max(1, Number($("playbackEpisode")?.value || 1)),
    payload = {
      player: kind,
      mode: "playlist",
      source: playbackEditions.get(animeId) || playbackSources.get(animeId) || "",
      start: selectedEpisode,
      enhanced: true,
    },
    handoff = await api(`/api/anime/${animeId}/playback/handoff`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  if (generation === detailGeneration && detailDialog.open) await deliverPlayback(kind, handoff, button);
}

function visibilityButton(bgmId, animeId, hidden = false) {
  return `<button type="button" class="text-button" data-work-visibility="${Number(bgmId)}" data-work-id="${Number(animeId) || ""}" data-hidden-value="${!hidden}">${esc(t(hidden ? "restoreWork" : "hideWork"))}</button>`;
}
function followButton(item, mode = "serial") {
  const combined = mode === "all";
  if (combined) mode = (item.follows || []).find((row) => row.state !== "cancelled")?.mode || "serial";
  const record = (item.follows || []).find((row) => row.mode === mode && row.state !== "cancelled");
  const text = combined ? "followWork" : record?.state === "available" ? "followAvailable" : record?.state === "subscribed" ? "radarSubscribed"
    : record ? "followPending" : mode === "archive" ? "followArchive" : "advanceSubscribe";
  return `<button type="button" class="text-button" data-follow-work="${Number(item.id)}" data-follow-mode="${mode}" data-icon="subscribe">${esc(t(text))}</button>`;
}
const followStateLabel = (state) => t(({ available: "followAvailable", unmatched: "followUnmatched",
  subscribed: "radarSubscribed", subscription_exists: "followManaged" })[state] || "followPending");
let followListGeneration = 0;
async function loadFollows() {
  const generation = ++followListGeneration;
  $("followListStatus").textContent = t("radarLoading");
  try {
    const response = await api("/api/follows");
    if (generation !== followListGeneration || !$("followListDialog").open) return;
    const rows = response.items.filter((row) => row.state !== "cancelled");
    $("followList").innerHTML = rows.length ? rows.map((row) => `<div class="hidden-work"><div><strong>${esc(row.work ? preferred(row.work) : `Bangumi #${row.bgmId}`)}</strong><small>${esc(t(row.mode === "archive" ? "followArchive" : "advanceSubscribe"))} · ${esc(followStateLabel(row.state))}</small></div>${row.work ? `<div class="follow-list-actions"><button type="button" class="text-button" data-follow-detail="${row.work.id}" data-icon="play">${esc(t("followOpen"))}</button><button type="button" class="text-button" data-follow-work="${row.work.id}" data-follow-mode="${row.mode}" data-icon="settings">${esc(t("settings"))}</button></div>` : ""}</div>`).join("") : `<p class="empty">${esc(t("followEmpty"))}</p>`;
    $("followListStatus").textContent = "";
  } catch (error) { if (generation === followListGeneration) $("followListStatus").textContent = error.message; }
}
$("radarFollows").onclick = () => { showModalDialog($("followListDialog")); loadFollows(); };
$("followListDialog").addEventListener("close", () => { if (!$("followListDialog").open) followListGeneration++; });
let followDetail = null, followGeneration = 0;
function serialPolicyLanguage(settings) {
  const choice = settings.torrentPolicy?.serialSubtitle?.language || "auto";
  const profile = String(choice === "auto" ? settings.ui?.language || "zh" : choice).toLowerCase();
  return profile.startsWith("zh") ? "zh" : profile.startsWith("ja") ? "ja" : "en";
}
function renderFollowChoices() {
  if (!followDetail) return;
  const mode = $("followMode").value, policy = config.torrentPolicy;
  const record = (followDetail.follows || []).find((row) => row.mode === mode);
  const active = record && record.state !== "cancelled";
  $("followForm").dataset.revision = String(record?.revision || 0);
  $("followCancel").hidden = !active;
  $("followCancel").textContent = t(["subscribed", "subscription_exists"].includes(record?.state) ? "followRemove" : "followCancel");
  $("followHint").textContent = t(mode === "serial" ? "followSerialHint" : "followArchiveHint");
  if (mode === "serial" && !capabilities.aniRssCredentialConfigured) $("followHint").textContent = t("followOfflineHint");
  if (mode === "serial" && ["subscribed", "subscription_exists"].includes(record?.state)) $("followHint").textContent = t("followManagedHint");
  const selectedLanguage = serialPolicyLanguage(config);
  const entries = mode === "archive" ? policy.resourceGroups.filter((group) => policy.archiveGroupIds.includes(group.id))
    : (groupCatalog.serialProfiles?.[selectedLanguage] || []).filter((group) => !group.wildcard).map((group) => ({ ...group, name: group.displayName }));
  const groups = active ? record.groups : {}, custom = Object.keys(groups).length > 0;
  $("followUsePreferences").checked = !custom;
  setCheckList("followGroups", entries.map((group) => ({ ...group, enabled: custom && Object.hasOwn(groups, group.id) })));
  // Stored unknown groups remain editable after a catalog/policy update.
  for (const key of Object.keys(groups).filter((key) => !entries.some((group) => group.id === key))) {
    const row = document.createElement("li"); row.dataset.value = key;
    row.innerHTML = `<label class="check"><input type="checkbox" checked><span>${esc(key)}</span></label>`;
    $("followGroups").append(row);
  }
  addGroupVersions("followGroups", Object.fromEntries(Object.entries(groups).filter(([, values]) => values !== null)));
  $("followGroups").hidden = !custom;
  $("followStatus").textContent = active ? followStateLabel(record.state) : "";
}
async function openFollow(animeId, mode = "serial") {
  const generation = ++followGeneration;
  followDetail = null;
  $("followWorkTitle").textContent = "";
  $("followStatus").textContent = t("radarLoading");
  $("followSave").disabled = true; $("followCancel").hidden = true;
  $("followCancel").disabled = false;
  $("followGroups").hidden = true;
  showModalDialog($("followDialog"));
  try {
    const detail = await api(`/api/anime/${animeId}?language=${encodeURIComponent(language)}`);
    if (generation !== followGeneration || !$("followDialog").open) return;
    followDetail = detail; $("followWorkTitle").textContent = preferred(detail); $("followMode").value = mode;
    renderFollowChoices(); $("followSave").disabled = false;
  } catch (error) { if (generation === followGeneration) $("followStatus").textContent = error.message === "follow_changed_reload" ? t("followChanged") : error.message; }
}
$("followMode").onchange = renderFollowChoices;
$("followUsePreferences").onchange = () => { $("followGroups").hidden = $("followUsePreferences").checked; };
$("followDialog").addEventListener("close", () => { if (!$("followDialog").open) followGeneration++; });
async function saveFollow(cancel = false) {
  if (!followDetail || $("followSave").disabled) return;
  const generation = followGeneration, groups = {};
  if (!cancel && !$("followUsePreferences").checked) {
    const variants = readGroupVersions("followGroups");
    $("followGroups").querySelectorAll(":scope > li").forEach((row) => {
      if (row.querySelector("input").checked) groups[row.dataset.value] = variants[row.dataset.value] || null;
    });
    if (!Object.keys(groups).length) { $("followStatus").textContent = t("followChooseGroup"); return; }
  }
  $("followSave").disabled = true; $("followCancel").disabled = true;
  try {
    const result = await api(`/api/anime/${followDetail.id}/follow`, { method: "POST", body: JSON.stringify({ mode: $("followMode").value,
      groups, revision: Number($("followForm").dataset.revision), cancel }) });
    if (generation !== followGeneration || !$("followDialog").open) return;
    if (!cancel) $("followForm").dataset.revision = String(result.revision);
    $("followDialog").close(); invalidateCatalogPages();
    if ($("followListDialog").open) loadFollows();
    if (detailDialog.open) await showDetail(followDetail.id);
    if ($("radarDialog").open) loadRadar();
  } catch (error) { if (generation === followGeneration) $("followStatus").textContent = error.message === "follow_changed_reload" ? t("followChanged") : error.message; }
  finally { if (generation === followGeneration) { $("followSave").disabled = false; $("followCancel").disabled = false; } }
}
$("followForm").onsubmit = (event) => { event.preventDefault(); saveFollow(); };
$("followCancel").onclick = () => saveFollow(true);
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-follow-work]");
  if (button && !button.disabled) openFollow(Number(button.dataset.followWork), button.dataset.followMode);
  const detail = event.target.closest("[data-follow-detail]");
  if (detail && !detail.disabled) showDetail(Number(detail.dataset.followDetail)).catch((error) => { $("followListStatus").textContent = error.message; });
});
let hiddenWorks = [], hiddenWorksGeneration = 0, hiddenWorksPage = 0;
function renderHiddenWorks() {
  const query = $("hiddenWorksSearch").value.trim().toLocaleLowerCase();
  const rows = hiddenWorks.filter((item) => [item.title_ja, item.title_zh_hans, item.title_en].some((value) => String(value || "").toLocaleLowerCase().includes(query)));
  hiddenWorksPage = Math.max(0, Math.min(hiddenWorksPage, Math.ceil(rows.length / 100) - 1));
  $("hiddenWorksPrevious").disabled = hiddenWorksPage === 0;
  $("hiddenWorksNext").disabled = (hiddenWorksPage + 1) * 100 >= rows.length;
  $("hiddenWorksCount").textContent = rows.length ? `${hiddenWorksPage * 100 + 1}–${Math.min(rows.length, (hiddenWorksPage + 1) * 100)} / ${rows.length}` : "";
  $("hiddenWorksList").innerHTML = rows.length ? rows.slice(hiddenWorksPage * 100, (hiddenWorksPage + 1) * 100).map((item) => `<div class="hidden-work"><div><strong>${esc(preferred(item))}</strong><small>${esc(localMonth(item.start_month))}${item.media_code ? ` · ${esc(label("media", item.media_code))}` : ""}</small></div>${visibilityButton(item.bgm_id, item.id, true)}</div>`).join("") : `<p class="empty">${esc(t("noHiddenWorks"))}</p>`;
}
async function loadHiddenWorks() {
  const generation = ++hiddenWorksGeneration;
  $("hiddenWorksStatus").textContent = t("radarLoading");
  try {
    const result = await api("/api/hidden-works");
    if (generation !== hiddenWorksGeneration || !$("hiddenWorksDialog").open) return;
    hiddenWorks = result.items;
    renderHiddenWorks();
    $("hiddenWorksStatus").textContent = "";
  } catch (error) {
    if (generation === hiddenWorksGeneration) $("hiddenWorksStatus").textContent = error.message;
  }
}
$("radarHidden").onclick = () => { hiddenWorks = []; hiddenWorksPage = 0; $("hiddenWorksSearch").value = ""; renderHiddenWorks(); showModalDialog($("hiddenWorksDialog")); loadHiddenWorks(); };
$("hiddenWorksSearch").oninput = () => { hiddenWorksPage = 0; renderHiddenWorks(); };
$("hiddenWorksPrevious").onclick = () => { hiddenWorksPage--; renderHiddenWorks(); };
$("hiddenWorksNext").onclick = () => { hiddenWorksPage++; renderHiddenWorks(); };
$("hiddenWorksDialog").addEventListener("close", () => { hiddenWorksGeneration++; });
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-work-visibility]");
  if (!button || button.disabled) return;
  button.disabled = true;
  const hidden = button.dataset.hiddenValue === "true", bgmId = Number(button.dataset.workVisibility), id = Number(button.dataset.workId);
  try {
    await api(`/api/hidden-works/${bgmId}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ hidden }) });
    const menu = button.closest(".subscription-menu");
    if (menu) menu.open = false;
    selected.delete(id); torrentSelections.delete(id); resourceSelections.delete(id); policyOverrides.delete(id);
    invalidateCatalogPages();
    if ($("hiddenWorksDialog").open) await loadHiddenWorks();
    if (detailDialog.open && Number(detailDialog.dataset.animeId) === id) await showDetail(id);
    radarRefreshPending = true;
    if (topModalDialog() === $("radarDialog")) await loadRadar();
    await search({ background: true });
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; }
});
document.addEventListener("click", (event) => {
  document.querySelectorAll(".subscription-menu[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.open = false;
  });
});
function aniSubscriptionMenu(subscription, animeId, disabled = false, bgmId = null) {
  const remoteId = esc(subscription.remoteId), title = esc(subscription.title || ""),
    button = (action, key, icon, danger = false) => `<button type="button" class="text-button ${danger ? "danger" : ""}" data-icon="${icon}" data-subscription-action="${action}" data-remote-id="${remoteId}" data-anime-id="${animeId}" data-subscription-title="${title}" ${disabled ? "disabled" : ""}>${t(key)}</button>`;
  return `<details class="subscription-menu"><summary title="${title}" aria-label="${title} · ${t(subscription.enabled ? "radarSubscribed" : "radarPaused")}">${t(subscription.enabled ? "radarSubscribed" : "radarPaused")} ▾</summary><div class="subscription-menu-actions">${button(subscription.enabled ? "pause" : "resume", subscription.enabled ? "aniRssPause" : "aniRssResume", subscription.enabled ? "pause" : "play")}${button("cancel", "aniRssCancel", "remove")}${authSession?.role === "admin" ? button("delete", "aniRssDeleteFiles", "remove", true) : ""}${bgmId ? visibilityButton(bgmId, animeId) : ""}</div></details>`;
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-subscription-action]");
  if (!button || button.disabled) return;
  const { subscriptionAction: action, remoteId, animeId, subscriptionTitle: title } = button.dataset;
  if (action === "cancel" && !window.confirm(t("aniRssCancelConfirm").replace("{title}", title))) return;
  if (action === "delete" && !window.confirm(`${title}\n\n${t("aniRssDeleteConfirm")}`)) return;
  const menu = button.closest("details");
  menu.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  try {
    const deleting = action === "cancel" || action === "delete";
    const result = await api(`/api/ani-rss/subscriptions/${encodeURIComponent(remoteId)}/${deleting ? "delete" : "enabled"}`, {
      method: "POST", timeoutMs: 100000, headers: { "Content-Type": "application/json" },
      body: JSON.stringify(deleting ? { deleteFiles: action === "delete" } : { enabled: action === "resume" }),
    });
    if (deleting && !result.deleted) throw new Error(t("aniRssDeleteFailed"));
    menu.open = false;
    if (deleting && playbackSources.get(Number(animeId)) === `ani-rss:${remoteId}`) playbackSources.delete(Number(animeId));
    if (detailDialog.open && Number(detailDialog.dataset.animeId) === Number(animeId)) await showDetail(Number(animeId));
    if ($("radarDialog").open) await loadRadar();
    await search({ background: true });
  } catch (error) { alert(error.message); }
  finally { menu.querySelectorAll("button").forEach((item) => { item.disabled = false; }); }
});
function aniSubscriptionManagementHtml(subscription, animeId) {
  const progress = subscription.episodeProgress || {};
  return `<div class="ani-rss-playback-management"><small>${esc(subscription.title)} · ${progress.current ? fmt(progress.current) : "?"} / ${progress.total ? fmt(progress.total) : "?"}</small>${aniSubscriptionMenu(subscription, animeId)}</div>`;
}

let detailGeneration = 0;
detailDialog.addEventListener("close", () => { if (!detailDialog.open) detailGeneration++; });
async function showDetail(id) {
  detailDialog.dataset.animeId = String(id);
  const generation = ++detailGeneration;
  if ($("detail").dataset.animeId !== String(id) || $("detail").dataset.language !== language) {
    $("detail").dataset.animeId = String(id);
    $("detail").dataset.language = language;
    $("detail").innerHTML = `<p class="detail-loading" role="status">${esc(t("radarLoading"))}</p>`;
  }
  showModalDialog(detailDialog);
  const x = await api(`/api/anime/${id}?language=${encodeURIComponent(language)}`).catch((error) => {
    if (generation === detailGeneration) {
      $("detail").innerHTML = `<p class="detail-loading error" role="status">${esc(error.message)}</p><button class="text-button detail-retry" data-icon="refresh" type="button">${esc(t("retryAction"))}</button>`;
      $("detail").querySelector(".detail-retry").onclick = () => showDetail(id).catch(() => {});
    }
    throw error;
  });
  if (generation !== detailGeneration) return;
  const
    libraryTargets = uniqueLibraryTargets(x.library?.targets || []),
    playableTargets = libraryTargets.filter((target) => target.state !== "absent" && Number(target.fileCount || target.observedFiles || 0) > 0),
    aniSubscriptions = x.ani_rss?.subscriptions || [],
    playableAniSubscriptions = aniSubscriptions.filter((item) => Number(item.playableCount || 0) > 0),
    sourceValues = new Set([
      ...playableTargets.map((target) => target.path),
      ...playableAniSubscriptions.map((item) => `ani-rss:${item.remoteId}`),
    ]);
  if (playbackSources.has(id) && !sourceValues.has(playbackSources.get(id))) playbackSources.delete(id);
  if (!playbackSources.has(id)) {
    if (playableTargets.length) {
      const preferredTarget = playableTargets.find((target) => target.state === x.library?.preferredOrigin) || playableTargets[0];
      playbackSources.set(id, preferredTarget.path);
    } else if (playableAniSubscriptions.length) {
      const remote = playableAniSubscriptions.find((item) => item.enabled) || playableAniSubscriptions[0];
      playbackSources.set(id, `ani-rss:${remote.remoteId}`);
    }
  }
  const selectedPlaybackSource = playbackSources.get(id) || "",
    playbackState = { loading: true, viewing: x.viewing },
    visibleTitles = sameAsOriginalLanguage(x)
      ? [
          {
            language: x.original_language || "ja",
            title: x.title_ja,
            title_type: "primary",
            source: "Bangumi Archive",
          },
        ]
      : x.titles,
    titles = visibleTitles
      .map(
        (y) =>
          `<li>${esc(y.language)} · ${esc(y.title)} <span class="source">${esc(y.title_type)} / ${esc(y.source)}</span></li>`,
      )
      .join(""),
    staffNames = (roleType) => [...new Set(
      x.staff.filter((y) => y.role_type === roleType).map((y) => String(y.name || "").trim()).filter(Boolean),
    )].join(" / "),
    directors = staffNames("director"),
    seriesComposition = staffNames("series_composition"),
    characterDesign = staffNames("character_design"),
    music = staffNames("music"),
    orig = x.cast.filter(
      (y) => (y.language || "und") === (x.original_language || "ja"),
    ),
    others = x.cast.filter(
      (y) => (y.language || "und") !== (x.original_language || "ja"),
    ),
    cast =
      orig
        .map((y) => `<li>${esc(y.character_name)} — ${esc(y.person_name)}</li>`)
        .join("") || "<li>—</li>",
    allCast = others
      .map(
        (y) =>
          `<li>${esc(y.character_name)} — ${esc(y.person_name)} ${esc(suffix(y.language))}</li>`,
      )
      .join(""),
    relations =
      x.relations
        .filter((y) => y.relation_code !== "other")
        .map((y) => {
          const kindCode = y.related_subject_kind || "other",
            kind = label("relatedKind", kindCode),
            sourceKind = { manga: "manga", light_novel: "light_novel", novel: "novel", game: "game" }[x.source_code],
            isTypedAdaptation = y.relation_code === "adaptation" && kindCode !== "other",
            relationText = isTypedAdaptation
              ? t(sourceKind === kindCode ? "originalWithKind" : "adaptationWithKind").replace("{kind}", kind)
              : (relationLabels[y.relation_code] || relationLabels.other)[li()];
          return `<li>${relationText ? `${esc(relationText)} · ` : ""}${esc(y.related_display_title || y.related_title)} · <a class="source" href="https://bgm.tv/subject/${Number(y.related_bgm_id)}" target="_blank" rel="noreferrer">BGM #${Number(y.related_bgm_id)}</a></li>`;
        })
        .join("") || "<li>—</li>",
    detailRelationButton = Number(x.series_member_count || 1) > 1
      ? `<button id="detailRelations" data-icon="graph" class="tool dark-tool detail-action" type="button">${t("viewRelations")} · ${fmt(x.series_member_count)}</button>`
      : "",
    originalTitle = secondaryTitle(x),
    detailSubtitleRow = originalTitle || detailRelationButton
      ? `<div class="detail-subtitle-row">${originalTitle ? `<p class="cn">${esc(originalTitle)}</p>` : `<span></span>`}${detailRelationButton}</div>`
      : "",
    displayTags = (x.display_tags || []).slice(0, 8).map((code) => label("theme", code)).filter(Boolean).join(" / "),
    countries = (x.countries || []).filter((code) => code && code !== "OTHER").map((code) => label("country", code)).join(" / "),
    sourceType = x.source_code && !["unknown", "other"].includes(String(x.source_code).toLowerCase())
      ? label("source", x.source_code)
      : "",
    originalName = String(x.original_name || "").trim(),
    originalAuthors = (x.original_authors || []).filter(Boolean).join(" / "),
    studios = (x.studios || []).filter(Boolean).join(" × "),
    currentEpisode = Number(x.episode_progress?.current || 0),
    totalEpisode = Number(x.episode_progress?.total || 0),
    episodes = currentEpisode || totalEpisode ? `${currentEpisode ? fmt(currentEpisode) : "?"} / ${totalEpisode ? fmt(totalEpisode) : "?"}` : "",
    factHtml = (className, labelText, value) => value ? `<div class="fact ${className}${value.length > 48 ? " wide-fact" : ""}"><b>${esc(labelText)}</b><span class="fact-value" title="${esc(value)}">${esc(value)}</span></div>` : "",
    summary = localizedSummary(x),
    aniResources = x.ani_rss?.resources || [],
    subtitleTarget = playableTargets.find((item) => item.path === selectedPlaybackSource && item.subtitleApplicable !== false)
      || playableTargets.find((item) => item.subtitleApplicable !== false),
    libraryContent = libraryHtml({ ...x.library, targets: libraryTargets }, x.id)
      || (playableAniSubscriptions.length ? `<p class="muted">${t("aniRssRemoteOnlyLibrary")}</p>` : `<p class="muted">${t("notInLibrary")}</p>`),
    playbackSourceSelector = playbackSourceHtml(playableTargets, playableAniSubscriptions, selectedPlaybackSource),
    aniRssManagement = aniSubscriptions.map((subscription) => aniSubscriptionManagementHtml(subscription, id)).join(""),
    choices = resourceChoices(x.torrents, aniResources, id),
    providerResources = (provider) => choices.map((group, index) => group.provider === provider ? resourceChoiceHtml(group, id, index) : "").join(""),
    resourceList = `<section class="resource-provider ani-rss-resources"><h4>${t("aniRssSubscriptionResources")}</h4><div class="torrent-list">${providerResources("ani-rss") || `<p class="muted">${t("noTorrent")}</p>`}</div>${aniResources.length ? `<small class="muted">${t("exactResourceHint")}</small>` : ""}</section><details class="resource-provider native-torrents" ${expandedTorrentWorks.has(id) || torrentSelections.has(id) ? "open" : ""}><summary>${t("torrentResources")} <small>(${choices.filter((group) => group.provider === "torrent").length})</small></summary><div class="torrent-list">${providerResources("torrent") || `<p class="muted">${t("noTorrent")}</p>`}</div></details>`,
    hasEligibleResource = (x.torrents || []).some((y) => y.eligible || (y.canOverride && policyOverrides.get(id) === `torrent:${y.infoHash}`))
      || aniResources.some((y) => y.eligible || (y.canOverride && policyOverrides.get(id) === `ani-rss:${y.resourceId}`));
  if (generation !== detailGeneration) return;
  $("detail").innerHTML =
    `<div class="detail-hero"><header class="detail-intro"><div class="detail-meta-row"><span class="date">${esc(localMonth(x.start_month))} · ${esc(label("media", x.media_code))}</span><div class="detail-visibility">${x.hidden ? `<span class="muted">${t("workHidden")}</span>` : ""}${visibilityButton(x.bgm_id, x.id, x.hidden)}</div></div><h2>${esc(preferred(x))}</h2>${detailSubtitleRow}</header><div class="detail-overview ${imagesEnabled ? "with-poster" : ""}">${imagesEnabled ? `<div class="detail-cover" data-cover="${x.id}"><button type="button" class="cover-reload" data-cover-reload="${x.id}" data-icon="refresh">${t("reloadImage")}</button></div>` : ""}<div class="detail-grid">${factHtml("country-fact", t("country"), countries)}${factHtml("source-type-fact", t("sourceType"), sourceType)}${factHtml("episodes-fact", t("episodes"), episodes)}${factHtml("original-name-fact", t("originalName"), originalName)}${factHtml("original-author-fact", t("originalAuthor"), originalAuthors)}${factHtml("director-fact", t("director"), directors)}${factHtml("series-composition-fact", t("seriesComposition"), seriesComposition)}${factHtml("character-design-fact", t("characterDesign"), characterDesign)}${factHtml("music-fact", t("music"), music)}${factHtml("studio-fact", t("studio"), studios)}${factHtml("tags-fact", t("tag"), displayTags)}</div></div></div><div class="detail-section-heading playback-title"><h3>${t("playback")}</h3></div>${playbackSourceSelector}<div id="detailPlayback">${playbackHtml(playbackState)}</div>${aniRssManagement}<section id="detailResources" class="detail-resources"><h3>${t("torrents")}</h3><p id="resourceChoiceHint" class="muted resource-choice-hint">${hasEligibleResource ? t("resourcePickHint") : ""}</p><div class="torrent-list">${resourceList || `<p class="muted">${t("noTorrent")}</p>`}</div><div class="resource-search-actions"><button type="button" id="searchWorkTorrents" data-icon="search" class="tool dark-tool" ${capabilities.qbtCredentialConfigured ? "" : "hidden"}>${t("searchPoolNow")}</button><button type="button" id="searchAniRss" data-icon="search" class="tool dark-tool">${t("searchAniRss")}</button><button type="button" id="startWorkDownload" data-icon="download" class="primary" ${hasEligibleResource ? "" : "disabled"}>${t("previewPlan")}</button>${followButton(x, "all")}<small id="searchWorkState" class="muted" role="status"></small></div></section><div class="detail-section-heading library-heading"><h3>${t("library")}</h3></div>${libraryContent}${subtitleToolsHtml(subtitleTarget)}<h3>${t("titles")}</h3><ul class="list">${titles}</ul><h3>${t("cast")}</h3><ul class="list">${cast}</ul>${others.length ? `<details><summary>${t("allCast")}</summary><ul class="list">${allCast}</ul></details>` : ""}<h3>${t("relations")}</h3><ul class="list">${relations}</ul><h3>${t("summary")}</h3><p class="summary-text">${esc(summary || "—").replace(/\n/g, "<br>")}</p><p class="source">Bangumi Archive · <a href="${esc(x.source_url)}" target="_blank" rel="noreferrer">BGM #${x.bgm_id}</a></p>`;
  bindCoverReloadButtons($("detail"));
  $("detail").querySelector(".native-torrents").ontoggle = (event) => {
    if (event.target.open) expandedTorrentWorks.add(id);
    else expandedTorrentWorks.delete(id);
  };
  $("detail")
    .querySelectorAll(`input[name="resource-${id}"]`)
    .forEach(
      (r) =>
        (r.onchange = () => {
          const [provider, value] = r.value.split(":", 2);
          if (provider === "ani-rss") {
            resourceSelections.set(id, value);
            torrentSelections.delete(id);
          } else {
            torrentSelections.set(id, value);
            resourceSelections.delete(id);
          }
          if (r.dataset.policyOverride === "true") policyOverrides.set(id, r.value);
          else policyOverrides.delete(id);
          $("startWorkDownload").disabled = false;
          updateDetailResourceAction();
          selected.add(id);
          updateSelection();
          render();
        }),
    );
  $("detail").querySelectorAll("[data-resource-choice]").forEach((row) => {
    const select = row.querySelector(".resource-variant"), radio = row.querySelector('input[type="radio"]'),
      variants = choices[Number(row.dataset.resourceChoice)].variants;
    if (select) select.onchange = () => {
      const choice = variants[Number(select.value)];
      if (!choice || !(choice.eligible || choice.canOverride)) return;
      radio.value = choice.value;
      radio.disabled = false;
      radio.dataset.policyOverride = String(!choice.eligible && choice.canOverride);
      select.title = choice.title;
      row.classList.toggle("policy-excluded", !choice.eligible && choice.canOverride);
      row.classList.remove("disabled");
      row.querySelector(".torrent-subline > span:first-child").textContent = choice.details;
      row.querySelector(".torrent-exclusion").textContent = choice.reason;
      radio.checked = true;
      radio.onchange();
    };
  });
  $("detail").querySelectorAll(".audit-work").forEach((button) => {
    button.onclick = () => verifyWorkLibrary(button, id, generation);
  });
  $("startWorkDownload").dataset.defaultProvider = !(x.torrents || []).some((row) => row.eligible)
    && aniResources.some((row) => row.eligible) ? "ani-rss" : "torrent";
  updateDetailResourceAction();
  if ($("playbackSource")) $("playbackSource").onchange = (event) => {
    playbackSources.set(id, event.target.value);
    playbackEditions.delete(id);
    showDetail(id).catch((error) => alert(error.message));
  };
  $("detail").querySelectorAll(".subtitle-tools").forEach((tool) => {
    const searchButton = tool.querySelector(".search-subtitles"),
      applyButton = tool.querySelector(".apply-subtitle"),
      select = tool.querySelector(".subtitle-candidates"),
      state = tool.querySelector(".subtitle-state"),
      target = tool.dataset.subtitleTarget;
    let candidates = [];
    searchButton.onclick = async () => {
      searchButton.disabled = true;
      applyButton.disabled = select.disabled = true;
      candidates = [];
      state.textContent = t("searchPoolRunning");
      try {
        const result = await api(`/api/anime/${id}/subtitles/search`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target }),
        });
        candidates = result.candidates || [];
        select.innerHTML = candidates.length
          ? candidates.map((item) => `<option value="${esc(item.candidateId)}">${esc(item.language || t("other"))} · ${esc(item.provider)} · ${esc(item.title)}</option>`).join("")
          : `<option>${t(result.state === "embedded" ? "subtitleEmbedded" : result.state === "sidecar_complete" ? "subtitlePresent" : "subtitleNotFound")}</option>`;
        select.disabled = !candidates.length;
        applyButton.disabled = !candidates.length;
        state.textContent = candidates.length ? "" : select.options[0].textContent;
      } catch (error) { state.textContent = error.message; }
      finally { searchButton.disabled = false; }
    };
    applyButton.onclick = async () => {
      applyButton.disabled = true;
      try {
        const result = await api(`/api/anime/${id}/subtitles/apply`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target, candidateId: select.value }),
        });
        state.textContent = `${t("subtitleApplied")} · ${result.installed}`;
      } catch (error) { state.textContent = error.message; applyButton.disabled = false; }
    };
  });
  if ($("searchWorkTorrents"))
    $("searchWorkTorrents").onclick = async () => {
      const button = $("searchWorkTorrents"), status = $("searchWorkState");
      button.disabled = true; status.textContent = t("searchPoolRunning");
      try {
        await api(`/api/anime/${id}/torrents/search`, { method: "POST" });
        const poll = async () => {
          if (generation !== detailGeneration || !detailDialog.open) return;
          try {
            const job = await api(`/api/anime/${id}/torrents/search`);
            if (generation !== detailGeneration || !detailDialog.open) return;
            if (generation !== detailGeneration || !detailDialog.open) return;
            if (job.state === "running") {
              status.textContent = job.sourcesTotal ? t("torrentSearchProgress").replace("{done}", job.sourcesDone || 0)
                .replace("{total}", job.sourcesTotal).replace("{found}", job.found || 0) : t("searchPoolRunning");
              setTimeout(poll, 1000); return;
            }
            if (job.state === "failed") { status.textContent = t("torrentSearchFailed"); button.disabled = false; return; }
            expandedTorrentWorks.add(id);
            await showDetail(id);
            const message = job.sourcesFailed ? t("torrentSearchPartial") : job.saved ? t("searchPoolDone") : t("searchPoolNone");
            if (Number(detailDialog.dataset.animeId) === id && detailDialog.open) {
              $("searchWorkState").textContent = message;
              const section = $("detailResources"), choices = [...section.querySelectorAll('.native-torrents input[type="radio"]')];
              $("resourceChoiceHint").textContent = choices.some((choice) => !choice.disabled)
                ? t("resourcePickHint") : choices.length ? t("resourceExcludedHint") : job.saved ? t("resourceUnmappedHint") : t("searchPoolNone");
              section.scrollIntoView({ block: "start", behavior: "smooth" });
              const first = choices.find((choice) => !choice.disabled);
              if (first) first.focus({ preventScroll: true });
            }
          } catch (error) { status.textContent = error.message; button.disabled = false; }
        };
        setTimeout(poll, 500);
      } catch (error) { status.textContent = error.message; button.disabled = false; }
    };
  if ($("searchAniRss"))
    $("searchAniRss").onclick = async () => {
      const button = $("searchAniRss"), status = $("searchWorkState");
      button.disabled = true; status.textContent = t("searchPoolRunning");
      try {
        await api(`/api/anime/${id}/ani-rss/search`, { method: "POST" });
        const poll = async () => {
          if (generation !== detailGeneration || !detailDialog.open) return;
          try {
            const job = await api(`/api/anime/${id}/ani-rss/search`);
            if (generation !== detailGeneration || !detailDialog.open) return;
            if (generation !== detailGeneration || !detailDialog.open) return;
            if (job.state === "running") { setTimeout(poll, 900); return; }
            if (job.state === "failed") { status.textContent = job.error || t("failed"); button.disabled = false; return; }
            status.textContent = job.found ? t("searchPoolDone") : t("searchPoolNone");
            await search();
            setTimeout(() => { if (generation === detailGeneration && detailDialog.open) showDetail(id); }, 650);
          } catch (error) {
            status.textContent = error.message;
            button.disabled = false;
          }
        };
        setTimeout(poll, 700);
      } catch (error) {
        status.textContent = error.message;
        button.disabled = false;
      }
    };
  if ($("startWorkDownload"))
    $("startWorkDownload").onclick = () => {
      selected.add(id);
      updateSelection();
      detailDialog.close();
      createPlan("default", { animeIds: [id], torrentSelections: Object.fromEntries([...torrentSelections].filter(([key]) => key === id)),
        resourceSelections: Object.fromEntries([...resourceSelections].filter(([key]) => key === id)),
        policyOverrides: Object.fromEntries([...policyOverrides].filter(([key]) => key === id)) });
    };
  $("detail").querySelectorAll(".copy-library-path").forEach((button) => {
    button.onclick = async () => {
      if (!await copyPlainText(button.dataset.copyPath)) return;
      const previous = button.textContent;
      button.textContent = t("copiedPath");
      setTimeout(() => { button.textContent = previous; }, 1600);
    };
  });
  if ($("detailRelations"))
    $("detailRelations").onclick = () => {
      detailDialog.close();
      showRelationGraph(id);
    };
  const playbackParams = new URLSearchParams();
  if (selectedPlaybackSource) playbackParams.set('source', selectedPlaybackSource);
  if (playbackEditions.has(id)) playbackParams.set('edition', playbackEditions.get(id));
  const playbackUrl = `/api/anime/${id}/playback?${playbackParams}`;
  api(playbackUrl)
    .then((state) => { if (generation === detailGeneration && detailDialog.open) renderDetailPlayback(state); })
    .catch((error) => { if (generation === detailGeneration && detailDialog.open) renderDetailPlayback({ viewing: x.viewing, error: error.message }); });
  function renderDetailPlayback(state) {
    state.selectedEdition = playbackEditions.get(id) || "";
    $("detailPlayback").innerHTML = playbackHtml(state);
    if (state.indexing) setTimeout(() => {
      if (generation !== detailGeneration || !detailDialog.open) return;
      api(playbackUrl)
        .then(next => { if (generation === detailGeneration && detailDialog.open) renderDetailPlayback(next); })
        .catch(error => { if (generation === detailGeneration && detailDialog.open) renderDetailPlayback({ viewing: x.viewing, error: error.message }); });
    }, 2000);
    if ($("playbackEdition")) $("playbackEdition").onchange = (event) => {
      if (event.target.value) playbackEditions.set(id, event.target.value);
      else playbackEditions.delete(id);
      showDetail(id).catch((error) => alert(error.message));
    };
    $("detailPlayback").querySelectorAll("[data-player]").forEach((button) => {
      button.onclick = async () => {
        const buttons = $("detailPlayback").querySelectorAll("[data-player]");
        buttons.forEach((item) => { item.disabled = true; });
        try { await handoffPlayback(button.dataset.player, id, button); }
        catch (error) { if (generation === detailGeneration && detailDialog.open) { $("detailPlayback").querySelector(".playback-error")?.remove(); const message = document.createElement("p"); message.className = "playback-error error"; message.textContent = error.message; $("detailPlayback").append(message); } }
        finally { buttons.forEach((item) => { item.disabled = false; }); }
      };
    });
  }
  showModalDialog(detailDialog);
  if (imagesEnabled) {
    covers();
    queueCoverElement($("detail").querySelector("[data-cover]"), coverBatch, true);
  }
  requestAnimationFrame(() => {
    detailDialog.scrollTop = 0;
  });
}
function updateDetailResourceAction() {
  const button = $("startWorkDownload");
  if (!button) return;
  const choice = $("detail").querySelector('input[name^="resource-"]:checked'),
    remote = choice ? choice.value.startsWith("ani-rss:") : button.dataset.defaultProvider === "ani-rss",
    override = choice?.dataset.policyOverride === "true";
  button.dataset.i18n = remote ? (override ? "forceSubscribe" : "enableSubscription")
    : (override ? "forceDownload" : "previewPlan");
  button.textContent = t(button.dataset.i18n);
  button.dataset.icon = remote ? "subscribe" : "download";
}
function updateSelection() {
  $("selectionBar").hidden = !selected.size;
  $("selectionCount").textContent = selected.size;
  $("createPlan").disabled = !selected.size;
}
async function createPlan(routingMode = "default", originalRequest = null) {
  if (typeof routingMode !== "string" || !["default", "ani-rss", "torrent"].includes(routingMode))
    routingMode = "default";
  const baseRequest = originalRequest || {
    animeIds: [...selected],
    torrentSelections: Object.fromEntries(torrentSelections),
    resourceSelections: Object.fromEntries(resourceSelections),
    policyOverrides: Object.fromEntries(policyOverrides),
  };
  const request = routingMode === "default" ? { ...baseRequest } : { ...baseRequest, routingMode };
  delete request._skippedWorks;
  try {
    let p = await api("/api/plans", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    planState.id = p.planId;
    if (p.state === "building") {
      $("plan").innerHTML = `<h2>${t("taskPreview")}</h2><p class="notice">${t("planBuilding")}</p><p><b>${fmt(p.workCount)}</b> ${t("selectedWorks")}</p>`;
      showModalDialog(planDialog);
      while (p.state === "building") {
        await new Promise((resolve) => setTimeout(resolve, 750));
        p = await api(`/api/plans/${planState.id}`);
      }
      if (p.state === "error") throw new Error(p.error || "plan build failed");
    }
    const subscriptionOnly = (p.aniRssJobs || []).length > 0 && !(p.jobs || []).length,
      planTitleKey = subscriptionOnly ? "subscriptionPlan" : (p.aniRssJobs || []).length ? "mixedPlan" : "plan",
      assessments = (p.assessments || [])
        .map((assessment) => {
          const actions = Object.entries(assessment.summary || {})
              .filter(([, count]) => count)
              .map(
                ([action, count]) =>
                  `<span class="plan-action ${esc(action)}"><b>${fmt(count)}</b> ${esc(t(action))}</span>`,
              )
              .join(""),
            warnings = assessment.files
              .filter(
                (file) => file.warning || file.action === "conflict_review",
              )
              .map(
                (file) =>
                  `<li><b>${esc(t(file.action))}</b> · ${esc(file.finalPath || file.oldPath)}${file.warning ? `<small>${esc(t(file.warning))}${file.warningDetail ? ` · ${esc(file.warningDetail)}` : ""}</small>` : ""}</li>`,
              )
              .join("");
          return `<section class="plan-assessment ${assessment.hasWarnings ? "warning" : ""}"><header><b>${esc(assessment.resourceGroup)}</b><span>${fmt(assessment.downloadBytes)} bytes</span></header><div class="plan-actions">${actions}</div>${warnings ? `<details><summary>${t("planWarnings")}</summary><ul class="list">${warnings}</ul></details>` : ""}</section>`;
        })
        .join(""),
      jobs = (p.jobs || [])
        .map(
          (job) =>
            `<div class="plan-job"><b>${esc(job.resourceGroup)}</b><code>${esc(job.savePath)}</code><small>${fmt(job.selectedBytes)} bytes · ${job.files.filter((file) => ["add_missing", "stage_replace"].includes(file.action)).length} ${t("files")}</small></div>`,
        )
        .join(""),
      aniRssJobs = (p.aniRssJobs || [])
        .map((job) => `<div class="plan-job"><b>Ani-RSS · ${esc(job.resourceGroup || t("other"))}</b><code>${esc(job.title)}</code><small>${fmt(job.selectedBytes || 0)} bytes</small></div>`)
        .join(""),
      skippedJobs = (p.skippedWorks || [])
        .map((work) => `<div class="plan-job disabled"><b>${esc(preferred(work) || work.title_ja || `#${work.id}`)}</b><small>${t("torrentUnavailable")}</small></div>`)
        .join(""),
      routeButtons = `<div class="plan-route-actions"><button type="button" class="tool ${routingMode === "ani-rss" ? "active" : ""}" data-plan-route="ani-rss" data-icon="subscribe">${t("planUseAniRss")}</button><button type="button" class="tool ${routingMode === "torrent" ? "active" : ""}" data-plan-route="torrent" data-icon="download">${t("planUseTorrent")}</button><button type="button" class="tool ${routingMode === "default" ? "active" : ""}" data-plan-route="default" data-icon="refresh">${t("planRestoreDefault")}</button></div>`,
      stoppedNotice = ((p.aniRssJobs || []).length ? "" : `<p class="notice">${t("planStopped")}</p>`)
        + ([...(p.jobs || []), ...(p.aniRssJobs || [])].some((job) => job.policyOverride)
          ? `<p class="notice">${t("forcePolicyNotice")}</p>` : ""),
      submitButton = capabilities.submissionEnabled
        ? `<button id="submitStoppedPlan" data-i18n="${subscriptionOnly ? "enableSubscription" : "submitStopped"}" data-icon="${subscriptionOnly ? "subscribe" : "download"}" class="primary" ${p.taskCount ? "" : "disabled"}>${t(subscriptionOnly ? "enableSubscription" : "submitStopped")}</button>`
        : `<p class="muted">${t("submissionDisabled")}</p>`;
    $("plan").innerHTML =
      `<h2 data-i18n="${planTitleKey}">${t(planTitleKey)}</h2>${stoppedNotice}<div class="plan-summary-row"><p><b>${p.taskCount}</b> ${t("tasks")} · ${fmt(p.totalBytes)} bytes</p>${routeButtons}</div>${p.taskCount ? "" : `<p class="notice safe">${t("planNoDownload")}</p>`}${assessments}${jobs}${aniRssJobs}${skippedJobs}${submitButton}`;
    $("plan").querySelectorAll("[data-plan-route]").forEach((button) => {
      button.onclick = () => createPlan(button.dataset.planRoute, baseRequest);
    });
    if ($("submitStoppedPlan") && p.taskCount)
      $("submitStoppedPlan").onclick = async () => {
        const button = $("submitStoppedPlan");
        button.disabled = true;
        button.dataset.i18n = "submitting";
        button.textContent = t("submitting");
        try {
          await api(`/api/plans/${planState.id}/submit`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ confirmStopped: true }),
          });
          button.dataset.i18n = "submitted";
          button.textContent = t("submitted");
          button.insertAdjacentHTML("beforebegin", `<p class="notice safe">${t("submitAccepted")}</p>`);
          if ((p.aniRssJobs || []).length && ["recent_episode", "random"].includes(sort)) {
            page = 0;
            search({ background: true }).catch(() => {});
          }
        } catch (error) {
          button.disabled = false;
          button.dataset.i18n = subscriptionOnly ? "enableSubscription" : "submitStopped";
          button.textContent = t(button.dataset.i18n);
          alert(`${t("queryFailed")}: ${friendlyPlanError(error)}`);
        }
      };
    if (!planDialog.open) showModalDialog(planDialog);
  } catch (e) {
    alert(`${t("queryFailed")}: ${friendlyPlanError(e)}`);
  }
}

function friendlyPlanError(error) {
  const message = String(error?.message || error || "");
  return message.includes("routingMode must be") ? t("invalidDownloadRoute") : message;
}

function draggable(list) {
  let a;
  list.querySelectorAll(":scope > li").forEach((x) => {
    x.draggable = true;
    x.ondragstart = (event) => {
      event.stopPropagation();
      a = x;
      x.classList.add("dragging");
    };
    x.ondragend = () => x.classList.remove("dragging");
    x.ondragover = (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (a && a !== x)
        list.insertBefore(
          a,
          e.clientY < x.getBoundingClientRect().top + x.offsetHeight / 2
            ? x
            : x.nextSibling,
        );
    };
  });
}
function setList(id, v, f = (x) => x) {
  const l = $(id);
  l.innerHTML = v
    .map(
      (x) =>
        `<li data-value="${esc(x)}"><span class="handle">↕</span>${esc(f(x))}</li>`,
    )
    .join("");
  draggable(l);
}
const order = (id) =>
  [...$(id).querySelectorAll(":scope > li")].map((x) => x.dataset.value);
const optionalOrder = (id) => ($(id) ? order(id) : []);
function setChecks(id, entries, f = (x) => x) {
  $(id).innerHTML = entries
    .map(
      ([k, on]) =>
        `<label class="check"><input type="checkbox" value="${esc(k)}" ${on ? "checked" : ""}><span>${esc(f(k))}</span></label>`,
    )
    .join("");
}
function setCheckList(id, entries) {
  const list = $(id);
  list.innerHTML = entries.map((x) =>
    `<li data-value="${esc(x.id)}"><span class="handle">↕</span><label class="check"><input type="checkbox" ${x.enabled ? "checked" : ""}><span>${esc(x.name)}</span></label></li>`
  ).join("");
  draggable(list);
}
const releasePlatforms = ["Baha", "CR", "NF", "ABEMA", "AMZN", "ADN", "ViuTV", "DSNP", "B-Global", "BiliBili", "U-NEXT", "Hulu", "DMM", "FOD", "unknown"];
const groupVersionSelections = new WeakMap();
function updateGroupToggle(id) {
  const button = document.querySelector(`[data-group-toggle="${id}"]`);
  if (!button) return;
  const parents = [...$(id).querySelectorAll(":scope > li > .check > input")];
  button.disabled = !parents.length;
  button.textContent = t(parents.length && parents.every((input) => input.checked) ? "clearAllGroups" : "selectAllGroups");
}
document.querySelectorAll("[data-group-toggle]").forEach((button) => {
  button.onclick = () => {
    const parents = [...$(button.dataset.groupToggle).querySelectorAll(":scope > li > .check > input")];
    const checked = !parents.every((input) => input.checked);
    parents.forEach((input) => { input.checked = checked; input.dispatchEvent(new Event("change", { bubbles: true })); });
    updateGroupToggle(button.dataset.groupToggle);
  };
});
function addGroupVersions(id, restrictions = {}) {
  $(id).querySelectorAll(":scope > li").forEach((row) => {
    const parent = row.querySelector("input"), group = row.dataset.value;
    const selected = Object.hasOwn(restrictions, group) ? new Set(restrictions[group]) : new Set(releasePlatforms);
    let remembered = new Set(selected);
    const platforms = [...new Set([...releasePlatforms, ...selected])];
    const details = document.createElement("details");
    details.className = "group-versions";
    details.innerHTML = `<summary></summary><div>${platforms.map((platform) => `<label class="check"><input type="checkbox" data-platform="${esc(platform)}" ${parent.checked && selected.has(platform) ? "checked" : ""}><span>${esc(platform === "unknown" ? t("unknownVersion") : platform)}</span></label>`).join("")}</div>`;
    row.append(details);
    const summary = details.querySelector("summary");
    summary.title = t("groupVersions");
    summary.setAttribute("aria-label", `${row.querySelector(".check span").textContent} · ${t("groupVersions")}`);
    const boxes = [...details.querySelectorAll("input")];
    groupVersionSelections.set(row, () => [...remembered]);
    const update = () => {
      const count = boxes.filter((box) => box.checked).length;
      if (count) remembered = new Set(boxes.filter((box) => box.checked).map((box) => box.dataset.platform));
      parent.checked = count > 0;
      parent.indeterminate = count > 0 && count < boxes.length;
      summary.textContent = count ? count === boxes.length ? t("allVersions") : String(count) : "";
      updateGroupToggle(id);
    };
    parent.onchange = () => {
      if (parent.checked && !remembered.size) remembered = new Set(platforms);
      boxes.forEach((box) => { box.checked = parent.checked && remembered.has(box.dataset.platform); });
      update();
    };
    boxes.forEach((box) => { box.onchange = update; });
    update();
  });
}
function readGroupVersions(id) {
  return Object.fromEntries([...$(id).querySelectorAll(":scope > li")].flatMap((row) => {
    const boxes = [...row.querySelectorAll("[data-platform]")];
    const chosen = groupVersionSelections.get(row)?.() || boxes.filter((box) => box.checked).map((box) => box.dataset.platform);
    return boxes.length && chosen.length !== boxes.length ? [[row.dataset.value, chosen]] : [];
  }));
}
const enabled = (id) =>
    new Set([...$(id).querySelectorAll("input:checked")].map((x) => x.value)),
  mergeOrder = (cur, vis) => [
    ...vis,
    ...cur.filter((x) => !new Set(vis).has(x)),
  ];
function syncPriority() {
  renderPriorityTree();
}
function dimensionLabel(x) {
  const m = {
    resourceCompleteness: ["资源完整性", "Resource completeness", "リソース完全性"],
    releaseStrategy: ["发布策略", "Release strategy", "リリース戦略"],
    seriesCompleteness: ["系列完整度", "Series completeness", "シリーズ完全性"],
    resourceGroup: ["资源组", "Release group", "リリースグループ"],
    collectionOrRevision: [
      "合集 / 修订版",
      "Collection / revision",
      "コレクション / 改訂版",
    ],
    attachmentCompleteness: [
      "附件完整度",
      "Attachment completeness",
      "付属品の完全性",
    ],
    sourceClass: ["片源类型", "Source class", "ソース種別"],
    resolution: ["分辨率", "Resolution", "解像度"],
    subtitle: ["字幕", "Subtitles", "字幕"],
    bitDepth: ["色深", "Bit depth", "ビット深度"],
    torrentCreationDate: [
      "Torrent 构建日期",
      "Torrent creation date",
      "Torrent 作成日時",
    ],
    size: ["文件大小", "Size", "サイズ"],
  };
  return m[x]?.[li()] || x;
}
function renderPriorityTree() {
  const p = config.torrentPolicy,
    sourceEnabled = enabled("contentClasses"),
    resolutionEnabled = enabled("allowedResolutions"),
    children = {
      releaseStrategy: { id: "releaseStrategyPriority", values: p.releaseStrategyPriority || [], label: priorityValueLabel },
      collectionOrRevision: { id: "collectionRevisionPriority", values: p.collectionRevisionPriority || [], label: priorityValueLabel },
      attachmentCompleteness: { id: "attachmentPriority", values: p.attachmentPriority || [], label: priorityValueLabel },
      sourceClass: { id: "contentClassPriority", values: p.contentClassPriority.filter((x) => sourceEnabled.has(x)), label: (x) => policyLabel("sourceClass", x) },
      resolution: { id: "resolutionPriority", values: p.resolutionPriority.filter((x) => resolutionEnabled.has(x)), label: (x) => policyLabel("resolution", x) },
      subtitle: { id: "subtitlePriority", values: p.subtitlePriority || [], label: (x) => policyLabel("subtitle", x) },
      bitDepth: { id: "bitDepthPriority", values: p.bitDepthPriority || [], label: priorityValueLabel },
      torrentCreationDate: { id: "creationDatePriority", values: p.creationDatePriority || ["newest", "oldest"], label: priorityValueLabel },
      size: { id: "sizePriority", values: p.sizePriority || ["larger", "smaller"], label: priorityValueLabel },
    },
    tree = $("strategyOrder");
  tree.innerHTML = p.strategyOrder.map((dimension) => {
    const child = children[dimension], note = dimension === "resourceGroup" ? t("groupPriorityHint") : dimension === "resourceCompleteness" ? t("resourceCompletenessHint") : t("fixedPriorityHint");
    return `<li data-value="${esc(dimension)}" class="priority-branch"><div class="priority-node"><span class="handle">↕</span><button type="button" class="priority-toggle" aria-expanded="false"><span>${esc(dimensionLabel(dimension))}</span><span class="disclosure">▾</span></button></div><div class="priority-children" hidden>${child ? `<ol id="${child.id}" class="sortable vertical nested-sort"></ol>` : `<p>${esc(note)}</p>`}</div></li>`;
  }).join("");
  draggable(tree);
  Object.values(children).forEach((child) => {
    if ($(child.id)) setList(child.id, child.values, child.label);
  });
  tree.querySelectorAll(".priority-toggle").forEach((button) => {
    button.onclick = () => {
      const panel = button.closest(".priority-branch").querySelector(".priority-children"),
        open = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", String(!open));
      panel.hidden = open;
    };
  });
}
function renderPolicy() {
  const p = config.torrentPolicy,
    allow = p.allowUnlisted || {};
  setChecks(
    "contentClasses",
    [
      ...Object.entries(p.contentClasses),
      ["__other__", allow.sourceClass !== false],
    ],
    (x) => policyLabel("sourceClass", x),
  );
  setChecks(
    "allowedResolutions",
    [
      ...Object.entries(p.resolutions || {}),
      ["__other__", allow.resolution !== false],
    ],
    (x) => policyLabel("resolution", x),
  );
  const regions = { china: true, japan: true, korea: true, usa: true, europe: true, other: true, ...(p.regions || {}) };
  const regionOrder = language.startsWith("zh")
    ? ["china", "japan", "korea", "usa", "europe", "other"]
    : language.startsWith("ja")
      ? ["japan", "korea", "china", "usa", "europe", "other"]
      : ["usa", "europe", "china", "japan", "korea", "other"];
  const regionLabels = { china: "regionChina", japan: "regionJapan", korea: "regionKorea", usa: "regionUsa", europe: "regionEurope", other: "regionOther" };
  setChecks("allowedRegions", regionOrder.map((key) => [key, regions[key] !== false]), (key) => t(regionLabels[key]));
  [
    "contentClasses",
    "allowedResolutions",
    "allowedRegions",
  ].forEach((id) =>
    $(id)
      .querySelectorAll("input")
      .forEach(
        (x) =>
          (x.onchange = () => {
            if (
              x.value === "__other__" &&
              !x.checked &&
              !globalThis.confirm(t("otherWarning"))
            )
              x.checked = true;
            syncPriority();
          }),
      ),
  );
  syncPriority();
  renderGroupPolicy();
}
function renderGroupPolicy() {
  const p = config.torrentPolicy, serial = p.serialSubtitle || (p.serialSubtitle = { language: "auto" });
  const archiveIds = new Set(p.archiveGroupIds || []);
  setCheckList("archiveGroups", p.resourceGroups.filter((x) => archiveIds.has(x.id)).sort((a,b) => a.tier-b.tier || a.order-b.order).map(x => ({id:x.id,name:x.name,enabled:x.enabled!==false})));
  addGroupVersions("archiveGroups", p.groupVariants?.archive);
  const enabledByLanguage = serial.enabledByLanguage || {};
  const selectedLanguage = serial.language === "auto" || !serial.language
    ? (language.startsWith("zh") ? "zh" : language.startsWith("ja") ? "ja" : "en")
    : serial.language;
  [["zh","serialGroupsZh"],["en","serialGroupsEn"],["ja","serialGroupsJa"]].forEach(([lang,id]) => {
    const explicit = Object.hasOwn(enabledByLanguage, lang) ? new Set(enabledByLanguage[lang]) : null;
    setCheckList(id, (groupCatalog.serialProfiles?.[lang] || []).filter(x => !x.wildcard).map(x => ({...x,name:x.displayName,enabled:explicit ? explicit.has(x.id) : lang === selectedLanguage})));
    const variants = p.groupVariants?.serial || {};
    addGroupVersions(id, Object.values(variants).some((value) => !Array.isArray(value)) ? variants[lang] : variants);
  });
  $("serialSubtitleLanguage").value = serial.language || "auto";
  $("serialSubtitleLanguage").onchange = (event) => {
    serial.language = event.target.value;
    if (!Object.keys(enabledByLanguage).length) renderGroupPolicy();
  };
  $("otherGroupsEnabled").checked = p.allowUnlisted?.resourceGroup === true;
}
function loadOperationalSettings() {
  $("pollMinutes").value = config.components.discovery.pollMinutes ?? 30;
  const collector = config.components.torrentCollector || {}, search = collector.search || {}, pool = collector.pool || {};
  $("collectorSearchSources").querySelectorAll("input").forEach((input) => { input.checked = (search.sources || ["mikan", "nyaa", "acgrip"]).includes(input.value); });
  validateCollectorSources();
  $("collectorMaxResults").value = search.maxResults ?? 80;
  $("collectorTimeout").value = search.timeoutSeconds ?? 120;
  $("collectorMonitor").checked = pool.monitorEnabled !== false;
  $("collectorCollection").checked = pool.collectionEnabled !== false;
  $("collectorHistory").checked = pool.historyEnabled !== false;
  $("minimumFree").value = config.storageGuard.minimumFreeTiB ?? 0.1;
  $("onDemandHash").checked =
    config.differentialPlanning?.samePathSizePolicy === "hash_and_skip";
}
function validateCollectorSources() {
  const box = $("collectorSearchSources");
  box.querySelector("input").setCustomValidity(box.querySelector("input:checked") ? "" : t("collectorSourcesRequired"));
}
function saveOperationalSettings(target) {
  const sources = [...$("collectorSearchSources").querySelectorAll("input:checked")].map((input) => input.value);
  if (!sources.length) throw new Error(t("collectorSourcesRequired"));
  target.components.torrentCollector = {
    search: {sources, maxResults: Number($("collectorMaxResults").value), timeoutSeconds: Number($("collectorTimeout").value)},
    pool: {monitorEnabled: $("collectorMonitor").checked, collectionEnabled: $("collectorCollection").checked, historyEnabled: $("collectorHistory").checked},
  };
  target.components.discovery.pollMinutes = Math.max(
    5,
    +$("pollMinutes").value || 30,
  );
  target.storageGuard.minimumFreeTiB = Math.max(
    0,
    +$("minimumFree").value || 0,
  );
  target.differentialPlanning = target.differentialPlanning || {};
  target.differentialPlanning.samePathSizePolicy = $("onDemandHash").checked
    ? "hash_and_skip"
    : "size_and_skip";
}
let passwordGeneration = 0;
function openPasswordSettings() {
  if (!authSession?.authEnabled) return;
  passwordGeneration++;
  $("passwordForm").reset();
  $("confirmPassword").setCustomValidity("");
  $("passwordStatus").textContent = "";
  $("passwordAccount").textContent = authSession.username;
  $("passwordUsername").value = authSession.username;
  $("passwordSave").disabled = false;
  showModalDialog($("passwordDialog"));
  $("currentPassword").focus();
}
$("changePassword").onclick = openPasswordSettings;
$("passwordDialog").addEventListener("close", () => {
  if (!$("passwordDialog").open) { passwordGeneration++; $("passwordForm").reset(); }
});
$("confirmPassword").oninput = $("newPassword").oninput = () => $("confirmPassword").setCustomValidity("");
$("passwordForm").onsubmit = async (event) => {
  event.preventDefault();
  if ($("passwordSave").disabled) return;
  if ($("newPassword").value !== $("confirmPassword").value) {
    $("confirmPassword").setCustomValidity(t("passwordMismatch")); $("confirmPassword").reportValidity(); return;
  }
  const generation = passwordGeneration;
  $("passwordSave").disabled = true;
  $("passwordStatus").textContent = "";
  try {
    await api("/api/auth/password", { method: "POST", body: JSON.stringify({ currentPassword: $("currentPassword").value, newPassword: $("newPassword").value }) });
    if (generation !== passwordGeneration) return;
    $("passwordForm").reset();
    $("passwordStatus").textContent = t("passwordChanged");
  } catch (error) {
    if (generation !== passwordGeneration) return;
    const errors = { current_password_incorrect: "currentPasswordIncorrect", password_length: "passwordLength",
      password_unchanged: "passwordUnchanged", password_changed_retry: "passwordRetry", too_many_login_attempts: "passwordRateLimited" };
    $("passwordStatus").textContent = errors[error.message] ? t(errors[error.message]) : error.message;
  } finally { if (generation === passwordGeneration) $("passwordSave").disabled = false; }
};
const deploymentControls = {
  'components.downloadClient.endpoint': 'qbtEndpoint', 'components.aniRss.endpoint': 'aniRssEndpoint',
  'components.aniRss.mode': 'aniRssMode', 'components.aniRss.mediaPath': 'aniRssMediaPath',
  'deployment.libraryUncRoot': 'libraryPath', 'deployment.qbtLibraryRoot': 'qbtLibraryPath',
  'deployment.torrentPoolRoot': 'torrentPoolPath', 'externalLibraries.0.path': 'externalReadOnlyPath',
  'metadata.network.archiveManifestEndpoints': 'archiveManifestEndpoints',
  'metadata.network.archiveAssetProxyTemplates': 'archiveAssetProxyTemplates',
};
function applyDeploymentSettings() {
  const overrides = capabilities.configurationOverrides || {};
  for (const [path, id] of Object.entries(deploymentControls)) {
    const field = $(id), managed = Object.hasOwn(overrides, path);
    if (!field) continue;
    field.closest('label')?.querySelector('.deployment-badge')?.remove();
    if (managed) {
      field.value = Array.isArray(overrides[path]) ? overrides[path].join('\n') : overrides[path];
      field.title = t('connectionHint');
      const label = field.closest('label')?.querySelector('span');
      if (label) { const badge = document.createElement('small'); badge.className = 'deployment-badge'; badge.textContent = t('deploymentManaged'); label.append(badge); }
    } else field.removeAttribute('title');
    if (field.tagName === 'SELECT') field.disabled = managed;
    else field.readOnly = managed;
  }
}
function retainPersistentOverrides(nextConfig) {
  for (const path of Object.keys(capabilities.configurationOverrides || {})) {
    const parts = path.split('.'), key = parts.pop();
    let current = config, next = nextConfig;
    for (const part of parts) { current = current?.[part]; next = next?.[part]; }
    if (!next || typeof next !== 'object') continue;
    if (current && Object.hasOwn(current, key)) next[key] = JSON.parse(JSON.stringify(current[key]));
    else delete next[key];
  }
}
function openSettings() {
  if (authSession?.role !== "admin") { openPasswordSettings(); return; }
  renderPolicy();
  loadOperationalSettings();
  $("submissionAllowed").checked = config.components.downloadClient.submissionEnabled ?? capabilities.submissionEnabled ?? true;
  $("submissionAllowed").disabled = false;
  $("qbtEndpoint").value =
    config.components.downloadClient.endpoint || "http://127.0.0.1:8080";
  $("qbtApiKey").value = "";
  $("qbtCredentialState").textContent = capabilities.qbtCredentialConfigured
    ? t("qbtCredentialConfigured") : t("qbtApiKeyHint");
  const aniRss = config.components.aniRss || {};
  $("aniRssMode").value = aniRss.mode || "manual";
  $("aniRssEndpoint").value = aniRss.endpoint || "http://127.0.0.1:7789";
  $("aniRssApiKey").value = "";
  $("aniRssMediaPath").value = aniRss.mediaPath || "";
  $("aniRssSyncMinutes").value = aniRss.syncMinutes || 30;
  $("aniRssCredentialState").textContent = capabilities.aniRssCredentialConfigured
    ? t("aniRssCredentialConfigured") : t("aniRssApiKeyHint");
  $("torrentPoolPath").value = config.deployment.torrentPoolRoot || "";
  $("libraryPath").value = config.deployment.libraryUncRoot || "";
  const librarySleep = config.library?.sleep || {};
  $("librarySleepEnabled").checked = librarySleep.enabled === true;
  $("librarySleepStart").value = librarySleep.start || "00:00";
  $("librarySleepEnd").value = librarySleep.end || "08:00";
  $('librarySleepClock').textContent = capabilities.librarySleepClock || '';
  const sleepFields = () => { $('librarySleepStart').disabled = $('librarySleepEnd').disabled = !$('librarySleepEnabled').checked; };
  $('librarySleepEnabled').onchange = sleepFields;
  sleepFields();
  $("qbtLibraryPath").value = config.deployment.qbtLibraryRoot || "";
  const externalSource = (config.externalLibraries || [])[0] || { id: "external-read-only", kind: "generic", enabled: false, path: "/External", scanMinutes: 60 };
  $("externalReadOnlyEnabled").checked = Boolean(externalSource.enabled);
  $("externalReadOnlyKind").value = externalSource.kind || "generic";
  $("externalReadOnlyPath").value = externalSource.path;
  $("externalScanMinutes").value = externalSource.scanMinutes || 60;
  const subtitleConfig = config.subtitles || {};
  const subtitleProviders = Object.fromEntries((subtitleConfig.providers || []).map((item) => [item.id, item]));
  $("subtitlesEnabled").checked = subtitleConfig.enabled !== false;
  $("assrtEndpoints").value = (subtitleProviders.assrt?.endpoints || ["https://api.assrt.net", "https://api.makedie.me"]).join("\n");
  $("assrtToken").value = "";
  $("openSubtitlesEndpoint").value = (subtitleProviders.opensubtitles?.endpoints || ["https://api.opensubtitles.com/api/v1"])[0];
  $("openSubtitlesKey").value = "";
  const playback = config.playback || {};
  $("preferDirectPaths").checked = playback.preferDirectPaths !== false;
  $("playbackPublicUrl").value = playback.publicBaseUrl || "";
  $("playlistTtl").value = playback.playlistIdleSeconds || playback.playlistTtlSeconds || 43200;
  $("playlistMaximum").value = playback.playlistMaximumSeconds || 604800;
  $("directPathMappings").value = (playback.directPathMappings || [])
    .map((mapping) => `${mapping.serverPathPrefix} => ${mapping.clientPathPrefix}`)
    .join("\n");
  const network = config.metadata?.network || {};
  $("archiveManifestEndpoints").value = (network.archiveManifestEndpoints || []).join("\n");
  $("archiveAssetProxyTemplates").value = (network.archiveAssetProxyTemplates || []).join("\n");
  $("bangumiApiEndpoints").value = (network.bangumiApiEndpoints || []).join("\n");
  const automaticUpdate = config.applicationUpdate?.automaticCheck || {};
  $("automaticUpdateEnabled").checked = automaticUpdate.enabled !== false;
  $("automaticUpdateMode").value = automaticUpdate.mode || "notify";
  $("automaticUpdateTime").value = automaticUpdate.time || "04:35";
  loadLogs();
  showModalDialog($("settingsDialog"));
  startArchiveStatusPolling();
  applyDeploymentSettings();
  connectionStorage = null;
  loadSystemHealth();
}
async function saveSettings(e) {
  e.preventDefault();
  const nextConfig = JSON.parse(JSON.stringify(config)),
    p = nextConfig.torrentPolicy,
    c = enabled("contentClasses"),
    r = enabled("allowedResolutions"),
    g = enabled("allowedRegions");
  saveOperationalSettings(nextConfig);
  const archiveOrder = order("archiveGroups"), archiveEnabled = new Set([...$("archiveGroups").querySelectorAll("li")].filter(x => x.querySelector("input").checked).map(x => x.dataset.value));
  p.archiveGroupIds = archiveOrder;
  p.resourceGroups.filter((x) => p.archiveGroupIds.includes(x.id)).forEach((x) => (x.enabled = archiveEnabled.has(x.id)));
  p.allowUnlisted = {
    resourceGroup: $("otherGroupsEnabled").checked,
    sourceClass: c.has("__other__"),
    resolution: r.has("__other__"),
    subtitle: p.allowUnlisted?.subtitle !== false,
  };
  archiveOrder.forEach((id, i) => {
    const x = p.resourceGroups.find((y) => y.id === id);
    x.tier = i + 1;
    x.order = 1;
  });
  Object.keys(p.contentClasses).forEach(
    (k) => (p.contentClasses[k] = c.has(k)),
  );
  Object.keys(p.resolutions).forEach((k) => (p.resolutions[k] = r.has(k)));
  p.regions = Object.fromEntries(["china", "japan", "korea", "usa", "europe", "other"].map((key) => [key, g.has(key)]));
  p.serialSubtitle = p.serialSubtitle || {};
  p.serialSubtitle.language = $("serialSubtitleLanguage").value;
  p.serialSubtitle.enabledByLanguage = {};
  p.groupVariants = { archive: readGroupVersions("archiveGroups"), serial: {} };
  [["zh","serialGroupsZh"],["en","serialGroupsEn"],["ja","serialGroupsJa"]].forEach(([lang,id]) => {
    p.serialSubtitle.enabledByLanguage[lang] = [...$(id).querySelectorAll("li")].filter(x => x.querySelector("input").checked).map(x => x.dataset.value);
    p.groupVariants.serial[lang] = readGroupVersions(id);
  });
  p.strategyOrder = order("strategyOrder");
  p.contentClassPriority = mergeOrder(
    p.contentClassPriority,
    optionalOrder("contentClassPriority"),
  );
  p.resolutionPriority = mergeOrder(
    p.resolutionPriority,
    optionalOrder("resolutionPriority"),
  );
  p.subtitlePriority = mergeOrder(
    p.subtitlePriority || [],
    optionalOrder("subtitlePriority"),
  );
  p.bitDepthPriority = mergeOrder(
    p.bitDepthPriority || [],
    optionalOrder("bitDepthPriority"),
  );
  [["releaseStrategyPriority", "releaseStrategyPriority"], ["collectionRevisionPriority", "collectionRevisionPriority"], ["attachmentPriority", "attachmentPriority"], ["creationDatePriority", "creationDatePriority"], ["sizePriority", "sizePriority"]].forEach(([key,id]) => {
    p[key] = mergeOrder(p[key] || [], optionalOrder(id));
  });
  nextConfig.download.defaultStartMode = "stopped";
  delete nextConfig.download.allowExplicitAutoStart;
  delete nextConfig.download.autoStartMaximumTasksPerBatch;
  delete nextConfig.storageGuard.maximumQueuedTiB;
  delete nextConfig.storageGuard.maximumDailyAddTiB;
  nextConfig.components.downloadClient.submissionEnabled = $("submissionAllowed").checked;
  nextConfig.components.downloadClient.endpoint = $("qbtEndpoint").value.trim();
  nextConfig.components.aniRss = {
    ...(nextConfig.components.aniRss || {}),
    endpoint: $("aniRssEndpoint").value.trim() || "http://127.0.0.1:7789",
    mode: $("aniRssMode").value,
    mediaPath: $("aniRssMediaPath").value.trim(),
    syncMinutes: Math.max(5, +$("aniRssSyncMinutes").value || 30),
  };
  nextConfig.deployment.torrentPoolRoot = $("torrentPoolPath").value.trim();
  nextConfig.deployment.libraryUncRoot = $("libraryPath").value.trim();
  nextConfig.library.sleep = {
    enabled: $("librarySleepEnabled").checked,
    start: $("librarySleepStart").value || "00:00",
    end: $("librarySleepEnd").value || "08:00",
  };
  if (nextConfig.library.sleep.enabled && nextConfig.library.sleep.start === nextConfig.library.sleep.end) throw new Error(t("librarySleepInvalid"));
  nextConfig.deployment.qbtLibraryRoot = $("qbtLibraryPath").value.trim();
  const externalSource = {
    ...(nextConfig.externalLibraries || [])[0],
    id: nextConfig.externalLibraries?.[0]?.id || "external-read-only",
    kind: $("externalReadOnlyKind").value,
    enabled: $("externalReadOnlyEnabled").checked,
    path: $("externalReadOnlyPath").value.trim() || "/External",
    readOnly: true,
    scanMinutes: Math.max(
    5,
    +$("externalScanMinutes").value || 60,
    ),
  };
  nextConfig.externalLibraries = [externalSource, ...(nextConfig.externalLibraries || []).slice(1)];
  const lines = (id) => $(id).value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  nextConfig.subtitles = nextConfig.subtitles || {};
  nextConfig.subtitles.enabled = $("subtitlesEnabled").checked;
  const subtitleProviderMap = Object.fromEntries((nextConfig.subtitles.providers || []).map((item) => [item.id, item]));
  nextConfig.subtitles.providers = [
    { ...(subtitleProviderMap.assrt || {}), id: "assrt", name: "ASSRT", enabled: true, apiKeyEnv: "ASSRT_API_TOKEN", endpoints: lines("assrtEndpoints") },
    { ...(subtitleProviderMap.opensubtitles || {}), id: "opensubtitles", name: "OpenSubtitles", enabled: true, apiKeyEnv: "OPEN_SUBTITLES_API_KEY", endpoints: [$("openSubtitlesEndpoint").value.trim()].filter(Boolean) },
  ];
  const credentials = {
    qbittorrent: $("qbtApiKey").value.trim(),
    aniRss: $("aniRssApiKey").value.trim(),
    assrt: $("assrtToken").value.trim(),
    opensubtitles: $("openSubtitlesKey").value.trim(),
  };
  const directPathMappings = lines("directPathMappings").map((line) => {
    const separator = line.indexOf("=>");
    if (separator < 1 || separator >= line.length - 2)
      throw new Error(t("directPathMappings"));
    return {
      serverPathPrefix: line.slice(0, separator).trim(),
      clientPathPrefix: line.slice(separator + 2).trim(),
    };
  });
  const playbackIdle = Math.max(900, Math.min(172800, +$("playlistTtl").value || 43200));
  nextConfig.playback = {
    ...(nextConfig.playback || {}),
    enabled: true,
    preferDirectPaths: $("preferDirectPaths").checked,
    publicBaseUrl: $("playbackPublicUrl").value.trim(),
    playlistIdleSeconds: playbackIdle,
    playlistMaximumSeconds: Math.max(playbackIdle, Math.min(2592000, +$("playlistMaximum").value || 604800)),
    directPathMappings,
  };
  nextConfig.metadata.network = nextConfig.metadata.network || {};
  nextConfig.metadata.network.archiveManifestEndpoints = lines("archiveManifestEndpoints");
  nextConfig.metadata.network.archiveAssetProxyTemplates = lines("archiveAssetProxyTemplates");
  nextConfig.metadata.network.bangumiApiEndpoints = lines("bangumiApiEndpoints");
  if (!nextConfig.metadata.network.archiveManifestEndpoints.length || !nextConfig.metadata.network.bangumiApiEndpoints.length)
    throw new Error(t("metadataEndpointsRequired"));
  nextConfig.applicationUpdate = {
    automaticCheck: {
      enabled: $("automaticUpdateEnabled").checked,
      mode: $("automaticUpdateMode").value === "install" ? "install" : "notify",
      time: $("automaticUpdateTime").value || "04:35",
    },
  };

  // Commit configuration and newly entered credentials as one server-side transaction.
  retainPersistentOverrides(nextConfig);
  const savedSettings = await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config: nextConfig, credentials }),
  });
  config = nextConfig;
  renderConnectionPaths(savedSettings.storage || null);
  if (credentials.qbittorrent) {
    $("qbtApiKey").value = "";
    capabilities.qbtCredentialConfigured = true;
    $("qbtCredentialState").textContent = t("qbtCredentialConfigured");
  }
  if (credentials.aniRss) {
    $("aniRssApiKey").value = "";
    capabilities.aniRssCredentialConfigured = true;
    $("aniRssCredentialState").textContent = t("aniRssCredentialConfigured");
  }
  if (credentials.assrt) $("assrtToken").value = "";
  if (credentials.opensubtitles) $("openSubtitlesKey").value = "";
  try {
    capabilities = await api("/api/capabilities");
  } catch (_) {
    // The settings transaction is already complete; a status refresh failure must not report it as failed.
  }
  $("saveState").textContent = t(savedSettings.mediaScanStarted ? "savedScanning" : "saved");
  setTimeout(() => ($("saveState").textContent = ""), savedSettings.mediaScanStarted ? 6000 : 1600);
  page = 0;
  loadSystemHealth();
  search();
}
function archiveText(s) {
  return s.state === "failed"
    ? `${t("archiveFailed")}: ${localizedError(s.error)}`
    : s.state === "unchanged"
      ? t("archiveUnchanged")
      : s.state === "complete"
        ? `${t("archiveComplete")}+${fmt(s.addedWorks || 0)}`
        : ["checking", "building", "merging"].includes(s.state)
          ? t({ checking: "archiveChecking", building: "archivePreparing", merging: "archiveMerging" }[s.state])
          : "";
}
let archiveStatusTimer = null, archiveStatusGeneration = 0, archiveStatusRequest = null;
function stopArchiveStatusPolling() {
  clearTimeout(archiveStatusTimer);
  archiveStatusTimer = null;
  archiveStatusGeneration++;
}
async function refreshArchiveStatus(generation = archiveStatusGeneration) {
  if (!archiveStatusRequest) archiveStatusRequest = api("/api/archive/update").finally(() => { archiveStatusRequest = null; });
  const state = await archiveStatusRequest;
  if (generation === archiveStatusGeneration && $("settingsDialog").open) {
    $("archiveStatus").textContent = archiveText(state);
    $("updateArchive").disabled = ["checking", "building", "merging"].includes(state.state);
  }
  return state;
}
async function pollArchiveStatus() {
  const generation = archiveStatusGeneration;
  let delay = 15000;
  try {
    const state = await refreshArchiveStatus(generation);
    if (["checking", "building", "merging"].includes(state.state)) delay = 3000;
  } catch (error) {
    if (generation === archiveStatusGeneration && $("settingsDialog").open) {
      $("archiveStatus").textContent = error.message;
      $("updateArchive").disabled = false;
    }
    delay = 10000;
  }
  if (generation === archiveStatusGeneration && $("settingsDialog").open) {
    archiveStatusTimer = setTimeout(pollArchiveStatus, delay);
  }
}
function startArchiveStatusPolling() {
  stopArchiveStatusPolling();
  pollArchiveStatus();
}
$("settingsDialog").addEventListener("close", stopArchiveStatusPolling);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && $("settingsDialog").open) startArchiveStatusPolling();
});
async function startArchiveUpdate() {
  $("updateArchive").disabled = true;
  $("archiveStatus").textContent = t("archiveChecking");
  try {
    await api("/api/archive/update", { method: "POST" });
  } catch (error) {
    $("updateArchive").disabled = false;
    throw error;
  }
  startArchiveStatusPolling();
}
async function importArchive(file) {
  if (!file) return;
  $("archiveStatus").textContent = t("archiveImporting");
  $("importArchive").disabled = true;
  try {
    const headers = { "Content-Type": "application/octet-stream", "X-Archive-Name": encodeURIComponent(file.name) };
    await api("/api/archive/import", { method: "POST", headers, body: file });
    await startArchiveUpdate();
  } finally {
    $("importArchive").disabled = false;
    $("archiveFile").value = "";
  }
}
let layoutPreviewGeneration = 0;
$("previewLibraryLayout").onclick = async () => {
  const generation = ++layoutPreviewGeneration;
  $("layoutPreviewStatus").textContent = t("radarLoading");
  $("layoutPreviewResults").replaceChildren(); $("layoutPreviewExport").hidden = true;
  showModalDialog($("layoutPreviewDialog"));
  try {
    await api("/api/library/layout-preview", { method: "POST" });
    while (generation === layoutPreviewGeneration && $("layoutPreviewDialog").open) {
      const result = await api("/api/library/layout-preview");
      if (result.state === "failed") throw new Error(localizedError(result.error));
      if (result.state === "complete") {
        $("layoutPreviewStatus").textContent = interpolate(t("layoutPreviewDone"), result);
        $("layoutPreviewResults").innerHTML = result.items.length ? `<table class="data-table"><thead><tr><th>${esc(t("layoutPreviewCurrent"))}</th><th>${esc(t("layoutPreviewProposed"))}</th><th>${esc(t("layoutPreviewReason"))}</th></tr></thead><tbody>${result.items.slice(0, 200).map((item) => `<tr><td>${esc(item.current)}</td><td>${esc(item.proposed || "—")}</td><td>${esc(t(item.reason === "supplement" ? "layoutPreviewSupplement" : item.reason === "owner_review" ? "layoutPreviewOwner" : "layoutPreviewName"))}${item.conflict ? `<br><span class="error">${esc(t("layoutPreviewConflict"))}</span>` : ""}</td></tr>`).join("")}</tbody></table>` : "";
        $("layoutPreviewExport").hidden = false;
        $("layoutPreviewExport").onclick = () => {
          const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
          const anchor = document.createElement("a"); anchor.href = url; anchor.download = "AnimeMachine-folder-preview.json";
          anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        };
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 900));
    }
  } catch (error) { if (generation === layoutPreviewGeneration) $("layoutPreviewStatus").textContent = error.message; }
};
const maintenanceTasks = new Set();
async function verifyWorkLibrary(button, animeId, generation) {
  if (button.disabled) return;
  const originalText = button.textContent;
  const active = () => generation === detailGeneration && $("detailDialog").open;
  button.parentElement.querySelector(".audit-work-status")?.remove();
  button.disabled = true;
  button.textContent = t("verifyRunning");
  try {
    await api(`/api/anime/${animeId}/library/audit`, { method: "POST" });
    while (active()) {
      const state = (await api("/api/maintenance/status")).library;
      if (!active()) return;
      if (state.state === "failed") throw new Error(localizedError(state.error || t("maintenanceFailed")));
      if (state.state === "complete") { await showDetail(animeId); return; }
      if (state.state !== "running") throw new Error(t("maintenanceFailed"));
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  } catch (error) {
    if (active()) {
      const message = document.createElement("small");
      message.className = "audit-work-status error";
      message.setAttribute("role", "status");
      message.textContent = error.message;
      button.insertAdjacentElement("afterend", message);
    }
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}
function interpolate(template, values) {
  return template.replace(/\{(\w+)\}/g, (match, key) => values[key] ?? match);
}
async function runMaintenance(kind, source = "settings") {
  const definitions = {
    metadata: { endpoint: "/api/metadata/repair", buttons: ["repairMetadata"], status: "maintenanceStatus" },
    library: { endpoint: "/api/library/audit", buttons: ["auditLibrary"], status: "libraryMaintenanceStatus" },
    placeholders: { endpoint: "/api/library/placeholders", buttons: ["createPlaceholders", "radarPlaceholders"], status: source === "radar" ? "radarPlaceholderStatus" : "libraryMaintenanceStatus" },
  };
  const task = definitions[kind];
  if (!task) throw new Error(t("maintenanceUnsupported"));
  if (maintenanceTasks.has(kind)) return;
  const statusNode = $(task.status);
  maintenanceTasks.add(kind);
  task.buttons.forEach((id) => { $(id).disabled = true; });
  statusNode.classList.remove("error");
  statusNode.dataset.i18n = "maintenanceRunning";
  delete statusNode.dataset.i18nValues;
  delete statusNode.dataset.i18nSuffix;
  statusNode.textContent = t("maintenanceRunning");
  try {
    if (kind === "placeholders" && !config.deployment?.libraryUncRoot) throw new Error(t("placeholdersConnectionRequired"));
    await api(task.endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    for (;;) {
      const status = (await api("/api/maintenance/status"))[kind];
      if (!["running", "complete", "failed"].includes(status.state)) throw new Error(t("maintenanceFailed"));
      if (status.state === "failed") throw new Error(kind === "placeholders" && /read-only|unavailable|writable|connect/i.test(status.error || "") ? t("placeholdersConnectionRequired") : status.error || t("maintenanceFailed"));
      const values = { files: 0, ...Object.fromEntries(Object.entries(status).map(([key, value]) => [key, typeof value === "number" ? fmt(value) : value])) };
      if (kind === "library" && status.state === "running" && status.phase === "discovering") {
        statusNode.dataset.i18n = "mediaScanProgress";
        statusNode.dataset.i18nValues = JSON.stringify(values);
        statusNode.textContent = interpolate(t("mediaScanProgress"), values);
        await new Promise((resolve) => setTimeout(resolve, 1500));
        continue;
      }
      if (kind !== "metadata") {
        statusNode.dataset.i18n = status.state === "running" && !status.total ? "maintenanceRunning" : kind === "library" ? "auditProgress" : status.state === "running" ? "placeholdersProgress" : "placeholdersResult";
        statusNode.dataset.i18nValues = JSON.stringify(values);
      } else delete statusNode.dataset.i18n;
      if (status.state === "running" && !status.total && kind !== "metadata") statusNode.textContent = t("maintenanceRunning");
      else if (kind === "metadata") statusNode.textContent = `${t("repairMetadata")} · ${fmt(status.repaired)}/${fmt(status.processed)}`;
      else statusNode.textContent = interpolate(t(kind === "library" ? "auditProgress" : status.state === "running" ? "placeholdersProgress" : "placeholdersResult"), values);
      if (status.state !== "running") {
        statusNode.dataset.i18nSuffix = status.skipped || status.failed || status.unavailable ? "maintenanceSkippedHint" : "maintenanceFinished";
        statusNode.textContent += ` · ${t(statusNode.dataset.i18nSuffix)}`;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  } catch (error) {
    delete statusNode.dataset.i18n;
    delete statusNode.dataset.i18nValues;
    delete statusNode.dataset.i18nSuffix;
    if (error.message === t("placeholdersConnectionRequired")) statusNode.dataset.i18n = "placeholdersConnectionRequired";
    statusNode.classList.add("error");
    statusNode.textContent = error.message;
  } finally {
    maintenanceTasks.delete(kind);
    task.buttons.forEach((id) => { $(id).disabled = false; });
  }
}
async function testConnection(kind) {
  const isAniRss = kind === "ani-rss",
    endpoint = $(isAniRss ? "aniRssEndpoint" : "qbtEndpoint").value,
    apiKey = $(isAniRss ? "aniRssApiKey" : "qbtApiKey").value.trim(),
    state = $(isAniRss ? "aniRssState" : "qbtState"),
    button = $(isAniRss ? "testAniRss" : "testQbt");
  if (button.disabled) return;
  button.disabled = true;
  const unchanged = () => $(isAniRss ? "aniRssEndpoint" : "qbtEndpoint").value === endpoint && $(isAniRss ? "aniRssApiKey" : "qbtApiKey").value.trim() === apiKey;
  state.textContent = "…";
  try {
    const r = await api("/api/connections/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, endpoint, apiKey }),
    });
    if (!unchanged()) { state.textContent = ""; return; }
    state.textContent = r.message === "service_redirect" ? t("errorServiceRedirect") : r.authenticated
      ? `${t("connectionOk")}${r.version ? ` · ${r.version}` : ""}`
      : r.reachable || r.message === "credentials_required"
        ? t("serviceCredentialsRequired")
        : t("connectionFailed");
  } catch (e) {
    state.textContent = unchanged() ? `${t("connectionFailed")}: ${e.message}` : "";
  } finally {
    button.disabled = false;
  }
}
let logRecords = [], historyRecords = [];
function recordTime(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat(localeForLanguage(), { dateStyle: "short", timeStyle: "medium" }).format(date) : "—";
}
function logMessage(message) {
  const names = {
    ani_rss_sync_complete: ["Ani-RSS 状态已同步", "Ani-RSS state synchronized", "Ani-RSS の状態を同期しました"],
    ani_rss_sync_failed: ["Ani-RSS 同步未完成", "Ani-RSS synchronization failed", "Ani-RSS を同期できませんでした"],
    ani_rss_resource_refresh_complete: ["资源检索已完成", "Resource search completed", "リソース検索が完了しました"],
    ani_rss_resource_refresh_failed: ["资源检索未完成", "Resource search did not complete", "リソース検索を完了できませんでした"],
    torrent_search_complete: ["Torrent 检索已完成", "Torrent search completed", "Torrent 検索が完了しました"],
    torrent_search_failed: ["Torrent 检索未完成", "Torrent search did not complete", "Torrent 検索を完了できませんでした"],
    qbt_state_refresh_failed: ["下载器状态暂不可用", "Downloader status is unavailable", "ダウンローダーの状態を取得できません"],
    seasonal_covers_prepared: ["当季封面已就绪", "Seasonal covers are ready", "今期のカバーを準備しました"],
    seasonal_cover_pack_invalid: ["预置封面未通过校验", "The bundled cover check failed", "同梱カバーの検証に失敗しました"],
    application_update_check_failed: ["暂时无法检查新版", "Update check is unavailable", "更新を確認できませんでした"],
    archive_schedule_failed: ["底包检查未完成", "Catalog update check did not complete", "カタログの更新を確認できませんでした"],
  };
  if (names[message]) return names[message][li()];
  if (!/^[a-z][a-z0-9_]+$/.test(message)) return message;
  return t(/(?:fail|error)/.test(message) ? "logTaskFailed" : /(?:complete|ready)$/.test(message) ? "logTaskComplete" : "logTaskStatus");
}
function renderLogs() {
  const query = $("logSearch").value.trim().toLocaleLowerCase(), problems = $("logFilter").value === "problems";
  const rows = logRecords.filter((item) => (!problems || ["WARNING", "ERROR", "CRITICAL"].includes(item.level))
    && `${logMessage(item.message)} ${item.message} ${typeof item.details === "string" ? item.details : JSON.stringify(item.details)}`.toLocaleLowerCase().includes(query));
  $("logList").innerHTML = rows.length ? rows.map((item) => {
    const level = ["ERROR", "CRITICAL"].includes(item.level) ? "error" : item.level === "WARNING" ? "warning" : "info";
    let details = item.details || "";
    if (typeof details !== "string") details = JSON.stringify(details, null, 2);
    else { try { details = JSON.stringify(JSON.parse(details), null, 2); } catch {} }
    return `<article class="record log-record ${level}"><div class="record-heading"><span class="record-badge">${esc(t({ error: "logError", warning: "logWarning", info: "logInfo" }[level]))}</span><strong>${esc(logMessage(item.message))}</strong><time>${esc(recordTime(item.at))}</time></div><details class="record-details"><summary>${t("technicalDetails")}</summary><pre>${esc(item.message)}${details ? "\n" + esc(details) : ""}</pre></details></article>`;
  }).join("") : `<p class="empty">${t("noLogs")}</p>`;
}
async function loadLogs() {
  const button = $("refreshLogs"); button.disabled = true;
  try { logRecords = (await api("/api/logs")).items; renderLogs(); }
  catch (error) { $("logList").innerHTML = `<p class="error">${esc(error.message)}</p>`; }
  finally { button.disabled = false; }
}
function renderHistory() {
  const query = $("historySearch").value.trim().toLocaleLowerCase();
  const rows = historyRecords.filter((item) => `${item.sourcePath || ""} ${item.targetPath || ""} ${item.backupPath || ""}`.toLocaleLowerCase().includes(query));
  $("historyList").innerHTML = rows.length ? rows.map((item) => {
    const operation = item.state === "restored" ? t("historyRestored") : t({ move: "historyMove", rename: "historyRename", remove: "historyRemove", create_directory: "historyCreate" }[item.operation] || "historyOther");
    const destination = item.operation === "remove" ? item.backupPath : item.targetPath;
    const source = item.sourcePath;
    const restore = item.operation === "remove" && item.state === "applied" ? `<button type="button" class="text-button restore-history" data-icon="refresh" data-event="${esc(item.eventId)}">${t("restoreHistory")}</button>` : "";
    return `<article class="record history-record"><div class="record-heading"><span class="record-badge">${esc(operation)}</span><time>${esc(recordTime(item.createdAt))}</time>${restore}</div><dl class="record-paths">${source && item.operation !== "create_directory" ? `<dt>${t("historySource")}</dt><dd>${esc(source)}</dd>` : ""}${destination ? `<dt>${t("historyTarget")}</dt><dd>${esc(destination)}</dd>` : ""}</dl>${item.bytes ? `<small class="muted">${esc(fmt(item.bytes))} B</small>` : ""}</article>`;
  }).join("") : `<p class="empty">${t("noHistory")}</p>`;
  $("historyList").querySelectorAll(".restore-history").forEach((button) => button.onclick = async () => {
    button.disabled = true;
    try { await api(`/api/history/${encodeURIComponent(button.dataset.event)}/restore`, { method: "POST" }); await loadHistory(); }
    catch (error) { alert(error.message); button.disabled = false; }
  });
}
async function loadHistory() {
  const button = $("refreshHistory"); button.disabled = true;
  try { historyRecords = (await api("/api/history?limit=200")).items; renderHistory(); }
  catch (error) { $("historyList").innerHTML = `<p class="error">${esc(error.message)}</p>`; }
  finally { button.disabled = false; }
}
$("logSearch").oninput = renderLogs;
$("logFilter").onchange = renderLogs;
$("historySearch").oninput = renderHistory;
function watchProgressText(item) {
  const sequence = Number(item.lastSequence || 0),
    key = { episode: "watchEpisode", volume: "watchVolume" }[item.releaseUnit];
  return [
    key && sequence > 0 ? interpolate(t(key), { sequence: fmt(sequence) }) : "",
    interpolate(t("watchPending"), { count: fmt(Number(item.pendingCount) || 0) }),
  ].filter(Boolean).join(" · ");
}
async function loadWatches() {
  try {
    const r = await api(`/api/watches?language=${encodeURIComponent(language)}`);
    $("watchList").innerHTML = r.items.length
      ? r.items.map((x) => `<div class="log"><b>${esc(x.title)}</b><span>${esc(sourceClassText(x.sourceClass))} · ${esc(x.resourceGroup || t("other"))} · ${esc(x.resolution ? `${x.resolution}p` : t("other"))} · ${esc(x.subtitle || t("other"))}</span><small>${esc(watchProgressText(x))}</small><button type="button" class="text-button remove-watch" data-icon="remove" data-watch="${x.watchId}">${t("removeWatch")}</button></div>`).join("")
      : `<p class="muted">${t("noWatches")}</p>`;
    document.querySelectorAll(".remove-watch").forEach((button) => button.onclick = async () => {
      await api(`/api/watches/${button.dataset.watch}`, { method: "DELETE" });
      loadWatches();
    });
  } catch (e) {
    $("watchList").innerHTML = `<p class="error">${esc(e.message)}</p>`;
  }
}
async function persistUi() {
  const preferences = {
    language,
    catalogView: view,
    imagesEnabled,
    pageSize: pageSize === "all" ? "all" : +pageSize,
    sort: configuredSort,
    sortDirection: direction,
  };
  try {
    localStorage.setItem(uiStorageKey(), JSON.stringify(preferences));
  } catch (_) {
    $("buildInfo").textContent = t("preferencesSaveFailed");
  }
}
function uiStorageKey() {
  return `anm-ui-v1:${authSession?.username || "local"}`;
}
function storedUi() {
  try {
    const saved = JSON.parse(localStorage.getItem(uiStorageKey()) || "{}");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
    const clean = {};
    if (["cards", "table"].includes(saved.catalogView)) clean.catalogView = saved.catalogView;
    if ([12, 20, 50, 100, "all"].includes(saved.pageSize)) clean.pageSize = saved.pageSize;
    if (["zh-Hans", "en", "ja"].includes(saved.language)) clean.language = saved.language;
    if (["recent_episode", "random", "title", "date", "studio", "type"].includes(saved.sort)) clean.sort = saved.sort;
    if (["asc", "desc"].includes(saved.sortDirection)) clean.sortDirection = saved.sortDirection;
    if (typeof saved.imagesEnabled === "boolean") clean.imagesEnabled = saved.imagesEnabled;
    return clean;
  } catch (_) {
    return {};
  }
}
function archiveVersion(s) {
  const nameMatch = String(s?.archive_name || "").match(/((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})/);
  if (nameMatch) return `${nameMatch[1]}${nameMatch[2]}${nameMatch[3]}`;
  const created = new Date(s?.archive_created_at || "");
  if (Number.isNaN(created.valueOf())) return "—";
  return `${created.getUTCFullYear()}${String(created.getUTCMonth() + 1).padStart(2, "0")}${String(created.getUTCDate()).padStart(2, "0")}`;
}
function archiveSummary(s) {
  catalogStats = s || {};
  return t("archiveBuildSummary")
    .replace("{version}", archiveVersion(catalogStats))
    .replace("{count}", fmt(Number(catalogStats.record_count || 0)));
}
function localizedMonth(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})$/);
  if (!match) return "—";
  const year = Number(match[1]), month = Number(match[2]);
  if (year < 1 || month < 1 || month > 12) return "—";
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, 1);
  date.setUTCHours(0, 0, 0, 0);
  return new Intl.DateTimeFormat(localeForLanguage(), { year: "numeric", month: "2-digit", timeZone: "UTC" }).format(date);
}
function diagnosticRouteLabel(route) {
  const mode = String(route?.mode || "direct");
  const name = mode === "environment_proxy" ? t("routeEnvironment")
    : mode === "windows_system_proxy" ? t("routeWindows")
    : mode === "system_proxy" ? t("routeSystem") : t("routeDirect");
  return route?.proxy ? `${name} · ${route.proxy}` : name;
}
function formatDiagnosticRate(bytesPerSecond) {
  const value = Number(bytesPerSecond || 0);
  if (!value) return "—";
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MiB/s`;
  return `${Math.round(value / 1024)} KiB/s`;
}
function formatEta(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 60) return `${Math.max(1, Math.round(value))}s`;
  const minutes = Math.round(value / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}
function selectionReasonText(reason) {
  return t({
    incumbent_best: "selectionIncumbentBest",
    best_quality: "selectionBestQuality",
    hysteresis_hold: "selectionHysteresisHold",
    hysteresis_margin: "selectionHysteresisMargin",
    incumbent_cooldown: "selectionIncumbentCooldown",
    candidate_cooldown: "selectionCandidateCooldown",
    meaningfully_better: "selectionMeaningfullyBetter",
  }[String(reason || "")] || "selectionBestQuality");
}
function budgetReasonText(reason) {
  const keys = String(reason || "idle").split("+").filter(Boolean).map((value) => ({
    idle: "budgetIdle", foreground: "budgetForeground", foreground_latency: "budgetForegroundLatency",
    cpu: "budgetCpu", io: "budgetIo", network: "budgetNetwork",
  }[value])).filter(Boolean);
  return [...new Set(keys)].map((key) => t(key)).join(" · ") || t("budgetIdle");
}
function renderStartupDiagnostics(startup) {
  if (!startup) return;
  const key = startup.state === "Starting" ? "startupStarting"
    : startup.state === "Warming" ? "startupWarming"
    : startup.state === "Warm" ? "startupWarm" : "startupReady";
  if ($("startupDiagnostics")) {
    const performance = startup.performance || {},
      formatTiming = (value) => value == null ? "—" : Number(value) < 1000 ? `${Math.round(Number(value))} ms` : `${(Number(value) / 1000).toFixed(2)} s`,
      previous = performance.previous || {},
      metrics = [
        ["perfCatalogReady", performance.catalogReadyMs, previous.catalogReadyMs],
        ["perfFirstScreen", performance.firstScreenMs, previous.firstScreenMs],
        ["perfFirstCover", performance.firstCoverMs, previous.firstCoverMs],
        ["perfWarmComplete", performance.warmCompleteMs, previous.warmCompleteMs],
      ];
    $("startupDiagnostics").innerHTML = `${startup.catalogReady ? `<span class="catalog-ready-badge">${esc(t("catalogAvailable"))}</span> ` : ""}${esc(t(key))}<div class="performance-baseline"><small>${esc(t("performanceBaseline"))}</small>${metrics.map(([label, value, prior]) => `<span><b>${esc(t(label))}</b> ${esc(formatTiming(value))}${prior == null ? "" : ` <small>(${esc(t("previousRun"))} ${esc(formatTiming(prior))})</small>`}</span>`).join("")}</div>`;
  }
}
function diagnosticTrendSparkline(points) {
  const values = (points || []).map((point) => Number(point.score) * 100).filter(Number.isFinite);
  if (!values.length) return `<span class="muted trend-empty">${esc(t("noTrendData"))}</span>`;
  const width = 150, height = 32, pad = 3;
  const coordinates = values.map((value, index) => {
    const x = values.length === 1 ? width / 2 : pad + index * (width - pad * 2) / (values.length - 1);
    const y = height - pad - Math.max(0, Math.min(100, value)) * (height - pad * 2) / 100;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `<svg class="trend-sparkline" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(t("imageSourceTrend"))}"><line x1="3" y1="29" x2="147" y2="29"></line><polyline points="${coordinates}"></polyline></svg>`;
}
function renderImageSourceTrends(item) {
  if (item?.service !== "bangumi_image" || !item?.trends) return "";
  const routes = [
    ["direct", t("routeDirect")],
    ["environment_proxy", t("routeEnvironment")],
    ["windows_system_proxy", t("routeWindows")],
  ];
  if ((item.trends.system_proxy || []).length) routes.push(["system_proxy", t("routeSystem")]);
  return `<div class="image-route-trends"><div class="trend-heading"><b>${esc(t("imageSourceTrend"))}</b><small>${esc(t("trend12h"))}</small></div>${routes.map(([mode, label]) => {
    const points = item.trends[mode] || [], latest = points.at(-1);
    const summary = latest ? `${Math.round(Number(latest.successRate || 0) * 100)}% · ${latest.latencyMs == null ? "—" : `${Math.round(Number(latest.latencyMs))} ms`}` : "—";
    return `<div class="trend-route"><span><b>${esc(label)}</b><small>${esc(summary)}</small></span>${diagnosticTrendSparkline(points)}</div>`;
  }).join("")}</div>`;
}
function renderNetworkDiagnostics(payload) {
  if (!$("networkDiagnostics")) return;
  const profile = payload?.networkProfile || {};
  $("networkRouteSummary").textContent = `${t("networkProfile")}: ${profile.label || profile.kind || "—"} · ${t("currentRoute")}: ${diagnosticRouteLabel(payload?.route)}`;
  const items = payload?.items || [];
  $("networkDiagnostics").innerHTML = items.map((item) => {
    const success = item.recentSuccessRate == null ? "—" : `${Math.round(Number(item.recentSuccessRate) * 100)}%`;
    const latency = item.latencyMs == null ? "—" : `${Math.round(Number(item.latencyMs))} ms`;
    const throughput = item.throughputKiBps == null ? "—" : `${Math.round(Number(item.throughputKiBps))} KiB/s`;
    const failure = item.lastFailure || t("noRecentFailure"), selection = item.selection || {};
    const selectionDetail = selection.selected
      ? `<small class="source-selection"><b>${esc(t("sourceSelected"))}</b> · ${esc(selectionReasonText(selection.reason))} · ${esc(t("sourceConfidence"))} ${Math.round(Number(selection.confidence || 0) * 100)}% · ${esc(t("sourceScore"))} ${Math.round(Number(selection.quality || 0) * 100)}</small>`
      : "";
    return `<div class="diagnostic-item"><div><b>${esc(item.label || item.service)}</b><small>${esc(item.baseUrl || "")} · ${esc(diagnosticRouteLabel(item.route))}</small>${selectionDetail}</div><span><small>${t("successRate")}</small><br>${esc(success)}</span><span><small>${t("latency")}</small><br>${esc(latency)}</span><span><small>${t("throughput")}</small><br>${esc(throughput)}</span><span><small>${t("lastFailure")}</small><br>${esc(failure)}</span>${renderImageSourceTrends(item)}</div>`;
  }).join("") || `<p class="muted">—</p>`;
}
function renderImagePreload(payload) {
  if (!$("imagePreloadStages")) return;
  const labels = { recent: t("recentSixMonths"), history: t("historyCatalog"), retry: t("failedRetry") };
  const stages = payload?.stages || {};
  $("imagePreloadStages").innerHTML = ["recent", "history", "retry"].map((name) => {
    const stage = stages[name] || {}, done = Number(stage.done || 0), total = Number(stage.total || 0), failed = Number(stage.failed || 0);
    const percent = total > 0 ? Math.min(100, Math.round(done * 100 / total)) : (payload?.state === "warm" ? 100 : 0);
    return `<div class="preload-stage"><b>${esc(labels[name])}</b><progress max="100" value="${percent}"></progress><span>${fmt(done)}/${fmt(total)}${failed ? ` · ${t("failed")} ${fmt(failed)}` : ""}</span></div>`;
  }).join("");
  const remaining = Number(payload?.estimatedRemaining || 0), current = payload?.current || {},
    eta = payload?.estimatedSeconds, rate = Number(payload?.throughputItemsPerSecond || 0), budget = payload?.resourceBudget || {};
  if ($("imagePreloadMeta")) {
    const budgetConcurrency = Number(budget.effectiveConcurrency);
    $("imagePreloadMeta").innerHTML = `<span><b>${esc(t("estimatedRemaining"))}</b> ${fmt(remaining)}</span>`
      + `${remaining > 0 && eta != null ? `<span><b>${esc(t("estimatedTime"))}</b> ${esc(formatEta(eta))}</span>` : ""}`
      + `${rate > 0 ? `<span><b>${esc(t("preloadRate"))}</b> ${rate.toFixed(rate < 1 ? 2 : 1)}/s</span>` : ""}`
      + `${Number.isFinite(budgetConcurrency) ? `<span><b>${esc(t("adaptiveConcurrency"))}</b> ${fmt(budgetConcurrency)} · ${esc(budgetReasonText(budget.reason))}</span>` : ""}`
      + `${(preferred(current) || current.title) ? `<span><b>${esc(t("currentItem"))}</b> ${esc(preferred(current) || current.title)}${Number(current.batchSize || 0) > 1 ? ` +${fmt(Number(current.batchSize) - 1)}` : ""}</span>` : ""}`;
  }
  const controls = payload?.controls || {};
  if (document.activeElement !== $("imagePreloadConcurrency")) $("imagePreloadConcurrency").value = Number(controls.concurrency || 1);
  if (document.activeElement !== $("imagePreloadBandwidth")) $("imagePreloadBandwidth").value = Number(controls.bandwidthKiBps || 0);
  const paused = Boolean(controls.paused);
  $("toggleImagePreload").dataset.paused = paused ? "1" : "0";
  $("toggleImagePreload").dataset.icon = paused ? "play" : "pause";
  $("toggleImagePreload").textContent = t(paused ? "resumePreload" : "pausePreload");
  $("imagePreloadStatus").textContent = paused ? t("preloadPaused")
    : payload?.state === "waiting_network" ? t("preloadWaitingNetwork")
    : payload?.state === "warm" ? t("preloadWarm")
    : `${t("preloadRunning")}${payload?.stage ? ` · ${labels[payload.stage] || payload.stage}` : ""}`;
}
function renderPlaybackDiagnostics(payload) {
  if (!$("playbackDiagnostics")) return;
  const items = payload?.items || [];
  $("playbackDiagnostics").innerHTML = items.map((item) =>
    `<div class="diagnostic-item"><div><b>${esc(item.name || item.token || "—")}</b><small>${esc(item.source || "")} · ${esc(item.state || "")}</small></div><span><small>${t("rangeHeader")}</small><br>${esc(item.range || "full")}</span><span><small>${t("resumeCount")}</small><br>${fmt(item.resumeCount || 0)}</span><span><small>${t("upstreamState")}</small><br>${esc(item.upstream || "—")}</span><span><small>${t("transferRate")}</small><br>${esc(formatDiagnosticRate(item.rateBps))}</span></div>`
  ).join("") || `<p class="muted">${t("noPlaybackSessions")}</p>`;
}
function systemHealthLabels() {
  return {
    names: { network: t("healthNetwork"), aniRss: t("healthAniRss"), qbittorrent: t("healthQbittorrent"), storage: t("healthStorage"), imagePreload: t("healthImagePreload"), playback: t("healthPlayback") },
    details: { ready: t("healthReady"), sleeping: t("librarySleeping"), unknown: t("healthUnknown"), degraded: t("healthDegraded"), unavailable: t("healthUnavailable"), not_configured: t("healthNotConfigured"), disabled: t("healthDisabled"), manual: t("healthManual"), warming: t("healthWarming"), warm: t("healthWarm"), failed: t("healthFailed") },
  };
}
const connectionPathKeys = {
  libraryPath: "library", torrentPoolPath: "torrentPool", aniRssMediaPath: "aniRssMedia", externalReadOnlyPath: "external1",
};
let connectionStorage = null, connectionHealthGeneration = 0, connectionHealthLoading = false;
function renderConnectionPaths(storage = connectionStorage) {
  connectionStorage = storage;
  for (const [id, key] of Object.entries(connectionPathKeys)) {
    const field = $(id), target = $(`${id}Status`);
    if (!field || !target) continue;
    const path = field.value.trim(), item = storage?.[key];
    const configPath = Object.keys(deploymentControls).find((name) => deploymentControls[name] === id);
    const saved = capabilities.configurationOverrides?.[configPath] ?? configPath.split(".").reduce((value, part) => value?.[part], config) ?? "";
    const disabled = id === "externalReadOnlyPath" && !$("externalReadOnlyEnabled").checked;
    const enabledChanged = id === "externalReadOnlyPath" && $("externalReadOnlyEnabled").checked !== Boolean(config.externalLibraries?.[0]?.enabled);
    let state, text;
    if (path !== String(saved).trim() || enabledChanged) { state = "pending"; text = t("pathSaveToCheck"); }
    else if (disabled) { state = "disabled"; text = t("healthDisabled"); }
    else if (!path) { state = "not_configured"; text = t("healthNotConfigured"); }
    else if (!item || item.path !== path) { state = "unknown"; text = t(connectionHealthLoading ? "healthLoading" : "pathNotChecked"); }
    else {
      state = item.state;
      text = t(({ available: "pathReadable", sleeping: "librarySleeping", not_configured: "healthNotConfigured",
        share_not_found: "pathMissing", permission_denied: "pathPermissionDenied", authentication_failed: "pathCredentialsInvalid" })[state] || "pathUnavailable");
    }
    target.dataset.state = state;
    target.textContent = text;
  }
}
for (const id of [...Object.keys(connectionPathKeys), "externalReadOnlyEnabled"]) {
  $(id).addEventListener(id === "externalReadOnlyEnabled" ? "change" : "input", () => renderConnectionPaths());
}
$("settingsDialog").addEventListener("close", () => { connectionHealthGeneration++; connectionHealthLoading = false; });
function systemHealthExtra(item) {
  const info = item?.info || {}, parts = [];
  if (item?.id === "network") {
    parts.push(`${t("currentRoute")}: ${diagnosticRouteLabel({ mode: info.route })}`);
    parts.push(`${t("healthSamples")}: ${fmt(Number(info.sampled || 0))}`);
    if (Number(info.degraded || 0)) parts.push(`${t("healthWarnings")}: ${fmt(Number(info.degraded))}`);
  } else if (item?.id === "aniRss") {
    parts.push(`${t("healthMode")}: ${String(info.mode || "—")}`);
    parts.push(`${t("healthState")}: ${String(info.connectionState || "—")}`);
  } else if (item?.id === "qbittorrent") {
    parts.push(`${t("healthState")}: ${info.enabled === false ? t("healthDisabled") : (item.detail ? (systemHealthLabels().details[item.detail] || item.detail) : "—")}`);
  } else if (item?.id === "storage") {
    parts.push(`${t("healthChecked")}: ${fmt(Number(info.checked || 0))}`);
    if (Number(info.warnings || 0)) parts.push(`${t("healthWarnings")}: ${fmt(Number(info.warnings))}`);
  } else if (item?.id === "imagePreload") {
    parts.push(`${t("healthState")}: ${String(info.stage || info.state || "—")}`);
    parts.push(`${t("estimatedRemaining")}: ${fmt(Number(info.estimatedRemaining || 0))}`);
    if (info.current) {
      const currentTitle = typeof info.current === "object"
        ? (preferred(info.current) || info.current.title || "") : String(info.current);
      if (currentTitle) parts.push(`${t("currentItem")}: ${currentTitle}`);
    }
  } else if (item?.id === "playback") {
    parts.push(`${t("healthSessions")}: ${fmt(Number(info.sessions || 0))}`);
    if (Number(info.degraded || 0)) parts.push(`${t("healthWarnings")}: ${fmt(Number(info.degraded))}`);
  }
  return parts.join(" · ");
}
function renderSystemHealth(payload) {
  renderConnectionPaths(payload?.storage || null);
  if (!$("systemHealthItems")) return;
  const { names, details } = systemHealthLabels();
  const warning = payload?.status === "warning";
  $("systemHealthSummary").className = `system-health-summary ${warning ? "warning" : "normal"}`;
  $("systemHealthSummary").textContent = t(warning ? "healthOverallWarning" : "healthOverallNormal");
  $("systemHealthItems").innerHTML = (payload?.items || []).map((item) => {
    const isWarning = item.status === "warning";
    return `<div class="system-health-item ${isWarning ? "warning" : "normal"}"><span class="health-dot" aria-hidden="true"></span><span><b>${esc(names[item.id] || item.id)}</b><small>${esc(details[item.detail] || item.detail || "")}</small></span><strong>${esc(t(isWarning ? "healthWarning" : "healthNormal"))}</strong></div>`;
  }).join("");
}
function renderSystemHealthDiagnostics(payload) {
  if (!$("systemHealthDiagnostics")) return;
  const { names, details } = systemHealthLabels();
  $("systemHealthDiagnostics").innerHTML = (payload?.items || []).map((item) => {
    const isWarning = item.status === "warning", extra = systemHealthExtra(item);
    const detail = [details[item.detail] || item.detail || "", extra].filter(Boolean).join(" · ");
    return `<div class="system-health-item ${isWarning ? "warning" : "normal"}"><span class="health-dot" aria-hidden="true"></span><span><b>${esc(names[item.id] || item.id)}</b><small title="${esc(detail)}">${esc(detail)}</small></span><strong>${esc(t(isWarning ? "healthWarning" : "healthNormal"))}</strong></div>`;
  }).join("") || `<p class="muted">—</p>`;
}
async function loadSystemHealth() {
  if (!$("systemHealthItems")) return;
  const generation = ++connectionHealthGeneration;
  connectionHealthLoading = true;
  renderConnectionPaths();
  $("systemHealthSummary").className = "system-health-summary muted";
  $("systemHealthSummary").textContent = t("healthLoading");
  try {
    const payload = await api("/api/system/health");
    if (generation !== connectionHealthGeneration) return;
    connectionHealthLoading = false;
    renderSystemHealth(payload);
  } catch (error) {
    if (generation !== connectionHealthGeneration) return;
    connectionHealthLoading = false;
    renderConnectionPaths(null);
    $("systemHealthSummary").className = "system-health-summary warning";
    $("systemHealthSummary").textContent = `${t("healthOverallWarning")} · ${error.message}`;
    $("systemHealthItems").innerHTML = "";
  }
}
let diagnosticTimer, diagnosticRequest = 0;
function renderDiagnosticFailure(id, error) {
  const target = $(id);
  if (target) target.innerHTML = `<p class="error">${esc(error?.message || String(error || "error"))}</p>`;
}
async function loadDiagnostics() {
  clearTimeout(diagnosticTimer);
  const request = ++diagnosticRequest;
  if (!$('settingsDialog')?.open || !document.querySelector('[data-panel="diagnostics"]')?.classList.contains('active')) return;
  const renderers = [
    ["/api/startup/state", renderStartupDiagnostics, "startupDiagnostics"],
    ["/api/system/health", renderSystemHealthDiagnostics, "systemHealthDiagnostics"],
    ["/api/diagnostics/network", renderNetworkDiagnostics, "networkDiagnostics"],
    ["/api/images/preload", renderImagePreload, "imagePreloadStages"],
    ["/api/diagnostics/playback", renderPlaybackDiagnostics, "playbackDiagnostics"],
  ];
  await Promise.allSettled(renderers.map(async ([url, render, target]) => {
    try {
      const payload = await api(url, { timeoutMs: 10000 });
      if (request === diagnosticRequest) render(payload);
    } catch (error) {
      if (request === diagnosticRequest) renderDiagnosticFailure(target, error);
    }
  }));
  if (request === diagnosticRequest && $('settingsDialog')?.open && document.querySelector('[data-panel="diagnostics"]')?.classList.contains('active'))
    diagnosticTimer = setTimeout(loadDiagnostics, 2500);
}
function renderScanProgress(s, startup = null) {
  if (startup) startupState = startup;
  const state = startupState || {}, preload = state.preload || {},
    bar = $("scanProgressBar"), box = $("scanProgress"), catalogBadge = $("catalogReadyBadge");
  if (catalogBadge) {
    catalogBadge.hidden = false;
    catalogBadge.textContent = t(state.catalogReady ? "catalogAvailable" : "catalogPreparing");
  }
  if (state.catalogReady === false) {
    const sync = s.sync || {}, details = sync.details || {},
      phaseKey = { archive_resolve: "catalogResolving", archive_download: "catalogDownloading",
        archive_ready: "catalogParsing", catalog_parse: "catalogParsing", catalog_write: "catalogWriting",
      }[sync.phase] || "catalogPreparing",
      downloadTotal = Number(details.total || 0), received = Number(details.received || 0);
    let text = t(sync.state === "error" ? "catalogPreparingFailed" : phaseKey);
    if (sync.phase === "archive_download" && downloadTotal > 0) {
      text += ` · ${(received / 1048576).toFixed(1)} / ${(downloadTotal / 1048576).toFixed(1)} MiB`;
      bar.value = Math.min(100, received * 100 / downloadTotal);
    } else {
      if (sync.phase === "catalog_write" && details.records) text += ` · ${fmt(details.records)}`;
      bar.removeAttribute("value");
    }
    $("scanProgressText").textContent = text;
    box.classList.remove("idle");
    return;
  }
  if (s.catalog_scope === "season") {
    $("scanProgressText").textContent = t(s.sync?.state === "error" ? "catalogFullDeferred" : "catalogSeasonReady");
    bar.removeAttribute("value");
    box.classList.remove("idle");
    return;
  }
  const frontier = String(preload.preparedThroughMonth || "");
  const displayComplete = state.state === "Warm" || preload.state === "warm" ||
    (Boolean(monthParts(frontier)) && frontier < "1980-01");
  let imageStatus = "backgroundImagesPreparing";
  if (displayComplete) imageStatus = "backgroundImagesComplete";
  else if (preload.state === "paused") imageStatus = "backgroundImagesPaused";
  else if (preload.state === "waiting_network") imageStatus = "backgroundImagesWaiting";
  const frontierLabel = language.startsWith("zh") ? frontier : localizedMonth(frontier);
  const detail = frontier && frontierLabel !== "—"
    ? t("imagesPreparedThrough").replace("{month}", frontierLabel)
    : t("imagesPreparingBackward");
  const separator = " · ";
  const statusSuffix = language.startsWith("zh") ? "；" : language.startsWith("ja") ? "；" : ";";
  $("scanProgressText").textContent = displayComplete ? t(imageStatus)
    : `${t(imageStatus)}${separator}${detail}${statusSuffix}`;
  const done = Number(preload.overallDone || 0), total = Number(preload.overallTotal || 0);
  if (state.state === "Warm") bar.value = 100;
  else if (total > 0) bar.value = Math.min(99, Math.round(done * 100 / total));
  else bar.removeAttribute("value");
  box.classList.toggle("idle", state.state !== "Starting" && state.state !== "Warming");
}

let syncSummaryTimer, lastCatalogRecordCount = null, lastArchiveName = null, lastCatalogGeneration = null;
async function refreshCatalogOptions() {
  const updated = await api("/api/options", { timeoutMs: 30000 }),
    values = Object.fromEntries(Object.entries(filters).map(([key, element]) => [key, element.value]));
  options = updated;
  fill("era", options.eras);
  fill("source_type", options.source_types, "source");
  fill("studio", options.studios);
  fill("tag", options.tags, "theme");
  Object.entries(values).forEach(([key, value]) => { filters[key].value = value; });
  personSuggestions("directorSuggestions", options.directors);
  personSuggestions("voiceSuggestions", options.voice_actors);
  renderMediaChecks();
}
async function pollSyncSummary() {
  clearTimeout(syncSummaryTimer);
  try {
    const [stats, startup, aniState] = await Promise.all([
      api("/api/stats"), api("/api/startup/state"), api("/api/ani-rss/status"),
    ]);
    const count = Number(stats.record_count || 0), archiveName = String(stats.archive_name || "");
    lastCatalogRecordCount = count;
    lastArchiveName = archiveName;
    const catalogGeneration = [archiveName, stats.archive_digest, stats.built_at].join(":");
    const catalogChanged = lastCatalogGeneration !== null && catalogGeneration !== lastCatalogGeneration;
    if (catalogChanged) await refreshCatalogOptions();
    lastCatalogGeneration = catalogGeneration;
    startupState = startup;
    const previousSort = sort;
    applyRecentEpisodeSortAvailability(aniState, { refresh: true });
    const generation = [aniState.successful_generation, aniState.last_success_at, aniState.last_release_update_at,
      aniState.release_progress_generation].join(":");
    if (catalogChanged || (lastAniRssGeneration !== null && generation !== lastAniRssGeneration)) invalidateCatalogPages();
    if (catalogChanged || (lastAniRssGeneration !== null && generation !== lastAniRssGeneration &&
        ["recent_episode", "random"].includes(sort) && previousSort === sort)) search({ background: true }).catch(() => {});
    if (catalogChanged && $("radarDialog").open) loadRadar();
    lastAniRssGeneration = generation;
    $("buildInfo").textContent = archiveSummary(stats);
    renderScanProgress(stats, startup);
    const activeWarmup = startup?.state === "Starting" || startup?.state === "Warming";
    syncSummaryTimer = setTimeout(pollSyncSummary, stats.sync?.state === "running" || activeWarmup ? 1500 : 15000);
  } catch (_) {
    syncSummaryTimer = setTimeout(pollSyncSummary, 10000);
  }
}
function setRecentDates() {
  const now = new Date(), endYear = now.getFullYear(), endMonth = now.getMonth() + 1,
    start = new Date(endYear, endMonth - 1 - 5, 1),
    monthValue = (year, month) => `${year}-${String(month).padStart(2, "0")}`;
  $("start_from").value = monthValue(start.getFullYear(), start.getMonth() + 1);
  $("start_to").value = monthValue(endYear, endMonth);
  syncEraSeasonFromDates();
}
function exactYear(value) {
  const text = String(value || ""), year = Number(text);
  return /^\d{4}$/.test(text) && year >= 1000 && year <= 9999 ? year : null;
}
function monthParts(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})$/);
  if (!match) return null;
  const year = Number(match[1]), month = Number(match[2]);
  return year >= 1 && month >= 1 && month <= 12
    ? { year, month: String(month).padStart(2, "0") }
    : null;
}
function ensureEraYearOption(year) {
  const select = $("era"), value = String(year);
  if (!select || [...select.options].some((option) => option.value === value)) return;
  const option = document.createElement("option");
  option.value = value;
  option.textContent = value;
  option.dataset.generatedYear = "1";
  select.insertBefore(option, select.options[1] || null);
}
// Radar filters are independent from the catalog's filters and selections.
let radarSeason = 1, radarTimer, radarController, radarGeneration = 0;
let radarQuarterKey = "", radarSubscribeGeneration = 0, radarRefreshPending = false;
let radarSort = "", radarDirection = "asc";
function updateRadarSortControls() {
  $("radarMobileSort").value = radarSort;
  $("radarMobileDirection").disabled = !radarSort;
  $("radarMobileDirection").textContent = radarDirection === "asc" ? "↑" : "↓";
}
$("radarMobileSort").onchange = () => {
  radarSort = $("radarMobileSort").value;
  radarDirection = "asc";
  updateRadarSortControls();
  loadRadar({ preferCache: true });
};
$("radarMobileDirection").onclick = () => {
  if (!radarSort) return;
  radarDirection = radarDirection === "asc" ? "desc" : "asc";
  updateRadarSortControls();
  loadRadar({ preferCache: true });
};
const radarPendingSubscriptions = new Set();
const radarSubmittingSubscriptions = new Set();
function radarIsHighlighted(item) { return item?.hasUnwatched === true; }
function radarSeasons(now = new Date()) {
  const anchor = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 7);
  const quarter = anchor.getFullYear() * 4 + Math.floor(anchor.getMonth() / 3);
  return [-1, 0, 1].map((offset) => {
    const ordinal = quarter + offset, year = Math.floor(ordinal / 4);
    const season = ["winter", "spring", "summer", "autumn"][((ordinal % 4) + 4) % 4];
    return { year, season, ...seasonDateRange(year, season) };
  });
}
function radarSeasonLabel(season) {
  const key = "season" + season.season[0].toUpperCase() + season.season.slice(1);
  return `${season.year} · ${t(key).replace(/\s*[（(].*$/, "")}`;
}
function updateRadarSeasonCount(button, season, count) {
  if (!button || button.dataset.from !== season.from || !Number.isInteger(count) || count < 0) return;
  button.dataset.count = String(count);
  const countText = t("radarWorks").replace("{count}", fmt(count));
  button.querySelector(".radar-season-count").textContent = language === "en" ? ` (${countText})` : `（${countText.replace(/ /g, "")}）`;
  button.setAttribute("aria-label", `${t(["radarPrevious", "radarCurrent", "radarNext"][Number(button.dataset.season)])} · ${radarSeasonLabel(season)} · ${countText}`);
}
function renderRadarTable(markup) {
  const root = $("radarResults");
  if (renderedContents.get(root) === markup) return;
  const template = document.createElement("template");
  template.innerHTML = markup;
  const current = root.querySelector("table"), next = template.content.querySelector("table");
  const focusedSort = current?.tHead?.contains(document.activeElement) ? document.activeElement.dataset.radarSort : null;
  if (current && next) {
    patchRenderedContent(current.tHead, next.tHead);
    reconcileKeyedChildren(current.tBodies[0], next.tBodies[0], "data-radar-id", (row, candidate) => {
      row.className = candidate.className;
      [...row.cells].forEach((cell, index) => {
        const nextCell = candidate.cells[index];
        cell.className = nextCell.className;
        if (nextCell.dataset.label) cell.dataset.label = nextCell.dataset.label;
        patchRenderedContent(cell, nextCell);
      });
      return true;
    });
  } else root.replaceChildren(template.content);
  if (focusedSort) root.querySelector(`[data-radar-sort="${focusedSort}"]`)?.focus({ preventScroll: true });
  renderedContents.set(root, markup);
}
function radarHeader(sortKey, labelKey, className = "") {
  const active = radarSort === sortKey;
  return `<th class="${className}" aria-sort="${active ? (radarDirection === "asc" ? "ascending" : "descending") : "none"}"><button class="radar-sort" type="button" data-radar-sort="${sortKey}">${esc(t(labelKey))}<span class="radar-sort-arrow" aria-hidden="true">${active ? (radarDirection === "asc" ? "↑" : "↓") : ""}</span></button></th>`;
}
let lastRadarScan = { state: "idle" }, radarPrefetchTimer;
function updateRadarScan(scan, available) {
    $("radarCatchup").disabled = config.playback?.enabled === false;
    $("radarSearch").disabled = !available || scan.state === "running";
    $("radarSync").disabled = !available || scan.state === "running";
    const searchLabel = $("radarSearch").querySelector("[data-i18n]");
    searchLabel.dataset.i18n = scan.failed && scan.state !== "running" ? "radarRetryShort" : "radarSearchShort";
    searchLabel.textContent = t(searchLabel.dataset.i18n);
    $("radarSearch").title = t(scan.failed && scan.state !== "running" ? "radarRetry" : "radarSearch");
    $("radarSearchProgress").hidden = scan.state !== "running";
    if (scan.total && scan.phase !== "sync") {
      $("radarSearchProgress").max = scan.total;
      $("radarSearchProgress").value = scan.done || 0;
    } else $("radarSearchProgress").removeAttribute("value");
    $("radarSearchStatus").textContent = scan.state === "idle" ? "" :
      scan.state === "running" && scan.phase === "sync" ? t("radarSyncing") :
      `${t(scan.state === "complete" ? "radarComplete" : scan.state === "running" ? "radarChecking" : scan.state === "interrupted" ? "radarInterrupted" : "failed")} · ${t("radarSearchProgress").replace("{done}", String(scan.done || 0)).replace("{total}", String(scan.total || 0)).replace("{failed}", String(scan.failed || 0))}${scan.state === "running" && scan.currentTitle ? ` · ${scan.currentTitle}` : ""}${scan.error ? ` · ${scan.error}` : ""}`;
    $("radarSearchStatus").title = $("radarSearchStatus").textContent;
}
function radarQuery(season) {
  return new URLSearchParams({ sort: radarSort || "recent_episode", direction: radarDirection,
    radar_grouped: radarSort ? "0" : "1", radar: "1", language, start_from: season.from,
    start_to: season.to, limit: "all" });
}
async function loadRadar({ preferCache = false } = {}) {
  if ($("radarResults").querySelector(".subscription-menu[open]")) {
    clearTimeout(radarTimer);
    radarTimer = setTimeout(loadRadar, 2000);
    return;
  }
  if (!$("radarDialog").open) return;
  clearTimeout(radarTimer);
  clearTimeout(radarPrefetchTimer);
  radarTimer = null;
  if (topModalDialog() !== $("radarDialog")) {
    radarRefreshPending = true;
    radarTimer = setTimeout(loadRadar, 30000);
    return;
  }
  radarRefreshPending = false;
  if ($("radarResults").dataset.language !== language) {
    $("radarResults").replaceChildren();
    renderedContents.delete($("radarResults"));
    $("radarResults").dataset.language = language;
  }
  radarController?.abort();
  const controller = new AbortController(), generation = ++radarGeneration;
  radarController = controller;
  const seasons = radarSeasons(), quarterKey = seasons[1].from;
  radarQuarterKey = quarterKey;
  const previousCounts = seasons.map((item, index) => {
    const button = $(`radarTab${index}`);
    return button?.dataset.from === item.from && button.dataset.count !== undefined ? Number(button.dataset.count) : null;
  });
  const tabsMarkup = seasons.map((item, index) =>
    `<button id="radarTab${index}" class="tab ${index === radarSeason ? "active" : ""}" role="tab" aria-controls="radarPanel" aria-selected="${index === radarSeason}" aria-label="${esc(t(["radarPrevious", "radarCurrent", "radarNext"][index]) + " · " + radarSeasonLabel(item))}" tabindex="${index === radarSeason ? 0 : -1}" data-season="${index}" data-from="${item.from}"><span>${esc(radarSeasonLabel(item))}</span><span class="radar-season-count"></span></button>`).join("");
  if (renderedContents.get($("radarTabs")) !== tabsMarkup) {
    $("radarTabs").innerHTML = tabsMarkup;
    renderedContents.set($("radarTabs"), tabsMarkup);
  }
  seasons.forEach((item, index) => updateRadarSeasonCount($(`radarTab${index}`), item, previousCounts[index]));
  $("radarPanel").setAttribute("aria-labelledby", `radarTab${radarSeason}`);
  $("radarTabs").querySelectorAll("button").forEach((button) => {
    button.onclick = () => { radarSeason = Number(button.dataset.season); loadRadar({ preferCache: true }); };
    button.onkeydown = (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      radarSeason = event.key === "Home" ? 0 : event.key === "End" ? 2 :
        (radarSeason + (event.key === "ArrowRight" ? 1 : 2)) % 3;
      loadRadar({ preferCache: true }); $(`radarTab${radarSeason}`).focus();
    };
  });
  const season = seasons[radarSeason];
  if (!$("radarResults").querySelector("table")) $("radarStatus").textContent = t("radarLoading");
  $("radarResults").setAttribute("aria-busy", "true");
  try {
    const scanPromise = api("/api/ani-rss/season-search", { signal: controller.signal, timeoutMs: 5000 })
      .catch((error) => ({ state: "unavailable", error: error.message }));
    const data = await catalogPage(radarQuery(season), { signal: controller.signal, preferCache });
    if (generation !== radarGeneration || !$("radarDialog").open) return;
    data.items.forEach((item) => { if (item.viewing) updateViewing(item.viewing); });
    if (topModalDialog() !== $("radarDialog")) { radarRefreshPending = true; return; }
    const available = data.aniRssAvailable;
    updateRadarScan(lastRadarScan, available);
    scanPromise.then((scan) => {
      if (generation !== radarGeneration || !$("radarDialog").open || topModalDialog() !== $("radarDialog")) return;
      lastRadarScan = scan;
      updateRadarScan(scan, available);
      if (scan.state === "running") { clearTimeout(radarTimer); radarTimer = setTimeout(loadRadar, 2000); }
    });
    updateRadarSeasonCount($(`radarTab${radarSeason}`), season, data.total);
    $("radarStatus").textContent = data.viewingUnavailable ? t("viewingUnavailable") : available ? "" : t("recentEpisodeRequiresAniRss");
    updateRadarSortControls();
    renderRadarTable(data.items.length ? `<table class="data-table radar-table"><thead><tr>
      ${radarHeader("title", "radarTitleColumn")}${radarHeader("premiere", "radarStart", "radar-start")}${radarHeader("progress", "radarProgress")}<th class="radar-watched">${esc(t("radarWatched"))}</th>${radarHeader("subscription", "radarSubscription")}${radarHeader("updated", "radarUpdated")}
      </tr></thead><tbody>${data.items.map((item) => {
        if (item.subscription_state === "active") radarPendingSubscriptions.delete(item.id);
        const pending = radarPendingSubscriptions.has(item.id) || radarSubmittingSubscriptions.has(item.id);
        const state = item.subscription_state;
        const stamp = item.last_episode_update_at ? new Date(item.last_episode_update_at) : null;
        const subscription = pending ? esc(t("radarSubscriptionPending")) : item.ani_rss_subscriptions?.length ?
          item.ani_rss_subscriptions.map((value) => aniSubscriptionMenu(value, item.id, !available, item.bgm_id)).join("") : state === "active" ?
          `<details class="subscription-menu"><summary>${t("radarSubscribed")} ▾</summary><div class="subscription-menu-actions">${visibilityButton(item.bgm_id, item.id)}</div></details>` : state === "paused" ? `<details class="subscription-menu"><summary>${t("radarPaused")} ▾</summary><div class="subscription-menu-actions">${visibilityButton(item.bgm_id, item.id)}</div></details>` :
          `<details class="subscription-menu"><summary>${t((item.follows || []).some((row) => row.state === "available") ? "followAvailable" : (item.follows || []).some((row) => row.state !== "cancelled" && row.state !== "subscribed") ? "followPending" : "radarSubscribe")} ▾</summary><div class="subscription-menu-actions"><button type="button" class="text-button" data-subscribe="${item.id}" data-icon="subscribe" ${!available || config.components?.downloadClient?.submissionEnabled === false ? "disabled" : ""}>${t("enableSubscription")}</button>${followButton(item)}${followButton(item, "archive")}${visibilityButton(item.bgm_id, item.id)}</div></details>`;
        return `<tr data-radar-id="${item.id}" class="${radarIsHighlighted(item) ? "radar-highlight" : ""}" ${radarIsHighlighted(item) ? `title="${esc(t("radarUnwatched"))}"` : ""}><td class="radar-title-cell ${imagesEnabled ? "with-radar-cover" : ""}">${imagesEnabled ? `<img class="radar-cover" data-cover="${item.id}" alt="" decoding="async" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='600'%3E%3Crect width='400' height='600' fill='%23e6e4de'/%3E%3C/svg%3E">` : ""}<button class="text-button radar-work" data-detail="${item.id}">${esc(preferred(item))}</button></td>
          <td class="radar-start" data-label="${esc(t("radarStart"))}">${esc(radarStartDate(item))}${language === "en" ? ` (${esc(label("media", item.media_code))})` : `（${esc(label("media", item.media_code))}）`}${radarReleaseDate(item)}</td>
          <td class="radar-progress" data-label="${esc(t("radarProgress"))}">${item.episode_progress?.current ?? "?"} / ${item.episode_progress?.total ?? "?"}</td>
          <td class="radar-watched" data-label="${esc(t("radarWatched"))}">${viewingButton(item.id, item.viewing, item.bgm_id, true)}</td>
          <td class="radar-subscription" data-label="${esc(t("radarSubscription"))}">${subscription}</td><td class="radar-updated" data-label="${esc(t("radarUpdated"))}">${stamp && Number.isFinite(stamp.getTime()) ? esc(stamp.toLocaleString(language)) : ""}</td></tr>`;
      }).join("")}</tbody></table>` : `<p class="empty">${t("radarEmpty")}</p>`);
    $("radarResults").querySelectorAll("[data-radar-sort]").forEach((button) => {
      button.onclick = () => {
        const column = button.dataset.radarSort;
        radarDirection = radarSort === column && radarDirection === "asc" ? "desc" : "asc";
        radarSort = column;
        updateRadarSortControls();
        loadRadar({ preferCache: true });
      };
    });
    if (imagesEnabled) {
      covers();
      $("radarResults").querySelectorAll("img[data-cover]").forEach((target) => {
        if (target.dataset.coverQueued !== "1") coverObserver?.observe(target);
      });
    }
    $("radarResults").querySelectorAll("[data-detail]").forEach((b) => {
      b.onclick = () => showDetail(Number(b.dataset.detail)).catch((error) => { $("radarStatus").textContent = error.message; });
    });
    $("radarResults").querySelectorAll("[data-subscribe]").forEach((b) => {
      b.onclick = () => openRadarSubscription(Number(b.dataset.subscribe));
    });
    const revision = catalogPageRevision;
    radarPrefetchTimer = setTimeout(async () => {
      for (let index = 0; index < seasons.length; index++) {
        if (index === radarSeason) continue;
        if (generation !== radarGeneration || revision !== catalogPageRevision || topModalDialog() !== $("radarDialog")) return;
        try {
          const data = await catalogPage(radarQuery(seasons[index]), { preferCache: true });
          if (generation === radarGeneration && $("radarDialog").open)
            updateRadarSeasonCount($(`radarTab${index}`), seasons[index], data.total);
        } catch (_) {} // Optional prefetch never blocks the selected season.
      }
    }, 350);

  } catch (error) {
    if (generation === radarGeneration && !controller.signal.aborted) {
      $("radarStatus").textContent = `${t("failed")}: ${error.message}`;
      const retry = document.createElement("button");
      retry.type = "button"; retry.className = "text-button"; retry.dataset.icon = "refresh";
      retry.textContent = t("reloadList"); retry.onclick = () => loadRadar();
      $("radarStatus").append(" ", retry);
    }
  } finally {
    if (generation === radarGeneration) $("radarResults").removeAttribute("aria-busy");
    if (generation === radarGeneration && $("radarDialog").open && !radarTimer)
      radarTimer = setTimeout(loadRadar, 30000);
  }
}
async function openRadarSubscription(animeId) {
  if (radarSubmittingSubscriptions.has(animeId)) return;
  const dialog = $("radarSubscribeDialog"), generation = ++radarSubscribeGeneration;
  const active = () => generation === radarSubscribeGeneration && dialog.open;
  $("radarResource").replaceChildren();
  $("radarResource").disabled = false;
  $("radarSubscribeSubmit").disabled = true;
  $("radarSubscribeForm").dataset.animeId = String(animeId);
  $("radarSubscribeStatus").textContent = t("radarLoading");
  $("radarSubscribeWork").textContent = "";
  $("radarSubscribeSubmit").textContent = t("enableSubscription");
  $("radarSubscribeSubmit").dataset.i18n = "enableSubscription";
  showModalDialog(dialog);
  try {
    let detail = await api(`/api/anime/${animeId}?language=${encodeURIComponent(language)}`);
    if (!active()) return;
    $("radarSubscribeWork").textContent = preferred(detail);
    const follow = (value) => (value.ani_rss?.resources || []).filter((r) => r.kind === "follow");
    if (!follow(detail).length) {
      const existingJob = await api(`/api/anime/${animeId}/ani-rss/search`);
      if (existingJob.state !== "running") {
        try {
          await api(`/api/anime/${animeId}/ani-rss/search`, { method: "POST" });
        } catch (error) {
          const concurrentJob = await api(`/api/anime/${animeId}/ani-rss/search`);
          if (concurrentJob.state !== "running") throw error;
        }
      }
      const deadline = Date.now() + 90000;
      while (active()) {
        const job = await api(`/api/anime/${animeId}/ani-rss/search`);
        if (job.state === "failed") throw new Error(job.error || t("failed"));
        if (job.state !== "running") break;
        if (Date.now() >= deadline) throw new Error(t("radarSearchTimeout"));
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      if (!active()) return;
      detail = await api(`/api/anime/${animeId}?language=${encodeURIComponent(language)}`);
    }
    if (!active()) return;
    const resources = follow(detail);
    $("radarResource").innerHTML = resources.map((r) =>
      `<option value="${esc(r.resourceId)}" data-policy-override="${!r.eligible && !!r.canOverride}" ${r.eligible || r.canOverride ? "" : "disabled"}>${esc([r.title, r.releaseVariant?.platform, subtitleText(r.subtitle), r.resolution ? r.resolution + "p" : "", r.releaseVariant?.codec, eligibilityReasonText(r)].filter(Boolean).join(" · "))}</option>`).join("");
    const selectedResource = resources.find((r) => r.eligible) || resources.find((r) => r.canOverride);
    if (selectedResource) $("radarResource").value = selectedResource.resourceId;
    $("radarSubscribeStatus").textContent = selectedResource ? t("radarSubscribeHint") : resources.length ? t("radarPolicyExcluded") : t("radarNoResources");
    $("radarSubscribeSubmit").disabled = !selectedResource;
    const updateChoice = () => {
      const row = resources.find((r) => r.resourceId === $("radarResource").value);
      $("radarSubscribeSubmit").disabled = !row || (!row.eligible && !row.canOverride);
      $("radarSubscribeSubmit").dataset.i18n = row && !row.eligible && row.canOverride ? "forceSubscribe" : "enableSubscription";
      $("radarSubscribeSubmit").textContent = t($("radarSubscribeSubmit").dataset.i18n);
      $("radarSubscribeStatus").textContent = row?.canOverride && !row.eligible
        ? `${eligibilityReasonText(row)} · ${t("forcePolicyNotice")}`
        : row?.eligible ? t("radarSubscribeHint") : resources.length ? t("radarPolicyExcluded") : t("radarNoResources");
    };
    $("radarResource").onchange = updateChoice;
    updateChoice();
  } catch (error) {
    if (active()) $("radarSubscribeStatus").textContent = error.message;
  }
}
$("radarButton").onclick = () => { radarSeason = 1; showModalDialog($("radarDialog")); loadRadar({ preferCache: true }); };
async function startRadarSearch(synchronize = false) {
  $("radarSearch").disabled = true;
  $("radarSync").disabled = true;
  $("radarSearchStatus").textContent = t(synchronize ? "radarSyncing" : "radarChecking");
  $("radarSearchProgress").hidden = false;
  $("radarSearchProgress").removeAttribute("value");
  try {
    const season = radarSeasons()[synchronize ? radarSeason : 1];
    await api("/api/ani-rss/season-search", { method: "POST", body: JSON.stringify({ from: season.from, to: season.to, language, synchronize }) });
    if (!synchronize) radarSeason = 1;
    await loadRadar();
  } catch (error) {
    $("radarSearchStatus").textContent = `${t("failed")}: ${error.message}`;
    $("radarSearch").disabled = false;
    $("radarSync").disabled = false;
    $("radarSearchProgress").hidden = true;
  }
}
$("radarSearch").onclick = () => startRadarSearch();
$("radarSync").onclick = () => startRadarSearch(true);
$("radarDialog").addEventListener("close", () => { if (!$("radarDialog").open) { clearTimeout(radarTimer); radarController?.abort(); radarGeneration++; } });
$("radarSubscribeDialog").addEventListener("close", () => { if (!$("radarSubscribeDialog").open) radarSubscribeGeneration++; });
$("radarSubscribeForm").onsubmit = async (event) => {
  event.preventDefault();
  const button = $("radarSubscribeSubmit"), resource = $("radarResource").value;
  if (button.disabled || !resource) return;
  const animeId = Number($("radarSubscribeForm").dataset.animeId);
  const generation = radarSubscribeGeneration;
  radarSubmittingSubscriptions.add(animeId);
  button.disabled = true;
  button.dataset.i18n = "radarSubmitting";
  button.textContent = t("radarSubmitting");
  $("radarSubscribeStatus").textContent = t("radarSubmittingHint");
  $("radarResource").disabled = true;
  try {
    await api(`/api/ani-rss/resources/${encodeURIComponent(resource)}/subscribe`, {
      method: "POST", timeoutMs: 240000,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policyOverride: $("radarResource").selectedOptions[0]?.dataset.policyOverride === "true" }),
    });
    radarPendingSubscriptions.add(animeId);
    if (generation === radarSubscribeGeneration) {
      $("radarSubscribeStatus").textContent = t("radarSubscriptionDone");
      $("radarSubscribeDialog").close();
    }
    loadRadar();
    if (["recent_episode", "random"].includes(sort)) { page = 0; search({ background: true }).catch(() => {}); }
  } catch (error) {
    if (generation === radarSubscribeGeneration) {
      $("radarSubscribeStatus").textContent = error.message;
      button.disabled = false;
    }
    $("radarSearchStatus").textContent = `${t("failed")}: ${error.message}`;
  } finally {
    radarSubmittingSubscriptions.delete(animeId);
    if (generation === radarSubscribeGeneration) $("radarResource").disabled = false;
    if (generation === radarSubscribeGeneration) $("radarResource").onchange?.();
    loadRadar();
  }
};

function seasonDateRange(year, season) {
  const y = Number(year);
  if (!Number.isInteger(y) || y < 1) return null;
  if (season === "winter") return { from: `${y - 1}-12`, to: `${y}-02` };
  if (season === "spring") return { from: `${y}-03`, to: `${y}-05` };
  if (season === "summer") return { from: `${y}-06`, to: `${y}-08` };
  if (season === "autumn") return { from: `${y}-09`, to: `${y}-11` };
  return { from: `${y}-01`, to: `${y}-12` };
}
function eraDateRange(value) {
  const year = exactYear(value);
  if (year !== null) return { ...seasonDateRange(year, ""), reversible: true };
  if (/^\d{4}s$/.test(value)) {
    const first = +value.slice(0, 4);
    return { from: `${first}-01`, to: `${first + 9}-12`, reversible: true };
  }
  if (value === "before1980") return { from: "", to: "1979-12", reversible: true };
  if (value === "future_or_unknown") return { from: "", to: "", reversible: false };
  return null;
}
function updateSeasonControl() {
  const select = $("season"), year = exactYear($("era").value);
  const enabled = year !== null;
  select.disabled = !enabled;
  if (!enabled) select.value = "";
}
function applyEraDateRange(value) {
  const year = exactYear(value);
  if (year !== null) {
    ensureEraYearOption(year);
    $("season").value = "";
    $("start_from").value = `${year}-01`;
    $("start_to").value = `${year}-12`;
    updateSeasonControl();
    return;
  }
  $("season").value = "";
  updateSeasonControl();
  const range = eraDateRange(value);
  if (!range) return;
  $("start_from").value = range.from;
  $("start_to").value = range.to;
}
function applySeasonDateRange() {
  const year = exactYear($("era").value);
  if (year === null) {
    $("season").value = "";
    updateSeasonControl();
    return;
  }
  const range = seasonDateRange(year, $("season").value);
  $("start_from").value = range.from;
  $("start_to").value = range.to;
}
function syncEraSeasonFromDates() {
  const from = $("start_from").value, to = $("start_to").value,
    fromParts = monthParts(from), toParts = monthParts(to);
  let matchedEra = "", matchedSeason = "";
  if (fromParts && toParts) {
    const candidateYears = [...new Set([fromParts.year, toParts.year, fromParts.year + 1])];
    for (const year of candidateYears) {
      for (const season of ["winter", "spring", "summer", "autumn"]) {
        const range = seasonDateRange(year, season);
        if (range.from === from && range.to === to) {
          ensureEraYearOption(year);
          matchedEra = String(year);
          matchedSeason = season;
          break;
        }
      }
      if (matchedSeason) break;
    }
    if (!matchedEra && fromParts.year === toParts.year) {
      ensureEraYearOption(fromParts.year);
      matchedEra = String(fromParts.year);
    }
  }
  if (!matchedEra) {
    for (const value of options.eras || []) {
      if (exactYear(value) !== null) continue;
      const range = eraDateRange(value);
      if (range?.reversible && range.from === from && range.to === to) {
        matchedEra = value;
        break;
      }
    }
  }
  $("era").value = matchedEra;
  $("season").value = matchedSeason;
  updateSeasonControl();
}

function updateViewToggle() {
  const button = $("viewToggle");
  if (!button) return;
  const key = view === "cards" ? "table" : "cards";
  button.dataset.icon = view === "cards" ? "table" : "group";
  button.textContent = t(key);
  button.title = t(key);
  button.setAttribute("aria-label", t(key));
}
function setView(v) {
  view = v === "table" ? "table" : "cards";
  results.className = `results ${view}`;
  updateViewToggle();
  render();
}
function applyRoleUi() {
  const admin = authSession?.role === "admin";
  $("settingsButton").hidden = !admin && !authSession?.authEnabled;
  $("settingsButton").disabled = !config.torrentPolicy;
  $("authButton").hidden = !authSession?.authEnabled;
  if (authSession?.authEnabled) $("authButton").textContent = `${authSession.username} · ${t("logout")}`;
  $("accountSettingsRow").hidden = !authSession?.authEnabled;
  $("accountSettingsName").textContent = authSession?.username || "";
  document.querySelectorAll("[data-admin-only]").forEach((element) => (element.hidden = !admin));
}
function showLogin() {
  showModalDialog($("loginDialog"));
  setTimeout(() => $("loginUsername").focus(), 0);
}
async function establishSession() {
  try {
    authSession = await api("/api/auth/session", { timeoutMs: 30000 });
    csrfToken = authSession.csrfToken || "";
    applyRoleUi();
    return true;
  } catch (error) {
    if (error.status === 401) {
      showLogin();
      return false;
    }
    throw error;
  }
}
function setUserManagementStatus(message = "", error = false) {
  const target = $("userManagementStatus");
  if (!target) return;
  target.textContent = message;
  target.classList.toggle("error", error);
}
async function loadUsers() {
  if (authSession?.role !== "admin") return;
  try {
    const payload = await api("/api/auth/users");
    const items = Array.isArray(payload.items) ? payload.items : [];
    $("userList").innerHTML = items.map((user) => {
      const current = user.username === authSession?.username;
      const badges = [
        `<span class="user-badge">${esc(t(user.role === "admin" ? "administrator" : "normalUser"))}</span>`,
        user.initialAdmin ? `<span class="user-badge">${esc(t("initialAdmin"))}</span>` : "",
        current ? `<span class="user-badge">${esc(t("currentUser"))}</span>` : "",
        !user.enabled ? `<span class="user-badge disabled">${esc(t("disabledUser"))}</span>` : "",
      ].join("");
      const initial = String(user.username || "?").slice(0, 1);
      return `<div class="user-row"><div class="user-summary"><span class="user-avatar" aria-hidden="true">${esc(initial)}</span><span class="user-identity"><b>${esc(user.username)}</b><small>${esc(t(user.role === "admin" ? "administrator" : "normalUser"))}</small></span></div><span class="user-badges">${badges}</span><button type="button" class="text-button toggle-user" data-icon="user" data-user-id="${user.id}" data-enabled="${user.enabled ? "1" : "0"}" ${current ? "disabled" : ""}>${esc(t(user.enabled ? "disableUser" : "enableUser"))}</button></div>`;
    }).join("") || `<p class="user-empty">${esc(t("noUsers"))}</p>`;
    $("userList").querySelectorAll(".toggle-user").forEach((button) => {
      button.onclick = async () => {
        button.disabled = true;
        setUserManagementStatus();
        try {
          await api(`/api/auth/users/${button.dataset.userId}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: button.dataset.enabled !== "1" }) });
          setUserManagementStatus(t("userUpdated"));
          await loadUsers();
        } catch (error) {
          setUserManagementStatus(error.message, true);
          button.disabled = false;
        }
      };
    });
  } catch (error) {
    $("userList").innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}
function updateReasonText(reason) {
  return t({
    docker_update_required: "updateDockerRequired",
    docker_update_runtime_unavailable: "updateDockerRequired",
    docker_state_not_writable: "updateDockerStateUnavailable",
    portable_release_required: "updatePortableRequired",
    install_root_unavailable: "updatePortableRequired",
    portable_release_invalid: "updatePortableRequired",
    install_parent_not_writable: "updatePortableRequired",
    platform_not_supported: "updateUnavailable",
    release_asset_unavailable: "updateUnavailable",
    release_checksum_unavailable: "updateUnavailable",
  }[String(reason || "")] || "updateUnavailable");
}
function updateMessage(key, values = {}) {
  let value = t(key);
  Object.entries(values).forEach(([name, replacement]) => {
    value = value.replaceAll(`{${name}}`, String(replacement));
  });
  return value;
}
function updateShaText(value) {
  return t({ pending: "shaPending", verified: "shaVerified", failed: "shaFailed", unavailable: "shaUnavailable", not_required: "shaNotRequired" }[String(value || "")] || "shaPending");
}
function updateUpgradeText(value) {
  const status = String(value?.status || "");
  if (!status) return t("upgradeIdle");
  if (value.restoredVersion) return t("updateRolledBack");
  if (value.historical) return t("upgradeHistorical");
  return t({ downloading: "upgradeDownloading", restarting: "upgradeRestarting", installed: "upgradeInstalled", failed: "upgradeFailedState", available: "upgradeStatusAvailable", latest: "upgradeStatusLatest", unavailable: "upgradeStatusUnavailable", installing: "upgradeStatusInstalling" }[status] || "upgradeIdle");
}
function updateDateText(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : new Intl.DateTimeFormat(localeForLanguage(), { dateStyle: "short", timeStyle: "medium" }).format(date);
}
function renderUpdateSourceDiagnostics(payload) {
  const target = $("updateSourceDiagnostics");
  if (!target) return;
  const items = payload?.items || [];
  target.innerHTML = items.map((item) => {
    const latency = item.latencyMs == null ? "—" : `${Math.round(Number(item.latencyMs))} ms`;
    const success = item.recentSuccessRate == null ? "—" : `${Math.round(Number(item.recentSuccessRate) * 100)}%`;
    const selected = item.selection?.selected ? ` · ${t("sourceSelected")}` : "";
    const type = t(item.sourceType === "proxy" ? "updateSourceProxy" : "updateSourceDirect");
    const cooling = item.coolingDown ? ` · ${t("selectionCandidateCooldown")}` : "";
    return `<div class="diagnostic-item"><div><b>${esc(item.label || item.kind || "—")}</b><small>${esc(item.host || item.baseUrl || "")} · ${esc(type)}${esc(selected)}${esc(cooling)}</small></div><span><small>${t("latency")}</small><br>${esc(latency)}</span><span><small>${t("successRate")}</small><br>${esc(success)}</span><span><small>${t("lastFailure")}</small><br>${esc(item.lastFailure || t("noRecentFailure"))}</span></div>`;
  }).join("") || `<p class="muted">—</p>`;
}
function renderUpdateDetails(payload) {
  if ($("updateCurrentVersion")) $("updateCurrentVersion").textContent = payload.currentVersion || "—";
  if ($("updateLatestVersion")) $("updateLatestVersion").textContent = payload.latestVersion || "—";
  if ($("updateDownloadSource")) $("updateDownloadSource").textContent = payload.downloadSourceLabel || payload.downloadSource || "—";
  if ($("updateSha256Status")) $("updateSha256Status").textContent = updateShaText(payload.sha256Status);
  if ($("updateUpgradeResult")) $("updateUpgradeResult").textContent = updateUpgradeText(payload.upgradeResult || payload.automaticResult);
  if ($("updateLastChecked")) $("updateLastChecked").textContent = updateDateText(payload.checkedAt);
  renderUpdateSourceDiagnostics(payload.sourceDiagnostics);
}
function updateButtons() {
  return {
    checks: [$("checkApplicationUpdateDetail")].filter(Boolean),
    applies: [$("applyApplicationUpdateDetail")].filter(Boolean),
    statuses: [$("updateDetailStatus")].filter(Boolean),
  };
}
async function loadApplicationUpdate(force = false) {
  if (applicationUpdateBusy) return;
  const controls = updateButtons();
  if (!controls.checks.length || authSession?.role !== "admin") return;
  controls.checks.forEach((button) => button.disabled = true);
  controls.statuses.forEach((target) => { target.classList.remove("error"); target.textContent = t("updateChecking"); });
  try {
    const local = await api('/api/update/status?local=1', { timeoutMs: 5000 });
    if (applicationUpdateBusy) return;
    renderUpdateDetails(local);
    if ($('uploadApplicationUpdate')) $('uploadApplicationUpdate').disabled = local.installationPending || !['portable', 'docker'].includes(local.installMode);
    if (local.installationPending || !force) {
      controls.statuses.forEach(target => target.textContent = t(local.installationPending ? 'updatePending' : 'updateLocalReady'));
      return local;
    }
    const payload = await api(`/api/update/status${force ? "?force=1" : ""}`, { timeoutMs: 30000 });
    if (applicationUpdateBusy) return;
    controls.applies.forEach((button) => {
      const reinstall = !payload.updateAvailable && payload.canForceUpdate;
      button.hidden = !((payload.updateAvailable && payload.canUpdate) || reinstall);
      button.dataset.force = reinstall ? "1" : "";
      button.dataset.i18n = reinstall ? "forceUpdateNow" : "updateNow";
      button.textContent = t(button.dataset.i18n);
      button.dataset.version = payload.latestVersion || "";
    });
    const message = payload.updateAvailable
      ? (payload.canUpdate ? t("upgradeStatusAvailable") : updateReasonText(payload.reason))
      : t("updateLatest");
    controls.statuses.forEach((target) => target.textContent = message);
    renderUpdateDetails(payload);
    return payload;
  } catch (error) {
    if (applicationUpdateBusy) return;
    controls.applies.forEach((button) => button.hidden = true);
    controls.statuses.forEach((target) => { target.classList.add("error"); target.textContent = updateMessage("updateCheckFailed", { message: error.message }); });
    throw error;
  } finally {
    controls.checks.forEach((button) => button.disabled = applicationUpdateBusy);
  }
}
async function checkUpdateRollback(health, expected) {
  if (!health.ok || health.version === expected) return;
  const local = await api('/api/update/status?local=1', { timeoutMs: 2500 });
  const result = local.upgradeResult || {};
  if (result.status === 'failed' && result.version === expected && result.restoredVersion === health.version) {
    const error = new Error(t('updateRolledBack'));
    error.code = 'update_rolled_back';
    throw error;
  }
}
async function applyOfflineUpdate(file) {
  if (!file) return;
  const control = $('uploadApplicationUpdate'), target = $('updateDetailStatus');
  if (control.disabled) return;
  if (!file.name.endsWith('.anm-update.zip') || !file.size || file.size > 1024 * 1024 * 1024) {
    target.textContent = t('offlinePackageHint'); return;
  }
  if (!confirm(t('offlineUpdateConfirm'))) return;
  applicationUpdateBusy = true;
  control.disabled = true;
  $('checkApplicationUpdateDetail').disabled = true;
  $('applyApplicationUpdateDetail').disabled = true;
  target.classList.remove('error'); target.textContent = t('updatePreparing');
  try {
    const payload = await api('/api/update/offline', { method: 'POST', body: file,
      headers: { 'Content-Type': 'application/octet-stream' }, timeoutMs: 180000 });
    target.textContent = t('updateRestarting');
    const deadline = Date.now() + 150000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      try {
        const health = await api('/api/health/live', { timeoutMs: 2500 });
        if (health.ok && health.version === payload.version) { window.location.reload(); return; }
        await checkUpdateRollback(health, payload.version);
      } catch (error) { if (error.code === 'update_rolled_back') throw error; }
    }
    throw new Error(t('updateRestartTimeout'));
  } catch (error) {
    const messages = { offline_package_invalid: 'offlinePackageHint', offline_package_not_newer: 'offlineNotNewer',
      offline_package_platform_mismatch: 'offlinePlatform', offline_package_checksum_failed: 'offlineChecksum',
      offline_package_incomplete: 'offlineIncomplete', 'update already in progress': 'updatePending' };
    target.classList.add('error'); target.textContent = messages[error.message] ? t(messages[error.message]) : error.message;
  } finally {
    applicationUpdateBusy = false;
    control.disabled = false;
    $('checkApplicationUpdateDetail').disabled = false;
    $('applyApplicationUpdateDetail').disabled = false;
    $('offlineUpdateFile').value = '';
  }
}
async function applyApplicationUpdate() {
  if (applicationUpdateBusy) return;
  applicationUpdateBusy = true;
  const controls = updateButtons();
  controls.applies.forEach((button) => button.disabled = true);
  controls.checks.forEach((button) => button.disabled = true);
  controls.statuses.forEach((target) => { target.classList.remove("error"); target.textContent = t("updatePreparing"); });
  try {
    const reinstall = controls.applies.some((button) => button.dataset.force === "1");
    const previousBoot = reinstall ? (await api("/api/health/live", { timeoutMs: 3000 })).bootId : null;
    const payload = await api("/api/update/apply", { method: "POST", timeoutMs: 180000,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ force: reinstall }) });
    controls.statuses.forEach((target) => target.textContent = t("updateRestarting"));
    const expected = String(payload.version || controls.applies.find((button) => button.dataset.version)?.dataset.version || "");
    const deadline = Date.now() + 150000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1200));
      try {
        const health = await api("/api/health/live", { timeoutMs: 2500 });
        if (health.ok && (!expected || health.version === expected) && (!reinstall || (previousBoot && health.bootId !== previousBoot))) { window.location.reload(); return; }
        await checkUpdateRollback(health, expected);
      } catch (error) { if (error.code === 'update_rolled_back') throw error; }
    }
    throw new Error(t("updateRestartTimeout"));
  } catch (error) {
    applicationUpdateBusy = false;
    controls.statuses.forEach((target) => { target.classList.add("error"); target.textContent = updateMessage("updateFailed", { message: error.message }); });
    controls.applies.forEach((button) => button.disabled = false);
    controls.checks.forEach((button) => button.disabled = false);
  }
}
async function initialize() {
  try {
    if (!(await establishSession())) return;
    const [stats, o, c, cap, groups, startup, aniState] = await Promise.all([
      api("/api/stats", { timeoutMs: 30000 }),
      api("/api/options", { timeoutMs: 30000 }),
      api("/api/config", { timeoutMs: 30000 }),
      api("/api/capabilities", { timeoutMs: 30000 }),
      api("/api/resource-groups", { timeoutMs: 30000 }),
      api("/api/startup/state", { timeoutMs: 30000 }),
      api("/api/ani-rss/status", { timeoutMs: 30000 }),
    ]);
    config = c;
    catalogStats = stats;
    startupState = startup;
    lastCatalogRecordCount = Number(stats.record_count || 0);
    lastArchiveName = String(stats.archive_name || "");
    lastCatalogGeneration = [lastArchiveName, stats.archive_digest, stats.built_at].join(":");
    lastAniRssGeneration = [aniState.successful_generation, aniState.last_success_at, aniState.last_release_update_at,
      aniState.release_progress_generation].join(":");
    capabilities = cap;
    options = o;
    groupCatalog = groups;
    applyRoleUi();
    const preferences = { ...c.ui, ...storedUi() };
    language = storedUi().language || storedLanguage() || detectSystemLanguage(c.ui?.availableLanguages);
    view = preferences.catalogView || view;
    imagesEnabled = preferences.imagesEnabled !== false;
    pageSize = String(preferences.pageSize || 12);
    seed = String(stats.instance_random_seed || "anm");
    configuredSort = preferences.sort || "recent_episode";
    sort = configuredSort;
    direction = preferences.sortDirection || "asc";
    fill("era", o.eras);
    fill("source_type", o.source_types, "source");
    fill("studio", o.studios);
    fill("tag", o.tags, "theme");
    personSuggestions("directorSuggestions", o.directors);
    personSuggestions("voiceSuggestions", o.voice_actors);
    renderMediaChecks();
    applyStatusDefaults();
    if (!restoreFilterState()) setRecentDates();
    $("pageSize").value = pageSize;
    applyRecentEpisodeSortAvailability(aniState);
    $("sort").value = sort;
    $("sortDirection").textContent = direction === "asc" ? "↑" : "↓";
    updateSortControls();
    setView(view);
    applyLanguage();
    $("buildInfo").textContent = archiveSummary(stats);
    renderScanProgress(stats, startup);
    await search();
    const activeWarmup = startup?.state === "Starting" || startup?.state === "Warming";
    syncSummaryTimer = setTimeout(pollSyncSummary, stats.sync?.state === "running" || activeWarmup ? 1500 : 15000);
  } catch (e) {
    results.innerHTML = `<div class="error">${t("failed")}: ${esc(e.message)}</div>`;
    $("buildInfo").textContent = t("failed");
  }
}
Object.values(filters).forEach((e) =>
  e.addEventListener("input", () => {
    if (e.id === "era") applyEraDateRange(e.value);
    else if (e.id === "start_from" || e.id === "start_to")
      syncEraSeasonFromDates();
    clearTimeout(timer);
    saveFilterState();
    page = 0;
    timer = setTimeout(
      () => {
        if (["director", "voice_actor"].includes(e.id))
          updatePeople(e.id, e.value).catch(() => {});
        search();
      },
      e.type === "search" ? 260 : 0,
    );
  }),
);
$("season").addEventListener("input", () => {
  applySeasonDateRange();
  clearTimeout(timer);
  saveFilterState();
  page = 0;
  timer = setTimeout(search, 0);
});
Object.values(statusGroups).forEach((b) =>
  b.addEventListener("change", () => {
    page = 0;
    saveFilterState();
    search();
  }),
);
$("reset").onclick = () => {
  try { localStorage.removeItem(filterStorageKey); } catch (_) {}
  Object.values(filters).forEach((x) => (x.value = ""));
  applyStatusDefaults();
  $("media_type").innerHTML = "";
  renderMediaChecks();
  setRecentDates();
  $("season").value = "";
  updateSeasonControl();
  saveFilterState();
  page = 0;
  search();
};
$("themeToggle").onclick = () => {
  themeMode = themeModes[(themeModes.indexOf(themeMode) + 1) % themeModes.length];
  try { localStorage.setItem("anm-theme", themeMode); } catch (_) {}
  applyTheme();
};
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => { if (themeMode === "system") applyTheme(); });
applyTheme();
$("language").onchange = async (e) => {
  language = e.target.value;
  try { localStorage.setItem("anm-language", language); } catch (_) {}
  applyLanguage();
  page = 0;
  await persistUi();
  search();
};
$("viewToggle").onclick = () => {
  setView(view === "cards" ? "table" : "cards");
  persistUi();
};
$("settingsButton").onclick = openSettings;
$("authButton").onclick = async () => {
  await api("/api/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  location.reload();
};
$("loginForm").onsubmit = async (event) => {
  event.preventDefault();
  $("loginError").textContent = "";
  try {
    authSession = await api("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: $("loginUsername").value, password: $("loginPassword").value }) });
    authSession.authEnabled = true;
    csrfToken = authSession.csrfToken || "";
    $("loginPassword").value = "";
    $("loginDialog").close();
    applyRoleUi();
    await initialize();
  } catch (error) {
    $("loginError").textContent = error.message;
  }
};
$("userCreateForm").onsubmit = (event) => { event.preventDefault(); $("createUser").click(); };
$("createUser").onclick = async () => {
  const button = $("createUser"), username = $("newUsername"), password = $("newUserPassword");
  setUserManagementStatus();
  if (!username.reportValidity() || !password.reportValidity()) return;
  button.disabled = true;
  try {
    await api("/api/auth/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: username.value, password: password.value, role: $("newUserRole").value }) });
    username.value = ""; password.value = "";
    setUserManagementStatus(t("userCreated"));
    await loadUsers();
  } catch (error) {
    setUserManagementStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
};
document.querySelectorAll(".settings-tabs .tab").forEach(
  (b) =>
    (b.onclick = () => {
      document
        .querySelectorAll(".settings-tabs .tab,.tab-panel")
        .forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      document.querySelectorAll(".settings-tabs .tab").forEach((tab) => {
        tab.setAttribute("aria-selected", String(tab === b));
        tab.tabIndex = tab === b ? 0 : -1;
      });
      b.scrollIntoView({ block: "nearest", inline: "nearest" });
      document
        .querySelector(`[data-panel="${b.dataset.tab}"]`)
        .classList.add("active");
      if (b.dataset.tab === "diagnostics") loadDiagnostics();
      else clearTimeout(diagnosticTimer);
      if (b.dataset.tab === "logs") loadLogs();
      if (b.dataset.tab === "history") loadHistory();
      if (b.dataset.tab === "subscriptions") loadWatches();
      if (b.dataset.tab === "users") loadUsers();
      if (b.dataset.tab === "updates") loadApplicationUpdate();
    }),
);
document.querySelector(".settings-tabs").onkeydown = (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || !event.target.matches(".tab")) return;
  event.preventDefault();
  const tabs = [...document.querySelectorAll(".settings-tabs .tab")].filter((tab) => !tab.hidden);
  const current = tabs.indexOf(event.target);
  const index = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
    (current + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
  tabs[index].click(); tabs[index].focus({ preventScroll: true });
};
$("recheckNetwork").onclick = async () => {
  const button = $("recheckNetwork");
  button.disabled = true;
  try {
    renderNetworkDiagnostics(await api("/api/diagnostics/network/recheck", { method: "POST", timeoutMs: 30000 }));
  } catch (error) {
    renderDiagnosticFailure("networkDiagnostics", error);
    $("networkRouteSummary").textContent = error.message;
  } finally { button.disabled = false; }
};
async function controlImagePreload(payload, button) {
  button.disabled = true;
  $("imagePreloadStatus").classList.remove("error");
  try {
    renderImagePreload(await api("/api/images/preload/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), timeoutMs: 15000 }));
  } catch (error) {
    $("imagePreloadStatus").classList.add("error");
    $("imagePreloadStatus").textContent = error.message;
  } finally { button.disabled = false; }
}
$("toggleImagePreload").onclick = async () => {
  const button = $("toggleImagePreload"), paused = button.dataset.paused === "1";
  await controlImagePreload({ paused: !paused }, button);
};
$("applyImagePreloadLimits").onclick = async () => {
  const button = $("applyImagePreloadLimits");
  await controlImagePreload({ concurrency: Number($("imagePreloadConcurrency").value || 1), bandwidthKiBps: Number($("imagePreloadBandwidth").value || 0) }, button);
};
$("checkApplicationUpdateDetail").onclick = () => loadApplicationUpdate(true).catch(() => {});
$("applyApplicationUpdateDetail").onclick = () => applyApplicationUpdate();
$('uploadApplicationUpdate').onclick = () => $('offlineUpdateFile').click();
$('offlineUpdateFile').onchange = event => applyOfflineUpdate(event.target.files?.[0]);
$("updateArchive").onclick = () =>
  startArchiveUpdate().catch(
    (e) => ($("archiveStatus").textContent = e.message),
  );
$("importArchive").onclick = () => $("archiveFile").click();
$("archiveFile").onchange = () => importArchive($("archiveFile").files[0]).catch((e) => ($("archiveStatus").textContent = e.message));
$("auditLibrary").onclick = () => runMaintenance("library");
$("createPlaceholders").onclick = () => runMaintenance("placeholders");
$("radarPlaceholders").onclick = () => runMaintenance("placeholders", "radar");
$("repairMetadata").onclick = () => runMaintenance("metadata");
$("settingsForm").onsubmit = (e) =>
  saveSettings(e).catch((x) => ($("saveState").textContent = x.message));
$("collectorSearchSources").addEventListener("change", validateCollectorSources);
let invalidSettingsField = null;
$("settingsForm").addEventListener("invalid", (event) => {
  event.preventDefault();
  if (invalidSettingsField) return;
  const field = event.target, panel = field.closest(".tab-panel");
  invalidSettingsField = field;
  if (panel) document.querySelector(`.settings-tabs [data-tab="${panel.dataset.panel}"]`)?.click();
  for (let parent = field.parentElement; parent && parent !== $("settingsForm"); parent = parent.parentElement)
    if (parent.tagName === "DETAILS") parent.open = true;
  $("saveState").textContent = field.validationMessage;
  field.focus();
  setTimeout(() => { invalidSettingsField = null; }, 0);
}, true);
$("testQbt").onclick = () => testConnection("qbittorrent");
$("testAniRss").onclick = () => testConnection("ani-rss");
$("refreshLogs").onclick = loadLogs;
$("refreshHistory").onclick = loadHistory;
$("refreshWatches").onclick = loadWatches;
$("sort").onchange = (e) => {
  sort = e.target.value;
  configuredSort = sort;
  updateSortControls();
  page = 0;
  search();
  persistUi();
};
$("reshuffle").onclick = async () => {
  try {
    const result = await api("/api/catalog/reshuffle", { method: "POST" });
    if (!result.seed) throw new Error(t("randomSeedUnavailable"));
    seed = String(result.seed);
    page = 0;
    await search();
  } catch (error) {
    results.innerHTML = `<div class="error">${esc(error.message)}</div>`;
  }
};
$("sortDirection").onclick = () => {
  direction = direction === "asc" ? "desc" : "asc";
  $("sortDirection").textContent = direction === "asc" ? "↑" : "↓";
  page = 0;
  search();
  persistUi();
};
$("pageSize").onchange = (e) => {
  pageSize = e.target.value;
  page = 0;
  search();
  persistUi();
};
$("prevPage").onclick = () => {
  if (page) {
    page--;
    search();
  }
};
$("nextPage").onclick = () => {
  page++;
  search();
};
$("clearSelection").onclick = () => {
  selected.clear();
  torrentSelections.clear();
  resourceSelections.clear();
  policyOverrides.clear();
  updateSelection();
  render();
};
$("createPlan").onclick = () => createPlan();
document
  .querySelectorAll("dialog .close")
  .forEach((b) => (b.onclick = () => b.closest("dialog").close()));
document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("close", () => {
    if (dialog.open) return;
    for (let index = modalStack.length - 1; index >= 0; index -= 1) {
      if (modalStack[index] === dialog) modalStack.splice(index, 1);
    }
    if (!topModalDialog() && coverBatch) {
      requestAnimationFrame(() => predictiveCoverWindow(coverBatch, "down"));
    }
    if (!topModalDialog()) {
      if (catalogRefreshPending) {
        catalogRefreshPending = false;
        search({ background: true }).catch(() => {});
      } else if (catalogRenderPending) render();
    }
    if (radarRefreshPending && topModalDialog() === $("radarDialog")) loadRadar();
  });
});
document.addEventListener("wheel", (event) => {
  if (event.ctrlKey) return;
  const dialog = topModalDialog();
  if (!dialog || modalWheelHasTarget(event, dialog)) return;
  scrollModalFallback(event, dialog);
  event.preventDefault();
}, { capture: true, passive: false });
window.__ANM_APP_STARTED__ = true;
localizeStaticUi();
initialize();
