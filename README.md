# Omni 第一阶段 — ClipFlow Visual v0.6.0

`omni第一阶段` 是 ClipFlow 的**第一阶段、本地可执行**版本：它只根据视频解码后的可见画面字幕规划切点、输出视频切段和三件固定交付物。

它适合让 WorkBuddy 或任意 Linux/macOS worker 下载后先做环境验证和本地试跑。它不是飞书多维表自动化成品，也不包含任何账号、Cookie、API Key、表格 ID、视频样本或生产数据。

## 本仓库包含什么

- 只使用可见视频帧的 OCR；不打开、不转写、不推断音频。
- 严格切段门槛：每段必须满足 `3.000 < 时长 <= 8.000` 秒。
- 切点必须是经可见字幕证实的句末；无法证实时生成 `cut_conflict.json` 并以 HOLD 停止，不会猜切或伪造输出。
- PASS 时生成固定三件交付物：`videos_and_screenshot.zip`、`supporting_materials.zip`、`文案.txt`。
- 每段 MP4 都有对应首帧候选截图和 `C001` 风格编号水印。
- 可重复运行的质量契约单元测试和环境预检脚本。

## 明确不包含什么

- 不会选取、锁定或扫描飞书多维表记录。
- 不会下载或上传飞书附件，也不会改写任何单元格。
- 不含 WorkBuddy dispatcher、队列、锁、重试、幂等上传或远端 SHA-256 回读实现。
- 不含任何真实视频、飞书链接、app/table/view ID、凭据或用户数据。

因此，WorkBuddy 对本仓库的第一阶段验收只能是“能安装、能通过测试、能对**其自行提供且获授权**的视频进行本地运行”。任何飞书读写必须作为下一阶段独立实现和验收。

## 运行前置条件

- Python 3.10 或更高版本
- `ffmpeg` 与 `ffprobe`
- `fc-match`（来自 Fontconfig，用于水印字体定位）
- Python 包：`numpy`、`opencv-python-headless`、`rapidocr-onnxruntime`（由 `pip install -e .` 安装）

首次执行 OCR 时，底层 OCR 运行时可能需要准备模型资产；请让执行环境遵守自己的网络与缓存策略，不要借此访问任何业务系统。

## 最小环境验证

```bash
git clone https://github.com/574210187-ship-it/omni第一阶段.git omni-phase1
cd omni-phase1
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python scripts/preflight.py
python -m unittest discover -s tests -v
clipflow-visual --help
```

上述命令没有处理真实视频、没有连接飞书，也没有任何外部写入。

## 本地视频试跑

仅使用已授权的非敏感测试视频，并把输出放在一个**不存在或为空**的目录中：

```bash
clipflow-visual run --input /absolute/path/to/test.mp4 --out ./output
```

退出语义：

- `0`：本地 PASS，输出目录包含三件固定交付物。
- `2`：安全 HOLD，输出 `cut_conflict.json`；这不是可用的媒体结果，不能上传或当作失败后自动重试的理由。
- `1`：环境或输入错误；根据输出中的原因修复后再从空目录重试。

当画面字幕足够清晰且可见句末能自动确认时，程序可直接 PASS。若句末需要人工视觉确认，先保留第一次输出中的 `visual_ocr_timeline.json`，生成与该文件 SHA-256 精确绑定的 `review.json`，再用**新的空输出目录**重新运行：

```bash
clipflow-visual run --input /absolute/path/to/test.mp4 --out ./output-reviewed --review-json /absolute/path/to/review.json
```

`review.json` 的证据来源只能是 `visible_video_frames_only`；不得用 ASR、口述内容或猜测来填充它。

## 给 WorkBuddy 的测试说明

直接交给 WorkBuddy 的受限测试任务在 [WORKBUDDY_TEST.md](WORKBUDDY_TEST.md)。它强制先做本地预检和单元测试，并禁止在第一阶段访问飞书、输入凭据或对外上传文件。

## 项目资料

- [PUBLIC_SCOPE.md](PUBLIC_SCOPE.md)：公开第一阶段边界与验收标准
- [SOURCE_PROVENANCE.md](SOURCE_PROVENANCE.md)：源代码基线与许可证说明
- [AGENTS.md](AGENTS.md)：处理门槛
- [ONLINE_WORK_MODE.md](ONLINE_WORK_MODE.md)：下一阶段在线流程的接口约束，不是本仓库已实现的自动化
- [BITABLE_WORKFLOW.md](BITABLE_WORKFLOW.md)：飞书写入契约说明，不含远端写入代码

## 许可证

MIT；详见 [LICENSE](LICENSE)。
