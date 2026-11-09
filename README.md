# Stockbucks Payment Engine

카드 탭 신호를 받아 **승인 → 가맹점 원화 송금 → 지갑 코인 매도(v5 알고리즘) → USDC/USDT 회사 Treasury 회수 → 차액 반환 → 잠금 해제**까지 처리하는 백엔드입니다. 구독 최적화(동의·사용량 수집·λ 반복·질문)와 실시간 시세·매크로·LLM 뉴스 점수도 함께 들어 있습니다.

```
POS Backend ──POST /v1/payments/authorize──▶ 승인(동기, p95 < 5ms in sim)
                                            ├─▶ 가맹점 원화 송금 (bank: sim | 오픈뱅킹)
                                            └─▶ 청산: 점수순 매도 → 0x 스왑 → Treasury 송금 → 잠금 해제
```

## 실행

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000          # 기본 SB_MODE=sim (체인·DEX·은행·시세 시뮬레이션)
python -m pytest -q                       # 36 tests
```
API 문서: `http://localhost:8000/docs`

## 팀원 연동 계약

### 신호 담당 (POS Backend → 엔진)
`POST /v1/payments/authorize` · 헤더 `X-SB-Signature: hex(HMAC-SHA256(SB_POS_HMAC_SECRET, raw body))` (`SB_REQUIRE_HMAC=1`일 때)
```json
{"checkout_id": "co_123", "amount_krw": 4500, "merchant_id": "cafe", "card_token": "NFC UID 해시"}
```
응답 `{"payment_id", "status": "AUTHORIZED|DECLINED", "reason": null|"LIMIT_EXCEEDED"|"WALLET_BUSY"|"UNKNOWN_WALLET"|"PRICE_UNAVAILABLE", "latency_ms", "idempotent_replay"}`
- `checkout_id`가 멱등 키입니다. 같은 값으로 다시 보내면 첫 결과를 그대로 돌려줍니다.
- POS 화면용 조회: `GET /v1/payments/{checkout_id}/pos`. 승인 결과만 담기고 청산 상태는 들어가지 않습니다.

### UI 담당
| 화면 | API |
| --- | --- |
| 가입 (매도 위임·잠금 안내 동의, 결제 코인, 성향) | `POST /v1/users`, `POST /v1/users/{id}/wallets`, `POST /v1/cards` |
| 지갑 잔고·잠금 상태·오늘 매도 가능량 | `GET /v1/wallets/{id}` |
| 출금 (잠금 중이면 409) | `POST /v1/wallets/{id}/withdraw` |
| 결제 전 매도 미리보기 | `GET /v1/market/sell-preview?wallet_id=&amount_krw=` |
| Ops: 4개 상태 축·스왑 내역·이벤트 | `GET /ops/payments`, `GET /ops/payments/{id}`, `GET /ops/alerts`, `GET /ops/decisions` |
| 동의 1~3단계 | `POST /v1/users/{id}/consents` |
| 사용량: iOS 스크린타임 집계 / 넷플릭스 CSV / 유튜브 테이크아웃 / Spotify·Google 토큰 / 저장공간 / 혜택 | `POST /v1/users/{id}/usage/...` |
| 구독 최적화 실행·질문·응답 | `POST .../subscriptions/optimize`, `GET .../questions`, `POST /v1/questions/{id}/answer` |

## 구조
| 경로 | 내용 |
| --- | --- |
| `app/services/authorization.py` | 한도 확인(H = A×1.03 vs 지갑 총합), WALLET_BUSY, CAS 지갑 잠금, 멱등 |
| `app/services/liquidation.py` | 매도 계획 → 스왑(재시도 2·최대 3건·슬리피지 0.5%·대체 코인) → Treasury → 차액 반환 → TTL |
| `app/services/payout.py`, `app/bank.py` | 가맹점 원화 송금 (결제당 1회, 재시도, 실패 시 critical 알림) |
| `app/algo/sell.py` | v5 매도 점수: 코인 MC = μ+c+τ+x/D−γRC, 스테이블 MC = μ+c+x/D+ψ·1[U<L*], 1,000원 그리디, 30% 상한 |
| `app/algo/subscription.py` | N = V − (1+λ)P/m − κ, 유형별 V, 질문 조건, λ 고정점 반복, 반복 결제 감지 |
| `app/market/` | 시세(Binance WS·Kraken 페그·환율), 매크로(FRED·DefiLlama·Deribit·ETF·펀딩비), 추세 5단계, LLM 뉴스 + 검증기 |
| `app/chain/` | `sim` 장부·AMM / `evm` RPC + 0x v2 + 임베디드 지갑(Privy 서버 지갑 / 로컬 키) |

## live 전환 체크리스트 (sim에서 live로)
- [ ] `SB_MODE=live`, `SB_RPC_URL`, `SB_TREASURY_ADDRESS`, `SB_ZEROEX_API_KEY`, `SB_WALLET_PROVIDER=privy` + Privy 키
- [ ] `app/assets.py` 토큰 주소를 공식 문서로 재확인 (Base 기준)
- [ ] Privy 요청 서명 형식, 0x v2 응답 필드 재확인 (`chain/evm.py`)
- [ ] 오픈뱅킹 이용기관 계약 + 필드 명세 확인 (`bank.py`) 또는 PG 정산 어댑터로 교체
- [ ] `SB_FRED_API_KEY`, `SB_SOSOVALUE_API_KEY`, `SB_ANTHROPIC_API_KEY`
- [ ] 운영 DB는 Postgres (`SB_DATABASE_URL`), 엔진 다중 인스턴스면 청산 워커를 큐(예: Redis)로 분리
- [ ] 구독 카탈로그 요금·v_s·σ_s·q는 가정값 → 실제 값으로
- [ ] 자동 매도의 투자일임 해당 여부, 트래블룰·외환 → 변호사 검토
