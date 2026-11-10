# Omni 第一阶段：公开范围与验收

## 已交付

公开包包含两个可执行层：

1. ClipFlow Visual：本地、视觉证据约束的视频切分和三件固定交付物。
2. Omni worker：通过飞书官方 REST API 完成剧情队列选择、租约、附件下载和上传、记录回填、远端 SHA-256 回读与中断恢复。

公开仓库不包含任何真实 Bitable 标识、应用凭据、视频、附件 token、Cookie 或运行日志。所有业务绑定只从私有环境变量和被忽略的 .workbuddy/job-config.json 读取。

## 可以证明什么

本地验收至少包括：

~~~bash
python scripts/preflight.py
python -m unittest discover -s tests -v
clipflow-visual --help
omni-phase1 --help
~~~

这些命令只证明代码可安装和可调用。

端到端验收还必须在拥有真实、私有飞书授权的 WorkBuddy 环境中完成：

1. check-config 读回精确剧情 view 和字段；
2. bootstrap --apply 创建缺失专用字段；
3. drain --apply 至少完成一次真实行处理；
4. 远端读回三件附件并逐一 SHA-256 匹配；
5. 最终 plan 证明没有 READY/RESUME 行，或清楚列出未完成的 HOLD/RETRYABLE/BUSY。

## 永远不允许

- 用聊天视图、通用状态字段或模糊字段猜测业务队列；
- 把 ClipFlow 本地 PASS、安装成功或 HOLD 称为远端完成；
- 在未加 --apply 时执行外部写入；
- 覆盖/删除既有输出附件；
- 不经远端完整文件 SHA-256 回读就写 COMPLETED；
- 通过浏览器点击、秘密泄漏或禁用 TLS 来绕过 API、权限或证书问题。

## 运行业务边界

worker 只处理二次分类型含“剧情”的行，并使用：

~~~text
READY → CLAIMED → VALIDATING → RUNNING → UPLOADING → VERIFYING → COMPLETED
                                      ↘ HOLD / RETRYABLE
~~~

详情见 [WORKFLOW.md](WORKFLOW.md) 与 [FEISHU_PERMISSIONS.md](FEISHU_PERMISSIONS.md)。
