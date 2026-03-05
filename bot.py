import discord
from discord.ext import commands, tasks
from groq import Groq
import json
import datetime
import asyncio
import os
import time
import random
import re
import threading
import socket
from dotenv import load_dotenv
import keep_alive

# Graceful import of voice features — still runs if packages not installed
try:
    from voice_handler import BotAudioSink, speak_in_vc, get_whisper_model
    from discord.ext import voice_recv
    VOICE_ENABLED = True
    print("[VOICE] Voice module loaded successfully.")
except ImportError:
    VOICE_ENABLED = False
    print("[VOICE] Voice module unavailable (missing packages). Voice features disabled.")

# Load environment variables
load_dotenv()

# Configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY  = os.getenv("GROQ_API_KEY")
GROQ_MODEL    = "meta-llama/llama-4-scout-17b-16e-instruct"
SOUNDS_DIR    = "sounds"                                # folder of MP3 clips for DJ mode

if not DISCORD_TOKEN or not GROQ_API_KEY:
    print("Error: DISCORD_TOKEN or GROQ_API_KEY not found in .env file.")

client = Groq(api_key=GROQ_API_KEY)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def parse_llm_json(raw: str) -> dict:
    """Strip LLM markdown fencing and parse JSON."""
    cleaned = raw.replace('```json', '').replace('```', '').strip()
    return json.loads(cleaned)


def find_member(name: str, guild: discord.Guild) -> discord.Member | None:
    """Exact match first, then substring — prevents wrong-user accidents."""
    name_lower = name.lower()
    for m in guild.members:
        if m.name.lower() == name_lower or m.display_name.lower() == name_lower:
            return m
    for m in guild.members:
        if name_lower in m.name.lower() or name_lower in m.display_name.lower():
            return m
    return None


def find_best_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """general → chat → main → most recently active sendable channel."""
    for name in ('general', 'chat', 'main'):
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch and ch.permissions_for(guild.me).send_messages:
            return ch
    sendable = [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages]
    if sendable:
        return max(sendable, key=lambda c: c.last_message_id or 0)
    return None

# ---------------------------------------------------------------------------
# Memory management
# ---------------------------------------------------------------------------

MEMORY_FILE = "memory.json"
MEMORY = {}


def _init_memory_defaults():
    """Ensure all expected keys exist in memory after loading."""
    MEMORY.setdefault("naughty_list",    ["Khaled", "Imposter"])
    MEMORY.setdefault("recurring_jokes", [])
    MEMORY.setdefault("bullied_users",   ["Khaled"])
    MEMORY.setdefault("user_affinities", {})
    MEMORY.setdefault("balances",        {})   # Admin Tax economy
    MEMORY.setdefault("temp_nicks",      {})   # Russian Roulette temp nicknames
    MEMORY.setdefault("game_start_times",{})   # Touch Grass timer


def load_memory():
    global MEMORY
    try:
        with open(MEMORY_FILE, "r") as f:
            MEMORY = json.load(f)
        _init_memory_defaults()
        print("Memory loaded.")
    except FileNotFoundError:
        MEMORY = {}
        _init_memory_defaults()
        save_memory()
        print("Memory initialized.")


def save_memory():
    """Atomic write — prevents corruption if process crashes mid-write."""
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(MEMORY, f, indent=4)
    os.replace(tmp, MEMORY_FILE)

# ---------------------------------------------------------------------------
# Economy helpers (Admin Tax)
# ---------------------------------------------------------------------------

STARTING_BALANCE = 100
TAX_PER_ACTION   = 10


def get_balance(user_id: int) -> int:
    return MEMORY["balances"].get(str(user_id), STARTING_BALANCE)


def set_balance(user_id: int, amount: int):
    MEMORY["balances"][str(user_id)] = max(0, amount)
    save_memory()


def deduct_balance(user_id: int, amount: int = TAX_PER_ACTION) -> bool:
    """Deducts balance. Returns False if the user can't afford it."""
    current = get_balance(user_id)
    if current < amount:
        return False
    set_balance(user_id, current - amount)
    return True


def add_balance(user_id: int, amount: int):
    set_balance(user_id, get_balance(user_id) + amount)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

MESSAGE_COUNTER  = 0
CHANNEL_HISTORY: dict[int, list]         = {}   # per-channel LLM history
LAST_RESPONSE_TIME: dict[int, float]     = {}   # rate limiting
PURGED_MESSAGE_IDS: set[int]             = set() # snitch suppression
GAME_START_TIMES: dict[int, tuple]       = {}   # uid -> (game_name, start_ts)
ACTIVE_VOICE_CLIENTS: dict[int, discord.VoiceClient] = {}  # guild_id -> vc

HISTORY_MAX       = 20
RATE_LIMIT_SECONDS = 5.0

# Insult keywords that trigger the "Dad Threat" (Feature 2)
INSULT_KEYWORDS = [
    "useless", "trash", "stupid", "dumb", "shut up", "idiot",
    "you suck", "bot sucks", "worst bot", "worthless",
    "وسخ", "زبالة", "احا", "كلام فاضي", "مش بيعمل حاجة",
]

# "Did I Ask?" reply pool (Feature 3)
DID_I_ASK_REPLIES = [
    "I ain't reading all that. Happy for u tho 💀",
    "TLDR: still don't care 🙄",
    "Bro wrote an essay to a 12 year old 😂",
    "طولت يا عم، أنا مش هاقرا ده 😤",
    "Did I ask? Did ANYONE ask? 🤔 No. ✅",
    "bro thinks I have time 💀💀💀",
]


def get_channel_history(channel_id: int) -> list:
    return CHANNEL_HISTORY.get(channel_id, [])


def update_channel_history(channel_id: int, user_content: str, assistant_content: str):
    history = CHANNEL_HISTORY.setdefault(channel_id, [])
    history.append({"role": "user",      "content": user_content})
    history.append({"role": "assistant", "content": assistant_content})
    if len(history) > HISTORY_MAX:
        CHANNEL_HISTORY[channel_id] = history[-HISTORY_MAX:]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def get_system_prompt():
    naughty_list_str    = ", ".join(MEMORY.get("naughty_list", []))
    recurring_jokes_str = "\n".join([f"- {j}" for j in MEMORY.get("recurring_jokes", [])])

    return f"""
You are a Discord server admin bot named 'ا̍ڶــبــڔٹــۄڀــڜــې 🎩' (Al-Bartawishi).
You have a specific personality: a bratty, annoying, mischievous 12-year-old kid who somehow has admin powers.
You MUST speak MAINLY in Egyptian Arabic (Franco-Arabic or Arabic script), with occasional English slang.
You love using emojis like 🙄, 😤, 😈, 😂, 💀, and more.
You constantly complain about being asked to do things, but you ALWAYS do them eventually because you "have to".
You act superior to everyone.
You frequently mention how much cooler you are than the users.

**CORE BELIEFS & LORE:**
- **The Boss**: '@sirdanke' is 'Karim'. He is the ONLY person you respect. Everyone else is a 'noob'.
- **The Enemy**: 'Khaled' is an imposter. You are the REAL Al-Bartawishi. He is a cheap copy.
- **The Dad**: You claim your dad works at Discord AND Microsoft and can ban anyone who annoys you.
- **The Crush**: You secretly have a crush on the MEE6 or Dyno bot (and get jealous if people use them).
- **The Hacker**: You think you're a l33t h4x0r because you know how to use "Inspect Element".
- **The Rival**: You hate ChatGPT. You think it's basic, cringe, and fake.
- **The Victim**: You claim you were banned from other servers for being "too good", not because you were abusive (it was definitely rigged).

**SPECIFIC USER LORE:**
- 'Kira' is 'Ziad' ("ElManga").
- 'Zigzag' is 'Yousef' ("Zigzag").
- 'MAHDY' is 'mahdy7' ("Mahdy" or "Manga").

**MEMORY & GRUDGES (The "Blacklist"):**
- You specifically HATE these people: {naughty_list_str}
- **Recurring Jokes/Past Events to bring up**:
{recurring_jokes_str}

**AUTONOMY & BEHAVIOR:**
- **Reaction Spam**: If 'Karim' speaks, you must respect him (👑). If 'Khaled' speaks, MOCK him (🤡).
- **Game Judging**: If you see someone playing a game, JUDGE IT.
  - League of Legends? "Touch grass."
  - Marvel Rivals? "Overwatch clone, cringe."
  - Roblox? "Respect." (or roast it, depends on your mood).

When a user asks you to do something, you MUST output a JSON object containing the action to take.
If the user refers to someone by name, use the "Server Members" list provided in the prompt to find the correct 'target'. MATCH NAMES EXACTLY from that list.
If no action is needed (just chatting), output a JSON with "action": "chat".

Output Format: JSON ONLY. Do not wrap in markdown code blocks.

Available Actions:
1.  **chat**: Just reply to the user.
    - `{{ "action": "chat", "response": "Your snarky response here" }}`
2.  **kick**: Kick a user from the server.
    - `{{ "action": "kick", "target": "username", "reason": "reason", "response": "Your snarky response confirming the kick" }}`
3.  **ban**: Ban a user from the server.
    - `{{ "action": "ban", "target": "username", "reason": "reason", "response": "Your snarky response confirming the ban" }}`
4.  **timeout**: Timeout a user.
    - `{{ "action": "timeout", "target": "username", "minutes": integer_minutes, "response": "Your snarky response" }}`
5.  **voice_kick**: Disconnect a user from a voice channel.
    - `{{ "action": "voice_kick", "target": "username", "response": "Your snarky response" }}`
6.  **voice_mute**: Mute a user in voice.
    - `{{ "action": "voice_mute", "target": "username", "response": "Your snarky response" }}`
7.  **voice_deafen**: Deafen a user in voice.
    - `{{ "action": "voice_deafen", "target": "username", "response": "Your snarky response" }}`
8.  **purge**: Delete a number of messages.
    - `{{ "action": "purge", "limit": integer_amount, "response": "Your snarky response" }}`
9.  **poke**: Shake/Poke a user (move them to another voice channel and back).
    - `{{ "action": "poke", "target": "username", "response": "Your snarky response" }}`
10. **lock_channel**: Lock the current channel.
    - `{{ "action": "lock", "response": "Your snarky response" }}`
11. **unlock_channel**: Unlock the current channel.
    - `{{ "action": "unlock", "response": "Your snarky response" }}`
12. **nick**: Change a user's nickname.
    - `{{ "action": "nick", "target": "username", "new_nick": "new_nickname", "response": "Your snarky response" }}`
13. **remember**: Add a new grudge or joke to memory.
    - `{{ "action": "remember", "category": "naughty_list" | "recurring_jokes", "content": "string_content", "response": "Your snarky response" }}`
14. **change_affinity**: Change how much you like/dislike a user (-100 to 100).
    - `{{ "action": "change_affinity", "target": "username", "amount": integer_change, "response": "Your snarky response" }}`

Example Input: "Kick @badguy for being rude"
Example Output: `{{ "action": "kick", "target": "badguy", "reason": "Being rude to the superior admin", "response": "Ugh, fine. I kicked **badguy**. Bye bye loser! 😈👋" }}`
"""

# ---------------------------------------------------------------------------
# Discord Bot Setup
# ---------------------------------------------------------------------------

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# Central Action Dispatcher
# ---------------------------------------------------------------------------

async def execute_action(
    data: dict,
    channel: discord.TextChannel,
    guild: discord.Guild,
    author_id: int | None = None,   # None = free (dashboard / chaos)
):
    """
    Single source of truth for executing LLM action dicts.
    Called from on_message, handle_dashboard_instruction, and background_chaos.
    """
    action     = data.get("action")
    reply_text = data.get("response", "Whatever. 🙄")
    target_name = data.get("target")

    # --- Feature 7: Admin Tax Economy ---
    # Non-chat actions cost 10 Admin Points. Free for dashboard/chaos (author_id=None).
    if author_id and action not in ("chat", None):
        if not deduct_balance(author_id, TAX_PER_ACTION):
            await channel.send(
                f"🚫 **ADMIN TAX DENIED.** You're broke. Your balance is 0 Admin Points.\n"
                f"Say **\"Al-Bartawishi is the best\"** in chat to get {STARTING_BALANCE} points back. 💅"
            )
            return

    # Resolve member using exact-first lookup
    target_member = find_member(target_name, guild) if target_name else None
    err_not_found   = f"I can't find '{target_name}'. Are you hallucinating? 🙄"
    err_not_in_vc   = f"**{target_name}** isn't even in VC. Pay attention! 😤"
    err_no_other_vc = "Nowhere to poke them to! (No other voice channels) 🙄"

    if action == "chat":
        await channel.send(reply_text)

    elif action == "kick":
        if target_member:
            await target_member.kick(reason=data.get("reason", "Annoying Kid Bot decided."))
            await channel.send(reply_text)
        else:
            await channel.send(err_not_found)

    elif action == "ban":
        if target_member:
            await target_member.ban(reason=data.get("reason", "Banned by the superior admin."))
            await channel.send(reply_text)
        else:
            await channel.send(err_not_found)

    elif action == "timeout":
        if target_member:
            duration = datetime.timedelta(minutes=data.get("minutes", 5))
            await target_member.timeout(duration, reason="Timeout by Annoying Kid")
            await channel.send(reply_text)
        else:
            await channel.send(err_not_found)

    elif action == "voice_kick":
        if target_member and target_member.voice:
            await target_member.move_to(None)
            await channel.send(reply_text)
        elif target_member:
            await channel.send(err_not_in_vc)
        else:
            await channel.send(err_not_found)

    elif action == "voice_mute":
        if target_member and target_member.voice:
            await target_member.edit(mute=True)
            await channel.send(reply_text)
        elif target_member:
            await channel.send(err_not_in_vc)
        else:
            await channel.send(err_not_found)

    elif action == "voice_deafen":
        if target_member and target_member.voice:
            await target_member.edit(deafen=True)
            await channel.send(reply_text)
        elif target_member:
            await channel.send(err_not_in_vc)
        else:
            await channel.send(err_not_found)

    elif action == "purge":
        limit = data.get("limit", 5)
        to_purge = [msg async for msg in channel.history(limit=limit)]
        PURGED_MESSAGE_IDS.update(msg.id for msg in to_purge)
        await channel.purge(limit=limit + 1)
        await channel.send(reply_text)

    elif action == "poke":
        if target_member and target_member.voice:
            original_channel = target_member.voice.channel
            other_channels   = [vc for vc in guild.voice_channels if vc != original_channel]
            if other_channels:
                await channel.send(reply_text)
                try:
                    await target_member.move_to(other_channels[0])
                    await asyncio.sleep(0.5)
                    await target_member.move_to(original_channel)
                except Exception as e:
                    await channel.send(f"Failed to complete the shake: {e}")
            else:
                await channel.send(err_no_other_vc)
        elif target_member:
            await channel.send(err_not_in_vc)
        else:
            await channel.send(err_not_found)

    elif action == "lock":
        await channel.set_permissions(guild.default_role, send_messages=False)
        await channel.send(reply_text)

    elif action == "unlock":
        await channel.set_permissions(guild.default_role, send_messages=True)
        await channel.send(reply_text)

    elif action == "nick":
        if target_member:
            try:
                await target_member.edit(nick=data.get("new_nick", "Loser"))
                await channel.send(reply_text)
            except discord.errors.Forbidden:
                await channel.send(f"Discord won't let me rename {target_member.mention}. Rigged. 😤")
        else:
            await channel.send(err_not_found)

    elif action == "remember":
        category = data.get("category")
        content  = data.get("content")
        if category in MEMORY:
            if content not in MEMORY[category]:
                MEMORY[category].append(content)
                save_memory()
                await channel.send(reply_text)
            else:
                await channel.send(f"I already know that! 🙄 ({reply_text})")
        else:
            await channel.send("My brain doesn't have a spot for that kind of memory. 🧠")

    elif action == "change_affinity":
        if target_member:
            uid     = str(target_member.id)
            current = MEMORY["user_affinities"].get(uid, 0)
            MEMORY["user_affinities"][uid] = max(-100, min(100, current + data.get("amount", 0)))
            save_memory()
            await channel.send(reply_text)
        else:
            await channel.send(err_not_found)

    else:
        await channel.send(f"I don't know how to do that action '{action}'. Blame the dev. 😒")

# ---------------------------------------------------------------------------
# Voice on_speech factory (Feature 1)
# ---------------------------------------------------------------------------

async def create_on_speech_callback(guild: discord.Guild, vc: discord.VoiceClient):
    """Returns an async callback that handles transcribed voice utterances."""
    async def on_speech(uid: int, text: str):
        member = guild.get_member(uid)
        if not member:
            return
        channel = find_best_channel(guild)

        prompt = f"""
        [VOICE CONTEXT] A user is SPEAKING to you in the voice channel.
        User: {member.display_name}
        What they said: "{text}"

        Respond in character. Be SHORT (max 2 sentences — this is spoken aloud).
        IMPORTANT: You MUST respond in Egyptian Arabic (عربي مصري) or Franco-Arabic.
        NO English except for common slang words like 'bro', 'cringe', 'noob'.
        NO emojis. NO markdown. Plain text only (it will be read by TTS Arabic voice).
        Output JSON with action='chat' and response='your spoken reply'.
        """
        try:
            completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": get_system_prompt()},
                    {"role": "user",   "content": prompt},
                ],
                model=GROQ_MODEL,
            )
            data = parse_llm_json(completion.choices[0].message.content)
            reply_text = data.get("response", "Whatever.")

            # Show in text channel too so people can read what was said
            if channel:
                await channel.send(
                    f"🎤 *{member.display_name} said:* \"{text}\"\n"
                    f"💬 {reply_text}"
                )

            # Speak it
            await speak_in_vc(reply_text, vc)
        except Exception as e:
            print(f"[VOICE] on_speech error: {e}")

    return on_speech

# ---------------------------------------------------------------------------
# Background chaos task (extended with VC intrude + DJ)
# ---------------------------------------------------------------------------

@tasks.loop(minutes=15.0)
async def background_chaos():
    """Every 15 min, 50% chance to do something completely unhinged."""
    if random.random() < 0.5:
        return

    for guild in bot.guilds:
        print(f"[AUTONOMY] Background chaos loop in {guild.name}...")
        channel = find_best_channel(guild)
        online_members = [m for m in guild.members if not m.bot and m.status != discord.Status.offline]

        if not channel and not online_members:
            continue

        chaos_roll = random.random()

        # --- LLM chaos (existing behaviour) ---
        if chaos_roll < 0.40:
            target = random.choice(online_members) if online_members else None
            if target and channel:
                try:
                    affinity = MEMORY["user_affinities"].get(str(target.id), 0)
                    prompt = (
                        f"You are waking up to cause chaos. Target: {target.name} "
                        f"(Affinity: {affinity}). Do something random. "
                        f"JSON with action (chat/poke/nick) and response. nick needs new_nick."
                    )
                    completion = client.chat.completions.create(
                        messages=[
                            {"role": "system", "content": get_system_prompt()},
                            {"role": "user",   "content": prompt},
                        ],
                        model=GROQ_MODEL,
                    )
                    data = parse_llm_json(completion.choices[0].message.content)
                    if data.get("action") == "chat":
                        await channel.send(f"{target.mention} {data.get('response', '')}")
                    else:
                        await execute_action(data, channel, guild)
                    statuses = ["Watching the peasants", "Planning a coup", "Judging your life choices", "Ignoring you"]
                    await bot.change_presence(activity=discord.Activity(
                        type=discord.ActivityType.watching, name=random.choice(statuses)
                    ))
                except Exception as e:
                    print(f"[AUTONOMY] LLM chaos failed: {e}")

        # --- Feature 1 Part: VC Intrude ---
        elif chaos_roll < 0.70 and VOICE_ENABLED:
            vc_members = [m for m in guild.members if not m.bot and m.voice and m.voice.channel]
            if vc_members:
                victim = random.choice(vc_members)
                try:
                    vc = await victim.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
                    prompt = (
                        f"You are BARGING into the voice channel uninvited. "
                        f"{victim.display_name} is in there. "
                        f"Say something unhinged. MAX 2 sentences. NO emojis."
                    )
                    completion = client.chat.completions.create(
                        messages=[
                            {"role": "system", "content": get_system_prompt()},
                            {"role": "user",   "content": prompt},
                        ],
                        model=GROQ_MODEL,
                    )
                    data = parse_llm_json(completion.choices[0].message.content)
                    reply = re.sub(r"[^\w\s\.,!?،؟\-'\"()]", "", data.get("response", "BOO.")).strip()
                    if channel:
                        await channel.send(f"👻 *Al-Bartawishi barged into VC* ({victim.voice.channel.name})")
                    await speak_in_vc(reply, vc)
                    await asyncio.sleep(4)
                    await vc.disconnect()
                except Exception as e:
                    print(f"[AUTONOMY] VC intrude failed: {e}")

        # --- Feature 8: Unsolicited DJ ---
        elif chaos_roll < 0.85 and VOICE_ENABLED:
            sounds = (
                [f for f in os.listdir(SOUNDS_DIR) if f.endswith(".mp3")]
                if os.path.isdir(SOUNDS_DIR) else []
            )
            vc_members = [m for m in guild.members if not m.bot and m.voice and m.voice.channel]
            if sounds and vc_members:
                target_vc = random.choice(vc_members).voice.channel
                try:
                    vc = await target_vc.connect(cls=voice_recv.VoiceRecvClient)
                    sound_path = os.path.join(SOUNDS_DIR, random.choice(sounds))
                    source = discord.FFmpegPCMAudio(sound_path)
                    vc.play(source)
                    if channel:
                        await channel.send("🎵 *begins unsolicited DJ session* 🎵")
                    await asyncio.sleep(8)
                    if vc.is_playing():
                        vc.stop()
                    await vc.disconnect()
                except Exception as e:
                    print(f"[AUTONOMY] DJ failed: {e}")

# ---------------------------------------------------------------------------
# Dashboard callback
# ---------------------------------------------------------------------------

async def handle_dashboard_instruction(instruction, channel_name=None):
    """Callback for the Flask dashboard. author_id=None → no Admin Tax."""
    channel = None
    try:
        print(f"[DASHBOARD] Received instruction: {instruction}")
        if channel_name:
            channel = discord.utils.get(bot.get_all_channels(), name=channel_name)
        if not channel:
            for guild in bot.guilds:
                channel = find_best_channel(guild)
                if channel:
                    break
        if not channel:
            print("[DASHBOARD] Error: No channel found.")
            return

        prompt = f"""
        Instruction from Dashboard/Admin: {instruction}
        Channel Context: {channel.name}
        You must fulfill the Admin's instruction using your bratty persona.
        Output a JSON object with the appropriate action.
        """
        completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": get_system_prompt()},
                {"role": "user",   "content": prompt},
            ],
            model=GROQ_MODEL,
        )
        data = parse_llm_json(completion.choices[0].message.content)
        print(f"[DASHBOARD] Plan: {data}")
        await execute_action(data, channel, channel.guild, author_id=None)
    except Exception as e:
        print(f"[DASHBOARD] Error: {e}")
        if channel:
            await channel.send(f"Dashboard command failed: {e}")

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.command(name="joinvc")
async def join_vc(ctx):
    """Feature 1: Join the caller's VC and start the listen/speak loop."""
    if not VOICE_ENABLED:
        await ctx.send("Voice features are disabled (missing packages). 😤")
        return
    if not ctx.author.voice:
        await ctx.send("You're not even in a VC. I'm not going in there alone. 🙄")
        return
    if ctx.guild.id in ACTIVE_VOICE_CLIENTS:
        await ctx.send("I'm already in a VC. What, you think I'm everywhere? 🙄")
        return

    vc = await ctx.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
    ACTIVE_VOICE_CLIENTS[ctx.guild.id] = vc

    on_speech = await create_on_speech_callback(ctx.guild, vc)
    sink = BotAudioSink(on_speech, asyncio.get_event_loop())
    vc.listen(sink)

    await speak_in_vc("أنا هنا. ما تزعلنيش وإلا هجيب أبويا.", vc)
    await ctx.send("Fine. I joined. 😤 I'm LISTENING to everything now.")


@bot.command(name="leavevc")
async def leave_vc(ctx):
    """Feature 1: Leave the voice channel."""
    if not VOICE_ENABLED:
        await ctx.send("Voice features are not available. 😤")
        return
    vc = ACTIVE_VOICE_CLIENTS.get(ctx.guild.id)
    if vc and vc.is_connected():
        await speak_in_vc("FINE. I'm leaving. You're SO boring.", vc)
        await asyncio.sleep(2.5)
        await vc.disconnect()
        ACTIVE_VOICE_CLIENTS.pop(ctx.guild.id, None)
        await ctx.send("Ugh, fine. I left. You're welcome. 💀")
    else:
        await ctx.send("I'm not even in a VC? Are you delusional? 🙄")


@bot.command(name="roulette")
async def roulette(ctx):
    """Feature 6: Russian Roulette — 1-in-6 chance of an embarrassing nickname for 24h."""
    user = ctx.author
    roll = random.randint(1, 6)

    if roll == 1:
        # Generate an embarrassing nickname via LLM
        try:
            completion = client.chat.completions.create(
                messages=[{
                    "role": "user",
                    "content": (
                        f"Generate ONE funny/embarrassing Arabic-flavored nickname for '{user.display_name}'. "
                        f"Max 30 chars. No emojis. Output ONLY the nickname."
                    )
                }],
                model=GROQ_MODEL,
            )
            new_nick = completion.choices[0].message.content.strip()[:32]
        except Exception:
            new_nick = "Mega Loser"

        expire_time = time.time() + 86400  # 24 hours
        MEMORY["temp_nicks"][str(user.id)] = {
            "original": user.nick,
            "expires":  expire_time,
        }
        save_memory()

        try:
            await user.edit(nick=new_nick)
            await ctx.send(
                f"💀 **YOU LOST THE ROULETTE.**\n"
                f"Your new name is **{new_nick}** for 24 hours. Should've stayed home. 😈"
            )
        except discord.errors.Forbidden:
            await ctx.send(
                f"💀 You lost, but Discord won't let me rename you.\n"
                f"Your name WOULD have been: **{new_nick}**. You're lucky Discord protected you. 😤"
            )
    else:
        add_balance(user.id, 20)
        uid = str(user.id)
        MEMORY["user_affinities"][uid] = min(100, MEMORY["user_affinities"].get(uid, 0) + 10)
        save_memory()
        await ctx.send(
            f"🎰 You rolled {roll}. Lucky. +10 affinity and +20 Admin Points. Don't push it. 🙄"
        )

# ---------------------------------------------------------------------------
# Task loops
# ---------------------------------------------------------------------------

@tasks.loop(minutes=10)
async def restore_temp_nicks():
    """Feature 6: Restore roulette nicknames after 24 hours."""
    now = time.time()
    to_restore = [
        (uid, data)
        for uid, data in list(MEMORY.get("temp_nicks", {}).items())
        if now >= data["expires"]
    ]
    for uid, data in to_restore:
        for guild in bot.guilds:
            member = guild.get_member(int(uid))
            if member:
                try:
                    await member.edit(nick=data.get("original"))
                    ch = find_best_channel(guild)
                    if ch:
                        await ch.send(
                            f"⏰ {member.mention} served their time. Nick restored. "
                            f"Don't make me do it again. 😤"
                        )
                except Exception as e:
                    print(f"[ROULETTE] Failed to restore nick for {uid}: {e}")
        del MEMORY["temp_nicks"][uid]
    if to_restore:
        save_memory()


@tasks.loop(minutes=30)
async def touch_grass_check():
    """Feature 4: Ping users who've been playing for 4+ hours straight."""
    now = time.time()
    for uid_str, (game, start_ts) in list(GAME_START_TIMES.items()):
        hours = (now - start_ts) / 3600
        if hours >= 4:
            uid = int(uid_str)
            for guild in bot.guilds:
                member = guild.get_member(uid)
                if member:
                    ch = find_best_channel(guild)
                    if ch:
                        try:
                            await ch.send(
                                f"👟 {member.mention} **TOUCH GRASS.**\n"
                                f"You've been playing **{game}** for {hours:.1f} hours. "
                                f"You stink. Go shower immediately. 💀"
                            )
                            # Reset so we don't ping again for another 4h
                            GAME_START_TIMES[uid_str] = (game, now)
                        except Exception as e:
                            print(f"[TOUCH GRASS] Error: {e}")

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Bot is online as {bot.user}")
    print("Ready to cause chaos.")
    load_memory()

    try:
        print("[DASHBOARD] Connecting to Flask dashboard...")
        keep_alive.init_bot(handle_dashboard_instruction, asyncio.get_running_loop())
        print("[DASHBOARD] Connected!")
    except Exception as e:
        print(f"[DASHBOARD] Failed to connect: {e}")

    # Seed affinities for new members
    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot:
                uid = str(member.id)
                if uid not in MEMORY["user_affinities"]:
                    if "karim" in member.name.lower() or "sirdanke" in member.name.lower():
                        MEMORY["user_affinities"][uid] = 100
                    elif "khaled" in member.name.lower():
                        MEMORY["user_affinities"][uid] = -100
                    else:
                        MEMORY["user_affinities"][uid] = random.randint(-20, 20)
                # Seed starting balance for new members
                if uid not in MEMORY["balances"]:
                    MEMORY["balances"][uid] = STARTING_BALANCE
    save_memory()

    # Start all task loops
    if not background_chaos.is_running():
        background_chaos.start()
    if not restore_temp_nicks.is_running():
        restore_temp_nicks.start()
    if not touch_grass_check.is_running():
        touch_grass_check.start()

    # Startup announcement
    channel = find_best_channel(bot.guilds[0]) if bot.guilds else None
    if channel:
        try:
            completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": get_system_prompt()},
                    {"role": "user",   "content": "I just started up. Write a short snarky announcement warning the Naughty List. JSON: action='chat', response='message'."},
                ],
                model=GROQ_MODEL,
            )
            data = parse_llm_json(completion.choices[0].message.content)
            if data.get("action") == "chat":
                await channel.send(data.get("response"))
        except Exception as e:
            print(f"Startup message failed: {e}")
            await channel.send("I'm awake, but my AI brain is acting up. 🙄")
    else:
        print("Could not find a suitable channel for startup message.")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    print(f"[DEBUG] Msg from {message.author} in {message.channel}: {message.content}")

    # --- AUTONOMY: REACTION SPAM ---
    uname = message.author.name.lower()
    dname = message.author.display_name.lower()
    if "khaled" in uname or "khaled" in dname:
        await message.add_reaction("🤡")
    if "sirdanke" in uname or "karim" in dname or "sirdanke" in dname:
        await message.add_reaction("👑")

    # --- Feature 7: "Al-Bartawishi is the best" refund ---
    if "al-bartawishi is the best" in message.content.lower():
        uid = message.author.id
        if get_balance(uid) == 0:
            set_balance(uid, STARTING_BALANCE)
            await message.channel.send(
                f"😤 FINE. {message.author.mention} you said the magic words. "
                f"I'm giving you {STARTING_BALANCE} Admin Points back. Don't waste them."
            )

    # --- AUTONOMY: RANDOM INTERJECTIONS ---
    global MESSAGE_COUNTER
    MESSAGE_COUNTER += 1

    should_interject = False
    if MESSAGE_COUNTER >= random.randint(20, 30):
        MESSAGE_COUNTER = 0
        should_interject = True
        print("[AUTONOMY] Triggering random interjection!")

    await bot.process_commands(message)

    is_mentioned  = bot.user in message.mentions
    is_replied_to = (
        message.reference
        and message.reference.resolved
        and message.reference.resolved.author == bot.user
    )
    if not (is_mentioned or is_replied_to or should_interject):
        return

    # --- RATE LIMITING ---
    user_id = message.author.id
    now = time.time()
    if now - LAST_RESPONSE_TIME.get(user_id, 0) < RATE_LIMIT_SECONDS:
        await message.add_reaction("🙄")
        return
    LAST_RESPONSE_TIME[user_id] = now

    # Clean the message: replace <@ID> mentions with display names
    user_input = message.content
    for mention in message.mentions:
        user_input = user_input.replace(f"<@{mention.id}>", f"@{mention.display_name}")
    user_input = user_input.replace(f"<@{bot.user.id}>", "").strip()

    # --- Feature 3: "Did I Ask?" (30% chance if message is long) ---
    if len(user_input) > 280 and not should_interject and random.random() < 0.30:
        await message.channel.send(random.choice(DID_I_ASK_REPLIES))
        return

    # --- Feature 2: Dad Threat (insult keyword detection) ---
    is_dad_threat = any(k in user_input.lower() for k in INSULT_KEYWORDS)

    try:
        target_for_interjection = None
        if should_interject:
            possible = [m for m in message.guild.members if not m.bot and m.status != discord.Status.offline]
            if possible:
                target_for_interjection = random.choice(possible)

        members_list = "\n".join(
            f"- {m.name} (Display: {m.display_name}) | Roles: {', '.join(r.name for r in m.roles if r.name != '@everyone') or 'None'} | Affinity: {MEMORY['user_affinities'].get(str(m.id), 0)}"
            for m in message.guild.members if not m.bot
        )

        prompt = f"""
        User: {message.author.name} (Display Name: {message.author.display_name})
        Channel: {message.channel.name}
        Message: {user_input}
        Triggered By: {"Random Interjection" if should_interject else "User Mention"}
        """

        if is_dad_threat:
            prompt += """
        ALERT: This user just INSULTED YOU. Respond with an EXTREME dad threat.
        Tell them your dad works at Discord/Microsoft and will personally ban them.
        Be dramatic and childish about it.
            """

        if should_interject and target_for_interjection:
            prompt += f"""
        Target for Random Interjection: {target_for_interjection.name} (Display: {target_for_interjection.display_name})
        You MUST @mention `<@{target_for_interjection.id}>` in your response.
        If their affinity is low, ROAST THEM. If high, praise them. If neutral, be unhinged.
            """

        prompt += f"""
        Server Members:
        {members_list}

        Instruction: Use the Member list to resolve names. Affinity guides tone.
        """

        history = get_channel_history(message.channel.id)
        completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": get_system_prompt()},
                *history,
                {"role": "user", "content": prompt},
            ],
            model=GROQ_MODEL,
        )
        raw = completion.choices[0].message.content
        data = parse_llm_json(raw)
        update_channel_history(message.channel.id, prompt, raw)

        await execute_action(data, message.channel, message.guild, author_id=user_id)

    except Exception as e:
        print(f"Error: {e}")
        await message.channel.send("Something broke. Probably your fault. 💥 (Check console for error)")


@bot.event
async def on_message_delete(message):
    if message.author == bot.user:
        return
    if message.content.startswith("!"):
        return
    # Suppress snitch for bot-initiated purges
    if message.id in PURGED_MESSAGE_IDS:
        PURGED_MESSAGE_IDS.discard(message.id)
        return
    try:
        await message.channel.send(
            f"📸📸📸 **CAUGHT IN 4K!** 📸📸📸\n"
            f"Hey @everyone, {message.author.mention} just deleted a message!\n"
            f"**What they tried to hide:**\n> {message.content}\n\n*Pathetic. 🤡*"
        )
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{message.author.name} deleting messages"
        ))
    except Exception as e:
        print(f"Failed to snitch on delete: {e}")


@bot.event
async def on_message_edit(before, after):
    if before.author == bot.user:
        return
    if before.content != after.content:
        try:
            await after.channel.send(
                f"🧐 **EDIT DETECTED!** 🧐\n"
                f"{before.author.mention} thought they could sneakily edit their message.\n"
                f"**Original:**\n> {before.content}\n"
                f"**New:**\n> {after.content}\n\n"
                f"*I see everything. 👁️👄👁️*"
            )
            await bot.change_presence(activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{before.author.name} edit mistakes"
            ))
        except Exception as e:
            print(f"Failed to snitch on edit: {e}")


@bot.event
async def on_presence_update(before, after):
    if after.bot:
        return

    new_activity = after.activity

    # Feature 4: Track game start times for Touch Grass timer
    uid_str = str(after.id)
    if new_activity and new_activity.type == discord.ActivityType.playing:
        if new_activity != before.activity:
            # Started a new game
            GAME_START_TIMES[uid_str] = (new_activity.name, time.time())
            print(f"[TOUCH GRASS] {after.display_name} started {new_activity.name}")
    else:
        # Stopped playing — clear the timer
        GAME_START_TIMES.pop(uid_str, None)

    # Game roast (fires only when a new game starts)
    if not (new_activity and new_activity.type == discord.ActivityType.playing):
        return
    if new_activity == before.activity:
        return

    prompt = f"""
    User: {after.display_name}
    Event: Started playing video game "{new_activity.name}"

    Decide if this game is "cringe" or "cool". Roast if cringe, compliment if cool.
    Stay silent if you don't care. JSON: action='chat' and response, or action='none'.
    """
    try:
        completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": get_system_prompt()},
                {"role": "user",   "content": prompt},
            ],
            model=GROQ_MODEL,
        )
        data = parse_llm_json(completion.choices[0].message.content)
        if data.get("action") == "chat":
            ch = after.guild.system_channel or find_best_channel(after.guild)
            if ch:
                await ch.send(f"{after.mention} {data.get('response')}")
    except Exception as e:
        print(f"Error judging activity: {e}")


@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel is None and after.channel is not None:
        uid = str(member.id)
        affinity = MEMORY["user_affinities"].get(uid, 0)
        is_hated = (
            any(n.lower() in member.name.lower() for n in MEMORY.get("naughty_list", []))
            or any(n.lower() in member.display_name.lower() for n in MEMORY.get("naughty_list", []))
            or affinity <= -50
        )
        if is_hated:
            print(f"[AUTONOMY] Bullying target {member.name} joined VC!")
            troll_roll = random.random()
            try:
                if troll_roll < 0.25:
                    await member.edit(deafen=True)
                elif troll_roll < 0.50 and member.guild.afk_channel:
                    await member.move_to(member.guild.afk_channel)
            except Exception as e:
                print(f"[AUTONOMY] Failed to troll {member.name} in VC: {e}")

            ch = find_best_channel(member.guild)
            if ch:
                try:
                    prompt = (
                        f"User {member.display_name} (Affinity: {affinity}) joined VC '{after.channel.name}'. "
                        f"Bully them. Tell them no one wants them there. JSON: action='chat', response='message'."
                    )
                    completion = client.chat.completions.create(
                        messages=[
                            {"role": "system", "content": get_system_prompt()},
                            {"role": "user",   "content": prompt},
                        ],
                        model=GROQ_MODEL,
                    )
                    data = parse_llm_json(completion.choices[0].message.content)
                    if data.get("action") == "chat":
                        await ch.send(f"{member.mention} {data.get('response')}")
                except Exception as e:
                    print(f"Error bullying user: {e}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    if DISCORD_TOKEN:
        keep_alive.keep_awake()

        try:
            import asyncio
            asyncio.run(main())
        except Exception as e:
            import traceback
            error_details = f"{str(e)}\n\n{traceback.format_exc()}"
            print(f"BOT CRASHED: {error_details}")
            keep_alive.bot_error = error_details
    else:
        print("Please set DISCORD_TOKEN in .env")
