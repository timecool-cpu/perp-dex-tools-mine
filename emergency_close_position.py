#!/usr/bin/env python3
"""
紧急平仓脚本 - 用于处理仓位累积问题
Emergency Position Close Script - For handling position accumulation issues
"""

import asyncio
import sys
import os
from decimal import Decimal

# Add the project root to the path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from exchanges.factory import ExchangeFactory
from helpers.logger import Logger

async def emergency_close_position():
    """紧急平仓所有仓位"""
    
    # 配置参数 - 请根据你的实际情况修改
    EXCHANGE = "grvt"  # 你的交易所
    TICKER = "ETH"     # 你的交易对
    CONTRACT_ID = f"{TICKER}_USDT_Perp"  # 合约ID
    
    print("🚨 紧急平仓脚本启动")
    print("=" * 50)
    
    try:
        # 创建交易所客户端
        exchange_client = ExchangeFactory.create_exchange(EXCHANGE)
        logger = Logger(f"EMERGENCY_{TICKER}")
        
        print(f"连接到交易所: {EXCHANGE}")
        print(f"交易对: {TICKER}")
        print()
        
        # 检查活跃订单
        print("检查活跃订单...")
        try:
            active_orders = await exchange_client.get_active_orders(CONTRACT_ID)
            if active_orders:
                print(f"发现 {len(active_orders)} 个活跃订单，正在取消...")
                for order in active_orders:
                    try:
                        order_id = order.get('order_id') if isinstance(order, dict) else getattr(order, 'order_id', None)
                        if order_id:
                            print(f"取消订单: {order_id}")
                            await exchange_client.cancel_order(order_id)
                    except Exception as e:
                        print(f"取消订单失败: {e}")
                print("等待订单取消完成...")
                await asyncio.sleep(10)
            else:
                print("✅ 没有活跃订单")
        except Exception as e:
            print(f"检查活跃订单失败: {e}")
        
        # 检查当前仓位
        print("检查当前仓位...")
        current_position = await exchange_client.get_account_positions()
        print(f"当前仓位: {current_position}")
        
        if current_position == 0:
            print("✅ 没有仓位需要平仓")
            return
        
        # 确定平仓方向和数量
        if current_position > 0:
            close_side = "sell"
            close_quantity = abs(current_position)
            print(f"检测到多头仓位: {close_quantity}")
        else:
            close_side = "buy"
            close_quantity = abs(current_position)
            print(f"检测到空头仓位: {close_quantity}")
        
        print(f"平仓方向: {close_side}")
        print(f"平仓数量: {close_quantity}")
        print()
        
        # 获取当前价格
        print("获取当前价格...")
        try:
            bbo = await exchange_client.get_bbo(CONTRACT_ID)
            best_bid = bbo.bid
            best_ask = bbo.ask
            print(f"最佳买价: {best_bid}")
            print(f"最佳卖价: {best_ask}")
        except Exception as e:
            print(f"获取价格失败: {e}")
            best_bid = Decimal('0')
            best_ask = Decimal('0')
        
        # 尝试市价单平仓
        print("\n尝试市价单平仓...")
        if hasattr(exchange_client, 'place_market_order'):
            try:
                market_result = await exchange_client.place_market_order(
                    CONTRACT_ID, close_quantity, close_side
                )
                if market_result and market_result.success:
                    print(f"✅ 市价单平仓成功: {market_result.order_id}")
                    
                    # 等待平仓完成
                    print("等待平仓完成...")
                    await asyncio.sleep(10)
                    
                    # 验证仓位
                    final_position = await exchange_client.get_account_positions()
                    if abs(final_position) < 0.001:
                        print("✅ 仓位已完全平仓")
                        return
                    else:
                        print(f"⚠️ 仓位仍然存在: {final_position}")
                else:
                    print(f"❌ 市价单平仓失败: {market_result.error_message if market_result else 'Unknown error'}")
            except Exception as e:
                print(f"❌ 市价单平仓异常: {e}")
        else:
            print("该交易所不支持市价单")
        
        # 如果市价单失败，尝试限价单
        print("\n尝试限价单平仓...")
        try:
            # 使用更激进的价格
            if close_side == "sell":
                # 卖单使用更低的价格
                close_price = best_bid - Decimal('10')  # 比买价低10
            else:
                # 买单使用更高的价格
                close_price = best_ask + Decimal('10')  # 比卖价高10
            
            print(f"使用激进价格: {close_price}")
            
            limit_result = await exchange_client.place_close_order(
                CONTRACT_ID, close_quantity, close_price, close_side
            )
            
            if limit_result and limit_result.success:
                print(f"✅ 限价单平仓成功: {limit_result.order_id}")
                print(f"价格: {close_price}")
                
                # 等待平仓完成
                print("等待平仓完成...")
                await asyncio.sleep(30)
                
                # 验证仓位
                final_position = await exchange_client.get_account_positions()
                if abs(final_position) < 0.001:
                    print("✅ 仓位已完全平仓")
                else:
                    print(f"⚠️ 仓位仍然存在: {final_position}")
                    print("请手动检查并平仓")
            else:
                print(f"❌ 限价单平仓失败: {limit_result.error_message if limit_result else 'Unknown error'}")
                
        except Exception as e:
            print(f"❌ 限价单平仓异常: {e}")
        
        # 最终检查
        print("\n最终仓位检查...")
        final_position = await exchange_client.get_account_positions()
        print(f"最终仓位: {final_position}")
        
        if abs(final_position) < 0.001:
            print("✅ 紧急平仓完成")
        else:
            print("❌ 紧急平仓失败，请手动处理")
            print("建议:")
            print("1. 检查交易所界面")
            print("2. 手动平仓")
            print("3. 检查保证金是否充足")
            
    except Exception as e:
        print(f"❌ 紧急平仓脚本异常: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    print("紧急平仓脚本")
    print("请确保:")
    print("1. 已正确配置交易所API")
    print("2. 有足够的保证金")
    print("3. 网络连接正常")
    print()
    
    confirm = input("确认执行紧急平仓? (y/N): ")
    if confirm.lower() == 'y':
        asyncio.run(emergency_close_position())
    else:
        print("已取消")
