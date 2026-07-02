import discord
from config import DISCORD_BOT_TOKEN

intents = discord.Intents.default()
client = discord.Client(intents=intents)

STATE = {"run": False}

@client.event
async def on_ready():
    print("CONTROL BOT ONLINE")

@client.event
async def on_message(message):
    global STATE

    if message.author == client.user:
        return

    if message.content == "!start":
        STATE["run"] = True
        await message.channel.send("🚀 BOT STARTED")

    if message.content == "!stop":
        STATE["run"] = False
        await message.channel.send("🛑 BOT STOPPED")

    if message.content == "!status":
        await message.channel.send(str(STATE))

client.run(DISCORD_BOT_TOKEN)
