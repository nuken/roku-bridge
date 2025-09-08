"""
A specific plugin implementation for the Fubo app (Roku App ID: 43465).
This app often requires navigating to a specific live event from a list.
"""
from .base_plugin import BaseAppPlugin
import logging

class FuboPlugin(BaseAppPlugin):
    """
    App-specific plugin for the Fubo app.
    """

    def __init__(self):
        # The official Roku App ID for Fubo is "43465"
        super().__init__(app_id="43465", app_name="Fubo")

    def tune_channel(self, roku_ip, channel_data):
        """
        Implements the tuning logic for the Fubo app.
        This sequence is now simplified based on user feedback.
        """
        logging.info(f"[{self.app_name} Plugin] Tuning to '{channel_data.get('name')}'.")

        plugin_data = channel_data.get('plugin_data', {})
        list_position = plugin_data.get('list_position')

        try:
            list_position = int(list_position)
        except (ValueError, TypeError):
             logging.error(f"[{self.app_name} Plugin] Invalid or missing 'list_position' in plugin_data for channel '{channel_data.get('name')}'. Must be an integer.")
             return None


        if not isinstance(list_position, int) or list_position < 1:
            logging.error(f"[{self.app_name} Plugin] Invalid 'list_position' ({list_position}). Must be 1 or greater.")
            return None

        # --- START OF CUSTOMIZABLE NAVIGATION ---
        # This sequence assumes the app opens and you need to navigate to the live guide.
        # You must watch your TV and adjust this to match the app's behavior.
        sequence = [
            # The app is launched by the main script, so we just wait
            {"wait": 4}, # Wait for the app to load

            "Left",      # Navigate to the left-side menu
            {"wait": 0.5},
            "Down",      # Navigate down to the "Live" or "Guide" section
            {"wait": 0.5},
            "Select",    # Select it to open the guide
            {"wait": 1.7}  # Wait for the guide to load
        ]

        # Only add navigation steps if we are NOT on the first item.
        if list_position > 1:
            # Here, we create a loop of "Down" keypresses.
            for _ in range(list_position - 1):
                sequence.append("Down")
                sequence.append({"wait": 0.1}) # Small delay between presses

        # Finally, select the channel to play.
        sequence.append("Select")
        sequence.append({"wait": 1})
        sequence.append("Select")
        # --- END OF CUSTOMIZABLE NAVIGATION ---


        return sequence

