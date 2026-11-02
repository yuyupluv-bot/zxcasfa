"""Encode a service account into a private file, never print the private key."""
from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Prepare FIREBASE_SERVICE_ACCOUNT_B64 for BotHost. Output is secret!')
    parser.add_argument('source', type=Path, help='Firebase service-account JSON')
    parser.add_argument('--output', type=Path, default=Path('firebase-secret.b64'))
    args = parser.parse_args()
    raw = args.source.read_bytes()
    data = json.loads(raw)
    if data.get('type') != 'service_account' or not all(data.get(k) for k in ('project_id', 'private_key', 'client_email')):
        parser.error('The input is not a Firebase service-account key (not google-services.json)')
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(base64.b64encode(raw).decode('ascii'))
    print('Secret file created. Copy its contents into FIREBASE_SERVICE_ACCOUNT_B64 in BotHost, then delete the file. Do not upload it to Git or chat.')


if __name__ == '__main__':
    main()
