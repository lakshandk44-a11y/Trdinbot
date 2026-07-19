# 🤖 HackerAI ට්‍රේඩින් බොට් — සම්පූර්ණ Setup Guide (සිංහලෙන්)

මේ document එකේ **VPS එකක් ගන්න ඉඳන්, Bot එක සම්පූර්ණයෙන්ම run කරන්නම වෙනකම්, එකින් එක, අඩුපාඩුවක් නැතුව** ලියලා තියෙනවා. මේක **මෙයාට කලින් කිසිම දෙයක් දන්නෙ නැති කෙනෙක්ට වුනත් follow කරන්න පුළුවන් විදියට** හදලා තියෙන්නෙ.

---

## 📋 Table of Contents

1. [මේ Bot එකෙන් වෙන්නෙ මොකක්ද](#1-මේ-bot-එකෙන්-වෙන්නෙ-මොකක්ද)
2. [VPS එකක් ගන්නා විදිය](#2-vps-එකක්-ගන්නා-විදිය)
3. [VPS එකට Connect වෙන විදිය (SSH)](#3-vps-එකට-connect-වෙන-විදිය-ssh)
4. [VPS එක Setup කරන විදිය](#4-vps-එක-setup-කරන-විදිය)
5. [Bot Code එක VPS එකට ගේන විදිය](#5-bot-code-එක-vps-එකට-ගේන-විදිය)
6. [API Keys සහ Environment Variables Setup](#6-api-keys-සහ-environment-variables-setup)
7. [Dependencies Install කිරීම](#7-dependencies-install-කිරීම)
8. [PM2 වලින් Bot එක Run කිරීම](#8-pm2-වලින්-bot-එක-run-කිරීම)
9. [Telegram Notification Setup](#9-telegram-notification-setup)
10. [Web Dashboard Setup (Optional)](#10-web-dashboard-setup-optional)
11. [Common Errors සහ Fixes](#11-common-errors-සහ-fixes)
12. [File එකින් එක — මොකක්ද කරන්නෙ](#12-file-එකින්-එක--මොකක්ද-කරන්නෙ)
13. [Bot එකේ Full Process — Start සිට Trade Close වෙනකම්](#13-bot-එකේ-full-process--start-සිට-trade-close-වෙනකම්)

---

## 1. මේ Bot එකෙන් වෙන්නෙ මොකක්ද

මේක **Binance Futures** එකේ, crypto coins 40ක් 24/7 automatic ව scan කරලා, **5 analysis tools** (Order Block, Fair Value Gap, Liquidity, ICT/SMC, Market Structure) use කරලා, ලාභ ලබන්න පුළුවන් trade setups හොයාගෙන, **automatic ව trade open කරලා, manage කරලා, close කරන** bot එකක්.

Bot එකට:
- **Analysis engine** එකක් තියෙනවා (chart එක කියවලා decision ගන්නවා)
- **Trade management** system එකක් තියෙනවා (SL/TP set කරනවා, trailing stop, profit lock)
- **Calibration** system එකක් තියෙනවා (historical data එකෙන් real win-rate verify කරනවා)
- **Telegram notification** system එකක් තියෙනවා (trade එකකට මොකද වෙන්නෙ කියලා message එකෙන් දන්වනවා)

---

## 2. VPS එකක් ගන්නා විදිය

**VPS (Virtual Private Server)** කියන්නෙ, 24/7 internet එකට connect වෙලා තියෙන, ඔයාගේම නොවන, cloud එකේ තියෙන computer එකක්. Bot එක **ඔයාගේ phone/laptop off කලත් run වෙන්න** මේක ඕන.

### Recommend කරන Providers:
- **AWS EC2** (Amazon) — Free tier එකක් තියෙනවා (year 1ට), ලෝකෙම trust කරන service එකක්
- **DigitalOcean** — ලේසි, simple, affordable
- **Vultr** — affordable, ලේසි

### VPS එකක් Setup කරද්දි තෝරගන්න ඕන Settings:
- **OS**: Ubuntu 22.04 හෝ Amazon Linux 2023 (recommend)
- **Size**: අඩුම ගානේ **2GB RAM, 2 CPU** (Bot + Dashboard + Telegram notify ටිකට ප්‍රමාණවත්)
- **Region**: ඔයාට ළඟම region එකක් (latency අඩු කරගන්න)

VPS එක create කරාට පස්සෙ, ඔයාට ලැබෙනවා:
- **VPS IP Address** එකක් (උදා: `54.123.45.67`)
- **Key file** එකක් (`.pem` file, AWS නම්) — මේක **ඉතාම වැදගත්**, save කරගන්න, කවුරුවත් share කරන්න එපා

---

## 3. VPS එකට Connect වෙන විදිය (SSH)

### 📱 Phone එකෙන් — Termux App එකෙන්

**1. Termux Install කරන්න** — Google Play Store එකෙන් (හෝ F-Droid එකෙන්, වඩාත් reliable)

**2. Termux Open කරලා, මේ commands run කරන්න:**

```bash
pkg update && pkg upgrade
pkg install openssh
```

**3. Key File එක (`.pem`) Phone එකට ගේන්න** — Google Drive/Email එකෙන් download කරලා, Termux එකට access කරගන්න:

```bash
termux-setup-storage
cp /storage/emulated/0/Download/your-key.pem ~/
chmod 400 ~/your-key.pem
```

**4. SSH කරන්න:**

```bash
ssh -i ~/your-key.pem ec2-user@<VPS_IP>
```

(`ec2-user` කියන්නෙ AWS Amazon Linux ට — Ubuntu නම් `ubuntu` කියලා දෙන්න)

### 💻 Computer එකෙන් — PuTTY (Windows) හෝ Terminal (Mac/Linux)

**Windows (PuTTY):**
1. `putty.org` එකෙන් PuTTY download කරන්න
2. `.pem` file එක `.ppk` ට convert කරන්න (PuTTYgen tool එකෙන්)
3. PuTTY open කරලා, Host Name එකට VPS IP දාන්න, SSH → Auth → Credentials එකේ `.ppk` file එක attach කරන්න
4. "Open" click කරන්න

**Mac/Linux (Terminal):**
```bash
chmod 400 your-key.pem
ssh -i your-key.pem ec2-user@<VPS_IP>
```

**Connect උනාට පස්සෙ**, terminal එකේ VPS එකේ command prompt එකක් පේනවා — දැන් ඔයා VPS එක ඇතුළෙ ඉන්නෙ.

---

## 4. VPS එක Setup කරන විදිය

VPS එකට connect වුනාට පස්සෙ, මේ commands **එකින් එක** run කරන්න:

### 4.1. System Update කරන්න
```bash
sudo yum update -y          # Amazon Linux
# හෝ
sudo apt update && sudo apt upgrade -y   # Ubuntu
```

### 4.2. Python Install කරන්න (බොහෝවිට දැනටමත් තියෙනවා)
```bash
python3 --version
pip3 --version
```
Version එකක් නැත්නම්:
```bash
sudo yum install python3 python3-pip -y      # Amazon Linux
sudo apt install python3 python3-pip -y      # Ubuntu
```

### 4.3. Node.js Install කරන්න (v20+, Telegram/Dashboard ට)
```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install 20
nvm alias default 20
node -v   # v20.x.x කියලා පේන්න ඕන
```

### 4.4. Git Install කරන්න
```bash
sudo yum install git -y      # Amazon Linux
sudo apt install git -y      # Ubuntu
```

### 4.5. PM2 Install කරන්න (Bot එක background එකේ run කරන්න)
```bash
npm install -g pm2
```

---

## 5. Bot Code එක VPS එකට ගේන විදිය

### Option A — GitHub Repository එකකින් (Recommend කරන්නෙ)

ඔයාගේ GitHub account එකේ repository එකක් හදලා, bot files ටික upload කරලා තියෙනවා නම්:

```bash
git clone https://github.com/<ඔයාගේ-username>/<repo-name>.git
cd <repo-name>
```

### Option B — Manually File හදන එක

```bash
mkdir Trdinbot
cd Trdinbot
nano config.py
# (Content එක paste කරලා, Ctrl+O → Enter → Ctrl+X)
```

(මේ විදිහට file එකින් එක හදන්න ඕන — GitHub ක්‍රමයම ලේසි)

### Bot එකට ඕන Files ටික:
```
main.py
bot_core.py
trade_manager.py
analysis_engine.py
config.py
backtest_calibration.py
backtest_dynamic_tpsl.py
telegram_notify.py
requirements.txt
```

---

## 6. API Keys සහ Environment Variables Setup

### 6.1. Binance API Key එකක් හදාගන්න

1. Binance.com (හෝ Demo Trading එකට `demo.binance.com`) → **API Management**
2. **"Create API"** → Label එකක් දෙන්න
3. **API Key** සහ **Secret Key** — copy කරගන්න (Secret Key එක **එකපාරයි පේන්නෙ**, save කරගන්න)
4. **Futures Trading permission** enable කරන්න
5. **Withdrawal permission OFF ම** තියන්න (Security සඳහා — bot එකට withdrawal permission ඕන නෑ)

### 6.2. Environment Variables Set කරන්න

VPS terminal එකේ:

```bash
nano ~/.bashrc
```

මේ lines ටික **අන්තිමට** add කරන්න (ඔයාගේම values දාන්න):

```bash
export BINANCE_API_KEY="ඔයාගේ_API_key_එකම"
export BINANCE_API_SECRET="ඔයාගේ_Secret_key_එකම"
export TELEGRAM_BOT_TOKEN="ඔයාගේ_Telegram_bot_token_එකම"
export TELEGRAM_CHAT_ID="ඔයාගේ_Telegram_chat_id_එකම"
```

Save කරන්න (`Ctrl+O` → `Enter` → `Ctrl+X`), ඊට පස්සෙ:

```bash
source ~/.bashrc
```

⚠️ **වැදගත්:** මේ file එක (`.bashrc`) **කවදාවත් GitHub එකට upload කරන්න එපා** — API keys public වෙන්න පුළුවන්.

---

## 7. Dependencies Install කිරීම

```bash
cd Trdinbot
pip3 install -r requirements.txt
```

`requirements.txt` file එකේ නැත්නම්, manually මේ ටික install කරන්න:

```bash
pip3 install requests pandas numpy flask flask-cors
```

---

## 8. PM2 වලින් Bot එක Run කිරීම

### 8.1. Bot එක පළමු වතාවට Start කරන්න

```bash
cd Trdinbot
pm2 start main.py --name my-binance-bot --interpreter python3
```

### 8.2. Bot එක Run වෙනවද Confirm කරගන්න

```bash
pm2 list
```

`my-binance-bot` — **status: online** පේන්න ඕන.

### 8.3. Log බලන්න

```bash
pm2 logs my-binance-bot
```

(Log එකෙන් exit වෙන්න: `Ctrl+C`)

### 8.4. VPS Reboot උනත් Bot එක Automatic ව Start වෙන්න

```bash
pm2 save
pm2 startup
```

(`pm2 startup` command එකෙන් දෙන command එකම copy කරලා, **ආයෙත් paste කරලා run කරන්න**)

### වැදගත් PM2 Commands

| Command | කරන්නෙ |
|---|---|
| `pm2 list` | Process ටික running ද කියලා බලන්න |
| `pm2 logs my-binance-bot` | Live log බලන්න |
| `pm2 restart my-binance-bot` | Bot එක restart කරන්න |
| `pm2 restart my-binance-bot --update-env` | Restart + අලුත් env variables load කරගන්න |
| `pm2 stop my-binance-bot` | Bot එක නවත්තන්න |
| `pm2 delete my-binance-bot` | Bot process එකම අයින් කරන්න |

---

## 9. Telegram Notification Setup

### 9.1. Telegram Bot එකක් හදාගන්න

1. Telegram app එකේ **"BotFather"** search කරන්න, chat එකක් start කරන්න
2. `/newbot` යවන්න
3. Bot name එකක් දෙන්න (ඕනම එකක්, උදා: "My Trading Bot")
4. Bot username එකක් දෙන්න (**`_bot`** කින් අවසන් වෙන්න ඕන, උදා: `mytrading_alert_bot`)
5. BotFather **Token එකක්** දෙනවා — මේක **copy කරගන්න** (`123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` වගේ)

### 9.2. ඔයාගේම Bot එකට Message එකක් යවන්න

Telegram එකෙන් **ඔයාගේම අලුත් bot එක search කරලා**, chat එකක් start කරලා, `hi` කියලා ඕනම message එකක් යවන්න. (**මේක අනිවාර්යයෙන්ම කරන්න ඕන**, නැත්නම් bot එකට ඔයාට message යවන්න බෑ)

### 9.3. Chat ID එක හොයාගන්න

Browser එකකින්, මේ URL එකට යන්න (`<TOKEN>` ඔයාගේම token එකෙන් replace කරන්න):

```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Response එකේ **`"chat":{"id": 123456789, ...}`** කියලා තියෙන **number එකම** — ඒකයි `TELEGRAM_CHAT_ID`.

### 9.4. Environment Variables Set කරන්න (Section 6.2 එකේම කරලා තියෙන්න ඕන)

```bash
export TELEGRAM_BOT_TOKEN="ඔයාගේ_token_එකම"
export TELEGRAM_CHAT_ID="ඔයාගේ_chat_id_එකම"
```

### 9.5. Test කරන්න

```bash
python3 -c "from telegram_notify import send_telegram; send_telegram('Test message!')"
```

Telegram එකට message එකක් එන්න ඕන.

### 9.6. Bot එක Restart කරන්න

```bash
pm2 restart my-binance-bot --update-env
```

**දැන් ඉඳන්, Bot එක trade කරන හැම වතාවකම, ඔයාට Telegram Message එකක් එනවා:**
- Trade Open වුනාම
- Trailing Stop Active වුනාම / Move වුනාම
- TP1 Hit වුනාම (TP2ට extend වෙනවද, එහෙම නැත්නම් close වෙනවද)
- Trade Close වුනාම (Profit/Loss)

---

## 10. Web Dashboard Setup (Optional)

Trade ටික browser එකකින්, live ලෙස බලාගන්න ඕන නම්:

```bash
pip3 install flask flask-cors
pm2 start dashboard_backend.py --name dashboard --interpreter python3
```

`dashboard.html` file එක browser එකකින් open කරන්න — trade data live ලෙස පේනවා.

(Full setup instructions — dashboard file එකේම comment ලෙස ලියලා තියෙනවා)

---

## 11. Common Errors සහ Fixes

### ❌ `ModuleNotFoundError: No module named 'flask'`
```bash
pip3 install flask flask-cors
```

### ❌ `ModuleNotFoundError: No module named 'requests'` (හෝ `pandas`, `numpy`)
```bash
pip3 install requests pandas numpy
```

### ❌ `pip3 install ... --break-system-packages` — "no such option"
`--break-system-packages` flag එකම අයින් කරලා, plain command එකම try කරන්න:
```bash
pip3 install <package-name>
```

### ❌ `Error: Script not found: /home/ec2-user/Trdinbot/dashboard_backend.py`
File name එකේ **capital/lowercase letters** හරියටම match වෙන්නෙ නැති නිසා (Linux **case-sensitive**). `ls` කරලා file name එකම check කරන්න, `mv` කරලා fix කරන්න:
```bash
mv Dashboard_Backend.py dashboard_backend.py
```

### ❌ `API Error 429: Too many requests; current limit of IP(...) is 6000 requests per minute`
Bot එකේම **built-in handling** එකක් තියෙනවා — 60s wait කරලා automatic ව retry කරනවා. **Crash වෙන්නෙ නෑ**. Repeatedly එනවා නම්, backtest scripts bot එකත් සමඟ **එකවර run කරන්න එපා** (එකම IP rate-limit budget එක share කරනවා).

### ❌ `API Error 400: {"code":-2019,"msg":"Margin is insufficient."}`
Balance එකට සාපේක්ෂව coin එකේ **minimum order size** (minNotional) එකට වඩා balance එක අඩුයි. Bot එකෙන්ම skip කරලා ඊළඟ coin එකට යනවා.

### ❌ `ReferenceError: crypto is not defined` (Node.js scripts වලට)
Node.js version එක අඩුයි. Node **v20+**ට upgrade කරන්න:
```bash
nvm install 20
nvm alias default 20
```

### ❌ Bot එක Trade එකක්වත් Open කරන්නෙ නෑ
Log එකේ **`tools_agreeing`/`profit_chance`** values බලන්න (📈 emoji එකෙන් පේනවා). `profit_chance` එක `MIN_PROFIT_CHANCE` (config.py) ට වඩා අඩු නම්, **market condition එකම** (choppy/uncertain) හේතුව වෙන්න පුළුවන් — bug එකක් නෙවෙයි.

### ❌ PM2 Process එක "errored" පේනවා
```bash
pm2 logs <process-name> --err
```
Error message එකම බලලා, ඒකට අනුව troubleshoot කරන්න (බොහෝවිට missing dependency එකක් හෝ syntax error එකක්).

### ❌ Environment Variables Set කළත් Bot එකට ලැබෙන්නෙ නෑ
```bash
pm2 restart my-binance-bot --update-env
```
**`--update-env` flag එකම අනිවාර්යයෙන්ම** දාන්න — නැත්නම් PM2 එකේ පරණ env values ම cache වෙලා තියෙනවා.

---

## 12. File එකින් එක — මොකක්ද කරන්නෙ

### 📄 `main.py` — Bot එකේ "Start Button" එක
Bot එක **පටන්ගන්නෙ මේ file එකෙන්ම**. Config.py එකේ තියෙන settings ටික ගන්නවා, `HackerAIBot` object එක හදනවා, Binance connection එක verify කරනවා, ඊට පස්සෙ bot එකේ main loop එක start කරනවා. **`pm2 start main.py`** කරද්දි run වෙන්නෙ මේ file එකම.

### 📄 `config.py` — Bot එකේ "Settings Panel" එක
Bot එකේ **හැම setting එකක්ම** මේ file එකේ තියෙනවා — MIN_PROFIT_CHANCE, MIN_TOOLS_MATCH, TAKE_PROFIT_PERCENT, STOP_LOSS_PERCENT, MAX_LEVERAGE, TOP_40_COINS (scan කරන coins list එක), TRAILING_STOP settings, API keys (environment variables වලින්ම ගන්නවා) ආදිය. **මේකේ number එකක් වෙනස් කලාම, bot එකේ behavior එකම වෙනස් වෙනවා** — ඒකයි "Settings Panel" එකක්.

### 📄 `bot_core.py` — Bot එකේ "Brain" එක (Main Logic)
- Binance API එකට connect වෙනවා
- Coins 40ම, 30s ට සැරයක් scan කරනවා
- Analysis engine එකෙන් signal එකක් (BUY/SELL/HOLD) ගන්නවා
- Signal එක criteria (tools≥3, profit_chance≥45%) match වුනොත්, **trade open කරනවා**
- SL/TP analysis එකෙන්ම calculate කරනවා
- TP1 hit වුනාම, ආයෙත් market analyze කරලා, TP2ට extend කරනවද කියලා තීරණය ගන්නවා
- Trading hours filter, position sizing calculation — ඔක්කොම මෙතන

### 📄 `trade_manager.py` — Bot එකේ "Trade Manager" (Open Trades බලාගන්නා කෙනා)
- Trade එකක් open වුනාට පස්සෙ, **ඒක manage කරන්නෙ මේ file එකෙන්ම**
- 5s ට සැරයක්ම, open trades ටික check කරනවා — SL/TP hit වුනාද කියලා
- **Trailing Stop** — Profit එකට ගියොත්, SL එක push කරගෙන යනවා (coin එකේ ATR/volatility එකට අනුව)
- Trade close වුනාම, Binance API එකට real order එකක් යවලා position එක close කරනවා
- Trade History එක (win/loss records) save කරගන්නවා
- `trade_state.json` file එකට **හැම update එකක්ම** save කරනවා (bot restart උනත් state එක අහිමි වෙන්නෙ නැති වෙන්න)

### 📄 `analysis_engine.py` — Bot එකේ "Chart Reader" (Analysis Tools 5)
Chart එකක් (OHLC candles) ගන්නවා, **tools 5ක්** run කරනවා:
1. **ICT/SMC** — BOS/CHoCH (market structure break)
2. **FVG** — Fair Value Gap (price imbalance)
3. **Order Block** — Institutional buy/sell zones
4. **Liquidity** — Buyside/sellside liquidity sweeps
5. **Market Structure** — Trend direction

Tools 5න් 3ක් (timeframe 3ම — 4h/1h/15m — වෙන වෙනම) agree උනොත්, "BUY" හෝ "SELL" signal එකක් return කරනවා. Calibration table එකෙන්ම, ඒ signal එකේ **real historical win-rate** එකත් attach කරනවා (profit_chance).

### 📄 `backtest_calibration.py` — "Real Data එකෙන් Test කරන" Script එක
Coins 40ම, මාස 9ක **historical data** එකෙන්, analysis engine එකම run කරලා, **"මේ analysis score එකට ඇත්තටම real win-rate එක මොකක්ද"** කියලා calculate කරනවා. Result එක `calibration_table.json` ට save වෙනවා — bot එකෙන් **real trading වලදී** මේ file එකම load කරගන්නවා, "raw score" එකට වඩා **verified, real number එකෙන්ම** trade decisions ගන්න.

**Run කරන්නෙ manually, occasionally** (weeks කිහිපයකට සැරයක්):
```bash
python3 backtest_calibration.py
```

### 📄 `backtest_dynamic_tpsl.py` — "Fixed % vs Analysis-based TP/SL" Compare කරන Script එක
Fixed percentage (2%/1%) TP/SL එකෙන්, analysis-derived (chart level) TP/SL එකෙන් — දෙකෙන්ම කොයි එකද වඩා හොඳ කියලා historical data එකෙන් compare කරනවා. Bot එකේ live trading වලට කෙලින්ම බලපාන්නෙ නෑ, ඒත් strategy decisions ගන්න useful.

### 📄 `telegram_notify.py` — Notification System එක
Trade එකකට **මොකද වෙනකොටත්** (open, trailing move, TP1 hit, close), Telegram bot API එකට message එකක් යවනවා. **Bot එකේ trading logic එකට කිසිම බලපෑමක් නෑ** — fail උනත් (Telegram down වුනත්), trade කරන එකට බාධාවක් වෙන්නෙ නෑ.

### 📄 `dashboard_backend.py` — Web Dashboard Backend (Optional)
`trade_state.json`, `live_status.json` files දෙකෙන් data කියවලා, browser එකට (HTML dashboard එකට) serve කරනවා. Binance API එකට කෙලින්ම call කරන්නෙ නෑ — bot එකම දැනටමත් save කරගත්ත data එකම reuse කරනවා.

---

## 13. Bot එකේ Full Process — Start සිට Trade Close වෙනකම්

```
┌─────────────────────────────────────────────────────────┐
│  1. pm2 start main.py                                    │
│     ↓                                                     │
│  2. main.py → config.py එකේ settings ටික load කරනවා       │
│     ↓                                                     │
│  3. Binance API connect (balance check)                  │
│     ↓                                                     │
│  4. calibration_table.json load (real win-rate data)     │
│     ↓                                                     │
│  5. Main Loop පටන්ගන්නවා (24/7, 30s ට සැරයක්)              │
└─────────────────────────────────────────────────────────┘
                          ↓
        ┌─────────────────────────────────┐
        │   Coins 40ම, එකින් එක Scan       │
        └─────────────────────────────────┘
                          ↓
   Timeframe 3ම (4h/1h/15m) chart data fetch
                          ↓
   5 Analysis Tools run (Order Block, FVG, Liquidity,
                          ICT/SMC, Market Structure)
                          ↓
   Timeframe 3ම, coin එකකටම, 5න් 3ක් agree වෙනවද?
        ↓ නෑ                              ↓ ඔව්
     Skip, ඊළඟ coin එකට             BUY/SELL Candidate
                                            ↓
                            Calibration table එකෙන් real
                            profit_chance එක check කරනවා
                                            ↓
                        ≥45%? ┌──────┴──────┐ <45%
                              ↓                    ↓
                    Trading hours window        Reject, ලොග් වෙනවා
                    ඇතුළෙද?                       (📈 diagnostic log)
                    ┌────┴────┐
                    ↓ ඔව්        ↓ නෑ
              ┌───────────┐  Skip
              │ TRADE OPEN │
              └───────────┘
                    ↓
    Position size calculate (margin × leverage, risk-capped)
    SL/TP calculate (analysis-derived, ශක්තිමත්ම level එකෙන්)
                    ↓
    Binance එකට real Order + STOP_MARKET (SL) යවනවා
                    ↓
    🟢 Telegram: "TRADE OPENED" message එක යනවා
                    ↓
┌───────────────────────────────────────────────────┐
│         Trade Management (5s ට සැරයක්ම)             │
├───────────────────────────────────────────────────┤
│  • Price update                                    │
│  • +0.5% profit ගියොත් → Trailing Stop ACTIVATED   │
│    → 🔺 Telegram message                            │
│  • Price තව ඉහළට → SL push (ATR-based distance)     │
│    → 🔺 Telegram message (හැම move එකකටම)            │
│  • TP1 level එකට hit උනොත් →                        │
│      Fresh analysis run → 🎯 Telegram message       │
│      ┌────────────┴────────────┐                   │
│      ↓ Confirm                  ↓ Confirm නෑ        │
│  SL = TP1 (lock)          TP1 එකේම Close            │
│  TP2 = අලුත් level          🔒 Telegram message      │
│  ✅ Telegram message                                │
└───────────────────────────────────────────────────┘
                    ↓
      SL හෝ TP price එකට hit වුනාම
                    ↓
      Trade Close, Binance position close order
                    ↓
      🟢/🔴 Telegram: "TRADE CLOSED" (Profit/Loss %)
                    ↓
      trade_state.json, live_status.json update
                    ↓
      Loop එක continue වෙනවා (coin ඊළඟ scan cycle එකට)
```

### සරලවම කිව්වොත්:

1. **Bot එක Start වෙනවා** → Settings load, Binance connect
2. **24/7 Scan** → Coins 40ම, hours 30s ට සැරයක්
3. **Analysis** → Tools 5ක්, timeframe 3ක්, coin එකින් එකටම check
4. **Filter** → Tools match + Real win-rate (calibration) + Trading hours
5. **Trade Open** → SL/TP set (analysis-derived), Telegram notify
6. **Manage** → Trailing stop, TP1→TP2 reanalysis, continuous monitoring
7. **Close** → SL/TP hit, Telegram notify, records save

---

## 🎉 සම්පූර්ණයි!

මේ document එකේ තියෙන steps ටික **පිළිවෙළින්ම** follow කලොත්, Bot එක VPS එකේ, background එකේ, permanently, Telegram notifications එක්කම run වෙන්න ඕන.

**ප්‍රශ්නයක් ආවොත්** — Section 11 (Common Errors) එක බලන්න, එහෙමත් නැත්නම් `pm2 logs my-binance-bot --err` කරලා exact error message එකම බලන්න.
