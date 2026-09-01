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
  sort = "random",
  direction = "asc",
  imagesEnabled = true,
  coverVersion = 0,
  coverObserver,
  coverBatch,
  searchController,
  timer,
  catalogStats = {};
let authSession = null,
  csrfToken = "";
let seed = "";
const selected = new Set(),
  torrentSelections = new Map(),
  resourceSelections = new Map(),
  playbackSources = new Map(),
  planState = { id: null },
  relationHiddenCodes = new Set();
const i18n = {
  "zh-Hans": {
    catalog: "AnimeMachine",
    aboutTitle: "关于 AnimeMachine",
    versionLabel: "版本",
    aboutLead: "AnimeMachine 是一款全自动动画收藏库系统，负责把动画元数据、Torrent 池、媒体目录和外部只读媒体库整理到同一套本地 Catalog 与状态体系。",
    aboutDetail: "程序连续完成资源筛选、下载投料、多级目录规划、系列关系整理、收藏核验、Web 浏览与长期贮存；qBittorrent、Ani-RSS 与 Torrent Collector 均为可选组件。",
    aboutRelations: "作品关系图根据 Bangumi Archive 及辅助证据组织前作、续作、总集篇、番外、衍生、不同演绎和跨系列关联，并在可接受的计算时间内优化节点布局与关系线轨道。",
    aboutComponents: "AnimeMachine 本身不提供动画检索或下载，也不内置 Torrent、磁力链接或媒体内容。外部媒体目录按只读来源处理；实际下载与订阅由用户连接的外部组件完成。",
    aboutArchiveSource: "提供本地动画元数据底包与作品关系基础。",
    aboutBangumiSource: "用于补全底包中缺失或明显异常的公开元数据。",
    aboutMalSource: "在本地证据不足时作为作品身份和前后作关系的辅助来源。",
    aboutAniRssSource: "提供可选的新番订阅、状态同步与远程播放能力；AnimeMachine 通过其 HTTP API 连接。",
    aboutSubtitleSources: "是用户可选的外部字幕 API；字幕版权及使用条件由对应服务说明。",
    cards: "磁贴",
    table: "表格",
    settings: "设置",
    loginTitle: "登录 AnimeMachine",
    login: "登录",
    logout: "退出",
    username: "用户名",
    password: "密码",
    usersTab: "用户",
    usersHint: "普通用户可浏览和提交下载，但不能修改设置。",
    normalUser: "普通用户",
    administrator: "管理员",
    createUser: "创建用户",
    disableUser: "停用",
    disableInitialAdmin: "停用初始admin用户（建议创建新的管理员用户，停用初始admin用户）",
    enableUser: "启用",
    submissionAllowed: "允许提交",
    loadingDb: "正在读取数据库…",
    backgroundSync: "后台同步",
    loadingWorks: "作品加载中，请稍等...",
    resetAll: "重置全部",
    custom: "自定义",
    titleAlias: "标题 / 别名",
    searchPlaceholder: "中文、英文、日语或简称",
    era: "年代",
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
    personSearch: "输入姓名进行模糊搜索",
    tag: "题材 / 标签",
    all: "全部",
    works: " 部作品",
    loading: "正在检索…",
    none: "没有符合条件的作品。",
    failed: "载入失败",
    queryFailed: "查询失败",
    invalidDownloadRoute: "下载方式无效，请刷新页面后重新选择作品。",
    pending: "待完善",
    directoryDate: "目录年月",
    episodes: "话数",
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
    planBuilding: "正在后台生成大批量下载计划，页面可以继续使用…",
    planWarnings: "计划包含需要注意的既有内容或冲突。",
    libraryPathUnreadable: "无法读取收藏库目标路径。",
    localFileUnreadable: "无法读取本地文件。",
    managedFileChanged: "本地文件与已记录的来源校验值不一致。",
    exactComparisonUnavailable: "已计算本地哈希，但缺少可比较的来源校验基准，不能宣称内容完全相同。",
    managedHashBaselineRecorded: "已为来源明确的受管文件记录首个哈希基准。",
    targetIsSymbolicLink: "目标是符号链接或重解析文件，已阻止自动写入。",
    targetNotRegularFile: "目标位置已被目录或其它非普通文件占用。",
    localFileChangedDuringHash: "本地文件在哈希过程中发生变化，请等待写入完成后重试。",
    stagedReplacement: "旧文件会保留；新版完成下载和校验前只进入暂存区。",
    sizeConflict: "同一路径的既有文件大小不同，且候选资源没有充分的修订证据。",
    summary: "简介",
    settingsHint: "配置会持久保存；认证凭据只从运行环境读取，不写入配置文件。",
    acceptedContent: "允许的资源类别",
    startMode: "提交后的默认状态",
    stopped: "停止",
    start: "自动开始（需逐批确认）",
    allowAutoStart: "允许在计划确认时选择自动开始",
    save: "保存",
    saved: "已保存",
    availability: "可用来源",
    available: "有可用来源",
    unavailable: "无可用来源",
    downloadSources: "个可下载源",
    externalMediaAvailable: "外部媒体已映射",
    noAvailableSource: "无可用来源",
    libraryState: "收藏库状态",
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
    previewPlan: "开始下载",
    torrents: "可用资源",
    noTorrent: "没有符合当前资格策略的资源",
    excludedSourceClass: "片源类型不符",
    excludedSerialProfile: "语言不符",
    excludedResourceGroup: "资源组不符",
    excludedResolution: "分辨率不符",
    excludedSubtitle: "字幕不符",
    excludedScope: "作品范围不符",
    excludedManifest: "文件映射不符",
    excludedPolicy: "策略不符",
    searchPoolNow: "重新搜索本地资源",
    searchAniRss: "通过 Ani-RSS 查询资源",
    aniRssManaged: "Ani-RSS 管理",
    aniRssResources: "Ani-RSS 资源",
    aniRssMode: "调用模式",
    aniRssPrefer: "优先调用 Ani-RSS",
    aniRssFallback: "备选调用 Ani-RSS",
    aniRssManual: "手动调用 Ani-RSS",
    aniRssApiKey: "API Key（仅当前进程）",
    aniRssApiKeyHint: "推荐通过 .env 环境变量持久配置；页面输入的密钥不会写入配置文件。",
    aniRssCredentialConfigured: "API Key 已载入当前进程。",
    aniRssMediaPath: "Ani-RSS 媒体路径",
    aniRssSyncMinutes: "状态同步间隔（分钟）",
    aniRssHint: "连接无效时自动按“手动调用”运行；连接恢复后使用所选模式。媒体目录始终只读。",
    searchPoolRunning: "正在检索资源池…",
    searchPoolDone: "检索完成，已更新可用资源",
    searchPoolNone: "检索完成，仍未找到可安全关联的资源",
    verifyWork: "立即核验",
    verifyRunning: "正在核验…",
    scanIdle: "资源池待命",
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
    aniRssDelete: "删除",
    aniRssDeleteConfirm: "确认删除此 Ani-RSS 订阅及其已下载文件？",
    aniRssDeleteFailed: "Ani-RSS 删除未确认",
    themeSystem: "跟随系统",
    themeDark: "暗黑模式",
    themeLight: "浅色模式",
    remotePlaybackSource: "Ani-RSS 远程播放",
    playbackStartFile: "起始媒体文件",
    aniRssPathUnconfigured: "当前 Ani-RSS 媒体路径未配置，请使用播放器载入播放列表以观看。",
    aniRssPathUnavailable: "当前 Ani-RSS 媒体路径不能访问，可以使用播放器载入播放列表以观看。",
    aniRssPathAvailable: "当前 Ani-RSS 媒体路径可访问。",
    managed: "AnimeMachine 管理",
    preexisting: "本地既有",
    notInLibrary: "未登记",
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
    random: "随机",
    sortDate: "年月",
    sortTitle: "作品名",
    sortStudio: "制作公司",
    sortType: "作品类型",
    reshuffle: "重新随机",
    perPage: "每页",
    selectPage: "勾选本页可下载作品",
    previous: "上一页",
    next: "下一页",
    priorityDimensions: "优选维度（从上到下）",
    priorityHint:
      "从上到下比较优选维度；点击任一维度可展开其二级顺序或判定说明。",
    resourcePriorityTab: "资源优先级",
    fixedPriorityHint: "此维度按内置规则判定，没有可拖动的二级项。",
    resourceCompletenessHint: "完整合集或可完整拼接的发布流优先；连载中的发布流允许落后最新进度一集，落后两集及以上会降级。",
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
    archiveUpdating: "正在更新动画底库…",
    archiveUnchanged: "动画底库已是最新",
    archiveComplete: "更新完成",
    archiveFailed: "动画底库更新失败",
    other: "其它",
    generalTab: "常规",
    resourceGroupsTab: "资源组",
    resourceGroupsHint: "收藏类资源不检查字幕；连载类资源按选定语言匹配资源组与字幕标记。界面只显示组名，匹配词由后台维护。",
    serialSubtitleLanguage: "连载资源字幕语言",
    followInterfaceLanguage: "跟随界面语言",
    archiveGroups: "收藏类 / 归档类",
    archiveGroupsHint: "默认全部启用，排序不考虑字幕。",
    chineseSerialGroups: "中文连载资源",
    englishSerialGroups: "英文连载资源",
    japaneseSerialGroups: "日文连载资源",
    otherResourceGroups: "其它（未识别或不符合上述规则）",
    connectionsTab: "连接",
    subscriptionsTab: "订阅",
    logsTab: "日志",
    historyTab: "历史记录",
    subscriptionsHint: "已完成的单集或单卷任务会按精确资源指纹监视后续发布；发现更新后只生成待确认项。",
    noWatches: "暂无监视任务。",
    removeWatch: "删除监视",
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
      "Compose 部署会以服务名覆盖回环地址；持久凭据从 .env 环境变量读取。",
    endpoint: "服务地址",
    qbtApiKey: "API Key（仅当前进程）",
    qbtApiKeyPlaceholder: "留空则使用 .env 环境变量",
    qbtApiKeyHint: "推荐通过 .env 环境变量持久配置；在此输入的密钥仅保存在当前进程内，重启后失效。",
    qbtCredentialConfigured: "API Key 已在当前进程配置",
    torrentPoolPath: "Torrent 池路径",
    torrentPoolHint: "AnimeMachine 只监视此目录中的种子文件；种子由用户或外部工具自行放入。",
    libraryPath: "动画收藏库路径",
    qbtLibraryPath: "qBittorrent 容器内路径",
    testConnection: "测试连接",
    connectionOk: "连接成功",
    authRequired: "服务可达，需要认证",
    connectionFailed: "连接失败",
    externalLibraries: "外部只读媒体库",
    externalReadOnlyEnabled: "映射外部只读媒体库",
    externalReadOnlyType: "目录结构",
    genericLayout: "通用目录",
    aniRssLayout: "ani-rss 兼容目录",
    externalReadOnlyPath: "容器内只读路径",
    externalScanMinutes: "只读扫描间隔（分钟）",
    externalLibraryHint:
      "只读取文件名、路径、大小和修改时间，不写入外部媒体库；ani-rss 是常见的兼容来源之一。",
    playbackConnection: "外部播放器交接",
    playbackEnabled: "启用 M3U 播放清单",
    preferDirectPaths: "优先使用播放器可访问的 SMB / NFS 路径",
    playbackPublicUrl: "外部设备访问 AnimeMachine 的地址（可留空）",
    playlistTtl: "播放会话空闲失效时间（秒）",
    playlistMaximum: "播放会话最长有效时间（秒）",
    directPathMappings: "直连路径映射（每行：容器路径 => 播放器路径）",
    playbackConnectionHint: "直连不可用时自动生成支持 Range 的短时 HTTP 地址；不会转码或提供 Web 连续播放。",
    subtitleSources: "字幕源",
    subtitlesEnabled: "启用归档资源字幕匹配",
    assrtEndpoints: "ASSRT API 端点（每行一个）",
    assrtToken: "ASSRT Token（仅当前进程）",
    openSubtitlesEndpoint: "OpenSubtitles API 端点",
    openSubtitlesKey: "OpenSubtitles API Key（仅当前进程）",
    subtitleSourcesHint: "只使用已配置的正式 API；按当前界面语言排序，低置信度结果必须由用户选择。",
    searchSubtitles: "检索字幕", useSubtitle: "使用该字幕", subtitlePresent: "字幕已存在",
    subtitleEmbedded: "媒体已内嵌字幕", subtitleNotFound: "未找到可靠字幕", subtitleApplied: "字幕已安装",
    logsHint: "仅显示近期的重要状态、警告和错误；不会显示密钥。",
    historyHint: "显示对用户原有内容执行的移动、改名与可恢复移除；AnimeMachine 自行创建的控制文件不占用此列表。",
    noHistory: "暂无用户内容变更记录。",
    historyMove: "移动",
    historyRename: "重命名",
    historyRemove: "可恢复移除",
    historyRestored: "已恢复",
    restoreHistory: "恢复",
    refresh: "刷新",
    noLogs: "暂无重要日志。",
    auditLibrary: "核验全部本地资源",
    auditStarted: "本地资源核验已在后台分批启动。",
    automation: "自动化与容量",
    pollMinutes: "资源池扫描间隔（分钟）",
    minimumFree: "最低保留空间（TiB）",
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
    catalog: "AnimeMachine",
    aboutTitle: "About AnimeMachine",
    versionLabel: "Version",
    aboutLead: "AnimeMachine is an automated anime-library system that organizes anime metadata, torrent pools, media directories and external read-only libraries under one local Catalog and state model.",
    aboutDetail: "It handles resource screening, download handoff, multi-level directory planning, series relationships, collection verification, Web browsing and long-term storage. qBittorrent, Ani-RSS and Torrent Collector are optional components.",
    aboutRelations: "The relationship graph uses Bangumi Archive and supporting evidence to organize prequels, sequels, recaps, side stories, derivatives, alternate adaptations and cross-series links, while optimizing node placement and edge tracks within practical runtime limits.",
    aboutComponents: "AnimeMachine does not provide anime search or downloads and does not bundle torrents, magnet links or media content. External media directories remain read-only; actual downloads and subscriptions are handled by components connected by the user.",
    aboutArchiveSource: "supplies the local catalog base and relationship evidence.",
    aboutBangumiSource: "fills public metadata that is missing or clearly inconsistent in the archive.",
    aboutMalSource: "provides supporting identity and chronology evidence when local evidence is insufficient.",
    aboutAniRssSource: "provides optional seasonal subscriptions, state synchronization and remote playback through its HTTP API.",
    aboutSubtitleSources: "is an optional external subtitle API; subtitle rights and usage terms remain with the provider.",
    cards: "Cards",
    table: "Table",
    settings: "Settings",
    loginTitle: "Sign in to AnimeMachine",
    login: "Sign in",
    logout: "Sign out",
    username: "Username",
    password: "Password",
    usersTab: "Users",
    usersHint: "Users may browse and submit downloads, but cannot change settings.",
    normalUser: "User",
    administrator: "Administrator",
    createUser: "Create user",
    disableUser: "Disable",
    disableInitialAdmin: "Disable the initial admin user (create a new administrator first)",
    enableUser: "Enable",
    submissionAllowed: "Allow submissions",
    loadingDb: "Loading database…",
    backgroundSync: "Background sync",
    loadingWorks: "Loading works, please wait...",
    resetAll: "Reset all",
    custom: "Custom",
    titleAlias: "Title / alias",
    searchPlaceholder: "English, Japanese, Chinese or abbreviation",
    era: "Era",
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
    personSearch: "Type a name to search",
    tag: "Theme / tag",
    all: "All",
    works: " works",
    loading: "Searching…",
    none: "No matching works.",
    failed: "Load failed",
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
    planBuilding: "Building the large download plan in the background. You may keep using this page…",
    planWarnings: "The plan contains existing-content notices or conflicts.",
    libraryPathUnreadable: "The library target path could not be read.",
    localFileUnreadable: "The local file could not be read.",
    managedFileChanged:
      "The local file differs from its recorded provenance hash.",
    exactComparisonUnavailable: "The local hash was computed, but no comparable source digest exists; exact equality cannot be claimed.",
    managedHashBaselineRecorded: "An initial hash baseline was recorded for the provenance-backed managed file.",
    targetIsSymbolicLink: "The target is a symbolic link or reparse file; automatic writes were blocked.",
    targetNotRegularFile: "The target is occupied by a directory or another non-regular file.",
    localFileChangedDuringHash: "The local file changed while hashing; retry after all writes finish.",
    stagedReplacement:
      "The old file is preserved; the revision stays in staging until download and verification complete.",
    sizeConflict:
      "A file at the same target has a different size and the candidate lacks sufficient revision evidence.",
    summary: "Summary",
    settingsHint:
      "Settings persist; credentials are read from the runtime environment and never written here.",
    acceptedContent: "Allowed release classes",
    startMode: "Default state after submission",
    stopped: "Stopped",
    start: "Auto start (confirm each batch)",
    allowAutoStart: "Allow auto-start after explicit plan confirmation",
    save: "Save",
    saved: "Saved",
    availability: "Available sources",
    available: "Has an available source",
    unavailable: "No available source",
    downloadSources: " download sources",
    externalMediaAvailable: "External media mapped",
    noAvailableSource: "No available source",
    libraryState: "Library",
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
    previewPlan: "Start download",
    torrents: "Available resources",
    noTorrent: "No source qualifies under the current policy",
    excludedSourceClass: "Source type mismatch",
    excludedSerialProfile: "Language mismatch",
    excludedResourceGroup: "Release group mismatch",
    excludedResolution: "Resolution mismatch",
    excludedSubtitle: "Subtitle mismatch",
    excludedScope: "Work scope mismatch",
    excludedManifest: "File mapping mismatch",
    excludedPolicy: "Policy mismatch",
    searchPoolNow: "Search local resources again",
    searchAniRss: "Search via Ani-RSS",
    aniRssManaged: "Managed by Ani-RSS",
    aniRssResources: "Ani-RSS resources",
    aniRssMode: "Invocation mode",
    aniRssPrefer: "Prefer Ani-RSS",
    aniRssFallback: "Use Ani-RSS as fallback",
    aniRssManual: "Use Ani-RSS manually",
    aniRssApiKey: "API key (current process only)",
    aniRssApiKeyHint: "Use the .env environment variable for persistence; a key entered here is never written to configuration.",
    aniRssCredentialConfigured: "API key loaded for the current process.",
    aniRssMediaPath: "Ani-RSS media path",
    aniRssSyncMinutes: "State sync interval (minutes)",
    aniRssHint: "An unavailable connection falls back to manual mode; the chosen mode resumes after recovery. Media access is always read-only.",
    searchPoolRunning: "Searching the torrent pool…",
    searchPoolDone: "Search complete; available sources updated",
    searchPoolNone: "Search complete; no safely linkable source found",
    verifyWork: "Verify now",
    verifyRunning: "Verifying…",
    scanIdle: "Torrent pool idle",
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
    aniRssDelete: "Delete",
    aniRssDeleteConfirm: "Delete this Ani-RSS subscription and its downloaded files?",
    aniRssDeleteFailed: "Ani-RSS deletion was not confirmed",
    themeSystem: "System theme",
    themeDark: "Dark mode",
    themeLight: "Light mode",
    remotePlaybackSource: "Ani-RSS remote playback",
    playbackStartFile: "Starting media file",
    aniRssPathUnconfigured: "The Ani-RSS media path is not configured. Load the playlist in your player to watch.",
    aniRssPathUnavailable: "The Ani-RSS media path is not accessible. Load the playlist in your player to watch.",
    aniRssPathAvailable: "The Ani-RSS media path is accessible.",
    managed: "Managed by AnimeMachine",
    preexisting: "Pre-existing",
    notInLibrary: "Not registered",
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
    random: "Random",
    sortDate: "Date",
    sortTitle: "Title",
    sortStudio: "Studio",
    sortType: "Format",
    reshuffle: "Reshuffle",
    perPage: "Per page",
    selectPage: "Select downloadable works on this page",
    previous: "Previous",
    next: "Next",
    priorityDimensions: "Ranking dimensions (top to bottom)",
    priorityHint:
      "Dimensions are compared from top to bottom. Select one to expand its secondary order or decision notes.",
    resourcePriorityTab: "Resource priority",
    fixedPriorityHint: "This dimension follows a built-in decision rule and has no draggable secondary items.",
    resourceCompletenessHint: "Complete collections or fully stitchable release streams rank first. An airing stream may trail the pool frontier by one episode; two or more episodes lowers its rank.",
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
    archiveUpdating: "Updating catalog base…",
    archiveUnchanged: "Catalog base is current",
    archiveComplete: "Catalog base updated",
    archiveFailed: "Catalog update failed",
    other: "Other",
    generalTab: "General",
    resourceGroupsTab: "Release groups",
    resourceGroupsHint: "Archive releases ignore subtitles. Serial releases match both the selected language profile and release markers; matching keywords remain internal.",
    serialSubtitleLanguage: "Serial subtitle language",
    followInterfaceLanguage: "Follow interface language",
    archiveGroups: "Collection / archive",
    archiveGroupsHint: "All are enabled by default; subtitle presence does not affect this order.",
    chineseSerialGroups: "Chinese serial releases",
    englishSerialGroups: "English serial releases",
    japaneseSerialGroups: "Japanese serial releases",
    otherResourceGroups: "Other (unrecognized or unmatched)",
    connectionsTab: "Connections",
    subscriptionsTab: "Subscriptions",
    logsTab: "Logs",
    historyTab: "History",
    subscriptionsHint: "Completed episode or volume jobs watch for later releases with the exact same release fingerprint. Matches remain pending for confirmation.",
    noWatches: "No active watches.",
    removeWatch: "Remove watch",
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
      "Compose overrides loopback addresses with service names; persistent credentials come from .env environment variables.",
    endpoint: "Endpoint",
    qbtApiKey: "API Key (this process only)",
    qbtApiKeyPlaceholder: "Leave blank to use the .env environment variable",
    qbtApiKeyHint: "Use the .env environment variable for persistence. A key entered here stays only in the current process and expires on restart.",
    qbtCredentialConfigured: "API Key is configured for this process",
    torrentPoolPath: "Torrent pool path",
    torrentPoolHint: "AnimeMachine only watches this directory. Torrents are supplied by the user or an external tool.",
    libraryPath: "Anime library path",
    qbtLibraryPath: "Path inside qBittorrent",
    testConnection: "Test connection",
    connectionOk: "Connected",
    authRequired: "Service reachable; authentication required",
    connectionFailed: "Connection failed",
    externalLibraries: "External read-only libraries",
    externalReadOnlyEnabled: "Map an external read-only library",
    externalReadOnlyType: "Directory layout",
    genericLayout: "Generic folders",
    aniRssLayout: "ani-rss compatible",
    externalReadOnlyPath: "Read-only path inside the container",
    externalScanMinutes: "Read-only scan interval (minutes)",
    externalLibraryHint:
        "Reads names, paths, sizes and modification times without writing to the external library. ani-rss is one common compatible source.",
    playbackConnection: "External player handoff",
    playbackEnabled: "Enable M3U playlists",
    preferDirectPaths: "Prefer player-accessible SMB / NFS paths",
    playbackPublicUrl: "AnimeMachine URL reachable by external devices (optional)",
    playlistTtl: "Playback idle timeout (seconds)",
    playlistMaximum: "Maximum playback session lifetime (seconds)",
    directPathMappings: "Direct path mappings (one per line: container path => player path)",
    playbackConnectionHint: "Falls back to short-lived Range-enabled HTTP URLs. No transcoding or continuous Web playback.",
    subtitleSources: "Subtitle sources",
    subtitlesEnabled: "Enable subtitle matching for archive releases",
    assrtEndpoints: "ASSRT API endpoints (one per line)",
    assrtToken: "ASSRT token (current process only)",
    openSubtitlesEndpoint: "OpenSubtitles API endpoint",
    openSubtitlesKey: "OpenSubtitles API key (current process only)",
    subtitleSourcesHint: "Only configured official APIs are used. Results follow the UI language; low-confidence matches require manual selection.",
    searchSubtitles: "Find subtitles", useSubtitle: "Use subtitle", subtitlePresent: "Subtitles available",
    subtitleEmbedded: "Embedded subtitles detected", subtitleNotFound: "No reliable subtitles found", subtitleApplied: "Subtitles installed",
    logsHint:
      "Recent important status, warnings and errors only; secrets are never shown.",
    refresh: "Refresh",
    noLogs: "No important events yet.",
    historyHint: "Shows moves, renames and recoverable removals of pre-existing user content. AnimeMachine-created control files are omitted.",
    noHistory: "No user-content changes yet.",
    historyMove: "Move",
    historyRename: "Rename",
    historyRemove: "Recoverable removal",
    historyRestored: "Restored",
    restoreHistory: "Restore",
    auditLibrary: "Verify all local resources",
    auditStarted: "Local verification started in background batches.",
    automation: "Automation & capacity",
    pollMinutes: "Pool scan interval (minutes)",
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
    catalog: "AnimeMachine",
    aboutTitle: "AnimeMachineについて",
    versionLabel: "バージョン",
    aboutLead: "AnimeMachine は、アニメのメタデータ、Torrent プール、メディアディレクトリ、外部の読み取り専用ライブラリを、一つのローカル Catalog と状態モデルに整理する全自動アニメライブラリシステムです。",
    aboutDetail: "リソース選別、ダウンロード引き渡し、多階層ディレクトリ計画、シリーズ関係、所蔵検証、Web 閲覧、長期保管を扱います。qBittorrent、Ani-RSS、Torrent Collector は任意のコンポーネントです。",
    aboutRelations: "作品関係図は Bangumi Archive と補助根拠から前作、続編、総集編、番外、派生、別解釈、シリーズ横断関係を整理し、実用的な計算時間内でノード配置と関係線の経路を最適化します。",
    aboutComponents: "AnimeMachine 自体はアニメの検索やダウンロードを提供せず、Torrent、マグネットリンク、メディア内容も内蔵しません。外部メディアディレクトリは読み取り専用で扱い、実際のダウンロードと購読はユーザーが接続した外部コンポーネントが実行します。",
    aboutArchiveSource: "ローカルカタログのベースと作品関係の根拠を提供します。",
    aboutBangumiSource: "アーカイブで欠落または明らかに不整合な公開メタデータを補完します。",
    aboutMalSource: "ローカル根拠が不足する場合に作品同定と前後関係の補助根拠として使用します。",
    aboutAniRssSource: "HTTP API を通じて、任意の新番購読、状態同期、リモート再生機能を提供します。",
    aboutSubtitleSources: "任意で利用する外部字幕APIです。字幕の権利と利用条件は各サービスに従います。",
    cards: "カード",
    table: "表",
    settings: "設定",
    loginTitle: "AnimeMachine にログイン",
    login: "ログイン",
    logout: "ログアウト",
    username: "ユーザー名",
    password: "パスワード",
    usersTab: "ユーザー",
    usersHint: "一般ユーザーは閲覧とダウンロード登録ができますが、設定は変更できません。",
    normalUser: "一般ユーザー",
    administrator: "管理者",
    createUser: "ユーザー作成",
    disableUser: "無効化",
    disableInitialAdmin: "初期 admin ユーザーを無効化（先に新しい管理者ユーザーを作成してください）",
    enableUser: "有効化",
    submissionAllowed: "登録を許可",
    loadingDb: "データベースを読み込み中…",
    backgroundSync: "バックグラウンド同期",
    loadingWorks: "作品を読み込んでいます。しばらくお待ちください...",
    resetAll: "すべてリセット",
    custom: "カスタム",
    titleAlias: "タイトル / 別名",
    searchPlaceholder: "日本語・英語・中国語・略称",
    era: "年代",
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
    personSearch: "名前を入力して検索",
    tag: "ジャンル / タグ",
    all: "すべて",
    works: " 作品",
    loading: "検索中…",
    none: "該当作品はありません。",
    failed: "読込失敗",
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
    planBuilding: "大規模なダウンロード計画をバックグラウンドで作成しています。このページは引き続き利用できます…",
    planWarnings: "既存内容の注意または競合を含む計画です。",
    libraryPathUnreadable: "ライブラリの対象パスを読み取れません。",
    localFileUnreadable: "ローカルファイルを読み取れません。",
    managedFileChanged:
      "ローカルファイルが記録済みの出所ハッシュと一致しません。",
    exactComparisonUnavailable: "ローカルハッシュは計算済みですが、比較可能な出所ハッシュがないため完全一致とは判定できません。",
    managedHashBaselineRecorded: "出所が確定した管理対象ファイルの初回ハッシュ基準を記録しました。",
    targetIsSymbolicLink: "保存先がシンボリックリンクまたは再解析ファイルのため、自動書き込みを停止しました。",
    targetNotRegularFile: "保存先がディレクトリまたは通常ファイル以外の項目で占有されています。",
    localFileChangedDuringHash: "ハッシュ計算中にローカルファイルが変化しました。書き込み完了後に再試行してください。",
    stagedReplacement:
      "旧ファイルを保持し、ダウンロードと検証が完了するまで更新版を一時保存します。",
    sizeConflict:
      "同じ対象パスの既存ファイルはサイズが異なり、候補に十分な更新根拠がありません。",
    summary: "あらすじ",
    settingsHint:
      "設定は保存されます。認証情報は実行環境からのみ読み込み、設定ファイルには保存しません。",
    acceptedContent: "許可するリリース種別",
    startMode: "登録後の状態",
    stopped: "停止",
    start: "自動開始（バッチごとに確認）",
    allowAutoStart: "計画確認時のみ自動開始を許可",
    save: "保存",
    saved: "保存しました",
    availability: "利用可能なソース",
    available: "利用可能なソースあり",
    unavailable: "利用可能なソースなし",
    downloadSources: "件のダウンロードソース",
    externalMediaAvailable: "外部メディア対応済み",
    noAvailableSource: "利用可能なソースなし",
    libraryState: "ライブラリ",
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
    previewPlan: "ダウンロードを開始",
    torrents: "利用可能なリソース",
    noTorrent: "現在の条件を満たすリソースはありません",
    excludedSourceClass: "ソース種別不一致",
    excludedSerialProfile: "言語不一致",
    excludedResourceGroup: "リリースグループ不一致",
    excludedResolution: "解像度不一致",
    excludedSubtitle: "字幕不一致",
    excludedScope: "作品範囲不一致",
    excludedManifest: "ファイル対応不一致",
    excludedPolicy: "ポリシー不一致",
    searchPoolNow: "ローカルリソースを再検索",
    searchAniRss: "Ani-RSS でリソースを検索",
    aniRssManaged: "Ani-RSS 管理",
    aniRssResources: "Ani-RSS リソース",
    aniRssMode: "呼び出しモード",
    aniRssPrefer: "Ani-RSS を優先",
    aniRssFallback: "Ani-RSS を予備として使用",
    aniRssManual: "Ani-RSS を手動で使用",
    aniRssApiKey: "API キー（現在のプロセスのみ）",
    aniRssApiKeyHint: "永続化には .env の環境変数を使用してください。ここで入力したキーは設定に保存されません。",
    aniRssCredentialConfigured: "API キーを現在のプロセスに読み込みました。",
    aniRssMediaPath: "Ani-RSS メディアパス",
    aniRssSyncMinutes: "状態同期間隔（分）",
    aniRssHint: "接続できない場合は自動的に手動モードになり、復旧後に選択したモードへ戻ります。メディアは常に読み取り専用です。",
    searchPoolRunning: "Torrent プールを検索中…",
    searchPoolDone: "検索が完了し、利用可能なソースを更新しました",
    searchPoolNone: "検索完了。安全に関連付けられるソースは見つかりませんでした",
    verifyWork: "今すぐ確認",
    verifyRunning: "確認中…",
    scanIdle: "Torrent プール待機中",
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
    aniRssDelete: "削除",
    aniRssDeleteConfirm: "この Ani-RSS 購読とダウンロード済みファイルを削除しますか？",
    aniRssDeleteFailed: "Ani-RSS の削除を確認できませんでした",
    themeSystem: "システム設定",
    themeDark: "ダークモード",
    themeLight: "ライトモード",
    remotePlaybackSource: "Ani-RSS リモート再生",
    playbackStartFile: "開始メディアファイル",
    aniRssPathUnconfigured: "Ani-RSS のメディアパスが設定されていません。プレイヤーでプレイリストを読み込んで再生してください。",
    aniRssPathUnavailable: "Ani-RSS のメディアパスにアクセスできません。プレイヤーでプレイリストを読み込んで再生してください。",
    aniRssPathAvailable: "Ani-RSS のメディアパスにアクセスできます。",
    managed: "AnimeMachine管理",
    preexisting: "既存ファイル",
    notInLibrary: "未登録",
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
    random: "ランダム",
    sortDate: "年月",
    sortTitle: "作品名",
    sortStudio: "制作会社",
    sortType: "形式",
    reshuffle: "再シャッフル",
    perPage: "表示数",
    selectPage: "このページの取得可能作品を選択",
    previous: "前へ",
    next: "次へ",
    priorityDimensions: "優先判定軸（上から下）",
    priorityHint:
      "上から順に比較します。項目を選択すると、二次順位または判定説明を展開できます。",
    resourcePriorityTab: "リソース優先順位",
    fixedPriorityHint: "この項目は内蔵ルールで判定され、並べ替え可能な二次項目はありません。",
    resourceCompletenessHint: "完全なコレクションまたは全話を連結できる配信系列を優先します。放送中は最新より1話遅れまで許容し、2話以上遅れると順位を下げます。",
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
    archiveUpdating: "カタログベースを更新中…",
    archiveUnchanged: "カタログベースは最新です",
    archiveComplete: "カタログベースを更新しました",
    archiveFailed: "カタログ更新失敗",
    other: "その他",
    generalTab: "一般",
    resourceGroupsTab: "リリースグループ",
    resourceGroupsHint: "アーカイブ向けは字幕を問いません。連載向けは選択言語のグループと字幕表記を照合します。照合語は内部で管理します。",
    serialSubtitleLanguage: "連載リリースの字幕言語",
    followInterfaceLanguage: "UI 言語に合わせる",
    archiveGroups: "収集 / アーカイブ",
    archiveGroupsHint: "初期状態ですべて有効。字幕は順位に影響しません。",
    chineseSerialGroups: "中国語連載リリース",
    englishSerialGroups: "英語連載リリース",
    japaneseSerialGroups: "日本語連載リリース",
    otherResourceGroups: "その他（未識別または条件外）",
    connectionsTab: "接続",
    subscriptionsTab: "購読",
    logsTab: "ログ",
    historyTab: "履歴",
    subscriptionsHint: "完了した単話・単巻タスクと同一のリリース指紋で後続リリースを監視し、確認待ちとして表示します。",
    noWatches: "監視タスクはありません。",
    removeWatch: "監視を削除",
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
      "Compose ではループバックアドレスをサービス名で上書きします。永続的な認証情報は .env の環境変数から読み込みます。",
    endpoint: "接続先",
    qbtApiKey: "API Key（現在のプロセスのみ）",
    qbtApiKeyPlaceholder: "空欄の場合は .env の環境変数を使用",
    qbtApiKeyHint: ".env の環境変数による永続設定を推奨します。ここで入力したキーは現在のプロセス内だけに保持され、再起動時に消えます。",
    qbtCredentialConfigured: "現在のプロセスに API Key を設定済み",
    torrentPoolPath: "Torrent プールのパス",
    torrentPoolHint: "AnimeMachine はこのディレクトリを監視するだけです。Torrent はユーザーまたは外部ツールが配置します。",
    libraryPath: "アニメライブラリのパス",
    qbtLibraryPath: "qBittorrent コンテナ内のパス",
    testConnection: "接続テスト",
    connectionOk: "接続成功",
    authRequired: "サービスに到達しました。認証が必要です。",
    connectionFailed: "接続失敗",
    externalLibraries: "外部読み取り専用メディアライブラリ",
    externalReadOnlyEnabled: "外部読み取り専用ライブラリを対応付ける",
    externalReadOnlyType: "ディレクトリ構成",
    genericLayout: "汎用フォルダー",
    aniRssLayout: "ani-rss 互換",
    externalReadOnlyPath: "コンテナ内の読み取り専用パス",
    externalScanMinutes: "読み取り専用走査間隔（分）",
    externalLibraryHint:
        "名前・パス・サイズ・更新時刻だけを読み取り、外部ライブラリへは書き込みません。ani-rss は一般的な互換元の一つです。",
    playbackConnection: "外部プレーヤー連携",
    playbackEnabled: "M3Uプレイリストを有効にする",
    preferDirectPaths: "プレーヤーから到達できるSMB / NFSパスを優先",
    playbackPublicUrl: "外部端末から到達できるAnimeMachine URL（任意）",
    playlistTtl: "再生セッションのアイドル期限（秒）",
    playlistMaximum: "再生セッションの最長有効時間（秒）",
    directPathMappings: "直接パスの対応（1行ごと：コンテナパス => プレーヤーパス）",
    playbackConnectionHint: "直接接続できない場合はRange対応の短時間HTTP URLを生成します。変換やWeb連続再生は行いません。",
    subtitleSources: "字幕ソース",
    subtitlesEnabled: "アーカイブ作品の字幕照合を有効にする",
    assrtEndpoints: "ASSRT APIエンドポイント（1行に1件）",
    assrtToken: "ASSRTトークン（現在のプロセスのみ）",
    openSubtitlesEndpoint: "OpenSubtitles APIエンドポイント",
    openSubtitlesKey: "OpenSubtitles APIキー（現在のプロセスのみ）",
    subtitleSourcesHint: "設定済みの公式APIだけを使用します。日本語では字幕なしも許容し、低信頼の候補は手動選択とします。",
    searchSubtitles: "字幕を検索", useSubtitle: "この字幕を使用", subtitlePresent: "字幕あり",
    subtitleEmbedded: "内蔵字幕を検出", subtitleNotFound: "信頼できる字幕が見つかりません", subtitleApplied: "字幕を導入しました",
    logsHint:
      "最近の重要な状態・警告・エラーのみ表示します。秘密情報は表示しません。",
    refresh: "更新",
    noLogs: "重要なログはまだありません。",
    historyHint: "既存のユーザーコンテンツに対する移動、名称変更、復元可能な削除を表示します。AnimeMachine が作成した制御ファイルは除外します。",
    noHistory: "ユーザーコンテンツの変更履歴はありません。",
    historyMove: "移動",
    historyRename: "名称変更",
    historyRemove: "復元可能な削除",
    historyRestored: "復元済み",
    restoreHistory: "復元",
    auditLibrary: "ローカルリソースをすべて確認",
    auditStarted: "ローカル確認をバックグラウンドで分割開始しました。",
    automation: "自動化・容量",
    pollMinutes: "プール走査間隔（分）",
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
const api = async (url, opt = {}) => {
    const method = String(opt.method || "GET").toUpperCase(),
      headers = new Headers(opt.headers || {}),
      timeoutMs = Math.max(0, Number(opt.timeoutMs || 0)),
      timeoutController = timeoutMs > 0 && !opt.signal ? new AbortController() : null,
      fetchOptions = { ...opt, headers, credentials: "same-origin" };
    delete fetchOptions.timeoutMs;
    if (!(["GET", "HEAD", "OPTIONS"].includes(method)) && csrfToken)
      headers.set("X-CSRF-Token", csrfToken);
    if (timeoutController) fetchOptions.signal = timeoutController.signal;
    const timeout = timeoutController
      ? setTimeout(() => timeoutController.abort(), timeoutMs)
      : null;
    let r;
    try {
      r = await fetch(url, fetchOptions);
    } catch (error) {
      if (timeoutController?.signal.aborted) {
        const timeoutError = new Error(`Request timed out: ${url}`);
        timeoutError.code = "request_timeout";
        throw timeoutError;
      }
      throw error;
    } finally {
      if (timeout) clearTimeout(timeout);
    }
    const text = await r.text();
    let b = {};
    if (text) {
      try {
        b = JSON.parse(text);
      } catch (_) {
        const responseError = new Error(
          r.ok ? `Invalid JSON response: ${url}` : `${r.status} ${r.statusText || "HTTP error"}`,
        );
        responseError.status = r.status;
        throw responseError;
      }
    }
    if (!r.ok) {
      if (r.status === 401 && url !== "/api/auth/login") showLogin();
      const responseError = new Error(b.error || `${r.status}`);
      responseError.status = r.status;
      throw responseError;
    }
    return b;
  },
  languageBase = (value) =>
    String(value || "")
      .split("-")[0]
      .toLowerCase(),
  sameAsOriginalLanguage = (x) =>
    languageBase(language) === languageBase(x.original_language || "ja"),
  preferred = (x) =>
    sameAsOriginalLanguage(x)
      ? x.title_ja
      : language === "zh-Hans"
        ? x.title_zh_hans || x.title_ja
        : language === "en"
          ? x.title_en || x.title_ja
          : x.title_ja,
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
    .forEach((e) => (e.textContent = t(e.dataset.i18n)));
  document
    .querySelectorAll("[data-i18n-placeholder]")
    .forEach((e) => (e.placeholder = t(e.dataset.i18nPlaceholder)));
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
  applyTheme();
  document
    .querySelectorAll("option[data-group]")
    .forEach(
      (x) =>
        (x.textContent =
          x.parentElement.id === "era"
            ? eraLabel(x.dataset.code)
            : label(x.dataset.group, x.dataset.code)),
    );
  sortCountryOptions();
  renderMediaChecks();
  if ($("settingsDialog").open) renderPolicy();
  render();
  updateSelection();
  updateSortControls();
  if (catalogStats.record_count)
    $("buildInfo").textContent = archiveSummary(catalogStats);
  if (catalogStats.sync) renderScanProgress(catalogStats);
}
function updateSortControls() {
  const random = sort === "random";
  $("reshuffle").hidden = !random;
  $("sortDirection").hidden = random;
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
    `<option value=""${id === "era" ? ' data-i18n="custom"' : ""}>${t(id === "era" ? "custom" : "all")}</option>` +
    rows
      .map(
        (x) =>
          `<option value="${esc(x)}" data-group="${g || ""}" data-code="${esc(x)}">${esc(id === "era" ? eraLabel(x) : g ? label(g, x) : x)}</option>`,
      )
      .join("");
}
function sortCountryOptions() {
  if (!options.countries) return;
  const current = $("country").value;
  const collator = new Intl.Collator(language, { sensitivity: "base" });
  fill(
    "country",
    [...options.countries].sort((a, b) => {
      const aOther = String(a).toLowerCase() === "other",
        bOther = String(b).toLowerCase() === "other";
      return (
        Number(aOther) - Number(bOther) ||
        collator.compare(label("country", a), label("country", b))
      );
    }),
    "country",
  );
  $("country").value = current || config.ui?.filterDefaults?.country || "JP";
}
function renderMediaChecks() {
  if (!options.media_types) return;
  const cur = new Set(
      [...$("media_type").querySelectorAll("input:checked")].map(
        (x) => x.value,
      ),
    ),
    def = new Set(config.ui?.filterDefaults?.mediaTypes || ["tv", "movie"]),
    use = cur.size ? cur : def;
  $("media_type").innerHTML = options.media_types
    .map(
      (c) =>
        `<label class="check"><input type="checkbox" value="${esc(c)}" ${use.has(c) ? "checked" : ""}><span>${esc(label("media", c))}</span></label>`,
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
    country: $("country"),
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
    availability: Number(catalogStats.runtime?.torrents || 0) === 0
      ? ["available", "unavailable"]
      : (config.ui?.filterDefaults?.availability || ["available"]),
    library_state: config.ui?.filterDefaults?.libraryStates || [
      "existing",
      "placeholder",
      "queued",
      "downloading",
      "external",
      "absent",
    ],
  };
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
    if (Array.isArray(state.mediaTypes)) {
      const values = new Set(state.mediaTypes);
      $("media_type").querySelectorAll("input").forEach((item) => (item.checked = values.has(item.value)));
    }
    Object.entries(state.statuses || {}).forEach(([key, values]) => {
      const selectedValues = new Set(values || []);
      statusGroups[key]?.querySelectorAll("input").forEach((item) => (item.checked = selectedValues.has(item.value)));
    });
    syncEraFromDates();
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
async function search() {
  searchController?.abort();
  const controller = new AbortController();
  searchController = controller;
  cancelCoverLoads();
  results.innerHTML = `<div class="empty">${t("loading")}</div>`;
  try {
    const d = await api(`/api/anime?${params()}`, { signal: controller.signal });
    if (searchController !== controller) return;
    items = d.items;
    total = d.total;
    searchExpanded = Boolean(d.searchExpanded);
    render();
  } catch (e) {
    if (e.name === "AbortError") return;
    results.innerHTML = `<div class="error">${t("queryFailed")}: ${esc(e.message)}</div>`;
  } finally {
    if (searchController === controller) searchController = null;
  }
}
const sourceBadge = (x, compact = false) => {
    const count = Number(x.usable_torrent_count || 0);
    const text = count
      ? `${fmt(count)}${t("downloadSources")}`
      : x.ani_rss_managed
        ? t("aniRssManaged")
        : Number(x.ani_rss_resource_count || 0)
          ? `${fmt(x.ani_rss_resource_count)} ${t("aniRssResources")}`
      : x.has_external_media
        ? t("externalMediaAvailable")
        : t("noAvailableSource");
    const kind = count ? "" : x.ani_rss_managed ? "ani-rss-managed" : Number(x.ani_rss_resource_count || 0) ? "ani-rss-resource" : x.has_external_media ? "external" : "muted-badge";
    return `<span class="badge ${kind}">${esc(text)}</span>`;
  },
  stateBadge = (x) => {
    const s = x.library_state || "absent";
    const text = s === "existing"
      ? t(x.library_managed ? "managedComplete" : "localExisting")
      : humanCode(s);
    return `<span class="badge library ${esc(s)}">${esc(text)}</span>`;
  },
  selectable = (x) =>
    (x.usable_torrent_count > 0 || x.ani_rss_resource_count > 0) &&
    ![
      "queued",
      "downloading",
      "occupied_review",
      "deprecated",
      "upgrade_staged",
      "upgrade_blocked",
    ].includes(x.library_state),
  selector = (x) =>
    selectable(x)
      ? `<label class="pick"><input type="checkbox" data-select="${x.id}" ${selected.has(x.id) ? "checked" : ""}></label>`
      : `<label class="pick"><input type="checkbox" disabled></label>`,
  relationButton = (x) =>
    Number(x.series_member_count || 1) > 1
      ? `<button class="relation-button" data-relations="${x.id}" type="button"><span aria-hidden="true">⑂</span>${t("viewRelations")} · ${fmt(x.series_member_count)}</button>`
      : "";
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
function render() {
  if (!$("total")) return;
  const downloadable = items.filter(selectable);
  items.filter((x) => !selectable(x)).forEach((x) => selected.delete(x.id));
  $("selectPage").disabled = !downloadable.length;
  $("selectPage").checked = downloadable.length > 0 && downloadable.every((x) => selected.has(x.id));
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
    results.innerHTML = `<div class="empty">${t(bootstrapping ? "loadingWorks" : "none")}</div>`;
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
  if (view === "table")
    results.innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th></th><th>${t("startMonth")}</th><th>${t("titleAlias")}</th><th>${t("mediaFormat")}</th><th>${t("availability")}</th><th>${t("libraryState")}</th><th>${t("studio")}</th></tr></thead><tbody>${items.map((x, index) => `${dividerBefore(index, true)}<tr data-id="${x.id}"><td>${selector(x)}${completeBadge(x)}</td><td>${esc(localMonth(x.start_month))}</td><td><b>${esc(preferred(x))}</b>${secondaryTitle(x) ? `<br><span class="muted">${esc(secondaryTitle(x))}</span>` : ""}${relationButton(x)}</td><td>${esc(label("media", x.media_code))}</td><td>${sourceBadge(x, true)}</td><td>${stateBadge(x)}</td><td>${esc((x.studios || []).join(" × "))}</td></tr>`).join("")}</tbody></table></div>`;
  else
    results.innerHTML = items
      .map(
        (x, index) =>
          `${dividerBefore(index)}<article class="card ${imagesEnabled ? "with-cover" : ""}" data-id="${x.id}" ${imagesEnabled ? `data-cover="${x.id}"` : ""}><div class="card-content">${selector(x)}<div class="badges">${sourceBadge(x)}${stateBadge(x)}${completeBadge(x)}</div><span class="date">${esc(localMonth(x.start_month))} · ${esc(label("media", x.media_code))}</span><h3>${esc(preferred(x))}</h3>${secondaryTitle(x) ? `<p class="cn">${esc(secondaryTitle(x))}</p>` : ""}<div class="chips">${String(
            x.tags || "",
          )
            .split(" / ")
            .filter(Boolean)
            .map((y) => `<span class="chip">${esc(label("theme", y))}</span>`)
            .join(
              "",
            )}</div><div class="meta">${x.studios?.length ? esc(x.studios.join(" × ")) : t("pending")}</div>${relationButton(x)}</div></article>`,
      )
      .join("");
  bind();
  covers();
}
function cancelCoverLoads() {
  coverObserver?.disconnect();
  coverBatch?.controller.abort();
  for (const timer of coverBatch?.timers || []) clearTimeout(timer);
  for (const url of coverBatch?.objectUrls || []) URL.revokeObjectURL(url);
  coverBatch = null;
}
function applyCoverBlob(target, blob, batch = coverBatch) {
  if (!target || !target.isConnected) return;
  const previous = target.dataset.coverObjectUrl;
  if (previous) {
    URL.revokeObjectURL(previous);
    batch?.objectUrls?.delete(previous);
  }
  const url = URL.createObjectURL(blob);
  batch?.objectUrls?.add(url);
  target.dataset.coverObjectUrl = url;
  target.style.setProperty("--cover", `url('${url}')`);
  target.classList.remove("cover-unavailable");
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
      if (!status.pending) break;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    const response = await fetch(`/api/anime/${animeId}/image?v=${Date.now()}`, {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`cover ${response.status}`);
    applyCoverBlob(target, await response.blob());
    target.dataset.coverAttempt = "0";
  } catch (error) {
    target.classList.add("cover-unavailable");
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
  target.dataset.coverQueued = "1";
  priority ? batch.queue.unshift(target) : batch.queue.push(target);
  pumpCoverLoads(batch);
}
function pumpCoverLoads(batch) {
  while (coverBatch === batch && batch.active < 12 && batch.queue.length) {
    const target = batch.queue.shift();
    batch.active += 1;
    const suffix = coverVersion ? `?v=${coverVersion}` : "";
    fetch(`/api/anime/${target.dataset.cover}/image${suffix}`, {
      signal: batch.controller.signal,
      credentials: "same-origin",
      cache: "no-cache",
    }).then(async (response) => {
      if (!response.ok) throw new Error(`cover ${response.status}`);
      return { blob: await response.blob(), status: response.headers.get("X-AnimeMachine-Image-Status") || "available" };
    }).then(({ blob, status }) => {
      if (coverBatch !== batch || !target.isConnected) return;
      applyCoverBlob(target, blob, batch);
      if (status === "queued") {
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
      if (error.name !== "AbortError") target.classList.add("cover-unavailable");
    }).finally(() => {
      batch.active -= 1;
      if (coverBatch === batch) pumpCoverLoads(batch);
    });
  }
}
function covers() {
  cancelCoverLoads();
  if (!imagesEnabled) return;
  const batch = { controller: new AbortController(), queue: [], active: 0, objectUrls: new Set(), timers: new Set() };
  coverBatch = batch;
  coverObserver = new IntersectionObserver(
    (es) =>
      es.forEach((e) => {
        if (e.isIntersecting) {
          queueCoverElement(e.target, batch);
          coverObserver.unobserve(e.target);
        }
      }),
    { rootMargin: "900px" },
  );
  [...results.querySelectorAll("[data-cover]")].forEach((target, index) => {
    if (index < 24) queueCoverElement(target, batch);
    else coverObserver.observe(target);
  });
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
  libraryHtml = (x, animeId) =>
    x.targets?.length
      ? x.targets
          .map(
            (y) => {
              const inspection = y.state === "absent" ? t("absentInspection") : inspectionLabel(y.inspectionMode),
                status = y.state === "external"
                  ? `${t("externalSource")} · ${t("readOnlyMapping")}`
                  : `${esc(humanCode(y.state))} · ${inspection}`,
                mappedPath = clientVisiblePath(y.path),
                count = y.fileCount != null
                  ? ` · ${y.fileCount} ${t("files")}`
                  : y.expectedFiles != null ? ` · ${y.observedFiles}/${y.expectedFiles} ${t("files")}` : "";
              const needsAudit = y.state === "existing" && ["not_inspected_preexisting", "catalog_state_only", "none"].includes(y.inspectionMode);
              const playable = y.state !== "absent" && Number(y.fileCount || y.observedFiles || 0) > 0,
                selectedSource = playbackSources.get(animeId) === y.path;
              return `<div class="library-resource"><div class="inventory selectable"><input type="radio" aria-label="${esc(y.path)}" name="library-source-${animeId}" value="${esc(y.path)}" ${playable ? "" : "disabled"} ${playable && selectedSource ? "checked" : ""}><span><b>${esc(y.path)}</b><div class="inventory-foot"><small>${status}${count}${needsAudit ? ` <button type="button" class="text-button audit-work" data-anime-id="${animeId}">${t("verifyWork")}</button>` : ""}</small><button type="button" class="text-button copy-library-path" data-copy-path="${esc(mappedPath || "")}" ${mappedPath ? "" : "disabled"}>${t("copyPath")}</button></div></span></div>${playable && y.subtitleApplicable !== false ? `<div class="subtitle-tools" data-subtitle-target="${esc(y.path)}"><button type="button" class="tool dark-tool search-subtitles">${t("searchSubtitles")}</button><select class="subtitle-candidates" disabled><option>${t("subtitleNotFound")}</option></select><button type="button" class="tool dark-tool apply-subtitle" disabled>${t("useSubtitle")}</button><small class="muted subtitle-state"></small></div>` : ""}</div>`;
            },
          )
          .join("")
      : "",
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
      ? (archive ? ["合卷", "Disc collection", "全巻"][li()] : ["合集", "Collection", "合集"][li()])
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
  eligibilityReasonText = (item) => {
    if (item?.eligible) return "";
    const reason = String(item?.eligibilityReason || "");
    if (reason === "source_class_disabled") return t("excludedSourceClass");
    if (reason === "resource_group_disabled")
      return item?.sourceFamily === "serial" ? t("excludedSerialProfile") : t("excludedResourceGroup");
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
        mergeable = ["episode", "volume"].includes(row.releaseUnit) && sequence?.length,
        key = mergeable
          ? [row.eligible, row.sourceFamily, row.sourceClass, row.resourceGroup, row.subtitle, row.resolution, row.scan, row.bitDepth, row.releaseUnit].join("|")
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
  torrentGroupHtml = (group, id) => {
    const y = group.top,
      storageDirectories = [...new Set(group.members.map((item) => item.storageDirectory).filter(Boolean))],
      storageDirectory = storageDirectories.filter((value) => value !== ".").join(" · "),
      resourceGroup = y.resourceGroup && !["unknown", "other"].includes(String(y.resourceGroup).toLowerCase())
        ? y.resourceGroup
        : "",
      sourceClass = y.sourceClass && !["unknown", "other"].includes(String(y.sourceClass).toLowerCase())
        ? sourceClassText(y.sourceClass)
        : "",
      resolution = resourceResolutionText(y.resolution, y.scan),
      files = group.members.reduce((sum, item) => sum + Number(item.fileCount || 0), 0),
      details = [resourceGroup, sourceClass, resolution, files ? `${fmt(files)} ${t("files")}` : ""]
        .filter(Boolean)
        .join(" · ");
    const exclusion = eligibilityReasonText(y);
    return `<label class="torrent ${y.eligible ? "" : "disabled"}"><input type="radio" name="resource-${id}" value="torrent:${esc(y.infoHash)}" ${y.eligible ? "" : "disabled"} ${torrentSelections.get(id) === y.infoHash ? "checked" : ""}><span class="torrent-lines"><b title="${esc(storageDirectory)}">${esc(storageDirectory)}</b><small class="torrent-subline" title="${esc(details)}${exclusion ? ` ${esc(exclusion)}` : ""}"><span>${esc(details)}</span>${exclusion ? `<span class="torrent-exclusion">${esc(exclusion)}</span>` : ""}</small></span></label>`;
  },
  aniResourceHtml = (y, id) => {
    const exclusion = eligibilityReasonText(y),
      resourceGroup = y.resourceGroup && !["unknown", "other"].includes(String(y.resourceGroup).toLowerCase())
        ? y.resourceGroup
        : "",
      sourceClass = y.sourceClass && !["unknown", "other"].includes(String(y.sourceClass).toLowerCase())
        ? sourceClassText(y.sourceClass)
        : "",
      details = [
        resourceGroup,
        sourceClass,
        resourceResolutionText(y.resolution),
        Number(y.itemCount || 0) > 0 ? `${fmt(y.itemCount)} ${t("files")}` : "",
      ].filter(Boolean).join(" · ");
    return `<label class="torrent ${y.eligible ? "" : "disabled"}"><input type="radio" name="resource-${id}" value="ani-rss:${esc(y.resourceId)}" ${y.eligible ? "" : "disabled"} ${resourceSelections.get(id) === y.resourceId ? "checked" : ""}><span class="torrent-lines"><b>Ani-RSS</b><small class="torrent-subline" title="${esc(details)}${exclusion ? ` ${esc(exclusion)}` : ""}"><span>${esc(details)}</span>${exclusion ? `<span class="torrent-exclusion">${esc(exclusion)}</span>` : ""}</small></span></label>`;
  };

function graphTitle(node) {
  return node.title_ja || preferred(node);
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
  popover.innerHTML = `<header><b>${esc(t(relationSubjectCategoryKey[category]))}</b><span>${esc(node.title_ja || "")}</span></header><ul>${subjects.map((item) => relatedSubjectLine(item, category)).join("")}</ul>`;
  popover.hidden = false;
  const rect = button.getBoundingClientRect(),
    box = popover.getBoundingClientRect(),
    left = Math.min(Math.max(12, rect.left), window.innerWidth - box.width - 12),
    below = rect.bottom + 8,
    top = below + box.height <= window.innerHeight - 12
      ? below
      : Math.max(12, rect.top - box.height - 8);
  popover.style.left = `${left}px`;
  popover.style.top = `${top}px`;
}

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
          status = node.library_state || "absent";
        return `<article class="relation-node ${node.strict_member ? "series" : "context"} ${node.id === graph.rootAnimeId ? "root" : ""}" data-graph-node="${node.id}" style="left:${point.x + shiftX}px;top:${point.y + shiftY}px"><div class="relation-node-head"><input type="checkbox" data-graph-select="${node.id}" ${selected.has(node.id) ? "checked" : ""} ${canSelect ? "" : "disabled"} aria-label="${esc(canSelect ? t("graphSelected") : t("graphUnavailableSelect"))}" title="${esc(canSelect ? t("graphSelected") : t("graphUnavailableSelect"))}"><time title="${esc(localMonth(node.start_month))}">${esc(relationMonth(node.start_month))}</time><span class="relation-subject-chips">${relationSubjectChips(node)}</span></div><button type="button" data-graph-detail="${node.id}">${esc(graphTitle(node))}</button><div class="relation-node-foot"><small>${esc(label("media", node.media_code))} · ${esc(humanCode(status))}${node.strict_member ? "" : ` · ${esc(t("graphContext"))}`}</small>${node.selection_warning ? `<span class="relation-risk" title="${esc(t("graphExistingWarning"))}">!</span>` : ""}</div></article>`;
      })
      .join("");
  $("relationGraph").innerHTML =
    `<header class="relation-heading"><div><p class="eyebrow">AnimeMachine · Automated Anime Library</p><h2>${esc(graph.seriesTitle || graphTitle(nodesById.get(graph.rootAnimeId)))} · ${t("relationGraph")}</h2><p>${t("relationHint")}</p>${graph.contextTruncated ? `<p class="relation-context-note">${t("graphContextTruncated")}</p>` : ""}<div class="relation-legend">${legend}</div></div><div class="relation-heading-actions"><b>${esc(t("relatedWorksCount").replace("{count}", fmt(graph.strictMemberCount)))}</b><div class="relation-export-actions"><button type="button" class="relation-fullscreen" data-relation-export="png">${t("exportPng")}</button><button type="button" class="relation-fullscreen" data-relation-export="svg">${t("exportSvg")}</button><button type="button" class="relation-fullscreen" data-relation-fullscreen>${t("fullscreenGraph")}</button></div></div></header><div class="relation-scroll"><div class="relation-stage" style="width:${width}px;height:${height}px"><svg class="relation-lines" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true"><defs><marker id="relation-arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z"/></marker></defs><g transform="translate(${shiftX} ${shiftY})">${edges}</g></svg>${edgeLabels}${nodes}</div></div>`;
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
  relationDialog.showModal();
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
    latin = (text.match(/[A-Za-z]/g) || []).length,
    han = (text.match(/[\u3400-\u9fff]/g) || []).length;
  if (kana >= 4 && kana >= latin / 2) return "ja";
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

function playbackHtml(state, localMediaAvailable = false) {
  if (!state?.available)
    return `<section class="playback-panel unavailable"><p class="muted">${t("noPlayableMedia")}</p></section>`;
  const platform = navigator.userAgent || "",
    remoteAniRss = state.sourceType === "ani-rss",
    directState = state.aniRssMediaPathState || "not_applicable",
    directUnavailable = remoteAniRss && directState !== "available"
      && !localMediaAvailable && !capabilities.qbtCredentialConfigured,
    episodeOptions = (state.items || []).map((item) => `<option value="${Number(item.index)}">${esc(item.title)}</option>`).join(""),
    pathHint = remoteAniRss
      ? `<small class="playback-path-state ${directState === "available" ? "available" : "muted"}">${t(directState === "available" ? "aniRssPathAvailable" : directState === "unconfigured" ? "aniRssPathUnconfigured" : "aniRssPathUnavailable")}</small>`
      : "",
    disabledDirect = directUnavailable ? " disabled" : "";
  return `<section class="playback-panel"><div class="playback-scope"><select id="playbackEpisode" aria-label="${t("playbackStartFile")}">${episodeOptions}</select></div><div class="playback-actions"><button type="button" class="tool dark-tool player-action" data-player="copy">${t("copyPlaylist")}</button><span class="playback-divider" aria-hidden="true">/</span><button type="button" class="tool dark-tool player-action" data-player="system">${t("systemPlayer")}</button><button type="button" class="tool dark-tool player-action" data-player="vlc"${disabledDirect}>${playerIcon("vlc")}<span>${t("openVlc")}</span></button>${/Windows/i.test(platform) ? `<button type="button" class="tool dark-tool player-action" data-player="potplayer"${disabledDirect}>${playerIcon("potplayer")}<span>${t("openPotPlayer")}</span></button>` : ""}${/(Macintosh|Mac OS X)/i.test(platform) ? `<button type="button" class="tool dark-tool player-action" data-player="iina">${playerIcon("iina")}<span>${t("openIina")}</span></button>` : ""}</div>${pathHint}</section>`;
}

function playerIcon(kind) {
  if (kind === "vlc")
    return `<svg class="player-logo" viewBox="0 0 24 24" aria-hidden="true"><path fill="#f47b20" d="M12 2 6.2 18h11.6L12 2Z"/><path fill="#fff" d="m9.3 9 1-2.7h3.4l1 2.7H9.3Zm-1.6 4.3 1-2.7h6.6l1 2.7H7.7Z"/><path fill="#d35c0b" d="M4.8 18h14.4v3H4.8z"/></svg>`;
  if (kind === "potplayer")
    return `<svg class="player-logo" viewBox="0 0 24 24" aria-hidden="true"><defs><linearGradient id="potplayer-gradient" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#c56cff"/><stop offset="1" stop-color="#6557d9"/></linearGradient></defs><circle cx="12" cy="12" r="10" fill="url(#potplayer-gradient)"/><path fill="#fff" d="m10 7 7 5-7 5V7Z"/></svg>`;
  if (kind === "iina")
    return `<svg class="player-logo" viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="4" fill="#56606c"/><path fill="#fff" d="m10 8 6 4-6 4V8Z"/></svg>`;
  return "";
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

async function copyPlainText(value) {
  const text = String(value || "");
  if (!text) throw new Error(t("copyPlaylist"));
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (_error) {
      // Clipboard access is commonly rejected on non-secure LAN origins.
      // Continue with the selection-based fallback instead of failing.
    }
  }
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.append(field);
  field.select();
  const copied = document.execCommand("copy");
  field.remove();
  if (!copied) throw new Error(t("copyPlaylist"));
}

async function handoffPlayback(kind, animeId, button) {
  const selectedEpisode = Math.max(1, Number($("playbackEpisode")?.value || 1)),
    payload = {
      player: kind,
      mode: "playlist",
      source: playbackSources.get(animeId) || "",
      start: selectedEpisode,
    },
    handoff = await api(`/api/anime/${animeId}/playback/handoff`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  if (kind === "copy") {
    await copyPlainText(handoff.playlistUrl || handoff.url);
    const previous = button.textContent;
    button.textContent = t("copiedPlaylist");
    setTimeout(() => { button.textContent = previous; }, 1800);
    return;
  }
  if (kind === "system") {
    const anchor = document.createElement("a"), url = new URL(handoff.playlistUrl || handoff.url);
    url.searchParams.set("download", "1");
    anchor.href = url.href;
    anchor.download = `AnimeMachine-${animeId}.m3u`;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    return;
  }
  if (!handoff.protocolUrl) throw new Error("Playback handoff is unavailable");
  location.href = handoff.protocolUrl;
}

async function showDetail(id) {
  const x = await api(`/api/anime/${id}?language=${encodeURIComponent(language)}`),
    playableTargets = (x.library?.targets || []).filter((target) => target.state !== "absent" && Number(target.fileCount || target.observedFiles || 0) > 0),
    aniSubscriptions = x.ani_rss?.subscriptions || [];
  if (!playbackSources.has(id)) {
    if (playableTargets.length) {
      const preferredTarget = playableTargets.find((target) => target.state === x.library?.preferredOrigin) || playableTargets[0];
      playbackSources.set(id, preferredTarget.path);
    } else if (aniSubscriptions.length) {
      const remote = aniSubscriptions.find((item) => item.enabled) || aniSubscriptions[0];
      playbackSources.set(id, `ani-rss:${remote.remoteId}`);
    }
  }
  const selectedPlaybackSource = playbackSources.get(id) || "",
    playbackState = await api(`/api/anime/${id}/playback${selectedPlaybackSource ? `?source=${encodeURIComponent(selectedPlaybackSource)}` : ""}`).catch(() => ({ available: false, count: 0, items: [] })),
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
          return `<li>${relationText ? `${esc(relationText)} · ` : ""}${esc(y.related_title)} · <a class="source" href="https://bgm.tv/subject/${Number(y.related_bgm_id)}" target="_blank" rel="noreferrer">BGM #${Number(y.related_bgm_id)}</a></li>`;
        })
        .join("") || "<li>—</li>",
    detailRelationButton = Number(x.series_member_count || 1) > 1
      ? `<button id="detailRelations" class="tool dark-tool detail-action" type="button"><span aria-hidden="true">⑂</span>${t("viewRelations")} · ${fmt(x.series_member_count)}</button>`
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
    episodes = Number(x.episode_count || 0) > 0 ? fmt(x.episode_count) : "",
    factHtml = (className, labelText, value) => `<div class="fact ${className}"><b>${esc(labelText)}</b><span class="fact-value" title="${esc(value)}">${esc(value)}</span></div>`,
    summary = localizedSummary(x),
    aniResources = x.ani_rss?.resources || [],
    managedByAniRss = aniSubscriptions.length
      ? `<div class="ani-rss-library">${aniSubscriptions.map((item) => { const value = `ani-rss:${item.remoteId}`, episodes = Number(item.currentEpisode || 0), totalEpisodes = Number(item.totalEpisode || 0), episodeText = episodes ? ` · ${fmt(episodes)}${totalEpisodes ? `/${fmt(totalEpisodes)}` : ""}` : "", deleteAction = episodes > 0 ? `<button type="button" class="text-button ani-rss-delete" data-ani-rss-delete="${esc(item.remoteId)}">${t("aniRssDelete")}</button>` : ""; return `<div class="inventory selectable ani-rss-inventory"><input type="radio" aria-label="${esc(item.title)}" name="library-source-${id}" value="${esc(value)}" ${playbackSources.get(id) === value ? "checked" : ""}><span><b>${t("remotePlaybackSource")} · ${esc(item.title)}</b><small>${t("aniRssManaged")}${episodeText}</small></span>${deleteAction}</div>`; }).join("")}</div>`
      : "",
    resourceList = `${torrentGroups(x.torrents).map((group) => torrentGroupHtml(group, x.id)).join("")}${aniResources.map((y) => aniResourceHtml(y, x.id)).join("")}`,
    hasEligibleResource = (x.torrents || []).some((y) => y.eligible) || aniResources.some((y) => y.eligible);
  $("detail").innerHTML =
    `${imagesEnabled ? `<div class="detail-cover" data-cover="${x.id}"><button type="button" class="cover-reload" data-cover-reload="${x.id}">${t("reloadImage")}</button></div>` : ""}<span class="date">${esc(localMonth(x.start_month))} · ${esc(label("media", x.media_code))}</span><h2>${esc(preferred(x))}</h2>${detailSubtitleRow}<div class="detail-grid">${factHtml("country-fact", t("country"), countries)}${factHtml("source-type-fact", t("sourceType"), sourceType)}${factHtml("original-name-fact", t("originalName"), originalName)}${factHtml("original-author-fact", t("originalAuthor"), originalAuthors)}${factHtml("director-fact", t("director"), directors)}${factHtml("series-composition-fact", t("seriesComposition"), seriesComposition)}${factHtml("character-design-fact", t("characterDesign"), characterDesign)}${factHtml("music-fact", t("music"), music)}${factHtml("studio-fact", t("studio"), studios)}${factHtml("episodes-fact", t("episodes"), episodes)}${factHtml("tags-fact", t("tag"), displayTags)}</div><div class="detail-section-heading library-heading"><h3>${t("library")}</h3></div>${libraryHtml(x.library, x.id)}${managedByAniRss}<div class="detail-section-heading playback-title"><h3>${t("playback")}</h3></div>${playbackHtml(playbackState, playableTargets.length > 0)}<h3>${t("torrents")}</h3><div class="torrent-list">${resourceList || `<p class="muted">${t("noTorrent")}</p>`}</div><div class="resource-search-actions"><button type="button" id="searchWorkTorrents" class="tool dark-tool">${t("searchPoolNow")}</button><button type="button" id="searchAniRss" class="tool dark-tool">${t("searchAniRss")}</button><button type="button" id="startWorkDownload" class="primary" ${hasEligibleResource ? "" : "disabled"}>${t("previewPlan")}</button><small id="searchWorkState" class="muted"></small></div><h3>${t("titles")}</h3><ul class="list">${titles}</ul><h3>${t("cast")}</h3><ul class="list">${cast}</ul>${others.length ? `<details><summary>${t("allCast")}</summary><ul class="list">${allCast}</ul></details>` : ""}<h3>${t("relations")}</h3><ul class="list">${relations}</ul><h3>${t("summary")}</h3><p class="summary-text">${esc(summary || "—").replace(/\n/g, "<br>")}</p><p class="source">Bangumi Archive · <a href="${esc(x.source_url)}" target="_blank" rel="noreferrer">BGM #${x.bgm_id}</a></p>`;
  bindCoverReloadButtons($("detail"));
  if (imagesEnabled) queueCoverElement($("detail").querySelector("[data-cover]"), coverBatch, true);
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
          selected.add(id);
          updateSelection();
          render();
        }),
    );
  $("detail").querySelectorAll(".audit-work").forEach((button) => {
    button.onclick = async () => {
      button.disabled = true;
      button.textContent = t("verifyRunning");
      await api(`/api/anime/${id}/library/audit`, { method: "POST" });
      setTimeout(() => showDetail(id), 900);
    };
  });
  $("detail").querySelectorAll(`input[name="library-source-${id}"]`).forEach((radio) => {
    radio.onchange = () => {
      playbackSources.set(id, radio.value);
      showDetail(id).catch((error) => alert(error.message));
    };
  });
  $("detail").querySelectorAll("[data-ani-rss-delete]").forEach((button) => {
    button.onclick = async () => {
      if (!window.confirm(t("aniRssDeleteConfirm"))) return;
      button.disabled = true;
      try {
        const result = await api(`/api/ani-rss/subscriptions/${encodeURIComponent(button.dataset.aniRssDelete)}/delete`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ deleteFiles: false }),
        });
        if (!result.deleted) throw new Error(t("aniRssDeleteFailed"));
        if (playbackSources.get(id) === `ani-rss:${button.dataset.aniRssDelete}`) playbackSources.delete(id);
        await showDetail(id);
      } catch (error) { alert(error.message); button.disabled = false; }
    };
  });
  $("detail").querySelectorAll(".subtitle-tools").forEach((tool) => {
    const searchButton = tool.querySelector(".search-subtitles"),
      applyButton = tool.querySelector(".apply-subtitle"),
      select = tool.querySelector(".subtitle-candidates"),
      state = tool.querySelector(".subtitle-state"),
      target = tool.dataset.subtitleTarget;
    let candidates = [];
    searchButton.onclick = async () => {
      searchButton.disabled = true;
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
      await api(`/api/anime/${id}/torrents/search`, { method: "POST" });
      const poll = async () => {
        const job = await api(`/api/anime/${id}/torrents/search`);
        if (job.state === "running") { setTimeout(poll, 900); return; }
        if (job.state === "failed") { status.textContent = job.error || t("failed"); button.disabled = false; return; }
        const updated = await api(`/api/anime/${id}?language=${encodeURIComponent(language)}`);
        status.textContent = updated.torrents?.length ? t("searchPoolDone") : t("searchPoolNone");
        await search();
        setTimeout(() => showDetail(id), 650);
      };
      setTimeout(poll, 700);
    };
  if ($("searchAniRss"))
    $("searchAniRss").onclick = async () => {
      const button = $("searchAniRss"), status = $("searchWorkState");
      button.disabled = true; status.textContent = t("searchPoolRunning");
      try {
        await api(`/api/anime/${id}/ani-rss/search`, { method: "POST" });
        const poll = async () => {
          try {
            const job = await api(`/api/anime/${id}/ani-rss/search`);
            if (job.state === "running") { setTimeout(poll, 900); return; }
            if (job.state === "failed") { status.textContent = job.error || t("failed"); button.disabled = false; return; }
            status.textContent = job.found ? t("searchPoolDone") : t("searchPoolNone");
            await search();
            setTimeout(() => showDetail(id), 650);
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
      createPlan();
    };
  $("detail").querySelectorAll(".copy-library-path").forEach((button) => {
    button.onclick = async () => {
      await copyPlainText(button.dataset.copyPath);
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
  $("detail").querySelectorAll("[data-player]").forEach((button) => {
    button.onclick = () => handoffPlayback(button.dataset.player, id, button).catch((error) => alert(error.message));
  });
  if (!detailDialog.open) detailDialog.showModal();
  requestAnimationFrame(() => {
    if (detailDialog.scrollHeight <= detailDialog.clientHeight) return;
    const cover = $("detail").querySelector(".detail-cover");
    if (!cover) { detailDialog.scrollTop = 0; return; }
    const loaded = Boolean(cover.dataset.coverObjectUrl);
    detailDialog.scrollTop = loaded ? 0 : Math.min(cover.offsetTop + cover.offsetHeight, detailDialog.scrollHeight - detailDialog.clientHeight);
  });
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
      $("plan").innerHTML = `<h2>${t("plan")}</h2><p class="notice">${t("planBuilding")}</p><p><b>${fmt(p.workCount)}</b> ${t("selectedWorks")}</p>`;
      planDialog.showModal();
      while (p.state === "building") {
        await new Promise((resolve) => setTimeout(resolve, 750));
        p = await api(`/api/plans/${planState.id}`);
      }
      if (p.state === "error") throw new Error(p.error || "plan build failed");
    }
    const assessments = (p.assessments || [])
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
      routeButtons = `<div class="plan-route-actions"><button type="button" class="tool ${routingMode === "ani-rss" ? "active" : ""}" data-plan-route="ani-rss">${t("planUseAniRss")}</button><button type="button" class="tool ${routingMode === "torrent" ? "active" : ""}" data-plan-route="torrent">${t("planUseTorrent")}</button><button type="button" class="tool ${routingMode === "default" ? "active" : ""}" data-plan-route="default">${t("planRestoreDefault")}</button></div>`,
      stoppedNotice = (p.aniRssJobs || []).length ? "" : `<p class="notice">${t("planStopped")}</p>`,
      submitButton = capabilities.submissionEnabled
        ? `<button id="submitStoppedPlan" class="primary" ${p.taskCount ? "" : "disabled"}>${t("submitStopped")}</button>`
        : `<p class="muted">${t("submissionDisabled")}</p>`;
    $("plan").innerHTML =
      `<h2>${t("plan")}</h2>${stoppedNotice}<div class="plan-summary-row"><p><b>${p.taskCount}</b> ${t("tasks")} · ${fmt(p.totalBytes)} bytes</p>${routeButtons}</div>${p.taskCount ? "" : `<p class="notice safe">${t("planNoDownload")}</p>`}${assessments}${jobs}${aniRssJobs}${skippedJobs}${submitButton}`;
    $("plan").querySelectorAll("[data-plan-route]").forEach((button) => {
      button.onclick = () => createPlan(button.dataset.planRoute, baseRequest);
    });
    if ($("submitStoppedPlan") && p.taskCount)
      $("submitStoppedPlan").onclick = async () => {
        const button = $("submitStoppedPlan");
        button.disabled = true;
        button.textContent = t("submitting");
        try {
          await api(`/api/plans/${planState.id}/submit`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ confirmStopped: true }),
          });
          button.textContent = t("submitted");
          button.insertAdjacentHTML("beforebegin", `<p class="notice safe">${t("submitAccepted")}</p>`);
        } catch (error) {
          button.disabled = false;
          button.textContent = t("submitStopped");
          alert(`${t("queryFailed")}: ${friendlyPlanError(error)}`);
        }
      };
    if (!planDialog.open) planDialog.showModal();
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
  [
    "contentClasses",
    "allowedResolutions",
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
  const enabledByLanguage = serial.enabledByLanguage || {};
  const selectedLanguage = serial.language === "auto" || !serial.language
    ? (language.startsWith("zh") ? "zh" : language.startsWith("ja") ? "ja" : "en")
    : serial.language;
  [["zh","serialGroupsZh"],["en","serialGroupsEn"],["ja","serialGroupsJa"]].forEach(([lang,id]) => {
    const explicit = Object.hasOwn(enabledByLanguage, lang) ? new Set(enabledByLanguage[lang]) : null;
    setCheckList(id, (groupCatalog.serialProfiles?.[lang] || []).filter(x => !x.wildcard).map(x => ({...x,name:x.displayName,enabled:explicit ? explicit.has(x.id) : lang === selectedLanguage})));
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
  $("minimumFree").value = config.storageGuard.minimumFreeTiB ?? 0.1;
  $("onDemandHash").checked =
    config.differentialPlanning?.samePathSizePolicy === "hash_and_skip";
}
function saveOperationalSettings() {
  config.components.discovery.pollMinutes = Math.max(
    5,
    +$("pollMinutes").value || 30,
  );
  config.storageGuard.minimumFreeTiB = Math.max(
    0,
    +$("minimumFree").value || 0,
  );
  config.differentialPlanning = config.differentialPlanning || {};
  config.differentialPlanning.samePathSizePolicy = $("onDemandHash").checked
    ? "hash_and_skip"
    : "size_and_skip";
}
$("settingsForm").addEventListener("submit", saveOperationalSettings, true);
function openSettings() {
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
  $("playbackEnabled").checked = playback.enabled !== false;
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
  refreshArchiveStatus();
  loadLogs();
  $("settingsDialog").showModal();
}
async function saveSettings(e) {
  e.preventDefault();
  const p = config.torrentPolicy,
    c = enabled("contentClasses"),
    r = enabled("allowedResolutions");
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
  p.serialSubtitle = p.serialSubtitle || {};
  p.serialSubtitle.language = $("serialSubtitleLanguage").value;
  p.serialSubtitle.enabledByLanguage = {};
  [["zh","serialGroupsZh"],["en","serialGroupsEn"],["ja","serialGroupsJa"]].forEach(([lang,id]) => {
    p.serialSubtitle.enabledByLanguage[lang] = [...$(id).querySelectorAll("li")].filter(x => x.querySelector("input").checked).map(x => x.dataset.value);
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
  config.download.defaultStartMode = "stopped";
  delete config.download.allowExplicitAutoStart;
  delete config.download.autoStartMaximumTasksPerBatch;
  delete config.storageGuard.maximumQueuedTiB;
  delete config.storageGuard.maximumDailyAddTiB;
  config.components.downloadClient.submissionEnabled = $("submissionAllowed").checked;
  config.components.downloadClient.endpoint = $("qbtEndpoint").value.trim();
  await configureQbtCredential();
  config.components.aniRss = {
    ...(config.components.aniRss || {}),
    endpoint: $("aniRssEndpoint").value.trim() || "http://127.0.0.1:7789",
    mode: $("aniRssMode").value,
    mediaPath: $("aniRssMediaPath").value.trim(),
    syncMinutes: Math.max(5, +$("aniRssSyncMinutes").value || 30),
  };
  await configureAniRssCredential();
  config.deployment.torrentPoolRoot = $("torrentPoolPath").value.trim();
  config.deployment.libraryUncRoot = $("libraryPath").value.trim();
  config.deployment.qbtLibraryRoot = $("qbtLibraryPath").value.trim();
  const externalSource = {
    id: "external-read-only",
    kind: $("externalReadOnlyKind").value,
    enabled: $("externalReadOnlyEnabled").checked,
    path: $("externalReadOnlyPath").value.trim() || "/External",
    readOnly: true,
    scanMinutes: Math.max(
    5,
    +$("externalScanMinutes").value || 60,
    ),
  };
  config.externalLibraries = [externalSource];
  const lines = (id) => $(id).value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  config.subtitles = config.subtitles || {};
  config.subtitles.enabled = $("subtitlesEnabled").checked;
  const subtitleProviderMap = Object.fromEntries((config.subtitles.providers || []).map((item) => [item.id, item]));
  config.subtitles.providers = [
    { ...(subtitleProviderMap.assrt || {}), id: "assrt", name: "ASSRT", enabled: true, apiKeyEnv: "ASSRT_API_TOKEN", endpoints: lines("assrtEndpoints") },
    { ...(subtitleProviderMap.opensubtitles || {}), id: "opensubtitles", name: "OpenSubtitles", enabled: true, apiKeyEnv: "OPEN_SUBTITLES_API_KEY", endpoints: [$("openSubtitlesEndpoint").value.trim()].filter(Boolean) },
  ];
  const subtitleCredentials = { assrt: $("assrtToken").value.trim(), opensubtitles: $("openSubtitlesKey").value.trim() };
  if (subtitleCredentials.assrt || subtitleCredentials.opensubtitles)
    await api("/api/connections/subtitles/credentials", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(subtitleCredentials) });
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
  config.playback = {
    enabled: $("playbackEnabled").checked,
    preferDirectPaths: $("preferDirectPaths").checked,
    publicBaseUrl: $("playbackPublicUrl").value.trim(),
    playlistIdleSeconds: playbackIdle,
    playlistMaximumSeconds: Math.max(playbackIdle, Math.min(2592000, +$("playlistMaximum").value || 604800)),
    directPathMappings,
  };
  config.metadata.network = config.metadata.network || {};
  config.metadata.network.archiveManifestEndpoints = lines("archiveManifestEndpoints");
  config.metadata.network.archiveAssetProxyTemplates = lines("archiveAssetProxyTemplates");
  config.metadata.network.bangumiApiEndpoints = lines("bangumiApiEndpoints");
  if (!config.metadata.network.archiveManifestEndpoints.length || !config.metadata.network.bangumiApiEndpoints.length)
    throw new Error(t("metadataEndpointsRequired"));
  await api("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  capabilities = await api("/api/capabilities");
  $("saveState").textContent = t("saved");
  setTimeout(() => ($("saveState").textContent = ""), 1600);
  page = 0;
  search();
}
function archiveText(s) {
  return s.state === "failed"
    ? `${t("archiveFailed")}: ${s.error || ""}`
    : s.state === "unchanged"
      ? t("archiveUnchanged")
      : s.state === "complete"
        ? `${t("archiveComplete")}+${fmt(s.addedWorks || 0)}`
        : ["checking", "building", "merging"].includes(s.state)
          ? t("archiveUpdating")
          : "";
}
async function refreshArchiveStatus() {
  const s = await api("/api/archive/update");
  $("archiveStatus").textContent = archiveText(s);
  $("updateArchive").disabled = ["checking", "building", "merging"].includes(
    s.state,
  );
  return s;
}
async function startArchiveUpdate() {
  await api("/api/archive/update", { method: "POST" });
  const poll = setInterval(async () => {
    const s = await refreshArchiveStatus();
    if (!["checking", "building", "merging"].includes(s.state)) {
      clearInterval(poll);
      if (s.state === "complete") location.reload();
    }
  }, 3000);
}
async function importArchive(file) {
  if (!file) return;
  $("archiveStatus").textContent = t("archiveImporting");
  $("importArchive").disabled = true;
  try {
    const headers = { "Content-Type": "application/octet-stream", "X-Archive-Name": encodeURIComponent(file.name) };
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
    const response = await fetch("/api/archive/import", { method: "POST", headers, body: file, credentials: "same-origin" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    await startArchiveUpdate();
  } finally {
    $("importArchive").disabled = false;
    $("archiveFile").value = "";
  }
}
async function runMaintenance(kind) {
  if (kind !== "metadata") throw new Error("unsupported maintenance operation");
  await api("/api/metadata/repair", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const poll = setInterval(async () => {
    try {
      const status = (await api("/api/maintenance/status")).metadata;
      $("maintenanceStatus").textContent = `${t("repairMetadata")} · ${fmt(status.repaired)}/${fmt(status.processed)}`;
      if (status.state === "running") return;
      clearInterval(poll);
      $("repairMetadata").disabled = false;
    } catch (error) {
      clearInterval(poll);
      $("repairMetadata").disabled = false;
      $("maintenanceStatus").textContent = error.message;
    }
  }, 1500);
  $("repairMetadata").disabled = true;
}
async function testConnection(kind) {
  const isAniRss = kind === "ani-rss",
    endpoint = $(isAniRss ? "aniRssEndpoint" : "qbtEndpoint").value,
    state = $(isAniRss ? "aniRssState" : "qbtState");
  state.textContent = "…";
  try {
    if (isAniRss) await configureAniRssCredential();
    else await configureQbtCredential();
    const r = await api("/api/connections/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, endpoint }),
    });
    state.textContent = r.authenticated
      ? `${t("connectionOk")}${r.version ? ` · ${r.version}` : ""}`
      : r.reachable
        ? t("authRequired")
        : t("connectionFailed");
  } catch (e) {
    state.textContent = `${t("connectionFailed")}: ${e.message}`;
  }
}
async function configureAniRssCredential() {
  const field = $("aniRssApiKey"), key = field?.value.trim();
  if (!key) return false;
  const result = await api("/api/connections/ani-rss/credential", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ apiKey: key }),
  });
  field.value = "";
  capabilities.aniRssCredentialConfigured = Boolean(result.configured);
  $("aniRssCredentialState").textContent = t("aniRssCredentialConfigured");
  return true;
}
async function configureQbtCredential() {
  const field = $("qbtApiKey"), key = field?.value.trim();
  if (!key) return false;
  const result = await api("/api/connections/qbittorrent/credential", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ apiKey: key }),
  });
  field.value = "";
  capabilities.qbtCredentialConfigured = Boolean(result.configured);
  $("qbtCredentialState").textContent = t("qbtCredentialConfigured");
  return true;
}
async function loadLogs() {
  try {
    const r = await api("/api/logs");
    $("logList").innerHTML = r.items.length
      ? r.items
          .map(
            (x) =>
              `<div class="log ${esc(x.level.toLowerCase())}"><time>${esc(new Intl.DateTimeFormat(localeForLanguage(), { dateStyle: "short", timeStyle: "medium" }).format(new Date(x.at)))}</time><b>${esc(x.level)}</b><span>${esc(x.message)}</span><small>${esc(x.details)}</small></div>`,
          )
          .join("")
      : `<p class="muted">${t("noLogs")}</p>`;
  } catch (e) {
    $("logList").innerHTML = `<p class="error">${esc(e.message)}</p>`;
  }
}
async function loadHistory() {
  try {
    const response = await api("/api/history?limit=200");
    $("historyList").innerHTML = response.items.length
      ? response.items.map((item) => {
          const destination = item.operation === "remove" ? item.backupPath : item.targetPath;
          const operation = item.state === "restored" ? t("historyRestored") : t({ move: "historyMove", rename: "historyRename", remove: "historyRemove" }[item.operation] || "historyMove");
          const restore = item.operation === "remove" && item.state === "applied"
            ? `<button type="button" class="text-button restore-history" data-event="${item.eventId}">${t("restoreHistory")}</button>` : "";
          return `<div class="log"><time>${esc(new Intl.DateTimeFormat(localeForLanguage(), { dateStyle: "short", timeStyle: "medium" }).format(new Date(item.createdAt)))}</time><b>${esc(operation)}</b><span>${esc(item.sourcePath || "—")}</span><small>→ ${esc(destination || "—")} · ${esc(fmt(item.bytes || 0))} B</small>${restore}</div>`;
        }).join("")
      : `<p class="muted">${t("noHistory")}</p>`;
    document.querySelectorAll(".restore-history").forEach((button) => button.onclick = async () => {
      await api(`/api/history/${button.dataset.event}/restore`, { method: "POST" });
      loadHistory();
    });
  } catch (error) {
    $("historyList").innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}
async function loadWatches() {
  try {
    const r = await api("/api/watches");
    $("watchList").innerHTML = r.items.length
      ? r.items.map((x) => `<div class="log"><b>${esc(x.title)}</b><span>${esc(sourceClassText(x.sourceClass))} · ${esc(x.resourceGroup || t("other"))} · ${esc(x.resolution ? `${x.resolution}p` : t("other"))} · ${esc(x.subtitle || t("other"))}</span><small>${esc(x.releaseUnit)} #${x.lastSequence} · ${x.pendingCount}</small><button type="button" class="text-button remove-watch" data-watch="${x.watchId}">${t("removeWatch")}</button></div>`).join("")
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
  if (!config.ui) return;
  Object.assign(config.ui, {
    language,
    catalogView: view,
    imagesEnabled,
    pageSize: pageSize === "all" ? "all" : +pageSize,
    sort,
    sortDirection: direction,
  });
  await api("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}
function archiveSummary(s) {
  catalogStats = s;
  let d = new Date(s.archive_created_at),
    m = String(s.archive_name || "").match(/(\d{4})-(\d{2})-(\d{2})/);
  if (Number.isNaN(d.valueOf()) && m)
    d = new Date(`${m[1]}-${m[2]}-${m[3]}T00:00:00Z`);
  const sync = s.sync || {}, phaseNames = {
      starting: ["准备", "preparing", "準備"], pool_discovery: ["发现种子", "discovering torrents", "Torrent 検出"],
      pool_parse: ["解析种子", "parsing torrents", "Torrent 解析"], pool_reconcile: ["增量收口", "reconciling changes", "差分整理"],
      torrent_mapping: ["关联作品", "mapping works", "作品対応"], partial_ready: ["部分结果可用", "partial results ready", "一部利用可能"],
      external_library: ["外部库", "external library", "外部ライブラリ"], runtime_overlay: ["更新索引", "updating index", "索引更新"],
      library_audit: ["核验收藏库", "auditing library", "ライブラリ検査"], ready: ["完成", "complete", "完了"],
    }, phase = phaseNames[sync.phase]?.[li()] || sync.phase || "";
  const done = Number(sync.stats?.parsed || 0) + Number(sync.stats?.unchanged || 0), total = Number(sync.stats?.discovered || 0);
  const progress = sync.state === "running" && phase ? ` · ${t("backgroundSync")}: ${phase}${total ? ` ${fmt(done)}/${fmt(total)}` : ""}` : "";
  return `${t("archiveReady")}: ${fmt(s.record_count)} ${t("works").trim()} · ${Number.isNaN(d.valueOf()) ? "—" : new Intl.DateTimeFormat(localeForLanguage(), { year: "numeric", month: "long", day: "numeric", timeZone: "UTC" }).format(d)} · ${fmt(s.runtime?.torrents || 0)} ${t("torrentAssets")}${progress}`;
}
function renderScanProgress(s) {
  const sync = s?.sync || {}, stats = sync.stats || sync.details || {}, phaseNames = {
      archive_resolve: ["检查动画底库", "Checking catalog archive", "カタログアーカイブ確認"],
      archive_download: ["下载动画底库", "Downloading catalog archive", "カタログアーカイブ取得"],
      archive_ready: ["校验动画底库", "Verifying catalog archive", "カタログアーカイブ検証"],
      catalog_parse: ["构建动画数据库", "Building anime catalog", "アニメカタログ構築"],
      catalog_ready: ["动画数据库可用", "Anime catalog ready", "アニメカタログ準備完了"],
      starting: ["准备扫描", "Preparing scan", "スキャン準備"],
      pool_discovery: ["发现种子", "Discovering torrents", "Torrent 検出"],
      pool_parse: ["解析种子", "Parsing torrents", "Torrent 解析"],
      pool_reconcile: ["增量收口", "Reconciling", "差分整理"],
      initial_torrent_mapping: ["初步关联作品", "Initial work mapping", "初期作品対応"],
      partial_ready: ["部分结果可用", "Partial results ready", "一部利用可能"],
      torrent_mapping: ["关联作品", "Mapping works", "作品対応"],
      path_reconciliation: ["校正本地路径", "Reconciling local paths", "ローカルパス照合"],
      torrent_search: ["搜索本地资源", "Searching local resources", "ローカル資源検索"],
      pool_unavailable: ["资源池不可用", "Torrent pool unavailable", "Torrent プール利用不可"],
      external_library: ["扫描外部库", "Scanning external library", "外部ライブラリ走査"],
      runtime_overlay: ["更新检索索引", "Updating search index", "検索索引更新"],
      library_audit: ["核验收藏库", "Verifying library", "ライブラリ確認"],
      ready: ["扫描完成", "Scan complete", "スキャン完了"],
    }, label = phaseNames[sync.phase]?.[li()] || sync.phase || t("scanIdle"),
    done = Number(stats.received || 0) || Number(stats.parsed || 0) + Number(stats.unchanged || 0) || Number(stats.processed || stats.examined || 0),
    total = Number(stats.discovered || stats.total || 0), bar = $("scanProgressBar"), box = $("scanProgress");
  box.classList.toggle("idle", sync.state !== "running");
  if (sync.state === "running" && total > 0) {
    const percentage = Math.min(99, Math.round(done * 100 / total));
    bar.value = done <= total ? percentage : 99;
    $("scanProgressText").textContent = `${label} · ${fmt(done)}/${fmt(total)}`;
  } else if (sync.state === "running") {
    bar.removeAttribute("value");
    $("scanProgressText").textContent = label;
  } else {
    bar.value = sync.state === "complete" ? 100 : 0;
    $("scanProgressText").textContent = sync.state === "complete" ? label : t("scanIdle");
  }
}
let syncSummaryTimer, lastCatalogRecordCount = null, lastArchiveName = null;
async function pollSyncSummary() {
  clearTimeout(syncSummaryTimer);
  try {
    const stats = await api("/api/stats");
    const count = Number(stats.record_count || 0), archiveName = String(stats.archive_name || "");
    if ((lastCatalogRecordCount === 0 && count > 0) ||
        (lastArchiveName === "bootstrap-pending" && archiveName !== "bootstrap-pending")) {
      window.location.reload();
      return;
    }
    lastCatalogRecordCount = count;
    lastArchiveName = archiveName;
    $("buildInfo").textContent = archiveSummary(stats);
    renderScanProgress(stats);
    syncSummaryTimer = setTimeout(pollSyncSummary, stats.sync?.state === "running" ? 1500 : 15000);
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
  $("era").value = "";
}
function eraDateRange(value) {
  if (/^\d{4}$/.test(value))
    return { from: `${value}-01`, to: `${value}-12`, reversible: true };
  if (/^\d{4}s$/.test(value)) {
    const first = +value.slice(0, 4);
    return {
      from: `${first}-01`,
      to: `${first + 9}-12`,
      reversible: true,
    };
  }
  if (value === "before1980")
    return { from: "", to: "1979-12", reversible: true };
  if (value === "future_or_unknown")
    return { from: "", to: "", reversible: false };
  return null;
}
function applyEraDateRange(value) {
  const range = eraDateRange(value);
  if (!range) return;
  $("start_from").value = range.from;
  $("start_to").value = range.to;
}
function syncEraFromDates() {
  const from = $("start_from").value,
    to = $("start_to").value,
    matching = (options.eras || []).find((value) => {
      const range = eraDateRange(value);
      return range?.reversible && range.from === from && range.to === to;
    });
  $("era").value = matching || "";
}
function setView(v) {
  view = v;
  results.className = `results ${v}`;
  $("cardsView").classList.toggle("active", v === "cards");
  $("tableView").classList.toggle("active", v === "table");
  render();
}
function applyRoleUi() {
  const admin = authSession?.role === "admin";
  $("settingsButton").hidden = !admin;
  $("authButton").hidden = !authSession?.authEnabled;
  if (authSession?.authEnabled) $("authButton").textContent = `${authSession.username} · ${t("logout")}`;
  document.querySelectorAll("[data-admin-only]").forEach((element) => (element.hidden = !admin));
}
function showLogin() {
  if (!$("loginDialog").open) $("loginDialog").showModal();
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
async function loadUsers() {
  if (authSession?.role !== "admin") return;
  const payload = await api("/api/auth/users");
  $("userList").innerHTML = payload.items.map((user) =>
    `<div class="log-item user-row"><b>${esc(user.username)}</b><span>${t(user.role === "admin" ? "administrator" : "normalUser")}</span><button type="button" class="text-button toggle-user" data-user-id="${user.id}" data-enabled="${user.enabled ? "1" : "0"}">${t(user.enabled ? (user.initialAdmin ? "disableInitialAdmin" : "disableUser") : "enableUser")}</button></div>`
  ).join("");
  $("userList").querySelectorAll(".toggle-user").forEach((button) => {
    button.onclick = async () => {
      await api(`/api/auth/users/${button.dataset.userId}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: button.dataset.enabled !== "1" }) });
      await loadUsers();
    };
  });
}
async function initialize() {
  try {
    if (!(await establishSession())) return;
    const [stats, o, c, cap, groups] = await Promise.all([
      api("/api/stats", { timeoutMs: 30000 }),
      api("/api/options", { timeoutMs: 30000 }),
      api("/api/config", { timeoutMs: 30000 }),
      api("/api/capabilities", { timeoutMs: 30000 }),
      api("/api/resource-groups", { timeoutMs: 30000 }),
    ]);
    config = c;
    catalogStats = stats;
    if ($("appVersion")) $("appVersion").textContent = stats.version || "—";
    lastCatalogRecordCount = Number(stats.record_count || 0);
    lastArchiveName = String(stats.archive_name || "");
    capabilities = cap;
    options = o;
    groupCatalog = groups;
    language =
      storedLanguage() || detectSystemLanguage(c.ui?.availableLanguages);
    view = c.ui?.catalogView || view;
    imagesEnabled = c.ui?.imagesEnabled !== false;
    pageSize = String(c.ui?.pageSize || 12);
    seed = String(stats.instance_random_seed || "anm");
    sort = c.ui?.sort || "random";
    direction = c.ui?.sortDirection || "asc";
    fill("era", o.eras);
    fill("source_type", o.source_types, "source");
    fill("studio", o.studios);
    fill("country", o.countries, "country");
    fill("tag", o.tags, "theme");
    personSuggestions("directorSuggestions", o.directors);
    personSuggestions("voiceSuggestions", o.voice_actors);
    renderMediaChecks();
    applyStatusDefaults();
    if (!restoreFilterState()) setRecentDates();
    $("pageSize").value = pageSize;
    $("sort").value = sort;
    $("sortDirection").textContent = direction === "asc" ? "↑" : "↓";
    updateSortControls();
    setView(view);
    applyLanguage();
    $("buildInfo").textContent = archiveSummary(stats);
    renderScanProgress(stats);
    await search();
    syncSummaryTimer = setTimeout(pollSyncSummary, stats.sync?.state === "running" ? 1500 : 15000);
  } catch (e) {
    results.innerHTML = `<div class="error">${t("failed")}: ${esc(e.message)}</div>`;
    $("buildInfo").textContent = t("failed");
  }
}
Object.values(filters).forEach((e) =>
  e.addEventListener("input", () => {
    if (e.id === "era") applyEraDateRange(e.value);
    else if (e.id === "start_from" || e.id === "start_to")
      syncEraFromDates();
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
  $("country").value = config.ui?.filterDefaults?.country || "JP";
  applyStatusDefaults();
  $("media_type").innerHTML = "";
  renderMediaChecks();
  setRecentDates();
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
  localStorage.setItem("anm-language", language);
  applyLanguage();
  page = 0;
  await persistUi();
  search();
};
$("cardsView").onclick = () => {
  setView("cards");
  persistUi();
};
$("tableView").onclick = () => {
  setView("table");
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
$("createUser").onclick = async () => {
  await api("/api/auth/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: $("newUsername").value, password: $("newUserPassword").value, role: $("newUserRole").value }) });
  $("newUsername").value = ""; $("newUserPassword").value = "";
  await loadUsers();
};
document.querySelectorAll(".settings-tabs .tab").forEach(
  (b) =>
    (b.onclick = () => {
      document
        .querySelectorAll(".settings-tabs .tab,.tab-panel")
        .forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      document
        .querySelector(`[data-panel="${b.dataset.tab}"]`)
        .classList.add("active");
      if (b.dataset.tab === "logs") loadLogs();
      if (b.dataset.tab === "history") loadHistory();
      if (b.dataset.tab === "subscriptions") loadWatches();
      if (b.dataset.tab === "users") loadUsers();
    }),
);
$("updateArchive").onclick = () =>
  startArchiveUpdate().catch(
    (e) => ($("archiveStatus").textContent = e.message),
  );
$("importArchive").onclick = () => $("archiveFile").click();
$("archiveFile").onchange = () => importArchive($("archiveFile").files[0]).catch((e) => ($("archiveStatus").textContent = e.message));
$("auditLibrary").onclick = async () => {
  await api("/api/library/audit", { method: "POST" });
  $("maintenanceStatus").textContent = t("auditStarted");
};
$("repairMetadata").onclick = () => runMaintenance("metadata").catch((e) => ($("maintenanceStatus").textContent = e.message));
$("settingsForm").onsubmit = (e) =>
  saveSettings(e).catch((x) => ($("saveState").textContent = x.message));
$("testQbt").onclick = () => testConnection("qbittorrent");
$("testAniRss").onclick = () => testConnection("ani-rss");
$("refreshLogs").onclick = loadLogs;
$("refreshHistory").onclick = loadHistory;
$("refreshWatches").onclick = loadWatches;
$("sort").onchange = (e) => {
  sort = e.target.value;
  updateSortControls();
  page = 0;
  search();
  persistUi();
};
$("reshuffle").onclick = async () => {
  try {
    const result = await api("/api/catalog/reshuffle", { method: "POST" });
    if (!result.seed) throw new Error("random seed unavailable");
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
$("selectPage").onchange = (e) => {
  items
    .filter(selectable)
    .forEach((x) =>
      e.target.checked ? selected.add(x.id) : selected.delete(x.id),
    );
  updateSelection();
  render();
};
$("clearSelection").onclick = () => {
  selected.clear();
  torrentSelections.clear();
  resourceSelections.clear();
  updateSelection();
  render();
};
$("createPlan").onclick = () => createPlan();
document
  .querySelectorAll("dialog .close")
  .forEach((b) => (b.onclick = () => b.closest("dialog").close()));
window.__ANM_APP_STARTED__ = true;
localizeStaticUi();
initialize();
