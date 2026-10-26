"""Rules that set the area (category) and tags of documents (Release 2.0.1, step S2).

A rule has conditions (source type, folder or file pattern, sender domain, file extension,
keyword) and actions (area, tags). All conditions set on a rule must hold. Rules are
evaluated in a fixed order (position, then id): the first matching rule with an area sets
the area, every matching rule contributes its tags.

Rules act on import. Existing documents change only through ``preview`` followed by
``apply``; each applied run is recorded and can be taken back with ``revert``. Manually
written notes are never changed by rules.

Every function takes an open connection from ``store.connect()`` (``?`` placeholders,
rows addressable by column name) and works for SQLite and PostgreSQL.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import PurePath

CONDITIONS = ('source_type', 'path_pattern', 'sender_domain', 'extension', 'keyword')
SOURCE_LABELS = {'file': 'Datei', 'imap': 'IMAP-Postfach', 'graph': 'Microsoft 365',
                 'pst': 'PST-Archiv', 'attachment': 'E-Mail-Anhang'}
MAX_TAGS_PER_RULE = 10
MAX_TAG_LENGTH = 40
MAX_NAME_LENGTH = 80
MAX_TEXT_LENGTH = 200
_DOMAIN = re.compile(r'^[a-z0-9-]+(\.[a-z0-9-]+)+$')
_EXTENSION = re.compile(r'^[a-z0-9]{1,10}$')
_SENDER = re.compile(r'@([A-Za-z0-9.-]+)')
_WILDCARDS = set('*?[')


# --- validation ------------------------------------------------------------------------------
def parse_tags(value):
    """Comma-separated tag names: trimmed, single spaces, duplicates (ignoring case) removed."""
    if isinstance(value, (list, tuple)):
        value = ','.join(value)
    tags, seen = [], set()
    for part in str(value or '').split(','):
        name = ' '.join(part.split())
        if not name:
            continue
        if len(name) > MAX_TAG_LENGTH:
            raise ValueError(f'Ein Tag darf höchstens {MAX_TAG_LENGTH} Zeichen haben.')
        if name.casefold() not in seen:
            seen.add(name.casefold())
            tags.append(name)
    if len(tags) > MAX_TAGS_PER_RULE:
        raise ValueError(f'Eine Regel darf höchstens {MAX_TAGS_PER_RULE} Tags setzen.')
    return tags


def normalize_rule(data):
    """Validated rule fields; raises ValueError with a message for the user."""
    def text(name, limit=MAX_TEXT_LENGTH):
        value = ' '.join(str(data.get(name) or '').split())
        if len(value) > limit:
            raise ValueError(f'Eingabe ist zu lang (höchstens {limit} Zeichen).')
        return value

    rule = {'name': text('name', MAX_NAME_LENGTH), 'enabled': 1 if data.get('enabled', True) else 0,
            'source_type': text('source_type'), 'path_pattern': text('path_pattern').replace('\\', '/'),
            'sender_domain': text('sender_domain').lower().lstrip('@'),
            'extension': text('extension').lower().lstrip('.'), 'keyword': text('keyword'),
            'category': text('category')}
    rule['tags'] = ', '.join(parse_tags(data.get('tags')))
    if not rule['name']:
        raise ValueError('Die Regel braucht einen Namen.')
    if rule['source_type'] and rule['source_type'] not in SOURCE_LABELS:
        raise ValueError('Unbekannter Quellentyp.')
    if rule['sender_domain'] and not _DOMAIN.match(rule['sender_domain']):
        raise ValueError('Absenderdomäne bitte wie "example.org" angeben.')
    if rule['extension'] and not _EXTENSION.match(rule['extension']):
        raise ValueError('Endung bitte wie "pdf" angeben.')
    if not any(rule[name] for name in CONDITIONS):
        raise ValueError('Mindestens eine Bedingung ist erforderlich.')
    if not rule['category'] and not rule['tags']:
        raise ValueError('Die Regel muss einen Bereich oder Tags setzen.')
    return rule


# --- evaluation ------------------------------------------------------------------------------
def sender_domain(content):
    """Domain of the "Von:" line that e-mail documents start with, or ''."""
    for line in (content or '').splitlines()[:12]:
        if line.startswith('Von: '):
            found = _SENDER.findall(line)
            return found[-1].lower().rstrip('.') if found else ''
    return ''


def _pattern_matches(pattern, locator):
    pattern, locator = pattern.lower(), (locator or '').replace('\\', '/').lower()
    if not _WILDCARDS & set(pattern):
        return pattern in locator  # plain text: part of the path or folder name
    return fnmatch.fnmatchcase(locator, pattern)


def matches(rule, doc):
    """True when every condition set on the rule holds for the document context."""
    if rule['source_type'] and rule['source_type'] != doc.get('source_type'):
        return False
    if rule['path_pattern'] and not _pattern_matches(rule['path_pattern'], doc.get('locator')):
        return False
    if rule['sender_domain']:
        domain = sender_domain(doc.get('content'))
        if not (domain == rule['sender_domain'] or domain.endswith('.' + rule['sender_domain'])):
            return False
    if rule['extension']:
        suffix = PurePath(doc.get('filename') or '').suffix.lower().lstrip('.')
        if suffix != rule['extension']:
            return False
    if rule['keyword']:
        text = (doc.get('title') or '') + '\n' + (doc.get('content') or '')
        if rule['keyword'].casefold() not in text.casefold():
            return False
    return True


def evaluate(rules, doc):
    """(area or None, tags) from the enabled rules in their fixed order."""
    category, tags, seen = None, [], set()
    if doc.get('source_type') == 'note':
        return None, []
    for rule in rules:
        if not rule['enabled'] or not matches(rule, doc):
            continue
        if category is None and rule['category']:
            category = rule['category']
        for tag in parse_tags(rule['tags']):
            if tag.casefold() not in seen:
                seen.add(tag.casefold())
                tags.append(tag)
    return category, tags


# --- rule storage ------------------------------------------------------------------------------
def list_rules(db):
    return [dict(row) for row in db.execute(
        'SELECT id,position,name,enabled,source_type,path_pattern,sender_domain,extension,keyword,'
        'category,tags FROM rules ORDER BY position,id')]


def save_rule(db, data, rule_id=None):
    rule = normalize_rule(data)
    values = tuple(rule[name] for name in ('name', 'enabled', *CONDITIONS, 'category', 'tags'))
    if rule_id is None:
        position = db.execute('SELECT COALESCE(MAX(position),0)+1 AS next FROM rules').fetchone()['next']
        row = db.execute('INSERT INTO rules(position,name,enabled,source_type,path_pattern,sender_domain,'
                         'extension,keyword,category,tags) VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id',
                         (position, *values)).fetchone()
        return row['id']
    cursor = db.execute('UPDATE rules SET name=?,enabled=?,source_type=?,path_pattern=?,sender_domain=?,'
                        'extension=?,keyword=?,category=?,tags=? WHERE id=?', (*values, rule_id))
    if cursor.rowcount != 1:
        raise ValueError('Regel wurde nicht gefunden.')
    return rule_id


def delete_rule(db, rule_id):
    db.execute('DELETE FROM rules WHERE id=?', (rule_id,))


def move_rule(db, rule_id, offset):
    """Move a rule one place up (offset -1) or down (+1); positions are renumbered 1..n."""
    order = [row['id'] for row in db.execute('SELECT id FROM rules ORDER BY position,id')]
    if rule_id not in order:
        raise ValueError('Regel wurde nicht gefunden.')
    index = order.index(rule_id)
    target = max(0, min(len(order) - 1, index + offset))
    order.insert(target, order.pop(index))
    for position, current in enumerate(order, start=1):
        db.execute('UPDATE rules SET position=? WHERE id=?', (position, current))


# --- tags ----------------------------------------------------------------------------------
def _tag_id(db, name):
    db.execute('INSERT INTO tags(name,normalized) VALUES(?,?) ON CONFLICT(normalized) DO NOTHING',
               (name, name.casefold()))
    return db.execute('SELECT id FROM tags WHERE normalized=?', (name.casefold(),)).fetchone()['id']


def document_tags(db, document_id):
    return [row['name'] for row in db.execute(
        'SELECT t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id WHERE dt.document_id=? '
        'ORDER BY t.normalized', (document_id,))]


def add_tags(db, document_id, names):
    """Attach tags; returns the names that were not attached before."""
    added = []
    for name in parse_tags(names) if names else []:
        tag_id = _tag_id(db, name)
        if db.execute('SELECT 1 FROM document_tags WHERE document_id=? AND tag_id=?',
                      (document_id, tag_id)).fetchone() is None:
            db.execute('INSERT INTO document_tags(document_id,tag_id) VALUES(?,?)', (document_id, tag_id))
            added.append(name)
    return added


def remove_tags(db, document_id, names):
    for name in names:
        db.execute('DELETE FROM document_tags WHERE document_id=? AND tag_id IN '
                   '(SELECT id FROM tags WHERE normalized=?)', (document_id, name.casefold()))
    db.execute('DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM document_tags)')


def tag_counts(db):
    """[(name, number of documents)] for every tag in use, alphabetically."""
    return [(row['name'], row['documents']) for row in db.execute(
        'SELECT t.name,COUNT(dt.document_id) AS documents FROM tags t JOIN document_tags dt ON dt.tag_id=t.id '
        'GROUP BY t.id,t.name,t.normalized ORDER BY t.normalized')]


def tags_by_document(db):
    result = {}
    for row in db.execute('SELECT dt.document_id,t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id '
                          'ORDER BY t.normalized'):
        result.setdefault(row['document_id'], []).append(row['name'])
    return result


# --- documents -----------------------------------------------------------------------------
def set_category(db, backend, document_id, category):
    """Change a document's area; SQLite also refreshes its full-text entries."""
    if backend == 'sqlite':
        db.execute("INSERT INTO search_index(search_index,rowid,title,category,content) "
                   "SELECT 'delete',c.id,d.title,d.category,c.content FROM chunks c JOIN documents d "
                   "ON d.id=c.document_id WHERE d.id=?", (document_id,))
    db.execute('UPDATE documents SET category=? WHERE id=?', (category, document_id))
    if backend == 'sqlite':
        db.execute('INSERT INTO search_index(rowid,title,category,content) '
                   'SELECT c.id,d.title,d.category,c.content FROM chunks c JOIN documents d '
                   'ON d.id=c.document_id WHERE d.id=?', (document_id,))


def decide_for_import(db, title, content, filename, source):
    """(area or None, tags) for a document about to be imported from ``source``."""
    rules = [rule for rule in list_rules(db) if rule['enabled']]
    if not rules:
        return None, []
    return evaluate(rules, {'title': title, 'content': content, 'filename': filename,
                            'source_type': source[0], 'locator': source[2]})


# --- existing documents: preview, apply, revert ---------------------------------------------
def preview(db):
    """Documents whose area or tags the current rules would change; nothing is written."""
    rules = [rule for rule in list_rules(db) if rule['enabled']]
    if not rules:
        return []
    tags = tags_by_document(db)
    rows = []
    for doc in db.execute('SELECT d.id,d.title,d.category,d.content,d.filename,s.source_type,s.locator '
                          'FROM documents d LEFT JOIN sources s ON s.document_id=d.id ORDER BY d.id'):
        doc = dict(doc)
        category, new_tags = evaluate(rules, doc)
        present = {name.casefold() for name in tags.get(doc['id'], [])}
        new_tags = [tag for tag in new_tags if tag.casefold() not in present]
        new_category = category or doc['category']
        if new_category != doc['category'] or new_tags:
            rows.append({'document_id': doc['id'], 'title': doc['title'], 'category': doc['category'],
                         'new_category': new_category, 'new_tags': new_tags})
    return rows


def apply(db, backend, rows):
    """Apply confirmed preview rows as one run; rows changed since the preview are skipped."""
    run_id = db.execute('INSERT INTO rule_runs(status) VALUES(?) RETURNING id', ('applied',)).fetchone()['id']
    changed = skipped = 0
    for row in rows:
        current = db.execute('SELECT category FROM documents WHERE id=?', (row['document_id'],)).fetchone()
        if current is None or current['category'] != row['category']:
            skipped += 1
            continue
        if row['new_category'] != row['category']:
            set_category(db, backend, row['document_id'], row['new_category'])
        added = add_tags(db, row['document_id'], row['new_tags'])
        if row['new_category'] == row['category'] and not added:
            skipped += 1
            continue
        db.execute('INSERT INTO rule_run_changes(run_id,document_id,previous_category,new_category,added_tags) '
                   'VALUES(?,?,?,?,?)', (run_id, row['document_id'], row['category'], row['new_category'],
                                         json.dumps(added, ensure_ascii=False)))
        changed += 1
    if not changed:
        db.execute('DELETE FROM rule_runs WHERE id=?', (run_id,))
        run_id = None
    return {'run_id': run_id, 'changed': changed, 'skipped': skipped}


def runs(db):
    return [dict(row) for row in db.execute(
        'SELECT r.id,r.status,r.created_at,r.reverted_at,COUNT(c.document_id) AS documents FROM rule_runs r '
        'LEFT JOIN rule_run_changes c ON c.run_id=r.id GROUP BY r.id,r.status,r.created_at,r.reverted_at '
        'ORDER BY r.id DESC')]


def revert(db, backend, run_id):
    """Take a run back completely. Documents whose area was changed again since the run keep
    that newer area (counted as conflicts); the run's tags are removed in any case."""
    run = db.execute('SELECT status FROM rule_runs WHERE id=?', (run_id,)).fetchone()
    if run is None:
        raise ValueError('Lauf wurde nicht gefunden.')
    if run['status'] != 'applied':
        raise ValueError('Lauf wurde bereits zurückgenommen.')
    restored = conflicts = 0
    for change in [dict(row) for row in db.execute(
            'SELECT document_id,previous_category,new_category,added_tags FROM rule_run_changes WHERE run_id=?',
            (run_id,))]:
        current = db.execute('SELECT category FROM documents WHERE id=?', (change['document_id'],)).fetchone()
        if change['previous_category'] != change['new_category']:
            if current['category'] == change['new_category']:
                set_category(db, backend, change['document_id'], change['previous_category'])
            else:
                conflicts += 1
        remove_tags(db, change['document_id'], json.loads(change['added_tags'] or '[]'))
        restored += 1
    db.execute("UPDATE rule_runs SET status='reverted',reverted_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
    return {'restored': restored, 'conflicts': conflicts}
