# 全遮盖识别工具

检查关卡模板的邻层一压一、悬空全遮盖等问题。在结果中定位坐标、比较原图与修复结果，按需微调后导出新模板。

## 首次启动

- 准备 macOS、Python 3.9 或以上和一个浏览器。推荐从 [Python 官网](https://www.python.org/downloads/macos/) 安装 Python，无需另装 Python 库。
- 双击 `启动全遮盖识别工具.command`，保持弹出的终端窗口开着。浏览器未自动打开时，可以复制终端显示的本地网址。

首次打开会检查本机常见模板仓路径。确认候选，或点「选择目录」选择自己的模板仓和 Tile 编辑器仓。只上传文件时可以点「稍后配置」。

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

## 导入、检查和导出

1. 有 `_m.bytes` 文件时，选择「上传模板文件」直接导入。按 ID 查找时，点「设置」填写「模板仓路径」，例如 `/Users/你的用户名/Projects/tileleveltemplate/Template`。把示例换成本机实际存放模板的目录，再粘贴模板 ID。
2. 点击「评估模板」。查看问题、坐标和模板预览，可选用「修复全部」或在预览中编辑，完成后保存修改。
3. 下载 ZIP，或点「预览导出」检查新文件名和路径，再确认写入。默认落盘到本工具的 `输出模板` 文件夹。
4. 如需同步，展开「同步到模板库（可选）」并分别填写目标。Tile 编辑器目录示例为 `/Users/你的用户名/Projects/tile/Tile/Assets/Resources/TileMatchLevelData`。勾选同步目标前请核对路径。

路径修改会记在当前浏览器。使用 Finder 选中文件夹，按 `⌥⌘C` 可复制路径。工具需要你自行提供模板文件。
