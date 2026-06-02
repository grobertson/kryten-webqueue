import os
import uvicorn

from .config import Config
from .app import create_app


def main():
    config_path = os.environ.get("WQ_CONFIG", "/etc/kryten-webqueue/config.json")
    config = Config.from_file(config_path)
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
