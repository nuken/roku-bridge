from abc import ABC, abstractmethod

class BaseAppPlugin(ABC):
    """
    Abstract base class for all app-specific plugins.
    """
    def __init__(self, app_id: str, app_name: str):
        self.app_id = app_id
        self.app_name = app_name

    @abstractmethod
    def tune_channel(self, roku_ip: str, channel_data: dict) -> list:
        """
        The core method of the plugin. It receives the Roku IP and the
        full channel configuration dictionary.

        It must return a sequence of commands to be executed.
        """
        pass