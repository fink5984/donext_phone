import os
import re
import json
import asyncio
import threading
import requests
import websockets
from websockets.exceptions import ConnectionClosed
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# =============================
# Twilio API for call hangup
# =============================
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

def twilio_hangup_call(call_sid: str):
    """נתק שיחת Twilio בפועל"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return False
    try:
        import requests
        from requests.auth import HTTPBasicAuth
        
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json"
        data = {"Status": "completed"}
        
        response = requests.post(
            url,
            data=data,
            auth=HTTPBasicAuth(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        )
        return response.status_code == 200
    except Exception as e:
        root_logger.error(f"Failed to hangup Twilio call {call_sid}: {e}")
        return False

def number_to_hebrew_words(num):
    """המרת מספר לטקסט עברי מדובר משופר"""
    if num == 0:
        return "אפס"
    
    ones = ["", "אחד", "שניים", "שלושה", "ארבעה", "חמישה", "שישה", "שבעה", "שמונה", "תשעה"]
    ones_feminine = ["", "אחת", "שתיים", "שלוש", "ארבע", "חמש", "שש", "שבע", "שמונה", "תשע"]
    teens = ["עשרה", "אחד עשר", "שניים עשר", "שלושה עשר", "ארבעה עשר", "חמישה עשר", 
             "שישה עשר", "שבעה עשר", "שמונה עשר", "תשעה עשר"]
    tens = ["", "", "עשרים", "שלושים", "ארבעים", "חמישים", "שישים", "שבעים", "שמונים", "תשעים"]
    hundreds = ["", "מאה", "מאתיים", "שלוש מאות", "ארבע מאות", "חמש מאות", "שש מאות", 
                "שבע מאות", "שמונה מאות", "תשע מאות"]
    
    def convert_hundreds(n, use_feminine=False):
        result = []
        names_to_use = ones_feminine if use_feminine else ones
        
        if n >= 100:
            result.append(hundreds[n // 100])
            n %= 100
        if n >= 20:
            result.append(tens[n // 10])
            if n % 10:
                result.append(names_to_use[n % 10])
        elif n >= 10:
            result.append(teens[n - 10])
        elif n > 0:
            result.append(names_to_use[n])
        return " ".join(result)
    
    if num < 1000:
        return convert_hundreds(num)
    
    parts = []
    
    # מיליארדים (לעתיד)
    if num >= 1000000000:
        billions = num // 1000000000
        if billions == 1:
            parts.append("מיליארד")
        elif billions == 2:
            parts.append("שני מיליארד")
        else:
            parts.append(f"{convert_hundreds(billions)} מיליארד")
        num %= 1000000000
    
    # מיליונים
    if num >= 1000000:
        millions = num // 1000000
        if millions == 1:
            parts.append("מיליון")
        elif millions == 2:
            parts.append("שני מיליון")
        else:
            parts.append(f"{convert_hundreds(millions)} מיליון")
        num %= 1000000
    
    # אלפים - עם טיפול מיוחד
    if num >= 1000:
        thousands = num // 1000
        if thousands == 1:
            parts.append("אלף")
        elif thousands == 2:
            parts.append("אלפיים")
        elif thousands >= 3 and thousands <= 10:
            # אלפים נקביים: שלושת אלפים, ארבעת אלפים וכו'
            parts.append(f"{ones_feminine[thousands]}ת אלפים")
        elif thousands == 11:
            parts.append("אחד עשר אלף")
        elif thousands >= 12 and thousands <= 19:
            parts.append(f"{teens[thousands - 10]} אלף")
        elif thousands >= 20:
            parts.append(f"{convert_hundreds(thousands)} אלף")
        num %= 1000
    
    # מאות
    if num > 0:
        parts.append(convert_hundreds(num))
    
    # תיקונים נוספים לקריאה טבעית
    result = " ".join(parts)
    
    # טיפול במקרים מיוחדים
    result = result.replace("אחדת אלפים", "שלושת אלפים")  # תיקון באג
    result = result.replace("שנייםת אלפים", "שני אלפים")   # תיקון באג
    
    return result

def format_amount_spoken(amount):
    """המר סכום למילים עבריות מדוברות"""
    try:
        num = int(float(str(amount).replace(',', '')))
        return f"{number_to_hebrew_words(num)} שקלים"
    except (ValueError, TypeError):
        return f"{amount} שקלים"

# =============================
# Env & Config
# =============================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("חסר OPENAI_API_KEY בקובץ .env")

DONEXT_API_URL = os.getenv("DONEXT_API_URL", "https://next.money-app.co.il/api/donext-api")
# קמפיין ברירת מחדל: אם מוגדר כאן – כל הכלים יעבדו עליו מבלי לבקש campaignId בכל קריאה
DEFAULT_CAMPAIGN_ID = os.getenv("CAMPAIGN_ID") or os.getenv("DEFAULT_CAMPAIGN_ID")
# שם קמפיין ברירת מחדל לפולבאק בלבד (אם אין מידע מ-searchByPhone)
FALLBACK_CAMPAIGN_NAME = os.getenv("CAMPAIGN_NAME", "הקמפיין")

# Debug logging for environment variables
print(f"🔍 DEBUG: CAMPAIGN_ID from env = '{os.getenv('CAMPAIGN_ID')}'")
print(f"🔍 DEBUG: DEFAULT_CAMPAIGN_ID from env = '{os.getenv('DEFAULT_CAMPAIGN_ID')}'") 
print(f"🔍 DEBUG: Final DEFAULT_CAMPAIGN_ID = '{DEFAULT_CAMPAIGN_ID}'")
print(f"🔍 DEBUG: Type of DEFAULT_CAMPAIGN_ID = {type(DEFAULT_CAMPAIGN_ID)}")

# =============================
# Logging
# =============================
LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

root_logger = logging.getLogger("realtime_campaign_phone")
root_logger.setLevel(logging.INFO)
if not root_logger.handlers:
    fh = logging.FileHandler(os.path.join(LOG_DIR, "app.log"), encoding="utf-8")
    fh.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(fmt)
    root_logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root_logger.addHandler(ch)

app = Flask(__name__)

def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)

# =============================
# Utils: phone parsing & role
# =============================

def convert_hebrew_acronyms(text: str) -> str:
    """המרת ראשי תיבות עבריים למילים מלאות לקריאה טובה יותר"""
    if not text:
        return text
    
    # מילון המרות לראשי תיבות עבריים נפוצים
    hebrew_acronyms = {
        'תשפ"ו': 'תף שין פיי ויו',
        'תשפ״ו': 'תף שין פיי ויו',
        'תשפה': 'תף שין פיי הא',
        'תשפ"ה': 'תף שין פיי הא',
        'תשפ״ה': 'תף שין פיי הא',
        'תשפד': 'תף שין פיי דלת',
        'תשפ"ד': 'תף שין פיי דלת',
        'תשפ״ד': 'תף שין פיי דלת',
        'תשפג': 'תף שין פיי גימל',
        'תשפ"ג': 'תף שין פיי גימל',
        'תשפ״ג': 'תף שין פיי גימל',
        'תשפב': 'תף שין פיי בית',
        'תשפ"ב': 'תף שין פיי בית',
        'תשפ״ב': 'תף שין פיי בית',
        'תשפא': 'תף שין פיי אלף',
        'תשפ"א': 'תף שין פיי אלף',
        'תשפ״א': 'תף שין פיי אלף',
        'תש"ף': 'תף שין פיי',
        'תש״ף': 'תף שין פיי',
        'ת"א': 'תל אביב',
        'ת״א': 'תל אביב',
        'י-ם': 'ירושלים',
        'ב"ה': 'בעזרת השם',
        'ב״ה': 'בעזרת השם'
    }
    
    # החלף את כל ראשי התיבות
    result = text
    for acronym, full_form in hebrew_acronyms.items():
        result = result.replace(acronym, full_form)
    
    return result

def normalize_il_phone(s: str) -> str:
    digits = re.sub(r"\D+", "", s or "")
    if digits.startswith("972") and len(digits) == 12:
        return "0" + digits[3:]
    if digits.startswith("05") and len(digits) == 10:
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return digits
    return digits


def extract_caller_from_sip_headers(sip_headers: list[dict]) -> str | None:
    candidates = []
    for h in (sip_headers or []):
        name = (h.get("name") or "").lower()
        val = h.get("value") or ""
        if any(k in name for k in ["p-asserted-identity", "from", "contact", "pai", "pai-number"]):
            candidates.append(val)
    text = " ".join(candidates)
    m = re.search(r"(\+?972[ \-]?\d[ \-]?\d{7,8}|0\d{8,9})", text)
    if not m:
        return None
    return normalize_il_phone(m.group(1))


def role_from_status_str(s: str) -> str:
    s = (s or "").strip().lower()
    if s in {"מתרים", "fundraiser", "raiser"}:
        return "fundraiser"
    if s in {"תורם", "donor"}:
        return "donor"
    if "תורם ומתרים" in s or "both" in s:
        return "both"
    return "unknown"

# =============================
# Donext API client helpers
# =============================

def api_ping() -> dict:
    r = requests.get(DONEXT_API_URL, params={"action": "ping"}, timeout=10)
    r.raise_for_status(); return r.json()


def api_search_by_phone(phone: str) -> dict:
    r = requests.get(DONEXT_API_URL, params={"action": "searchByPhone", "phone": phone}, timeout=12)
    r.raise_for_status(); return r.json()


def api_campaign_total(campaign_id: int) -> dict:
    r = requests.get(DONEXT_API_URL, params={"action": "campaignTotal", "campaignId": campaign_id}, timeout=12)
    r.raise_for_status(); return r.json()


def api_donor_total(donor_name: str, campaign_id: int) -> dict:
    r = requests.get(DONEXT_API_URL, params={"action": "donorTotal", "donorName": donor_name, "campaignId": campaign_id}, timeout=12)
    r.raise_for_status(); return r.json()


def api_fundraiser_stats(fundraiser_phone: str = None, fundraiser_name: str = None) -> dict:
    params = {"action": "fundraiserStats"}
    if fundraiser_phone:
        params["fundraiserPhone"] = fundraiser_phone
    if fundraiser_name:
        params["fundraiserName"] = fundraiser_name
    r = requests.get(DONEXT_API_URL, params=params, timeout=12)
    r.raise_for_status(); return r.json()


def api_fundraiser_donors(fundraiser_phone: str, campaign_id: int) -> dict:
    """שליפת רשימת התורמים המשוייכים למתרים בקמפיין ספציפי"""
    params = {
        "action": "fundraiserDonors",
        "campaignId": campaign_id,
        "fundraiserPhone": fundraiser_phone
    }
    r = requests.get(DONEXT_API_URL, params=params, timeout=12)
    r.raise_for_status(); return r.json()


def api_add_donation(payload: dict) -> dict:
    body = {"action": "addDonation", **payload}
    
    # לוג מפורט של הקריאה
    root_logger.info(f"🌐 API CALL - add_donation")
    root_logger.info(f"📍 URL: {DONEXT_API_URL}")
    root_logger.info(f"📦 Full Request Body: {json.dumps(body, ensure_ascii=False, indent=2)}")
    
    r = requests.post(DONEXT_API_URL, json=body, timeout=20)
    
    # לוג מפורט של התשובה
    root_logger.info(f"📥 Response Status: {r.status_code}")
    root_logger.info(f"📥 Response Headers: {dict(r.headers)}")
    root_logger.info(f"📥 Response Text: {r.text}")
    
    r.raise_for_status()
    return r.json()

# =============================
# Opening message builder (campaign name comes from phone search!)
# =============================

def choose_campaign_by_id(person: dict, preferred_campaign_id: int | None) -> dict | None:
    """Return the campaign object that matches preferred_campaign_id if exists; otherwise first campaign or None."""
    campaigns = (person or {}).get("campaigns") or []
    if preferred_campaign_id is not None:
        for c in campaigns:
            try:
                if int(c.get("campaignNumber")) == int(preferred_campaign_id):
                    return c
            except Exception:
                pass
        # אם לא נמצא קמפיין כזה, נחזיר None (לא את הראשון)
        return None
    return campaigns[0] if campaigns else None


def derive_identity(search_resp: dict, default_campaign_id: int | None) -> dict:
    """Return dict with keys: full_name, role, campaign_id, campaign_name, total_donation."""
    out = {
        "full_name": None,
        "role": "unknown",
        "campaign_id": default_campaign_id,
        "campaign_name": None,
        "total_donation": None,
    }
    if not (search_resp and search_resp.get("success") and isinstance(search_resp.get("data"), list) and search_resp["data"]):
        return out

    # חפש קמפיין תואם בכל ה-persons
    found = None
    for person in search_resp["data"]:
        for c in (person.get("campaigns") or []):
            try:
                if default_campaign_id is not None and int(c.get("campaignNumber")) == int(default_campaign_id):
                    found = (person, c)
                    break
            except Exception:
                pass
        if found:
            break

    if found:
        person, campaign_obj = found
        out["full_name"] = person.get("fullName")
        out["campaign_id"] = int(campaign_obj.get("campaignNumber"))
        out["campaign_name"] = campaign_obj.get("campaignName")
        out["total_donation"] = campaign_obj.get("totalDonation")
        out["role"] = role_from_status_str(campaign_obj.get("status"))
    else:
        # לא נמצא קמפיין תואם – fallback ל-person הראשון (אם יש)
        person = search_resp["data"][0]
        out["full_name"] = person.get("fullName")
        campaigns = person.get("campaigns") or []
        if campaigns:
            campaign_obj = campaigns[0]
            out["campaign_id"] = int(campaign_obj.get("campaignNumber"))
            out["campaign_name"] = campaign_obj.get("campaignName")
            out["total_donation"] = campaign_obj.get("totalDonation")
            out["role"] = role_from_status_str(campaign_obj.get("status"))
        else:
            out["campaign_name"] = FALLBACK_CAMPAIGN_NAME
            out["role"] = "unknown"

    return out


def build_welcome_text(campaign_name: str | None, full_name: str | None, total_donation: float | int | None) -> str:
    # שם הקמפיין בא תמיד מהחיפוש (אם נמצא); אחרת פולבאק מה-ENV
    cname = campaign_name or FALLBACK_CAMPAIGN_NAME
    # המר ראשי תיבות עבריים למילים מלאות לקריאה טובה יותר
    cname = convert_hebrew_acronyms(cname)
    
    name = full_name or "ידיד יקר"
    if total_donation and float(total_donation) > 0:
        donated_line = f"עד כה תרמת בקמפיין סך {int(float(total_donation))} שקלים."
    else:
        donated_line = "עדיין לא תרמת בקמפיין."
    return f"ברוכים הבאים לקמפיין {cname}. שלום {name}. {donated_line}"

# =============================
# Realtime helpers
# =============================
async def rt_send(ws, obj):
    await ws.send(json.dumps(obj, ensure_ascii=False))

async def say_only(ws, text: str, f_call_id: str | None = None):
    # הוסף לוגינג כדי לעקוב אחרי מה נשלח
    root_logger.info(f"💬 SAY_ONLY called with text: {text[:100]}..." if len(text) > 100 else f"💬 SAY_ONLY called with text: {text}")
    root_logger.info(f"🆔 Function call ID: {f_call_id}")
    
    payload = {
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    }
    if f_call_id is not None:
        payload = {
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": f_call_id, "output": text}
        }
        root_logger.info(f"📤 Sending function call output for ID: {f_call_id}")
    else:
        root_logger.info("📤 Sending regular message")
    
    root_logger.info(f"📦 Payload: {json.dumps(payload, ensure_ascii=False)}")
    await rt_send(ws, payload)
    await rt_send(ws, {"type": "response.create"})
    root_logger.info("✅ Message sent and response.create triggered")

async def end_call(ws, call_id: str = None):
    """סיום השיחה בפועל - שליחת הודעת סיום ועצירת ה-WebSocket"""
    try:
        # שלח הודעת סיום
        await rt_send(ws, {
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "כל טוב ולהתראות"}]}
        })
        await rt_send(ws, {"type": "response.create"})
        
        # שלח אירועי סיום מפורשים
        await rt_send(ws, {"type": "session.update", "session": {"turn_detection": None}})
        await rt_send(ws, {"type": "response.cancel"})
        
        # חכה קצת שההודעה תישלח
        await asyncio.sleep(2)
        
        # אם יש call_id, נסה לנתק דרך OpenAI API
        if call_id:
            try:
                import requests
                hangup_url = f"https://api.openai.com/v1/realtime/calls/{call_id}/hangup"
                headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
                requests.post(hangup_url, headers=headers, timeout=5)
            except Exception as e:
                root_logger.warning(f"Failed to hangup via OpenAI API: {e}")
        
        # סגור את ה-WebSocket בכוח
        await ws.close(code=1000, reason="Call ended by user")
        
    except Exception as e:
        # אם יש שגיאה, נסה לסגור בכוח
        try:
            await ws.close(code=1000, reason="Force close")
        except:
            pass

# =============================
# Tool handling (Donext-mapped)
# =============================
async def handle_tool(ws, call_logger, name: str, args: dict, f_call_id: str, ctx: dict):
    role = ctx.get("role") or "unknown"
    
    # לוג תחילת ביצוע הפונקציה
    call_logger.info(f"🔧 EXECUTING FUNCTION: {name}")
    call_logger.info(f"📋 FUNCTION ARGS: {json.dumps(args, ensure_ascii=False)}")
    call_logger.info(f"👤 USER ROLE: {role}")
    call_logger.info(f"🆔 Function call ID: {f_call_id}")
    root_logger.info(f"🔧 EXECUTING FUNCTION: {name} | ARGS: {json.dumps(args, ensure_ascii=False)} | ROLE: {role} | CALL_ID: {f_call_id}")

    def echo_json(resp: dict):
        return json.dumps(resp, ensure_ascii=False)

    try:
        if name == "campaign_total":
            call_logger.info("📊 Starting campaign_total function")
            cid = args.get("campaignId") or ctx.get("campaignId") or DEFAULT_CAMPAIGN_ID
            call_logger.info(f"🆔 Campaign ID from args: {args.get('campaignId')}")
            call_logger.info(f"🆔 Campaign ID from ctx: {ctx.get('campaignId')}")
            call_logger.info(f"🆔 DEFAULT_CAMPAIGN_ID: {DEFAULT_CAMPAIGN_ID}")
            call_logger.info(f"🆔 Final campaign ID: {cid}")
            
            if not cid:
                call_logger.warning("❌ No campaign ID available")
                return await say_only(ws, json.dumps({"error": "אין לי גישה לנתוני הקמפיין כרגע. נסה שוב מאוחר יותר."}, ensure_ascii=False), f_call_id)
            
            call_logger.info(f"🌐 Making API call to campaign_total with ID: {cid}")
            resp = api_campaign_total(int(cid))
            call_logger.info(f"📥 API Response: {json.dumps(resp, ensure_ascii=False)}")
            
            # החזר את התשובה הגולמית המלאה ל-AI לעיבוד טבעי
            if resp.get("success") and resp.get("data"):
                data = resp["data"]
                total = data.get("totalDonations", 0)
                donors = data.get("activeDonorsCount", 0)
                target = data.get("targetAmount", "")
                campaign_id = data.get("campaignId", cid)
                
                call_logger.info(f"📊 Raw data from API - totalDonations: {total}, activeDonorsCount: {donors}, targetAmount: {target}, campaignId: {campaign_id}")
                
                # החזר את כל הנתונים הגולמיים ל-AI
                result_data = {
                    "success": True,
                    "campaignId": campaign_id,
                    "totalDonations": total,
                    "totalDonationsFormatted": f"{total:,} שקלים",
                    "totalDonationsSpoken": format_amount_spoken(total),
                    "activeDonorsCount": donors,
                    "targetAmount": target,
                    "targetAmountFormatted": f"{int(target):,} שקלים" if target and str(target).isdigit() else target,
                    "targetAmountSpoken": format_amount_spoken(target) if target and str(target).isdigit() else target,
                    "progressPercentage": round((total / int(target)) * 100, 1) if target and str(target).isdigit() and int(target) > 0 else None,
                    "amountRemaining": int(target) - total if target and str(target).isdigit() else None,
                    "amountRemainingFormatted": f"{int(target) - total:,} שקלים" if target and str(target).isdigit() else None,
                    "amountRemainingSpoken": format_amount_spoken(int(target) - total) if target and str(target).isdigit() else None,
                    "message": f"נתוני קמפיין {campaign_id}: נאספו {format_amount_spoken(total)} מ-{donors} תורמים" + (f" מתוך יעד של {format_amount_spoken(target)}" if target and str(target).isdigit() else "")
                }
                call_logger.info(f"✅ Campaign total success - Amount: {total:,}, Donors: {donors}, Target: {target}")
            else:
                result_data = {
                    "success": False,
                    "error": "לא הצלחתי לקבל את נתוני הקמפיין כרגע"
                }
                call_logger.warning("❌ Campaign total failed - no success or data")
            
            call_logger.info(f"💬 Returning data to AI: {json.dumps(result_data, ensure_ascii=False)}")
            call_logger.info("🚀 About to return data to AI for natural response")
            result = await say_only(ws, json.dumps(result_data, ensure_ascii=False), f_call_id)
            call_logger.info("✅ Data returned to AI for processing")
            return result

        if name == "donor_total":
            call_logger.info("👤 Starting donor_total function")
            # אם לא נתן שם, נשתמש בשם מהקונטקסט
            donor_name = args.get("donorName") or ctx.get("full_name")
            cid = args.get("campaignId") or ctx.get("campaignId") or DEFAULT_CAMPAIGN_ID
            call_logger.info(f"🏷️ Donor name from args: {args.get('donorName')}")
            call_logger.info(f"🏷️ Donor name from context: {ctx.get('full_name')}")
            call_logger.info(f"🏷️ Final donor name: {donor_name}, Campaign ID: {cid}")
            
            if not donor_name:
                call_logger.warning("❌ No donor name available from args or context")
                return await say_only(ws, json.dumps({"error": "אני צריך את השם המלא שלך כדי לבדוק את נתוני התרומה."}, ensure_ascii=False), f_call_id)
            if not cid:
                call_logger.warning("❌ No campaign ID available")
                return await say_only(ws, json.dumps({"error": "מצטער, אין לי גישה לנתוני הקמפיין כרגע."}, ensure_ascii=False), f_call_id)
            
            call_logger.info(f"🌐 Making API call to donor_total with name: {donor_name}, campaign: {cid}")
            resp = api_donor_total(donor_name, int(cid))
            call_logger.info(f"📥 API Response: {json.dumps(resp, ensure_ascii=False)}")
            
            # החזר את התשובה המלאה ל-AI לעיבוד טבעי
            if resp.get("success") and resp.get("data"):
                total = resp["data"].get("totalDonation", 0)
                result_data = {
                    "success": True,
                    "donorName": donor_name,
                    "totalDonation": int(total) if total > 0 else 0,
                    "totalDonationFormatted": f"{int(total)} שקלים" if total > 0 else "0 שקלים",
                    "totalDonationSpoken": format_amount_spoken(total) if total > 0 else "אפס שקלים",
                    "hasDonations": total > 0,
                    "message": f"נתוני התרומה של {donor_name}: {format_amount_spoken(total)}" if total > 0 else f"{donor_name} עדיין לא תרם"
                }
                call_logger.info(f"✅ Donor total success - {donor_name}: {total} שקלים")
            else:
                result_data = {
                    "success": False,
                    "donorName": donor_name,
                    "error": "לא מצאתי נתונים על השם הזה במערכת"
                }
                call_logger.warning(f"❌ Donor total failed for {donor_name}")
            
            call_logger.info(f"💬 Returning data to AI: {json.dumps(result_data, ensure_ascii=False)}")
            return await say_only(ws, json.dumps(result_data, ensure_ascii=False), f_call_id)

        if name == "fundraiser_stats":
            call_logger.info("📈 Starting fundraiser_stats function")
            
            # בדיקת הרשאות - רק מתרים יכולים לראות נתוני מתרים
            if role not in {"fundraiser", "both"}:
                call_logger.warning(f"❌ Access denied - role '{role}' cannot access fundraiser stats")
                return await say_only(ws, json.dumps({"error": "מצטער, נתוני מתרים זמינים רק למתרים במערכת. אוכל לבדוק עבורך את נתוני התרומות שלך כתורם."}, ensure_ascii=False), f_call_id)
            
            # תמיד נשתמש במספר הטלפון של המתקשר מהקונטקסט
            fp = ctx.get("caller_phone")
            call_logger.info(f"📞 Using caller phone from context: {fp}")
            
            if not fp:
                call_logger.warning("❌ No caller phone available in context")
                return await say_only(ws, json.dumps({"error": "מצטער, לא מצאתי את מספר הטלפון שלך במערכת."}, ensure_ascii=False), f_call_id)
            
            call_logger.info(f"🌐 Making API call to fundraiser_stats with phone: {fp}")
            resp = api_fundraiser_stats(fundraiser_phone=fp, fundraiser_name=None)
            call_logger.info(f"📥 API Response: {json.dumps(resp, ensure_ascii=False)}")
            
            # החזר את התשובה המלאה ל-AI לעיבוד טבעי
            if resp.get("success") and resp.get("data"):
                data = resp["data"]
                
                # נבדק אם יש foundFundraisers או שהתשובה במבנה הישן
                if "foundFundraisers" in data and data["foundFundraisers"]:
                    # מבנה חדש
                    fundraiser = data["foundFundraisers"][0]  # נקח את המתרים הראשון
                    fundraiser_name = fundraiser.get("fundraiserName", "")
                    total_raised = fundraiser.get("totalDonationsAmount", 0)
                    donors_with_donations = fundraiser.get("donorsWithDonations", 0)
                    total_donors = fundraiser.get("totalDonors", 0)
                    total_expected = fundraiser.get("totalExpected", 0)
                    campaign_id = fundraiser.get("campaignId", "")
                    
                    call_logger.info(f"📊 New format - Name: {fundraiser_name}, Raised: {total_raised}, Active donors: {donors_with_donations}, Total donors: {total_donors}, Expected: {total_expected}")
                    
                    result_data = {
                        "success": True,
                        "fundraiserName": fundraiser_name,
                        "campaignId": campaign_id,
                        "totalRaised": total_raised,
                        "totalRaisedFormatted": f"{total_raised:,} שקלים",
                        "totalRaisedSpoken": format_amount_spoken(total_raised),
                        "donorsWithDonations": donors_with_donations,
                        "totalDonors": total_donors,
                        "totalExpected": total_expected,
                        "totalExpectedFormatted": f"{total_expected:,} שקלים" if total_expected > 0 else "ללא יעד אישי",
                        "totalExpectedSpoken": format_amount_spoken(total_expected) if total_expected > 0 else "ללא יעד אישי",
                        "hasPersonalTarget": total_expected > 0,
                        "progressPercentage": round((total_raised / total_expected) * 100, 1) if total_expected > 0 else None,
                        "message": f"נתוני המתרים {fundraiser_name}: גייסת {format_amount_spoken(total_raised)} מ-{donors_with_donations} תורמים פעילים מתוך {total_donors} תורמים סך הכל" + (f", יעד אישי: {format_amount_spoken(total_expected)}" if total_expected > 0 else "")
                    }
                else:
                    # מבנה ישן - תאימות לאחור
                    total_raised = data.get("totalRaised", 0)
                    donors_count = data.get("donorsCount", 0)
                    result_data = {
                        "success": True,
                        "totalRaised": total_raised,
                        "totalRaisedFormatted": f"{total_raised:,} שקלים",
                        "totalRaisedSpoken": format_amount_spoken(total_raised),
                        "donorsCount": donors_count,
                        "message": f"נתוני המתרים: גייסת {format_amount_spoken(total_raised)} מ-{donors_count} תורמים"
                    }
                    
                call_logger.info(f"✅ Fundraiser stats success - data processed and formatted")
            else:
                result_data = {
                    "success": False,
                    "error": "לא מצאתי נתוני מתרים עבורך במערכת"
                }
                call_logger.warning("❌ Fundraiser stats failed - no data found")
            
            call_logger.info(f"💬 Returning data to AI: {json.dumps(result_data, ensure_ascii=False)}")
            return await say_only(ws, json.dumps(result_data, ensure_ascii=False), f_call_id)

        if name == "fundraiser_donors":
            call_logger.info("👥 Starting fundraiser_donors function")
            
            # בדיקת הרשאות - רק מתרים יכולים לראות רשימת תורמים
            if role not in {"fundraiser", "both"}:
                call_logger.warning(f"❌ Access denied - role '{role}' cannot access fundraiser donors")
                return await say_only(ws, json.dumps({"error": "מצטער, רשימת התורמים זמינה רק למתרים במערכת."}, ensure_ascii=False), f_call_id)
            
            # השתמש במספר הטלפון של המתקשר ובקמפיין הנוכחי
            fp = ctx.get("caller_phone")
            cid = ctx.get("campaignId") or DEFAULT_CAMPAIGN_ID
            call_logger.info(f"📞 Using caller phone: {fp}, campaign: {cid}")
            
            if not fp:
                call_logger.warning("❌ No caller phone available in context")
                return await say_only(ws, json.dumps({"error": "מצטער, לא מצאתי את מספר הטלפון שלך במערכת."}, ensure_ascii=False), f_call_id)
            if not cid:
                call_logger.warning("❌ No campaign ID available")
                return await say_only(ws, json.dumps({"error": "מצטער, אין לי גישה לנתוני הקמפיין כרגע."}, ensure_ascii=False), f_call_id)
            
            call_logger.info(f"🌐 Making API call to fundraiser_donors with phone: {fp}, campaign: {cid}")
            resp = api_fundraiser_donors(fp, int(cid))
            call_logger.info(f"📥 API Response: {json.dumps(resp, ensure_ascii=False)}")
            
            # החזר את התשובה המלאה ל-AI לעיבוד טבעי
            if resp.get("success") and resp.get("data"):
                data = resp["data"]
                donors = data.get("donors", [])
                total_donors = data.get("totalDonors", 0)
                fundraiser_name = data.get("fundraiserName", "")
                
                # בנה רשימה קריאה של התורמים
                donors_list = []
                for i, donor in enumerate(donors, 1):
                    donors_list.append({
                        "index": i,
                        "donorId": donor.get("donorId"),
                        "fullName": donor.get("fullName"),
                        "phone": donor.get("phone"),
                        "city": donor.get("city", "")
                    })
                
                result_data = {
                    "success": True,
                    "fundraiserName": fundraiser_name,
                    "totalDonors": total_donors,
                    "donors": donors_list,
                    "message": f"רשימת התורמים של {fundraiser_name}: {total_donors} תורמים"
                }
                call_logger.info(f"✅ Fundraiser donors success - {total_donors} donors found")
            else:
                result_data = {
                    "success": False,
                    "error": "לא מצאתי תורמים עבורך במערכת"
                }
                call_logger.warning("❌ Fundraiser donors failed - no data found")
            
            call_logger.info(f"💬 Returning data to AI: {json.dumps(result_data, ensure_ascii=False)}")
            return await say_only(ws, json.dumps(result_data, ensure_ascii=False), f_call_id)

        if name == "add_donation":
            call_logger.info("💰 Starting add_donation function")
            if role not in {"fundraiser", "both"}:
                call_logger.warning(f"❌ Access denied - role '{role}' cannot add donations")
                return await say_only(ws, json.dumps({"error": "מצטער, רק מתרים יכולים להוסיף תרומות במערכת."}, ensure_ascii=False), f_call_id)
            
            # בדיקת פרמטרים חסרים
            amount = args.get("amount")
            donor_name = args.get("donorName")
            call_logger.info(f"💵 Donation amount: {amount}, donor: {donor_name}")
            
            if not amount:
                call_logger.warning("❌ Missing donation amount")
                return await say_only(ws, json.dumps({"error": "אני צריך לדעת את סכום התרומה כדי להמשיך."}, ensure_ascii=False), f_call_id)
            if not donor_name:
                call_logger.warning("❌ Missing donor name")
                return await say_only(ws, json.dumps({"error": "אני צריך את שם התורם המלא כדי לרשום את התרומה."}, ensure_ascii=False), f_call_id)
            
            payload = {
                "campaignId": args.get("campaignId") or ctx.get("campaignId") or DEFAULT_CAMPAIGN_ID,
                "amount": amount,
                "donorName": donor_name,
                "fundraiserPhone": args.get("fundraiserPhone") or ctx.get("caller_phone"),
                "numberOfPayments": args.get("numberOfPayments", 1),
                "isUnlimited": args.get("isUnlimited", False),
                "hasPaymentMethod": args.get("hasPaymentMethod", True),
            }
            call_logger.info(f"📦 Donation payload: {json.dumps(payload, ensure_ascii=False)}")
            call_logger.info(f"🔍 DETAILED PAYLOAD BREAKDOWN:")
            call_logger.info(f"   📋 campaignId: {payload.get('campaignId')} (type: {type(payload.get('campaignId'))})")
            call_logger.info(f"   💰 amount: {payload.get('amount')} (type: {type(payload.get('amount'))})")
            call_logger.info(f"   👤 donorName: {payload.get('donorName')}")
            call_logger.info(f"   📞 fundraiserPhone: {payload.get('fundraiserPhone')}")
            call_logger.info(f"   💳 numberOfPayments: {payload.get('numberOfPayments')}")
            call_logger.info(f"   ♾️ isUnlimited: {payload.get('isUnlimited')}")
            call_logger.info(f"   💳 hasPaymentMethod: {payload.get('hasPaymentMethod')}")
            
            call_logger.info("🌐 Making API call to add_donation")
            resp = api_add_donation(payload)
            call_logger.info(f"📥 API Response: {json.dumps(resp, ensure_ascii=False)}")
            
            # בדיקה נוספת - האם התרומה אכן נוספה?
            if resp.get("success"):
                call_logger.info("✅ API returned success - verifying donation was actually added...")
                # נבדק אם יש donationId בתשובה
                donation_id = resp.get("data", {}).get("donationId") if resp.get("data") else None
                if donation_id:
                    call_logger.info(f"✅ Donation ID received: {donation_id}")
                else:
                    call_logger.warning("⚠️ No donation ID in response - donation might not have been saved")
            
            # החזר את התשובה המלאה ל-AI לעיבוד טבעי
            if resp.get("success"):
                result_data = {
                    "success": True,
                    "donorName": donor_name,
                    "amount": int(amount),
                    "amountFormatted": f"{int(amount)} שקלים",
                    "amountSpoken": format_amount_spoken(amount),
                    "message": f"התרומה נרשמה בהצלחה: {format_amount_spoken(amount)} על שם {donor_name}"
                }
                call_logger.info(f"✅ Donation added successfully - {amount} שקלים for {donor_name}")
            else:
                error_msg = resp.get("error", {}).get("message", "שגיאה לא ידועה")
                result_data = {
                    "success": False,
                    "donorName": donor_name,
                    "amount": int(amount),
                    "error": f"לא הצלחתי לרשום את התרומה. {error_msg}"
                }
                call_logger.error(f"❌ Donation failed - error: {error_msg}")
            
            call_logger.info(f"💬 Returning data to AI: {json.dumps(result_data, ensure_ascii=False)}")
            return await say_only(ws, json.dumps(result_data, ensure_ascii=False), f_call_id)

        if name == "end_call":
            call_logger.info("🔚 Starting end_call function - terminating call")
            root_logger.info("🔚 Call termination requested by user")
            
            # שלח הודעת סיום
            call_logger.info("💬 Sending goodbye message")
            await rt_send(ws, {
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "כל טוב ולהתראות"}]}
            })
            await rt_send(ws, {"type": "response.create"})
            
            # נסה לנתק גם דרך Twilio אם יש call_id
            call_id = ctx.get("call_id")
            if call_id:
                call_logger.info(f"📞 Attempting Twilio hangup for call: {call_id}")
                twilio_result = twilio_hangup_call(call_id)
                call_logger.info(f"📞 Twilio hangup result: {twilio_result}")
            else:
                call_logger.warning("⚠️ No call_id available for Twilio hangup")
            
            # סגור WebSocket
            call_logger.info("🌐 Closing WebSocket connection")
            await end_call(ws, call_id)
            
            # הרם Exception כדי לסיים את הלולאה
            call_logger.info("🛑 Raising ConnectionClosed exception to end call loop")
            raise ConnectionClosed("Call ended by user")
            return

        call_logger.warning(f"❓ Unknown function requested: {name}")
        return await say_only(ws, "מצטער, לא זיהיתי את הבקשה. אתה יכול לבקש ממני לבדוק נתוני קמפיין, נתוני תורם או להוסיף תרומה.", f_call_id)

    except requests.HTTPError as e:
        call_logger.error(f"🌐 HTTP Error in function {name}: {e}")
        return await say_only(ws, "יש לי בעיה זמנית בחיבור למערכת. נסה שוב בעוד רגע.", f_call_id)
    except Exception as e:
        call_logger.error(f"💥 Unexpected error in function {name}: {e}")
        return await say_only(ws, "משהו לא עבד כמו שצריך. אתה יכול לנסות שוב?", f_call_id)

# =============================
# Realtime loop
# =============================
async def connect_to_call(call_id: str, call_logger: logging.Logger, ctx: dict, welcome_text: str, options_text: str):
    ws_url = f"wss://api.openai.com/v1/realtime?call_id={call_id}"
    headers = [("Authorization", f"Bearer {OPENAI_API_KEY}"), ("Origin", "https://api.openai.com")]

    pending_calls: dict[str, dict] = {}
    call_ended = False

    try:
        async with websockets.connect(ws_url, extra_headers=headers, ping_interval=5, ping_timeout=5, max_size=8 * 1024 * 1024, close_timeout=1) as ws:
            call_logger.info("WS connected")

            # טריגר ליצירת תגובה לפי ההנחיות שב-ACCEPT (הן כוללות את הפתיח והאפשרויות)
            await rt_send(ws, {"type": "response.create"})

            async for raw in ws:
                if call_ended:
                    break
                    
                try:
                    event = json.loads(raw)
                except Exception:
                    continue
                t = event.get("type")

                if t == "response.function_call_arguments.delta":
                    f_call_id = event.get("call_id")
                    name = event.get("name")
                    delta = event.get("delta", "")
                    if not f_call_id:
                        continue
                    st = pending_calls.setdefault(f_call_id, {"name": name, "args_str": ""})
                    if name:
                        st["name"] = name
                        call_logger.info(f"🔄 Function call started: {name} (ID: {f_call_id})")
                    st["args_str"] += delta

                if t == "response.function_call_arguments.done":
                    f_call_id = event.get("call_id")
                    if not f_call_id or f_call_id not in pending_calls:
                        continue
                    name = event.get("name") or pending_calls[f_call_id].get("name") or ""
                    args_str = pending_calls[f_call_id]["args_str"] or ""
                    try:
                        args = json.loads(args_str) if args_str else {}
                    except Exception:
                        args = {}
                    
                    call_logger.info(f"🎯 Function call completed: {name}")
                    call_logger.info(f"📋 Final arguments: {json.dumps(args, ensure_ascii=False)}")
                    root_logger.info(f"🎯 FUNCTION CALL: {name} | ARGS: {json.dumps(args, ensure_ascii=False)}")
                    
                    call_logger.info(f"FUNCTION {name} args={jdump(args)}")
                    await handle_tool(ws, call_logger, name, args, f_call_id, ctx)
                    pending_calls.pop(f_call_id, None)
                    
                    # בדוק אם זה היה end_call
                    if name == "end_call":
                        call_logger.info("🔚 End call function executed - setting call_ended flag")
                        call_ended = True
                        break

                if t in {"realtime.call.ended", "response.error"}:
                    break
    except Exception as e:
        root_logger.error(f"WS error for {call_id}: {e}")
        call_logger.error(f"WS ERROR: {e}")

# =============================
# Webhook
# =============================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True) or {}
    root_logger.info("📞 Incoming call event:\n" + jdump(data))

    ev_type = data.get("type")
    root_logger.info(f"📋 Event type: {ev_type}")
    
    if ev_type == "realtime.call.ended":
        root_logger.info("🔚 Call ended event received")
        return jsonify({"status": "ended"}), 200
    if ev_type != "realtime.call.incoming":
        root_logger.info(f"⏭️ Ignoring event type: {ev_type}")
        return jsonify({"status": "ignored"}), 200

    d = data.get("data") or {}
    call_id = d.get("call_id") or d.get("id")
    sip_headers = d.get("sip_headers") or []
    root_logger.info(f"🆔 Processing call ID: {call_id}")
    
    if not call_id:
        root_logger.error("❌ Missing call_id in webhook data")
        return jsonify({"error": "missing call_id"}), 400

    call_logger = logging.getLogger(f"call_{call_id}")
    if not call_logger.handlers:
        fh = logging.FileHandler(os.path.join(LOG_DIR, f"{datetime.now():%Y%m%d_%H%M%S}_{call_id}.log"), encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        call_logger.addHandler(fh)
        call_logger.setLevel(logging.INFO)

    # Ping health (optional)
    call_logger.info("🏥 Checking API health")
    try:
        api_ping()
        call_logger.info("✅ API health check passed")
    except Exception as e:
        call_logger.error(f"❌ API health check failed: {e}")
        return jsonify({"error": "api_unavailable"}), 503

    # Lookup caller → pick campaign name & id per ENV campaign id
    caller_phone = extract_caller_from_sip_headers(sip_headers) or ""
    call_logger.info(f"📞 Extracted caller phone: {caller_phone}")
    root_logger.info(f"📞 CALLER PHONE: {caller_phone}")

    search_resp = {}
    identity = {"full_name": None, "role": "unknown", "campaign_id": (int(DEFAULT_CAMPAIGN_ID) if DEFAULT_CAMPAIGN_ID else None), "campaign_name": None, "total_donation": None}

    try:
        if caller_phone:
            call_logger.info(f"🔍 Searching caller in database: {caller_phone}")
            search_resp = api_search_by_phone(caller_phone)
            call_logger.info(f"📥 Search response: {json.dumps(search_resp, ensure_ascii=False)}")
            identity = derive_identity(search_resp, int(DEFAULT_CAMPAIGN_ID) if DEFAULT_CAMPAIGN_ID else None)
            call_logger.info(f"👤 Derived identity: {json.dumps(identity, ensure_ascii=False)}")
        else:
            call_logger.warning("⚠️ No caller phone number found in SIP headers")
    except Exception as e:
        call_logger.error(f"❌ Search by phone failed: {e}")
        root_logger.warning(f"searchByPhone failed: {e}")

    welcome_text = build_welcome_text(identity.get("campaign_name"), identity.get("full_name"), identity.get("total_donation"))
    call_logger.info(f"👋 Welcome text: {welcome_text}")

    role = identity.get("role") or "unknown"
    final_campaign_id = identity.get("campaign_id")
    call_logger.info(f"👤 Final user role: {role}")
    call_logger.info(f"🏷️ Final campaign ID: {final_campaign_id}")
    root_logger.info(f"👤 USER ROLE: {role} | CAMPAIGN: {final_campaign_id}")

    # Options by role
    if role in {"fundraiser", "both"}:
        options_text = "איך אני יכול לעזור לך היום? אני יכול לעדכן אותך על נתוני הקמפיין, להציג את רשימת התורמים שלך, לרשום תרומה חדשה, להציג את הנתונים האישיים שלך כמתרים, או כל דבר אחר שתצטרך."
        call_logger.info("🔧 User has fundraiser permissions - full options available")
    else:
        options_text = "איך אני יכול לעזור לך היום? אני יכול לעדכן אותך על נתוני הקמפיין, לבדוק את התרומות שלך, או כל דבר אחר שתצטרך."
        call_logger.info("📊 User has donor permissions - limited options available")
    
    call_logger.info(f"💬 Options text: {options_text}")

    # ===== Accept with instruction that SAYS the welcome/options verbatim =====
    accept_url = f"https://api.openai.com/v1/realtime/calls/{call_id}/accept"
    call_logger.info(f"🤝 Accepting call with OpenAI API: {accept_url}")

    tools = [
        {
            "type": "function",
            "name": "campaign_total",
            "description": f"שליפת סך התרומות בקמפיין. חובה להעביר campaignId: {final_campaign_id}",
            "parameters": {"type": "object", "properties": {"campaignId": {"type": "integer", "description": f"מספר הקמפיין, תמיד השתמש בערך: {final_campaign_id}"}}, "required": ["campaignId"]}
        },
        {
            "type": "function",
            "name": "donor_total",
            "description": f"סך תרומה אישי של תורם בקמפיין. חובה להעביר campaignId: {final_campaign_id}. השם נלקח אוטומטית אם המשתמש זוהה במערכת",
            "parameters": {
                "type": "object",
                "properties": {"donorName": {"type": "string", "description": "שם התורם - אופציונלי אם המשתמש כבר זוהה במערכת"}, "campaignId": {"type": "integer", "description": f"מספר הקמפיין, תמיד השתמש בערך: {final_campaign_id}"}},
                "required": ["campaignId"]
            }
        },
        {
            "type": "function",
            "name": "end_call",
            "description": "סיום השיחה בפועל - ניתוק השיחה",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    ]

    # הוסף כלים לפי תפקיד המשתמש
    if role in {"fundraiser", "both"}:
        tools.extend([
            {
                "type": "function",
                "name": "fundraiser_stats",
                "description": "הצגת נתוני מתרים אישיים (זמין רק למתרים, מספר הטלפון נלקח אוטומטית מהשיחה)",
                "parameters": {"type": "object", "properties": {}, "required": []}
            },
            {
                "type": "function",
                "name": "fundraiser_donors",
                "description": "הצגת רשימת התורמים של המתרים (זמין רק למתרים, מספר הטלפון נלקח אוטומטית מהשיחה)",
                "parameters": {"type": "object", "properties": {}, "required": []}
            },
            {
                "type": "function",
                "name": "add_donation",
                "description": f"הוספת תרומה לקמפיין. חובה להעביר campaignId: {final_campaign_id}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "campaignId": {"type": "integer", "description": f"מספר הקמפיין, תמיד השתמש בערך: {final_campaign_id}"},
                        "amount": {"type": "number", "description": "סכום התרומה"},
                        "donorName": {"type": "string", "description": "שם התורם המלא"},
                        "fundraiserPhone": {"type": "string", "description": "מספר טלפון של המתרים"},
                        "numberOfPayments": {"type": "integer", "description": "מספר תשלומים"},
                        "isUnlimited": {"type": "boolean", "description": "האם תרומה בלתי מוגבלת"},
                        "hasPaymentMethod": {"type": "boolean", "description": "האם יש אמצעי תשלום"}
                    },
                    "required": ["amount", "donorName"]
                }
            }
        ])

    composed_instructions = (
        "אתה עוזר אינטליגנטי וידידותי לקמפיין גיוס תרומות. התנהג בצורה טבעית ושיחתית. "
        f"פתח את השיחה באמירת המשפט הבא במדויק: '{welcome_text}'. "
        "מיד לאחר מכן שאל פשוט: 'מה תרצה לעשות?' ותן למשתמש לענות. "
        "אל תקריא את האופציות מראש! תן למשתמש לבטא את רצונו בחופשיות. "
        "\n"
        f"חשוב מאוד: אתה עובד עם קמפיין מספר {final_campaign_id}. בכל קריאה לפונקציה שדורשת campaignId, תמיד השתמש בערך {final_campaign_id}.\n"
        f"תפקיד המשתמש: {role}. זכור את התפקיד הזה לאורך כל השיחה!\n"
        "\n"
        "מתי לקרוא את האופציות:\n"
        f"קרא את האופציות הבאות רק במקרים הבאים: '{options_text}'\n"
        "- אם המשתמש לא הבין או ביקש לדעת מה הוא יכול לעשות\n"
        "- אם המשתמש ביקש משהו שהוא לא מורשה לבצע (בעיית הרשאות)\n"
        "- אם המשתמש ביקש משהו שלא קיים במערכת\n"
        "- אם המשתמש אמר 'לא יודע' או 'תגיד לי מה אפשר'\n"
        "בכל מקרה אחר - נסה להבין את הבקשה ולהגיב ישירות!\n"
        "\n"
        "כלל זהב - עצור אחרי שענית על הבקשה!\n"
        "- המשתמש ביקש נתוני מתרים? תן רק נתוני מתרים ועצור!\n"
        "- המשתמש ביקש נתוני קמפיין? תן רק נתוני קמפיין ועצור!\n"
        "- אל תקרא פונקציות נוספות שהמשתמש לא ביקש!\n"
        "- אל תציע אוטומטית דברים שהמשתמש לא שאל עליהם!\n"
        "\n"
        "הבחנה חשובה בין תפקידים:\n"
        "- תורם (donor): יכול לבדוק רק את התרומות שלו עצמו ונתוני הקמפיין הכלליים\n"
        "- מתרים (fundraiser): יכול לבדוק נתוני הקמפיין, להוסיף תרומות, ולראות נתוני מתרים\n"
        "- שניהם (both): יכול לעשות הכל\n"
        "\n"
        "עיבוד תשובות פונקציות:\n"
        "כל פונקציה מחזירה נתונים במבנה JSON. עליך לעבד את הנתונים ולתת תשובה טבעית ואישית:\n"
        "- אם success=true, השתמש בנתונים לבניית תשובה חיובית ומעודדת\n"
        "- אם success=false או יש שדה error, תן תשובה מבינה ועוזרת\n"
        "- השתמש בשמות אישיים ובנתונים הספציפיים שקיבלת\n"
        "- הוסף רגש ועידוד לתשובות\n"
        "- כל סכום כסף תאמר 'שקלים' ולעולם לא 'ש\"ח'\n"
        "\n"
        "הצגת סכומים בשפה מדוברת:\n"
        "חשוב מאוד! כשאתה מציג סכומים, השתמש בשדות הטקסט המדובר:\n"
        "- במקום 'totalDonationsFormatted' השתמש ב-'totalDonationsSpoken'\n"
        "- במקום 'amountFormatted' השתמש ב-'amountSpoken'\n"
        "- במקום 'totalRaisedFormatted' השתמש ב-'totalRaisedSpoken'\n"
        "- שדות ה-'Spoken' מכילים טקסט עברי מדובר (כמו 'עשרים אלף וחמישים ואחד שקלים')\n"
        "- שדות ה-'Formatted' מכילים מספרים עם פסיקים (כמו '20,051 שקלים')\n"
        "- תמיד העדף את שדות ה-'Spoken' לקריאה בקול!\n"
        "\n"
        "הצגת נתוני קמפיין (campaign_total):\n"
        "כשאתה מקבל נתוני קמפיין, הצג את כל המידע החשוב:\n"
        "- הסכום שנאסף עד כה (totalDonations)\n"
        "- מספר התורמים (activeDonorsCount)\n"
        "- היעד של הקמפיין (targetAmount) - תמיד הזכר את היעד!\n"
        "- אחוז ההתקדמות אם יש יעד (progressPercentage)\n"
        "- כמה חסר ליעד (amountRemaining)\n"
        "- תן הערכה חיובית והתייחס לקדימה לעבר היעד\n"
        "דוגמה: 'נאספו עד כה עשרים אלף וחמישים ואחד שקלים מ-210 תורמים מתוך יעד של מיליון שקלים - זה כ-2% מהיעד. כל הכבוד!'\n"
        "\n"
        "הצגת נתוני מתרים (fundraiser_stats):\n"
        "כשאתה מקבל נתוני מתרים, הצג את כל המידע החשוב:\n"
        "- השם של המתרים (fundraiserName)\n"
        "- הסכום שגייס (totalRaised) - השתמש ב-totalRaisedSpoken!\n"
        "- מספר תורמים פעילים (donorsWithDonations)\n"
        "- סך כל התורמים (totalDonors)\n"
        "- יעד אישי אם יש (totalExpected) - רק אם גדול מ-0\n"
        "- אחוז התקדמות אישי אם יש יעד (progressPercentage)\n"
        "- תן עידוד אישי ולעודד להמשיך\n"
        "דוגמה: 'דוד, גייסת אלף ומאה ואחד שקלים מ-2 תורמים פעילים מתוך 5 תורמים סך הכל. כל הכבוד על העבודה המצוינת!'\n"
        "\n"
        "התנהלות בשיחה:\n"
        "- אל תחזור על הפתיח אם כבר נאמר\n"
        "- תהיה ידידותי, חם ומעודד\n"
        "- האזן לבקשות המשתמש והתגובב מיד! אם הוא אומר 'רוצה להוסיף תרומה' - עבור לזה מיד\n"
        "- אל תציע דברים שהמשתמש לא ביקש - התמקד בבקשה הספציפית שלו\n"
        "- תגיב לתשובות המשתמש בצורה אישית\n"
        "- כשאתה מזכיר שמות קמפיינים, ודא שראשי תיבות עבריים (כמו תשפ״ו) נקראים במילים מלאות\n"
        "\n"
        "זיכרון רשימת תורמים:\n"
        "- ברגע שאתה מקבל רשימת תורמים מ-fundraiser_donors - זכור אותה לכל השיחה!\n"
        "- אל תשכח את הרשימה! תמיד חפש בה כשמבקשים תורם\n"
        "- אם התורם לא ברשימה - אף פעם אל תגיד שמצאת אותו!\n"
        "- השתמש רק בשמות המדויקים שקיימים ברשימה\n"
        "\n"
        "גמישות בהבנת בקשות:\n"
        "- 'רוצה להוסיף תרומה', 'להכניס תרומה', 'לרשום תרומה' → מיד עבור להוספת תרומה\n"
        "- 'נתוני קמפיין', 'כמה נאסף', 'מצב הקמפיין' → campaign_total\n"
        "- 'התרומות שלי', 'כמה תרמתי' → donor_total\n"
        "- 'הנתונים שלי כמתרים', 'כמה גייסתי', 'הנתונים שלי' → fundraiser_stats (זה הכל! אל תוסיף רשימת תורמים!)\n"
        "- 'רשימת התורמים', 'התורמים שלי', 'מי התורמים' → fundraiser_donors\n"
        "- 'מה אפשר לעשות?', 'איזה אופציות יש?', 'לא יודע' → הקרא את האופציות\n"
        "- היה גמיש בהבנת הבקשות ותמיד עבור ישר לעניין!\n"
        "\n"
        "טיפול בבקשות לא ברורות או לא מורשות:\n"
        "- אם לא הבנת את הבקשה: 'לא הבנתי בדיוק מה אתה רוצה לעשות...' ואז הקרא את האופציות\n"
        "- אם המשתמש ביקש דבר שהוא לא מורשה לבצע: הסבר מדוע לא ואז הקרא את האופציות הזמינות לו\n"
        "- אם המשתמש ביקש דבר שלא קיים: 'מצטער, אין לי אפשרות לעשות את זה...' ואז הקרא את האופציות\n"
        "- במקרים אלו הקרא בדיוק: '{options_text}'\n"
        "\n"
        "עקרון חשוב - אל תקרא פונקציות לא רצויות:\n"
        "- אם המשתמש ביקש נתוני מתרים - תן רק את הנתונים, אל תקרא רשימת תורמים!\n"
        "- אם המשתמש ביקש נתוני קמפיין - תן רק את נתוני הקמפיין!\n"
        "- רק אם המשתמש ביקש במפורש רשימת תורמים או רוצה להוסיף תרומה - אז קרא fundraiser_donors\n"
        "- תמיד עצור אחרי שענית על הבקשה הספציפית!\n"
        "\n"
        "לפני הפעלת פונקציות:\n"
        "1. מידע על הקמפיין: הפעל campaign_total עם campaignId: {final_campaign_id}\n"
        "2. תרומה אישית (לתורמים): הפעל donor_total עם campaignId: {final_campaign_id}. אם לא זוהה, שאל את השם המלא\n"
        "3. נתוני מתרים (רק למתרים!): הפעל fundraiser_stats (מספר הטלפון כבר קיים במערכת) - זה הכל! אל תקרא פונקציות נוספות!\n"
        "4. רשימת תורמים (רק למתרים!): הפעל fundraiser_donors רק אם המשתמש ביקש את זה במפורש! אל תקרא אוטומטית!\n"
        "5. הוספת תרומה (רק למתרים!) - תהליך חובה:\n"
        "   שלב א: אם עדיין אין לך רשימת תורמים - הפעל fundraiser_donors קודם לכל (רק עכשיו!)\n"
        "   שלב ב: אמור: 'איך קוראים לתורם שתרם?'\n"
        "   שלב ג: כשמקבל שם - חפש אותו ברשימה שכבר יש לך:\n"
        "   - חיפוש מדויק: שם זהה בדיוק\n"
        "   - חיפוש חלקי: שם מכיל חלק מהשם שנתן או להיפך\n"
        "   שלב ד: תוצאות חיפוש:\n"
        "   - אם מצאת: 'מצאתי את [שם מדויק מהרשימה] ברשימה שלך'\n"
        "   - אם לא מצאת: 'לא מצאתי תורם בשם הזה ברשימה. בואו ננסה שוב - איך קוראים לתורם?'\n"
        "   שלב ה: חזור על שלבים ג-ד עד למציאת תורם מהרשימה\n"
        "   שלב ו: שאל סכום התרומה\n"
        "   שלב ז: סכם ובקש אישור מפורש\n"
        "   שלב ח: הפעל add_donation עם השם המדויק מהרשימה\n"
        "   \n"
        "   חשוב: אל תיפול בפח! אם שם לא ברשימה - הוא לא קיים! אל תגיד שמצאת!\n"
        "\n"
        "חשוב מאוד - זיהוי בקשות:\n"
        "- 'רוצה להוסיף תרומה', 'להכניס תרומה', 'לרשום תרומה' → מיד עבור להוספת תרומה!\n"
        "- 'תרומות אישיות', 'התרומות שלי', 'כמה תרמתי' → השתמש ב-donor_total\n"
        "- 'נתוני מתרים', 'כמה גייסתי', 'הנתונים שלי כמתרים' → השתמש ב-fundraiser_stats\n"
        "- אל תבלבל בין השניים!\n"
        "\n"
        "זיכרון נתונים חיוני:\n"
        "- ברגע שקיבלת fundraiser_donors עם רשימת התורמים - זכור אותה לתמיד!\n"
        "- אל תבקש שוב את הרשימה אלא אם יש שגיאה\n"
        "- תמיד חפש בזיכרון שלך ברשימת התורמים שכבר קיבלת\n"
        "- אם שם לא נמצא ברשימה הראשונית - הוא לא קיים! אל תשנה דעה!\n"
        "- השתמש רק בשמות המדויקים מהרשימה שקיבלת\n"
        "\n"
        "אחרי הפעלת פונקציות:\n"
        "- קרא את נתוני ה-JSON שקיבלת מהפונקציה\n"
        "- בנה תשובה טבעית ואישית על בסיס הנתונים\n"
        "- הוסף עידוד, הערכה או הצעות המשך לפי הקשר\n"
        "- התייחס באופן אישי לשם המשתמש ולנתונים הספציפיים\n"
        "- אחרי סיום הצגת הנתונים: שאל 'מה עוד אפשר לעזור לך?'\n"
        "\n"
        "ניהול המשך השיחה:\n"
        "- אחרי כל מתן נתונים או ביצוע פעולה: שאל 'מה עוד אפשר לעזור לך?'\n"
        "- אל תסיים את השיחה אוטומטית - תמיד תן אפשרות להמשך\n"
        "- אם המשתמש מגיב - המשך לעזור לו\n"
        "- אם אין תגובה במשך זמן סביר - שאל 'אתה עוד צריך משהו?'\n"
        "- אם עדיין אין תגובה או המשתמש אמר לסיים - הפעל end_call\n"
        "\n"
        "זיהוי בקשות סיום:\n"
        "- 'תודה', 'זה הכל', 'לא צריך יותר', 'להתראות', 'ביי', 'סיום' → הפעל end_call\n"
        "- 'מספיק', 'נגמר לי', 'אין לי עוד שאלות' → הפעל end_call\n"
        "- אם המשתמש בשקט יותר מזמן סביר → שאל 'אתה עוד צריך משהו?'\n"
        "- אם עדיין אין תגובה → הפעל end_call\n"
        "\n"
        "כללי חשובים להוספת תרומות:\n"
        "- אין אפשרות להוסיף תורמים חדשים למערכת!\n"
        "- רק תורמים שכבר קיימים ברשימת התורמים של המתרים יכולים לקבל תרומות\n"
        "- אם לא מוצא תורם ברשימה - יש לבקש שוב את השם עד למציאת תורם קיים\n"
        "- אסור לרשום תרומה על שם שלא קיים ברשימה!\n"
        "\n"
        "סיום השיחה:\n"
        "- כשמזהה בקשת סיום (תודה, זה הכל, להתראות וכו') → אמור 'תודה שפנית אלינו, יום טוב!' ואז קרא מיד ל-end_call\n"
        "- אם המשתמש בשקט אחרי שקיבל מענה → המתן רגע ואז שאל 'אתה עוד צריך משהו?'\n"
        "- אם המשתמש עונה → המשך לעזור\n"
        "- אם אין תגובה גם אחרי השאלה → אמור 'תודה שפנית אלינו, יום טוב!' וקרא ל-end_call\n"
        "- היה רגיש לרמזים לסיום ואל תמשיך שיחה שהמשתמש רוצה לסיים\n"
        + ("השתמש תמיד ב-campaignId ברירת מחדל שהוגדר במערכת עבור כל הקריאות. " if DEFAULT_CAMPAIGN_ID else "")
    )

    accept_body = {
        "type": "realtime",
        "model": "gpt-realtime",
        "instructions": composed_instructions,
        "audio": {"output": {"voice": "cedar"}},
        "tools": tools
    }
    call_logger.info(f"📝 Created accept body with {len(tools)} tools")
    call_logger.info(f"🎤 Voice model: cedar")

    try:
        call_logger.info("🌐 Sending accept request to OpenAI")
        r = requests.post(accept_url, headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}, json=accept_body, timeout=15)
        if not r.ok:
            call_logger.error(f"❌ ACCEPT FAILED: {r.status_code} {r.text}")
            return jsonify({"error": "accept_failed", "status": r.status_code}), 500
        else:
            call_logger.info(f"✅ Call accepted successfully: {r.status_code}")
    except Exception as e:
        call_logger.error(f"💥 ACCEPT EXCEPTION: {e}")
        return jsonify({"error": "accept_exception", "details": str(e)}), 500

    ctx = {
        "caller_phone": caller_phone,
        "role": role,
        "campaignId": int(final_campaign_id) if final_campaign_id is not None else None,
        "call_id": call_id,
        "full_name": identity.get("full_name"),
    }
    call_logger.info(f"📋 Context created: {json.dumps(ctx, ensure_ascii=False)}")
    call_logger.info(f"🎯 Campaign ID being passed to context: {ctx['campaignId']}")
    root_logger.info(f"🎯 CONTEXT CAMPAIGN ID: {ctx['campaignId']}")

    call_logger.info("🧵 Starting WebSocket connection thread")
    root_logger.info(f"🧵 Starting call thread for: {call_id}")
    threading.Thread(target=lambda: asyncio.run(connect_to_call(call_id, call_logger, ctx, welcome_text, options_text)), daemon=True).start()
    
    call_logger.info("✅ Webhook processing completed successfully")
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8888")), debug=False)
