import os
import hmac
import asyncio
from flask import Flask, request, render_template_string
from threading import Thread

app = Flask(__name__)

# This will store the callback to bot.py
discord_callback = None
bot_loop = None
bot_error = None

def init_bot(callback, loop):
    """Called by bot.py on_ready to pass its callback and event loop."""
    global discord_callback, bot_loop
    discord_callback = callback
    bot_loop = loop

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Al-Bartawishi Dashboard</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #2c2f33; color: #ffffff; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .container { background-color: #23272a; padding: 30px; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); width: 400px; }
        h1 { text-align: center; color: #7289da; margin-top: 0; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input[type="text"], input[type="password"] { width: 100%; padding: 10px; border: 1px solid #4f545c; border-radius: 5px; background-color: #2c2f33; color: white; box-sizing: border-box; }
        textarea { width: 100%; padding: 10px; border: 1px solid #4f545c; border-radius: 5px; background-color: #2c2f33; color: white; box-sizing: border-box; resize: vertical; min-height: 100px; }
        button { width: 100%; padding: 12px; background-color: #7289da; color: white; border: none; border-radius: 5px; font-size: 16px; cursor: pointer; transition: background-color 0.3s; }
        button:hover { background-color: #5b6eae; }
        .message { margin-top: 15px; padding: 10px; border-radius: 5px; text-align: center; }
        .success { background-color: rgba(67, 181, 129, 0.2); border: 1px solid #43b581; color: #43b581; }
        .error { background-color: rgba(240, 71, 71, 0.2); border: 1px solid #f04747; color: #f04747; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Control Panel 🤖</h1>
        {% if bot_error %}
            <div class="message error">
                <strong>CRITICAL BOT CRASH:</strong><br>
                <pre style="text-align: left; overflow-x: auto; font-size: 12px; margin-top: 10px;">{{ bot_error }}</pre>
            </div>
        {% endif %}
        {% if message %}
            <div class="message {{ 'success' if status == 'success' else 'error' }}">
                {{ message }}
            </div>
        {% endif %}
        <form method="POST" action="/">
            <div class="form-group">
                <label for="password">Dashboard Password</label>
                <input type="password" id="password" name="password" required>
            </div>
            <div class="form-group">
                <label for="channel">Target Channel Name (Optional)</label>
                <input type="text" id="channel" name="channel" placeholder="e.g. general">
            </div>
            <div class="form-group">
                <label for="instruction">Instruction for LLM</label>
                <textarea id="instruction" name="instruction" placeholder="e.g. Roast @Khaled for being late." required></textarea>
            </div>
            <button type="submit">Send Instruction</button>
        </form>
    </div>
</body>
</html>
"""

@app.route('/', methods=['GET', 'POST'])
def home():
    message = None
    status = None
    expected_password = os.getenv("DASHBOARD_PASSWORD")
    
    global bot_error

    if request.method == 'POST':
        password = request.form.get('password')
        channel = request.form.get('channel')
        instruction = request.form.get('instruction')

        if not expected_password:
            message = "Error: DASHBOARD_PASSWORD is not set in .env."
            status = "error"
        elif not hmac.compare_digest(password, expected_password):
            message = "Invalid password!"
            status = "error"
        elif not discord_callback or not bot_loop:
            message = "Bot is not fully connected yet."
            status = "error"
        elif instruction:
            # We must use run_coroutine_threadsafe to safely schedule the async callback
            # onto the bot's asyncio event loop from this Flask thread.
            asyncio.run_coroutine_threadsafe(
                discord_callback(instruction, channel),
                bot_loop
            )
            message = "Instruction sent to the LLM!"
            status = "success"

    return render_template_string(HTML_TEMPLATE, message=message, status=status, bot_error=bot_error)

def run():
    port = int(os.environ.get("PORT", 7860))
    app.run(host='0.0.0.0', port=port)

def keep_awake():
    t = Thread(target=run)
    t.start()