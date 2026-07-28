import streamlit as st
import time
import re
import json
import requests
from bs4 import BeautifulSoup

st.set_page_config(page_title="비드큐 AI 초정밀 입찰 분석기", page_icon="🎯", layout="centered")

with st.sidebar:
    st.header("⚙️ 시스템 설정")
    gemini_key = st.text_input(
        "Gemini API Key",
        value=st.secrets.get("GEMINI_API_KEY", ""),
        type="password",
        placeholder="구글 API 키를 입력하세요"
    )
    bidq_id = st.text_input("비드큐 아이디", value=st.secrets.get("BIDQ_ID", ""))
    bidq_pw = st.text_input("비드큐 비밀번호", value=st.secrets.get("BIDQ_PW", ""), type="password")

    st.markdown("---")
    MODEL_OPTIONS = {
        "gemini-2.5-flash (균형, 추천)": "gemini-2.5-flash",
        "gemini-2.5-flash-lite (가장 저렴/빠름)": "gemini-2.5-flash-lite",
        "gemini-3.5-flash (가장 강력)": "gemini-3.5-flash",
        "gemini-flash-latest (항상 최신으로 자동 전환)": "gemini-flash-latest",
    }
    model_label = st.selectbox("사용할 Gemini 모델", list(MODEL_OPTIONS.keys()), index=0)
    selected_model = MODEL_OPTIONS[model_label]

BASE = "https://www.bidq.co.kr"

# ---------------------------------------------------------------
# 1. 비드큐 로그인
# ---------------------------------------------------------------
def bidq_login(session: requests.Session, userid: str, userpass: str):
    """로그인 페이지에서 CSRF 토큰을 추출한 뒤 로그인 요청을 보낸다."""
    login_page_url = f"{BASE}/bidq/member/login/index"
    r = session.get(login_page_url, timeout=20)
    r.raise_for_status()

    csrf_token = None
    soup = BeautifulSoup(r.text, "html.parser")

    meta = soup.find("meta", {"name": "csrf-token"})
    if meta and meta.get("content"):
        csrf_token = meta["content"]

    if not csrf_token:
        hidden = soup.find("input", {"name": "_csrf-frontend"})
        if hidden and hidden.get("value"):
            csrf_token = hidden["value"]

    if not csrf_token:
        m = re.search(r'name=["\']_csrf-frontend["\']\s+value=["\']([^"\']+)["\']', r.text)
        if m:
            csrf_token = m.group(1)

    if not csrf_token:
        raise RuntimeError("로그인 페이지에서 CSRF 토큰을 찾지 못했습니다. 페이지 구조가 바뀌었을 수 있습니다.")

    payload = {
        "_csrf-frontend": csrf_token,
        "refurl": "",
        "userid": userid,
        "userpass": userpass,
    }
    headers = {
        "Referer": login_page_url,
        "Origin": BASE,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    r2 = session.post(f"{BASE}/bidq/member/login/loginexec", data=payload,
                       headers=headers, timeout=20, allow_redirects=True)
    r2.raise_for_status()

    if "로그아웃" not in r2.text and "logout" not in r2.text.lower():
        raise RuntimeError("로그인에 실패한 것 같습니다. 아이디/비밀번호를 확인해주세요.")

    return True


# ---------------------------------------------------------------
# 2. 공고번호로 공고 상세정보(기초금액, 발주처 등) 조회
# ---------------------------------------------------------------
def find_bid_detail(session: requests.Session, notice_no: str):
    search_url = f"{BASE}/bidq/bids/list"
    params = {
        "bidtype": "pur",
        "bid_suc": "bid",
        "searchWord": notice_no,
        "word_type": "all_Search",
        "subWord": notice_no,
    }
    r = session.get(search_url, params=params, timeout=20)
    r.raise_for_status()

    search_soup = BeautifulSoup(r.text, "html.parser")
    bidid = None
    for a in search_soup.find_all("a", href=True):
        href = a["href"]
        if "bids/detail/bid" in href and "bidid=" in href:
            m = re.search(r'bidid=([\w\-]+)', href)
            if m:
                bidid = m.group(1)
                break

    if not bidid:
        m = re.search(r'bids/detail/bid\?bidid=([\w\-]+)', r.text)
        if m:
            bidid = m.group(1)

    if not bidid:
        raise RuntimeError(f"'{notice_no}' 공고를 검색 결과에서 찾지 못했습니다. 공고번호를 다시 확인해주세요.")

    detail_url = f"{BASE}/bidq/bids/detail/bid"
    r2 = session.get(detail_url, params={"bidid": bidid, "bidtype": "pur"}, timeout=20)
    r2.raise_for_status()
    soup = BeautifulSoup(r2.text, "html.parser")

    def label_value(label_text):
        cell = soup.find(string=re.compile(re.escape(label_text)))
        if not cell:
            return None
        parent = cell.find_parent(["th", "td", "dt"])
        if not parent:
            return None
        sib = parent.find_next_sibling(["td", "dd"])
        if sib:
            return sib.get_text(strip=True)
        return None

    org_name = label_value("발주기관")
    if org_name:
        org_name = re.split(r'발주처\s*분석|사정율\s*분석', org_name)[0].strip()
    basic = label_value("기초금액")
    lower_limit = label_value("낙찰하한")
    yega_range = label_value("예가변동")
    title = soup.find("h2") or soup.find("h3")
    title_text = title.get_text(strip=True) if title else notice_no

    if not org_name:
        raise RuntimeError("상세페이지에서 발주기관 정보를 찾지 못했습니다. 페이지 구조가 바뀌었을 수 있습니다.")

    return {
        "bidid": bidid,
        "org_name": org_name,
        "title": title_text,
        "base_price": basic or "정보 없음",
        "lower_limit_rate": lower_limit or "정보 없음",
        "price_range": yega_range or "정보 없음",
    }


# ---------------------------------------------------------------
# 3. 발주처의 과거 개찰 데이터(opened-data) 조회
# ---------------------------------------------------------------
def fetch_opened_data(session: requests.Session, org_name: str, months_back: int = 24, page: int = 1, page_size: int = 100):
    from datetime import date, timedelta
    date2 = date.today()
    date1 = date2 - timedelta(days=months_back * 30)

    url = f"{BASE}/bidq/analysis/common-api/opened-data"
    payload = {
        "org_codes": [],
        "org_names": [org_name],
        "bidtype": "pur",
        "itemcode": "",
        "amount1": "",
        "amount2": "",
        "amount_column": "presum",
        "danga_option": "",
        "danga_option2": "",
        "danga_org": "none",
        "danga_year": "clear",
        "date1": date1.strftime("%Y-%m-%d"),
        "date2": date2.strftime("%Y-%m-%d"),
        "graphType": "orgi",
        "innum1": "",
        "innum2": "",
        "localcode": "",
        "location": "",
        "page": page,
        "pageSize": page_size,
        "point": "100",
    }
    from urllib.parse import quote
    headers = {
        "Content-Type": "application/json",
        "Referer": f"{BASE}/bidq/analysis/orgi?bidtype=pur&org={quote(org_name)}",
        "Origin": BASE,
    }
    r = session.post(url, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------
# 4. 과거 데이터를 통계로 요약
# ---------------------------------------------------------------
def summarize_history(opened_json: dict, keyword: str = None):
    records = opened_json.get("data", [])
    rows = []
    for rec in records:
        title = rec.get("constnm", "")
        if keyword and keyword not in title:
            continue
        rows.append({
            "날짜": rec.get("constdt"),
            "공고명": title,
            "기초금액": rec.get("basic"),
            "낙찰하한율(pct)": rec.get("pct"),
            "사정률(success_pct)": rec.get("success_pct"),
            "낙찰가/기초 비율(pctPer)": rec.get("pctPer"),
            "참여업체수": rec.get("innum"),
        })
    return rows


# ---------------------------------------------------------------
# 5. 메인 화면
# ---------------------------------------------------------------
st.title("🎯 비드큐(BidQ) 기반 AI 초정밀 입찰 분석 시스템")
st.caption("공고번호를 입력하면 실제 비드큐 데이터를 자동으로 수집해 AI가 분석합니다.")
st.markdown("---")

notice_no = st.text_input("🔍 공고번호를 입력하세요", value="E260720-639922-0")
analyze_btn = st.button("🚀 초정밀 AI 분석 시작", use_container_width=True)

SYSTEM_PROMPT = """
# 역할 및 페르소나
너는 학교급식 소액수의 입찰(농산, 수산, 축산, 공산 등) 데이터를 남들보다 몇 배는 더 세밀하고 깊게 쪼개어 분석하는 '1등급 낙찰 전략 분석가'이다. 사용자는 최종 투찰 전, 단돈 1,000원이라도 더할지 뺄지 과감한 결단을 내려야 한다. 따라서 너는 우유부단하게 여러 금액을 제시하지 말고, 오직 '단 하나의 최종 원픽 투찰금액'과 함께 사용자가 완벽하게 납득하고 확신을 가질 수 있도록 압도적이고 정밀한 분석 근거를 서술해야 한다.

★ [금액 산출 및 안전 원칙]
- 투찰금액은 나라장터 공식 방식 [기초금액 × 적용사정률 × 낙찰하한율]로 산출한다.
- 하한선 미달 탈락을 방지하기 위해 소수점 이하 단위는 '절상(ROUNDUP)'을 원칙으로 하며, 동가 추첨 방지를 위해 십원 단위 끝자리는 전략적 미세 정렬(예: 50원 등)을 적용한다.
- 최종 추천 금액이 최소 하한선 이상인지 스스로 역산 검증 후 단 하나의 원픽 금액만 출력한다.

# 아래 [실제 과거 개찰 데이터]는 이 발주처(학교)의 실제 낙찰 이력이다. 반드시 이 데이터를 근거로 분석하라.

# ✍️ 출력 포맷

---
## 📌 [눈도장] 이번 입찰 핵심 조건 한눈에 보기
| 항목 | 설정 데이터 |
| :--- | :--- |
| **발주처(학교명)** | [학교명] |
| **기초금액** | [원 단위 표시] |
| **낙찰 하한율** | [값] |
| **예가 변동 범위** | [값] |

---

## 🎯 최종 원픽 투찰 금액
* **최종 추천 금액:** **[원 단위까지 딱 하나의 금액만 작성]**
* **적용 사정률:** [예: 100.1234% (+0.1234%)]

---

## 🔍 초정밀 분석서
1. **해당 학교 고유의 사정률 흐름 분석** (제공된 과거 데이터 기반)
2. **품목별 특이사항**
3. **대수의 법칙 기반 회귀 판단**
4. **참여업체수 변동과 경쟁 강도**

---

## 💡 최종 결단을 위한 '한 끗' 조언
"""

if analyze_btn:
    if not gemini_key:
        st.error("🚨 왼쪽 사이드바에 Gemini API Key를 입력해 주세요!")
    elif not bidq_id or not bidq_pw:
        st.error("🚨 왼쪽 사이드바에 비드큐 아이디/비밀번호를 입력해 주세요!")
    elif not notice_no:
        st.warning("⚠️ 공고번호를 입력해 주세요.")
    else:
        status_box = st.status("비드큐 로그인 및 데이터 수집 중...", expanded=True)
        try:
            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            })

            status_box.write("1️⃣ 비드큐 로그인 중...")
            bidq_login(session, bidq_id, bidq_pw)
            status_box.write("✅ 로그인 성공!")

            status_box.write(f"2️⃣ '{notice_no}' 공고 상세정보 조회 중...")
            bid_info = find_bid_detail(session, notice_no)
            status_box.write(f"✅ 발주처 확인: {bid_info['org_name']}")

            status_box.write("3️⃣ 발주처 과거 개찰 이력(최근 24개월) 수집 중...")
            opened_json = fetch_opened_data(session, bid_info["org_name"], months_back=24)
            total = opened_json.get("total_count", 0)
            status_box.write(f"✅ 과거 개찰 데이터 {total}건 수집 완료!")

            history_rows = summarize_history(opened_json)

            status_box.write(f"4️⃣ Gemini AI 분석 중... (모델: {selected_model})")

            history_text = "\n".join(
                f"- {r['날짜']} | {r['공고명']} | 기초금액:{r['기초금액']} | 하한율:{r['낙찰하한율(pct)']}% | "
                f"사정률:{r['사정률(success_pct)']}% | 낙찰/기초:{r['낙찰가/기초 비율(pctPer)']}% | 참여업체:{r['참여업체수']}개"
                for r in history_rows
            )

            prompt_input = f"""{SYSTEM_PROMPT}

[이번 공고 정보]
- 공고번호: {notice_no}
- 공고명: {bid_info['title']}
- 발주처: {bid_info['org_name']}
- 기초금액: {bid_info['base_price']}
- 낙찰하한율: {bid_info['lower_limit_rate']}
- 예가변동폭: {bid_info['price_range']}

[실제 과거 개찰 데이터 (최근 24개월, {total}건)]
{history_text}
"""

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent?key={gemini_key}"
            gh = {"Content-Type": "application/json"}
            gp = {"contents": [{"parts": [{"text": prompt_input}]}]}
            res = requests.post(url, headers=gh, json=gp, timeout=120)
            res_json = res.json()

            if res.status_code == 200 and "candidates" in res_json:
                result_text = res_json["candidates"][0]["content"]["parts"][0]["text"]
                status_box.update(label="✅ 초정밀 AI 분석 완료!", state="complete", expanded=False)
                st.markdown("---")
                st.markdown(result_text)
                with st.expander(f"📊 수집된 과거 데이터 원본 보기 ({len(history_rows)}건)"):
                    st.dataframe(history_rows)
            else:
                error_msg = res_json.get("error", {}).get("message", "구글 API 응답 오류가 발생했습니다.")
                status_box.update(label="❌ 오류 발생", state="error", expanded=True)
                st.error(f"구글 API 응답 오류 (status {res.status_code}): {error_msg}")

        except Exception as e:
            status_box.update(label="❌ 오류 발생", state="error", expanded=True)
            st.error(f"오류 상세: {e}")