# 关键结构生成工具

从关卡模板生成带关键结构的候选，比较结构与难度，选中候选后验证并导出。还可以生成 DupPara 参数，或用「目标消耗校准 Beta」调参。

## 首次启动

- 准备 macOS、Python 3.10 或以上和一个浏览器。推荐从 [Python 官网](https://www.python.org/downloads/macos/) 安装 Python，无需另装 Python 库。
- 双击 `启动关键结构生成工具.command`，保持弹出的终端窗口开着。

首次打开会提示确认本机的源模板和配置仓路径。可以选取目录，或点「稍后配置」直接上传模板。之后可在「设置」里修改。

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

## 设置本机目录

打开「设置」，每项填写或选择后点对应的保存按钮。

| 设置项 | 示例 | 何时需要 |
| --- | --- | --- |
| Tile 源模板目录 | `/Users/你的用户名/Projects/tile/Tile/Assets/Resources/TileMatchLevelData` | 按模板 ID 导入。也可以直接拖入 `_m.bytes` 文件 |
| 输出工作区 | 默认是工具内的 `输出关卡模板` | 存放新模板和验证报告。可选其他可写目录 |
| ulinkconfig 根目录 | `/Users/你的用户名/Projects/ulinkconfig` | 使用「一键配置新参数」或「目标消耗校准」 |

路径需换成本机实际目录。模板与配置表从你有权限的项目仓库准备。工具附带生成评分用的派生模型和统计参考。

## 基本操作

1. 在「创建任务」导入模板 ID 或拖入模板文件，先从一个熟悉的模板试跑。
2. 设置生成方案，例如结构步数、类型和塔内模式，启动生成。
3. 对比候选及求解轨迹，选择要保留的候选。
4. 到「验证导出」复核并导出，从设置的输出目录取文件。参数校准如果需要写回配置，请先核对工具内的预览和写入范围。

更多功能见网页右上角「使用说明」及 [详细说明](docs/README.md)。
