# 参与开发

AnimeMachine 的开发环境统一使用 Python 3.11 或更新版本。首先创建虚拟环境，再使用 `pip install -e .[test]` 安装项目，最后运行：

```text
python scripts/test_all.py
```

项目文件按用途固定组织：(1)运行时代码放在 `src/animemachine`；(2)镜像定义放在 `packaging`；(3)部署示例放在 `deploy`；(4)构建和维护入口放在 `scripts`。不得提交凭据、私有路径、数据库、Torrent 文件、日志、媒体清单或生成产物。凡是修改 Catalog 导入、目录身份识别或作品关系逻辑，都必须补充可重复执行的单元测试或集成测试；如果修改会影响已保存的数据结构，还必须提供明确的 Schema 迁移。

按作品检索 Torrent 在隔离子进程中运行；可选 Torrent Collector 也可作为独立 Compose 服务，向共享池补充元数据。自动化测试使用合成元数据和相互隔离的临时目录。真实索引、下载和播放器联调使用专门的测试环境，并与个人服务及文件分开。
