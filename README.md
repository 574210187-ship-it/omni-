# Omni 第一阶段 — 剧情队列端到端执行器 v0.7.0

这是可以交给 WorkBuddy 执行的第一阶段完整流程，不只是本地剪辑程序。

~~~
私有飞书绑定
→ 核对“剧情”视图与字段
→ 自动找出未完成的剧情行
→ 单行认领与租约
→ 下载源视频、仅按可见画面处理
→ 追加三件成品
→ 下载回读并逐件 SHA-256 校验
→ 回填状态、运行 ID、输入 SHA、证据和执行记录
→ 下一行，直到当前可执行队列清空
~~~

公开仓库绝不含飞书凭据、真实 Bitable URL/ID、附件、视频或业务记录。它通过私有环境变量完成一次性绑定；之后 omni-phase1 drain --apply 会串行处理当前“剧情”队列。

## 两层程序，职责明确

- clipflow-visual：严格的本地视觉处理核心。只使用视频已解码画面中的可见字幕；不使用 ASR。每段必须满足 3.000 < 时长 <= 8.000 秒，且切点必须有可见句末证据。
- omni-phase1：飞书 Bitable worker。负责视图校验、选择行、租约、下载、执行核心、上传、追加附件、远端 SHA-256 回读、回填与恢复。

本地核心仍生成 local_delivery_contract.json，表示核心本身不会偷偷联网。只有 omni-phase1 加 --apply 才会调用飞书 API；没有 --apply 的命令是只读或预览。

## 绝对范围

- 只接受私有绑定中 expected_view_name: 剧情 的精确 view ID；视图名或 ID 不匹配立即停止。
- 只处理 二次分类型 值含有 剧情 的记录；不会再进入“聊天”。
- 不读取或改写通用 状态 字段。worker 只使用 ClipFlow状态 等专用字段。
- 输出固定追加到 处理后音、视频：videos_and_screenshot.zip、supporting_materials.zip、文案.txt。
- 每件附件都必须下载回本地，并与本地交付物作 SHA-256 比对；未通过不得标记 COMPLETED。
- 任何视觉证据不足、输入不存在、输出冲突或无法安全恢复的行都会写为 HOLD 并继续下一行；不会编造产物或覆盖既有附件。

## 一次性飞书准备

请先按 [FEISHU_PERMISSIONS.md](FEISHU_PERMISSIONS.md) 给自建飞书应用授权，并把该应用加入目标多维表的高级权限。然后在 WorkBuddy 的私有 Secrets 中设置：

~~~
FEISHU_APP_ID
FEISHU_APP_SECRET
FEISHU_BITABLE_URL
~~~

FEISHU_BITABLE_URL 必须是剧情视图的完整 Bitable URL，且 URL 必须带 table= 与 view=。程序从该 URL 精确提取 app/table/view，不会扫描账号中的其它表。

复制安全模板到被忽略的私有目录：

~~~bash
mkdir -p .workbuddy
cp job-config.example.json .workbuddy/job-config.json
~~~

模板默认映射的是当前业务字段：

| 用途 | 字段 |
| --- | --- |
| 剧情筛选 | 二次分类型 = 剧情 |
| 输入附件 | 对标视频、图文 |
| 输出附件 | 处理后音、视频 |
| 审计日志 | 执行记录 |
| 专用 worker 状态 | ClipFlow状态、ClipFlow运行ID、ClipFlow锁到期、ClipFlow输入SHA、ClipFlow错误码/证据 |

首次 bootstrap --apply 只会创建缺失的六个文本 worker 字段；不会修改任何记录。

## WorkBuddy 的完整执行指令

把 [WORKBUDDY_NEW_WINDOW_PROMPT.md](WORKBUDDY_NEW_WINDOW_PROMPT.md) 整段复制到一个全新 WorkBuddy 窗口。它规定了从 clone、私有绑定、权限验收到 drain、最终回读的完整顺序和停止条件。

## 安装与手动命令

~~~bash
git clone --depth 1 https://github.com/574210187-ship-it/omni-.git omni-phase1
cd omni-phase1
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
~~~

若执行环境已经提供可信系统 CA、但 Python 默认路径遗漏它而导致 CERTIFICATE_VERIFY_FAILED，可显式指定该可信 CA，例如：

~~~bash
PIP_CERT=/etc/ssl/certs/ca-certificates.crt python -m pip install -e .
~~~

不得使用 --trusted-host、-k 或关闭 TLS 校验。

执行顺序：

~~~bash
omni-phase1 --config .workbuddy/job-config.json check-config
omni-phase1 --config .workbuddy/job-config.json bootstrap --apply
omni-phase1 --config .workbuddy/job-config.json plan
omni-phase1 --config .workbuddy/job-config.json drain --apply
omni-phase1 --config .workbuddy/job-config.json plan
~~~

plan 永远只读。drain --apply 固定单进程、单行串行。中断后再次执行相同命令：它会先读取状态、租约和既有附件，不会盲目覆盖或重复追加。

## 状态机

~~~
READY
  → CLAIMED → VALIDATING → RUNNING → UPLOADING → VERIFYING → COMPLETED
                                        │
                                        ├─ 恢复时先查已有附件与本地运行收据
                                        └─ SHA 不同/同名冲突 → HOLD

任何可见画面证据不足、输入异常、部分输出无安全收据
  → HOLD

临时网络/系统问题
  → RETRYABLE
~~~

飞书 Bitable 没有跨 worker 的 compare-and-swap 锁。因此必须只运行一个 drain worker；程序本地锁与远端租约降低重复执行风险，并在每次远端写入前重新读取运行 ID/租约。

## 本地质量验证

~~~bash
python scripts/preflight.py
python -m unittest discover -s tests -v
clipflow-visual --help
omni-phase1 --help
~~~

这些验证不等于已连接飞书。只有私有绑定完成后，check-config、bootstrap --apply、drain --apply 的实际远端读回才构成端到端验收。

## 文档

- [FEISHU_PERMISSIONS.md](FEISHU_PERMISSIONS.md)：一次性需要开通的飞书权限与表级授权
- [WORKBUDDY_NEW_WINDOW_PROMPT.md](WORKBUDDY_NEW_WINDOW_PROMPT.md)：可直接粘贴的新窗口完整指令
- [WORKFLOW.md](WORKFLOW.md)：行选择、恢复、幂等和验收规则
- [PUBLIC_SCOPE.md](PUBLIC_SCOPE.md)：公开边界与验收定义
- [SOURCE_PROVENANCE.md](SOURCE_PROVENANCE.md)：源代码与发布沿革

GitHub slug 为 omni-；项目展示名称为“Omni 第一阶段”。

## 许可证

MIT；详见 [LICENSE](LICENSE)。
