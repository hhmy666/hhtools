# 交互契约

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Select/Listbox | Native | DESIGN.md | 原生下拉框；浏览器字库 | Chrome 键盘与选择 |
| Form | 原生字段和工作台事件 | 后端命名校验 | 读取、另存与试玩 | 浏览器验收 |
| Scrollbar | 全遮盖样式表 | DESIGN.md | 工具区滚动 | 浏览器检查 |
| Toast | 工作台状态区 | DESIGN.md | 成功、错误、提示 | 浏览器验收 |
| CRUD | 后端模板存储 | 使用说明.md | 先另存，后独占创建 | 单元与浏览器验收 |

路径发现只检查当前用户常见目录中实际存在的文件夹；没有 WebEditor 时使用空白本地配置和数字图案，用户可导入自己的 CSV，不携带原始项目配置。
