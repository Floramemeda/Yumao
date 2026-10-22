# 皮套系统（Persona Proxy）

## 指令

| 指令 | 谁能用 | 说明 |
|---|---|---|
| `/profile create` | 有准入身份组、配额未满 | 弹窗填写名称、头像 URL、人设描述 |
| `/profile edit` `/profile list` `/profile delete` | 本人 | 维护自己的皮套（身份组被撤或配额为 0 也能维护，但不能发言） |
| `/say profile [message]` | 有身份组 + 皮套可用 | 填 message 直接发；不填打开长文输入框 |
| `/send profile` | 同上，仅展示频道 | 依次发出人设描述原文、头像 URL |
| `/say-retry draft` / `/say-draft draft` | 本人 | 重发失败的草稿（需在原频道）/ 查看草稿原文 |
| 右键消息 → 应用 →「以皮套回复」 | 同 `/say` | 选皮套 → 输入 → 以皮套发出，正文前带原消息链接（不是 Discord 原生回复） |
| `/grant profile user limit` | 管理服务器权限 | 覆盖设置某人的皮套上限（0–50，默认 1） |
| 右键消息 → 应用 →「皮套查询（管理）」 | 管理服务器权限 | 查某条皮套消息的真实发送者 |

## 配置（`config.json` → `guilds`）

```json
"guilds": {
  "<服务器ID>": {
    "enabled": true,
    "allowed_role_ids": [<准入身份组ID>, ...],
    "unlimited_role_ids": [<不受配额限制的身份组ID>, ...],
    "profile_display_channel_id": <展示频道ID>,
    "creation_log_thread_id": <创建日志子区ID>
  }
}
```

- 没出现在 `guilds` 里、或 `allowed_role_ids` 为空的服务器：**谁都不能用**（fail closed）。
- `unlimited_role_ids`：持有者配额按系统上限（50）计算，`/grant` 对其不生效；只影响配额，**不能**代替准入身份组。
  身份组被撤后回到 `/grant` 设定值或默认 1，超出的皮套按创建时间暂时停用。
- 未配置展示频道：`/send` 友好拒绝；未配置日志子区：照常创建，只在运行日志里报错。

## 玉猫需要的 Discord 权限

- 在要用皮套的频道（子区取其父频道）：**管理 Webhook**、查看频道。
- 日志子区：发消息、嵌入链接。展示频道的头像预览取决于频道的「嵌入链接」权限和图片网址本身。
- 玩家本人必须在该频道/子区有发言权限；皮套不能越过只读频道。

## 数据

- `data/persona_proxy/<服务器ID>.json`：配额、皮套、草稿（保留 30 天）、皮套 Webhook id（不存 token）。
- `data/persona_proxy/audit/<服务器ID>.jsonl`：每条皮套消息的真实发送者；删除皮套不会删除审计。
- 必须放在持久磁盘；仅支持单个玉猫进程。

## 上线前必须人工验收：长文输入框

自动化测试无法模拟真实客户端。请在真实 Discord 里用 `/say` 打开输入框，写一段长文，分别等 **1、10、16、20、30 分钟**后提交，
再试一次「打开输入框 → 重启玉猫 → 提交」，记录每次是否成功发出。
玉猫只能保证"**收到**提交之后不丢"：如果 Discord 客户端自己提交失败（玩家看到"出了点问题"），文字没有到达玉猫，无法恢复。
