"""웹 시뮬레이터의 공고 데이터를 공공 API 에서 받아 index.html 에 갈아끼운다.

브라우저는 공공데이터포털 API 를 CORS 로 직접 부를 수 없다.
그래서 하루 한 번 이 스크립트가 대신 받아 페이지에 구워 넣는다.
(GitHub Actions 가 매일 06:00 KST 에 실행한다 — .github/workflows/daily.yml)

수집 대상
  1) 청약홈 분양정보  — 접수가 끝나지 않은 공고 + 주택형별 분양가
  2) 국토교통부 실거래가 — 각 공고 시·군·구의 최근 아파트 매매 (주변 시세)

실행:
    DATA_GO_KR_KEY=... python scripts/build_data.py [--out web/index.html]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

APPLYHOME_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
RTMS_URL = (
    "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
)
GWANBO_URL = "https://apis.data.go.kr/1741000/ApiTotalService/getApiTotalList"
GWANBO_HOST = "https://gwanbo.go.kr"
KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"

# 청약홈이 주는 다섯 갈래를 모두 본다. 오퍼레이션마다 필드 이름이 달라서
# 여기에 함께 적어 둔다 — 오피스텔에서 APT 필드를 그대로 읽다가 분양가가
# 통째로 빈 적이 있다.
#
#   detail/model : 오퍼레이션 이름
#   begin/end    : 접수 시작·종료 필드
#   ty           : 주택형 이름 필드(여럿이면 앞에서부터 찾는다)
#   area         : 전용면적. None 이면 주택형 문자열에서 뽑는다
#   price        : 분양가(만원)
#   general      : 일반공급 세대수
#   special      : 특별공급 세대수. None 이면 특별공급이 없는 갈래다
#   rental       : 분양이 아니라 임대인가
OPERATIONS = {
    "APT": {
        "detail": "getAPTLttotPblancDetail", "model": "getAPTLttotPblancMdl",
        "begin": ["RCEPT_BGNDE"], "end": ["RCEPT_ENDDE"],
        "ty": ["HOUSE_TY"], "area": None,
        "price": "LTTOT_TOP_AMOUNT",
        "general": "SUPLY_HSHLDCO", "special": "SPSPLY_HSHLDCO",
        "rental": False,
    },
    "OFFICETEL": {
        "detail": "getUrbtyOfctlLttotPblancDetail", "model": "getUrbtyOfctlLttotPblancMdl",
        "begin": ["SUBSCRPT_RCEPT_BGNDE"], "end": ["SUBSCRPT_RCEPT_ENDDE"],
        "ty": ["TP", "GP"], "area": "EXCLUSE_AR",
        "price": "SUPLY_AMOUNT",
        "general": "SUPLY_HSHLDCO", "special": None,
        "rental": False,
    },
    "REMNANT": {
        "detail": "getRemndrLttotPblancDetail", "model": "getRemndrLttotPblancMdl",
        "begin": ["SUBSCRPT_RCEPT_BGNDE", "GNRL_RCEPT_BGNDE"],
        "end": ["SUBSCRPT_RCEPT_ENDDE", "GNRL_RCEPT_ENDDE"],
        "ty": ["HOUSE_TY"], "area": None,
        "price": "LTTOT_TOP_AMOUNT",
        "general": "SUPLY_HSHLDCO", "special": "SPSPLY_HSHLDCO",
        "rental": False,
    },
    "PUBLIC_RENT": {
        "plainDate": True,   # 날짜가 YYYYMMDD 로 온다
        "detail": "getPblPvtRentLttotPblancDetail", "model": "getPblPvtRentLttotPblancMdl",
        "begin": ["SUBSCRPT_RCEPT_BGNDE"], "end": ["SUBSCRPT_RCEPT_ENDDE"],
        "ty": ["TP", "GP"], "area": "EXCLUSE_AR",
        "price": "SUPLY_AMOUNT",
        "general": "GNSPLY_HSHLDCO", "special": None,
        "rental": True,
    },
    "OPTIONAL": {
        "plainDate": True,   # 날짜가 YYYYMMDD 로 온다
        "detail": "getOPTLttotPblancDetail", "model": "getOPTLttotPblancMdl",
        "begin": ["SUBSCRPT_RCEPT_BGNDE", "GNRL_RCEPT_BGNDE"],
        "end": ["SUBSCRPT_RCEPT_ENDDE", "GNRL_RCEPT_ENDDE"],
        "ty": ["HOUSE_TY"], "area": None,
        "price": "LTTOT_TOP_AMOUNT",
        "general": "SUPLY_HSHLDCO", "special": None,
        "rental": False,
    },
}

# 공공지원 민간임대의 특별공급은 이름이 따로다
PUBLIC_RENT_SPECIAL = [
    ("SPSPLY_YGMN_HSHLDCO", "청년"),
    ("SPSPLY_NEW_MRRG_HSHLDCO", "신혼"),
    ("SPSPLY_AGED_HSHLDCO", "고령자"),
]

# SUBSCRPT_AREA_CODE_NM 은 축약형('경기')으로 온다.
AREA_NAMES = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시",
    "인천": "인천광역시", "광주": "광주광역시", "대전": "대전광역시",
    "울산": "울산광역시", "세종": "세종특별자치시", "경기": "경기도",
    "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전남": "전라남도", "경북": "경상북도",
    "경남": "경상남도", "제주": "제주특별자치도",
}

# 실거래가 조회에 쓰는 법정동코드(5자리).
# 실제 응답의 estateAgentSggNm 으로 검증한 값만 넣는다.
# 새 지역 공고가 뜨면 여기에 추가한다 — 없으면 시세만 비고, 공고는 정상 표시된다.
LAWD_CODES = {
    ("서울특별시", "종로구"): "11110", ("서울특별시", "중구"): "11140",
    ("서울특별시", "용산구"): "11170", ("서울특별시", "성동구"): "11200",
    ("서울특별시", "광진구"): "11215", ("서울특별시", "동대문구"): "11230",
    ("서울특별시", "중랑구"): "11260", ("서울특별시", "성북구"): "11290",
    ("서울특별시", "강북구"): "11305", ("서울특별시", "도봉구"): "11320",
    ("서울특별시", "노원구"): "11350", ("서울특별시", "은평구"): "11380",
    ("서울특별시", "서대문구"): "11410", ("서울특별시", "마포구"): "11440",
    ("서울특별시", "양천구"): "11470", ("서울특별시", "강서구"): "11500",
    ("서울특별시", "구로구"): "11530", ("서울특별시", "금천구"): "11545",
    ("서울특별시", "영등포구"): "11560", ("서울특별시", "동작구"): "11590",
    ("서울특별시", "관악구"): "11620", ("서울특별시", "서초구"): "11650",
    ("서울특별시", "강남구"): "11680", ("서울특별시", "송파구"): "11710",
    ("서울특별시", "강동구"): "11740",
    ("인천광역시", "중구"): "28110", ("인천광역시", "동구"): "28140",
    ("인천광역시", "미추홀구"): "28177", ("인천광역시", "연수구"): "28185",
    ("인천광역시", "남동구"): "28200", ("인천광역시", "부평구"): "28237",
    ("인천광역시", "계양구"): "28245", ("인천광역시", "서구"): "28260",
    ("경기도", "수원시"): "41110", ("경기도", "성남시"): "41130",
    ("경기도", "의정부시"): "41150", ("경기도", "안양시"): "41170",
    ("경기도", "부천시"): "41190", ("경기도", "광명시"): "41210",
    ("경기도", "평택시"): "41220", ("경기도", "안산시"): "41270",
    ("경기도", "고양시"): "41280", ("경기도", "과천시"): "41290",
    ("경기도", "구리시"): "41310", ("경기도", "남양주시"): "41360",
    ("경기도", "오산시"): "41370", ("경기도", "시흥시"): "41390",
    ("경기도", "군포시"): "41410", ("경기도", "의왕시"): "41430",
    ("경기도", "하남시"): "41450", ("경기도", "용인시"): "41460",
    ("경기도", "파주시"): "41480", ("경기도", "이천시"): "41500",
    ("경기도", "안성시"): "41550", ("경기도", "김포시"): "41570",
    ("경기도", "화성시"): "41590", ("경기도", "광주시"): "41610",
    ("경기도", "양주시"): "41630", ("경기도", "포천시"): "41650",
    ("부산광역시", "해운대구"): "26350", ("부산광역시", "남구"): "26290",
    ("대구광역시", "수성구"): "27260", ("대전광역시", "유성구"): "30200",
    ("경상남도", "창원시"): "48120",
}

# 특별공급 유형별 세대수 필드
# 공식 명세 기준. NWWDS=NeWlyWeDS(신혼부부), NWBB=NeW BaBy(신생아) 로
# 이름이 헷갈려 한때 둘을 바꿔 매핑하고 있었다.
# 청년·신생아는 공공주택일 때만 값이 있다.
SPECIAL_FIELDS = [
    ("NWWDS_HSHLDCO", "신혼부부"), ("LFE_FRST_HSHLDCO", "생애최초"),
    ("MNYCH_HSHLDCO", "다자녀"), ("OLD_PARNTS_SUPORT_HSHLDCO", "노부모부양"),
    ("NWBB_HSHLDCO", "신생아"), ("YGMN_HSHLDCO", "청년"),
    ("INSTT_RECOMEND_HSHLDCO", "기관추천"),
    ("TRANSR_INSTT_ENFSN_HSHLDCO", "이전기관"), ("ETC_HSHLDCO", "기타"),
]

TIMEOUT = 40

# urllib 이 막힌 환경인지 한 번만 판단한다.
# 매 요청마다 타임아웃(20초+)을 기다리면 전체 수집이 몇 분씩 늘어진다.
_USE_CURL = False


# ---------------------------------------------------------------------------
def fetch(url: str, params: dict, headers: dict | None = None) -> bytes:
    """urllib 우선, 막히면 curl 로 폴백.

    사내망처럼 프록시를 강제하는 환경에서는 urllib 이 직접 나가지 못하는데
    curl 은 시스템 프록시를 타서 동작한다. GitHub Actions 에서는 urllib 로 끝난다.
    """
    global _USE_CURL
    query = urllib.parse.urlencode(params, doseq=True)
    full = f"{url}?{query}"

    if not _USE_CURL:
        try:
            request = urllib.request.Request(full, headers=headers or {})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as res:
                return res.read()
        except Exception:
            _USE_CURL = True  # 이후 요청은 바로 curl 로 간다

    command = ["curl", "-s", "--max-time", str(TIMEOUT)]
    for name, value in (headers or {}).items():
        command += ["-H", f"{name}: {value}"]
    # 프록시를 타면 응답이 빈 채로 끊기는 일이 있다. 한 번은 다시 물어본다.
    result = subprocess.run(command + [full], capture_output=True)
    if not result.stdout.strip():
        result = subprocess.run(command + [full], capture_output=True)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"요청 실패: {url}")
    return result.stdout


def fetch_json(url: str, params: dict):
    return json.loads(fetch(url, params).decode("utf-8"))


def odcloud(operation: str, key: str, params: dict) -> list[dict]:
    body = fetch_json(
        f"{APPLYHOME_BASE}/{operation}", {"serviceKey": key, **params}
    )
    data = body.get("data")
    return data if isinstance(data, list) else []


def area_of(house_ty: str | None) -> float | None:
    """'055.9700A' → 55.97 (전용면적). SUPLY_AR 은 공급면적이라 쓰면 안 된다."""
    match = re.match(r"^\s*(\d+(?:\.\d+)?)", house_ty or "")
    return round(float(match.group(1)), 2) if match else None


def to_int(value) -> int | None:
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    return int(digits) if digits else None


def as_date(value) -> str | None:
    """'20260907' 과 '2026-09-07' 을 모두 '2026-09-07' 로 맞춘다.

    갈래마다 형식이 다르게 온다. 섞인 채로 문자열 비교를 하면 '-' 가
    숫자보다 작아서 마감 판정이 뒤집힌다.
    """
    text = str(value or "").strip()
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) != 8:
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


def to_float(value) -> float | None:
    """'84.5775' → 84.58. 오피스텔 전용면적(EXCLUSE_AR)은 숫자로 그냥 온다."""
    match = re.match(r"^\s*(\d+(?:\.\d+)?)", str(value or ""))
    return round(float(match.group(1)), 2) if match else None


# ---------------------------------------------------------------------------
def geocode(address: str, name: str, kakao_key: str) -> dict | None:
    """주소를 좌표와 행정구역으로 바꾼다.

    청약 공고 주소는 형식이 제각각이라 한 가지 방법으로는 못 잡는다.
    실제 응답에서 확인한 형태들:
      · 정식      "서울특별시 성북구 장위동 68-37 일대"
      · 괄호형    "김포 풍무역세권 B4블록 (경기도 김포시 사우동 458번지 일원)"
      · 지구명    "인천검암역세권 공공주택지구 내 B-1BL"   ← 주소 DB 에 없다
    그래서 주소검색 → 괄호 안 주소 → 단지명 장소검색 → 앞부분만 → 순으로
    시도한다. 하나라도 걸리면 그 결과를 쓴다. 지도에 점을 찍고 시·군·구를
    알아내는 게 목적이라 번지까지 정확할 필요는 없다.

    반환: {"lat", "lng", "sido", "sigungu", "matched"} — 실패하면 None
    """
    if not kakao_key:
        return None

    headers = {"Authorization": f"KakaoAK {kakao_key}"}

    def region_of(document: dict) -> tuple[str, str]:
        """문서에서 시도·시군구를 뽑는다. LAWD_CODES 키 형식에 맞춘다."""
        block = document.get("address") or document.get("road_address") or {}
        sido_short = (block.get("region_1depth_name") or "").strip()
        sigungu = (block.get("region_2depth_name") or "").strip()

        # 장소검색(keyword) 응답에는 region_*depth 가 없다 — 주소 문자열을 쪼갠다
        if not sido_short:
            tokens = (document.get("address_name")
                      or document.get("road_address_name") or "").split()
            if len(tokens) >= 2:
                sido_short, sigungu = tokens[0], tokens[1]

        # "수원시 영통구" 처럼 자치구까지 오면 시 단위로 줄인다(LAWD_CODES 가 시 단위)
        parts = sigungu.split()
        if len(parts) > 1 and parts[0].endswith("시"):
            sigungu = parts[0]

        sido = AREA_NAMES.get(sido_short, sido_short)
        return sido, sigungu

    def query(url: str, text: str, how: str) -> dict | None:
        text = (text or "").strip()
        if len(text) < 2:
            return None
        try:
            body = json.loads(
                fetch(url, {"query": text, "size": 1}, headers=headers).decode("utf-8")
            )
        except Exception:
            return None  # 빈 응답·네트워크 오류는 다음 방법으로 넘어간다
        documents = body.get("documents") or []
        if not documents:
            return None
        try:
            lat, lng = float(documents[0]["y"]), float(documents[0]["x"])
        except (KeyError, TypeError, ValueError):
            return None
        sido, sigungu = region_of(documents[0])
        return {"lat": lat, "lng": lng, "sido": sido, "sigungu": sigungu,
                "matched": how}

    address = (address or "").strip()
    tokens = address.split()
    inner = re.search(r"\(([^)]+)\)", address)

    attempts = [
        (KAKAO_ADDRESS_URL, address, "주소"),
        (KAKAO_ADDRESS_URL, inner.group(1) if inner else "", "괄호 안 주소"),
        (KAKAO_KEYWORD_URL, name, "단지명"),
        (KAKAO_ADDRESS_URL, " ".join(tokens[:3]), "주소(동까지)"),
        (KAKAO_ADDRESS_URL, " ".join(tokens[:2]), "주소(시군구까지)"),
        (KAKAO_KEYWORD_URL, " ".join(tokens[:3]), "지구명"),
        (KAKAO_KEYWORD_URL, address, "주소 장소검색"),
        # 앞의 행정구역을 떼면 지구명만 남는다 — 신도시·택지지구가 여기서 잡힌다
        (KAKAO_KEYWORD_URL, " ".join(tokens[2:]), "지구명(행정구역 제외)"),
    ]
    for url, text, how in attempts:
        if hit := query(url, text, how):
            return hit
    return None


def geocode_apt(sido: str, sigungu: str, dong: str, jibun: str, apt: str,
                kakao_key: str) -> tuple[float, float] | None:
    """실거래 아파트 단지의 좌표.

    지번이 있으면 주소 검색이 가장 정확하다(실거래가 API 가 지번을 준다).
    지번이 없거나 못 찾으면 단지명으로 장소 검색을 한다.
    동(洞)까지만으로 주소 검색을 하면 동 중심 좌표가 돌아와 단지들이
    한 점에 겹치므로, 지번 없이 주소 검색으로 내려가지는 않는다.
    """
    if not kakao_key or not apt:
        return None

    headers = {"Authorization": f"KakaoAK {kakao_key}"}
    # 지번이 있으면 주소 검색이 가장 정확하다. 없거나 못 찾으면 장소 검색으로 간다.
    attempts = []
    if jibun:
        attempts.append((KAKAO_ADDRESS_URL, f"{sido} {sigungu} {dong} {jibun}"))
    attempts += [
        (KAKAO_KEYWORD_URL, f"{sigungu} {dong} {apt}"),
        (KAKAO_KEYWORD_URL, f"{sido} {sigungu} {apt}"),
        (KAKAO_KEYWORD_URL, f"{dong} {apt}"),
    ]
    for url, query in attempts:
        try:
            body = json.loads(
                fetch(url, {"query": query.strip(), "size": 1},
                      headers=headers).decode("utf-8")
            )
        except Exception:
            continue
        documents = body.get("documents") or []
        if not documents:
            continue
        try:
            return float(documents[0]["y"]), float(documents[0]["x"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def collect_notices(key: str, today: date, kakao_key: str = "") -> list[dict]:
    """접수가 끝나지 않은 공고 + 주택형별 분양가."""
    since = (today - timedelta(days=90)).isoformat()
    notices: list[dict] = []

    def first(row: dict, names: list[str]):
        for name in names:
            value = row.get(name)
            if value not in (None, "", " "):
                return value
        return None

    for kind, spec in OPERATIONS.items():
        rows: list[dict] = []
        for page in range(1, 6):
            try:
                # 모집공고일 형식도 갈래마다 다르다. 안 맞으면 조건이 무시된다.
                page_rows = odcloud(spec["detail"], key, {
                    "page": page, "perPage": 100,
                    "cond[RCRIT_PBLANC_DE::GTE]":
                        since.replace("-", "") if spec.get("plainDate") else since,
                })
            except Exception as error:   # 한 갈래가 막혀도 나머지는 살린다
                print(f"  ! {kind} 조회 실패: {error}", file=sys.stderr)
                break
            rows += page_rows
            if len(page_rows) < 100:
                break

        for row in rows:
            begin = as_date(first(row, spec["begin"]))
            end = as_date(first(row, spec["end"])) or begin
            if not end or end < today.isoformat():
                continue  # 이미 마감

            house_manage_no = str(row.get("HOUSE_MANAGE_NO") or "")
            pblanc_no = str(row.get("PBLANC_NO") or "")
            if not house_manage_no or not pblanc_no:
                continue

            address = row.get("HSSPLY_ADRES") or ""
            tokens = address.split()
            sido = AREA_NAMES.get((row.get("SUBSCRPT_AREA_CODE_NM") or "").strip()) or ""
            # 무순위 공고 주소는 시도로 시작하지 않는 경우가 있어 일치할 때만 쓴다
            sigungu = tokens[1] if len(tokens) > 1 and tokens[0] == sido else ""

            # 주소를 좌표로. 주소 문자열로 시·군·구를 못 뽑은 공고도
            # 카카오가 알려주는 행정구역으로 채워진다 → 시세 조회까지 이어진다.
            place = geocode(address, row.get("HOUSE_NM") or "", kakao_key)
            if place:
                sido = sido or place["sido"]
                sigungu = sigungu or place["sigungu"]

            units = []
            for unit in odcloud(spec["model"], key, {
                "page": 1, "perPage": 50,
                "cond[HOUSE_MANAGE_NO::EQ]": house_manage_no,
                "cond[PBLANC_NO::EQ]": pblanc_no,
            }):
                label = first(unit, spec["ty"]) or ""
                # 전용면적은 갈래마다 오는 방식이 다르다.
                # APT·무순위·임의공급은 주택형 문자열이 곧 전용면적('055.9700A'),
                # 오피스텔·공공지원임대는 EXCLUSE_AR 로 따로 온다.
                area = (to_float(unit.get(spec["area"])) if spec["area"]
                        else area_of(label))
                special_fields = (PUBLIC_RENT_SPECIAL if kind == "PUBLIC_RENT"
                                  else SPECIAL_FIELDS)

                units.append({
                    "ty": label,
                    "area": area,
                    "gen": to_int(unit.get(spec["general"])) or 0,
                    "spc": (to_int(unit.get(spec["special"])) or 0
                            if spec["special"] else 0),
                    "price": to_int(unit.get(spec["price"])),
                    "sp": {
                        name: to_int(unit.get(field))
                        for field, name in special_fields
                        if to_int(unit.get(field))
                    },
                })
            units.sort(key=lambda u: u["area"] or 0)
            prices = [u["price"] for u in units if u["price"]]

            # 규제지역은 둘이 함께 지정되는 일이 많다(서울 전역이 그렇다).
            # 겹치면 더 강한 투기과열지구 기준을 쓴다.
            speculation = row.get("SPECLT_RDN_EARTH_AT") == "Y"
            adjustment = row.get("MDAT_TRGET_AREA_SECD") == "Y"
            # 수도권 내 민영 공공주택지구는 가점제 비율이 따로 있다
            metro_public = row.get("NPLN_PRVOPR_PUBLIC_HOUSE_AT") == "Y"
            # 주택상세구분코드 01:민영, 03:국민. 오피스텔·무순위는 이 코드의 뜻이
            # 달라서(01:도시형생활주택 …) APT 에만 적용한다.
            national = kind == "APT" and str(row.get("HOUSE_DTL_SECD") or "") == "03"

            # 같은 오퍼레이션 안에도 갈래가 있다. 무순위와 불법행위 재공급이
            # 한데 오고, APT 에는 민간사전청약·신혼희망타운이 섞인다.
            # 판정은 큰 유형으로 하되 화면에는 정확한 이름을 보여 준다.
            kind_name = (str(row.get("HOUSE_DTL_SECD_NM") or "").strip()
                         or str(row.get("HOUSE_SECD_NM") or "").strip())

            flags = []
            if speculation:
                flags.append("투기과열지구")
            if adjustment:
                flags.append("조정대상지역")
            if row.get("PARCPRC_ULS_AT") == "Y":
                flags.append("분양가상한제")
            if row.get("PUBLIC_HOUSE_EARTH_AT") == "Y":
                flags.append("공공주택지구")
            if row.get("LRSCL_BLDLND_AT") == "Y":
                flags.append("대규모 택지")
            if row.get("IMPRMN_BSNS_AT") == "Y":
                flags.append("정비사업")
            if metro_public:
                flags.append("수도권 민영 공공주택지구")

            notices.append({
                "name": (row.get("HOUSE_NM") or "").strip(),
                "type": kind,
                "sido": sido, "sigungu": sigungu, "addr": address,
                "lat": place["lat"] if place else None,
                "lng": place["lng"] if place else None,
                "builder": (row.get("CNSTRCT_ENTRPS_NM")
                            or row.get("BSNS_MBY_NM") or "")[:40],
                "kindName": kind_name,
                "isRental": spec["rental"],
                "isNationalHousing": national,
                "isSpeculationArea": speculation or adjustment,
                "speculation": speculation,          # 투기과열지구
                "adjustment": adjustment,            # 조정대상지역
                "metroPublicLand": metro_public,     # 수도권 내 민영 공공주택지구
                "price": min(prices) if prices else None,
                "priceMax": max(prices) if prices else None,
                "area": units[0]["area"] if units else None,
                "supply": to_int(row.get("TOT_SUPLY_HSHLDCO")),
                "begin": begin, "end": end,
                "special": as_date(row.get("SPSPLY_RCEPT_BGNDE")),
                "rank1": as_date(row.get("GNRL_RNK1_CRSPAREA_RCPTDE")
                                 or row.get("GNRL_RNK1_ETC_AREA_RCPTDE")
                                 or row.get("GNRL_RCEPT_BGNDE")),
                "announce": as_date(row.get("PRZWNER_PRESNATN_DE")),
                "moveIn": row.get("MVN_PREARNGE_YM"),
                "flags": flags,
                "url": row.get("PBLANC_URL") or row.get("HMPG_ADRES") or "",
                "tel": row.get("MDHS_TELNO") or "",
                "units": units,
            })

    notices.sort(key=lambda n: n["begin"] or "9999")
    return notices


# ---------------------------------------------------------------------------
def collect_trades(key: str, regions: set[tuple[str, str]], today: date,
                   kakao_key: str = "") -> dict:
    """공고가 있는 시·군·구의 최근 아파트 매매 실거래 (주변 시세)."""
    trades: dict[str, list[dict]] = {}

    for sido, sigungu in sorted(regions):
        code = LAWD_CODES.get((sido, sigungu))
        if not code:
            continue  # 코드 미등록 지역 — 시세만 비고 공고는 정상 표시

        rows: list[dict] = []
        # 최근 2개월치를 본다. 이번 달은 거래가 적을 수 있다.
        for back in (0, 1):
            month = date(today.year, today.month, 1) - timedelta(days=back * 28)
            try:
                xml = fetch(RTMS_URL, {
                    "serviceKey": key, "LAWD_CD": code,
                    "DEAL_YMD": month.strftime("%Y%m"), "numOfRows": 200,
                })
            except Exception as error:  # 한 지역 실패가 전체를 막지 않게
                print(f"  ! {sido} {sigungu} 실거래 실패: {error}", file=sys.stderr)
                continue

            for item in ET.fromstring(xml).findall(".//item"):
                def text(tag):
                    return (item.findtext(tag) or "").strip()

                amount = to_int(text("dealAmount"))
                area = text("excluUseAr")
                if not amount or not area:
                    continue
                # 계약이 해제된 거래는 시세가 아니다. 남겨 두면 가격이 왜곡된다.
                if text("cdealType") or text("cdealDay"):
                    continue
                # 토지임대부는 땅값이 빠져 있어 일반 매매와 나란히 둘 수 없다.
                if text("landLeaseholdGbn").upper() == "Y":
                    continue
                rows.append({
                    "apt": text("aptNm"), "dong": text("umdNm"),
                    "jibun": text("jibun"),
                    "area": round(float(area), 2), "amount": amount,
                    "floor": to_int(text("floor")),
                    "year": to_int(text("buildYear")),
                    "ym": f"{text('dealYear')}.{text('dealMonth').zfill(2)}",
                })

        # 단지별로 가장 최근(=목록 뒤쪽) 거래 하나만 남기고, 거래가 많은 순으로
        by_apt: dict[str, dict] = {}
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["apt"]] = counts.get(row["apt"], 0) + 1
            by_apt[row["apt"]] = row
        picked = sorted(by_apt.values(), key=lambda r: -counts[r["apt"]])[:5]
        # 지도에 시세를 함께 찍으려면 단지 좌표가 있어야 한다
        for row in picked:
            spot = geocode_apt(sido, sigungu, row["dong"], row["jibun"],
                               row["apt"], kakao_key)
            row["lat"], row["lng"] = spot if spot else (None, None)
        if picked:
            trades[f"{sido} {sigungu}"] = picked

    return trades


# ---------------------------------------------------------------------------
# 관보 — 법령이 공포되면 반드시 여기에 실린다.
# 「주택공급에 관한 규칙」은 국토교통부령이라 국회를 거치지 않으므로,
# 관보가 개정 시점을 잡는 가장 직접적인 창구다.
#
# 필수 파라미터가 흔치 않은 이름이다. numOfRows 가 아니라 pageSize 고,
# type 은 'json' 이 아니라 '1' 이다(type=json 은 200 에 본문 0바이트가 온다).
GWANBO_KEYWORDS = ["주택", "청약", "분양", "부동산", "주거"]

# 제목에 이 말이 있으면 우리와 상관없는 개별 사업 고시다. 목록이 이런 걸로 덮인다.
GWANBO_NOISE = [
    "행정처분", "지구계획", "사업계획", "준공", "환경영향평가", "공람",
    "지장물", "수용", "재결", "감정평가", "실시계획", "지구단위계획",
]

# 우리에게 중요한 순서. 규칙·법률 개정이 위로 오게 한다.
GWANBO_WEIGHTS = {
    "주택공급에 관한 규칙": 100, "주택법": 70, "공공주택 특별법": 60,
    "청약": 55, "분양가": 50, "주택공급": 45,
    "시행령": 20, "시행규칙": 20, "일부개정령": 25, "입법예고": 15,
}


def collect_policies(key: str, today: date) -> list[dict]:
    """최근 3개월 관보에서 주택·청약 관련 공포·입법예고만 추린다."""
    since = today - timedelta(days=90)
    seen: dict[str, dict] = {}

    for word in GWANBO_KEYWORDS:
        try:
            body = json.loads(fetch(GWANBO_URL, {
                "serviceKey": key, "pageNo": 1, "pageSize": 100,
                "reqFrom": since.strftime("%Y%m%d"),
                "reqTo": today.strftime("%Y%m%d"),
                "search": word, "type": 1,
            }).decode("utf-8"))
        except Exception as error:
            print(f"  ! 관보 '{word}' 조회 실패: {error}", file=sys.stderr)
            continue

        items = ((body.get("response") or {}).get("items") or {}).get("item") or []
        if isinstance(items, dict):
            items = [items]

        for item in items:
            title = (item.get("cntntSj") or "").strip()
            if not title or any(noise in title for noise in GWANBO_NOISE):
                continue

            key_no = item.get("cntntSeqNo") or title
            if key_no in seen:
                continue

            score = sum(w for k, w in GWANBO_WEIGHTS.items() if k in title)
            pdf = item.get("pdfFilePath") or ""
            seen[key_no] = {
                "title": title[:200],
                "org": (item.get("pblcnInstNm") or "").strip(),
                "date": (item.get("hopePblictDt") or "").replace(".", "-"),
                "kind": (item.get("cmplatSeNm") or "").strip(),
                "law": (item.get("basisLawNm") or "").strip(),
                "summary": (item.get("rvsnRsnMainCn") or "").strip()[:400] or None,
                "url": (GWANBO_HOST + pdf) if pdf.startswith("/") else (pdf or None),
                "score": score,
            }

    rows = sorted(seen.values(), key=lambda r: (-r["score"], r["date"]), reverse=False)
    rows.sort(key=lambda r: (-r["score"], r["date"]))
    return rows[:30]


# ---------------------------------------------------------------------------
def main() -> int:
    # 윈도우 콘솔(cp949)에서 한글·기호 출력이 깨지지 않게
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="index.html")
    args = parser.parse_args()

    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        print("DATA_GO_KR_KEY 환경변수가 필요합니다.", file=sys.stderr)
        return 1

    # 카카오 키는 없어도 된다 — 좌표만 비고 나머지는 그대로 동작한다.
    kakao_key = os.environ.get("KAKAO_REST_API_KEY", "").strip()
    if not kakao_key:
        print("      (KAKAO_REST_API_KEY 없음 — 좌표 없이 진행합니다)")

    # GitHub 러너는 UTC 로 돈다. 06:00 KST 는 전날 21:00 UTC 라
    # date.today() 를 쓰면 기준일이 하루 전으로 잡히고 마감 판정도 어긋난다.
    today = datetime.now(timezone(timedelta(hours=9))).date()
    print(f"[1/4] 청약 공고 수집 (기준일 {today})")
    notices = collect_notices(key, today, kakao_key)
    priced = sum(1 for n in notices if n["price"])
    located = sum(1 for n in notices if n["lat"])
    print(f"      접수 진행/예정 {len(notices)}건 · 분양가 확보 {priced}건"
          f" · 좌표 확보 {located}건")

    regions = {(n["sido"], n["sigungu"]) for n in notices if n["sido"] and n["sigungu"]}
    print(f"[2/4] 주변 시세 수집 ({len(regions)}개 시·군·구)")
    trades = collect_trades(key, regions, today, kakao_key)
    rows = [row for group in trades.values() for row in group]
    print(f"      시세 확보 {len(trades)}개 지역 · 단지 {len(rows)}곳"
          f" (좌표 {sum(1 for r in rows if r['lat'])}곳)")

    print("[3/4] 정책·법령 수집 (관보)")
    policies = collect_policies(key, today)
    print(f"      주택 관련 {len(policies)}건")

    print(f"[4/4] {args.out} 갱신")
    path = __import__("pathlib").Path(args.out)
    html = path.read_text(encoding="utf-8")

    block = (
        "  /* DATA:BEGIN — scripts/build_data.py 가 매일 갈아끼운다. 직접 고치지 말 것 */\n"
        "  var NOTICES = "
        + json.dumps(notices, ensure_ascii=False, indent=2).replace("\n", "\n  ")
        + ";\n  var TRADES = "
        + json.dumps(trades, ensure_ascii=False, indent=2).replace("\n", "\n  ")
        + ";\n  var POLICIES = "
        + json.dumps(policies, ensure_ascii=False, indent=2).replace("\n", "\n  ")
        + ";\n"
        + f'  var DATA_DATE = "{today.isoformat()}";\n'
        "  /* DATA:END */"
    )

    updated = re.sub(
        r"  /\* DATA:BEGIN.*?/\* DATA:END \*/",
        lambda _: block,
        html,
        count=1,
        flags=re.S,
    )
    if updated == html:
        print("      DATA 마커를 찾지 못했습니다.", file=sys.stderr)
        return 1

    path.write_text(updated, encoding="utf-8")
    print(f"      완료: 공고 {len(notices)}건, 시세 {len(trades)}개 지역, "
          f"정책 {len(policies)}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
