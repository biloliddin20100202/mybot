# ton_bot.py
# Pyrogram + SQLite TON mining bot (tugmali, admin panel bilan)
# Ishga tushirish: `pip install pyrogram tgcrypto` va `python ton_bot.py`

import time
import sqlite3
import re
import threading
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# -----------------------
# CONFIG (siz bergan ma'lumotlar)
# -----------------------
API_ID = 29484304
API_HASH = "53b687723c77668106677e6b35b7a0d7"
BOT_TOKEN = "8063808161:AAHVxw7LT-z-mhDRzbGPYaje30uCY1Hqb24"
ADMIN_ID = 7860327961

DB_FILE = "ton_mining.db"

# -----------------------
# In-memory state for simple flows (not persisted)
# -----------------------
# admin_state[admin_id] = {"action":"adding_device", "step":1, "data":{}}
admin_state = {}
# awaiting_withdraw[user_id] = True (waiting user to send amount)
awaiting_withdraw = {}
# awaiting_deposit_amount not needed (we read from photo caption)
# awaiting_set_wallet_by_admin[admin_id] etc:
admin_edit_state = {}

# -----------------------
# DB init
# -----------------------
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

# users: basic user record + referer
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance REAL DEFAULT 0,
    referrals INTEGER DEFAULT 0,
    referer INTEGER DEFAULT NULL
)
""")

# device_types: admin configurable device types
cur.execute("""
CREATE TABLE IF NOT EXISTS device_types (
    name TEXT PRIMARY KEY,
    price REAL,
    hourly_profit REAL,
    duration_seconds INTEGER -- how long device works in seconds
)
""")

# user_devices: active device per user (we allow only 1 active device per user)
cur.execute("""
CREATE TABLE IF NOT EXISTS user_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    device_name TEXT,
    start_time INTEGER,
    end_time INTEGER,
    last_mine_time INTEGER DEFAULT 0
)
""")

# deposits: user sent photo as proof
cur.execute("""
CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    file_id TEXT,
    status TEXT DEFAULT 'pending', -- pending/approved/rejected
    created_at INTEGER
)
""")

# withdraws: user requested withdraws
cur.execute("""
CREATE TABLE IF NOT EXISTS withdraws (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    wallet TEXT,
    status TEXT DEFAULT 'pending',
    created_at INTEGER
)
""")

# settings: key-value for wallet, min withdraw, referral bonus, withdraw referrals required
cur.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")

# initialize default settings if absent
defaults = {
    "admin_wallet": "EQ_EXAMPLE_WALLET_ADDRESS",
    "min_withdraw": "10",  # TON
    "ref_bonus": "1",      # TON per referral or per regen
    "withdraw_referrals_required": "5",
    "default_device_duration_days": "7"  # default duration in days when creating device_type via admin quick add
}
for k, v in defaults.items():
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
conn.commit()

# insert default device types if none exist
cur.execute("SELECT COUNT(*) FROM device_types")
if cur.fetchone()[0] == 0:
    # default: Bronza, Gold, Legendary
    cur.execute("INSERT INTO device_types VALUES (?, ?, ?, ?)", ("Bronza", 1.0, 0.01, 7*24*3600))
    cur.execute("INSERT INTO device_types VALUES (?, ?, ?, ?)", ("Gold", 5.0, 0.05, 7*24*3600))
    cur.execute("INSERT INTO device_types VALUES (?, ?, ?, ?)", ("Legendary", 10.0, 0.1, 7*24*3600))
    conn.commit()

# -----------------------
# Helpers
# -----------------------
def get_setting(key):
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else None

def set_setting(key, value):
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()

def user_exists(user_id):
    cur.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
    return cur.fetchone() is not None

def add_user(user_id, username=None, referer=None):
    if not user_exists(user_id):
        cur.execute("INSERT INTO users (user_id, username, referer) VALUES (?, ?, ?)", (user_id, username, referer))
        conn.commit()

def get_device_types():
    cur.execute("SELECT name, price, hourly_profit, duration_seconds FROM device_types")
    return cur.fetchall()

def get_user_balance(user_id):
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    return r[0] if r else 0.0

def format_float(x):
    return "{:.4f}".format(float(x))

# -----------------------
# Bot
# -----------------------
app = Client("ton_mining_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Menus ---
def main_menu_kb():
    # inline keyboard for main user menu
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📦 Aparatlar do‘koni", callback_data="shop")],
            [InlineKeyboardButton("⛏ Mining (1 soatda 1 marta)", callback_data="mine")],
            [InlineKeyboardButton("💰 Hisobim", callback_data="balance"),
             InlineKeyboardButton("➕ Hisobni to‘ldirish", callback_data="deposit")],
            [InlineKeyboardButton("👥 Referalim", callback_data="referral"),
             InlineKeyboardButton("📤 Pul yechish", callback_data="withdraw")]
        ]
    )

def admin_menu_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚙ Aparatlar boshqaruvi", callback_data="admin_devices")],
            [InlineKeyboardButton("📥 To‘lovlar (depositlar)", callback_data="admin_deposits"),
             InlineKeyboardButton("📤 Yechib olish so‘rovlari", callback_data="admin_withdraws")],
            [InlineKeyboardButton("🔧 Sozlamalar", callback_data="admin_settings"),
             InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="admin_users")],
            [InlineKeyboardButton("📢 Xabar yuborish (hamma)", callback_data="admin_broadcast")]
        ]
    )

# -----------------------
# Start handler with referral handling
# -----------------------
@app.on_message(filters.command("start") & filters.private)
def start_handler(client, message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    # check referral param
    text = message.text or ""
    referer = None
    # start can be like "/start" or "/start 12345" or "/start=12345"
    m = re.search(r"start(?:\s|=)?(\d+)", text)
    if m:
        try:
            referer_id = int(m.group(1))
            if referer_id != user_id and user_exists(referer_id):
                referer = referer_id
        except:
            referer = None

    add_user(user_id, username=username, referer=referer)
    # if referer exists, increment referral counter and maybe give bonus immediately if you want
    if referer:
        cur.execute("UPDATE users SET referrals = referrals + 1 WHERE user_id=?", (referer,))
        # give referal bonus to referer immediately
        try:
            ref_bonus = float(get_setting("ref_bonus") or "1")
        except:
            ref_bonus = 1.0
        cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (ref_bonus, referer))
        conn.commit()
        # notify referer
        try:
            app.send_message(referer, f"👥 Siz yangi referal oldingiz! Bonus: {format_float(ref_bonus)} TON.")
        except:
            pass

    # greet user
    if user_id == ADMIN_ID:
        message.reply("👋 Admin panelga xush kelibsiz.", reply_markup=admin_menu_kb())
    else:
        message.reply("👋 Xush kelibsiz! TON mining apparatlar do‘koniga hush kelibsiz.", reply_markup=main_menu_kb())

# -----------------------
# Callback router for user and admin actions
# -----------------------
@app.on_callback_query()
def callback_router(client, callback_query):
    data = callback_query.data
    uid = callback_query.from_user.id

    # --- USER: shop ---
    if data == "shop":
        types = get_device_types()
        if not types:
            callback_query.message.edit_text("📦 Do‘kon bo‘sh. Admin, iltimos apparat qo‘shing.")
            return
        buttons = []
        for name, price, hourly, duration in types:
            buttons.append([InlineKeyboardButton(f"{name} | {price} TON | {hourly} TON/soat", callback_data=f"buy:{name}")])
        buttons.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="menu")])
        callback_query.message.edit_text("📦 Do‘kon. Qaysi apparatni sotib olasiz?", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # --- USER: buy ---
    if data.startswith("buy:"):
        if uid == ADMIN_ID:
            callback_query.answer("Admin o‘zi sotib ololmaydi.", show_alert=True)
            return
        _, name = data.split(":", 1)
        # check device type exists
        cur.execute("SELECT price, hourly_profit, duration_seconds FROM device_types WHERE name=?", (name,))
        row = cur.fetchone()
        if not row:
            callback_query.answer("❌ Bunday apparat topilmadi.", show_alert=True)
            return
        price, hourly_profit, duration_seconds = row
        # check user balance
        cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
        bal = cur.fetchone()[0]
        if bal < price:
            callback_query.answer(f"❌ Balansingiz yetarli emas. Narx: {price} TON, Sizda: {format_float(bal)} TON", show_alert=True)
            return
        # check user has active device?
        now = int(time.time())
        cur.execute("SELECT id, end_time FROM user_devices WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
        ud = cur.fetchone()
        if ud and ud[1] > now:
            callback_query.answer("❌ Sizda hozir faol apparat mavjud. Yaratilgan apparat muddati tugamasin.", show_alert=True)
            return
        # deduct and create user_device
        cur.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (price, uid))
        cur.execute("INSERT INTO user_devices (user_id, device_name, start_time, end_time, last_mine_time) VALUES (?, ?, ?, ?, ?)",
                    (uid, name, now, now + duration_seconds, 0))
        conn.commit()
        callback_query.answer("✅ Aparat sotib olindi. Har 1 soatda ⛏ tugmasini bosishni unutmang!", show_alert=True)
        try:
            callback_query.message.edit_text(f"✅ Siz {name} apparatini sotib oldingiz.\n⏳ Muddati: {int(duration_seconds/86400)} kun.\n⛏ Har 1 soatda bosing va TON oling.", reply_markup=main_menu_kb())
        except:
            pass
        return

    # --- USER: mine ---
    if data == "mine":
        # check active device
        cur.execute("SELECT id, device_name, start_time, end_time, last_mine_time FROM user_devices WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
        row = cur.fetchone()
        now = int(time.time())
        if not row or row[3] < now:
            callback_query.answer("❌ Sizda hozir faol apparat mavjud emas yoki muddati tugagan. Do‘konga o'ting.", show_alert=True)
            return
        ud_id, device_name, start_time, end_time, last_mine_time = row
        # check 1 hour rule
        if now - last_mine_time < 3600:
            remain = 3600 - (now - last_mine_time)
            mins = remain // 60
            secs = remain % 60
            callback_query.answer(f"⌛ Keyingi miningni {mins}m {secs}s dan keyin bosing.", show_alert=True)
            return
        # get hourly profit from device_types
        cur.execute("SELECT hourly_profit FROM device_types WHERE name=?", (device_name,))
        hp = cur.fetchone()
        if not hp:
            callback_query.answer("❌ Aparat turi topilmadi (admin o‘chirgan bo‘lishi mumkin).", show_alert=True)
            return
        hourly_profit = float(hp[0])
        # add to user balance and update last_mine_time
        cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (hourly_profit, uid))
        cur.execute("UPDATE user_devices SET last_mine_time=? WHERE id=?", (now, ud_id))
        conn.commit()
        callback_query.answer(f"✅ {format_float(hourly_profit)} TON qazib olindi!", show_alert=True)
        try:
            callback_query.message.edit_text(f"✅ {format_float(hourly_profit)} TON qo‘shildi.\n{main_menu_kb()}", reply_markup=main_menu_kb())
        except:
            pass
        return

    # --- USER: balance ---
    if data == "balance":
        bal = get_user_balance(uid)
        callback_query.message.edit_text(f"💰 Sizning balansingiz: {format_float(bal)} TON", reply_markup=main_menu_kb())
        return

    # --- USER: deposit ---
    if data == "deposit":
        wallet = get_setting("admin_wallet")
        callback_query.message.edit_text(f"💳 Hisobni to‘ldirish:\n\nAdmin wallet: `{wallet}`\n\nTON yuborganingizdan so‘ng, CHEK (skrinshot) ni shu chatga yuboring — bot avtomatik adminga yuboradi va tasdiqni kutadi.", reply_markup=main_menu_kb())
        return

    # --- USER: referral ---
    if data == "referral":
        # create referral link
        bot_username = client.get_me().username
        link = f"https://t.me/{bot_username}?start={uid}"
        cur.execute("SELECT referrals FROM users WHERE user_id=?", (uid,))
        refs = cur.fetchone()[0]
        callback_query.message.edit_text(f"👥 Sizning referal linkingiz:\n{link}\n\nHozirgacha: {refs} ta referal", reply_markup=main_menu_kb())
        return

    # --- USER: withdraw ---
    if data == "withdraw":
        # show balance and ask for withdraw amount via next message
        bal = get_user_balance(uid)
        min_w = float(get_setting("min_withdraw") or "10")
        req_refs = int(float(get_setting("withdraw_referrals_required") or "5"))
        cur.execute("SELECT referrals FROM users WHERE user_id=?", (uid,))
        refs = cur.fetchone()[0]
        text = f"💰 Sizning balansingiz: {format_float(bal)} TON\nMinimal yechib olish: {min_w} TON\nKerakli referal soni: {req_refs}\nSizda referallar: {refs}\n\nAgar shartlar bajarsa, 'So‘rov yuborish' tugmasini bosing va keyin SUMMA va WALLET manzilingizni yuboring."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("So‘rov yuborish", callback_data="withdraw_request")],
                                   [InlineKeyboardButton("⬅️ Orqaga", callback_data="menu")]])
        callback_query.message.edit_text(text, reply_markup=kb)
        return

    if data == "withdraw_request":
        # check user meets minimal criteria
        bal = get_user_balance(uid)
        min_w = float(get_setting("min_withdraw") or "10")
        req_refs = int(float(get_setting("withdraw_referrals_required") or "5"))
        cur.execute("SELECT referrals FROM users WHERE user_id=?", (uid,))
        refs = cur.fetchone()[0]
        if bal < min_w:
            callback_query.answer(f"❌ Minimal {min_w} TON yetarli emas. Sizda: {format_float(bal)} TON", show_alert=True)
            return
        if refs < req_refs:
            callback_query.answer(f"❌ Kerakli referallar soni: {req_refs}. Sizda: {refs}", show_alert=True)
            return
        # ask user to send text "SUMMA WALLET" in next message — set awaiting flag
        awaiting_withdraw[uid] = True
        callback_query.answer("🔔 Endi chatga SUMMA va WALLET manzilingizni yuboring (masalan: 12.5 EQxxx...)", show_alert=True)
        return

    # --- back to main menu ---
    if data == "menu" or data == "main_menu":
        if uid == ADMIN_ID:
            callback_query.message.edit_text("👑 Admin panel", reply_markup=admin_menu_kb())
        else:
            callback_query.message.edit_text("Asosiy menyu", reply_markup=main_menu_kb())
        return

    # ------------------ ADMIN ACTIONS ------------------
    if uid == ADMIN_ID:
        # admin: open admin menu
        if data == "admin_panel":
            callback_query.message.edit_text("👑 Admin panel", reply_markup=admin_menu_kb())
            return

        # admin: device management
        if data == "admin_devices":
            # show list and options
            types = get_device_types()
            buttons = []
            for name, price, hourly, dur in types:
                buttons.append([InlineKeyboardButton(f"{name} | {price} TON | {hourly} TON/soat | {int(dur/86400)} kun",
                                                     callback_data=f"adm_edit:{name}")])
            buttons.append([InlineKeyboardButton("➕ Yangi apparat qo‘shish", callback_data="adm_add_device")])
            buttons.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_panel")])
            callback_query.message.edit_text("⚙ Aparatlar boshqaruvi", reply_markup=InlineKeyboardMarkup(buttons))
            return

        # admin: add device start
        if data == "adm_add_device":
            admin_state[uid] = {"action": "add_device", "step": 1, "data": {}}
            callback_query.message.edit_text("➕ Yangi apparat qo‘shish.\nIltimos aparat nomini yuboring (masalan: Gold):")
            return

        # admin: edit specific device
        if data.startswith("adm_edit:"):
            _, name = data.split(":", 1)
            # show options: edit name/price/hourly/duration/delete
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Nomni tahrirlash", callback_data=f"adm_edit_name:{name}"),
                 InlineKeyboardButton("💸 Narxni tahrirlash", callback_data=f"adm_edit_price:{name}")],
                [InlineKeyboardButton("⚡ Soatlik foyda", callback_data=f"adm_edit_hourly:{name}"),
                 InlineKeyboardButton("⏳ Muddat (kun)", callback_data=f"adm_edit_duration:{name}")],
                [InlineKeyboardButton("❌ O‘chirish", callback_data=f"adm_delete:{name}")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_devices")]
            ])
            callback_query.message.edit_text(f"⚙ {name} ni tahrirlash", reply_markup=kb)
            return

        # admin: delete device
        if data.startswith("adm_delete:"):
            _, name = data.split(":", 1)
            cur.execute("DELETE FROM device_types WHERE name=?", (name,))
            conn.commit()
            callback_query.answer("✅ O‘chirildi", show_alert=True)
            # refresh device list
            callback_query.message.edit_text("⚙ Aparatlar boshqaruvi — yangilandi", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_devices")]]))
            return

        # admin: edit flows (name/price/hourly/duration)
        if data.startswith("adm_edit_name:") or data.startswith("adm_edit_price:") or data.startswith("adm_edit_hourly:") or data.startswith("adm_edit_duration:"):
            action, name = data.split(":", 1)
            admin_state[uid] = {"action": action, "step": 1, "data": {"name": name}}
            prompt = {
                "adm_edit_name": "Yangi nomni kiriting:",
                "adm_edit_price": "Yangi narxni (TON) kiriting:",
                "adm_edit_hourly": "Yangi soatlik foydani (TON) kiriting:",
                "adm_edit_duration": "Yangi muddatni kunlarda kiriting (masalan: 7):"
            }[action]
            callback_query.message.edit_text(prompt)
            return

        # admin: deposits list
        if data == "admin_deposits":
            cur.execute("SELECT id, user_id, amount, status, created_at FROM deposits ORDER BY created_at DESC LIMIT 30")
            rows = cur.fetchall()
            if not rows:
                callback_query.message.edit_text("📥 Depozitlar yo‘q", reply_markup=admin_menu_kb())
                return
            text = "📥 Oxirgi depozitlar:\n\n"
            buttons = []
            for did, uid2, amount, status, created in rows:
                text += f"ID:{did} | user:{uid2} | {amount} TON | {status}\n"
                buttons.append([InlineKeyboardButton(f"ID {did} tasdiqlash", callback_data=f"adm_deposit_approve:{did}"),
                                InlineKeyboardButton(f"ID {did} rad etish", callback_data=f"adm_deposit_reject:{did}")])
            buttons.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_panel")])
            callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return

        # admin: approve/reject deposit (also deposit forwarding sets file_id so admin can see)
        if data.startswith("adm_deposit_approve:") or data.startswith("adm_deposit_reject:"):
            cmd, did = data.split(":", 1)
            did = int(did)
            cur.execute("SELECT user_id, amount, status FROM deposits WHERE id=?", (did,))
            row = cur.fetchone()
            if not row:
                callback_query.answer("ID topilmadi", show_alert=True)
                return
            uid2, amount, status = row
            if cmd.endswith("approve"):
                if status == "approved":
                    callback_query.answer("Allaqachon tasdiqlangan", show_alert=True)
                    return
                cur.execute("UPDATE deposits SET status='approved' WHERE id=?", (did,))
                cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, uid2))
                conn.commit()
                callback_query.answer("✅ Tasdiqlandi", show_alert=True)
                try:
                    app.send_message(uid2, f"✅ Sizning depozitingiz ({amount} TON) tasdiqlandi va balansingizga qo‘shildi.")
                except:
                    pass
            else:
                # reject
                cur.execute("UPDATE deposits SET status='rejected' WHERE id=?", (did,))
                conn.commit()
                callback_query.answer("❌ Rad etildi", show_alert=True)
                try:
                    app.send_message(uid2, f"❌ Sizning depozitingiz (ID:{did}) rad etildi. Iltimos ma'lumotlarni tekshiring.")
                except:
                    pass
            return

        # admin: withdraws
        if data == "admin_withdraws":
            cur.execute("SELECT id, user_id, amount, wallet, status, created_at FROM withdraws ORDER BY created_at DESC LIMIT 30")
            rows = cur.fetchall()
            if not rows:
                callback_query.message.edit_text("📤 Yechib olish so‘rovlari yo‘q", reply_markup=admin_menu_kb())
                return
            text = "📤 Oxirgi yechib olish so‘rovlari:\n\n"
            buttons = []
            for wid, uid2, amount, wallet, status, created in rows:
                text += f"ID:{wid} | user:{uid2} | {amount} TON | {wallet} | {status}\n"
                buttons.append([InlineKeyboardButton(f"ID {wid} tasdiqlash", callback_data=f"adm_with_approve:{wid}"),
                                InlineKeyboardButton(f"ID {wid} rad etish", callback_data=f"adm_with_reject:{wid}")])
            buttons.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_panel")])
            callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data.startswith("adm_with_approve:") or data.startswith("adm_with_reject:"):
            cmd, wid = data.split(":", 1)
            wid = int(wid)
            cur.execute("SELECT user_id, amount, status FROM withdraws WHERE id=?", (wid,))
            row = cur.fetchone()
            if not row:
                callback_query.answer("ID topilmadi", show_alert=True)
                return
            uid2, amount, status = row
            if cmd.endswith("approve"):
                if status == "approved":
                    callback_query.answer("Allaqachon tasdiqlangan", show_alert=True)
                    return
                # mark approved and subtract balance (should already be reserved, but we ensure)
                cur.execute("UPDATE withdraws SET status='approved' WHERE id=?", (wid,))
                cur.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, uid2))
                conn.commit()
                callback_query.answer("✅ Yechib olish tasdiqlandi. Foydalanuvchiga xabar jo‘natildi.", show_alert=True)
                try:
                    app.send_message(uid2, f"✅ Sizning yechib olish so‘rovingiz ({amount} TON) tasdiqlandi. Tez orada yuboriladi.")
                except:
                    pass
            else:
                cur.execute("UPDATE withdraws SET status='rejected' WHERE id=?", (wid,))
                conn.commit()
                callback_query.answer("❌ Yechib olish rad etildi", show_alert=True)
                try:
                    app.send_message(uid2, f"❌ Sizning yechib olish so‘rovingiz ({amount} TON) rad etildi.")
                except:
                    pass
            return

        # admin: users list
        if data == "admin_users":
            cur.execute("SELECT user_id, username, balance, referrals FROM users ORDER BY balance DESC LIMIT 100")
            rows = cur.fetchall()
            text = "👥 Foydalanuvchilar (yuklab 100):\n\n"
            for u_id, uname, bal, refs in rows:
                text += f"{u_id} | @{uname if uname else 'no-name'} | {format_float(bal)} TON | refs:{refs}\n"
            callback_query.message.edit_text(text, reply_markup=admin_menu_kb())
            return

        # admin: settings
        if data == "admin_settings":
            text = "🔧 Sozlamalar:\n"
            wallet = get_setting("admin_wallet")
            min_w = get_setting("min_withdraw")
            refb = get_setting("ref_bonus")
            req_refs = get_setting("withdraw_referrals_required")
            text += f"Wallet: {wallet}\nMin withdraw: {min_w} TON\nReferral bonus: {refb} TON\nWithdraw req refs: {req_refs}\n\n"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Wallet o‘zgartirish", callback_data="adm_set_wallet"),
                 InlineKeyboardButton("✏️ Min withdraw", callback_data="adm_set_min_withdraw")],
                [InlineKeyboardButton("✏️ Referral bonus", callback_data="adm_set_ref_bonus"),
                 InlineKeyboardButton("✏️ Withdraw ref req", callback_data="adm_set_with_refs")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="admin_panel")]
            ])
            callback_query.message.edit_text(text, reply_markup=kb)
            return

        # admin settings edit triggers
        if data in ("adm_set_wallet", "adm_set_min_withdraw", "adm_set_ref_bonus", "adm_set_with_refs"):
            admin_state[uid] = {"action": data, "step": 1, "data": {}}
            prompts = {
                "adm_set_wallet": "Yangi wallet manzilini yuboring:",
                "adm_set_min_withdraw": "Yangi minimal yechib olish summasini (TON) kiriting:",
                "adm_set_ref_bonus": "Yangi referral bonusni (TON) kiriting:",
                "adm_set_with_refs": "Yechib olish uchun zarur referal sonini kiriting:"
            }
            callback_query.message.edit_text(prompts[data])
            return

        # admin: broadcast
        if data == "admin_broadcast":
            admin_state[uid] = {"action": "broadcast", "step": 1, "data": {}}
            callback_query.message.edit_text("📢 Broadcast xabar yuborish. Xabar matnini yuboring:")
            return

    # default fallback — menu
    callback_query.answer()

# -----------------------
# Message handler for deposits (photo), withdraw amount, and admin states
# -----------------------
@app.on_message(filters.private & ~filters.bot)
def private_message_handler(client, message):
    uid = message.from_user.id
    text = message.text or ""
    # 1) If message is photo (deposit)
    if message.photo:
        # get caption as amount if provided
        caption = message.caption or ""
        amount = 0.0
        # try to parse a number from caption
        nums = re.findall(r"[\d]+(?:[.,]\d+)?", caption)
        if nums:
            try:
                amount = float(nums[0].replace(',', '.'))
            except:
                amount = 0.0
        # save deposit
        file_id = message.photo.file_id
        now = int(time.time())
        cur.execute("INSERT INTO deposits (user_id, amount, file_id, created_at) VALUES (?, ?, ?, ?)",
                    (uid, amount, file_id, now))
        dep_id = cur.lastrowid
        conn.commit()
        # forward to admin with approve/reject inline buttons
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"adm_deposit_approve:{dep_id}"),
             InlineKeyboardButton("❌ Rad etish", callback_data=f"adm_deposit_reject:{dep_id}")]
        ])
        # message text for admin
        try:
            client.send_message(ADMIN_ID, f"📥 Yangi depozit (ID:{dep_id})\nUser: {uid}\nSum: {format_float(amount)} TON\n-- Chek quyida --")
            client.send_photo(ADMIN_ID, file_id, caption=f"ID:{dep_id} | user:{uid} | {format_float(amount)} TON", reply_markup=kb)
        except Exception as e:
            print("Admin notify error:", e)
        message.reply("✅ Chek qabul qilindi. Admin tasdiqlasin.")
        return

    # 2) If user is sending withdraw amount and awaiting flag set
    if uid in awaiting_withdraw:
        # expecting "SUMMA WALLET" or two-line input
        content = text.strip()
        parts = content.split()
        if len(parts) >= 2:
            # try parse first numeric
            num_match = re.search(r"[\d]+(?:[.,]\d+)?", parts[0])
            if not num_match:
                message.reply("❌ Iltimos SUMMAni raqam sifatida kiriting. Masalan: 12.5 EQxxxx...")
                return
            amount = float(num_match.group(0).replace(',', '.'))
            wallet = " ".join(parts[1:])
            # check criteria again
            bal = get_user_balance(uid)
            min_w = float(get_setting("min_withdraw") or "10")
            req_refs = int(float(get_setting("withdraw_referrals_required") or "5"))
            cur.execute("SELECT referrals FROM users WHERE user_id=?", (uid,))
            refs = cur.fetchone()[0]
            if amount > bal:
                message.reply(f"❌ Sizda yetarli balans yo‘q. Sizda: {format_float(bal)} TON")
                awaiting_withdraw.pop(uid, None)
                return
            if amount < min_w:
                message.reply(f"❌ Minimal yechib olish: {min_w} TON")
                awaiting_withdraw.pop(uid, None)
                return
            if refs < req_refs:
                message.reply(f"❌ Yechib olish uchun kerakli referallar: {req_refs}. Sizda: {refs}")
                awaiting_withdraw.pop(uid, None)
                return
            # create withdraw request in DB
            now = int(time.time())
            cur.execute("INSERT INTO withdraws (user_id, amount, wallet, created_at) VALUES (?, ?, ?, ?)",
                        (uid, amount, wallet, now))
            wid = cur.lastrowid
            conn.commit()
            # notify admin with approve/reject inline buttons
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"adm_with_approve:{wid}"),
                 InlineKeyboardButton("❌ Rad etish", callback_data=f"adm_with_reject:{wid}")]
            ])
            client.send_message(ADMIN_ID, f"📤 Yangi yechib olish so‘rovi (ID:{wid})\nUser: {uid}\nSum: {format_float(amount)} TON\nWallet: {wallet}", reply_markup=kb)
            message.reply("✅ Yechib olish so‘rovingiz adminga yuborildi. Tasdiqlanishini kuting.")
            awaiting_withdraw.pop(uid, None)
            return
        else:
            message.reply("❌ Iltimos SUMMA va WALLET manzilingizni bitta xabarda yuboring (masalan: `12.5 EQxxxx...`).")
            return

    # 3) Admin flows (adding device, editing settings, broadcast)
    if uid == ADMIN_ID and uid in admin_state:
        st = admin_state[uid]
        act = st.get("action")
        step = st.get("step")
        # add device flow
        if act == "add_device":
            data = st["data"]
            if step == 1:
                # received device name
                name = text.strip()
                data["name"] = name
                admin_state[uid]["step"] = 2
                message.reply("➡️ Narxni kiriting (TON bilan, masalan: 5.0):")
                return
            elif step == 2:
                # price
                m = re.search(r"[\d]+(?:[.,]\d+)?", text)
                if not m:
                    message.reply("❌ Iltimos raqam formatida kiriting.")
                    return
                price = float(m.group(0).replace(',', '.'))
                data["price"] = price
                admin_state[uid]["step"] = 3
                message.reply("➡️ Soatlik foydani kiriting (TON/soat, masalan: 0.05):")
                return
            elif step == 3:
                m = re.search(r"[\d]+(?:[.,]\d+)?", text)
                if not m:
                    message.reply("❌ Iltimos raqam formatida kiriting.")
                    return
                hourly = float(m.group(0).replace(',', '.'))
                data["hourly"] = hourly
                admin_state[uid]["step"] = 4
                message.reply("➡️ Muddatni kunlarda kiriting (masalan: 7):")
                return
            elif step == 4:
                m = re.search(r"[\d]+", text)
                if not m:
                    message.reply("❌ Iltimos butun son kiriting (kunlarda).")
                    return
                days = int(m.group(0))
                data["days"] = days
                # insert into device_types
                dur = days * 24 * 3600
                try:
                    cur.execute("INSERT INTO device_types (name, price, hourly_profit, duration_seconds) VALUES (?, ?, ?, ?)",
                                (data["name"], data["price"], data["hourly"], dur))
                    conn.commit()
                except Exception as e:
                    message.reply(f"❌ Xatolik: {e}")
                    admin_state.pop(uid, None)
                    return
                message.reply(f"✅ Aparat qo‘shildi: {data['name']} | Narx: {data['price']} TON | Soatlik: {data['hourly']} TON | {days} kun")
                admin_state.pop(uid, None)
                return

        # admin edit device (name/price/hourly/duration)
        if act and act.startswith("adm_edit_"):
            # act could be adm_edit_name, adm_edit_price...
            parts = act.split("_")
            # we stored selected name in st["data"]["name"]
            target = st["data"].get("name")
            if parts[1] == "edit":
                key = parts[2]  # e.g., name, price, hourly, duration or adm_set_x
                if key == "name":
                    # new name in text
                    new_name = text.strip()
                    # change primary key — do it carefully
                    cur.execute("UPDATE device_types SET name=? WHERE name=?", (new_name, target))
                    conn.commit()
                    message.reply(f"✅ Nom o‘zgartirildi: {new_name}")
                    admin_state.pop(uid, None)
                    return
                elif key == "price":
                    m = re.search(r"[\d]+(?:[.,]\d+)?", text)
                    if not m:
                        message.reply("❌ Raqam kiriting.")
                        return
                    new_price = float(m.group(0).replace(',', '.'))
                    cur.execute("UPDATE device_types SET price=? WHERE name=?", (new_price, target))
                    conn.commit()
                    message.reply(f"✅ Narx o‘zgartirildi: {new_price} TON")
                    admin_state.pop(uid, None)
                    return
                elif key == "hourly":
                    m = re.search(r"[\d]+(?:[.,]\d+)?", text)
                    if not m:
                        message.reply("❌ Raqam kiriting.")
                        return
                    new_h = float(m.group(0).replace(',', '.'))
                    cur.execute("UPDATE device_types SET hourly_profit=? WHERE name=?", (new_h, target))
                    conn.commit()
                    message.reply(f"✅ Soatlik foyda o‘zgartirildi: {new_h} TON/soat")
                    admin_state.pop(uid, None)
                    return
                elif key == "duration":
                    m = re.search(r"[\d]+", text)
                    if not m:
                        message.reply("❌ Butun son kiriting (kunlar).")
                        return
                    days = int(m.group(0))
                    dur = days * 24 * 3600
                    cur.execute("UPDATE device_types SET duration_seconds=? WHERE name=?", (dur, target))
                    conn.commit()
                    message.reply(f"✅ Muddat o‘zgartirildi: {days} kun")
                    admin_state.pop(uid, None)
                    return

        # admin settings edits
        if act and act.startswith("adm_set_"):
            key = act
            if key == "adm_set_wallet":
                new_wallet = text.strip()
                set_setting("admin_wallet", new_wallet)
                message.reply(f"✅ Wallet o‘zgartirildi: {new_wallet}")
                admin_state.pop(uid, None)
                return
            if key == "adm_set_min_withdraw":
                m = re.search(r"[\d]+(?:[.,]\d+)?", text)
                if not m:
                    message.reply("❌ Raqam kiriting.")
                    return
                set_setting("min_withdraw", m.group(0).replace(',', '.'))
                message.reply(f"✅ Minimal yechib olish o‘zgardi: {m.group(0)} TON")
                admin_state.pop(uid, None)
                return
            if key == "adm_set_ref_bonus":
                m = re.search(r"[\d]+(?:[.,]\d+)?", text)
                if not m:
                    message.reply("❌ Raqam kiriting.")
                    return
                set_setting("ref_bonus", m.group(0).replace(',', '.'))
                message.reply(f"✅ Referral bonusi o‘zgardi: {m.group(0)} TON")
                admin_state.pop(uid, None)
                return
            if key == "adm_set_with_refs":
                m = re.search(r"[\d]+", text)
                if not m:
                    message.reply("❌ Butun son kiriting.")
                    return
                set_setting("withdraw_referrals_required", m.group(0))
                message.reply(f"✅ Withdraw uchun referal soni o‘zgardi: {m.group(0)}")
                admin_state.pop(uid, None)
                return

        # admin broadcast
        if act == "broadcast":
            msg_text = text
            # send to all users (careful: in big DB this can be slow)
            cur.execute("SELECT user_id FROM users")
            rows = cur.fetchall()
            sent, failed = 0, 0
            for (u_id,) in rows:
                try:
                    client.send_message(u_id, f"📢 Broadcast:\n\n{msg_text}")
                    sent += 1
                except:
                    failed += 1
            message.reply(f"✅ Broadcast yuborildi. Yuborildi: {sent}, muvaffaqiyatsiz: {failed}")
            admin_state.pop(uid, None)
            return

    # 4) Fallback: if admin sent plain text but no state
    if uid == ADMIN_ID:
        message.reply("👑 Admin: panelga qaytish uchun /start bosing yoki menyudan foydalaning.")
    else:
        message.reply("🤖 Asosiy menyu uchun /start bosing yoki menyudan foydalaning.")

# -----------------------
# Background worker (optional)
# We DO NOT auto-credit mining here because you asked mining to be manual (user must press per hour).
# But we can optionally notify users whose devices expired. Let's run a small checker that every 10 minutes
# finds devices whose end_time < now and notifies user that device expired.
# -----------------------
def expiry_worker():
    while True:
        now = int(time.time())
        cur.execute("SELECT id, user_id, device_name, end_time FROM user_devices WHERE end_time <= ? AND end_time > 0", (now,))
        rows = cur.fetchall()
        for r in rows:
            ud_id, user_id, device_name, end_time = r
            # Instead of deleting we just notify and set end_time=0 to mark expired
            # But only notify once: check last_mine_time is not None - we'll mark end_time to -1 after notifying to avoid repeated msgs
            try:
                app.send_message(user_id, f"⏳ Sizning {device_name} apparatingiz muddati tugadi. Yangi apparat sotib oling.")
            except:
                pass
            # mark expired with end_time = 0 to signal no active device
            cur.execute("UPDATE user_devices SET end_time = 0 WHERE id=?", (ud_id,))
            conn.commit()
        time.sleep(600)  # 10 minutes

threading.Thread(target=expiry_worker, daemon=True).start()

# -----------------------
# Run bot
# -----------------------
print("🤖 TON mining bot ishga tushmoqda...")
app.run()
