# Plan: سرویس `arbitrage_orders` — شناسایی و اصلاح سفارش‌های بازِ گیرافتاده

## Context (چرا؟)

در آربیتراژ، معامله در دو پا انجام می‌شه (خرید روی یک صرافی، فروش روی دیگری).
گاهی پای فروش (یا خرید) پر نمی‌شه چون در حد میلی‌ثانیه یک نفر زودتر سفارشِ طرف
مقابل را برداشته. نتیجه: یک **سفارش بازِ گیرافتاده** که روی order book مونده و پر
نشده، و سود آربیتراژ از بین می‌ره.

این تغییر دو قابلیت اضافه می‌کند:
1. **شناسایی**: گرفتن سفارش‌های بازِ همه‌ی صرافی‌ها و فیلتر آن‌هایی که **بیشتر از N
   ثانیه** پیش ثبت شده‌اند (یعنی گیر کرده‌اند).
2. **اصلاح**: کنسل کردن آن سفارش‌ها و ثبت مجددشان با قیمتِ **تهاجمی‌تر** —
   درصدی (از کانفیگ) نسبت به **قیمتِ قبلیِ خودِ سفارش** (بدون هیچ نگاهی به order
   book یا قیمت بازار).

### تصمیم‌های تأییدشده با کاربر
- یک **میکروسرویس جدید `arbitrage_orders/`** (آینه‌ی `arbitrage_wallet/`)، نه افزودن به `arbitrage_main` (مسیر داغ `/run` دست‌نخورده بماند).
- **`arbitrage_main/` و `arbitrage_wallet/` اصلاً تغییر نمی‌کنند.** متدهای جدید فقط روی کپیِ client ها داخل `arbitrage_orders/` اضافه می‌شوند. تنها فایل‌های موجودی که لمس می‌شوند `docker-compose.yaml` و `docker-compose.override.yaml` هستند (صرفاً افزودن بلوک سرویس جدید).
- دو endpoint: `POST /open-orders` (خواندن) و `POST /reprice-stale-orders` (اقدامِ ترکیبی: یافتن+کنسل+ثبت مجدد در یک فراخوانی).
- قیمت جدید = `old_price × (1 ± pct/100)` — **بدون** lookup قیمت بازار.
  - سفارش فروش → قیمت pct% **پایین‌تر** (`1 − pct/100`).
  - سفارش خرید → قیمت pct% **بالاتر** (`1 + pct/100`).
- ثبت مجدد فقط روی **مقدار باقیمانده‌ی پر‌نشده** (unmatched).
- `min_age_seconds` و `reprice_percent` از body خوانده می‌شوند (n8n همیشه body می‌فرستد).

## endpointهای هر صرافی (همه از مستندات رسمی، قطعی)

| صرافی | سفارش‌های باز | کنسل سفارش | ثبت مجدد (موجود) | فیلد زمان |
|-------|----------------|------------|------------------|-----------|
| Nobitex | `POST /market/orders/list` `{"status":"open"}` | `POST /market/orders/update-status` `{"order":id,"status":"canceled"}` | `place_order(type,src,dst,amount,price)` | `created_at` (ISO) |
| Bitpin | `GET /api/v1/odr/orders/?state=active` | `DELETE /api/v1/odr/orders/{id}/` | `place_order(symbol,side,base_amount,price)` | `created_at` (ISO) |
| Wallex | `GET /v1/account/openOrders` | `DELETE /v1/account/orders` `{"clientOrderId":...}` | `place_order(symbol,side,quantity,price)` | `created_at` |
| OmpFinex | `GET /v1/user/order` | `DELETE /v1/user/order/{id}` | `place_order(market_id,side,quantity,price)` | فیلد created (probe) |
| Ramzinex | `POST /users/me/orders3` `{"states":1}` | `POST /users/me/orders/{id}/cancel` | `place_order(pair_id,side,amount,price)` | `created_at_ms` (epoch ثانیه) |

نکته: هیچ صرافی‌ای «ویرایش سفارش» ندارد؛ الگوی جهانی = cancel + re-place. auth لازم
برای هر ۵ صرافی همین الان در client ها پیاده است (`_signed_post`/`_auth_headers`/`_auth`).
دو نقطه‌ی نیازمندِ تأیید با API واقعی حین پیاده‌سازی: مقدار دقیق `status` نوبیتکس
(`"open"`/`"Active"`) و پارامترهای لیست OmpFinex — هر دو با fallback در نرمال‌سازی
پوشش داده می‌شوند.

## ساختار سرویس جدید (آینه‌ی `arbitrage_wallet/`)

پوشه‌ی جدید `arbitrage_orders/` شامل:

### ۱) کپی فایل‌های پایه
کپیِ بی‌تغییرِ `base.py` و ۵ client (`nobitex.py`, `bitpin.py`, `ompfinex.py`,
`wallex.py`, `ramzinex.py`) از `arbitrage_main/` — این فایل‌ها بین main/wallet
هم‌اکنون کپیِ یکسان‌اند، همان الگو ادامه پیدا می‌کند.

### ۲) افزودن دو متد به هر ۵ client
به هر client (در کپیِ `arbitrage_orders/`) اضافه می‌شود:

- `async def get_open_orders(self)` → لیست خام سفارش‌های باز:
  - Nobitex: `await self._signed_post("/market/orders/list", {"status": "open"})`
  - Bitpin: auth لازم (مثل `get_wallets`) سپس `_get_json(BASE+"/api/v1/odr/orders/", params={"state":"active"}, headers=self._auth_headers())`
  - Wallex: `_get_json(BASE+"/v1/account/openOrders", headers=self._auth())`
  - OmpFinex: `_get_json(BASE+"/v1/user/order", headers=self._auth())`
  - Ramzinex: auth لازم سپس `_post_json(BASE_PRIVATE+"/users/me/orders3", {"states":1}, headers=self._auth_headers())` با retry روی 401 (مثل `get_wallets`)

- `async def cancel_order(self, order_id)` → کنسل (طبق جدول بالا؛ همان الگوی
  auth/retx هر client). برای Wallex شناسه `clientOrderId` است.

- یک پیش‌فرضِ `NotImplementedError` برای هر دو متد در `base.py` (مثل بقیه‌ی interface).

### ۳) `order_engine.py` (آینه‌ی `wallet_engine.py`)
- همان `DEFAULT_CONFIG`+`keys`, `build_clients`, `deep_merge`, `attach_session`,
  و حلقه‌ی `asyncio.gather`. API keys فقط از body؛ ذخیره/لاگ نمی‌شوند.
- config افزوده: `min_age_seconds` (پیش‌فرض 0)، `reprice_percent` (پیش‌فرض 0)،
  `max_orders` (سقفِ اختیاریِ تعداد سفارشِ پردازش‌شده در هر اجرا؛ **پیش‌فرض 0 = بدون سقف**)،
  `exchanges` (whitelist مثل wallet).
- `normalize_orders(label, raw) -> list[dict]`: شکل خام هر صرافی را به یک رکورد
  واحد تبدیل می‌کند، با همه‌ی فیلدهای لازم برای **بازساختِ** place_order همان صرافی:
  ```
  {exchange, id, symbol, side, price, amount, remaining,
   created_epoch, age_seconds, _place_args}
  ```
  - id: `id`/`order_id`/`clientOrderId`
  - side: `side`/`type`/`isBuy`→buy/sell
  - price: قیمتِ بومیِ خودِ سفارش (بدون price_scale)
  - remaining: مقدار پر‌نشده — Nobitex `unmatchedAmount`، Ramzinex `amount_nr−filled_nr`،
    Wallex `origQty−executedQty`، Bitpin/OmpFinex فیلد باقیمانده (probe؛ fallback به کل مقدار)
  - created_epoch: `created_at_ms`(Ramzinex، ثانیه)/`created_at`/`createdAt`/`timestamp`؛
    پارس هم رشته‌ی ISO و هم عدد epoch (تشخیص ms با طول رقم)
  - `_place_args`: شناسه‌های موردنیازِ place_order همان صرافی (src/dst یا symbol یا
    market_id یا pair_id) که از همان raw استخراج می‌شود
  - defensive، مثل `normalize_wallet_rows`/`_pick` در `base.py`.
- `_filter_by_age(orders, min_age_seconds)`: `age = now − created_epoch`، نگه‌داشتن `age_seconds ≥ min_age_seconds`.
- `compute_new_price(side, old_price, pct)`: فروش `old_price×(1−pct/100)`، خرید `old_price×(1+pct/100)`؛ گرد کردن به دقتِ بومیِ صرافی (مثل `place_order` فعلی که `int(price)` می‌زند).
- دو تابع سطح بالا:
  - `run_list(cfg)` → فقط فن‌اوت + نرمال + فیلتر سن. خروجی per-exchange مثل wallet:
    ```json
    {"timestamp":..., "exchanges":[...],
     "orders": {"nobitex": {"open_orders":[...], "count":N}, ...},
     "errors":[...]}
    ```
  - `run_reprice(cfg)` → `run_list` را صدا می‌زند؛ برای هر سفارشِ گیرافتاده (اگر
    `max_orders > 0` فقط تا آن تعداد، وگرنه همه): `cancel_order(id)` → اگر موفق،
    `place_order(... new_price, remaining ...)`؛
    جمع‌آوری نتیجه:
    ```json
    {"timestamp":..., "processed":N,
     "actions":[{"exchange","id","side","old_price","new_price","remaining",
                 "canceled":bool,"replaced":bool,"new_order":..., "error":null}],
     "errors":[...]}
    ```
    اگر کنسل ناموفق بود، ثبت مجدد انجام **نمی‌شود** (جلوگیری از دوبل‌سفارش) و در `error` ثبت می‌شود.

### ۴) `main.py` (آینه‌ی `arbitrage_wallet/main.py`)
همان `_parse_body`/`_read_body`، با routeها:
- `POST /open-orders` → `run_list(cfg)`
- `POST /reprice-stale-orders` → `run_reprice(cfg)`
- `POST /preview-config` و `GET /health`

### ۵) فایل‌های scaffolding
`Dockerfile`, `requirements.txt`, `config.sample.json`, `.dockerignore`, `.gitignore`:
کپی از `arbitrage_wallet/` (config.sample فیلدهای `min_age_seconds`/`reprice_percent`/
`max_orders` را بازتاب دهد و `include_zero`/`assets` را حذف کند).

## اتصال به compose

در `docker-compose.yaml` (کنار `arbitrage_wallet`):
```yaml
  arbitrage_orders:
    build: ./arbitrage_orders
    container_name: arbitrage_orders
    restart: unless-stopped
    expose:
      - "8000"
    networks:
      - internal
```
در `docker-compose.override.yaml` برای دیباگ لوکال:
```yaml
  arbitrage_orders:
    ports:
      - "8002:8000"
```

## فایل‌های مرجع (الگو/کپی)
- الگوی سرویس: `arbitrage_wallet/wallet_engine.py`, `arbitrage_wallet/main.py`
- نرمال‌سازی defensive: `normalize_wallet_rows`/`_pick` در `arbitrage_main/base.py`
- client ها و الگوی auth/retry/place_order: `arbitrage_main/ramzinex.py`, `arbitrage_main/bitpin.py`, `arbitrage_main/nobitex.py`, `arbitrage_main/wallex.py`, `arbitrage_main/ompfinex.py`

## Verification (تست end-to-end)

1. **واحد، بدون شبکه** (اجرای محلی `order_engine.py` با stdin، مثل wallet):
   - `normalize_orders` با نمونه‌ی پاسخ هر صرافی (به‌خصوص `created_at_ms` رمزینکس و
     ISO نوبیتکس) → صحت `age_seconds`, `remaining`, `_place_args`.
   - `compute_new_price`: فروش 1000 با pct=10 → 900؛ خرید 1000 با pct=10 → 1100.
   - `_filter_by_age` با چند سن مصنوعی و `min_age_seconds=30`.
2. **با کلید واقعی (لوکال)** — `docker compose up arbitrage_orders` (پورت 8002):
   ```bash
   curl -s localhost:8002/health
   # مرحله ۱:
   curl -s -X POST localhost:8002/open-orders \
     -H 'Content-Type: application/json' \
     -d '{"min_age_seconds":30,"keys":{...}}' | jq
   # مرحله ۲ (با احتیاط — سفارش واقعی می‌گذارد):
   curl -s -X POST localhost:8002/reprice-stale-orders \
     -H 'Content-Type: application/json' \
     -d '{"min_age_seconds":30,"reprice_percent":10,"max_orders":1,"keys":{...}}' | jq
   ```
   - همین‌جا مقدار `status` نوبیتکس و پارامتر لیست OmpFinex با پاسخ واقعی نهایی شود.
3. **سناریوی واقعی**: یک سفارش فروش دستی روی قیمتی بالاتر از بازار بگذار (پر نشود)،
   >30s صبر کن، `/reprice-stale-orders` با `max_orders:1, reprice_percent:10` بزن →
   تأیید کن سفارش قبلی کنسل شد و سفارش جدید روی قیمت 10% پایین‌تر و با مقدار باقیمانده ثبت شد.
   تکرار برای یک سفارش خرید (قیمت بالاتر).

## رفتار تکرارشونده (تأییدشده، عمدی)

سفارشِ ثبت‌مجددشده، `created_at` اش از نو صفر می‌شود؛ پس تا گذشتنِ دوباره‌ی
`min_age_seconds` «گیرافتاده» حساب نمی‌شود. اگر بعد از آن باز هم باز مانده باشد، در
فراخوانیِ بعدیِ `/reprice-stale-orders` **دوباره کنسل و pct% پایین‌تر (خرید: بالاتر)
ثبت می‌شود**. چون stateless است و فقط قیمتِ فعلیِ سفارش در دست است، اثر **مرکب**
است و قیمت پله‌پله حرکت می‌کند (مثلاً فروش با pct=10٪: 1000→900→810→…).
**هیچ کف/سقف یا سقفِ تعداد پله‌ای اعمال نمی‌شود** (تصمیم کاربر)؛ کنترلِ توقف بر عهده‌ی
n8n / اپراتور است.

## ریسک‌ها / محافظت‌ها
- **دوبل‌سفارش**: ثبت مجدد فقط پس از کنسلِ موفق. `max_orders` سقفِ هر اجرا.
- **قیمت/مقدار بومی**: قیمت از واحد بومیِ خود سفارش خوانده و همان‌جا گرد می‌شود؛ درگیر price_scale نمی‌شویم.
- **سفارش بدون باقیمانده**: اگر `remaining == 0`، رد می‌شود (چیزی برای ثبت مجدد نیست).
