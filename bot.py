# -*- coding: utf-8 -*-
import os
import sys
import re
import traceback
import json
from datetime import datetime

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
    """Читает settings.json. Возвращает dict {chat_id_str: {"time": "06:30"}}."""
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
    """Записывает settings.json атомарно (через временный файл)."""
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
            "        pip install aiogram aiohttp beautifulsoup4 lxml apscheduler\n"
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
    YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
    YANDEX_API_URL = "https://api.weather.yandex.ru/v2/forecast"
    VOLGOGRAD_LAT = 48.708
    VOLGOGRAD_LON = 44.513
    CITY_NAME = "Волгограде"
    # =============================================

    if "ВАШ_ТОКЕН" in BOT_TOKEN or not BOT_TOKEN.strip():
        msg = "\n[!] BOT_TOKEN не заменён. Вставьте токен от @BotFather."
        print(msg)
        _log_raw(msg)
        return
    if "ВАШ_КЛЮЧ" in YANDEX_API_KEY or not YANDEX_API_KEY.strip():
        msg = "\n[!] YANDEX_API_KEY не заменён. Вставьте ключ Яндекс Погоды."
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
    scheduler = AsyncIOScheduler(timezone="Europe/Volgograd")

    WIND_DIR_RU = {
        "nw": "СЗ", "n": "С", "ne": "СВ", "e": "В",
        "se": "ЮВ", "s": "Ю", "sw": "ЮЗ", "w": "З", "c": "Штиль",
    }

    # --- ЯНДЕКС ПОГОДА: ПОЧАСОВОЙ ПРОГНОЗ ---
    async def get_yandex_hourly_forecast():
        headers = {"X-Yandex-API-Key": YANDEX_API_KEY}
        params = {
            "lat": VOLGOGRAD_LAT,
            "lon": VOLGOGRAD_LON,
            "lang": "ru_RU",
            "limit": 2,
            "hours": "true",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    YANDEX_API_URL, headers=headers, params=params, timeout=15
                ) as response:
                    text = await response.text()
                    log.info("Яндекс API (почасовой): HTTP %s", response.status)
                    if response.status != 200:
                        log.error("Яндекс API вернул ошибку: %s", text[:500])
                        return None
                    try:
                        data = json.loads(text)
                    except Exception as e:
                        log.error("Яндекс API: невалидный JSON: %s", e)
                        return None
                    forecasts = data.get("forecasts")
                    if not forecasts:
                        log.error("Яндекс API: пустой forecasts. Ответ: %s", text[:500])
                        return None
                    log.info("Яндекс API: получено %d дней", len(forecasts))
                    return forecasts
        except Exception as e:
            log.error("Ошибка запроса к Яндекс Погоде: %s", e)
            return None

    # --- ЯНДЕКС ПОГОДА: 10 ДНЕЙ ---
    async def get_10day_forecast():
        headers = {"X-Yandex-API-Key": YANDEX_API_KEY}
        params = {
            "lat": VOLGOGRAD_LAT,
            "lon": VOLGOGRAD_LON,
            "lang": "ru_RU",
            "limit": 10,
            "hours": "false",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    YANDEX_API_URL, headers=headers, params=params, timeout=15
                ) as response:
                    text = await response.text()
                    log.info("Яндекс API (10 дней): HTTP %s", response.status)
                    if response.status != 200:
                        log.error("Яндекс API (10 дней) вернул ошибку: %s", text[:500])
                        return None
                    try:
                        data = json.loads(text)
                    except Exception as e:
                        log.error("Яндекс API (10 дней): невалидный JSON: %s", e)
                        return None
                    forecasts = data.get("forecasts")
                    if not forecasts:
                        log.error("Яндекс API (10 дней): пустой forecasts.")
                        return None
                    log.info("Яндекс API (10 дней): получено %d дней", len(forecasts))
                    return forecasts
        except Exception as e:
            log.error("Ошибка запроса 10-дневного прогноза: %s", e)
            return None

    def get_weather_icon(condition: str) -> str:
        c = (condition or "").lower()
        if c == "clear":
            return "☀️"
        if c in ("partly-cloudy", "mostly-clear"):
            return "🌤️"
        if c in ("cloudy", "overcast"):
            return "☁️"
        if "rain" in c or c in ("drizzle", "showers", "light-rain", "heavy-rain"):
            return "🌧️"
        if "snow" in c:
            return "❄️"
        if "thunderstorm" in c:
            return "⛈️"
        return "🌫️"

    def format_hourly_forecast(days_data, day_index=0):
        if not days_data or len(days_data) <= day_index:
            return "⚠️ Не удалось получить прогноз."

        day = days_data[day_index]
        day_date = day.get("date", "")
        try:
            day_date = datetime.strptime(day_date, "%Y-%m-%d").strftime("%d.%m.%Y")
        except Exception:
            pass

        target_hours = [7, 9, 11, 13, 16, 19, 22]
        hours_map = {}
        for h in day.get("hours", []):
            try:
                hours_map[int(h.get("hour", -1))] = h
            except Exception:
                continue

        lines = [f"<b>Прогноз в {CITY_NAME} на {day_date}</b> 🌍", ""]
        for hour in target_hours:
            h = hours_map.get(hour)
            if not h:
                lines.append(f"<b>{hour:02d}:00</b> — нет данных")
                continue
            icon = get_weather_icon(h.get("condition", ""))
            temp = h.get("temp", "?")
            wind_speed = h.get("wind_speed", "?")
            wind_dir_code = (h.get("wind_dir") or "").lower()
            wind_dir = WIND_DIR_RU.get(wind_dir_code, wind_dir_code.upper())
            humidity = h.get("humidity", "?")
            temp_str = f"{temp:+d}" if isinstance(temp, int) else f"{temp}"
            lines.append(
                f"<b>{hour:02d}:00</b> {icon} {temp_str}° "
                f"💨{wind_speed}м/с {wind_dir} 💧{humidity}%"
            )
        lines.append("")
        lines.append("📡 Источник: Яндекс Погода")
        lines.append(f"🕐 Обновлено: {datetime.now().strftime('%H:%M:%S')}")
        return "\n".join(lines)

    def format_10day_forecast(days_data):
        if not days_data:
            return "⚠️ Не удалось получить прогноз на 10 дней."

        lines = [f"<b>Погода в {CITY_NAME} на 10 дней</b> 📆", ""]
        for day in days_data:
            date_raw = day.get("date", "")
            try:
                date_obj = datetime.strptime(date_raw, "%Y-%m-%d")
                date_str = date_obj.strftime("%d.%m")
            except Exception:
                date_str = date_raw

            parts = day.get("parts", {})
            day_part = parts.get("day", {}) or parts.get("day_short", {})
            condition = day_part.get("condition", "")
            icon = get_weather_icon(condition)

            t_min = day_part.get("temp_min", "?")
            t_max = day_part.get("temp_max", "?")
            if isinstance(t_min, int) and isinstance(t_max, int):
                temp_str = f"{t_min:+d}…{t_max:+d}°"
            else:
                temp_str = f"{t_min}…{t_max}°"

            wind_speed = day_part.get("wind_speed", "?")
            wind_dir_code = (day_part.get("wind_dir") or "").lower()
            wind_dir = WIND_DIR_RU.get(wind_dir_code, wind_dir_code.upper())
            humidity = day_part.get("humidity", "?")

            lines.append(
                f"<b>{date_str}</b> {icon} {temp_str} "
                f"💨{wind_speed}{wind_dir} 💧{humidity}%"
            )

        lines.append("")
        lines.append("📡 Источник: Яндекс Погода")
        lines.append(f"🕐 Обновлено: {datetime.now().strftime('%H:%M:%S')}")
        return "\n".join(lines)

    async def send_daily_forecast(chat_id: int):
        log.info(f"Отправка ежедневного прогноза для chat_id={chat_id}")
        days_data = await get_yandex_hourly_forecast()
        if not days_data:
            try:
                await bot.send_message(chat_id, "⚠️ Не удалось получить прогноз погоды.")
            except Exception as e:
                log.error("Не удалось отправить сообщение %s: %s", chat_id, e)
            return
        report = format_hourly_forecast(days_data, day_index=0)
        try:
            await bot.send_message(chat_id, report, parse_mode="HTML")
        except Exception as e:
            log.error("Не удалось отправить сообщение %s: %s", chat_id, e)

    # --- ВОССТАНОВЛЕНИЕ РАССЫЛОК ИЗ settings.json ---
    def restore_schedules():
        """Читает settings.json и заново добавляет задачи в scheduler."""
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

    @dp.message(F.text == "🌡️ Погода сейчас")
    @dp.message(Command("weather"))
    async def btn_weather_now(message: types.Message):
        placeholder = await message.answer("⏳ Получаю почасовой прогноз…")
        days_data = await get_yandex_hourly_forecast()
        report = format_hourly_forecast(days_data, day_index=0)
        await placeholder.edit_text(report, parse_mode="HTML", reply_markup=refresh_kb())

    @dp.callback_query(F.data == "refresh_weather")
    async def cb_refresh(call):
        days_data = await get_yandex_hourly_forecast()
        report = format_hourly_forecast(days_data, day_index=0)
        try:
            await call.message.edit_text(report, parse_mode="HTML", reply_markup=refresh_kb())
        except TelegramBadRequest:
            pass
        await call.answer("Обновлено ✅")

    @dp.message(F.text == "📅 Прогноз на завтра")
    async def btn_tomorrow(message: types.Message):
        await message.answer("⏳ Получаю прогноз на завтра…")
        days_data = await get_yandex_hourly_forecast()
        report = format_hourly_forecast(days_data, day_index=1)
        await message.answer(report, parse_mode="HTML")

    @dp.message(F.text == "📆 Погода на 10 дней")
    async def btn_10days(message: types.Message):
        await message.answer("⏳ Получаю прогноз на 10 дней…")
        days_data = await get_10day_forecast()
        report = format_10day_forecast(days_data)
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

        # --- Сохраняем в settings.json ---
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

            # --- Удаляем из settings.json ---
            data = load_settings()
            if str(chat_id) in data:
                del data[str(chat_id)]
                save_settings(data)

            await message.answer("🔕 Рассылка отключена.", reply_markup=main_kb)
        else:
            await message.answer("Рассылка не была настроена.", reply_markup=main_kb)

    @dp.message(F.text == "ℹ️ Помощь")
    async def btn_help(message: types.Message):
        # Показываем текущую настройку, если есть
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
            "📆 <b>Погода на 10 дней</b> — дневной прогноз (мин/макс, ветер, влажность)\n"
            "⚙️ <b>Настроить рассылку</b> — ежедневная отправка в указанное время\n"
            "🔕 <code>/stop_broadcast</code> — отключить рассылку\n\n"
            f"{broadcast_info}\n\n"
            "<b>Обозначения:</b>\n"
            "💨 — ветер (м/с), 💧 — влажность (%)\n\n"
            "Источник: Яндекс Погода",
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
        # Восстанавливаем сохранённые рассылки ДО старта планировщика
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
