import os
import json
import sqlite3
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "bot.db")

DEFAULTS = {
    "withholding": 0.16,
    "total_shares": 35.0,
    "manager_rate": 0.02,
    "mo_shares": 2.25,
    "sho_shares": 2.25,
    "sa_shares": 2.25,
    "other_expense_shares": 2.0,
    "tax_rate": 0.12,
    "avd": 100000.0,
    "ostr": 100000.0,
    "dev": 90000.0,
    "rum": 200000.0,
    "ip_d_fee": 120000.0,
    "general_payout_rate": 0.025,
    "buk_coef": 1.58,
    "kir_coef": 1.58,
    "tro_coef": 1.40,
}
LABELS = {
    "withholding": "Удержание", "total_shares": "Всего долей",
    "manager_rate": "Ставка менеджера", "mo_shares": "Доли Мо",
    "sho_shares": "Доли Шо", "sa_shares": "Доли Са",
    "other_expense_shares": "Доли прочих расходов", "tax_rate": "Налоговая ставка",
    "avd": "авд", "ostr": "остр", "dev": "дев", "rum": "рум",
    "ip_d_fee": "Оплата ИП Д", "general_payout_rate": "Ставка общей выплаты",
    "buk_coef": "Коэффициент Бук", "kir_coef": "Коэффициент Кир", "tro_coef": "Коэффициент Тро",
}
PERCENT_KEYS = {"withholding", "manager_rate", "tax_rate", "general_payout_rate"}
APPROVAL_KEYS = ["Бук", "Кир", "Тро", "Мальдивы", "Яс", "Б", "М", "Мо", "Шо", "Са", "авд", "остр", "дев", "рум"]

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL NOT NULL)")
        con.execute("""CREATE TABLE IF NOT EXISTS calculations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, user_id INTEGER NOT NULL,
            ip_zh REAL NOT NULL, ip_d REAL NOT NULL, buk_turnover REAL NOT NULL,
            kir_turnover REAL NOT NULL, tro_turnover REAL NOT NULL, result_json TEXT NOT NULL)""")
        for k, v in DEFAULTS.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k, v))

def settings() -> Dict[str, float]:
    with db() as con:
        rows = con.execute("SELECT key,value FROM settings").fetchall()
    out = DEFAULTS.copy()
    out.update({r["key"]: float(r["value"]) for r in rows})
    return out

def money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"

def num(s: str) -> float:
    s = s.lower().replace("₽","").replace("руб","").replace("р.","").replace(" ", "").replace(",", ".")
    mult = 1
    if s.endswith("млн"): mult, s = 1_000_000, s[:-3]
    elif s.endswith("м"): mult, s = 1_000_000, s[:-1]
    elif s.endswith("тыс"): mult, s = 1_000, s[:-3]
    elif s.endswith("к"): mult, s = 1_000, s[:-1]
    return float(s) * mult

def allowed(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS

@dataclass
class CalcInput:
    ip_zh: float
    ip_d: float
    buk_turnover: float = 0
    kir_turnover: float = 0
    tro_turnover: float = 0

def calculate(inp: CalcInput, s: Dict[str, float], approved: Dict[str, float] | None = None) -> dict:
    total_revenue = inp.ip_zh + inp.ip_d
    distribution_fund = total_revenue * (1 - s["withholding"])
    one_share = distribution_fund / s["total_shares"] if s["total_shares"] else 0

    mo = one_share * s["mo_shares"]
    sho = one_share * s["sho_shares"]
    sa = one_share * s["sa_shares"]
    other_fund = one_share * s["other_expense_shares"]
    fixed = s["avd"] + s["ostr"] + s["dev"] + s["rum"]

    owner_pool = distribution_fund - mo - sho - sa - other_fund - fixed
    owner_base = owner_pool / 4

    reserve = total_revenue * s["withholding"]
    taxes = total_revenue * s["tax_rate"]
    ip_d_fee = s["ip_d_fee"] if total_revenue else 0
    ip_zh_fee = total_revenue * s["general_payout_rate"] - ip_d_fee if total_revenue else 0
    reserve_for_t = reserve - taxes - ip_d_fee - ip_zh_fee

    def mgr(turnover, coef):
        base = turnover * s["manager_rate"] * (1 - s["withholding"])
        return {"turnover": turnover, "base": base, "coef": coef, "calculated": base * coef}

    managers = {
        "Бук": mgr(inp.buk_turnover, s["buk_coef"]),
        "Кир": mgr(inp.kir_turnover, s["kir_coef"]),
        "Тро": mgr(inp.tro_turnover, s["tro_coef"]),
    }
    manager_total = sum(v["calculated"] for v in managers.values())
    maldives = other_fund - manager_total

    calculated = {
        "Мальдивы": maldives, "Бук": managers["Бук"]["calculated"],
        "Кир": managers["Кир"]["calculated"], "Тро": managers["Тро"]["calculated"],
        "Яс": owner_base, "Б": owner_base, "М": owner_base,
        "Мо": mo, "Шо": sho, "Са": sa,
        "авд": s["avd"], "остр": s["ostr"], "дев": s["dev"], "рум": s["rum"],
    }
    approved_clean = calculated.copy()
    if approved:
        for k, v in approved.items():
            if k in approved_clean:
                approved_clean[k] = float(v)

    # Разница между расчетными и утвержденными выплатами уходит в Т.
    rounding_residue = sum(calculated.values()) - sum(approved_clean.values())
    t_final = owner_base + reserve_for_t + rounding_residue
    payouts = approved_clean | {"Т": t_final}

    total_payouts = sum(payouts.values())
    available_after_maintenance = distribution_fund + reserve_for_t
    control = available_after_maintenance - total_payouts

    warnings = []
    if reserve_for_t < 0: warnings.append("расходы на содержание превышают резерв")
    if maldives < 0: warnings.append("выплаты менеджерам превышают фонд прочих расходов")
    if abs(control) >= 1: warnings.append(f"контрольная разница {money(control)}")

    return {
        "input": asdict(inp), "total_revenue": total_revenue, "distribution_fund": distribution_fund,
        "one_share": one_share, "reserve": reserve, "taxes": taxes, "ip_d_fee": ip_d_fee,
        "ip_zh_fee": ip_zh_fee, "reserve_for_t": reserve_for_t, "other_fund": other_fund,
        "managers": managers, "owner_base": owner_base, "calculated": calculated,
        "approved": approved_clean, "rounding_residue": rounding_residue,
        "payouts": payouts, "total_payouts": total_payouts, "control": control, "warnings": warnings,
    }

def result_text(r: dict) -> str:
    p = r["payouts"]
    lines = [
        "🧮 РАСЧЁТ ВЫПЛАТ", "",
        f"ИП Ж: {money(r['input']['ip_zh'])}", f"ИП Д: {money(r['input']['ip_d'])}",
        f"Общая выручка: {money(r['total_revenue'])}", "",
        f"Резерв 16%: {money(r['reserve'])}", f"Налоги: {money(r['taxes'])}",
        f"Оплата ИП Д: {money(r['ip_d_fee'])}", f"Оплата ИП Ж: {money(r['ip_zh_fee'])}",
        f"Остаток резерва для Т: {money(r['reserve_for_t'])}",
        f"Остаток от округлений → Т: {money(r['rounding_residue'])}", "",
        "👥 ВЫПЛАТЫ",
        f"Яс — {money(p['Яс'])}", f"Б — {money(p['Б'])}", f"М — {money(p['М'])}", f"Т — {money(p['Т'])}",
        f"Мо — {money(p['Мо'])}", f"Шо — {money(p['Шо'])}", f"Са — {money(p['Са'])}", "",
        "📊 МЕНЕДЖЕРЫ",
        f"Бук — {money(p['Бук'])}", f"Кир — {money(p['Кир'])}", f"Тро — {money(p['Тро'])}",
        f"Мальдивы — {money(p['Мальдивы'])}", "",
        "🏢 ПОСТОЯННЫЕ",
        f"авд — {money(p['авд'])}", f"остр — {money(p['остр'])}", f"дев — {money(p['дев'])}", f"рум — {money(p['рум'])}", "",
        f"ИТОГО: {money(r['total_payouts'])}", f"Контроль: {money(r['control'])}",
    ]
    lines += ["", ("⚠️ " + "; ".join(r["warnings"])) if r["warnings"] else "✅ Расчёт сходится"]
    return "\n".join(lines)

def approval_text(r: dict) -> str:
    c = r["calculated"]
    return "\n".join(["Предварительный расчёт готов.", "",
        "Можно принять расчётные суммы как есть или вручную утвердить/округлить выплаты.",
        "Разница после ручного утверждения автоматически уйдёт в Т.", "",
        f"Бук {money(c['Бук'])} · Кир {money(c['Кир'])} · Тро {money(c['Тро'])}",
        f"Яс/Б/М по {money(c['Яс'])}", f"Мо {money(c['Мо'])} · Шо {money(c['Шо'])} · Са {money(c['Са'])}",
        f"Мальдивы {money(c['Мальдивы'])}"])

class Calc(StatesGroup):
    ip_zh=State(); ip_d=State(); buk=State(); kir=State(); tro=State()
    approval_value=State()

kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🧮 Новый расчёт")],
    [KeyboardButton(text="📜 История"), KeyboardButton(text="⚙️ Параметры")],
], resize_keyboard=True)

approve_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Утвердить как рассчитано", callback_data="approve:auto")],
    [InlineKeyboardButton(text="✏️ Утвердить/округлить вручную", callback_data="approve:manual")],
])

dp = Dispatcher(storage=MemoryStorage())

async def guard(m: Message) -> bool:
    if allowed(m.from_user.id): return True
    await m.answer("⛔️ Нет доступа к этому боту.")
    return False

@dp.message(CommandStart())
async def start(m: Message):
    if not await guard(m): return
    await m.answer("Готов считать выплаты по схеме из Excel.\nНажми «🧮 Новый расчёт».", reply_markup=kb)

@dp.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"Ваш Telegram ID: {m.from_user.id}")

@dp.message(F.text == "🧮 Новый расчёт")
async def new_calc(m: Message, state: FSMContext):
    if not await guard(m): return
    await state.clear(); await state.set_state(Calc.ip_zh)
    await m.answer("Введи выручку ИП Ж.\nНапример: 18500000 или 18,5м")

async def take_amount(m, state, key, next_state, prompt):
    try: v=num(m.text)
    except Exception: return await m.answer("Не понял сумму. Например: 18,5м")
    await state.update_data(**{key:v}); await state.set_state(next_state); await m.answer(prompt)

@dp.message(Calc.ip_zh)
async def s1(m:Message,state:FSMContext): await take_amount(m,state,"ip_zh",Calc.ip_d,"Теперь выручка ИП Д:")
@dp.message(Calc.ip_d)
async def s2(m:Message,state:FSMContext): await take_amount(m,state,"ip_d",Calc.buk,"Оборот Бук (если нет — 0):")
@dp.message(Calc.buk)
async def s3(m:Message,state:FSMContext): await take_amount(m,state,"buk_turnover",Calc.kir,"Оборот Кир (если нет — 0):")
@dp.message(Calc.kir)
async def s4(m:Message,state:FSMContext): await take_amount(m,state,"kir_turnover",Calc.tro,"Оборот Тро (если нет — 0):")

@dp.message(Calc.tro)
async def s5(m: Message, state: FSMContext):
    try: v=num(m.text)
    except Exception: return await m.answer("Не понял сумму.")
    d=await state.get_data()
    inp=CalcInput(d["ip_zh"],d["ip_d"],d["buk_turnover"],d["kir_turnover"],v)
    r=calculate(inp,settings())
    await state.update_data(inp=asdict(inp), prelim=r)
    await m.answer(approval_text(r), reply_markup=approve_kb)

async def save_and_show(m: Message, state: FSMContext, r: dict):
    inp=r["input"]
    with db() as con:
        con.execute("""INSERT INTO calculations(created_at,user_id,ip_zh,ip_d,buk_turnover,kir_turnover,tro_turnover,result_json)
        VALUES (?,?,?,?,?,?,?,?)""",(datetime.now().isoformat(timespec="seconds"),m.from_user.id,inp["ip_zh"],inp["ip_d"],
        inp["buk_turnover"],inp["kir_turnover"],inp["tro_turnover"],json.dumps(r,ensure_ascii=False)))
    await state.clear(); await m.answer(result_text(r),reply_markup=kb)

@dp.callback_query(F.data=="approve:auto")
async def auto_approve(cq: CallbackQuery,state:FSMContext):
    d=await state.get_data()
    if "inp" not in d: return await cq.answer("Расчёт уже завершён.",show_alert=True)
    r=calculate(CalcInput(**d["inp"]),settings())
    await cq.answer(); await save_and_show(cq.message,state,r)

@dp.callback_query(F.data=="approve:manual")
async def manual_approve(cq: CallbackQuery,state:FSMContext):
    d=await state.get_data()
    if "prelim" not in d: return await cq.answer("Сначала сделайте расчёт.",show_alert=True)
    await state.update_data(approval_index=0,approved={})
    await state.set_state(Calc.approval_value)
    key=APPROVAL_KEYS[0]; val=d["prelim"]["calculated"][key]
    await cq.answer()
    await cq.message.answer(f"✏️ {key}: расчёт {money(val)}\nВведи утверждённую сумму или «=» чтобы оставить как есть.")

@dp.message(Calc.approval_value)
async def approval_value(m:Message,state:FSMContext):
    d=await state.get_data(); idx=int(d["approval_index"]); key=APPROVAL_KEYS[idx]
    prelim=d["prelim"]; approved=dict(d.get("approved",{}))
    if m.text.strip()=="=": approved[key]=prelim["calculated"][key]
    else:
        try: approved[key]=num(m.text)
        except Exception: return await m.answer("Введи сумму или знак =")
    idx+=1
    if idx<len(APPROVAL_KEYS):
        await state.update_data(approval_index=idx,approved=approved)
        k=APPROVAL_KEYS[idx]; val=prelim["calculated"][k]
        return await m.answer(f"✏️ {k}: расчёт {money(val)}\nУтверждённая сумма или «=»:")
    r=calculate(CalcInput(**d["inp"]),settings(),approved)
    await save_and_show(m,state,r)

@dp.message(F.text=="📜 История")
async def history(m:Message):
    if not await guard(m): return
    with db() as con:
        rows=con.execute("SELECT id,created_at,ip_zh,ip_d FROM calculations WHERE user_id=? ORDER BY id DESC LIMIT 10",(m.from_user.id,)).fetchall()
    if not rows: return await m.answer("История пока пустая.")
    lines=["Последние расчёты:"]
    for r in rows: lines.append(f"#{r['id']} · {r['created_at'][:16].replace('T',' ')} · Ж {money(r['ip_zh'])} · Д {money(r['ip_d'])}")
    lines.append("\nОткрыть: /show 12"); await m.answer("\n".join(lines))

@dp.message(Command("show"))
async def show(m:Message):
    if not await guard(m): return
    try: cid=int(m.text.split()[1])
    except Exception: return await m.answer("Формат: /show 12")
    with db() as con: row=con.execute("SELECT result_json FROM calculations WHERE id=? AND user_id=?",(cid,m.from_user.id)).fetchone()
    if not row: return await m.answer("Расчёт не найден.")
    await m.answer(result_text(json.loads(row["result_json"])))

@dp.message(F.text=="⚙️ Параметры")
async def params(m:Message):
    if not await guard(m): return
    s=settings(); lines=["⚙️ Текущие параметры:"]
    for k in DEFAULTS:
        v=s[k]; shown=f"{v*100:g}%" if k in PERCENT_KEYS else f"{v:g}"
        lines.append(f"{LABELS[k]}: {shown}")
    lines.append("\nИзменить: /set параметр значение\nКлючи: /keys")
    await m.answer("\n".join(lines))

@dp.message(Command("keys"))
async def keys(m:Message):
    if not allowed(m.from_user.id): return
    await m.answer("\n".join(f"{k} — {LABELS[k]}" for k in DEFAULTS))

@dp.message(Command("set"))
async def set_param(m:Message):
    if not allowed(m.from_user.id): return await m.answer("Нет доступа.")
    parts=m.text.split()
    if len(parts)!=3 or parts[1] not in DEFAULTS: return await m.answer("Формат: /set withholding 0.16\nКлючи: /keys")
    try: value=float(parts[2].replace(",","."))
    except Exception: return await m.answer("Значение должно быть числом.")
    with db() as con:
        con.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(parts[1],value))
    await m.answer(f"✅ {LABELS[parts[1]]} = {value}")

async def main():
    if not TOKEN: raise RuntimeError("Не задан BOT_TOKEN")
    init_db()
    bot=Bot(TOKEN)
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())
