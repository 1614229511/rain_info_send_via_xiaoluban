"""降雨状态机与防抖判定。

本模块只做纯逻辑计算，不联网、不打印、不直接写文件，便于单元测试。

核心状态（与任务约定一致）：
    status           "DRY"（无雨）| "RAINING"（降雨中）
    last_event_time  上次成功推送的 Unix 时间戳（秒）
    dry_counter      连续检测到无雨的次数，用于雨停防抖

附加字段（可选，向后兼容旧状态文件）：
    rain_announced   本轮降雨是否已成功播报“开始下雨”
    last_intensity   最近一次降水强度（mm/h）
    last_description 最近一次彩云分钟级描述
    last_update_time 最近一次评估的 Unix 时间戳
    location         最近一次解析到的位置
"""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from pathlib import Path

# 可感知小雨阈值（mm/h）
DEFAULT_THRESHOLD = 0.08
# 连续 N 次检测到无雨才判定雨停（按 5 分钟周期约 15 分钟）
DEFAULT_DRY_CONFIRM = 3
# 两次“开始下雨”推送的最小间隔（分钟），0 表示不限制
DEFAULT_MIN_PUSH_INTERVAL_MINUTES = 30

EVENT_RAIN_START = "RAIN_START"
EVENT_RAIN_STOP = "RAIN_STOP"

# 推送失败需要回滚的控制字段；遥测字段（强度/描述/位置）保留。
_CONTROL_FIELDS = ("status", "last_event_time", "dry_counter", "rain_announced")


def new_state() -> dict:
    """返回一份全新的初始状态（DRY）。"""
    return {
        "status": "DRY",
        "last_event_time": 0,
        "dry_counter": 0,
        "rain_announced": False,
        "last_intensity": None,
        "last_description": "",
        "last_update_time": 0,
    }


def normalize_state(raw: object) -> dict:
    """把任意读入的 JSON 规范成合法状态，并保留未知字段。"""
    state = new_state()
    if isinstance(raw, dict):
        state.update(raw)

    if state.get("status") not in ("DRY", "RAINING"):
        state["status"] = "DRY"

    try:
        last_event_time = int(state.get("last_event_time") or 0)
    except (TypeError, ValueError):
        last_event_time = 0
    state["last_event_time"] = max(0, last_event_time)

    try:
        dry_counter = int(state.get("dry_counter") or 0)
    except (TypeError, ValueError):
        dry_counter = 0
    state["dry_counter"] = max(0, dry_counter)

    # 旧版本状态文件没有 rain_announced：降雨中视为已播报，避免漏发雨停通知。
    if not isinstance(raw, dict) or "rain_announced" not in raw:
        state["rain_announced"] = state["status"] == "RAINING"
    else:
        state["rain_announced"] = bool(state["rain_announced"])

    return state


def load_state(path: str | os.PathLike[str]) -> dict:
    """读取状态文件；文件不存在或为空时返回初始状态。

    文件损坏（JSONDecodeError）会向上抛出，由调用方决定备份并重建。
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return new_state()
    if not text.strip():
        return new_state()
    return normalize_state(json.loads(text))


def save_state(path: str | os.PathLike[str], state: dict) -> None:
    """原子写入状态文件，避免断电/并发产生半截 JSON。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=False) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        dir=str(file_path.parent), prefix=".runtime_state.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, file_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _safe_intensity(value: object) -> float:
    try:
        intensity = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(intensity) or intensity < 0:
        return 0.0
    return intensity


def evaluate(
    state: dict,
    intensity: object,
    now: int,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    dry_confirm: int = DEFAULT_DRY_CONFIRM,
    min_push_interval_minutes: float = DEFAULT_MIN_PUSH_INTERVAL_MINUTES,
) -> dict:
    """根据当前强度评估一次状态跃迁。

    返回字典：
        state           新的状态对象（不修改入参）
        event           None | "RAIN_START" | "RAIN_STOP"
        push            是否应当推送
        reason          若状态跃迁但不推送，给出原因
        previous_status 跃迁前的状态

    规则：
        DRY     + 强度 >= 阈值            -> RAIN_START
        RAINING + 强度 <  阈值            -> dry_counter + 1
        dry_counter >= dry_confirm        -> RAIN_STOP，计数清零
        强度回到阈值以上                   -> 计数清零，维持 RAINING

    防抖补充：
        * “开始下雨”受冷却时间限制（两次开始通知至少间隔 N 分钟）；
        * 若本次开始通知被冷却抑制，则本轮降雨不视为已播报，
          雨停时也不再发通知，避免“没收到开始却收到结束”的困惑；
        * 雨停通知不受冷却限制，只要开始通知成功发出过就会推送。
    """
    s = copy.deepcopy(state)
    s = normalize_state(s)

    value = _safe_intensity(intensity)
    threshold = max(0.0, float(threshold))
    dry_confirm = max(1, int(dry_confirm))
    try:
        cooldown_seconds = max(0.0, float(min_push_interval_minutes) * 60.0)
    except (TypeError, ValueError):
        cooldown_seconds = 0.0

    previous_status = s["status"]
    s["last_intensity"] = round(value, 3)
    s["last_update_time"] = int(now)

    event: str | None = None
    reason = ""

    if previous_status == "DRY":
        s["dry_counter"] = 0
        if value >= threshold:
            s["status"] = "RAINING"
            s["rain_announced"] = False
            event = EVENT_RAIN_START
    else:
        if value >= threshold:
            s["dry_counter"] = 0
        else:
            s["dry_counter"] += 1
            if s["dry_counter"] >= dry_confirm:
                s["status"] = "DRY"
                s["dry_counter"] = 0
                event = EVENT_RAIN_STOP

    push = False
    if event == EVENT_RAIN_START:
        last_event_time = float(s.get("last_event_time") or 0)
        # last_event_time=0 表示从未推送过，不应被冷却期拦截。
        if (
            cooldown_seconds > 0
            and last_event_time > 0
            and (now - last_event_time) < cooldown_seconds
        ):
            remaining = int((cooldown_seconds - (now - last_event_time)) / 60) + 1
            reason = f"处于推送冷却期（约剩 {remaining} 分钟），本次开始通知被抑制"
            s["rain_announced"] = False
        else:
            push = True
            s["rain_announced"] = True
            s["last_event_time"] = int(now)
    elif event == EVENT_RAIN_STOP:
        if s.get("rain_announced"):
            push = True
            s["last_event_time"] = int(now)
        else:
            reason = "本轮降雨未成功播报开始通知，跳过雨停通知"
        s["rain_announced"] = False

    return {
        "state": s,
        "event": event,
        "push": push,
        "reason": reason,
        "previous_status": previous_status,
    }


def revert_transition(new_state: dict, previous_state: dict) -> dict:
    """推送失败时回滚控制字段，保留最新遥测数据。

    这样下一轮仍会重新检测到同一跃迁并重试推送，而不会把事件“吞掉”。
    """
    reverted = copy.deepcopy(new_state)
    previous = normalize_state(previous_state)
    for field in _CONTROL_FIELDS:
        reverted[field] = previous[field]
    return reverted


def status_label(status: str) -> str:
    return "降雨中" if status == "RAINING" else "无雨"
