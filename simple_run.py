#!/usr/bin/env python3
"""
Simple Trading Strategy - Place one order, hold for specified duration, then auto-close
"""

import argparse
import asyncio
import logging
from pathlib import Path
import sys
import dotenv
from decimal import Decimal
from trading_bot import TradingBot, TradingConfig
from exchanges import ExchangeFactory


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Simple Trading Strategy - One order, hold, then close')

    # Exchange selection
    parser.add_argument('--exchange', type=str, default='edgex',
                        choices=ExchangeFactory.get_supported_exchanges(),
                        help='Exchange to use (default: edgex). '
                             f'Available: {", ".join(ExchangeFactory.get_supported_exchanges())}')

    # Trading parameters
    parser.add_argument('--ticker', type=str, default='ETH',
                        help='Ticker (default: ETH)')
    parser.add_argument('--quantity', type=Decimal, default=Decimal(0.1),
                        help='Order quantity (default: 0.1)')
    parser.add_argument('--direction', type=str, default='buy', choices=['buy', 'sell'],
                        help='Direction of the bot (default: buy)')
    parser.add_argument('--hold-minutes', type=int, default=60,
                        help='How long to hold the position in minutes (default: 60)')
    parser.add_argument('--loops', type=int, default=-1,
                        help='Number of loops to run (-1 for infinite loop, default: -1)')
    parser.add_argument('--env-file', type=str, default=".env",
                        help=".env file path (default: .env)")
    parser.add_argument('--boost', action='store_true',
                        help='Use market orders for closing (faster execution)')

    # Closing behavior (limit-first with retries)
    parser.add_argument('--close-retry-max', type=int, default=5,
                        help='Max retries for limit close before falling back to market (default: 5)')
    parser.add_argument('--close-retry-timeout', type=int, default=60,
                        help='Seconds to wait for each limit close attempt (default: 60)')

    return parser.parse_args()


def setup_logging(log_level: str):
    """Setup global logging configuration."""
    # Convert string level to logging constant
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Clear any existing handlers to prevent duplicates
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Configure root logger WITHOUT adding a console handler
    # This prevents duplicate logs when TradingLogger adds its own console handler
    root_logger.setLevel(level)

    # Suppress websockets debug logs unless DEBUG level is explicitly requested
    if log_level.upper() != 'DEBUG':
        logging.getLogger('websockets').setLevel(logging.WARNING)

    # Suppress other noisy loggers
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)

    # Suppress Lighter SDK debug logs
    logging.getLogger('lighter').setLevel(logging.WARNING)
    # Also suppress any root logger DEBUG messages that might be coming from Lighter
    if log_level.upper() != 'DEBUG':
        # Set root logger to WARNING to suppress DEBUG messages from Lighter SDK
        root_logger.setLevel(logging.WARNING)


async def main():
    """Main entry point."""
    args = parse_arguments()

    # Setup logging first
    setup_logging("WARNING")

    # Validate boost-mode can only be used with aster and backpack exchange
    if args.boost and args.exchange.lower() != 'aster' and args.exchange.lower() != 'backpack':
        print(f"Error: --boost can only be used when --exchange is 'aster' or 'backpack'. "
              f"Current exchange: {args.exchange}")
        sys.exit(1)

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"Env file not found: {env_path.resolve()}")
        sys.exit(1)
    dotenv.load_dotenv(args.env_file)

    # Create configuration
    config = TradingConfig(
        ticker=args.ticker.upper(),
        contract_id='',  # will be set in the bot's run method
        tick_size=Decimal(0),
        quantity=args.quantity,
        take_profit=Decimal(0),  # Not used in simple strategy
        direction=args.direction.lower(),
        max_orders=1,  # Only one order in simple strategy
        wait_time=0,  # Not used in simple strategy
        exchange=args.exchange.lower(),
        grid_step=Decimal(0),  # Not used in simple strategy
        stop_price=Decimal(-1),  # Not used in simple strategy
        pause_price=Decimal(-1),  # Not used in simple strategy
        boost_mode=args.boost,
        close_retry_max=args.close_retry_max,
        close_retry_timeout=args.close_retry_timeout,
    )

    # Create and run the bot
    bot = TradingBot(config)
    try:
        print(f"Starting Simple Trading Strategy:")
        print(f"  Exchange: {args.exchange}")
        print(f"  Ticker: {args.ticker}")
        print(f"  Quantity: {args.quantity}")
        print(f"  Direction: {args.direction}")
        print(f"  Hold Duration: {args.hold_minutes} minutes")
        print(f"  Loop Mode: {'Infinite' if args.loops == -1 else f'{args.loops} loops'}")
        print(f"  Boost Mode: {args.boost}")
        print(f"  Close Retry Max: {args.close_retry_max}")
        print(f"  Close Retry Timeout: {args.close_retry_timeout}s")
        print("=" * 50)
        
        await bot.simple_run(hold_duration_minutes=args.hold_minutes, loop_count=args.loops)
    except Exception as e:
        print(f"Simple strategy execution failed: {e}")
        # The bot's simple_run method already handles graceful shutdown
        return


if __name__ == "__main__":
    asyncio.run(main())
