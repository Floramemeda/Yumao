# 撸猫续火花

每日撸猫签到：`/撸猫` 签到并让玉猫随机回应，`/火花` 查看进度（仅自己可见）并开关催撸。

## 规则速览

- 每个服务器内、每人每个本地自然日只有第一次撸猫算签到；之后再撸只回话、不加进度。
- **连续火花**：连续签到 2 天点亮；漏签一天即熄灭，`best_streak` 保留历史最高。
- **亲密等级**：只看累计有效签到天数，永不下降。
- **22:00 催撸**：昨天是连续第 ≥2 天、今天还没撸、没关催撸的人，本地 22:00 在水区被 @ 一次。
  22:00 不是截止时间，午夜前撸猫都能保住火花。

## 管理员配置（`config.json`）

| 字段 | 说明 |
|---|---|
| `default_timezone` | 未单独配置的服务器使用的时区（IANA 名称，如 `Asia/Shanghai`），不依赖主机时区 |
| `guilds.<服务器ID>.timezone` | 该服务器的时区 |
| `guilds.<服务器ID>.reminder_enabled` | 是否开启 22:00 催撸 |
| `guilds.<服务器ID>.reminder_channel_id` | 催撸发到的水区频道/子区 ID；不填或不可用时跳过并记日志 |
| `reminder_time` | 催撸时刻，默认 `22:00`（服务器本地时间） |
| `reminder_mentions_per_message` | 每条催撸消息最多 @ 几人，多了自动分条 |
| `levels` | 亲密等级的名称、emoji、门槛（累计天数）、说明 |

没出现在 `guilds` 里的服务器也能撸猫（用默认时区），只是没有催撸。

文案在 `lines.json`：每组（first / checkin / spark_activated / comeback / repeat / meme / reminder）下有若干池，
每个池可设 `enabled`、`weight`、`min_level`（亲密等级解锁）、`entries`。
`meme.songs` 通过 `include_chat_responses` 复用了 `responses.py` 里「玉猫唱歌」的文案（只读引用）。
文案可用占位符：`{mention}`（仅催撸）、`{streak}`、`{level_name}`。

进度存档：`data/pet_spark_state.json`。存档损坏时会改名为 `pet_spark_state.corrupt-<时间戳>.json` 备份后以空进度启动，请人工检查。

## 手动验收

1. `/撸猫`：收到玉猫回应和“今日撸猫成功 · 连续 1 天 · 🌱 Lv.1 初识”；再撸一次提示今天撸过了，天数不变。
2. 点回复下方的「🐾 撸撸玉猫」：以点击者身份签到，与 `/撸猫` 效果相同、不会重复累计。
3. `/火花`：仅自己可见，显示今天、连续火花、历史最高、累计天数、亲密等级；点「关闭催撸」再看状态变为已关闭。
4. 催撸：把某人在存档里的 `last_checkin_local_date` 改为昨天、`streak_count` 改为 2，
   把 `reminder_time` 临时改成几分钟后，重启 Bot，到点后水区出现一次 @；再次重启不会重复 @。
