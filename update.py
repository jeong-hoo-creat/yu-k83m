"""All for YU 공지 자동 갱신 스크립트.

GitHub Actions가 매일 실행해요. 학교 사이트 목록을 읽어서 data.json을 새로 써요.
- GEMINI_API_KEY 비밀값이 있으면 Gemini(무료 등급)로 제목·날짜를 더 정확하게 뽑아요.
- 없으면 링크와 날짜 패턴으로 뽑아요.
외부 패키지 없이 파이썬 기본 기능만 써요.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

KST = dt.timezone(dt.timedelta(hours=9))
TODAY = dt.datetime.now(KST).date()
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"

SOURCES = [
    # name, 기본 분류, 목록 주소, 글 링크 패턴, 목록의 날짜 의미
    {"name": "경영학과 공지", "cat": "경영학과", "url": "https://www.yu.ac.kr/biz/board/notice.do",
     "link": r"[?&]articleNo=(\d+)", "dates": "posted", "limit": 15},
    {"name": "학사안내", "cat": "학사", "url": "https://www.yu.ac.kr/main/bachelor/bachelor-guide.do",
     "link": r"[?&]articleNo=(\d+)", "dates": "period", "limit": 15},
    {"name": "편의시설", "cat": "생활", "url": "https://www.yu.ac.kr/yucf/notice/diet.do",
     "link": r"[?&]articleNo=(\d+)", "dates": "posted", "limit": 1},
    {"name": "유토피아", "cat": "비교과", "url": "https://yutopia.yu.ac.kr/",
     "link": r"/program/all/view/(\d+)", "dates": "period", "limit": 40},
    {"name": "어울림", "cat": "취업", "url": "https://join.yu.ac.kr/front_new/",
     "link": r"(?:P_IDX|[?&]idx)=(\d+)", "dates": "period", "limit": 20},
]

DATE_RE = re.compile(r"(20\d\d)[.\-/]\s?(\d{1,2})[.\-/]\s?(\d{1,2})")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, data=None, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        cs = r.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(cs, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def text_of(fragment):
    fragment = re.sub(r"(?is)<(script|style).*?</\1>", " ", fragment)
    fragment = re.sub(r"(?s)<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(fragment)).strip()


def iso(m):
    y, mo, d = int(m[0]), int(m[1]), int(m[2])
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return ""


def clean_url(base, href, pattern):
    url = urllib.parse.urljoin(base, html.unescape(href))
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(p.query)
    keep = {k: v for k, v in q.items() if k in ("mode", "articleNo", "P_IDX", "idx", "g_page", "m_page", "act")}
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(keep, doseq=True), fragment=""))


# ---------- 방법 1: 링크·날짜 패턴으로 뽑기 ----------
def heuristic(src, page):
    items, seen = [], set()
    anchors = list(re.finditer(r'(?is)<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page))
    for i, a in enumerate(anchors):
        href, inner = a.group(1), a.group(2)
        m = re.search(src["link"], html.unescape(href))
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        # 이 링크부터 다음 글 링크 전까지를 한 항목으로 봐요
        end = len(page)
        for b in anchors[i + 1:]:
            if re.search(src["link"], html.unescape(b.group(1))) and not re.search(src["link"] .replace("(\\d+)", m.group(1)), html.unescape(b.group(1))):
                end = b.start()
                break
        start = max(0, a.start() - 400)
        seg = page[start:end]
        title = text_of(inner)
        t2 = re.search(r'title="([^"]{6,})"', a.group(0))
        if len(title) < 4 and t2:
            title = html.unescape(t2.group(1))
        if len(title) < 4:
            tm = re.search(r'(?is)class="[^"]*(?:tit|subject|name)[^"]*"[^>]*>(.*?)</', page[a.start():end])
            title = text_of(tm.group(1)) if tm else ""
        if len(title) < 4 or len(title) > 200:
            continue
        dates = [d for d in (iso(x) for x in DATE_RE.findall(page[a.start():end][:1500])) if d]
        item = {"title": title, "url": clean_url(src["url"], href, src["link"]), "cat": src["cat"]}
        if src["dates"] == "posted" and dates:
            item["posted"] = dates[0]
        elif src["dates"] == "period" and dates:
            if len(dates) >= 2:
                item["from"], item["deadline"] = dates[0], dates[1]
            else:
                item["deadline"] = dates[0]
        items.append(item)
        if len(items) >= src["limit"]:
            break
    if not items:
        items = loose(src, page)
    return items


def loose(src, page):
    """링크가 onclick·data 속성 안에 있을 때: 페이지 어디서든 글 번호를 찾아요."""
    items, seen = [], set()
    hits = list(re.finditer(src["link"], page))
    for i, m in enumerate(hits):
        if m.group(1) in seen:
            continue
        seen.add(m.group(1))
        end = len(page)
        for n in hits[i + 1:]:
            if n.group(1) != m.group(1):
                end = n.start()
                break
        seg = page[m.start():end][:4000]
        tm = re.search(r'(?is)class="[^"]*(?:tit|subject|name|program)[^"]*"[^>]*>(.*?)</', seg)
        title = text_of(tm.group(1)) if tm else ""
        if len(title) < 4:
            chunks = [c.strip() for c in re.split(r"(?s)<[^>]+>", seg) if c.strip()]
            chunks = [html.unescape(c) for c in chunks if len(c.strip()) >= 6 and not DATE_RE.search(c) and not c.strip().startswith(("http", "/", "javascript"))]
            title = re.sub(r"\s+", " ", chunks[0]).strip() if chunks else ""
        if len(title) < 4 or len(title) > 200:
            continue
        href = src["url"].rstrip("/").split("/ko/")[0] + "/ko/program/all/view/" + m.group(1) if "program/all/view" in src["link"] else urllib.parse.urljoin(src["url"], page[m.start():m.end()])
        dates = [d for d in (iso(x) for x in DATE_RE.findall(seg[:1500])) if d]
        item = {"title": title, "url": href, "cat": src["cat"]}
        if len(dates) >= 2:
            item["from"], item["deadline"] = dates[0], dates[1]
        elif dates:
            item["deadline"] = dates[0]
        items.append(item)
        if len(items) >= src["limit"]:
            break
    return items


# ---------- 방법 2: Gemini로 뽑기 (GEMINI_API_KEY가 있을 때) ----------
GEMINI_MODEL = None


def gemini_model(key):
    global GEMINI_MODEL
    if GEMINI_MODEL:
        return GEMINI_MODEL
    j = json.loads(get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", headers={"x-goog-api-key": key}))
    names = [m["name"] for m in j.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])
             and "gemini" in m["name"] and not re.search(r"embedding|image|tts|audio|live|preview|exp", m["name"])]
    names.sort(key=lambda n: (0 if "flash-lite" in n else 1 if "flash" in n else 2, n), reverse=False)
    GEMINI_MODEL = names[0]
    log("Gemini model:", GEMINI_MODEL)
    return GEMINI_MODEL


def page_for_llm(src, page):
    page = re.sub(r"(?is)<(script|style|svg|noscript).*?</\1>", " ", page)
    page = re.sub(r'(?is)<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                  lambda m: f" [{text_of(m.group(2))}]({urllib.parse.urljoin(src['url'], html.unescape(m.group(1)))}) ", page)
    return text_of(page)[:30000]


def llm(src, page, key):
    prompt = f"""아래는 영남대학교 '{src['name']}' 페이지 내용이야. 학생에게 알릴 게시글·프로그램·채용을 최대 {src['limit']}개 뽑아.
JSON 배열만 답해. 각 항목: {{"title": 제목, "url": 원문 링크(페이지에 있는 그대로), "posted": 게시일 YYYY-MM-DD 또는 "", "from": 신청 시작일 또는 "", "deadline": 신청 마감일(시간 있으면 "YYYY-MM-DD HH:MM") 또는 "", "summary": 대상·일시·장소·혜택을 한두 문장으로}}.
페이지에 없는 정보는 지어내지 말고 빈 문자열로 둬. 오늘은 {TODAY.isoformat()}.

{page_for_llm(src, page)}"""
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                       "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}).encode()
    model = gemini_model(key)
    j = json.loads(get(f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent", data=body,
                       headers={"x-goog-api-key": key, "Content-Type": "application/json"}, timeout=120))
    text = "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"] if not p.get("thought"))
    arr = json.loads(text)
    out = []
    for x in arr if isinstance(arr, list) else []:
        title, url = str(x.get("title", "")).strip(), str(x.get("url", "")).strip()
        if len(title) < 4 or not url.startswith("http") or not re.search(src["link"], url):
            continue
        item = {"title": title[:200], "url": clean_url(src["url"], url, src["link"]), "cat": src["cat"]}
        for k in ("posted", "from", "deadline"):
            v = str(x.get(k, "")).strip()
            if re.match(r"^20\d\d-\d\d-\d\d( \d\d:\d\d)?$", v):
                item[k] = v if k == "deadline" else v[:10]
        if x.get("summary"):
            item["body"] = str(x["summary"])[:400]
        out.append(item)
    return out


# ---------- 합치기 ----------
def main():
    try:
        with open("data.json", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"notices": []}
    notices = data.get("notices", [])
    by_url = {n["url"]: n for n in notices}
    next_id = max([n.get("id", 0) for n in notices] + [0]) + 1
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    added = updated = 0

    for src in SOURCES:
        try:
            page = get(src["url"])
        except Exception as e:
            log(f"[{src['name']}] 페이지를 못 읽었어요: {e}")
            continue
        items = []
        if key:
            try:
                items = llm(src, page, key)
                log(f"[{src['name']}] Gemini로 {len(items)}건")
            except Exception as e:
                log(f"[{src['name']}] Gemini 실패, 패턴 방식으로 바꿔요: {e}")
        if not items:
            items = heuristic(src, page)
            log(f"[{src['name']}] 패턴 방식으로 {len(items)}건")
        for it in items:
            old = by_url.get(it["url"])
            if old:
                # 직접 정리한 항목은 제목을 지키고, 비어 있는 날짜만 채워요
                fields = ("title", "deadline", "from") if old.get("auto") else ("deadline", "from")
                for k in fields:
                    if it.get(k) and it[k] != old.get(k) and (old.get("auto") or not old.get(k)):
                        old[k] = it[k]
                        updated += 1
                if it.get("body") and old.get("auto"):
                    old["body"] = it["body"]
                continue
            n = {"id": next_id, "cat": it["cat"], "title": it["title"],
                 "posted": it.get("posted") or TODAY.isoformat(), "deadline": it.get("deadline", ""),
                 "body": it.get("body") or f"{src['name']}에 새로 올라온 글이에요. 자세한 내용은 원문을 확인하세요.",
                 "url": it["url"], "source": src["name"], "auto": True}
            if it.get("from"):
                n["from"] = it["from"]
            notices.append(n)
            by_url[n["url"]] = n
            next_id += 1
            added += 1

    # 오래된 글 정리: 마감 3주 지난 것, 마감 없이 60일 지난 것 (생활 안내는 유지)
    def keep(n):
        if n.get("cat") == "생활" and not n.get("auto"):
            return True
        dl = (n.get("deadline") or "")[:10]
        if dl:
            return dt.date.fromisoformat(dl) >= TODAY - dt.timedelta(days=21)
        ps = n.get("posted") or ""
        return not ps or dt.date.fromisoformat(ps) >= TODAY - dt.timedelta(days=60)

    notices = [n for n in notices if keep(n)]
    notices.sort(key=lambda n: n.get("posted") or "", reverse=True)
    notices = notices[:200]
    out = {"updated": dt.datetime.now(KST).isoformat(timespec="minutes"), "notices": notices}
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log(f"완료: 새 글 {added}건, 바뀐 항목 {updated}건, 전체 {len(notices)}건")


# ---------- 내 일정: 러닝엑스 캘린더 피드 → 암호화해서 저장 ----------
def parse_ics(text):
    text = re.sub(r"\r\n?", "\n", text).replace("\n ", "").replace("\n\t", "")
    out, cur = [], None
    for line in text.split("\n"):
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur and cur.get("title") and cur.get("t"):
                out.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, val = line.split(":", 1)
        name = head.split(";")[0].upper()
        if name == "SUMMARY":
            cur["title"] = re.sub(r"\\([,;\\])", r"\1", val.replace("\\n", " "))[:160]
        elif name == "DTSTART" and "t" not in cur:
            m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$", val.strip())
            if m:
                y, mo, d, h, mi, sec, z = m.groups()
                if h is None:
                    t = dt.datetime(int(y), int(mo), int(d), 23, 59, tzinfo=KST)
                    cur["allDay"] = True
                else:
                    tz = dt.timezone.utc if z else KST
                    t = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0), tzinfo=tz)
                    cur["allDay"] = False
                cur["t"] = int(t.timestamp() * 1000)
        elif name == "URL" and val.strip().startswith("http"):
            cur["url"] = val.strip()
    return out


def sync_schedule():
    feed, pw = os.environ.get("LMS_FEED_URL", "").strip(), os.environ.get("SCHEDULE_PASS", "")
    if not feed or not pw:
        log("[내 일정] 비밀값이 없어서 건너뛰어요")
        return
    import base64, hashlib, hmac
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        log("[내 일정] cryptography 패키지가 없어요")
        return
    try:
        events = parse_ics(get(feed))
    except Exception as e:
        log(f"[내 일정] 피드를 못 읽었어요: {e}")
        return
    now_ms = int(dt.datetime.now(KST).timestamp() * 1000)
    events = sorted([e for e in events if e["t"] >= now_ms - 30 * 86400000], key=lambda e: e["t"])
    plain = json.dumps(events, ensure_ascii=False, separators=(",", ":")).encode()
    hkey = hashlib.pbkdf2_hmac("sha256", pw.encode(), b"all-for-yu-hash", 200000, 32)
    h = hmac.new(hkey, plain, "sha256").hexdigest()
    try:
        with open("schedule.enc.json", encoding="utf-8") as f:
            if json.load(f).get("h") == h:
                log(f"[내 일정] 바뀐 일정 없음 ({len(events)}건)")
                return
    except Exception:
        pass
    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200000, 32)
    ct = AESGCM(key).encrypt(iv, plain, None)
    b = lambda x: base64.b64encode(x).decode()
    with open("schedule.enc.json", "w", encoding="utf-8") as f:
        json.dump({"v": 1, "salt": b(salt), "iv": b(iv), "ct": b(ct), "h": h,
                   "updated": dt.datetime.now(KST).isoformat(timespec="minutes")}, f)
    log(f"[내 일정] 암호화해서 저장 ({len(events)}건)")


# ---------- 경영학과 교수 연락처 → profs.json ----------
PROF_URL = "https://www.yu.ac.kr/biz/grad/grad-prof.do"
LABELS = {"성명": "name", "이름": "name", "직위": "pos", "직급": "pos", "연구분야": "field", "전공": "field",
          "연구실": "room", "전화": "phone", "연락처": "phone", "전화번호": "phone"}


def parse_profs(page):
    page = re.sub(r"(?is)<(script|style).*?</\1>", " ", page)
    lines = [html.unescape(x).strip() for x in re.sub(r"(?s)<[^>]+>", "\n", page).split("\n")]
    lines = [re.sub(r"\s+", " ", x) for x in lines if x.strip()]
    profs, cur, i = [], None, 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^(성명|이름|직위|직급|연구분야|전공|연구실|전화번호|전화|연락처)\s*[:：]?\s*(.*)$", ln)
        if m:
            key, val = LABELS[m.group(1)], m.group(2).strip()
            if not val and i + 1 < len(lines) and not re.match(r"^(성명|이름|직위|직급|학위|연구분야|전공|연구실|전화번호|전화|연락처|이메일|E-?mail)", lines[i + 1]):
                val = lines[i + 1].strip()
                i += 1
            if key == "name":
                if cur and cur.get("name"):
                    profs.append(cur)
                cur = {"name": re.sub(r"\s*(교수|부교수|조교수)$", "", val)}
            elif cur is not None and val:
                cur.setdefault(key, val)
        i += 1
    if cur and cur.get("name"):
        profs.append(cur)
    out = []
    for p in profs:
        name = p["name"].strip()
        if not re.fullmatch(r"[가-힣]{2,4}", name):
            continue
        room, phone = p.get("room", ""), p.get("phone", "")
        # 연구실 칸에 전화번호가 들어간 경우 정리
        if re.search(r"\d{3,4}-\d{4}", room) and not re.search(r"[가-힣]", room):
            phone, room = phone or room, ""
        ph = re.search(r"(?:053-?)?810-?\d{4}", phone or "")
        if ph:
            digits = re.sub(r"\D", "", ph.group(0))
            phone = "053-810-" + digits[-4:]
        else:
            phone = ""
        room = re.sub(r"\s*4층\s*", " ", room).strip()
        out.append({"name": name, "pos": p.get("pos", "교수"), "field": p.get("field", "").replace("미기재", ""),
                    "room": room if "상경관" in room else "", "phone": phone})
    return out


def page_lines(page):
    page = re.sub(r"(?is)<(script|style).*?</\1>", " ", page)
    page = re.sub(r'(?is)<a[^>]+href="tel:([^"]+)"[^>]*>.*?</a>', lambda m: "\nTEL " + m.group(1) + "\n", page)
    lines = [re.sub(r"\s+", " ", html.unescape(x)).strip() for x in re.sub(r"(?s)<[^>]+>", "\n", page).split("\n")]
    return [x for x in lines if x]


def parse_profs_loose(page):
    """라벨이 없을 때: '교수/부교수/조교수' 줄을 기준으로 앞의 이름, 뒤의 연구실·전화를 찾아요."""
    lines = page_lines(page)
    out, seen = [], set()
    for i, ln in enumerate(lines):
        pos = re.fullmatch(r"(?:직위\s*[:：]?\s*)?(교수|부교수|조교수|명예교수|석좌교수|특임교수)", ln)
        if not pos:
            continue
        name = ""
        for j in range(i - 1, max(-1, i - 6), -1):
            mm = re.fullmatch(r"(?:성명\s*[:：]?\s*)?([가-힣]{2,4})(?:\s*(?:교수|교수님))?", lines[j])
            if mm and mm.group(1) not in ("교수", "부교수", "조교수", "연구실", "연구분야", "전화", "이메일", "학위", "직위", "성명", "전공"):
                name = mm.group(1)
                break
        if not name or name in seen:
            continue
        seen.add(name)
        room = phone = field = ""
        for k in range(i + 1, min(len(lines), i + 16)):
            t = lines[k]
            if re.fullmatch(r"(교수|부교수|조교수|명예교수|석좌교수|특임교수)", t):
                break
            if not phone:
                ph = re.search(r"(?:053-?)?810-?\d{4}", t)
                if ph:
                    phone = "053-810-" + re.sub(r"\D", "", ph.group(0))[-4:]
            if not room and "상경관" in t:
                room = re.sub(r"^.*?(상경관)", r"\1", t).replace("4층", "").strip()
            if not field and re.match(r"^(연구분야|전공)", t):
                v = re.sub(r"^(연구분야|전공)\s*[:：]?\s*", "", t)
                field = v if v else (lines[k + 1] if k + 1 < len(lines) else "")
        out.append({"name": name, "pos": pos.group(1), "field": field.replace("미기재", ""), "room": room, "phone": phone})
    return out


def sync_profs():
    try:
        page = get(PROF_URL)
    except Exception as e:
        log(f"[교수 연락처] 페이지를 못 읽었어요: {e}")
        return
    profs = parse_profs(page)
    if len(profs) < 10:
        profs = parse_profs_loose(page)
    if len(profs) < 10:
        sample = [x for x in page_lines(page) if re.search(r"교수|상경관|810", x)][:12]
        log(f"[교수 연락처] 참고용 페이지 일부: {sample}")
    if len(profs) < 10:
        log(f"[교수 연락처] {len(profs)}명만 읽혀서 기존 정보를 그대로 둬요")
        return
    with open("profs.json", "w", encoding="utf-8") as f:
        json.dump({"updated": dt.datetime.now(KST).isoformat(timespec="minutes"), "profs": profs}, f, ensure_ascii=False, indent=1)
    log(f"[교수 연락처] {len(profs)}명 저장")


if __name__ == "__main__":
    main()
    sync_schedule()
    sync_profs()
