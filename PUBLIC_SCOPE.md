# Omni 第一阶段：公开范围与验收

## 已交付的第一阶段

这是一个可公开下载的本地媒体处理核心。它的受支持路径是：

```text
已授权本地视频
→ 解码可见帧并进行画面 OCR
→ 验证可见字幕句末与严格时长
→ 本地 ZIP/TXT 交付物
→ 本地完整性校验
```

本阶段的通过条件：

1. `python scripts/preflight.py` 返回成功；
2. `python -m unittest discover -s tests -v` 全部通过；
3. `clipflow-visual --help` 可执行；
4. 在自带权限的测试视频上，程序要么以 `0` 产生完整交付物，要么以 `2` 产生可解释的 `cut_conflict.json` HOLD；两者都不允许猜测或伪造结果。

## 不属于第一阶段

以下能力尚未实现，不得因为仓库可以安装而宣称已经具备：

- 飞书多维表选行、过滤、锁定或恢复；
- 飞书附件下载、上传、追加与远端 SHA-256 回读；
- WorkBuddy 队列、状态机、持久化运行 ID、幂等写入或失败重试；
- 浏览器自动点击、凭据保存或任何生产系统写入。

未来若做第二阶段，必须以专用配置绑定一个明确的 app/table/view/字段映射，使用最小权限，并实现：

```text
QUEUED → CLAIMED → VALIDATING → RUNNING → UPLOADING → VERIFYING → COMPLETED
```

缺少可见字幕句末证据时必须进入 `HOLD`；系统故障进入可审计的 `RETRYABLE` 或 `FAILED`，不能标记为完成。
