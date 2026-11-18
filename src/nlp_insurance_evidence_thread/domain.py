import json
from pathlib import Path

def load_facts(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def summarize(facts):
    decided = {row['cost'] for row in facts['decisions']}
    return [row['id'] for row in facts['costs'] if row['id'] in decided]
