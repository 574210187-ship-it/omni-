# Source provenance

本公开仓库最初从以下本地视觉核心抽取：

- source repository: https://github.com/574210187-ship-it/koubo-rules
- source path: tools/reference-clipflow/
- source commit: 744aa8810e6df6c89a7ed9ba7b5b7e19ea861af5
- original local package version: clipflow-visual 0.6.0

公开 v0.6.1 只包含本地 ClipFlow 核心。v0.7.0 在保留该核心的 local_delivery_contract.json 的前提下，新增独立 omni_phase1 worker：

- 私有环境变量绑定，不公开真实飞书标识；
- documented Feishu Bitable/Drive REST adapter；
- 剧情 view/字段门禁、行状态机、租约、幂等附件追加；
- 单元测试和 WorkBuddy 完整交接文档；
- 远端附件 SHA-256 回读验收。

没有从源仓库复制真实飞书记录、附件 token、账号、视频、凭据或业务运行日志。

包含的代码继续使用 [MIT License](LICENSE)。
