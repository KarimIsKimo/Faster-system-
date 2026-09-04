import asyncio
import json
import logging
import re
import time
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import uvicorn
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("clinic-desk")

app = FastAPI()

# --- CONFIGURATION ---
BASE_URL = "https://www.jothenclinics.com"
LOGIN_PAGE = f"{BASE_URL}/admin"
CLINIC_USERNAME = "rehab"
CLINIC_PASSWORD = "321"
POSTBACK_CACHE_TTL = 90  # seconds
# ---------------------

def get_full_form_payload(html: str):
    """Bulletproof ASP.NET emulator: Captures EVERY hidden token, client state, and input."""
    soup = BeautifulSoup(html, "html.parser")
    payload = {}
    for inp in soup.find_all(["input", "textarea", "select"]):
        name = inp.get("name")
        if not name:
            continue

        tag_type = inp.get("type", "").lower()
        if inp.name == "input" and tag_type in ["submit", "button", "image"]:
            continue

        if inp.name == "select":
            selected_opt = inp.find("option", selected=True)
            if selected_opt:
                payload[name] = selected_opt.get("value", "")
            else:
                opts = inp.find_all("option")
                payload[name] = opts[0].get("value", "") if opts else ""
        elif inp.name == "textarea":
            payload[name] = inp.get_text()
        else:
            if tag_type in ["checkbox", "radio"]:
                if inp.has_attr("checked"):
                    payload[name] = inp.get("value", "on")
            else:
                payload[name] = inp.get("value", "")

    payload.setdefault("__EVENTTARGET", "")
    payload.setdefault("__EVENTARGUMENT", "")
    return payload


class RobustSessionManager:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=25.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        self.is_logged_in = False
        self.cached_vs = ""
        self.cached_ev = ""
        self.cached_vsg = "A3E4F36A"
        self.lock = asyncio.Lock()
        self._postback_cache: dict[str, dict] = {}

    def update_tokens(self, html: str):
        soup = BeautifulSoup(html, "html.parser")
        vs = soup.find("input", {"id": "__VIEWSTATE"})
        ev = soup.find("input", {"id": "__EVENTVALIDATION"})
        vsg = soup.find("input", {"id": "__VIEWSTATEGENERATOR"})
        if vs: self.cached_vs = vs.get("value", "")
        if ev: self.cached_ev = ev.get("value", "")
        if vsg: self.cached_vsg = vsg.get("value", "")

    def cache_postback(self, key: str, target, btn, phone):
        self._postback_cache[key] = {"target": target, "btn": btn, "ts": time.time(), "phone": phone}

    def get_cached_postback(self, key: str):
        entry = self._postback_cache.get(key)
        if not entry: return None
        if time.time() - entry["ts"] > POSTBACK_CACHE_TTL:
            self._postback_cache.pop(key, None)
            return None
        return entry

    async def _perform_login(self):
        log.info("Attempting login...")
        self.client.cookies.clear()
        try:
            resp_get = await self.client.get(LOGIN_PAGE)
            payload = get_full_form_payload(resp_get.text)
            payload["txtUserName"] = CLINIC_USERNAME
            payload["TxtPassword"] = CLINIC_PASSWORD
            payload["btnSave"] = "دخول"

            resp_post = await self.client.post(
                LOGIN_PAGE, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            if "/Doctor" in str(resp_post.url).lower() or "DoctorCalender" in resp_post.text:
                log.info("Login successful")
                self.is_logged_in = True
                cal_res = await self.client.get(f"{BASE_URL}/Doctor/DoctorCalenderLaser")
                self.update_tokens(cal_res.text)
                return True
            else:
                self.is_logged_in = False
                return False
        except Exception as e:
            log.exception(f"Login failed: {e}")
            self.is_logged_in = False
            return False

    async def ensure_active_session(self):
        if not self.is_logged_in:
            await self._perform_login()

    async def heartbeat_loop(self):
        while True:
            try:
                async with self.lock:
                    if not self.is_logged_in:
                        await self._perform_login()
                    else:
                        res = await self.client.get(f"{BASE_URL}/Doctor/DoctorCalenderLaser")
                        if "admin" in str(res.url).lower() or "login" in str(res.url).lower():
                            self.is_logged_in = False
                        else:
                            self.update_tokens(res.text)
            except Exception:
                pass
            await asyncio.sleep(180)


session_mgr = RobustSessionManager()


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(session_mgr.heartbeat_loop())


def _match_key(name: str) -> str:
    return " ".join(name.strip().split()[:3]).lower()


async def _search_telerik(q: str):
    try:
        lod_param = {
            "Command": "LOD", "Text": q,
            "ClientState": {"value": "0", "text": "", "enabled": True, "logEntries": [], "checkedIndices": [], "checkedItemsTextOverflows": False},
            "Context": {"Text": q, "NumberOfItems": 0}, "NumberOfItems": 0,
        }
        payload = {
            "RadScriptManager1_TSM": "", "__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "",
            "__VIEWSTATE": session_mgr.cached_vs, "__VIEWSTATEGENERATOR": session_mgr.cached_vsg, "__EVENTVALIDATION": session_mgr.cached_ev,
            "__CALLBACKID": "ctl00$MainContent$ComPatient", "__CALLBACKPARAM": json.dumps(lod_param),
            "ctl00$MainContent$DdlBranchSearch": "98", "ctl00$MainContent$DdlBranch": "98",
        }
        resp = await session_mgr.client.post(
            f"{BASE_URL}/Doctor/DoctorCalenderLaser",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-MicrosoftAjax": "Delta=true"}
        )
        if "admin" in str(resp.url).lower() or "login" in str(resp.url).lower(): return []

        sections = resp.text.split("_$$_")
        if len(sections) < 2: return []
        json_chunk = sections[0].strip()
        if "[" in json_chunk: json_chunk = json_chunk[json_chunk.find("["):]

        try: raw_items = json.loads(json_chunk)
        except Exception: raw_items = []

        html_labels = [m.strip() for m in re.findall(r'<li class="rcbItem">(.*?)</li>', sections[1], re.DOTALL)]
        matches = []
        for i, item in enumerate(raw_items):
            pid = item.get("value")
            label = item.get("text") or (html_labels[i] if i < len(html_labels) else "")
            phone, name = "", label

            digits = re.findall(r"0\d[\d\s-]{7,12}", label)
            if digits:
                phone = digits[0].replace(" ", "").replace("-", "").strip()
                name = label.replace(digits[0], "").strip()

            matches.append({
                "system_code": pid,
                "name": name if name else label,
                "phone": phone if phone else "مسجل بالملف",
                "branch": "الرحاب",
            })
        return matches
    except Exception as e:
        log.exception(f"Telerik search error: {e}")
        return []


async def _search_grid(q: str, is_num: bool):
    try:
        res1 = await session_mgr.client.get(f"{BASE_URL}/Doctor/Patient")
        if "admin" in str(res1.url).lower() or "login" in str(res1.url).lower(): return []

        payload = get_full_form_payload(res1.text)
        payload["__EVENTTARGET"] = ""
        payload["ctl00$MainContent$BtnSearchPatient"] = "Search"

        if is_num:
            payload["ctl00$MainContent$TxtPhoneSearch"] = q
            payload["ctl00$MainContent$TxtNameSearch"] = ""
        else:
            payload["ctl00$MainContent$TxtPhoneSearch"] = ""
            payload["ctl00$MainContent$TxtNameSearch"] = q

        res2 = await session_mgr.client.post(
            f"{BASE_URL}/Doctor/Patient",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": f"{BASE_URL}/Doctor/Patient"}
        )

        soup = BeautifulSoup(res2.text, "html.parser")
        matches = []

        for tr in soup.find_all("tr"):
            if tr.find(["th"]) or "تاريخ الميلاد" in tr.get_text() or "اسم المريض" in tr.get_text(): continue

            action_btn = tr.find("input", type=lambda t: t and t.lower() in ["submit", "image", "button"])
            action_link = tr.find("a", href=lambda h: h and "__doPostBack" in h and "Sort$" not in h)

            if not action_btn and not action_link: continue

            row_text = tr.get_text(" | ")
            clean_row = " ".join(row_text.split())

            if not is_num and q.lower() not in clean_row.lower(): continue

            phone_match = re.search(r'0\d{8,12}', clean_row.replace(" ", ""))
            extracted_phone = phone_match.group(0) if phone_match else "مسجل بالملف"

            text_parts = [p.strip() for p in row_text.split("|") if p.strip()]
            extracted_name = ""
            for part in text_parts:
                if any(x in part.lower() for x in ["details", "تفاصيل", "تعديل", "select", "edit", "view"]): continue
                if re.search(r'\d{5,}', part) or re.search(r'\d{2}/\d{2}/\d{4}', part): continue
                if len(part) > 3 and len(part) > len(extracted_name):
                    extracted_name = part

            final_name = extracted_name if extracted_name else "مريض"

            target_postback = None
            btn_name = action_btn.get("name") if action_btn else None
            if not btn_name and action_link:
                m = re.search(r"__doPostBack\('(.*?)'", action_link.get("href"))
                if m: target_postback = m.group(1)

            key = _match_key(final_name)
            session_mgr.cache_postback(key, target_postback, btn_name, extracted_phone)

            matches.append({
                "system_code": "ملف دقيق",
                "name": final_name,
                "phone": extracted_phone,
                "branch": "الرحاب",
            })
        return matches
    except Exception as e:
        log.exception(f"_search_grid error for q={q!r}: {e}")
        return []


async def fetch_partial_matches(query: str):
    async with session_mgr.lock:
        await session_mgr.ensure_active_session()
        query = query.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).strip()
        is_phone = any(c.isdigit() for c in query)

        results_grid = await _search_grid(query, is_phone)
        results_telerik = await _search_telerik(query)

    merged = {}
    for p in results_grid + results_telerik:
        key = _match_key(p["name"])
        if key not in merged or p.get("system_code") == "ملف دقيق":
            merged[key] = p

    return list(merged.values())

def parse_grid_for_patient(html: str, search_name: str, search_phone: str):
    """Case-Insensitive Grid parser to ensure English names are never missed."""
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.find("table", id=re.compile("GridResults", re.I))
    if not grid: return False, None, None, None

    rows = [tr for tr in grid.find_all("tr") if not tr.find("th") and tr.get_text(strip=True)]
    if not rows: return False, None, None, None

    name_clean = search_name.lower().strip()
    phone_clean = search_phone.strip() if search_phone != "مسجل بالملف" else ""

    for tr in rows:
        row_text = " ".join(tr.get_text(" ").split()).lower()

        is_match = False
        if phone_clean and phone_clean in row_text:
            is_match = True
        elif name_clean and name_clean in row_text:
            is_match = True
        elif len(rows) == 1:
            is_match = True
        elif name_clean:
            parts = name_clean.split()
            if len(parts) >= 2 and parts[0] in row_text and parts[-1] in row_text:
                is_match = True

        if is_match:
            found_phone = None
            pm = re.search(r"01\d{8,10}", row_text)
            if pm: found_phone = pm.group(0)

            submit_btn_name, target_postback = None, None
            btn = tr.find("input", type=lambda t: t and t.lower() in ["submit", "button", "image"])
            if btn: submit_btn_name = btn.get("name")
            else:
                link = tr.find("a", href=lambda h: h and "__doPostBack" in h)
                if link:
                    m = re.search(r"__doPostBack\('(.*?)'", link.get("href"))
                    if m: target_postback = m.group(1)

            if submit_btn_name or target_postback:
                return True, target_postback, submit_btn_name, found_phone

    return False, None, None, None


async def extract_patient_notes(name: str, phone: str, q: str, sys: str):
    key = _match_key(name)

    async with session_mgr.lock:
        await session_mgr.ensure_active_session()

        cached = session_mgr.get_cached_postback(key)
        target_postback = cached["target"] if cached else None
        submit_btn_name = cached["btn"] if cached else None
        found_phone = (cached["phone"] if cached and cached["phone"] != "مسجل بالملف" else phone) or phone

        try:
            # 1. Determine best phone number to search with (Extract from initial query if missing)
            search_phone = found_phone
            if search_phone == "مسجل بالملف" or not search_phone:
                digits = re.sub(r'\D', '', q)
                if digits: search_phone = digits

            res_html = ""

            if not target_postback and not submit_btn_name:
                res1 = await session_mgr.client.get(f"{BASE_URL}/Doctor/Patient")
                if "admin" in str(res1.url).lower() or "login" in str(res1.url).lower():
                    await session_mgr._perform_login()
                    res1 = await session_mgr.client.get(f"{BASE_URL}/Doctor/Patient")

                payload_base = get_full_form_payload(res1.text)
                payload_base["__EVENTTARGET"] = ""
                payload_base["ctl00$MainContent$BtnSearchPatient"] = "Search"

                found_btn = False

                # Attempt A: Search by Phone (Always reliable if provided)
                if search_phone and search_phone != "مسجل بالملف":
                    p1 = payload_base.copy()
                    p1["ctl00$MainContent$TxtPhoneSearch"] = search_phone
                    p1["ctl00$MainContent$TxtNameSearch"] = ""
                    res2 = await session_mgr.client.post(f"{BASE_URL}/Doctor/Patient", data=p1, headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": f"{BASE_URL}/Doctor/Patient"})
                    found_btn, target_postback, submit_btn_name, grid_phone = parse_grid_for_patient(res2.text, name, search_phone)
                    if found_btn:
                        res_html = res2.text
                        if grid_phone: found_phone = grid_phone

                # Attempt B: Search by Full Name (Fallback)
                if not found_btn:
                    p2 = payload_base.copy()
                    p2["ctl00$MainContent$TxtPhoneSearch"] = ""
                    p2["ctl00$MainContent$TxtNameSearch"] = name
                    res2 = await session_mgr.client.post(f"{BASE_URL}/Doctor/Patient", data=p2, headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": f"{BASE_URL}/Doctor/Patient"})
                    found_btn, target_postback, submit_btn_name, grid_phone = parse_grid_for_patient(res2.text, name, search_phone)
                    if found_btn:
                        res_html = res2.text
                        if grid_phone: found_phone = grid_phone

                # Attempt C: Search by First Two Words of Name (Case-insensitive catch-all)
                if not found_btn:
                    short_name = " ".join(name.split()[:2])
                    p3 = payload_base.copy()
                    p3["ctl00$MainContent$TxtPhoneSearch"] = ""
                    p3["ctl00$MainContent$TxtNameSearch"] = short_name
                    res2 = await session_mgr.client.post(f"{BASE_URL}/Doctor/Patient", data=p3, headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": f"{BASE_URL}/Doctor/Patient"})
                    found_btn, target_postback, submit_btn_name, grid_phone = parse_grid_for_patient(res2.text, name, search_phone)
                    if found_btn:
                        res_html = res2.text
                        if grid_phone: found_phone = grid_phone

                if not found_btn:
                    return {"note": "المريض غير موجود أو تعذر قراءة الجدول", "phone": found_phone}
            else:
                res1 = await session_mgr.client.get(f"{BASE_URL}/Doctor/Patient")
                res_html = res1.text

            # Execute Details Postback
            payload_click = get_full_form_payload(res_html)
            if target_postback:
                payload_click["__EVENTTARGET"] = target_postback
                payload_click["__EVENTARGUMENT"] = ""
            elif submit_btn_name:
                payload_click["__EVENTTARGET"] = ""
                payload_click[submit_btn_name] = "Details"
                payload_click[f"{submit_btn_name}.x"] = "5"
                payload_click[f"{submit_btn_name}.y"] = "5"

            res3 = await session_mgr.client.post(
                f"{BASE_URL}/Doctor/Patient",
                data=payload_click,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": f"{BASE_URL}/Doctor/Patient"}
            )

            # Universal Notes Extraction (Captures Permanent Notes AND standard Notes)
            soup3 = BeautifulSoup(res3.text, "html.parser")
            notes = []
            for inp in soup3.find_all(["textarea", "input"]):
                name_attr = inp.get("name", "").lower()
                id_attr = inp.get("id", "").lower()
                if inp.name == "textarea" or "note" in name_attr or "note" in id_attr or "remark" in name_attr:
                    if "clientstate" in name_attr or "clientstate" in id_attr: continue
                    val = inp.get_text(strip=True) if inp.name == "textarea" else inp.get("value", "").strip()
                    if val and val not in notes:
                        notes.append(val)

            final_note = " - ".join(notes) if notes else "لا توجد ملاحظات مسجلة"
            return {"note": final_note, "phone": found_phone}

        except Exception as e:
            log.exception(f"Notes fetch error for name={name!r} phone={phone!r}: {e}")
            return {"note": "خطأ في الاتصال بالخادم", "phone": phone}


@app.get("/api/search")
async def search(q: str):
    if len(q.strip()) < 3: raise HTTPException(status_code=400, detail="Query too short")
    try: matched = await fetch_partial_matches(q.strip())
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    return {"count": len(matched), "patients": matched[:8]}


@app.get("/api/notes")
async def notes(name: str, phone: str = "", q: str = "", sys: str = ""):
    return await extract_patient_notes(name, phone, q, sys)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_content = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Jothen Clinics - Fast Desk</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    .fade-in { animation: fadeIn 0.3s ease-in-out; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(2px); } to { opacity: 1; transform: translateY(0); } }
  </style>
</head>
<body class="bg-slate-50 min-h-screen p-4 sm:p-8 font-sans text-slate-800">
  <div class="max-w-4xl mx-auto">
    
    <div class="flex items-center justify-between mb-8 pb-4 border-b border-slate-200">
      <div>
        <h1 id="txtTitle" class="text-2xl font-extrabold text-slate-900 tracking-tight">عيادات جوثن - لوحة الاستقبال السريعة</h1>
        <p id="txtSubtitle" class="text-sm text-slate-500 mt-1">نسخة المحاكاة الصلبة (بحث متعدد المراحل + استخراج الملاحظات الدائمة)</p>
      </div>
      <div class="flex items-center gap-3">
        <span id="badgeStatus" class="inline-flex items-center px-3 py-1 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-800 shadow-sm border border-emerald-200">
          <svg class="w-3 h-3 ml-1" fill="currentColor" viewBox="0 0 20 20"><circle cx="10" cy="10" r="5"></circle></svg>
          متصل بالنظام
        </span>
        <button id="btnLang" onclick="toggleLanguage()" class="px-4 py-1.5 bg-white border border-slate-300 rounded-lg text-sm font-semibold text-slate-700 hover:bg-slate-50 hover:text-slate-900 transition shadow-sm">English</button>
      </div>
    </div>

    <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 mb-6">
      <label id="lblSearch" class="block text-sm font-semibold text-slate-700 mb-3">بحث برقم الهاتف أو الاسم (يقبل أرقام غير كاملة)</label>
      <div class="relative flex items-center border border-slate-300 rounded-xl bg-slate-50 focus-within:ring-2 focus-within:ring-emerald-500 focus-within:border-emerald-500 focus-within:bg-white overflow-hidden transition-all duration-200">
        <div id="loader" class="hidden px-4 text-emerald-600">
          <svg class="animate-spin h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
        </div>
        <input type="text" id="searchInput" autofocus autocomplete="off" placeholder="اكتب أي جزء من الرقم أو الاسم..." class="w-full px-4 py-4 text-lg bg-transparent focus:outline-none placeholder-slate-400" oninput="handleSearch(this.value)">
        <button id="clearBtn" onclick="clearSearch()" class="hidden px-4 text-slate-400 hover:text-rose-500 transition-colors" title="مسح">
          <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
        </button>
      </div>
    </div>

    <div id="resultsCard" class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden mb-8">
      <div class="px-6 py-4 bg-slate-50 border-b border-slate-200 flex justify-between items-center">
        <h2 id="lblResults" class="font-bold text-slate-800">نتائج البحث المشابهة والجزئية</h2>
        <span id="resultCount" class="text-xs font-semibold text-slate-600 bg-slate-200 px-3 py-1 rounded-full shadow-inner">0 نتيجة</span>
      </div>
      <ul id="resultsList" class="divide-y divide-slate-100">
        <li class="p-10 text-center text-slate-400 flex flex-col items-center justify-center gap-3" id="promptRow">
          <svg class="w-10 h-10 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
          اكتب 3 أرقام أو حروف على الأقل لعرض النتائج المشابهة...
        </li>
      </ul>
    </div>
  </div>

  <script>
    let currentLang = 'ar';
    let debounceTimer = null;
    let searchToken = 0;
    let currentQuery = '';

    const t = {
      ar: {
        title: "عيادات جوثن - لوحة الاستقبال السريعة", subtitle: "نسخة المحاكاة الصلبة (بحث متعدد المراحل + استخراج الملاحظات الدائمة)", status: "متصل بالنظام", search: "بحث برقم الهاتف أو الاسم (يقبل أرقام غير كاملة)", ph: "اكتب أي جزء من الرقم أو الاسم...", resHeader: "نتائج البحث المشابهة والجزئية", resUnit: "نتائج مطابقة", prompt: "اكتب 3 أرقام أو حروف على الأقل لعرض النتائج المشابهة...", min: "ادخل 3 أحرف/أرقام على الأقل للبحث...", empty: "لم يتم العثور على أرقام أو أسماء مشابهة.", btn: "English", realFileBadge: "رقم الملف (ملاحظات):", sysCode: "كود النظام:", phone: "الهاتف", branch: "الفرع", loading: "جاري التحميل...", fail: "فشل التحميل", copyPhone: "نسخ الرقم", copyNote: "نسخ الملاحظات"
      },
      en: {
        title: "Jothen Clinics - Fast Desk", subtitle: "Solid Emulation Version (Multi-Tier Fallback + Permanent Notes)", status: "Connected", search: "Search by phone or name (partial numbers supported)", ph: "Type any part of phone or name...", resHeader: "Similar & Partial Search Results", resUnit: "matches", prompt: "Type at least 3 digits/letters to view matching patients...", min: "Enter at least 3 digits/characters...", empty: "No similar phone numbers or patients found.", btn: "عربي", realFileBadge: "File Number (Notes):", sysCode: "System Code:", phone: "Phone", branch: "Branch", loading: "Loading...", fail: "Failed to load", copyPhone: "Copy Phone", copyNote: "Copy Notes"
      }
    };

    function toggleLanguage() {
      currentLang = currentLang === 'ar' ? 'en' : 'ar';
      const c = t[currentLang];
      document.documentElement.setAttribute('dir', currentLang === 'ar' ? 'rtl' : 'ltr');
      document.documentElement.setAttribute('lang', currentLang);
      document.getElementById('txtTitle').innerText = c.title;
      document.getElementById('txtSubtitle').innerText = c.subtitle;
      document.getElementById('badgeStatus').innerHTML = `<svg class="w-3 h-3 ${currentLang === 'ar' ? 'ml-1' : 'mr-1'}" fill="currentColor" viewBox="0 0 20 20"><circle cx="10" cy="10" r="5"></circle></svg>` + c.status;
      document.getElementById('lblSearch').innerText = c.search;
      document.getElementById('searchInput').setAttribute('placeholder', c.ph);
      document.getElementById('lblResults').innerText = c.resHeader;
      document.getElementById('btnLang').innerText = c.btn;

      const val = document.getElementById('searchInput').value;
      if (!val || val.trim().length < 3) {
         document.getElementById('resultsList').innerHTML = `<li class="p-10 text-center text-slate-400 flex flex-col items-center justify-center gap-3"><svg class="w-10 h-10 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>${c.prompt}</li>`;
      }
    }

    function clearSearch() {
        const input = document.getElementById('searchInput');
        input.value = '';
        input.focus();
        handleSearch('');
    }

    async function copyToClipboard(textId, btnId) {
        const textElement = document.getElementById(textId);
        if (!textElement) return;

        let textToCopy = textElement.innerText;
        if (textToCopy === t[currentLang].loading) return;

        try {
            await navigator.clipboard.writeText(textToCopy);
            const btn = document.getElementById(btnId);
            const originalHTML = btn.innerHTML;

            btn.innerHTML = `<svg class="w-4 h-4 text-emerald-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"></path></svg>`;
            btn.classList.add('bg-emerald-100');

            setTimeout(() => {
                btn.innerHTML = originalHTML;
                btn.classList.remove('bg-emerald-100');
            }, 1500);
        } catch (err) {
            console.error('Failed to copy', err);
        }
    }

    async function runWithConcurrency(items, limit, worker) {
      let i = 0;
      async function next() {
        while (i < items.length) {
          const idx = i++;
          await worker(items[idx], idx);
        }
      }
      await Promise.all(Array.from({ length: Math.min(limit, items.length) }, next));
    }

    async function handleSearch(val) {
      const c = t[currentLang];
      currentQuery = val.trim();
      const myToken = ++searchToken;

      const clearBtn = document.getElementById('clearBtn');
      if (currentQuery.length > 0) {
          clearBtn.classList.remove('hidden');
      } else {
          clearBtn.classList.add('hidden');
      }

      if (currentQuery.length < 3) {
        document.getElementById('resultsList').innerHTML = `<li class="p-10 text-center text-slate-400 flex flex-col items-center justify-center gap-3"><svg class="w-10 h-10 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>${currentQuery.length > 0 ? c.min : c.prompt}</li>`;
        document.getElementById('resultCount').innerText = `0 ${c.resUnit}`;
        return;
      }

      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(async () => {
        document.getElementById('loader').classList.remove('hidden');
        try {
          const res = await fetch(`/api/search?q=${encodeURIComponent(currentQuery)}`);
          if (myToken !== searchToken) return;
          if (!res.ok) throw new Error("Server error");

          const data = await res.json();
          if (myToken !== searchToken) return;

          const list = document.getElementById('resultsList');
          list.innerHTML = '';
          document.getElementById('resultCount').innerText = `${data.count} ${c.resUnit}`;

          if (data.patients.length === 0) {
            list.innerHTML = `<li class="p-10 text-center text-slate-500 font-medium">${c.empty}</li>`;
          } else {
            data.patients.forEach((p, index) => {
              const li = document.createElement('li');
              li.className = 'p-5 hover:bg-slate-50 transition-colors duration-150 flex flex-col md:flex-row md:items-center justify-between gap-4 group';

              const phoneHtml = p.phone !== "مسجل بالملف" ? p.phone : "مسجل بالملف";

              li.innerHTML = `
                <div class="flex-1">
                  <h3 class="text-lg font-extrabold text-slate-800">${p.name}</h3>
                  <div class="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs font-medium text-slate-600 mt-2">
                    <span class="inline-flex items-center gap-1.5 bg-white border border-slate-200 px-2.5 py-1 rounded-md shadow-sm">
                      ${c.phone}: <strong id="phone-text-${index}" class="text-emerald-700 font-bold text-sm tracking-wide">${phoneHtml}</strong>
                      <button onclick="copyToClipboard('phone-text-${index}', 'copy-phone-${index}')" id="copy-phone-${index}" class="p-1 ml-1 rounded text-slate-400 hover:text-emerald-600 hover:bg-emerald-50 transition-colors focus:outline-none" title="${c.copyPhone}">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
                      </button>
                    </span>
                    <span class="inline-flex items-center bg-slate-100 text-slate-600 px-2.5 py-1 rounded-md">${c.branch}: ${p.branch}</span>
                    <span class="inline-flex items-center bg-slate-100 text-slate-600 px-2.5 py-1 rounded-md">${c.sysCode} ${p.system_code}</span>
                  </div>
                </div>
                <div class="bg-amber-50 border border-amber-200 rounded-xl px-4 py-3 text-right shrink-0 min-w-[280px] shadow-sm relative transition-shadow hover:shadow-md">
                  <div class="flex justify-between items-start mb-1">
                      <span class="text-[11px] font-bold text-amber-700 uppercase tracking-wide">${c.realFileBadge}</span>
                      <button onclick="copyToClipboard('note-${index}', 'copy-note-${index}')" id="copy-note-${index}" class="p-1 rounded text-amber-400 hover:text-amber-700 hover:bg-amber-100 transition-colors focus:outline-none opacity-0 group-hover:opacity-100" title="${c.copyNote}">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
                      </button>
                  </div>
                  <span id="note-${index}" class="text-lg font-black text-amber-900 block animate-pulse text-amber-500 transition-all duration-300">
                     ${c.loading}
                  </span>
                </div>
              `;
              list.appendChild(li);
            });

            runWithConcurrency(data.patients, 2, async (p, index) => {
              if (myToken !== searchToken) return;
              try {
                // Pass the original query (q), the system_code (sys), name, and phone
                const r = await fetch(`/api/notes?sys=${encodeURIComponent(p.system_code)}&name=${encodeURIComponent(p.name)}&phone=${encodeURIComponent(p.phone)}&q=${encodeURIComponent(currentQuery)}`);
                const noteData = await r.json();
                if (myToken !== searchToken) return;

                const el = document.getElementById(`note-${index}`);
                if (el) {
                  el.innerText = noteData.note;
                  el.classList.remove('animate-pulse', 'text-amber-500');
                  el.classList.add('fade-in');
                }

                const phoneEl = document.getElementById(`phone-text-${index}`);
                if (phoneEl && noteData.phone && noteData.phone !== "مسجل بالملف") {
                  phoneEl.innerText = noteData.phone;
                  phoneEl.classList.add('fade-in');
                }
              } catch (e) {
                const el = document.getElementById(`note-${index}`);
                if (el) {
                  el.innerText = c.fail;
                  el.classList.remove('animate-pulse', 'text-amber-500');
                  el.classList.add('text-rose-600');
                }
              }
            });
          }
        } catch (e) {
          console.error(e);
          if (myToken === searchToken) {
            document.getElementById('resultsList').innerHTML = `<li class="p-6 text-center text-rose-600 font-bold bg-rose-50 rounded-xl m-4">تعذر الاتصال بالخادم. تأكد من عمل السيرفر.</li>`;
          }
        } finally {
          if (myToken === searchToken) {
            document.getElementById('loader').classList.add('hidden');
          }
        }
      }, 200);
    }
  </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
