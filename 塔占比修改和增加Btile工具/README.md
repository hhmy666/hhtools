# 塔占比修改及增加 Btile 工具

批量降低关卡模板的塔占比，或增加塔和 B Tile。可以对比、验收新模板，并按需为新模板配置 DupPara 参数。

## 首次启动

- 准备 macOS、Python 3.9 或以上和一个浏览器。推荐从 [Python 官网](https://www.python.org/downloads/macos/) 安装 Python，无需另装 Python 库。
- 双击 `启动塔占比修改及增加Btile工具.command`，保持弹出的终端窗口开着。
- 如果工具发现模板库，核对后点「确认使用」。也可以在「设置」手动填写后点「保存设置」，回到工作台确认。

| 设置项 | 路径示例 |
| --- | --- |
| Tile 仓路径 | `/Users/你的用户名/Projects/tile/Tile/Assets/Resources/TileMatchLevelData` |
| 模板仓路径 | `/Users/你的用户名/Projects/tileleveltemplate/Template` |
| 配置仓库 / CSV 目录 | `/Users/你的用户名/Projects/ulinkconfig/csv` |
| 默认导出目录 | 工具内的 `输出模板`，也可选自己的可写目录 |

用本机实际目录替换示例。按 ID 读取和同步时才需要对应模板库；只上传模板文件时可以先跳过。配置目录供「一键配置新参数」使用。

## 基本操作

1. 上传 `_m.bytes` 文件，或选读取来源并按行粘贴模板 ID。
2. 点击「去增加塔」或「去减少塔」，设置批次参数。可对单个模板单独调整。
3. 运行后查看验收结果并对比阵型。按需微调、重新验收，再下载或预览同步。
4. 如需调整普通 B Tile，从「增加Btile工具」进入。生成参数用「一键配置新参数」，操作前核对配置目录。

更多操作见网页内的「使用说明」及 [详细说明](docs/README.md)。
