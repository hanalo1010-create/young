"""
비드큐(BidQ) 기반 AI 입찰 분석기 v3 (단일 파일 완벽 수정본)

[실행 방법]
  1. pip install streamlit requests beautifulsoup4 pandas
  2. streamlit run app.py

[기록 파일]
  같은 폴더에 bid_log.csv 로 자동 생성 및 저장됩니다.
"""

import json
import math
import os
import re
import statistics
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ===============================================================
# 페이지 기본 설정
# ===============================================================
st.set_page_config(
    page_title="비드큐 AI 입찰 분석기", page_icon="🎯", layout="wide"
)

BASE = "https://www.bidq.co.kr"
LOG_PATH = "bid_log.csv"

LOG_COLUMNS = [
    "기록ID",
    "등록일",
    "공고번호",
    "발주처",
    "공고명",
    "기초금액",
    "낙찰하한율",
    "예측사정률",
    "내투찰금액",
    "상태",
    "실제낙찰사정률",
    "실제낙찰금액",
    "참여업체수",
    "메모",
]


# ===============================================================
# 0. 공통 도우미
# ===============================================================
def safe_secret(key: str) -> str:
    """secrets.toml 이 없어도 오류 없이 빈 값을 돌려준다."""
    try:
        return st.secrets.get(key, "")
    except Exception:
        return ""


def to_number(text):
    """'12,345,000원' -> 12345000.0 / '87.745%' -> 87.745 / 실패하면 None"""
    if text is None:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(text))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def looks_like_notice_no(text: str) -> bool:
    """공고번호처럼 생겼는지 판단 (숫자가 많고 하이픈이 있으면 공고번호로 봄)."""
    t = (text or "").strip()
    return bool(t) and sum(c.isdigit() for c in t) >= 6 and "-" in t


def norm_text(s) -> str:
    """공고명 비교용: 띄어쓰기·괄호·기호를 모두 제거해서 단순화."""
    return re.sub(r"[\s()\[\]\-_,·:/]", "", str(s or ""))


# ===============================================================
# 1. 사이드바
# ===============================================================
with st.sidebar:
    st.header("⚙️ 시스템 설정")
    gemini_key = st.text_input(
        "Gemini API Key",
        value=safe_secret("GEMINI_API_KEY"),
        type="password",
        placeholder="구글 API 키",
    )
    bidq_id = st.text_input("비드큐 아이디", value=safe_secret("BIDQ_ID"))
    bidq_pw = st.text_input(
        "비드큐 비밀번호", value=safe_secret("BIDQ_PW"), type="password"
    )

    st.markdown("---")
    # 2026년 7월 기준 최신 모델 목록
    MODEL_OPTIONS = {
        "gemini-3.6-flash (최신 주력, 추천)": "gemini-3.6-flash",
        "gemini-3.5-flash (안정)": "gemini-3.5-flash",
        "gemini-3.5-flash-lite (가장 저렴/빠름)": "gemini-3.5-flash-lite",
        "gemini-2.5-flash (구버전)": "gemini-2.5-flash",
        "gemini-flash-latest (항상 최신 자동 전환)": "gemini-flash-latest",
    }
    model_label = st.selectbox(
        "Gemini 모델", list(MODEL_OPTIONS.keys()), index=0
    )
    selected_model = MODEL_OPTIONS[model_label]

    st.markdown("---")
    months_back = st.slider("과거 데이터 조회 기간(개월)", 6, 60, 24, step=6)
    tail_adjust = st.number_input(
        "끝자리 가산액(원)",
        min_value=0,
        max_value=990,
        value=0,
        step=10,
        help=(
            "동가 경쟁을 피하려고 금액을 조금 올릴 때 씁니다. 0 이상만"
            " 허용해서 하한선 아래로 내려가는 사고를 막습니다."
        ),
    )

    st.markdown("---")
    use_correction = st.checkbox(
        "내 기록으로 사정률 자동 보정",
        value=True,
        help=(
            "지금까지 쌓인 내 오차 평균만큼 AI 사정률을 자동으로 조정합니다."
            " 기록이 5건 이상일 때만 작동합니다."
        ),
    )


# ===============================================================
# 2. 비드큐 접속
# ===============================================================
def bidq_login(session: requests.Session, userid: str, userpass: str):
    login_page_url = f"{BASE}/bidq/member/login/index"
    r = session.get(login_page_url, timeout=20)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    csrf_token = None

    meta = soup.find("meta", {"name": "csrf-token"})
    if meta and meta.get("content"):
        csrf_token = meta["content"]
    if not csrf_token:
        hidden = soup.find("input", {"name": "_csrf-frontend"})
        if hidden and hidden.get("value"):
            csrf_token = hidden["value"]
    if not csrf_token:
        m = re.search(
            r'name=["\']_csrf-frontend["\']\s+value=["\']([^"\']+)["\']', r.text
        )
        if m:
            csrf_token = m.group(1)
    if not csrf_token:
        raise RuntimeError(
            "로그인 페이지에서 보안 토큰을 찾지 못했습니다. 사이트 구조가"
            " 바뀐 것 같습니다."
        )

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
    r2 = session.post(
        f"{BASE}/bidq/member/login/loginexec",
        data=payload,
        headers=headers,
        timeout=20,
        allow_redirects=True,
    )
    r2.raise_for_status()

    if "로그아웃" not in r2.text and "logout" not in r2.text.lower():
        raise RuntimeError("로그인 실패. 아이디/비밀번호를 확인해주세요.")
    return True


@st.cache_resource(show_spinner=False)
def get_session(userid: str, userpass: str):
    """로그인 세션을 재사용한다 (매번 로그인하지 않도록)."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
    })
    bidq_login(s, userid, userpass)
    return s


def make_label_reader(soup):
    """표에서 '기초금액' 같은 항목명을 찾아 옆 칸 값을 읽는 함수를 만들어 준다."""

    def label_value(label_text):
        for cell in soup.find_all(string=re.compile(re.escape(label_text))):
            parent = cell.find_parent(["th", "td", "dt"])
            if not parent:
                continue
            sib = parent.find_next_sibling(["td", "dd"])
            if sib:
                value = sib.get_text(strip=True)
                if value:
                    return value
        return None

    return label_value


def find_bid_detail(session: requests.Session, notice_no: str):
    """공고번호로 상세 정보를 가져온다.

    진행 중 공고(detail/bid)와 개찰 완료 공고(detail/suc)를 모두 인식한다.
    """
    params = {
        "bidtype": "pur",
        "bid_suc": "bid",
        "searchWord": notice_no,
        "word_type": "all_Search",
        "subWord": notice_no,
    }
    r = session.get(f"{BASE}/bidq/bids/list", params=params, timeout=20)
    r.raise_for_status()

    bidid, kind = None, "bid"
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"bids/detail/(bid|suc)\?bidid=([\w\-]+)", href)
        if m:
            kind, bidid = m.group(1), m.group(2)
            break
    if not bidid:
        m = re.search(r"bids/detail/(bid|suc)\?bidid=([\w\-]+)", r.text)
        if m:
            kind, bidid = m.group(1), m.group(2)
    if not bidid:
        raise RuntimeError(
            f"'{notice_no}' 공고를 찾지 못했습니다. 공고번호를 확인해주세요."
        )

    r2 = session.get(
        f"{BASE}/bidq/bids/detail/{kind}",
        params={"bidid": bidid, "bidtype": "pur"},
        timeout=20,
    )
    r2.raise_for_status()
    dsoup = BeautifulSoup(r2.text, "html.parser")
    lv = make_label_reader(dsoup)

    org_name = lv("발주기관")
    if org_name:
        org_name = re.split(r"발주처\s*분석|사정율\s*분석", org_name)[0].strip()
    if not org_name:
        raise RuntimeError(
            "상세페이지에서 발주기관을 찾지 못했습니다. 사이트 구조가 바뀐 것"
            " 같습니다."
        )

    title = dsoup.find("h2") or dsoup.find("h3")
    return {
        "bidid": bidid,
        "kind": kind,  # bid=진행중 / suc=개찰완료
        "org_name": org_name,
        "title": title.get_text(strip=True) if title else notice_no,
        "base_price": lv("기초금액") or "정보 없음",
        "lower_limit_rate": lv("낙찰하한") or "정보 없음",
        "price_range": lv("예가변동") or "정보 없음",
        "win_price": lv("낙찰금액") or lv("낙찰가"),
        "win_rate": lv("사정률") or lv("사정율"),
    }


# ===============================================================
# 3. 과거 개찰 데이터
# ===============================================================
def fetch_opened_page(session, org_name, months_back, page, page_size=100):
    date2 = date.today()
    date1 = date2 - timedelta(days=months_back * 31)
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
    headers = {
        "Content-Type": "application/json",
        "Referer": f"{BASE}/bidq/analysis/orgi?bidtype=pur&org={quote(org_name)}",
        "Origin": BASE,
    }
    r = session.post(
        f"{BASE}/bidq/analysis/common-api/opened-data",
        json=payload,
        headers=headers,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_all_opened(
    session, org_name, months_back=24, max_pages=15, quiet=False
):
    """페이지를 끝까지 넘겨가며 전부 수집한다."""
    rows, total = [], 0
    for page in range(1, max_pages + 1):
        try:
            data = fetch_opened_page(session, org_name, months_back, page)
        except Exception as e:
            if not quiet:
                st.warning(f"'{org_name}' {page}페이지 수집 오류: {e}")
            break
        page_rows = data.get("data") or []
        total = data.get("total_count", total) or total
        if not page_rows:
            break
        rows.extend(page_rows)
        if total and len(rows) >= total:
            break
        time.sleep(0.3)
    return rows, total


def merge_records(*record_lists):
    seen, merged = set(), []
    for records in record_lists:
        for rec in records or []:
            key = (rec.get("constdt"), rec.get("constnm"), rec.get("basic"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(rec)
    merged.sort(key=lambda r: str(r.get("constdt") or ""), reverse=True)
    return merged


def summarize_history(records):
    return [
        {
            "날짜": r.get("constdt"),
            "공고명": r.get("constnm", ""),
            "기초금액": r.get("basic"),
            "낙찰하한율(%)": r.get("pct"),
            "사정률(%)": r.get("success_pct"),
            "낙찰가/기초(%)": r.get("pctPer"),
            "참여업체수": r.get("innum"),
        }
        for r in records
    ]


def compute_stats(records):
    """사정률 통계를 파이썬이 직접 계산한다 (AI에게 숫자를 세게 하면 틀림)."""
    rates, innums = [], []
    for r in records:
        v = to_number(r.get("success_pct"))
        if v is not None and 90 <= v <= 110:  # 명백한 이상치 제외
            rates.append(v)
        n = to_number(r.get("innum"))
        if n is not None:
            innums.append(n)
    if not rates:
        return None

    recent = [
        (r.get("constdt"), to_number(r.get("success_pct")))
        for r in records[:8]
        if to_number(r.get("success_pct")) is not None
    ]
    below = [v for v in rates if v < 100]

    return {
        "건수": len(rates),
        "평균": statistics.mean(rates),
        "중앙값": statistics.median(rates),
        "표준편차": statistics.pstdev(rates) if len(rates) > 1 else 0.0,
        "최저": min(rates),
        "최고": max(rates),
        "미만비율": len(below) / len(rates) * 100,
        "최근8건": recent,
        "평균업체수": statistics.mean(innums) if innums else None,
    }


def stats_to_text(s):
    if not s:
        return "사정률 통계를 계산할 데이터가 없습니다."
    recent_txt = ", ".join(f"{d}:{v:.4f}%" for d, v in s["최근8건"]) or "없음"
    lines = [
        f"- 유효 표본: {s['건수']}건",
        f"- 사정률 평균: {s['평균']:.4f}%",
        f"- 사정률 중앙값: {s['중앙값']:.4f}%",
        (
            f"- 사정률 표준편차: {s['표준편차']:.4f}%p  (클수록 예측이"
            " 어려운 발주처)"
        ),
        f"- 사정률 범위: {s['최저']:.4f}% ~ {s['최고']:.4f}%",
        f"- 100% 미만 비율: {s['미만비율']:.1f}%",
        f"- 최근 8건 사정률(최신순): {recent_txt}",
    ]
    if s["평균업체수"] is not None:
        lines.append(f"- 평균 참여업체수: {s['평균업체수']:.1f}개")
    return "\n".join(lines)


def volatility_grade(std):
    """표준편차로 '이 학교에 들어가도 되는지' 등급을 매긴다."""
    if std <= 0.10:
        return (
            "🟢 안정",
            "예측하기 쉬운 발주처입니다. 적극적으로 노려볼 만합니다.",
        )
    if std <= 0.20:
        return (
            "🟡 보통",
            "무난합니다. 평균 근처를 노리되 무리하지 마세요.",
        )
    if std <= 0.30:
        return (
            "🟠 주의",
            "흔들림이 있습니다. 하한선에 가깝게 붙이는 편이 안전합니다.",
        )
    return (
        "🔴 위험",
        "너무 튀는 발주처입니다. 예측이 사실상 불가능하니 건너뛰는 편이"
        " 이득입니다.",
    )


# ===============================================================
# 4. 투찰 기록 저장소 (CSV)
# ===============================================================
def load_log() -> pd.DataFrame:
    if os.path.exists(LOG_PATH):
        try:
            df = pd.read_csv(LOG_PATH, dtype=str).fillna("")
            for c in LOG_COLUMNS:
                if c not in df.columns:
                    df[c] = ""
            return df[LOG_COLUMNS]
        except Exception as e:
            st.warning(
                f"기록 파일을 읽지 못했습니다({e}). 새로 시작합니다."
            )
    return pd.DataFrame(columns=LOG_COLUMNS)


def save_log(df: pd.DataFrame):
    try:
        df.to_csv(LOG_PATH, index=False, encoding="utf-8-sig")
        return True
    except Exception as e:
        st.error(f"기록 저장 실패: {e}")
        return False


def append_log(row: dict):
    df = load_log()
    row["기록ID"] = datetime.now().strftime("%Y%m%d%H%M%S")
    row["등록일"] = datetime.now().strftime("%Y-%m-%d")
    for c in LOG_COLUMNS:
        row.setdefault(c, "")
    df = pd.concat([df, pd.DataFrame([row])[LOG_COLUMNS]], ignore_index=True)
    return save_log(df)


def error_frame(df: pd.DataFrame) -> pd.DataFrame:
    """예측 사정률과 실제 사정률이 둘 다 있는 행만 뽑아 오차를 계산한다."""
    d = df.copy()
    d["_예측"] = pd.to_numeric(d["예측사정률"], errors="coerce")
    d["_실제"] = pd.to_numeric(d["실제낙찰사정률"], errors="coerce")
    d = d.dropna(subset=["_예측", "_실제"]).copy()
    d["오차(%p)"] = (d["_예측"] - d["_실제"]).round(4)
    return d


def get_correction(df: pd.DataFrame, org_name: str = None, min_n: int = 5):
    """지금까지의 평균 오차를 보정값으로 돌려준다.

    오차가 +0.05%p면 내가 계속 0.05%p 높게 본다는 뜻이므로, 다음엔 그만큼 빼야 한다.
    """
    d = error_frame(df)
    if org_name:
        sub = d[d["발주처"] == org_name]
        if len(sub) >= 3:
            return -sub["오차(%p)"].mean(), len(sub), "이 발주처"
    if len(d) >= min_n:
        return -d["오차(%p)"].mean(), len(d), "전체"
    return 0.0, len(d), None


def find_match(records, title, base_price):
    """개찰 데이터 중에서 내가 기록한 공고와 같은 건을 찾는다."""
    t = norm_text(title)
    bp = to_number(base_price)
    fallback = None
    for rec in records:
        rt = norm_text(rec.get("constnm"))
        rb = to_number(rec.get("basic"))
        if t and rt and (t == rt or t in rt or rt in t):
            return rec, "공고명 일치"
        if bp and rb and abs(bp - rb) < 1 and fallback is None:
            fallback = rec
    return (fallback, "기초금액 일치(확인 필요)") if fallback else (None, None)


# ===============================================================
# 5. 최종 투찰금액 계산 (AI가 아니라 파이썬이 담당)
# ===============================================================
def calc_bid_price(
    base_price: float,
    applied_rate: float,
    lower_rate: float,
    tail_add: int = 0,
):
    """기초금액 x 사정률 = 예정가격(내림)

    예정가격 x 낙찰하한율 = 하한선(올림)  <- 이 아래로 쓰면 무효
    ※ 절사·절상 규칙은 기관·공고마다 다를 수 있으니 공고문을 반드시 대조하세요.
    """
    est_price = math.floor(base_price * (applied_rate / 100.0))
    floor_price = math.ceil(est_price * (lower_rate / 100.0))
    bid = floor_price + int(tail_add)
    return {
        "예정가격": est_price,
        "하한선": floor_price,
        "투찰금액": bid,
        "충족": bid >= floor_price,
    }


# ===============================================================
# 6. AI 프롬프트 및 API 호출 (백틱 오타 방지 처리 완료)
# ===============================================================
SYSTEM_PROMPT = """
너는 학교급식 소액수의 입찰 데이터를 분석하는 낙찰 전략 분석가다.
아래 [실제 과거 개찰 데이터]와 [파이썬이 계산한 통계]를 근거로,
이번 공고에 적용할 '단 하나의 사정률(%)'을 판단하라.

[반드시 지킬 규칙]
1. 금액은 절대 계산하지 마라. 금액 계산은 프로그램이 한다.
2. 주어진 데이터에 없는 숫자를 지어내지 마라. 모르면 모른다고 써라.
3. 근거를 쓸 때는 반드시 실제 날짜와 실제 수치를 인용하라.
   (나쁜 예: "최근 사정률이 낮은 편이다")
   (좋은 예: "2026-05-12 99.8721%, 2026-06-03 99.9102% 등 최근 3건이 100% 아래")
4. analysis_markdown은 최소 800자 이상으로 아래 6개 항목을 모두 채워라.

### 1. 어떤 자료를 썼나
몇 건, 어느 기간, 발주처명(과거명 포함), 이상치를 뺀 유효 표본 수.
### 2. 이 발주처의 사정률 성향
평균/중앙값/표준편차가 뜻하는 바를 쉬운 말로. 실제 사례 3건 이상을 날짜와 함께 인용.
### 3. 최근 흐름과 평균 회귀 판단
최근 8건이 평균 위인지 아래인지, 쏠려 있다면 되돌아올 때인지 새 기준선인지.
### 4. 경쟁 강도
평균 참여업체수를 근거로 하한선에 붙어야 하는지 판단.
### 5. 그래서 왜 이 사정률인가
결론 한 문장 + 평균/중앙값/최근흐름 중 무엇에 얼마나 무게를 뒀는지 명시.
### 6. 이 판단이 틀릴 수 있는 경우
빗나갈 시나리오 2~3가지를 솔직하게.

반드시 아래 JSON 형식으로만 답하라. 코드블록이나 다른 말은 붙이지 마라.
{
  "applied_rate": 100.1234,
  "confidence": "상|중|하",
  "confidence_reason": "신뢰도를 그렇게 매긴 이유 한 줄",
  "analysis_markdown": "### 1. 어떤 자료를 썼나\\n..."
}
"""


def call_gemini(api_key, model, prompt_text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    res = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=body,
        timeout=180,
    )
    data = res.json()
    if res.status_code != 200 or "candidates" not in data:
        msg = data.get("error", {}).get("message", "구글 API 응답 오류")
        raise RuntimeError(f"Gemini 오류 (status {res.status_code}): {msg}")
    raw = data["candidates"][0]["content"]["parts"][0]["text"]

    # 백틱 오타로 인한 파이썬 SyntaxError 방지 안전 처리
    fence = chr(96) * 3
    cleaned = raw.strip()
    cleaned = re.sub(r"^\s*" + fence + r"(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*" + fence + r"\s*$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except Exception:
        raise RuntimeError(
            f"AI 응답을 해석하지 못했습니다. 원문 앞부분:\n{raw[:500]}"
        )


# ===============================================================
# 7. 화면
# ===============================================================
st.title("🎯 비드큐(BidQ) 기반 AI 입찰 분석기")

tab1, tab2, tab3 = st.tabs(
    ["🔍 입찰 분석", "📒 투찰 기록 & 오차 추적", "🏫 학교별 성향 카드"]
)


# ---------------------------------------------------------------
# 탭 1 : 입찰 분석
# ---------------------------------------------------------------
with tab1:
    st.caption(
        "공고번호를 넣으면 비드큐 데이터를 모아 AI가 사정률을 판단하고, 금액은"
        " 코드가 계산합니다."
    )

    notice_no = st.text_input("🔍 ① 이번 공고번호", value="E260720-639922-0")
    legacy_input = st.text_input(
        "🏫 ② 과거 발주처명 또는 옛 공고번호 (선택)",
        value="",
        placeholder="예) 인천OO초등학교  또는  E250310-123456-0",
        help="행정구역·학교명이 바뀌어 과거 데이터가 안 잡힐 때 사용합니다.",
    )
    analyze_btn = st.button(
        "🚀 분석 시작", use_container_width=True, type="primary"
    )

    if analyze_btn:
        if not gemini_key:
            st.error("🚨 사이드바에 Gemini API Key를 입력해 주세요.")
        elif not bidq_id or not bidq_pw:
            st.error("🚨 사이드바에 비드큐 아이디/비밀번호를 입력해 주세요.")
        elif not notice_no.strip():
            st.warning("⚠️ 공고번호를 입력해 주세요.")
        else:
            status_box = st.status("비드큐 접속 중...", expanded=True)
            try:
                status_box.write("1️⃣ 비드큐 로그인...")
                session = get_session(bidq_id, bidq_pw)
                status_box.write("✅ 로그인 성공")

                status_box.write(f"2️⃣ '{notice_no}' 공고 조회...")
                bid_info = find_bid_detail(session, notice_no.strip())
                status_box.write(f"✅ 발주처: {bid_info['org_name']}")
                if bid_info["kind"] == "suc":
                    status_box.write("ℹ️ 이미 개찰이 끝난 공고입니다.")

                legacy_org = None
                legacy_raw = legacy_input.strip()
                if legacy_raw:
                    if looks_like_notice_no(legacy_raw):
                        try:
                            legacy_org = find_bid_detail(session, legacy_raw)[
                                "org_name"
                            ]
                            status_box.write(
                                f"✅ 옛 발주처명 확인: {legacy_org}"
                            )
                        except Exception as e:
                            legacy_org = legacy_raw
                            status_box.write(
                                f"⚠️ 옛 공고 조회 실패({e}) → 입력값을 이름으로"
                                " 사용"
                            )
                    else:
                        legacy_org = legacy_raw

                status_box.write(
                    f"3️⃣ 과거 개찰 이력 수집 (최근 {months_back}개월)..."
                )
                cur_rows, _ = fetch_all_opened(
                    session, bid_info["org_name"], months_back
                )
                status_box.write(
                    f"   • 현재명 '{bid_info['org_name']}': {len(cur_rows)}건"
                )

                old_rows = []
                if legacy_org and legacy_org != bid_info["org_name"]:
                    old_rows, _ = fetch_all_opened(
                        session, legacy_org, months_back
                    )
                    status_box.write(
                        f"   • 과거명 '{legacy_org}': {len(old_rows)}건"
                    )

                records = merge_records(cur_rows, old_rows)
                if not records:
                    raise RuntimeError(
                        "과거 개찰 데이터를 찾지 못했습니다. ②번 칸에 변경 전"
                        " 발주처명을 넣어보세요."
                    )
                status_box.write(f"✅ 합계 {len(records)}건 (중복 제거)")

                stats = compute_stats(records)
                history_rows = summarize_history(records)
                history_text = "\n".join(
                    f"- {r['날짜']} | {r['공고명']} | 기초:{r['기초금액']} |"
                    f" 하한율:{r['낙찰하한율(%)']}% | 사정률:{r['사정률(%)']}% |"
                    f" 낙찰가/기초:{r['낙찰가/기초(%)']}% | 업체수:{r['참여업체수']}"
                    for r in history_rows
                )

                status_box.write(f"4️⃣ AI 분석 중 ({selected_model})...")
                prompt_input = f"""{SYSTEM_PROMPT}

[파이썬이 계산한 통계 — 이 숫자는 정확하니 그대로 인용하라]
{stats_to_text(stats)}

[이번 공고 정보]
- 공고번호: {notice_no}
- 공고명: {bid_info['title']}
- 발주처: {bid_info['org_name']}{f" (과거명: {legacy_org})" if legacy_org else ""}
- 기초금액: {bid_info['base_price']}
- 낙찰하한율: {bid_info['lower_limit_rate']}
- 예가변동폭: {bid_info['price_range']}

[실제 과거 개찰 데이터 (최근 {months_back}개월, {len(records)}건)]
{history_text}
"""
                ai = call_gemini(gemini_key, selected_model, prompt_input)
                status_box.update(
                    label="✅ 분석 완료", state="complete", expanded=False
                )

                # 결과를 세션에 저장 (기록 버튼에서 사용)
                st.session_state["last"] = {
                    "notice_no": notice_no.strip(),
                    "bid_info": bid_info,
                    "legacy_org": legacy_org,
                    "ai": ai,
                    "stats": stats,
                    "history_rows": history_rows,
                    "records_n": len(records),
                }

            except Exception as e:
                status_box.update(
                    label="❌ 오류 발생", state="error", expanded=True
                )
                st.error(f"오류 상세: {e}")

    # ----- 결과 표시 -----
    last = st.session_state.get("last")
    if last:
        bid_info, ai, stats = last["bid_info"], last["ai"], last["stats"]
        base_price = to_number(bid_info["base_price"])
        lower_rate = to_number(bid_info["lower_limit_rate"])
        raw_rate = float(ai.get("applied_rate"))

        # 내 기록 기반 보정
        log_df = load_log()
        corr, corr_n, corr_scope = get_correction(log_df, bid_info["org_name"])
        applied_rate = (
            raw_rate + corr if (use_correction and corr_scope) else raw_rate
        )

        st.markdown("---")
        st.subheader("📌 이번 입찰 조건")
        st.table({
            "항목": [
                "발주처",
                "공고명",
                "기초금액",
                "낙찰하한율",
                "예가 변동폭",
                "분석 데이터",
            ],
            "값": [
                bid_info["org_name"]
                + (f" (+ 과거명 {last['legacy_org']})" if last["legacy_org"] else ""),
                bid_info["title"],
                bid_info["base_price"],
                bid_info["lower_limit_rate"],
                bid_info["price_range"],
                f"{last['records_n']}건 / 최근 {months_back}개월",
            ],
        })

        st.subheader("🎯 추천 투찰 금액")
        if base_price and lower_rate:
            calc = calc_bid_price(
                base_price, applied_rate, lower_rate, tail_adjust
            )
            c1, c2 = st.columns([2, 1])
            c1.metric("최종 투찰금액", f"{calc['투찰금액']:,.0f} 원")
            c2.metric(
                "적용 사정률",
                f"{applied_rate:.4f} %",
                delta=(
                    f"{corr:+.4f}%p 보정"
                    if (use_correction and corr_scope)
                    else None
                ),
            )

            if use_correction and corr_scope:
                st.caption(
                    f"🔧 내 과거 기록 {corr_n}건({corr_scope}) 기준으로 AI 원본값"
                    f" {raw_rate:.4f}%에서 {corr:+.4f}%p 보정했습니다."
                )

            if calc["충족"]:
                st.success("✅ 하한선 이상 확인 (탈락 위험 없음)")
            else:
                st.error("🚨 하한선 미달! 금액을 다시 확인하세요.")

            with st.expander("🧮 이 금액이 나온 계산 과정", expanded=False):
                st.markdown(f"""
| 단계 | 계산식 | 결과 |
| :--- | :--- | ---: |
| ① 기초금액 | 공고 값 | {base_price:,.0f} 원 |
| ② 적용 사정률 | AI 판단 + 내 기록 보정 | {applied_rate:.4f} % |
| ③ 예정가격(추정) | ① × ② 의 **내림** | {calc['예정가격']:,.0f} 원 |
| ④ 낙찰하한율 | 공고 값 | {lower_rate:.3f} % |
| ⑤ 하한선 | ③ × ④ 의 **올림** | {calc['하한선']:,.0f} 원 |
| ⑥ 끝자리 가산 | 사이드바 설정 | + {tail_adjust:,} 원 |
| **⑦ 최종 투찰금액** | ⑤ + ⑥ | **{calc['투찰금액']:,.0f} 원** |

**왜 내림과 올림을 섞나요?** 예정가격은 보수적으로 내려 잡고, 하한선은 올려 잡습니다.
1원이라도 하한선 아래면 즉시 무효라, 반올림 대신 무조건 올림으로 사고를 막습니다.

⚠️ 절사·절상 규칙은 기관·공고마다 다를 수 있습니다. 공고문을 꼭 대조하세요.
""")

            # ★ 기록 저장 버튼
            st.markdown("#### 📒 이 건을 기록에 저장")
            colA, colB = st.columns([1, 2])
            actual_bid = colA.number_input(
                "실제로 투찰한 금액(원)",
                min_value=0,
                value=int(calc["투찰금액"]),
                step=10,
                help="추천값과 다르게 넣으셨다면 실제 넣은 금액으로 고쳐주세요.",
            )
            memo = colB.text_input(
                "메모 (선택)", placeholder="예: 감으로 100원 더 올림"
            )
            if st.button("💾 기록에 저장하기", use_container_width=True):
                ok = append_log({
                    "공고번호": last["notice_no"],
                    "발주처": bid_info["org_name"],
                    "공고명": bid_info["title"],
                    "기초금액": f"{base_price:.0f}",
                    "낙찰하한율": f"{lower_rate:.3f}",
                    "예측사정률": f"{applied_rate:.4f}",
                    "내투찰금액": f"{actual_bid:.0f}",
                    "상태": "대기",
                    "메모": memo,
                })
                if ok:
                    st.success(
                        "✅ 저장했습니다. '투찰 기록' 탭에서 확인하세요."
                    )
        else:
            st.warning(
                "기초금액/낙찰하한율을 숫자로 읽지 못했습니다. AI 판단 사정률:"
                f" **{raw_rate:.4f}%**"
            )

        if stats:
            with st.expander("📈 분석에 사용된 실제 통계 수치"):
                grade, advice = volatility_grade(stats["표준편차"])
                st.markdown(f"""
| 항목 | 값 | 의미 |
| :--- | ---: | :--- |
| 유효 표본 | {stats['건수']}건 | 이상치를 뺀 분석 대상 |
| 사정률 평균 | {stats['평균']:.4f}% | 이 발주처의 무게중심 |
| 사정률 중앙값 | {stats['중앙값']:.4f}% | 극단값에 덜 흔들리는 기준 |
| 표준편차 | {stats['표준편차']:.4f}%p | {grade} |
| 범위 | {stats['최저']:.4f}% ~ {stats['최고']:.4f}% | 실제로 나왔던 폭 |
| 100% 미만 비율 | {stats['미만비율']:.1f}% | 낮게 형성되는 경향 |
""")
                st.caption(advice)

        st.markdown("---")
        st.subheader("🔍 초정밀 분석서")
        if ai.get("confidence_reason"):
            st.info(
                f"**신뢰도 {ai.get('confidence', '-')}** —"
                f" {ai['confidence_reason']}"
            )
        st.markdown(ai.get("analysis_markdown", "분석 내용이 없습니다."))

        with st.expander(
            f"📊 수집된 과거 데이터 원본 ({len(last['history_rows'])}건)"
        ):
            st.dataframe(last["history_rows"], use_container_width=True)


# ---------------------------------------------------------------
# 탭 2 : 투찰 기록 & 오차 추적  ★1순위
# ---------------------------------------------------------------
with tab2:
    st.subheader("📒 투찰 기록 & 오차 추적")
    st.caption(
        "내가 쓴 금액과 실제 결과를 쌓아, 내 예측이 어느 쪽으로 얼마나"
        " 빗나가는지 찾아냅니다."
    )

    df = load_log()

    if df.empty:
        st.info(
            "아직 기록이 없습니다. '입찰 분석' 탭에서 분석 후 **기록에"
            " 저장하기**를 눌러보세요."
        )
    else:
        d = error_frame(df)
        done, waiting = len(d), len(df) - len(d)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("전체 기록", f"{len(df)}건")
        c2.metric("개찰 완료", f"{done}건", delta=f"대기 {waiting}건")

        if done > 0:
            bias = d["오차(%p)"].mean()
            mae = d["오차(%p)"].abs().mean()
            c3.metric("평균 오차(편향)", f"{bias:+.4f}%p")
            c4.metric("평균 빗나간 폭", f"{mae:.4f}%p")

            st.markdown("#### 🔧 내 예측 습관 진단")
            if done < 5:
                st.warning(
                    f"아직 {done}건뿐입니다. **5건 이상** 쌓여야 보정값을 신뢰할"
                    " 수 있습니다."
                )
            elif abs(bias) < 0.02:
                st.success(
                    "편향이 거의 없습니다. 예측이 한쪽으로 치우치지 않고"
                    " 있습니다."
                )
            elif bias > 0:
                st.warning(
                    "**사정률을 계속 높게 보는 습관**이 있습니다 (평균"
                    f" {bias:+.4f}%p). 다음부터 {bias:.4f}%p 정도 낮춰 잡으면"
                    " 중심에 가까워집니다."
                )
            else:
                st.warning(
                    "**사정률을 계속 낮게 보는 습관**이 있습니다 (평균"
                    f" {bias:+.4f}%p). 다음부터 {abs(bias):.4f}%p 정도 높여"
                    " 잡으면 중심에 가까워집니다."
                )
            st.caption(
                "사이드바의 '내 기록으로 사정률 자동 보정'을 켜두면 이 값이"
                " 자동 반영됩니다."
            )

            # 낙찰률
            judged = df[df["상태"].isin(["낙찰", "패찰"])]
            if len(judged) > 0:
                win_rate = (judged["상태"] == "낙찰").sum() / len(judged) * 100
                st.metric(
                    "낙찰률",
                    f"{win_rate:.1f}%",
                    delta=f"{(judged['상태']=='낙찰').sum()}/{len(judged)}건",
                )

            # 학교별 약점
            st.markdown("#### 🏫 학교별로 내가 약한 곳")
            g = (
                d.groupby("발주처")
                .agg(
                    건수=("오차(%p)", "size"),
                    평균오차=("오차(%p)", "mean"),
                    빗나간폭=("오차(%p)", lambda x: x.abs().mean()),
                )
                .round(4)
                .sort_values("빗나간폭", ascending=False)
            )
            st.dataframe(g, use_container_width=True)
            st.caption(
                "**빗나간폭**이 큰 학교가 내가 약한 곳입니다. 건수 3건"
                " 이상부터 참고하세요."
            )

            # 추이
            st.markdown("#### 📉 시간에 따라 나아지고 있나")
            trend = d.sort_values("등록일")[
                ["등록일", "오차(%p)"]
            ].reset_index(drop=True)
            trend["절대오차"] = trend["오차(%p)"].abs()
            st.line_chart(trend.set_index("등록일")[["오차(%p)", "절대오차"]])
            st.caption(
                "**오차**가 0선 근처로 모이면 편향이 잡히는 것, **절대오차**가"
                " 내려가면 정확도가 올라가는 것입니다."
            )
        else:
            st.info(
                "아직 개찰 결과가 입력된 기록이 없습니다. 아래 버튼으로 자동"
                " 수집해보세요."
            )

    st.markdown("---")
    st.markdown("#### 🔄 개찰 결과 자동 채우기")
    st.caption(
        "비드큐의 개찰 데이터에서 내 기록과 같은 공고를 찾아 실제"
        " 사정률·낙찰금액을 자동으로 넣습니다."
    )

    if st.button("🔄 비드큐에서 개찰 결과 가져오기", use_container_width=True):
        if not bidq_id or not bidq_pw:
            st.error("사이드바에 비드큐 아이디/비밀번호를 입력해 주세요.")
        elif df.empty:
            st.warning("채울 기록이 없습니다.")
        else:
            try:
                session = get_session(bidq_id, bidq_pw)
                targets = df[df["실제낙찰사정률"].astype(str).str.strip() == ""]
                if targets.empty:
                    st.info("이미 모든 기록에 개찰 결과가 들어있습니다.")
                else:
                    filled, failed = 0, []
                    prog = st.progress(0.0)
                    orgs = list(targets["발주처"].unique())
                    for i, org in enumerate(orgs):
                        rows, _ = fetch_all_opened(
                            session, org, months_back, quiet=True
                        )
                        for idx in targets[targets["발주처"] == org].index:
                            rec, how = find_match(
                                rows,
                                df.at[idx, "공고명"],
                                df.at[idx, "기초금액"],
                            )
                            if not rec:
                                failed.append(df.at[idx, "공고명"])
                                continue
                            rate = to_number(rec.get("success_pct"))
                            if rate is None:
                                failed.append(df.at[idx, "공고명"])
                                continue
                            df.at[idx, "실제낙찰사정률"] = f"{rate:.4f}"
                            df.at[idx, "참여업체수"] = str(
                                rec.get("innum") or ""
                            )
                            win = to_number(rec.get("sucamt")) or to_number(
                                rec.get("succost")
                            )
                            if win:
                                df.at[idx, "실제낙찰금액"] = f"{win:.0f}"
                                mine = to_number(df.at[idx, "내투찰금액"])
                                if mine is not None:
                                    df.at[idx, "상태"] = (
                                        "낙찰"
                                        if abs(mine - win) < 1
                                        else "패찰"
                                    )
                            df.at[idx, "메모"] = (
                                str(df.at[idx, "메모"]) + f" [{how}]"
                            ).strip()
                            filled += 1
                        prog.progress((i + 1) / len(orgs))

                    save_log(df)
                    st.success(f"✅ {filled}건 자동으로 채웠습니다.")
                    if failed:
                        st.warning(
                            f"⚠️ {len(failed)}건은 못 찾았습니다. 아래"
                            " 표에서 직접 입력해주세요.\n\n"
                            + "\n".join(f"- {t}" for t in failed[:10])
                        )
                    st.rerun()
            except Exception as e:
                st.error(f"자동 수집 실패: {e}")

    st.markdown("---")
    st.markdown("#### ✏️ 기록 직접 수정")
    st.caption(
        "표를 직접 고칠 수 있습니다. 맨 아래 빈 줄에 새 기록을 추가할 수도"
        " 있습니다. 고친 뒤 반드시 **저장** 버튼을 누르세요."
    )

    edited = st.data_editor(
        load_log(),
        num_rows="dynamic",
        use_container_width=True,
        key="log_editor",
        column_config={
            "상태": st.column_config.SelectboxColumn(
                "상태", options=["대기", "낙찰", "패찰"]
            ),
            "실제낙찰사정률": st.column_config.TextColumn(
                "실제낙찰사정률", help="예: 100.1234"
            ),
        },
    )
    cc1, cc2 = st.columns(2)
    if cc1.button("💾 수정 내용 저장", use_container_width=True):
        if save_log(edited[LOG_COLUMNS]):
            st.success("저장했습니다.")
            st.rerun()
    cc2.download_button(
        "⬇️ 엑셀(CSV)로 내려받기",
        data=load_log().to_csv(index=False, encoding="utf-8-sig"),
        file_name="bid_log.csv",
        mime="text/csv",
        use_container_width=True,
    )


# ---------------------------------------------------------------
# 탭 3 : 학교별 성향 카드  ★2순위
# ---------------------------------------------------------------
with tab3:
    st.subheader("🏫 학교별 성향 카드")
    st.caption(
        "들어가기 전에 이 학교가 예측 가능한 곳인지 먼저 판단합니다. 안"
        " 들어가는 판단도 수익입니다."
    )

    col1, col2 = st.columns([3, 2])
    org_query = col1.text_input(
        "발주처(학교)명", placeholder="예) 인천OO초등학교"
    )
    legacy_query = col2.text_input(
        "과거 발주처명 (선택)", placeholder="행정구역 변경 전 이름"
    )

    if st.button("🔎 성향 조회", use_container_width=True, type="primary"):
        if not bidq_id or not bidq_pw:
            st.error("사이드바에 비드큐 아이디/비밀번호를 입력해 주세요.")
        elif not org_query.strip():
            st.warning("발주처명을 입력해 주세요.")
        else:
            try:
                with st.spinner("비드큐에서 개찰 이력을 모으는 중..."):
                    session = get_session(bidq_id, bidq_pw)
                    rows, _ = fetch_all_opened(
                        session, org_query.strip(), months_back
                    )
                    old = []
                    if legacy_query.strip():
                        old, _ = fetch_all_opened(
                            session, legacy_query.strip(), months_back
                        )
                    recs = merge_records(rows, old)

                if not recs:
                    st.error(
                        "개찰 이력을 찾지 못했습니다. 이름이 정확한지, 행정구역"
                        " 변경 전 이름이 필요한지 확인해보세요."
                    )
                else:
                    s = compute_stats(recs)
                    if not s:
                        st.error(
                            "사정률 데이터가 없어 통계를 낼 수 없습니다."
                        )
                    else:
                        grade, advice = volatility_grade(s["표준편차"])
                        st.markdown(f"### {grade} — {org_query.strip()}")
                        st.info(advice)

                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("표본 수", f"{s['건수']}건")
                        m2.metric("평균 사정률", f"{s['평균']:.4f}%")
                        m3.metric("표준편차", f"{s['표준편차']:.4f}%p")
                        m4.metric(
                            "평균 업체수",
                            (
                                f"{s['평균업체수']:.1f}개"
                                if s["평균업체수"]
                                else "-"
                            ),
                        )

                        st.markdown(f"""
| 항목 | 값 |
| :--- | ---: |
| 중앙값 | {s['중앙값']:.4f}% |
| 최저 ~ 최고 | {s['최저']:.4f}% ~ {s['최고']:.4f}% |
| 100% 미만 비율 | {s['미만비율']:.1f}% |
""")
                        if s["최근8건"]:
                            st.markdown("#### 최근 흐름")
                            rec_df = pd.DataFrame(
                                s["최근8건"], columns=["날짜", "사정률"]
                            )
                            rec_df = rec_df.sort_values("날짜")
                            st.line_chart(rec_df.set_index("날짜"))
                            avg_recent = rec_df["사정률"].mean()
                            gap = avg_recent - s["평균"]
                            if gap > 0.05:
                                st.caption(
                                    f"최근 8건 평균이 전체 평균보다 {gap:+.4f}%p"
                                    " 높습니다. 위로 쏠려 있어 되돌아올"
                                    " 가능성을 염두에 두세요."
                                )
                            elif gap < -0.05:
                                st.caption(
                                    f"최근 8건 평균이 전체 평균보다 {gap:+.4f}%p"
                                    " 낮습니다. 아래로 쏠려 있어 되돌아올"
                                    " 가능성을 염두에 두세요."
                                )
                            else:
                                st.caption(
                                    "최근 흐름이 전체 평균과 비슷합니다."
                                    " 안정적입니다."
                                )

                        with st.expander(f"📊 개찰 이력 원본 ({len(recs)}건)"):
                            st.dataframe(
                                summarize_history(recs), use_container_width=True
                            )
            except Exception as e:
                st.error(f"조회 실패: {e}")
