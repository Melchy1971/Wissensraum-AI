"""Command-line interface for automation and headless use."""
import argparse
import json
from pathlib import Path
from core import Store, AI, load_key, import_document
BASE = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description='Wissensraum: SQL-Wissensmanagement')
    parser.add_argument('--db',type=Path,default=BASE/'data'/'wissen.db')
    commands = parser.add_subparsers(dest='command',required=True)
    commands.add_parser('list')
    imp = commands.add_parser('import')
    imp.add_argument('path',type=Path)
    imp.add_argument('--category',default='Allgemein')
    for name in ('search','ask'):
        sub = commands.add_parser(name)
        sub.add_argument('query')
    summary = commands.add_parser('summarize')
    summary.add_argument('id',type=int)
    backup = commands.add_parser('backup')
    backup.add_argument('target',type=Path)
    args = parser.parse_args()
    try:
        store = Store(args.db)
        if args.command == 'list':
            result = store.list()
        elif args.command == 'import':
            title,text,name,data = import_document(args.path)
            doc_id,created = store.add(title,text,args.category,name,data)
            result = {'id':doc_id,'created':created}
        elif args.command == 'search':
            result = store.search(args.query)
        elif args.command == 'backup':
            store.backup(args.target)
            result = {'backup':str(args.target)}
        else:
            import os
            ai = AI(store,load_key(BASE),os.environ.get('OPENAI_MODEL','gpt-4.1-mini'))
            answer,sources = ai.ask(args.query) if args.command=='ask' else ai.summarize(args.id)
            result = {'answer':answer,'sources':sources}
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except Exception as e:
        parser.exit(1,f'Fehler: {e}\n')

if __name__ == '__main__':
    main()
