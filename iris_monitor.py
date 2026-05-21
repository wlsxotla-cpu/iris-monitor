"""
IRIS 사업공고 모니터링 스크립트 v10
- requests + BeautifulSoup으로 HTML 직접 파싱 (Playwright 불필요)
- 신규 공고 HTML 리포트 생성
- IRIS에서 보기: 실제 Chrome으로 공고 상세페이지 열기
- 컴퓨터 켤 때 자동 실행 (--auto 옵션)
"""
import json, sys, time, re, threading
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

# requests, bs4 설치 확인
try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("[오류] 필요한 패키지가 없습니다.")
    print("  pip install requests beautifulsoup4")
    sys.exit(1)

# ── 설정 ──────────────────────────────────────────────────────
BASE_URL   = "https://www.iris.go.kr"
TARGET_URL = f"{BASE_URL}/contents/retrieveBsnsAncmBtinSituListView.do"
VIEW_URL   = f"{BASE_URL}/contents/retrieveBsnsAncmView.do"
SERVER_PORT = 19876

ALL_MINISTRIES = {
    "1":  ("과학기술정보통신부", "AR4001", "#1a6fc4"),
    "2":  ("산업통상부",         "AR4002", "#c45c1a"),
    "3":  ("중소벤처기업부",     "AR4003", "#1a9e52"),
    "4":  ("국토교통부",         "AR4004", "#7b1fa2"),
    "5":  ("교육부",             "AR4005", "#00796b"),
    "6":  ("보건복지부",         "AR4009", "#c62828"),
    "7":  ("산림청",             "AR4013", "#558b2f"),
    "8":  ("해양수산부",         "AR4016", "#0277bd"),
    "9":  ("소방청",             "AR4024", "#d84315"),
    "10": ("범부처",             "AR4999", "#455a64"),
}

TABS = {
    "접수예정": "ancmPre",
    "접수중":   "ancmIng",
}

SCRIPT_DIR  = Path(__file__).parent
SEEN_FILE   = SCRIPT_DIR / "seen_announcements.json"
CONFIG_FILE = SCRIPT_DIR / "iris_config.json"
OUTPUT_DIR  = SCRIPT_DIR / "results"
OUTPUT_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": TARGET_URL,
    "Origin": "https://www.iris.go.kr",
}

# ── 유틸 ──────────────────────────────────────────────────────
def load_seen() -> set:
    if SEEN_FILE.exists():
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_seen(ids: set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)

def show_toast(title: str, message: str):
    try:
        import subprocess
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.Visible = $true
$notify.BalloonTipTitle = "{title}"
$notify.BalloonTipText = "{message}"
$notify.BalloonTipIcon = "Info"
$notify.ShowBalloonTip(5000)
Start-Sleep -Seconds 6
$notify.Dispose()
"""
        subprocess.Popen(
            ['powershell', '-WindowStyle', 'Hidden', '-Command', script],
            creationflags=0x08000000
        )
    except Exception:
        pass

# ── 공고 목록 수집 (requests) ─────────────────────────────────
def fetch_list_page(session, tab_arg: str, ministry_values: list, page_index: int) -> tuple[list, bool]:
    """한 페이지 공고 목록 수집. (items, has_next) 반환"""
    # 디버그8에서 성공한 방식과 동일하게 구성
    payload_list = [
        ("bizSearch", ""),
        ("bsnsTl", ""),
        ("ancmPrg", tab_arg),
        ("pageIndex", str(page_index)),
        ("ancmId", ""),
        ("ancmNo", ""),
        ("ancmTurn", ""),
        ("seq", ""),
        ("hirkSorgnBsnsCd", ""),
        ("bsnsAncmTap", ""),
        ("shSorgnYyBsnsCd", ""),
        ("sorgnIdArr", ""),
        ("ancmSttArr", ""),
        ("pbofrTpArr", ""),
        ("qualCndtArr", ""),
        ("techFildArr", ""),
        ("shBsnsYy", ""),
    ]
    for val in ministry_values:
        payload_list.append(("blngGovdSeArr", val))

    try:
        resp = session.post(TARGET_URL, data=payload_list, headers=HEADERS, timeout=30)
        resp.encoding = "utf-8"
        html = resp.text
        if len(html) < 500:
            print(f"    [경고] 응답이 너무 짧음 ({len(html)}자): {html[:200]}")
    except Exception as e:
        print(f"    [오류] 요청 실패: {e}")
        return [], False

    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 공고 목록 파싱 - dl 또는 li 안에 공고번호 포함된 것
    for el in soup.find_all(["dl", "li"]):
        text = el.get_text()
        if "공고번호" not in text:
            continue
        # 자식 중 공고번호 있으면 스킵 (중복 방지)
        children_text = " ".join(c.get_text() for c in el.find_all(["dl", "li"]))
        if "공고번호" in children_text:
            continue

        link = el.find("a")
        if not link:
            continue

        title   = link.get_text().strip()
        onclick = link.get("onclick", "")
        m       = re.search(r"view\('(\d+)'", onclick)
        ann_id  = m.group(1) if m else ""

        # 공고번호
        num_m   = re.search(r"공고번호\s*[:\s]*([\S ]+?)(?:\s*공고일자|\n)", text)
        ann_num = num_m.group(1).strip() if num_m else ""

        # 공고일자
        date_m   = re.search(r"공고일자\s*[:\s]*(\d{4}-\d{2}-\d{2})", text)
        ann_date = date_m.group(1) if date_m else ""

        # 공모유형
        type_m   = re.search(r"공모유형\s*[:\s]*([\S ,]+?)(?:\s*\n|접수|$)", text)
        ann_type = type_m.group(1).strip() if type_m else ""

        # 부처
        ministry = next((ALL_MINISTRIES[k][0] for k in ALL_MINISTRIES
                        if ALL_MINISTRIES[k][1] in ministry_values
                        and ALL_MINISTRIES[k][0] in text), "기타")

        # 전문기관
        agency  = ""
        arrow_m = re.search(r"[^\n>]+>\s*([^\n]+)", text)
        if arrow_m:
            agency = arrow_m.group(1).strip().split("\n")[0].strip()

        uid = ann_id if ann_id else f"{title}_{ann_date}"
        if not title:
            continue

        items.append({
            "id":             uid,
            "ann_id":         ann_id,
            "title":          title,
            "ministry":       ministry,
            "agency":         agency,
            "announce_num":   ann_num,
            "announce_date":  ann_date,
            "type":           ann_type,
            "status":         "접수예정" if tab_arg == "ancmPre" else "접수중",
            "receipt_period": "",
            "attachments":    [],
        })

    # 다음 페이지 여부
    next_page = soup.find("a", title=f"{page_index + 1}페이지")
    has_next  = next_page is not None

    return items, has_next

# ── 공고 상세 수집 (접수기간 + 첨부파일 이름) ─────────────────
def fetch_detail(session, item: dict, tab_arg: str):
    ann_id = item["ann_id"]
    if not ann_id:
        return

    payload = [
        ("ancmId",   ann_id),
        ("ancmPrg",  tab_arg),
        ("pageIndex", ""),
    ]
    try:
        resp = session.post(VIEW_URL, data=payload, headers=HEADERS, timeout=30)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 접수기간
        for li in soup.find_all("li", class_="write"):
            strong = li.find("strong")
            if strong and "접수기간" in strong.get_text():
                span = li.find("span")
                if span:
                    item["receipt_period"] = span.get_text().strip()
                break

        # 첨부파일 이름 수집
        attachments = []
        for a in soup.find_all("a", href=re.compile("downloadAtchFile")):
            fname_raw = a.get_text().strip()
            fname     = re.sub(r'\s*\(\d+[\d.]*\s*[KMG]B\)\s*$', '', fname_raw).strip()
            href      = a.get("href", "")
            if fname:
                attachments.append({"name": fname, "href": href})
        item["attachments"] = attachments

    except Exception as e:
        print(f"      [경고] 상세 수집 실패 ({ann_id}): {e}")

# ── 전체 수집 ─────────────────────────────────────────────────
def fetch_all(ministry_info: dict) -> list[dict]:
    target_values = list(ministry_info.keys())
    all_results   = []
    session       = requests.Session()

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] IRIS 공고 수집 중...")

    # 세션 초기화 - GET으로 먼저 접속해서 쿠키 획득
    try:
        resp = session.get(TARGET_URL, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
        }, timeout=30)
        resp.raise_for_status()
        print(f"  세션 초기화 완료 (쿠키: {len(session.cookies)}개)")
    except Exception as e:
        print(f"  [경고] 세션 초기화 실패: {e}")

    for tab_name, tab_arg in TABS.items():
        print(f"  → '{tab_name}' 탭...")
        # 부처별로 개별 요청 후 합치기
        tab_results = []
        for ministry_val in target_values:
            page_num = 1
            while True:
                items, has_next = fetch_list_page(session, tab_arg, [ministry_val], page_num)
                print(f"    {ALL_MINISTRIES[next(k for k in ALL_MINISTRIES if ALL_MINISTRIES[k][1]==ministry_val)][0]} 페이지 {page_num}: {len(items)}건")
                tab_results.extend(items)
                if has_next:
                    page_num += 1
                    time.sleep(0.3)
                else:
                    break
        all_results.extend(tab_results)

    # 중복 제거 및 부처 필터
    seen_uid, unique = set(), []
    for r in all_results:
        if r["id"] not in seen_uid:
            seen_uid.add(r["id"])
            unique.append(r)
    selected_names = set(info[0] for info in ministry_info.values())
    all_results = [a for a in unique if a["ministry"] in selected_names]

    # 신규만 상세 수집
    seen      = load_seen()
    new_items = [a for a in all_results if a["id"] not in seen]
    if new_items:
        print(f"\n  신규 {len(new_items)}건 상세 정보 수집 중...")
        for idx, item in enumerate(new_items):
            tab_arg = "ancmIng" if item["status"] == "접수중" else "ancmPre"
            print(f"    [{idx+1}/{len(new_items)}] {item['title'][:40]}...")
            fetch_detail(session, item, tab_arg)
            time.sleep(0.2)

    return all_results

# ── 로컬 서버 ─────────────────────────────────────────────────
class IrisHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/favicon.ico", "/robots.txt"):
            self.send_response(204)
            self.end_headers()
            return

        if parsed.path == "/report":
            try:
                html_files = sorted(OUTPUT_DIR.glob("iris_*.html"), reverse=True)
                if html_files:
                    content = html_files[0].read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(content)
                else:
                    self.send_response(404)
                    self.end_headers()
            except Exception:
                self.send_response(500)
                self.end_headers()
            return

        if parsed.path == "/":
            params  = parse_qs(parsed.query)
            ann_id  = unquote(params.get("ann_id", [""])[0])
            tab_arg = unquote(params.get("tab",    ["ancmIng"])[0])
            title   = unquote(params.get("title",  [""])[0])

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write("ok".encode("utf-8"))

            if ann_id or title:
                threading.Thread(
                    target=open_iris_browser,
                    args=(ann_id, tab_arg, title),
                    daemon=True
                ).start()
            return

        self.send_response(204)
        self.end_headers()

def open_iris_browser(ann_id: str, tab_arg: str, title: str):
    """Chrome으로 IRIS 공고 상세페이지 열기 (GET URL 방식)"""
    import subprocess, os

    if ann_id:
        # GET 방식으로 바로 상세페이지 열기
        url = f"{BASE_URL}/contents/retrieveBsnsAncmView.do?ancmId={ann_id}&ancmPrg={tab_arg}"
    else:
        # ann_id 없으면 목록 페이지 + 공고명 클립보드 복사
        url = TARGET_URL
        if title:
            try:
                subprocess.run(
                    ['powershell', '-command', f'Set-Clipboard -Value "{title}"'],
                    creationflags=0x08000000, capture_output=True
                )
            except Exception:
                pass

    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    chrome_exe = next((p for p in chrome_paths if os.path.exists(p)), None)

    if chrome_exe:
        subprocess.Popen([chrome_exe, "--new-window", url])
    else:
        subprocess.Popen(f'start "" "{url}"', shell=True, creationflags=0x08000000)

def start_server():
    server = HTTPServer(("localhost", SERVER_PORT), IrisHandler)
    server.serve_forever()

# ── 부처 선택 메뉴 ─────────────────────────────────────────────
def select_ministries(auto: bool = False) -> dict:
    saved = []
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f).get("selected", [])

    if auto and saved:
        selected_info = {val: (name, color) for k, (name, val, color) in ALL_MINISTRIES.items() if val in saved}
        print(f"  자동 실행: {', '.join(v[0] for v in selected_info.values())}")
        return selected_info

    print("\n" + "=" * 50)
    print("  모니터링할 부처를 선택하세요")
    print("=" * 50)
    for key, (name, val, _) in ALL_MINISTRIES.items():
        mark = "✓" if val in saved else " "
        print(f"  [{key:>2}] [{mark}] {name}")
    print("-" * 50)
    print("  번호를 쉼표로 구분해서 입력하세요.")
    if saved:
        saved_names = [ALL_MINISTRIES[k][0] for k in ALL_MINISTRIES if ALL_MINISTRIES[k][1] in saved]
        print(f"  Enter 그냥 누르면 이전 설정 유지: {', '.join(saved_names)}")
    print("  예) 1,2,3  또는  1 2 3")
    print("=" * 50)

    while True:
        raw = input("  선택 > ").strip()
        if not raw and saved:
            selected_info = {val: (name, color) for k, (name, val, color) in ALL_MINISTRIES.items() if val in saved}
            print(f"\n  이전 설정 유지: {', '.join(v[0] for v in selected_info.values())}")
            return selected_info
        keys = re.split(r"[,\s]+", raw)
        invalid = [k for k in keys if k not in ALL_MINISTRIES]
        if invalid:
            print(f"  [오류] 잘못된 번호: {', '.join(invalid)}")
            continue
        selected_info = {ALL_MINISTRIES[k][1]: (ALL_MINISTRIES[k][0], ALL_MINISTRIES[k][2]) for k in keys}
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"selected": list(selected_info.keys())}, f, ensure_ascii=False)
        print(f"\n  선택됨: {', '.join(v[0] for v in selected_info.values())}")
        return selected_info

# ── HTML 리포트 ────────────────────────────────────────────────
def make_html(items: list[dict], ministry_info: dict, label: str, now_str: str) -> str:
    name_to_color = {info[0]: info[1] for info in ministry_info.values()}
    name_to_color["기타"] = "#777"

    groups: dict[str, list] = {}
    for item in items:
        groups.setdefault(item["ministry"], []).append(item)

    order = [info[0] for info in ministry_info.values()] + ["기타"]
    sorted_groups = [(m, groups[m]) for m in order if m in groups]

    cards_html = ""
    for ministry, mitems in sorted_groups:
        color  = name_to_color.get(ministry, "#777")
        sec_id = ministry.replace(" ", "_")

        pre_items = [x for x in mitems if x["status"] == "접수예정"]
        ing_items = [x for x in mitems if x["status"] == "접수중"]

        def make_cards(card_items):
            html = ""
            for item in card_items:
                status_cls = "badge-ing" if item["status"] == "접수중" else "badge-pre"
                period     = item.get("receipt_period", "") or item.get("announce_date", "")
                tab_arg    = "ancmIng" if item["status"] == "접수중" else "ancmPre"
                ann_id_js  = item.get("ann_id", "")
                title_js   = item["title"].replace("'", "\\'").replace('"', '&quot;')

                attach_html = ""
                for att in item.get("attachments", []):
                    fname   = att.get("name", "")
                    ext     = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                    ext_cls = f"ext-{ext}" if ext in ["hwp","hwpx","pdf","zip","xlsx","xls","docx"] else "ext-other"
                    attach_html += f'<a class="attach-link {ext_cls}" href="#" onclick="openIris(\'{ann_id_js}\',\'{tab_arg}\',\'{title_js}\');return false;">📎 {fname}</a>'
                attach_section = f'<div class="attachments">{attach_html}</div>' if attach_html else '<div class="no-attach">첨부파일 없음</div>'

                html += f"""
                <div class="card">
                  <div class="card-header">
                    <span class="badge {status_cls}">{item['status']}</span>
                    <span class="card-type">{item['type']}</span>
                  </div>
                  <div class="card-title">{item['title']}</div>
                  <div class="card-meta">
                    <span>📋 {item['announce_num']}</span>
                    <span>🏢 {item['agency']}</span>
                  </div>
                  <div class="card-period">
                    <span class="period-label">📅 접수기간</span>
                    <span class="period-value">{period}</span>
                  </div>
                  {attach_section}
                  <div class="card-footer">
                    <a class="btn-iris" href="#" onclick="openIris('{ann_id_js}','{tab_arg}','{title_js}');return false;">🔗 IRIS에서 보기</a>
                  </div>
                </div>"""
            return html

        pre_html = make_cards(pre_items)
        ing_html = make_cards(ing_items)

        cards_html += f"""
        <div class="ministry-section" id="{sec_id}">
          <div class="ministry-header" style="background:{color}">
            <span class="ministry-name">{ministry}</span>
            <span class="ministry-count">{len(mitems)}건</span>
          </div>
          <div class="status-tabs">
            <button class="status-tab active" onclick="switchTab(this, '{sec_id}_pre')">
              접수예정 <span class="tab-count">{len(pre_items)}</span>
            </button>
            <button class="status-tab" onclick="switchTab(this, '{sec_id}_ing')">
              접수중 <span class="tab-count">{len(ing_items)}</span>
            </button>
          </div>
          <div id="{sec_id}_pre" class="tab-panel">
            {'<div class="cards">' + pre_html + '</div>' if pre_items else '<div class="no-items">해당 공고 없음</div>'}
          </div>
          <div id="{sec_id}_ing" class="tab-panel" style="display:none">
            {'<div class="cards">' + ing_html + '</div>' if ing_items else '<div class="no-items">해당 공고 없음</div>'}
          </div>
        </div>"""

    total   = len(items)
    summary = " &nbsp;|&nbsp; ".join(
        f'<a href="#{m.replace(" ","_")}" class="summary-link" style="color:{name_to_color.get(m,"#777")}">{m} {len(lst)}건</a>'
        for m, lst in sorted_groups
    )
    min_names = ", ".join(info[0] for info in ministry_info.values())
    port      = SERVER_PORT

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>IRIS 공고 - {now_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Malgun Gothic',sans-serif;background:#f4f6f9;color:#222}}
.top-bar{{background:#1e3a5f;color:white;padding:16px 24px;position:sticky;top:0;z-index:100}}
.top-bar h1{{font-size:18px;font-weight:700}}
.top-bar .meta{{font-size:13px;margin-top:4px;opacity:.85}}
.summary{{background:white;padding:12px 24px;border-bottom:1px solid #ddd;font-size:14px;position:sticky;z-index:99;box-shadow:0 2px 4px rgba(0,0,0,.08)}}
.summary .total{{font-weight:700;margin-right:16px}}
.summary-link{{font-weight:700;text-decoration:none;padding:3px 8px;border-radius:4px}}
.summary-link:hover{{background:#f0f0f0;text-decoration:underline}}
.container{{max-width:1200px;margin:24px auto;padding:0 16px}}
.ministry-section{{margin-bottom:32px}}
.ministry-header{{display:flex;align-items:center;justify-content:space-between;padding:10px 18px;border-radius:8px 8px 0 0;color:white}}
.ministry-name{{font-size:16px;font-weight:700}}
.ministry-count{{font-size:13px;background:rgba(255,255,255,.25);padding:2px 10px;border-radius:12px}}
.status-tabs{{display:flex;background:#f0f2f5;border-left:1px solid #ddd;border-right:1px solid #ddd}}
.status-tab{{flex:1;padding:8px;border:none;background:none;cursor:pointer;font-size:13px;font-weight:600;color:#888;border-bottom:2px solid transparent;font-family:'Malgun Gothic',sans-serif}}
.status-tab.active{{color:#1e3a5f;border-bottom:2px solid #1e3a5f;background:white}}
.status-tab:hover{{background:#e8eaf0}}
.tab-count{{display:inline-block;background:#e0e0e0;color:#555;font-size:11px;padding:1px 6px;border-radius:10px;margin-left:4px}}
.status-tab.active .tab-count{{background:#1e3a5f;color:white}}
.tab-panel{{background:#f9fafc;border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px}}
.no-items{{padding:24px;text-align:center;color:#aaa;font-size:14px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;padding:16px}}
.card{{background:white;border-radius:8px;padding:16px;box-shadow:0 1px 4px rgba(0,0,0,.08);border:1px solid #e8eaf0;display:flex;flex-direction:column;gap:8px}}
.card-header{{display:flex;align-items:center;gap:8px}}
.badge{{font-size:11px;font-weight:700;padding:3px 8px;border-radius:4px}}
.badge-ing{{background:#e8f5e9;color:#2e7d32}}
.badge-pre{{background:#e3f2fd;color:#1565c0}}
.card-type{{font-size:11px;color:#888}}
.card-title{{font-size:14px;font-weight:700;line-height:1.5;color:#1a1a2e}}
.card-meta{{font-size:12px;color:#666;display:flex;flex-direction:column;gap:3px}}
.card-period{{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#fff8e1;border-radius:6px;border-left:3px solid #ffa000}}
.period-label{{font-size:11px;font-weight:700;color:#e65100;white-space:nowrap}}
.period-value{{font-size:13px;font-weight:600;color:#333}}
.attachments{{display:flex;flex-direction:column;gap:4px}}
.attach-link{{font-size:12px;padding:4px 8px;border-radius:4px;text-decoration:none;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}}
.ext-hwp,.ext-hwpx{{background:#e8eaf6;color:#3949ab}}
.ext-pdf{{background:#fce4ec;color:#c62828}}
.ext-zip{{background:#f3e5f5;color:#6a1b9a}}
.ext-xlsx,.ext-xls{{background:#e8f5e9;color:#2e7d32}}
.ext-docx{{background:#e3f2fd;color:#1565c0}}
.ext-other{{background:#f5f5f5;color:#555}}
.no-attach{{font-size:12px;color:#aaa}}
.card-footer{{text-align:right;margin-top:auto}}
.btn-iris{{font-size:12px;color:#1a6fc4;text-decoration:none;font-weight:600;cursor:pointer}}
.btn-iris:hover{{text-decoration:underline}}
.no-new{{text-align:center;padding:60px;color:#888;font-size:16px}}
#status-msg{{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#1e3a5f;color:white;padding:16px 28px;border-radius:12px;font-size:15px;z-index:9999;box-shadow:0 4px 20px rgba(0,0,0,.3);text-align:center}}
</style>
</head>
<body>
<div class="top-bar">
  <h1>📋 IRIS 사업공고 모니터링 — {label}</h1>
  <div class="meta">생성: {now_str} &nbsp;|&nbsp; 대상 부처: {min_names}</div>
</div>
<div class="summary">
  <span class="total">총 {total}건</span>{summary}
</div>
<div id="status-msg"></div>
<div class="container">
  {'<div class="no-new">✅ 신규 공고가 없습니다.</div>' if total==0 else cards_html}
</div>
<script>
window.addEventListener('load', function() {{
  var topBar = document.querySelector('.top-bar');
  var summary = document.querySelector('.summary');
  if (topBar && summary) summary.style.top = topBar.offsetHeight + 'px';
}});
function switchTab(btn, panelId) {{
  var section = btn.closest('.ministry-section');
  section.querySelectorAll('.status-tab').forEach(function(t){{ t.classList.remove('active'); }});
  section.querySelectorAll('.tab-panel').forEach(function(p){{ p.style.display='none'; }});
  btn.classList.add('active');
  document.getElementById(panelId).style.display = 'block';
}}
var _irisLoading = false;
function openIris(annId, tabArg, title) {{
  if (_irisLoading) return;
  _irisLoading = true;
  var msg = document.getElementById('status-msg');
  msg.innerHTML = '🔄 IRIS 공고 여는 중...';
  msg.style.display = 'block';
  var url = 'http://localhost:{port}/?ann_id=' + encodeURIComponent(annId) + '&tab=' + tabArg + '&title=' + encodeURIComponent(title);
  fetch(url)
    .then(function() {{
      setTimeout(function() {{ msg.style.display='none'; _irisLoading=false; }}, 2000);
    }})
    .catch(function() {{
      msg.innerHTML = '⚠️ 서버 연결 실패. iris_monitor.py 가 실행 중인지 확인하세요.';
      setTimeout(function() {{ msg.style.display='none'; _irisLoading=false; }}, 3000);
    }});
}}
</script>
</body>
</html>"""

# ── 메인 ──────────────────────────────────────────────────────
def main():
    auto    = "--auto" in sys.argv
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    print("=" * 50)
    print("  IRIS 사업공고 모니터링")
    print("=" * 50)

    ministry_info = select_ministries(auto=auto)
    if not ministry_info:
        print("\n[오류] 저장된 부처 설정이 없습니다. python iris_monitor.py 로 먼저 부처를 선택해주세요.")
        return

    seen      = load_seen()
    first_run = len(seen) == 0
    announcements = fetch_all(ministry_info)

    if not announcements:
        print("\n[결과] 수집된 공고가 없습니다.")
        return

    all_ids = {a["id"] for a in announcements}

    if first_run:
        new_items = announcements
        print(f"\n[최초 실행] {len(announcements)}건 저장. 다음부터 신규만 표시됩니다.")
        save_seen(all_ids)
        label = "최초 전체 목록"
    else:
        new_items = [a for a in announcements if a["id"] not in seen]
        print(f"\n전체: {len(announcements)}건  |  신규: {len(new_items)}건")
        save_seen(seen | all_ids)
        label = f"신규 공고 {len(new_items)}건"

    html  = make_html(new_items, ministry_info, label, now_str)
    fname = OUTPUT_DIR / f"iris_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    fname.write_text(html, encoding="utf-8")
    print(f"\n[저장] {fname}")

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    print(f"[서버] localhost:{SERVER_PORT} 실행 중...")

    if new_items:
        show_toast(
            f"📋 IRIS 신규 공고 {len(new_items)}건",
            f"{', '.join(info[0] for info in ministry_info.values())} 공고가 있습니다."
        )
        import subprocess
        subprocess.Popen(
            f'start "" "http://localhost:{SERVER_PORT}/report"',
            shell=True, creationflags=0x08000000
        )
        print(f"  신규 공고 {len(new_items)}건 — http://localhost:{SERVER_PORT}/report")
    else:
        print("  신규 공고 없음.")

    print("[완료] 이 창을 닫으면 서버도 종료됩니다. 열어두세요.")
    print("=" * 50)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n종료합니다.")

if __name__ == "__main__":
    main()
