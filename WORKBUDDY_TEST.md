# WorkBuddy 验收入口

这个项目现在包含完整的剧情队列 worker。不要再只做本地 help/单测后就说已经能够跑飞书。

- 仅验证安装、依赖、预检和单元测试：按 README 的“本地质量验证”执行。
- 要让 WorkBuddy 真正执行剧情队列：先按 [FEISHU_PERMISSIONS.md](FEISHU_PERMISSIONS.md) 配置权限，再将 [WORKBUDDY_NEW_WINDOW_PROMPT.md](WORKBUDDY_NEW_WINDOW_PROMPT.md) 全文交给一个新窗口。

该新窗口指令已规定私有 secrets、精确剧情 view、执行顺序、HOLD 处理、远端 SHA-256 验收和最终队列清空检查。不要将旧的 local-only 测试边界套用到已经完成私有授权的 worker 执行任务上。
