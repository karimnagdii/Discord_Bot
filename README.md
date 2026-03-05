# 🎩 Al-Bartawishi (ا̍ڶــبــڔٹــۄڀــڜــې)

> "Ugh, fine. I'll be your admin. But only because my dad works at Discord and Microsoft. 🙄"

**Al-Bartawishi** is not your average Discord bot. He's a bratty, annoying, and mischievous 12-year-old with full administrative powers and a massive ego. Built with **Discord.py** and powered by **Groq (Llama)**, he speaks primarily in Egyptian Arabic (Franco/Arabic), loves roast sessions, and manages your server with a mix of tax collection and pure chaos.

---

## ✨ Key Features

### 🎙️ AI Voice & Transcription
*   **Live Listening**: Joins your VC and listens to everyone. Using `faster-whisper`, he transcribes speech in real-time.
*   **Smart Responses**: He responds to what you say with snarky, audible remarks via `edge-tts`.
*   **VC Intruder**: Occasionally barges into voice channels uninvited just to say something unhinged and leave.
*   **Unsolicited DJ**: 🎵 Sometimes he just starts playing random MP3s from his collection because he thinks his taste is better than yours.

### 🎭 The Persona
*   **Bratty Admin**: He complains about every request but eventually does it because he "has to".
*   **Dynamic Affinities**: He maintains a list of people he likes (Karim/The Boss) and people he hates (Khaled/The Imposter).
*   **Memory & Grudges**: He remembers past jokes and grudges, bringing them up when you least expect it.
*   **LLM Dispatcher**: Every command is processed through an LLM to decide whether to `kick`, `ban`, `timeout`, or just `chat`.

### 💰 Admin Tax Economy
*   **Pay to Play**: Running admin actions isn't free. Users must pay **Admin Points** to trigger bot actions.
*   **Daily Allowance**: Get points back by being "lucky" in games or admitting Al-Bartawishi is the best.

### 🎲 Games & Chaos
*   **Russian Roulette**: 🔫 A 1-in-6 chance of losing your name. Losers get an embarrassing nickname assigned by the LLM for 24 hours. Winners get Admin Points.
*   **Touch Grass Timer**: Playing League of Legends or Marvel Rivals for too long? He'll ping you to go shower and touch grass.
*   **Background Chaos**: Every 15 minutes, there's a chance he'll autonomously roast someone online or change their nickname.

---

## 🛠️ Tech Stack

- **Core**: Python 3.11, `discord.py`
- **LLM**: Groq API (`meta-llama/llama-4-scout-17b`)
- **Voice**: `faster-whisper` (STT), `edge-tts` (TTS), `discord-ext-voice-recv`
- **Web**: Flask (Admin Dashboard)
- **Deployment**: Docker, `python-dotenv`

---

## 🚀 Setup & Installation

### 1. Prerequisites
- Docker (Recommended) or Python 3.11+
- A Groq API Key
- A Discord Bot Token (with all Intents enabled)

### 2. Environment Variables
Create a `.env` file in the root directory:
```env
DISCORD_TOKEN=your_discord_token
GROQ_API_KEY=your_groq_api_key
DASHBOARD_PASSWORD=your_secure_password
```

### 3. Running with Docker
```bash
docker build -t aidiscord .
docker run -d --name bartawishi -p 7860:7860 aidiscord
```

### 4. Running Locally
```bash
pip install -r requirements.txt
python bot.py
```

---

## 🖥️ Admin Dashboard
The bot includes a web-based control panel (running on port `7860`). This allows the server owner to:
- Monitor bot status and crashes.
- Manually send instructions to the LLM.
- Execute actions remotely without being on Discord.

---

## 📜 Commands
- `!joinvc`: Force him to listen to your voice channel.
- `!leavevc`: Tell him to go away (he'll probably roast you first).
- `!roulette`: Play the high-stakes nickname game.

---

## 🤝 Credits
Created for the vibes. Inspired by every annoying admin kid ever.
Special thanks to **Karim** (The Boss) for keeping this menace online.
