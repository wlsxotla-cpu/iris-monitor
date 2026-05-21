"""
IRIS 사업공고 모니터링 - Streamlit 앱
"""
import streamlit as st
import json, time, re, threading
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── 설정 ──────────────────────────────────────────────────────
BASE_URL   = "https://www.iris.go.kr"
TARGET_URL = f"{BASE_URL}/contents/retrieveBsnsAncmBtinSituListView.do"
VIEW_URL   = f"{BASE_URL}/contents/retrieveBsnsAncmView.do"

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

TABS = {"접수예정": "ancmPre", "접수중": "ancmIng"}

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

def load_config() -> list:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("selected", [])
    return []

def save_config(vals: list):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"selected": vals}, f, ensure_ascii=False)

# ── 수집 함수 ──────────────────────────────────────────────────
def fetch_list_page(session, tab_arg, ministry_val, page_index):
    payload_list = [
        ("bizSearch", ""), ("bsnsTl", ""), ("ancmPrg", tab_arg),
        ("pageIndex", str(page_index)), ("ancmId", ""), ("ancmNo", ""),
        ("ancmTurn", ""), ("seq", ""), ("hirkSorgnBsnsCd", ""),
        ("bsnsAncmTap", ""), ("shSorgnYyBsnsCd", ""), ("sorgnIdArr", ""),
        ("ancmSttArr", ""), ("pbofrTpArr", ""), ("qualCndtArr", ""),
        ("techFildArr", ""), ("shBsnsYy", ""),
        ("blngGovdSeArr", ministry_val),
    ]
    try:
        resp = session.post(TARGET_URL, data=payload_list, headers=HEADERS, timeout=30)
        resp.encoding = "utf-8"
        html = resp.text
    except Exception as e:
        return [], False

    soup  = BeautifulSoup(html, "html.parser")
    items = []
    ministry_name = next((ALL_MINISTRIES[k][0] for k in ALL_MINISTRIES if ALL_MINISTRIES[k][1] == ministry_val), "기타")

    for el in soup.find_all(["dl", "li"]):
        text = el.get_text()
        if "공고번호" not in text:
            continue
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

        num_m    = re.search(r"공고번호\s*[:\s]*([\S ]+?)(?:\s*공고일자|\n)", text)
        ann_num  = num_m.group(1).strip() if num_m else ""
        date_m   = re.search(r"공고일자\s*[:\s]*(\d{4}-\d{2}-\d{2})", text)
        ann_date = date_m.group(1) if date_m else ""
        type_m   = re.search(r"공모유형\s*[:\s]*([\S ,]+?)(?:\s*\n|접수|$)", text)
        ann_type = type_m.group(1).strip() if type_m else ""
        agency   = ""
        arrow_m  = re.search(r"[^\n>]+>\s*([^\n]+)", text)
        if arrow_m:
            agency = arrow_m.group(1).strip().split("\n")[0].strip()

        uid = ann_id if ann_id else f"{title}_{ann_date}"
        if not title:
            continue

        items.append({
            "id": uid, "ann_id": ann_id, "title": title,
            "ministry": ministry_name, "agency": agency,
            "announce_num": ann_num, "announce_date": ann_date,
            "type": ann_type,
            "status": "접수예정" if tab_arg == "ancmPre" else "접수중",
            "receipt_period": "", "attachments": [],
        })

    next_page = soup.find("a", title=f"{page_index + 1}페이지")
    return items, next_page is not None

def fetch_detail(session, item):
    ann_id  = item["ann_id"]
    tab_arg = "ancmIng" if item["status"] == "접수중" else "ancmPre"
    if not ann_id:
        return
    payload = [("ancmId", ann_id), ("ancmPrg", tab_arg), ("pageIndex", "")]
    try:
        resp = session.post(VIEW_URL, data=payload, headers=HEADERS, timeout=30)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        for li in soup.find_all("li", class_="write"):
            strong = li.find("strong")
            if strong and "접수기간" in strong.get_text():
                span = li.find("span")
                if span:
                    item["receipt_period"] = span.get_text().strip()
                break
        attachments = []
        for a in soup.find_all("a", href=re.compile("downloadAtchFile")):
            fname = re.sub(r'\s*\(\d+[\d.]*\s*[KMG]B\)\s*$', '', a.get_text().strip()).strip()
            if fname:
                attachments.append({"name": fname})
        item["attachments"] = attachments
    except Exception:
        pass

def fetch_all(ministry_vals, progress_cb=None):
    session = requests.Session()
    session.get(TARGET_URL, headers={"User-Agent": HEADERS["User-Agent"],
                                      "Accept": HEADERS["Accept"]}, timeout=30)
    all_results = []
    for tab_name, tab_arg in TABS.items():
        for val in ministry_vals:
            page_num = 1
            while True:
                items, has_next = fetch_list_page(session, tab_arg, val, page_num)
                all_results.extend(items)
                if progress_cb:
                    progress_cb(tab_name, val, page_num, len(items))
                if has_next:
                    page_num += 1
                    time.sleep(0.2)
                else:
                    break

    # 중복 제거
    seen_uid, unique = set(), []
    for r in all_results:
        if r["id"] not in seen_uid:
            seen_uid.add(r["id"])
            unique.append(r)

    # 신규만 상세 수집
    seen      = load_seen()
    new_items = [a for a in unique if a["id"] not in seen]
    if new_items:
        for item in new_items:
            fetch_detail(session, item)
            time.sleep(0.2)

    return unique

# ── Streamlit UI ───────────────────────────────────────────────
st.set_page_config(page_title="IRIS 사업공고 모니터링", page_icon="📋", layout="wide")

st.markdown("""
<style>
.ministry-header {
    padding: 10px 16px;
    border-radius: 8px 8px 0 0;
    color: white;
    font-size: 16px;
    font-weight: 700;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.card {
    background: white;
    border-radius: 8px;
    padding: 14px;
    border: 1px solid #e8eaf0;
    margin-bottom: 12px;
    box-shadow: 0 1px 4px rgba(0,0,0,.06);
}
.period-box {
    background: #fff8e1;
    border-left: 3px solid #ffa000;
    padding: 6px 10px;
    border-radius: 4px;
    margin: 6px 0;
    font-size: 13px;
}
</style>
""", unsafe_allow_html=True)

st.title("📋 IRIS 사업공고 모니터링")

# ── 사이드바: 설정 ─────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 설정")

    saved_vals = load_config()
    ministry_options = {f"{v[0]}": k for k, v in ALL_MINISTRIES.items()}
    saved_names = [ALL_MINISTRIES[k][0] for k in ALL_MINISTRIES if ALL_MINISTRIES[k][1] in saved_vals]

    selected_names = st.multiselect(
        "모니터링할 부처",
        options=[v[0] for v in ALL_MINISTRIES.values()],
        default=saved_names,
    )
    selected_vals = [ALL_MINISTRIES[k][1] for k in ALL_MINISTRIES if ALL_MINISTRIES[k][0] in selected_names]

    if st.button("설정 저장"):
        save_config(selected_vals)
        st.success("저장됐습니다!")

    st.divider()

    seen = load_seen()
    st.metric("기존 공고 수", len(seen))

    if st.button("🗑️ seen 초기화 (전체 재수집)"):
        SEEN_FILE.unlink(missing_ok=True)
        st.success("초기화됐습니다!")
        st.rerun()

    st.divider()
    if st.button("🔄 공고 수집 시작", type="primary", use_container_width=True):
        if not selected_vals:
            st.error("부처를 선택해주세요.")
        else:
            save_config(selected_vals)
            st.session_state["run_fetch"] = True
            st.session_state["fetch_vals"] = selected_vals
            st.rerun()

# ── 수집 실행 ──────────────────────────────────────────────────
if st.session_state.get("run_fetch"):
    st.session_state["run_fetch"] = False
    vals = st.session_state.get("fetch_vals", [])

    with st.status("🔄 IRIS 공고 수집 중...", expanded=True) as status:
        logs = []

        def progress_cb(tab_name, val, page_num, count):
            name = next((ALL_MINISTRIES[k][0] for k in ALL_MINISTRIES if ALL_MINISTRIES[k][1] == val), val)
            msg = f"  {tab_name} | {name} | 페이지 {page_num}: {count}건"
            st.write(msg)
            logs.append(msg)

        results = fetch_all(vals, progress_cb)

        seen      = load_seen()
        first_run = len(seen) == 0
        all_ids   = {a["id"] for a in results}

        if first_run:
            new_items = results
            save_seen(all_ids)
            label = f"최초 전체 목록 {len(results)}건"
        else:
            new_items = [a for a in results if a["id"] not in seen]
            save_seen(seen | all_ids)
            label = f"신규 공고 {len(new_items)}건"

        st.session_state["results"]   = results
        st.session_state["new_items"] = new_items
        st.session_state["label"]     = label
        status.update(label=f"✅ 완료 — {label}", state="complete")

# ── 결과 표시 ──────────────────────────────────────────────────
results   = st.session_state.get("results", [])
new_items = st.session_state.get("new_items", [])
label     = st.session_state.get("label", "")

if not results:
    st.info("왼쪽 사이드바에서 부처를 선택하고 '공고 수집 시작'을 눌러주세요.")
else:
    # 탭: 신규 / 전체
    tab_new, tab_all = st.tabs([f"🆕 신규 공고 ({len(new_items)}건)", f"📋 전체 공고 ({len(results)}건)"])

    def render_items(items):
        if not items:
            st.info("공고가 없습니다.")
            return

        # 부처별 그룹
        groups = {}
        for item in items:
            groups.setdefault(item["ministry"], []).append(item)

        ministry_order = [ALL_MINISTRIES[k][0] for k in ALL_MINISTRIES] + ["기타"]
        for ministry in ministry_order:
            if ministry not in groups:
                continue
            mitems = groups[ministry]
            color  = next((ALL_MINISTRIES[k][2] for k in ALL_MINISTRIES if ALL_MINISTRIES[k][0] == ministry), "#777")

            st.markdown(f"""
            <div class="ministry-header" style="background:{color}">
                <span>{ministry}</span>
                <span style="background:rgba(255,255,255,.25);padding:2px 10px;border-radius:12px;font-size:13px">{len(mitems)}건</span>
            </div>
            """, unsafe_allow_html=True)

            # 접수예정 / 접수중 탭
            pre = [x for x in mitems if x["status"] == "접수예정"]
            ing = [x for x in mitems if x["status"] == "접수중"]

            t1, t2 = st.tabs([f"접수예정 {len(pre)}건", f"접수중 {len(ing)}건"])

            for tab_obj, tab_items in [(t1, pre), (t2, ing)]:
                with tab_obj:
                    if not tab_items:
                        st.caption("해당 공고 없음")
                        continue
                    cols = st.columns(3)
                    for i, item in enumerate(tab_items):
                        with cols[i % 3]:
                            status_color = "🟢" if item["status"] == "접수중" else "🔵"
                            st.markdown(f"**{status_color} {item['title']}**")
                            st.caption(f"📋 {item['announce_num']}")
                            st.caption(f"🏢 {item['agency']}")
                            if item.get("receipt_period"):
                                st.markdown(f"""<div class="period-box">📅 접수기간: <b>{item['receipt_period']}</b></div>""", unsafe_allow_html=True)
                            elif item.get("announce_date"):
                                st.markdown(f"""<div class="period-box">📅 공고일: <b>{item['announce_date']}</b></div>""", unsafe_allow_html=True)

                            # 첨부파일
                            if item.get("attachments"):
                                for att in item["attachments"]:
                                    fname = att.get("name", "")
                                    ann_id  = item.get("ann_id", "")
                                    tab_arg = "ancmIng" if item["status"] == "접수중" else "ancmPre"
                                    url = f"{BASE_URL}/contents/retrieveBsnsAncmView.do?ancmId={ann_id}&ancmPrg={tab_arg}"
                                    st.markdown(f"📎 [{fname}]({url})", unsafe_allow_html=False)
                            else:
                                st.caption("첨부파일 없음")

                            ann_id  = item.get("ann_id", "")
                            tab_arg = "ancmIng" if item["status"] == "접수중" else "ancmPre"
                            iris_url = f"{BASE_URL}/contents/retrieveBsnsAncmView.do?ancmId={ann_id}&ancmPrg={tab_arg}"
                            st.markdown(f"[🔗 IRIS에서 보기]({iris_url})")
                            st.divider()

    with tab_new:
        render_items(new_items)
    with tab_all:
        render_items(results)
