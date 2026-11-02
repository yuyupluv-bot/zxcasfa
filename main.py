"""Root entry point for Python hosting platforms such as Bothost."""
from bot import runtime_guard

if __name__ == "__main__":
    runtime_guard.start()

from bot.main import run


if __name__ == "__main__":
    run()
