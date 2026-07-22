"""Entry point for hosts that expect a single main file.

Equivalent to: python -m iris.bot
"""
import asyncio

from iris.bot import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
