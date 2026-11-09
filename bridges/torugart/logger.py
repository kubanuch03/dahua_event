import logging
import os
from datetime import datetime


RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")


def setup_logging(log_filename="traffic_monitor.log"):
    # Create a directory for logs if it doesn't exist
    log_dir = "./data/"
    os.makedirs(log_dir, exist_ok=True)

    # Create a timestamped log file name
    timestamp = RUN_ID
    log_file_path = os.path.join(log_dir, f"{log_filename}_{timestamp}.log")

    # Configure the root logger
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(name)s - %(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file_path),
            logging.StreamHandler(),
        ],
    )


_SECRET_KEYS = ("password", "passwd", "pwd", "пароль", "api_key", "apikey", "token", "secret")


def mask_secrets(obj):
    """Копия структуры, где значения секретных полей (пароль, ключ, токен) заменены на ***."""
    if isinstance(obj, dict):
        return {k: "***" if any(s in str(k).lower() for s in _SECRET_KEYS) else mask_secrets(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_secrets(v) for v in obj]
    return obj


def get_logger(name):
    """Get a logger with a specific name."""
    return logging.getLogger(name)
