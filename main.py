import os
import ccxt.async_support as ccxt
import asyncio
import discord
from discord.ext import commands
from strategy import TradingStrategy
import requests

exchange = ccxt.binance({'apiKey': os.getenv('BINANCE_API_KEY'), 'secret': os.getenv('BINANCE_SECRET'), 'options': {'defaultType': 'future'}})
WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')

def send_alert(msg):
    requests.post(WEBHOOK_URL, json={"content": msg})

async def trade_bot(amount, leverage):
    balance_data = await exchange.fetch_balance()
    usdt_balance = balance_data['total']['USDT']
    
    if usdt_balance < amount:
        send_alert(f"❌ බොට් ස්ටාර්ට් කළ නොහැක! ප්‍රමාණවත් බැලන්ස් එකක් නොමැත. වත්මන් බැලන්ස්: {usdt_balance} USDT")
        return

    send_alert(f"✅ බොට් සාර්ථකව ආරම්භ විය. වත්මන් බැලන්ස්: {usdt_balance} USDT")

# case_insensitive=True එකතු කරන ලදී
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all(), case_insensitive=True)

# පණිවිඩ සැකසීම සඳහා අවශ්‍ය කොටස එකතු කරන ලදී
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)

@bot.command()
async def start(ctx, amount: float, leverage: int):
    await trade_bot(amount, leverage)

@bot.command()
async def stop(ctx):
    await ctx.send("🛑 බොට් නවතන ලදී.")
    await bot.close()

bot.run(os.getenv('DISCORD_TOKEN'))
