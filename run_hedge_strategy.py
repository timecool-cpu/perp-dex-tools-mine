#!/usr/bin/env python3
"""
运行Lighter和Paradex之间的对冲策略
"""

import asyncio
import argparse
from decimal import Decimal
from hedge_strategy import HedgeStrategy
from dotenv import load_dotenv
from types import SimpleNamespace


async def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='运行Lighter和Paradex之间的对冲策略')
    parser.add_argument('--env-file', type=str, default='.env', help='环境变量文件路径（默认 .env）')
    parser.add_argument('--ticker', type=str, default='ETH-PERP', help='交易对名称')
    parser.add_argument('--quantity', type=str, default='0.03', help='交易数量')
    parser.add_argument('--side', type=str, default='buy', choices=['buy', 'sell'], help='在Lighter上的交易方向')
    parser.add_argument('--price-offset-ticks', type=int, default=1, help='BBO价格偏移的tick数量')
    parser.add_argument('--order-timeout', type=int, default=60, help='订单超时取消时间（秒）')
    parser.add_argument('--max-retries', type=int, default=3, help='取消后最大重试次数')
    parser.add_argument('--auto-cancel-enabled', action='store_true', default=True, help='启用自动取消重新挂单')
    parser.add_argument('--price-check-interval', type=int, default=5, help='价格检查间隔（秒）')
    parser.add_argument('--price-tolerance', type=float, default=0.001, help='价格容忍度（0.001 = 0.1%）')
    parser.add_argument('--hedge-cycle-seconds', type=int, default=3600, help='对冲周期（秒），周期开始时挂单，到期取消两边订单')
    parser.add_argument('--hold-time', type=int, default=3900, help='持仓维持时间（秒），默认65分钟=3900秒')
    args = parser.parse_args()

    # 加载环境变量
    try:
        load_dotenv(args.env_file)
    except Exception:
        # 容错：即使加载失败也继续，后续客户端会校验并报错
        pass

    # 配置策略
    # 为不同交易所规范化符号
    base_symbol = args.ticker.split('-')[0] if '-PERP' in args.ticker else args.ticker

    # 基础配置
    config = {
        "ticker": args.ticker,
        "quantity": args.quantity,
        "side": args.side,
        "price_offset_ticks": args.price_offset_ticks,
        "order_timeout_seconds": args.order_timeout,
        "max_retries": args.max_retries,
        "auto_cancel_enabled": args.auto_cancel_enabled,
        "price_check_interval": args.price_check_interval,
        "price_tolerance": args.price_tolerance,
        "hedge_cycle_seconds": args.hedge_cycle_seconds,
        "hold_time_seconds": args.hold_time,
    }

    # 各交易所专用配置（以具名属性对象形式，便于客户端读取）
    # lighter：使用 ticker、tick_size、close_order_side
    lighter_cfg = SimpleNamespace(
        ticker=base_symbol,
        tick_size=Decimal("0.01"),
        close_order_side=args.side
    )
    # paradex：使用 ticker、tick_size、quantity、direction、close_order_side
    paradex_direction = 'sell' if args.side == 'buy' else 'buy'
    paradex_cfg = SimpleNamespace(
        ticker=base_symbol,
        tick_size=Decimal("0.01"),
        quantity=Decimal(str(args.quantity)),
        direction=paradex_direction,
        close_order_side=paradex_direction
    )
    config["lighter"] = lighter_cfg
    config["paradex"] = paradex_cfg
    
    # 创建并运行策略
    strategy = HedgeStrategy(config)
    result = await strategy.run()
    
    if result:
        print(f"对冲策略成功完成: {args.side} {args.quantity} {args.ticker} 在Lighter，并在Paradex上对冲")
    else:
        print("对冲策略执行失败，请查看日志了解详情")


if __name__ == "__main__":
    asyncio.run(main())