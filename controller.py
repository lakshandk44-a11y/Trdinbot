import discord
from config import DISCORD_BOT_TOKEN

import discord

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

STATE = {
    "run": False,
    "trade_size": 0.01,
    "leverage": 1
}

@client.event
async def on_ready():
    print("CONTROL BOT ONLINE")

@client.event
async def on_message(message):

    if message.author == client.user:
        return

    global STATE

    if message.content.startswith("!start"):
        try:
            parts = message.content.split()

            STATE["trade_size"] = float(parts[1])
            STATE["leverage"] = int(parts[2])
            STATE["run"] = True

            await message.channel.send(
                f"🚀 STARTED\n💰 SIZE: {STATE['trade_size']}\n⚡ LEV: {STATE['leverage']}"
            )
        except:
            await message.channel.send("Format: !start 10 5")

    if message.content == "!stop":
        STATE["run"] = False
        await message.channel.send("🛑 STOPPED")

    if message.content == "!status":
        await message.channel.send(str(STATE))

client.run(DISCORD_BOT_TOKEN)
