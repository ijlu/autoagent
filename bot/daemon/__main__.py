"""Allow running the daemon with: python -m bot.daemon"""
from bot.daemon.main import main

if __name__ == "__main__":
    raise SystemExit(main())
