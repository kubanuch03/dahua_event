import logging
import os
from datetime import datetime


def setup_logging(log_filename="traffic_monitor.log"):
    # Create a directory for logs if it doesn't exist
    log_dir = "./data/"
    os.makedirs(log_dir, exist_ok=True)

    # Create a timestamped log file name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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


def get_logger(name):
    """Get a logger with a specific name."""
    return logging.getLogger(name)
