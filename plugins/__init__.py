import os
import importlib
import inspect
from .base_plugin import BaseAppPlugin

# --- Plugin Discovery ---
discovered_plugins = {}

def discover_plugins():
    """
    Dynamically discovers and loads plugins from the 'plugins' directory.
    """
    global discovered_plugins
    if discovered_plugins:
        return

    plugins_dir = os.path.dirname(__file__)
    for filename in os.listdir(plugins_dir):
        if filename.endswith('_plugin.py'):
            module_name = f"plugins.{filename[:-3]}"
            try:
                module = importlib.import_module(module_name)
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, BaseAppPlugin) and obj is not BaseAppPlugin:
                        instance = obj()
                        # Use the python script filename as the key
                        script_name = filename
                        discovered_plugins[script_name] = instance
                        print(f"Successfully loaded plugin: {instance.app_name} from {script_name}")
            except Exception as e:
                print(f"Error loading plugin from {filename}: {e}")

discover_plugins()