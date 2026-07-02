import discord
from discord.ext import commands
from scanner import Scanner
from strategy import Strategy
from paper_trader import PaperTrader
from notifier import Notifier
from config import DISCORD_BOT_TOKEN

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

scanner = Scanner()
strategy = Strategy()
trader = PaperTrader()
notifier = Notifier()

active_signal = None


@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}")


# =========================
# START BOT
# =========================
@bot.command()
async def start(ctx):
    global active_signal

    await ctx.send("🚀 Bot Started Scanning Market...")

    results = scanner.scan_all()

    if not results:
        await ctx.send("No strong signals found.")
        return

    best = results[0]
    decision = strategy.evaluate(best)

    message = f"""
🚨 SIGNAL FOUND

Coin: {best['coin']}
Score: {best['score']}
Decision: {decision}

Reply YES to execute paper trade
Reply NO to ignore
"""

    active_signal = best

    await ctx.send(message)
    notifier.send(message)


# =========================
# STOP BOT
# =========================
@bot.command()
async def stop(ctx):
    await ctx.send("🛑 Bot Stopped.")


# =========================
# USER RESPONSE
# =========================
@bot.event
async def on_message(message):
    global active_signal

    if message.author == bot.user:
        return

    content = message.content.upper()

    # YES = open paper trade
    if content == "YES" and active_signal:
        trade = trader.open_trade(
            active_signal["coin"],
            "BUY",
            10
        )

        await message.channel.send(
            f"✅ PAPER TRADE OPENED\n{trade}"
        )

        active_signal = None

    # NO = ignore signal
    elif content == "NO":
        await message.channel.send("❌ Signal Ignored")
        active_signal = None

    await bot.process_commands(message)


def run_bot():
    bot.run(DISCORD_BOT_TOKEN)
