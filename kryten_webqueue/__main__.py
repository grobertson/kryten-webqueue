import os
import uvicorn

from .config import Config
from .app import create_app
from .logging_config import build_log_config


def main():
    config_path = os.environ.get("WQ_CONFIG", "/etc/kryten-webqueue/config.json")
    config = Config.from_file(config_path)
    app = create_app(config)
    log_config = build_log_config(config.log_level, config.promo_log_level)
    uvicorn.run(app, host=config.host, port=config.port, log_config=log_config)


if __name__ == "__main__":
    main()
