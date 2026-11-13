# 玩家行为分析工具

先把「玩家单关 TileNum」回放 CSV 汇总为玩家行为分层，再结合线上关卡副本 CSV，生成关卡分类与推荐序列。

## 首次启动

- 准备 macOS、Python 3.9 或以上和一个浏览器。推荐从 [Python 官网](https://www.python.org/downloads/macos/) 安装 Python。使用历史固定配置时，还需要 Git 命令和包含对应历史提交的配置仓库。
- 双击 `启动玩家行为分析工具.command`。如果缺少 `pandas` 或 `numpy`，启动器会在工具目录内安装到本地 Python 环境。安装时请联网并保持窗口开启。安装完成后会打开浏览器。

工具会自动使用在当前 Mac 找到的关卡模板和配置仓目录。「本机依赖」显示当前路径和检查状态。需要调整时，打开「设置」，点「选择目录」再保存。更新后请先关闭旧启动器的终端窗口，再打开新版。

### 用终端安装 Python（已有 Homebrew）

打开 Mac 的「终端」，依次粘贴下面的命令。最后一行应显示 `Python 3.13.x`。

```zsh
brew install python@3.13
echo 'export PATH="$(brew --prefix python@3.13)/libexec/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile
python3 --version
```

安装完成后，回到 Finder 重新双击启动器。如果提示 `brew: command not found`，可以到 [Python 官网](https://www.python.org/downloads/macos/) 下载 Python 3.13 的 macOS installer（`.pkg`），双击安装后新开终端，再用 `python3 --version` 确认版本。[Homebrew Python 3.13 说明](https://formulae.brew.sh/formula/python@3.13)

### macOS 提示「Apple 无法验证」时

确认启动器来自团队分享的 hhtools 仓库后，按以下步骤授权：

1. 双击本工具的 `.command` 启动器。弹出警告后，点击「完成」。
2. 打开「 → 系统设置 → 隐私与安全性」，向下滚动到「安全性」区域。
3. 核对被拦截的文件名，点击「仍要打开」，按系统提示确认。如果没有看到按钮，重新双击同一个启动器，再回到此页面。

每个工具的启动器按需分别授权。公司管理的 Mac 如果没有放行选项，请联系 IT。[Apple 官方操作说明](https://support.apple.com/zh-cn/102445)

## 在工具里配置数据目录

点「设置」，填入你电脑上的目录，点「保存设置」。

| 设置项 | 示例 |
| --- | --- |
| 关卡模板目录 | `/Users/你的用户名/Projects/tile/Tile/Assets/Resources/TileMatchLevelData` |
| 配置仓库或 CSV 目录 | `/Users/你的用户名/Projects/ulinkconfig/csv` |

可在 Finder 选中文件夹，按 `⌥⌘C` 复制路径。模板目录应包含任务所需的 `_m.bytes`，配置目录应来自团队仓库。历史回放使用与数据匹配的配置快照，默认固定提交为 `4eac5d…` 和 `5022d0…`。只有明确要按当前配置分析时，才切换到「当前仓库检出版本」。

## 基本操作

1. 在第一阶段导入一份或多份玩家单关 TileNum CSV，先做预检，按提示补齐文件或路径，再运行并下载玩家行为分层工作簿。
2. 若需要排关，继续导入线上关卡副本 CSV，选择排关方案，生成并下载推荐序列和副本标签。
3. 在「运行记录」中打开已有结果。上传文件、草稿和任务留在本机的 `运行数据`，生成的文件保存在 `输出分析结果`。

更多分析口径见 [详细说明](docs/README.md)。
