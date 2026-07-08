#!/usr/bin/env bash
# test_bitpin_nobitex.sh
# Read-only connectivity + auth tests for Bitpin and Nobitex.
# Does NOT place any order. Safe to run.
#
# Usage:  bash test_bitpin_nobitex.sh
# Needs:  curl, python3  (nobitex auth needs: pip install pynacl)

set -u

# ============================================================================
# FILL THESE IN BEFORE RUNNING  (leave empty to skip that exchange's tests)
# ============================================================================
BITPIN_API_KEY=""
BITPIN_SECRET_KEY=""
NOBITEX_API_KEY=""
NOBITEX_SECRET_KEY=""
OMPFINEX_API_KEY=""
WALLEX_API_KEY=""
RAMZINEX_API_KEY=""
RAMZINEX_SECRET_KEY=""
# ============================================================================

line() { printf '\n========== %s ==========\n' "$1"; }

# 0) outbound IP (useful for whitelisting / geo-block diagnosis)
line "SERVER OUTBOUND IP"
curl -s -m 10 https://api.ipify.org; echo

# ---------------------------------------------------------------------------
# BITPIN
# ---------------------------------------------------------------------------
line "BITPIN  public orderbook (USDT_IRT)"
curl -s -m 15 -w "\n[HTTP %{http_code}  %{time_total}s]\n" \
  https://api.bitpin.ir/api/v1/mth/orderbook/USDT_IRT/ | head -c 400
echo

if [ -z "$BITPIN_API_KEY" ] || [ -z "$BITPIN_SECRET_KEY" ]; then
  line "BITPIN  auth -- SKIPPED (fill BITPIN_API_KEY / BITPIN_SECRET_KEY at top)"
else
line "BITPIN  authenticate -> access token"
BITPIN_AUTH=$(curl -s -m 20 https://api.bitpin.ir/api/v1/usr/authenticate/ \
  -H "Content-Type: application/json" \
  -d "{\"api_key\":\"$BITPIN_API_KEY\",\"secret_key\":\"$BITPIN_SECRET_KEY\"}")
ACCESS=$(printf '%s' "$BITPIN_AUTH" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('access',''))
except Exception: print('')")
if [ -z "$ACCESS" ]; then
  echo "!! no access token. raw authenticate response:"
  printf '%s\n' "$BITPIN_AUTH" | head -c 500; echo
else
  echo "access token OK: ${ACCESS:0:20}..."
  line "BITPIN  wallets (auth) -- non-zero balances only"
  curl -s -m 20 https://api.bitpin.ir/api/v1/wlt/wallets/ \
    -H "Authorization: Bearer $ACCESS" \
  | python3 -c "import sys,json
try:
    rows=json.load(sys.stdin)
except Exception as e:
    print('!! could not parse wallets response:',e); raise SystemExit(0)
if not isinstance(rows,list):
    print('!! unexpected response:',str(rows)[:500]); raise SystemExit(0)
def f(x):
    try: return float(x)
    except Exception: return 0.0
nz=[r for r in rows if f(r.get('balance'))!=0 or f(r.get('frozen'))!=0]
print('total wallets:',len(rows),' non-zero:',len(nz))
for r in nz:
    print('  %-8s balance=%s frozen=%s service=%s' % (r.get('asset'), r.get('balance'), r.get('frozen'), r.get('service')))"
  echo
fi
fi

# ---------------------------------------------------------------------------
# NOBITEX
# ---------------------------------------------------------------------------
line "NOBITEX  public orderbook (USDTIRT)"
curl -s -m 15 -A "TraderBot/arbitrage" -w "\n[HTTP %{http_code}  %{time_total}s]\n" \
  https://apiv2.nobitex.ir/v3/orderbook/USDTIRT | head -c 400
echo

if [ -z "$NOBITEX_API_KEY" ] || [ -z "$NOBITEX_SECRET_KEY" ]; then
  line "NOBITEX  auth -- SKIPPED (fill NOBITEX_API_KEY / NOBITEX_SECRET_KEY at top)"
else
line "NOBITEX  wallets/list (Ed25519 auth)"
NOBITEX_API_KEY="$NOBITEX_API_KEY" NOBITEX_SECRET_KEY="$NOBITEX_SECRET_KEY" python3 - <<'PY'
import base64, os, time, urllib.request, urllib.error
try:
    import nacl.signing
except ImportError:
    print("!! pynacl not installed. run: pip install pynacl")
    raise SystemExit(0)

API_KEY    = os.environ["NOBITEX_API_KEY"]
SECRET_KEY = os.environ["NOBITEX_SECRET_KEY"]
BASE, PATH, METHOD, BODY = "https://apiv2.nobitex.ir", "/users/wallets/list", "POST", "{}"

sk  = nacl.signing.SigningKey(base64.urlsafe_b64decode(SECRET_KEY))
ts  = str(int(time.time()))
sig = base64.b64encode(sk.sign((ts+METHOD+PATH+BODY).encode()).signature).decode()

req = urllib.request.Request(BASE+PATH, data=BODY.encode(), method=METHOD, headers={
    "Nobitex-Key": API_KEY, "Nobitex-Signature": sig, "Nobitex-Timestamp": ts,
    "User-Agent": "TraderBot/arbitrage", "Content-Type": "application/json"})
try:
    print("HTTP 200")
    print(urllib.request.urlopen(req, timeout=15).read().decode()[:800])
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:800])
except Exception as e:
    print("ERROR:", e)
PY
fi

# ---------------------------------------------------------------------------
# OMPFINEX   (Bearer api_key; one call returns ALL markets)
# ---------------------------------------------------------------------------
line "OMPFINEX  public orderbook (all markets)"
curl -s -m 15 -w "\n[HTTP %{http_code}  %{time_total}s]\n" \
  https://api.ompfinex.com/v1/orderbook | head -c 400
echo

if [ -z "$OMPFINEX_API_KEY" ]; then
  line "OMPFINEX  auth -- SKIPPED (fill OMPFINEX_API_KEY at top)"
else
  line "OMPFINEX  wallet (auth) -- non-zero balances only"
  curl -s -m 20 https://api.ompfinex.com/v1/user/wallet \
    -H "Authorization: Bearer $OMPFINEX_API_KEY" \
  | python3 -c "import sys,json
try:
    raw=json.load(sys.stdin)
except Exception as e:
    print('!! could not parse wallet response:',e); raise SystemExit(0)
rows=raw.get('data') if isinstance(raw,dict) else raw
if not isinstance(rows,list):
    print('!! unexpected response:',str(raw)[:500]); raise SystemExit(0)
def f(x):
    try: return float(x)
    except Exception: return 0.0
nz=[r for r in rows if f(r.get('balance'))!=0 or f(r.get('blocked_balance'))!=0]
print('total assets:',len(rows),' non-zero:',len(nz))
for r in nz:
    cur=r.get('currency'); asset=cur.get('id') if isinstance(cur,dict) else cur
    print('  %-8s balance=%s blocked=%s' % (asset, r.get('balance'), r.get('blocked_balance')))"
  echo
fi

# ---------------------------------------------------------------------------
# WALLEX   (X-API-Key header)
# ---------------------------------------------------------------------------
line "WALLEX  public depth (USDTTMN)"
curl -s -m 15 -w "\n[HTTP %{http_code}  %{time_total}s]\n" \
  "https://api.wallex.ir/v1/depth?symbol=USDTTMN" | head -c 400
echo

if [ -z "$WALLEX_API_KEY" ]; then
  line "WALLEX  auth -- SKIPPED (fill WALLEX_API_KEY at top)"
else
  line "WALLEX  balances (auth) -- non-zero only"
  curl -s -m 20 https://api.wallex.ir/v1/account/balances \
    -H "X-API-Key: $WALLEX_API_KEY" \
  | python3 -c "import sys,json
try:
    raw=json.load(sys.stdin)
except Exception as e:
    print('!! could not parse balances response:',e); raise SystemExit(0)
bal=(raw.get('result') or {}).get('balances') if isinstance(raw,dict) else None
if not isinstance(bal,dict):
    print('!! unexpected response:',str(raw)[:500]); raise SystemExit(0)
def f(x):
    try: return float(x)
    except Exception: return 0.0
nz={k:v for k,v in bal.items() if f(v.get('value'))!=0 or f(v.get('locked'))!=0}
print('total assets:',len(bal),' non-zero:',len(nz))
for k,v in nz.items():
    print('  %-8s value=%s locked=%s' % (k, v.get('value'), v.get('locked')))"
  echo
fi

# ---------------------------------------------------------------------------
# RAMZINEX   (getToken -> x-api-key + Authorization2: Bearer)
# ---------------------------------------------------------------------------
line "RAMZINEX  public orderbook (pair_id 11 = USDT/IRR)"
curl -s -m 15 -w "\n[HTTP %{http_code}  %{time_total}s]\n" \
  https://publicapi.ramzinex.com/exchange/api/v1.0/exchange/orderbooks/11/buys_sells | head -c 400
echo

if [ -z "$RAMZINEX_API_KEY" ] || [ -z "$RAMZINEX_SECRET_KEY" ]; then
  line "RAMZINEX  auth -- SKIPPED (fill RAMZINEX_API_KEY / RAMZINEX_SECRET_KEY at top)"
else
  line "RAMZINEX  getToken"
  RZ_AUTH=$(curl -s -m 20 https://api.ramzinex.com/exchange/api/v1.0/exchange/auth/api_key/getToken \
    -H "Content-Type: application/json" \
    -d "{\"api_key\":\"$RAMZINEX_API_KEY\",\"secret\":\"$RAMZINEX_SECRET_KEY\"}")
  RZ_TOKEN=$(printf '%s' "$RZ_AUTH" | python3 -c "import sys,json
try: print(json.load(sys.stdin)['data']['token'])
except Exception: print('')")
  if [ -z "$RZ_TOKEN" ]; then
    echo "!! no token. raw getToken response:"
    printf '%s\n' "$RZ_AUTH" | head -c 500; echo
  else
    echo "token OK: ${RZ_TOKEN:0:20}..."
    line "RAMZINEX  funds summary (auth) -- non-zero only"
    curl -s -m 20 https://api.ramzinex.com/exchange/api/v1.0/exchange/users/me/funds/summaryDesktop \
      -H "x-api-key: $RAMZINEX_API_KEY" -H "Authorization2: Bearer $RZ_TOKEN" \
    | python3 -c "import sys,json
try:
    raw=json.load(sys.stdin)
except Exception as e:
    print('!! could not parse funds response:',e); raise SystemExit(0)
rows=raw.get('data') if isinstance(raw,dict) else raw
if not isinstance(rows,list):
    print('!! unexpected response:',str(raw)[:500]); raise SystemExit(0)
def f(x):
    try: return float(x)
    except Exception: return 0.0
nz=[r for r in rows if f(r.get('total_nr'))!=0 or f(r.get('in_order_nr'))!=0]
print('total assets:',len(rows),' non-zero:',len(nz))
for r in nz:
    print('  currency_id=%-4s total=%s in_order=%s' % (r.get('currency_id'), r.get('total_nr'), r.get('in_order_nr')))"
    echo
  fi
fi

line "DONE"
