# 飞书一次性授权清单

这个 worker 使用飞书开放平台 REST API，不靠浏览器点击表格。创建一个企业自建应用，并把下面权限和表级访问一次性开好；完成后 WorkBuddy 只需要私有环境变量，就可以直接执行。

## 推荐的 API 权限

推荐直接给应用开启：

| 飞书控制台显示名 | Scope |
| --- | --- |
| 查看、评论、编辑和管理多维表格 | bitable:app |

这是本 worker 的推荐单一权限。飞书官方文档将它列为以下接口的可用权限：

- 列表/读取 Bitable 记录；
- 更新 Bitable 记录；
- 列字段、列视图、新增字段；
- 上传 Bitable 附件；
- 下载 Bitable 附件。

因此，不需要申请通讯录、消息、日历、聊天记录或管理后台权限。程序不读取人员字段，也不需要 contact 相关 scope。

如果组织安全策略不允许 bitable:app，管理员可以按飞书当前控制台提供的等价细粒度权限组合开通；但必须覆盖记录读取和更新、字段读取和创建、视图读取、以及 Bitable 附件上传和下载。先运行 check-config 和 bootstrap --apply 验证，不要靠名称猜测。

## 必须做的表级授权

API scope 不等于该应用有权访问某一张表。针对目标多维表：

1. 在飞书开放平台创建自建应用，保存 App ID 和 App Secret 到 WorkBuddy 私有 Secrets。
2. 在应用权限管理中开通并发布 bitable:app。
3. 打开目标多维表的分享/高级权限设置，将该“文档应用”加入。
4. 给应用目标表的读、写、附件下载、附件上传和字段创建权限。最不容易误配的是“可管理”；若使用自定义权限，必须包含这些项目。
5. 若表有行权限或字段权限，确保应用能读取 二次分类型、对标视频、图文，写入 处理后音、视频、执行记录 和全部 ClipFlow 专用字段。

如果 check-config 返回 WRONG_VIEW_BINDING、MISSING_BUSINESS_FIELDS 或 HTTP 403，先修正这个授权；不要把 URL 改成聊天视图，也不要让 agent 通过网页绕过。

## 私有 Secrets

只在 WorkBuddy 的 Secrets/环境变量设置页保存：

~~~
FEISHU_APP_ID
FEISHU_APP_SECRET
FEISHU_BITABLE_URL
~~~

FEISHU_BITABLE_URL 必须是“剧情”视图的完整 URL，并且包含 table= 和 view=。它不是公开仓库配置；不要把它、App Secret、tenant_access_token、Cookie 或附件 token 提交进 Git。

程序会用 App ID 和 App Secret 临时换取 tenant_access_token；token 只保留在进程内存，并且不会打印到日志。

## 首次验收顺序

~~~bash
omni-phase1 --config .workbuddy/job-config.json check-config
omni-phase1 --config .workbuddy/job-config.json bootstrap --apply
omni-phase1 --config .workbuddy/job-config.json plan
~~~

- check-config：只读验证 App、剧情 view 和业务字段。
- bootstrap --apply：仅创建缺失的 ClipFlow 文本状态字段；不改任何业务行。
- plan：只读列出准备执行、需 HOLD、已完成、忙碌等行。

这些三步成功后，drain --apply 才有权限进行单行媒体处理、附件追加和回填。
