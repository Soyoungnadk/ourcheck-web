# ourcheck 판정 시뮬레이터

**https://soyoungnadk.github.io/ourcheck-web/**

내 자산·거주지·무주택 여부·청약통장 조건을 넣으면, 공고별로
**신청 가능한지와 그 근거**를 보여주는 한 장짜리 페이지다.

## 무엇을 하나

7개 규칙(`TYPE` `SCHEDULE` `REGION` `HOMELESS` `ACCOUNT` `BUDGET` `SPECIAL`)을
차례로 검사하고, 통과 여부와 사유를 그대로 보여준다.
"안 된다"만 알려주면 무엇을 고쳐야 할지 알 수 없기 때문이다.

- 입력값은 브라우저 `localStorage` 에만 남는다. 서버로 보내지 않는다.
- 빌드도 설치도 필요 없다. `index.html` 한 장에 CSS·JS가 모두 들어 있고,
  외부 의존은 Google Fonts 뿐이다.

## 주의

- 공고 데이터는 규칙 확인용 **샘플**이다. 실시간 청약홈 데이터가 아니다.
  (브라우저에서는 공공데이터포털 API 가 CORS 로 막힌다 — 실시간 수집은 앱에서만 된다)
- 판정 규칙은 「주택공급에 관한 규칙」을 코드로 옮긴 해석이다.
  **실제 신청 전에는 반드시 청약홈 모집공고문을 확인할 것.**

## 앱과의 관계

판정 로직은 비공개 저장소 `ourcheck` 의
`src/app/lib/domain/matching_engine.dart` 를 JavaScript 로 옮긴 사본이다.
규칙의 정본은 Dart 쪽이며, 상수를 고치면 양쪽을 함께 고쳐야 한다.
