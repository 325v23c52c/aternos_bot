import asyncio
import logging
import os
import re
import shutil
import subprocess

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Токен: краще задати змінною BOT_TOKEN на хостингу, але якщо її нема — береться цей
TOKEN = os.getenv("BOT_TOKEN", "8632361412:AAGs6-N9Ps-2YXd6WRzm4_fiSvgaLb8vtr0")

DEFAULT_NICK = "AFK_BOT"
MAX_SECONDS = 2 * 365 * 24 * 3600   # максимум 2 роки
MAX_BOTS = 5                         # скільки AFK-ботів одночасно (щоб не вбити free-хостинг)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JS_FILE = os.path.join(BASE_DIR, "afk.js")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("afk")

# ---------- Minecraft-частина (mineflayer, працює через Node.js) ----------
AFK_JS = r"""
const mineflayer = require('mineflayer')
const [host, port, version, username] = process.argv.slice(2)

const bot = mineflayer.createBot({
  host, port: parseInt(port), version, username, auth: 'offline'
})

bot.once('spawn', () => {
  console.log('SPAWN')
  // легкий анти-афк: маленький стрибок раз на хвилину
  setInterval(() => {
    try {
      bot.setControlState('jump', true)
      setTimeout(() => bot.setControlState('jump', false), 300)
    } catch (e) {}
  }, 60000)
})
bot.on('kicked', (reason) => console.log('KICKED ' + JSON.stringify(reason)))
bot.on('error', (e) => console.log('ERROR ' + e.message))
bot.on('end', () => { console.log('END'); process.exit(0) })
"""


def prepare_node():
    """Створює afk.js і ставить mineflayer, якщо ще не стоїть."""
    with open(JS_FILE, "w", encoding="utf-8") as f:
        f.write(AFK_JS)
    if not shutil.which("node") or not shutil.which("npm"):
        raise RuntimeError("На хостингу не знайдено Node.js (node/npm). Потрібен хостинг з Node.js.")
    if not os.path.isdir(os.path.join(BASE_DIR, "node_modules", "mineflayer")):
        log.info("Встановлюю mineflayer...")
        subprocess.run(["npm", "install", "mineflayer"], cwd=BASE_DIR, check=True)


# ---------- Парсинг часу ----------
UNITS = {
    # секунди
    "с": 1, "c": 1, "s": 1, "сек": 1, "sec": 1,
    # хвилини
    "х": 60, "x": 60, "хв": 60, "хвил": 60, "мин": 60, "m": 60, "min": 60,
    # години
    "г": 3600, "h": 3600, "год": 3600, "час": 3600,
    # дні
    "д": 86400, "d": 86400, "дн": 86400, "дні": 86400, "день": 86400,
    # тижні
    "т": 604800, "тиж": 604800, "w": 604800,
    # місяці (30 днів)
    "міс": 30 * 86400, "мес": 30 * 86400, "mo": 30 * 86400,
    # роки (365 днів)
    "р": 365 * 86400, "рік": 365 * 86400, "роки": 365 * 86400, "y": 365 * 86400,
}


def parse_time(text: str):
    """'266с', '29г', '1д12г30х', '2р' -> секунди. Повертає None, якщо формат поганий."""
    text = text.lower().replace(" ", "")
    parts = re.findall(r"(\d+)([a-zа-яіїєґ]+)", text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    total = 0
    for num, unit in parts:
        if unit not in UNITS:
            return None
        total += int(num) * UNITS[unit]
    return total


def fmt_duration(sec: int) -> str:
    names = [("р", 365 * 86400), ("міс", 30 * 86400), ("д", 86400), ("г", 3600), ("х", 60), ("с", 1)]
    out = []
    for name, size in names:
        if sec >= size:
            out.append(f"{sec // size}{name}")
            sec %= size
    return " ".join(out) or "0с"


# ---------- Сесії ----------
sessions = {}  # user_id -> {"proc": Process|None, "stopped": bool, "task": Task}

HELP_TEXT = (
    "👋 Привіт! Я AFK-бот для Minecraft. Я заходжу на твій сервер і стою АФК потрібний час.\n\n"
    "📋 Команди:\n"
    "/start — показати це повідомлення\n"
    "/start_afk <айпі> <версія> <час> [нікнейм] — зайти на сервер\n"
    "/stop_afk — вийти з сервера\n\n"
    "⏱ Час (максимум 2 роки):\n"
    "с — секунди, х — хвилини, г — години, д — дні, міс — місяці, р — роки\n"
    "Можна комбінувати: 1д12г30х\n\n"
    "Приклади:\n"
    "/start_afk mc.example.com 1.20.1 266с\n"
    "/start_afk mc.example.com:25565 1.16.5 29г MyBot\n\n"
    "Якщо нікнейм не вказати — буде AFK_BOT.\n"
    "⚠️ Працює тільки на серверах з вимкненою ліцензійною перевіркою (offline-mode)."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def read_output(app, chat_id, proc):
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode(errors="ignore").strip()
        log.info("node: %s", text)
        if text == "SPAWN":
            await app.bot.send_message(chat_id, "✅ Бот зайшов на сервер і стоїть АФК.")
        elif text.startswith("KICKED"):
            await app.bot.send_message(chat_id, f"⚠️ Бота кікнули: {text[7:300]}")
        elif text.startswith("ERROR"):
            await app.bot.send_message(chat_id, f"❌ Помилка: {text[6:300]}")


async def run_session(app, chat_id, uid, host, port, version, nick, seconds):
    s = sessions[uid]
    timed_out = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", JS_FILE, host, str(port), version, nick,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        s["proc"] = proc
        reader = asyncio.create_task(read_output(app, chat_id, proc))
        try:
            await asyncio.wait_for(proc.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            timed_out = True
            proc.terminate()
            await proc.wait()
        await reader
        if timed_out:
            msg = "⏰ Час вийшов, бот вийшов із сервера."
        elif s["stopped"]:
            msg = "🛑 Бот вийшов із сервера."
        else:
            msg = "🔌 Бот відключився від сервера."
        await app.bot.send_message(chat_id, msg)
    except Exception as e:
        log.exception("session error")
        await app.bot.send_message(chat_id, f"❌ Не вдалося запустити бота: {e}")
    finally:
        sessions.pop(uid, None)


async def start_afk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = context.args

    if len(args) < 3:
        await update.message.reply_text(
            "Формат: /start_afk <айпі> <версія> <час> [нікнейм]\n"
            "Приклад: /start_afk mc.example.com 1.20.1 29г"
        )
        return
    if uid in sessions:
        await update.message.reply_text("У тебе вже є запущений бот. Спочатку напиши /stop_afk")
        return
    if len(sessions) >= MAX_BOTS:
        await update.message.reply_text("Зараз забагато активних ботів, спробуй пізніше.")
        return

    address, version, time_str = args[0], args[1], args[2]
    nick = args[3] if len(args) > 3 else DEFAULT_NICK

    # адреса (host або host:port)
    m = re.fullmatch(r"([A-Za-z0-9.\-]+)(?::(\d{1,5}))?", address)
    if not m:
        await update.message.reply_text("❌ Неправильний айпі/адреса сервера.")
        return
    host, port = m.group(1), int(m.group(2) or 25565)

    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", version):
        await update.message.reply_text("❌ Неправильна версія. Приклад: 1.20.1")
        return
    if not re.fullmatch(r"\w{3,16}", nick):
        await update.message.reply_text("❌ Нікнейм: 3–16 символів, лише літери/цифри/_ (латиницею).")
        return

    seconds = parse_time(time_str)
    if seconds is None or seconds <= 0:
        await update.message.reply_text("❌ Неправильний час. Приклади: 266с, 29г, 30х, 1д12г, 2р")
        return
    if seconds > MAX_SECONDS:
        await update.message.reply_text("❌ Максимальний час — 2 роки.")
        return

    await update.message.reply_text(
        f"⏳ Запускаю бота {nick} на {host}:{port} (версія {version}) на {fmt_duration(seconds)}..."
    )
    sessions[uid] = {"proc": None, "stopped": False}
    sessions[uid]["task"] = asyncio.create_task(
        run_session(context.application, update.effective_chat.id, uid, host, port, version, nick, seconds)
    )


async def stop_afk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = sessions.get(uid)
    if not s:
        await update.message.reply_text("У тебе немає запущеного бота.")
        return
    s["stopped"] = True
    if s["proc"] and s["proc"].returncode is None:
        s["proc"].terminate()
    else:
        s["task"].cancel()
        sessions.pop(uid, None)
        await update.message.reply_text("🛑 Зупинено.")


def main():
    prepare_node()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("start_afk", start_afk))
    app.add_handler(CommandHandler("stop_afk", stop_afk))
    log.info("Бот запущено")
    app.run_polling()


if __name__ == "__main__":
    main()
