"""
Layer 3：执行层
================
滑点模拟 + 部分成交 + 延迟执行模型
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecutionParams:
    """执行参数"""
    slippage_bps: int = 20       # 滑点（基点），默认20bps = 0.2%
    partial_fill_prob: float = 0.7  # 部分成交概率
    delay_seconds: int = 5           # 延迟执行（模拟报单延迟）


@dataclass
class ExecutionResult:
    """执行结果"""
    order_id: str
    action: str       # BUY / SELL
    intended_price: float
    filled_price: float
    slippage_bps: float
    qty_filled: int
    qty_intended: int
    partial: bool
    delay_seconds: float


def simulate_slippage(price: float, slippage_bps: int = 20) -> float:
    """模拟滑点（对买方偏高报，对卖方偏低报）"""
    return round(price * (1 + slippage_bps / 10000), 2)


def simulate_partial_fill(qty_intended: int, prob: float = 0.7) -> int:
    """模拟部分成交"""
    import random
    if random.random() > prob:
        return qty_intended  # 全部成交
    else:
        return int(qty_intended * random.uniform(0.3, 0.9) / 100) * 100  # 30%~90%成交，按手取整


def simulate_execution(action: str, price: float, qty: int,
                      params: ExecutionParams = None) -> ExecutionResult:
    """
    模拟执行（考虑滑点+部分成交+延迟）
    """
    import random
    import time as time_module
    from datetime import datetime

    if params is None:
        params = ExecutionParams()

    # 1. 延迟
    time_module.sleep(params.delay_seconds)

    # 2. 滑点
    slip = params.slippage_bps
    if action == "BUY":
        filled_price = simulate_slippage(price, slip)
    else:
        filled_price = simulate_slippage(price, -slip)  # 卖方滑点为负

    # 3. 部分成交
    qty_filled = simulate_partial_fill(qty, params.partial_fill_prob)
    partial = qty_filled < qty

    # 4. 生成订单号
    order_id = f"{action[0]}{datetime.now().strftime('%Y%m%d%H%M%S')}{random.randint(100,999)}"

    return ExecutionResult(
        order_id=order_id,
        action=action,
        intended_price=price,
        filled_price=filled_price,
        slippage_bps=slip,
        qty_filled=qty_filled,
        qty_intended=qty,
        partial=partial,
        delay_seconds=params.delay_seconds
    )


def format_execution_report(results: list) -> str:
    """格式化执行报告"""
    lines = ["\n📋 执行报告"]
    for r in results:
        slip_str = f"{r.slippage_bps:+d}bps" if r.slippage_bps != 0 else "无滑点"
        partial_str = f"[部分成交 {r.qty_filled}/{r.qty_intended}]" if r.partial else f"[全部成交 {r.qty_filled}]"
        lines.append(
            f"  {r.action} {r.intended_price:.2f} → {r.filled_price:.2f} "
            f"{slip_str} {partial_str} 延迟{r.delay_seconds}s"
        )
    return "\n".join(lines)
