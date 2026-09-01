[中文](architecture.md) | [English](architecture.en.md) | [日本語](architecture.ja.md)  
[README](../README.md) | [部署与使用指南](guide.md) | [架构与数据库](architecture.md)

# AnimeMachine 架构说明

AnimeMachine 的架构以证据边界和写权限边界为基础：Torrent Collector 可以发现并写入新的 `.torrent` 元数据，AnimeMachine 只读扫描 Torrent Pool；AnimeMachine 负责生成下载计划，实际媒体下载交给 qBittorrent 或 Ani-RSS；外部媒体库可以参与识别和播放，并默认保持只读。明确的分层状态能够阻止模糊匹配沿自动化链路继续放大，降低错误目录和错误下载的风险。

Collector 的完整性过滤位于 `src/animemachine/torrents/collector_filter.py`，网络抓取与服务循环位于 `collector.py`，迁移、重试、原子保存和 Pool 审计位于 `collector_state.py`。Collector 只读访问本地 Catalog。SEARCH、FILTER 与 metainfo 分类版本分别管理：FILTER 变化优先重评已经发现的结果，只有 SEARCH 变化才建立新的历史发现任务。Torrent v1、v2 与 hybrid 的 identity 统一由 `metainfo.py` 计算和保存，避免不同入口各自推导一套 hash 规则。

## 运行边界

AnimeMachine 的主要实现位于 `src/animemachine`。Web、Catalog、Torrent 索引、收藏库核验、qBittorrent、Ani-RSS、播放和字幕模块共享 SQLite 证据，但不共享“所有事情都可以写”的权限。一个模块产生的结果，只有达到下一个模块要求的证据强度，才会继续进入目录修改、下载提交或替换流程。

程序直接处理两类输入：(1)用户或可选 Torrent Collector 放进共享目录的本地 Torrent 元数据；(2)用户连接的外部组件返回的记录。Archive、封面和补充元数据统一经过网络层，由网络层处理代理/直连选择、镜像切换、失败退避、响应大小限制和完整性校验。代理设置按每次请求动态读取，运行期间修改系统或环境代理后不要求重启；localhost、回环和私有网段保持直连。凭据只从环境变量、密钥文件或本地设置取得，不写入公开配置和文档。

可选 Torrent Collector 作为独立 Compose 服务运行，对共享 Torrent Pool 有写权限；AnimeMachine 对同一 Pool 仍保持只读。这个边界很重要：Collector 负责“能否把元数据纳入候选池”，AnimeMachine 负责“这份候选到底对应哪部作品、能否覆盖目标、是否值得下载”。前者不能替代后者。

## 数据流

```text
Bangumi Archive ──> 动画 Catalog ──> 标题、关系、目录规划
Torrent Collector ─> 共享 Torrent 池（合集元数据）
Torrent 池 ───────> 增量索引 ─────> 候选策略与文件清单
本地收藏库 ───────> 核验/历史 ───> 差分补完与状态
Ani-RSS ──────────> 订阅与只读媒体映射
候选计划 ─────────> qBittorrent / Ani-RSS ──> 下载状态回写
```

Archive 在后台分阶段构建，只有完整性检查通过后才原子发布。大型 Torrent Pool 与外部媒体库采用增量、分批处理，避免一次全量扫描长期占用数据库事务。Web 查询使用独立连接和短事务，因此后台建库、扫描和前台浏览可以并行进行。

封面是另一个容易拖慢整个系统的典型外部依赖，所以网络读取被移到独立 `ImageFetcher` 进程。程序优先尝试 Bangumi 约 400 px 的服务端缩放图，并对官方源与可用镜像执行短延迟并行后备，原图留作最后兜底。瞬时失败只做短期负缓存；服务重启后会清除这类瞬时失败状态。缓存未命中时，Web API 先返回占位图，后台成功后由前端自动替换；同作品正在下载时再次点击“重新加载”，也会保留一次强制刷新，而不是被正在进行的请求直接吞掉。

## 身份与目录

目录处理遵循一个固定顺序：**先确认作品，再确认已有路径，最后才考虑新建。** 作品身份由标题、别名、首播年月、系列关系和现有文件共同提供证据；如果已有目录中存在唯一、可信的相近目标，优先复用。发现错误目录时也先在原位置纠正，而不是先新建一个“看起来更标准”的目录。官方英语副标题只有在与原名语义不同、且证据充分时才追加到目录名称。

这套设计的原因是，Bangumi 的逻辑条目并不天然等于物理目录。分割放送、附属短篇、原生没有拆分的电影/OVA 合集、超长连续作品，都可能需要把多个逻辑成员归到同一物理目标；反过来，物理目录合并以后，Catalog 中的逻辑成员仍要分别保留。所有下载计划因此先解析到规范目标路径，再与本地文件做差分，不能跳过身份层直接按 Torrent 标题拼目录。

## 资源策略

用户提供的 Torrent 与 Collector 收集的 Torrent 最终进入同一增量索引。Collector 的预筛选只负责挡掉明确不符合自动收集边界的元数据，不能替代 AnimeMachine 的作品识别、资格判断和排序。资源类别只有在用户明确关闭时才被排除，无法识别的值进入“其它”；这比把未知值当成错误更适合长期收藏库，因为旧资源的命名并不总是规范。

排序以一部作品或一个系列的完整方案为单位。系统先判断方案是否覆盖应有集数/卷数，再比较合集形式、资源组、片源、分辨率、字幕和修订日期等条件。资源质量在覆盖关系成立后参与比较，高分辨率不能抵消缺集。

同一 infohash 在 AnimeMachine 中只能存在一个受管任务。一个合集可以把不同文件索引映射给多个子作品；以后需要补充文件时继续复用原任务，只追加此前没有选择的索引。任何正式提交前都会先生成不可变计划。如果计划发现目标目录冲突或待替换文件，就转入暂存、核验或人工审核，不直接覆盖现有内容。

## 作品关系图算法

作品关系图先从多对多关系中恢复关系结构，再确定绘制方式。第一阶段处理图本身：(1)把 Bangumi 原始关系规范化为方向边；(2)补齐能够确定的反向关系；(3)删除可以确认的重复边，以及能够证明的传递冗余；(4)使用严格关系建立系列连通分量。只有证据足够强时，系统才修正明显误连、给孤立子系列补入口，或把关系重新锚定到子系列首部；原始证据始终保留，修正结果不会覆盖来源事实。

第二阶段才处理布局。节点按年月、媒介和标题序列安排；同媒介或具有明确数字顺序的作品优先同排。主线续作尽量走中轴直线，番外、衍生、总集篇、不同演绎和跨系列关系使用独立轨道与端口。路由器同时考虑节点边界、已占轨道、标签矩形和转折成本，可选择直线、正交折线或必要的纵向绕行；同一起点的长跨距关系再按时间和目标位置分层，尽量减少无法辨认的交叉。复杂关系图支持隐藏指定关系类型、全屏浏览和导出 SVG/PNG。

## 仓库结构

```text
src/                 运行源码与内置静态资源
config/              公开配置示例与 JSON Schema
deploy/              本地、四种 Compose 模板及共享 Torrent Collector 定义
packaging/            镜像构建定义
scripts/              Windows、Linux、macOS 构建/启动/清理入口
tests/                可重复的单元与集成测试
docs/                 用户与开发文档
```

运行数据库、Archive、封面缓存、日志、凭据、私有路径和构建产物都属于运行时数据或本地产物，不属于源码仓库。这个区分也意味着：源码可以重新取得，状态目录却应该备份；反过来，清理构建缓存不应触碰用户 Catalog 和收藏历史。

## 数据库约定

AnimeMachine 使用两个 SQLite 文件，分别承载“大规模扫描写入”和“前台稳定查询”。(1)动画 Catalog 默认位于 `$ANM_STATE_DIR/catalog/anime-catalog.sqlite3`，保存 Bangumi Archive 派生的动画元数据、作品关系、封面、外部媒体映射，以及 Web 查询和计划生成需要的运行状态投影；(2)运行库默认位于 `$ANM_STATE_DIR/catalog/runtime.sqlite3`，保存 Torrent Pool 原始增量索引、已核验身份、文件分区、提交证据和本地核验结果。运行库完成短批次后，只把已经核验的结果同步到 Catalog 的 `runtime_*` 表，因此前台不需要直接争用扫描库。

两个数据库路径都可以由环境变量覆盖，但两个 AnimeMachine 实例不能同时写入同一状态目录。动画 Catalog 的基础结构由 `src/animemachine/catalog/service.py` 构建，运行库迁移由 `src/animemachine/catalog/migrations.py` 管理，查询投影由 `src/animemachine/torrents/runtime.py` 管理。迁移必须向前兼容并可重复执行；凭据、私有路径清单和人工导出结果不得写入公开文档或测试夹具。

### 动画 Catalog

- `anime_work`：动画逻辑作品，保存 Bangumi ID、原标题、首播年月、媒介、话数、简介和物理归并角色。
- `anime_title`：保存中、英、日标题和别名；目录命名只使用已经核验的原名，以及确有必要的副标题。
- `anime_staff`、`anime_cast`、`anime_studio`、`anime_studio_cluster`：保存制作人员、声优和制作公司的归一化结果。
- `anime_tag`、`anime_theme`、`anime_theme_evidence`、`anime_country`：保存筛选字段及其规则证据。
- `anime_relation`：保存 Archive 原始关系；`anime_relation_edge` 和 `anime_series_component` 分别保存规范化关系与严格系列连通分量。
- `anime_image`：保存经过验证的封面 MIME、字节、来源和缓存状态；只有完整下载并通过格式验证后才原子写入。
- `external_library_source`、`external_media_file`：保存外部只读媒体库的来源、目录和文件级证据。
- `ani_rss_state`、`ani_rss_subscription`、`ani_rss_resource`、`ani_rss_action`：保存 Ani-RSS 同步状态与幂等操作记录。
- `metadata_snapshot`、`metadata_evidence`、`metadata_repair_queue`：保存底包来源、补充元数据和延后修补任务。

### 运行库与查询投影

- `torrent_source`、`torrent`、`torrent_manifest_file`：保存 Torrent Pool 文件指纹、infohash、标题、片源、资源组、manifest 和解析错误；只有来源指纹变化时才重新读取。
- `anime_work`、`anime_work_member`、`torrent_work`：保存物理目标、逻辑成员，以及 Torrent 到作品的已核验多对多映射；`torrent_work` 是身份与最终路径的权威证据。
- `file_map`：保存合集内每个文件索引的规范目标和选择证据。
- `submission`、`submission_revision`、`submission_file_revision`：保存 AnimeMachine 管理的 qBittorrent 任务和追加选择历史；一个 infohash 只对应一个任务。
- `asset_provenance`、`release_baseline`、`upgrade_candidate`：保存本地文件来源、比较基线和可核验的升级候选。
- `torrent_resolution`、`torrent_target_path`、`title_review`、`scope_exclusion`：保存不能自动确认的范围、标题、目标与处理理由。
- `runtime_work`、`runtime_torrent`、`runtime_torrent_work`、`runtime_torrent_file`、`runtime_file_map`、`runtime_submission`：位于动画 Catalog 中，是已核验运行数据的前台查询投影，不替代运行库中的权威记录。
- `download_plan`、`runtime_watch`、`runtime_watch_match`、`runtime_asset`、`runtime_completeness`、`runtime_review`：位于动画 Catalog 中，保存不可变计划、追更、本地完整度和前台审核状态。

### 状态原则

AnimeMachine 的状态处理可以归纳为六条：(1)逻辑作品不等于物理目录，分割放送、附属短篇和原生未拆分合集可以共享一个物理目标；(2)未核验映射、临时提案和模糊匹配不能授权目录修改或下载提交；(3)本地文件差分只在同一规范目标内比较，快速模式以规范目标和精确字节数判断跳过，精确模式只在存在比较基准时计算哈希；(4)替换必须同时具备修订证据、完整暂存和验证，任何一步失败都保留旧文件；(5)磁力任务在取得完整 manifest 前只登记，不自动映射文件，也不提交下载；(6)配置变化只使派生排序和尚未执行的计划失效，不删除已经核验的作品身份和来源证据。

这六条的共同目标是让“状态变化”可解释。系统可以因为新证据改变结论，但不能因为一次配置改动或一次模糊匹配，把以前已经验证过的事实一起抹掉。

### 并发

长时间导入和扫描使用短批次提交，避免持有长事务；Web 读取使用独立连接并启用 WAL。封面网络访问完全运行在独立进程中，不占用 Web 请求线程或主进程 HTTP 连接池。前端进度来自原子状态文件或后台任务表。耗时任务保持独立运行，其延迟不会扩散到普通浏览和查询。
