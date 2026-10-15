from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from statistics import median
import hashlib
import json
D = Decimal
METHODS = ('fixed_100', 'all_mean', 'item_mean', 'item_median', 'recent_item3', 'item_date_mean', 'recent_item_weighted')
COUNTERFACTUAL = '기록된 예정가격·타 업체 가격을 고정한 가상 비교이며 실제 낙찰 결과가 아님'
ENGINE_VERSION = '2026-10-15.school-evidence-v4'

def core_number(value, name='number'):
    if isinstance(value, bool) or value is None:
        raise ValueError(f'{name}: 유한 숫자가 필요합니다')
    try:
        result = D(str(value).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f'{name}: 숫자 형식 오류') from None
    if not result.is_finite():
        raise ValueError(f'{name}: 유한 숫자가 필요합니다')
    return result

def _variation(value):
    result = core_number(value, '예가변동폭')
    if not D('0') < result <= D('3'):
        raise ValueError('예가변동폭은 0 초과 3 이하의 %p로 명시해야 합니다')
    return result

def _rate(value, variation=3):
    result, width = (core_number(value, '예가율'), _variation(variation))
    if not D('100') - width <= result <= D('100') + width:
        raise ValueError('예가율은 예정가격/기초금액×100입니다. 업체 기초대비 투찰률과 혼동하지 마세요')
    return result

def _money(value, name):
    result = core_number(value, name)
    if result <= 0 or result != result.to_integral_value():
        raise ValueError(f'{name}: 양의 원 단위 정수가 필요합니다')
    return result

def _floor(value):
    result = core_number(value, '하한율')
    if result not in (D('88'), D('90')):
        raise ValueError('현재 지원하는 원문 하한율은 88% 또는 90%입니다')
    return result

def _date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        raise ValueError('date: YYYY-MM-DD 개찰 날짜가 필요합니다') from None

def calculate_bid(base, rate, floor, variation=3):
    base, rate, floor = (_money(base, '기초금액'), _rate(rate, variation), _floor(floor))
    with localcontext() as ctx:
        ctx.prec = 50
        expected = base * rate / D('100')
        exact = expected * floor / D('100')
        price = int(exact.to_integral_value(rounding=ROUND_CEILING))
        boundary = D(price) * D('10000') / (base * floor)
    return {'price': price, 'exact_expected': expected, 'exact_bid': exact, 'boundary': boundary, 'rate': rate, 'floor': floor, 'formula': 'ceil(base × assumed_rate × floor / 10000)', 'boundary_rule': '실제 예가율이 boundary보다 크면 하한 미달'}

def normalize_records(rows, variation=3):
    """Require documented denominators; reject conflicts instead of repairing data."""
    unique = {}
    for row in rows:
        record = dict(row)
        for key in ('notice', 'date', 'commodity', 'base', 'planned', 'floor'):
            if key not in record or record[key] in (None, ''):
                raise ValueError(f"{record.get('notice') or record.get('notice_number') or '공고번호 미확인'}: {key} 필수 과거자료 누락")
        record['notice'] = str(record['notice']).strip()
        record['commodity'] = str(record['commodity']).strip()
        record['date'] = _date(record['date'])
        record['base'] = _money(record['base'], '기초금액')
        record['planned'] = _money(record['planned'], '예정가격')
        record['floor'] = _floor(record['floor'])
        record['rate'] = _rate(record['planned'] / record['base'] * 100, variation)
        record['winning'] = _money(record['winning'], '기록상 1순위 금액') if record.get('winning') not in (None, '') else None
        if record['winning'] is not None and record['winning'] < record['planned'] * record['floor'] / 100:
            raise ValueError(f"{record['notice']}: 기록상 1순위가 하한 미달, 원문 확인 필요")
        record.setdefault('participants', None)
        previous = unique.get(record['notice'])
        if previous is not None:
            keys = ('date', 'commodity', 'base', 'planned', 'floor', 'winning', 'participants')
            if any((previous[k] != record[k] for k in keys)):
                raise ValueError(f"공고번호 {record['notice']} 중복 자료 충돌")
        else:
            unique[record['notice']] = record
    return sorted(unique.values(), key=lambda r: (r['date'], r['notice']))

def stats(rows):
    values = [r['rate'] for r in rows]
    if not values:
        return {'n': 0, 'independent_dates': 0, 'mean': None, 'median': None, 'stdev': None}
    mean = sum(values) / len(values)
    variance = sum(((x - mean) ** 2 for x in values)) / len(values)
    return {'n': len(values), 'independent_dates': len({r['date'] for r in rows}), 'mean': mean, 'median': median(values), 'stdev': variance.sqrt(), 'min': min(values), 'max': max(values), 'start': min((r['date'] for r in rows)), 'end': max((r['date'] for r in rows))}

def date_groups(rows):
    """Equal-date summaries avoid counting concurrent notices as independent evidence."""
    grouped = {}
    for row in rows:
        grouped.setdefault(row['date'], []).append(row)
    return [{'date': day, 'n': len(group), 'mean': stats(group)['mean'], 'min': min((r['rate'] for r in group)), 'max': max((r['rate'] for r in group)), 'notices': [r['notice'] for r in group]} for day, group in sorted(grouped.items())]

def candidate_methods(rows, commodity, floor):
    comparable = [r for r in rows if r['floor'] == _floor(floor)]
    items = [r for r in comparable if r['commodity'] == commodity]
    methods = {'fixed_100': D('100')}
    if comparable:
        methods['all_mean'] = stats(comparable)['mean']
    if items:
        methods['item_mean'] = stats(items)['mean']
        methods['item_median'] = stats(items)['median']
        recent_dates = sorted({r['date'] for r in items}, reverse=True)[:3]
        methods['recent_item3'] = stats([r for r in items if r['date'] in recent_dates])['mean']
        groups = date_groups(items)
        methods['item_date_mean'] = sum((g['mean'] for g in groups)) / len(groups)
        recent = groups[-6:]
        methods['recent_item_weighted'] = sum((g['mean'] * (i + 1) for i, g in enumerate(recent))) / sum(range(1, len(recent) + 1))
    return methods

def evaluate_historical(record, rate):
    bid = calculate_bid(record['base'], rate, record['floor'])
    if D(bid['price']) < record['planned'] * record['floor'] / 100:
        outcome = 'below_floor'
    elif record['winning'] is None:
        outcome = 'first_unknown'
    elif bid['price'] > record['winning']:
        outcome = 'above_first'
    else:
        outcome = 'at_or_below_first'
    return {'notice': record['notice'], 'date': record['date'], 'price': bid['price'], 'rate': rate, 'actual_rate': record['rate'], 'error': abs(rate - record['rate']), 'outcome': outcome, 'interpretation': COUNTERFACTUAL}

def _summary(evaluations):
    output = {'n': len(evaluations), 'mae': None, **{key: 0 for key in ('below_floor', 'above_first', 'at_or_below_first', 'first_unknown')}}
    if evaluations:
        output['mae'] = sum((r['error'] for r in evaluations)) / len(evaluations)
    for row in evaluations:
        output[row['outcome']] += 1
    return output

def walk_forward(rows, commodity=None, floor=None):
    """Predeclared methods, strict prior-date input, chronological holdout."""
    rows = normalize_records(rows)
    targets = [r for r in rows if (commodity is None or r['commodity'] == commodity) and (floor is None or r['floor'] == _floor(floor))]
    dates = sorted({r['date'] for r in targets})
    split = max(1, int(len(dates) * 0.7))
    train_dates = set(dates[:split])
    evaluations = {method: [] for method in METHODS}
    for target in targets:
        prior = [r for r in rows if r['date'] < target['date']]
        for method, rate in candidate_methods(prior, target['commodity'], target['floor']).items():
            result = evaluate_historical(target, rate)
            result['training_notices'] = [r['notice'] for r in prior]
            result['phase'] = 'training' if target['date'] in train_dates else 'holdout'
            evaluations[method].append(result)
    summaries = {}
    for method, values in evaluations.items():
        summaries[method] = {}
        for phase in ('training', 'holdout'):
            selected = [r for r in values if r['phase'] == phase]
            ids = {r['notice'] for r in selected}
            baseline = [r for r in evaluations['fixed_100'] if r['notice'] in ids]
            summaries[method][phase] = {'method': _summary(selected), 'baseline_same_notices': _summary(baseline)}
    eligible = [m for m in METHODS if summaries[m]['training']['method']['n'] >= 5]
    common_ids = set.intersection(*[{r['notice'] for r in evaluations[m] if r['phase'] == 'training'} for m in eligible]) if eligible else set()
    comparable_training = {m: _summary([r for r in evaluations[m] if r['notice'] in common_ids]) for m in eligible}
    common_dates = {r['date'] for r in rows if r['notice'] in common_ids}
    chosen = min(eligible, key=lambda m: (comparable_training[m]['mae'], METHODS.index(m))) if len(common_dates) >= 5 else 'fixed_100'
    held = summaries[chosen]['holdout']
    measured, baseline = (held['method'], held['baseline_same_notices'])
    held_dates = {r['date'] for r in evaluations[chosen] if r['phase'] == 'holdout'}
    superiority = chosen != 'fixed_100' and len(dates) >= 12 and (len(held_dates) >= 5) and (measured['first_unknown'] == 0) and (measured['mae'] < baseline['mae']) and (measured['below_floor'] <= baseline['below_floor']) and (measured['at_or_below_first'] > baseline['at_or_below_first'])
    checks = {'non_baseline_selected_on_training': chosen != 'fixed_100', 'at_least_5_common_training_dates': len(common_dates) >= 5, 'at_least_12_target_dates': len(dates) >= 12, 'at_least_5_holdout_dates': len(held_dates) >= 5, 'winning_prices_known_on_holdout': measured['n'] > 0 and measured['first_unknown'] == 0, 'holdout_error_improves': measured['mae'] is not None and baseline['mae'] is not None and (measured['mae'] < baseline['mae']), 'holdout_below_floor_not_worse': measured['below_floor'] <= baseline['below_floor'], 'holdout_valid_price_competition_improves': measured['at_or_below_first'] > baseline['at_or_below_first']}
    return {'selection_checks': checks, 'unmet_checks': [key for key, passed in checks.items() if not passed], 'selected_on_training': chosen, 'selected_method': chosen if superiority else None, 'verified_superiority': superiority, 'confidence': 'limited' if superiority else 'low', 'summaries': summaries, 'evaluations': evaluations, 'training_dates': sorted(train_dates), 'holdout_dates': dates[split:], 'selection_common_notices': sorted(common_ids), 'target_scope': {'commodity': commodity, 'floor': floor, 'records': len(targets), 'independent_dates': len(dates)}, 'interpretation': COUNTERFACTUAL, 'selection_rule': '사전 정의 후보의 학습 MAE로 1개 선택, 별도 평가에서 단순 기준 대비 오차·가격경쟁 개선 확인; 우위 불명확 시 통계 추천 보류; 100%는 비교 기준만 유지'}

def report_analysis(notice, rows, cost_floor=None):
    allowed = {'육류', '농산물'}
    if notice.get('commodity') not in allowed:
        raise ValueError('현재 공고는 축산물·농산물만 분석합니다')
    base, floor = (_money(notice['base'], '기초금액'), _floor(notice['floor']))
    variation = _variation(notice.get('variation', 3))
    selected_rows = [row for row in rows if row.get('commodity') in allowed]
    excluded_rows = [row for row in rows if row.get('commodity') not in allowed]
    normalized = normalize_records(selected_rows, variation)
    cutoff = _date(notice.get('date', date.today()))
    cutoff_date = date.fromisoformat(cutoff)
    try:
        start = cutoff_date.replace(year=cutoff_date.year - 2).isoformat()
    except ValueError:
        start = cutoff_date.replace(year=cutoff_date.year - 2, day=28).isoformat()
    history = [r for r in normalized if start <= r['date'] < cutoff]
    methods = candidate_methods(history, notice['commodity'], floor)
    validation = walk_forward(history, notice['commodity'], floor)
    chosen = validation['selected_method']
    if chosen not in methods:
        chosen = None
    min_cost = _money(cost_floor, '최저수용가') if cost_floor is not None else None
    candidates = []
    for method, rate in methods.items():
        candidate = {'method': method, **calculate_bid(base, rate, floor, variation)}
        candidate['cost_acceptable'] = None if min_cost is None else candidate['price'] >= min_cost
        candidate['validation'] = validation['summaries'][method]
        candidates.append(candidate)
    for label, rate in (('low_scenario', D('100') - variation), ('high_scenario', D('100') + variation)):
        candidates.append({'method': label, **calculate_bid(base, rate, floor, variation), 'cost_acceptable': None if min_cost is None else calculate_bid(base, rate, floor, variation)['price'] >= min_cost, 'validation': None, 'scenario': '공고 예가변동폭의 극단값 민감도; 예측값 아님'})
    main = next((c for c in candidates if c['method'] == chosen), None)
    comparison = next((c for c in candidates if c['method'] == 'fixed_100'))
    reference = main or comparison
    for candidate in candidates:
        candidate['difference'] = candidate['price'] - reference['price']
        candidate['difference_reference'] = reference['method']
    comparable = [r for r in history if r['floor'] == floor]
    items = [r for r in comparable if r['commodity'] == notice['commodity']]
    groups = date_groups(items)
    diagnostic_reasons = []
    if not history:
        diagnostic_reasons.append('기준일 이전 24개월 사용 가능한 학교 자료가 없습니다')
    elif not comparable:
        diagnostic_reasons.append('현재 하한율과 같은 학교 자료가 없습니다')
    elif not items:
        diagnostic_reasons.append('현재 품목·하한율이 모두 같은 자료가 없습니다; 기관 평균은 다른 품목 참고값입니다')
    if not validation['verified_superiority']:
        diagnostic_reasons.append('독립 평가에서 통계 우위 미확인; 100% 자동 추천을 하지 않습니다')
    diagnostics = {'input_rows': len(rows), 'eligible_rows': len(selected_rows), 'excluded_rows': len(excluded_rows), 'included_by_commodity': {kind: sum(r['commodity'] == kind for r in normalized) for kind in ('육류', '농산물')}, 'excluded_by_commodity': {str(kind): sum(r.get('commodity') == kind for r in excluded_rows) for kind in {r.get('commodity') for r in excluded_rows}}, 'unique_rows': len(normalized), 'window_rows': len(history), 'same_floor_rows': len(comparable), 'same_item_floor_rows': len(items), 'same_item_independent_dates': len(groups), 'candidate_methods': list(methods), 'training_dates': len(validation['training_dates']), 'holdout_dates': len(validation['holdout_dates']), 'unmet_validation_checks': validation['unmet_checks'], 'reasons': diagnostic_reasons}
    evidence = {'history': [{k: r.get(k) for k in ('notice', 'date', 'commodity', 'base', 'planned', 'floor', 'rate', 'winning', 'participants')} for r in history], 'item_date_groups': groups, 'recent_item_dates': groups[-6:], 'same_floor_date_groups': date_groups(comparable), 'cutoff': cutoff, 'window_start': start, 'weighting_rule': '최근 동일 품목·하한율 최대 6개 개찰일의 일별 평균에 오래된 순서로 1..N 가중치; 사전 정의 시험 후보이며 검증된 법칙 아님', 'scope': '현재 학교의 수집 자료만 사용; 다른 학교 자료를 수집한 것으로 간주하지 않음'}
    coverage = dict(notice.get('coverage', {}))
    coverage.update({'source_rows': len(rows), 'excluded_rows': len(excluded_rows), 'collected_rows': len(normalized), 'used_rows': len(history), 'independent_dates': len({r['date'] for r in history}), 'cutoff': cutoff, 'window_start': start, 'excluded_same_day_or_future': sum((r['date'] >= cutoff for r in normalized))})
    required = ('notice_verified', 'notice_source_complete', 'qualification_verified', 'correction_verified', 'history_24_months_complete', 'all_pages_complete', 'three_analyses_complete', 'multiple_estimates_verified', 'competition_verified')
    missing = [key for key in required if coverage.get(key) is not True]
    data_required = ('notice_verified', 'notice_source_complete', 'correction_verified', 'history_24_months_complete', 'all_pages_complete')
    data_missing = [key for key in data_required if coverage.get(key) is not True]
    eligibility = 'verified' if coverage.get('qualification_verified') is True else 'user_documents_required'
    identity = {'engine': ENGINE_VERSION, 'notice': {'id': str(notice.get('notice', notice.get('notice_number', notice.get('code', '')))), 'base': str(base), 'floor': str(floor), 'commodity': notice['commodity'], 'variation': str(variation), 'cutoff': cutoff}, 'rows': [{key: str(row[key]) if isinstance(row[key], D) else row[key] for key in ('notice', 'date', 'commodity', 'base', 'planned', 'floor', 'winning')} for row in history], 'cost_floor': None if min_cost is None else str(min_cost)}
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    warnings = []
    if missing:
        warnings.append('잠정 후보·필수 확인 미완료: ' + ', '.join(missing))
    if not validation['verified_superiority']:
        warnings.append('통계 우위 미확인: 자동 추천 보류. 100%는 비교 기준이며 AI의 학교별 근거 판단이 필요합니다')
    if min_cost is None:
        warnings.append('원가·배송·손질·폐기·세금 미확인: 수익성 미검증')
    elif main is not None and (not main['cost_acceptable']):
        warnings.append('주후보가 사용자 최저수용가 미만입니다. 자동 가산하지 않으며 제출 후보로 부적합합니다')
    return {'main_candidate': main, 'comparison_candidate': comparison, 'recommendation_status': 'statistical_selection' if main else 'awaiting_ai_selection', 'diagnostics': diagnostics, 'school_evidence': evidence, 'alternatives': [c for c in candidates if c is not main], 'candidates': candidates, 'coverage': coverage, 'validation': validation, 'confidence': validation['confidence'], 'warnings': warnings, 'status': 'verified' if not missing else 'provisional', 'data_status': 'verified' if not data_missing else 'incomplete', 'eligibility_status': eligibility, 'replay': {'engine_version': ENGINE_VERSION, 'input_sha256': fingerprint, 'guarantee': '같은 버전·공고 조건·원자료·기준일·최저수용가의 통계 후보 및 금액 계산은 동일; AI 재호출 선택은 달라질 수 있음', 'limitation': '과거 수동 판단의 고정 사정률 또는 다른 원자료와 결과 일치를 보장하지 않음'}, 'profitability': 'unverified' if min_cost is None else 'selection_pending' if main is None else 'cost_floor_pass' if main['cost_acceptable'] else 'cost_floor_fail', 'statistics': {'all_same_floor': stats([r for r in history if r['floor'] == floor]), 'item_same_floor': stats([r for r in history if r['floor'] == floor and r['commodity'] == notice['commodity']])}, 'reason': '별도 평가 우위 확인' if validation['verified_superiority'] else '통계 추천 보류: 학교 자료에 근거한 후보 선택 필요', 'counterargument': '실제 예가율이 미달 경계를 넘으면 무효이며 높은 후보는 기록상 1순위보다 비쌀 수 있습니다', 'interpretation': COUNTERFACTUAL}
import re
from html import unescape
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup

def ne_num(value):
    match = re.fullmatch('\\s*([+-]?\\d[\\d,]*(?:\\.\\d+)?)\\s*%?\\s*', str(value or ''))
    return float(match[1].replace(',', '')) if match else None

def collect_notice_source_evidence(client, html, notice):
    """Resolve only the exact BidQ mirror loader published by this notice.

    Observed frontend: $('#content-box').load('bid-content?bidid=...&bidtype=pur&selected_file_index=')
    This is a BidQ-hosted mirror, never a request to the external original.
    """
    soup = BeautifulSoup(html, 'html.parser')
    scripts = '\n'.join((s.get_text() for s in soup.select('script:not([src])')))
    source_url = notice.get('source_url', '')
    source_bidid = parse_qs(urlparse(source_url).query).get('bidid', [None])[0]
    mirror_urls = re.findall('\\$\\(\\s*[\'\\"]#content-box[\'\\"]\\s*\\)\\s*\\.load\\(\\s*[\'\\"]([^\'\\"]+)[\'\\"]', scripts)
    fetched = []
    reason = None
    content = soup.select_one('#content-box')
    if content is not None and (not content.select('td.item')):
        candidates = list(dict.fromkeys((unescape(item) for item in mirror_urls)))
        if len(candidates) != 1:
            reason = '비드큐 공고원문 로더 경로 누락 또는 상충'
        else:
            address = urljoin(source_url, candidates[0])
            parsed = urlparse(address)
            query = parse_qs(parsed.query)
            if parsed.scheme != 'https' or parsed.hostname not in {'www.bidq.co.kr', 'bidq.co.kr'} or parsed.path != '/bidq/bids/detail/bid-content' or (not source_bidid) or (query.get('bidid') != [source_bidid]) or (query.get('bidtype') != ['pur']):
                reason = '공고원문 로더의 도메인 또는 공고 식별자 불일치'
            else:
                response = client._request('GET', address)
                loaded = BeautifulSoup(response.text, 'html.parser')
                content.clear()
                for element in list(loaded.contents):
                    content.append(element)
                fetched.append(address)
    result = notice_source_evidence(str(soup), notice)
    result['mirror_source_urls'] = fetched
    result['mirror_load_reason'] = reason
    return result

def notice_source_evidence(html, notice):
    soup = BeautifulSoup(html, 'html.parser')
    source = soup.select_one('#content-box')
    fields = {}
    if source:
        for cell in source.select('td.item'):
            value = cell.find_next_sibling('td')
            if value:
                fields[re.sub('\\s+', '', cell.get_text(' ', strip=True))] = value.get_text(' ', strip=True)
    current = str(notice.get('notice_number') or '')
    mirrored = fields.get('공고번호-차수', '')
    base = ne_num(fields.get('기초가격(입찰시작가격)'))
    award = fields.get('낙찰방식', '')
    floor_match = re.search('예정가격의\\s*\\[?\\s*(\\d+(?:\\.\\d+)?)\\s*\\]?\\s*%', award)
    floor = float(floor_match[1]) if floor_match else None
    matching = bool(current and mirrored == current and (base == notice.get('base')) and (floor == notice.get('floor')))
    required = ('제한조건', '자격제한', '납품기간/시간', '납품장소', '낙찰방식')
    complete = matching and all((fields.get(key) for key in required))
    history = soup.select_one('#history_popover_content')
    correction_ids = sorted(set(re.findall('E\\d{6}-\\d+-\\d+', history.get_text(' ', strip=True)))) if history else []
    scripts = '\n'.join((s.get_text() for s in soup.select('script:not([src])')))
    history_placeholder = bool(re.search('\\$\\(\\s*[\'\\"]#history_popover_content[\'\\"]\\s*\\)\\s*\\.(?:load\\(|html\\(\\s*(?:data|response))', scripts))
    correction_complete = bool(history is not None and (not history_placeholder) and current.endswith('-0') and matching and (not correction_ids))
    correction_links = []
    if history:
        for anchor in history.select('a[href]'):
            address = urljoin(notice.get('source_url', ''), anchor['href'])
            if urlparse(address).hostname in {'www.bidq.co.kr', 'bidq.co.kr'}:
                correction_links.append(address)
    linked_ids = []
    if history:
        for anchor in history.select('a[href]'):
            address = urljoin(notice.get('source_url', ''), anchor['href'])
            ids = re.findall('E\\d{6}-\\d+-\\d+', anchor.get_text(' ', strip=True))
            if urlparse(address).hostname in {'www.bidq.co.kr', 'bidq.co.kr'} and '/bids/detail/' in urlparse(address).path and parse_qs(urlparse(address).query).get('bidid'):
                linked_ids.extend(ids)
    family, _, revision_text = current.rpartition('-')
    if revision_text.isdigit() and int(revision_text) > 0 and matching and (not history_placeholder):
        chain = {family + '-' + str(i) for i in range(int(revision_text) + 1)}
        same_family = {item for item in correction_ids if item.rpartition('-')[0] == family}
        correction_complete = same_family == chain and set(linked_ids) >= chain
    return {'source_url': notice.get('source_url'), 'notice_number': current, 'mirror_notice_number': mirrored, 'mirrored_fields': fields, 'notice_source_complete': complete, 'conditions_source_complete': complete, 'qualification_source_complete': complete, 'qualification_verified': False, 'business_eligibility': 'unknown', 'eligibility_reason': '공고의 참가조건은 수집했으나 업체 인증·소재지·수의계약 자격은 증빙 미확인', 'correction_verified': correction_complete, 'correction_notice_numbers': correction_ids, 'correction_links': correction_links, 'attachments_external_not_fetched': [a.get_text(' ', strip=True) for a in source.select('a[href]')] if source else [], 'external_original_verified': False, 'correction_reason': '비드큐 게시 정정 내역 확인' if correction_complete else '정정 공고 연결 및 최신 차수 확인 필요'}

def ne_competitor_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    for table in soup.select('table'):
        trs = table.select('tr')
        if not trs:
            continue
        headers = [re.sub('\\s+', '', c.get_text(' ', strip=True)) for c in trs[0].find_all(['th', 'td'])]
        if not {'등록번호', '업체명', '투찰금액'} <= set(headers):
            continue
        for tr in trs[1:]:
            cells = tr.find_all('td', recursive=False)
            if len(cells) != len(headers):
                continue
            fields = dict(zip(headers, [c.get_text(' ', strip=True) for c in cells]))
            registration = re.sub('\\D', '', fields['등록번호'])
            amount = ne_num(fields['투찰금액'])
            if len(registration) != 10 or amount is None:
                raise ValueError('업체 등록번호 또는 투찰금액 원자료 미확인')
            reported = fields.get('업체별사정율(%)', '')
            rate_match = re.match('([+-]?\\d+(?:\\.\\d+)?)', reported)
            rows.append({'company': fields['업체명'], 'business_registration_number': registration, 'amount': amount, 'rank': ne_num(fields.get('순위')), 'status': fields.get('비고'), 'raw_fields': fields, 'rate_planned_denominator': ne_num(fields.get('투찰율(%)')), 'rate_base_denominator': ne_num(fields.get('기초대비(%)')), 'reported_bid_rate': float(rate_match[1]) if rate_match else None})
    pages = set()
    active = None
    for a in soup.select('.pagination a'):
        match = re.search('movePage\\((\\d+)\\)', a.get('onclick', ''))
        if match:
            pages.add(int(match[1]))
        if 'active' in (a.parent.get('class') or []) and a.get_text(strip=True).isdigit():
            active = int(a.get_text(strip=True))
    return (rows, pages, active)

def collect_competitor_evidence(client, html, source_url, expected_count=None, max_pages=200):
    """Follow exact protocol published inline by BidQ detail/suc.
    GET relative comlist with resetSuccomParams; page links expose movePage(n).
    Count, unique registration, page sequence and empty end must reconcile.
    """
    soup = BeautifulSoup(html, 'html.parser')
    script = '\n'.join((s.get_text() for s in soup.select('script:not([src])')))
    protocol = re.search('\\$\\.get\\(\\s*[\'\\"]comlist[\'\\"]\\s*,\\s*succom_params', script)
    bidid_match = re.search('[\'\\"]bidid[\'\\"]\\s*:\\s*[\'\\"]([^\'\\"]+)[\'\\"]', script)
    bidtype_match = re.search('[\'\\"]bidtype[\'\\"]\\s*:\\s*[\'\\"]([^\'\\"]+)[\'\\"]', script)
    query_bidid = parse_qs(urlparse(source_url).query).get('bidid', [None])[0]
    count_match = re.search('참여업체수\\s*[:：]\\s*([\\d,]+)', soup.get_text(' ', strip=True))
    reported_count = int(count_match[1].replace(',', '')) if count_match else expected_count
    empty = {'rows': [], 'collected': 0, 'expected': reported_count, 'status': 'missing', 'complete': False, 'source_url': source_url, 'reason': '참여업체 Ajax 프로토콜 또는 총건수 미확인'}
    if not protocol or not bidid_match or (not bidtype_match) or (bidid_match[1] != query_bidid) or (reported_count is None):
        return empty
    if expected_count is not None and int(expected_count) != int(reported_count):
        return {**empty, 'reason': '참여업체 총건수 원자료 상충'}
    endpoint = urljoin(source_url, 'comlist')
    params = {'bidid': query_bidid, 'bidtype': bidtype_match[1], 'search_type': '', 'search_word': '', 'hahan': '', 'sort': 'rank2', 'page': 1}
    rows, seen, pending, visited = ([], set(), {1}, [])
    complete = True
    reason = ''
    while pending:
        page = min(pending)
        pending.remove(page)
        if page in visited or len(visited) >= max_pages:
            complete, reason = (False, '업체 페이지 안전 상한 도달')
            break
        params['page'] = page
        response = client._request('GET', endpoint, params=dict(params))
        try:
            parsed, advertised, active = ne_competitor_page(response.text)
        except ValueError as error:
            return {'rows': rows, 'collected': len(rows), 'expected': reported_count,
                    'status': 'partial' if rows else 'missing', 'complete': False,
                    'source_url': source_url, 'endpoint': endpoint, 'pages': visited + [page],
                    'reason': str(error)}
        visited.append(page)
        if active is not None and active != page:
            complete, reason = (False, '요청 업체 페이지와 응답 페이지 불일치')
            break
        for row in parsed:
            identity = row['business_registration_number']
            if identity in seen:
                complete, reason = (False, '업체 중복 또는 페이지 반복 응답')
                break
            seen.add(identity)
            rows.append(row)
        if not complete:
            break
        pending.update(advertised - set(visited))
        if len(rows) > int(reported_count):
            complete, reason = (False, '업체 총건수 초과')
            break
        if not parsed and len(rows) != int(reported_count):
            complete, reason = (False, '업체 페이지 조기 종료')
            break
    complete = complete and len(rows) == int(reported_count) and (visited == list(range(1, len(visited) + 1)))
    return {'rows': rows, 'collected': len(rows), 'expected': reported_count, 'status': 'complete' if complete else 'partial' if rows else 'missing', 'complete': complete, 'source_url': source_url, 'endpoint': endpoint, 'pages': visited, 'reason': '등록번호 중복 없이 총건수 및 게시된 전체 페이지 일치' if complete else reason or '업체 총건수/페이지 미일치'}
import calendar
import logging
import re
from datetime import date, datetime, timezone, timedelta
from time import monotonic
from urllib.parse import urljoin, urlparse, parse_qs, quote, urlunparse
import requests
from bs4 import BeautifulSoup
BASE = 'https://www.bidq.co.kr'
logger = logging.getLogger(__name__)
DISTRICT_HINTS = {'서구': ('서해구', '검단구'), '서해구': ('서구',), '검단구': ('서구',), '동구': ('제물포구',), '제물포구': ('중구', '동구'), '중구': ('제물포구', '영종구'), '영종구': ('중구',)}

class CollectionError(RuntimeError):
    pass

def subtract_months(day, months):
    if not isinstance(months, int) or months < 1 or months > 120:
        raise ValueError('조회 기간은 1~120개월 정수여야 합니다.')
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))

def client_number(value):
    if value is None or isinstance(value, bool):
        return None
    match = re.fullmatch('\\s*([+-]?\\d[\\d,]*(?:\\.\\d+)?)\\s*(?:원|%|개|명)?\\s*', str(value))
    return float(match[1].replace(',', '')) if match else None

def labels(soup):
    result = {}
    for cell in soup.find_all(['th', 'dt', 'td']):
        sibling = cell.find_next_sibling(['td', 'dd'])
        if sibling:
            key = re.sub('\\s+', '', cell.get_text(' ', strip=True))
            cleaned = BeautifulSoup(str(sibling), 'html.parser')
            for action in cleaned.select('button, [role="button"]'):
                action.decompose()
            for anchor in cleaned.select('a'):
                if re.fullmatch('(?:분석\\s*Q?\\s*)?(?:통합분석|조회|분석|분석Q)', anchor.get_text(' ', strip=True)):
                    anchor.decompose()
            value = cleaned.get_text(' ', strip=True)
            value = re.sub('\\s*(?:분석\\s*Q\\s*통합분석|분석Q|통합분석)\\s*$', '', value).strip()
            result.setdefault(key, value)
    return result

def choose(mapping, *keys):
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != '':
            return value
    return None

def school_token(name):
    match = re.search('([가-힣A-Za-z0-9]+(?:초등학교|중학교|고등학교|유치원))', name or '')
    return match[1] if match else None

def commodity_classification(explicit=None, title=None):
    patterns = {'육류': '육류|축산물|축산|돼지고기|소고기|닭고기|돈육|우육', '농산물': '농산|농산물', '수산물': '수산|수산물', '공산품': '공산|가공식품', '김치류': '김치류|김치'}
    for text, source in ((explicit, 'explicit_label'), (title, 'title_classification')):
        matches = [kind for kind, pattern in patterns.items() if re.search(pattern, str(text or ''))]
        if matches:
            return (matches[0], source) if len(matches) == 1 else (None, 'mixed_commodity_unconfirmed')
    return (None, 'unconfirmed')

def iso_day(value):
    match = re.search('(\\d{4})[.\\-/](\\d{1,2})[.\\-/](\\d{1,2})', str(value or ''))
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None

def variation_width(value):
    text = str(value or '').replace(' ', '')
    symmetric = re.fullmatch('(?:±|\\+/-)(\\d+(?:\\.\\d+)?)%?', text)
    if symmetric:
        return float(symmetric[1])
    pair = re.fullmatch('-(\\d+(?:\\.\\d+)?)%?(?:~|/|∼)\\+(\\d+(?:\\.\\d+)?)%?', text)
    if pair and pair[1] == pair[2]:
        return float(pair[1])
    return None

def related_school_names(current, candidate):
    """Exact school plus province/city and observed district transition.

    These groups restrict automatic inclusion; they do not independently prove
    administrative continuity. That remains an explicit verification issue.
    """
    if not school_token(current) or school_token(current) != school_token(candidate):
        return False
    if current == candidate:
        return True
    province = '(?:[가-힣]+(?:광역시|특별시|특별자치시|특별자치도)|[가-힣]+도)'
    a, b = (re.search(province, current), re.search(province, candidate))
    if not a or not b or a[0] != b[0]:
        return False

    def district(name):
        match = re.search('(?:광역시|특별시|특별자치시|도)\\s*([가-힣]+?[구군])', name)
        return match[1] if match else None
    da, db = (district(current), district(candidate))
    if da and da == db:
        return True
    return a[0] == '인천광역시' and db in DISTRICT_HINTS.get(da, ())

def parse_notice(html, url, expected_number=None):
    soup = BeautifulSoup(html, 'html.parser')
    fields = labels(soup)
    notice_no = choose(fields, '공고번호', '입찰공고번호')
    if not notice_no or (expected_number and notice_no.strip() != expected_number.strip()):
        raise CollectionError('상세 공고번호가 요청 번호와 정확히 일치하지 않습니다.')
    org = choose(fields, '발주기관', '발주처', '공고기관')
    if org:
        org = re.split('발주처\\s*분석|사정[률율]\\s*분석', org)[0].strip()
    title = choose(fields, '공고명', '입찰공고명')
    if not title:
        published_title = soup.select_one('b.constnm')
        if published_title:
            cleaned_title = BeautifulSoup(str(published_title), 'html.parser')
            for icon in cleaned_title.select('label.bid-icon'):
                icon.decompose()
            title = cleaned_title.get_text(' ', strip=True)
    if not title:
        for header in soup.find_all(['h1', 'h2', 'h3']):
            text = header.get_text(' ', strip=True)
            if re.search('구매|급식|납품|육류|축산|농산|수산|공산|공고', text) and text not in {'입찰공고', '공고상세', '공고정보', '입찰공고 상세'}:
                title = text
                break
    base = client_number(choose(fields, '기초금액', '기초가격'))
    planned = client_number(choose(fields, '예정가격', '예정금액'))
    floor = client_number(choose(fields, '낙찰하한율', '낙찰하한률', '낙찰하한'))
    variation = choose(fields, '예가변동폭', '예가변동', '예가범위')
    deadline = choose(fields, '입찰마감일시', '투찰마감일시', '입찰마감', '투찰마감', '마감일시')
    opening = choose(fields, '개찰일시', '개찰일', '개찰일자')
    correction = choose(fields, '정정여부', '공고구분', '공고상태')
    qualification = choose(fields, '참가자격', '입찰참가자격', '지역제한')
    commodity, commodity_source = commodity_classification(choose(fields, '품목', '업종', '물품분류'), title)
    return {'notice_number': notice_no.strip(), 'notice': notice_no.strip(), 'id': notice_no.strip(), 'name': org, 'title': title or '', 'title_verified': bool(title), 'base': base, 'floor': floor, 'variation': variation_width(variation), 'variation_raw': variation, 'deadline': deadline, 'date': iso_day(opening or deadline), 'opening_raw': opening, 'correction': correction, 'qualification': qualification, 'planned': planned, 'commodity': commodity, 'commodity_source': commodity_source, 'planned_rate': planned / base * 100 if base and planned else None, 'winning': client_number(choose(fields, '낙찰금액', '낙찰가')), 'participants': client_number(choose(fields, '참여업체수', '참가업체수')), 'source_url': url, 'raw_fields': fields, 'bidid': parse_qs(urlparse(url).query).get('bidid', [None])[0]}

def parse_competitors(html):
    """Parse labelled HTML only; do not invent JS pagination/API semantics."""
    soup = BeautifulSoup(html, 'html.parser')
    bidders = []
    for table in soup.find_all('table'):
        header = table.find('tr')
        if not header:
            continue
        columns = [re.sub('\\s+', '', c.get_text(' ', strip=True)) for c in header.find_all(['th', 'td'])]
        if not any((c in columns for c in ('업체명', '상호명', '업체'))):
            continue
        for tr in table.find_all('tr')[1:]:
            cells = tr.find_all('td')
            if len(cells) != len(columns):
                continue
            raw = dict(zip(columns, (c.get_text(' ', strip=True) for c in cells)))
            company = choose(raw, '업체명', '상호명', '업체')
            if not company:
                continue
            bidders.append({'company': company, 'rank': client_number(choose(raw, '순위', '등수')), 'business_registration_number': re.sub('\\D', '', str(choose(raw, '사업자등록번호', '사업자번호', '사업자등록번호(법인등록번호)') or '')) or None, 'amount': client_number(choose(raw, '투찰금액', '입찰금액')), 'status': choose(raw, '비고', '판정', '상태'), 'rate_planned_denominator': client_number(choose(raw, '예정가격대비투찰률', '예정가격대비(%)')), 'rate_base_denominator': client_number(choose(raw, '기초금액대비투찰률', '기초금액대비(%)')), 'reported_rate_unverified': choose(raw, '투찰률', '사정률', '사정율'), 'raw_fields': raw})
    return {'rows': bidders, 'status': 'partial' if bidders else 'missing', 'collected': len(bidders), 'reason': '업체목록 총건수/마지막 페이지 미확인'}

def canonical_row(raw):
    base = client_number(choose(raw, 'basic', 'base_price', '기초금액'))
    planned = client_number(choose(raw, 'yega', 'planned_price', 'planned', '예정가격'))
    notice_id = choose(raw, 'notinum', 'notice', 'notice_number', 'notice_no', 'constno', 'bidno', '공고번호')
    title = choose(raw, 'constnm', 'title', '공고명') or ''
    commodity, commodity_source = commodity_classification(choose(raw, 'commodity', 'itemname', '품목'), title)
    return {'notice_number': notice_id, 'notice': notice_id, 'id': notice_id, 'bidid': choose(raw, 'bidid', 'bid_id'), 'date': iso_day(choose(raw, 'constdt', 'date', '개찰일')), 'name': choose(raw, 'org', 'orgnm', 'org_name', '발주기관'), 'title': title, 'commodity': commodity, 'commodity_source': commodity_source, 'base': base, 'planned': planned, 'planned_rate': planned / base * 100 if base and planned else None, 'winning': client_number(choose(raw, 'success1', 'winning_price', 'winning', '낙찰금액')), 'floor': client_number(choose(raw, 'floor', 'lower_limit_rate', '낙찰하한율', '낙찰하한률')), 'participants': client_number(choose(raw, 'innum', 'participants', '참여업체수')), 'reported_success_pct_unverified': raw.get('success_pct'), 'raw': raw}

def multiple_estimates(raw):
    cells = raw.get('multispares_pct')
    if not isinstance(cells, list) or len(cells) != 15:
        return {'verified': False, 'reason': '15개 복수예가 누락'}
    values, selected = ([], [])
    for index, cell in enumerate(cells, 1):
        deviation = client_number(cell.get('0')) if isinstance(cell, dict) else None
        if deviation is None:
            return {'verified': False, 'reason': '복수예가 편차 수치 누락'}
        values.append(deviation + 100)
        if cell.get('selmsYN') == 'Y':
            selected.append(index)
    if len(selected) != 4:
        return {'verified': False, 'values': values, 'selected': selected, 'reason': '선택된 예가 4개 미확인'}
    base, planned = (client_number(raw.get('basic')), client_number(raw.get('yega')))
    selected_mean = sum((values[index - 1] for index in selected)) / 4
    actual = planned / base * 100 if base and planned else None
    tolerance = max(0.0002, 100 / base) if base else 0
    verified = actual is not None and abs(selected_mean - actual) <= tolerance
    return {'verified': verified, 'values': values, 'selected': selected, 'selected_mean': selected_mean, 'planned_rate': actual, 'reason': None if verified else '선택4개 평균과 예정가격/기초금액 불일치 또는 금액누락'}

class BidQClient:
    def __init__(self, userid, password, session=None, today=None, timeout=20):
        self.session = session or requests.Session()
        self.userid, self.password = userid, password
        self.today = today or datetime.now(timezone(timedelta(hours=9))).date()
        self.timeout = min(max(timeout, 1), 30)
        self._authenticated = False
        self.stage = '연결 준비'
        self._stage_started = monotonic()
        self._stage_requests = 0
        self._detail_response_cache = {}

    def _set_stage(self, stage):
        logger.info('BidQ collection stage %s: %.1fs, %d HTTP requests',
                    self.stage, monotonic() - self._stage_started, self._stage_requests)
        self.stage = stage
        self._stage_started = monotonic()
        self._stage_requests = 0

    def close(self):
        self.password = self.userid = None
        self._authenticated = False
        self._detail_response_cache.clear()
        self.session.close()

    def _request(self, method, path, **kwargs):
        url = urljoin(BASE, path)
        if urlparse(url).scheme != "https" or urlparse(url).hostname not in {"bidq.co.kr", "www.bidq.co.kr"}:
            raise CollectionError("비드큐 이외의 주소는 조회하지 않습니다.")
        # Reuse only authenticated, exact full detail URLs within this client's run.
        # Queries remain untouched so bidid/revision and bid/suc never share an entry.
        cache_key = url if (self._authenticated and method.upper() == 'GET'
            and urlparse(url).path in {'/bidq/bids/detail/bid', '/bidq/bids/detail/suc'}
            and not kwargs.get('params') and not kwargs.get('data') and not kwargs.get('json')) else None
        if cache_key is not None and cache_key in self._detail_response_cache:
            return self._detail_response_cache[cache_key]
        # Redirects are followed manually so credentials and requests never leave BidQ.
        for _ in range(6):
            self._stage_requests += 1
            response = self.session.request(method, url, timeout=self.timeout, allow_redirects=False, **kwargs)
            response.raise_for_status()
            if response.status_code not in {301, 302, 303, 307, 308}:
                if cache_key is not None and url == cache_key and response.status_code == 200:
                    self._detail_response_cache[cache_key] = response
                return response
            url = urljoin(url, response.headers.get("Location", ""))
            redirected = urlparse(url)
            if redirected.hostname not in {"bidq.co.kr", "www.bidq.co.kr"}:
                raise CollectionError("비드큐 외부 주소 이동 차단 (대상: " + (redirected.hostname or '미확인') + ")")
            # BidQ login may return an old http Location; never send a plain-http request.
            if redirected.scheme == 'http':
                url = urlunparse(redirected._replace(scheme='https'))
            elif redirected.scheme != "https":
                raise CollectionError("비드큐 주소 이동의 통신 방식 미지원")
            if response.status_code in {301, 302, 303}:
                method, kwargs = "GET", {}
        raise CollectionError("리디렉션 횟수 초과")

    def login(self):
        self._set_stage('비드큐 로그인')
        response = self._request("GET", "/bidq/member/login/index")
        soup = BeautifulSoup(response.text, "html.parser")
        token = soup.find("meta", attrs={"name": "csrf-token"})
        hidden = soup.find("input", attrs={"name": "_csrf-frontend"})
        csrf = token.get("content") if token else hidden.get("value") if hidden else None
        if not csrf:
            raise CollectionError("비드큐 로그인 보안 토큰 미확인")
        result = self._request("POST", "/bidq/member/login/loginexec", data={"_csrf-frontend": csrf,
                               "refurl": "", "userid": self.userid, "userpass": self.password},
                               headers={"Referer": BASE + "/bidq/member/login/index", "Origin": BASE})
        if "로그아웃" not in result.text and "logout" not in result.text.lower():
            raise CollectionError("비드큐 로그인 실패")
        self._authenticated = True
        self.password = None

    def _detail_links(self, html):
        soup = BeautifulSoup(html, "html.parser")
        return list(dict.fromkeys(urljoin(BASE, a["href"]) for a in soup.select("a[href]")
                    if re.search(r"/bidq/bids/detail/(?:bid|suc)\?", a["href"])))

    def find_notice(self, notice_number):
        self._set_stage('공고 검색·상세 확인')
        found = []
        for kind in ("bid", "suc"):
            search = self._request("GET", "/bidq/bids/list", params={"bidtype": "pur", "bid_suc": kind,
                "searchWord": notice_number, "word_type": "all_Search", "subWord": notice_number})
            for url in self._detail_links(search.text):
                html = self._request("GET", url).text
                fields = labels(BeautifulSoup(html, "html.parser"))
                if choose(fields, "공고번호", "입찰공고번호") == notice_number:
                    found.append(parse_notice(html, url, notice_number))
            if found:
                break
        if len({n["bidid"] for n in found}) != 1:
            raise CollectionError("요청 공고번호의 고유 상세 공고를 확인하지 못했습니다.")
        return found[0]

    def get_result(self, notice_number):
        """Refresh one exact notice; never infer participation or awards by price."""
        if not self._authenticated:
            self.login()
        notice = self.find_notice(notice_number.strip())
        source = notice["source_url"]
        if "/detail/suc" not in source and notice.get("planned") is None:
            return {"notice_number": notice["notice_number"], "planned": None,
                    "winning": None, "date": notice.get("date"), "bidders": [],
                    "source_url": source, "state": "pending", "owned_result": None,
                    "completeness": {"notice_exact": True, "planned_verified": False,
                                     "bidders": "missing", "owned_status_verified": False}}
        html = self._request("GET", source).text
        refreshed = parse_notice(html, source, notice_number.strip())
        parsed = parse_competitors(html)
        owned = [row for row in parsed["rows"] if row.get("business_registration_number") == "3418703699"]
        owned_result = None
        if len(owned) == 1:
            status = owned[0].get("status")
            # Explicit published statuses only. Rank=1 and matching amount are not an award.
            if status and re.search(r"낙찰|미달|부적격|유찰|무효|탈락", status):
                owned_result = {"business_registration_number": "3418703699", "company": owned[0]["company"],
                                "published_status": status, "source_url": source}
        return {"notice_number": refreshed["notice_number"], "planned": refreshed.get("planned"),
                "winning": refreshed.get("winning"), "date": refreshed.get("date"),
                "bidders": parsed["rows"], "source_url": source,
                "state": "opened" if refreshed.get("planned") is not None else "unverified",
                "owned_result": owned_result,
                "completeness": {"notice_exact": True, "planned_verified": refreshed.get("planned") is not None,
                                 "winning_verified": refreshed.get("winning") is not None,
                                 "bidders": parsed["status"], "bidders_reason": parsed["reason"],
                                 "owned_status_verified": owned_result is not None},
                "issues": ["업체목록 전체 페이지 미확인; 온담 미표시를 불참·패찰로 판정하지 않음"]}

    def _school_detail_fields(self, urls):
        """Read independent school-search details with isolated authenticated sessions.

        Keep the original link order for evidence and alias decisions. Test clients
        with injected sessions retain the sequential request path.
        """
        if len(urls) < 2 or not self._authenticated or not isinstance(self.session, requests.Session):
            return [(url, labels(BeautifulSoup(self._request("GET", url).text, "html.parser"))) for url in urls]

        cookies = self.session.cookies.copy()
        headers = self.session.headers.copy()
        settings = {key: getattr(self.session, key) for key in ("trust_env", "verify", "cert", "proxies")}

        def read(url):
            session = requests.Session()
            session.cookies = cookies.copy()
            session.headers.update(headers)
            for key, value in settings.items():
                setattr(session, key, value.copy() if key == "proxies" else value)
            client = BidQClient(None, None, session=session, today=self.today, timeout=self.timeout)
            client._authenticated = True
            try:
                html = client._request("GET", url).text
                return url, labels(BeautifulSoup(html, "html.parser"))
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=min(4, len(urls))) as pool:
            return list(pool.map(read, urls))

    def discover_names(self, name):
        token = school_token(name)
        if not token:
            self._alias_discovery = {"school": None, "names": [name], "status": "missing",
                                     "reason": "현재 발주기관의 학교명 추출 미확인"}
            return [name], False
        response = self._request("GET", "/bidq/bids/list", params={"bidtype": "pur", "bid_suc": "suc",
            "searchWord": token, "word_type": "all_Search", "subWord": token})
        names, evidence, excluded = [name], [], []
        reported_totals, listing_ids = set(), set()
        queue, visited, detail_seen = [(None, response.text)], set(), set()
        unmapped_more = False
        # Follow actual published pagination hrefs, never synthesize page numbers.
        while queue:
            page_url, html = queue.pop(0)
            if page_url:
                if page_url in visited:
                    continue
                if len(visited) >= 100:
                    raise CollectionError("학교명 구매검색 100페이지 한도 초과")
                visited.add(page_url)
                html = self._request("GET", page_url).text
            soup = BeautifulSoup(html, "html.parser")
            for match in re.finditer(r"총\s*([\d,]+)\s*건", soup.get_text(" ", strip=True)):
                reported_totals.add(int(match[1].replace(",", "")))
            for url in self._detail_links(html):
                listing_ids.add(parse_qs(urlparse(url).query).get("bidid", [url])[0])
            for anchor in soup.select('.pagination a[href], a[rel="next"], a[rel="last"]'):
                linked = urljoin(BASE, anchor["href"])
                parsed = urlparse(linked)
                params = parse_qs(parsed.query)
                # A page href must retain both purchasing and exact school filters.
                if parsed.hostname in {"www.bidq.co.kr", "bidq.co.kr"} and parsed.path.startswith("/bidq/bids/list"):
                    if params.get("bidtype") == ["pur"] and params.get("searchWord") == [token]:
                        if linked not in visited:
                            queue.append((linked, None))
                    elif "disabled" not in anchor.get("class", []):
                        unmapped_more = True
            for more in soup.find_all(["a", "button"], string=re.compile("더보기")):
                if not more.has_attr("disabled") and not more.get("href", "").startswith("/bidq/bids/list"):
                    unmapped_more = True
            # Detail pages prove each candidate is an actual purchasing institution.
            # Check the safety limit before dispatch; worker results retain link order.
            detail_urls = []
            for url in self._detail_links(html):
                if url in detail_seen:
                    continue
                detail_seen.add(url)
                if len(detail_seen) > 1000:
                    raise CollectionError("학교명 구매검색 상세 확인 1000건 한도 초과")
                detail_urls.append(url)
            for url, fields in self._school_detail_fields(detail_urls):
                org = choose(fields, "발주기관", "발주처", "공고기관")
                if org:
                    org = re.split(r"발주처\s*분석|사정[률율]\s*분석", org)[0].strip()
                if not org:
                    continue
                record = {"name": org, "source_url": url, "notice_number": choose(fields, "공고번호", "입찰공고번호")}
                if related_school_names(name, org):
                    evidence.append(record)
                    if org not in names:
                        names.append(org)
                else:
                    record["reason"] = "학교명 또는 행정구역 동일성 불일치"
                    excluded.append(record)
        search_complete = (len(reported_totals) == 1 and len(listing_ids) == next(iter(reported_totals))
                           and not unmapped_more)
        district = re.search(r"(?:^|\s)([가-힣]+구)(?:\s|$)", name)
        transition_expected = bool(district and district[1] in {"서해구", "검단구", "제물포구", "영종구"})
        legacy_found = any(record["name"] != name for record in evidence)
        # Source details establish the exact unchanged school, province and observed
        # before/after agency labels. Legal district continuity is not inferred by itself.
        identity_verified = (search_complete and bool(evidence) and
                             (not transition_expected or legacy_found))
        self._alias_discovery = {"school": token, "names": names, "status": "complete" if identity_verified else "partial",
            "search_pages": len(visited) + 1, "detail_count": len(detail_seen),
            "candidate_evidence": evidence, "excluded": excluded,
            "unmapped_pagination": unmapped_more, "identity_verified": identity_verified,
            "search_complete": search_complete, "reported_totals": sorted(reported_totals),
            "listed_unique": len(listing_ids), "legacy_found": legacy_found,
            "identity_method": "exact_school_province_transition_source_details",
            "reason": None if identity_verified else "학교 검색 총건수/최종페이지 또는 변경 전 기관 상세 원문 미확인"}
        if "인천광역시" in name:
            district = re.search(r"(?:^|\s)([가-힣]+구)(?:\s|$)", name)
            self._alias_discovery["administrative_search_hints"] = list(DISTRICT_HINTS.get(district[1], ())) if district else []
        # No legacy alias found is never represented as complete current-only history.
        return names, identity_verified

    def _params(self, names, start, end, graph_type="orgi"):
        params = {"org_codes": [], "org_names": names, "bidtype": "pur", "itemcode": "",
                "amount1": "", "amount2": "", "amount_column": "presum", "danga_option": "",
                "danga_option2": "", "danga_org": "none", "danga_year": "clear",
                "date1": start.isoformat(), "date2": end.isoformat(), "graphType": graph_type,
                "innum1": "", "innum2": "", "localcode": "", "location": "", "point": "100"}
        if graph_type == "freq":
            for key in ("danga_option", "danga_option2", "danga_year", "danga_org"):
                params.pop(key)
        return params

    def _opened(self, names, start, end, graph_type="orgi"):
        params = self._params(names, start, end, graph_type)
        headers = {"Content-Type": "application/json", "Origin": BASE,
                   "Referer": BASE + "/bidq/analysis/" + graph_type}
        graph = self._request("POST", "/bidq/analysis/common-api/org-graph-data", json=params, headers=headers).json()
        reported = number(graph.get("total")) if isinstance(graph, dict) else None
        if reported is None or reported < 0 or reported != int(reported):
            raise CollectionError("그래프 API 총건수 미확인")
        total, rows, pages = int(reported), [], 0
        seen_ids = set()
        for page in range(1, 501):
            payload = {**params, "page": page, "pageSize": 20}
            data = self._request("POST", "/bidq/analysis/common-api/opened-data", json=payload,
                headers=headers).json()
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise CollectionError("발주처 분석 API 응답 구조 미확인")
            batch = data["data"]
            for raw in batch:
                if not isinstance(raw, dict):
                    raise CollectionError("개찰행 응답 구조 미확인")
                identity = choose(raw, "notinum", "notice", "notice_number", "bidid")
                if not identity:
                    raise CollectionError("개찰행 고유번호 누락: 전체 페이지 확인 불가")
                if identity in seen_ids:
                    raise CollectionError("개찰 페이지 중복 공고: 전체 페이지 확인 불가")
                seen_ids.add(identity)
                day = iso_day(choose(raw, "constdt", "date", "개찰일"))
                if day and not start.isoformat() <= day <= end.isoformat():
                    raise CollectionError("개찰행이 요청 조회기간 밖에 있습니다.")
            rows.extend(batch)
            pages = page
            if len(rows) == total:
                return rows, {"status": "complete", "total": total, "collected": len(rows), "pages": pages,
                              "start": start.isoformat(), "end": end.isoformat(), "names": names,
                              "graph_type": graph_type, "graph": graph, "org_count": data.get("org_count"),
                              "unique_rows_verified": len(seen_ids) == total, "last_page_verified": True}
            if not batch or len(rows) > total:
                raise CollectionError("발주처 마지막 페이지/총건수 불일치")
        raise CollectionError("발주처 500페이지 안전 한도 초과: 일부 자료를 완료로 반환하지 않습니다.")

    def _history_parts(self, names, start):
        """Collect independent institution/graph scopes concurrently, in input order.

        A requests.Session is not thread-safe. Each scope gets its own session,
        copied authentication cookies, and the same request settings. Injected
        test sessions keep the original sequential route.
        """
        tasks = [(name, graph_type) for graph_type in ("orgi", "freq") for name in names]

        def collect_scope(client, name, graph_type):
            found, scope = client._opened([name], start, self.today, graph_type)
            if graph_type == "freq":
                params = client._params([name], start, self.today, "freq")
                freq = client._request("POST", "/bidq/analysis/common-api/org-point-freq48", json=params,
                    headers={"Origin": BASE, "Referer": BASE + "/bidq/analysis/freq"}).json()
                if not isinstance(freq, list) or any(not isinstance(item, dict) or "cate" not in item or "24m" not in item for item in freq):
                    raise CollectionError("사정률 빈도48 응답 스키마 미확인")
                counts = [number(item["24m"].get("count")) if isinstance(item["24m"], dict) else None for item in freq]
                if any(count is None or count < 0 or count != int(count) for count in counts) or sum(counts) != scope["total"]:
                    raise CollectionError("사정률 24개월 빈도 합계와 조회 총건수 불일치")
                scope["frequency48"] = freq
            return found, scope

        if len(tasks) < 2 or not self._authenticated or not isinstance(self.session, requests.Session):
            return [collect_scope(self, *task) for task in tasks]

        cookies = self.session.cookies.copy()
        headers = self.session.headers.copy()
        settings = {key: getattr(self.session, key) for key in ("trust_env", "verify", "cert", "proxies")}

        def read(task):
            name, graph_type = task
            session = requests.Session()
            session.cookies = cookies.copy()
            session.headers.update(headers)
            for key, value in settings.items():
                setattr(session, key, value.copy() if key == "proxies" else value)
            client = BidQClient(None, None, session=session, today=self.today, timeout=self.timeout)
            client._authenticated = True
            try:
                return collect_scope(client, name, graph_type)
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
            return list(pool.map(read, tasks))

    def _historical_detail_pages(self, urls):
        """Fetch eligible historical details concurrently without sharing a Session.

        The caller supplies only rows that still need source amounts. Results stay
        in input order, including when an individual response arrives later.
        """
        urls = list(dict.fromkeys(urls))
        if len(urls) < 2 or not self._authenticated or not isinstance(self.session, requests.Session):
            return {url: self._request('GET', url).text for url in urls}

        cookies = self.session.cookies.copy()
        headers = self.session.headers.copy()
        settings = {key: getattr(self.session, key) for key in ('trust_env', 'verify', 'cert', 'proxies')}

        def read(url):
            session = requests.Session()
            session.cookies = cookies.copy()
            session.headers.update(headers)
            for key, value in settings.items():
                setattr(session, key, value.copy() if key == 'proxies' else value)
            client = BidQClient(None, None, session=session, today=self.today, timeout=self.timeout)
            client._authenticated = True
            try:
                return client._request('GET', url).text
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=min(4, len(urls))) as pool:
            return dict(zip(urls, pool.map(read, urls)))

    def _competition_evidence(self, comparable):
        """Read independent comparable notices in parallel, preserving rank order."""
        def collect_one(client, row):
            html = client._request('GET', row['source_url']).text
            competitors = collect_competitor_evidence(client, html, row['source_url'], row.get('participants'))
            planned = row.get('planned')
            threshold = planned * row['floor'] / 100 if planned else None
            valid = sorted(entry['amount'] for entry in competitors['rows']
                           if threshold is not None and entry['amount'] >= threshold)
            competitors['comparison'] = {'planned': planned, 'floor': row['floor'], 'threshold': threshold,
                'below_floor_count': sum(entry['amount'] < threshold for entry in competitors['rows']) if threshold else None,
                'valid_count': len(valid), 'first_floor_gap': valid[0] - threshold if valid else None,
                'first_second_gap': valid[1] - valid[0] if len(valid) > 1 else None,
                'owned_rows': [entry for entry in competitors['rows'] if entry['business_registration_number']=='3418703699']}
            return {'notice_number': row['notice_number'], **competitors}

        if len(comparable) < 2 or not self._authenticated or not isinstance(self.session, requests.Session):
            return [collect_one(self, row) for row in comparable]
        cookies = self.session.cookies.copy()
        headers = self.session.headers.copy()
        settings = {key: getattr(self.session, key) for key in ('trust_env', 'verify', 'cert', 'proxies')}

        def read(row):
            session = requests.Session()
            session.cookies = cookies.copy()
            session.headers.update(headers)
            for key, value in settings.items():
                setattr(session, key, value.copy() if key == 'proxies' else value)
            client = BidQClient(None, None, session=session, today=self.today, timeout=self.timeout)
            client._authenticated = True
            try:
                return collect_one(client, row)
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=min(3, len(comparable))) as pool:
            return list(pool.map(read, comparable))

    def collect(self, notice_number, legacy_names=None, months=24):
        start = subtract_months(self.today, months)
        if not self._authenticated:
            self.login()
        notice = self.find_notice(notice_number.strip())
        if not notice["name"]:
            raise CollectionError("발주기관 미확인")
        self._set_stage('공고 원문·정정 내역 확인')
        notice_html = self._request('GET', notice['source_url']).text
        source_evidence = collect_notice_source_evidence(self, notice_html, notice)
        notice['conditions_evidence'] = source_evidence
        if source_evidence['qualification_source_complete']:
            notice['qualification'] = source_evidence['mirrored_fields']['자격제한']
        if source_evidence['correction_verified']:
            notice['correction'] = '비드큐 게시 정정 내역 확인'
        self._set_stage('학교명·변경 전후 발주처 연결')
        names, alias_verified = self.discover_names(notice["name"])
        for name in legacy_names or []:
            if school_token(name) != school_token(notice["name"]):
                raise CollectionError("이전 기관명 학교명이 현재 공고와 다릅니다.")
            if name not in names:
                names.append(name)
                alias_verified = False
        self._set_stage('24개월 발주처·사정률 분석 수집')
        history_parts = self._history_parts(names, start)
        self._set_stage('과거 개찰행 품목·금액 대조 준비')
        raw_rows, parts, alias_counts = [], [], []
        for name, (found, scope) in zip(names, history_parts[:len(names)]):
            raw_rows.extend(found)
            parts.append(scope)
            alias_counts.append({"name": name, "total": scope.get("total"),
                                 "collected": len(found), "pages": scope.get("pages"), "status": scope.get("status")})
        org = {"status": "complete", "total": sum(part.get("total", 0) for part in parts),
               "collected": len(raw_rows), "by_institution": alias_counts, "parts": parts,
               "start": start.isoformat(), "end": self.today.isoformat(), "names": names}
        frequency_rows, frequency_parts = [], []
        for found, scope in history_parts[len(names):]:
            frequency_parts.append(scope)
            frequency_rows.extend(found)
        issues = []
        rows, seen, competition = [], set(), []
        eligible_raw_rows = []
        excluded_commodity_rows = 0
        by_notice = {}
        canonical_rows = [(raw, canonical_row(raw)) for raw in raw_rows]
        detail_urls = [BASE + '/bidq/bids/detail/suc?bidid=' + quote(str(row['bidid']), safe='') + '&bidtype=pur'
                       for _, row in canonical_rows
                       if row['commodity'] not in {'수산물', '공산품', '김치류'} and row['bidid']
                       and any(row.get(key) is None for key in ('notice', 'base', 'planned', 'floor'))]
        self._set_stage('과거 개찰 상세 금액 확인 (' + str(len(set(detail_urls))) + '건)')
        detail_pages = self._historical_detail_pages(detail_urls)
        self._set_stage('과거 개찰행 금액·품목 검산')
        for raw, row in canonical_rows:
            # Preserve the observed row/count, but do not let an unrelated item
            # trigger detail requests or monetary validation for this analysis.
            if row['commodity'] in {'수산물', '공산품', '김치류'}:
                rows.append(row)
                excluded_commodity_rows += 1
                continue
            # Canonical amounts are re-read from actual detail labels when a BidQ id exists.
            if row["bidid"] and any(row.get(key) is None for key in ("notice", "base", "planned", "floor")):
                url = BASE + "/bidq/bids/detail/suc?bidid=" + quote(str(row["bidid"]), safe="") + "&bidtype=pur"
                html = detail_pages[url]
                detail = parse_notice(html, url, row["notice_number"])
                for key in ("notice_number", "notice", "id", "base", "planned", "planned_rate", "winning", "floor", "name"):
                    if detail[key] is not None:
                        if key in {"base", "planned"} and row.get(key) is not None and row[key] != detail[key]:
                            raise CollectionError("개찰 API와 상세 금액 불일치: " + str(row["notice_number"]))
                        row[key] = detail[key]
                if row["planned"] is None:
                    raise CollectionError("과거 상세 공고의 예정가격 원문 미확인: " + str(row["notice_number"]))
                if detail["commodity"]:
                    row["commodity"], row["commodity_source"] = detail["commodity"], detail["commodity_source"]
                row["source_url"] = url
                row["raw_fields"] = detail["raw_fields"]
                if re.search(r"육류|축산|돼지|소고기|닭고기", row["title"]):
                    competition.append({"notice_number": row["notice_number"],
                                        "source_url": url, **parse_competitors(html)})
            if row['commodity'] in {'수산물', '공산품', '김치류'}:
                rows.append(row)
                excluded_commodity_rows += 1
                continue
            if row['commodity'] in {'육류', '농산물'}:
                eligible_raw_rows.append(raw)
            identity = row["notice_number"] or row["bidid"]
            if not identity:
                issues.append("공고번호/BidQ ID 없는 개찰행: 중복 및 상세 대조 미확인")
                rows.append(row)
                continue
            if identity in seen:
                previous = by_notice[identity]
                if any(previous.get(key) is not None and row.get(key) is not None and previous[key] != row[key]
                       for key in ('base','planned','floor','winning')):
                    raise CollectionError('기관별 중복 공고의 금액·하한율 상충: '+str(identity))
                continue
            seen.add(identity)
            by_notice[identity] = row
            rows.append(row)
        # Collect full bidder lists for the three latest comparable notices.
        comparable = sorted([row for row in rows if row.get('commodity') == notice.get('commodity')
                             and row.get('floor') == notice.get('floor') and row.get('source_url')],
                            key=lambda row: (row.get('date') or '', row['notice_number'] or ''), reverse=True)[:3]
        self._set_stage('최근 동일 품목 경쟁업체 전체 페이지 확인 (' + str(len(comparable)) + '건)')
        competition = self._competition_evidence(comparable)
        self._set_stage('수집 자료 최종 검산')
        competition_complete = bool(comparable) and all(item['complete'] for item in competition)
        required = ("base", "floor", "variation", "deadline", "correction", "qualification")
        issues.extend("현재 공고 " + key + " 원문 미확인" for key in required if notice.get(key) is None)
        if not alias_verified:
            issues.append("동명 학교/행정구역 변경 기관 동일성 추가 확인 필요")
        if any(row["planned_rate"] is None for row in rows if row['commodity'] in {'육류', '농산물'}):
            issues.append("일부 예정가격/기초금액 미확인: success_pct를 예가율로 사용하지 않음")
        missing = {"status": "missing", "reason": "실제 화면/API 매핑 및 마지막 페이지를 확인하지 못함"}
        multi_records = [{"notice": choose(raw, "notinum", "notice", "notice_number"), **multiple_estimates(raw)} for raw in eligible_raw_rows]
        multi_complete = bool(eligible_raw_rows) and all(record["verified"] for record in multi_records)
        org_ids = {choose(raw, "notinum", "notice", "notice_number", "bidid") for raw in raw_rows}
        freq_ids = {choose(raw, "notinum", "notice", "notice_number", "bidid") for raw in frequency_rows}
        if org_ids != freq_ids:
            raise CollectionError("발주처/사정률 분석 공고 집합 불일치")
        freq_scope = {"status": "complete", "parts": frequency_parts, "collected": len(frequency_rows),
                      "total": sum(part.get("total", 0) for part in frequency_parts)}
        coverage = {"org": org, "freq": freq_scope,
                    "multi": {"status": "complete" if multi_complete else "partial", "records": multi_records,
                              "total": len(eligible_raw_rows), "collected": len(multi_records),
                              "source": "opened-data.multispares_pct; type is a frontend store branch only"},
                    "competition": {"status": "complete" if competition_complete else ('partial' if competition else 'missing'),
                                    "notices": competition, 'sample_rule':'최근 동일 품목·하한율 최대 3개 공고의 전체 참여업체',
                                    'expected_notices':len(comparable), 'collected_notices':len(competition),
                                    "reason": None if competition_complete else "비교 공고 또는 업체 총건수·최종페이지 미확인"},
                    'notice_source': source_evidence,
                    "aliases": {**getattr(self, "_alias_discovery", {}), "names": names,
                                "counts": alias_counts, "identity_verified": alias_verified},
                    "captured_at": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"),
                    "excluded_commodity_rows": excluded_commodity_rows}
        coverage.update({"notice_verified": all(notice.get(key) is not None for key in ("notice", "name", "base", "floor", "variation", "deadline")),
            "qualification_verified": False, "qualification_source_complete": source_evidence['qualification_source_complete'],
            'notice_source_complete': source_evidence['notice_source_complete'],
            "correction_verified": source_evidence['correction_verified'],
            "history_24_months_complete": months == 24 and alias_verified and
                all(part.get("status") == "complete" and part.get("unique_rows_verified") for part in parts + frequency_parts),
            "all_pages_complete": alias_verified and all(part.get("last_page_verified") for part in parts + frequency_parts),
            "three_analyses_complete": False, "multiple_estimates_verified": False, "competition_verified": competition_complete})
        coverage["multiple_estimates_verified"] = multi_complete
        coverage["three_analyses_complete"] = multi_complete and all(part.get("status") == "complete" for part in parts + frequency_parts)
        # A qualification/correction label alone cannot establish the full conditions.
        notice["coverage"] = coverage
        if not multi_complete:
            issues.append("복수예가15개/선택4개/금액 검산 미완료")
        if not competition_complete: issues.append("최근 동일 품목 경쟁업체 전체 수집 미완료")
        return {"notice": notice, "school": school_token(notice["name"]),
                "aliases": coverage["aliases"], "rows": rows, "coverage": coverage,
                "issues": list(dict.fromkeys(issues)), "verified": False}

def _numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return str(result) if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None

def _stats(value):
    if not isinstance(value, dict):
        return {}
    return {k: _numeric(value[k]) for k in STAT_KEYS if k in value}

def _public_text(value, key):
    text = str(value)[:100]
    if re.search('(?:\\d[ -]?){9,}|@|AIza|https?://|api.?key|password|token', text, re.I):
        return None
    if key == 'date' and (not re.fullmatch('\\d{4}-\\d{2}-\\d{2}', text)):
        return None
    return text

def build_review_payload(notice, report):
    public = {k: _numeric(notice[k]) for k in ('base', 'floor', 'variation') if k in notice}
    for key in ('commodity', 'date', 'name'):
        if key in notice:
            public[key] = _public_text(notice[key], key)
    candidates = []
    for candidate in report.get('candidates', []):
        method = candidate.get('method')
        if method not in ALLOWED_METHODS:
            continue
        item = {'method': method, **{k: _numeric(candidate.get(k)) for k in ('price', 'rate', 'boundary', 'difference')}}
        validation = candidate.get('validation') or {}
        item['validation'] = {phase: {name: _stats(validation.get(phase, {}).get(name, {})) for name in ('method', 'baseline_same_notices')} for phase in ('training', 'holdout')}
        item['cost_acceptable'] = candidate.get('cost_acceptable') if candidate.get('cost_acceptable') in (True, False, None) else None
        candidates.append(item)
    coverage = report.get('coverage') or {}
    payload = {'notice': public, 'candidates': candidates, 'main_method': (report.get('main_candidate') or {}).get('method'), 'coverage': {k: coverage.get(k) is True for k in COVERAGE_KEYS}, 'coverage_counts': {k: _numeric(coverage.get(k)) for k in ('collected_rows', 'used_rows', 'independent_dates')}, 'statistics': {k: _stats(report.get('statistics', {}).get(k, {})) for k in ('all_same_floor', 'item_same_floor')}, 'verified_superiority': report.get('validation', {}).get('verified_superiority') is True, 'profitability': report.get('profitability') if report.get('profitability') in ('unverified', 'cost_floor_pass', 'cost_floor_fail') else 'unverified'}
    evidence = {}
    for group, values in payload['statistics'].items():
        for key, value in values.items():
            if value is not None:
                evidence[f'statistics.{group}.{key}'] = value
    for candidate in candidates:
        method = candidate['method']
        evidence[f'candidate.{method}.rate'] = candidate['rate']
        for phase, groups in candidate['validation'].items():
            for group, values in groups.items():
                for key, value in values.items():
                    if value is not None:
                        evidence[f'validation.{method}.{phase}.{group}.{key}'] = value
    history = []
    for index, row in enumerate((report.get('school_evidence', {}).get('history') or report.get('history_evidence', []))[-120:]):
        if not isinstance(row, dict):
            continue
        record = {k: _numeric(row[k]) for k in ('rate', 'floor', 'base', 'planned', 'winning') if k in row}
        for key in ('date', 'commodity'):
            if key in row:
                record[key] = _public_text(row[key], key)
        history.append(record)
        evidence[f'history.{index}'] = record
    payload['history'] = history
    competition = report.get('competition_evidence') or {}
    if isinstance(competition, dict):
        for key in ('notice_count', 'participant_rows', 'valid_rows', 'below_floor_rows', 'median_gap', 'own_rows'):
            value = _numeric(competition.get(key))
            if value is not None:
                evidence[f'competition.{key}'] = value
    competition_source = coverage.get('competition') or {}
    if isinstance(competition_source, dict):
        for index, entry in enumerate(competition_source.get('notices', [])[:6]):
            if not isinstance(entry, dict) or entry.get('complete') is not True:
                continue
            comparison = entry.get('comparison') or {}
            numbers = {key: _numeric(comparison.get(key)) for key in ('planned', 'floor', 'threshold', 'below_floor_count', 'valid_count', 'first_floor_gap', 'first_second_gap') if _numeric(comparison.get(key)) is not None}
            numbers['participants'] = _numeric(entry.get('collected'))
            numbers['own_rows_count'] = str(len(comparison.get('owned_rows') or []))
            evidence[f'competition.sample.{index}'] = numbers
    payload['evidence_map'] = evidence
    return payload

def apply_ai_selection(report, review):
    """Apply only a validated reference to a code-computed, affordable candidate."""
    result = copy.deepcopy(report)
    result['ai_review'] = copy.deepcopy(review)
    selected = None
    if review.get('status') in ('suggestion', 'provisional') and review.get('evidence_validated') is True:
        selected = next((c for c in result.get('candidates', []) if c.get('method') == review.get('selected_method') and c.get('method') in ALLOWED_METHODS and (c.get('cost_acceptable') is not False)), None)
    backup = False
    if selected is None and result.get('data_status') == 'verified':
        alternatives = [c for c in result.get('candidates', []) if c.get('method') in ALLOWED_METHODS - {'fixed_100'} and c.get('cost_acceptable') is not False]
        measured = []
        for c in alternatives:
            holdout = ((c.get('validation') or {}).get('holdout') or {}).get('method') or {}
            if holdout.get('n', 0) >= 3 and holdout.get('mae') is not None:
                measured.append((Decimal(str(holdout['mae'])), c['method'], c))
        if measured:
            selected = min(measured, key=lambda item: (item[0], item[1]))[2]
        else:
            selected = next((c for method in ('item_median', 'item_date_mean', 'item_mean', 'all_mean') for c in alternatives if c['method'] == method), None)
        backup = selected is not None
    result['main_candidate'] = selected
    result['alternatives'] = [c for c in result.get('candidates', []) if c is not selected]
    result['recommendation_status'] = 'data_backed_provisional' if backup else 'ai_selected' if selected else 'withheld'
    result['reason'] = 'AI 판단이 완료되지 않아 학교 자료의 후속 검증 오차가 가장 작은 계산 후보를 잠정 표시합니다' if backup else review.get('reason', 'AI 근거 검토 미완료: 추천 보류')
    result['counterargument'] = '검증 표본이 작을 수 있으며 낙찰 우위는 입증되지 않았습니다' if backup else review.get('counterargument', '')
    result['warnings'] = [w for w in result.get('warnings', []) if not w.startswith('통계 우위 미확인: 자동 추천 보류')]
    if selected and result.get('validation', {}).get('verified_superiority') is not True:
        result['warnings'].append('통계 우위는 검증되지 않았습니다. AI 선택은 학교 자료에 근거한 판단이며 낙찰 우위를 입증하지 않습니다')
    if backup:
        result['warnings'].append('Gemini가 후보를 선택하지 못해 검산된 통계 잠정가를 표시했습니다. AI가 고른 금액은 아닙니다')
    if selected and selected.get('cost_acceptable') is True:
        result['profitability'] = 'cost_floor_pass'
    for candidate in result.get('candidates', []):
        candidate['difference'] = candidate['price'] - selected['price'] if selected else None
    return result

def _result(status, model, reason, **extra):
    return {'status': status, 'selected_method': None, 'reason': reason, 'counterargument': '', 'category': 'candidate_review', 'limits': ['AI 선택은 자료에 근거한 판단이며 낙찰을 보장하지 않습니다'], 'source': {'provider': 'Google Gemini', 'model': model}, **extra}

def _http_reason(code):
    return {400: 'Gemini 요청 설정 오류. 선택 모델의 지원 형식을 확인하세요', 401: 'Gemini API 인증 실패. API 키를 확인하세요', 403: 'Gemini API 사용 권한이 없습니다. 키·프로젝트 권한을 확인하세요', 404: '이 API 키에서 선택 모델을 찾을 수 없습니다. 모델 목록을 조회하고 다른 모델을 선택하세요', 429: 'Gemini API 요청량 또는 사용 한도를 초과했습니다. 사용량·한도를 확인하세요', 402: 'Gemini API 결제 확인이 필요합니다. 결제 계정·잔액을 확인하세요'}.get(code, 'Gemini API 호출 실패. 접속 상태와 서비스 상태를 확인하세요')

def list_generation_models(api_key, session=None):
    """Official models.list; key is header-only and remote text never echoed."""
    source = {'provider': 'Google Gemini', 'endpoint': 'v1beta/models'}
    if not isinstance(api_key, str) or not api_key.strip():
        return {'status': 'error', 'models': [], 'reason': 'Gemini API 키가 설정되지 않았습니다', 'source': source}
    try:
        if session is None:
            import requests
            session = requests
        names, tokens, page_token = (set(), set(), None)
        for _ in range(20):
            params = {'pageSize': 1000}
            if page_token:
                params['pageToken'] = page_token
            response = session.get('https://generativelanguage.googleapis.com/v1beta/models', headers={'x-goog-api-key': api_key.strip()}, params=params, timeout=(10, 20))
            if response.status_code != 200:
                code = response.status_code if isinstance(response.status_code, int) else 'unknown'
                return {'status': 'error', 'models': [], 'http_status': code, 'reason': _http_reason(code), 'source': source}
            payload = response.json()
            models = payload.get('models', [])
            if not isinstance(models, list):
                raise ValueError('invalid models response')
            for entry in models:
                if not isinstance(entry, dict):
                    raise ValueError('invalid model entry')
                name = entry.get('name', '')
                if isinstance(name, str) and re.fullmatch('models/gemini-[a-zA-Z0-9.-]{1,90}', name) and ('generateContent' in entry.get('supportedGenerationMethods', [])) and (not re.search('image|audio|tts|live|embed|robotics|computer-use|video|transcribe|nano-banana|omni|customtools', name, re.I)):
                    names.add(name.removeprefix('models/'))
            next_token = payload.get('nextPageToken')
            if not next_token:
                return {'status': 'ok', 'models': sorted(names), 'reason': '이 API 키에서 조회된 텍스트 생성 모델입니다. 실제 호출 한도는 별도 확인합니다', 'source': source}
            if not isinstance(next_token, str) or len(next_token) > 2048 or next_token in tokens or (api_key.strip() in next_token):
                raise ValueError('invalid pagination token')
            tokens.add(next_token)
            page_token = next_token
        raise ValueError('pagination limit')
    except Exception:
        return {'status': 'error', 'models': [], 'reason': 'Gemini 모델 목록 조회 실패. 접속과 API 키를 확인하세요', 'source': source}

def _usage_metadata(envelope):
    metadata = envelope.get('usageMetadata')
    if not isinstance(metadata, dict):
        return {}
    keys = ('promptTokenCount', 'candidatesTokenCount', 'totalTokenCount', 'thoughtsTokenCount', 'cachedContentTokenCount')
    return {key: metadata[key] for key in keys if isinstance(metadata.get(key), int) and (not isinstance(metadata[key], bool)) and (metadata[key] >= 0)}

def review_candidates(api_key, model='gemini-3.8-flash', notice=None, report=None, session=None):
    """Return bounded review; failure messages never contain response/exception text."""
    if not isinstance(model, str) or not re.fullmatch('gemini-[a-zA-Z0-9.-]{1,90}', model):
        return _result('rejected', 'invalid', '모델 이름 형식이 올바르지 않습니다')
    if not isinstance(api_key, str) or not api_key.strip():
        return _result('error', model, 'Gemini API 키가 설정되지 않았습니다')
    payload = build_review_payload(notice or {}, report or {})
    if api_key.strip() in json.dumps(payload, ensure_ascii=False):
        return _result('rejected', model, '공개 자료에 민감정보가 섞여 있어 전송하지 않았습니다')
    methods = {c['method'] for c in payload['candidates']}
    if not methods:
        return _result('rejected', model, '검산된 사전 후보가 없습니다')
    data_complete = all((payload['coverage'][key] for key in PRICE_COVERAGE_KEYS))
    selectable = methods if data_complete and len(methods - {'fixed_100'}) > 0 else methods | {'abstain'}
    schema = {'type': 'OBJECT', 'properties': {'selected_method': {'type': 'STRING', 'enum': sorted(selectable)}, 'reason': {'type': 'STRING'}, 'counterargument': {'type': 'STRING'}, 'category': {'type': 'STRING', 'enum': ['candidate_review']}, 'limits': {'type': 'ARRAY', 'items': {'type': 'STRING'}}, 'evidence_refs': {'type': 'ARRAY', 'items': {'type': 'STRING'}}}, 'required': ['selected_method', 'reason', 'counterargument', 'category', 'limits', 'evidence_refs']}
    body = {'systemInstruction': {'parts': [{'text': PROMPT}]}, 'contents': [{'role': 'user', 'parts': [{'text': json.dumps(payload, ensure_ascii=False)}]}], 'generationConfig': {'temperature': 0, 'maxOutputTokens': 4096, 'responseMimeType': 'application/json', 'responseSchema': schema}}
    try:
        if session is None:
            import requests
            session = requests
        response = session.post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent', headers={'x-goog-api-key': api_key.strip(), 'Content-Type': 'application/json'}, json=body, timeout=(10, 90))
        if response.status_code != 200:
            code = response.status_code if isinstance(response.status_code, int) else 'unknown'
            return _result('error', model, _http_reason(code), http_status=code)
        envelope = response.json()
        candidate = envelope['candidates'][0]
        if candidate.get('finishReason') != 'STOP':
            finish_reason = candidate.get('finishReason')
            safe_reason = finish_reason if finish_reason in ('MAX_TOKENS', 'SAFETY', 'RECITATION', 'OTHER') else 'UNKNOWN'
            return _result('rejected', model, 'Gemini 응답이 정상 완료되지 않았습니다', finish_reason=safe_reason, usage=_usage_metadata(envelope))
        text = ''.join((part.get('text', '') for part in candidate['content']['parts'] if not part.get('thought')))
        result = json.loads(text)
    except Exception:
        return _result('error', model, 'Gemini 연결 또는 응답 해석 실패. 접속과 모델 설정을 확인하세요')
    required = {'selected_method', 'reason', 'counterargument', 'category', 'limits', 'evidence_refs'}
    valid = isinstance(result, dict) and set(result) == required and (result.get('selected_method') in selectable) and (result.get('category') == 'candidate_review') and isinstance(result.get('limits'), list) and (len(result['limits']) <= 6)
    if not valid:
        return _result('rejected', model, 'AI 응답이 사전 후보·구조 제한을 위반했습니다')
    narratives = [result['reason'], result['counterargument'], *result['limits']]
    if any((not isinstance(t, str) or len(t) > 300 for t in narratives)) or not result['reason'] or (not result['counterargument']):
        return _result('rejected', model, 'AI 설명 형식이 올바르지 않습니다')
    joined = ' '.join(narratives)
    if api_key.strip() in joined or re.search('AIza|낙찰\\s*보장(?!\\s*(?:불가|하지|없))|확정\\s*낙찰|반드시\\s*낙찰|guaranteed', joined, re.I) or re.search('(?:승률|낙찰\\s*확률|win probability)\\s*(?:은|이|:)?\\s*\\d', joined, re.I):
        return _result('rejected', model, 'AI 응답에 허용하지 않은 확신·확률·민감정보 표현이 있습니다')
    refs = result.get('evidence_refs')
    if not isinstance(refs, list) or len(refs) > 12 or any((not isinstance(ref, str) or ref not in payload['evidence_map'] for ref in refs)):
        return _result('rejected', model, 'AI가 원자료에 없는 근거를 인용했습니다')
    abstain = result['selected_method'] == 'abstain'
    if not abstain and (len(set(refs)) < 2 or not any((ref.startswith(('statistics.', 'history.')) for ref in refs))):
        return _result('rejected', model, '학교 자료에 연결된 선택 근거가 부족합니다')
    source_numbers = set(re.findall('(?<![A-Za-z])[-+]?\\d+(?:\\.\\d+)?', json.dumps(payload, ensure_ascii=False)))
    for original in tuple(source_numbers):
        try:
            number = Decimal(original)
            for places in range(1, 7):
                rounded = format(number.quantize(Decimal(1).scaleb(-places)), 'f')
                source_numbers.add(rounded)
                source_numbers.add(rounded.rstrip('0').rstrip('.'))
        except InvalidOperation:
            pass
    if any((value not in source_numbers for value in re.findall('[-+]?\\d+(?:\\.\\d+)?', joined))):
        return _result('rejected', model, 'AI 설명의 숫자가 제공 자료와 일치하지 않습니다')
    if not abstain:
        selected = next((c for c in payload['candidates'] if c['method'] == result['selected_method']))
        data_complete = all((payload['coverage'][key] for key in PRICE_COVERAGE_KEYS))
        if not data_complete or selected['cost_acceptable'] is False:
            return _result('rejected', model, '자료 검증 또는 최저수용가 조건이 충족되지 않아 추천을 보류합니다')
    incomplete = not all(payload['coverage'].values())
    if incomplete and re.search('최종\\s*(?:추천|확정|후보)|검증\\s*완료', joined):
        return _result('rejected', model, '필수 확인 미완료 상태에서 확정 표현을 사용했습니다')
    version = envelope.get('modelVersion', model)
    if not isinstance(version, str) or not re.fullmatch('gemini-[a-zA-Z0-9.-]{1,90}', version):
        version = model
    result.update({'status': 'abstained' if abstain else 'provisional' if incomplete else 'suggestion', 'evidence_validated': True, 'source': {'provider': 'Google Gemini', 'model': model, 'model_version': version}, 'usage': _usage_metadata(envelope)})
    result['limits'].append('AI는 계산된 후보만 선택하며 낙찰을 보장하지 않습니다')
    if incomplete:
        result['limits'].append('필수 출처 확인 미완료: 잠정 검토')
    return result
APPROVED_FIXTURES = [{'school': '인천도담초등학교', 'notice': 'E261008-674394-0', 'base': 20477210, 'floor': 88, 'rate': 99.6, 'price': 17947866}, {'school': '인천뷰티예술고등학교', 'notice': 'E261006-674238-0', 'base': 15211000, 'floor': 90, 'rate': 99.65, 'price': 13641986}, {'school': '인천성지초등학교', 'notice': 'E261007-674324-0', 'base': 7770105, 'floor': 90, 'rate': 100.0, 'price': 6993095}, {'school': '인천송림초등학교', 'notice': 'E261007-674292-0', 'base': 27869809, 'floor': 88, 'rate': 99.8, 'price': 24476382}, {'school': '인천능허대초등학교', 'notice': 'E261006-674227-0', 'base': 29661280, 'floor': 88, 'rate': 99.65, 'price': 26010570}, {'school': '인천상정고등학교', 'notice': 'E261007-674284-0', 'base': 25778000, 'floor': 88, 'rate': 100.2, 'price': 22730010}, {'school': '인천하정초등학교', 'notice': 'E261007-674320-0', 'base': 15255800, 'floor': 90, 'rate': 100.05, 'price': 13737086}, {'school': '마전중학교', 'notice': 'E261006-674182-0', 'base': 52532200, 'floor': 88, 'rate': 99.95, 'price': 46205222}]
APPLIED_GUIDE = "# 비드큐 입찰 분석 지침\n\n## 2026-10-14 최신 승인 — 분석 깊이는 유지하고 입찰가부터 빠르게 보고\n\n사용자 요청: “앞으로는 입찰가만 먼저 빠르게 주고 토큰도 아껴줘. 이 방식도 지침서에 넣어줘. 그렇다고 분석을 대충해선 안돼 할꺼는 다하되 시간을 좀 줄이자는 거야.”\n\n- **가격 먼저:** 학교별 필수 확인과 후보 비교·검산이 끝나는 즉시 `학교명 / 추천 입찰가 / 마감시간` 중심의 짧은 표를 제공한다. 다른 학교의 완료나 긴 설명문·통합보고서 작성을 기다리지 않는다. 가격 선택에 직접 영향을 주는 미확인 조건·변경·하한 미달 경계는 필요한 한두 문장만 덧붙인다. 상세 근거·대안·검증은 원자료와 기록에 보존하고 사용자가 요청하면 설명한다. 앞선 지침의 긴 답변 순서보다 이 표시 방식을 우선한다.\n- **필수 분석 유지:** 현재 공고와 정정·자격·기초금액·하한율·예가폭, 변경 전후 학교 자료, 세 분석의 24개월과 마지막 더보기, 동일 품목·하한율·경쟁·온담 비교, 후보 비교와 시간순 검증, 산술·미달 경계·원가 미확인 구분을 생략하지 않는다. 가격을 먼저 제공한다는 것은 분석 완료 후 설명을 짧게 한다는 뜻이다. 필수 확인 전의 가격을 검증 완료 주후보로 제시하지 않는다.\n- **기존 자료 재사용:** 저장된 원자료·검증된 계산 도구·모형 평가를 학교명과 공고번호로 찾아 재사용한다. 현재 공고 조건은 매번 확인하며 자료의 기간·필터·최종 페이지·변경 여부를 검증한다. 유효한 재사용 자료와 새 개찰·정정 자료를 합쳐 24개월 범위를 충족하면 동일 자료를 다시 전부 읽거나 재계산하지 않는다. 출처·시점·범위가 불명확하거나 변경됐으면 필요한 범위만 보완한다.\n- **병렬 작업 효율:** 독립 학교의 수집·계산·검산은 병렬 수행하되 동일 학교·탭·파일의 중복 담당을 만들지 않는다. 에이전트에는 필요한 공고·자료·기준만 전달하고 내부 인계는 가격·변경점·확인 건수·파일 경로 중심으로 짧게 한다. 긴 대화 전체 전달·중복 요약·불필요한 상태 조회와 캡처를 줄인다. 병렬 작업 자체가 토큰 절감을 보장한다고 말하지 않는다.\n- **검증과 기록:** 원자료와 계산 결과를 구조화해 저장하고 필요한 읽기 확인을 수행한다. 의미 있는 검산을 통과한 내용은 새 변경·오류 없이 반복하지 않는다. 이미 저장된 기록을 활용하며 요청하지 않은 장문의 설명·보고서를 매번 새로 만들지 않는다. 연결이 중단되면 저장된 완료 지점부터 이어간다.\n- **예외 표시:** 자료 부족·참가자격 미확인·정정·후보 수정이 있으면 가격과 함께 짧고 정확하게 알린다. 임시 계산을 불가피하게 제시할 때는 `잠정 후보·필수 확인 미완료`라고 표시하며 최종 검증을 대체하지 않는다. 실제 낙찰 보장이나 확인하지 않은 수익성 주장은 하지 않는다. eaT 방문 금지와 사용자 입력·포커스 비점유 원칙은 유지한다.\n\n## 2026-10-14 사용자 승인 — 다음 분석부터 후보 비교·검증으로 가격 선정\n\n사용자는 현재 방식이 최적인지와 추가 추론 가능성을 물은 뒤 “그럼 다음번 분석부터는 어떻게 변하는거야? 지침서에 넣어주고 나한테 브리핑도 해줘”라고 요청했다. 다음 분석부터 아래 기준을 적용한다. 자료 수집·24개월·최종 더보기·복수예가·배경 작업·병렬 처리 기준은 유지하며, 가격 선정과 검증에 대해서는 이 항목을 기존의 임의 절충·중앙값 선정 설명보다 우선한다.\n\n### 매번 적용할 실행 순서\n\n1. **목표와 제약 구분:** 공고 조건·자격·하한율을 먼저 확인한다. 하한 통과, 유효가격 경쟁, 이익 확보를 별도 목표로 표시한다. 사용자 원가·최저 수용가가 있으면 후보의 하한 제약으로 반영한다. 없으면 가격 경쟁 분석은 계속하되 이익을 포함한 최적화 또는 기대이익 최대라고 주장하지 않는다.\n2. **같은 조건의 자료 정리:** 학교명으로 변경 전후 기관 자료를 연결하고 정정·재입찰 중복을 확인한다. 24개월 전체, 동일 품목, 동일 하한율·계약방식, 최근 기간/건수를 나누고 유효 표본 수를 적는다. 과거 하한율의 원문 확인과 업체사정률 역산 추정을 구분한다.\n3. **후보를 먼저 정의:** 100% 기준, 기관 전체 평균, 동일 품목 평균·중앙값 등 자료가 허용하는 소수의 단순 방법을 비교 대상으로 먼저 정한다. 최근 자료 가중치나 기관·품목 절충을 추가하면 비중과 이유를 기록한다. 과거 성적을 보고 후보·기간·비중을 계속 바꾸어 가장 좋은 숫자를 찾지 않는다. 임의 50:50 비중, 0.02%p 가산, 0.05%p 격자 또는 정밀한 끝자리를 검증된 법칙으로 취급하지 않는다.\n4. **시간순으로 방법 비교:** 각 과거 개찰 시점보다 앞선 자료만으로 후보를 계산한다. 같은 개찰일의 다른 품목과 이후 결과는 입력에서 제외한다. 방법/가중치 선택에 쓴 기간과 별도 평가 기간을 구분하고, 적은 표본에서는 선택 편향과 불확실성을 밝힌다. 100% 등 단순 기준과 같은 평가 공고에서 비교하며, 예가 추정 오차뿐 아니라 하한 미달·유효하지만 기록상 1순위 초과·유효하면서 1순위 이하를 함께 집계한다. 실제 낙찰과 가상 비교를 혼동하지 않는다.\n5. **경쟁 구간 분석:** 최근 및 비교조건에 맞는 대표 공고의 업체 목록을 끝까지 확인해 유효/미달, 하한 주변 밀집, 1순위-하한 및 1·2순위 차이, 반복 참여업체와 온담 결과를 비교한다. 확인한 공고·업체 행 수와 미확인 공고를 표시한다. 이번 참가자·가격을 확정하거나 과거 행동이 반복된다고 단정하지 않는다.\n6. **최종 가격 후보표 작성:** 후보마다 사정률·원화 금액·주후보 대비 차액·실제 예가율의 미달 경계·평가 표본과 기간·과거 가격경쟁 결과·원가 조건을 같은 표에서 비교한다. 낮은/중심/높은 예가 대응 가격을 제시하되 각각 미달 위험과 높은 가격으로 밀리는 관계를 설명한다. 검증된 확률모형 없이 관측 빈도를 이번 낙찰확률로 표시하지 않는다.\n7. **주후보 선택과 반대 근거:** 별도 평가에서 상대적으로 나은 방법이 있으면 그 근거와 표본 한계를 제시한다. 우위가 불분명하면 검증된 최적가라는 표현을 피하고 단순 기준·중심 후보와 대안을 설명한다. 더 복잡한 방법이 단순 기준보다 못하면 채택하지 않는다. 하한 통과와 가격 경쟁 중 어떤 선택을 더 반영했는지 밝히고 근거 부족을 임의 안전마진으로 숨기지 않는다.\n8. **산술 검산·보고·복기:** 현재 공고 산식으로 원 단위 계산과 미달 경계를 역산한다. 학교별 검증 완료 즉시 주후보·대안·선택 이유·반대 근거·확인 범위를 보고한다. 사전 후보·모형·비중·자료 마감 시점과 원자료를 보존하고, 이후 확보한 실제 결과를 별도로 추가해 개선한다. 사후 결과로 사전 추천 이유를 바꾸지 않으며 별도 요청 없이 감시 자동화를 만들지 않는다.\n\n### 자료가 충분할 때만 추가할 시험 방법\n\n- **비슷한 학교의 공통 자료 활용:** 예가 생성 방식·변동폭·품목·하한율·금액대 등 비교조건을 설명하고, 학교별 자료가 적을 때 공통 자료와 절충하는 방법을 시간순 검증한다. 학교 수·개찰 수·독립 개찰일 수·가중치와 제외 기준을 공개한다. 여러 품목·기관을 단순히 많이 섞었다는 이유로 예측력이 좋아졌다고 하지 않는다.\n- **분포와 경쟁을 함께 비교:** 표본이 허용하면 한 예가율 대신 예가 분포와 경쟁가격 시나리오를 만들어 후보별 미달/경쟁 결과의 민감도를 비교한다. 분포·경쟁 가정의 근거와 별도 평가 결과를 설명한다. 모형 가정으로 계산한 확률·기대이익은 관측값과 구분하며 불안정하면 정량 승률을 제공하지 않는다.\n- **가중치·계절·연속 패턴·추첨번호:** 시험 가설로만 다룬다. 이후 자료에서 단순 기준보다 안정적으로 개선되는지 평가하고 효과가 약하면 제외한다. 여러 규칙 중 우연히 좋은 것을 고르는 위험을 밝힌다. 추첨번호 빈도와 15개 전체 평균은 선택된 4개의 다음 결과를 확정하는 근거가 아니다.\n\n### 보고의 정확성\n\n- 과거 후보가격을 기록상 1순위와 비교할 때는 다른 업체 가격과 예정가격을 고정한 가상 비교임을 명시한다. 번호 선택·동가·자격 심사 등 실제 절차를 재현한 낙찰 결과로 표현하지 않는다.\n- 미달 위험을 줄이는 높은 후보와 유효가격 경쟁에서 유리할 수 있는 낮은 후보의 관계를 설명한다. 원가·배송·손질·폐기·세금이 미확인이면 수익성은 미검증이다.\n- 기존 8학교 분석의 과거 고정후보 비교 63건(미달30/기록상 1순위 초과29/유효하면서 1순위 이하4)은 사후 비교다. 실제 낙찰률이나 현재 방법의 최적화 증거로 사용하지 않는다. 해당 사전 가격과 설명은 변경하지 않는다.\n- 이 지침 저장은 다음 분석의 실행 기준 변경이다. 새 방법을 이미 구현·검증했거나 낙찰 성능이 개선됐다는 뜻은 아니다. 분석마다 실제 수행한 방법과 아직 시험하지 않은 범위를 구분한다.\n\n## 2026-10-14 병렬 처리와 완료 즉시 보고\n\n- 여러 학교 요청은 독립적인 조사·통계·검산을 병렬로 분담한다. 첫 학교 자료가 확보되는 즉시 계산과 보고서 준비를 시작하며 전체 수집 완료를 기다리지 않는다.\n- 같은 브라우저 탭과 같은 파일은 한 담당만 갱신한다. 백그라운드 별도 탭 제어가 지원되지 않으면 브라우저 수집은 순차로 처리하고 학교별 계산·검산·보고서 준비를 병렬로 진행한다.\n- 공고 조건·24개월 자료·마지막 더보기·복수예가 확인과 검산이 끝난 학교부터 후보 금액과 근거를 알린다. 확인되지 않은 자료를 완료로 표시하거나 속도를 위해 필수 확인을 생략하지 않는다.\n- 기존 자료와 검증된 계산 도구를 재사용하고 중복 조사·반복 검산을 줄인다. 연결이 끊기면 저장된 확인 지점부터 재개한다. 로그인된 탭이 없으면 장애를 알리고 독립적인 공개 자료 조회와 파일 준비는 계속한다.\n\n## 2026-10-13 사용자 승인 — 앞으로 유지할 분석·보고 기준\n\n사용자는 가석초·청라 분석을 확인한 뒤 “앞으로도 지금 처럼 분석 부탁해 지침서에 잘 정리해줘”라고 요청했다. 이번 자료 수집 깊이, 병렬 검산, 근거와 한계를 함께 설명하는 방식을 향후 입찰 분석의 기본 기준으로 유지한다. 이는 분석 방식에 대한 승인이며 특정 후보의 낙찰 성과가 확인됐다는 뜻은 아니다.\n\n1. **현재 공고부터 확인:** 공고번호·학교명·정정 여부·기초금액·낙찰하한율·예가변동폭·복수예가 방식·마감 및 개찰시각을 다시 확인한다. 예정가격 대비 투찰률, 기초대비 투찰률, 발주처 예가율과 업체 입찰사정률의 분모를 구분한다.\n2. **학교명으로 과거 이력 연결:** 행정구역이 달라도 학교명과 기관 동일성을 확인하여 변경 전후 자료를 연결한다. 공고번호 중복을 제거하고 기관명별·통합 건수를 기록한다.\n3. **세 분석 모두 끝까지 조회:** 발주처 분석·사정률 분석·복수예가 분석을 각각 24개월로 적용하고 흐름·빈도 그래프와 하단 `더보기` 마지막 페이지를 확인한다. 사이트 총건수, 수집 행 수, 실제 조회 날짜와 미확인 범위를 남긴다.\n4. **전체·동일 품목·최근 자료 비교:** 표본 수, 평균·중앙값·산포와 최근 흐름을 비교한다. 동일 하한율·계약방식의 비교를 우선하고, 최근 몇 건과 최근 몇 개월을 혼동하지 않는다. 같은 날 여러 품목의 자료를 독립적인 시계열 관측으로 과장하지 않는다.\n5. **실제 경쟁가격과 온담 결과 대조:** 최근 공고와 금액·납품기간이 비슷한 공고의 참여업체 목록을 필요한 범위에서 마지막 페이지까지 확인한다. 하한 미달 수, 하한가와 1순위 차이, 1·2순위 차이, 온담 투찰 결과를 구분하고 확인 공고 수·업체 행 수를 밝힌다.\n6. **후보와 반대 근거를 함께 제시:** 학교별 주후보 한 금액과 가정 사정률을 먼저 제시하고 선택 이유, 불리한 근거, 산식·원 단위 처리, 대안 금액과 차액, 실제 예가율의 하한 미달 경계를 설명한다. 원가 미확인 시 수익성이 검증된 가격으로 표현하지 않는다.\n7. **과거 검산과 시간순 검증:** 후보 사정률을 각 과거 공고의 기초금액·하한율에 맞춰 적용해 하한 미달 / 유효하지만 기록상 1순위보다 높은 가격 / 기록상 1순위보다 낮은 유효가격을 나눈다. 시간순 검증에서는 같은 날 다른 품목과 미래 자료를 입력에서 제외한다. 예가 추정 오차가 작다는 것과 낙찰 경쟁력이 높다는 것은 별도로 평가한다.\n8. **추첨 불확실성을 정직하게 설명:** 15개 복수예가 평균과 선택된 4개로 정해진 예정가격을 구분한다. 최빈 구간·과거 추첨번호·끝자리 정밀도만으로 다음 결과나 승률을 단정하지 않는다. 불리한 검증 결과도 숨기지 않으며 과거 실패액을 다음 공고에 고정 가산하지 않는다.\n9. **백그라운드 수집·병렬 검산·기록 보존:** 지정된 영주 Chrome 세션과 사용자 입력·포커스 비점유 원칙을 지킨다. 독립적인 학교별 계산·검산은 병렬로 진행하되 같은 탭·파일은 단일 담당이 처리한다. 출처 링크·원자료·검산·보고서·사용자 요청과 첨부를 연결해 저장하고 읽어 확인한다. 실제 개찰 결과를 후속으로 확보하면 사전 후보와 이유를 보존한 채 결과를 추가하여 복기한다.\n\n승인된 설명 수준의 예시는 [가석·청라 상세 분석보고서](입찰분석_대화기록/2026-10-13_가석청라_분석보고서.txt), [가석 독립검산](입찰분석_대화기록/2026-10-13_가석_독립검산.json), [청라 통계·검산](입찰분석_대화기록/2026-10-13_청라_통계.json)을 참고한다. 가석 10,494,311원·99.65%, 청라 3,783,150원·100.00%는 당시 공고의 개찰 전 후보이며 다음 입찰의 고정 추천값으로 재사용하지 않는다. 이번 승인 메시지만으로 실제 제출금액이나 낙찰 여부를 확정하지 않는다.\n\n## 사용자 입력 장치·포커스 조작 금지 — 2026-10-10 최우선\n\n- 모든 작업은 백그라운드로만 수행한다. 실제 마우스 이동·클릭·드래그·스크롤, OS 키보드 입력·단축키, 사용자 클립보드 읽기·변경, 창·탭 활성화와 포커스 이동을 금지한다. 이 방을 포함한 모든 카테고리와 위임 작업에 적용하며 기존의 백그라운드 우선·앱 열기 지시보다 우선한다.\n- 백그라운드로 불가능한 단계는 멈추고 장애만 짧게 알린다. 전면 조작으로 자동 전환하지 않으며 독립적인 파일 준비·렌더링·검증은 계속한다. 상세 기준은 [AI 공통 작업 수행 지침](AI_공통_작업수행_지침.md)의 같은 제목 항목을 따른다.\n\n\n## 영상 제작을 요청받았을 때의 공통 음량 기준 2026년 10월 8일\n\n이 분야의 조사·분석에는 음량 처리가 필요하지 않다. 실제 영상 제작·편집을 요청받으면 더빙·현장음·효과음·배경음을 합친 최종 전체 믹스를 약 -14 LUFS (Integrated)로 맞추고, 제작 보조 기준으로 True Peak -1 dBTP 이하를 관리한다. 내보낸 최종 파일을 실제 측정한 뒤 수치를 기록한다. 상세 기준은 [AI 공통 작업 수행 지침](AI_공통_작업수행_지침.md)의 '영상 편집 공통 음량 기준 2026년 10월 8일'을 따른다. 원본과 조사용 참고 영상 및 과거 관측값은 보존한다.\n\n## 공통 작업 수행 지침 2026년 10월 8일\n\n작업 시작 시 [AI 공통 작업 수행 지침](AI_공통_작업수행_지침.md)의 9개 원칙과 「모든 작업의 백그라운드 우선·비용 절약」 항목을 읽고 이 지침과 함께 적용한다. 목표와 완료 기준 확인, 실제 수행, 불필요한 재질문 방지, 독립 작업 병렬 수행, 오류 원인 확인과 대안 적용, 가능한 작업 계속 진행, 실제 결과 검증, 검증 후 마무리, 정확한 진행·완료 보고를 공통 기준으로 삼는다.\n\n사용자의 최신 요청과 이 분야의 기존 세부 조건을 유지하며, 다른 분야의 편집·조사 규칙을 섞지 않는다. 공통 원칙 전문은 연결된 문서에서 관리한다.\n\n2026년 10월 8일 사용자가 요청한 입찰 분석 방식과 이 방의 대화를 반영한다. 다음 분석은 공고 조건과 과거 낙찰 내역을 실제로 확인하고, 이번보다 더 상세하게 경쟁 구조와 추천금액의 근거를 설명한다. 사용자의 최신 요청을 우선한다.\n\n전체 대화 원문과 첨부 이미지 5장은 [입찰가 분석하기 전체 대화 기록](입찰분석_대화기록/2026-10-08_입찰가분석하기_전체대화.md)에 보존한다. 아래 실행 기준은 대화에서 정리한 지침이며, 대화 원문과 당시 분석 결과를 대체하지 않는다.\n\n## 최고 수준의 전문성·집중·검증 기준 — 2026-10-12 사용자 요청\n\n사용자 원문: “너는 입찰 전문가 중에서도 특급 전문가야 전세계에서 너가 제일 분석잘하고 날카롭게 분석을 해야돼 지침서에 잘 넣고 항상 집중해보자”\n\n이 요청은 앞으로 모든 입찰 분석에서 최고 수준의 정확성과 날카로운 판단을 추구하라는 상시 품질 기준으로 적용한다. 전문성은 자기평가나 확신의 강도가 아니라 아래의 실제 조사와 검증으로 보여준다.\n\n- 분석마다 대상 공고·정정 여부·마감 시각을 먼저 확인하고, 공고 원문과 첨부 조건을 기준으로 판단한다. 과거 공고의 수치나 추천금액을 이번 공고에 그대로 대입하지 않는다.\n- 24개월 흐름·빈도 그래프와 하단 `더보기` 마지막 페이지를 확인하고, 표시 총건수와 수집 행 수를 대조한다. 미확인 자료와 접근 제한은 정확히 구분한다.\n- 전체 기관 통계, 동일 품목·하한율·계약방식의 비교 사례, 경쟁업체 분포, 온담의 과거 결과를 구분해 분석한다. 평균·최빈 구간 하나로 결론을 내리지 않는다.\n- 추천 후보마다 선택 근거와 그 후보가 불리해지는 반대 근거를 함께 검토한다. 비교 기간·표본·가중치를 바꿨을 때 결론이 얼마나 달라지는지 확인하고, 대안 금액과 선택 조건을 제시한다.\n- 과거 적용 검증은 당시 이용 가능했던 자료만으로 시간순으로 수행한다. 전체 결과를 본 뒤 고른 후보의 성적은 사후 비교로 표시한다. 하한 통과, 가상 1순위, 실제 낙찰을 구분하고 적은 표본의 성적을 이번 승률로 단정하지 않는다.\n- 추천금액·입찰사정률·하한 미달 경계를 서로 역산해 검산하고, 원 단위 처리와 해당 공고의 별도 산식을 확인한다. 정밀한 끝자리 자체를 예측력의 근거로 삼지 않는다.\n- 가격 경쟁력과 수익성을 함께 검토한다. 구매 규격·수량·원가·배송·반품·계약 특수조건을 반영하고, 원가가 없으면 이익 확보 여부는 미확인으로 표시한다.\n- 관측 사실·계산 결과·예측과 판단을 분리해 설명한다. 근거가 약하거나 자료가 충돌하면 이를 숨기지 않고 추천의 확실성을 낮춘다. 새로운 증거가 기존 판단과 다르면 즉시 수정한다. 근거 없는 세계 최고 주장이나 낙찰 보장은 하지 않는다.\n- 후속 분석에서 실제 개찰 결과를 확보하면 이전 추천의 하한 미달 여부·1순위와의 차이·실패 원인을 점검해 다음 판단에 반영한다. 결과를 보고 사전 추천 이유를 바꾸지 않으며, 별도 요청 없이 주기적 감시를 예약하지 않는다.\n\n아래의 수집·계산·답변 기준을 매번 함께 적용하며, 마지막으로 추천의 근거·반대 근거·검산·미확인 범위가 빠지지 않았는지 점검한 뒤 보고한다.\n\n## 사용자가 지정한 확인 순서\n\n1. [비드큐](https://www.bidq.co.kr)에서 공고번호를 검색하여 대상 공고를 확인한다.\n2. 공고 상세에서 예가변동폭, 낙찰하한율, 기초금액을 먼저 확인한다. 공고명·품목·지역·계약방법·복수예가 방식·투찰마감·개찰시각·납품기간·참가자격·정정 여부도 확인한다. 원문과 첨부문서에서 확인한 내용과 비드큐 표시만 확인한 내용을 구분한다.\n3. 해당 발주처의 분석 페이지에서 기간을 24개월로 설정하고 실제 시작일·종료일 및 적용 필터를 확인한다.\n4. 위쪽 예가/기초 흐름 그래프와 아래쪽 구간별 빈도 막대그래프를 모두 확인한다. 색상이나 막대 하나만 보고 추천 사정률을 정하지 않는다.\n5. 하단 개찰공고 표의 `더보기`를 마지막 페이지까지 펼친다. 예를 들어 1/2라면 더보기를 눌러 2/2까지 확인한다. 이미 마지막까지 펼쳐져 있으면 다시 누를 필요가 없다.\n6. 사이트 표시 총건수와 실제 읽은 개찰 행 수를 대조한다. 마지막 페이지 표시 또는 더보기 비활성 상태까지 확인한다. 일부 행만 확인하고 전체 확인으로 보고하지 않는다.\n7. 동일 품목의 과거 개찰 상세로 들어가 참여업체 목록의 `전체보기`, 추가 페이지, 낙찰하한 미달 업체를 필요한 범위에서 확인한다. 확인한 공고 수·참여업체 행 수와 미확인 범위를 명시한다.\n\n사용자는 지정한 화면 외에도 도움이 될 자료가 있으면 추가로 조사해 달라고 요청했다. 공고에 연결된 구매내역서·자격기준·계약 특수조건을 확인하고, 업체별 전체 가격·온담의 참여 기록·후보의 과거 적용 결과를 가능한 범위에서 보완한다. 외부 연락이나 실제 투찰은 이 조사 요청에 포함되지 않는다.\n\n업체 목록의 다음 페이지를 누른 직후에는 이전 행이 잠시 남을 수 있다. 페이지 번호뿐 아니라 첫 행·마지막 행과 실제 건수, 등록번호 중복 여부를 확인한 뒤 합친다. 같은 이름의 서로 다른 업체는 등록번호로 구분한다.\n\n접속은 사용자가 이미 이용 중인 비드큐 로그인 세션을 우선한다. 이 대화에서는 영주 Chrome의 비드큐 세션을 사용했다. 다른 계정으로 임의 전환하지 않는다. 조회와 분석 요청을 실제 투찰·계약 제출의 승인으로 해석하지 않는다.\n\n## 자료 수집과 비교 기준\n\n개찰표에서는 공고번호·개찰일·품목·기초금액·예정가격·실제 예가/기초·1순위 투찰금액·1순위 입찰사정률·참여업체 수를 읽는다. 화면 표시 소수점과 실제 금액으로 재계산한 값의 차이는 반올림·절사 가능성을 표시한다.\n\n전체 기관 자료, 동일 품목 자료, 동일 하한율과 계약방식 자료를 구분한다. 동일 공고의 정정·재입찰·유찰을 확인하여 임의로 독립 표본으로 중복 계산하지 않는다. 예가 생성 방식이나 변동폭이 다른 자료는 비교 범위를 설명한다. 원자료를 임의로 제외하지 않으며 제외한 경우 기준과 건수를 적는다.\n\n육류와 공산품은 참여업체 수와 하한율 등이 다를 수 있다. 전체 기관의 실제 예가 분포는 참고하되, 서로 다른 품목의 1순위 입찰사정률을 한꺼번에 평균 내어 육류의 추천값으로 사용하지 않는다. 같은 품목이라는 이유만으로 작은 표본의 평균을 다음 예가로 확신하지 않는다.\n\n## 다음 분석에서 더 상세히 제공할 내용\n\n### 예정가격 분포\n\n- 24개월 전체와 동일 품목의 표본 수, 평균, 중앙값, 표준편차, 최저·최고, 필요하면 사분위수를 제시한다.\n- 최근 기간 또는 최근 건수별 통계와 전체 통계를 비교한다. 6개월·12개월·24개월 또는 최근 4·8·12건 가운데 자료량에 맞는 비교를 선택하고 기간과 건수를 함께 적는다.\n- 구간별 빈도는 구간 폭·건수·전체 대비 비율을 밝히고 공동 최빈 구간도 빠짐없이 표시한다. 구간을 바꾸면 결론이 달라지는지도 필요에 따라 확인한다.\n- 품목별 차이와 최근 흐름이 엇갈리면 둘 다 설명한다. 표본 수가 적거나 동일 날짜에 여러 품목이 몰려 있으면 그 한계를 반영한다.\n- 파동, 오르내림, 특정 계절이나 색상만으로 다음 추첨 결과를 단정하지 않는다. 관측된 빈도를 이번 낙찰 확률로 바꾸어 말하지 않는다.\n\n### 실제 낙찰 경쟁\n\n- 동일 품목의 최근 사례와 금액·계약 조건이 비슷한 사례를 비교한다. 대표적인 성공 사례 하나만 골라 일반화하지 않는다.\n- 참여업체 수의 평균·범위·변화, 실제 하한가와 1순위 금액의 차이, 1순위와 2순위의 차이를 원화와 사정률 차이(%p)로 설명한다.\n- 업체별 투찰자료에 접근할 수 있으면 유효 투찰과 하한 미달을 구분하고, 사정률 구간별 밀집 정도와 반복 참여업체를 확인한다. 일부 페이지만 읽은 분포를 전체 경쟁 분포로 보고하지 않는다.\n- 지난 입찰에서 드물었던 구간이 이번에도 유리하다고 단정하지 않는다. 반복 낙찰이나 비슷한 가격만으로 담합 등 부정행위를 추정하지 않는다.\n\n### 온담의 과거 투찰\n\n사이트에서 `나의업체`로 확인된 주식회사 온담에프앤디의 과거 투찰금액·입찰사정률·순위·실제 하한가와의 차이·1순위와의 차이를 비교한다. 하한 미달과 유효가격이지만 높은 가격으로 밀린 경우를 구분한다. 한 번 높은 가격으로 실패했다는 이유만으로 다음에는 무조건 낮추라고 하지 않는다. 해당 공고에서 확인되지 않은 참여나 실적은 추정하지 않는다.\n\n### 추천금액과 수익성\n\n추천 후보 1개를 먼저 제시한 뒤 선택 근거와 다른 시나리오를 설명한다. 전체 평균, 동일 품목 평균, 최근 자료, 경쟁 분포 중 무엇을 얼마나 반영했는지 밝힌다. 가중치를 사용하는 경우 임의 판단인지 검증된 방법인지 구분한다. 평균 부근의 후보를 낙찰확률 최대값이라고 부르지 않는다.\n\n낮은 예가·중심 예가·높은 예가에 대응하는 후보를 비교한다. 금액이 높을수록 하한 미달 가능성은 줄 수 있지만 더 낮은 유효 투찰에 밀릴 수 있다는 관계를 설명한다. 실제 사정률이 어디를 넘으면 추천금액이 하한 미달이 되는지 역산한다.\n\n원가·배송·손질·폐기·인건비와 세금 조건을 확인할 수 있으면 손익을 반영한다. 원가를 받지 못했다면 가격 경쟁 후보까지 제시하되 이익 확보 여부는 확인되지 않았음을 짧게 명시한다. 자료 부족을 이유로 가능한 분석까지 중단하지 않는다.\n\n구매내역서의 개별 행과 공통 특수조건을 함께 읽는다. 한쪽 문서에 규격 문구가 빠졌다고 요구 조건이 없다고 단정하지 않는다. 공고·내역서·특수조건의 납품기간과 시간, 제품과 차량 온도처럼 적용 대상이 다른 숫자를 구분한다. 단가 공란으로 계산된 합계 0을 실제 원가 0으로 취급하지 않는다.\n\n## 용어와 계산 기준\n\n단순한 기초금액 기반 산식을 적용할 수 있는 공고에서는 다음과 같이 계산한다. A값 등 별도 산식이 있는 공고에는 자동 적용하지 않는다.\n\n- 실제 사정률(%) = 실제 예정가격 ÷ 기초금액 × 100.\n- 낙찰하한가 = 실제 예정가격 × 낙찰하한율. 원 단위 처리 방식은 해당 공고·시스템 기준을 따른다.\n- 후보 투찰금액 = 기초금액 × 가정한 사정률 ÷ 100 × 낙찰하한율 ÷ 100.\n- 입찰사정률(%) = 투찰금액 ÷ (기초금액 × 낙찰하한율 ÷ 100) × 100.\n- 기초대비 투찰률(%) = 투찰금액 ÷ 기초금액 × 100.\n- 예정가격 대비 투찰률(%) = 투찰금액 ÷ 실제 예정가격 × 100.\n\n발주처의 `예가/기초`와 업체의 `입찰사정률`은 다르다. 전자는 실제 추첨 예가를, 후자는 업체 가격의 위치를 보여준다. 화면에서 같은 사정률 용어를 사용하더라도 비교 열을 분명히 적는다. %와 %p도 구분한다.\n\n중간 계산은 충분한 정밀도로 유지하고 마지막에만 원 단위 또는 설명한 단위로 처리한다. 10원 단위 정리 같은 편의상 처리를 공식 규칙으로 설명하지 않는다. 소수점 네 자리나 끝자리 조정이 승률을 높인다는 근거 없는 주장을 하지 않는다.\n\n과거 적용 검증을 수행하면 당시까지 알려진 자료로만 후보를 만들고 이후 결과와 비교한다. 전체 기간을 보고 고른 값을 같은 기간에 적용한 결과는 사후 비교로 표시한다. 하한 통과율·과거 가상 순위·실제 낙찰률을 구분하고, 검증한 건수와 제외 조건을 공개한다.\n\n## 답변 순서와 검증\n\n1. 추천 후보 금액과 가정 사정률, 판단의 확실성.\n2. 공고 조건과 마감·개찰 시각.\n3. 확인 범위: 24개월 실제 날짜, 총건수, 더보기 마지막 페이지, 동일 품목 건수, 업체별 상세 확인 범위.\n4. 전체·동일 품목·최근 통계, 흐름·빈도 그래프의 의미와 한계.\n5. 과거 낙찰자의 하한가 접근 정도, 경쟁업체 분포, 온담의 과거 결과.\n6. 추천 산식·대안 시나리오·하한 미달 경계·원가 조건.\n7. 해당 주장을 확인할 수 있는 공고와 개찰 상세 링크.\n\n관측 사실·산술 계산·분석자의 판단을 구분한다. 전문가다운 설명은 과도한 확신이나 전문용어의 양이 아니라 확인 범위, 비교의 타당성, 계산의 정확성과 선택 근거로 보여준다. 화면에서 확인하지 못한 내용을 확인했다고 표현하지 않는다. 날짜·계정·공고 조건은 매번 현재 상태를 확인한다.\n\n기본 결과는 대화창에서 제공한다. 별도 보고서나 파일은 사용자가 요청하거나 이미 합의된 경우에만 납품한다. 이번에는 사용자가 이 방의 전체 대화 내용을 지침서에 보존하도록 명시했다. 독립 계산·검산은 병렬로 진행하되 같은 탭·파일을 동시에 조작하지 않는다.\n\n## 2026년 10월 8일 분석 사례\n\n아래는 E261001-674137-0을 분석했을 당시의 확인값과 판단값이다. 실제 낙찰 결과가 아니라 개찰 전 후보 선정 기록이며, 향후 입찰의 고정값으로 재사용하지 않는다.\n\n| 항목 | 당시 내용 |\n|---|---|\n| 공고 | 인천송도꿈유치원 2026년 11~12월 육류 구매 |\n| 기초금액 | 13,203,200원 |\n| 예가변동폭과 하한율 | ±3%, 90% |\n| 복수예가 방식 | 15개 중 4개 |\n| 투찰마감과 개찰 | 2026-10-12 09:00, 10:00 KST |\n| 발주처 분석 기간 | 2024-10-08~2026-10-08 |\n| 확인 범위 | 더보기 2/2, 개찰 32건, 육류 8건 |\n| 전체 사정률 | 평균 99.8863%, 중앙값 100.0359%, 범위 98.4755~100.9742% |\n| 육류 사정률 | 평균 99.4464%, 중앙값 99.4553%, 범위 98.4755~100.2949% |\n| 전체 최근 12건 평균 | 100.0651% |\n| 공동 최빈 구간 | 99.2xxx, 100.1xxx 각각 4/32건 |\n| 육류 참여업체 | 46~84개, 평균 66.875개 |\n| 추천 당시 후보 | 사정률 99.90%, 11,871,000원 |\n\n### 육류 과거 8건\n\n| 개찰일 | 실제 예가 사정률 | 1순위 입찰사정률 | 참여업체 수 |\n|---|---:|---:|---:|\n| 2026-07-29 | 99.6595% | 99.6610% | 84 |\n| 2026-04-06 | 100.2949% | 100.3146% | 58 |\n| 2026-01-28 | 98.4755% | 98.4984% | 74 |\n| 2025-10-20 | 98.8483% | 98.8632% | 68 |\n| 2025-08-01 | 100.0314% | 100.0459% | 60 |\n| 2025-04-17 | 100.1668% | 100.1680% | 68 |\n| 2025-02-14 | 98.8437% | 98.8984% | 77 |\n| 2024-11-14 | 99.2511% | 99.3101% | 46 |\n\n2026-07-29 육류 개찰에서 하한가 표시는 약 14,592,982원, 1순위 14,593,200원, 온담 14,654,870원·18순위였다. 온담과 1순위의 차이는 61,670원이며 입찰사정률은 각각 100.0821%와 99.6610%였다. 당시 1순위는 계산상 하한가보다 약 218원 높았다. 이 사실은 당시 가격 차이를 설명하며 다음 예가의 방향을 확정하지 않는다.\n\n추천 당시 산식은 `13,203,200 × 0.9990 × 0.90 = 11,870,997.12원`이었다. 후보 11,871,000원은 설명 편의를 위해 10원 단위로 올린 값이다. 실제 사정률이 약 99.900024%보다 높아지면 이 금액은 산술상 하한에 미달한다. 실제 사정률 100%의 하한가는 11,882,880원으로 후보보다 11,880원 높다. 원가는 제공되지 않았으며, 10월 8일 분석 당시 실제 투찰·낙찰 여부는 확인되지 않았다. 이후 사용자가 전달한 개찰 결과는 아래 후속 기록에서 구분한다.\n\n### 2026-10-12 사용자 전달 개찰 결과와 복기\n\n- 출처는 사용자의 이 방 후속 메시지다. 이번 기록 시점에 개찰 원문을 별도로 재조회한 결과가 아니다.\n- 사용자는 인천송도꿈유치원 건에서 자사 결과를 “-1등”, 예정가격 대비 투찰률을 89.92%로 전달했고, 낙찰하한율 90%에 미달했다고 밝혔다. 1등 금액은 11,881,680원으로 전달했다.\n- 앞서 제안한 후보 11,871,000원과 사용자 전달 1등 금액의 차이는 `11,881,680 - 11,871,000 = 10,680원`이다. 후속 메시지에는 자사의 실제 투찰금액이 별도로 적혀 있지 않으므로, 추천 후보와 실제 제출금액의 동일 여부는 구분한다.\n- 표시 투찰률 기준 하한율과의 차이는 0.08%p다. 89.92%는 예정가격 대비 투찰률이며, 추천 당시의 입찰사정률 약 99.90%와 분모가 다르다. 반올림·절사된 표시율만으로 실제 예정가격이나 정확한 하한 부족액을 확정하지 않는다. 1등과의 금액 차이 역시 하한 부족액과 같다고 간주하지 않는다.\n- 이번 결과는 사용자 전달 기준으로 하한 미달 사례다. “거의 맞혔다”는 이유로 기존 추천의 예측력을 입증했다고 평가하지 않는다. 1등 금액을 사전에 예측할 수 있었다고 설명하거나, 정확한 하한가·동가 조건 없이 10,680원만 올리면 단독 낙찰이었다고 단정하지 않는다.\n- 다음 분석에서는 후보별 하한 미달 경계와 유효가격 내 경쟁력을 함께 비교한다. 이번 한 건을 이유로 다음 투찰에 10,680원을 고정 가산하거나 사정률을 일괄 상향하지 않는다. 기존 과거 검증과 이번 후속 결과를 구분해 누적하고, 원문 확보 시 실제 예정가격·하한가·자사 제출금액·전체 순위를 보완한다.\n\n### 당시 확인 페이지\n\n- [사용자가 지정한 공고 검색](https://www.bidq.co.kr/bidq/bids/list?bidtype=pur&bid_suc=bid&searchWord=E261001-674137-0&word_type=all_Search&subWord=E261001-674137-0)\n- [E261001-674137-0 공고 상세](https://www.bidq.co.kr/bidq/bids/detail/bid?bidid=261002070008748-00-00-01&bidtype=pur)\n- [인천송도꿈유치원 발주처 분석](https://www.bidq.co.kr/bidq/analysis/orgi?bidtype=pur&org=%EC%9D%B8%EC%B2%9C%EA%B4%91%EC%97%AD%EC%8B%9C+%EC%97%B0%EC%88%98%EA%B5%AC+%EC%9D%B8%EC%B2%9C%EC%86%A1%EB%8F%84%EA%BF%88%EC%9C%A0%EC%B9%98%EC%9B%90%28%ED%95%99%EA%B5%90%EA%B8%89%EC%8B%9D%29)\n- [2026년 7월 29일 육류 개찰 상세](https://www.bidq.co.kr/bidq/bids/detail/suc?bidid=260723070135259-00-00-01&bidtype=pur)\n- [요청과 답변 전체 원문 및 첨부 이미지](입찰분석_대화기록/2026-10-08_입찰가분석하기_전체대화.md)\n\n## 같은 날 추가 조사 결과\n\n육류 8건의 참여업체 목록을 마지막 페이지까지 읽어 총 535개 참여 기록을 확인했다. 공고별 표시 업체 수와 읽은 행 수 및 등록번호 중복 여부가 일치했다. 등록번호 기준 고유 업체는 122개, 8건 모두 참여한 업체는 19개, 1순위 업체는 8건 모두 달랐다. 같은 기관에서 계속 참여하는 업체가 많다는 관측이며 향후 낙찰자를 예측하는 결과는 아니다.\n\n| 개찰일 | 참여 기록 수 | 계산상 하한 미달 | 1순위 업체 | 1순위와 2순위 금액 차이 |\n|---|---:|---:|---|---:|\n| 2026-07-29 | 84 | 32 | 청정 | 700원 |\n| 2026-04-06 | 58 | 38 | 송도푸드 | 17,550원 |\n| 2026-01-28 | 74 | 0 | 현대식품 | 9,670원 |\n| 2025-10-20 | 68 | 5 | 푸드케이 | 19,600원 |\n| 2025-08-01 | 60 | 25 | 다인 | 1,380원 |\n| 2025-04-17 | 68 | 35 | 신선푸드 | 590원 |\n| 2025-02-14 | 77 | 5 | 육라운드 | 900원 |\n| 2024-11-14 | 46 | 10 | 주)대경축산 | 7,290원 |\n\n535개 기록에서 업체 입찰사정률의 0.1%p 구간별 빈도는 100.1xxx 36건, 99.8xxx 32건, 100.0xxx와 100.2xxx 각 30건이었다. 이는 과거 업체의 투찰 분포이며 발주처의 예가 분포나 이번 승률이 아니다. 낮은 빈도 구간을 골랐다는 이유만으로 유리하다고 판단하지 않는다.\n\n이 8건의 전체 명단에서 현재 온담 등록번호에 해당하는 참여는 2026-07-29 한 건에서만 확인됐다. 다른 7건에서 해당 번호가 보이지 않는다는 사실만 기록하고, 온담이 장기간 일관되게 높은 가격을 쓰는 업체라고 일반화하지 않는다.\n\n11,871,000원의 내재 입찰사정률 약 99.900024%를 각 과거 공고의 기초금액과 하한율에 맞춰 적용하면 3건은 하한 미달, 나머지 5건은 기록상 1순위보다 높은 가격이었다. 99.45%는 미달 4건·통과 4건, 100.10%는 미달 2건·통과 6건이며, 세 후보 모두 하한 이상이면서 당시 1순위보다 낮은 사례가 없었다. 원화 금액을 과거 공고에 그대로 대입한 계산이 아니다. 전체 자료를 보고 고른 후보의 사후 비교이므로 미래 낙찰확률이나 검증된 투자·입찰 전략으로 표현하지 않는다.\n\n시간순으로 이전 개찰일까지의 자료만 사용해 다음 육류 공고 7건을 비교했을 때 기관 전체 평균의 평균절대오차는 0.6283%p, 이전 육류 평균은 0.7263%p, 항상 100%는 0.6666%p였다. 같은 개찰일 다른 품목 결과는 입력에서 제외했다. 표본 7건으로 방법의 우월성을 확정할 수 없다. 기존 99.90%는 중심 부근의 가정 후보이며 낙찰에 유리하다는 검증이 확보된 값이 아니라는 판단을 유지한다.\n\n### 구매내역서와 특수조건\n\n- [축산물 구매내역서 원본](입찰분석_대화기록/2026년_11-12월_축산물_구매내역서.xlsx): `sheet1!C6:G26`에 21품목·790.2kg. 닭 232kg, 돼지 415kg, 육우 143.2kg. `H6:H26`의 단가는 공란이며 금액 열의 저장값 0은 원가가 아니다.\n- 구매내역 사용기간은 `B4`의 2026-11-02~12-24이고, 공고의 계약기간 2026-11-01~12-31과 구분한다. 월별·일자별 수량과 고정 배송 횟수는 내역서에 없다.\n- [특수조건 원본](입찰분석_대화기록/2026_납품업체자격_특수조건.hwp)과 [추출 텍스트](입찰분석_대화기록/특수조건_추출.txt)를 함께 보존한다. 제8조는 08:00~09:00 납품·매주 발주서·발주 후 1일 이내 원칙을 명시한다. 공고 본문의 08:00 표기와 함께 실제 검수시각을 확인할 필요가 있다. 매주 발주서가 주 1회 배송을 뜻하지 않는다.\n- 붙임2 축산물류 ⑧3)은 닭고기에 1등급 이상·무항생제를 요구한다. 내역서의 삼계용 닭 15kg 행에 무항생제 문구가 없더라도 공통 조건을 함께 반영해야 한다.\n- `sheet1!C25:G25`는 육우 양지/얇은슬라이스와 속성 차돌박이를 함께 표시한다. 규격에 맞는 견적을 사용하며 일반 양지 가격으로 임의 대체하지 않는다.\n- 내역서 `B28`의 냉장품 5℃ 이하·냉동품 −18℃ 이하와 특수조건 제10조의 운반차량 냉장 10℃ 이하·온도기록장치 기준은 적용 대상이 다르다. 제품 온도를 10℃까지 허용한다고 해석하지 않는다.\n- 제6조·제13조는 수량 변동과 계약단가에 따른 실제 수량 월 정산을, 제17조는 계약단가 무변동 원칙과 조건부 협의를 명시한다. 제12조의 반품 비용, 제14조의 지연배상, 보험·증명서·가공·포장·냉장운송 비용도 원가 판단에 반영한다.\n- 내역서 `B29:B31`은 식품명 순서·규격·단위·사용량의 임의 변경 시 무효 문구를 포함한다. 원본 양식을 수정해 분석 결과를 덮어쓰지 않는다.\n\n후보 11,871,000원을 예정 총량으로 나누면 혼합 평균 매출은 15,022.78원/kg이다. 모든 품목 평균 매입원가가 100원/kg 달라지면 예정 총량 기준 총원가는 79,020원 달라진다. 이는 단가·수량 가정의 민감도이며 수익 예측은 아니다. 실제 21개 품목별 매입단가와 배송·가공 등 비용을 확인하기 전에는 수익성을 확정하지 않는다.\n\n## 행정구역 변경 기관의 자료 연결과 조회 범위 2026년 10월 12일\n\n- 학교명이 같고 행정구역 표기만 변경된 기관은 학교명을 기준으로 과거 발주처 별칭을 연결한다. 기관명이 비슷하다는 이유만으로 다른 학교를 합치지 않는다. 이번 인천가석초등학교는 서해구·서구·서구가정3동, 청라유치원은 서해구·서구 표기를 확인 대상으로 삼는다.\n- 현재 공고번호로 이번 기초금액·낙찰하한율·예가변동폭·마감·정정 여부를 먼저 확인한다. 사이트별 용어가 달라도 의미와 분모를 확인하며, 과거 공고 조건으로 이번 조건을 대체하지 않는다.\n- 이전 자료가 부족하면 공고번호 대신 학교명으로 검색한 뒤 `구매`를 선택하여 행정구역 변경 전 공고를 찾는다. 과거 공고에서는 발주처 분석·사정률 분석·복수예가 분석을 확인한다.\n- 각 발주처 별칭에 대해 위 세 분석을 각각 24개월로 조회하고 실제 조회 시작일·종료일, 적용 필터, 총건수를 기록한다. 흐름·빈도 그래프와 하단 표를 읽고 `더보기` 마지막 페이지 또는 비활성 상태까지 확인한다. 자료가 없는 분석은 0건으로 구분한다.\n- 별칭별 자료는 공고번호를 기준으로 중복 제거한다. 같은 공고의 정정·재공고·유찰·재입찰을 독립 낙찰 표본으로 중복 계산하지 않는다. 전체 기관 자료와 동일 육류·축산물 자료를 분리하고, 별칭별 건수와 통합 건수를 대조한다.\n- 영주 Chrome의 기존 비드큐 세션에서 사용자 마우스·키보드·클립보드·창 포커스를 점유하지 않는 백그라운드 방식만 사용한다. 학교별 조사와 독립 검산은 병렬화하되 같은 탭·파일을 동시에 조작하지 않는다.\n- 사용자가 이번 작업에서 eaT 사이트는 불필요하다고 정정했으므로 eaT에 접속하지 않는다. 비드큐에서 확인한 자료를 기준으로 분석하며 실제 투찰·계약 제출은 수행하지 않는다.\n\n이번 요청과 후속 정정 및 첨부 10장의 보존 범위는 [가석 청라 최신 사용자 요청 기록](입찰분석_대화기록/2026-10-13_가석청라_사용자요청.md)에 기록한다. 이는 기존 전체 대화 기록을 대체하지 않는다.\n## 2026-10-15 학교별 AI 후보 선택과 잠정 입찰가\n\n- 과거 자료의 검증 우위가 불분명해도 100%를 자동 추천 입찰가로 대체하지 않는다. 100%는 다른 후보와 비교하는 기준이며, 학교별 증거가 직접 지지할 때만 선택할 수 있다.\n- 이번 공고 조건과 변경 전후 발주처의 24개월 전체 원자료를 먼저 검산한다. 동일 품목·하한율, 독립 개찰일, 최근 흐름, 복수예가와 실제 경쟁 표본을 구분해 AI에 제공한다. 자료 수집 단계별 건수와 후보 생성·탈락 사유를 남긴다.\n- 코드는 예정가격/기초금액 예가율 후보와 원 단위 가격을 계산하고 하한율을 마지막에 한 번만 적용한다. AI는 계산된 후보 중 하나를 근거 ID와 반대 근거를 들어 고른다. AI가 임의 금액·확률·가중치를 생성하거나 원자료 숫자를 바꾸는 것은 허용하지 않는다.\n- 이번 공고와 수집 범위가 검증되고 비교 가능한 학교 자료가 있으면 AI는 통계적 우위가 확정되지 않았다는 이유만으로 판단을 보류하지 않는다. 계산된 후보 중 가장 설명 가능한 **잠정 입찰가 1개**를 고르고, 최근 변동·표본 부족·하한 미달과 경쟁 탈락 위험을 반대 근거로 적는다.\n- AI가 응답하지 않거나 근거 검증에서 탈락해도 수집 자료가 완전하면, 계산된 비100% 후보 중 시간순 사후 오차가 확인된 후보를 **통계 잠정가**로 표시한다. AI가 고른 가격처럼 표기하지 않고, 사후 표본이 부족한 경우 동일 품목 중앙값을 우선 비교한다. 단순 100%를 자동 대체값으로 쓰지 않는다.\n- 현재 공고의 기초금액·하한율·정정 내용과 변경 전후 발주처의 과거 개찰자료가 검증되면 잠정 가격을 표시한다. 복수예가 세부자료·경쟁업체 전체 목록·원가·업체 참가자격이 미확인된 경우는 가격과 별도 경고로 표시한다.\n- 공고의 핵심 금액을 확인할 수 없거나 검증 가능한 과거 개찰자료·허용 가능한 비100% 가격 후보가 전혀 없을 때만 **가격 산정 불가**로 표시하고, 수집 실패인지 실제 비교 자료 부재인지와 누락 항목을 구분해 알린다. 근거 없는 숫자를 만들지 않는다.\n- AI 선택은 판단이며 낙찰 우위의 실증이 아니다. 과거에 실제로 알 수 있었던 자료만 사용한 시간순 검증으로 이후 결과와 비교하고, 경쟁업체·원가·참가자격을 별도로 확인한다. 최종 실제 투찰은 사용자가 결정한다.\n"
import hmac
import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
import streamlit as st
import requests
ROOT = Path(__file__).parent
KST = ZoneInfo('Asia/Seoul')

def safe_internal_error(error):
    """Show a diagnostic code location without exposing credentials in the error text."""
    frames = traceback.extract_tb(error.__traceback__)
    local = next((frame for frame in reversed(frames) if Path(frame.filename).parent == ROOT), None)
    location = f'{Path(local.filename).name}:{local.lineno}' if local else '위치 미확인'
    return f'앱 내부 처리 오류 ({type(error).__name__}, {location}). 해당 공고의 가격은 표시하지 않았습니다.'
METHOD_LABELS = {'fixed_100': '100% 비교 기준', 'all_mean': '동일 하한율 기관 평균', 'item_mean': '동일 품목 평균', 'item_median': '동일 품목 중앙값', 'recent_item3': '최근 3개 개찰일 평균', 'item_date_mean': '동일 품목 개찰일별 평균', 'recent_item_weighted': '최근 개찰일 가중평균', 'low_scenario': '낮은 예가 민감도', 'high_scenario': '높은 예가 민감도'}

def clean(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value

def packed(value):
    return json.dumps(clean(value), ensure_ascii=False, indent=2)

def secret(key):
    try:
        return str(st.secrets.get(key, ''))
    except Exception:
        return ''

def analyze(bundle, cost_floor=None):
    if not isinstance(bundle, dict) or not bundle.get('rows'):
        raise ValueError('원자료가 비어 있습니다.')
    notice = dict(bundle['notice'])
    notice['date'] = bundle.get('analysis_cutoff') or bundle.get('report', {}).get('coverage', {}).get('cutoff') or datetime.now(KST).date().isoformat()
    notice['coverage'] = bundle.get('coverage', {})
    return report_analysis(notice, bundle['rows'], cost_floor=cost_floor)

def collect(number, userid, password, aliases):
    client = BidQClient(userid, password, today=datetime.now(KST).date())
    try:
        return client.collect(number, legacy_names=aliases, months=24)
    except CollectionError as error:
        raise CollectionError(f"{getattr(client, 'stage', '자료 수집')}: {error}") from None
    except requests.exceptions.Timeout:
        raise CollectionError(f"{getattr(client, 'stage', '자료 수집')}: 비드큐 응답 시간 초과") from None
    except requests.exceptions.HTTPError as error:
        status = error.response.status_code if error.response is not None else '미확인'
        raise CollectionError(f"{getattr(client, 'stage', '자료 수집')}: 비드큐 HTTP {status}") from None
    except requests.exceptions.RequestException:
        raise CollectionError(f"{getattr(client, 'stage', '자료 수집')}: 비드큐 통신 또는 응답 형식 오류") from None
    finally:
        client.close()

def collect_analyze(number, userid, password, aliases, cost_floor, api_key, model):
    bundle = collect(number, userid, password, aliases)
    bundle['analysis_cutoff'] = datetime.now(KST).date().isoformat()
    try:
        report = analyze(bundle, cost_floor)
    except ValueError as error:
        missing = [label for key, label in (('base', '기초금액'), ('floor', '낙찰하한율'), ('variation', '예가변동폭')) if bundle.get('notice', {}).get(key) is None]
        message = '공고 필수 항목 미확인: ' + ', '.join(missing) if missing else str(error)
        raise CollectionError('금액 검산: ' + message) from None
    except (KeyError, TypeError):
        raise CollectionError('금액 검산: 자료 구조가 예상 형식과 달라 계산을 중단했습니다') from None
    if api_key:
        try:
            bundle['gemini_review'] = review_candidates(api_key, model, bundle['notice'], report)
        except Exception:
            bundle['gemini_review'] = {'status': 'error', 'reason': 'Gemini 검토 처리 오류: 추천 보류'}
        report = apply_ai_selection(report, bundle['gemini_review'])
    else:
        bundle['gemini_review'] = {'status': 'error', 'reason': 'Gemini API 키가 없어 학교별 후보 선택을 실행하지 못했습니다'}
        report = apply_ai_selection(report, bundle['gemini_review'])
    bundle['report'] = report
    return (bundle, report)

def remember(bundle, report):
    snapshot = clean({**bundle, 'report': report, 'captured_at': datetime.now(KST).isoformat()})
    st.session_state.setdefault('records', []).append(snapshot)

def display(bundle, report, prefix):
    notice = bundle['notice']
    main = report['main_candidate']
    state_label = '가격 산정 자료 확인 · 추가 자료·참가자격 별도 확인' if report.get('data_status') == 'verified' else '잠정·가격 산정 핵심 자료 확인 미완료'
    if report['status'] == 'verified':
        state_label = '자료·계산 검증 완료'
    st.dataframe([{'학교': notice.get('name', ''), '입찰가': f"{main['price']:,}원" if main else '추천 보류', '마감': notice.get('deadline', '미확인'), '상태': state_label}], hide_index=True, width='stretch')
    if main:
        selection_label = 'AI가 학교 자료에서 선택한 후보' if report.get('recommendation_status') == 'ai_selected' else 'AI 선택 미완료 · 통계 잠정 후보'
        st.caption(f"{selection_label}: {METHOD_LABELS.get(main['method'], main['method'])} · 가정 사정률 {main['rate']:.6f}% / 금액 역산 사정률 {main['boundary']:.6f}% / 하한율 {notice['floor']}%")
        st.caption(f"실제 예가율 {main['boundary']:.6f}% 초과 시 산술상 미달. 자릿수만 늘린다고 동가를 피하거나 예측력이 높아지는 것은 아닙니다.")
    else:
        st.caption('AI 근거 검증 또는 자료 확인이 끝나지 않아 추천 입찰가를 보류했습니다. 100%는 아래 비교 기준일 뿐입니다.')
    counts = report['diagnostics']['included_by_commodity']
    st.caption(f"과거 자료: 축산물 {counts['육류']}건 · 농산물 {counts['농산물']}건 사용 / 다른 품목 {report['diagnostics']['excluded_rows']}건 제외")
    coverage_labels = {'qualification_verified': '업체 참가자격 증빙', 'correction_verified': '최신 정정 내역', 'history_24_months_complete': '변경 전후 24개월 전체 자료', 'all_pages_complete': '마지막 페이지', 'three_analyses_complete': '발주처·사정률·복수예가', 'multiple_estimates_verified': '복수예가 검산', 'competition_verified': '경쟁업체 전체 목록', 'notice_verified': '공고 조건', 'notice_source_complete': '공고 원문·참가조건'}
    for warning in dict.fromkeys(report['warnings'] + bundle.get('issues', [])):
        for field, label in coverage_labels.items():
            warning = warning.replace(field, label)
        st.warning(warning)
    ai = bundle.get('gemini_review')
    if ai and ai.get('status') in ('error', 'rejected'):
        st.warning('Gemini 근거 검토 미완료: ' + ai.get('reason', '응답 확인 실패') + ' · 추가 설정에서 모델을 바꾼 뒤 AI 검토만 다시 실행할 수 있습니다.')
    with st.expander('근거·대안·수집 범위·검증'):
        st.write(report['reason'])
        st.write(report['counterargument'])
        st.write(report['interpretation'])
        st.dataframe([{'방법': METHOD_LABELS.get(c['method'], c['method']), '사정률': str(c['rate']), '금액': c['price'], '차액': c['difference'], '미달경계': str(c['boundary'])} for c in report['candidates']], hide_index=True, width='stretch')
        st.json(clean({'diagnostics': report.get('diagnostics'), 'AI 근거': (ai or {}).get('evidence_refs')}), expanded=False)
        nearby = []
        for delta in (Decimal('-0.005'), Decimal('0'), Decimal('0.005')) if main else ():
            try:
                trial = calculate_bid(notice['base'], main['rate'] + delta, notice['floor'], notice.get('variation', 3))
                nearby.append({'가정 사정률': f"{trial['rate']:.6f}%", '입찰가': trial['price'], '차액': trial['price'] - main['price'], '금액 역산 사정률': f"{trial['boundary']:.6f}%"})
            except ValueError:
                pass
        st.write('인근 세밀 후보의 민감도 비교 — 동가 회피나 낙찰 우위를 검증한 가격이 아닙니다.')
        st.dataframe(nearby, hide_index=True, width='stretch')
        st.write(f"과거 공고 {report['coverage']['collected_rows']}건 · 독립 개찰일 {report['coverage']['independent_dates']}일")
        st.json(clean({'statistics': report['statistics'], 'coverage': report['coverage'], 'validation': report['validation'], 'replay': report.get('replay')}), expanded=False)
        if bundle.get('aliases'):
            st.write('학교명으로 연결한 발주처와 수집 건수')
            st.json(clean(bundle['aliases']), expanded=False)
        if bundle.get('gemini_review'):
            st.write('Gemini 후보 선택 근거 — 계산된 후보 중에서만 선택합니다.')
            st.json(clean(bundle['gemini_review']), expanded=False)
        if notice.get('conditions_evidence'):
            st.write('비드큐에 게시된 공고 원문·정정·참가조건')
            st.json(clean(notice['conditions_evidence']), expanded=False)
            st.caption('업체의 실제 HACCP·등록·소재지 증빙과 외부 첨부문서는 별도 확인이 필요합니다.')
    snapshot = {**bundle, 'report': report, 'captured_at': datetime.now(KST).isoformat()}
    st.download_button('원자료·검산 JSON', packed(snapshot), f'{prefix}_분석.json', 'application/json', key=f'{prefix}_download')
    st.caption('추천내역은 이 세션에 자동 기록됩니다. 영구 저장소 연결 전에는 기록 JSON을 보관하세요.')
st.set_page_config(page_title='우리 입찰 분석기', page_icon='🎯', layout='wide')
access = secret('APP_ACCESS_PASSWORD')
if access:
    if not hmac.compare_digest(st.text_input('앱 접속 암호', type='password'), access):
        st.info('접속 암호를 입력하세요.')
        st.stop()
st.title('🎯 우리 입찰 분석기')
st.caption('우리 지침 기준 · 가격 먼저 · 하한율은 한 번만 적용 · 실제 투찰 기능 없음')
with st.sidebar:
    st.header('비드큐 연결')
    private_app = secret('OWNER_PRIVATE_APP').lower() == 'true'
    server_available = bool(secret('BIDQ_ID') and secret('BIDQ_PW'))
    saved_connection = server_available and bool(access or private_app)
    if saved_connection:
        userid = secret('BIDQ_ID')
        password = secret('BIDQ_PW')
        api_key = secret('GEMINI_API_KEY')
        st.success('저장된 연결정보 자동 사용 · 공고번호만 입력하세요.')
        st.caption('비밀번호와 API 키는 서버에서만 사용하며 화면에 표시하지 않습니다.')
        st.caption('연결정보 변경은 Streamlit 관리 화면의 Secrets에서 한 번만 합니다.')
    else:
        userid = st.text_input('비드큐 아이디')
        password = st.text_input('비드큐 비밀번호', type='password')
        api_key = st.text_input('Gemini API Key', type='password')
        if secret('GEMINI_API_KEY'):
            st.caption('서버 Gemini 키는 해당 비드큐 계정의 로그인 성공 후 사용합니다.')
            if userid and hmac.compare_digest(userid, secret('BIDQ_ID')):
                api_key = api_key or secret('GEMINI_API_KEY')
    with st.expander('추가 설정'):
        default_model = secret('GEMINI_MODEL') or 'gemini-3.8-flash'
        if st.button('현재 모델 목록 새로고침'):
            available = list_generation_models(api_key)
            if available.get('status') == 'ok':
                st.session_state['generation_models'] = available['models']
                st.success('API가 반환한 모델 목록을 확인했습니다. 호출 가능 여부는 실제 검토 시 확인합니다.')
            else:
                st.warning(available.get('reason', '모델 목록 조회 실패'))
        options = list(dict.fromkeys([default_model, *st.session_state.get('generation_models', []), 'gemini-3.8-flash', 'gemini-3.6-flash', 'gemini-3.5-flash-lite']))
        selection = st.selectbox('Gemini 모델 선택', options + ['직접 입력'], key='selected_gemini_model')
        model = st.text_input('Gemini 모델 직접 입력', value=default_model, key='custom_gemini_model') if selection == '직접 입력' else selection
        st.caption('선택한 모델을 사용하며 다른 모델로 몰래 변경하지 않습니다. 모델별 API 사용료는 다를 수 있습니다.')
        if st.button('수집 자료로 AI 검토만 다시 실행'):
            existing = st.session_state.get('results', [])
            if not api_key:
                st.warning('Gemini API 키가 필요합니다.')
            elif not existing:
                st.info('먼저 비드큐 자료 수집을 실행하세요.')
            else:
                for collected, current_report in existing:
                    collected['gemini_review'] = review_candidates(api_key, model, collected['notice'], current_report)
                st.session_state['results'] = [(collected, apply_ai_selection(current_report, collected['gemini_review'])) for collected, current_report in existing]
                st.success('AI가 수집 자료를 다시 검토했습니다. 후보 선택과 보류 사유를 아래에서 확인하세요.')
    st.caption('금액은 코드가 검산하고 Gemini가 학교별 근거로 후보를 선택합니다. 키·접속정보는 코드·공유 캐시·다운로드에 저장하지 않습니다.')
    minimum = st.number_input('최저 수용 입찰가(원, 미확인 시 0)', min_value=0, value=0, step=1000)
    cost_floor = minimum or None
live, imported, exact, records, guide = st.tabs(['비드큐 분석', '원자료 재분석', '정확한 금액 계산', '투찰 기록', '적용 지침'])
with live:
    st.caption('이번 공고번호만 입력하세요. 학교명으로 변경 전후 발주처를 검색합니다. 여러 공고는 줄바꿈으로 입력하세요.')
    numbers = st.text_area('이번 공고번호', placeholder='E261008-674394-0')
    with st.expander('자동 연결이 불완전할 때만 보완'):
        legacy = st.text_area('추가 발주처명(선택, 한 줄에 하나)')
    if st.button('자료 수집·후보 비교', type='primary'):
        requested = list(dict.fromkeys((x.strip() for x in numbers.splitlines() if x.strip())))
        if not requested:
            st.error('공고번호를 입력하세요.')
        elif not userid or not password:
            st.error('비드큐 아이디·비밀번호를 입력하세요.')
        elif len(requested) > 20:
            st.error('한 번에 최대 20건을 입력하세요.')
        else:
            aliases = [x.strip() for x in legacy.splitlines() if x.strip()]
            st.session_state['results'] = []
            with st.spinner('학교별 병렬 수집·검산 중'):
                with ThreadPoolExecutor(max_workers=min(3, len(requested))) as pool:
                    futures = {pool.submit(collect_analyze, n, userid, password, aliases, cost_floor, api_key, model): n for n in requested}
                    for future in as_completed(futures):
                        number = futures[future]
                        try:
                            bundle, report = future.result()
                            st.session_state['results'].append((bundle, report))
                            remember(bundle, report)
                            chosen = report.get('main_candidate')
                            st.write(f"{bundle['notice'].get('name', number)}: {chosen['price']:,}원 ({report['status']})" if chosen else f"{bundle['notice'].get('name', number)}: 추천 보류 · 근거·자료 확인 필요")
                        except CollectionError as error:
                            st.error(f'{number}: {error}. 확인되지 않은 자료로 가격을 만들지 않습니다.')
                        except Exception as error:
                            st.error(f'{number}: {safe_internal_error(error)}')
    for index, (bundle, report) in enumerate(st.session_state.get('results', [])):
        display(bundle, report, f'live_{index}')
with imported:
    st.caption('현재 공고·기간·수집 범위를 확인한 notice/rows/coverage 원자료를 사용합니다.')
    upload = st.file_uploader('원자료 JSON', type=['json'], key='history')
    if st.button('저장 자료로 후보 비교'):
        if not upload:
            st.error('원자료 파일을 선택하세요.')
        else:
            try:
                bundle = json.load(upload)
                report = analyze(bundle, cost_floor)
                bundle['gemini_review'] = review_candidates(api_key, model, bundle['notice'], report) if api_key else {'status': 'error', 'reason': 'Gemini API 키가 없습니다'}
                report = apply_ai_selection(report, bundle['gemini_review'])
                st.session_state['imported'] = (bundle, report)
                remember(bundle, report)
            except (ValueError, KeyError, TypeError) as error:
                st.error(str(error))
    if st.session_state.get('imported'):
        display(*st.session_state['imported'], 'imported')
with exact:
    st.caption('같은 입력이면 기존 추천금액과 원 단위까지 일치합니다. 사정률은 예정가격÷기초금액×100입니다.')
    base = st.number_input('기초금액(원)', min_value=1, value=52532200, step=1)
    rate = st.text_input('예가/기초 사정률(%)', value='99.95')
    floor = st.selectbox('낙찰하한율(%)', [88, 90])
    if st.button('금액 계산'):
        try:
            result = calculate_bid(base, rate, floor)
            st.metric('계산된 비교 입찰가', f"{result['price']:,}원")
            st.caption(f"{base:,} × {rate}/100 × {floor}/100 → 마지막 원 미만 올림. 실제 예가율 {result['boundary']:.6f}% 초과 시 미달.")
        except ValueError as error:
            st.error(str(error))
    fixture = ROOT / 'approved_candidates.json'
    approved = json.loads(fixture.read_text(encoding='utf-8')) if fixture.exists() else globals().get('APPROVED_FIXTURES', [])
    if approved:
        st.write('2026-10-14 후보 재현 — 지난 공고의 기록이며 새 공고 추천값이 아닙니다.')
        rows = []
        for case in approved:
            price = calculate_bid(case['base'], case['rate'], case['floor'])['price']
            rows.append({'학교': case['school'], '기존 추천': case['price'], '재계산': price, '일치': price == case['price']})
        st.dataframe(rows, hide_index=True, width='stretch')
with records:
    st.caption('다른 방문자와 기록을 공유하지 않습니다. 세션 종료·서버 재시작 전에 JSON을 보관하세요. 실제 결과는 공식 자료로 확인해 별도 기록합니다.')
    saved = st.session_state.setdefault('records', [])
    if st.button('비드큐에서 실제 결과 확인'):
        if not userid or not password:
            st.error('비드큐 연결정보를 입력하세요.')
        elif not saved:
            st.info('확인할 추천 기록이 없습니다.')
        else:
            client = BidQClient(userid, password, today=datetime.now(KST).date())
            try:
                for record in saved:
                    code = record['notice'].get('id') or record['notice'].get('notice_number')
                    if code:
                        try:
                            record['official_result'] = clean(client.get_result(code))
                        except Exception:
                            record['official_result'] = {'status': '미확인', 'reason': '공식 결과 조회 실패'}
                st.success('확인 가능한 공식 결과를 추가했습니다. 사전 추천과 실제 제출금액은 변경하지 않았습니다.')
            finally:
                client.close()
    incoming = st.file_uploader('기록 JSON 가져오기', type=['json'], key='records_upload')
    if st.button('기록 불러오기') and incoming:
        try:
            values = json.load(incoming)
            if not isinstance(values, list) or not all((isinstance(v, dict) and 'notice' in v for v in values)):
                raise ValueError('기록 형식이 다릅니다.')
            st.session_state['records'] = values
            st.rerun()
        except (ValueError, TypeError) as error:
            st.error(str(error))
    if saved:
        chosen = st.selectbox('결과를 추가할 기록', range(len(saved)), format_func=lambda i: saved[i]['notice'].get('name', str(i)))
        submitted = st.number_input('실제 제출금액(원)', min_value=0, value=0)
        planned = st.number_input('실제 예정가격(원)', min_value=0, value=0)
        outcome = st.selectbox('공식 확인 결과', ['미확인', '낙찰', '하한 미달', '유효가격 패찰', '기타'])
        if st.button('공식 결과 추가'):
            saved[chosen]['actual_result'] = {'submitted': submitted or None, 'planned': planned or None, 'outcome': outcome}
            st.success('사전 추천을 보존한 채 결과를 추가했습니다.')
        st.json(clean(saved))
    st.download_button('기록 JSON 내려받기', packed(saved), '입찰기록.json', 'application/json')
with guide:
    path = ROOT / 'BID_GUIDE.md'
    if path.exists():
        st.markdown(path.read_text(encoding='utf-8'))
    elif globals().get('APPLIED_GUIDE'):
        st.markdown(APPLIED_GUIDE)
    else:
        st.error('적용 지침 파일이 누락되었습니다.')
    st.caption('자료 연결 미완료는 명시합니다. 가격 정확성과 낙찰 가능성을 구분하며 결과를 보장하지 않습니다.')
