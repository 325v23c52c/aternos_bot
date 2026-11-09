import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass

# ================== НАЛАШТУВАННЯ ==================
TOKEN = "8632361412:AAGs6-N9Ps-2YXd6WRzm4_fiSvgaLb8vtr0"
BOT_IP = os.getenv("BOT_IP", "")  # змінна BOT_IP з Railway Variables
DEFAULT_NICK = "AFK_BOT"
MAX_SECONDS = 2 * 365 * 24 * 3600  # максимум 2 роки
MAX_SUPPORTED = (1, 19, 2)  # quarry підтримує версії 1.7 - 1.19.2
# ==================================================

SELF_FILE = os.path.abspath(__file__)


# ----------------------------------------------------------------------
# ЧАСТИНА 1: Minecraft AFK клієнт (запускається як окремий процес)
# python bot.py --afk <host> <port> <nick>
# ----------------------------------------------------------------------
def run_afk_client(host: str, port: int, nick: str):
    from twisted.internet import reactor
    from quarry.net.client import ClientFactory, SpawningClientProtocol
    from quarry.net.auth import OfflineProfile

    def say(text):
        print(text, flush=True)

    class AFKProtocol(SpawningClientProtocol):
        def player_joined(self):
            try:
                super().player_joined()
            except Exception:
                pass
            say("AFK_SPAWN")

        def packet_disconnect(self, buff):
            try:
                reason = buff.unpack_chat().to_string()
            except Exception:
                reason = "відключено сервером"
            say("AFK_KICKED " + str(reason))

        def connection_lost(self, reason=None):
            say("AFK_END")
            if reactor.running:
                reactor.stop()

    class AFKFactory(ClientFactory):
        protocol = AFKProtocol

    def on_error(failure):
        say("AFK_ERROR " + failure.getErrorMessage())
        if reactor.running:
            reactor.stop()

    factory = AFKFactory(OfflineProfile(nick))
    d = factory.connect(host, port)
    d.addErrback(on_error)
    reactor.run()


if len(sys.argv) >= 5 and sys.argv[1] == "--afk":
    run_afk_client(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    sys.exit(0)


# ----------------------------------------------------------------------
# ЧАСТИНА 2: Telegram бот
# ----------------------------------------------------------------------
from telegram import Update  # noqa: E402
from telegram.ext import Application, CommandHandler, ContextTypes  # noqa: E402

# одиниці часу (укр/рос + англ.)
UNITS = {
    "с": 1, "s": 1, "сек": 1,
    "х": 60, "x": 60, "хв": 60, "min": 60,
    "г": 3600, "h": 3600, "год": 3600,
    "д": 86400, "d": 86400, "дн": 86400,
    "т": 7 * 86400, "w": 7 * 86400,
    "м": 30 * 86400, "мс": 30 * 86400, "mo": 30 * 86400,
    "р": 365 * 86400, "y": 365 * 86400, "рік": 365 * 86400,
}


def parse_time(text: str):
    m = re.fullmatch(r"(\d+)\s*([a-zA-Zа-яА-ЯіїєґІЇЄҐ]+)", text.strip().lower())
    if not m:
        return None
    amount, unit = int(m.group(1)), m.group(2)
    if unit not in UNITS:
        return None
    return amount * UNITS[unit]


def fmt_time(sec: int) -> str:
    parts = []
    for name, size in (("р", 365 * 86400), ("міс", 30 * 86400), ("д", 86400),
                       ("г", 3600), ("хв", 60), ("с", 1)):
        if sec >= size:
            parts.append(f"{sec // size}{name}")
            sec %= size
    return " ".join(parts) or "0с"


def version_too_new(version: str) -> bool:
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not m:
        return False
    v = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    return v > MAX_SUPPORTED


@dataclass
class Session:
    proc: asyncio.subprocess.Process
    end_time: float
    server: str
    nick: str
    timer: asyncio.Task
    reader: asyncio.Task
    stopped: bool = False


sessions: dict[int, Session] = {}


async def kill_session(user_id: int):
    s = sessions.pop(user_id, None)
    if not s:
        return False
    s.stopped = True
    s.timer.cancel()
    if s.proc.returncode is None:
        s.proc.terminate()
        try:
            await asyncio.wait_for(s.proc.wait(), 5)
        except asyncio.TimeoutError:
            s.proc.kill()
    s.reader.cancel()
    return True


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привіт! Я AFK-бот для Minecraft. Я заходжу на твій сервер і стою AFK скільки треба.\n\n"
        "📌 Команди:\n"
        "/start_afk <айпі сервера> <версія> <час> [нікнейм]\n"
        "   Приклад: /start_afk play.server.net 1.16.5 5г MyBot\n"
        "   Нікнейм не обов'язковий (за замовчуванням AFK_BOT).\n"
        "   Айпі можна з портом: play.server.net:25566\n\n"
        "/stop_afk — бот виходить з сервера\n"
        "/status — скільки ще залишилось\n\n"
        "⏱ Час (максимум 2 роки):\n"
        "с — секунди (266с)\n"
        "х — хвилини (30х)\n"
        "г — години (29г)\n"
        "д — дні (3д)\n"
        "т — тижні (2т)\n"
        "м — місяці (6м)\n"
        "р — роки (1р)\n\n"
        "⚠️ Підтримуються версії Minecraft 1.7 – 1.19.2.\n"
        "⚠️ Працює тільки на серверах з вимкненим online-mode (піратських)."
    )


async def cmd_start_afk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    args = ctx.args

    if len(args) < 3:
        await update.message.reply_text(
            "❌ Використання:\n/start_afk <айпі> <версія> <час> [нікнейм]\n"
            "Приклад: /start_afk play.server.net 1.16.5 5г"
        )
        return

    if user_id in sessions:
        await update.message.reply_text("⚠️ У тебе вже запущений AFK-бот. Спочатку /stop_afk")
        return

    server, version, time_str = args[0], args[1], args[2]
    nick = args[3] if len(args) > 3 else DEFAULT_NICK

    if version_too_new(version):
        await update.message.reply_text(
            "❌ Ця версія надто нова. Підтримуються версії до 1.19.2."
        )
        return

    seconds = parse_time(time_str)
    if seconds is None or seconds <= 0:
        await update.message.reply_text("❌ Неправильний час. Приклади: 266с, 30х, 29г, 3д, 6м, 1р")
        return
    if seconds > MAX_SECONDS:
        await update.message.reply_text("❌ Максимальний час — 2 роки.")
        return

    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", nick):
        await update.message.reply_text("❌ Нікнейм: 3-16 символів, тільки латиниця, цифри та _")
        return

    host, _, port = server.partition(":")
    port = port or "25565"
    if not port.isdigit():
        await update.message.reply_text("❌ Неправильний порт.")
        return

    proc = await asyncio.create_subprocess_exec(
        sys.executable, SELF_FILE, "--afk", host, port, nick,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def timer():
        await asyncio.sleep(seconds)
        if user_id in sessions:
            await kill_session(user_id)
            await ctx.bot.send_message(chat_id, f"⏰ Час вийшов, бот {nick} вийшов з {server}.")

    async def reader():
        try:
            async for raw in proc.stdout:
                line = raw.decode(errors="ignore").strip()
                if line == "AFK_SPAWN":
                    await ctx.bot.send_message(chat_id, f"✅ {nick} зайшов на {server} і стоїть AFK.")
                elif line.startswith("AFK_KICKED"):
                    await ctx.bot.send_message(chat_id, f"🚫 Бота кікнуло: {line[11:300]}")
                elif line.startswith("AFK_ERROR"):
                    await ctx.bot.send_message(chat_id, f"❌ Помилка: {line[10:300]}")
            await proc.wait()
            s = sessions.get(user_id)
            if s and not s.stopped:
                await kill_session(user_id)
                await ctx.bot.send_message(chat_id, f"🔌 Бот {nick} відключився від {server}.")
        except asyncio.CancelledError:
            pass

    sessions[user_id] = Session(
        proc=proc,
        end_time=time.time() + seconds,
        server=server,
        nick=nick,
        timer=asyncio.create_task(timer()),
        reader=asyncio.create_task(reader()),
    )
    await update.message.reply_text(
        f"⏳ Підключаюсь до {server} (версія {version}) як {nick}\n"
        f"AFK на {fmt_time(seconds)}..."
    )


async def cmd_stop_afk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await kill_session(update.effective_user.id):
        await update.message.reply_text("👋 Бот вийшов з сервера.")
    else:
        await update.message.reply_text("ℹ️ У тебе немає запущеного AFK-бота.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = sessions.get(update.effective_user.id)
    if not s:
        await update.message.reply_text("ℹ️ У тебе немає запущеного AFK-бота.")
        return
    left = max(0, int(s.end_time - time.time()))
    await update.message.reply_text(f"🟢 {s.nick} на {s.server}\nЗалишилось: {fmt_time(left)}")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("start_afk", cmd_start_afk))
    app.add_handler(CommandHandler("stop_afk", cmd_stop_afk))
    app.add_handler(CommandHandler("status", cmd_status))
    print("Бот запущено")
    app.run_polling()


if __name__ == "__main__":
    main()
