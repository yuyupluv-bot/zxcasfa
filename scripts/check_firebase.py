"""Check local Firebase initialization; optionally send an explicit test push.

No FCM request occurs without --driver-id. Use only your own test device.
"""
from __future__ import annotations
import argparse
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / '.env')


def main():
    parser = argparse.ArgumentParser(description='Check Firebase credentials or send a test notification to your test driver')
    parser.add_argument('--driver-id', type=int, help='Internal DB user ID (not VK ID). Sends a real new_order notification!')
    args = parser.parse_args()
    from bot.push_service import _firebase, send
    if _firebase() is None:
        raise SystemExit('Firebase configuration failed. Check the credential variable (never paste its value into chat).')
    print('Firebase credentials loaded. This does not prove FCM delivery.')
    if args.driver_id is not None:
        from common.database import session_scope
        with session_scope() as session:
            sent = send(session, args.driver_id, 'new_order')
        print(f'FCM accepted messages: {sent}. Confirm sound/notification on the phone separately.')
        raise SystemExit(0 if sent else 1)


if __name__ == '__main__':
    main()
