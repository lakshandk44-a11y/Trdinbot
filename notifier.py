import requests
from config import DISCORD_WEBHOOK

class Notifier:

    def send(self, message):
        if not DISCORD_WEBHOOK:
            return

        data = {"content": message}
        requests.post(DISCORD_WEBHOOK, json=data)
