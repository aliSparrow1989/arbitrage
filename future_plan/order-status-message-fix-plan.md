# اصلاح پیام تلگرامِ نتایج اجرا (n8n Code node)

## Context (چرا این تغییر)

پیامِ تلگرامیِ تولیدشده توسط Code node در n8n وضعیت سفارش‌ها را **اشتباه** گزارش می‌کند. در نمونه‌ی واقعی ([arbitrage_main/execute.txt](../arbitrage_main/execute.txt)) پیام «✅ خرید شد» را نشان می‌داد در حالی که در واقعیت سفارش فقط **ثبت** شده بود و هیچ معامله‌ای نشده بود (`order.status: "Active"`, `matchedAmount: "0"`, `dealed_base_amount: "0.00"`).

علت ریشه‌ای: همه‌ی سفارش‌ها **limit order** هستند و آنی پر نمی‌شوند؛ پاسخی که `execute()` همان لحظه برمی‌گرداند فقط یک snapshot لحظه‌ی ثبت است. کُد فعلی دو سیگنالِ غلط را به‌جای fill استفاده می‌کرد:

- `buy.status === 'ok'` → این یعنی «درخواست API قبول شد»، نه fill. تقریباً همیشه true.
- `order.status === 'Filled'` → nobitex این مقدار را ندارد (مقدارش `'Done'` است) → کد مُرده.
- `sell.state === 'done'` → bitpin این مقدار را ندارد (مقادیر واقعی: `active` / `closed` / `initial` / `cancelled`، تأییدشده از دو SDK مستقل) → کد مُرده.
- برچسب «معامله‌شده» روی `amount` غلط بود؛ `amount` حجمِ **سفارش‌داده‌شده** است نه حجمِ پرشده.

**تصمیم کاربر:** پیام فقط بگوید سفارش‌ها **«ثبت شد»** (نه «انجام شد»)، و **به هسته (پایتون/engine) دست نزنیم**. این بدون تغییر هسته ممکن است، چون خروجی `execute()` فیلدهای عمومیِ `executed` / `error` / `buy` / `sell` / `dry_run` را دارد که برای هر ۵ صرافی یکسان‌اند.

## نتیجه‌ی مطلوب

پیام صادق باشد: «هر دو سفارش ثبت شد (در انتظار پر شدن)» / «ثبت ناموفق» / «یک سمت ثبت شد، سمت دیگر نه (ریسک باز)»، و برچسب‌های نقدینگی/حجم درست باشند. هیچ ادعای دروغینِ «انجام شد».

## شکلِ پاسخِ هر ۵ صرافی (تأییدشده)

helperهای ریپو (`_post_json`/`_get_json` در [base.py:258](../arbitrage_main/base.py#L258)) پاسخ را خام برمی‌گردانند (`return await resp.json()`) — پس پاکتِ کاملِ هر صرافی به n8n می‌رسد. شکلِ پاکت از خودِ کدِ پارسِ ریپو استخراج شد:

| صرافی | پاکتِ موفقیتِ ثبت سفارش | منبع تأیید |
|---|---|---|
| nobitex | `status === 'ok'` (و `order` موجود) | [execute.txt](../arbitrage_main/execute.txt) |
| bitpin | وجود `.id` (بدون پاکت، آبجکت سفارش مستقیم) | [execute.txt](../arbitrage_main/execute.txt) |
| wallex | `success === true` (آبجکت زیر `.result`) | [wallex.py:26](../arbitrage_main/wallex.py#L26) |
| ompfinex | `status === 'OK'` (آبجکت زیر `.data`) | [ompfinex.py:40](../arbitrage_main/ompfinex.py#L40) |
| ramzinex | `status === 0` عددی (آبجکت زیر `.data`) | [ramzinex.py:45,59](../arbitrage_main/ramzinex.py#L45) |

## تغییر (فقط Code node در n8n — هیچ فایلی در ریپو عوض نمی‌شود)

کلِ کد node با نسخه‌ی زیر جایگزین شود:

```js
// n8n Code node — Mode: Run Once for All Items (JavaScript)

const nf = (n) => Number(n || 0).toLocaleString('en-US', { maximumFractionDigits: 2 });

// آیا این سمت (خرید/فروش) واقعاً ثبت شد؟ بر اساس پاکتِ موفقیتِ هر ۵ صرافی
const placed = (side) => {
  if (!side || typeof side !== 'object') return false;
  if (side.status === 'ok')  return true;  // nobitex
  if (side.status === 'OK')  return true;  // ompfinex
  if (side.status === 0)     return true;  // ramzinex (عددی)
  if (side.success === true) return true;  // wallex
  if (side.id != null)       return true;  // bitpin (آبجکت سفارش مستقیم)
  return false;                            // در غیر این صورت ثبت‌نشده/خطا
};

const out = [];

for (const item of $input.all()) {
  const data = item.json;
  const runs = Array.isArray(data) ? data : [data];

  for (const run of runs) {
    for (const ex of run.executions || []) {
      const o = ex.opportunity || {};
      const r = ex.result || {};

      const err = r.error ? ' — ' + r.error : '';
      const buyPlaced  = placed(r.buy);
      const sellPlaced = placed(r.sell);

      let statusLine;
      if (r.dry_run) {
        statusLine = '🧪 حالت آزمایشی (dry run) — سفارشی ثبت نشد';
      } else if (buyPlaced && sellPlaced) {
        statusLine = '✅ هر دو سفارش ثبت شد (در انتظار پر شدن)';
      } else if (buyPlaced && !sellPlaced) {
        statusLine = '⚠️ خرید ثبت شد، فروش ثبت نشد (ریسک باز)' + err;
      } else if (!buyPlaced && sellPlaced) {
        statusLine = '⚠️ فروش ثبت شد، خرید ثبت نشد' + err;
      } else {
        statusLine = '❌ هیچ سفارشی ثبت نشد' + err;
      }

      const lines = [
        `🔄 ${o.pair}`,
        statusLine,
        `🟢 خرید از ${o.buy_from}: ${nf(o.buy_price)}`,
        `🔴 فروش به ${o.sell_to}: ${nf(o.sell_price)}`,
        `💰 سود تخمینی: ${nf(o.net_usdt)} $ | ${nf(Math.round(o.net_irt))} ریال` +
          (o.net_pct != null ? ` (${nf(o.net_pct)}%)` : ''),
        `📊 نقدینگی در بهترین قیمت: ${nf(o.ask_vol)} | حجم سفارش: ${nf(o.amount)}`,
      ];

      out.push({ json: { pair: o.pair, text: lines.join('\n') } });
    }
  }
}

return [
  {
    json: {
      text: out.map((i) => i.json.text).join('\n\n➖➖➖➖➖\n\n'),
      count: out.length,
    },
  },
];
```

### تفاوت‌های کلیدی با نسخه‌ی فعلی
- وضعیت بر اساس **«ثبت شد»** (placement) محاسبه می‌شود، نه fill — صادق و برای هر ۵ صرافی کار می‌کند.
- `📊 موجود/معامله‌شده` → `📊 نقدینگی در بهترین قیمت / حجم سفارش` (برچسب درست).
- `💰 سود` → `💰 سود تخمینی` (چون سفارش هنوز پر نشده، سود قطعی نیست).

## محدودیتِ شناخته‌شده (صادقانه)
- `placed()` فقط **«ثبت موفق»** را تشخیص می‌دهد، نه **fill**؛ این دقیقاً همان چیزی است که کاربر خواست (پیام بگوید «ثبت شد»).
- پاکتِ nobitex/bitpin/wallex از داده‌ی واقعی/کدِ ریپو تأیید شده. پاکتِ **ompfinex (`status:'OK'`)** و **ramzinex (`status:0`)** از کدِ پارسِ ریپو و SDKهای ثانویه استنتاج شده، نه از یک نمونه‌ی JSONِ ثبتِ سفارش. اگر بعداً یک نمونه‌ی واقعی از این دو صرافی دیدیم، باید `placed()` را با آن صحت‌سنجی کنیم.
- اگر بخواهیم بعداً **fill واقعی** را هم نشان دهیم (نه فقط ثبت)، فیلدهای پرشده این‌ها هستند: nobitex `order.matchedAmount`/`order.status==='Done'`، bitpin `dealed_base_amount`/`state==='closed'`، wallex `result.executedQty`/`result.status==='FILLED'`. راهِ مقاوم‌ترش افزودن فیلد یکدستِ `buy_filled/sell_filled` در `execute()` پایتون است (که الان طبق خواست کاربر انجام نمی‌دهیم).

## Verification (چطور تست کنیم)
1. کد بالا را در همان Code node در n8n paste کن.
2. محتویات یکی از executionها از [arbitrage_main/execute.txt](../arbitrage_main/execute.txt) را به‌عنوان ورودی بده (یا workflow را روی یک run واقعی اجرا کن).
3. انتظار: برای نمونه‌ی `execute.txt` (nobitex `status:ok` + bitpin `id` موجود) پیام باید **«✅ هر دو سفارش ثبت شد (در انتظار پر شدن)»** باشد — نه «خرید شد».
4. یک حالت خطا را هم تست کن: ورودی‌ای که `result.executed=false` و `result.error` دارد → باید پیام «❌ هیچ سفارشی ثبت نشد — <error>» یا حالت «ریسک باز» را نشان دهد.
5. اگر به یک جفتِ شاملِ wallex/ompfinex/ramzinex رسیدی، چک کن `placed()` آن را درست «ثبت‌شده» تشخیص دهد (به‌ترتیب `success:true` / `status:'OK'` / `status:0`).
6. چک کن خط آخر بشود `📊 نقدینگی در بهترین قیمت: ... | حجم سفارش: ...`.
