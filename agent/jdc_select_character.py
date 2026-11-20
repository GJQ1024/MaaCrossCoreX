import json
import os
import re
import time
from difflib import SequenceMatcher
from typing import Optional

from maa.custom_action import CustomAction
from maa.context import Context


# ============================================================
# 基础配置
# ============================================================

JDC_TARGET = {
    "dps": 100,
    "np": 90,
    "heal": 70,
    "buff": 60
}

JDC_MODES = {
    "dps": "sum",
    "np": "sum",
    "heal": "sum",
    "buff": "sum"
}

JDC_PRIORITIES = {
    "dps": 1,
    "np": 2,
    "heal": 3,
    "buff": 4
}

JDC_INCLUDE_DISABLED = False

JDC_NAME_MATCH_RATIO = 0.50

# OCR 易混字形组：同组字符在模糊匹配里按近似等价处理。
# 实机证据：角色名「赤溟」被 OCR 读成「赤溪」，两者与「赤夕」的
# 编辑相似度同为 0.50，旧逻辑按列表顺序先命中「赤夕」导致匹配错人。
JDC_NAME_CONFUSABLE_GROUPS = (
    "溟溪",
)

# 等长逐位比对时，易混字形按此权重计入（同字符为 1.0，其他为 0.0）
JDC_NAME_CONFUSABLE_WEIGHT = 0.85


# ============================================================
# TAG 路线配置
#
# 同分时：
# 额外 > 持续 > 反击 > 群体
# ============================================================

JDC_TAG_PRIORITIES = {
    "extra": 1,
    "dot": 2,
    "ret": 3,
    "aoe": 4
}

JDC_TAG_LABELS = {
    "extra": "额外",
    "dot": "持续",
    "ret": "反击",
    "aoe": "群体"
}


# ============================================================
# 流程阶段
# ============================================================

JDC_PHASE_SELECT = "select"
JDC_PHASE_BUILD_TEAM = "build_team"
JDC_PHASE_ROUTE = "route"
JDC_PHASE_BATTLE = "battle"
JDC_PHASE_SELECT_EXTRA = "select_extra"
JDC_PHASE_COMPLETE = "complete"


# ============================================================
# 文件
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

CHARACTER_JSON_PATH = os.path.join(
    BASE_DIR,
    "characters.json"
)

JDC_STATE_FILE = os.path.join(
    BASE_DIR,
    "jdc_state.json"
)


# ============================================================
# 角色数据
# ============================================================

def load_characters():
    with open(
        CHARACTER_JSON_PATH,
        "r",
        encoding="utf-8"
    ) as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(
            "characters.json 顶层必须为数组"
        )

    return data


characters = load_characters()


# ============================================================
# 共享运行状态
# ============================================================

jdc_phase = JDC_PHASE_SELECT

jdc_groups = []

jdc_preliminary = []

jdc_round = 0

jdc_final_team = []


# TAG

jdc_route_scores = {
    "extra": 0.0,
    "dot": 0.0,
    "ret": 0.0,
    "aoe": 0.0
}

jdc_route_tags = []

jdc_route_names = []


# 推关

jdc_route_last_round = 0

jdc_route_last_tag = None

jdc_route_current_stars = 0

jdc_route_total_stars = 27

jdc_route_history = []


# 入场次数（角斗场免费次数最多累计 3 次）
# entry_count=0 表示默认行为（1 次）
jdc_entry_count = 0

jdc_entry_until_empty = False

jdc_entry_done = 0

JDC_MAX_FREE_ENTRIES = 3


# ============================================================
# 名字清洗
# ============================================================

def normalize_character_name(
    name: str
) -> str:

    if not name:
        return ""

    text = str(name).strip()

    remove_chars = [
        "·",
        "・",
        "•",
        "‧",
        "∙",
        "⋅",
        "丶",
        ".",
        "．",
        "。",
        " ",
        "\t",
        "\n",
        "\r"
    ]

    for ch in remove_chars:
        text = text.replace(
            ch,
            ""
        )

    # 等级
    for word in [
        "等级",
        "LV",
        "Lv",
        "lv"
    ]:
        text = text.replace(
            word,
            ""
        )

    # 去数字
    text = "".join(
        ch
        for ch in text
        if not ch.isdigit()
    )

    return text


CHARACTER_NAME_MAP = {
    normalize_character_name(
        c.get("name", "")
    ): c.get("name", "")
    for c in characters
    if c.get("name")
}


# ============================================================
# OCR角色名匹配
# ============================================================

def character_glyph_similarity(
    a: str,
    b: str
) -> float:

    if a == b:
        return 1.0

    for group in JDC_NAME_CONFUSABLE_GROUPS:

        if a in group and b in group:
            return JDC_NAME_CONFUSABLE_WEIGHT

    return 0.0


def name_match_score(
    ocr_name: str,
    real_name: str
):
    """返回 (综合得分, 编辑相似度, 逐位字形相似度)。

    综合得分 = max(编辑相似度, 逐位字形相似度)：
    等长时逐位比对能识别「溟/溪」这类 OCR 易混字形，
    从而在编辑相似度打平（如 0.50）时选对候选。
    """

    ratio = SequenceMatcher(
        None,
        ocr_name,
        real_name
    ).ratio()

    # 长度不同没有逐位语义，直接按编辑相似度
    if (
        not ocr_name
        or
        len(ocr_name) != len(real_name)
    ):
        return ratio, ratio, 0.0

    positional = sum(
        character_glyph_similarity(a, b)
        for a, b in zip(
            ocr_name,
            real_name
        )
    ) / len(ocr_name)

    return max(ratio, positional), ratio, positional


def match_character_name(
    ocr_name: str,
    min_ratio: float = JDC_NAME_MATCH_RATIO
) -> Optional[str]:

    if not ocr_name:
        return None

    raw = str(
        ocr_name
    ).strip()

    if not raw:
        return None

    # 原文完全匹配
    for c in characters:

        real_name = c.get(
            "name",
            ""
        )

        if real_name == raw:
            return real_name

    normalized = normalize_character_name(
        raw
    )

    # 标准化完全匹配
    if normalized in CHARACTER_NAME_MAP:

        real_name = (
            CHARACTER_NAME_MAP[
                normalized
            ]
        )

        if real_name != raw:

            print(
                "[角斗场] OCR角色名标准化："
                f"{raw} -> {real_name}"
            )

        return real_name

    # 模糊匹配
    best_name = None
    best_score = 0.0
    best_ratio = 0.0
    best_positional = 0.0

    for c in characters:

        real_name = c.get(
            "name",
            ""
        )

        real_normalized = (
            normalize_character_name(
                real_name
            )
        )

        if not real_normalized:
            continue

        score, ratio, positional = (
            name_match_score(
                normalized,
                real_normalized
            )
        )

        if score > best_score:

            best_score = score
            best_ratio = ratio
            best_positional = positional
            best_name = real_name

    if (
        best_name is not None
        and
        best_score >= min_ratio
    ):

        print(
            "[角斗场] OCR角色名模糊匹配："
            f"{raw} -> {best_name} "
            f"(相似度 {best_score:.2f}，"
            f"编辑 {best_ratio:.2f}，"
            f"字形 {best_positional:.2f})"
        )

        return best_name

    print(
        "[角斗场] OCR角色名无法匹配："
        f"{raw} "
        f"(最高相似度 {best_score:.2f})"
    )

    return None


def find_character(
    name: str
):

    matched = match_character_name(
        name
    )

    if matched is None:
        return None

    for c in characters:

        if c.get("name") == matched:
            return c

    return None


# ============================================================
# 属性算法
# ============================================================

def total_stats(
    team
):

    result = {
        "dps": 0.0,
        "np": 0.0,
        "heal": 0.0,
        "buff": 0.0
    }

    for c in team:

        for key in result:

            result[key] += float(
                c.get(
                    key,
                    0
                )
                or
                0
            )

    return result


def average_stats(
    team
):

    if not team:

        return {
            "dps": 0.0,
            "np": 0.0,
            "heal": 0.0,
            "buff": 0.0
        }

    total = total_stats(
        team
    )

    return {
        key: value / len(team)
        for key, value
        in total.items()
    }


def team_metric(
    team,
    key,
    mode
):

    if mode == "avg":

        return average_stats(
            team
        )[key]

    return total_stats(
        team
    )[key]


def priority_factor(
    priority
):

    return (
        1
        +
        (
            4
            -
            priority
        )
        *
        0.06
    )


# ============================================================
# 持久化
# ============================================================

def save_jdc_state():

    data = {
        "phase": jdc_phase,

        "round": jdc_round,

        "groups": list(
            jdc_groups
        ),

        "preliminary": [
            c["name"]
            for c
            in jdc_preliminary
        ],

        "final_team": [
            c["name"]
            for c
            in jdc_final_team
        ],

        "route_scores": dict(
            jdc_route_scores
        ),

        "route_tags": list(
            jdc_route_tags
        ),

        "route_names": list(
            jdc_route_names
        ),

        "last_stage_round": (
            jdc_route_last_round
        ),

        "last_stage_tag": (
            jdc_route_last_tag
        ),

        "stars": (
            jdc_route_current_stars
        ),

        "stars_total": (
            jdc_route_total_stars
        ),

        "route_history": list(
            jdc_route_history
        ),

        "entry_count": jdc_entry_count,

        "entry_until_empty": (
            jdc_entry_until_empty
        ),

        "entry_done": jdc_entry_done
    }

    temp_file = (
        JDC_STATE_FILE
        +
        ".tmp"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        temp_file,
        JDC_STATE_FILE
    )

    print(
        "[角斗场] 状态已保存："
        f"phase={jdc_phase} "
        f"round={jdc_round} "
        f"角色={len(jdc_preliminary)} "
        f"stars={jdc_route_current_stars}"
    )


def load_jdc_state():

    global jdc_phase

    global jdc_round
    global jdc_groups
    global jdc_preliminary

    global jdc_final_team

    global jdc_route_scores
    global jdc_route_tags
    global jdc_route_names

    global jdc_route_last_round
    global jdc_route_last_tag

    global jdc_route_current_stars
    global jdc_route_total_stars

    global jdc_route_history

    global jdc_entry_count
    global jdc_entry_until_empty
    global jdc_entry_done

    if not os.path.exists(
        JDC_STATE_FILE
    ):

        print(
            "[角斗场] 无历史状态"
        )

        return False

    try:

        with open(
            JDC_STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        jdc_phase = data.get(
            "phase",
            JDC_PHASE_SELECT
        )

        jdc_round = int(
            data.get(
                "round",
                0
            )
        )

        jdc_groups = list(
            data.get(
                "groups",
                []
            )
        )

        # 已获得角色
        restored = []

        for name in data.get(
            "preliminary",
            []
        ):

            c = find_character(
                name
            )

            if c is not None:
                restored.append(c)

        jdc_preliminary = restored

        # 最终5人
        restored_final = []

        for name in data.get(
            "final_team",
            []
        ):

            c = find_character(
                name
            )

            if c is not None:
                restored_final.append(c)

        jdc_final_team = (
            restored_final
        )

        # TAG
        jdc_route_scores = dict(
            data.get(
                "route_scores",
                {
                    "extra": 0.0,
                    "dot": 0.0,
                    "ret": 0.0,
                    "aoe": 0.0
                }
            )
        )

        jdc_route_tags = list(
            data.get(
                "route_tags",
                []
            )
        )

        jdc_route_names = list(
            data.get(
                "route_names",
                []
            )
        )

        # 推关
        jdc_route_last_round = int(
            data.get(
                "last_stage_round",
                0
            )
        )

        jdc_route_last_tag = data.get(
            "last_stage_tag"
        )

        jdc_route_current_stars = int(
            data.get(
                "stars",
                0
            )
        )

        jdc_route_total_stars = int(
            data.get(
                "stars_total",
                27
            )
        )

        jdc_route_history = list(
            data.get(
                "route_history",
                []
            )
        )

        jdc_entry_count = int(
            data.get(
                "entry_count",
                0
            )
            or
            0
        )

        jdc_entry_until_empty = bool(
            data.get(
                "entry_until_empty",
                False
            )
        )

        jdc_entry_done = int(
            data.get(
                "entry_done",
                0
            )
            or
            0
        )

        print(
            "[角斗场] 状态恢复成功："
            f"phase={jdc_phase}"
        )

        print(
            "[角斗场] 已获得角色："
            +
            str(
                [
                    c["name"]
                    for c
                    in jdc_preliminary
                ]
            )
        )

        print(
            "[角斗场] 最终队伍："
            +
            str(
                [
                    c["name"]
                    for c
                    in jdc_final_team
                ]
            )
        )

        print(
            "[角斗场] 路线："
            +
            " > ".join(
                jdc_route_names
            )
        )

        return True

    except Exception as e:

        print(
            "[角斗场] 状态恢复失败："
            +
            repr(e)
        )

        return False


def reset_jdc_state():

    global jdc_phase

    global jdc_round
    global jdc_groups
    global jdc_preliminary

    global jdc_final_team

    global jdc_route_scores
    global jdc_route_tags
    global jdc_route_names

    global jdc_route_last_round
    global jdc_route_last_tag

    global jdc_route_current_stars
    global jdc_route_total_stars

    global jdc_route_history

    global jdc_entry_count
    global jdc_entry_until_empty
    global jdc_entry_done

    jdc_phase = (
        JDC_PHASE_SELECT
    )

    jdc_round = 0

    jdc_groups = []

    jdc_preliminary = []

    jdc_final_team = []

    jdc_route_scores = {
        "extra": 0.0,
        "dot": 0.0,
        "ret": 0.0,
        "aoe": 0.0
    }

    jdc_route_tags = []

    jdc_route_names = []

    jdc_route_last_round = 0

    jdc_route_last_tag = None

    jdc_route_current_stars = 0

    jdc_route_total_stars = 27

    jdc_route_history = []

    jdc_entry_count = 0

    jdc_entry_until_empty = False

    jdc_entry_done = 0

    if os.path.exists(
        JDC_STATE_FILE
    ):

        os.remove(
            JDC_STATE_FILE
        )

    print(
        "[角斗场] 状态已重置"
    )


# ============================================================
# 入场次数控制
# ============================================================

def set_entry_config(
    count,
    until_empty
):
    """写入本次任务的入场配置（在导航前落盘，供后续轮次读取）。"""

    global jdc_entry_count
    global jdc_entry_until_empty
    global jdc_entry_done

    try:

        count = int(
            str(count).strip()
            or
            0
        )

    except Exception:

        count = 0

    if count < 0:

        count = 0

    jdc_entry_count = count

    jdc_entry_until_empty = bool(
        until_empty
    )

    jdc_entry_done = 0

    save_jdc_state()

    if jdc_entry_until_empty:

        mode = (
            "清空免费次数"
            f"（最多 {JDC_MAX_FREE_ENTRIES} 次）"
        )

    else:

        mode = (
            f"指定 {count if count > 0 else 1} 次"
        )

    print(
        "[角斗场] 入场配置："
        + mode
    )


def reset_run_progress():
    """清空单次入场的推关进度与角色池，保留入场次数配置。

    每次「全部领取」代表一次入场结束，下一次入场的回合从 1 重新开始；
    若不清 last_round，第二次入场会把回合1 全部当成已打过而不点，空转到失败。
    """

    global jdc_phase

    global jdc_round
    global jdc_groups
    global jdc_preliminary
    global jdc_final_team

    global jdc_route_scores
    global jdc_route_tags
    global jdc_route_names

    global jdc_route_last_round
    global jdc_route_last_tag

    global jdc_route_current_stars
    global jdc_route_total_stars

    global jdc_route_history

    jdc_phase = (
        JDC_PHASE_SELECT
    )

    jdc_round = 0

    jdc_groups = []

    jdc_preliminary = []

    jdc_final_team = []

    jdc_route_scores = {
        "extra": 0.0,
        "dot": 0.0,
        "ret": 0.0,
        "aoe": 0.0
    }

    jdc_route_tags = []

    jdc_route_names = []

    jdc_route_last_round = 0

    jdc_route_last_tag = None

    jdc_route_current_stars = 0

    jdc_route_total_stars = 27

    jdc_route_history = []

    print(
        "[角斗场] 单次入场进度已清空（回合/角色池/路线重置，入场次数保留）"
    )


def record_entry_done():
    """一轮打完（全部领取）后记一次；返回累计已打次数。"""

    global jdc_entry_done

    jdc_entry_done += 1

    reset_run_progress()

    save_jdc_state()

    print(
        "[角斗场] 已完成入场次数："
        f"{jdc_entry_done}"
        f"（上限："
        + (
            str(JDC_MAX_FREE_ENTRIES)
            if jdc_entry_until_empty
            else
            str(jdc_entry_count if jdc_entry_count > 0 else 1)
        )
        + "）"
    )

    return jdc_entry_done


def need_next_entry():
    """按配置判断是否还要再入场。"""

    if jdc_entry_done >= JDC_MAX_FREE_ENTRIES:

        print(
            "[角斗场] 已达到免费次数上限"
            f" {JDC_MAX_FREE_ENTRIES}，不再入场"
        )

        return False

    if jdc_entry_until_empty:

        return True

    limit = (
        jdc_entry_count
        if jdc_entry_count > 0
        else
        1
    )

    return jdc_entry_done < limit


# ============================================================
# 三选一评分
# ============================================================

def preliminary_candidate_score(
    candidate,
    preliminary,
    target,
    modes,
    priorities
):

    keys = [
        "dps",
        "np",
        "heal",
        "buff"
    ]

    labels = {
        "dps": "DPS",
        "np": "NP",
        "heal": "Heal",
        "buff": "Buff"
    }

    deficit = {}

    for key in keys:

        current = team_metric(
            preliminary,
            key,
            modes[key]
        )

        goal = max(
            0.0,
            float(
                target.get(
                    key,
                    0
                )
            )
        )

        if (
            goal > 0
            and
            current < goal
        ):

            deficit[key] = (
                goal
                -
                current
            ) / goal

        else:

            deficit[key] = 0.0

    score = 0.0

    strong = []
    perfect = []

    for key in keys:

        value = float(
            candidate.get(
                key,
                0
            )
            or
            0
        )

        factor = priority_factor(
            priorities[key]
        )

        if value >= 90:

            factor = 1.16

            strong.append(
                f"{labels[key]} {value:g}"
            )

        if value >= 100:

            perfect.append(
                f"{labels[key]} 100"
            )

        score += (
            value
            *
            factor
        )

        if deficit[key] > 0:

            score += (
                value
                *
                (
                    0.35
                    +
                    deficit[key]
                    *
                    0.9
                )
            )

        if 90 <= value < 100:

            score += (
                18
                +
                (
                    value - 90
                )
                *
                1.8
            )

        if value >= 100:

            score += 65

    values = sorted(
        [
            float(
                candidate.get(
                    key,
                    0
                )
                or
                0
            )
            for key in keys
        ],
        reverse=True
    )

    score += (
        values[0]
        *
        0.18
        +
        values[1]
        *
        0.08
    )

    if perfect:

        reason = (
            "100分特长："
            +
            " / ".join(
                perfect
            )
        )

    elif strong:

        reason = (
            "高分特长："
            +
            " / ".join(
                strong
            )
        )

    else:

        biggest_need = max(
            keys,
            key=lambda k: (
                deficit[k]
            )
        )

        if deficit[
            biggest_need
        ] > 0:

            reason = (
                "综合评分 + 补强 "
                +
                labels[
                    biggest_need
                ]
            )

        else:

            reason = "综合评分"

    return {
        "score": score,
        "reason": reason
    }


def select_current_round(
    names
):

    used = {
        c["name"]
        for c
        in jdc_preliminary
    }

    candidates = []

    for name in names:

        c = find_character(
            name
        )

        if c is None:
            continue

        if c["name"] in used:
            continue

        if (
            not JDC_INCLUDE_DISABLED
            and
            not c.get(
                "isEnable",
                False
            )
        ):
            continue

        candidates.append(
            c
        )

    fallback = False

    if not candidates:

        fallback = True

        for name in names:

            c = find_character(
                name
            )

            if c is None:
                continue

            if c["name"] in used:
                continue

            candidates.append(
                c
            )

    if not candidates:
        return None

    current_big_dps = len(
        [
            c
            for c
            in jdc_preliminary
            if float(
                c.get(
                    "dps",
                    0
                )
            )
            >=
            85
        ]
    )

    current_heal = team_metric(
        jdc_preliminary,
        "heal",
        JDC_MODES[
            "heal"
        ]
    )

    heal_goal = float(
        JDC_TARGET[
            "heal"
        ]
    )

    heal_unmet = (
        heal_goal > 0
        and
        current_heal < heal_goal
    )

    dps100 = [
        c
        for c in candidates
        if float(
            c.get(
                "dps",
                0
            )
        )
        >=
        100
    ]

    dps85 = [
        c
        for c in candidates
        if float(
            c.get(
                "dps",
                0
            )
        )
        >=
        85
    ]

    heal100 = [
        c
        for c in candidates
        if float(
            c.get(
                "heal",
                0
            )
        )
        >=
        100
    ]

    heal90 = [
        c
        for c in candidates
        if float(
            c.get(
                "heal",
                0
            )
        )
        >=
        90
    ]

    pool = candidates

    special_rule = ""

    if (
        current_big_dps >= 2
        and
        heal_unmet
        and
        heal100
    ):

        pool = heal100

        special_rule = (
            "已有2个大C，Heal100硬补强"
        )

    elif (
        current_big_dps >= 2
        and
        heal_unmet
        and
        heal90
    ):

        pool = heal90

        special_rule = (
            "已有2个大C，Heal90+优先"
        )

    elif (
        current_big_dps < 2
        and
        dps100
    ):

        pool = dps100

        special_rule = (
            "DPS100硬优先"
        )

    elif (
        current_big_dps < 2
        and
        dps85
    ):

        pool = dps85

        special_rule = (
            "DPS85+主C优先"
        )

    best = None

    for c in pool:

        detail = (
            preliminary_candidate_score(
                c,
                jdc_preliminary,
                JDC_TARGET,
                JDC_MODES,
                JDC_PRIORITIES
            )
        )

        score = float(
            detail[
                "score"
            ]
        )

        if special_rule == (
            "DPS100硬优先"
        ):

            score += 500

        elif special_rule == (
            "DPS85+主C优先"
        ):

            score += 180

        elif special_rule == (
            "已有2个大C，Heal100硬补强"
        ):

            score += 600

        elif special_rule == (
            "已有2个大C，Heal90+优先"
        ):

            score += 260

        reason = detail[
            "reason"
        ]

        if special_rule:

            reason = (
                special_rule
                +
                "；"
                +
                reason
            )

        if (
            best is None
            or
            score > best[
                "score"
            ]
        ):

            best = {
                "character": c,
                "score": score,
                "reason": reason,
                "fallback": fallback
            }

    if best is None:
        return None

    selected_name = (
        best[
            "character"
        ][
            "name"
        ]
    )

    return {
        "selectedName": (
            selected_name
        ),

        "index": names.index(
            selected_name
        ),

        "score": (
            best[
                "score"
            ]
        ),

        "reason": (
            best[
                "reason"
            ]
        ),

        "character": (
            best[
                "character"
            ]
        ),

        "fallback": (
            best[
                "fallback"
            ]
        )
    }


# ============================================================
# OCR
# ============================================================

def extract_ocr_text(
    detail
):

    if detail is None:
        return None

    if not getattr(
        detail,
        "hit",
        False
    ):
        return None

    best = getattr(
        detail,
        "best_result",
        None
    )

    if best is not None:

        text = getattr(
            best,
            "text",
            None
        )

        if text:

            return str(
                text
            ).strip()

        content = getattr(
            best,
            "content",
            None
        )

        if content:

            return str(
                content
            ).strip()

    return None


def ocr_character_name_box(
    context,
    image,
    roi
):
    """
    返回 (文本, 名字框)。

    名字框为整图坐标 [x, y, w, h]；
    拖拽起点必须取该框中心，而不是槽位几何中心，
    否则列表重排后「名字→坐标」的对应关系会失效。
    """

    detail = (
        context.run_recognition(
            "角斗场_角色OCR",
            image,
            pipeline_override={
                "角斗场_角色OCR": {
                    "recognition": "OCR",
                    "expected": ".+",
                    "roi": roi
                }
            }
        )
    )

    text = extract_ocr_text(
        detail
    )

    if not text:
        return None, None

    best = getattr(
        detail,
        "best_result",
        None
    )

    box = (
        getattr(
            best,
            "box",
            None
        )
        if best is not None
        else None
    )

    coords = None

    if box is not None:

        x = getattr(box, "x", None)
        y = getattr(box, "y", None)
        w = getattr(box, "w", None)
        h = getattr(box, "h", None)

        if None not in (
            x,
            y,
            w,
            h
        ):

            coords = (
                int(x),
                int(y),
                int(w),
                int(h)
            )

        elif isinstance(
            box,
            (
                list,
                tuple
            )
        ) and len(box) >= 4:

            coords = tuple(
                int(v)
                for v
                in box[:4]
            )

    return text, coords


def ocr_character_name(
    context,
    image,
    roi
):

    text, _ = ocr_character_name_box(
        context,
        image,
        roi
    )

    return text


# 2026-11-09 02:38 实机证据：三张卡的名字用「单卡窄 ROI」做 OCR 时，
# 右侧「乌琳」连续 3 次被读成「坏」（相似度 0.00）；
# 而同一帧上 maafw 用大 ROI（如 [830,70,420,310]）做整页 OCR 时，
# 「乌琳」在 [893,300,27,14] 处被正确识别（score 0.86）。
# 因此名字识别改为：先整行 OCR（上下文更大），再按 x 归属到左/中/右卡；
# 某张卡整行里没有可用结果时，才退回单卡窄 ROI。
JDC_NAME_ROW_ROI = [150, 270, 1000, 80]

JDC_NAME_SLOT_X = [450.0, 800.0]

# 2026-11-09 09:32 实机（on_error 帧）：左卡角色名是单字「拉」，窄 ROI
# [182,296,159,30] 读成「打」（与角色表相似度 0.00）；同一帧改用宽 ROI
# [160,270,240,60] 时读回「拉」（score 1.000）。
# 因此窄 ROI 读不过角色表时，用同卡宽 ROI 再做一次 OCR；仍读不过才按未识别处理。
JDC_WIDE_NAME_ROIS = [
    [160, 270, 240, 60],
    [500, 270, 240, 60],
    [860, 270, 260, 60]
]

# 2026-11-09 08:29/08:45 实机证据（on_error 帧 + 日志）：
# 选满第 8 人后页面切到「已选择角色（8/8）」的队伍视图，但节点锚点仍命中，
# 名字行 OCR 读到的是队伍成员与等级残留（"1"、"80"、"LV"），不是三张候选卡；
# 左卡窄 ROI 又读出 "1"，被当成名字 → 3 次重试后硬失败。
# 因此先读标题里的选人数：满编就不再当三选一处理，直接放行给下游（编队/入场）。
JDC_TITLE_ROI = [500, 55, 400, 45]

JDC_TEAM_CAPACITY = 8

JDC_TITLE_COUNT_RE = re.compile(r"[（(]\s*(\d+)\s*[/／]\s*(\d+)\s*[)）]")


def read_selected_count(
    context,
    image
):
    """读标题「已选择角色（N/M）」里的人数与上限；读不到返回 None。

    2026-11-09 09:16 实机：标题是「已选择角色（2/6）」——本轮容量是 6，不是 8，
    所以上限必须从标题取，不能写死。
    """

    detail = context.run_recognition(
        "角斗场_角色OCR",
        image,
        pipeline_override={
            "角斗场_角色OCR": {
                "recognition": "OCR",
                "expected": ".+",
                "roi": JDC_TITLE_ROI
            }
        }
    )

    if detail is None:
        return None

    texts = [
        str(getattr(item, "text", "") or "")
        for item in (getattr(detail, "all_results", None) or [])
    ]

    best = getattr(detail, "best_result", None)

    if best is not None:
        texts.append(
            str(getattr(best, "text", "") or "")
        )

    for text in texts:
        m = JDC_TITLE_COUNT_RE.search(text)

        if m:
            print(f"[角斗场] 标题读数：{text!r} -> {m.group(1)}/{m.group(2)}")
            return int(m.group(1)), int(m.group(2))

    print(f"[角斗场] 标题读数：未读到 N/M（原始={texts!r}）")
    return None


def ocr_text_candidates(
    context,
    image,
    roi
):

    detail = context.run_recognition(
        "角斗场_角色OCR",
        image,
        pipeline_override={
            "角斗场_角色OCR": {
                "recognition": "OCR",
                "expected": ".+",
                "roi": roi
            }
        }
    )

    if detail is None:
        return []

    results = []

    for item in (
        getattr(
            detail,
            "all_results",
            None
        )
        or
        []
    ):

        text = str(
            getattr(
                item,
                "text",
                ""
            )
            or
            ""
        ).strip()

        if not text:
            continue

        box = getattr(
            item,
            "box",
            None
        )

        center_x = None

        if box is not None:

            try:
                center_x = (
                    float(box[0])
                    +
                    float(box[2]) / 2.0
                )
            except (
                TypeError,
                ValueError,
                IndexError
            ):
                center_x = None

        results.append(
            (
                text,
                center_x
            )
        )

    return results


def slot_of_center_x(
    center_x
):

    if center_x is None:
        return None

    if center_x < JDC_NAME_SLOT_X[0]:
        return 0

    if center_x < JDC_NAME_SLOT_X[1]:
        return 1

    return 2


def resolve_character_names(
    context,
    image,
    param
):

    row_roi = param.get(
        "name_row_roi"
    ) or JDC_NAME_ROW_ROI

    raw_names = [
        None,
        None,
        None
    ]

    for text, center_x in ocr_text_candidates(
        context,
        image,
        row_roi
    ):

        slot = slot_of_center_x(
            center_x
        )

        if slot is None:
            continue

        if raw_names[slot]:
            continue

        if match_character_name(
            text
        ) is None:
            continue

        raw_names[slot] = text

    fallback_rois = [
        param.get("left_name_roi"),
        param.get("middle_name_roi"),
        param.get("right_name_roi")
    ]

    for index, roi in enumerate(
        fallback_rois
    ):

        if raw_names[index]:
            continue

        if not roi:
            continue

        text = ocr_character_name(
            context,
            image,
            roi
        )

        # 窄 ROI 会读到等级/序号之类的残留（实机读到过 "1"），不能直接当名字；
        # 只接受能对上角色表的读数，否则保持 None，由上层按未识别处理。
        if text and match_character_name(text) is not None:
            raw_names[index] = text
            continue

        # 窄 ROI 读不过角色表时，用同卡宽 ROI 二次识别（单字名更稳）；
        # 宽 ROI 会覆盖邻卡边缘，因此仍按中心 x 归属回本槽，且必须过角色表。
        wide_roi = JDC_WIDE_NAME_ROIS[index]
        for wide_text, center_x in ocr_text_candidates(
            context,
            image,
            wide_roi
        ):
            if slot_of_center_x(center_x) != index:
                continue
            if match_character_name(wide_text) is None:
                continue
            print(f"[角斗场] 宽 ROI 补识别 槽位{index}：{wide_text!r}")
            raw_names[index] = wide_text
            break

    return raw_names


def rect_center(
    rect
):

    x, y, w, h = rect

    return (
        int(
            x + w / 2
        ),
        int(
            y + h / 2
        )
    )


# ============================================================
# 三选一 CustomAction
# ============================================================

class JdcSelectCharacter(
    CustomAction
):

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg
    ) -> bool:

        global jdc_round
        global jdc_phase

        try:

            print("")
            print(
                "========================================"
            )

            print(
                "[角斗场] "
                "jdc_select_character 开始"
            )

            # =================================================
            # Agent重启后自动恢复
            # =================================================

            if (
                not jdc_preliminary
                and
                os.path.exists(
                    JDC_STATE_FILE
                )
            ):

                load_jdc_state()

            param = json.loads(
                argv.custom_action_param
            )

            image = (
                context
                .tasker
                .controller
                .post_screencap()
                .wait()
                .get()
            )

            # 满编页仍命中「已选择角色」锚点：此时没有三张候选卡，强行 OCR 只会读到
            # 队伍成员名/等级残留；直接放行给下游（编队入口/入场确认），不再判失败。
            title_count = read_selected_count(
                context,
                image
            )

            if title_count is not None:
                selected_count, capacity = title_count

                if selected_count >= capacity:

                    print(
                        "[角斗场] 队伍已选满 "
                        f"（{selected_count}/{capacity}），"
                        "本页不是三选一，跳过选角"
                    )

                    return True

                print(
                    "[角斗场] 队伍未满 "
                    f"（{selected_count}/{capacity}），按候选页处理"
                )

            # 2026-11-09 02:12:50 实机证据：右侧角色名「乌琳」被 OCR 读成「坏」，
            # 一次失败就整任务失败过于脆弱；界面刚切过来时文字还可能被过渡动画遮住。
            # 这里对三张卡的名字做最多 3 次重识别（每次重新截图），
            # 全部匹配后才继续，仍然匹配不上就明确失败。
            names = []

            unresolved = True

            for attempt in range(3):
                raw_names = resolve_character_names(
                    context,
                    image,
                    param
                )

                print(
                    "[角斗场] OCR原始结果："
                    +
                    str(
                        raw_names
                    )
                )

                names = []

                unresolved = False

                for raw in raw_names:

                    matched = None

                    if raw:
                        matched = (
                            match_character_name(
                                raw
                            )
                        )

                    if matched is None:

                        print(
                            "[角斗场] "
                            f"角色无法匹配：{raw}"
                        )

                        unresolved = True

                    names.append(
                        matched
                    )

                if not unresolved:
                    break

                print(
                    "[角斗场] "
                    f"第{attempt + 1}次角色名未全部匹配，"
                    "重新截图识别"
                )

                time.sleep(
                    1.2
                )

                image = (
                    context
                    .tasker
                    .controller
                    .post_screencap()
                    .wait()
                    .get()
                )

            if unresolved:

                print(
                    "[角斗场] "
                    "角色名连续 3 次未全部匹配，失败"
                )

                return False

            result = (
                select_current_round(
                    names
                )
            )

            if result is None:

                print(
                    "[角斗场] "
                    "没有有效角色可选"
                )

                return False

            confirm_rects = [
                param[
                    "left_confirm"
                ],

                param[
                    "middle_confirm"
                ],

                param[
                    "right_confirm"
                ]
            ]

            index = result[
                "index"
            ]

            click_x, click_y = (
                rect_center(
                    confirm_rects[
                        index
                    ]
                )
            )

            print(
                f"[角斗场] 第{jdc_round + 1}次选角："
                f"{names}"
            )

            print(
                "[角斗场] 选择："
                f"{result['selectedName']}"
            )

            print(
                "[角斗场] 评分："
                f"{result['score']:.1f}"
            )

            print(
                "[角斗场] 原因："
                f"{result['reason']}"
            )

            context.tasker.controller.post_click(
                click_x,
                click_y
            ).wait()

            jdc_groups.append(
                names.copy()
            )

            jdc_preliminary.append(
                result[
                    "character"
                ]
            )

            jdc_round += 1

            # 6人以后出现的选角
            if jdc_round > 6:

                jdc_phase = (
                    JDC_PHASE_SELECT_EXTRA
                )

            else:

                jdc_phase = (
                    JDC_PHASE_SELECT
                )

            save_jdc_state()

            print(
                "[角斗场] 当前累计角色："
                +
                str(
                    [
                        c["name"]
                        for c
                        in jdc_preliminary
                    ]
                )
            )

            print(
                "========================================"
            )

            return True

        except Exception as e:

            print(
                "[角斗场] "
                "jdc_select_character异常："
                +
                repr(e)
            )

            return False