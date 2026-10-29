"""不给糖就捣蛋：Embed 与文案。玩家优先显示报名时填的万圣节昵称，没填才用 <@id>（配合 AllowedMentions.none()，只显示不 ping）。

公共文案只使用已公开的事实：搜查结果、被排除部位、命中部位。未被找到的藏糖位置只出现在本人的状态里。
"""
from __future__ import annotations

import discord

from .constants import ACTIVE, BODY_PARTS, CLOSED, REGISTRATION, STATE_LABELS, WITHDRAWN, label
from .service import hits, player_view, stats

ORANGE = discord.Color.orange()
PARTS_TOTAL = len(BODY_PARTS)


def nickname_text(nick: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(nick))


def at(event: dict, uid: str) -> str:
    nick = (event["players"].get(uid) or {}).get("nickname")
    return nickname_text(nick) if nick else f"<@{uid}>"


def ats(event: dict, uids, empty: str = "无") -> str:
    return "、".join(at(event, u) for u in uids) or empty


def panel(event: dict) -> discord.Embed:
    lines = [
        "一人一颗糖，藏在七个神秘角落。",
        "每人只有三次机会，搜查记录全员可见。",
        "找到别人，或者被别人找到，都算配对成功！",
        "",
        f"**玩法**：点「🍬 报名藏糖」私密选好藏糖部位；活动开始后用 `/trickortreat search` 搜查别人。",
        "🎭 可以用皮套或任何自定义角色参加：报名时填【万圣节昵称】，播报就显示昵称，不会 @ 你。",
        "-# ⚠️ 自愿参加：含成人向身体部位标签，搜查记录公开；配对只是节日游戏称号，不代表任何现实或 RP 互动要求。",
        "-# 退出：`/trickortreat withdraw`",
    ]
    state = STATE_LABELS[event["state"]]
    if event["state"] == ACTIVE and not event["config"]["allow_late_join"]:
        state += "（不接受补报）"
    e = discord.Embed(title="🎃 不给糖就捣蛋 · 万圣夜寻糖", description="\n".join(lines), color=ORANGE)
    e.set_footer(text=f"当前阶段：{state} · 第 {event['id']} 轮")
    return e


def join_ok(part: str, attempts: int, nickname: str | None = None) -> str:
    text = (f"🍬 藏糖成功！你把糖藏在「{label(part)}」。你拥有 {attempts} 次搜查机会。"
            "藏糖位置不会向别人公开。")
    if nickname:
        text += f"\n🎭 万圣节昵称：{nickname_text(nickname)}（播报会显示这个名字）"
    return text


def search_reply(event: dict, record: dict, me: dict, total: int) -> str:
    """给搜查者本人的私密结果。"""
    where = f"{at(event, record['target'])} 的「{label(record['part'])}」"
    if record["hit"]:
        head = f"🎉 你在 {where} 找到了糖果！{at(event, record['target'])} 成为你的万圣节奖品～"
    else:
        head = f"🍂 你搜查了 {where}……什么都没找到。"
    return f"{head}\n🎟️ 剩余搜查：{me['attempts_remaining']} / {total}"


def announcement(item: dict, event: dict) -> discord.Embed:
    """outbox 公告：内容完全由入队时的快照决定，补发时不会变。footer 带公告编号，供补发前核对。"""
    p = item["payload"]
    if item["kind"] == "search":
        a, b = at(event, p["searcher"]), at(event, p["target"])
        if p["hit"]:
            e = discord.Embed(title="🍬 不给糖就捣蛋 · 找到啦！", color=discord.Color.gold(), description="\n".join([
                f"**{a}** 搜查了 **{b}** 的「{label(p['part'])}」！",
                "🎉 找到了藏起来的糖果！",
                f"🎁 **{b} 成为 {a} 的万圣节奖品！**",
                f"💞 **{a} 与 {b} 均已成功配对**",
                f"🎟️ {a} 剩余搜查：{p['remaining']} / {p['total']}",
            ]))
        else:
            ex = p["excluded"]
            e = discord.Embed(title="🎃 万圣夜 · 搜查失败！", color=ORANGE, description="\n".join([
                f"**{a}** 搜查了 **{b}** 的「{label(p['part'])}」！",
                "🍬 什么都没找到！",
                f"📋 {b} 已排除：{'、'.join(label(x) for x in ex)}（{len(ex)} / {PARTS_TOTAL}）",
                f"🎟️ {a} 剩余搜查：{p['remaining']} / {p['total']}",
            ]))
    else:
        e = result_embed(p, event)
    e.set_footer(text=f"公告 #{item['id']}")
    return e


def result_embed(p: dict, event: dict) -> discord.Embed:
    hunters = (f"{ats(event, p['hunters'])}（{p['hunter_count']} 颗）" if p["hunters"] else "无")
    lines = [
        f"🍬 找到的糖果：**{p['found']}** 颗",
        f"💞 已配对：**{p['matched']}** 人 · 未配对：{p['unmatched']} 人（有效报名 {p['registered']} 人"
        + (f"，退出 {p['withdrawn']} 人" if p["withdrawn"] else "") + "）",
        f"🏆 糖果猎人：{hunters}",
        f"🙈 糖果没被找到：{ats(event, p['unfound'])}",
    ]
    return discord.Embed(title="🎃 万圣夜寻糖 · 最终结果", description="\n".join(lines), color=ORANGE)


def status(event: dict, uid: str) -> str:
    v = player_view(event, uid)
    if v is None:
        return "你还没有报名本轮活动。"
    lines = [f"🍬 你的糖藏在「{label(v['hiding_part'])}」（只有你知道）"]
    if v.get("nickname"):
        lines.append(f"🎭 万圣节昵称：{nickname_text(v['nickname'])}")
    if v["found_by"]:
        lines.append(f"🎁 已被 {at(event, v['found_by'])} 找到")
    else:
        lines.append("🙈 还没被找到")
    lines.append(f"🎟️ 剩余搜查：{v['attempts_remaining']} / {v['attempts_total']}")
    lines.append(f"🍭 搜到的糖果：{len(v['candies'])} 颗" + (f"（{ats(event, v['candies'])}）" if v["candies"] else ""))
    lines.append("💞 已配对成功" if v["matched"] else "💭 还没有配对")
    if v["state"] == WITHDRAWN:
        lines.append("🚪 你已退出本轮，不能再搜查也不会被搜。")
    if v["event_state"] == REGISTRATION:
        lines.append("-# 活动还在报名阶段，开始后才能搜查。")
    elif v["event_state"] == CLOSED:
        lines.append("-# 本轮活动已经结束。")
    return "\n".join(lines)


def board(event: dict) -> discord.Embed:
    st = stats(event)
    rate = f"{st['matched'] / st['registered']:.0%}" if st["registered"] else "—"
    lines = [
        f"👥 报名：**{st['registered']}** 人" + (f"（退出 {st['withdrawn']} 人）" if st["withdrawn"] else ""),
        f"🙈 未被找到的糖：**{len(st['unfound'])}** 颗 · 🍬 已找到：**{st['found']}** 颗",
        f"💞 配对成功：**{st['matched']}** 人（{rate}）",
        f"🎟️ 已用搜查：{st['attempts_used']} / {st['attempts_total']}",
    ]
    if st["exclusions"]:
        lines.append("")
        lines.append("**📋 公开排除线索**")
        for uid, parts in st["exclusions"].items():
            lines.append(f"{at(event, uid)}：{'、'.join(label(x) for x in parts)}（{len(parts)} / {PARTS_TOTAL}）")
    if event["state"] == ACTIVE and st["registered"] and not st["can_continue"]:
        lines.append("")
        lines.append("🔚 已经没有可搜的目标或剩余次数，等待管理员结算。")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n……"
    e = discord.Embed(title="🔎 万圣夜寻糖 · 公共进度", description=text, color=ORANGE)
    e.set_footer(text=f"{STATE_LABELS[event['state']]} · 第 {event['id']} 轮")
    return e


def prizes(event: dict, uid: str) -> str:
    v = player_view(event, uid)
    mine = ""
    if v is not None:
        mine = (f"🎁 你的奖品：{ats(event, v['candies'], '还没有')}\n"
                + (f"🍬 你的糖被 {at(event, v['found_by'])} 找到，你是 TA 的奖品\n" if v["found_by"] else ""))
    rows = [f"• {at(event, s['searcher'])} 🍬→ {at(event, s['target'])}（{label(s['part'])}）" for s in hits(event)]
    allrec = "\n".join(rows[-30:]) if rows else "还没有人找到糖果。"
    if len(rows) > 30:
        allrec = f"……（共 {len(rows)} 条，仅显示最近 30 条）\n" + allrec
    return f"{mine}\n**全部奖品记录**\n{allrec}".strip()


def admin_status(event: dict | None, settings: dict, pending: int, failed: int) -> str:
    entry, ann = settings.get("entry_channel_id"), settings.get("announcement_channel_id")
    lines = [f"📍 报名入口：{f'<#{entry}>' if entry else '未设置'} · 公告频道：{f'<#{ann}>' if ann else '未设置'}",
             f"🧾 补报：{'允许' if settings.get('allow_late_join', True) else '不允许'}（默认值，开新一轮时生效）"]
    if event is None:
        lines.append("现在没有未结束的活动。")
    else:
        st = stats(event)
        lines += [f"🎃 第 {event['id']} 轮 · {STATE_LABELS[event['state']]} · 本轮补报："
                  f"{'允许' if event['config']['allow_late_join'] else '不允许'}",
                  f"👥 报名 {st['registered']} 人（退出 {st['withdrawn']}）· 找到 {st['found']} 颗 · "
                  f"配对 {st['matched']} 人 · 搜查 {st['attempts_used']} / {st['attempts_total']}"]
    lines.append(f"📮 待发公告 {pending} 条 · 发送失败 {failed} 条")
    return "\n".join(lines)
