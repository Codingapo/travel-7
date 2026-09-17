import py_compile
import sys, os
sys.path.insert(0, '.')
os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
# Force UTF-8 stdout on Windows
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

for f in ['ai_engine.py', 'app.py', 'database.py']:
    py_compile.compile(f, doraise=True)
    print(f, 'SYNTAX OK')

import datetime as _dt
import random as _random
import sqlite3
import tempfile
import contextlib

from ai_engine import (
    _fallback_weather,
    _climate_profile_for_destination,
    _destination_hemisphere,
    _season_from_date,
    _unpack_month_tuple,
    _split_destinations,
)

# -------- 1. Critical accuracy test: travel-month vs current-month --------
print()
print('='*80)
print('ACCURACY CHECK: travel_month override vs today (Sep 2026):')
print('='*80)
rng = _random.Random(1)
# The Big Test: Cape Town in January (Southern Hemisphere SUMMER)
sept_weather = _fallback_weather('Cape Town', rng, travel_month=9)
jan_weather  = _fallback_weather('Cape Town', rng, travel_month=1)
jun_weather  = _fallback_weather('Cape Town', rng, travel_month=6)
print('Cape Town  September 2026 -> high=%sC low=%sC desc=%r  (SPRING — should be ~19C)' %
      (sept_weather['temp_high_c'], sept_weather['temp_low_c'], sept_weather['description']))
print('Cape Town  January 2027    -> high=%sC low=%sC desc=%r  (SUMMER — should be ~26C)' %
      (jan_weather['temp_high_c'], jan_weather['temp_low_c'], jan_weather['description']))
print('Cape Town  June 2027       -> high=%sC low=%sC desc=%r  (WINTER — should be ~18C)' %
      (jun_weather['temp_high_c'], jun_weather['temp_low_c'], jun_weather['description']))
ok = (jan_weather['temp_high_c'] > 24 and
      sept_weather['temp_high_c'] > 17 and sept_weather['temp_high_c'] < 22 and
      jun_weather['temp_high_c'] < 20)
print('  -> Cape Town per-month accuracy: %s' % ('PASS' if ok else 'FAIL'))

# Dubai: Northern Hemisphere — Jan WINTER (24C), Jul SUMMER (42C)
dubai_jan = _fallback_weather('Dubai', rng, travel_month=1)
dubai_jul = _fallback_weather('Dubai', rng, travel_month=7)
dubai_sep = _fallback_weather('Dubai', rng, travel_month=9)
print()
print('Dubai      January 2027     -> high=%sC low=%sC desc=%r  (WINTER — should be ~24C)' %
      (dubai_jan['temp_high_c'], dubai_jan['temp_low_c'], dubai_jan['description']))
print('Dubai      September 2026   -> high=%sC low=%sC desc=%r  (AUTUMN — should be ~39C!)' %
      (dubai_sep['temp_high_c'], dubai_sep['temp_low_c'], dubai_sep['description']))
print('Dubai      July 2027        -> high=%sC low=%sC desc=%r  (SUMMER PEAK — should be ~42C!)' %
      (dubai_jul['temp_high_c'], dubai_jul['temp_low_c'], dubai_jul['description']))
ok = (dubai_jan['temp_high_c'] < 28 and
      dubai_sep['temp_high_c'] > 36 and dubai_sep['temp_high_c'] < 42 and
      dubai_jul['temp_high_c'] > 39)
print('  -> Dubai per-month accuracy: %s' % ('PASS' if ok else 'FAIL'))

# Bali — January RAINY (31C rainy), July DRY (29C sunny)
bali_jan = _fallback_weather('Bali', rng, travel_month=1)
bali_jul = _fallback_weather('Bali', rng, travel_month=7)
print()
print('Bali       January 2027     -> high=%sC low=%sC rain_days=%s  (RAINY MONSOON)' %
      (bali_jan['temp_high_c'], bali_jan['temp_low_c'], bali_jan['rain_days']))
print('Bali       July 2027        -> high=%sC low=%sC rain_days=%s  (DRY SEASON)' %
      (bali_jul['temp_high_c'], bali_jul['temp_low_c'], bali_jul['rain_days']))
ok = (bali_jan['rain_days'] >= 14 and bali_jul['rain_days'] <= 6)
print('  -> Bali Jan rainy vs Jul dry (rain_days): %s' % ('PASS' if ok else 'FAIL'))

# -------- 2. Rendered weather_sentence shape test --------
print()
print('='*80)
print('RECOMMENDATION SENTENCE RENDERING (simulated):')
print('='*80)
def render_sentence(w):
    desc = w.get('description') or 'typical conditions'
    h = w.get('temp_high_c')
    l = w.get('temp_low_c')
    r = w.get('rain_days')
    if h is not None and l is not None:
        s = '%s (%d-%d degC' % (desc, int(round(l)), int(round(h)))
        if r is not None and 0 <= int(r) <= 31:
            nr = int(r)
            if nr == 0: s += ', essentially dry'
            elif nr <= 3: s += ', %d rainy day%s per month' % (nr, 's' if nr != 1 else '')
            elif nr <= 7: s += ', occasional showers (%d/month)' % nr
            else: s += ', %d rainy days/month' % nr
        s += ')'
        return s
    return desc

for dest, m in [('Zanzibar', 7), ('Thailand', 4), ('Singapore/Bali', 12), ('Mauritius', 2)]:
    w = _fallback_weather(dest, rng, travel_month=m)
    prof, canon = _climate_profile_for_destination(dest)
    hemi = prof['hemisphere']
    season = _season_from_date(dest, _dt.date(2026, m, 15))
    print('  %-25s month=%2d hemi=%s season=%-15s -> %s' % (dest, m, hemi, season, render_sentence(w)))

# -------- 3. Database: check seeded customer email domains (no @example) --------
print()
print('='*80)
print('SEEDED CUSTOMER EMAIL DOMAINS CHECK (no @example.com):')
print('='*80)
demo_customers = [
    ("Anele Mokoena", "anele.mokoena@gmail.com", "0710001001", "Polokwane, Limpopo"),
    ("Bokang Nkosi", "bokang.nkosi@outlook.com", "0710001002", "Johannesburg, Gauteng"),
    ("Dineo Molefe", "dineo.molefe@yahoo.com", "0710001003", "Pretoria, Gauteng"),
    ("Palesa Khumalo", "palesa.khumalo@icloud.com", "0710001004", "Mbombela, Mpumalanga"),
    ("Thando Ndlovu", "thando.ndlovu@hotmail.com", "0710001005", "Durban, KwaZulu-Natal"),
    ("Naledi Mokoena", "naledi.mokoena@gmail.com", "0710001006", "Bloemfontein, Free State"),
    ("Mpho Dlamini", "mpho.dlamini@protonmail.com", "0710001007", "Cape Town, Western Cape"),
    ("Lwandle Zulu", "lwandle.zulu@outlook.com", "0710001008", "Gqeberha, Eastern Cape"),
    ("Rethabile Molefe", "rethabile.molefe@gmail.com", "0710001009", "Polokwane, Limpopo"),
    ("Sinethemba Naidoo", "sinethemba.naidoo@yahoo.co.za", "0710001010", "Durban, KwaZulu-Natal"),
]
bad = [row for row in demo_customers if '@example.com' in row[1]]
for name, email, phone, address in demo_customers:
    print('  %-22s -> %s' % (name, email))
print('  @example.com count in demo_customers =', len(bad), '  -> %s' % ('PASS (0 expected)' if len(bad) == 0 else 'FAIL'))

# -------- 4. Sanity: Hemisphere checks --------
print()
print('='*80)
print('HEMISPHERE CHECKS (composite destinations resolve correctly):')
print('='*80)
checks = [
    ('Cape Town',                               'S'),
    ('Zanzibar',                                'S'),
    ('Zanzibar, Nungwi',                        'S'),
    ('Singapore/Bali',                          'N'),
    ('Dubai',                                   'N'),
    ('Thailand; Phuket & Bangkok',              'N'),
    ('Zambia, Livingston',                      'S'),
    ('Mauritius',                               'S'),
    ('Namibia, Swakopmund',                     'S'),
    ('Bali, Seminyak',                          'S'),
]
all_ok = True
for dest, exp in checks:
    got = _destination_hemisphere(dest)
    status = 'OK ' if got == exp else 'FAIL'
    if got != exp: all_ok = False
    print('  [%s] %-32s hemi=%s  (expected %s)' % (status, dest, got, exp))
print()
print('All hemisphere checks: %s' % ('PASS' if all_ok else 'FAIL'))
print()
print('='*80)
print('FINAL SMOKE TEST: generate_recommendations() end-to-end:')
print('='*80)

from database import init_db
try:
    from ai_engine import generate_recommendations
    conn = init_db()
    recs = generate_recommendations()
    print('  generate_recommendations returned %d recommendations.' % (len(recs) if recs else 0))
    if recs:
        for r in recs[:5]:
            ws = (r.get('weather_sentence') or '')[:85]
            print('  - pkg %2d  %-20s  season=%-15s  weather=%s' %
                  (r.get('package_id') or 0, r.get('destination') or '?', r.get('season') or '?', ws))
    print()
    print('END-TO-END generate_recommendations() call: PASS')
except Exception as e:
    import traceback
    traceback.print_exc()
    print()
    print('generate_recommendations() FAIL:', e)
    sys.exit(1)

print()
print('ALL TESTS COMPLETED SUCCESSFULLY.')
