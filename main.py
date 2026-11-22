"""
HiggyGRAM — Telegram-бот: выдаёт номера для входа на сайт, присылает коды (их отправляет сайт),
принимает оплату звёздами Telegram (1 ⭐ = 1000 H-STARS) и даёт администратору панель выдачи премиум-номеров.
Работает через HTTP-API сайта (bot_api.php) — доступа к базе данных у бота нет.
Запуск: python main.py
"""
import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

BASE = Path(__file__).resolve().parent


def load_env() -> None:
    f = BASE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
API_KEY = os.environ.get("BOT_API_KEY", "")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}
if not BOT_TOKEN or not SITE_URL or not API_KEY:
    raise SystemExit("Задайте BOT_TOKEN, SITE_URL и BOT_API_KEY (см. .env.example)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hgbot")

PENDING = BASE / "pending_topups.json"   # оплаченные звёзды, которые не удалось зачислить (сайт был недоступен)
http: aiohttp.ClientSession
bot: Bot
router = Router()
adm = Router()
adm.message.filter(F.from_user.id.in_(ADMIN_IDS))
adm.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
esc = html.escape
KINDS = {"us": "+1", "ru": "+7", "kz": "+7 7 (Казахстан)", "custom": "особый"}


def fmt_n(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


# ---------------------------------------------------------------- API сайта
async def site(action: str, **data) -> dict:
    url = f"{SITE_URL}/bot_api.php?action={action}"
    form = {k: str(v) for k, v in data.items() if v is not None}
    try:
        async with http.post(url, data=form, headers={"X-Bot-Key": API_KEY}) as r:
            try:
                j = await r.json(content_type=None)
            except Exception:
                j = {"ok": False, "error": f"Ответ сайта не распознан (HTTP {r.status})"}
            j["_status"] = r.status
            return j
    except Exception as e:  # сеть / таймаут
        log.warning("site %s: %s", action, e)
        return {"ok": False, "error": "Сайт временно недоступен, попробуйте позже.", "_status": 0}


def err(j: dict) -> str:
    return "⚠️ " + esc(str(j.get("error", "Ошибка")))


# ---------------------------------------------------------------- клавиатуры и тексты
def main_kb(uid: int, has_number: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📱 Мой номер" if has_number else "📱 Получить номер", callback_data="num")
    b.button(text="⭐ Пополнить H-STARS", callback_data="top")
    b.button(text="🌐 Открыть сайт", url=SITE_URL)
    if uid in ADMIN_IDS:
        b.button(text="👑 Админ-панель", callback_data="adm")
    b.adjust(1)
    return b.as_markup()


def kinds_kb(prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="+1", callback_data=f"{prefix}:us")
    b.button(text="+7", callback_data=f"{prefix}:ru")
    b.button(text="+7 7 (Казахстан)", callback_data=f"{prefix}:kz")
    b.adjust(1)
    return b.as_markup()


def number_text(info: dict) -> str:
    n, u = info["number"], info.get("user")
    t = [f"📱 <b>Ваш номер:</b> <code>{esc(n['formatted'])}</code>"]
    if n.get("premium"):
        t.append("👑 Премиум-номер")
    t.append(f"Тип: {KINDS.get(n['kind'], n['kind'])}")
    if u:
        t.append(f"\nАккаунт: <b>{esc(u['name'])}</b> (@{esc(u['username'])})\n💰 Баланс: {fmt_n(u['stars'])} H-STARS")
    else:
        t.append("\nАккаунта на сайте ещё нет. Откройте сайт, введите этот номер — код для входа придёт сюда, в этот чат.")
    t.append("\nОдин аккаунт — один номер. Сменить номер можно на сайте: Настройки → Номер и безопасность.")
    return "\n".join(t)


async def show_menu(m: Message | CallbackQuery, text: str | None = None) -> None:
    u = m.from_user
    info = await site("info", tg_id=u.id, tg_username=u.username or "")
    has = bool(info.get("ok") and info.get("number"))
    text = text or ("👋 <b>HiggyGRAM</b>\nЗдесь вы получаете номер для входа на сайт, коды подтверждения и пополняете H-STARS звёздами Telegram.")
    msg = m.message if isinstance(m, CallbackQuery) else m
    await msg.answer(text, reply_markup=main_kb(u.id, has))


# ---------------------------------------------------------------- пользователь
@router.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    p = (command.args or "").strip()
    if p == "number":
        return await give_number(m, m.from_user)
    if p == "topup":
        return await topup_menu(m, m.from_user)
    await show_menu(m)


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext) -> None:
    await state.clear()
    await show_menu(m, "Отменено.")


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await c.answer()
    await show_menu(c)


async def give_number(m: Message, u) -> None:
    info = await site("info", tg_id=u.id, tg_username=u.username or "")
    if not info.get("ok"):
        return await m.answer(err(info))
    if info.get("number"):
        return await m.answer(number_text(info), reply_markup=main_kb(u.id, True))
    await m.answer("Выберите тип номера. Номер выдаётся один раз и привязывается к вашему Telegram:\n\n<b>+1</b> — США/Канада\n<b>+7</b> — Россия\n<b>+7 7</b> — Казахстан",
                   reply_markup=kinds_kb("pick"))


@router.callback_query(F.data == "num")
async def cb_num(c: CallbackQuery) -> None:
    await c.answer()
    await give_number(c.message, c.from_user)


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(c: CallbackQuery) -> None:
    await c.answer()
    kind = c.data.split(":")[1]
    j = await site("issue", tg_id=c.from_user.id, tg_username=c.from_user.username or "", kind=kind)
    if not j.get("ok"):
        return await c.message.answer(err(j))
    prefix = "Ваш номер уже выдан.\n\n" if j.get("existing") else "✅ Номер выдан!\n\n"
    await c.message.answer(prefix + number_text(j), reply_markup=main_kb(c.from_user.id, True))


# ---------------------------------------------------------------- пополнение звёздами
class Top(StatesGroup):
    amount = State()


async def topup_menu(m: Message, u) -> None:
    info = await site("info", tg_id=u.id, tg_username=u.username or "")
    if not info.get("ok"):
        return await m.answer(err(info))
    if not info.get("number"):
        return await m.answer("Сначала получите номер и создайте аккаунт на сайте.", reply_markup=main_kb(u.id, False))
    if not info.get("user"):
        return await m.answer(f"Сначала создайте аккаунт на сайте: откройте {SITE_URL} и войдите по номеру <code>{esc(info['number']['formatted'])}</code> — код придёт сюда.",
                              reply_markup=main_kb(u.id, True))
    per = int(info.get("per_star", 1000))
    b = InlineKeyboardBuilder()
    for s in (1, 5, 10, 25, 50, 100):
        b.button(text=f"{s} ⭐ → {fmt_n(s * per)}", callback_data=f"pay:{s}")
    b.button(text="✍️ Другая сумма", callback_data="payc")
    b.button(text="⬅️ Назад", callback_data="menu")
    b.adjust(2, 2, 2, 1, 1)
    await m.answer(f"💰 Баланс: <b>{fmt_n(info['user']['stars'])}</b> H-STARS\n\n<b>1 ⭐ = {fmt_n(per)} H-STARS</b>. Выберите сумму — откроется оплата звёздами Telegram.",
                   reply_markup=b.as_markup())


@router.callback_query(F.data == "top")
async def cb_top(c: CallbackQuery) -> None:
    await c.answer()
    await topup_menu(c.message, c.from_user)


@router.callback_query(F.data == "payc")
async def cb_payc(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.set_state(Top.amount)
    await c.message.answer("Сколько звёзд ⭐ потратить? Отправьте число от 1 до 10000 (или /cancel).")


@router.message(Top.amount)
async def got_amount(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    if not t.isdigit() or not 1 <= int(t) <= 10000:
        return await m.answer("Введите целое число от 1 до 10000 или /cancel.")
    await state.clear()
    await send_invoice(m.from_user.id, int(t))


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(c: CallbackQuery) -> None:
    await c.answer()
    await send_invoice(c.from_user.id, int(c.data.split(":")[1]))


async def send_invoice(uid: int, stars: int) -> None:
    per = 1000
    info = await site("info", tg_id=uid)
    if info.get("ok"):
        per = int(info.get("per_star", 1000))
        if not info.get("user"):
            return await bot.send_message(uid, "Сначала создайте аккаунт на сайте по своему номеру.")
    await bot.send_invoice(
        chat_id=uid, title=f"{fmt_n(stars * per)} H-STARS",
        description=f"Пополнение баланса HiggyGRAM: {stars} ⭐ → {fmt_n(stars * per)} H-STARS",
        payload=f"topup:{uid}:{stars}", provider_token="", currency="XTR",
        prices=[LabeledPrice(label="H-STARS", amount=stars)])


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    p = (q.invoice_payload or "").split(":")
    if len(p) != 3 or p[0] != "topup" or p[1] != str(q.from_user.id) or q.currency != "XTR":
        return await q.answer(ok=False, error_message="Некорректный платёж.")
    info = await site("info", tg_id=q.from_user.id)
    if not info.get("ok"):
        return await q.answer(ok=False, error_message="Сайт временно недоступен, попробуйте позже.")
    if not info.get("user"):
        return await q.answer(ok=False, error_message="Сначала создайте аккаунт на сайте по своему номеру.")
    await q.answer(ok=True)


def load_pending() -> list:
    try:
        return json.loads(PENDING.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_pending(items: list) -> None:
    PENDING.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


async def notify_admins(text: str) -> None:
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text)
        except Exception:
            pass


async def try_credit(item: dict) -> dict:
    return await site("topup", tg_id=item["tg_id"], stars=item["stars"], charge_id=item["charge_id"])


@router.message(F.successful_payment)
async def paid(m: Message) -> None:
    sp = m.successful_payment
    item = {"tg_id": m.from_user.id, "stars": sp.total_amount, "charge_id": sp.telegram_payment_charge_id}
    log.info("payment %s", item)
    j = {}
    for i in range(3):
        j = await try_credit(item)
        if j.get("ok"):
            break
        if j.get("_status") not in (0, 500, 502, 503, 504):
            break
        await asyncio.sleep(2 * (i + 1))
    if j.get("ok"):
        bal = f"\n💰 Баланс: {fmt_n(j['balance'])} H-STARS" if j.get("balance") is not None else ""
        return await m.answer(f"✅ Оплата получена! Зачислено <b>{fmt_n(j['hstars'])}</b> H-STARS.{bal}")
    items = load_pending()
    items.append(item)
    save_pending(items)
    await m.answer("✅ Оплата получена. Зачисление задерживается — H-STARS придут автоматически в течение нескольких минут.")
    await notify_admins(f"⚠️ Платёж {item['stars']} ⭐ от {item['tg_id']} не зачислен сразу ({esc(str(j.get('error')))}). Сохранён, бот повторит попытку.\nID платежа: <code>{esc(item['charge_id'])}</code>")


async def retry_loop() -> None:
    while True:
        await asyncio.sleep(60)
        items = load_pending()
        if not items:
            continue
        left = []
        for it in items:
            j = await try_credit(it)
            if j.get("ok"):
                try:
                    await bot.send_message(it["tg_id"], f"✅ Зачислено {fmt_n(j['hstars'])} H-STARS за ваш платёж.")
                except Exception:
                    pass
            elif j.get("_status") in (0, 500, 502, 503, 504):
                left.append(it)
            else:
                await notify_admins(f"❌ Платёж не зачислен: {it['stars']} ⭐ от {it['tg_id']} — {esc(str(j.get('error')))}\nID: <code>{esc(it['charge_id'])}</code>")
        save_pending(left)


# ---------------------------------------------------------------- админ-панель
class A(StatesGroup):
    target = State()
    mode = State()
    phone = State()
    country = State()
    link_tg = State()
    find = State()
    reset_target = State()


def adm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎁 Выдать премиум-номер", callback_data="a:grant")
    b.button(text="🔎 Найти номер / пользователя", callback_data="a:find")
    b.button(text="♻️ Снять премиум-номер", callback_data="a:reset")
    b.button(text="📊 Статистика", callback_data="a:stats")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data="adm")]])


def who_text(j: dict) -> str:
    n, u = j.get("number"), j.get("user")
    t = []
    if n:
        t.append(f"📱 <code>{esc(n['formatted'])}</code>{' 👑' if n['premium'] else ''} · {KINDS.get(n['kind'], n['kind'])}")
        t.append(f"Telegram ID: <code>{n['tg_id']}</code>" + (f" (@{esc(n['tg_username'])})" if n.get("tg_username") else "") if n.get("tg_id") else "Telegram: не привязан")
    else:
        t.append("Номера нет")
    if u:
        t.append(f"Аккаунт: <b>{esc(u['name'])}</b> @{esc(u['username'])} · ID {u['id']} · {fmt_n(u['stars'])} H-STARS · статус: {u['status']}")
    else:
        t.append("Аккаунта на сайте нет")
    return "\n".join(t)


@adm.callback_query(F.data == "adm")
async def cb_adm(c: CallbackQuery, state: FSMContext) -> None:
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Нет доступа", show_alert=True)
    await state.clear()
    await c.answer()
    await c.message.answer("👑 <b>Админ-панель</b>", reply_markup=adm_kb())


@router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext) -> None:
    if m.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await m.answer("👑 <b>Админ-панель</b>", reply_markup=adm_kb())


TARGET_HELP = ("Кому? Отправьте:\n• <b>Telegram ID</b> (цифры) — человек должен был нажать Start в боте,\n• <b>@username</b> в Telegram,\n"
               "• или <b>username на сайте</b> (например, для старых аккаунтов).")


@adm.callback_query(F.data == "a:grant")
async def a_grant(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.set_state(A.target)
    await c.message.answer("🎁 <b>Выдача премиум-номера</b>\n" + TARGET_HELP, reply_markup=cancel_kb())


@adm.message(A.target)
async def a_target(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    j = await site("admin_find", admin_id=m.from_user.id, target=t)
    if not j.get("ok"):
        return await m.answer(err(j) + "\nПопробуйте ещё раз.", reply_markup=cancel_kb())
    await state.update_data(target=t)
    await state.set_state(A.mode)
    b = InlineKeyboardBuilder()
    b.button(text="✍️ Ввести номер вручную", callback_data="a:manual")
    b.button(text="🎲 Случайный по коду страны", callback_data="a:random")
    b.button(text="✖ Отмена", callback_data="adm")
    b.adjust(1)
    await m.answer("Найдено:\n" + who_text(j) + "\n\nКак выдать номер?", reply_markup=b.as_markup())


@adm.callback_query(F.data == "a:manual", A.mode)
async def a_manual(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.set_state(A.phone)
    await c.message.answer("Отправьте номер целиком с кодом страны, например:\n<code>+1 555 777 7777</code>\n<code>+7 777 123 45 67</code>\n<code>+44 7700 900123</code>\n\nКод страны — любой.", reply_markup=cancel_kb())


@adm.callback_query(F.data == "a:random", A.mode)
async def a_random(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.set_state(A.country)
    await c.message.answer("Отправьте код страны цифрами, без «+»: <code>1</code>, <code>7</code>, <code>77</code> (Казахстан), <code>44</code>, <code>380</code>… Бот подберёт свободный номер.", reply_markup=cancel_kb())


async def confirm_grant(m: Message, state: FSMContext, **extra) -> None:
    await state.update_data(**extra)
    d = await state.get_data()
    what = f"номер <code>{esc(d['phone_raw'])}</code>" if d.get("phone_raw") else f"случайный номер с кодом +{esc(d['country'])}"
    b = InlineKeyboardBuilder()
    b.button(text="✅ Выдать", callback_data="a:do")
    b.button(text="✖ Отмена", callback_data="adm")
    b.adjust(2)
    await m.answer(f"Выдать <b>{esc(d['target'])}</b> {what} как премиум-номер?\nСтарый номер получателя будет заменён.", reply_markup=b.as_markup())


@adm.message(A.phone)
async def a_phone(m: Message, state: FSMContext) -> None:
    raw = (m.text or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        return await m.answer("Номер должен содержать от 8 до 15 цифр. Попробуйте ещё раз.", reply_markup=cancel_kb())
    await confirm_grant(m, state, phone=digits, phone_raw="+" + digits, country=None)


@adm.message(A.country)
async def a_country(m: Message, state: FSMContext) -> None:
    cc = re.sub(r"\D", "", m.text or "")
    if not re.fullmatch(r"[1-9]\d{0,2}", cc):
        return await m.answer("Код страны — 1–3 цифры без нуля в начале. Попробуйте ещё раз.", reply_markup=cancel_kb())
    await confirm_grant(m, state, country=cc, phone=None, phone_raw=None)


async def do_grant(m: Message, state: FSMContext, link_tg: str | None = None) -> None:
    d = await state.get_data()
    j = await site("admin_grant", admin_id=m.chat.id, target=d["target"], phone=d.get("phone"), country=d.get("country"), link_tg=link_tg)
    if j.get("need_tg"):
        await state.set_state(A.link_tg)
        return await m.answer("Этот аккаунт на сайте не привязан к Telegram, поэтому коды некуда отправлять.\nОтправьте <b>Telegram ID владельца</b> (он должен нажать Start в боте), чтобы привязать:", reply_markup=cancel_kb())
    if not j.get("ok"):
        await state.clear()
        return await m.answer(err(j), reply_markup=adm_kb())
    await state.clear()
    note = "Пользователь уведомлён в Telegram." if j.get("notified") else "⚠️ Уведомить в Telegram не удалось (человек не нажал Start в боте) — сообщите ему сами."
    await m.answer(f"✅ Выдан премиум-номер <code>{esc(j['number']['formatted'])}</code>\n{note}\n\n" + who_text(j), reply_markup=adm_kb())


@adm.callback_query(F.data == "a:do")
async def a_do(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    if not (await state.get_data()).get("target"):
        return await c.message.answer("Сессия устарела, начните заново.", reply_markup=adm_kb())
    await do_grant(c.message, state)


@adm.message(A.link_tg)
async def a_link(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    if not t.isdigit():
        return await m.answer("Нужен Telegram ID — только цифры.", reply_markup=cancel_kb())
    await do_grant(m, state, link_tg=t)


@adm.callback_query(F.data == "a:find")
async def a_find(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.set_state(A.find)
    await c.message.answer("🔎 Отправьте номер (начните с «+»), Telegram ID, @username или username на сайте.", reply_markup=cancel_kb())


@adm.message(A.find)
async def a_find_q(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    if t.startswith("+"):
        j = await site("admin_find_phone", admin_id=m.from_user.id, phone=t)
    else:
        j = await site("admin_find", admin_id=m.from_user.id, target=t)
        if j.get("ok") and not j.get("number") and not j.get("user") and re.fullmatch(r"\d{8,15}", t):
            j = await site("admin_find_phone", admin_id=m.from_user.id, phone=t)
    await state.clear()
    await m.answer(err(j) if not j.get("ok") else ("Результат:\n" + who_text(j)), reply_markup=adm_kb())


@adm.callback_query(F.data == "a:reset")
async def a_reset(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    await state.set_state(A.reset_target)
    await c.message.answer("♻️ <b>Снять премиум-номер</b>: пользователь получит обычный случайный номер.\n" + TARGET_HELP, reply_markup=cancel_kb())


@adm.message(A.reset_target)
async def a_reset_t(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    j = await site("admin_find", admin_id=m.from_user.id, target=t)
    if not j.get("ok"):
        return await m.answer(err(j), reply_markup=cancel_kb())
    if not j.get("number"):
        return await m.answer("У этого пользователя нет номера.", reply_markup=cancel_kb())
    await state.update_data(target=t)
    b = InlineKeyboardBuilder()
    for k, lbl in (("us", "+1"), ("ru", "+7"), ("kz", "+7 7")):
        b.button(text=f"Выдать {lbl}", callback_data=f"a:rs:{k}")
    b.button(text="✖ Отмена", callback_data="adm")
    b.adjust(3, 1)
    await m.answer("Найдено:\n" + who_text(j) + "\n\nКакой обычный номер выдать взамен?", reply_markup=b.as_markup())


@adm.callback_query(F.data.startswith("a:rs:"))
async def a_reset_do(c: CallbackQuery, state: FSMContext) -> None:
    await c.answer()
    d = await state.get_data()
    if not d.get("target"):
        return await c.message.answer("Сессия устарела, начните заново.", reply_markup=adm_kb())
    j = await site("admin_reset", admin_id=c.from_user.id, target=d["target"], kind=c.data.split(":")[2])
    await state.clear()
    await c.message.answer(err(j) if not j.get("ok") else f"✅ Теперь у пользователя номер <code>{esc(j['number']['formatted'])}</code>", reply_markup=adm_kb())


@adm.callback_query(F.data == "a:stats")
async def a_stats(c: CallbackQuery) -> None:
    await c.answer()
    j = await site("admin_stats", admin_id=c.from_user.id)
    if not j.get("ok"):
        return await c.message.answer(err(j))
    await c.message.answer(
        f"📊 <b>Статистика</b>\nПользователей на сайте: {j['users']}\nВыдано номеров: {j['numbers']} (премиум: {j['premium_numbers']}, ещё без аккаунта: {j['unregistered']})\n"
        f"Пополнений: {j['topup_count']} на {fmt_n(j['topup_stars'])} ⭐", reply_markup=adm_kb())


# ---------------------------------------------------------------- запуск
@router.message()
async def fallback(m: Message) -> None:
    await show_menu(m)


async def main() -> None:
    global http, bot
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(adm)      # админские обработчики — раньше общих
    dp.include_router(router)
    asyncio.create_task(retry_loop())
    me = await bot.get_me()
    log.info("Бот @%s запущен, сайт %s, админов: %d", me.username, SITE_URL, len(ADMIN_IDS))
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "pre_checkout_query"])
    finally:
        await http.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
