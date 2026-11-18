# 通关数据模拟 · 交互约定

导入、模拟设置与结果共用一个工作台。进入结果视图或返回设置时保留当前输入。
提交失败显示服务返回的原因。查询进度中断时继续查询原任务。
目录选择和帮助使用带焦点返回的原生对话框。只读结果表在窄屏内保留横向滚动。

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
| --- | --- | --- | --- | --- |
| Select/Listbox | Native select | index.html | Unset and numeric target | Keyboard and pointer |
| Form | Native inputs and Constraint Validation API | assets/app.js | Simulation and repository | Empty, minimum, and invalid bounds |
| Scrollbar | Global styles and per-region table overflow | assets/styles.css | Page and table | Desktop and narrow viewport |
| Dialog | Native dialog | index.html | Help and repository | Escape and focus return |
