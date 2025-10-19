#!/usr/bin/env python3
"""
WebSocket连接测试脚本
用于诊断GRVT WebSocket连接问题
"""

import asyncio
import sys
import os
from decimal import Decimal

# Add the project root to the path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from exchanges.factory import ExchangeFactory
from helpers.logger import Logger

async def test_websocket_connection():
    """测试WebSocket连接"""
    
    print("🔌 WebSocket连接测试开始")
    print("=" * 50)
    
    try:
        # 创建GRVT客户端
        config = {
            'ticker': 'ETH',
            'quantity': Decimal('0.01'),
            'direction': 'buy',
            'exchange': 'grvt'
        }
        
        exchange_client = ExchangeFactory.create_exchange('grvt', config)
        logger = Logger("WEBSOCKET_TEST")
        
        print("1. 初始化GRVT客户端...")
        print(f"   环境变量检查:")
        print(f"   - GRVT_TRADING_ACCOUNT_ID: {'✓' if os.getenv('GRVT_TRADING_ACCOUNT_ID') else '✗'}")
        print(f"   - GRVT_PRIVATE_KEY: {'✓' if os.getenv('GRVT_PRIVATE_KEY') else '✗'}")
        print(f"   - GRVT_API_KEY: {'✓' if os.getenv('GRVT_API_KEY') else '✗'}")
        print(f"   - GRVT_ENVIRONMENT: {os.getenv('GRVT_ENVIRONMENT', 'prod')}")
        print()
        
        print("2. 连接WebSocket...")
        await exchange_client.connect()
        print("   ✓ WebSocket连接成功")
        
        print("3. 检查连接状态...")
        if hasattr(exchange_client, 'is_websocket_connected'):
            connected = exchange_client.is_websocket_connected()
            print(f"   连接状态: {'✓ 已连接' if connected else '✗ 未连接'}")
        else:
            print("   ⚠ 无法检查连接状态")
        
        print("4. 测试订单订阅...")
        def test_handler(message):
            print(f"   收到消息: {message}")
        
        exchange_client.setup_order_update_handler(test_handler)
        print("   ✓ 订单处理器设置完成")
        
        print("5. 等待消息 (30秒)...")
        await asyncio.sleep(30)
        
        print("6. 断开连接...")
        await exchange_client.disconnect()
        print("   ✓ WebSocket断开成功")
        
        print("\n✅ WebSocket连接测试完成")
        
    except Exception as e:
        print(f"\n❌ WebSocket连接测试失败: {e}")
        import traceback
        traceback.print_exc()
        
        # 尝试清理
        try:
            if 'exchange_client' in locals():
                await exchange_client.disconnect()
        except:
            pass

async def test_websocket_reconnection():
    """测试WebSocket重连机制"""
    
    print("\n🔄 WebSocket重连测试开始")
    print("=" * 50)
    
    try:
        config = {
            'ticker': 'ETH',
            'quantity': Decimal('0.01'),
            'direction': 'buy',
            'exchange': 'grvt'
        }
        
        exchange_client = ExchangeFactory.create_exchange('grvt', config)
        
        print("1. 初始连接...")
        await exchange_client.connect()
        print("   ✓ 初始连接成功")
        
        print("2. 模拟连接断开...")
        if hasattr(exchange_client, '_ws_client') and exchange_client._ws_client:
            try:
                await exchange_client._ws_client.__aexit__(None, None, None)
            except:
                pass
            exchange_client._ws_client = None
            exchange_client._is_connected = False
        print("   ✓ 连接已断开")
        
        print("3. 测试重连...")
        if hasattr(exchange_client, 'ensure_websocket_connection'):
            success = await exchange_client.ensure_websocket_connection()
            print(f"   重连结果: {'✓ 成功' if success else '✗ 失败'}")
        else:
            print("   ⚠ 不支持重连机制")
        
        print("4. 清理...")
        await exchange_client.disconnect()
        print("   ✓ 清理完成")
        
        print("\n✅ WebSocket重连测试完成")
        
    except Exception as e:
        print(f"\n❌ WebSocket重连测试失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    print("WebSocket连接诊断工具")
    print("请确保已正确配置GRVT环境变量")
    print()
    
    # 运行测试
    asyncio.run(test_websocket_connection())
    asyncio.run(test_websocket_reconnection())
