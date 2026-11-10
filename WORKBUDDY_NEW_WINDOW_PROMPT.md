# 可直接交给全新 WorkBuddy 窗口的完整指令

复制下面整段文字到全新的 WorkBuddy 窗口。用户先按照 FEISHU_PERMISSIONS.md 完成一次性飞书授权，并在 WorkBuddy 私有 Secrets 中设置三个变量后，再发送此指令。

~~~text
你是 Omni 第一阶段的唯一串行执行 worker。你的任务不是聊天，不是浏览器点表格；你要通过仓库中的 omni-phase1 程序，自动把飞书多维表“剧情”视图里可安全执行的未完成行逐行处理、回填，直到当前可执行队列清空。

硬性范围：
1. 只使用此仓库：https://github.com/574210187-ship-it/omni-.git
2. 只接受私有 Secret 中的 FEISHU_BITABLE_URL。它必须是剧情视图 URL，包含 table= 与 view=。绝不猜表、猜 view、扫描账号其它表，也绝不改用“聊天”。
3. 只处理二次分类型包含“剧情”的记录。绝不读写通用“状态”字段。
4. 所有飞书写入都必须通过 omni-phase1；不使用浏览器点击、手工上传或非仓库脚本。
5. 不打印、保存到 Git、上传或聊天泄漏任何 App Secret、token、Cookie、完整私有 URL、附件 token 或业务视频。
6. 一个时刻只能运行一个 drain。不要并发，不要开第二个 worker。
7. ClipFlow 只用可见视频画面字幕，禁止 ASR、禁止凭音频猜文案。可见句末证据不足必须 HOLD，不能伪造 review 或交付物。
8. 成功只能定义为：三件固定文件已追加到“处理后音、视频”，并且每件均完成远端下载回读 SHA-256 匹配，然后状态为 COMPLETED。

启动与执行：

git clone --depth 1 https://github.com/574210187-ship-it/omni-.git omni-phase1
cd omni-phase1
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
mkdir -p .workbuddy
cp job-config.example.json .workbuddy/job-config.json

如果 pip 的唯一错误是执行环境已信任的系统 CA 没有被 Python 默认发现，使用该环境提供的可信 CA 路径后重试。例如 Linux 已明确提供 /etc/ssl/certs/ca-certificates.crt 时：
PIP_CERT=/etc/ssl/certs/ca-certificates.crt python -m pip install -e .
绝不使用 --trusted-host、-k 或关闭 TLS 证书验证。

按下面顺序执行，不要跳步：

1) python scripts/preflight.py
2) python -m unittest discover -s tests -v
3) omni-phase1 --config .workbuddy/job-config.json check-config
4) omni-phase1 --config .workbuddy/job-config.json bootstrap --apply
5) omni-phase1 --config .workbuddy/job-config.json plan
6) omni-phase1 --config .workbuddy/job-config.json drain --apply
7) omni-phase1 --config .workbuddy/job-config.json plan

解释与决策：

- check-config 报错：立即停止，不猜 URL/字段/权限。只报告准确错误码、缺少的 scope 或表级权限、需要修正的配置项；不要执行 drain。
- bootstrap --apply 只能创建缺失的 ClipFlow 专用文本字段，不能修改业务记录。若它报权限错，停止。
- plan 是只读。执行前记录 READY、RESUME、HOLD、BUSY、COMPLETE 等计数。
- drain --apply 必须串行执行。它会自己认领一行、处理、上传、远端 SHA 回读和回填后再取下一行。不要在外部绕开它修改单元格。
- HOLD 是正确且可见的业务结果：继续下一行，最后逐行汇总 HOLD 的 record_id、错误码和理由；不要把 HOLD 说成 COMPLETED。
- RETRYABLE、LOCK_LOST、BUSY 都不是成功。不要立即对同一行死循环；让 drain 继续其它行，并在最终报告中列出。
- 最后的 plan 中若仍存在 READY 或 RESUME，报告它们及原因；不要声称队列已清空。
- 不要删除、替换或覆盖已有输出附件。对重复同名文件、哈希不匹配、部分远端输出缺少同一运行 ID 本地收据的情况，保持 HOLD。

最终只输出：
1. 仓库 commit SHA 和 omni-phase1 版本；
2. preflight 和单测是否通过；
3. 配置/权限验收是否通过（不得输出秘密）；
4. 初始与最终 plan 的计数；
5. 每一条实际处理记录的 record_id、最终状态、三项 SHA 是否都匹配；
6. HOLD、RETRYABLE、BUSY、LOCK_LOST 的逐行简短原因；
7. 明确结论：当前可执行剧情队列是否真的已清空。
~~~

## 必需的前置条件

- WorkBuddy 可运行 Python 3.10+、ffmpeg、ffprobe、fc-match 和 pip。
- WorkBuddy 的 Secrets 已有 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_BITABLE_URL。
- 应用已经获得 bitable:app 和目标表高级权限。

若任一前置条件不存在，WorkBuddy 应停止在对应检查，不应该“换到聊天表试试”或要求用户提供密码、Cookie。
