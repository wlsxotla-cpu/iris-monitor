"""
IRIS 사업공고 모니터링 - Streamlit 앱 (안정 버전)
"""
import streamlit as st
import requests
from bs4 import BeautifulSoup
import re, time
from datetime import datetime, timedelta

BASE_URL   = "https://www.iris.go.kr"
TARGET_URL = f"{BASE_URL}/contents/retrieveBsnsAncmBtinSituListView.do"
VIEW_URL   = f"{BASE_URL}/contents/retrieveBsnsAncmView.do"

ALL_MINISTRIES = {
    "과학기술정보통신부": ("AR4001", "#1a6fc4"),
    "산업통상부":         ("AR4002", "#c45c1a"),
    "중소벤처기업부":     ("AR4003", "#1a9e52"),
    "국토교통부":         ("AR4004", "#7b1fa2"),
    "교육부":             ("AR4005", "#00796b"),
    "보건복지부":         ("AR4012", "#c62828"),
    "산림청":             ("AR4013", "#2e7d32"),
    "해양수산부":         ("AR4016", "#0277bd"),
    "소방청":             ("AR4915", "#b71c1c"),
    "범부처":             ("AR4999", "#455a64"),
}

TABS = {"접수예정": "ancmPre", "접수중": "ancmIng"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": TARGET_URL,
    "Origin": "https://www.iris.go.kr",
}

def fetch_list_page(session, tab_arg, ministry_val, page_index):
    payload = [
        ("bizSearch",""),("bsnsTl",""),("ancmPrg",tab_arg),
        ("pageIndex",str(page_index)),("ancmId",""),("ancmNo",""),
        ("ancmTurn",""),("seq",""),("hirkSorgnBsnsCd",""),
        ("bsnsAncmTap",""),("shSorgnYyBsnsCd",""),("sorgnIdArr",""),
        ("ancmSttArr",""),("pbofrTpArr",""),("qualCndtArr",""),
        ("techFildArr",""),("shBsnsYy",""),
        ("blngGovdSeArr", ministry_val),
    ]
    try:
        resp = session.post(TARGET_URL, data=payload, headers=HEADERS, timeout=30)
        resp.encoding = "utf-8"
        html = resp.text
    except Exception:
        return [], False

    soup  = BeautifulSoup(html, "html.parser")
    items = []
    ministry_name = next((k for k, v in ALL_MINISTRIES.items() if v[0] == ministry_val), "기타")

    for el in soup.find_all(["dl","li"]):
        text = el.get_text()
        if "공고번호" not in text:
            continue
        if "공고번호" in " ".join(c.get_text() for c in el.find_all(["dl","li"])):
            continue
        link = el.find("a")
        if not link:
            continue

        title   = link.get_text().strip()
        onclick = link.get("onclick","")
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

        if not title:
            continue

        items.append({
            "id": ann_id or f"{title}_{ann_date}",
            "ann_id": ann_id, "title": title,
            "ministry": ministry_name, "agency": agency,
            "announce_num": ann_num, "announce_date": ann_date,
            "type": ann_type,
            "status": "접수예정" if tab_arg == "ancmPre" else "접수중",
            "receipt_period": "", "attachments": [],
        })

    return items, soup.find("a", title=f"{page_index+1}페이지") is not None

def fetch_detail(session, item):
    if not item["ann_id"]:
        return
    tab_arg = "ancmIng" if item["status"] == "접수중" else "ancmPre"
    try:
        resp = session.post(VIEW_URL, data=[
            ("ancmId", item["ann_id"]), ("ancmPrg", tab_arg), ("pageIndex","")
        ], headers=HEADERS, timeout=30)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        for li in soup.find_all("li", class_="write"):
            strong = li.find("strong")
            if strong and "접수기간" in strong.get_text():
                span = li.find("span")
                if span:
                    item["receipt_period"] = span.get_text().strip()
                break
        item["attachments"] = [
            {"name": re.sub(r'\s*\(\d+[\d.]*\s*[KMG]B\)\s*$','',a.get_text().strip()).strip()}
            for a in soup.find_all("a", href=re.compile("downloadAtchFile"))
            if a.get_text().strip()
        ]
    except Exception:
        pass

@st.cache_data(ttl=300, show_spinner=False)
def fetch_all(ministry_vals, tab_keys):
    session = requests.Session()
    session.get(TARGET_URL, headers={
        "User-Agent": HEADERS["User-Agent"],
        "Accept": HEADERS["Accept"],
        "Accept-Language": HEADERS["Accept-Language"],
    }, timeout=30)

    all_results = []
    for tab_arg in tab_keys:
        for val in ministry_vals:
            page_num = 1
            while True:
                items, has_next = fetch_list_page(session, tab_arg, val, page_num)
                all_results.extend(items)
                if has_next:
                    page_num += 1
                    time.sleep(0.2)
                else:
                    break

    seen_uid, unique = set(), []
    for r in all_results:
        if r["id"] not in seen_uid:
            seen_uid.add(r["id"])
            unique.append(r)

    for item in unique:
        fetch_detail(session, item)
        time.sleep(0.15)

    return unique

# ── UI ────────────────────────────────────────────────────────
st.set_page_config(page_title="IRIS 사업공고 모니터링", page_icon="📋", layout="wide")

st.markdown("""
<style>
.period-box {
    background: #fff8e1; border-left: 3px solid #ffa000;
    padding: 6px 10px; border-radius: 4px; margin: 6px 0;
    font-size: 13px; color: #333 !important;
}
.ministry-header {
    padding: 10px 16px; border-radius: 8px 8px 0 0;
    color: white; font-size: 16px; font-weight: 700;
    display: flex; justify-content: space-between; align-items: center;
    margin-top: 16px;
}
</style>
""", unsafe_allow_html=True)

st.title("📋 IRIS 사업공고 모니터링")

with st.sidebar:
    st.header("⚙️ 설정")

    selected_names = st.multiselect(
        "모니터링할 부처",
        options=list(ALL_MINISTRIES.keys()),
        default=["과학기술정보통신부", "산업통상부", "중소벤처기업부"],
    )

    tab_options = st.multiselect(
        "조회 탭",
        options=["접수예정", "접수중"],
        default=["접수예정", "접수중"],
    )

    new_days = st.slider("신규 기준 (최근 N일 이내)", 1, 30, 3)

    st.divider()
    if st.button("🔄 공고 조회", type="primary", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"마지막 조회: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

if not selected_names:
    st.info("왼쪽에서 부처를 선택해주세요.")
    st.stop()

if not tab_options:
    st.info("조회할 탭을 선택해주세요.")
    st.stop()

selected_vals = tuple(ALL_MINISTRIES[n][0] for n in selected_names)
tab_args      = tuple(TABS[t] for t in tab_options)

with st.spinner("🔄 IRIS 공고 수집 중..."):
    results = fetch_all(selected_vals, tab_args)

cutoff    = (datetime.now() - timedelta(days=new_days)).strftime("%Y-%m-%d")
new_items = [a for a in results if a.get("announce_date","") >= cutoff]

st.success(f"총 {len(results)}건 | 최근 {new_days}일 신규 {len(new_items)}건")

tab_new, tab_all = st.tabs([
    f"🆕 신규 ({len(new_items)}건)",
    f"📋 전체 ({len(results)}건)"
])

def render_items(items):
    if not items:
        st.info("공고가 없습니다.")
        return

    groups = {}
    for item in items:
        groups.setdefault(item["ministry"], []).append(item)

    for ministry in list(ALL_MINISTRIES.keys()) + ["기타"]:
        if ministry not in groups:
            continue
        mitems = groups[ministry]
        color  = ALL_MINISTRIES.get(ministry, ("","#777"))[1]

        st.markdown(f"""
        <div class="ministry-header" style="background:{color}">
            <span>{ministry}</span>
            <span style="background:rgba(255,255,255,.25);padding:2px 10px;border-radius:12px;font-size:13px">{len(mitems)}건</span>
        </div>
        """, unsafe_allow_html=True)

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
                        is_new = item.get("announce_date","") >= cutoff
                        st.markdown(f"**{'🆕 ' if is_new else ''}{item['title']}**")
                        st.caption(f"📋 {item['announce_num']}")
                        st.caption(f"🏢 {item['agency']}")

                        if item.get("receipt_period"):
                            st.markdown(f'<div class="period-box">📅 접수기간: <b>{item["receipt_period"]}</b></div>', unsafe_allow_html=True)
                        elif item.get("announce_date"):
                            st.markdown(f'<div class="period-box">📅 공고일: <b>{item["announce_date"]}</b></div>', unsafe_allow_html=True)

                        ann_id   = item.get("ann_id","")
                        tab_arg  = "ancmIng" if item["status"]=="접수중" else "ancmPre"
                        iris_url = f"{BASE_URL}/contents/retrieveBsnsAncmView.do?ancmId={ann_id}&ancmPrg={tab_arg}"

                        if item.get("attachments"):
                            for att in item["attachments"]:
                                st.markdown(f"📎 [{att['name']}]({iris_url})")
                        else:
                            st.caption("첨부파일 없음")

                        st.markdown(f"[🔗 IRIS에서 보기]({iris_url})")
                        st.divider()

with tab_new:
    render_items(new_items)
with tab_all:
    render_items(results)
