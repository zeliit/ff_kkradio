# ff_kkradio

FlaskFarm(FF)용 국내 인터넷 라디오 플러그인.

## 주요 기능

- 전체 M3U: `/ff_kkradio/api/m3u`
- 묶음 M3U: `/ff_kkradio/api/m3u?group=부산·경남`
- 방송사별 M3U: `/ff_kkradio/api/m3u?station=kbs`
- 개별 M3U: `/ff_kkradio/api/channel.m3u?id=kbs_1radio`
- 개별 고정 재생 URL: `/ff_kkradio/api/live.m3u8?id=kbs_1radio`
- radio.bsod.kr `/api/url` 기반 동적 원본 해석
- KBS는 radio.bsod.kr 실패 시 KBS 공식 API 직접 해석 fallback
- KRadio 방식의 User-Agent / Referer 헤더 주입
- HLS manifest 내부 URL을 FF 프록시 주소로 재작성하여 세그먼트 요청까지 헤더 유지
- 동적 URL 10분 캐시

## 설치

플러그인 폴더 이름을 `ff_kkradio`로 두고 FF plugins 디렉터리에 설치한 뒤 FF를 재시작합니다.

향후 GitHub 저장소 생성 후 `https://github.com/zeliit/ff_kkradio`로 직접 설치하는 구조를 권장합니다.

## 참고

초기 0.1.0은 radio.bsod.kr의 resolver와 KRadio의 헤더/재생 구조를 결합한 형태입니다. 방송사별 공식 API를 더 직접 구현하면 외부 resolver 의존도를 점차 줄일 수 있습니다.
