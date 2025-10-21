"""
Modular Trading Bot - Supports multiple exchanges
"""

import os
import time
import asyncio
import traceback
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from exchanges import ExchangeFactory
from helpers import TradingLogger
from helpers.lark_bot import LarkBot
from helpers.telegram_bot import TelegramBot


@dataclass
class TradingConfig:
    """Configuration class for trading parameters."""
    ticker: str
    contract_id: str
    quantity: Decimal
    take_profit: Decimal
    tick_size: Decimal
    direction: str
    max_orders: int
    wait_time: int
    exchange: str
    grid_step: Decimal
    stop_price: Decimal
    pause_price: Decimal
    boost_mode: bool
    # 新增：平仓限价重试配置
    close_retry_max: int = 5
    close_retry_timeout: int = 60

    @property
    def close_order_side(self) -> str:
        """Get the close order side based on bot direction."""
        return 'buy' if self.direction == "sell" else 'sell'


@dataclass
class OrderMonitor:
    """Thread-safe order monitoring state."""
    order_id: Optional[str] = None
    filled: bool = False
    filled_price: Optional[Decimal] = None
    filled_qty: Decimal = 0.0

    def reset(self):
        """Reset the monitor state."""
        self.order_id = None
        self.filled = False
        self.filled_price = None
        self.filled_qty = 0.0


class TradingBot:
    """Modular Trading Bot - Main trading logic supporting multiple exchanges."""

    def __init__(self, config: TradingConfig):
        self.config = config
        self.logger = TradingLogger(config.exchange, config.ticker, log_to_console=True)

        # Create exchange client
        try:
            self.exchange_client = ExchangeFactory.create_exchange(
                config.exchange,
                config
            )
        except ValueError as e:
            raise ValueError(f"Failed to create exchange client: {e}")

        # Trading state
        self.active_close_orders = []
        self.last_close_orders = 0
        self.last_open_order_time = 0
        self.last_log_time = 0
        self.current_order_status = None
        self.order_filled_event = asyncio.Event()
        self.order_canceled_event = asyncio.Event()
        self.shutdown_requested = False
        self.order_filled_amount = 0.0
        self.order_filled_price = Decimal('0')
        self.loop = None

        # Register order callback
        self._setup_websocket_handlers()

    async def graceful_shutdown(self, reason: str = "Unknown"):
        """Perform graceful shutdown of the trading bot."""
        self.logger.log(f"Starting graceful shutdown: {reason}", "INFO")
        self.shutdown_requested = True

        try:
            # Disconnect from exchange
            await self.exchange_client.disconnect()
            self.logger.log("Graceful shutdown completed", "INFO")

        except Exception as e:
            self.logger.log(f"Error during graceful shutdown: {e}", "ERROR")
    
    async def handle_websocket_error(self, error: Exception):
        """Handle WebSocket connection errors."""
        self.logger.log(f"WebSocket error detected: {error}", "ERROR")
        
        # Check if we can reconnect
        if hasattr(self.exchange_client, 'ensure_websocket_connection'):
            try:
                success = await self.exchange_client.ensure_websocket_connection()
                if success:
                    self.logger.log("WebSocket reconnected successfully", "INFO")
                else:
                    self.logger.log("Failed to reconnect WebSocket, stopping strategy", "ERROR")
                    await self.graceful_shutdown("WebSocket connection failed")
            except Exception as reconnect_error:
                self.logger.log(f"Error during WebSocket reconnection: {reconnect_error}", "ERROR")
                await self.graceful_shutdown("WebSocket reconnection failed")
        else:
            self.logger.log("Exchange does not support WebSocket reconnection, stopping strategy", "ERROR")
            await self.graceful_shutdown("WebSocket connection lost")

    def _setup_websocket_handlers(self):
        """Setup WebSocket handlers for order updates."""
        def order_update_handler(message):
            """Handle order updates from WebSocket."""
            try:
                # Handle different message types from different exchanges
                if hasattr(message, 'order_id'):
                    # Lighter exchange sends OrderInfo objects
                    order_id = message.order_id
                    status = message.status
                    side = message.side
                    order_type = "OPEN" if side == self.config.direction else "CLOSE"
                    filled_size = message.filled_size
                    size = message.size
                    price = message.price
                    contract_id = self.config.contract_id  # Lighter doesn't send contract_id in OrderInfo
                else:
                    # Other exchanges send dictionary objects
                    # Check if this is for our contract
                    if message.get('contract_id') != self.config.contract_id:
                        return

                    order_id = message.get('order_id')
                    status = message.get('status')
                    side = message.get('side', '')
                    order_type = message.get('order_type', '')
                    filled_size = Decimal(message.get('filled_size', 0))
                    size = message.get('size')
                    price = message.get('price')

                if order_type == "OPEN":
                    self.current_order_status = status

                if status == 'FILLED':
                    if order_type == "OPEN":
                        self.order_filled_amount = filled_size
                        self.order_filled_price = price  # Store price for later use
                        # Ensure thread-safe interaction with asyncio event loop
                        if self.loop is not None:
                            self.loop.call_soon_threadsafe(self.order_filled_event.set)
                        else:
                            # Fallback (should not happen after run() starts)
                            self.order_filled_event.set()
                    elif order_type == "CLOSE":
                        # For close orders, also set the event to indicate fill
                        if self.loop is not None:
                            self.loop.call_soon_threadsafe(self.order_filled_event.set)
                        else:
                            self.order_filled_event.set()

                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{size} @ {price}", "INFO")
                    self.logger.log_transaction(order_id, side, size, price, status)
                elif status == "CANCELED":
                    if order_type == "OPEN":
                        self.order_filled_amount = filled_size
                        if self.loop is not None:
                            self.loop.call_soon_threadsafe(self.order_canceled_event.set)
                        else:
                            self.order_canceled_event.set()

                        if self.order_filled_amount > 0:
                            self.logger.log_transaction(order_id, side, self.order_filled_amount, price, status)
                            
                    # PATCH
                    if self.config.exchange == "extended":
                        self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                        f"{Decimal(size) - filled_size} @ {price}", "INFO")
                    else:
                        self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                        f"{size} @ {price}", "INFO")
                elif status == "PARTIALLY_FILLED":
                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{filled_size} @ {price}", "INFO")
                    
                    # Handle partial fills for open orders
                    if order_type == "OPEN":
                        self.order_filled_amount = filled_size
                        self.order_filled_price = price
                        # Set event for partial fill too
                        if self.loop is not None:
                            self.loop.call_soon_threadsafe(self.order_filled_event.set)
                        else:
                            self.order_filled_event.set()
                else:
                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{size} @ {price}", "INFO")

            except Exception as e:
                self.logger.log(f"Error handling order update: {e}", "ERROR")
                self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
                
                # Check if this is a WebSocket connection error
                if "ConnectionClosed" in str(e) or "websocket" in str(e).lower():
                    if self.loop is not None:
                        # Schedule WebSocket error handling in the event loop
                        self.loop.call_soon_threadsafe(
                            lambda: asyncio.create_task(self.handle_websocket_error(e))
                        )

        # Setup order update handler
        self.exchange_client.setup_order_update_handler(order_update_handler)

    def _calculate_wait_time(self) -> Decimal:
        """Calculate wait time between orders."""
        cool_down_time = self.config.wait_time

        if len(self.active_close_orders) < self.last_close_orders:
            self.last_close_orders = len(self.active_close_orders)
            return 0

        self.last_close_orders = len(self.active_close_orders)
        if len(self.active_close_orders) >= self.config.max_orders:
            return 1

        if len(self.active_close_orders) / self.config.max_orders >= 2/3:
            cool_down_time = 2 * self.config.wait_time
        elif len(self.active_close_orders) / self.config.max_orders >= 1/3:
            cool_down_time = self.config.wait_time
        elif len(self.active_close_orders) / self.config.max_orders >= 1/6:
            cool_down_time = self.config.wait_time / 2
        else:
            cool_down_time = self.config.wait_time / 4

        # if the program detects active_close_orders during startup, it is necessary to consider cooldown_time
        if self.last_open_order_time == 0 and len(self.active_close_orders) > 0:
            self.last_open_order_time = time.time()

        if time.time() - self.last_open_order_time > cool_down_time:
            return 0
        else:
            return 1

    async def _place_and_monitor_open_order(self) -> bool:
        """Place an order and monitor its execution."""
        try:
            # Reset state before placing order
            self.order_filled_event.clear()
            self.current_order_status = 'OPEN'
            self.order_filled_amount = 0.0

            # Place the order
            order_result = await self.exchange_client.place_open_order(
                self.config.contract_id,
                self.config.quantity,
                self.config.direction
            )

            if not order_result.success:
                return False

            if order_result.status == 'FILLED':
                return await self._handle_order_result(order_result)
            elif not self.order_filled_event.is_set():
                try:
                    await asyncio.wait_for(self.order_filled_event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass

            # Handle order result
            return await self._handle_order_result(order_result)

        except Exception as e:
            self.logger.log(f"Error placing order: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            return False

    async def _handle_order_result(self, order_result) -> bool:
        """Handle the result of an order placement."""
        order_id = order_result.order_id
        filled_price = order_result.price

        if self.order_filled_event.is_set() or order_result.status == 'FILLED':
            if self.config.boost_mode:
                close_order_result = await self.exchange_client.place_market_order(
                    self.config.contract_id,
                    self.config.quantity,
                    self.config.close_order_side
                )
            else:
                close_side = self.config.close_order_side
                if close_side == 'sell':
                    close_price = filled_price * (1 + self.config.take_profit/100)
                else:
                    close_price = filled_price * (1 - self.config.take_profit/100)

                close_order_result = await self.exchange_client.place_close_order(
                    self.config.contract_id,
                    self.config.quantity,
                    close_price,
                    close_side
                )
                if self.config.exchange == "lighter":
                    await asyncio.sleep(1)

            if not close_order_result.success:
                self.logger.log(f"[CLOSE] Failed to place close order: {close_order_result.error_message}", "ERROR")
                raise Exception(f"[CLOSE] Failed to place close order: {close_order_result.error_message}")

            return True

        else:
            new_order_price = await self.exchange_client.get_order_price(self.config.direction)

            def should_wait(direction: str, new_order_price: Decimal, order_result_price: Decimal) -> bool:
                if direction == "buy":
                    return new_order_price <= order_result_price
                elif direction == "sell":
                    return new_order_price >= order_result_price
                return False

            if self.config.exchange == "lighter":
                current_order_status = self.exchange_client.current_order.status
            else:
                order_info = await self.exchange_client.get_order_info(order_id)
                current_order_status = order_info.status

            while (
                should_wait(self.config.direction, new_order_price, order_result.price)
                and current_order_status == "OPEN"
            ):
                self.logger.log(f"[OPEN] [{order_id}] Waiting for order to be filled @ {order_result.price}", "INFO")
                await asyncio.sleep(5)
                if self.config.exchange == "lighter":
                    current_order_status = self.exchange_client.current_order.status
                else:
                    order_info = await self.exchange_client.get_order_info(order_id)
                    if order_info is not None:
                        current_order_status = order_info.status
                new_order_price = await self.exchange_client.get_order_price(self.config.direction)

            self.order_canceled_event.clear()
            # Cancel the order if it's still open
            self.logger.log(f"[OPEN] [{order_id}] Cancelling order and placing a new order", "INFO")
            if self.config.exchange == "lighter":
                cancel_result = await self.exchange_client.cancel_order(order_id)
                start_time = time.time()
                while (time.time() - start_time < 10 and self.exchange_client.current_order.status != 'CANCELED' and
                        self.exchange_client.current_order.status != 'FILLED'):
                    await asyncio.sleep(0.1)

                if self.exchange_client.current_order.status not in ['CANCELED', 'FILLED']:
                    raise Exception(f"[OPEN] Error cancelling order: {self.exchange_client.current_order.status}")
                else:
                    self.order_filled_amount = self.exchange_client.current_order.filled_size
            else:
                try:
                    cancel_result = await self.exchange_client.cancel_order(order_id)
                    if not cancel_result.success:
                        self.order_canceled_event.set()
                        self.logger.log(f"[CLOSE] Failed to cancel order {order_id}: {cancel_result.error_message}", "WARNING")
                    else:
                        self.current_order_status = "CANCELED"

                except Exception as e:
                    self.order_canceled_event.set()
                    self.logger.log(f"[CLOSE] Error canceling order {order_id}: {e}", "ERROR")

                if self.config.exchange == "backpack" or self.config.exchange == "extended":
                    self.order_filled_amount = cancel_result.filled_size
                else:
                    # Wait for cancel event or timeout
                    if not self.order_canceled_event.is_set():
                        try:
                            await asyncio.wait_for(self.order_canceled_event.wait(), timeout=5)
                        except asyncio.TimeoutError:
                            order_info = await self.exchange_client.get_order_info(order_id)
                            self.order_filled_amount = order_info.filled_size

            if self.order_filled_amount > 0:
                close_side = self.config.close_order_side
                if self.config.boost_mode:
                    close_order_result = await self.exchange_client.place_close_order(
                        self.config.contract_id,
                        self.order_filled_amount,
                        filled_price,
                        close_side
                    )
                else:
                    if close_side == 'sell':
                        close_price = filled_price * (1 + self.config.take_profit/100)
                    else:
                        close_price = filled_price * (1 - self.config.take_profit/100)

                    close_order_result = await self.exchange_client.place_close_order(
                        self.config.contract_id,
                        self.order_filled_amount,
                        close_price,
                        close_side
                    )
                    if self.config.exchange == "lighter":
                        await asyncio.sleep(1)

                self.last_open_order_time = time.time()
                if not close_order_result.success:
                    self.logger.log(f"[CLOSE] Failed to place close order: {close_order_result.error_message}", "ERROR")

            return True

        return False

    async def _log_status_periodically(self):
        """Log status information periodically, including positions."""
        if time.time() - self.last_log_time > 60 or self.last_log_time == 0:
            print("--------------------------------")
            try:
                # Get active orders
                active_orders = await self.exchange_client.get_active_orders(self.config.contract_id)

                # Filter close orders
                self.active_close_orders = []
                for order in active_orders:
                    if order.side == self.config.close_order_side:
                        self.active_close_orders.append({
                            'id': order.order_id,
                            'price': order.price,
                            'size': order.size
                        })

                # Get positions
                position_amt = await self.exchange_client.get_account_positions()

                # Calculate active closing amount
                active_close_amount = sum(
                    Decimal(order.get('size', 0))
                    for order in self.active_close_orders
                    if isinstance(order, dict)
                )

                self.logger.log(f"Current Position: {position_amt} | Active closing amount: {active_close_amount} | "
                                f"Order quantity: {len(self.active_close_orders)}")
                self.last_log_time = time.time()
                # Check for position mismatch
                if abs(position_amt - active_close_amount) > (2 * self.config.quantity):
                    error_message = f"\n\nERROR: [{self.config.exchange.upper()}_{self.config.ticker.upper()}] "
                    error_message += "Position mismatch detected\n"
                    error_message += "###### ERROR ###### ERROR ###### ERROR ###### ERROR #####\n"
                    error_message += "Please manually rebalance your position and take-profit orders\n"
                    error_message += "请手动平衡当前仓位和正在关闭的仓位\n"
                    error_message += f"current position: {position_amt} | active closing amount: {active_close_amount} | "f"Order quantity: {len(self.active_close_orders)}\n"
                    error_message += "###### ERROR ###### ERROR ###### ERROR ###### ERROR #####\n"
                    self.logger.log(error_message, "ERROR")

                    await self.send_notification(error_message.lstrip())

                    if not self.shutdown_requested:
                        self.shutdown_requested = True

                    mismatch_detected = True
                else:
                    mismatch_detected = False

                return mismatch_detected

            except Exception as e:
                self.logger.log(f"Error in periodic status check: {e}", "ERROR")
                self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

            print("--------------------------------")

    async def _meet_grid_step_condition(self) -> bool:
        if self.active_close_orders:
            picker = min if self.config.direction == "buy" else max
            next_close_order = picker(self.active_close_orders, key=lambda o: o["price"])
            next_close_price = next_close_order["price"]

            best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
            if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
                raise ValueError("No bid/ask data available")

            if self.config.direction == "buy":
                new_order_close_price = best_ask * (1 + self.config.take_profit/100)
                if next_close_price / new_order_close_price > 1 + self.config.grid_step/100:
                    return True
                else:
                    return False
            elif self.config.direction == "sell":
                new_order_close_price = best_bid * (1 - self.config.take_profit/100)
                if new_order_close_price / next_close_price > 1 + self.config.grid_step/100:
                    return True
                else:
                    return False
            else:
                raise ValueError(f"Invalid direction: {self.config.direction}")
        else:
            return True

    async def _check_price_condition(self) -> bool:
        stop_trading = False
        pause_trading = False

        if self.config.pause_price == self.config.stop_price == -1:
            return stop_trading, pause_trading

        best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
        if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
            raise ValueError("No bid/ask data available")

        if self.config.stop_price != -1:
            if self.config.direction == "buy":
                if best_ask >= self.config.stop_price:
                    stop_trading = True
            elif self.config.direction == "sell":
                if best_bid <= self.config.stop_price:
                    stop_trading = True

        if self.config.pause_price != -1:
            if self.config.direction == "buy":
                if best_ask >= self.config.pause_price:
                    pause_trading = True
            elif self.config.direction == "sell":
                if best_bid <= self.config.pause_price:
                    pause_trading = True

        return stop_trading, pause_trading

    async def send_notification(self, message: str):
        lark_token = os.getenv("LARK_TOKEN")
        if lark_token:
            async with LarkBot(lark_token) as lark_bot:
                await lark_bot.send_text(message)

        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if telegram_token and telegram_chat_id:
            with TelegramBot(telegram_token, telegram_chat_id) as tg_bot:
                tg_bot.send_text(message)

    async def run(self):
        """Main trading loop."""
        try:
            self.config.contract_id, self.config.tick_size = await self.exchange_client.get_contract_attributes()

            # Log current TradingConfig
            self.logger.log("=== Trading Configuration ===", "INFO")
            self.logger.log(f"Ticker: {self.config.ticker}", "INFO")
            self.logger.log(f"Contract ID: {self.config.contract_id}", "INFO")
            self.logger.log(f"Quantity: {self.config.quantity}", "INFO")
            self.logger.log(f"Take Profit: {self.config.take_profit}%", "INFO")
            self.logger.log(f"Direction: {self.config.direction}", "INFO")
            self.logger.log(f"Max Orders: {self.config.max_orders}", "INFO")
            self.logger.log(f"Wait Time: {self.config.wait_time}s", "INFO")
            self.logger.log(f"Exchange: {self.config.exchange}", "INFO")
            self.logger.log(f"Grid Step: {self.config.grid_step}%", "INFO")
            self.logger.log(f"Stop Price: {self.config.stop_price}", "INFO")
            self.logger.log(f"Pause Price: {self.config.pause_price}", "INFO")
            self.logger.log(f"Boost Mode: {self.config.boost_mode}", "INFO")
            self.logger.log("=============================", "INFO")

            # Capture the running event loop for thread-safe callbacks
            self.loop = asyncio.get_running_loop()
            # Connect to exchange
            await self.exchange_client.connect()

            # wait for connection to establish
            await asyncio.sleep(5)

            # Main trading loop
            while not self.shutdown_requested:
                # Update active orders
                active_orders = await self.exchange_client.get_active_orders(self.config.contract_id)

                # Filter close orders
                self.active_close_orders = []
                for order in active_orders:
                    if order.side == self.config.close_order_side:
                        self.active_close_orders.append({
                            'id': order.order_id,
                            'price': order.price,
                            'size': order.size
                        })

                # Periodic logging
                mismatch_detected = await self._log_status_periodically()

                stop_trading, pause_trading = await self._check_price_condition()
                if stop_trading:
                    msg = f"\n\nWARNING: [{self.config.exchange.upper()}_{self.config.ticker.upper()}] \n"
                    msg += "Stopped trading due to stop price triggered\n"
                    msg += "价格已经达到停止交易价格，脚本将停止交易\n"
                    await self.send_notification(msg.lstrip())
                    await self.graceful_shutdown(msg)
                    continue

                if pause_trading:
                    await asyncio.sleep(5)
                    continue

                if not mismatch_detected:
                    wait_time = self._calculate_wait_time()

                    if wait_time > 0:
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        meet_grid_step_condition = await self._meet_grid_step_condition()
                        if not meet_grid_step_condition:
                            await asyncio.sleep(1)
                            continue

                        await self._place_and_monitor_open_order()
                        self.last_close_orders += 1

        except KeyboardInterrupt:
            self.logger.log("Bot stopped by user")
            await self.graceful_shutdown("User interruption (Ctrl+C)")
        except Exception as e:
            self.logger.log(f"Critical error: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            await self.graceful_shutdown(f"Critical error: {e}")
            raise
        finally:
            # Ensure all connections are closed even if graceful shutdown fails
            try:
                await self.exchange_client.disconnect()
            except Exception as e:
                self.logger.log(f"Error disconnecting from exchange: {e}", "ERROR")

    async def simple_run(self, hold_duration_minutes: int = 60, loop_count: int = -1):
        """
        Simple trading strategy: Place one order, hold for specified duration, then auto-close.
        Runs in a loop until stopped or loop_count is reached.
        
        Args:
            hold_duration_minutes: How long to hold the position in minutes (default: 60)
            loop_count: Number of loops to run (-1 for infinite loop, default: -1)
        """
        try:
            # Get contract attributes
            self.config.contract_id, self.config.tick_size = await self.exchange_client.get_contract_attributes()

            # Log configuration
            self.logger.log("=== Simple Trading Strategy (Loop Mode) ===", "INFO")
            self.logger.log(f"Ticker: {self.config.ticker}", "INFO")
            self.logger.log(f"Contract ID: {self.config.contract_id}", "INFO")
            self.logger.log(f"Quantity: {self.config.quantity}", "INFO")
            self.logger.log(f"Direction: {self.config.direction}", "INFO")
            self.logger.log(f"Exchange: {self.config.exchange}", "INFO")
            self.logger.log(f"Hold Duration: {hold_duration_minutes} minutes", "INFO")
            self.logger.log(f"Loop Mode: {'Infinite' if loop_count == -1 else f'{loop_count} loops'}", "INFO")
            self.logger.log("==========================================", "INFO")

            # Capture the running event loop for thread-safe callbacks
            self.loop = asyncio.get_running_loop()
            
            # Connect to exchange
            await self.exchange_client.connect()
            await asyncio.sleep(5)  # Wait for connection to establish

            # Main trading loop
            loop_number = 0
            while not self.shutdown_requested:
                loop_number += 1
                
                # Check loop count limit
                if loop_count != -1 and loop_number > loop_count:
                    self.logger.log(f"Reached loop limit ({loop_count}), stopping strategy", "INFO")
                    break
                
                self.logger.log(f"=== Starting Loop #{loop_number} ===", "INFO")
                
                try:
                    # Check if we already have a position
                    current_position = await self.exchange_client.get_account_positions()
                    if current_position != 0:
                        self.logger.log(f"CRITICAL: Existing position detected: {current_position} (expected: 0)", "ERROR")
                        self.logger.log("This indicates a serious issue - position not properly closed in previous loop!", "ERROR")
                        
                        # Force close ALL existing positions regardless of size
                        position_size = abs(current_position)
                        close_side = self.config.close_order_side
                        
                        self.logger.log(f"Force closing position of size: {position_size}", "WARNING")
                        close_result = await self._close_position_simple(position_size, Decimal('0'))
                        
                        if close_result:
                            self.logger.log("Existing position force-closed successfully", "INFO")
                            await asyncio.sleep(10)  # Wait longer for position to update
                            
                            # Triple-check position is actually closed
                            final_position = await self.exchange_client.get_account_positions()
                            if abs(final_position) > 0.001:
                                self.logger.log(f"CRITICAL: Position still exists after force close: {final_position}", "ERROR")
                                self.logger.log("Stopping strategy to prevent further position accumulation!", "ERROR")
                                await self.graceful_shutdown("Position accumulation detected - manual intervention required")
                                return
                            else:
                                self.logger.log("Position successfully closed, continuing with new loop", "INFO")
                        else:
                            self.logger.log("CRITICAL: Failed to force close existing position", "ERROR")
                            self.logger.log("Stopping strategy to prevent further position accumulation!", "ERROR")
                            await self.graceful_shutdown("Failed to close existing position - manual intervention required")
                            return

                    # Check for any active orders before placing new order
                    try:
                        active_orders = await self.exchange_client.get_active_orders(self.config.contract_id)
                        if active_orders:
                            self.logger.log(f"CRITICAL: Found {len(active_orders)} active orders from previous loop", "WARNING")
                            self.logger.log("This indicates orders were not properly handled in previous loop", "WARNING")
                            
                            # Cancel all active orders
                            for order in active_orders:
                                try:
                                    order_id = order.get('order_id') if isinstance(order, dict) else getattr(order, 'order_id', None)
                                    if order_id:
                                        self.logger.log(f"Cancelling active order: {order_id}", "INFO")
                                        await self.exchange_client.cancel_order(order_id)
                                    else:
                                        self.logger.log(f"Cannot get order_id from order: {order}", "WARNING")
                                except Exception as cancel_error:
                                    self.logger.log(f"Error cancelling order {order}: {cancel_error}", "WARNING")
                            
                            # Wait a bit for cancellations to process
                            self.logger.log("Waiting 10 seconds for order cancellations to process...", "INFO")
                            await asyncio.sleep(10)
                            
                            # Double-check that orders are cancelled
                            remaining_orders = await self.exchange_client.get_active_orders(self.config.contract_id)
                            if remaining_orders:
                                self.logger.log(f"WARNING: {len(remaining_orders)} orders still active after cancellation attempt", "WARNING")
                                self.logger.log("Waiting additional 20 seconds...", "INFO")
                                await asyncio.sleep(20)
                            else:
                                self.logger.log("All active orders successfully cancelled", "INFO")
                    except Exception as e:
                        self.logger.log(f"Error checking/cancelling active orders: {e}", "WARNING")

                    # Step 1: Place the initial order
                    self.logger.log("Step 1: Placing initial order...", "INFO")
                    order_result = await self._place_simple_order()
                    
                    if not order_result:
                        self.logger.log("Failed to place initial order, retrying in 30 seconds", "ERROR")
                        await asyncio.sleep(30)
                        continue

                    # Step 2: Wait for order to be filled
                    self.logger.log("Step 2: Waiting for order to be filled...", "INFO")
                    filled_order = await self._wait_for_order_fill(order_result.order_id, timeout=60, is_open_order=True)
                    
                    if not filled_order:
                        self.logger.log("Order was not filled within 60 seconds, checking if position already exists...", "WARNING")
                        
                        # CRITICAL: Check if position already exists before trying market order
                        current_position = await self.exchange_client.get_account_positions()
                        if current_position != 0:
                            self.logger.log(f"CRITICAL: Position already exists: {current_position}, order may have filled but not detected", "ERROR")
                            self.logger.log("Skipping market order to prevent double position", "WARNING")
                            
                            # Try to get order info one more time
                            try:
                                order_info = await self.exchange_client.get_order_info(order_result.order_id)
                                if order_info and order_info.status == 'FILLED':
                                    self.logger.log(f"Order {order_result.order_id} was actually filled: {order_info.filled_size} @ {order_info.price}", "INFO")
                                    filled_order = order_info
                                else:
                                    self.logger.log(f"Order status: {order_info.status if order_info else 'Unknown'}", "WARNING")
                            except Exception as e:
                                self.logger.log(f"Error checking order info: {e}", "WARNING")
                            
                            if filled_order:
                                # Position exists and order was filled, continue with the loop
                                pass
                            else:
                                # Position exists but order status unclear, retry
                                self.logger.log("Position exists but order status unclear, retrying in 30 seconds", "ERROR")
                                await asyncio.sleep(30)
                                continue
                        else:
                            # No position exists, safe to try market order
                            self.logger.log("No position exists, trying market order...", "WARNING")
                            
                            # Cancel the unfilled order first
                            try:
                                await self.exchange_client.cancel_order(order_result.order_id)
                                self.logger.log(f"Cancelled unfilled open order: {order_result.order_id}", "INFO")
                            except Exception as cancel_error:
                                self.logger.log(f"Failed to cancel order: {cancel_error}", "WARNING")
                            
                            # 额外验证取消状态，避免取消未成功导致重复开仓
                            try:
                                order_info_after_cancel = await self.exchange_client.get_order_info(order_result.order_id)
                                if order_info_after_cancel and order_info_after_cancel.status == 'FILLED':
                                    self.logger.log(f"Order {order_result.order_id} actually filled while canceling: {order_info_after_cancel.filled_size} @ {order_info_after_cancel.price}", "INFO")
                                    filled_order = order_info_after_cancel
                                elif order_info_after_cancel and order_info_after_cancel.status == 'OPEN':
                                    self.logger.log("Open order still active after cancel; skipping market order to prevent double position", "WARNING")
                                    await asyncio.sleep(30)
                                    continue
                            except Exception as verify_error:
                                self.logger.log(f"Error verifying cancel status: {verify_error}", "WARNING")
                            
                            # Try market order for opening position（仅在确认取消成功或订单不再活跃时）
                            has_market_order = hasattr(self.exchange_client, 'place_market_order')
                            if has_market_order and not filled_order:
                                self.logger.log("Using market order to open position...", "INFO")
                                market_result = await self.exchange_client.place_market_order(
                                    self.config.contract_id,
                                    self.config.quantity,
                                    self.config.direction
                                )
                                
                                if market_result.success:
                                    filled_order = await self._wait_for_order_fill(market_result.order_id, timeout=30, is_open_order=True)
                                    if not filled_order:
                                        self.logger.log("Market order also failed, retrying in 30 seconds", "ERROR")
                                        await asyncio.sleep(30)
                                        continue
                                else:
                                    self.logger.log(f"Market order failed: {market_result.error_message}, retrying in 30 seconds", "ERROR")
                                    await asyncio.sleep(30)
                                    continue
                            elif not has_market_order:
                                self.logger.log("Exchange does not support market orders, retrying in 30 seconds", "ERROR")
                                await asyncio.sleep(30)
                                continue

                    filled_price = filled_order.price
                    filled_quantity = filled_order.filled_size
                    
                    # Check if this is a partial fill
                    if filled_order.status == 'PARTIALLY_FILLED':
                        self.logger.log(f"WARNING: Order partially filled: {filled_quantity} @ {filled_price} (requested: {self.config.quantity})", "WARNING")
                        self.logger.log("Continuing with partial position - this may affect strategy performance", "WARNING")
                    else:
                        self.logger.log(f"Order filled: {filled_quantity} @ {filled_price}", "INFO")

                    # Step 3: Hold position for specified duration
                    hold_duration_seconds = hold_duration_minutes * 60
                    self.logger.log(f"Step 3: Holding position for {hold_duration_minutes} minutes...", "INFO")
                    
                    start_time = time.time()
                    while time.time() - start_time < hold_duration_seconds and not self.shutdown_requested:
                        remaining_time = hold_duration_seconds - (time.time() - start_time)
                        remaining_minutes = remaining_time / 60
                        
                        # Log status every 10 minutes
                        if int(remaining_time) % 600 == 0:
                            self.logger.log(f"Position held for {int((time.time() - start_time)/60)} minutes. "
                                          f"Remaining: {remaining_minutes:.1f} minutes", "INFO")
                        
                        await asyncio.sleep(60)  # Check every minute

                    if self.shutdown_requested:
                        break

                    # Step 4: Auto-close position
                    self.logger.log("Step 4: Auto-closing position...", "INFO")
                    close_result = await self._close_position_simple(filled_quantity, filled_price)
                    
                    if close_result:
                        self.logger.log("Position successfully closed!", "INFO")
                        self.logger.log(f"Loop #{loop_number} completed successfully", "INFO")
                    else:
                        self.logger.log("CRITICAL: Failed to close position automatically", "ERROR")
                        # Check if position still exists
                        try:
                            current_position = await self.exchange_client.get_account_positions()
                            if abs(current_position) > 0.001:
                                self.logger.log(f"CRITICAL: Position still exists: {current_position}", "ERROR")
                                self.logger.log("Stopping strategy to prevent position accumulation!", "ERROR")
                                await self.graceful_shutdown("Failed to close position - manual intervention required")
                                return
                            else:
                                self.logger.log("Position appears to be closed despite error", "INFO")
                        except Exception as e:
                            self.logger.log(f"Error checking position after close failure: {e}", "WARNING")
                            self.logger.log("Stopping strategy due to uncertainty about position state", "ERROR")
                            await self.graceful_shutdown("Position state uncertain - manual intervention required")
                            return
                    

                    
                except Exception as e:
                    self.logger.log(f"Error in loop #{loop_number}: {e}", "ERROR")
                    self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
                    self.logger.log("Waiting 30 seconds before retrying...", "INFO")
                    await asyncio.sleep(30)
                    continue

            self.logger.log("Simple trading strategy loop completed", "INFO")

        except KeyboardInterrupt:
            self.logger.log("Simple strategy stopped by user", "INFO")
            await self.graceful_shutdown("User interruption (Ctrl+C)")
        except Exception as e:
            self.logger.log(f"Error in simple strategy: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            await self.graceful_shutdown(f"Error: {e}")
            raise
        finally:
            try:
                await self.exchange_client.disconnect()
            except Exception as e:
                self.logger.log(f"Error disconnecting from exchange: {e}", "ERROR")

    async def _place_simple_order(self) -> Optional[object]:
        """Place a simple order and return the result."""
        try:
            # Reset state
            self.order_filled_event.clear()
            self.current_order_status = 'OPEN'
            self.order_filled_amount = 0.0

            # Place the order
            order_result = await self.exchange_client.place_open_order(
                self.config.contract_id,
                self.config.quantity,
                self.config.direction
            )

            # Handle case where order_result might be None (some exchanges)
            if order_result is None:
                self.logger.log("Order placement returned None - this might indicate a connection issue", "ERROR")
                return None

            if not order_result.success:
                self.logger.log(f"Failed to place order: {order_result.error_message}", "ERROR")
                return None

            self.logger.log(f"Order placed: {order_result.order_id} @ {order_result.price}", "INFO")
            return order_result

        except Exception as e:
            self.logger.log(f"Error placing order: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            return None

    async def _wait_for_order_fill(self, order_id: str, timeout: int = 60, is_open_order: bool = True) -> Optional[object]:
        """Wait for order to be filled within timeout."""
        try:
            start_time = time.time()
            last_check_time = 0
            websocket_filled = False
            
            while time.time() - start_time < timeout:
                # Check if order is filled via WebSocket event
                if self.order_filled_event.is_set():
                    websocket_filled = True
                    self.logger.log(f"WebSocket detected order {order_id} filled", "INFO")
                    
                    # If WebSocket says filled, trust it and return success
                    # Don't rely on get_order_info which might be inconsistent
                    self.logger.log(f"Order {order_id} filled according to WebSocket", "INFO")
                    
                    # Create a mock order info object for WebSocket fills
                    class MockOrderInfo:
                        def __init__(self, order_id, filled_size, price):
                            self.order_id = order_id
                            self.filled_size = filled_size
                            self.price = price
                            self.status = 'FILLED'
                    
                    # Use the filled amount and price from WebSocket event
                    filled_size = getattr(self, 'order_filled_amount', self.config.quantity)
                    filled_price = getattr(self, 'order_filled_price', Decimal('0'))
                    mock_order = MockOrderInfo(order_id, filled_size, filled_price)
                    return mock_order

                # Check order status directly (with error handling)
                try:
                    order_info = await self.exchange_client.get_order_info(order_id)
                    if order_info:
                        if order_info.status == 'FILLED':
                            self.logger.log(f"Order {order_id} filled: {order_info.filled_size} @ {order_info.price}", "INFO")
                            return order_info
                        elif order_info.status == 'PARTIALLY_FILLED':
                            self.logger.log(f"Order {order_id} partially filled: {order_info.filled_size} @ {order_info.price}", "WARNING")
                            # 直接使用交易所返回的部分成交信息，避免依赖 WebSocket 状态
                            return order_info
                        elif order_info.status == 'CANCELED':
                            self.logger.log(f"Order {order_id} was canceled", "WARNING")
                            return None
                        elif order_info.status == 'REJECTED':
                            self.logger.log(f"Order {order_id} was rejected", "ERROR")
                            return None
                        elif order_info.status == 'OPEN':
                            # Order is still open, continue waiting
                            pass
                except Exception as order_check_error:
                    # Log error but don't fail immediately - might be temporary API issue
                    current_time = time.time()
                    if current_time - last_check_time > 30:  # Log error only every 30 seconds
                        self.logger.log(f"Error checking order status: {order_check_error}", "WARNING")
                        last_check_time = current_time

                await asyncio.sleep(2)  # Check every 2 seconds (more frequent)

            # If we reach here, timeout occurred
            if websocket_filled:
                self.logger.log(f"CRITICAL: WebSocket detected fill but get_order_info timeout for {order_id}", "ERROR")
                # Try one more time to get order info
                try:
                    order_info = await self.exchange_client.get_order_info(order_id)
                    if order_info and order_info.status == 'FILLED':
                        self.logger.log(f"Order {order_id} filled: {order_info.filled_size} @ {order_info.price}", "INFO")
                        return order_info
                except Exception as e:
                    self.logger.log(f"Final order info check failed: {e}", "ERROR")
            
            self.logger.log(f"Order {order_id} not filled within {timeout} seconds", "WARNING")
            return None

        except Exception as e:
            self.logger.log(f"Error waiting for order fill: {e}", "ERROR")
            return None

    async def _close_position_simple(self, quantity: Decimal, entry_price: Decimal) -> bool:
        """Close position using limit-first strategy with retries, fallback to market."""
        try:
            # First, check and cancel any active orders before closing position
            try:
                active_orders = await self.exchange_client.get_active_orders(self.config.contract_id)
                if active_orders:
                    for order in active_orders:
                        try:
                            order_id = order.get('order_id') if isinstance(order, dict) else getattr(order, 'order_id', None)
                            if order_id:
                                self.logger.log(f"Cancelling active order before close: {order_id}", "INFO")
                                await self.exchange_client.cancel_order(order_id)
                        except Exception as cancel_error:
                            self.logger.log(f"Error cancelling order {order}: {cancel_error}", "WARNING")
            except Exception as e:
                self.logger.log(f"Error checking active orders before close: {e}", "WARNING")

            # Get actual position size and side (don't trust input quantity)
            actual_position = await self.exchange_client.get_account_positions()
            close_side = 'sell' if actual_position > 0 else 'buy'
            close_quantity = abs(actual_position)

            self.logger.log(f"CRITICAL: Closing actual position: {close_quantity} {close_side} (parameter was: {quantity})", "WARNING")

            # If boost mode explicitly requested, use market-first (existing behavior)
            has_close_market = hasattr(self.exchange_client, 'place_close_market_order')
            has_general_market = hasattr(self.exchange_client, 'place_market_order')
            if self.config.boost_mode:
                if has_close_market:
                    self.logger.log("Using close market order to close position (boost mode)", "INFO")
                    close_result = await self.exchange_client.place_close_market_order(
                        self.config.contract_id, close_quantity, close_side
                    )
                elif has_general_market:
                    self.logger.log("Using market order to close position (boost mode)", "INFO")
                    close_result = await self.exchange_client.place_market_order(
                        self.config.contract_id, close_quantity, close_side
                    )
                else:
                    self.logger.log("Market order not available; falling back to limit close", "WARNING")
                    close_result = None
            else:
                close_result = None

            # Non-boost path: limit-first with retries, then market fallback
            if close_result is None or not getattr(close_result, 'success', False):
                # Try up to close_retry_max attempts with progressively aggressive pricing
                for attempt in range(1, self.config.close_retry_max + 1):
                    try:
                        best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
                        if close_side == 'sell':
                            # Cross the spread to bid; make more aggressive on retries
                            close_price = best_bid - (self.config.tick_size * max(0, attempt - 1))
                        else:
                            # Cross the spread to ask; make more aggressive on retries
                            close_price = best_ask + (self.config.tick_size * max(0, attempt - 1))

                        self.logger.log(
                            f"[CLOSE] Attempt {attempt}/{self.config.close_retry_max} with limit {close_side} @ {close_price}",
                            "INFO"
                        )
                        limit_result = await self.exchange_client.place_close_order(
                            self.config.contract_id, close_quantity, close_price, close_side
                        )
                        if not limit_result.success:
                            self.logger.log(f"[CLOSE] Limit close placement failed: {limit_result.error_message}", "WARNING")
                            # Continue to next attempt without waiting
                            continue

                        # Wait for fill for configured timeout
                        filled = await self._wait_for_order_fill(limit_result.order_id, timeout=self.config.close_retry_timeout, is_open_order=False)
                        if filled:
                            self.logger.log(f"Position closed: {filled.filled_size} @ {filled.price}", "INFO")
                            await asyncio.sleep(5)
                            final_position = await self.exchange_client.get_account_positions()
                            if abs(final_position) < 0.001:
                                self.logger.log("Position close verified successfully", "INFO")
                                return True
                            else:
                                self.logger.log(f"CRITICAL: Position still exists after close: {final_position}", "ERROR")
                                # Even if order reports filled, position not zero; retry further
                        else:
                            # Cancel and retry with more aggressive price
                            self.logger.log("Close order not filled within timeout, cancelling and retrying...", "WARNING")
                            try:
                                await self.exchange_client.cancel_order(limit_result.order_id)
                                self.logger.log(f"Cancelled unfilled close order: {limit_result.order_id}", "INFO")
                            except Exception as cancel_error:
                                self.logger.log(f"Failed to cancel close order: {cancel_error}", "WARNING")
                            # Continue loop for next attempt
                            continue
                    except Exception as e:
                        self.logger.log(f"Error during close attempt {attempt}: {e}", "WARNING")
                        continue

                # After retries, use market fallback if available
                if has_close_market:
                    self.logger.log("Retries exhausted; using close market order to close position", "WARNING")
                    market_result = await self.exchange_client.place_close_market_order(
                        self.config.contract_id, close_quantity, close_side
                    )
                elif has_general_market:
                    self.logger.log("Retries exhausted; using market order to close position", "WARNING")
                    market_result = await self.exchange_client.place_market_order(
                        self.config.contract_id, close_quantity, close_side
                    )
                else:
                    self.logger.log("No market order available for fallback", "ERROR")
                    return False

                if market_result and market_result.success:
                    self.logger.log(f"Market close order placed: {market_result.order_id}", "INFO")
                    market_filled = await self._wait_for_order_fill(market_result.order_id, timeout=60, is_open_order=False)
                    if market_filled:
                        await asyncio.sleep(5)
                        final_position = await self.exchange_client.get_account_positions()
                        if abs(final_position) < 0.001:
                            self.logger.log("Position close verified successfully (market)", "INFO")
                            return True
                    self.logger.log("Market fallback did not verify position close", "ERROR")
                    return False
                else:
                    self.logger.log("Market fallback failed to place order", "ERROR")
                    return False

            # If boost-mode market close succeeded
            if close_result and close_result.success:
                self.logger.log(f"Close order placed: {close_result.order_id}", "INFO")
                close_filled = await self._wait_for_order_fill(close_result.order_id, timeout=60, is_open_order=False)
                if close_filled:
                    await asyncio.sleep(5)
                    final_position = await self.exchange_client.get_account_positions()
                    if abs(final_position) < 0.001:
                        self.logger.log("Position close verified successfully", "INFO")
                        return True
                self.logger.log("Boost-mode market close did not verify position close", "ERROR")
                return False

        except Exception as e:
            self.logger.log(f"Error in _close_position_simple: {e}", "ERROR")
            return False

    async def _force_close_position(self, position_size: Decimal) -> bool:
        """Force close position using market order if available."""
        try:
            self.logger.log(f"Attempting force close for position: {position_size}", "WARNING")
            
            # Determine close side
            close_side = "sell" if position_size > 0 else "buy"
            close_quantity = abs(position_size)
            
            # Try market order if available
            if hasattr(self.exchange_client, 'place_close_market_order'):
                self.logger.log("Using close market order for force close", "WARNING")
                market_result = await self.exchange_client.place_close_market_order(
                    self.config.contract_id,
                    close_quantity,
                    close_side
                )
            elif hasattr(self.exchange_client, 'place_market_order'):
                self.logger.log("Using market order for force close", "WARNING")
                market_result = await self.exchange_client.place_market_order(
                    self.config.contract_id,
                    close_quantity,
                    close_side
                )
                
                if market_result and market_result.success:
                    self.logger.log(f"Force close market order placed: {market_result.order_id}", "WARNING")
                    await asyncio.sleep(10)  # Wait for execution
                    
                    # Check final position
                    final_position = await self.exchange_client.get_account_positions()
                    if abs(final_position) < 0.001:
                        self.logger.log("Force close successful", "INFO")
                        return True
                    else:
                        self.logger.log(f"Force close failed, position still exists: {final_position}", "ERROR")
                        self.logger.log("This may indicate a deeper issue with the exchange or position tracking", "ERROR")
                        return False
                else:
                    self.logger.log("Force close market order failed", "ERROR")
                    return False
            else:
                self.logger.log("Market order not available for force close", "ERROR")
                return False
                
        except Exception as e:
            self.logger.log(f"Error in force close: {e}", "ERROR")
            return False
