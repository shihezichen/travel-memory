import sys, subprocess
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

admin = db.get_or_create_admin()
raw_key, prefix = db.create_api_key(admin['id'], 'companion-write', 'read,write')

subprocess.run(
    ['python3', os.path.join(os.environ.get("SECRETS_VAULT_DIR", ""), "scripts", "secrets_tool.py"),
     'set', f'TRAVEL_COMPANION_KEY={raw_key}'],
    check=True,
)
print('stored OK, prefix =', prefix)
