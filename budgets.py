"""Token and cost budgets per AI provider (Release 2.4, step 9.2).

Every answer of a provider is counted in ``ai_usage`` with the token numbers the provider reports
(no text). A budget per provider and calendar month (UTC) limits tokens and, if prices are entered,
estimated cost in euro. From 80 % the status warns; at 100 % further requests are refused before
they are sent. Prices are not built in because they change; the user enters them per million tokens.
Embeddings for the semantic search (local Ollama) are not counted.
"""
import json
from datetime import datetime, timezone

SETTING = 'budget.{}'
WARN_SHARE = 0.8
FIELDS = ('tokens', 'eur', 'price_in', 'price_out')
MAX_TOKENS = 10 ** 12
MAX_EUR = 10 ** 6


def tokens(provider, result):
    """(input, output) tokens a provider response reports; (0, 0) if it reports none."""
    if not isinstance(result, dict):
        return 0, 0
    if provider == 'openai':
        usage = result.get('usage') or {}
        pair = usage.get('input_tokens'), usage.get('output_tokens')
    elif provider == 'anthropic':
        usage = result.get('usage') or {}
        pair = usage.get('input_tokens'), usage.get('output_tokens')
    elif provider == 'gemini':
        usage = result.get('usageMetadata') or {}
        pair = (usage.get('promptTokenCount'),
                (usage.get('candidatesTokenCount') or 0) + (usage.get('thoughtsTokenCount') or 0))
    else:
        pair = result.get('prompt_eval_count'), result.get('eval_count')
    return tuple(value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
                 for value in pair)


def record(store, provider, model, result):
    """Store the token numbers of one response; nothing if the provider reports none."""
    used_in, used_out = tokens(provider, result)
    if store is None or not (used_in or used_out):
        return
    with store.connect() as db:
        db.execute('INSERT INTO ai_usage(provider,model,input_tokens,output_tokens) VALUES(?,?,?,?)',
                   (provider, model, used_in, used_out))


def limits(store, provider):
    """Budget of ``provider``: tokens and eur (0 = no limit), price_in and price_out per million tokens."""
    with store.connect() as db:
        row = db.execute('SELECT value FROM settings WHERE name=?', (SETTING.format(provider),)).fetchone()
    try:
        stored = json.loads(row['value']) if row else {}
    except ValueError:
        stored = {}
    return {field: stored.get(field, 0) if isinstance(stored, dict) else 0 for field in FIELDS}


def set_limits(store, provider, **values):
    """Change the given fields; negative, non-numeric or absurd values are rejected."""
    from settings import PROVIDERS
    if provider not in PROVIDERS:
        raise ValueError('Unbekannter KI-Anbieter.')
    current = limits(store, provider)
    for field, value in values.items():
        if field not in FIELDS:
            raise ValueError('Unbekanntes Budgetfeld.')
        if value is None:
            continue
        number = float(value)
        ceiling = MAX_TOKENS if field == 'tokens' else MAX_EUR
        if not 0 <= number <= ceiling:
            raise ValueError('Budget und Preise müssen zwischen 0 und ' + f'{ceiling:,}'.replace(',', '.') + ' liegen.')
        current[field] = int(number) if field == 'tokens' else round(number, 4)
    with store.connect() as db:
        db.execute('DELETE FROM settings WHERE name=?', (SETTING.format(provider),))
        db.execute('INSERT INTO settings(name,value) VALUES(?,?)', (SETTING.format(provider), json.dumps(current)))
    return current


def _month_start(now):
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _utc(value):
    if hasattr(value, 'astimezone'):
        return value.astimezone(timezone.utc)
    return datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)


def status(store, provider, now=None):
    """Use of the current month against the budget; ``level`` is ok, warnung or gesperrt."""
    now = now or datetime.now(timezone.utc)
    start = _month_start(now)
    used = {'anfragen': 0, 'eingabe': 0, 'ausgabe': 0}
    with store.connect() as db:
        # ponytail: scans the provider's rows and filters the month in Python, which is the same on both
        # backends; step 9.3 (retention) keeps the table small; an index on created_at is the upgrade.
        for row in db.execute('SELECT input_tokens,output_tokens,created_at FROM ai_usage WHERE provider=?',
                              (provider,)):
            if _utc(row['created_at']) >= start:
                used['anfragen'] += 1
                used['eingabe'] += int(row['input_tokens'])
                used['ausgabe'] += int(row['output_tokens'])
    limit = limits(store, provider)
    total = used['eingabe'] + used['ausgabe']
    cost = (used['eingabe'] * limit['price_in'] + used['ausgabe'] * limit['price_out']) / 1_000_000
    shares = [total / limit['tokens']] if limit['tokens'] else []
    if limit['eur'] and (limit['price_in'] or limit['price_out']):
        shares.append(cost / limit['eur'])
    share = max(shares, default=0.0)
    level = 'gesperrt' if share >= 1 else 'warnung' if share >= WARN_SHARE else 'ok'
    return {'anbieter': provider, 'monat': start.strftime('%Y-%m'), **used, 'token': total,
            'kosten_eur': round(cost, 4), 'budget': limit, 'anteil': round(share, 4), 'stufe': level}


def check(store, provider, now=None):
    """Refuse a request once the month's budget is used up (``ProviderError`` of kind ``budget``)."""
    if store is None:
        return
    current = status(store, provider, now)
    if current['stufe'] == 'gesperrt':
        from core import ProviderError
        from settings import PROVIDERS
        raise ProviderError(f'Monatsbudget für {PROVIDERS[provider]["name"]} ist ausgeschöpft. '
                            'Unter Einstellungen → Budgets erhöhen oder bis zum Monatswechsel warten.',
                            'budget', f'anteil={current["anteil"]}')


def statuses(store, now=None):
    """Status of every provider that has a budget or use in the current month."""
    from settings import PROVIDERS
    found = (status(store, provider, now) for provider in PROVIDERS)
    return [s for s in found if s['anfragen'] or s['budget']['tokens'] or s['budget']['eur']]


def lines(found):
    """German text for ``statuses``."""
    from settings import PROVIDERS
    result = []
    for s in found:
        limit = s['budget']
        tokens_text = f'{s["token"]:,}'.replace(',', '.') + ' Token'
        if limit['tokens']:
            tokens_text += ' von ' + f'{limit["tokens"]:,}'.replace(',', '.')
        cost = f', {s["kosten_eur"]:.2f} €'.replace('.', ',') if limit['price_in'] or limit['price_out'] else ''
        if cost and limit['eur']:
            cost += f' von {limit["eur"]:.2f} €'.replace('.', ',')
        label = {'ok': '', 'warnung': ' · Warnung: mindestens 80 % verbraucht',
                 'gesperrt': ' · gesperrt: Budget ausgeschöpft'}[s['stufe']]
        name = PROVIDERS.get(s['anbieter'], {}).get('name', s['anbieter'])
        result.append(f'{name} {s["monat"]}: {s["anfragen"]} Anfragen, {tokens_text}{cost}{label}')
    return result or ['Kein Budget gesetzt und keine gezählte KI-Nutzung in diesem Monat.']
