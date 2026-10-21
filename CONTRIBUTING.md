[中文](CONTRIBUTING.md) | [English](CONTRIBUTING.en.md) | [日本語](CONTRIBUTING.ja.md)

# 参与开发

AnimeMachine 使用 Python 3.11 或更新版本。界面使用原生 HTML、CSS 和 JavaScript；作品资料与运行状态保存在 SQLite 中。

## 组件用途

| 组件 | 用途 |
| --- | --- |
| AnimeMachine | 浏览作品、关联媒体、规划下载、管理观看记录和播放列表 |
| Bangumi Archive | 提供作品名称、日期、制作人员与作品关系 |
| qBittorrent | 接收下载任务并保存媒体；按需连接已有服务或联合部署 |
| Ani-RSS | 查询资源、管理追番订阅、提供媒体与缓存封面；可选接入 |
| Torrent Collector | 收集 Torrent 元数据，供按作品检索和下载计划使用 |
| PotPlayer 扩展 | 在播放器内回传精确播放位置；播放本身可独立使用 |

组件间通过 API 或约定的目录通信。运行方式见[部署指南](docs/guide.md)，数据流与边界见[架构说明](docs/architecture.md)。

## 本地开发

在项目根目录创建虚拟环境并安装开发依赖：

```sh
python -m venv .venv
```

激活虚拟环境后运行：

```sh
python -m pip install -e ".[test]"
python scripts/test_all.py
python scripts/check_quality.py
```

修改 JavaScript 时使用 Node.js 检查语法。修改部署文件时安装 Docker Compose，并运行：

```text
node --check src/animemachine/web/static/app.js
python scripts/check_compose.py
```

## 文件组织

| 目录 | 内容 |
| --- | --- |
| `src/animemachine` | 应用代码、界面与内置资源 |
| `tests` | 合成测试资料、单元测试与集成测试 |
| `packaging` | Docker 镜像与发布包定义 |
| `deploy/compose` | 默认完整组合、替代模板与独立采集器配置 |
| `scripts` | 测试、校验、构建与维护入口 |
| `docs` | 三语使用指南、架构说明与配置参考 |

## 提交修改

围绕具体问题组织修改，并说明使用效果及验证结果。作品识别、关系和资料导入的改动以可重复的样例验证；涉及已保存数据时，同时提供迁移及旧版本升级测试。界面和使用说明同步维护中文、英文和日文。

测试使用合成元数据与独立临时目录；下载及播放器联调使用专用测试环境。提交内容包含源码、维护文档和可复现样例，实例配置、凭据、媒体与构建产物保存在本地。
