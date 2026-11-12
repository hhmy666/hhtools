# 关卡参数配置工具

批量修改 `config_level.csv` 里的关卡、模板及 DupPara1–10 参数，并对比修改前后的 CSV。

## 启动与依赖

双击 `关卡参数配置工具（修改config_level表）.html`，用 Chrome、Edge 或 Safari 打开。CSV 解析库 Papa Parse 5.4.1 和许可证已放在 `vendor` 文件夹，页面可离线使用，无需安装 Python 或其他软件。请保留整个工具文件夹。 [Papa Parse 官方仓库](https://github.com/mholt/PapaParse/tree/5.4.1)

## 基本操作

1. 点「导入 config_level」，选择你自己的项目文件。例如在 Finder 中进入 `/Users/你的用户名/Projects/ulinkconfig/csv`，选中 `config_level.csv`。每次打开页面后按需选择文件。
2. 点「粘贴关卡id/模板id和参数配置」。从表格复制数据时，第一列为关卡 ID，第二列为模板 ID，随后依次为 DupPara1–10。只填 DupPara1 时，其余参数会复制该值。
3. 点「执行配置修改」，选对应规则，确认后查看结果与详情。规则含义可从「规则解释」查看。
4. 点「导出新 config_level.csv」保存副本，再用「检验修改」选择原文件和新文件核对差异。核对无误后按团队流程使用导出的文件。
