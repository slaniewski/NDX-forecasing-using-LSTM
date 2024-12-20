import os
import configparser
import logging
import time


class Setup:
    """
    Set root path, config path, and logging options

    Logging levels:
    logging hierarchy (each above contains every below):
    logging.NOTSET 0
    logging.DEBUG 10
    logging.INFO 20
    logging.WARNING 30
    logging.ERROR 40
    logging.CRITICAL 50
    """
    def __init__(self) -> None:

        # Set the root dir
        abs_path = os.path.abspath(__file__)
        dir_name = os.path.dirname(abs_path)
        os.chdir(dir_name)
        os.chdir('..')
        self.ROOT_PATH = os.getcwd().replace("\\", "/")+"/"

        # Specify config path
        self.CONFIG_PATH = self.ROOT_PATH+"/config.ini"
        self.config = configparser.ConfigParser()
        self.config.read(self.CONFIG_PATH)

        stock_name = self.config['prep']['Stock']
        for key in self.config['prep']:
            self.config['prep'][key] = self.config['prep'][key].format(stock=stock_name)

        if not os.path.isdir("reports/logs/"):
            os.mkdir("reports/logs/")
        logging.basicConfig(
            filename=f'reports/logs/logger_{time.strftime("%Y%m%d-%H%M%S")}.log',
            level=int(self.config["logger"]["LoggerLevel"]),
            format="%(asctime)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        self.logger = logging.getLogger("Config")
        self.logger.addHandler(logging.StreamHandler())
        self.logger.debug(f"Root Path: {self.ROOT_PATH}")
