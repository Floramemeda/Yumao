"""万象娃娃机：纯规则（不依赖 Discord）。词条、线索、成功率和彩蛋全部来自 config.json。

候选娃娃 = 四个词条（主体 / 姿态 / 玩具类型 / 异常特征）+ 两条与词条无关的线索（抓取点 / 摆放位置）。
词条用稳定键（s01、m01……）保存，同时存一份名称快照，以后词条改名、删除或换顺序，旧娃娃照样能正确显示。
"""
from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).parent

FIELDS = ("subject", "mood", "toy", "quirk")
POOLS = {"subject": "subjects", "mood": "moods", "toy": "toy_types", "quirk": "quirks"}


class ConfigError(ValueError):
    pass


def load_config(path: Path | None = None) -> dict:
    with open(path or _DIR / "config.json", encoding="utf-8") as f:
        return prepare(json.load(f))


def prepare(cfg: dict) -> dict:
    """校验配置并把彩蛋规则里的词条名换成稳定键（存进 cfg["_eggs"]）。配置写错时启动即报错，而不是玩到一半出错。"""
    for field, key in POOLS.items():
        pool = cfg.get(key)
        if not isinstance(pool, dict) or not pool:
            raise ConfigError(f"词条池 {key} 不能为空")
        if len(set(pool.values())) != len(pool):
            raise ConfigError(f"词条池 {key} 有重名词条")
    techs, grips, positions = cfg["techniques"], cfg["grips"], cfg["positions"]
    for g in grips:
        if set(cfg["base_rates"].get(g, {})) != set(techs):
            raise ConfigError(f"base_rates 缺少抓取点 {g} 的完整数据")
    for p in positions:
        if set(cfg["position_modifiers"].get(p, {})) != set(techs):
            raise ConfigError(f"position_modifiers 缺少位置 {p} 的完整数据")
    if not cfg.get("fail_texts"):
        raise ConfigError("fail_texts 不能为空")

    eggs, seen = [], set()
    for rule in cfg.get("easter_eggs", []):
        if rule["id"] in seen:
            raise ConfigError(f"彩蛋 id 重复：{rule['id']}")
        seen.add(rule["id"])
        match = rule.get("match", {})
        if not 2 <= len(match) <= 4 or not set(match) <= set(FIELDS):
            raise ConfigError(f"彩蛋 {rule['id']} 必须指定 2～4 个词条（subject / mood / toy / quirk）")
        ids = {}
        for field, name in match.items():
            by_name = {v: k for k, v in cfg[POOLS[field]].items()}
            if name not in by_name:
                raise ConfigError(f"彩蛋 {rule['id']} 引用了不存在的词条：{name}")
            ids[field] = by_name[name]
        eggs.append({**rule, "ids": ids, "priority": int(rule.get("priority", 0))})
    cfg["_eggs"] = eggs
    return cfg


def roll_candidate(cfg: dict, rng) -> dict:
    """四个词条和两条线索各自独立、均匀抽取。返回稳定键 + 名称快照。"""
    c = {f: rng.choice(sorted(cfg[POOLS[f]])) for f in FIELDS}
    c["grip"] = rng.choice(sorted(cfg["grips"]))
    c["position"] = rng.choice(sorted(cfg["positions"]))
    c["names"] = {f: cfg[POOLS[f]][c[f]] for f in FIELDS}
    return c


def doll_name(names: dict) -> str:
    """[Mood][Subject][Toy Type]；异常特征不进名字。"""
    return f"{names['mood']}{names['subject']}{names['toy']}"


def combo_key(c: dict) -> str:
    return "|".join(c[f] for f in FIELDS)


def chance(cfg: dict, grip: str, position: str, technique: str) -> int:
    """clamp(基础成功率 + 位置修正, rate_min, rate_max)，单位是百分比整数。"""
    raw = cfg["base_rates"][grip][technique] + cfg["position_modifiers"][position][technique]
    return max(cfg["rate_min"], min(cfg["rate_max"], raw))


def match_egg(cfg: dict, c: dict) -> dict | None:
    """在完整四词条上检查彩蛋。多条规则同时命中时，指定词条越多越优先，同样多再看 priority。"""
    hits = [r for r in cfg["_eggs"] if all(c[f] == v for f, v in r["ids"].items())]
    if not hits:
        return None
    return max(hits, key=lambda r: (len(r["ids"]), r["priority"]))
