# -*- coding: utf-8 -*-
import os
import sys
import re
import traceback
import json
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "bot.log")
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")
try:
    os.chdir(SCRIPT_DIR)
except Exception:
    pass


def _log_raw(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass


def _pause():
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nНажмите Enter, чтобы закрыть окно... ")
    except Exception:
        pass


def _relaunch_if_pythonw():
    try:
        exe = os.path.basename(sys.executable).lower()
    except Exception:
        return False
    if "pythonw" not in exe:
        return False
    import shutil, subprocess
    candidates = [
        os.path.join(os.path.dirname(sys.executable), "python.exe"),
        shutil.which("python"),
        shutil.which("py"),
    ]
    python_exe = next((c for c in candidates if c and os.path.exists(c)), None)
    if not python_exe:
        _log_raw("[FATAL] python.exe не найден.")
        return False
    try:
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        subprocess.Popen([python_exe, os.path.abspath(__file__)], creationflags=flags)
        return True
    except Exception as e:
        _log_raw("[FATAL] Ошибка перезапуска: %s" % e)
        return False


if _relaunch_if_pythonw():
    sys.exit(0)


# ---------- СОХРАНЕНИЕ НАСТРОЕК РАССЫЛКИ ----------
def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            _log_raw("[WARN] settings.json имеет неверный формат, игнорируем.")
            return {}
        return data
    except Exception as e:
        _log_raw("[WARN] Не удалось прочитать settings.json: %s" % e)
        return {}


def save_settings(data):
    try:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        _log_raw("[ERROR] Не удалось сохранить settings.json: %s" % e)


def run_bot():
    import asyncio
    import logging

    try:
        import aiohttp
        import pytz
        from aiogram import Bot, Dispatcher, types, F
        from aiogram.filters import Command
        from aiogram.types import (
            ReplyKeyboardMarkup, KeyboardButton,
            InlineKeyboardMarkup, InlineKeyboardButton,
        )
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.state import State, StatesGroup
        from aiogram.exceptions import TelegramBadRequest
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ModuleNotFoundError as e:
        msg = (
            "\n[!] Не установлена библиотека: %s\n"
            "    Выполните в терминале:\n"
            "        pip install aiogram aiohttp beautifulsoup4 lxml apscheduler pytz\n"
            % e.name
        )
        print(msg)
        _log_raw(msg)
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger("weather_bot")
    log.info("=" * 50)
    log.info("Новый запуск бота")

    # ================= НАСТРОЙКИ =================
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
    VOLGOGRAD_LAT = 48.708
    VOLGOGRAD_LON = 44.513
    CITY_NAME = "Волгограде"

    # Telegram ID админа (узнать через @userinfobot)
    ADMIN_ID = 477663194  # ← ЗАМЕНИТЕ на свой ID
    # =============================================

    if not BOT_TOKEN or "8991141233:AAHKKl4d20kfwZ1QF60TXzrCQRDLXSNkAwA" in BOT_TOKEN:
        msg = "\n[!] BOT_TOKEN не задан. Установите переменную окружения BOT_TOKEN."
        print(msg)
        _log_raw(msg)
        return

    # --- КЛАВИАТУРЫ ---
    main_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌡️ Погода сейчас")],
            [KeyboardButton(text="📅 Прогноз на завтра")],
            [KeyboardButton(text="📆 Погода на 10 дней")],
            [KeyboardButton(text="⚙️ Настроить рассылку")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )

    def refresh_kb():
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_weather")
        ]])

    class BroadcastState(StatesGroup):
        waiting_for_time = State()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    scheduler = AsyncIOScheduler(timezone=pytz.timezone("Europe/Moscow"))

    # --- WMO-коды Open-Meteo → эмодзи ---
    def get_weather_icon(code) -> str:
        try:
            c = int(code)
        except (TypeError, ValueError):
            return "🌫️"
        if c == 0:
            return "☀️"
        if c in (1, 2):
            return "🌤️"
        if c == 3:
            return "☁️"
        if c in (45, 48):
            return "🌫️"
        if c in (51, 53, 55, 56, 57):
            return "🌧️"
        if c in (61, 63, 65, 66, 67):
            return "🌧️"
        if c in (71, 73, 75, 77):
            return "❄️"
        if c in (80, 81, 82):
            return "🌧️"
        if c in (85, 86):
            return "❄️"
        if c in (95, 96, 99):
            return "⛈️"
        return "🌫️"

    # --- Направление ветра (градусы) → румб ---
    def wind_dir_ru(deg) -> str:
        try:
            d = float(deg)
        except (TypeError, ValueError):
            return ""
        dirs = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
        return dirs[int((d + 22.5) // 45) % 8]

    # --- OPEN-METEO: ПОЧАСОВОЙ ПРОГНОЗ НА 2 ДНЯ ---
    async def get_openmeteo_forecast():
        params = {
            "latitude": VOLGOGRAD_LAT,
            "longitude": VOLGOGRAD_LON,
            "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,weather_code",
            "wind_speed_unit": "ms",
            "timezone": "Europe/Moscow",
            "forecast_days": 2,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(OPEN_METEO_URL, params=params, timeout=15) as response:
                    text = await response.text()
                    log.info("Open-Meteo (почасовой): HTTP %s", response.status)
                    if response.status != 200:
                        log.error("Open-Meteo вернул ошибку: %s", text[:500])
                        return None
                    return json.loads(text)
        except Exception as e:
            log.error("Ошибка запроса к Open-Meteo: %s", e)
            return None

    # --- OPEN-METEO: ДНЕВНОЙ ПРОГНОЗ НА 10 ДНЕЙ ---
    async def get_10day_forecast():
        params = {
            "latitude": VOLGOGRAD_LAT,
            "longitude": VOLGOGRAD_LON,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,wind_speed_10m_max,wind_direction_10m_dominant",
            "wind_speed_unit": "ms",
            "timezone": "Europe/Moscow",
            "forecast_days": 10,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(OPEN_METEO_URL, params=params, timeout=15) as response:
                    text = await response.text()
                    log.info("Open-Meteo (10 дней): HTTP %s", response.status)
                    if response.status != 200:
                        log.error("Open-Meteo (10 дней) вернул ошибку: %s", text[:500])
                        return None
                    return json.loads(text)
        except Exception as e:
            log.error("Ошибка запроса 10-дневного прогноза: %s", e)
            return None

    # --- ФОРМАТТЕР: ПОЧАСОВОЙ ---
    def format_hourly_forecast(data, day_index=0):
        if not data or "hourly" not in data:
            return "⚠️ Не удалось получить прогноз."

        hourly = data["hourly"]
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        hums = hourly.get("relative_humidity_2m", [])
        winds = hourly.get("wind_speed_10m", [])
        wind_dirs = hourly.get("wind_direction_10m", [])
        codes = hourly.get("weather_code", [])

        target_date = datetime.now().date() + timedelta(days=day_index)
        target_str = target_date.strftime("%Y-%m-%d")

        target_hours = [7, 9, 11, 13, 16, 19, 22]
        lines = [f"<b>Прогноз в {CITY_NAME} на {target_date.strftime('%d.%m.%Y')}</b> 🌍", ""]

        for hour in target_hours:
            idx = None
            for i, t in enumerate(times):
                if t.startswith(target_str) and t.endswith(f"T{hour:02d}:00"):
                    idx = i
                    break
            if idx is None or idx >= len(temps):
                lines.append(f"<b>{hour:02d}:00</b> — нет данных")
                continue

            icon = get_weather_icon(codes[idx] if idx < len(codes) else None)
            temp = temps[idx]
            hum = hums[idx] if idx < len(hums) else "?"
            wind = winds[idx] if idx < len(winds) else "?"
            wdir = wind_dirs[idx] if idx < len(wind_dirs) else 0

            temp_str = f"{temp:+.0f}" if isinstance(temp, (int, float)) else str(temp)
            wind_str = f"{wind:.0f}" if isinstance(wind, (int, float)) else str(wind)
            hum_str = f"{hum:.0f}" if isinstance(hum, (int, float)) else str(hum)

            lines.append(
                f"<b>{hour:02d}:00</b> {icon} {temp_str}° "
                f"💨{wind_str}м/с {wind_dir_ru(wdir)} 💧{hum_str}%"
            )

        lines.append("")
        lines.append("📡 Источник: Open-Meteo")
        lines.append(f"🕐 Обновлено: {datetime.now().strftime('%H:%M:%S')}")
        return "\n".join(lines)

    # --- ФОРМАТТЕР: 10 ДНЕЙ ---
    def format_10day_forecast(data):
        if not data or "daily" not in data:
            return "⚠️ Не удалось получить прогноз на 10 дней."

        daily = data["daily"]
        dates = daily.get("time", [])
        codes = daily.get("weather_code", [])
        t_maxs = daily.get("temperature_2m_max", [])
        t_mins = daily.get("temperature_2m_min", [])
        winds = daily.get("wind_speed_10m_max", [])
        wind_dirs = daily.get("wind_direction_10m_dominant", [])

        lines = [f"<b>Погода в {CITY_NAME} на 10 дней</b> 📆", ""]

        for i, d in enumerate(dates):
            try:
                date_obj = datetime.strptime(d, "%Y-%m-%d")
                date_str = date_obj.strftime("%d.%m")
            except Exception:
                date_str = d

            icon = get_weather_icon(codes[i] if i < len(codes) else None)
            tmin = t_mins[i] if i < len(t_mins) else "?"
            tmax = t_maxs[i] if i < len(t_maxs) else "?"
            wind = winds[i] if i < len(winds) else 0
            wdir = wind_dirs[i] if i < len(wind_dirs) else 0

            if isinstance(tmin, (int, float)) and isinstance(tmax, (int, float)):
                temp_str = f"{tmin:+.0f}…{tmax:+.0f}°"
            else:
                temp_str = f"{tmin}…{tmax}°"

            wind_str = f"{wind:.0f}" if isinstance(wind, (int, float)) else str(wind)

            lines.append(
                f"<b>{date_str}</b> {icon} {temp_str} "
                f"💨{wind_str}{wind_dir_ru(wdir)}"
            )

        lines.append("")
        lines.append("📡 Источник: Open-Meteo")
        lines.append(f"🕐 Обновлено: {datetime.now().strftime('%H:%M:%S')}")
        return "\n".join(lines)

    async def send_daily_forecast(chat_id: int):
        log.info(f"Отправка ежедневного прогноза для chat_id={chat_id}")
        data = await get_openmeteo_forecast()
        if not data:
            try:
                await bot.send_message(chat_id, "⚠️ Не удалось получить прогноз погоды.")
            except Exception as e:
                log.error("Не удалось отправить сообщение %s: %s", chat_id, e)
            return
        report = format_hourly_forecast(data, day_index=0)
        try:
            await bot.send_message(chat_id, report, parse_mode="HTML")
        except Exception as e:
            log.error("Не удалось отправить сообщение %s: %s", chat_id, e)

    # --- ВОССТАНОВЛЕНИЕ РАССЫЛОК ---
    def restore_schedules():
        data = load_settings()
        if not data:
            log.info("settings.json пуст или отсутствует — рассылок для восстановления нет.")
            return
        count = 0
        for chat_id_str, item in data.items():
            try:
                chat_id = int(chat_id_str)
                time_str = item.get("time", "")
                hour, minute = map(int, time_str.split(":"))
                job_id = f"daily_forecast_{chat_id}"
                scheduler.add_job(
                    send_daily_forecast,
                    trigger=CronTrigger(hour=hour, minute=minute),
                    args=[chat_id],
                    id=job_id,
                    replace_existing=True,
                )
                count += 1
                log.info("Восстановлена рассылка для chat_id=%s на %s", chat_id, time_str)
            except Exception as e:
                log.error("Не удалось восстановить рассылку для %s: %s", chat_id_str, e)
        log.info("Восстановлено рассылок: %d", count)

    # --- ХЕНДЛЕРЫ ---
    @dp.message(Command("start"))
    async def cmd_start(message: types.Message):
        await message.answer(
            "Привет! Я показываю погоду в Волгограде.\n"
            "Пользуйся кнопками внизу 👇",
            reply_markup=main_kb,
        )

    @dp.message(Command("admin"))
    async def cmd_admin(message: types.Message):
        if message.from_user.id != ADMIN_ID:
            await message.answer("⛔ У вас нет доступа к этой команде.")
            return

        data = load_settings()
        if not data:
            await message.answer("📭 Пока никто не настроил рассылку.")
            return

        lines = ["<b>📊 Настройки рассылки</b>", ""]
        count = 0
        for chat_id_str, item in data.items():
            time_str = item.get("time", "не указано")
            lines.append(f"🆔 <code>{chat_id_str}</code> — ⏰ {time_str}")
            count += 1

        lines.append("")
        lines.append(f"👥 Всего пользователей: <b>{count}</b>")

        await message.answer("\n".join(lines), parse_mode="HTML")

    @dp.message(F.text == "🌡️ Погода сейчас")
    @dp.message(Command("weather"))
    async def btn_weather_now(message: types.Message):
        placeholder = await message.answer("⏳ Получаю почасовой прогноз…")
        data = await get_openmeteo_forecast()
        report = format_hourly_forecast(data, day_index=0)
        await placeholder.edit_text(report, parse_mode="HTML", reply_markup=refresh_kb())

    @dp.callback_query(F.data == "refresh_weather")
    async def cb_refresh(call):
        data = await get_openmeteo_forecast()
        report = format_hourly_forecast(data, day_index=0)
        try:
            await call.message.edit_text(report, parse_mode="HTML", reply_markup=refresh_kb())
        except TelegramBadRequest:
            pass
        await call.answer("Обновлено ✅")

    @dp.message(F.text == "📅 Прогноз на завтра")
    async def btn_tomorrow(message: types.Message):
        await message.answer("⏳ Получаю прогноз на завтра…")
        data = await get_openmeteo_forecast()
        report = format_hourly_forecast(data, day_index=1)
        await message.answer(report, parse_mode="HTML")

    @dp.message(F.text == "📆 Погода на 10 дней")
    async def btn_10days(message: types.Message):
        await message.answer("⏳ Получаю прогноз на 10 дней…")
        data = await get_10day_forecast()
        report = format_10day_forecast(data)
        await message.answer(report, parse_mode="HTML")

    @dp.message(F.text == "⚙️ Настроить рассылку")
    async def btn_setup_broadcast(message: types.Message, state: FSMContext):
        await message.answer(
            "Введите время для ежедневного прогноза в формате <b>ЧЧ:ММ</b> "
            "(например, <code>08:30</code>).\n\n"
            "Отключить: /stop_broadcast\n\n"
            "<i>Настройка сохраняется и переживает перезапуск бота.</i>",
            parse_mode="HTML",
        )
        await state.set_state(BroadcastState.waiting_for_time)

    @dp.message(BroadcastState.waiting_for_time)
    async def process_time_input(message: types.Message, state: FSMContext):
        time_str = message.text.strip()
        if not re.match(r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$", time_str):
            await message.answer(
                "❌ Неверный формат. Введите как <b>ЧЧ:ММ</b>, например <code>09:00</code>.",
                parse_mode="HTML",
            )
            return
        hour, minute = map(int, time_str.split(":"))
        chat_id = message.chat.id
        job_id = f"daily_forecast_{chat_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        scheduler.add_job(
            send_daily_forecast,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[chat_id],
            id=job_id,
            replace_existing=True,
        )

        data = load_settings()
        data[str(chat_id)] = {"time": time_str, "hour": hour, "minute": minute}
        save_settings(data)

        await state.clear()
        await message.answer(
            f"✅ Готово! Прогноз будет приходить ежедневно в <b>{time_str}</b>.\n"
            f"<i>Настройка сохранена — переживёт перезапуск бота.</i>",
            parse_mode="HTML",
            reply_markup=main_kb,
        )

    @dp.message(Command("stop_broadcast"))
    async def cmd_stop_broadcast(message: types.Message):
        chat_id = message.chat.id
        job_id = f"daily_forecast_{chat_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            data = load_settings()
            if str(chat_id) in data:
                del data[str(chat_id)]
                save_settings(data)
            await message.answer("🔕 Рассылка отключена.", reply_markup=main_kb)
        else:
            await message.answer("Рассылка не была настроена.", reply_markup=main_kb)

    @dp.message(F.text == "ℹ️ Помощь")
    async def btn_help(message: types.Message):
        data = load_settings()
        current = data.get(str(message.chat.id), {})
        current_time = current.get("time")
        broadcast_info = (
            f"⏰ Текущая рассылка: <b>{current_time}</b>"
            if current_time else
            "⏰ Рассылка не настроена"
        )

        await message.answer(
            "<b>Что я умею</b>\n\n"
            "🌡️ <b>Погода сейчас</b> — почасовой прогноз на сегодня\n"
            "📅 <b>Прогноз на завтра</b> — почасовой прогноз на завтра\n"
            "📆 <b>Погода на 10 дней</b> — дневной прогноз\n"
            "⚙️ <b>Настроить рассылку</b> — ежедневная отправка в указанное время\n"
            "🔕 <code>/stop_broadcast</code> — отключить рассылку\n\n"
            f"{broadcast_info}\n\n"
            "<b>Обозначения:</b>\n"
            "💨 — ветер (м/с), 💧 — влажность (%)\n\n"
            "Источник: Open-Meteo",
            parse_mode="HTML",
        )

    @dp.message()
    async def fallback(message: types.Message):
        await message.answer(
            "Пользуйтесь кнопками внизу 👇\n"
            "Или командами: /start, /weather, /stop_broadcast",
            reply_markup=main_kb,
        )

    async def runner():
        restore_schedules()
        scheduler.start()
        log.info("Планировщик запущен.")
        me = await bot.get_me()
        log.info("✅ Бот @%s подключён. Ожидаю сообщения.", me.username)
        await dp.start_polling(bot)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        log.info("Остановлен пользователем (Ctrl+C).")


try:
    run_bot()
except SystemExit:
    raise
except BaseException as e:
    tb = traceback.format_exc()
    msg = "[FATAL] %s: %s\n%s" % (type(e).__name__, e, tb)
    _log_raw(msg)
    try:
        print("\n❌ Ошибка:\n" + msg)
    except Exception:
        pass
finally:
    _pause()
